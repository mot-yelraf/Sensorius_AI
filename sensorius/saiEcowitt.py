"""Discover Ecowitt gateways and poll their LAN APIs read-only.

The service materializes station settings, normalizes observations, and feeds
the shared data logger while remaining restartable by the task supervisor.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .saiSensorSettingsManager import SensorSettingsManager
from .saiUtils import debug_enabled, printDM
from .sensor_modules.station_ecowitt import (
    DEFAULT_POLL_INTERVAL_SEC,
    ECOWITT_DISPLAY_METRICS,
    ECOWITT_DISPLAY_STYLES,
    MAX_POLL_INTERVAL_SEC,
    MIN_POLL_INTERVAL_SEC,
    normalize_ecowitt_livedata,
    normalize_sensor_inventory,
    normalized_gateway_sensor_id,
    rain_reset_hour_from_totals,
    rain_source_from_totals,
)

MODULE = "saiEcowitt"
DEBUG = debug_enabled(MODULE)
TASK_NAME = "Ecowitt Gateway Ingest"
HEARTBEAT_INTERVAL_SEC = 20.0
REQUEST_TIMEOUT_SEC = 5.0
MAX_RESPONSE_BYTES = 512 * 1024


class EcowittError(RuntimeError):
    """Operator-visible Ecowitt validation or protocol error."""


def normalize_gateway_url(value: Any) -> str:
    """Validate and normalize a plain-HTTP gateway base URL."""
    raw = str(value or "").strip()
    if not raw:
        raise EcowittError("Ecowitt gateway URL is required.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise EcowittError("Ecowitt gateway URL is invalid.") from exc
    if parsed.scheme.lower() != "http":
        raise EcowittError("Ecowitt gateway URL must use plain HTTP.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise EcowittError("Ecowitt gateway URL must contain a host and no credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise EcowittError("Enter only the gateway base URL without a path, query, or fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EcowittError("Ecowitt gateway URL contains an invalid port.") from exc
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return urlunsplit(("http", netloc, "", "", ""))


def _inventory_json(inventory: list[dict[str, Any]]) -> str:
    return json.dumps(inventory, separators=(",", ":"), sort_keys=True)


def migrate_ecowitt_display_defaults(
    sensor_id: str,
    *,
    manager: SensorSettingsManager | None = None,
) -> bool:
    """Replace only obsolete Ecowitt-owned display defaults for an existing station."""
    sid = str(sensor_id or "").strip()
    if not sid:
        return False
    mgr = manager or SensorSettingsManager("sensor_settings")
    try:
        doc = mgr.load(sid) or OrderedDict()
    except FileNotFoundError:
        return False
    display = doc.get("Display") if isinstance(doc, dict) else None
    if not isinstance(display, dict) or display.get("METRIC_6") != "Baro-Pressure":
        return False
    display["METRIC_6"] = "Gateway Baro-Pressure"
    mgr.save(sid, doc)
    return True


def ensure_ecowitt_sensor_settings(
    sensor_id: str,
    *,
    inventory: list[dict[str, Any]],
    gateway_model: str = "Ecowitt Gateway",
    manager: SensorSettingsManager | None = None,
) -> None:
    """Materialize the Ecowitt station settings without replacing user display choices."""
    mgr = manager or SensorSettingsManager("sensor_settings")
    try:
        doc = mgr.load(sensor_id) or OrderedDict()
        existed = True
    except FileNotFoundError:
        doc = OrderedDict()
        existed = False
    if not isinstance(doc, OrderedDict):
        doc = OrderedDict(doc)

    sensor = doc.get("Sensor")
    if not isinstance(sensor, dict):
        sensor = OrderedDict()
        doc["Sensor"] = sensor
    sensor.update({
        "TYPE": "station",
        "DEVICE": "ecowitt",
        "SENSOR_ID": sensor_id,
        "STATION_MODEL": str(gateway_model or "Ecowitt Gateway").strip(),
        "INVENTORY_JSON": _inventory_json(inventory),
    })
    sensor.setdefault("LOCATION", "Weather Station")

    display = doc.get("Display")
    if not isinstance(display, dict):
        display = OrderedDict()
        doc["Display"] = display
    if not existed:
        for idx, metric in enumerate(ECOWITT_DISPLAY_METRICS, start=1):
            display[f"METRIC_{idx}"] = metric
        display[SensorSettingsManager.DISPLAY_METRIC_MODE_KEY] = SensorSettingsManager.DISPLAY_METRIC_MODE_PICK6
    elif display.get("METRIC_6") == "Baro-Pressure":
        # Early Ecowitt settings reused the WeeWX default even though GW1100
        # pressure is reported by the gateway's WH25 block.
        display["METRIC_6"] = "Gateway Baro-Pressure"
    styles = display.get("Style")
    if not isinstance(styles, dict):
        styles = OrderedDict()
        display["Style"] = styles
    for idx, style in enumerate(ECOWITT_DISPLAY_STYLES, start=1):
        styles.setdefault(f"METRIC_{idx}", style)
    mgr.save(sensor_id, doc)


class EcowittGatewayIngest:
    """Discover and poll one Ecowitt gateway through its local HTTP API."""

    def __init__(self, *, settings, data_logger, supervisor=None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self._request_lock = asyncio.Lock()
        self._last_rain_day: float | None = None
        self._last_rain_timestamp: str = ""
        self._rain_sensor_id = ""
        self._rain_source = ""
        self._rain_reset_hour = 0
        self._last_rain_config_refresh_mono = 0.0
        self._status: dict[str, Any] = {
            "state": "disabled",
            "label": "Ecowitt integration disabled",
            "last_gateway_success": "",
            "last_accepted_reading": "",
            "last_error": "",
            "inventory": [],
            "gateway_model": "",
            "sensor_id": "",
        }
        self._last_error_log_mono = 0.0
        self._migrated_sensor_id = ""

    def _feed_watchdog(self, *, error: bool = False) -> None:
        if self.supervisor and hasattr(self.supervisor, "feedthedogs"):
            self.supervisor.feedthedogs(TASK_NAME, error=error)

    async def _sleep_with_heartbeat(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            self._feed_watchdog()
            chunk = min(HEARTBEAT_INTERVAL_SEC, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get_setting("Ecowitt", "ENABLED", False, reload_if_changed=True))

    @property
    def gateway_url(self) -> str:
        return str(self.settings.get_setting("Ecowitt", "GATEWAY_URL", "", reload_if_changed=True) or "").strip()

    @property
    def sensor_id(self) -> str:
        return str(self.settings.get_setting("Ecowitt", "SENSOR_ID", "", reload_if_changed=True) or "").strip()

    @property
    def poll_interval_sec(self) -> float:
        try:
            value = float(self.settings.get_setting(
                "Ecowitt", "POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC, reload_if_changed=True
            ) or DEFAULT_POLL_INTERVAL_SEC)
        except Exception:
            value = DEFAULT_POLL_INTERVAL_SEC
        return max(MIN_POLL_INTERVAL_SEC, min(MAX_POLL_INTERVAL_SEC, value))

    async def _get_json(self, client: httpx.AsyncClient, base_url: str, endpoint: str, **params) -> Any:
        try:
            response = await client.get(f"{base_url}/{endpoint}", params=params or None)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise EcowittError(f"Gateway timed out while requesting {endpoint}.") from exc
        except httpx.ConnectError as exc:
            raise EcowittError("Could not connect to the Ecowitt gateway.") from exc
        except httpx.HTTPStatusError as exc:
            raise EcowittError(f"Gateway returned HTTP {exc.response.status_code} for {endpoint}.") from exc
        except httpx.HTTPError as exc:
            raise EcowittError(f"Gateway request failed for {endpoint}.") from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise EcowittError(f"Gateway response for {endpoint} was unexpectedly large.")
        try:
            return response.json()
        except ValueError as exc:
            raise EcowittError(f"Gateway returned invalid JSON for {endpoint}.") from exc

    async def discover(self, gateway_url: Any) -> dict[str, Any]:
        """Validate a gateway and return identity, inventory, and live-data metadata."""
        base_url = normalize_gateway_url(gateway_url)
        async with self._request_lock:
            timeout = httpx.Timeout(REQUEST_TIMEOUT_SEC)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                version = await self._get_json(client, base_url, "get_version")
                network = await self._get_json(client, base_url, "get_network_info")
                page1 = await self._get_json(client, base_url, "get_sensors_info", page=1)
                page2 = await self._get_json(client, base_url, "get_sensors_info", page=2)
                live = await self._get_json(client, base_url, "get_livedata_info")
                rain_totals = await self._get_json(client, base_url, "get_rain_totals")

        if not isinstance(version, dict) or not isinstance(network, dict) or not isinstance(live, dict):
            raise EcowittError("Gateway response schema is not supported.")
        platform_name = str(version.get("platform", "") or "").strip().lower()
        version_text = str(version.get("version", "") or "").strip()
        if platform_name and platform_name != "ecowitt":
            raise EcowittError("The device does not identify itself as an Ecowitt gateway.")
        sensor_id = normalized_gateway_sensor_id(network.get("mac"))
        inventory = normalize_sensor_inventory([page1, page2])
        source = rain_source_from_totals(rain_totals if isinstance(rain_totals, dict) else {})
        reset_hour = rain_reset_hour_from_totals(rain_totals if isinstance(rain_totals, dict) else {})
        self._rain_source = source
        self._rain_reset_hour = reset_hour
        self._last_rain_config_refresh_mono = time.monotonic()
        live_sections = {key for key, value in live.items() if isinstance(value, list) and bool(value)}
        for sensor in inventory:
            sensor["reporting"] = self._sensor_reporting(sensor, live_sections)
        values = normalize_ecowitt_livedata(live, rain_source=source)
        now = datetime.now(timezone.utc).isoformat()
        result = {
            "ok": True,
            "gateway_url": base_url,
            "sensor_id": sensor_id,
            "gateway_model": version_text or "Ecowitt Gateway",
            "firmware": version_text,
            "inventory": inventory,
            "rain_source": source,
            "rain_reset_hour": reset_hour,
            "live_metric_count": len(values),
            "live_metrics": sorted(values),
        }
        self._status.update({
            "state": "online",
            "label": "Ecowitt gateway reachable",
            "last_gateway_success": now,
            "last_error": "",
            "inventory": inventory,
            "gateway_model": result["gateway_model"],
            "sensor_id": sensor_id,
            "rain_source": source,
            "rain_reset_hour": reset_hour,
            "live_metrics": result["live_metrics"],
        })
        return result

    @staticmethod
    def _sensor_reporting(sensor: dict[str, Any], live_sections: set[str]) -> bool:
        try:
            sensor_type = int(str(sensor.get("type", "") or "-1"))
        except ValueError:
            sensor_type = -1
        if sensor_type in range(6, 14):
            return "ch_aisle" in live_sections
        if sensor_type in list(range(14, 22)) + list(range(58, 66)):
            return "ch_soil" in live_sections
        if sensor_type in range(22, 26):
            return "ch_pm25" in live_sections
        if sensor_type == 26:
            return "lightning" in live_sections
        if sensor_type in range(27, 31):
            return "ch_leak" in live_sections
        if sensor_type in range(31, 39):
            return "ch_temp" in live_sections
        if sensor_type == 39:
            return "co2" in live_sections
        if sensor_type in range(40, 48):
            return "ch_leaf" in live_sections
        if sensor_type in range(66, 70):
            return "ch_lds" in live_sections
        return bool(live_sections.intersection({"common_list", "rain", "piezoRain", "wh25", "ch_ec"}))

    def save_configuration(self, discovery: dict[str, Any], poll_interval_sec: Any) -> None:
        """Persist a successfully discovered gateway and materialize its station."""
        try:
            interval = int(poll_interval_sec)
        except Exception as exc:
            raise EcowittError("Retrieval interval must be a whole number of seconds.") from exc
        if interval < MIN_POLL_INTERVAL_SEC or interval > MAX_POLL_INTERVAL_SEC:
            raise EcowittError(
                f"Retrieval interval must be between {MIN_POLL_INTERVAL_SEC} and {MAX_POLL_INTERVAL_SEC} seconds."
            )
        sensor_id = str(discovery.get("sensor_id", "") or "").strip()
        inventory = discovery.get("inventory") if isinstance(discovery.get("inventory"), list) else []
        ensure_ecowitt_sensor_settings(
            sensor_id,
            inventory=inventory,
            gateway_model=str(discovery.get("gateway_model", "") or "Ecowitt Gateway"),
        )
        self.settings.set_many_in_memory([
            ("Ecowitt", "ENABLED", True),
            ("Ecowitt", "GATEWAY_URL", str(discovery.get("gateway_url", "") or "")),
            ("Ecowitt", "POLL_INTERVAL_SEC", interval),
            ("Ecowitt", "SENSOR_ID", sensor_id),
            ("Ecowitt", "INVENTORY_JSON", _inventory_json(inventory)),
            ("Ecowitt", "RAIN_SOURCE", str(discovery.get("rain_source", "traditional") or "traditional")),
            ("Ecowitt", "RAIN_RESET_HOUR", int(discovery.get("rain_reset_hour", 0) or 0)),
        ])
        self.settings.save_settings()
        self._status.update({"state": "online", "label": "Ecowitt integration enabled"})

    def disable(self) -> None:
        """Disable polling while preserving station settings and historical readings."""
        self.settings.replace_setting("Ecowitt", "ENABLED", False)
        self._status.update({"state": "disabled", "label": "Ecowitt integration disabled"})

    def status(self) -> dict[str, Any]:
        """Return a safe runtime/configuration snapshot for the web UI."""
        result = dict(self._status)
        result.update({
            "enabled": self.enabled,
            "gateway_url": self.gateway_url,
            "poll_interval_sec": self.poll_interval_sec,
            "sensor_id": self.sensor_id or result.get("sensor_id", ""),
        })
        if not result.get("inventory"):
            try:
                stored = self.settings.get_setting("Ecowitt", "INVENTORY_JSON", "[]", reload_if_changed=True)
                parsed = json.loads(str(stored or "[]"))
                if isinstance(parsed, list):
                    result["inventory"] = parsed
            except Exception:
                pass
        if not result["enabled"]:
            result.update({"state": "disabled", "label": "Ecowitt integration disabled"})
        return result

    def _restore_rain_checkpoint(self, sensor_id: str) -> None:
        if self._rain_sensor_id == sensor_id:
            return
        self._rain_sensor_id = sensor_id
        self._last_rain_day = None
        self._last_rain_timestamp = ""
        try:
            latest = self.data_logger.get_latest_values(sensor_id) or {}
            if latest.get("Rain Day") is not None:
                self._last_rain_day = float(latest["Rain Day"])
                self._last_rain_timestamp = str(self.data_logger.get_latest_timestamp(sensor_id) or "")
        except Exception:
            pass

    def _add_interval_rain(self, values: dict[str, float], sensor_id: str) -> None:
        current = values.get("Rain Day")
        if current is None:
            return
        self._restore_rain_checkpoint(sensor_id)
        local_tz = getattr(self.data_logger, "local_tz", None) or timezone.utc
        now = datetime.now(local_tz)
        previous = self._last_rain_day
        previous_timestamp = self._last_rain_timestamp
        self._last_rain_day = float(current)
        self._last_rain_timestamp = now.isoformat()
        if previous is None:
            return
        if current >= previous:
            values["Rain"] = round(float(current) - previous, 3)
            return
        crossed_reset = False
        try:
            prior_dt = datetime.fromisoformat(previous_timestamp)
            if prior_dt.tzinfo is None:
                prior_dt = prior_dt.replace(tzinfo=now.tzinfo)
            prior_local = prior_dt.astimezone(now.tzinfo)
            candidate = prior_local.replace(hour=self._rain_reset_hour, minute=0, second=0, microsecond=0)
            if candidate <= prior_local:
                candidate += timedelta(days=1)
            crossed_reset = candidate <= now
        except Exception:
            pass
        values["Rain"] = round(float(current), 3) if crossed_reset else 0.0

    async def poll_once(self) -> bool:
        """Fetch and store one live reading from the configured gateway."""
        base_url = normalize_gateway_url(self.gateway_url)
        sensor_id = self.sensor_id
        if not sensor_id:
            raise EcowittError("Ecowitt gateway must be discovered before polling.")
        async with self._request_lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SEC), follow_redirects=False) as client:
                live = await self._get_json(client, base_url, "get_livedata_info")
                refresh_rain_config = (
                    self._rain_source not in {"none", "traditional", "piezo"}
                    or time.monotonic() - self._last_rain_config_refresh_mono >= 86400.0
                )
                if refresh_rain_config:
                    rain_totals = await self._get_json(client, base_url, "get_rain_totals")
                    self._rain_source = rain_source_from_totals(rain_totals)
                    self._rain_reset_hour = rain_reset_hour_from_totals(rain_totals)
                    self._last_rain_config_refresh_mono = time.monotonic()
        if not isinstance(live, dict):
            raise EcowittError("Gateway live-data response schema is not supported.")
        values = normalize_ecowitt_livedata(live, rain_source=self._rain_source)
        if not values:
            raise EcowittError("Gateway returned no supported live sensor values.")
        self._add_interval_rain(values, sensor_id)
        await asyncio.to_thread(self.data_logger.log_readings, None, sensor_id, values)
        now = datetime.now(timezone.utc).isoformat()
        self._status.update({
            "state": "online",
            "label": "Receiving Ecowitt weather data",
            "last_gateway_success": now,
            "last_accepted_reading": now,
            "last_error": "",
            "sensor_id": sensor_id,
            "live_metric_count": len(values),
            "live_metrics": sorted(values),
            "rain_source": self._rain_source,
        })
        return True

    async def run(self) -> None:
        """Poll forever while allowing live settings changes."""
        while True:
            self._feed_watchdog()
            if not self.enabled or not self.gateway_url:
                self._status.update({"state": "disabled", "label": "Ecowitt integration disabled"})
                await self._sleep_with_heartbeat(self.poll_interval_sec)
                continue
            try:
                sensor_id = self.sensor_id
                if sensor_id and self._migrated_sensor_id != sensor_id:
                    await asyncio.to_thread(migrate_ecowitt_display_defaults, sensor_id)
                    self._migrated_sensor_id = sensor_id
                configured_source = str(self._status.get("rain_source", "") or "")
                if configured_source not in {"none", "traditional", "piezo"}:
                    configured_source = str(
                        self.settings.get_setting("Ecowitt", "RAIN_SOURCE", "", reload_if_changed=True) or ""
                    )
                self._rain_source = configured_source if configured_source in {"none", "traditional", "piezo"} else ""
                try:
                    self._rain_reset_hour = max(0, min(23, int(
                        self.settings.get_setting("Ecowitt", "RAIN_RESET_HOUR", 0, reload_if_changed=True) or 0
                    )))
                except Exception:
                    self._rain_reset_hour = 0
                await self.poll_once()
                self._feed_watchdog()
            except Exception as exc:
                self._feed_watchdog(error=True)
                message = str(exc) if isinstance(exc, EcowittError) else "Ecowitt polling failed."
                self._status.update({"state": "offline", "label": "Ecowitt gateway unavailable", "last_error": message})
                now_mono = time.monotonic()
                if now_mono - self._last_error_log_mono >= 300.0:
                    self._last_error_log_mono = now_mono
                    printDM(message, location=MODULE, level="warning")
            await self._sleep_with_heartbeat(self.poll_interval_sec)
