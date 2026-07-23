"""Runtime timezone synchronization for Sensorius and Nodus devices.

The service keeps persisted ``[Time]`` settings aligned with the current IANA
timezone rules, including Standard Time and Daylight Saving Time transitions.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .saiRuntimePaths import resolve_runtime_base_dir
from .saiSensorSettingsManager import SensorSettingsManager
from .saiSettings import saiSettings
from .saiSwitchSettingsManager import SwitchSettingsManager
from .saiUtils import debug_enabled, normalize_hostname_base, printDM

try:
    import tomllib
except Exception:  # pragma: no cover - Python 3.11+ in supported runtimes
    tomllib = None


MODULE = "saiTimeSync"
TASK_NAME = "Time Sync Manager"
TIME_KEYS = ("TZ", "TZ_OFFSET", "TZ_NAME")
DEFAULT_SYNC_INTERVAL_SEC = 3600.0
MIN_SYNC_INTERVAL_SEC = 60.0
DEFAULT_ACK_TIMEOUT_SEC = 5.0
DEFAULT_RESULT_TIMEOUT_SEC = 20.0
TRANSITION_WAKE_MARGIN_SEC = 120.0
DEBUG = debug_enabled(MODULE)


@dataclass
class NodusTimeTarget:
    """Physical Nodus MQTT target and its mirrored system setting ids."""

    hostname: str
    system_ids: set[str] = field(default_factory=set)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _zone_state(tzinfo: ZoneInfo, when_utc: datetime) -> tuple[int, str]:
    local = _as_utc(when_utc).astimezone(tzinfo)
    offset = local.utcoffset() or timedelta(0)
    return int(offset.total_seconds()), str(local.tzname() or getattr(tzinfo, "key", "") or "")


def time_values_for_zone(tz_name: str | None, when_utc: datetime | None = None) -> dict[str, Any] | None:
    """Return current ``Time.*`` values for an IANA timezone key."""

    tz_key = str(tz_name or "").strip()
    if not tz_key:
        return None
    try:
        tzinfo = ZoneInfo(tz_key)
    except Exception:
        return None
    offset_sec, tz_short = _zone_state(tzinfo, _as_utc(when_utc))
    return {
        "TZ": tz_key,
        "TZ_OFFSET": offset_sec,
        "TZ_NAME": tz_short,
    }


def find_next_time_transition(
    tz_name: str | None,
    *,
    start_utc: datetime | None = None,
    horizon_days: int = 370,
) -> datetime | None:
    """Return the next UTC instant where offset/name changes, or ``None``."""

    tz_key = str(tz_name or "").strip()
    if not tz_key:
        return None
    try:
        tzinfo = ZoneInfo(tz_key)
    except Exception:
        return None

    start = _as_utc(start_utc)
    deadline = start + timedelta(days=max(int(horizon_days), 1))
    base_state = _zone_state(tzinfo, start)
    step = timedelta(hours=6)
    prev = start
    probe = start + step

    while probe <= deadline:
        if _zone_state(tzinfo, probe) != base_state:
            lo = prev
            hi = probe
            while (hi - lo) > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if _zone_state(tzinfo, mid) == base_state:
                    lo = mid
                else:
                    hi = mid
            return hi
        prev = probe
        probe += step

    return None


def _settings_get(settings: Any, section: str, key: str, default: Any = None) -> Any:
    try:
        return settings.get_setting(section, key, default)
    except Exception:
        return default


def desired_time_values(settings: Any, when_utc: datetime | None = None) -> dict[str, Any] | None:
    """Resolve the hub timezone and current offset/name from settings."""

    time_tz = str(_settings_get(settings, "Time", "TZ", "") or "").strip()
    astral_tz = str(_settings_get(settings, "Astral", "TIMEZONE", "") or "").strip()
    for candidate in (time_tz, astral_tz):
        values = time_values_for_zone(candidate, when_utc=when_utc)
        if values:
            return values
    return None


def _values_match(current: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    current = current or {}
    for key in TIME_KEYS:
        left = current.get(key)
        right = desired.get(key)
        if key == "TZ_OFFSET":
            try:
                if int(left) != int(right):
                    return False
            except Exception:
                return False
        elif str(left or "") != str(right or ""):
            return False
    return True


def _value_matches(key: str, current: Any, desired: Any) -> bool:
    if key == "TZ_OFFSET":
        try:
            return int(current) == int(desired)
        except Exception:
            return False
    return str(current or "") == str(desired or "")


def _time_patch_confirms_key(patch: dict | None, key: str, value: Any) -> bool:
    if not isinstance(patch, dict):
        return False
    want_key = str(key or "").strip().upper()
    if not want_key:
        return False
    for update in patch.get("updates") or []:
        if not isinstance(update, dict):
            continue
        section = str(update.get("section") or "").strip().lower()
        update_key = str(update.get("key") or "").strip().upper()
        if section == "time" and update_key == want_key:
            return _value_matches(want_key, update.get("value"), value)
    return False


def _settings_time_values(settings: Any) -> dict[str, Any]:
    return {
        "TZ": _settings_get(settings, "Time", "TZ", None),
        "TZ_OFFSET": _settings_get(settings, "Time", "TZ_OFFSET", None),
        "TZ_NAME": _settings_get(settings, "Time", "TZ_NAME", None),
    }


def apply_time_values(settings: Any, desired: dict[str, Any]) -> list[str]:
    """Persist changed local hub ``Time.*`` values and return changed keys."""

    current = _settings_time_values(settings)
    changed = [key for key in TIME_KEYS if not _value_matches(key, current.get(key), desired.get(key))]
    if not changed:
        return []

    updates = [("Time", key, desired[key]) for key in changed]
    if hasattr(settings, "set_many_in_memory") and hasattr(settings, "save_settings"):
        settings.set_many_in_memory(updates)
        settings.save_settings()
        return changed

    for _section, key, value in updates:
        if hasattr(settings, "replace_setting"):
            settings.replace_setting("Time", key, value)
    return changed


def _normalize_topic_host(hostname: str | None) -> str:
    return normalize_hostname_base(str(hostname or "").strip())


def _read_toml(path: Path) -> dict[str, Any]:
    if not tomllib or not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _system_settings_path(system_root: Path, system_id: str) -> Path:
    return system_root / str(system_id or "").strip() / saiSettings.STANDARD_FILENAME


def _read_system_hostname(system_root: Path, system_id: str) -> str:
    doc = _read_toml(_system_settings_path(system_root, system_id))
    net = doc.get("Network") if isinstance(doc.get("Network"), dict) else {}
    return str(net.get("HOSTNAME") or "").strip()


def _read_system_time(system_root: Path, system_id: str) -> dict[str, Any]:
    doc = _read_toml(_system_settings_path(system_root, system_id))
    time_block = doc.get("Time") if isinstance(doc.get("Time"), dict) else {}
    return dict(time_block or {})


def _serial_suffix(device_id: str | None) -> str:
    text = str(device_id or "").strip()
    return text.rsplit("-", 1)[-1] if "-" in text else text


def _is_nodus_sensor(doc: dict[str, Any] | None) -> bool:
    sensor = (doc or {}).get("Sensor") if isinstance((doc or {}).get("Sensor"), dict) else {}
    sensor_type = str(sensor.get("TYPE", "") or "").strip().lower()
    return sensor_type in {"nodus", "picow", "pico2w"}


def _is_nodus_switch(doc: dict[str, Any] | None) -> bool:
    switch = (doc or {}).get("Switch") if isinstance((doc or {}).get("Switch"), dict) else {}
    switch_type = str(switch.get("TYPE", "") or "").strip().lower()
    device = str(switch.get("DEVICE", "") or "").strip().lower()
    return switch_type in {"nodus", "picow", "pico2w"} or device in {"nodus"}


def _local_host_candidates(settings: Any) -> set[str]:
    candidates = {
        str(getattr(settings, "device_id", "") or ""),
        str(_settings_get(settings, "Network", "HOSTNAME", "") or ""),
    }
    try:
        candidates.add(socket.gethostname() or "")
    except Exception:
        pass
    return {_normalize_topic_host(item).lower() for item in candidates if _normalize_topic_host(item)}


def _build_serial_host_index(system_root: Path) -> dict[str, str]:
    serial_to_host: dict[str, str] = {}
    try:
        children = list(system_root.iterdir())
    except Exception:
        return serial_to_host
    for child in children:
        if not child.is_dir() or child.name.startswith(".") or child.name in {"factory", "factory_nodus", "__pycache__"}:
            continue
        host = _read_system_hostname(system_root, child.name)
        if not host or "-" not in host:
            continue
        serial = _serial_suffix(host)
        previous = str(serial_to_host.get(serial) or "").strip()
        if previous and previous.lower().startswith("switch-") and not host.lower().startswith("switch-"):
            serial_to_host[serial] = host
        elif not previous or not host.lower().startswith("switch-"):
            serial_to_host[serial] = host
    return serial_to_host


def _resolve_with_ingest(mqtt_ingest: Any, device_id: str, device_type: str) -> str:
    try:
        resolver = getattr(mqtt_ingest, "resolve_nodus_hostname", None)
        if callable(resolver):
            return str(resolver(device_id, device_type=device_type) or "").strip()
    except Exception:
        return ""
    return ""


def discover_nodus_time_targets(
    *,
    settings: Any,
    mqtt_ingest: Any = None,
    system_base_dir: str | Path | None = None,
    sensor_base_dir: str | Path = "sensor_settings",
    switch_base_dir: str | Path = "switch_settings",
) -> list[NodusTimeTarget]:
    """Discover physical Nodus hosts that should receive ``Time.*`` updates."""

    system_root = resolve_runtime_base_dir(system_base_dir or getattr(settings, "base_dir", None) or saiSettings.DEFAULT_BASE_DIR)
    local_hosts = _local_host_candidates(settings)
    serial_host_index = _build_serial_host_index(system_root)
    targets: dict[str, NodusTimeTarget] = {}

    def add_target(host: str, system_id: str) -> None:
        topic_host = _normalize_topic_host(host)
        if not topic_host or topic_host.lower() in local_hosts:
            return
        target = targets.setdefault(topic_host, NodusTimeTarget(hostname=topic_host))
        if system_id:
            target.system_ids.add(str(system_id).strip())

    sensor_ids: list[str] = []
    try:
        sensor_mgr = SensorSettingsManager(str(sensor_base_dir))
        for sensor_id in (sensor_mgr.list_ids() or []):
            sensor_id = str(sensor_id or "").strip()
            if not sensor_id:
                continue
            try:
                doc = sensor_mgr.load(sensor_id)
            except Exception:
                continue
            if not _is_nodus_sensor(doc):
                continue
            sensor_ids.append(sensor_id)
            host = (
                _resolve_with_ingest(mqtt_ingest, sensor_id, "sensor")
                or _read_system_hostname(system_root, sensor_id)
                or sensor_id
            )
            add_target(host, sensor_id)
    except Exception as exc:
        if DEBUG:
            printDM(f"[time-sync] sensor target discovery skipped: {exc}", location=MODULE)

    try:
        switch_mgr = SwitchSettingsManager(str(switch_base_dir))
        for switch_id in (switch_mgr.list_switches() or []):
            switch_id = str(switch_id or "").strip()
            if not switch_id:
                continue
            try:
                doc = switch_mgr.load(switch_id)
            except Exception:
                continue
            if not _is_nodus_switch(doc):
                continue
            serial = _serial_suffix(switch_id)
            paired_sensor = next((sid for sid in sensor_ids if sid.endswith(f"-{serial}")), "")
            host = (
                _resolve_with_ingest(mqtt_ingest, switch_id, "switch")
                or (serial_host_index.get(serial) if serial else "")
                or (_read_system_hostname(system_root, paired_sensor) if paired_sensor else "")
                or _read_system_hostname(system_root, switch_id)
                or switch_id
            )
            # Combined Nodus sensor+switch devices have one physical system
            # settings shadow keyed by the sensor/host id; the switch id is an
            # MQTT role alias, not an independent system document.
            add_target(host, paired_sensor or switch_id)
    except Exception as exc:
        if DEBUG:
            printDM(f"[time-sync] switch target discovery skipped: {exc}", location=MODULE)

    return sorted(targets.values(), key=lambda item: item.hostname)


def target_shadow_matches(system_root: Path, target: NodusTimeTarget, desired: dict[str, Any]) -> bool:
    """Return True only when all known mirrored system docs already match."""

    if not target.system_ids:
        return False
    saw_doc = False
    for system_id in sorted(target.system_ids):
        path = _system_settings_path(system_root, system_id)
        if not path.exists():
            return False
        saw_doc = True
        if not _values_match(_read_system_time(system_root, system_id), desired):
            return False
    return saw_doc


def target_shadow_key_matches(system_root: Path, target: NodusTimeTarget, key: str, value: Any) -> bool:
    """Return True when every known mirrored system doc already has one Time key."""

    if not target.system_ids:
        return False
    saw_doc = False
    for system_id in sorted(target.system_ids):
        path = _system_settings_path(system_root, system_id)
        if not path.exists():
            return False
        saw_doc = True
        if not _value_matches(key, _read_system_time(system_root, system_id).get(key), value):
            return False
    return saw_doc


def _seed_target_shadow_if_missing(system_root: Path, system_id: str) -> bool:
    path = _system_settings_path(system_root, system_id)
    if path.exists():
        return False
    templates = (
        system_root / "factory_nodus" / f"{saiSettings.STANDARD_FILENAME}.def",
        system_root / "factory" / saiSettings.STANDARD_FILENAME,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for template in templates:
            if template.exists():
                shutil.copy2(template, path)
                return True
    except Exception as exc:
        if DEBUG:
            printDM(f"[time-sync] shadow seed failed for {system_id}: {exc}", location=MODULE)
    return False


def update_target_shadow(
    system_root: Path,
    target: NodusTimeTarget,
    desired: dict[str, Any],
    *,
    keys: tuple[str, ...] = TIME_KEYS,
) -> None:
    """Update local mirrored Nodus system settings after confirmed apply."""

    for system_id in sorted(target.system_ids):
        path = _system_settings_path(system_root, system_id)
        existed_before = path.exists()
        if not existed_before:
            _seed_target_shadow_if_missing(system_root, system_id)
        try:
            mgr = saiSettings(
                apply_live=False,
                make_startup_backup=False,
                base_dir=str(system_root),
                device_id=system_id,
            )
            updates = []
            if not existed_before:
                updates.append(("Network", "HOSTNAME", target.hostname or system_id))
            updates.extend(("Time", key, desired[key]) for key in keys if key in desired)
            if not updates:
                continue
            mgr.set_many_in_memory(updates)
            mgr.save_settings()
        except Exception as exc:
            if DEBUG:
                printDM(f"[time-sync] shadow update failed for {system_id}: {exc}", location=MODULE)


class TimeSyncService:
    """Supervised task that syncs hub and Nodus ``Time.*`` settings."""

    def __init__(
        self,
        *,
        settings: Any,
        mqtt_ingest: Any = None,
        supervisor: Any = None,
        system_base_dir: str | Path | None = None,
        sensor_base_dir: str | Path = "sensor_settings",
        switch_base_dir: str | Path = "switch_settings",
        interval_sec: float | None = None,
    ):
        self.settings = settings
        self.mqtt_ingest = mqtt_ingest
        self.supervisor = supervisor
        self.enabled = _env_bool("SENSORIUS_TIME_SYNC_ENABLED", True)
        self.interval_sec = max(
            MIN_SYNC_INTERVAL_SEC,
            float(interval_sec if interval_sec is not None else _env_float("SENSORIUS_TIME_SYNC_INTERVAL_SEC", DEFAULT_SYNC_INTERVAL_SEC, MIN_SYNC_INTERVAL_SEC)),
        )
        self.ack_timeout_sec = _env_float("SENSORIUS_TIME_SYNC_ACK_TIMEOUT_SEC", DEFAULT_ACK_TIMEOUT_SEC, 1.0)
        self.result_timeout_sec = _env_float("SENSORIUS_TIME_SYNC_RESULT_TIMEOUT_SEC", DEFAULT_RESULT_TIMEOUT_SEC, 1.0)
        self.system_root = resolve_runtime_base_dir(system_base_dir or getattr(settings, "base_dir", None) or saiSettings.DEFAULT_BASE_DIR)
        self.sensor_base_dir = sensor_base_dir
        self.switch_base_dir = switch_base_dir
        self.last_sync: dict[str, Any] = {}

    def _feed_watchdog(self, *, error: bool = False) -> None:
        if self.supervisor and hasattr(self.supervisor, "feedthedogs"):
            self.supervisor.feedthedogs(TASK_NAME, error=error)

    def _report_issue(self, message: str, *, recommend_restart: bool = False) -> None:
        if self.supervisor and hasattr(self.supervisor, "report_issue"):
            self.supervisor.report_issue(TASK_NAME, message, recommend_restart=recommend_restart, issue_type="service_warning")

    async def _push_update(self, target: NodusTimeTarget, key: str, value: Any) -> bool:
        ingest = self.mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_config"):
            return False

        publish_result = ingest.publish_nodus_config(
            target.hostname,
            payload={"updates": [{"section": "Time", "key": key, "value": value}]},
            restart=False,
        )
        if not bool((publish_result or {}).get("ok", False)):
            return False
        message_id = str((publish_result or {}).get("message_id") or "").strip()
        if not message_id:
            return False

        ack = None
        if hasattr(ingest, "wait_for_config_ack"):
            ack = await ingest.wait_for_config_ack(message_id, timeout=self.ack_timeout_sec)
            if isinstance(ack, dict) and not bool(ack.get("accepted", False)):
                return False

        if hasattr(ingest, "wait_for_config_result"):
            result = await ingest.wait_for_config_result(message_id, timeout=self.result_timeout_sec)
            if isinstance(result, dict):
                return result.get("applied") is True

        if hasattr(ingest, "wait_for_nodus_meta_patch"):
            patch = await ingest.wait_for_nodus_meta_patch(message_id, source="config_set", timeout=3.0)
            if _time_patch_confirms_key(patch, key, value):
                return True

        if hasattr(ingest, "wait_for_config_result"):
            return False

        return isinstance(ack, dict) and bool(ack.get("accepted", False))

    async def _push_target_time(self, target: NodusTimeTarget, desired: dict[str, Any]) -> bool:
        for key in TIME_KEYS:
            if target_shadow_key_matches(self.system_root, target, key, desired[key]):
                continue
            self._feed_watchdog()
            ok = await self._push_update(target, key, desired[key])
            self._feed_watchdog(error=not ok)
            if not ok:
                return False
            update_target_shadow(self.system_root, target, desired, keys=(key,))
        return True

    async def sync_once(self, *, when_utc: datetime | None = None, push_nodus: bool = True) -> dict[str, Any]:
        """Run one local/remote time synchronization cycle."""

        desired = desired_time_values(self.settings, when_utc=when_utc)
        if not desired:
            message = "Time sync skipped: configured Time.TZ is missing or not loadable"
            self._report_issue(message, recommend_restart=False)
            self.last_sync = {"ok": False, "error": "timezone_unavailable", "updated": [], "targets": []}
            return dict(self.last_sync)

        local_updated = apply_time_values(self.settings, desired)
        if local_updated:
            printDM(
                f"Updated hub Time settings: TZ={desired['TZ']} TZ_OFFSET={desired['TZ_OFFSET']} TZ_NAME={desired['TZ_NAME']}",
                location=MODULE,
            )

        pushed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        targets: list[NodusTimeTarget] = []
        if push_nodus:
            targets = discover_nodus_time_targets(
                settings=self.settings,
                mqtt_ingest=self.mqtt_ingest,
                system_base_dir=self.system_root,
                sensor_base_dir=self.sensor_base_dir,
                switch_base_dir=self.switch_base_dir,
            )
            for target in targets:
                if target_shadow_matches(self.system_root, target, desired):
                    skipped.append(target.hostname)
                    continue
                ok = await self._push_target_time(target, desired)
                if ok:
                    pushed.append(target.hostname)
                    printDM(f"Synced Time settings to Nodus {target.hostname}", location=MODULE)
                else:
                    failed.append(target.hostname)
                    self._report_issue(f"Time sync failed for Nodus {target.hostname}", recommend_restart=False)

        self.last_sync = {
            "ok": not failed,
            "desired": dict(desired),
            "updated": local_updated,
            "targets": [target.hostname for target in targets],
            "pushed": pushed,
            "skipped": skipped,
            "failed": failed,
        }
        return dict(self.last_sync)

    def _sleep_seconds(self, now_utc: datetime | None = None) -> float:
        desired = desired_time_values(self.settings, when_utc=now_utc)
        tz_key = str((desired or {}).get("TZ") or _settings_get(self.settings, "Time", "TZ", "") or "").strip()
        transition = find_next_time_transition(tz_key, start_utc=now_utc)
        if transition:
            seconds_until = (transition - _as_utc(now_utc)).total_seconds() + TRANSITION_WAKE_MARGIN_SEC
            if seconds_until > 0:
                return max(MIN_SYNC_INTERVAL_SEC, min(self.interval_sec, seconds_until))
        return self.interval_sec

    async def _sleep_with_heartbeat(self, sleep_s: float, heartbeat_every_s: float = 20.0) -> None:
        remaining = max(float(sleep_s), 0.0)
        while remaining > 0.0:
            self._feed_watchdog()
            chunk = min(float(heartbeat_every_s), remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def run(self) -> None:
        if DEBUG:
            printDM(f"Time Sync Manager started (enabled={self.enabled}, interval={self.interval_sec:.0f}s)", location=MODULE)
        while True:
            try:
                self._feed_watchdog()
                if self.enabled:
                    await self.sync_once()
                sleep_s = self._sleep_seconds()
            except Exception as exc:
                self._feed_watchdog(error=True)
                self._report_issue(f"Time sync hit a recoverable error: {exc}", recommend_restart=False)
                printDM(f"[time-sync] loop error: {exc}", location=MODULE)
                sleep_s = MIN_SYNC_INTERVAL_SEC
            await self._sleep_with_heartbeat(sleep_s)
