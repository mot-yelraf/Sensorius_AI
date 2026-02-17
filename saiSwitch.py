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
import board
from pathlib import Path
from saiUtils import printDM, debug_enabled, get_timestamp
from saiSwitchFactory import create_switch
from saiMQTTClient import get_mqtt_client
from saiDataLogger import saiDataLogger
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
    def __init__(self, switch_settings=None, supervisor=None, sensor=None):
        self.supervisor = supervisor
        self.sensor = sensor
        self.settings = switch_settings or {}
        self.is_present = False
        self.data_logger = saiDataLogger()

        # State & policy
        self.last_state = {}
        self.override_script = {}
        self.last_set_time = {}
        self.min_on_time = 5
        self.min_off_time = 5
        self._advanced_delay_due = {}

        # Settings accessor that works with either wrapper or dict
        try:
            get = self.settings.get_setting
        except AttributeError:
            def get(section, key, default=None):
                return (self.settings or {}).get(section, {}).get(key, default)

        sw = self._switch_block()
        sw_type = str(sw.get("TYPE", "") or "").strip().lower()
        has_en_keys = ("SWITCH_1_ENABLE_PIN" in sw) or ("SWITCH_2_ENABLE_PIN" in sw)

        def _enable_field_value(sw_map: dict, idx: int):
            return sw_map.get(f"SWITCH_{idx}_ENABLE_PIN", "")

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

        self.script_rules = {}
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

        # parse any embedded TriggerScript JSON from [Switch] now
        try:
            self.script_rules = self._parse_trigger_scripts() or {}
        except Exception as e:
            printDM(f"Init parse TriggerScripts failed: {e}", location=MODULE)
            self.script_rules = {}

    # ---------- helpers: switch_key & db convenience --------------------------
    def reload_settings(self, new_settings):
        self.settings = new_settings
        self.script_rules = self._parse_trigger_scripts()

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

    def _parse_trigger_scripts(self):
        rules = {}
        for key in self.settings.get("Switch", {}):
            if key.startswith("SWITCH_") and key.endswith("_TriggerScript"):
                base = key.replace("_TriggerScript", "")
                label = self.settings["Switch"].get(base)
                script_str = self.settings["Switch"].get(key)
                if label and script_str:
                    try:
                        rules[label] = json.loads(script_str)
                    except Exception as e:
                        printDM(f"Failed to parse TriggerScript for {label}: {e}", location=MODULE)
        return rules

    def _time_in_window(self, start: str, end: str, now_str: str) -> bool:
        # Inclusive start, exclusive end: [start, end)
        if not start and not end:
            return True
        s = (start or "00:00")
        e = (end or "24:00")
        # handle wrap-around (e.g., 22:00–06:00)
        return (s <= now_str < e) if s <= e else (now_str >= s) or (now_str < e)

    def _log(self, name, on: bool):
        # Persist as a SWITCH EVENT (not a generic reading) so /switch-status-update
        # can fetch the latest “On”/“Off” and recent event list.
        from saiDataLogger import saiDataLogger
        from saiUtils import get_timestamp
        logger = saiDataLogger()
        switch_key = self._switch_key(name)
        sensor_lineage = f"Switch_{self.switch_id}" if getattr(self, "switch_id", None) else None
        # Use your dedicated API (present in saiDataLogger) for switch events:
        logger.log_switch_event(
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
                    "state": bool(on),      # True / False
                    "timestamp": get_timestamp(),
                    "source": "manual/ui",
                }
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
        if self.override_script.get(name, False):
            printDM(f"Override active: {name} forced to {on}", location=MODULE)
            ok = self._set_switch_state(name, on)
            if ok:
                self.last_state[name] = on                     # <-- keep RAM state in sync
                self._log(name, on)
                self.last_set_time[name] = now
            return bool(ok)

        prev_on = self.get_state(name)
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
        Compute .../switch_settings/<SWITCH_ID>/automations.toml using the settings manager.
        Returns None if path cannot be resolved.
        """
        try:
            from saiSwitchSettingsManager import SwitchSettingsManager
            mgr = SwitchSettingsManager("switch_settings")
            switch_toml_path = Path(mgr.get_path(self.switch_id))
            return switch_toml_path.parent / "automations.toml"
        except Exception:
            return None

    def _load_triggers_dict(self) -> dict:
        """
        Uses saiAutomationManager.load_triggers(manager, switch_id) if available.
        Falls back to loading automations.toml directly via tomllib.
        Returns dict with 'Advanced' key.
        """
        # Try helper first
        try:
            from saiSwitchSettingsManager import SwitchSettingsManager
            from saiAutomationManager import load_triggers
            mgr = SwitchSettingsManager("switch_settings")
            data = load_triggers(mgr, self.switch_id) or {}
            return {"Advanced": data.get("Advanced") or {}}
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
        True if we have in-memory script rules OR enabled automations.toml rules.
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
                        script = _json.loads(str(script_json))
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

        # in-memory JSON scripts counted as rules
        if getattr(self, "script_rules", None):
            if any(self.script_rules.values()):
                return True

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
            * "timer":  periodic window based on duration_min (minutes) and
                        freq_hours (hours). True for the first duration_min
                        minutes of each freq_hours period within a day.
            * "sensor": uses hysteresis around `value` to decide if the channel
                        should be ON, based on the *current state* of the target
                        switch channel.

        - For each action:
            * Let `rule_ok` be the OR of group results for that action.
            * If rule_ok is True:  desired = action.set
              else:                desired = not action.set  (complementary)

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

                    rule_ok   = any(group_results)
                    inside_set = bool(act.get("set", True))
                    desired    = inside_set if rule_ok else (not inside_set)

                    curr = bool(self.get_state(target_label))
                    if curr == desired:
                        continue

                    delay_s = int(act.get("delay_s", 0) or 0)
                    delay_key = (str(_rule_id), str(target_label), str(skey), bool(desired))
                    if delay_s > 0:
                        now_mono = time.monotonic()
                        due_at = self._advanced_delay_due.get(delay_key)
                        if due_at is None:
                            self._advanced_delay_due[delay_key] = now_mono + min(delay_s, 300)
                            continue
                        if now_mono < due_at:
                            continue
                        self._advanced_delay_due.pop(delay_key, None)
                    else:
                        self._advanced_delay_due.pop(delay_key, None)

                    if DEBUG:
                        printDM(
                            f"[advanced] applying rule {_rule_id} to '{target_label}': "
                            f"rule_ok={rule_ok} desired={desired} (switch_key={skey})",
                            location=MODULE,
                        )

                    self.set_state(target_label, desired)

            except Exception as e:
                printDM(f"[advanced] rule error: {e}", location=MODULE)
                
    def evaluate_and_apply_scripts(self, current_values):
        now = time.monotonic()
        for name, rule in self.script_rules.items():
            if self.override_script.get(name, False):
                if DEBUG:
                    printDM(f"[evaluate_and_apply_scripts] Skipping '{name}' due to override", location=MODULE)
                continue

            current_state = self.get_state(name)
            result = self._evaluate_script(rule, current_values, current_state)
            if result is None:
                continue

            elapsed = now - self.last_set_time.get(name, 0)

            if result == current_state:
                continue
            if result and elapsed < self.min_off_time:
                continue
            if not result and elapsed < self.min_on_time:
                continue

            if self._set_switch_state(name, result):
                self.last_state[name] = result
                self.last_set_time[name] = now

    def _evaluate_script(self, rule, sensor_data, current_state: bool):
        """
        Evaluate a single per-channel rule (from in-settings TriggerScript JSON).
        Supports:
          - time-only conditions: {"type":"time","start":"HH:MM","end":"HH:MM"}
          - sensor conditions (existing behavior) with optional start/end gate
        Returns desired state (True/False) or None (no decision).
        """
        logic = (rule.get("logic", "AND") or "AND").upper()
        results = []
        now_str = time.strftime("%H:%M")

        conditions = rule.get("conditions", [])
        if not isinstance(conditions, list) or not conditions:
            return None

        for cond in conditions:
            ctype = str(cond.get("type", "") or "").strip().lower()

            # --- TIME-ONLY CONDITION ------------------------
            if ctype == "time":
                start = cond.get("start") or "00:00"
                end   = cond.get("end")   or "24:00"
                results.append(self._time_in_window(start, end, now_str))
                continue

            # --- SENSOR CONDITION (existing behavior) -------
            sid    = cond.get("sensor")
            metric = cond.get("metric")
            op     = cond.get("op", ">")

            try:
                val  = float(cond.get("value", 0))
                hyst = float(cond.get("hyst", 0))
            except Exception:
                results.append(False)
                continue

            # Optional additional time gate on the sensor condition
            start = cond.get("start")
            end   = cond.get("end")
            if start or end:
                if not self._time_in_window(start or "00:00", end or "24:00", now_str):
                    results.append(False)
                    continue

            sensor_vals = sensor_data.get(sid) if isinstance(sensor_data, dict) else None
            if not sensor_vals or metric not in sensor_vals:
                results.append(False)
                continue

            actual = sensor_vals[metric]
            try:
                threshold_hi = val + hyst
                threshold_lo = val - hyst
                if op == ">":
                    want = (actual > threshold_hi) if not current_state else (actual > threshold_lo)
                elif op == "<":
                    want = (actual < threshold_lo) if not current_state else (actual < threshold_hi)
                elif op == "==":
                    want = (actual == val)
                elif op == "!=":
                    want = (actual != val)
                else:
                    want = False
                results.append(bool(want))
            except Exception:
                results.append(False)

        return all(results) if logic == "AND" else any(results)

    async def run_controladora_monitor(self, sensor, interval=29):
        """
        Periodically evaluate switch rules.
        - If ANY rules (in-memory TriggerScript JSON or automations.toml) are enabled,
          we will run evaluation each cycle.
        - If a bound sensor is present and healthy, include its current dataset.
          Otherwise, we evaluate with the last known values (if any) or {}.
        """
        while True:
            try:
                # Decide if we should do any work this tick
                rules_present = self._rules_enabled()
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
                                raw_values, *_ = bound_sensor.current_data_set()
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
                    self.evaluate_and_apply_scripts(current_values_map)
                    self._evaluate_and_apply_advanced(current_values_map)

            except Exception as e:
                printDM(f"Switch monitor error: {e}", location=MODULE)

            # keep the dogs fed & cadence jitter
            if getattr(self, "supervisor", None) and hasattr(self.supervisor, "feedthedogs"):
                self.supervisor.feedthedogs(f"{self.switch_id} Controladora Monitor")

            await asyncio.sleep(interval + random.uniform(-0.8, 0.8))
            await asyncio.sleep(0)  # REPL hook


class RemoteSwitchController(SwitchController):
    """MQTT-backed switch controller for remote Nodus/Pico devices."""

    def __init__(self, switch_settings=None, supervisor=None, sensor=None, mqtt_ingest=None):
        self.is_remote = True
        self.mqtt_ingest = mqtt_ingest
        super().__init__(switch_settings=switch_settings, supervisor=supervisor, sensor=sensor)

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
                self.last_state[label] = str(raw).strip().lower() in ("on", "true", "1")
        except Exception:
            return

    def get_state(self, name):
        self._refresh_state_from_ingest()
        return super().get_state(name)


def build_switch_controller(*, switch_settings=None, supervisor=None, sensor=None, mqtt_ingest=None):
    if is_remote_switch_settings(switch_settings):
        return RemoteSwitchController(
            switch_settings=switch_settings,
            supervisor=supervisor,
            sensor=sensor,
            mqtt_ingest=mqtt_ingest,
        )
    return SwitchController(
        switch_settings=switch_settings,
        supervisor=supervisor,
        sensor=sensor,
    )
