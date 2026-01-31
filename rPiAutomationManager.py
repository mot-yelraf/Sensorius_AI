"""Switch automation rule manager and TOML schema helper.

Loads, validates, and persists switch automation rules stored per host at
`switch_settings/<hostname>/automations.toml`. Supports Basic rules (single
condition + action), Advanced rules (JSON scripts), and global Script toggles.

This module provides a small API for reading/writing rules, normalizing schema,
and querying rule state by switch key for runtime use in the automation engine.
"""
from __future__ import annotations

# ---------- user-defined constants (top) ----------
TRIGGERS_BASE_DIR: str = r"switch_settings"
TRIGGERS_FILENAME: str = "automations.toml"
TMP_SUFFIX: str = ".tmp"

# Preferred top-level sections (kept stable for readability)
SECTION_META: str = "Meta"
SECTION_BASIC: str = "Basic"     # { <rule_id>: { enabled, condition, action } }
SECTION_ADV: str = "Advanced"    # { <rule_id>: { enabled, script_json } }
SECTION_SCRIPTS: str = "Scripts" # { <script_name>: true/false }

# Default Meta fields we may extend
DEFAULT_META: dict = {
    "version": 1,
    "notes": "Switch automation configuration. Edit carefully.",
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

from rPiUtils import debug_enabled, printDM

MODULE = "rPiAutomationManager"
DEBUG = debug_enabled(MODULE)

class AutomationManager:
    """
    File layout:
      switch_settings/<hostname>/automations.toml

    TOML schema (kept human-friendly):
      [Meta]
      version = 1
      notes = "Switch automations configuration. Edit carefully."

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
    def get_advanced_rule_for_switch_key(self, hostname: str, switch_key: str) -> dict:
        """
        Look up the Advanced rule whose script_json.actions[*].switch_key == switch_key.

        'switch_key' is the canonical DB identity for a channel:
        "<switch_id>::<channel_id>" (e.g. "sensoria-hub-0::S1-saihub0").

        Returns { 'found': bool, 'enabled': bool, 'rule_id': str|None }.
        """
        key = (switch_key or "").strip()
        if not key:
            return {"found": False, "enabled": False, "rule_id": None}

        try:
            data = self.load(hostname) or {}
            adv = (data.get(SECTION_ADV) or {})
            import json as _json

            for rule_id, rule in adv.items():
                if not isinstance(rule, dict):
                    continue

                enabled = bool(rule.get("enabled", False))
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
                    if sk == key:
                        return {
                            "found": True,
                            "enabled": enabled,
                            "rule_id": rule_id,
                        }

            return {"found": False, "enabled": False, "rule_id": None}
        except Exception:
            return {"found": False, "enabled": False, "rule_id": None}

    def get_advanced_enabled_for_switch_key(self, hostname: str, switch_key: str) -> bool:
        """
        Convenience: return the 'enabled' flag for the Advanced rule associated with switch_key.
        """
        info = self.get_advanced_rule_for_switch_key(hostname, switch_key)
        return bool(info.get("enabled", False)) if info and info.get("found") else False

    def set_advanced_enabled_for_switch_key(self, hostname: str, switch_key: str, enabled: bool) -> bool:
        """
        Toggle Advanced rule 'enabled' given a canonical switch_key '<switch_id>::<channel_id>'.
        Also syncs the inner JSON script's 'enabled' field if present.
        Returns True if any rule was updated.
        """
        key = (switch_key or "").strip()
        if not key:
            return False

        data = self.load(hostname)
        adv = data.get(SECTION_ADV, {}) or {}
        import json as _json

        changed = False

        for rule_id, rule in adv.items():
            if not isinstance(rule, dict):
                continue

            script_json = rule.get("script_json", "")
            try:
                script = _json.loads(str(script_json))
            except Exception:
                script = None

            actions = (script or {}).get("actions") or []
            found_here = False
            for act in actions:
                try:
                    sk = (act.get("switch_key") or "").strip()
                except AttributeError:
                    continue
                if sk == key:
                    found_here = True
                    break

            if not found_here:
                continue

            # Outer enabled flag
            rule["enabled"] = bool(enabled)

            # Inner script.enabled for consistency
            if isinstance(script, dict):
                script["enabled"] = bool(enabled)
                rule["script_json"] = _json.dumps(
                    script,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )

            adv[rule_id] = rule
            changed = True

        if changed:
            data[SECTION_ADV] = adv
            self.save(hostname, data)

        return changed
    
    def load(self, hostname: str) -> Dict[str, Any]:
        """
        Load automations.toml into a dict with all expected sections present.
        Missing file returns defaults.
        """
        triggers_path = self._path_for_hostname(hostname)
        if not triggers_path.exists():
            if DEBUG:
                printDM(f"[No file yet for {hostname}; returning defaults", location=f"{MODULE}.load")
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
            if DEBUG:
                printDM(f"[Failed to read {triggers_path}: {e}", location=f"{MODULE}.load")
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

        adv: Dict[str, Any] = data.get(SECTION_ADV, {}) or {}
        scripts: Dict[str, Any] = data.get(SECTION_SCRIPTS, {}) or {}

        def _emit_meta(buf: io.StringIO) -> None:
            buf.write("[Meta]\n")
            # Keep order for readability
            buf.write(f"version = {int(meta.get('version', 1))}\n")
            notes = str(meta.get("notes", "Switch trigger configuration. Edit carefully."))
            buf.write(f"{_toml_key('notes')} = {_toml_string(notes)}\n\n")

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
        _emit_advanced(buf)
        _emit_scripts(buf)

        text = buf.getvalue()
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp_path, triggers_path)
            if DEBUG:
                printDM(f"[Saved {triggers_path}", location=f"{MODULE}.save")

        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ---------- CRUD helpers ----------
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
            if DEBUG:
                printDM(f"[delete_rule: invalid section  {section}", location=f"{MODULE}.save")
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

# ---------- tiny TOML emit helper  ----------
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

def enable_trigger(manager, switch_id: str, section: str, key: str, enable: bool = True) -> bool:
    """
    Toggle 'enabled' on a trigger.
    'section' should be 'Advanced' or 'Scripts'; 'Basic' is no longer supported.
    """
    section = section.strip().title()
    if section not in (SECTION_ADV, SECTION_SCRIPTS):
        return False

    triggers = load_automations(manager, switch_id)
    section_dict = triggers.get(section, {}) or {}
    if key not in section_dict:
        return False

    rule = section_dict[key]
    if isinstance(rule, dict):
        rule["enabled"] = bool(enable)
    else:
        # For legacy non-dict entries (unlikely for Advanced, but safe)
        section_dict[key] = {"script": rule, "enabled": bool(enable)}

    triggers[section] = section_dict
    save_automations(manager, switch_id, triggers)
    return True

def remove_trigger(manager, switch_id: str, section: str, key: str) -> bool:
    """
    Convenience: remove a trigger rule from automations.toml and persist.
    Returns True if removed, False if not found.
    """
    section = section.strip().title()
    if section not in (SECTION_ADV, SECTION_SCRIPTS):
        return False

    triggers = load_automations(manager, switch_id)
    section_dict = triggers.get(section, {}) or {}
    if key not in section_dict:
        return False

    del section_dict[key]
    triggers[section] = section_dict
    save_automations(manager, switch_id, triggers)
    return True
    
# ---------- public convenience (module-level) ----------
def load_automations(manager: AutomationManager, switch_id: str) -> dict:
    """
    Public helper: return the full automations dict for a given switch_id/hostname.
    Mirrors AutomationManager.load().
    """
    if not isinstance(manager, AutomationManager):
        raise TypeError("load_automations expects an AutomationManager instance as first arg")
    return manager.load(switch_id)


def save_automations(manager: AutomationManager, switch_id: str, data: dict) -> None:
    """
    Public helper: persist the full automations dict for a given switch_id/hostname.
    Mirrors AutomationManager.save().
    """
    if not isinstance(manager, AutomationManager):
        raise TypeError("save_automations expects an AutomationManager instance as first arg")
    if not isinstance(data, dict):
        raise TypeError("save_automations expects 'data' to be a dict")
    manager.save(switch_id, data)


# --- Back-compat aliases used by rPiWebRoutes.submit_advanced_trigger ---
def load_triggers(manager: AutomationManager, switch_id: str) -> dict:
    """Deprecated alias → load_automations."""
    return load_automations(manager, switch_id)


def save_triggers(manager: AutomationManager, switch_id: str, data: dict) -> None:
    """Deprecated alias → save_automations."""
    save_automations(manager, switch_id, data)


# Optional: help static analyzers/explicit imports
__all__ = [
    "AutomationManager",
    "load_automations", "save_automations",
    "load_triggers", "save_triggers",
    "enable_trigger", "remove_trigger",
]
