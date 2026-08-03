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
import socket
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .saiUtils import printDM, debug_enabled, get_timestamp
from .saiSwitchFactory import create_switch
from .saiMQTTClient import get_mqtt_client
from .saiDataLogger import saiDataLogger
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
    from .saiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None

MODULE = "saiSwitch"
DEBUG = debug_enabled(MODULE)
REMOTE_SWITCH_TYPES = {"picow", "pico2w", "nodus", "remote", "mqtt"}
_ADVANCED_ACTIVE_ACTIONS_KEY = "ADVANCED_ACTIVE_ACTIONS_JSON"
_ADVANCED_IDLE_LOG_INTERVAL_S = 60.0


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
        self._advanced_active_actions_persisted_json = None
        self._advanced_debug_next_idle_log_at = 0.0
        self._advanced_debug_cycle_verbose = False
        self._astral_location_cache = {"value": None, "expires_at": 0.0}
        self._advanced_bd_transition_keys = {}
        self._advanced_bd_transition_segments = {}

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
                from .saiSwitchFactory import ensure_switch_settings_for_host
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

        self._restore_advanced_runtime_state()

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
          "<switch_id>::<channel_id>"
        """
        chan_id = None
        try:
            chan_id = (self.channel_id_for_label or {}).get(name)
        except Exception:
            chan_id = None
        chan_id = str(chan_id or "").strip()
        sid = str(getattr(self, "switch_id", "") or "").strip()
        suffix = chan_id or str(name or "").strip()

        if _build_switch_key and sid:
            return _build_switch_key(sid, suffix)
        return f"{sid}::{suffix}" if sid else f"{chan_id}::{name}"

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

    def _load_runtime_setting(self, key: str, default=None):
        try:
            if hasattr(self.settings, "get_setting"):
                return self.settings.get_setting("Runtime", key, default)
        except Exception:
            pass
        try:
            return ((self.settings or {}).get("Runtime", {}) or {}).get(key, default)
        except Exception:
            return default

    def _restore_advanced_runtime_state(self) -> None:
        """Restore persisted advanced-action ownership used for previous_state revert."""
        try:
            raw = self._load_runtime_setting(_ADVANCED_ACTIVE_ACTIONS_KEY, "")
            if not raw:
                self._advanced_active_actions = {}
                self._advanced_active_actions_persisted_json = None
                return

            payload = json.loads(str(raw))
            if not isinstance(payload, list):
                self._advanced_active_actions = {}
                self._advanced_active_actions_persisted_json = None
                return

            restored: dict[tuple[str, str, str, bool], dict] = {}
            for item in payload:
                if not isinstance(item, dict):
                    continue
                rule_id = str(item.get("rule_id", "") or "").strip()
                target_label = str(item.get("target_label", "") or "").strip()
                switch_key = str(item.get("switch_key", "") or "").strip()
                if not rule_id or not target_label or not switch_key:
                    continue
                desired = bool(item.get("desired", True))
                restored[(rule_id, target_label, switch_key, desired)] = {
                    "rule_id": rule_id,
                    "rule_name": str(item.get("rule_name", "") or "").strip(),
                    "target_label": target_label,
                    "switch_key": switch_key,
                    "desired": desired,
                    "revert_action": str(item.get("revert_action", "do_nothing") or "do_nothing").strip().lower(),
                    "revert_to": bool(item.get("revert_to", False)),
                    "activated_at": float(item.get("activated_at", 0.0) or 0.0),
                    "restored_at_startup": True,
                }
            self._advanced_active_actions = restored
            self._advanced_active_actions_persisted_json = str(raw)
            if DEBUG and restored:
                printDM(
                    f"[advanced] restored {len(restored)} persisted active action(s) for {self.switch_id}",
                    location=MODULE,
                )
        except Exception as e:
            self._advanced_active_actions = {}
            self._advanced_active_actions_persisted_json = None
            if DEBUG:
                printDM(f"[advanced] restore runtime state failed: {e}", location=MODULE)

    def _persist_advanced_runtime_state(self) -> None:
        """Persist active advanced-action ownership separately from raw switch events."""
        payload = []
        for action_key, active in sorted((self._advanced_active_actions or {}).items()):
            if not isinstance(active, dict):
                continue
            if str(active.get("revert_action", "") or "").strip().lower() != "previous_state":
                continue
            rule_id, target_label, switch_key, desired = action_key
            payload.append(
                {
                    "rule_id": str(rule_id),
                    "rule_name": str(active.get("rule_name", "") or "").strip(),
                    "target_label": str(target_label),
                    "switch_key": str(switch_key),
                    "desired": bool(desired),
                    "revert_action": "previous_state",
                    "revert_to": bool(active.get("revert_to", False)),
                    "activated_at": float(active.get("activated_at", 0.0) or 0.0),
                }
            )

        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True) if payload else ""
        if raw == getattr(self, "_advanced_active_actions_persisted_json", None):
            return

        try:
            from .saiSwitchSettingsManager import SwitchSettingsManager

            mgr = SwitchSettingsManager("switch_settings")
            mgr.set_setting(self.switch_id, f"Runtime.{_ADVANCED_ACTIVE_ACTIONS_KEY}", raw)
            self._advanced_active_actions_persisted_json = raw
        except Exception as e:
            if DEBUG:
                printDM(f"[advanced] persist runtime state failed: {e}", location=MODULE)

    def _recover_advanced_revert_from_history(self, info: dict, current_state: bool) -> bool:
        """Best-effort recovery of a previous_state revert after process restart."""
        rule_id = str(info.get("rule_id", "") or "").strip()
        if str(info.get("revert_action", "") or "").strip().lower() != "previous_state":
            return False

        target_label = str(info.get("target_label", "") or "").strip()
        if not target_label:
            return False

        desired = bool(info.get("desired", True))
        authoritative_current = bool(current_state)
        if bool(getattr(self, "is_remote", False)):
            try:
                ing = getattr(self, "mqtt_ingest", None)
                if ing is None:
                    from .saiMQTTIngest import get_current_ingest
                    ing = get_current_ingest()
                sid = str(getattr(self, "switch_id", "") or "").strip()
                channel_id = str((getattr(self, "channel_id_for_label", {}) or {}).get(target_label, "") or "").strip()
                if ing is not None and sid:
                    pending_state = None
                    pending_getter = getattr(self, "_pending_state_from_ingest", None)
                    if callable(pending_getter):
                        pending_state = pending_getter(ing, sid, target_label, channel_id)
                    if pending_state is not None:
                        authoritative_current = bool(pending_state)
                    else:
                        ch_map = (getattr(ing, "_switch_state_cache", {}) or {}).get(sid, {}) or {}
                        raw = None
                        if channel_id:
                            raw = ch_map.get(channel_id)
                            if raw is None:
                                raw = ch_map.get(channel_id.lower())
                        if raw is None:
                            raw = ch_map.get(target_label)
                        if raw is None:
                            raw = ch_map.get(str(target_label).lower())
                        if raw is not None:
                            authoritative_current = str(raw).strip().lower() in ("on", "true", "1")
            except Exception:
                authoritative_current = bool(current_state)

        if authoritative_current != desired:
            return False

        rule_name = str(info.get("rule_name", "") or "").strip()
        conditions = list(info.get("conditions") or [])

        def _normalized_rule_source(source: object) -> str:
            raw = str(source or "").strip()
            low = raw.lower()
            if low.startswith("mqtt-auto:"):
                raw = raw.split(":", 1)[1].strip()
            elif low.startswith("auto/rule:"):
                raw = raw.split(":", 1)[1].strip()
            elif low in {"mqtt-auto", "auto/rule"}:
                raw = ""
            if raw.lower().endswith("/mqtt"):
                raw = raw[:-5].strip()
            return raw.strip().lower()

        try:
            rows = self.data_logger.get_last_switch_events(
                self._switch_key(target_label),
                limit=25,
                include_source=True,
            )
        except Exception as e:
            if DEBUG:
                printDM(f"[advanced] history recovery query failed: {e}", location=MODULE)
            return False

        if len(rows) < 2:
            if DEBUG:
                printDM(
                    f"[advanced] {target_label} rule {rule_id or '?'} history recovery skipped: insufficient rows",
                    location=MODULE,
                )
            return False

        expected_source = rule_name.lower()
        matched_idx = None
        matched_is_on = None
        for idx, (row_state, _row_ts, row_source) in enumerate(rows):
            row_is_on = str(row_state).strip().lower() == "on"
            if row_is_on != authoritative_current:
                continue
            actual_source = _normalized_rule_source(row_source)
            if expected_source:
                if actual_source != expected_source:
                    continue
            elif actual_source:
                continue
            matched_idx = idx
            matched_is_on = row_is_on
            break

        if matched_idx is None or matched_is_on is None:
            generic_revert_to = self._infer_previous_state_revert_from_transition(
                conditions=conditions,
                rows=rows,
                authoritative_current=authoritative_current,
                desired=desired,
            )
            if generic_revert_to is not None:
                event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
                if DEBUG:
                    printDM(
                        f"[advanced] {target_label} rule {rule_id or '?'} recovering previous_state from "
                        f"generic transition: current={authoritative_current} revert_to={generic_revert_to}",
                        location=MODULE,
                    )
                return bool(self.set_state(target_label, generic_revert_to, force=True, event_source=event_source))
            if DEBUG:
                row_preview = [
                    {
                        "state": str(row_state).strip(),
                        "ts": str(row_ts or "").strip(),
                        "src": str(row_source or "").strip(),
                    }
                    for row_state, row_ts, row_source in rows[:8]
                ]
                printDM(
                    f"[advanced] {target_label} rule {rule_id or '?'} history recovery skipped: "
                    f"no matching source row for current_state={authoritative_current} "
                    f"rule_name={rule_name or '-'} recent_rows={row_preview}",
                    location=MODULE,
                )
            return False

        revert_to = None
        for prior_state, _prior_ts, _prior_source in rows[matched_idx + 1:]:
            prior_is_on = str(prior_state).strip().lower() == "on"
            if prior_is_on != matched_is_on:
                revert_to = prior_is_on
                break
        if revert_to is None:
            if DEBUG:
                printDM(
                    f"[advanced] {target_label} rule {rule_id or '?'} history recovery skipped: no prior opposite state",
                    location=MODULE,
                )
            return False

        event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
        if DEBUG:
            printDM(
                f"[advanced] {target_label} rule {rule_id or '?'} recovering previous_state from history: "
                f"current={authoritative_current} revert_to={revert_to} matched_row={matched_idx}",
                location=MODULE,
            )
        return bool(self.set_state(target_label, revert_to, force=True, event_source=event_source))

    def _infer_previous_state_revert_from_transition(
        self,
        *,
        conditions: list[dict],
        rows: list[tuple[str, str, str | None]],
        authoritative_current: bool,
        desired: bool,
    ) -> bool | None:
        """
        Legacy bootstrap for remote previous_state recovery.

        When old event rows lost automation provenance, infer ownership only for
        simple time-window rules whose most recent transition into the current
        desired state lines up with the configured window start.
        """
        if authoritative_current != desired:
            return None

        candidate_starts: list[str] = []
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if str(cond.get("type", "") or "").strip().lower() != "time":
                continue
            start = str(cond.get("start", "") or "").strip()
            end = str(cond.get("end", "") or "").strip()
            if start and end:
                candidate_starts.append(start)
        if not candidate_starts or len(rows) < 2:
            return None

        def _parse_local_ts(text: object) -> datetime | None:
            raw = str(text or "").strip()
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(raw)
            except Exception:
                return None
            if dt.tzinfo is None:
                try:
                    dt = dt.replace(tzinfo=getattr(self.data_logger, "local_tz", ZoneInfo("America/Denver")))
                except Exception:
                    pass
            return dt

        leading_block: list[tuple[str, str, str | None]] = []
        for row in rows:
            row_is_on = str(row[0]).strip().lower() == "on"
            if row_is_on != authoritative_current:
                break
            leading_block.append(row)
        if not leading_block or len(rows) <= len(leading_block):
            return None

        prior_row = rows[len(leading_block)]
        prior_is_on = str(prior_row[0]).strip().lower() == "on"
        if prior_is_on == authoritative_current:
            return None

        transition_dt = _parse_local_ts(leading_block[-1][1])
        if transition_dt is None:
            return None

        transition_minutes = transition_dt.hour * 60 + transition_dt.minute

        def _minutes_from_hhmm(text: str) -> int | None:
            try:
                hh, mm = text.split(":", 1)
                return int(hh) * 60 + int(mm)
            except Exception:
                return None

        for start in candidate_starts:
            start_minutes = _minutes_from_hhmm(start)
            if start_minutes is None:
                continue
            if abs(transition_minutes - start_minutes) <= 2:
                return prior_is_on
        return None

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
            from .saiSettings import saiSettings
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
        Astral condition supports:
          - sunrise|sunset: true when local time is at/after event + offset_min
          - sunrise_to_sunset: true during daytime window
          - sunset_to_sunrise: true during nighttime window
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
        if event not in {"sunrise", "sunset", "sunrise_to_sunset", "sunset_to_sunrise"}:
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
            observer = loc.observer
            try:
                observer.elevation = float(resolved.get("altitude") or 0.0)
            except Exception:
                pass
            s = _astral_sun(observer, date=now_local.date(), tzinfo=tz)
            sunrise_dt = s.get("sunrise")
            sunset_dt = s.get("sunset")
            if sunrise_dt is None or sunset_dt is None:
                if DEBUG:
                    printDM("[astral] missing sunrise/sunset event time", location=MODULE)
                return False

            if event == "sunrise_to_sunset":
                start_dt = sunrise_dt + timedelta(minutes=offset)
                result = start_dt <= now_local < sunset_dt
            elif event == "sunset_to_sunrise":
                start_dt = sunset_dt + timedelta(minutes=offset)
                result = now_local >= start_dt or now_local < sunrise_dt
            else:
                evt_dt = sunrise_dt if event == "sunrise" else sunset_dt
                threshold = evt_dt + timedelta(minutes=offset)
                result = now_local >= threshold

            if DEBUG:
                printDM(
                    f"[astral] event={event} now={now_local.isoformat()} sunrise={sunrise_dt.isoformat()} sunset={sunset_dt.isoformat()} result={result}",
                    location=MODULE,
                )
            return result
        except Exception as e:
            if DEBUG:
                printDM(f"[astral] evaluation error: {e}", location=MODULE)
            return False

    def _log(self, name, on: bool, *, source: str = "manual/ui"):
        # Persist as a SWITCH EVENT (not a generic reading) so /switch-status-update
        # can fetch the latest “On”/“Off” and recent event list.
        # Remote/Nodus switch history should come only from confirmed MQTT ingest.
        if bool(getattr(self, "is_remote", False)):
            return
        from .saiUtils import get_timestamp
        switch_key = self._switch_key(name)
        sensor_lineage = f"Switch_{self.switch_id}" if getattr(self, "switch_id", None) else None
        # Use your dedicated API (present in saiDataLogger) for switch events:
        self.data_logger.log_switch_event(
            switch_key=switch_key,
            is_on=bool(on),
            timestamp=get_timestamp(),
            source=source,
            sensor_id=sensor_lineage
        )
        try:
            # late import, then get the live FastAPI app via routes
            from . import saiWebRoutes as routes
            bcast = getattr(routes, "app", None)
            # when register_routes ran we stashed the coroutine on app.state
            switch_broadcast = getattr(getattr(bcast, "state", object()), "switch_broadcast", None)
            if switch_broadcast:
                # fire and forget
                import asyncio
                payload = {
                    "type": "switch_event",
                    "key": switch_key,      # "switch-id::channel-id"
                    "ui_key": f"{self.switch_id}::{name}" if getattr(self, "switch_id", None) else switch_key,
                    "state": bool(on),      # True / False
                    "timestamp": get_timestamp(),
                    "source": source,
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

    def _sync_auto_off_state(
        self,
        name: str,
        is_on: bool,
        *,
        restart: bool = False,
        allow_create_if_missing: bool = True,
    ) -> None:
        seconds = int(self.auto_off_seconds.get(name, 0) or 0)
        if not is_on or seconds <= 0:
            self.auto_off_deadline[name] = None
            return
        if restart:
            self.auto_off_deadline[name] = time.time() + seconds
            return
        if allow_create_if_missing and not self.auto_off_deadline.get(name):
            self.auto_off_deadline[name] = time.time() + seconds

    def sync_manual_toggle_result(self, name: str, is_on: bool, *, previous_state: bool) -> None:
        """Apply post-toggle runtime state after a confirmed manual switch command."""
        self.last_state[name] = bool(is_on)
        self.last_set_time[name] = time.monotonic()
        self._sync_auto_off_state(
            name,
            bool(is_on),
            restart=bool(is_on and not previous_state),
            allow_create_if_missing=bool(is_on and not previous_state),
        )

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
            # Expiry is single-shot: send one OFF request, then clear the timer
            # immediately so stale remote ON refreshes cannot restart it.
            self.auto_off_deadline[name] = None
            self.set_state(name, False, force=True, event_source="manual/timer")

    def label_for_channel_id(self, channel_id: str) -> str:
        chan = (channel_id or "").strip().lower()
        if not chan:
            return ""
        for label, cid in (self.channel_id_for_label or {}).items():
            if str(cid or "").strip().lower() == chan:
                return label
        return ""

    def _set_switch_state(
        self,
        name: str,
        on: bool,
        *,
        event_origin: str = "manual",
        event_label: str = "",
    ) -> bool:
        # 1) Try device backend first
        if hasattr(self, "switch") and hasattr(self.switch, "set_state"):
            if bool(self.switch.set_state(name, on)):
                return True

        # 2) Fallback to MQTT ingest (remote Nodus) if available
        try:
            from .saiMQTTIngest import get_current_ingest  # small helper you’ll add below
            ing = get_current_ingest()
            if ing:
                return bool(
                    ing.set_switch(
                        self.switch_id,
                        name,
                        on,
                        event_origin=event_origin,
                        event_label=event_label,
                    )
                )
        except Exception:
            pass

        printDM(f"Backend switch object missing set_state() for '{name}' and no ingest fallback", location=MODULE)
        return False


    def set_state(self, name, on: bool, *, force: bool = False, event_source: str = "manual/ui"):
        now = time.monotonic()
        prev_on = self.get_state(name)
        event_source_text = str(event_source or "").strip()
        event_origin = "auto" if event_source_text.lower().startswith("auto") else "manual"
        event_label = ""
        if event_source_text.lower().startswith("auto/rule:"):
            event_label = event_source_text.split(":", 1)[1].strip()
        if self.override_script.get(name, False):
            printDM(f"Override active: {name} forced to {on}", location=MODULE)
            ok = self._set_switch_state(name, on, event_origin=event_origin, event_label=event_label)
            if ok:
                self.last_state[name] = on                     # <-- keep RAM state in sync
                self._log(name, on, source=event_source)
                self.last_set_time[name] = now
                if event_origin == "manual":
                    self._sync_auto_off_state(name, bool(on), restart=bool(on and not prev_on))
                else:
                    self.auto_off_deadline[name] = None
            return bool(ok)

        elapsed = now - self.last_set_time.get(name, 0)
        if not force and on == prev_on:
            return False
        if on and elapsed < self.min_off_time:
            return False
        if not on and elapsed < self.min_on_time:
            return False

        printDM(f"Setting {name} to {'ON' if on else 'OFF'} (override: {self.override_script.get(name, False)})", location=MODULE)
        ok = self._set_switch_state(name, on, event_origin=event_origin, event_label=event_label)
        if ok:
            self.last_state[name] = on                         # <-- keep RAM state in sync
            self._log(name, on, source=event_source)
            self.last_set_time[name] = now
            if event_origin == "manual":
                self._sync_auto_off_state(name, bool(on), restart=bool(on and not prev_on))
            else:
                self.auto_off_deadline[name] = None

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
            from .saiAutomationManager import AutomationManager
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
            from .saiAutomationManager import AutomationManager
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

                from .saiSwitchSettingsManager import SwitchSettingsManager
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

    def _automation_condition_report(
        self,
        cond: dict,
        result: bool,
        current_values_map: dict,
    ) -> str:
        """Format one evaluated automation condition for an email report."""
        ctype = str(cond.get("type", "") or "").strip().lower()
        status = "TRUE" if result else "FALSE"

        raw_days = cond.get("days") or []
        day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = []
        for raw_day in raw_days:
            try:
                day_num = int(raw_day)
            except Exception:
                continue
            if 0 <= day_num <= 6:
                days.append(day_names[day_num])
        day_text = ",".join(days) if days else "all days"

        if ctype == "sensor":
            sensor_id = str(cond.get("sensor", "") or "").strip()
            metric = str(cond.get("metric", "") or "").strip()
            op = str(cond.get("op", ">") or ">").strip()
            threshold = cond.get("value")
            hyst = cond.get("hyst", 0)
            actual = None
            values = self._get_values_for_sensor(sensor_id, current_values_map)
            if metric in values:
                actual = values.get(metric)
            else:
                wanted = metric.lower().replace("-", "").replace("_", "").replace(" ", "")
                for key, value in values.items():
                    normalized = str(key).lower().replace("-", "").replace("_", "").replace(" ", "")
                    if normalized == wanted:
                        actual = value
                        break

            boundary_text = ""
            try:
                threshold_num = float(threshold)
                hyst_num = float(hyst or 0)
                if op == ">":
                    boundary_text = (
                        f"; trigger > {threshold_num + hyst_num:g}; "
                        f"clear <= {threshold_num - hyst_num:g}"
                    )
                elif op == "<":
                    boundary_text = (
                        f"; trigger < {threshold_num - hyst_num:g}; "
                        f"clear >= {threshold_num + hyst_num:g}"
                    )
            except Exception:
                pass
            actual_text = "unavailable" if actual is None else str(actual)
            return (
                f"[{status}] Sensor {sensor_id}; value {actual_text}; "
                f"{metric} {op} {threshold}; hysteresis {hyst}{boundary_text}"
            )

        if ctype == "time":
            start = str(cond.get("start", "00:00") or "00:00")
            end = str(cond.get("end", "24:00") or "24:00")
            now_text = datetime.now().astimezone().strftime("%H:%M %Z")
            return f"[{status}] Time of day {start}-{end}; {day_text}; now {now_text}"

        if ctype == "astral":
            event = str(cond.get("astral_event", cond.get("event", "sunrise")) or "sunrise")
            event = event.replace("_", " ")
            offset = int(cond.get("offset_min", cond.get("offset_minutes", 0)) or 0)
            sign = "+" if offset >= 0 else ""
            return f"[{status}] Astral {event}; offset {sign}{offset} min; {day_text}"

        if ctype == "timer":
            duration = int(cond.get("duration_min", 1) or 1)
            period = cond.get("period_min")
            if period is None:
                period = int(cond.get("freq_hours", 1) or 1) * 60
            return f"[{status}] Timer active {duration} min every {int(period)} min"

        if ctype == "bd_transitions":
            transition = cond.get("_bd_transition")
            if isinstance(transition, dict):
                from_text = self._biodynamic_segment_text(transition.get("from"))
                to_text = self._biodynamic_segment_text(transition.get("to"))
                transition_at = self._format_biodynamic_transition_time(
                    transition.get("transition_at")
                )
                return (
                    f"[{status}] Biodynamic Calendar Transition at {transition_at}; "
                    f"From {from_text}; To {to_text}"
                )
            return f"[{status}] Biodynamic Calendar Transition"

        return f"[{status}] {ctype or 'unknown'} condition"

    @staticmethod
    def _biodynamic_segment_text(segment) -> str:
        if not isinstance(segment, dict):
            return "Unknown"
        values = [
            str(segment.get("sign") or "").strip(),
            str(segment.get("element") or "").strip(),
            str(segment.get("plant_part") or "").strip(),
        ]
        return " / ".join(value for value in values if value) or "Unknown"

    @staticmethod
    def _format_biodynamic_transition_time(value) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "unknown time"
        try:
            parsed = datetime.fromisoformat(raw)
            formatted = parsed.strftime("%b %d, %Y %I:%M %p %Z")
            return formatted.replace(" 0", " ")
        except Exception:
            return raw

    def _get_current_biodynamic_transition(self) -> dict:
        """Return the current biodynamic segment used by transition automations."""
        try:
            from .saiBiodynamicCalendarApp import get_registered_biodynamic_calendar_service

            shared_service = get_registered_biodynamic_calendar_service()
            if shared_service is not None:
                transition = shared_service.current_transition_sync()
                if isinstance(transition, dict) and transition.get("transition_at"):
                    return dict(transition)

            # Startup/standalone compatibility before the shared web service is registered.
            from .saiBiodynamics import get_biodynamic_local_now, get_biodynamic_payload

            now_local = get_biodynamic_local_now()
            payload = get_biodynamic_payload(now_local.date())
            current = payload.get("current") if isinstance(payload, dict) else {}
            if not bool(payload.get("ok")) or not isinstance(current, dict):
                return {}
            transition_at = str(current.get("window_start") or "").strip()
            if not transition_at:
                return {}
            return {
                "transition_at": transition_at,
                "sign": str(current.get("sign") or "").strip(),
                "element": str(current.get("element") or "").strip(),
                "plant_part": str(current.get("plant_part") or "").strip(),
                "color": str(current.get("color") or "").strip(),
                "accent": str(current.get("accent") or "").strip(),
            }
        except Exception as exc:
            if DEBUG:
                printDM(f"[advanced] biodynamic transition unavailable: {exc}", location=MODULE)
            return {}

    def _broadcast_biodynamic_transition(self, transition: dict) -> None:
        """Publish a biodynamic transition to connected dashboard clients."""
        try:
            from . import saiWebRoutes as routes

            bcast = getattr(routes, "app", None)
            switch_broadcast = getattr(
                getattr(bcast, "state", object()),
                "switch_broadcast",
                None,
            )
            if not switch_broadcast:
                printDM(
                    "[advanced] BD transition toast unavailable: dashboard broadcaster is not registered",
                    location=MODULE,
                    level="warning",
                )
                return
            payload = {
                "type": "bd_transition",
                "transition_at": str(transition.get("transition_at") or ""),
                "from": dict(transition.get("from") or {}),
                "to": dict(transition.get("to") or {}),
            }
            asyncio.create_task(switch_broadcast(payload))
        except Exception as exc:
            printDM(
                f"[advanced] BD transition toast broadcast failed: {exc}",
                location=MODULE,
                level="warning",
            )

    def _automation_action_report(self, action: dict) -> str:
        """Format one configured automation action for an email report."""
        action_type = str(action.get("type", "switch") or "switch").strip().lower()
        if action_type == "none":
            return "None: biodynamic transition toast only"
        if action_type == "notify":
            return f"Notify: email {str(action.get('to', '') or '').strip()}"

        switch_key = str(action.get("switch_key", "") or "").strip()
        display_key = switch_key.replace("::", ":", 1)
        if "::" in switch_key:
            sid, suffix = switch_key.split("::", 1)
            try:
                from .saiSwitchSettingsManager import SwitchSettingsManager

                doc = SwitchSettingsManager("switch_settings").load(sid) or {}
                sw = doc.get("Switch") or {}
                for idx in range(1, 33):
                    label = str(sw.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                    channel_id = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                    if label and suffix.lower() in {label.lower(), channel_id.lower()}:
                        display_key = f"{sid}:{label}"
                        break
            except Exception:
                pass

        state = "On" if bool(action.get("set", True)) else "Off"
        revert = (
            "Previous State"
            if str(action.get("revert_action", "") or "").strip().lower() == "previous_state"
            else "Do Nothing"
        )
        delay = max(0, int(action.get("delay_s", 0) or 0))
        return f"Switch {display_key}: {state}; revert {revert}; delay {delay} sec"

    def _automation_subject_metrics(
        self,
        evaluated_groups: list[dict],
        current_values_map: dict,
        *,
        triggered: bool,
    ) -> list[str]:
        """Return live sensor metric summaries for groups causing a notification."""
        summaries = []
        seen = set()
        for group in evaluated_groups:
            if bool(group.get("result", False)) != bool(triggered):
                continue
            for cond, _result in group.get("conditions", []):
                if str(cond.get("type", "") or "").strip().lower() != "sensor":
                    continue
                sensor_id = str(cond.get("sensor", "") or "").strip()
                metric = str(cond.get("metric", "") or "").strip()
                values = self._get_values_for_sensor(sensor_id, current_values_map)
                actual = values.get(metric)
                if actual is None:
                    wanted = metric.lower().replace("-", "").replace("_", "").replace(" ", "")
                    for key, value in values.items():
                        normalized = str(key).lower().replace("-", "").replace("_", "").replace(" ", "")
                        if normalized == wanted:
                            actual = value
                            break
                if actual is None:
                    continue
                summary_key = (sensor_id.lower(), metric.lower())
                if summary_key in seen:
                    continue
                seen.add(summary_key)
                unit = ""
                try:
                    from .saiHomeAssistantMqtt import metric_meta_for_metric

                    unit = str(metric_meta_for_metric(metric).get("unit", "") or "")
                except Exception:
                    pass
                summaries.append(f"{metric} was {actual}{unit}")
        return summaries

    def _build_automation_notification(
        self,
        *,
        rule_id: str,
        rule_name: str,
        triggered: bool,
        evaluated_groups: list[dict],
        actions: list[dict],
        current_values_map: dict,
    ) -> tuple[str, str]:
        """Build the subject and body for a triggered or cleared automation."""
        notification_state = "ACTIVATED" if triggered else "CLEARED"
        display_name = rule_name or rule_id
        subject = f"Sensorius {notification_state}: {display_name}"
        bd_transition = None
        for group in evaluated_groups:
            for cond, result in group.get("conditions", []):
                candidate = cond.get("_bd_transition") if isinstance(cond, dict) else None
                if bool(result) and isinstance(candidate, dict):
                    bd_transition = candidate
                    break
            if bd_transition:
                break
        if triggered and bd_transition:
            from_text = self._biodynamic_segment_text(bd_transition.get("from"))
            to_text = self._biodynamic_segment_text(bd_transition.get("to"))
            transition_at = self._format_biodynamic_transition_time(
                bd_transition.get("transition_at")
            )
            subject = (
                f"Sensorius BD Transition: {from_text} to {to_text} "
                f"at {transition_at}"
            )
        metric_summaries = self._automation_subject_metrics(
            evaluated_groups,
            current_values_map,
            triggered=triggered,
        )
        if metric_summaries:
            subject += ": " + "; ".join(metric_summaries)
        evaluated_at = datetime.now().astimezone().isoformat()
        hub = socket.gethostname() or "unknown"

        body_lines = [
            "Sensorius automation notification",
            "",
            "Conditions (AND within each group; OR between groups):",
        ]
        for group_index, group in enumerate(evaluated_groups, start=1):
            group_state = "TRUE" if bool(group.get("result", False)) else "FALSE"
            if group_index > 1:
                body_lines.append("OR")
            body_lines.append(f"Group {group_index}: {group_state}")
            for cond, result in group.get("conditions", []):
                body_lines.append(
                    self._automation_condition_report(
                        cond,
                        bool(result),
                        current_values_map,
                    )
                )

        body_lines.extend([
            "",
            f"State: {notification_state}",
            f"Automation: {display_name}",
            f"Rule ID: {rule_id}",
            f"Evaluation time: {evaluated_at}",
            f"Hub: {hub}",
            "",
            "Configured actors:",
        ])
        for action in actions:
            body_lines.append("- " + self._automation_action_report(action))

        return subject, "\n".join(body_lines)

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
                        either period_min (minutes) or legacy freq_hours.
                        Hour-based rules still align to on-the-hour periods.
                        Minute-based rules may use anchor_epoch to start at save time.
            * "bd_transitions": true for one evaluation pass when the current
                        biodynamic calendar segment changes. The condition also
                        publishes a persistent dashboard toast.
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
        debug_cycle_verbose = False
        if DEBUG:
            debug_now = time.monotonic()
            if debug_now >= float(
                getattr(self, "_advanced_debug_next_idle_log_at", 0.0) or 0.0
            ):
                self._advanced_debug_next_idle_log_at = (
                    debug_now + _ADVANCED_IDLE_LOG_INTERVAL_S
                )
                debug_cycle_verbose = True
        self._advanced_debug_cycle_verbose = debug_cycle_verbose
        try:
            triggers = self._load_triggers_dict()
            advanced = triggers.get("Advanced") or {}
            if debug_cycle_verbose:
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
        bd_condition_results: dict[str, bool] = {}

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

        def _eval_single_condition(
            cond: dict,
            target_label: str,
            current_action_state: bool | None = None,
            rule_id: str = "",
        ) -> bool:
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
                # duration_min: active window length in minutes
                # period_min:   repeat period in minutes
                # freq_hours:   legacy hourly repeat period
                try:
                    duration_min = int(cond.get("duration_min") or 0)
                except Exception:
                    duration_min = 0
                try:
                    freq_hours = int(cond.get("freq_hours") or 0)
                except Exception:
                    freq_hours = 0
                try:
                    period_min = int(cond.get("period_min") or 0)
                except Exception:
                    period_min = 0
                if period_min <= 0 and freq_hours > 0:
                    period_min = freq_hours * 60
                try:
                    anchor_epoch = int(cond.get("anchor_epoch") or 0)
                except Exception:
                    anchor_epoch = 0

                if duration_min <= 0 or period_min <= 0 or duration_min >= period_min:
                    return False

                period_sec = max(period_min, 1) * 60
                duration_sec = max(1, min(duration_min * 60, period_sec - 1))

                if anchor_epoch > 0:
                    phase = max(0, int(time.time()) - anchor_epoch) % period_sec
                else:
                    # Preserve legacy on-the-hour alignment for hourly timers.
                    phase = seconds_since_midnight % period_sec
                return phase < duration_sec

            # --- BIODYNAMIC TRANSITION CONDITION -----------------------------
            if ctype == "bd_transitions":
                executor_sid = str(cond.get("executor_switch_id", "") or "").strip()
                own_sid = str(getattr(self, "switch_id", "") or "").strip()
                if executor_sid and executor_sid.lower() != own_sid.lower():
                    return False

                state_key = str(rule_id or "bd_transitions")
                if state_key in bd_condition_results:
                    return bd_condition_results[state_key]

                transition = self._get_current_biodynamic_transition()
                transition_key = str(transition.get("transition_at") or "").strip()
                transition_keys = getattr(self, "_advanced_bd_transition_keys", None)
                if not isinstance(transition_keys, dict):
                    transition_keys = {}
                    self._advanced_bd_transition_keys = transition_keys
                transition_segments = getattr(
                    self,
                    "_advanced_bd_transition_segments",
                    None,
                )
                if not isinstance(transition_segments, dict):
                    transition_segments = {}
                    self._advanced_bd_transition_segments = transition_segments
                previous_key = str(transition_keys.get(state_key) or "").strip()
                previous_segment = transition_segments.get(state_key)
                triggered = bool(
                    transition_key
                    and previous_key
                    and transition_key != previous_key
                )
                if transition_key:
                    transition_keys[state_key] = transition_key
                    transition_segments[state_key] = {
                        "sign": str(transition.get("sign") or ""),
                        "element": str(transition.get("element") or ""),
                        "plant_part": str(transition.get("plant_part") or ""),
                        "color": str(transition.get("color") or ""),
                        "accent": str(transition.get("accent") or ""),
                    }
                bd_condition_results[state_key] = triggered
                if triggered:
                    transition_event = {
                        "transition_at": transition_key,
                        "from": dict(previous_segment or {}),
                        "to": dict(transition_segments[state_key]),
                    }
                    cond["_bd_transition"] = transition_event
                    self._broadcast_biodynamic_transition(transition_event)
                return triggered

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
                curr_state = (
                    bool(current_action_state)
                    if current_action_state is not None
                    else bool(self.get_state(target_label))
                )

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
        notify_states = getattr(self, "_advanced_notify_states", None)
        if not isinstance(notify_states, dict):
            notify_states = {}
            self._advanced_notify_states = notify_states
        persist_active_actions = False

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
                    action_type = str(act.get("type", "switch") or "switch").strip().lower()
                    has_bd_transition = any(
                        str(cond.get("type", "") or "").strip().lower()
                        == "bd_transitions"
                        for group in groups
                        for cond in group
                    )
                    if (
                        action_type == "switch"
                        and not str(act.get("switch_key", "") or "").strip()
                        and has_bd_transition
                    ):
                        # Compatibility for BD None actors saved by v0.26.209.4,
                        # whose route normalizer rewrote them as empty switches.
                        action_type = "none"
                    if action_type == "none":
                        executor_sid = str(act.get("executor_switch_id", "") or "").strip()
                        own_sid = str(getattr(self, "switch_id", "") or "").strip()
                        if not executor_sid:
                            executor_sid = next(
                                (
                                    str(cond.get("executor_switch_id", "") or "").strip()
                                    for group in groups
                                    for cond in group
                                    if str(cond.get("type", "") or "").strip().lower()
                                    == "bd_transitions"
                                    and str(cond.get("executor_switch_id", "") or "").strip()
                                ),
                                "",
                            )
                        if executor_sid and executor_sid.lower() != own_sid.lower():
                            continue
                        if (
                            not executor_sid
                            and own_sid.lower()
                            != str(socket.gethostname() or "").strip().lower()
                        ):
                            continue
                        for group in groups:
                            for cond in group:
                                _eval_single_condition(
                                    cond,
                                    "",
                                    rule_id=str(_rule_id),
                                )
                        continue
                    if action_type == "notify":
                        executor_sid = str(act.get("executor_switch_id", "") or "").strip()
                        own_sid = str(getattr(self, "switch_id", "") or "").strip()
                        if not executor_sid or executor_sid.lower() != own_sid.lower():
                            continue
                        recipient = str(act.get("to", "") or "").strip()
                        notify_key = (str(_rule_id), recipient)
                        delivery_service = getattr(self, "email_delivery_service", None)
                        if delivery_service is not None:
                            was_active = bool(
                                delivery_service.persisted_automation_state(
                                    str(_rule_id),
                                    recipient,
                                )
                            )
                        else:
                            was_active = bool(notify_states.get(notify_key, False))
                        evaluated_groups = []
                        for group in groups:
                            evaluated_conditions = []
                            group_ok = True
                            for cond in group:
                                condition_ok = bool(
                                    _eval_single_condition(
                                        cond,
                                        "",
                                        current_action_state=was_active,
                                        rule_id=str(_rule_id),
                                    )
                                )
                                evaluated_conditions.append((cond, condition_ok))
                                if not condition_ok:
                                    group_ok = False
                            evaluated_groups.append(
                                {
                                    "result": group_ok,
                                    "conditions": evaluated_conditions,
                                }
                            )
                        rule_ok = any(
                            bool(group.get("result", False))
                            for group in evaluated_groups
                        )
                        should_notify = (
                            rule_ok != was_active
                            and recipient
                            and (rule_ok or not has_bd_transition)
                        )
                        if should_notify:
                            rule_name = str(script.get("name", "") or _rule_id).strip()
                            subject, body = self._build_automation_notification(
                                rule_id=str(_rule_id),
                                rule_name=rule_name,
                                triggered=rule_ok,
                                evaluated_groups=evaluated_groups,
                                actions=list(actions),
                                current_values_map=current_values_map,
                            )

                            if delivery_service is None:
                                printDM(
                                    f"[advanced] Notify delivery service unavailable for {recipient}",
                                    location=MODULE,
                                    level="warning",
                                )
                            else:
                                delivery_service.enqueue_automation_transition(
                                    rule_id=str(_rule_id),
                                    triggered=rule_ok,
                                    recipient=recipient,
                                    subject=subject,
                                    body=body,
                                )
                        elif delivery_service is None:
                            notify_states[notify_key] = rule_ok
                        continue

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
                    if target_sid.lower() != str(getattr(self, "switch_id", "") or "").strip().lower():
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
                            if not _eval_single_condition(
                                cond,
                                target_label,
                                rule_id=str(_rule_id),
                            ):
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
                        "rule_name": str(script.get("name", "") or "").strip(),
                        "conditions": list(conditions),
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
            nonlocal persist_active_actions
            revert_action = str(active.get("revert_action", "") or "").strip().lower()
            target_label = str(active.get("target_label", "") or "").strip()
            if not target_label:
                active_actions.pop(action_key, None)
                persist_active_actions = True
                return True
            if revert_action != "previous_state":
                active_actions.pop(action_key, None)
                persist_active_actions = True
                return True

            revert_to = bool(active.get("revert_to", False))
            current_state = bool(self.get_state(target_label))
            if current_state == revert_to:
                active_actions.pop(action_key, None)
                persist_active_actions = True
                return True

            rule_name = str(active.get("rule_name", "") or "").strip()
            event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
            ok = bool(self.set_state(target_label, revert_to, force=True, event_source=event_source))
            if ok or bool(self.get_state(target_label)) == revert_to:
                active_actions.pop(action_key, None)
                persist_active_actions = True
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
                if active:
                    _revert_active_action(action_key, active)
                elif action_key not in self._advanced_revert_cooldown:
                    self._advanced_revert_cooldown.add(action_key)
                    if self._recover_advanced_revert_from_history(info, current_state):
                        if DEBUG:
                            printDM(
                                f"[advanced] {target_label} rule {info['rule_id']} recovered previous_state from history",
                                location=MODULE,
                            )
                if debug_cycle_verbose:
                    printDM(
                        f"[advanced] {target_label} rule {info['rule_id']} no-op: "
                        f"rule_ok={rule_ok} group_results={info.get('group_results')}",
                        location=MODULE,
                    )
                continue

            self._advanced_revert_cooldown.discard(action_key)

            if active:
                if current_state == desired:
                    if debug_cycle_verbose:
                        printDM(
                            f"[advanced] {target_label} rule {info['rule_id']} skipped: "
                            f"curr={current_state} desired={desired} switch_key={skey}",
                            location=MODULE,
                        )
                    continue
                rule_name = str(active.get("rule_name", "") or "").strip()
                event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
                ok = bool(self.set_state(target_label, desired, event_source=event_source))
                if ok:
                    active_actions[action_key] = dict(active, last_applied_at=now_mono)
                    persist_active_actions = True
                continue

            if pending:
                due_at = float(pending.get("due_at", 0.0) or 0.0)
                if due_at > now_mono:
                    continue
                pending_actions.pop(action_key, None)
                if current_state == desired:
                    if debug_cycle_verbose:
                        printDM(
                            f"[advanced] {target_label} rule {info['rule_id']} skipped after delay: "
                            f"curr={current_state} desired={desired} switch_key={skey}",
                            location=MODULE,
                        )
                    continue
                if DEBUG:
                    printDM(
                        f"[advanced] applying delayed rule {info['rule_id']} to '{target_label}': "
                        f"desired={desired} (switch_key={skey})",
                        location=MODULE,
                    )
                rule_name = str(info.get("rule_name", "") or "").strip()
                event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
                ok = bool(self.set_state(target_label, desired, event_source=event_source))
                if not ok:
                    pending_actions[action_key] = dict(pending, due_at=time.monotonic() + 1.0)
                    continue
                active_actions[action_key] = {
                    "rule_id": str(info.get("rule_id", "") or "").strip(),
                    "rule_name": str(info.get("rule_name", "") or "").strip(),
                    "target_label": target_label,
                    "switch_key": skey,
                    "desired": desired,
                    "revert_action": revert_action,
                    "revert_to": current_state,
                    "activated_at": now_mono,
                }
                persist_active_actions = True
                if revert_action == "previous_state":
                    self._advanced_revert_cooldown.add(action_key)
                continue

            if delay_s > 0:
                pending_actions[action_key] = {
                    "due_at": now_mono + min(delay_s, 300),
                    "rule_name": str(info.get("rule_name", "") or "").strip(),
                    "target_label": target_label,
                    "switch_key": skey,
                    "desired": desired,
                    "revert_action": revert_action,
                }
                continue

            if current_state == desired:
                if debug_cycle_verbose:
                    printDM(
                        f"[advanced] {target_label} rule {info['rule_id']} skipped: "
                        f"curr={current_state} desired={desired} switch_key={skey}",
                        location=MODULE,
                    )
                continue

            if DEBUG:
                printDM(
                    f"[advanced] applying rule {info['rule_id']} to '{target_label}': "
                    f"rule_ok={rule_ok} desired={desired} (switch_key={skey})",
                    location=MODULE,
                )
            if DEBUG:
                printDM(
                    f"[advanced] {target_label} apply {info['rule_id']}: "
                    f"curr={current_state} desired={desired} switch_key={skey}",
                    location=MODULE,
                )

            rule_name = str(info.get("rule_name", "") or "").strip()
            event_source = f"auto/rule:{rule_name}" if rule_name else "auto/rule"
            ok = bool(self.set_state(target_label, desired, event_source=event_source))
            if not ok:
                continue

            active_actions[action_key] = {
                "rule_id": str(info.get("rule_id", "") or "").strip(),
                "rule_name": str(info.get("rule_name", "") or "").strip(),
                "target_label": target_label,
                "switch_key": skey,
                "desired": desired,
                "revert_action": revert_action,
                "revert_to": current_state,
                "activated_at": now_mono,
            }
            persist_active_actions = True
            if revert_action == "previous_state":
                self._advanced_revert_cooldown.add(action_key)
            else:
                self._advanced_revert_cooldown.discard(action_key)

        if persist_active_actions:
            self._persist_advanced_runtime_state()
                
    async def run_controladora_monitor(self, sensor, interval=5):
        """
        Periodically evaluate switch rules.
        - If any enabled automation rules are present,
          we will run evaluation each cycle.
        - If a bound sensor is present and healthy, include its current dataset.
          Otherwise, we evaluate with the last known values (if any) or {}.
        """
        heartbeat_every_s = 10.0
        labels = []
        try:
            labels = list(self.get_switch_names() or [])
        except Exception:
            labels = []
        if DEBUG:
            printDM(
                f"[monitor-start] switch_id={self.switch_id} remote={int(bool(getattr(self, 'is_remote', False)))} "
                f"sensor_bound={int(bool(sensor or getattr(self, 'sensor', None)))} labels={labels}",
                location=MODULE,
            )

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
            tick_no = int(getattr(self, "_monitor_tick_count", 0) or 0) + 1
            self._monitor_tick_count = tick_no
            rules_check_ms = 0.0
            snapshot_ms = 0.0
            eval_ms = 0.0
            rules_present = False
            try:
                if DEBUG and tick_no == 1:
                    printDM(
                        f"[monitor-first-tick] {self.switch_id} entering monitor loop",
                        location=MODULE,
                    )
                # Decide if we should do any work this tick
                rules_check_started = time.monotonic()
                rules_present = self._rules_enabled()
                rules_check_ms = (time.monotonic() - rules_check_started) * 1000.0
                if not rules_present:
                    if DEBUG and bool(
                        getattr(self, "_advanced_debug_cycle_verbose", False)
                    ):
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
            if DEBUG and bool(
                getattr(self, "_advanced_debug_cycle_verbose", False)
            ):
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
        self._settings_signature = None
        super().__init__(switch_settings=switch_settings, supervisor=supervisor, sensor=sensor, data_logger=data_logger)

    def _capture_settings_signature(self, sw_block: dict) -> tuple:
        sig: list[tuple[str, object]] = []
        for idx in range(1, 9):
            sig.append((f"L{idx}", str(sw_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()))
            sig.append((f"C{idx}", str(sw_block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()))
            sig.append((f"E{idx}", str(sw_block.get(f"SWITCH_{idx}_ENABLE_PIN", sw_block.get(f"SWITCH_{idx}_EN", "")) or "").strip()))
        sig.append(("LOC", str(sw_block.get("SWITCH_LOCATION", "") or "").strip()))
        return tuple(sig)

    def _apply_remote_settings_doc(self, doc: dict) -> None:
        sw = doc.get("Switch", {}) if isinstance(doc, dict) else {}
        if not isinstance(sw, dict):
            return

        prior_last_state = dict(getattr(self, "last_state", {}) or {})
        prior_override = dict(getattr(self, "override_script", {}) or {})
        prior_last_set_time = dict(getattr(self, "last_set_time", {}) or {})
        prior_auto_off_seconds = dict(getattr(self, "auto_off_seconds", {}) or {})
        prior_auto_off_deadline = dict(getattr(self, "auto_off_deadline", {}) or {})

        if hasattr(self.settings, "settings") and isinstance(getattr(self.settings, "settings"), dict):
            self.settings.settings = doc
        else:
            self.settings = doc

        self.location = str(sw.get("SWITCH_LOCATION", self.location) or self.location).strip()
        self.switch = create_switch(settings=self.settings, mqtt_client=self.mqtt)
        self.is_present = bool(getattr(self.switch, "is_present", False))

        self.last_state = {}
        self.override_script = {}
        self.last_set_time = {}
        self.auto_off_seconds = {}
        self.auto_off_deadline = {}
        self.channel_id_for_label = {}

        labels = list(self.switch.get_switch_names() or [])
        for idx in range(1, 9):
            label = str(sw.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
            if not label or label not in labels:
                continue
            self.last_state[label] = bool(prior_last_state.get(label, sw.get(f"SWITCH_{idx}_LAST_STATE", False)))
            self.override_script[label] = bool(prior_override.get(label, sw.get(f"SWITCH_{idx}_OVERRIDE_SCRIPT", False)))
            self.last_set_time[label] = float(prior_last_set_time.get(label, 0.0) or 0.0)
            self.auto_off_seconds[label] = int(prior_auto_off_seconds.get(label, 0) or 0)
            self.auto_off_deadline[label] = prior_auto_off_deadline.get(label)
            channel_id = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
            if channel_id:
                self.channel_id_for_label[label] = channel_id

        self._settings_signature = self._capture_settings_signature(sw)

    def _refresh_definition_from_settings(self) -> None:
        try:
            from .saiSwitchSettingsManager import SwitchSettingsManager

            mgr = SwitchSettingsManager("switch_settings")
            doc = mgr.load(self.switch_id) or {}
            sw = doc.get("Switch", {}) if isinstance(doc, dict) else {}
            if not isinstance(sw, dict):
                return
            signature = self._capture_settings_signature(sw)
            if signature == self._settings_signature:
                return
            self._apply_remote_settings_doc(doc)
        except Exception:
            return

    def _pending_state_from_ingest(self, ing, sid: str, label: str, channel_id: str) -> bool | None:
        try:
            pending_map = getattr(ing, "_pending_set", {}) or {}
            now_ts = time.time()
            pending_ttl_s = 15.0

            pending = pending_map.get((str(sid or ""), str(label or "")))
            if pending is None and channel_id:
                for (psid, _plabel), meta in pending_map.items():
                    if str(psid or "").strip() != str(sid or "").strip():
                        continue
                    meta_channel = str((meta or {}).get("channel_id") or "").strip()
                    if meta_channel and meta_channel == channel_id:
                        pending = meta
                        break
            if not isinstance(pending, dict):
                return None

            pending_ts = float(pending.get("ts") or 0.0)
            if pending_ts <= 0.0 or (now_ts - pending_ts) > pending_ttl_s:
                return None
            if "state" not in pending:
                return None
            return bool(pending.get("state"))
        except Exception:
            return None

    def _refresh_state_from_ingest(self) -> None:
        try:
            ing = self.mqtt_ingest
            if ing is None:
                from .saiMQTTIngest import get_current_ingest
                ing = get_current_ingest()
            if ing is None:
                return

            sid = str(getattr(self, "switch_id", "") or "").strip()
            if not sid:
                return

            ch_map = (getattr(ing, "_switch_state_cache", {}) or {}).get(sid, {}) or {}
            if not isinstance(ch_map, dict):
                return

            for label in (self.get_switch_names() or []):
                channel_id = str((self.channel_id_for_label or {}).get(label, "") or "").strip()
                pending_state = self._pending_state_from_ingest(ing, sid, str(label or ""), channel_id)
                if pending_state is not None:
                    new_state = bool(pending_state)
                else:
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
                    self._sync_auto_off_state(
                        label,
                        new_state,
                        restart=False,
                        allow_create_if_missing=False,
                    )
        except Exception:
            return

    def get_state(self, name):
        self._refresh_definition_from_settings()
        self._refresh_state_from_ingest()
        return super().get_state(name)

    def get_switch_names(self) -> list[str]:
        self._refresh_definition_from_settings()
        return super().get_switch_names()

    def set_state(self, name, on: bool, *, force: bool = False, event_source: str = "manual/ui"):
        self._refresh_definition_from_settings()
        return super().set_state(name, on, force=force, event_source=event_source)


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
