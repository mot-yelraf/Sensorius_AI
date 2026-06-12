"""Switch automation rule manager and TOML schema helper.

Loads, validates, and persists switch automation rules in a shared file at
`switch_settings/automations/automations.toml`.

Current runtime contract is Advanced-only automation rules plus optional global
Script toggles:
- ``[Advanced]``: named rules containing ``enabled`` + ``script_json``
- ``[Scripts]``: coarse global boolean flags

This module provides a small API for reading/writing Advanced rules,
normalizing schema, and querying rule state by switch key for runtime use in
the automation engine.
"""
from __future__ import annotations

# ---------- user-defined constants (top) ----------
TRIGGERS_BASE_DIR: str = r"switch_settings"
TRIGGERS_SUBDIR: str = "automations"
TRIGGERS_FILENAME: str = "automations.toml"
TMP_SUFFIX: str = ".tmp"

# Preferred top-level sections (kept stable for readability)
SECTION_META: str = "Meta"
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
import re
import threading
from pathlib import Path
from typing import Any, Dict, Callable, TypeVar
try:
    import tomllib  # Python 3.11+
except Exception as e:  # pragma: no cover
    raise RuntimeError("Python 3.11+ required: tomllib is missing") from e

from saiRuntimePaths import resolve_runtime_base_dir
from saiUtils import debug_enabled, printDM

MODULE = "saiAutomationManager"
DEBUG = debug_enabled(MODULE)
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
T = TypeVar("T")


def _as_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no", ""}
    return bool(value)

