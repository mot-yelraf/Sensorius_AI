# saiSwitchTriggerManager.py
# Manages switch trigger rules persisted at: switch_settings/<hostname>/triggers.toml
# Python 3.11+ only (uses tomllib for robust reading)
from __future__ import annotations

# ---------- user-defined constants (top) ----------
TRIGGERS_BASE_DIR: str = r"switch_settings"
TRIGGERS_FILENAME: str = "triggers.toml"
TMP_SUFFIX: str = ".tmp"

# Preferred top-level sections (kept stable for readability)
SECTION_META: str = "Meta"
SECTION_BASIC: str = "Basic"     # { <rule_id>: { enabled, condition, action } }
SECTION_ADV: str = "Advanced"    # { <rule_id>: { enabled, script_json } }
SECTION_SCRIPTS: str = "Scripts" # { <script_name>: true/false }

# Default Meta fields we may extend
DEFAULT_META: dict = {
    "version": 1,
    "notes": "Switch trigger configuration. Edit carefully.",
}

# ---------- imports ----------
import os
import io
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib  # Python 3.11+
except Exception as e:  # pragma: no cover
    raise RuntimeError("Python 3.11+ required: tomllib is missing") from e

logger = logging.getLogger("saiSwitchTriggerManager")


class SwitchTriggerManager:
    """
    File layout:
      switch_settings/<hostname>/triggers.toml

    TOML schema (kept human-friendly):
      [Meta]
      version = 1
      notes = "Switch trigger configuration. Edit carefully."

      [Basic]
      # Each key is a rule_id; value is an inline table with 'enabled', 'condition', 'action'
      # Condition fields (single-condition rule):
      #   sensor_id, metric, op, threshold, hysteresis, min_interval_sec
      # Action fields:
      #   (preferred) switch_key="switch_id::Label", set=true|false
      #   (alt) switch_id="...", label="...", set=...
      #   (alt) hostname="...", label="...", set=...
      # Example:
      #   CoolWhenHot = { enabled=true,
      #     condition = { sensor_id="avpd-i2c-0-sensoria-hub-0", metric="Temperature_F", op=">", threshold=82.0, hysteresis=1.0, min_interval_sec=120 },
      #     action    = { switch_key="switch-dijn0w::Fan", set=true }
      #   }

      [Advanced]
      # Each key is a rule_id; value is an inline table with 'enabled', 'script_json' (stringified JSON)
      # The JSON can encode multi-condition logic, schedules, etc.
      #   NightLights = { enabled=false, script_json="{\"when\":\"22:00-06:00\",\"action\":{\"switch_key\":\"switch-xyz::Light\",\"set\":true}}" }

      [Scripts]
      # Optional global script toggles (coarse on/off flags the runtime can check)
      #   Cool_when_hot = true
      #   Lights_at_night = false
    """

    def __init__(self, base_dir: str = TRIGGERS_BASE_DIR) -> None:
        self.base_dir = Path(base_dir)

    # ---------- path helpers ----------
    def _dir_for_hostname(self, hostname: str) -> Path:
        return self.base_dir / hostname

    def _path_for_hostname(self, hostname: str) -> Path:
        parent = self._dir_for_hostname(hostname)
        parent.mkdir(parents=True, exist_ok=True)
        return parent / TRIGGERS_FILENAME

    # ---------- public API ----------
    def load(self, hostname: str) -> Dict[str, Any]:
        """
        Load triggers.toml into a dict with all expected sections present.
        Missing file returns defaults.
        """
        triggers_path = self._path_for_hostname(hostname)
        if not triggers_path.exists():
            logger.debug("[triggers] No file yet for %s; returning defaults", hostname)
            return {
                SECTION_META: dict(DEFAULT_META),
                SECTION_BASIC: {},
                SECTION_ADV: {},
                SECTION_SCRIPTS: {},
            }

        try:
            with triggers_path.open("rb") as f:
                data = tomllib.load(f) or {}
        except Exception as e:
            logger.warning("[triggers] Failed to read %s: %r; returning defaults", triggers_path, e)
            return {
                SECTION_META: dict(DEFAULT_META),
                SECTION_BASIC: {},
                SECTION_ADV: {},
                SECTION_SCRIPTS: {},
            }

        # Normalize missing sections
        data.setdefault(SECTION_META, dict(DEFAULT_META))
        data.setdefault(SECTION_BASIC, {})
        data.setdefault(SECTION_ADV, {})
        data.setdefault(SECTION_SCRIPTS, {})
        return data

    def save(self, hostname: str, data: Dict[str, Any]) -> None:
        """
        Atomically write out in a stable, human-readable TOML.
        We do not require a TOML writer; we emit carefully.
        """
        triggers_path = self._path_for_hostname(hostname)
        tmp_path = triggers_path.with_suffix(triggers_path.suffix + TMP_SUFFIX)

        # Normalize sections and sort keys for stable diffs
        meta = dict(DEFAULT_META)
        meta.update(data.get(SECTION_META, {}) or {})

        basic: Dict[str, Any] = data.get(SECTION_BASIC, {}) or {}
        adv: Dict[str, Any] = data.get(SECTION_ADV, {}) or {}
        scripts: Dict[str, Any] = data.get(SECTION_SCRIPTS, {}) or {}

        def _emit_meta(buf: io.StringIO) -> None:
            buf.write("[Meta]\n")
            # Keep order for readability
            buf.write(f"version = {int(meta.get('version', 1))}\n")
            notes = str(meta.get("notes", "Switch trigger configuration. Edit carefully."))
            buf.write(f"{_toml_key('notes')} = {_toml_string(notes)}\n\n")

        def _emit_basic(buf: io.StringIO) -> None:
            if not basic:
                return
            buf.write("[Basic]\n")
            for rule_id in sorted(basic.keys()):
                rule = basic.get(rule_id) or {}
                enabled = bool(rule.get("enabled", False))
                condition = rule.get("condition", {}) or {}
                action = rule.get("action", {}) or {}

                # Emit inline tables for compactness
                buf.write(f"{_toml_key(rule_id)} = {{ ")
                buf.write(f"enabled={_toml_bool(enabled)}, ")

                # condition =
                buf.write("condition = { ")
                buf.write(_emit_kv_inline({
                    "sensor_id": condition.get("sensor_id", ""),
                    "metric": condition.get("metric", ""),
                    "op": condition.get("op", ">"),
                    "threshold": condition.get("threshold", 0),
                    "hysteresis": condition.get("hysteresis", 0),
                    "min_interval_sec": condition.get("min_interval_sec", 0),
                }))
                buf.write(" }, ")

                # action =
                # Prefer switch_key if present; else write the provided fields
                act_map = {}
                if "switch_key" in action:
                    act_map["switch_key"] = action.get("switch_key", "")
                else:
                    # keep whatever the caller provided (switch_id/label or hostname/label)
                    if "switch_id" in action:
                        act_map["switch_id"] = action.get("switch_id", "")
                    if "hostname" in action:
                        act_map["hostname"] = action.get("hostname", "")
                    act_map["label"] = action.get("label", "")
                act_map["set"] = bool(action.get("set", False))

                buf.write("action = { ")
                buf.write(_emit_kv_inline(act_map))
                buf.write(" } ")

                buf.write("}\n")
            buf.write("\n")

        def _emit_advanced(buf: io.StringIO) -> None:
            if not adv:
                return
            buf.write("[Advanced]\n")
            for rule_id in sorted(adv.keys()):
                rule = adv.get(rule_id) or {}
                enabled = bool(rule.get("enabled", False))
                script_json = rule.get("script_json", "")
                # Ensure script_json is a single-line compact JSON string
                if isinstance(script_json, (dict, list)):
                    script_json = json.dumps(script_json, separators=(",", ":"), ensure_ascii=False)
                else:
                    # normalize whitespace if it is a string
                    try:
                        parsed = json.loads(str(script_json))
                        script_json = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
                    except Exception:
                        script_json = str(script_json)
                buf.write(
                    f"{_toml_key(rule_id)} = {{ enabled={_toml_bool(enabled)}, script_json={_toml_string(script_json)} }}\n"
                )
            buf.write("\n")

        def _emit_scripts(buf: io.StringIO) -> None:
            if not scripts:
                return
            buf.write("[Scripts]\n")
            for name in sorted(scripts.keys()):
                buf.write(f"{_toml_key(name)} = {_toml_bool(bool(scripts[name]))}\n")
            buf.write("\n")

        buf = io.StringIO()
        _emit_meta(buf)
        _emit_basic(buf)
        _emit_advanced(buf)
        _emit_scripts(buf)

        text = buf.getvalue()
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp_path, triggers_path)
            logger.debug("[triggers] Saved %s (%d bytes)", triggers_path, len(text))
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ---------- CRUD helpers ----------
    def upsert_basic_rule(
        self,
        hostname: str,
        rule_id: str,
        *,
        enabled: bool,
        condition: Dict[str, Any],
        action: Dict[str, Any],
    ) -> None:
        """
        Create or update a Basic rule (single-condition).
        Required fields inside 'condition': sensor_id, metric, op, threshold
        Optional: hysteresis, min_interval_sec
        Action prefers 'switch_key' (e.g., "switch-abc123::Fan") else accepts switch_id/label or hostname/label.
        """
        data = self.load(hostname)
        basic = data.get(SECTION_BASIC, {}) or {}
        basic[rule_id] = {
            "enabled": bool(enabled),
            "condition": {
                "sensor_id": str(condition.get("sensor_id", "")),
                "metric": str(condition.get("metric", "")),
                "op": str(condition.get("op", ">")),
                "threshold": condition.get("threshold", 0),
                "hysteresis": condition.get("hysteresis", 0),
                "min_interval_sec": condition.get("min_interval_sec", 0),
            },
            "action": self._normalize_action(action),
        }
        data[SECTION_BASIC] = basic
        self.save(hostname, data)

    def upsert_advanced_rule(
        self,
        hostname: str,
        rule_id: str,
        *,
        enabled: bool,
        script: str | dict | list,
    ) -> None:
        """
        Create or update an Advanced rule with a JSON script payload.
        'script' may be a dict/list (will be JSON-dumped) or a JSON string.
        """
        data = self.load(hostname)
        adv = data.get(SECTION_ADV, {}) or {}

        if isinstance(script, (dict, list)):
            script_json = json.dumps(script, separators=(",", ":"), ensure_ascii=False)
        else:
            # Validate or pass-through
            s = str(script)
            try:
                parsed = json.loads(s)
                script_json = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                script_json = s  # store verbatim; runtime should validate before executing

        adv[rule_id] = {"enabled": bool(enabled), "script_json": script_json}
        data[SECTION_ADV] = adv
        self.save(hostname, data)

    def delete_rule(self, hostname: str, section: str, rule_id: str) -> bool:
        """
        Delete a rule by id from 'Basic' or 'Advanced'.
        Returns True if removed.
        """
        section = section.strip().title()
        if section not in (SECTION_BASIC, SECTION_ADV):
            logger.debug("[triggers] delete_rule: invalid section %s", section)
            return False
        data = self.load(hostname)
        rules = data.get(section, {}) or {}
        if rule_id in rules:
            del rules[rule_id]
            data[section] = rules
            self.save(hostname, data)
            return True
        return False

    def set_rule_enabled(self, hostname: str, section: str, rule_id: str, enabled: bool) -> bool:
        """
        Enable/disable a specific rule under Basic or Advanced.
        """
        section = section.strip().title()
        if section not in (SECTION_BASIC, SECTION_ADV):
            return False
        data = self.load(hostname)
        rules = data.get(section, {}) or {}
        rule = rules.get(rule_id)
        if not rule:
            return False
        rule["enabled"] = bool(enabled)
        rules[rule_id] = rule
        data[section] = rules
        self.save(hostname, data)
        return True

    def set_script_enabled(self, hostname: str, script_name: str, enabled: bool) -> None:
        """
        Toggle a coarse global script flag under [Scripts].
        Useful for UI checkboxes that gate groups of rules.
        """
        data = self.load(hostname)
        scripts = data.get(SECTION_SCRIPTS, {}) or {}
        scripts[script_name] = bool(enabled)
        data[SECTION_SCRIPTS] = scripts
        self.save(hostname, data)

    # ---------- utility ----------
    @staticmethod
    def _normalize_action(action: Dict[str, Any]) -> Dict[str, Any]:
        """Accepts switch_key or switch_id/label or hostname/label; ensures 'set' is present."""
        out: Dict[str, Any] = {"set": bool(action.get("set", False))}
        if "switch_key" in action and action.get("switch_key"):
            out["switch_key"] = str(action["switch_key"])
            return out
        # else copy provided fields (label is required in these modes)
        if "switch_id" in action:
            out["switch_id"] = str(action["switch_id"])
        if "hostname" in action:
            out["hostname"] = str(action["hostname"])
        out["label"] = str(action.get("label", ""))
        return out


