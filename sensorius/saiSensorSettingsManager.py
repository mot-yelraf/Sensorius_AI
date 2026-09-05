"""Cached TOML manager for per-sensor settings.

Provides fast load/save/update of sensor_settings/<sensor_id>/sensor.toml and
is the source of truth for the sensor factory + controller pipeline:
settings -> saiSensorFactory -> saiSensor.
"""
from __future__ import annotations

import os
import threading
import copy
import json
from pathlib import Path
from collections import OrderedDict
from .saiLocalIdentity import extract_local_host_id_from_sensor_id, resolve_persisted_host_serial
from .saiRuntimePaths import resolve_runtime_base_dir
from .sensor_modules.station_weewx import WEEWX_DISPLAY_METRICS, WEEWX_DISPLAY_STYLES

try:
    import tomllib  # Python 3.11+ (read)
except Exception:
    tomllib = None

from .saiUtils import debug_enabled, printDM

MODULE = "saiSensorSettingsManager"
DEBUG = debug_enabled(MODULE)

DISPLAY_METRIC_MODE_PICK6 = "Pick 6"
DISPLAY_METRIC_MODE_ALL = "All"
DISPLAY_METRIC_MODE_KEY = "METRIC_DISPLAY_MODE"
_DIRECT_LOCAL_BUS_TOKENS = ("i2c", "spi", "uart", "rs485")


def is_direct_local_sensor_id(sensor_id: str | None) -> bool:
    """Return True for Pi-attached local bus sensor IDs."""
    parts = [part.strip().lower() for part in str(sensor_id or "").strip().split("-") if part.strip()]
    return len(parts) >= 4 and parts[1] in _DIRECT_LOCAL_BUS_TOKENS and parts[2].isdigit()


def infer_direct_local_device(sensor_id: str | None) -> str:
    """Infer the local sensor device key from ``<device>-<bus>-<n>-<host>`` IDs."""
    text = str(sensor_id or "").strip()
    if not is_direct_local_sensor_id(text) or "-" not in text:
        return ""
    return text.split("-", 1)[0].strip().lower()


