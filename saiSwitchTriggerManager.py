"""Backward-compatible switch trigger manager.

This module now mirrors the active automation storage/runtime contract used by
`saiAutomationManager` while preserving the historical class/function names.

Storage location:
  switch_settings/automations/automations.toml
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
import io
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, TypeVar

try:
    import tomllib  # Python 3.11+
except Exception as e:  # pragma: no cover
    raise RuntimeError("Python 3.11+ required: tomllib is missing") from e

import logging

logger = logging.getLogger("saiSwitchTriggerManager")

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
T = TypeVar("T")


class SwitchTriggerManager:
    """Compatibility manager for Advanced automation rules."""

    def __init__(self, base_dir: str = TRIGGERS_BASE_DIR) -> None:
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    # ---------- path helpers ----------
    def _validate_hostname(self, hostname: str) -> str:
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
        _ = hostname
        return self._storage_path()

    def get_storage_path(self) -> Path:
        return self._storage_path()

    def _dir_for_hostname(self, hostname: str) -> Path:
        # Kept for compatibility; storage is shared globally.
        _ = hostname
        return self._storage_dir()

    def _atomic_update(self, hostname: str, mutator: Callable[[Dict[str, Any]], T]) -> T:
        with self._lock:
            data = self.load(hostname)
            result = mutator(data)
            self.save(hostname, data)
            return result

    # ---------- public API ----------
    def load(self, hostname: str) -> Dict[str, Any]:
        """
        Load automations.toml into a dict with all expected sections present.
        Missing file returns defaults.
        """
        triggers_path = self._path_for_hostname(hostname)
        if not triggers_path.exists():
            logger.debug("[triggers] No file yet for %s; returning defaults", hostname)
            return {
                SECTION_META: dict(DEFAULT_META),
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
                SECTION_ADV: {},
                SECTION_SCRIPTS: {},
            }

        data.setdefault(SECTION_META, dict(DEFAULT_META))
        data.setdefault(SECTION_ADV, {})
        data.setdefault(SECTION_SCRIPTS, {})
        return data

    def save(self, hostname: str, data: Dict[str, Any]) -> None:
        """Atomically write a stable, human-readable TOML payload."""
        with self._lock:
            triggers_path = self._path_for_hostname(hostname)
            tmp_path = triggers_path.with_suffix(triggers_path.suffix + TMP_SUFFIX)

            meta = dict(DEFAULT_META)
            meta.update(data.get(SECTION_META, {}) or {})

            adv: Dict[str, Any] = data.get(SECTION_ADV, {}) or {}
            scripts: Dict[str, Any] = data.get(SECTION_SCRIPTS, {}) or {}

            def _emit_meta(buf: io.StringIO) -> None:
                buf.write("[Meta]\n")
                buf.write(f"version = {int(meta.get('version', 1))}\n")
                notes = str(meta.get("notes", DEFAULT_META["notes"]))
                buf.write(f"{_toml_key('notes')} = {_toml_string(notes)}\n\n")

            def _emit_advanced(buf: io.StringIO) -> None:
                if not adv:
                    return
                buf.write("[Advanced]\n")
                for rule_id in sorted(adv.keys()):
                    rule = adv.get(rule_id) or {}
                    enabled = bool(rule.get("enabled", False))
                    script_json = rule.get("script_json", "")

                    if isinstance(script_json, (dict, list)):
                        script_json = json.dumps(script_json, separators=(",", ":"), ensure_ascii=False)
                    else:
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
                logger.debug("[triggers] Saved %s (%d bytes)", triggers_path, len(text))
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # ---------- CRUD helpers ----------
    def upsert_basic_rule(self, *args, **kwargs) -> None:
        """Basic rules are no longer supported."""
        raise NotImplementedError("Basic automations are no longer supported; use Advanced rules.")

    def upsert_advanced_rule(
        self,
        hostname: str,
        rule_id: str,
        *,
        enabled: bool,
        script: str | dict | list,
    ) -> None:
        """Create or update an Advanced rule with a JSON script payload."""

        def _mutate(data: Dict[str, Any]) -> None:
            adv = data.get(SECTION_ADV, {}) or {}

            if isinstance(script, (dict, list)):
                script_json = json.dumps(script, separators=(",", ":"), ensure_ascii=False)
            else:
                s = str(script)
                try:
                    parsed = json.loads(s)
                    script_json = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
                except Exception:
                    script_json = s

            adv[rule_id] = {"enabled": bool(enabled), "script_json": script_json}
            data[SECTION_ADV] = adv

        self._atomic_update(hostname, _mutate)

    def delete_rule(self, hostname: str, section: str, rule_id: str) -> bool:
        """Delete a rule by id from 'Advanced'. Returns True if removed."""
        section = section.strip().title()
        if section not in (SECTION_ADV,):
            logger.debug("[triggers] delete_rule: invalid section %s", section)
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
        """Enable/disable a specific rule under Advanced."""
        section = section.strip().title()
        if section not in (SECTION_ADV,):
            return False

        def _mutate(data: Dict[str, Any]) -> bool:
            rules = data.get(section, {}) or {}
            rule = rules.get(rule_id)
            if not rule:
                return False
            rule["enabled"] = bool(enabled)

            # Keep inner payload's enabled value in sync when possible.
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
        """Toggle a coarse global script flag under [Scripts]."""

        def _mutate(data: Dict[str, Any]) -> None:
            scripts = data.get(SECTION_SCRIPTS, {}) or {}
            scripts[script_name] = bool(enabled)
            data[SECTION_SCRIPTS] = scripts

        self._atomic_update(hostname, _mutate)

    @staticmethod
    def _normalize_action(action: Dict[str, Any]) -> Dict[str, Any]:
        """Back-compat helper: normalize action identifiers and state target."""
        out: Dict[str, Any] = {"set": bool(action.get("set", False))}
        if "switch_key" in action and action.get("switch_key"):
            out["switch_key"] = str(action["switch_key"])
            return out
        if "switch_id" in action:
            out["switch_id"] = str(action["switch_id"])
        if "hostname" in action:
            out["hostname"] = str(action["hostname"])
        out["label"] = str(action.get("label", ""))
        return out


# ---------- tiny TOML emit helpers (no third-party writer) ----------
def _toml_key(key: str) -> str:
    """Return a safe key (quote if necessary per TOML rules)."""
    if key and key.replace("_", "").replace("-", "").isalnum():
        return key
    return _toml_string(key)


def _toml_string(val: str) -> str:
    s = str(val)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return f'"{s}"'


def _toml_bool(v: bool) -> str:
    return "true" if v else "false"


def _emit_kv_inline(d: Dict[str, Any]) -> str:
    """Back-compat helper for inline table emission."""
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


# ---------- module-level compatibility helpers ----------
def load_triggers(manager: SwitchTriggerManager, switch_id: str) -> dict:
    """Public helper mirroring manager.load()."""
    if not isinstance(manager, SwitchTriggerManager):
        raise TypeError("load_triggers expects a SwitchTriggerManager instance as first arg")
    return manager.load(switch_id)


def save_triggers(manager: SwitchTriggerManager, switch_id: str, data: dict) -> None:
    """Public helper mirroring manager.save()."""
    if not isinstance(manager, SwitchTriggerManager):
        raise TypeError("save_triggers expects a SwitchTriggerManager instance as first arg")
    if not isinstance(data, dict):
        raise TypeError("save_triggers expects 'data' to be a dict")
    manager.save(switch_id, data)


def enable_trigger(manager: SwitchTriggerManager, switch_id: str, section: str, key: str, enable: bool = True) -> bool:
    """Toggle enabled on a trigger entry under Advanced or Scripts."""
    section = section.strip().title()
    if section not in (SECTION_ADV, SECTION_SCRIPTS):
        return False

    triggers = load_triggers(manager, switch_id)
    section_dict = triggers.get(section, {}) or {}
    if key not in section_dict:
        return False

    rule = section_dict[key]
    if section == SECTION_SCRIPTS:
        section_dict[key] = bool(enable)
    elif isinstance(rule, dict):
        rule["enabled"] = bool(enable)
    else:
        section_dict[key] = {"script": rule, "enabled": bool(enable)}

    triggers[section] = section_dict
    save_triggers(manager, switch_id, triggers)
    return True


def remove_trigger(manager: SwitchTriggerManager, switch_id: str, section: str, key: str) -> bool:
    """Remove a trigger entry under Advanced or Scripts."""
    section = section.strip().title()
    if section not in (SECTION_ADV, SECTION_SCRIPTS):
        return False

    triggers = load_triggers(manager, switch_id)
    section_dict = triggers.get(section, {}) or {}
    if key not in section_dict:
        return False

    del section_dict[key]
    triggers[section] = section_dict
    save_triggers(manager, switch_id, triggers)
    return True


__all__ = [
    "SwitchTriggerManager",
    "load_triggers",
    "save_triggers",
    "enable_trigger",
    "remove_trigger",
]