# ---------- tiny TOML emit helpers (no third-party writer) ----------
def _toml_key(key: str) -> str:
    """
    Return a safe key (quote if necessary per TOML rules).
    We keep it simple: quote anything with non-alnum/underscore/dash.
    """
    if key and key.replace("_", "").replace("-", "").isalnum():
        return key
    return _toml_string(key)

def _toml_string(val: str) -> str:
    s = str(val)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return f"\"{s}\""

def _toml_bool(v: bool) -> str:
    return "true" if v else "false"

def _emit_kv_inline(d: Dict[str, Any]) -> str:
    """
    Emit k=v pairs for an inline table, in a consistent k-sort.
    Numbers/bools go raw; strings go quoted.
    """
    parts = []
    for k in sorted(d.keys()):
        v = d[k]
        if isinstance(v, bool):
            parts.append(f"{_toml_key(k)}={_toml_bool(v)}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(f"{_toml_key(k)}={v}")
        else:
            parts.append(f"{_toml_key(k)}={_toml_string(str(v))}")
    return ", ".join(parts)

# saiSwitchTriggerManager.py (add to bottom)

def enable_trigger(manager, switch_id: str, section: str, key: str, enable: bool = True) -> bool:
    """
    Convenience: toggle 'enabled' flag on a trigger in triggers.toml and persist.
    Returns True if updated, False if no matching trigger found.
    """
    triggers = load_triggers(manager, switch_id)
    section_dict = triggers.get(section, {})
    if key not in section_dict:
        return False

    rule = section_dict[key]
    if isinstance(rule, dict):
        rule["enabled"] = bool(enable)
    else:
        # For advanced scripts we can wrap as dict {"script": ..., "enabled": ...}
        section_dict[key] = {"script": rule, "enabled": bool(enable)}

    save_triggers(manager, switch_id, triggers)
    return True


def remove_trigger(manager, switch_id: str, section: str, key: str) -> bool:
    """
    Convenience: remove a trigger rule from triggers.toml and persist.
    Returns True if removed, False if not found.
    """
    triggers = load_triggers(manager, switch_id)
    section_dict = triggers.get(section, {})
    if key not in section_dict:
        return False

    del section_dict[key]
    save_triggers(manager, switch_id, triggers)
    return True