class AutomationManager:
    """
    File layout:
      switch_settings/automations/automations.toml

    TOML schema (kept human-friendly):
      [Meta]
      version = 1
      notes = "Switch automations configuration. Edit carefully."

      [Advanced]
      # Each key is a rule_id; value is an inline table with 'enabled', 'script_json' (stringified JSON)
      # The JSON can encode multi-condition logic, schedules, etc.
      #   NightLights = { enabled=false, script_json="{\"when\":\"22:00-06:00\",\"action\":{\"switch_key\":\"switch-xyz::Light\",\"set\":true}}" }

      [Scripts]
      # Optional global script toggles (coarse on/off flags the runtime can check)
      #   Cool_when_hot = true
      #   Lights_at_night = false
    """

    _shared_lock = threading.RLock()
    _shared_cache: dict[str, dict[str, Any]] = {}

    def __init__(self, base_dir: str = TRIGGERS_BASE_DIR) -> None:
        self.base_dir = resolve_runtime_base_dir(base_dir)
        self._lock = threading.RLock()

    # ---------- path helpers ----------
    def _validate_hostname(self, hostname: str) -> str:
        """Allow only safe switch-id/hostname path segments."""
        host = str(hostname or "").strip()
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"Invalid hostname/switch_id path segment: {hostname!r}")
        return host

    def _storage_dir(self) -> Path:
        parent = self.base_dir / TRIGGERS_SUBDIR
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    def _storage_path(self) -> Path:
        return self._storage_dir() / TRIGGERS_FILENAME

    def _path_for_hostname(self, hostname: str) -> Path:
        # Storage is now shared globally; keep hostname parameter for API compatibility.
        _ = hostname
        return self._storage_path()

    def get_storage_path(self) -> Path:
        """Public helper for shared automation file path."""
        return self._storage_path()

    def _normalize_loaded_data(self, data: Dict[str, Any] | None) -> Dict[str, Any]:
        normalized = dict(data or {})
        normalized.setdefault(SECTION_META, dict(DEFAULT_META))
        normalized.setdefault(SECTION_ADV, {})
        normalized.setdefault(SECTION_SCRIPTS, {})
        return normalized

    def _build_runtime_cache(self, data: Dict[str, Any]) -> dict[str, Any]:
        adv = (data.get(SECTION_ADV) or {})
        runtime_adv: dict[str, Any] = {}
        switch_key_index: dict[str, list[dict[str, Any]]] = {}

        for rule_id, rule in adv.items():
            if not isinstance(rule, dict):
                continue

            enabled_outer = _as_enabled(rule.get("enabled", False))
            script_json = rule.get("script_json", "")
            try:
                script = json.loads(str(script_json))
            except Exception:
                script = None

            runtime_rule = dict(rule)
            if isinstance(script, (dict, list)):
                runtime_rule["script_json"] = script
            runtime_adv[str(rule_id)] = runtime_rule

            if not isinstance(script, dict):
                continue

            actions = script.get("actions") or []
            switch_keys: set[str] = set()
            for act in actions:
                if not isinstance(act, dict):
                    continue
                switch_key = str(act.get("switch_key", "") or "").strip()
                if switch_key:
                    switch_keys.add(switch_key)

            if not switch_keys:
                continue

            rule_info = {
                "rule_id": str(rule_id),
                "enabled_outer": enabled_outer,
                "enabled_inner": bool(script.get("enabled", True)),
                "switch_keys": tuple(sorted(switch_keys)),
            }
            for switch_key in switch_keys:
                switch_key_index.setdefault(switch_key, []).append(rule_info)

        return {
            "data": data,
            "runtime_advanced": runtime_adv,
            "switch_key_index": switch_key_index,
        }

    def _read_storage_file(self, triggers_path: Path, hostname: str) -> Dict[str, Any]:
        if not triggers_path.exists():
            if DEBUG:
                printDM(f"[No file yet for {hostname}; returning defaults", location=f"{MODULE}.load")
            return self._normalize_loaded_data(None)

        try:
            with triggers_path.open("rb") as f:
                data = tomllib.load(f) or {}
        except Exception as e:
            if DEBUG:
                printDM(f"[Failed to read {triggers_path}: {e}", location=f"{MODULE}.load")
            return self._normalize_loaded_data(None)

        return self._normalize_loaded_data(data)

    def _cache_key(self, hostname: str) -> tuple[str, Path]:
        path = self._path_for_hostname(hostname)
        return (str(path.resolve()), path)

    def _cached_payload(self, hostname: str) -> dict[str, Any]:
        cache_key, triggers_path = self._cache_key(hostname)
        try:
            stat = triggers_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stat = None
            stamp = None

        with self._shared_lock:
            cached = self._shared_cache.get(cache_key)
            if cached and cached.get("stamp") == stamp:
                return cached

            data = self._read_storage_file(triggers_path, hostname)
            runtime = self._build_runtime_cache(data)
            payload = {
                "stamp": stamp,
                "path": triggers_path,
                **runtime,
            }
            self._shared_cache[cache_key] = payload
            return payload

    def _replace_cached_payload(self, hostname: str, data: Dict[str, Any]) -> dict[str, Any]:
        cache_key, triggers_path = self._cache_key(hostname)
        try:
            stat = triggers_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None

        payload = {
            "stamp": stamp,
            "path": triggers_path,
            **self._build_runtime_cache(self._normalize_loaded_data(data)),
        }
        with self._shared_lock:
            self._shared_cache[cache_key] = payload
        return payload

    def _atomic_update(self, hostname: str, mutator: Callable[[Dict[str, Any]], T]) -> T:
        """Serialize load->mutate->save for one manager instance."""
        with self._shared_lock:
            data = self.load(hostname)
            result = mutator(data)
            self.save(hostname, data)
            return result

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
            index = self._cached_payload(hostname).get("switch_key_index") or {}
            key_aliases = self._expand_switch_key_aliases(hostname, key)
            for alias in key_aliases:
                for rule_info in index.get(alias, []):
                    return {
                        "found": True,
                        "enabled": bool(rule_info.get("enabled_outer", False)),
                        "rule_id": rule_info.get("rule_id"),
                    }

            return {"found": False, "enabled": False, "rule_id": None}
        except Exception:
            if DEBUG:
                printDM(
                    f"[get_advanced_rule_for_switch_key] unexpected error for host={hostname}",
                    location=f"{MODULE}.get_advanced_rule_for_switch_key",
                )
            return {"found": False, "enabled": False, "rule_id": None}

    def get_advanced_state_for_switch_key(self, hostname: str, switch_key: str) -> dict:
        """
        Aggregate Advanced rule state for a switch_key across *all* matching rules.
        """
        def _as_enabled(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "off", "no", ""}
            return bool(value)

        key = (switch_key or "").strip()
        if not key:
            return {
                "found": False,
                "rule_count": 0,
                "enabled_count": 0,
                "enabled_any": False,
                "enabled_all": False,
                "rule_ids": [],
            }

        try:
            key_aliases = self._expand_switch_key_aliases(hostname, key)
            index = self._cached_payload(hostname).get("switch_key_index") or {}

            matched_rule_ids: set[str] = set()
            enabled_count = 0
            for alias in key_aliases:
                for rule_info in index.get(alias, []):
                    rule_id = str(rule_info.get("rule_id"))
                    if rule_id in matched_rule_ids:
                        continue
                    matched_rule_ids.add(rule_id)
                    if bool(rule_info.get("enabled_outer", False)):
                        enabled_count += 1

            rule_ids = sorted(matched_rule_ids)
            rule_count = len(rule_ids)
            found = rule_count > 0
            return {
                "found": found,
                "rule_count": rule_count,
                "enabled_count": enabled_count,
                "enabled_any": enabled_count > 0,
                "enabled_all": found and enabled_count == rule_count,
                "rule_ids": rule_ids,
            }
        except Exception:
            if DEBUG:
                printDM(
                    f"[get_advanced_state_for_switch_key] unexpected error for host={hostname}",
                    location=f"{MODULE}.get_advanced_state_for_switch_key",
                )
            return {
                "found": False,
                "rule_count": 0,
                "enabled_count": 0,
                "enabled_any": False,
                "enabled_all": False,
                "rule_ids": [],
            }

    def get_advanced_enabled_for_switch_key(self, hostname: str, switch_key: str) -> bool:
        """
        Convenience: aggregate enabled state for switch_key.
        True when any matching Advanced rule is enabled.
        """
        state = self.get_advanced_state_for_switch_key(hostname, switch_key)
        return bool(state.get("enabled_any", False))

    def set_advanced_enabled_for_switch_key(self, hostname: str, switch_key: str, enabled: bool) -> bool:
        """
        Toggle Advanced rule 'enabled' given a canonical switch_key '<switch_id>::<channel_id>'.
        Also syncs the inner JSON script's 'enabled' field if present.
        Returns True if any rule was updated.
        """
        key = (switch_key or "").strip()
        if not key:
            return False
        key_aliases = self._expand_switch_key_aliases(hostname, key)

        def _mutate(data: Dict[str, Any]) -> bool:
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
                    if sk in key_aliases:
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

            data[SECTION_ADV] = adv
            return changed

        return self._atomic_update(hostname, _mutate)

    def _expand_switch_key_aliases(self, hostname: str, switch_key: str) -> set[str]:
        aliases = {str(switch_key or "").strip()}
        key = str(switch_key or "").strip()
        if "::" not in key:
            return aliases

        sid, suffix = key.split("::", 1)
        sid = sid.strip()
        suffix = suffix.strip()
        if not sid or not suffix:
            return aliases

        # Keep lookups tolerant to case drift between persisted settings,
        # runtime controller IDs, and user-facing headers.
        sid_variants = {sid, sid.lower(), sid.upper()}
        aliases.update(f"{sid_variant}::{suffix}" for sid_variant in sid_variants if sid_variant)

        try:
            from saiSwitchSettingsManager import SwitchSettingsManager

            doc = SwitchSettingsManager("switch_settings").load(hostname) or {}
            sw = (doc.get("Switch") or {})
            if not isinstance(sw, dict):
                return aliases

            for n in range(1, 33):
                label = str(sw.get(f"SWITCH_{n}_LABEL", "") or "").strip()
                channel_id = str(sw.get(f"SWITCH_{n}_CHANNEL_ID", "") or "").strip()
                if not label:
                    continue
                if suffix.lower() == label.lower() or (channel_id and suffix.lower() == channel_id.lower()):
                    for sid_variant in sid_variants:
                        if not sid_variant:
                            continue
                        aliases.add(f"{sid_variant}::{label}")
                        if channel_id:
                            aliases.add(f"{sid_variant}::{channel_id}")
                    break
        except Exception:
            pass

        return aliases
    
    def load(self, hostname: str) -> Dict[str, Any]:
        """
        Load automations.toml into a dict with all expected sections present.
        Missing file returns defaults.
        """
        return self._cached_payload(hostname).get("data") or self._normalize_loaded_data(None)

    def load_runtime_advanced(self, hostname: str) -> Dict[str, Any]:
        """
        Return Advanced rules with parsed script_json where possible.
        This is intended for runtime evaluation paths.
        """
        payload = self._cached_payload(hostname)
        runtime_adv = payload.get("runtime_advanced") or {}
        return dict(runtime_adv)

    def save(self, hostname: str, data: Dict[str, Any]) -> None:
        """
        Atomically write out in a stable, human-readable TOML.
        We do not require a TOML writer; we emit carefully.
        """
        with self._shared_lock:
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
                    enabled = _as_enabled(rule.get("enabled", False))
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
                self._replace_cached_payload(hostname, {
                    SECTION_META: meta,
                    SECTION_ADV: adv,
                    SECTION_SCRIPTS: scripts,
                })
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
        def _mutate(data: Dict[str, Any]) -> None:
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

        self._atomic_update(hostname, _mutate)

    def delete_rule(self, hostname: str, section: str, rule_id: str) -> bool:
        """
        Delete a rule by id from 'Advanced'.
        Returns True if removed.
        """
        section = section.strip().title()
        if section not in (SECTION_ADV,):
            if DEBUG:
                printDM(f"[delete_rule: invalid section  {section}", location=f"{MODULE}.save")
            return False
        def _mutate(data: Dict[str, Any]) -> bool:
            rules = data.get(section, {}) or {}
            if rule_id in rules:
                del rules[rule_id]
                data[section] = rules
                return True
            return False

        return self._atomic_update(hostname, _mutate)

    def set_rule_enabled(self, hostname: str, section: str, rule_id: str, enabled: bool) -> bool:
        """
        Enable/disable a specific rule under Advanced.
        """
        section = section.strip().title()
        if section not in (SECTION_ADV,):
            return False
        def _mutate(data: Dict[str, Any]) -> bool:
            rules = data.get(section, {}) or {}
            rule = rules.get(rule_id)
            if not rule:
                return False
            rule["enabled"] = bool(enabled)
            # Keep inner payload in sync when possible.
            try:
                script = json.loads(str(rule.get("script_json", "")))
                if isinstance(script, dict):
                    script["enabled"] = bool(enabled)
                    rule["script_json"] = json.dumps(script, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                pass
            rules[rule_id] = rule
            data[section] = rules
            return True

        return self._atomic_update(hostname, _mutate)

    def set_script_enabled(self, hostname: str, script_name: str, enabled: bool) -> None:
        """
        Toggle a coarse global script flag under [Scripts].
        Useful for UI checkboxes that gate groups of rules.
        """
        def _mutate(data: Dict[str, Any]) -> None:
            scripts = data.get(SECTION_SCRIPTS, {}) or {}
            scripts[script_name] = bool(enabled)
            data[SECTION_SCRIPTS] = scripts

        self._atomic_update(hostname, _mutate)

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
    if section == SECTION_SCRIPTS:
        section_dict[key] = bool(enable)
    elif isinstance(rule, dict):
        rule["enabled"] = bool(enable)
    else:
        # For legacy non-dict Advanced entries (safe fallback)
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


# --- Back-compat aliases used by saiWebRoutes.submit_advanced_trigger ---
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