class SensorSettingsManager:
    """
    Manages per-sensor TOML files under a base directory (e.g., 'sensor_settings').

    Public API:
      - load(file_id) -> OrderedDict
      - save(file_id, data) -> None
      - update_setting(file_id, key, value) -> None
      - get_setting(file_id, key, default=None)
      - list_ids() -> list[str]
      - delete_sensor(sensor_id) -> bool
      - get_display_metrics(sensor_id) -> list[str]
      - get_display_metric_mode(sensor_id) -> str
      - set_display_metrics(sensor_id, metrics: list[str]) -> None
      - get_path(sensor_id) -> Path
      - invalidate_cache(file_id: str | None = None, base_dir: str | None = None) -> None
    """

    DISPLAY_METRIC_MODE_PICK6 = DISPLAY_METRIC_MODE_PICK6
    DISPLAY_METRIC_MODE_ALL = DISPLAY_METRIC_MODE_ALL
    DISPLAY_METRIC_MODE_KEY = DISPLAY_METRIC_MODE_KEY

    # ---- class-level RAM cache (thread-safe across instances) ----
    _cache_by_path: dict[str, OrderedDict] = {}
    _mtime_by_path: dict[str, float | None] = {}
    _lock = threading.RLock()
    
    _default_base_dir = r"sensor_settings"
    STANDARD_FILENAME = "sensor.toml"
    _TEMPLATE_DIR_NAMES = {"factory", "template", "templates"}

    def __init__(self, base_dir_name: str = _default_base_dir):
        # Bare runtime roots live under ~/Sensorius, not the source checkout.
        self.base_dir = resolve_runtime_base_dir(base_dir_name)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if DEBUG:
            printDM(f"Initialized with base_dir={self.base_dir}", location=MODULE)

    # --------------- cache control ---------------
    @classmethod
    def invalidate_cache(cls, file_id: str | None = None, base_dir: str | None = None):
        """
        Invalidate cache for a specific file (by id + base_dir),
        for a whole base_dir, or for ALL files.
        Supports both new (<base>/<id>/sensor.toml) and legacy (<base>/<id>.toml).
        """
        def _candidates(basedir: str, fid: str) -> list[str]:
            base = Path(rf"{basedir}").expanduser().resolve()
            newp = (base / fid / cls.STANDARD_FILENAME).resolve()
            oldp = (base / f"{fid}.toml").resolve()
            return [str(newp), str(oldp)]

        with cls._lock:
            # Specific file in a given base_dir
            if file_id and base_dir:
                for abs_path in _candidates(base_dir, file_id):
                    cls._cache_by_path.pop(abs_path, None)
                    cls._mtime_by_path.pop(abs_path, None)
                    if DEBUG:
                        printDM(f"Cache invalidated for {abs_path}", location=MODULE)
                return

            # Everything under a given base_dir
            if base_dir and not file_id:
                base = Path(rf"{base_dir}").expanduser().resolve()
                to_drop = [p for p in list(cls._cache_by_path.keys())
                           if str(Path(p)).startswith(str(base))]
                for abs_path in to_drop:
                    cls._cache_by_path.pop(abs_path, None)
                    cls._mtime_by_path.pop(abs_path, None)
                    if DEBUG:
                        printDM(f"Cache invalidated under {base} → {abs_path}", location=MODULE)
                return

            # Global flush (no args, or file_id without base_dir)
            cls._cache_by_path.clear()
            cls._mtime_by_path.clear()
            if DEBUG:
                printDM("Cache invalidated for ALL files", location=MODULE)


    # --------------- public API ---------------
    def list_ids(self) -> list[str]:
        """Return all sensor file IDs (filename without .toml) in the base directory."""           
        ids: set[str] = set()
        # New layout: any directory containing sensor.toml
        for child in self.base_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if child.name.lower() in self._TEMPLATE_DIR_NAMES:
                continue
            if (child / self.STANDARD_FILENAME).exists():
                ids.add(child.name)
        # Legacy: top-level *.toml
        for toml_path in self.base_dir.glob("*.toml"):
            if toml_path.stem.lower() in self._TEMPLATE_DIR_NAMES:
                continue
            ids.add(toml_path.stem)
        return sorted(ids)

    def get_candidate_paths(self, sensor_id: str) -> tuple[Path, Path]:
        return (self._new_path_for(sensor_id), self._legacy_path_for(sensor_id))

    def get_path(self, sensor_id: str) -> Path:
        # Prefer new layout; fallback if only old exists
        p = self._resolve_read_path(sensor_id)
        return p if p else self._new_path_for(sensor_id)

    def load(self, file_id: str) -> OrderedDict:
        """
        Public load() retained; backed by the RAM cache.
        Returns a defensive copy to prevent accidental cache mutation.
        """
        abs_path = self._resolve_read_path(file_id)
        if not abs_path:
            raise FileNotFoundError(f"No settings for sensor_id={file_id}")
        return self._load_cached_file(abs_path)

    def save(self, file_id: str, data: dict | OrderedDict):
        """
        Merge 'data' into existing TOML (if any) and write the FULL merged doc.
        Also updates RAM cache (write-through).
        """
        with self._lock:
            # Invalidate both path variants before writing (prevents dual-cache)
            self.invalidate_cache(file_id, str(self.base_dir))

            # Determine write path (new layout) and potential read path (new or legacy)
            abs_path = self._resolve_write_path(file_id)
            read_path = self._resolve_read_path(file_id)

            # Start from existing on-disk doc if present; else an empty OrderedDict
            if read_path and read_path.exists():
                current_doc = self._parse_toml_from_disk(read_path)
            elif abs_path.exists():
                current_doc = self._parse_toml_from_disk(abs_path)
            else:
                current_doc = OrderedDict()

            # Normalize input to OrderedDict and deep-merge
            incoming_doc = self._to_ordered(data)  # converts dicts to OrderedDict recursively
            merged_doc = self._deep_merge(current_doc, incoming_doc)

            # Emit FULL merged doc to the canonical (new-layout) path
            self._emit_toml_to_disk(abs_path, merged_doc)

            # Update RAM cache with the FULL merged doc
            self.__class__._cache_by_path[str(abs_path)] = copy.deepcopy(merged_doc)
            try:
                self.__class__._mtime_by_path[str(abs_path)] = os.path.getmtime(abs_path)
            except Exception:
                self.__class__._mtime_by_path[str(abs_path)] = None

            if DEBUG:
                printDM(f"Saved (merged) and cached: {abs_path}", location=MODULE)

    def update_setting(self, file_id: str, key: str, value):
        """
        Update a single top-level key in the sensor's TOML file (flat or inside a section).
        If your files are sectioned, pass dotted keys like 'Display.metric_1'.
        """
        with self._lock:
            current = self.load(file_id) or OrderedDict()
            section_name, key_name = self._split_dotted_key(key)

            if section_name:
                if section_name not in current or not isinstance(current[section_name], dict):
                    current[section_name] = OrderedDict()
                current[section_name][key_name] = value
            else:
                current[key_name] = value

            self.save(file_id, current)

    def get_setting(self, file_id: str, key: str, default=None):
        data = self.load(file_id) or {}
        section_name, key_name = self._split_dotted_key(key)

        if section_name:
            section = data.get(section_name, {})
            return section.get(key_name, default) if isinstance(section, dict) else default
        return data.get(key_name, default)

    def delete_sensor(self, sensor_id: str) -> bool:
        """
        Delete the sensor's TOML (new or legacy) and drop it from RAM cache.
        Returns True if something was deleted.
        """
        deleted = False

        new_file = self._new_path_for(sensor_id)
        legacy_file = self._legacy_path_for(sensor_id)

        try:
            if new_file.exists():
                # remove file
                new_file.unlink()
                deleted = True
                # try to remove the empty directory (ignore if not empty)
                try:
                    new_file.parent.rmdir()
                except Exception:
                    pass
            elif legacy_file.exists():
                legacy_file.unlink()
                deleted = True
        except Exception as exc:
            printDM(f"delete_sensor({sensor_id}) error: {exc}", location=MODULE)

        # Clear cache entries for both variants
        self.invalidate_cache(sensor_id, str(self.base_dir))
        if deleted and DEBUG:
            printDM(f"Deleted settings for {sensor_id}", location=MODULE)
        return deleted

    def get_display_metrics(self, sensor_id: str) -> list[str]:
        try:
            # First attempt: as-is
            settings = self.load(sensor_id)
        except FileNotFoundError:
            # Optional fallback only if you truly expect mixed-case IDs
            try:
                alt_id = sensor_id.lower()
                if alt_id != sensor_id:
                    settings = self.load(alt_id)
                else:
                    raise
            except Exception:
                if DEBUG:
                    printDM(f"[get_display_metrics] No settings for {sensor_id}", location=MODULE)
                return []

        display_block = settings.get("Display", {})
        if DEBUG:
            printDM(f"[get_display_metrics] Display block for {sensor_id}: {display_block}", location=MODULE)

        ordered_metrics: list[str] = []
        for ordinal in range(1, 7):
            key_u = f"METRIC_{ordinal}"
            key_l = f"metric_{ordinal}"
            raw_val = display_block.get(key_u, display_block.get(key_l, ""))
            if isinstance(raw_val, str) and raw_val.strip():
                ordered_metrics.append(raw_val.strip())
        return ordered_metrics

    def get_display_styles(self, sensor_id: str, default_style: str = "Gauge") -> list[str]:
        try:
            settings = self.load(sensor_id)
        except FileNotFoundError:
            return [default_style] * 6

        display_block = settings.get("Display", {}) if isinstance(settings, dict) else {}
        style_block = {}
        if isinstance(display_block, dict):
            style_block = display_block.get("Style", {}) or display_block.get("style", {}) or {}

        ordered_styles: list[str] = []
        for ordinal in range(1, 7):
            key_u = f"METRIC_{ordinal}"
            key_l = f"metric_{ordinal}"
            raw_val = default_style
            if isinstance(style_block, dict):
                raw_val = style_block.get(key_u, style_block.get(key_l, default_style))
            raw_str = str(raw_val or "").strip() or default_style
            ordered_styles.append(raw_str)
        return ordered_styles

    @staticmethod
    def normalize_display_metric_mode(value) -> str:
        raw = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
        compact = raw.replace(" ", "")
        if compact in {"all", "showall"}:
            return DISPLAY_METRIC_MODE_ALL
        return DISPLAY_METRIC_MODE_PICK6

    def get_display_metric_mode(self, sensor_id: str) -> str:
        """Return the local WebUI metric selection mode for a sensor."""
        try:
            settings = self.load(sensor_id)
        except FileNotFoundError:
            return DISPLAY_METRIC_MODE_PICK6

        display_block = settings.get("Display", {}) if isinstance(settings, dict) else {}
        raw_val = ""
        if isinstance(display_block, dict):
            raw_val = (
                display_block.get(DISPLAY_METRIC_MODE_KEY)
                or display_block.get(DISPLAY_METRIC_MODE_KEY.lower())
                or display_block.get("DISPLAY_MODE")
                or display_block.get("display_mode")
                or ""
            )
        return self.normalize_display_metric_mode(raw_val)

    def set_display_metric_mode(self, sensor_id: str, mode: str):
        """Persist the local WebUI metric selection mode without changing six-slot metrics."""
        self.update_setting(sensor_id, f"Display.{DISPLAY_METRIC_MODE_KEY}", self.normalize_display_metric_mode(mode))

    def set_display_metrics(self, sensor_id: str, metrics: list[str]):
        """
        Set display metrics for a sensor. Accepts a list of up to 6 items.
        Pads with empty strings if fewer than 6.
        """
        with self._lock:
            if len(metrics) > 6:
                raise ValueError("Maximum of 6 display metrics allowed")

            settings = self.load(sensor_id) or OrderedDict()
            if "Display" not in settings or not isinstance(settings["Display"], dict):
                settings["Display"] = OrderedDict()

            for ordinal in range(1, 7):
                key_name = f"METRIC_{ordinal}"
                value = metrics[ordinal - 1] if ordinal - 1 < len(metrics) else ""
                settings["Display"][key_name] = value

            self.save(sensor_id, settings)

    # --------------- first-boot seeding ---------------
    def seed_from_factory(
        self,
        sensor_id: str,
        device: str,
        location: str = "Unknown",
        serial_num: str = ""
    ) -> Path:
        """
        Create sensor_settings/<sensor_id>/sensor.toml from factory/sensor.toml if present,
        otherwise write a minimal file. Idempotent: if the file already exists, it is left
        untouched and its path is returned.

        Also ensures [Display].METRIC_1..METRIC_6 defaults based on device type:
          apvpd -> ["Ambient VPD","Temperature","Rel-Humidity","Plant VPD","Plant Temperature","Plant Rel-Humidity"]
          aqi   -> ["Air Quality","Temperature","Rel-Humidity","Ambient VPD","Dewpoint Deficit","dewVPD Risk"]
          avpd  -> ["Ambient VPD","Temperature","Rel-Humidity","Baro-Pressure","Dewpoint Deficit","dewVPD Risk"]
          aht   -> ["Ambient VPD","Temperature","Rel-Humidity","Humidity","Dew Point Deficit","DewVPD Risk"]
          co2   -> ["CO2","Temperature","Rel-Humidity","Ambient VPD","Dewpoint Deficit","dewVPD Risk"]
          lux   -> ["Light Intensity","Auto Light","Estimated PPFD","Visible Light Intensity","",""]
          voc   -> ["VOC Index","NOx Index","","","",""]
          soil  -> ["Soil Moisture","Soil Moisture Deficit","Soil Stress Index","Soil Temp_C","Soil pH","Soil EC"]
          weewx -> ["Temperature_F","Rel-Humidity","Rain","Rain Last 24h","Wind Direction","Baro-Pressure"]
        """
        dst = self._resolve_write_path(sensor_id)  # ensures parent dir
        if dst.exists():
            if DEBUG:
                printDM(f"[seed_from_factory] exists → {dst}", location=MODULE)
            return dst

        # start from factory template if available
        template = (self.base_dir / "factory" / self.STANDARD_FILENAME).resolve()
        seeded_from_template = template.exists()
        if seeded_from_template:
            data = self._parse_toml_from_disk(template)
        else:
            data = OrderedDict()

        host_id = extract_local_host_id_from_sensor_id(sensor_id)
        if not str(serial_num or "").strip() and host_id:
            serial_num = resolve_persisted_host_serial(
                host_id,
                switch_base_dir="switch_settings",
                sensor_base_dir=self.base_dir,
            )

        # ensure required sections/keys
        if "Sensor" not in data or not isinstance(data["Sensor"], dict):
            data["Sensor"] = OrderedDict()
        if is_direct_local_sensor_id(sensor_id):
            data["Sensor"]["TYPE"] = "pi"
        data["Sensor"]["DEVICE"] = device
        data["Sensor"]["SENSOR_ID"] = sensor_id
        data["Sensor"]["LOCATION"] = location
        data["Sensor"]["SERIAL_NUM"] = serial_num

        # Normalize the device key to future-proof variants like "aqi_airco"
        base_device = (device or "").split("_", 1)[0].lower()

        # --- Correct mapping ---
        metric_defaults_by_device: dict[str, list[str]] = {
            "apvpd": ["Ambient VPD", "Temperature", "Rel-Humidity", "Plant VPD", "Plant Temperature", "Plant Rel-Humidity"],
            "aqi":   ["Air Quality", "Temperature", "Rel-Humidity", "Ambient VPD", "Dew Point Deficit", "dewVPD Risk"],
            "avpd":  ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure", "Dew Point Deficit", "dewVPD Risk"],
            "aht":   ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "aht10": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "ahtx0": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "co2":   ["CO2", "Temperature", "Rel-Humidity", "Ambient VPD", "Dew Point Deficit", "dewVPD Risk"],
            "lux":   ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
            "veml":  ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
            "voc":   ["VOC Index", "NOx Index", "", "", "", ""],
            "sgp30": ["Equivalent CO2", "TVOC", "", "", "", ""],
            "sgp40": ["VOC Index", "", "", "", "", ""],
            "sgp41": ["VOC Index", "NOx Index", "", "", "", ""],
            "sgp4x": ["VOC Index", "NOx Index", "", "", "", ""],
            "soil":  ["Soil Moisture", "Soil Moisture Deficit", "Soil Stress Index", "Soil Temp_C", "Soil pH", "Soil EC"],
            "weewx": list(WEEWX_DISPLAY_METRICS),
        }
        metric_fallback: list[str] = ["", "", "", "", "", ""]
        chosen_metrics = metric_defaults_by_device.get(base_device, metric_fallback)

        # Ensure [Display] exists
        if "Display" not in data or not isinstance(data["Display"], dict):
            data["Display"] = OrderedDict()
        display = data["Display"]

        # Fill METRIC_1..6:
        # - If this is first-boot seeding from factory template, FORCE the per-device defaults.
        # - If not from template (i.e., we constructed an empty doc), also set them.
        # (We only avoid overwriting when file already existed, but we returned above in that case.)
        for idx in range(6):
            key = f"METRIC_{idx + 1}"
            display[key] = chosen_metrics[idx]

        style_block = display.get("Style")
        if not isinstance(style_block, dict):
            style_block = OrderedDict()
            display["Style"] = style_block
        for idx in range(6):
            key = f"METRIC_{idx + 1}"
            default_style = WEEWX_DISPLAY_STYLES[idx] if base_device == "weewx" else ""
            style_block.setdefault(key, default_style)

        # Recheck under the save lock: a concurrent discovery/save may have seeded it.
        with self._lock:
            if dst.exists():
                return dst
            self._emit_toml_to_disk(dst, data)
            self.__class__._cache_by_path[str(dst)] = copy.deepcopy(data)
            try:
                self.__class__._mtime_by_path[str(dst)] = os.path.getmtime(dst)
            except Exception:
                self.__class__._mtime_by_path[str(dst)] = None

        if DEBUG:
            printDM(f"[seed_from_factory] seeded → {dst}", location=MODULE)
        return dst

    def ensure_direct_local_type(self, sensor_id: str) -> bool:
        """
        Repair direct Pi bus sensor settings that were accidentally materialized
        as Nodus shadows from stale dashboard/database discovery.
        """
        if not is_direct_local_sensor_id(sensor_id):
            return False

        try:
            data = self.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            return False

        if not isinstance(data, OrderedDict):
            data = OrderedDict(data)

        sensor_block = data.get("Sensor")
        if not isinstance(sensor_block, dict):
            sensor_block = OrderedDict()
            data["Sensor"] = sensor_block

        changed = False
        if str(sensor_block.get("TYPE", "") or "").strip().lower() != "pi":
            sensor_block["TYPE"] = "pi"
            changed = True
        inferred_device = infer_direct_local_device(sensor_id)
        if inferred_device and not str(sensor_block.get("DEVICE", "") or "").strip():
            sensor_block["DEVICE"] = inferred_device
            changed = True
        if str(sensor_block.get("SENSOR_ID", "") or "").strip() != str(sensor_id or "").strip():
            sensor_block["SENSOR_ID"] = str(sensor_id or "").strip()
            changed = True

        if changed:
            self.save(sensor_id, data)
            if DEBUG:
                printDM(f"[ensure_direct_local_type] repaired {sensor_id}", location=MODULE)
        return changed

    def ensure_local_serial_num(self, sensor_id: str) -> bool:
        """
        Backfill [Sensor].SERIAL_NUM for directly connected local sensors.
        Returns True if the file was updated.
        """
        host_id = extract_local_host_id_from_sensor_id(sensor_id)
        if not host_id:
            return False

        doc = self.load(sensor_id) or OrderedDict()
        sensor = doc.get("Sensor", {}) or {}
        if not isinstance(sensor, dict):
            return False

        existing = str(sensor.get("SERIAL_NUM", "") or "").strip()
        if existing:
            return False

        serial_num = resolve_persisted_host_serial(
            host_id,
            switch_base_dir="switch_settings",
            sensor_base_dir=self.base_dir,
        )
        if not serial_num:
            return False

        sensor["SERIAL_NUM"] = serial_num
        doc["Sensor"] = sensor
        self.save(sensor_id, doc)
        return True

    # ---------- internal helpers ---------------
    def _deep_merge(self, base: OrderedDict, update: dict | OrderedDict) -> OrderedDict:
        """
        Recursively merge 'update' into 'base' (both mapping-like),
        preserving existing keys unless explicitly overwritten.
        """
        if update is None:
            return base
        for k, v in (update.items() if isinstance(update, dict) else []):
            if isinstance(v, dict):
                if k not in base or not isinstance(base.get(k), dict):
                    base[k] = OrderedDict()
                # ensure OrderedDict at nested level
                if not isinstance(base[k], OrderedDict):
                    base[k] = self._to_ordered(base[k])  # convert dict->OrderedDict if needed
                self._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    # ---------- path helpers ----------
    def _dir_for(self, sensor_id: str) -> Path:
        safe = sensor_id.strip()
        if not safe:
            raise ValueError("sensor_id cannot be empty")
        if "/" in safe or "\\" in safe:
            raise ValueError(f"Illegal sensor_id: {sensor_id!r}")
        if safe in (".", ".."):
            raise ValueError(f"Illegal sensor_id: {sensor_id!r}")
        return self.base_dir / safe

    def _new_path_for(self, sensor_id: str) -> Path:
        # Preferred new layout: sensor_settings/<sensor_id>/sensor.toml
        return self._dir_for(sensor_id) / self.STANDARD_FILENAME

    def _legacy_path_for(self, sensor_id: str) -> Path:
        # Backward-compat: sensor_settings/<sensor_id>.toml
        safe = sensor_id.strip()
        if not safe:
            raise ValueError("sensor_id cannot be empty")
        if "/" in safe or "\\" in safe:
            raise ValueError(f"Illegal sensor_id: {sensor_id!r}")
        if safe in (".", ".."):
            raise ValueError(f"Illegal sensor_id: {sensor_id!r}")
        return self.base_dir / f"{safe}.toml"

    def _resolve_read_path(self, sensor_id: str) -> Path | None:
        p_new = self._new_path_for(sensor_id)
        if p_new.exists():
            return p_new
        p_old = self._legacy_path_for(sensor_id)
        return p_old if p_old.exists() else None

    def _resolve_write_path(self, sensor_id: str) -> Path:
        # Always write to NEW layout
        p_new = self._new_path_for(sensor_id)
        p_new.parent.mkdir(parents=True, exist_ok=True)
        return p_new
        
    def _split_dotted_key(self, key: str) -> tuple[str | None, str]:
        """
        Accepts 'Section.key' or 'key' and returns (section|None, key).
        """
        if "." in key:
            section, leaf = key.split(".", 1)
            return section, leaf
        return None, key

    def _load_cached_file(self, abs_path: Path, force: bool = False) -> OrderedDict:
        path_key = str(abs_path)
        with self._lock:
            try:
                stat_result = abs_path.stat()
                file_exists = True
                new_mtime = stat_result.st_mtime
            except FileNotFoundError:
                file_exists = False
                new_mtime = None
            cached = self._cache_by_path.get(path_key)
            cached_mtime = self._mtime_by_path.get(path_key)

            needs_refresh = (
                force
                or (cached is None)
                or (cached_mtime != new_mtime)
                or (not file_exists)  # <-- ensure we don't serve stale after deletion
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


    def _to_ordered(self, data: dict | OrderedDict) -> OrderedDict:
        """Recursively convert dicts to OrderedDict for stable writes."""
        if isinstance(data, dict) and not isinstance(data, OrderedDict):
            ordered = OrderedDict()
            for k in data:
                ordered[k] = self._to_ordered(data[k])
            return ordered
        if isinstance(data, list):
            return [self._to_ordered(x) for x in data]  # type: ignore[return-value]
        return data


    # ---------- parsing & emitting helpers ----------
    def _parse_toml_from_disk(self, abs_path: Path) -> OrderedDict:
        """
        Prefer tomllib if available; fall back to a minimal parser that handles:
          - Sections: [Section]
          - Scalars: strings, ints, floats, booleans
          - Lists: numbers/strings
        """
        if tomllib:
            try:
                with abs_path.open("rb") as f:
                    data = tomllib.load(f)  # raises on malformed TOML
                return self._to_ordered(data)
            except Exception as exc:
                # In production, malformed TOML should fail fast rather than being "best-effort" parsed.
                raise ValueError(f"Invalid TOML in {abs_path}: {exc}") from exc

        # Fallback: simple tolerant parser (order-preserving)
        result = OrderedDict()
        try:
            with abs_path.open("r", encoding="utf-8") as f:
                current_section = None
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        current_section = line[1:-1]
                        result[current_section] = OrderedDict()
                        continue
                    if "=" not in line:
                        continue
                    key, raw_val = map(str.strip, line.split("=", 1))
                    parsed_val = self._parse_scalar_or_list(raw_val)
                    target = result if current_section is None else result[current_section]
                    target[key] = parsed_val
        except Exception as exc:
            printDM(f"Fallback parse error for {abs_path}: {exc}", location=MODULE)
        return result

    def _parse_scalar_or_list(self, raw_val: str):
        """Parse strings, bools, numbers, and simple lists of those types."""
        # Normalize booleans (barewords)
        if raw_val.lower() == "true":
            return True
        if raw_val.lower() == "false":
            return False

        # Quoted string
        if (raw_val.startswith('"') and raw_val.endswith('"')) or (raw_val.startswith("'") and raw_val.endswith("'")):
            return raw_val[1:-1]

        # List like [1, 2, "x"]
        if raw_val.startswith("[") and raw_val.endswith("]"):
            inner = raw_val[1:-1].strip()
            if not inner:
                return []
            parts = []
            buf = ""
            in_str = False
            quote_char = ""
            for ch in inner:
                if in_str:
                    buf += ch
                    if ch == quote_char:
                        in_str = False
                else:
                    if ch in ("'", '"'):
                        in_str = True
                        quote_char = ch
                        buf += ch
                    elif ch == ",":
                        item = buf.strip()
                        if item:
                            parts.append(self._parse_scalar_or_list(item))
                        buf = ""
                    else:
                        buf += ch
            if buf.strip():
                parts.append(self._parse_scalar_or_list(buf.strip()))
            return parts

        # Numbers (int/float)
        try:
            if "." in raw_val:
                return float(raw_val)
            return int(raw_val)
        except Exception:
            # leave bareword as-is
            return raw_val

    def _emit_toml_to_disk(self, abs_path: Path, data: dict | OrderedDict):
        """
        Minimal TOML emitter supporting:
          - Flat dicts
          - {section: {key: val}}  (single-level sections)
          - Nested sections like [Calibration.System] via nested dicts:
                { "Calibration": {
                      "CALIBRATED": false,
                      "System": { "TEMP_OFFSET": 0.0, ... },
                      "Device": { ... },
                  }}
        Uses a temp file then atomic replace.
        """
        lines: list[str] = []

        def emit_kv(key: str, value):
            if isinstance(value, list):
                encoded = []
                for item in value:
                    if isinstance(item, str):
                        encoded.append(json.dumps(item))
                    elif isinstance(item, bool):
                        encoded.append("true" if item else "false")
                    else:
                        encoded.append(f"{item}")
                lines.append(f"{key} = [{', '.join(encoded)}]")
            elif isinstance(value, str):
                lines.append(f"{key} = {json.dumps(value)}")
            elif isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                lines.append(f"{key} = {value}")

        def emit_section(section_name: str, kv_pairs: dict):
            """
            Emit a section and any nested subsections.
            Scalars (non-dict values) are written in [section_name].
            Any nested dicts become [section_name.Sub] subsections.
            """
            # First, scalars in this section
            lines.append(f"[{section_name}]")
            for k, v in kv_pairs.items():
                if isinstance(v, dict):
                    continue
                emit_kv(k, v)
            lines.append("")  # blank line after section

            # Then nested subsections, if any
            for k, v in kv_pairs.items():
                if not isinstance(v, dict):
                    continue
                nested_name = f"{section_name}.{k}"
                emit_section(nested_name, v)

        # Decide whether we are in "sectioned" mode
        is_sectioned = any(isinstance(v, dict) for v in data.values())

        if is_sectioned:
            # Top-level keys that are dicts become sections.
            # Any scalar top-level keys are emitted as simple k=v pairs at the top.
            scalar_top: dict = {}
            section_top: dict[str, dict] = {}

            for k, v in data.items():
                if isinstance(v, dict):
                    section_top[k] = v
                else:
                    scalar_top[k] = v

            # Emit any top-level scalars (rare in your usage, but safe)
            for k, v in scalar_top.items():
                emit_kv(k, v)
            if scalar_top:
                lines.append("")

            # Emit each section (with nested subsections)
            for section_name, kv_pairs in section_top.items():
                emit_section(section_name, kv_pairs)
        else:
            # Legacy: flat file, no sections
            for k, v in data.items():
                emit_kv(k, v)
            lines.append("")

        tmp_path = abs_path.with_suffix(".toml.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            tmp_path.replace(abs_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
