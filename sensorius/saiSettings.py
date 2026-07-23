"""Settings manager for Sensorius system configuration.

This module provides a thin TOML-like settings layer with:
- Device-scoped storage in ``system_settings/<device_id>/settings.toml``.
- Legacy single-file fallback compatibility.
- Process-local read caching keyed by absolute path + mtime.
- Atomic write-through persistence and startup backup creation.

Notes:
- Serialization is intentionally simple and optimized for the project's
  current settings schema.
- Runtime validation of user-entered values is handled separately.
"""

from __future__ import annotations

import os
import json
import base64
import tomllib
import threading
import copy
import shutil
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import OrderedDict
from .saiUtils import debug_enabled, printDM, get_pi_network_info, get_time_settings
from .saiSensorSettingsManager import SensorSettingsManager
from .saiRuntimePaths import resolve_runtime_base_dir

MODULE = "saiSettings"
DEBUG = debug_enabled(MODULE)
DEFAULT_MAX_SETTINGS_FILE_BYTES = 1024 * 1024

class saiSettings:
    # ---- class-level cache (path -> settings / mtime) ----
    _cache_by_path: dict[str, OrderedDict] = {}
    _mtime_by_path: dict[str, float | None] = {}
    _lock = threading.RLock()
    _startup_backup_done_by_path: set[str] = set()
    IP_GEOLOCATION_PROVIDERS = (
        ("ipapi.co", "https://ipapi.co/json/"),
        ("ip-api.com", "http://ip-api.com/json/"),
        ("ipwho.is", "https://ipwho.is/"),
    )

    # ---- foldered-layout constants ----
    DEFAULT_BASE_DIR = r"system_settings"
    STANDARD_FILENAME = "settings.toml"
    _SECRET_PREFIX_V1 = "obf:v1:"
    _SECRET_V1_MARKER = b"\x00"
    _SECRET_LITERAL_ESCAPE = "plain:"
    _SECRET_KEY_V1 = b"sai-ha-v1"

    def __init__(
        self,
        filename: str | None = "settings.toml",   # legacy single-file path (kept for drop-in)
        make_startup_backup: bool = True,
        apply_live: bool = True,
        base_dir: str = DEFAULT_BASE_DIR,         # foldered layout root
        device_id: str | None = None              # system_settings/<device_id>/settings.toml
    ):
        """
        If 'device_id' is provided (or discoverable), settings live at:
            <base_dir>/<device_id>/settings.toml
        Otherwise we fall back to the legacy single-file 'filename'.

        Writes always target the new foldered path; reads prefer new path, fallback to legacy.
        """
        self._cache = None
        self._mtime = None
        
        # Resolve device_id (default to Pi hostname)
        if device_id is None:
            try:
                net_info = get_pi_network_info()
                device_id = (net_info.get("hostname") or "").strip()
            except Exception:
                device_id = ""
        self.device_id = device_id

        # Resolve base dir (absolute)
        self.base_dir = resolve_runtime_base_dir(base_dir)

        # Candidate paths
        self._new_path = (self.base_dir / self.device_id / self.STANDARD_FILENAME) if self.device_id else None
        # Legacy single-file path (absolute)
        self._legacy_path = Path(rf"{filename}").expanduser().resolve() if filename else None

        # Choose initial active path for reading
        self._abs_path = self._resolve_read_path()

        # Ensure parent directories for new path will exist on write
        if self._new_path:
            try:
                self._new_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        # ensure a real file exists for this host on first boot
        self._seed_from_factory_if_missing()

        self.apply_live = apply_live
        self._dirty = False

        # --- Create .bak backup on first instantiation (of the path we are reading) ---
        if make_startup_backup:
            self._ensure_startup_backup()

        # Load into RAM (and from disk only if needed)
        self.settings = self._load_settings_cached()

        # Apply live values & keep cache/write-through consistent
        if self.apply_live:
            self.apply_auto_values()

        # convenience attrs used elsewhere in the project
        self.broker = self.get_setting("SensorNetwork", "BROKER")
        self.sensor_ids = self.get_all_sensor_ids()

        if DEBUG:
            printDM(
                f"Using settings path: {self._abs_path} (device_id={self.device_id or 'N/A'})",
                location=MODULE,
            )

    # ---------- path resolution ----------
    def _resolve_read_path(self) -> str:
        """
        Prefer new foldered path if it exists, else fall back to legacy file.
        If neither exists, default to NEW path if we have a device_id, else legacy filename.
        """
        # prefer new if present
        if self._new_path and self._new_path.exists():
            if self._settings_file_within_size(self._new_path):
                return str(self._new_path.resolve())
            backup_path = self._new_path.with_name(self._new_path.name + ".bak")
            if backup_path.exists() and self._settings_file_within_size(backup_path):
                printDM(
                    f"Settings file too large; reading startup backup instead: {backup_path}",
                    location=MODULE,
                )
                return str(backup_path.resolve())
            return str(self._new_path.resolve())
        # fallback legacy if present
        if self._legacy_path and self._legacy_path.exists():
            if self._settings_file_within_size(self._legacy_path):
                return str(self._legacy_path.resolve())
            backup_path = self._legacy_path.with_name(self._legacy_path.name + ".bak")
            if backup_path.exists() and self._settings_file_within_size(backup_path):
                printDM(
                    f"Legacy settings file too large; reading startup backup instead: {backup_path}",
                    location=MODULE,
                )
                return str(backup_path.resolve())
            return str(self._legacy_path.resolve())
        # neither exists: choose where we'll write next
        if self._new_path:
            return str(self._new_path.resolve())
        # worst case: legacy path string (may not exist yet)
        return str(self._legacy_path.resolve()) if self._legacy_path else os.path.abspath(self.STANDARD_FILENAME)

    def _resolve_write_path(self) -> str:
        """
        Always write to NEW foldered layout if we have a device_id,
        otherwise write to legacy path (for strict drop-in scenarios).
        """
        if self._new_path:
            self._new_path.parent.mkdir(parents=True, exist_ok=True)
            return str(self._new_path.resolve())
        # fallback: legacy
        if self._legacy_path:
            self._legacy_path.parent.mkdir(parents=True, exist_ok=True)
            return str(self._legacy_path.resolve())
        # last resort: local CWD settings.toml
        return os.path.abspath(self.STANDARD_FILENAME)

    def _candidate_paths(self) -> list[str]:
        """
        All absolute path variants that may be cached:
        - NEW: <base_dir>/<device_id>/settings.toml
        - LEGACY: <legacy filename>
        """
        paths: list[str] = []
        if self._new_path:
            paths.append(str(self._new_path.resolve()))
        if self._legacy_path:
            paths.append(str(self._legacy_path.resolve()))
        # include current abs path
        paths.append(str(Path(self._abs_path).resolve()))
        # de-dup
        return list(dict.fromkeys(paths))

    def _max_settings_file_bytes(self) -> int:
        try:
            return max(1024, int(os.environ.get("SENSORIUS_SETTINGS_MAX_BYTES", DEFAULT_MAX_SETTINGS_FILE_BYTES)))
        except Exception:
            return DEFAULT_MAX_SETTINGS_FILE_BYTES

    def _settings_file_within_size(self, path: str | Path) -> bool:
        try:
            return Path(path).stat().st_size <= self._max_settings_file_bytes()
        except FileNotFoundError:
            return True
        except Exception:
            return False

    # ---- public helpers to manually control cache if needed ----
    @classmethod
    def invalidate_cache(cls, path: str | None = None):
        """Invalidate cache for a specific file or all files."""
        with cls._lock:
            if path:
                ap = os.path.abspath(path)
                cls._cache_by_path.pop(ap, None)
                cls._mtime_by_path.pop(ap, None)
                if DEBUG:
                    printDM(f"Invalidated settings cache: {ap}", location=MODULE)
            else:
                cls._cache_by_path.clear()
                cls._mtime_by_path.clear()
                if DEBUG:
                    printDM("Invalidated settings cache: ALL", location=MODULE)

    def invalidate_this_cache(self):
        """Invalidate cache entries for this instance's candidate paths."""
        with self.__class__._lock:
            for p in self._candidate_paths():
                self.__class__._cache_by_path.pop(p, None)
                self.__class__._mtime_by_path.pop(p, None)
                if DEBUG:
                    printDM(f"Invalidated settings cache: {p}", location=MODULE)

    # ---- core: cached load with mtime check ----
    def _load_settings_cached(self, force: bool = False) -> OrderedDict:
        path = self._abs_path
        with self._lock:
            file_exists = os.path.exists(path)
            new_mtime = os.path.getmtime(path) if file_exists else None
            cached = self._cache_by_path.get(path)
            cached_mtime = self._mtime_by_path.get(path)

            needs_refresh = (
                force
                or cached is None
                or cached_mtime != new_mtime
                or (not file_exists)   # avoid serving stale if file was deleted
            )

            if needs_refresh:
                parsed = self._parse_settings_from_disk(path) if file_exists else OrderedDict()
                self._cache_by_path[path] = parsed
                self._mtime_by_path[path] = new_mtime
                if DEBUG:
                    src = "disk" if file_exists else "empty"
                    printDM(f"Loaded settings from {src}, cached mtime={new_mtime}", location=MODULE)
            else:
                if DEBUG:
                    printDM("Loaded settings from RAM cache", location=MODULE)

            # return a defensive copy so callers can’t mutate shared cache
            return copy.deepcopy(self._cache_by_path[path])

    def _maybe_reload(self):
        """
        Ensure this instance's 'settings' reflects the latest on-disk contents.
        Uses the class-level cache keyed by self._abs_path.
        """
        # Let the cached loader decide whether a refresh is needed by mtime.
        doc = self._load_settings_cached(force=False)
        # Keep instance copy in sync so existing call sites keep working.
        self.settings = doc

    # ---- original parser kept intact ----
    def _parse_settings_from_disk(self, path: str) -> OrderedDict:
        settings = OrderedDict()
        section = None
        try:
            path_obj = Path(path)
            if not self._settings_file_within_size(path_obj):
                size = path_obj.stat().st_size
                printDM(
                    (
                        f"Settings file too large to parse safely: {path_obj} "
                        f"({size} bytes > {self._max_settings_file_bytes()} bytes)"
                    ),
                    location=MODULE,
                )
                return settings
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1]
                        settings[section] = OrderedDict()
                    elif "=" in line and section:
                        key, value = map(str.strip, line.split("=", 1))
                        # lists handled above already
                        if value.startswith('[') and value.endswith(']'):
                            value = json.loads(value.replace("'", '"'))
                        elif value.startswith('"') and value.endswith('"'):
                            try:
                                value = json.loads(value)
                            except Exception:
                                value = value[1:-1]
                        else:
                            lv = value.lower()
                            if lv == "true":
                                value = True
                            elif lv == "false":
                                value = False
                            else:
                                try:
                                    value = float(value) if "." in value else int(value)
                                except Exception:
                                    pass
                        settings[section][key] = value
                                               
        except Exception as e:
            printDM(f"Settings parse error: {e}", location=MODULE)
        return settings

    # NEW: perform a one-time backup of the original file
    def _ensure_startup_backup(self):
        with self.__class__._lock:
            src = self._abs_path
            if src in self.__class__._startup_backup_done_by_path:
                return
            if os.path.exists(src):
                bak = src + ".bak"
                try:
                    if not os.path.exists(bak):
                        shutil.copy2(src, bak)
                        if DEBUG:
                            printDM(f"Startup backup created: {bak}", location=MODULE)
                except Exception as e:
                    printDM(f"Startup backup failed: {e}", location=MODULE)
            self.__class__._startup_backup_done_by_path.add(src)

    def _seed_from_factory_if_missing(self):
        """
        If the active write path doesn’t exist yet, initialize settings from
        system_settings/factory/settings.toml (if present), then save once.
        """
        write_path = self._resolve_write_path()
        if os.path.exists(write_path):
            return  # already seeded or migrated

        factory_path = self.base_dir / "factory" / self.STANDARD_FILENAME
        # Start from factory defaults if available; else start empty
        if factory_path.exists():
            self.settings = self._parse_settings_from_disk(str(factory_path))
        else:
            self.settings = OrderedDict()

        # We’ll apply live values after this in __init__
        self._dirty = True
        self.save_settings()

    # helper to hash a rendered TOML so we can skip no-op writes
    def _hash_text(self, s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @classmethod
    def obfuscate_secret(cls, plain: str) -> str:
        """
        Reversible lightweight obfuscation for UI-stored secrets.
        This is not encryption; it only avoids raw plaintext-at-rest.
        """
        text = str(plain or "")
        if not text:
            return ""
        # Prefix a marker byte so decode can distinguish managed payloads
        # from arbitrary strings that resemble our wire prefix.
        raw = cls._SECRET_V1_MARKER + text.encode("utf-8")
        key = cls._SECRET_KEY_V1
        xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        token = base64.urlsafe_b64encode(xored).decode("ascii")
        return f"{cls._SECRET_PREFIX_V1}{token}"

    @classmethod
    def deobfuscate_secret(cls, stored: str) -> str:
        """
        Decode supported obfuscated secrets. Plaintext is returned unchanged.
        """
        text = str(stored or "")
        if not text:
            return ""
        if not text.startswith(cls._SECRET_PREFIX_V1):
            return text
        payload = text[len(cls._SECRET_PREFIX_V1):]
        if not payload:
            return ""

        try:
            raw = base64.urlsafe_b64decode(payload.encode("ascii"))
            key = cls._SECRET_KEY_V1
            plain = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
            if plain.startswith(cls._SECRET_V1_MARKER):
                return plain[len(cls._SECRET_V1_MARKER):].decode("utf-8")

            # Backward compatibility with early v1 values and escaped literals.
            text = plain.decode("utf-8")
            if text.startswith(cls._SECRET_LITERAL_ESCAPE):
                return text[len(cls._SECRET_LITERAL_ESCAPE):]
            return text
        except Exception:
            # Keep compatibility if a malformed value somehow exists.
            return text

    def _toml_escape_string(self, value: str) -> str:
        """Return a basic TOML-safe double-quoted string body."""
        return (
            value
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    # ---- write-through save keeps RAM cache in sync ----
    def save_settings(self, settings: OrderedDict | None = None):
        with self._lock:
            if not self._dirty and settings is None:
                return
            settings_to_save = settings or self.settings

            # render to text first so we can compare with what’s on disk
            lines = []
            for section, pairs in settings_to_save.items():
                lines.append(f"[{section}]\n")
                for key, value in pairs.items():
                    if isinstance(value, list):
                        value = json.dumps(value)
                    elif isinstance(value, str):
                        value = f'"{self._toml_escape_string(value)}"'
                    elif isinstance(value, bool):
                        value = "true" if value else "false"
                    else:
                        # ints/floats and other JSON-like scalars
                        # (if you ever store dicts here, consider json.dumps)
                        pass

                    lines.append(f"{key} = {value}\n")
                lines.append("\n")
            new_text = "".join(lines)

            # compute write target (NEW layout preferred)
            write_path = self._resolve_write_path()
            # Read current on-disk (if exists) to skip no-op write
            old_text = ""
            if os.path.exists(write_path):
                try:
                    if self._settings_file_within_size(write_path):
                        with open(write_path, "r", encoding="utf-8") as f:
                            old_text = f.read()
                    else:
                        printDM(
                            f"Existing settings file is too large; replacing without full read: {write_path}",
                            location=MODULE,
                        )
                except Exception:
                    old_text = ""

            if self._hash_text(new_text) == self._hash_text(old_text):
                if DEBUG:
                    printDM("save_settings skipped (no changes)", location=MODULE)
                self._dirty = False
                # ensure our active path is the write path now
                self._abs_path = write_path
                return

            # ensure parent exists for new layout
            Path(write_path).parent.mkdir(parents=True, exist_ok=True)

            # write to disk (atomic replace via temp)
            tmp_path = write_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, write_path)

            # switch active path to NEW layout (if we just migrated)
            self._abs_path = write_path
            self._dirty = False
            if DEBUG:
                printDM(f"Settings saved → {write_path}", location=MODULE)

            # clear any legacy/new entries to prevent dual-cache
            for p in self._candidate_paths():
                self.__class__._cache_by_path.pop(p, None)
                self.__class__._mtime_by_path.pop(p, None)
            # set the new one
            self.__class__._cache_by_path[write_path] = copy.deepcopy(settings_to_save)
            try:
                self.__class__._mtime_by_path[write_path] = os.path.getmtime(write_path)
            except Exception:
                self.__class__._mtime_by_path[write_path] = None

    def set_in_memory(self, section: str, key: str, value):
        """Update a setting in RAM only; defer disk write until save_settings()."""
        with self._lock:
            if section not in self.settings or not isinstance(self.settings[section], OrderedDict):
                self.settings[section] = OrderedDict()
            self.settings[section][key] = value
            self._dirty = True

    def set_many_in_memory(self, updates: list[tuple[str, str, object]]):
        """Batch in-RAM updates. 'updates' is a list of (section, key, value)."""
        with self._lock:
            for section, key, value in updates:
                if section not in self.settings or not isinstance(self.settings[section], OrderedDict):
                    self.settings[section] = OrderedDict()
                self.settings[section][key] = value
            self._dirty = True

    def has_unsaved_changes(self) -> bool:
        return bool(self._dirty)

    # ---- public API unchanged below ----
    def get_active_settings_path(self) -> str:
        return self._abs_path

    def apply_auto_values(self):
        # Update network and time from live Pi info
        net_info = get_pi_network_info()
        time_info = get_time_settings() or {}
        updates: list[tuple[str, str, object]] = []

        if net_info.get("hostname"):
            updates.append(("Network", "HOSTNAME", net_info["hostname"]))

        # Accept either case from get_time_settings()
        tz        = time_info.get("TZ",        time_info.get("tz"))
        tz_offset = time_info.get("TZ_OFFSET", time_info.get("tzOffset"))
        tz_name   = time_info.get("TZ_NAME",   time_info.get("tzName"))

        # Only overwrite if we actually detected a TZ (i.e., not None/empty)
        if tz:
            updates.append(("Time", "TZ", tz))
            if tz_offset is not None:
                # Keep your current convention: seconds (factory uses -21600)
                updates.append(("Time", "TZ_OFFSET", int(tz_offset)))
            if tz_name:
                updates.append(("Time", "TZ_NAME", tz_name))
        else:
            # No detection → keep whatever was there (factory defaults)
            # Do nothing.
            pass

        if updates:
            self.set_many_in_memory(updates)
            self.save_settings()

    def replace_setting(self, section, key, value):
        if section not in self.settings:
            self.settings[section] = OrderedDict()
        self.settings[section][key] = value
        self._dirty = True
        self.save_settings()  # write-through + updates RAM cache

    @staticmethod
    def _safe_float(value) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _truthy_text(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _extract_ip_geolocation(payload: dict | None) -> tuple[float | None, float | None, str]:
        """Return latitude, longitude, timezone from known IP geolocation payloads."""
        if not isinstance(payload, dict):
            return None, None, ""

        lat = saiSettings._safe_float(payload.get("latitude"))
        lon = saiSettings._safe_float(payload.get("longitude"))
        if lat is None:
            lat = saiSettings._safe_float(payload.get("lat"))
        if lon is None:
            lon = saiSettings._safe_float(payload.get("lon"))

        tz_raw = payload.get("timezone")
        if isinstance(tz_raw, dict):
            tz_name = str(tz_raw.get("id") or tz_raw.get("name") or "").strip()
        else:
            tz_name = str(tz_raw or "").strip()
        return lat, lon, tz_name

    def timezone_info(self, tz_name: str) -> tuple[int, str]:
        """
        Resolve timezone offset/name from an IANA timezone string.
        Returns (offset_seconds, tz_abbreviation).
        """
        tz = ZoneInfo(str(tz_name).strip())
        now = datetime.now(tz)
        offset = now.utcoffset()
        offset_sec = int(offset.total_seconds()) if offset is not None else 0
        return offset_sec, (now.tzname() or "")

    def resolve_astral_location(self, *, persist_if_auto: bool = False, timeout_sec: float = 2.5) -> dict:
        """
        Resolve Astral location from manual settings or IP geolocation fallback.
        When persist_if_auto is True, successful IP resolution is written to
        [Astral].LATITUDE/LONGITUDE (and TIMEZONE if empty). Manual
        [Astral].ALTITUDE is returned when configured.
        """
        resolved_tz = str(self.get_setting("Astral", "TIMEZONE", "") or "").strip() or str(self.get_setting("Time", "TZ", "") or "").strip()
        resolved_lat = None
        resolved_lon = None
        cfg_altitude = self._safe_float(self.get_setting("Astral", "ALTITUDE", ""))
        resolved_altitude = cfg_altitude if cfg_altitude is not None and -500.0 <= cfg_altitude <= 10000.0 else None
        source = "none"
        provider = ""
        error = ""

        cfg_lat = self._safe_float(self.get_setting("Astral", "LATITUDE", ""))
        cfg_lon = self._safe_float(self.get_setting("Astral", "LONGITUDE", ""))
        cfg_source = str(self.get_setting("Astral", "SOURCE", "") or "").strip().lower()
        cfg_provider = str(self.get_setting("Astral", "PROVIDER", "") or "").strip()
        auto_ip = self._truthy_text(self.get_setting("Astral", "AUTO_IP", True), default=True)
        use_saved_coordinates = (
            cfg_lat is not None
            and cfg_lon is not None
            and -90.0 <= cfg_lat <= 90.0
            and -180.0 <= cfg_lon <= 180.0
            and (cfg_source != "ip" or not auto_ip or not persist_if_auto)
        )

        if use_saved_coordinates:
            resolved_lat = cfg_lat
            resolved_lon = cfg_lon
            source = "manual" if cfg_source != "ip" else "ip_cached"
            provider = cfg_provider if cfg_source == "ip" else ""
        else:
            if auto_ip:
                provider_errors: list[str] = []
                try:
                    import httpx
                    with httpx.Client(timeout=timeout_sec) as client:
                        for provider_name, url in self.IP_GEOLOCATION_PROVIDERS:
                            try:
                                resp = client.get(url)
                                if resp.status_code != 200:
                                    provider_errors.append(f"{provider_name}: HTTP {resp.status_code}")
                                    continue
                                payload = resp.json() or {}
                                if payload.get("success") is False:
                                    provider_errors.append(f"{provider_name}: {payload.get('message') or 'unsuccessful response'}")
                                    continue
                                if str(payload.get("status") or "").strip().lower() == "fail":
                                    provider_errors.append(f"{provider_name}: {payload.get('message') or 'failed response'}")
                                    continue
                                ip_lat, ip_lon, ip_tz = self._extract_ip_geolocation(payload)
                                if ip_lat is not None and ip_lon is not None and -90.0 <= ip_lat <= 90.0 and -180.0 <= ip_lon <= 180.0:
                                    resolved_lat = ip_lat
                                    resolved_lon = ip_lon
                                    if ip_tz:
                                        resolved_tz = ip_tz
                                    source = "ip"
                                    provider = provider_name
                                    break
                                provider_errors.append(f"{provider_name}: invalid coordinates")
                            except Exception as provider_exc:
                                provider_errors.append(f"{provider_name}: {provider_exc.__class__.__name__}: {provider_exc}")
                                continue
                except Exception as exc:
                    provider_errors.append(f"httpx: {exc.__class__.__name__}: {exc}")
                if source == "none" and provider_errors:
                    error = "; ".join(provider_errors[-3:])
                if source == "none" and cfg_source == "ip" and cfg_lat is not None and cfg_lon is not None:
                    resolved_lat = cfg_lat
                    resolved_lon = cfg_lon
                    source = "ip_cached"
                    provider = cfg_provider
            else:
                error = "Astral.AUTO_IP is disabled"

        if persist_if_auto and source == "ip" and resolved_lat is not None and resolved_lon is not None:
            updates: list[tuple[str, str, object]] = []
            updates.append(("Astral", "LATITUDE", f"{resolved_lat:.6f}"))
            updates.append(("Astral", "LONGITUDE", f"{resolved_lon:.6f}"))
            updates.append(("Astral", "SOURCE", "ip"))
            updates.append(("Astral", "PROVIDER", provider))
            if not str(self.get_setting("Astral", "TIMEZONE", "") or "").strip() and resolved_tz:
                updates.append(("Astral", "TIMEZONE", resolved_tz))
            if updates:
                self.set_many_in_memory(updates)
                self.save_settings()

        return {
            "lat": resolved_lat,
            "lon": resolved_lon,
            "altitude": resolved_altitude,
            "tz": resolved_tz,
            "source": source,
            "provider": provider,
            "error": error,
        }

    def get_section(self, name: str, reload_if_changed: bool = False) -> dict:
        if reload_if_changed:
            self._maybe_reload()
        # read from the instance copy (kept in sync by _maybe_reload / save_settings)
        return (self.settings or {}).get(name, {})

    def get_setting(self, section: str, key: str, default=None, *, reload_if_changed: bool = False):
        if reload_if_changed:
            self._maybe_reload()
        return (self.settings or {}).get(section, {}).get(key, default)

    def get_broker(self, reload_if_changed: bool = False) -> str | None:
        sn = self.get_section("SensorNetwork", reload_if_changed=reload_if_changed)
        b = sn.get("BROKER")
        return str(b) if b else None

    def get_all_clients(self, reload_if_changed: bool = False) -> list[str]:
        sn = self.get_section("SensorNetwork", reload_if_changed=reload_if_changed)
        val = sn.get("CLIENTS", [])
        return list(val) if isinstance(val, (list, tuple)) else []

    def add_client(self, hostname):
        # CLIENTS is deprecated; discovery is automatic.
        pass

    def remove_client(self, hostname):
        # CLIENTS is deprecated; discovery is automatic.
        pass

    def get_all_sensor_ids(self):
        try:
            mgr = SensorSettingsManager("sensor_settings")
            return mgr.list_ids()
        except Exception as e:
            if DEBUG:
                printDM(f"Failed listing sensor settings IDs: {e}", location=MODULE)
            return []

    def get_gaugeSize(self):
        return self.get_setting("Display", "gauge_size", "Large")

    def get_displayStyle(self):
        return self.get_setting("Display", "display_style", "Gauge")

    @classmethod
    def get_factory_nodus_ap_credentials(
        cls,
        *,
        base_dir: str | None = None,
    ) -> tuple[str, str]:
        """
        Read AP credentials from:
        system_settings/factory_nodus/settings.toml.def
        """
        root = resolve_runtime_base_dir(base_dir or cls.DEFAULT_BASE_DIR)
        settings_dir = root / "factory_nodus"
        path = settings_dir / f"{cls.STANDARD_FILENAME}.def"
        if not path.exists():
            return "", ""
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            network = data.get("Network", {}) if isinstance(data, dict) else {}
            # Prefer explicit AP credentials; fall back to legacy SSID/PASSWORD keys.
            ssid = str(network.get("AP_SSID", "") or network.get("SSID", "") or "")
            password = str(network.get("AP_PASSWORD", "") or network.get("PASSWORD", "") or "")
            return ssid, password
        except Exception as e:
            if DEBUG:
                printDM(f"Failed reading factory Nodus AP credentials from {path}: {e}", location=MODULE)
            return "", ""
