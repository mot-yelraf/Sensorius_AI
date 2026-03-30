"""farmOS telemetry export bridge for Sensorius runtime readings.

This module queues freshly written sensor readings from the data logger and
ships them to farmOS using the built-in `httpx` JSON:API client path. It owns
queueing, token selection, outbound request shaping, retry-oriented worker
behavior, and status snapshots used by the web routes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import threading
from typing import Any

import httpx

from saiUtils import debug_enabled, printDM
from saiSettings import saiSettings

MODULE = "saiFarmOSBridge"
DEBUG = debug_enabled(MODULE)


class saiFarmOSBridge:
    def __init__(self, *, settings, data_logger, supervisor=None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener_registered = False
        self._queue: deque[dict[str, Any]] = deque()
        self._queue_lock = threading.RLock()
        self._token_runtime: str = ""
        self._last_error: str = ""

    def status_snapshot(self) -> dict[str, Any]:
        with self._queue_lock:
            queue_depth = len(self._queue)
        return {
            "enabled": self._is_enabled(),
            "base_url": self._base_url(),
            "verify_tls": self._verify_tls(),
            "log_bundle": self._log_bundle(),
            "queue_depth": queue_depth,
            "has_static_token": bool(self._cfg_str("FarmOS", "ACCESS_TOKEN", "")),
            "has_runtime_token": bool(self._token_runtime),
            "last_error": self._last_error or "",
        }

    def _cfg_bool(self, section: str, key: str, default: bool) -> bool:
        try:
            return bool(self.settings.get_setting(section, key, default))
        except Exception:
            return default

    def _cfg_str(self, section: str, key: str, default: str = "") -> str:
        try:
            return str(self.settings.get_setting(section, key, default) or "").strip()
        except Exception:
            return default

    def _cfg_int(self, section: str, key: str, default: int, *, minimum: int = 1, maximum: int = 100000) -> int:
        try:
            value = int(self.settings.get_setting(section, key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _cfg_float(self, section: str, key: str, default: float, *, minimum: float = 0.1, maximum: float = 300.0) -> float:
        try:
            value = float(self.settings.get_setting(section, key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _is_enabled(self) -> bool:
        return self._cfg_bool("FarmOS", "ENABLED", False)

    def _register_listener_once(self) -> None:
        if self._listener_registered:
            return
        try:
            self.data_logger.add_readings_listener(self._on_readings_written)
            self._listener_registered = True
        except Exception as exc:
            printDM(f"Failed to register readings listener: {exc}", location=MODULE)

    def _on_readings_written(self, sensor_id: str, timestamp_iso: str, values: dict) -> None:
        if not sensor_id or not isinstance(values, dict) or not values:
            return
        item = {
            "sensor_id": str(sensor_id).strip(),
            "timestamp": str(timestamp_iso or "").strip(),
            "values": dict(values),
        }
        self._enqueue(item)

    def _enqueue(self, item: dict[str, Any]) -> None:
        queue_max = self._cfg_int("FarmOS", "QUEUE_MAX", 1000, minimum=10, maximum=50000)
        with self._queue_lock:
            self._queue.append(item)
            while len(self._queue) > queue_max:
                self._queue.popleft()

    def _pop(self) -> dict[str, Any] | None:
        with self._queue_lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def _push_front(self, item: dict[str, Any]) -> None:
        with self._queue_lock:
            self._queue.appendleft(item)

    def _base_url(self) -> str:
        return self._cfg_str("FarmOS", "BASE_URL", "").rstrip("/")

    def _verify_tls(self) -> bool:
        return self._cfg_bool("FarmOS", "VERIFY_TLS", True)

    def _timeout(self) -> float:
        return self._cfg_float("FarmOS", "REQUEST_TIMEOUT_SEC", 10.0, minimum=1.0, maximum=120.0)

    def _log_bundle(self) -> str:
        return self._cfg_str("FarmOS", "LOG_BUNDLE", "observation").lower() or "observation"

    def _format_name(self, sensor_id: str, values: dict) -> str:
        metric_text = ", ".join(f"{k}={v}" for k, v in list(values.items())[:5])
        if not metric_text:
            metric_text = "values=none"
        return f"Sensorius {sensor_id}: {metric_text}"

    def _build_jsonapi_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        sensor_id = str(item.get("sensor_id") or "").strip()
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        timestamp = str(item.get("timestamp") or "").strip()
        if not timestamp:
            timestamp = datetime.now().astimezone().isoformat()
        bundle = self._log_bundle()
        return {
            "data": {
                "type": f"log--{bundle}",
                "attributes": {
                    "name": self._format_name(sensor_id, values),
                    "timestamp": timestamp,
                    "status": "done",
                },
                "meta": {
                    "sensorius": {
                        "sensor_id": sensor_id,
                        "values": values,
                    }
                },
            }
        }

    def _auth_header_value(self) -> str:
        token = self._cfg_str("FarmOS", "ACCESS_TOKEN", "")
        if token:
            return token
        return self._token_runtime

    async def _refresh_token_if_needed(self, client: httpx.AsyncClient) -> None:
        if self._cfg_str("FarmOS", "ACCESS_TOKEN", ""):
            return
        if self._token_runtime:
            return

        base_url = self._base_url()
        client_id = self._cfg_str("FarmOS", "CLIENT_ID", "farm")
        client_secret = self._cfg_str("FarmOS", "CLIENT_SECRET", "")
        username = self._cfg_str("FarmOS", "USERNAME", "")
        password_obf = self._cfg_str("FarmOS", "PASSWORD", "")
        password = saiSettings.deobfuscate_secret(password_obf)
        if not (base_url and client_id and username and password):
            return

        token_url = f"{base_url}/oauth/token"
        form = {
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
        }
        if client_secret:
            form["client_secret"] = client_secret

        try:
            resp = await client.post(token_url, data=form)
            resp.raise_for_status()
            data = resp.json()
            token = str(data.get("access_token") or "").strip()
            if token:
                self._token_runtime = token
                self._last_error = ""
                if DEBUG:
                    printDM("Obtained farmOS OAuth token", location=MODULE)
        except Exception as exc:
            self._last_error = f"token refresh failed: {exc}"
            if DEBUG:
                printDM(self._last_error, location=MODULE)

    async def _post_item(self, client: httpx.AsyncClient, item: dict[str, Any]) -> None:
        base_url = self._base_url()
        if not base_url:
            raise RuntimeError("FarmOS.BASE_URL is empty")

        bundle = self._log_bundle()
        url = f"{base_url}/api/log/{bundle}"
        payload = self._build_jsonapi_payload(item)

        headers = {"Content-Type": "application/vnd.api+json"}
        token = self._auth_header_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 401 and self._token_runtime:
            self._token_runtime = ""
        resp.raise_for_status()

    async def test_connection(self) -> dict[str, Any]:
        """
        Best-effort connectivity and auth test against farmOS.
        """
        base_url = self._base_url()
        if not base_url:
            return {"ok": False, "error": "FarmOS.BASE_URL is empty"}

        timeout_sec = self._timeout()
        verify_tls = self._verify_tls()
        headers = {"Accept": "application/vnd.api+json, application/json"}

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec),
                verify=verify_tls,
            ) as client:
                await self._refresh_token_if_needed(client)
                token = self._auth_header_value()
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                api_url = f"{base_url}/api"
                resp = await client.get(api_url, headers=headers)
                if resp.status_code == 401:
                    self._last_error = "Unauthorized (401)"
                    return {"ok": False, "status_code": 401, "error": self._last_error}
                resp.raise_for_status()

                self._last_error = ""
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "url": api_url,
                }
        except Exception as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error}

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._register_listener_once()

        while True:
            item = None
            try:
                if self.supervisor:
                    self.supervisor.feedthedogs("FarmOS Bridge")

                if not self._is_enabled():
                    await asyncio.sleep(2.0)
                    continue

                timeout_sec = self._timeout()
                verify_tls = self._verify_tls()
                interval_sec = self._cfg_float("FarmOS", "FLUSH_INTERVAL_SEC", 3.0, minimum=0.2, maximum=60.0)

                item = self._pop()
                if not item:
                    await asyncio.sleep(interval_sec)
                    continue

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout_sec),
                    verify=verify_tls,
                ) as client:
                    await self._refresh_token_if_needed(client)
                    await self._post_item(client, item)
                self._last_error = ""

                if DEBUG:
                    sid = item.get("sensor_id", "?")
                    printDM(f"farmOS write ok for {sid}", location=MODULE)

            except Exception as exc:
                self._last_error = str(exc)
                if isinstance(item, dict):
                    self._push_front(item)
                await asyncio.sleep(2.0)
                if DEBUG:
                    printDM(f"farmOS write failed: {exc}", location=MODULE)
