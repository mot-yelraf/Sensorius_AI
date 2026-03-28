"""Switch controller for GPIO and MQTT-backed relay devices.

Flow:
1) saiSwitchSettingsManager loads TOML settings per switch.
2) saiSwitchFactory creates the concrete relay/MQTT backend.
3) saiSwitch manages state, applies min on/off timing, logs events, and
   publishes MQTT state/event updates for the rest of the system.
"""

import json
import time
import random
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from saiUtils import printDM, debug_enabled, get_timestamp
from saiSwitchFactory import create_switch
from saiMQTTClient import get_mqtt_client
from saiDataLogger import saiDataLogger
try:
    import requests
except Exception:
    requests = None
try:
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun
except Exception:
    LocationInfo = None
    _astral_sun = None
try:
    # canonical helper that knows how to combine switch_id + label + channel_id
    from saiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None

MODULE = "saiSwitch"
DEBUG = debug_enabled(MODULE)
REMOTE_SWITCH_TYPES = {"picow", "pico2w", "nodus", "remote", "mqtt"}


def _switch_type_from_settings(switch_settings) -> str:
    try:
        if hasattr(switch_settings, "get_setting"):
            return str(
                switch_settings.get_setting("Switch", "TYPE", "")
                or ""
            ).strip().lower()
    except Exception:
        pass
    try:
        sw = (switch_settings or {}).get("Switch", {}) or {}
        return str(sw.get("TYPE", "") or "").strip().lower()
    except Exception:
        return ""


def is_remote_switch_settings(switch_settings) -> bool:
    return _switch_type_from_settings(switch_settings) in REMOTE_SWITCH_TYPES

