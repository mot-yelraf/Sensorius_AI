"""Apply paced Nodus configuration changes and correlated device restarts.

Routes supply hostname resolution, the shared host lock, and the current transport.
This service owns command sequencing and response interpretation while MQTT ingest
retains publication, receipt caches, and metadata-shadow persistence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from .saiUtils import printDM
from .saiWebContracts import HostLock, HostnameResolver, SystemSettingsReader

MODULE = "saiWebRoutes"


class NodusCommandTransport(Protocol):
    """Provide the required correlated configuration operations."""

    def publish_nodus_config(self, device_id: str, *, payload: dict,
                             restart: bool = False) -> dict: ...

    async def wait_for_config_ack(self, message_id: str, timeout: float = 3.0) -> dict | None: ...

    async def wait_for_config_result(self, message_id: str, timeout: float = 8.0) -> dict | None: ...


class NodusDeviceCommands:
    """Sequence device operations without depending on FastAPI or route closures."""

    def __init__(self, *, get_ingest: Callable[[], NodusCommandTransport | None],
                 resolve_hostname: HostnameResolver, get_host_lock: HostLock,
                 ack_timeout: float, result_timeout: float, debug: bool = False) -> None:
        """Bind providers without publishing commands or reading settings."""
        self._get_ingest = get_ingest
        self._resolve_hostname = resolve_hostname
        self._get_host_lock = get_host_lock
        self.ack_timeout = ack_timeout
        self.result_timeout = result_timeout
        self.debug = debug

    async def push_setting(
        self,
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        section: str,
        key: str,
        value: Any,
        sensor_file_name: str | None,
        system_mgr: SystemSettingsReader | None = None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        """Apply one setting through the shared ordered batch workflow."""
        return await self.push_settings_batch(
            device_id=device_id,
            device_type=device_type,
            setting_file_key=setting_file_key,
            updates=[(section, key, value)],
            sensor_file_name=sensor_file_name,
            system_mgr=system_mgr,
            system_root=system_root,
            ip_hint=ip_hint,
            sys_host_index=sys_host_index,
        )


    async def _publish_config_update(
        self,
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        update_payload: dict[str, Any],
    ) -> bool:
        """Wait for acceptance and completed application before sending the next key."""
        def _config_result_applied(result_payload: dict[str, Any] | None) -> bool:
            # Gate the next queued update on Nodus confirming the apply completed.
            return isinstance(result_payload, dict) and result_payload.get("applied") is True

        ingest = self._get_ingest()
        if not ingest or not hasattr(ingest, "publish_nodus_config"):
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] mqtt_ingest unavailable; skipping publish",
                    location=MODULE,
                )
            return False

        if self.debug:
            client_desc = ""
            try:
                if hasattr(ingest, "_describe_publish_client"):
                    client_desc = str(ingest._describe_publish_client(use_ha_client=False) or "")
            except Exception:
                client_desc = ""
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] preparing publish target={target_device} topic=nodus/{target_device}/config/set update={str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()} {client_desc}".strip(),
                location=MODULE,
            )

        publish_result = ingest.publish_nodus_config(
            target_device,
            payload={"updates": [update_payload]},
            restart=False,
        )
        if not bool(publish_result.get("ok", False)):
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] failed to publish config/set to nodus/{target_device}/config/set",
                    location=MODULE,
                )
            return False

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_config_ack(message_id, timeout=self.ack_timeout)
        if not ack or not bool(ack.get("accepted", False)):
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config ack failed for nodus/{target_device}/config/set message_id={message_id}: {ack}",
                    location=MODULE,
                )
            return False

        result = await ingest.wait_for_config_result(message_id, timeout=self.result_timeout)
        if result is None:
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config result timeout for nodus/{target_device}/config/set message_id={message_id}",
                    location=MODULE,
                )
            return False
        if not _config_result_applied(result):
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config apply failed for nodus/{target_device}/config/set message_id={message_id}: {result}",
                    location=MODULE,
                )
            return False

        if self.debug:
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] OK via MQTT nodus/{target_device}/config/set: {str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()}",
                location=MODULE,
            )
        return True


    async def request_restart(
        self,
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        restart_mode: str = "soft",
    ) -> tuple[bool, str]:
        """Request a restart and validate its correlated acceptance and result."""
        def _truthy(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value or "").strip().lower() in {"1", "true", "yes", "on", "ok", "accepted", "applied", "rebooting", "restarted"}

        def _restart_ack_accepted(ack_payload: dict[str, Any] | None) -> bool:
            if not isinstance(ack_payload, dict):
                return False
            if "accepted" in ack_payload:
                return _truthy(ack_payload.get("accepted"))
            if "ok" in ack_payload:
                return _truthy(ack_payload.get("ok"))
            return True

        def _restart_result_applied(result_payload: dict[str, Any] | None) -> bool:
            if not isinstance(result_payload, dict):
                return False
            for key in ("applied", "restarted", "rebooting", "accepted", "ok", "success"):
                if key in result_payload and _truthy(result_payload.get(key)):
                    return True
            return False

        ingest = self._get_ingest()
        if not ingest:
            return False, "MQTT ingest unavailable"

        message_id = f"rst-{int(time.time())}-{uuid4().hex[:8]}"
        publish_result: dict[str, Any] | None = None

        if hasattr(ingest, "publish_nodus_restart"):
            publish_result = ingest.publish_nodus_restart(
                target_device,
                restart_mode=restart_mode,
                message_id=message_id,
            )
        elif hasattr(ingest, "publish_json"):
            topic = f"nodus/{target_device}/config/set"
            envelope = {
                "message_id": message_id,
                "payload": {},
                "restart": True,
                "restart_mode": restart_mode,
            }
            ok = bool(ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
            publish_result = {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}
        else:
            return False, "MQTT publish unavailable"

        if not bool((publish_result or {}).get("ok", False)):
            return False, "Failed to publish restart request"

        ack = None
        if hasattr(ingest, "wait_for_config_ack"):
            ack = await ingest.wait_for_config_ack(message_id, timeout=self.ack_timeout)
        if not isinstance(ack, dict):
            return False, "Restart request was not acknowledged by the device"
        if not _restart_ack_accepted(ack):
            error_text = str(ack.get("error") or "").strip()
            return False, error_text or "Restart request was rejected by the device"

        result = None
        if hasattr(ingest, "wait_for_config_result"):
            result = await ingest.wait_for_config_result(message_id, timeout=self.result_timeout)
        if result is None:
            return False, "Restart request timed out waiting for device result"
        if not _restart_result_applied(result):
            error_text = str((result or {}).get("error") or "").strip()
            return False, error_text or "Restart request was not applied by the device"

        if self.debug:
            printDM(
                f"[restart_nodus_device:{device_type}:{device_id}] target={target_device} message_id={message_id} ack={ack} result={result}",
                location=MODULE,
            )
        return True, "Device restarting..."


    async def push_settings_batch(
        self,
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        updates: list[tuple[str, str, Any]],
        sensor_file_name: str | None,
        system_mgr: SystemSettingsReader | None = None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        """Serialize settings updates per physical host and stop on the first failure."""
        hostname = self._resolve_hostname(
            device_id, system_mgr, system_root,
            device_type=device_type, sys_host_index=sys_host_index
        )
        target_device = str(hostname or device_id or "").strip()
        if not target_device:
            if self.debug:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] no MQTT target device resolved; skipping publish",
                    location=MODULE,
                )
            return False

        updates_payload: list[dict[str, Any]] = []
        for section, key, value in (updates or []):
            item = {"section": section, "key": key, "value": value}
            if setting_file_key == "sensor":
                item["sensor_id"] = str(device_id or "").strip()
                if sensor_file_name:
                    item["name"] = sensor_file_name
            elif setting_file_key == "switch":
                item["name"] = "switch.toml"
            updates_payload.append(item)
        if not updates_payload:
            return True
        # Serialize config writes per physical Nodus host. This preserves the
        # one-key-at-a-time behavior even when multiple routes/tasks target the
        # same device concurrently (for example paired sensor + switch updates).
        async with self._get_host_lock(target_device):
            for update_payload in updates_payload:
                ok = await self._publish_config_update(
                    target_device=target_device,
                    device_id=device_id,
                    device_type=device_type,
                    update_payload=update_payload,
                )
                if not ok:
                    return False
        return True
