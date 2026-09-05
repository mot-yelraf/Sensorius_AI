"""Cached TOML manager for switch settings and metadata.

Provides fast load/save/update of switch_settings/<switch_id>/switch.toml and
is the source of truth for the switch factory + controller pipeline:
settings -> saiSwitchFactory -> saiSwitch.
"""

from __future__ import annotations

import os
import re
import threading
import secrets
import copy
import json
from pathlib import Path
from collections import OrderedDict
from .saiUtils import printDM, debug_enabled
from .saiRuntimePaths import resolve_runtime_base_dir
from .saiLocalIdentity import (
    is_placeholder_channel_id,
    make_channel_id,
    resolve_persisted_host_serial,
)

MODULE = "saiSwitchSettingsManager"
DEBUG = debug_enabled(MODULE)

class SwitchSettingsManager:
    """
    Manages per-switch TOML files under a base directory (e.g., 'switch_settings').

    Public API (unchanged):
      - list_switches() -> list[str]
      - get_path(switch_id) -> Path
      - load(switch_id) -> OrderedDict | None
      - save(switch_id, settings: dict|OrderedDict) -> None
      - update_setting(switch_id, key, value) -> None
      - delete_switch(switch_id) -> bool

    optional:
      - invalidate_cache(switch_id: str | None = None, base_dir: str | None = None) -> None
    """

    # ---- class-level RAM cache (thread-safe across instances) ----
    _cache_by_path: dict[str, OrderedDict] = {}
    _mtime_by_path: dict[str, float | None] = {}
    _lock = threading.RLock()

    # ---- user-defined variables (defaults) ----
    _default_base_dir = r"switch_settings"
    STANDARD_FILENAME = "switch.toml"

    # Directory names that should be treated as templates and not listed as real switches
    _TEMPLATE_DIR_NAMES = {"factory", "templates", "template", "_templates"}

    def __init__(self, base_dir: str = _default_base_dir):
        # Bare runtime roots live under ~/Sensorius, not the source checkout.
        self.base_dir = resolve_runtime_base_dir(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if DEBUG:
            printDM(f"Initialized with base_dir={self.base_dir}", location=MODULE)

    # --------------- cache control ---------------
    @classmethod
    def invalidate_cache(cls, switch_id: str | None = None, base_dir: str | None = None):
        """
        Invalidate cache for a specific file (by id + base_dir),
        for a whole base_dir, or for ALL files.
        Supports both new (<base>/<id>/switch.toml) and legacy (<base>/<id>.toml).
        """
        def _candidates(basedir: str, fid: str) -> list[str]:
            base = Path(rf"{basedir}").expanduser().resolve()
            newp = (base / fid / cls.STANDARD_FILENAME).resolve()
            oldp = (base / f"{fid}.toml").resolve()
            return [str(newp), str(oldp)]

        with cls._lock:
            # Specific file in a given base_dir
            if switch_id and base_dir:
                for abs_path in _candidates(base_dir, switch_id):
                    cls._cache_by_path.pop(abs_path, None)
                    cls._mtime_by_path.pop(abs_path, None)
                    if DEBUG:
                        printDM(f"[SwitchMgr] Cache invalidated for {abs_path}", location=MODULE)
                return

            # Everything under a given base_dir
            if base_dir and not switch_id:
                base = Path(rf"{base_dir}").expanduser().resolve()
                to_drop = [p for p in list(cls._cache_by_path.keys())
                           if str(Path(p)).startswith(str(base))]
                for abs_path in to_drop:
                    cls._cache_by_path.pop(abs_path, None)
                    cls._mtime_by_path.pop(abs_path, None)
                    if DEBUG:
                        printDM(f"[SwitchMgr] Cache invalidated under {base} → {abs_path}", location=MODULE)
                return

            # Global flush (no args, or id without base_dir)
            cls._cache_by_path.clear()
            cls._mtime_by_path.clear()
            if DEBUG:
                printDM("[SwitchMgr] Cache invalidated for ALL files", location=MODULE)

    # --------------- public API (unchanged signatures) ---------------
    def list_switches(self) -> list[str]:
        """
        Return discovered switch IDs, excluding template entries.
        Template filtering rules:
          - Skip directories named in _TEMPLATE_DIR_NAMES (case-insensitive).
          - Skip any entry whose [Switch].DEVICE == "template".
          - Skip any entry whose [Switch].SWITCH_DEVICE_ID is empty.
        Applies to both new layout (<base>/<id>/switch.toml) and legacy (<base>/<id>.toml).
        """
        ids: set[str] = set()

        # --- New layout: <base>/<id>/switch.toml
        for child in self.base_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith("."):
                continue
            if name.lower() in self._TEMPLATE_DIR_NAMES:
                # e.g., 'factory' template directory
                continue

            sw_file = child / self.STANDARD_FILENAME
            if not sw_file.exists():
                continue

            # Peek to ensure not a template / not empty SWITCH_DEVICE_ID
            try:
                dat = self._parse_toml_from_disk(sw_file)
                sw = dat.get("Switch", {})
                device = str(sw.get("DEVICE", "")).strip().lower()
                switch_id = str(sw.get("SWITCH_DEVICE_ID", "")).strip()

                if device == "template":
                    continue
                if not switch_id:
                    # Treat blank SWITCH_DEVICE_ID as template-ish / not provisioned
                    continue
            except Exception:
                # On parse failure, be conservative: do not include
                continue

            ids.add(name)

        # --- Legacy: top-level *.toml
        for toml_path in self.base_dir.glob("*.toml"):
            stem = toml_path.stem
            if stem.lower() in self._TEMPLATE_DIR_NAMES:
                continue

            try:
                dat = self._parse_toml_from_disk(toml_path)
                sw = dat.get("Switch", {})
                device = str(sw.get("DEVICE", "")).strip().lower()
                switch_id = str(sw.get("SWITCH_DEVICE_ID", "")).strip()
                if device == "template" or not switch_id:
                    continue
            except Exception:
                continue

            ids.add(stem)

        return sorted(ids)

    def get_path(self, switch_id: str) -> Path:
        # Prefer new layout; fallback if only old exists
        p = self._resolve_read_path(switch_id)
        return p if p else self._new_path_for(switch_id)

    def get_setting(self, switch_id: str, dotted_key: str, default=None, *, force: bool=False):
        """
        Read a single setting via dotted path, e.g. "Switch.SWITCH_LOCATION".
        If force=True, bypass RAM cache and reload from disk (mtime aware).
        """
        # Resolve path (exists check so we can force-load)
        abs_path = self._resolve_read_path(switch_id)
        if not abs_path:
            return default

        # Load with cache policy
        doc = self._load_cached_file(abs_path, force=force) or {}
        try:
            section, key = dotted_key.split(".", 1)
        except ValueError:
            return default
        block = doc.get(section, {})
        if not isinstance(block, dict):
            return default
        return block.get(key, default)

    def set_setting(self, switch_id: str, dotted_key: str, value) -> None:
        """
        Write a single setting to [Switch] (or another section if you add support later).
        """
        with self._lock:
            try:
                section, key = dotted_key.split(".", 1)
            except ValueError:
                raise ValueError(f"Bad dotted key: {dotted_key!r}")

            current = self.load(switch_id) or OrderedDict()
            if section not in current or not isinstance(current[section], dict):
                current[section] = OrderedDict()
            current[section][key] = value
            self.save(switch_id, current)


    def get_switch_channel_names(self, doc_or_id) -> list[str]:
        """
        Return channel labels ['Fan','Light',...] ordered by channel index.
        Accepts either:
          - a loaded settings dict/OrderedDict (with [Switch] block), or
          - a switch_id (str), in which case this method will load it.
        """
        # 1) resolve the [Switch] block
        if isinstance(doc_or_id, (dict, OrderedDict)):
            switch_block = (doc_or_id or {}).get("Switch", {}) or {}
        else:
            loaded_doc = self.load(str(doc_or_id)) or {}
            switch_block = loaded_doc.get("Switch", {}) or {}

        if not isinstance(switch_block, dict):
            return []

        # 2) collect SWITCH_N_LABEL values in numeric order
        channel_pairs: list[tuple[int, str]] = []
        for key, value in switch_block.items():
            if not key.startswith("SWITCH_"):
                continue
            m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
            if not m:
                continue
            label = (value or "")
            if isinstance(label, str) and label.strip():
                try:
                    channel_pairs.append((int(m.group(1)), label.strip()))
                except ValueError:
                    pass

        channel_pairs.sort(key=lambda kv: kv[0])
        return [name for _, name in channel_pairs]

    def build_switch_list(self) -> dict[str, dict]:
        """
        Scan switch_settings/* for real switches (using list_switches())
        and return a compact structure for the web tier:

        {
          "<switch_id>": {
            "switch_id": "<switch_id>",
            "device":    "<DEVICE or 'switch'>",
            "type":      "<TYPE or None>",           # 'pi' | 'picow' | 'pico2w' typically
            "switch_location":  "<SWITCH_LOCATION or 'Unknown'>",
            "channels":  ["Fan","Light",...],        # ordered labels
            "source":    "settings_fs",              # discovery tag
          },
          ...
        }
        """
        discovered: dict[str, dict] = {}
        for switch_id in (self.list_switches() or []):
            try:
                doc = self.load(switch_id) or {}
                sw = doc.get("Switch", {}) or {}
                device_name = str(sw.get("DEVICE", "") or "switch")
                type_value  = sw.get("TYPE")  # 'pi' or 'picow'/'pico2w' or None
                switch_loc    = str(sw.get("SWITCH_LOCATION", "") or "Unknown")
                channel_names = self.get_switch_channel_names(doc)

                discovered[switch_id] = {
                    "switch_id": switch_id,
                    "device": device_name,
                    "type": type_value,
                    "switch_location": switch_loc,
                    "channels": channel_names,
                    "source": "settings_fs",
                }
            except Exception as exc:
                if DEBUG:
                    printDM(f"[SwitchMgr] build_switch_list: {switch_id} skipped: {exc}", location=MODULE)
                continue
        return discovered


    def ensure_host_switch(
        self,
        host_id: str | None = None,
        template_id: str = "factory",
        switch_loc: str | None = None,
    ) -> str:
        """
        Ensure a host-specific switch file exists:
          switch_settings/<host_id>/switch.toml

        If missing, copy from 'template_id' (default 'factory') and
        update [Switch].DEVICE to "switch", SWITCH_ID, and optional SWITCH_LOCATION.

        Returns the host_id used.
        """
        import socket

        # 1) resolve host id
        host_id = (host_id or socket.gethostname() or "").strip()
        if not host_id:
            raise ValueError("ensure_host_switch: could not determine host_id")

        # 2) already exists? done.
        host_path = self._new_path_for(host_id)
        if host_path.exists():
            if DEBUG:
                printDM(f"[SwitchMgr] Host switch already present: {host_path}", location=MODULE)
            return host_id

        # 3) load template from factory dir by name first, then legacy id.
        # Special-case "factory" to a concrete default template so startup
        # does not silently fall back to hardcoded defaults.
        tmpl = self.load_factory_template(template_id) or self.load(template_id)
        if (not tmpl or "Switch" not in tmpl) and str(template_id).strip().lower() == "factory":
            tmpl = self.load_factory_template("switch_3_relay")
        if not tmpl or "Switch" not in tmpl or not isinstance(tmpl["Switch"], dict):
            if DEBUG:
                printDM(f"[SwitchMgr] Template '{template_id}' missing or invalid; creating minimal default", location=MODULE)
            tmpl = OrderedDict()
            tmpl["Switch"] = OrderedDict({
                "DEVICE": "switch",
                "DEVICE_SERIAL_NUM": "",
                "SWITCH_DEVICE_ID": "",
                "SWITCH_LOCATION": "Unknown",
                "SWITCH_ENABLE_PIN": 5,
                "SWITCH_ACTIVE_LEVEL": "high",
                "SWITCH_1_LABEL": "Fan",    "SWITCH_1_CHANNEL_ID": "S1-", "SWITCH_1_PIN": 26, "SWITCH_1_LAST_STATE": False, "SWITCH_1_OVERRIDE_SCRIPT": False,
                "SWITCH_2_LABEL": "Light",  "SWITCH_2_CHANNEL_ID": "S2-", "SWITCH_2_PIN": 20, "SWITCH_2_LAST_STATE": False, "SWITCH_2_OVERRIDE_SCRIPT": False,
                "SWITCH_3_LABEL": "Pump",   "SWITCH_3_CHANNEL_ID": "S3-", "SWITCH_3_PIN": 21, "SWITCH_3_LAST_STATE": False, "SWITCH_3_OVERRIDE_SCRIPT": False,
            })

        # 4) apply overrides: DEVICE = "switch", SWITCH_DEVICE_ID, SWITCH_LOCATION
        sw = tmpl["Switch"]
        sw["DEVICE"] = "switch"  # <-- force host copy to be a real switch, not a template
        sw["SWITCH_DEVICE_ID"] = host_id
        sw["SWITCH_ID"] = host_id
        if switch_loc is not None:
            sw["SWITCH_LOCATION"] = str(switch_loc)
        self._ensure_local_identity_fields(host_id, sw)

        # 5) save to host path (new layout) and cache
        self.save(host_id, tmpl)

        if DEBUG:
            printDM(f"[SwitchMgr] Created host switch settings at {host_path}", location=MODULE)

        return host_id

    # ---------- factory template I/O ----------
    def load_factory_template(self, template_name: str, template_dir: str = "factory") -> OrderedDict | None:
        """
        Load a template TOML from <base_dir>/<template_dir>/<template_name>.toml
        Returns an OrderedDict like {"Switch": {...}} or None if missing.
        """
        tpl_path = (self.base_dir / template_dir / f"{template_name}.toml").resolve()
        try:
            if tpl_path.exists():
                return self._parse_toml_from_disk(tpl_path)
        except Exception as exc:
            if DEBUG:
                printDM(f"[SwitchMgr] load_factory_template({template_name}) error: {exc}", location=MODULE)
        return None

    def materialize_from_template(self, switch_id: str, template_name: str, *, switch_loc: str | None = None) -> str:
        """
        Create/overwrite <base>/<switch_id>/switch.toml from a factory template.
        Preserves identity & location fields and forces DEVICE='switch'.
        Returns the switch_id used.
        """
        tmpl = self.load_factory_template(template_name)
        if not tmpl or "Switch" not in tmpl or not isinstance(tmpl["Switch"], dict):
            raise FileNotFoundError(f"Factory template not found/invalid: {template_name!r}")

        sw = tmpl["Switch"]
        sw["DEVICE"] = "switch"
        sw["SWITCH_DEVICE_ID"] = switch_id
        sw["SWITCH_ID"] = switch_id
        if switch_loc is not None:
            sw["SWITCH_LOCATION"] = str(switch_loc)
        self._ensure_local_identity_fields(switch_id, sw)

        self.save(switch_id, tmpl)
        if DEBUG:
            printDM(f"[SwitchMgr] Materialized {switch_id} from template '{template_name}'", location=MODULE)
        return switch_id

    def retarget_to_template(self, switch_id: str, template_name: str,
                             preserve_keys: tuple[str, ...] = ("DEVICE", "SWITCH_DEVICE_ID", "SWITCH_LOCATION")) -> None:
        """
        Replace the on-disk switch.toml layout using a factory template while
        preserving key identity/location fields.
        """
        current = self.load(switch_id) or OrderedDict()
        keep = {}
        try:
            sw = current.get("Switch", {}) or {}
            for k in preserve_keys:
                if k in sw:
                    keep[k] = sw[k]
        except Exception:
            pass

        tmpl = self.load_factory_template(template_name)
        if not tmpl or "Switch" not in tmpl:
            raise FileNotFoundError(f"Factory template not found/invalid: {template_name!r}")

        tpl_sw = tmpl["Switch"]
        tpl_sw.update(keep)
        self._ensure_local_identity_fields(switch_id, tpl_sw)
        self.save(switch_id, tmpl)
        if DEBUG:
            printDM(f"[SwitchMgr] Retargeted {switch_id} to template '{template_name}' (preserved {list(keep.keys())})",
                    location=MODULE)


    def load(self, switch_id: str) -> OrderedDict | None:
        abs_path = self._resolve_read_path(switch_id)
        if not abs_path:
            return None
        return self._load_cached_file(abs_path)

    def save(self, switch_id: str, settings: dict | OrderedDict) -> None:
        """
        Save settings to disk and update RAM cache (write-through).
        Accepts the same structure the old code used: {"Switch": {...}}.
        """
        with self._lock:
            # Clear both potential cache entries first to avoid dual-cache state
            self.invalidate_cache(switch_id, str(self.base_dir))

            abs_path = self._resolve_write_path(switch_id)
            self._emit_toml_to_disk(abs_path, settings)

            self.__class__._cache_by_path[str(abs_path)] = copy.deepcopy(OrderedDict(settings))
            try:
                self.__class__._mtime_by_path[str(abs_path)] = os.path.getmtime(abs_path)
            except Exception:
                self.__class__._mtime_by_path[str(abs_path)] = None

            if DEBUG:
                printDM(f"Saved and cached: {abs_path}", location=MODULE)

    def update_setting(self, switch_id: str, key: str, value) -> None:
        """
        Update a single key under [Switch].
        """
        with self._lock:
            current = self.load(switch_id) or OrderedDict()
            if "Switch" not in current or not isinstance(current["Switch"], dict):
                current["Switch"] = OrderedDict()
            current["Switch"][key] = value
            self.save(switch_id, current)

    def delete_switch(self, switch_id: str) -> bool:
        new_file = self._new_path_for(switch_id)
        legacy_file = self._legacy_path_for(switch_id)
        deleted = False

        try:
            if new_file.exists():
                new_file.unlink()
                deleted = True
                try:
                    new_file.parent.rmdir()  # remove empty dir if possible
                except Exception:
                    pass
            elif legacy_file.exists():
                legacy_file.unlink()
                deleted = True
        except Exception as exc:
            printDM(f"delete_switch({switch_id}) error: {exc}", location=MODULE)

        # Clear cache entries for both variants
        self.invalidate_cache(switch_id, str(self.base_dir))
        if deleted and DEBUG:
            printDM(f"Deleted settings for {switch_id}", location=MODULE)
        return deleted

    # --------------- internals ---------------
    def _dir_for(self, switch_id: str) -> Path:
        safe = (switch_id or "").strip()
        if not safe or safe in {".", ".."}:
            raise ValueError(f"Illegal switch_id: {switch_id!r}")
        if "/" in safe or "\\" in safe:
            raise ValueError(f"Illegal switch_id: {switch_id!r}")
        target = (self.base_dir / safe).resolve()
        base = self.base_dir.resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(f"Illegal switch_id path traversal: {switch_id!r}") from None
        return target

    def _new_path_for(self, switch_id: str) -> Path:
        # Preferred new layout: switch_settings/<switch_id>/switch.toml
        return self._dir_for(switch_id) / self.STANDARD_FILENAME

    def _legacy_path_for(self, switch_id: str) -> Path:
        # Backward-compat: switch_settings/<switch_id>.toml
        return self.base_dir / f"{switch_id}.toml"

    def _resolve_read_path(self, switch_id: str) -> Path | None:
        p_new = self._new_path_for(switch_id)
        if p_new.exists():
            return p_new
        p_old = self._legacy_path_for(switch_id)
        return p_old if p_old.exists() else None

    def _resolve_write_path(self, switch_id: str) -> Path:
        # Always write to NEW layout
        p_new = self._new_path_for(switch_id)
        p_new.parent.mkdir(parents=True, exist_ok=True)
        return p_new

    def _load_cached_file(self, abs_path: Path, force: bool = False) -> OrderedDict:
        """
        Load from RAM cache unless mtime changed or force=True.
        Returns a deep copy so callers cannot mutate shared cache.
        """
        path_key = str(abs_path)
        with self._lock:
            file_exists = abs_path.exists()
            new_mtime = abs_path.stat().st_mtime if file_exists else None
            cached = self._cache_by_path.get(path_key)
            cached_mtime = self._mtime_by_path.get(path_key)

            needs_refresh = (
                force
                or (cached is None)
                or (cached_mtime != new_mtime)
                or (not file_exists)  # ensure we don't serve stale after deletion
            )

            if needs_refresh:
                parsed = self._parse_toml_from_disk(abs_path) if file_exists else OrderedDict()
                self._cache_by_path[path_key] = parsed
                self._mtime_by_path[path_key] = new_mtime
                if DEBUG:
                    src = "disk" if file_exists else "empty"
                    printDM(f"Loaded {path_key} from {src}; mtime={new_mtime}", location=MODULE)
            else:
                if DEBUG:
                    printDM(f"Loaded {path_key} from RAM cache", location=MODULE)

            return copy.deepcopy(self._cache_by_path[path_key])

    @staticmethod
    def _strip_inline_comment(line: str) -> str:
        """
        Remove inline comments (# ...) only when '#' is outside quotes.
        Preserves quotes and escaped quotes.
        """
        in_quote = False
        quote_char = ""
        escaped = False
        for idx, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if in_quote:
                if ch == quote_char:
                    in_quote = False
                continue
            else:
                if ch in ("'", '"'):
                    in_quote = True
                    quote_char = ch
                    continue
                if ch == "#":
                    # Trim everything from here
                    return line[:idx].rstrip()
        return line.rstrip()

    def _parse_toml_from_disk(self, abs_path: Path) -> OrderedDict:
        """
        Minimal parser for current TOML (only [Switch] section needed).
        Preserves numbers, booleans, and strings. Tolerant of whitespace and inline comments.
        """
        settings = OrderedDict()
        section = None
        try:
            with abs_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.rstrip("\n")
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#"):
                        continue

                    # Remove inline comments outside of quotes
                    line = self._strip_inline_comment(stripped)
                    if not line:
                        continue

                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1]
                        settings[section] = OrderedDict()
                        continue

                    if "=" in line and section:
                        key, value = map(str.strip, line.split("=", 1))
                        # booleans
                        val_lower = value.lower()
                        if val_lower in ("true", "false"):
                            parsed = (val_lower == "true")
                        # quoted string
                        elif (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                            parsed = value[1:-1]
                        else:
                            # number, or leave as string
                            try:
                                parsed = float(value) if "." in value else int(value)
                            except Exception:
                                parsed = value
                        settings[section][key] = parsed
        except Exception as exc:
            printDM(f"Parse error for {abs_path}: {exc}", location=MODULE)
        return settings

    def _emit_toml_to_disk(self, abs_path: Path, data: dict | OrderedDict) -> None:
        """
        Minimal TOML emitter supporting scalar key/value pairs by section.
        Atomic write via temp file + os.replace
        """
        if not isinstance(data, dict):
            raise ValueError("Settings payload must be a dict-like object")

        switch_section = data.get("Switch", {})
        if not isinstance(switch_section, dict):
            raise ValueError("Missing [Switch] section")

        lines: list[str] = []
        for section_name, section_values in data.items():
            if not isinstance(section_values, dict):
                continue
            lines.append(f"[{section_name}]\n")
            for key, value in section_values.items():
                if isinstance(value, str):
                    encoded = json.dumps(value)           # safe quoting/escaping
                elif isinstance(value, bool):
                    encoded = "true" if value else "false"
                else:
                    encoded = f"{value}"
                lines.append(f"{key} = {encoded}\n")
            lines.append("\n")

        text_out = "".join(lines)
        tmp_path = abs_path.with_suffix(".toml.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                f.write(text_out)
            os.replace(tmp_path, abs_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # --------------- channel-id helpers ---------------
    @staticmethod
    def _generate_channel_id(channel_index: int, suffix: str | None = None) -> str:
        """
        Generate a stable-looking channel ID like 'S1-123456'.
        Called only when a SWITCH_N_CHANNEL_ID is missing/invalid.
        """
        suffix_text = str(suffix or "").strip()
        if suffix_text:
            return make_channel_id(channel_index, suffix_text)
        random_value = secrets.randbelow(1_000_000)  # 000000..999999
        return f"S{channel_index}-{random_value:06d}"

    def _ensure_channel_ids(self, switch_block: dict, *, suffix: str | None = None) -> None:
        """
        Ensure each defined SWITCH_N_LABEL has a companion SWITCH_N_CHANNEL_ID.
        Repairs placeholder IDs like 'S1-' and does NOT overwrite real IDs.
        """
        if not isinstance(switch_block, dict):
            return

        # We iterate over a snapshot of items so we can safely add new keys.
        for key, value in list(switch_block.items()):
            if not key.startswith("SWITCH_"):
                continue

            m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
            if not m:
                continue

            try:
                channel_index = int(m.group(1))
            except ValueError:
                continue

            # Only care about channels that actually have a label
            label = value
            if not (isinstance(label, str) and label.strip()):
                continue

            id_key = f"SWITCH_{channel_index}_CHANNEL_ID"
            existing_id = switch_block.get(id_key)

            # If an ID already exists and is non-placeholder, leave it alone
            if isinstance(existing_id, str) and existing_id.strip() and not is_placeholder_channel_id(existing_id, channel_index=channel_index):
                continue

            # Otherwise, generate and assign a new one
            switch_block[id_key] = self._generate_channel_id(channel_index, suffix=suffix)

    def _ensure_local_identity_fields(self, switch_id: str, switch_block: dict) -> str:
        if not isinstance(switch_block, dict):
            return ""

        sw_type = str(switch_block.get("TYPE", "pi") or "pi").strip().lower()
        is_remote = sw_type in {"picow", "pico2w", "nodus", "mqtt", "remote"}
        serial = str(switch_block.get("DEVICE_SERIAL_NUM", "") or "").strip()

        if not is_remote:
            if not serial:
                serial = resolve_persisted_host_serial(
                    switch_id,
                    switch_base_dir=self.base_dir,
                    sensor_base_dir="sensor_settings",
                )
            if serial:
                switch_block["DEVICE_SERIAL_NUM"] = serial
                self._ensure_channel_ids(switch_block, suffix=serial)
                return serial

        self._ensure_channel_ids(switch_block)
        return serial
            
    def ensure_channel_ids_for_switch(self, switch_id: str) -> bool:
        """
        Backfill SWITCH_N_CHANNEL_ID keys for an existing switch, if missing.
        Returns True if changes were made and saved, False otherwise.
        """
        with self._lock:
            doc = self.load(switch_id) or OrderedDict()
            sw = doc.get("Switch", {}) or {}
            if not isinstance(sw, dict):
                return False

            before = dict(sw)
            self._ensure_local_identity_fields(switch_id, sw)

            if sw != before:
                # Something changed; write back
                doc["Switch"] = sw
                self.save(switch_id, doc)
                if DEBUG:
                    printDM(f"[SwitchMgr] Added missing channel IDs for {switch_id}", location=MODULE)
                return True
            return False