class SwitchController:
    def __init__(self, switch_settings=None, supervisor=None, sensor=None, data_logger=None):
        self.supervisor = supervisor
        self.sensor = sensor
        self.settings = switch_settings or {}
        self.is_present = False
        self.data_logger = data_logger or saiDataLogger()

        # State & policy
        self.last_state = {}
        self.override_script = {}
        self.last_set_time = {}
        self.auto_off_seconds = {}
        self.auto_off_deadline = {}
        self.min_on_time = 5
        self.min_off_time = 5
        self._advanced_delay_due = {}
        self._advanced_revert_cooldown = set()
        self._advanced_active_actions = {}
        self._astral_location_cache = {"value": None, "expires_at": 0.0}

        # Settings accessor that works with either wrapper or dict
        try:
            get = self.settings.get_setting
        except AttributeError:
            def get(section, key, default=None):
                return (self.settings or {}).get(section, {}).get(key, default)

        sw = self._switch_block()
        sw_type = str(sw.get("TYPE", "") or "").strip().lower()
        has_en_keys = (
            ("SWITCH_1_ENABLE_PIN" in sw) or ("SWITCH_2_ENABLE_PIN" in sw)
            or ("SWITCH_1_EN" in sw) or ("SWITCH_2_EN" in sw)
        )

        def _enable_field_value(sw_map: dict, idx: int):
            return sw_map.get(f"SWITCH_{idx}_ENABLE_PIN", sw_map.get(f"SWITCH_{idx}_EN", ""))

        def _has_install_marker(val) -> bool:
            if val is None:
                return False
            if isinstance(val, bool):
                return val
            return str(val).strip() != ""

        self.device     = get("Switch", "DEVICE",     sw.get("DEVICE", "switch"))
        self.serial_num = get("Switch", "DEVICE_SERIAL_NUM", sw.get("DEVICE_SERIAL_NUM", "unknown"))
        self.switch_id  = get("Switch", "SWITCH_DEVICE_ID",  sw.get("SWITCH_DEVICE_ID", "switch"))
        self.location   = get("Switch", "SWITCH_LOCATION",   sw.get("SWITCH_LOCATION", "Unknown"))
        self.topic      = f"switch/{self.switch_id}/event"
        # map human label -> channel_id (SWITCH_n_ID); used for DB identity
        self.channel_id_for_label: dict[str, str | None] = {}
        
        # If this is a Pi GPIO switch, ensure settings match detected hardware/template
        try:
            sw_type = str(sw.get("TYPE", "pi")).strip().lower()
            if sw_type not in REMOTE_SWITCH_TYPES:
                from saiSwitchFactory import ensure_switch_settings_for_host
                refreshed = ensure_switch_settings_for_host(self.switch_id, self.location)
                if isinstance(self.settings, dict):
                    self.settings = refreshed or self.settings
                elif hasattr(self.settings, "settings") and isinstance(getattr(self.settings, "settings"), dict):
                    self.settings.settings = refreshed or self.settings.settings
                sw = self._switch_block()
        except Exception as e:
            if DEBUG:
                printDM(f"ensure_switch_settings_for_host error: {e}", location=MODULE)


        # Bind MQTT client to the switch identity (safe even if factory ignores it)
        self.mqtt = get_mqtt_client(self.switch_id)

        # ---- create a single multi-channel switch instance ----
        self.switch = create_switch(settings=self.settings, mqtt_client=self.mqtt)
        if isinstance(getattr(self, "switch", None), str):
            printDM(f"[{self.switch_id}] BUG: self.switch is a str = {self.switch!r}", location="saiSwitch")

        self.is_present = bool(getattr(self.switch, "is_present", False))
        if DEBUG:
            try:
                ch_count = len(self.switches)
            except Exception:
                ch_count = 0
            printDM(f"SwitchController created: present={self.is_present} channels={ch_count}", location=MODULE)

        # ---- Gather labels & persisted states from [Switch] ----
        labels = []
        for k, v in sw.items():
            if not k.startswith("SWITCH_") or not k.endswith("_LABEL"):
                continue
            parts = k.split("_")
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            n = int(parts[1])
            label = str(v).strip()
            if not label:
                continue
            if sw_type in ("picow", "pico2w") or has_en_keys:
                if not _has_install_marker(_enable_field_value(sw, n)):
                    continue

            labels.append(label)

            # persisted state & override flags (optional)
            state_key    = f"SWITCH_{n}_LAST_STATE"
            override_key = f"SWITCH_{n}_OVERRIDE_SCRIPT"
            self.last_state[label] = bool(sw.get(state_key, False))
            self.override_script[label] = bool(sw.get(override_key, False))
            self.last_set_time.setdefault(label, 0.0)
            self.auto_off_seconds.setdefault(label, 0)
            self.auto_off_deadline.setdefault(label, None)

            # channel ID from new schema (may be empty → None)
            chan_id_key = f"SWITCH_{n}_CHANNEL_ID"
            chan_id = str(sw.get(chan_id_key, "") or "").strip() or None
            self.channel_id_for_label[label] = chan_id

        # Intersect with device-reported channels if available
        try:
            device_labels = set(self.switch.get_switch_names())
            labels = [lb for lb in labels if lb in device_labels] or list(device_labels)
        except Exception:
            pass

        # ---- register each label in DB switch_ids via switch_key ---------
        try:
            for label in labels:
                self.data_logger.upsert_switch_identity(
                    switch_key=self._switch_key(label),
                    switch_id=self.switch_id,
                    label=label,
                    location=self.location,
                )
        except Exception as e:
            printDM(f"upsert_switch_identity init error: {e}", location=MODULE)

        # ---- Apply persisted states through the device object ----
        for label in labels:
            try:
                desired = bool(self.last_state.get(label, False))
                self.switch.set_state(label, desired)
                self.last_set_time.setdefault(label, 0.0)
                if DEBUG:
                    printDM(
                        f"label:{label}, last_state:{desired}, override_script:{self.override_script.get(label, False)}",
                        location=f"{MODULE}:__init__"
                    )
            except Exception as e:
                printDM(f"Init state apply failed for '{label}': {e}", location=MODULE)

        # ---- Seed initial sensor values (if present) ----
        self.values = None
        if self.sensor:
            try:
                raw_values, *_ = self.sensor.current_data_set()
                self.values = {self.sensor.sensor_id: raw_values}
            except Exception as e:
                printDM(f"Initial trigger evaluation error: {e}", location=MODULE)

        try:
            ch_count = len(self.switch.get_switch_names()) if self.is_present else 0
        except Exception:
            ch_count = 0
        printDM(f"SwitchController created: present={self.is_present}, channels={ch_count}", location=MODULE)

    # ---------- helpers: switch_key & db convenience --------------------------
    def reload_settings(self, new_settings):
        self.settings = new_settings

    def _switch_block(self) -> dict:
        try:
            if hasattr(self.settings, "get"):
                return self.settings.get("Switch", {}) or {}
        except Exception:
            pass
        return (self.settings or {}).get("Switch", {}) or {}

    def get_switch_names(self) -> list[str]:
        try:
            return list(self.switch.get_switch_names())
        except Exception:
            return list(self.last_state.keys())
        
    def _switch_key(self, name: str) -> str:
        """
        Canonical DB identity for a channel:
          "<channel_id>::<label>"
        """
        chan_id = None
        try:
            chan_id = (self.channel_id_for_label or {}).get(name)
        except Exception:
            chan_id = None
        chan_id = str(chan_id or "").strip()

        if _build_switch_key:
            return _build_switch_key(chan_id, name)
        return f"{chan_id}::{name}"

    def get_latest_state_from_db(self, label: str) -> str | None:
        """Convenience: return 'On'/'Off'/None for a given label using sw_events."""
        try:
            return self.data_logger.get_latest_switch_state(self._switch_key(label))
        except Exception as e:
            printDM(f"get_latest_state_from_db error({label}): {e}", location=MODULE)
            return None

    def get_last_events_from_db(self, label: str, limit: int = 5) -> list[tuple[str, str]]:
        """Convenience: return [(state, timestamp), ...] for a label using sw_events."""
        try:
            return self.data_logger.get_last_switch_events(self._switch_key(label), limit=limit)
        except Exception as e:
            printDM(f"get_last_events_from_db error({label}): {e}", location=MODULE)
            return []

    @property
    def switches(self) -> list[str]:
        try:
            if hasattr(self, "switch") and hasattr(self.switch, "get_switch_names"):
                return list(self.switch.get_switch_names())
            if hasattr(self, "switch") and hasattr(self.switch, "channels"):
                return [c.get("name", f"Relay {c.get('n','?')}") for c in self.switch.channels]
        except Exception:
            pass
        return []

    def _time_in_window(self, start: str, end: str, now_str: str) -> bool:
        # Inclusive start, exclusive end: [start, end)
        if not start and not end:
            return True
        s = (start or "00:00")
        e = (end or "24:00")
        if s == "00:00" and e == "00:00":
            # Treat midnight-to-midnight as an explicit all-day window.
            return True
        # handle wrap-around (e.g., 22:00–06:00)
        return (s <= now_str < e) if s <= e else (now_str >= s) or (now_str < e)

    def _ip_geolocate(self, timeout_s: float = 2.5) -> dict | None:
        """
        Resolve coarse location via IP geolocation (internet required).
        """
        if requests is None:
            return None
        try:
            resp = requests.get("https://ipapi.co/json/", timeout=timeout_s)
            if resp.status_code != 200:
                return None
            payload = resp.json() or {}
            lat = payload.get("latitude")
            lon = payload.get("longitude")
            tz_name = str(payload.get("timezone", "") or "").strip()
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except Exception:
                return None
            if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
                return None
            if not tz_name:
                return None
            return {"lat": lat_f, "lon": lon_f, "tz": tz_name}
        except Exception:
            return None

    def _resolve_astral_location(self) -> dict | None:
        """
        Resolve location for astral calculations using the same path as the UI.
        This keeps dashboard sunrise/sunset and automation evaluation consistent.
        """
        now = time.monotonic()
        cache = getattr(self, "_astral_location_cache", None) or {}
        if now < float(cache.get("expires_at", 0.0) or 0.0):
            return cache.get("value")

        resolved = None
        try:
            from saiSettings import saiSettings
            settings = saiSettings(apply_live=False)
            resolved = settings.resolve_astral_location(
                persist_if_auto=True,
                timeout_sec=2.5,
            )
        except Exception as e:
            if DEBUG:
                printDM(f"[astral] resolve location error: {e}", location=MODULE)
            resolved = None

        if not isinstance(resolved, dict):
            resolved = None
        elif (
            resolved.get("lat") is None
            or resolved.get("lon") is None
            or not str(resolved.get("tz", "") or "").strip()
        ):
            if DEBUG:
                printDM(f"[astral] incomplete location payload: {resolved}", location=MODULE)
            resolved = None

        ttl = 3600.0 if resolved else 15.0
        self._astral_location_cache = {"value": resolved, "expires_at": now + ttl}
        return resolved

    def _eval_astral_condition(self, cond: dict) -> bool:
        """
        Astral condition is true when local time in configured timezone is at/after:
          sunrise|sunset + offset_min
        Optionally restricted by `days` (0=Mon..6=Sun).
        """
        if LocationInfo is None or _astral_sun is None:
            if DEBUG:
                printDM("[astral] astral dependency unavailable", location=MODULE)
            return False

        resolved = self._resolve_astral_location()
        if not resolved:
            if DEBUG:
                printDM("[astral] no resolved location available", location=MODULE)
            return False

        tz_name = str(resolved.get("tz", "") or "").strip()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            if DEBUG:
                printDM(f"[astral] invalid timezone {tz_name!r}", location=MODULE)
            return False

        now_local = datetime.now(tz)

        raw_days = cond.get("days") or []
        allowed_days: list[int] = []
        for d in raw_days:
            try:
                n = int(d)
            except Exception:
                continue
            if 0 <= n <= 6:
                allowed_days.append(n)
        if allowed_days and (now_local.weekday() not in allowed_days):
            if DEBUG:
                printDM(
                    f"[astral] weekday filtered out: now={now_local.weekday()} allowed={allowed_days}",
                    location=MODULE,
                )
            return False

        event = str(cond.get("astral_event", cond.get("event", "sunrise")) or "sunrise").strip().lower()
        if event not in {"sunrise", "sunset"}:
            if DEBUG:
                printDM(f"[astral] invalid event {event!r}", location=MODULE)
            return False

        try:
            offset = int(cond.get("offset_min", cond.get("offset_minutes", 0)) or 0)
        except Exception:
            offset = 0
        offset = max(-120, min(120, offset))

        try:
            loc = LocationInfo(
                name="sensorius",
                region="local",
                timezone=tz_name,
                latitude=float(resolved["lat"]),
                longitude=float(resolved["lon"]),
            )
            s = _astral_sun(loc.observer, date=now_local.date(), tzinfo=tz)
            evt_dt = s.get(event)
            if evt_dt is None:
                if DEBUG:
                    printDM(f"[astral] missing event time for {event}", location=MODULE)
                return False
            threshold = evt_dt + timedelta(minutes=offset)
            result = now_local >= threshold
            if DEBUG:
                printDM(
                    f"[astral] event={event} now={now_local.isoformat()} threshold={threshold.isoformat()} result={result}",
                    location=MODULE,
                )
            return result
        except Exception as e:
            if DEBUG:
                printDM(f"[astral] evaluation error: {e}", location=MODULE)
            return False

    def _log(self, name, on: bool):
        # Persist as a SWITCH EVENT (not a generic reading) so /switch-status-update
        # can fetch the latest “On”/“Off” and recent event list.
        from saiUtils import get_timestamp
        switch_key = self._switch_key(name)
        sensor_lineage = f"Switch_{self.switch_id}" if getattr(self, "switch_id", None) else None
        # Use your dedicated API (present in saiDataLogger) for switch events:
        self.data_logger.log_switch_event(
            switch_key=switch_key,
            is_on=bool(on),
            timestamp=get_timestamp(),
            source="manual/ui",
            sensor_id=sensor_lineage
        )
        try:
            # late import, then get the live FastAPI app via routes
            import saiWebRoutes as routes
            bcast = getattr(routes, "app", None)
            # when register_routes ran we stashed the coroutine on app.state
            switch_broadcast = getattr(getattr(bcast, "state", object()), "switch_broadcast", None)
            if switch_broadcast:
                # fire and forget
                import asyncio
                payload = {
                    "type": "switch_event",
                    "key": switch_key,      # "switch-id::Label"
                    "ui_key": f"{self.switch_id}::{name}" if getattr(self, "switch_id", None) else switch_key,
                    "state": bool(on),      # True / False
                    "timestamp": get_timestamp(),
                    "source": "manual/ui",
                }
                payload.update(self.get_auto_off_status(name))
                asyncio.create_task(switch_broadcast(payload))
        except Exception:
            pass

    def set_override(self, name, enabled: bool):
        self.override_script[name] = enabled


    def get_state(self, name):
        return self.last_state.get(name, False)

    def all_states(self):
        try:
            names = self.get_switch_names()
            return {name: bool(self.get_state(name)) for name in names}
        except Exception as e:
            printDM(f"Error in all_states: {e}", location=MODULE)
            return {}

    def get_channel_index(self, label: str) -> int | None:
        try:
            chans = getattr(self.switch, "channels", None)
            if isinstance(chans, list):
                for ch in chans:
                    if str(ch.get("name", "")).strip().lower() == label.strip().lower():
                        return int(ch.get("n"))
        except Exception:
            pass

        sw = (getattr(self, "settings", {}) or {}).get("Switch", {}) or {}
        for n in range(1, 33):
            if str(sw.get(f"SWITCH_{n}_LABEL", "")).strip().lower() == label.strip().lower():
                return n
        return None

    def set_auto_off_seconds(self, name: str, seconds: int) -> int:
        try:
            value = int(seconds)
        except Exception:
            value = 0
        value = max(0, min(value, 9999))
        self.auto_off_seconds[name] = value
        if value <= 0:
            self.auto_off_deadline[name] = None
        elif bool(self.get_state(name)):
            self._sync_auto_off_state(name, True, restart=True)
        else:
            self.auto_off_deadline[name] = None
        return value

    def get_auto_off_status(self, name: str) -> dict:
        seconds = int(self.auto_off_seconds.get(name, 0) or 0)
        deadline = self.auto_off_deadline.get(name)
        is_on = bool(self.get_state(name))
        remaining = 0
        if seconds > 0 and is_on and deadline:
            remaining = max(0, int(deadline - time.time() + 0.999))
            if remaining <= 0:
                deadline = None
        return {
            "timer_seconds": seconds,
            "timer_enabled": bool(seconds > 0),
            "timer_deadline_epoch": float(deadline) if deadline else None,
            "timer_remaining_s": remaining,
        }

    def _sync_auto_off_state(self, name: str, is_on: bool, *, restart: bool = False) -> None:
        seconds = int(self.auto_off_seconds.get(name, 0) or 0)
        if not is_on or seconds <= 0:
            self.auto_off_deadline[name] = None
            return
        if restart or not self.auto_off_deadline.get(name):
            self.auto_off_deadline[name] = time.time() + seconds

    def _process_auto_off_timers(self) -> None:
        now = time.time()
        for name, seconds in list((self.auto_off_seconds or {}).items()):
            try:
                seconds = int(seconds or 0)
            except Exception:
                seconds = 0
            if seconds <= 0:
                self.auto_off_deadline[name] = None
                continue
            deadline = self.auto_off_deadline.get(name)
            if not deadline or deadline > now:
                continue
            if not bool(self.get_state(name)):
                self.auto_off_deadline[name] = None
                continue
            ok = bool(self.set_state(name, False, force=True))
            if not ok and bool(self.get_state(name)):
                self.auto_off_deadline[name] = time.time() + 1.0

    def label_for_channel_id(self, channel_id: str) -> str:
        chan = (channel_id or "").strip().lower()
        if not chan:
            return ""
        for label, cid in (self.channel_id_for_label or {}).items():
            if str(cid or "").strip().lower() == chan:
                return label
        return ""

    def _set_switch_state(self, name: str, on: bool) -> bool:
        # 1) Try device backend first
        if hasattr(self, "switch") and hasattr(self.switch, "set_state"):
            if bool(self.switch.set_state(name, on)):
                return True

        # 2) Fallback to MQTT ingest (remote Nodus) if available
        try:
            from saiMQTTIngest import get_current_ingest  # small helper you’ll add below
            ing = get_current_ingest()
            if ing:
                return bool(ing.set_switch(self.switch_id, name, on))
        except Exception:
            pass

        printDM(f"Backend switch object missing set_state() for '{name}' and no ingest fallback", location=MODULE)
        return False


    def set_state(self, name, on: bool, *, force: bool = False):
        now = time.monotonic()
        prev_on = self.get_state(name)
        if self.override_script.get(name, False):
            printDM(f"Override active: {name} forced to {on}", location=MODULE)
            ok = self._set_switch_state(name, on)
            if ok:
                self.last_state[name] = on                     # <-- keep RAM state in sync
                self._log(name, on)
                self.last_set_time[name] = now
                self._sync_auto_off_state(name, bool(on), restart=bool(on and not prev_on))
            return bool(ok)

        elapsed = now - self.last_set_time.get(name, 0)
        if not force and on == prev_on:
            return False
        if on and elapsed < self.min_off_time:
            return False
        if not on and elapsed < self.min_on_time:
            return False

        printDM(f"Setting {name} to {'ON' if on else 'OFF'} (override: {self.override_script.get(name, False)})", location=MODULE)
        ok = self._set_switch_state(name, on)
        if ok:
            self.last_state[name] = on                         # <-- keep RAM state in sync
            self._log(name, on)
            self.last_set_time[name] = now
            self._sync_auto_off_state(name, bool(on), restart=bool(on and not prev_on))

        # Only publish this telemetry for local backend; MQTTSwitch already sent a command.
        # Prefer ID-based topics using SWITCH_n_CHANNEL_ID.
        if not self.mqtt:
            self.mqtt = get_mqtt_client(self.switch_id)
        if ok and self.mqtt and self.mqtt.is_connected():
            try:
                backend_name = getattr(
                    getattr(self, "switch", None),
                    "__class__",
                    type("X", (object,), {}),
                ).__name__
                if backend_name != "MQTTSwitch":
                    channel_id = (self.channel_id_for_label or {}).get(name)
                    if channel_id and hasattr(self.mqtt, "publish_switch_state"):
                        self.mqtt.publish_switch_state(
                            self.switch_id,
                            channel_id,
                            bool(on),
                            include_event=True,
                        )
                    elif DEBUG:
                        printDM(
                            f"MQTT publish skipped for {self.switch_id}/{name}: missing channel_id",
                            location=MODULE,
                        )
            except Exception as e:
                printDM(f"MQTT publish error: {e}", location=MODULE)

        return bool(ok)

    # --- rule detection helpers -------------------------------------------------
    def _get_triggers_path(self) -> Path | None:
        """
        Compute shared automation path:
        .../switch_settings/automations/automations.toml
        Returns None if path cannot be resolved.
        """
        try:
            from saiAutomationManager import AutomationManager
            mgr = AutomationManager("switch_settings")
            return Path(mgr.get_storage_path())
        except Exception:
            return None

    def _load_triggers_dict(self) -> dict:
        """
        Uses AutomationManager shared automations file.
        Falls back to loading automations.toml directly via tomllib.
        Returns dict with 'Advanced' key.
        """
        # Try manager first
        try:
            from saiAutomationManager import AutomationManager
            mgr = AutomationManager("switch_settings")
            return {"Advanced": mgr.load_runtime_advanced(self.switch_id) or {}}
        except Exception:
            pass

        # Fallback: read the file directly
        try:
            import tomllib
            path = self._get_triggers_path()
            if not path or not path.exists():
                return {"Advanced": {}}
            with path.open("rb") as f:
                data = tomllib.load(f) or {}
            return {"Advanced": data.get("Advanced") or {}}
        except Exception:
            return {"Advanced": {}}

    def _compare_with_hysteresis(self, op: str, actual: float, threshold: float, hyst: float, current_state: bool) -> bool:
        """
        Return the desired state (True/False) using hysteresis around threshold.
        For op ">", turns ON above (threshold+hyst), turns OFF below (threshold-hyst).
        For op "<", turns ON below (threshold-hyst), turns OFF above (threshold+hyst).
        """
        op = (op or ">").strip()
        hi = threshold + hyst
        lo = threshold - hyst

        if op == ">":
            return (actual > hi) if not current_state else (actual > lo)
        elif op == "<":
            return (actual < lo) if not current_state else (actual < hi)
        elif op == "==":
            return (actual == threshold)
        elif op == "!=":
            return (actual != threshold)
        # default (unknown op) → do nothing (keep current)
        return current_state

    def _get_values_for_sensor(self, sensor_id: str, current_values_map: dict) -> dict:
        """
        Return a values dict for the requested sensor_id, preferring the live map;
        falling back to last cached self.values; and finally to the DataLogger.
        """
        # 1) live map
        vals = (current_values_map or {}).get(sensor_id)
        if isinstance(vals, dict) and vals:
            if DEBUG:
                printDM(f"[vals] {self.switch_id}:{sensor_id} from live map keys={list(vals.keys())[:6]}", location=MODULE)
            return vals
        # 2) cached snapshot
        vals = (getattr(self, "values", {}) or {}).get(sensor_id)
        if isinstance(vals, dict) and vals:
            if DEBUG:
                printDM(f"[vals] {self.switch_id}:{sensor_id} from cache keys={list(vals.keys())[:6]}", location=MODULE)
            return vals
        # 3) DB fallback
        try:
            vals = self.data_logger.get_latest_values(sensor_id) or {}
            if DEBUG:
                printDM(f"[vals] {self.switch_id}:{sensor_id} from DB keys={list(vals.keys())[:6]}", location=MODULE)
            return vals
        except Exception as e:
            if DEBUG:
                printDM(f"[vals] {self.switch_id}:{sensor_id} DB error: {e}", location=MODULE)
        return {}

    def _has_enabled_rules_from_triggers(self, triggers: dict) -> bool:
        """
        A conservative check: any item in [Advanced] is 'enabled'
        unless it has an explicit enabled=false (bool or stringy false).
        """
        def _truthy_enabled(v) -> bool:
            if isinstance(v, dict):
                flag = v.get("enabled", True)
            else:
                # when Advanced stores JSON-as-string, we can’t cheaply parse
                # here; assume enabled so users aren’t surprised.
                flag = True
            if isinstance(flag, str):
                return flag.strip().lower() not in ("0", "false", "no", "off")
            return bool(flag)

        for section in ("Advanced",):
            entries = triggers.get(section) or {}
            for _k, val in entries.items():
                try:
                    if _truthy_enabled(val):
                        return True
                except Exception:
                    # safest default: treat as enabled
                    return True
        return False

    def _rules_enabled(self) -> bool:
        """
        Fast “do we have anything to evaluate?” check with mtime caching.
        True if we have enabled automations.toml rules.
        """
        def _sync_overrides_from_triggers(triggers: dict) -> None:
            """
            Keep per-channel override flags aligned with Advanced rule enabled state.
            If any Advanced rule targeting a label is enabled -> override False.
            If rules exist for label and all are disabled -> override True.
            """
            try:
                adv = triggers.get("Advanced") or {}
                if not isinstance(adv, dict):
                    return

                # Build map: label -> {"found": bool, "enabled_any": bool}
                state_map: dict[str, dict] = {}
                import json as _json

                for _rid, rule in adv.items():
                    if not isinstance(rule, dict):
                        continue
                    enabled_raw = rule.get("enabled", False)
                    if isinstance(enabled_raw, str):
                        enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "off")
                    else:
                        enabled = bool(enabled_raw)
                    script_json = rule.get("script_json", "")
                    try:
                        script = (
                            script_json
                            if isinstance(script_json, (dict, list))
                            else _json.loads(str(script_json))
                        )
                    except Exception:
                        continue
                    actions = script.get("actions") or []
                    for act in actions:
                        try:
                            sk = (act.get("switch_key") or "").strip()
                        except AttributeError:
                            continue
                        if not sk or "::" not in sk:
                            continue
                        sid_part, suffix_part = sk.split("::", 1)
                        if (sid_part or "").strip().lower() != str(self.switch_id).strip().lower():
                            continue
                        label = str(act.get("switch", "") or "").strip()
                        if not label:
                            label = self.label_for_channel_id((suffix_part or "").strip())
                        if not label:
                            label = (suffix_part or "").strip()
                        if not label:
                            continue
                        slot = state_map.setdefault(label, {"found": False, "enabled_any": False})
                        slot["found"] = True
                        if enabled:
                            slot["enabled_any"] = True

                if not state_map:
                    return

                from saiSwitchSettingsManager import SwitchSettingsManager
                mgr = SwitchSettingsManager("switch_settings")
                for label, st in state_map.items():
                    # Compute desired override value
                    desired_override = False if st.get("enabled_any") else True
                    # Only apply if we have a matching channel index
                    idx = None
                    try:
                        idx = self.get_channel_index(label)
                    except Exception:
                        idx = None
                    if not idx:
                        ordered = list(self.get_switch_names() or [])
                        lbl_lower = (label or "").strip().lower()
                        idx = next((i + 1 for i, nm in enumerate(ordered) if (nm or "").strip().lower() == lbl_lower), None)
                    if not idx:
                        continue

                    current_override = bool(self.override_script.get(label, False))
                    if current_override != desired_override:
                        mgr.update_setting(self.switch_id, f"SWITCH_{idx}_OVERRIDE_SCRIPT", desired_override)
                        self.override_script[label] = desired_override
            except Exception:
                pass

        # disk-based automations.toml
        try:
            if not hasattr(self, "_rules_cache"):
                self._rules_cache = {"mtime": None, "enabled": False}

            triggers_path = self._get_triggers_path()
            if not triggers_path or not triggers_path.exists():
                # remember absence so we don’t stat on every tick
                self._rules_cache["mtime"] = None
                self._rules_cache["enabled"] = False
                return False

            mtime = triggers_path.stat().st_mtime
            # cache miss or changed → re-evaluate
            if self._rules_cache["mtime"] != mtime:
                triggers = self._load_triggers_dict()
                enabled  = self._has_enabled_rules_from_triggers(triggers)
                self._rules_cache.update({"mtime": mtime, "enabled": enabled})
                _sync_overrides_from_triggers(triggers)
                if DEBUG:
                    printDM(f"[rules] automations.toml mtime changed; enabled={enabled}", location=MODULE)
                    printDM(f"{self.switch_id}: [rules] path={triggers_path} mtime changed; enabled={enabled}", location=MODULE)

            return bool(self._rules_cache["enabled"])
        except Exception as e:
            if DEBUG:
                printDM(f"[rules] detection error: {e}", location=MODULE)
            return False

    def _evaluate_and_apply_advanced(self, current_values_map: dict):
        """
        Evaluate [Advanced] rules from automations.toml.

        Semantics (matching the Advanced Automation UI):

        - The script JSON has a flat `conditions` list.
        - Rows with `{type:"or"}` act as separators between groups.
        - Each group is a contiguous block of non-OR conditions.
        - Inside a group, conditions are ANDed.
        - Across groups, results are ORed.

        - Condition types:
            * "time":   time-of-day window using _time_in_window(start, end),
                        optionally restricted to certain weekdays via `days`.
                        days = [0..6] = Mon..Sun (Python-style weekday).
            * "astral": sunrise/sunset threshold using IP/manual location:
                        true when local time is at/after event + offset_min.
            * "timer":  periodic window based on duration_min (minutes) and
                        freq_hours (hours). True for the first duration_min
                        minutes of each freq_hours period within a day.
            * "sensor": uses hysteresis around `value` to decide if the channel
                        should be ON, based on the *current state* of the target
                        switch channel.

        - For each action:
            * Let `rule_ok` be the OR of group results for that action.
            * If rule_ok becomes True, wait `delay_s` seconds, then apply `action.set`.
            * While rule_ok stays True, keep the channel at `action.set`.
            * If rule_ok becomes False:
              - `revert_action == "previous_state"` restores the pre-action state.
              - `revert_action == "do_nothing"` leaves the current state unchanged.

        Only targets actions whose switch_key belongs to this controller.
        """
        try:
            triggers = self._load_triggers_dict()
            advanced = triggers.get("Advanced") or {}
            if DEBUG:
                printDM(
                    f"[advanced] {self.switch_id}: evaluating {len(advanced)} rule(s)",
                    location=MODULE,
                )
        except Exception as e:
            if DEBUG:
                printDM(f"[advanced] load error: {e}", location=MODULE)
            return

        # Snapshot "now" once per evaluation pass
        now_tm = time.localtime()
        now_str = time.strftime("%H:%M", now_tm)
        now_wday = now_tm.tm_wday  # 0=Mon .. 6=Sun
        seconds_since_midnight = (
            now_tm.tm_hour * 3600 + now_tm.tm_min * 60 + now_tm.tm_sec
        )

        def _split_condition_groups(conditions: list[dict]) -> list[list[dict]]:
            """
            Convert a flat list of conditions (with type:'or' rows as separators)
            into a list of groups. Each group is a list of non-OR conditions.
            """
            groups: list[list[dict]] = []
            current: list[dict] = []
            for c in conditions:
                ctype = str(c.get("type", "") or "").strip().lower()
                if ctype == "or":
                    if current:
                        groups.append(current)
                        current = []
                    continue
                current.append(c)
            if current:
                groups.append(current)
            return groups

        def _eval_single_condition(cond: dict, target_label: str) -> bool:
            """
            Evaluate a single condition for a specific target switch label.
            Uses current_values_map + hysteresis around cond.value where applicable.
            """
            ctype = str(cond.get("type", "") or "").strip().lower()

            # --- TIME-OF-DAY CONDITION ---------------------------------------
            # type == "time"
            if ctype == "time":
                start = cond.get("start") or "00:00"
                end   = cond.get("end")   or "24:00"

                # Optional: restrict to specific weekdays (0=Mon..6=Sun)
                # If 'days' is missing or empty, treat as "all days".
                raw_days = cond.get("days") or []
                allowed_days: list[int] = []
                for d in raw_days:
                    try:
                        n = int(d)
                    except Exception:
                        continue
                    if 0 <= n <= 6:
                        allowed_days.append(n)

                if allowed_days and (now_wday not in allowed_days):
                    return False

                return self._time_in_window(start, end, now_str)

            # --- ASTRAL CONDITION --------------------------------------------
            # type == "astral"
            if ctype == "astral":
                return self._eval_astral_condition(cond)

            # --- TIMER CONDITION ----------------------------------------------
            # type == "timer"
            if ctype == "timer":
                # duration_min: 1–60 minutes (clamped)
                # freq_hours:   period between pulses (1,3,6,12,24, etc.)
                try:
                    duration_min = int(cond.get("duration_min") or 0)
                except Exception:
                    duration_min = 0
                try:
                    freq_hours = int(cond.get("freq_hours") or 0)
                except Exception:
                    freq_hours = 0

                if duration_min <= 0 or freq_hours <= 0:
                    return False

                period_sec = max(freq_hours, 1) * 3600
                duration_sec = max(1, min(duration_min * 60, period_sec))

                # Repeat every 'freq_hours' from local midnight
                # True for the first 'duration_min' minutes of each period.
                phase = seconds_since_midnight % period_sec
                return phase < duration_sec

            # --- SENSOR CONDITION ---------------------------------------------
            if ctype == "sensor":
                sensor_id = str(cond.get("sensor", "") or "").strip()
                metric    = str(cond.get("metric", "") or "").strip()
                if not sensor_id or not metric:
                    return False

                # Pull latest values for that sensor (live → cache → DB)
                vals = self._get_values_for_sensor(sensor_id, current_values_map)
                if not vals:
                    if DEBUG:
                        printDM(
                            f"[advanced] {self.switch_id}: no values for sensor '{sensor_id}'",
                            location=MODULE,
                        )
                    return False

                # Try exact metric first, then a lenient normalized match
                actual = None
                if metric in vals:
                    actual = vals[metric]
                else:
                    key_norm = (
                        metric.lower()
                        .replace("-", "")
                        .replace("_", "")
                        .replace(" ", "")
                    )
                    for k, v in vals.items():
                        if not k:
                            continue
                        if (
                            k.lower()
                            .replace("-", "")
                            .replace("_", "")
                            .replace(" ", "")
                            == key_norm
                        ):
                            actual = v
                            break
                if actual is None:
                    if DEBUG:
                        printDM(
                            f"[advanced] {self.switch_id}: metric '{metric}' not found; "
                            f"keys={list(vals.keys())[:8]}",
                            location=MODULE,
                        )
                    return False

                try:
                    actual_f   = float(actual)
                    threshold  = float(cond.get("value"))
                except Exception:
                    return False

                try:
                    hyst = float(cond.get("hyst", 0) or 0)
                except Exception:
                    hyst = 0.0

                op = str(cond.get("op", ">") or ">").strip()
                curr_state = bool(self.get_state(target_label))

                # Interpret "condition truth" as "this condition alone wants the
                # channel to be ON" using hysteresis around the threshold.
                desired_for_cond = self._compare_with_hysteresis(
                    op, actual_f, threshold, hyst, curr_state
                )
                return bool(desired_for_cond)

            # Unknown condition type → treat as False (safe default)
            return False

        now_mono = time.monotonic()
        pending_actions = getattr(self, "_advanced_delay_due", None)
        if not isinstance(pending_actions, dict):
            pending_actions = {}
            self._advanced_delay_due = pending_actions
        active_actions = getattr(self, "_advanced_active_actions", None)
        if not isinstance(active_actions, dict):
            active_actions = {}
            self._advanced_active_actions = active_actions
        if not isinstance(getattr(self, "_advanced_revert_cooldown", None), set):
            self._advanced_revert_cooldown = set()

        action_evals: dict[tuple[str, str, str, bool], dict] = {}

        # ----- main rule loop -------------------------------------------------
        for _rule_id, rule in (advanced or {}).items():
            try:
                # Outer enabled flag (TOML-level)
                enabled_outer = rule.get("enabled", True)
                if isinstance(enabled_outer, str):
                    if enabled_outer.strip().lower() in ("0", "false", "no", "off"):
                        continue
                elif not bool(enabled_outer):
                    continue

                script_raw = rule.get("script_json", "")
                try:
                    script = (
                        json.loads(script_raw)
                        if not isinstance(script_raw, (dict, list))
                        else script_raw
                    )
                except Exception:
                    if DEBUG:
                        printDM(f"[advanced] bad JSON; id={_rule_id}", location=MODULE)
                    continue

                # Inner enabled flag (inside JSON)
                if not bool(script.get("enabled", True)):
                    continue

                conditions = script.get("conditions") or []
                actions    = script.get("actions") or []
                if not conditions or not actions:
                    continue

                # Build AND groups separated by OR markers
                groups = _split_condition_groups(conditions)
                if not groups:
                    continue

                # ---- per-action evaluation (so hysteresis uses that channel) ----
                for act in actions:
                    skey = str(act.get("switch_key", "") or "").strip()
                    if "::" not in skey:
                        if DEBUG:
                            printDM(f"[advanced] bad switch_key {skey!r}", location=MODULE)
                        continue

                    # switch_key is the *canonical* identity:
                    #   "<switch_id>::<channel_id-or-label>"
                    raw_sid, raw_suffix = skey.split("::", 1)
                    target_sid = raw_sid.strip()

                    # Only act if this rule targets *this* controller
                    if target_sid != getattr(self, "switch_id", None):
                        continue

                    # Resolve the human-facing label we use for:
                    #   - get_state / set_state
                    #   - override_script
                    #   - hysteresis / condition evaluation
                    label_from_act = (act.get("switch") or "").strip()
                    target_label = label_from_act

                    if not target_label:
                        # Try to map from channel_id -> label using controller metadata
                        chan_id = raw_suffix.strip()
                        try:
                            # Preferred: explicit helper if you added one
                            label_resolver = getattr(self, "label_for_channel_id", None)
                            if callable(label_resolver):
                                target_label = label_resolver(chan_id) or ""
                        except Exception:
                            target_label = ""

                    if not target_label:
                        # Fallback: invert channel_id_for_label if present
                        try:
                            cid_map = dict(getattr(self, "channel_id_for_label", {}) or {})
                            inv_map = {v: k for k, v in cid_map.items() if v}
                            target_label = inv_map.get(raw_suffix.strip(), "")
                        except Exception:
                            target_label = ""

                    if not target_label:
                        # Final fallback so *old* label-style keys still work:
                        #   "sensoria-hub-0::Fan" => target_label = "Fan"
                        target_label = raw_suffix.strip()

                    if not target_label:
                        if DEBUG:
                            printDM(
                                f"[advanced] could not resolve label for switch_key={skey!r}",
                                location=MODULE,
                            )
                        continue

                    # Respect per-channel override by label
                    if self.override_script.get(target_label, False):
                        if DEBUG:
                            printDM(
                                f"[advanced] '{target_label}' skipped due to override",
                                location=MODULE,
                            )
                        continue

                    # AND inside group, OR across groups
                    group_results = []
                    for group in groups:
                        grp_ok = True
                        for cond in group:
                            if not _eval_single_condition(cond, target_label):
                                grp_ok = False
                                break
                        group_results.append(grp_ok)

                    rule_ok = any(group_results)
                    desired = bool(act.get("set", True))
                    action_key = (str(_rule_id), str(target_label), str(skey), bool(desired))
                    revert_action = str(act.get("revert_action", "do_nothing") or "do_nothing").strip().lower()
                    if revert_action not in {"previous_state", "do_nothing"}:
                        revert_action = "do_nothing"
                    delay_s = int(act.get("delay_s", 0) or 0)

                    action_evals[action_key] = {
                        "rule_id": str(_rule_id),
                        "target_label": target_label,
                        "switch_key": skey,
                        "desired": desired,
                        "revert_action": revert_action,
                        "delay_s": max(0, delay_s),
                        "rule_ok": rule_ok,
                        "group_results": list(group_results),
                    }

            except Exception as e:
                printDM(f"[advanced] rule error: {e}", location=MODULE)

        def _revert_active_action(action_key: tuple[str, str, str, bool], active: dict) -> bool:
            revert_action = str(active.get("revert_action", "") or "").strip().lower()
            target_label = str(active.get("target_label", "") or "").strip()
            if not target_label:
                active_actions.pop(action_key, None)
                return True
            if revert_action != "previous_state":
                active_actions.pop(action_key, None)
                return True

            revert_to = bool(active.get("revert_to", False))
            current_state = bool(self.get_state(target_label))
            if current_state == revert_to:
                active_actions.pop(action_key, None)
                return True

            ok = bool(self.set_state(target_label, revert_to, force=True))
            if ok or bool(self.get_state(target_label)) == revert_to:
                active_actions.pop(action_key, None)
                return True
            return False

        seen_action_keys = set(action_evals.keys())

        for action_key in list(pending_actions.keys()):
            if action_key not in seen_action_keys:
                pending_actions.pop(action_key, None)
        for action_key, active in list(active_actions.items()):
            if action_key not in seen_action_keys:
                _revert_active_action(action_key, active if isinstance(active, dict) else {})

        for action_key, info in action_evals.items():
            target_label = str(info.get("target_label", "") or "").strip()
            skey = str(info.get("switch_key", "") or "").strip()
            desired = bool(info.get("desired", True))
            rule_ok = bool(info.get("rule_ok", False))
            revert_action = str(info.get("revert_action", "do_nothing") or "do_nothing").strip().lower()
            delay_s = int(info.get("delay_s", 0) or 0)
            current_state = bool(self.get_state(target_label))
            pending = pending_actions.get(action_key) if isinstance(pending_actions.get(action_key), dict) else None
            active = active_actions.get(action_key) if isinstance(active_actions.get(action_key), dict) else None

            if not rule_ok:
                pending_actions.pop(action_key, None)
                self._advanced_revert_cooldown.discard(action_key)
                if active:
                    _revert_active_action(action_key, active)
                if DEBUG and str(target_label).strip().lower() == "fan":
                    printDM(
                        f"[advanced] fan rule {info['rule_id']} no-op: rule_ok={rule_ok} group_results={info.get('group_results')}",
                        location=MODULE,
                    )
                continue

            if active:
                if current_state == desired:
                    if DEBUG and str(target_label).strip().lower() == "fan":
                        printDM(
                            f"[advanced] fan rule {info['rule_id']} skipped: curr={current_state} desired={desired} switch_key={skey}",
                            location=MODULE,
                        )
                    continue
                ok = bool(self.set_state(target_label, desired))
                if ok:
                    active_actions[action_key] = dict(active, last_applied_at=now_mono)
                continue

            if pending:
                due_at = float(pending.get("due_at", 0.0) or 0.0)
                if due_at > now_mono:
                    continue
                pending_actions.pop(action_key, None)
                if current_state == desired:
                    if DEBUG and str(target_label).strip().lower() == "fan":
                        printDM(
                            f"[advanced] fan rule {info['rule_id']} skipped after delay: curr={current_state} desired={desired} switch_key={skey}",
                            location=MODULE,
                        )
                    continue
                if DEBUG:
                    printDM(
                        f"[advanced] applying delayed rule {info['rule_id']} to '{target_label}': "
                        f"desired={desired} (switch_key={skey})",
                        location=MODULE,
                    )
                ok = bool(self.set_state(target_label, desired))
                if not ok:
                    pending_actions[action_key] = dict(pending, due_at=time.monotonic() + 1.0)
                    continue
                active_actions[action_key] = {
                    "target_label": target_label,
                    "switch_key": skey,
                    "desired": desired,
                    "revert_action": revert_action,
                    "revert_to": current_state,
                    "activated_at": now_mono,
                }
                if revert_action == "previous_state":
                    self._advanced_revert_cooldown.add(action_key)
                continue

            if delay_s > 0:
                pending_actions[action_key] = {
                    "due_at": now_mono + min(delay_s, 300),
                    "target_label": target_label,
                    "switch_key": skey,
                    "desired": desired,
                    "revert_action": revert_action,
                }
                continue

            if current_state == desired:
                if DEBUG and str(target_label).strip().lower() == "fan":
                    printDM(
                        f"[advanced] fan rule {info['rule_id']} skipped: curr={current_state} desired={desired} switch_key={skey}",
                        location=MODULE,
                    )
                continue

            if DEBUG:
                printDM(
                    f"[advanced] applying rule {info['rule_id']} to '{target_label}': "
                    f"rule_ok={rule_ok} desired={desired} (switch_key={skey})",
                    location=MODULE,
                )
            if DEBUG and str(target_label).strip().lower() == "fan":
                printDM(
                    f"[advanced] fan apply {info['rule_id']}: curr={current_state} desired={desired} switch_key={skey}",
                    location=MODULE,
                )

            ok = bool(self.set_state(target_label, desired))
            if not ok:
                continue

            active_actions[action_key] = {
                "target_label": target_label,
                "switch_key": skey,
                "desired": desired,
                "revert_action": revert_action,
                "revert_to": current_state,
                "activated_at": now_mono,
            }
            if revert_action == "previous_state":
                self._advanced_revert_cooldown.add(action_key)
            else:
                self._advanced_revert_cooldown.discard(action_key)
                
    async def run_controladora_monitor(self, sensor, interval=29):
        """
        Periodically evaluate switch rules.
        - If any enabled automation rules are present,
          we will run evaluation each cycle.
        - If a bound sensor is present and healthy, include its current dataset.
          Otherwise, we evaluate with the last known values (if any) or {}.
        """
        heartbeat_every_s = 10.0

        async def _sleep_with_heartbeat(total_sleep_s: float) -> None:
            remaining = max(float(total_sleep_s), 0.0)
            while remaining > 0.0:
                self._process_auto_off_timers()
                if getattr(self, "supervisor", None) and hasattr(self.supervisor, "feedthedogs"):
                    self.supervisor.feedthedogs(f"{self.switch_id} Controladora Monitor")
                chunk = min(1.0, remaining)
                await asyncio.sleep(chunk)
                remaining -= chunk
                await asyncio.sleep(0)

        while True:
            tick_started = time.monotonic()
            rules_check_ms = 0.0
            snapshot_ms = 0.0
            eval_ms = 0.0
            rules_present = False
            try:
                # Decide if we should do any work this tick
                rules_check_started = time.monotonic()
                rules_present = self._rules_enabled()
                rules_check_ms = (time.monotonic() - rules_check_started) * 1000.0
                if not rules_present:
                    if DEBUG:
                        printDM(f"{self.switch_id} Switch monitor: no enabled rules; skipping eval", location=MODULE)
                else:
                    # Prefer passed-in sensor; fall back to self.sensor
                    bound_sensor = sensor or getattr(self, "sensor", None)

                    # Try to capture fresh metrics when we can
                    current_values_map = None
                    if bound_sensor is not None and hasattr(bound_sensor, "current_data_set"):
                        if getattr(bound_sensor, "present", True) is not False:
                            try:
                                snapshot_started = time.monotonic()
                                raw_values, *_ = bound_sensor.current_data_set()
                                snapshot_ms = (time.monotonic() - snapshot_started) * 1000.0
                                sensor_key = (
                                    getattr(bound_sensor, "sensor_id", None)
                                    or getattr(bound_sensor, "devID", None)
                                )
                                if sensor_key:
                                    current_values_map = {sensor_key: raw_values}
                                    # cache for a later tick when no sensor is available
                                    self.values = current_values_map
                                elif DEBUG:
                                    printDM("Switch monitor: missing sensor_id/devID; using cached/empty values",
                                            location=MODULE)
                            except Exception as e:
                                if DEBUG:
                                    printDM(f"Switch monitor: data fetch error: {e}", location=MODULE)

                    # Fall back to the last snapshot or empty dict
                    if current_values_map is None:
                        current_values_map = getattr(self, "values", {}) or {}

                    # Evaluate rules (per-switch overrides are handled inside)
                    eval_started = time.monotonic()
                    self._evaluate_and_apply_advanced(current_values_map)
                    eval_ms = (time.monotonic() - eval_started) * 1000.0

            except Exception as e:
                printDM(f"Switch monitor error: {e}", location=MODULE)

            total_ms = (time.monotonic() - tick_started) * 1000.0
            if DEBUG:
                printDM(
                    f"[monitor-profile] {self.switch_id} rules_present={int(bool(rules_present))} "
                    f"rules_check_ms={rules_check_ms:.1f} snapshot_ms={snapshot_ms:.1f} "
                    f"eval_ms={eval_ms:.1f} total_ms={total_ms:.1f}",
                    location=MODULE,
                )

            # keep the dogs fed & cadence jitter
            if getattr(self, "supervisor", None) and hasattr(self.supervisor, "feedthedogs"):
                self.supervisor.feedthedogs(f"{self.switch_id} Controladora Monitor")

            await _sleep_with_heartbeat(interval + random.uniform(-0.8, 0.8))


class RemoteSwitchController(SwitchController):
    """MQTT-backed switch controller for remote Nodus/Pico devices."""

    def __init__(self, switch_settings=None, supervisor=None, sensor=None, mqtt_ingest=None, data_logger=None):
        self.is_remote = True
        self.mqtt_ingest = mqtt_ingest
        super().__init__(switch_settings=switch_settings, supervisor=supervisor, sensor=sensor, data_logger=data_logger)

    def _refresh_state_from_ingest(self) -> None:
        try:
            ing = self.mqtt_ingest
            if ing is None:
                from saiMQTTIngest import get_current_ingest
                ing = get_current_ingest()
            if ing is None:
                return

            sid = str(getattr(self, "switch_id", "") or "").strip()
            if not sid:
                return

            ch_map = (getattr(ing, "_switch_state_cache", {}) or {}).get(sid, {}) or {}
            if not isinstance(ch_map, dict) or not ch_map:
                return

            for label in (self.get_switch_names() or []):
                channel_id = str((self.channel_id_for_label or {}).get(label, "") or "").strip()
                raw = None
                if channel_id:
                    raw = ch_map.get(channel_id)
                    if raw is None:
                        raw = ch_map.get(channel_id.lower())
                if raw is None:
                    raw = ch_map.get(label)
                if raw is None:
                    raw = ch_map.get(str(label).lower())
                if raw is None:
                    continue
                new_state = str(raw).strip().lower() in ("on", "true", "1")
                prev_state = bool(self.last_state.get(label, False))
                self.last_state[label] = new_state
                if new_state != prev_state:
                    self._sync_auto_off_state(label, new_state, restart=False)
        except Exception:
            return

    def get_state(self, name):
        self._refresh_state_from_ingest()
        return super().get_state(name)


def build_switch_controller(*, switch_settings=None, supervisor=None, sensor=None, mqtt_ingest=None, data_logger=None):
    if is_remote_switch_settings(switch_settings):
        return RemoteSwitchController(
            switch_settings=switch_settings,
            supervisor=supervisor,
            sensor=sensor,
            mqtt_ingest=mqtt_ingest,
            data_logger=data_logger,
        )
    return SwitchController(
        switch_settings=switch_settings,
        supervisor=supervisor,
        sensor=sensor,
        data_logger=data_logger,
    )
