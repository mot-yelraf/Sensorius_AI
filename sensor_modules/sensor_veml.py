# sensor_modules/sensor_veml7700.py
"""
sensor_veml7700.py
Enhanced VEML7700 ambient light sensor with:
- Lux and AutoLux (auto gain/integration when available)
- Optional non-linear correction (soft knee at high lux)
- PPFD (µmol/m²/s) from lux with configurable spectrum factor
- DLI (mol/m²/day) computed since local midnight with O(1) memory
"""
import time
from saiUtils import printDM, debug_enabled, get_timestamp
from sensor_modules.base import BaseSensor

MODULE = "VEML7700Sensor"
DEBUG = debug_enabled("saiSensorFactory")

class VEML7700Sensor(BaseSensor):
    # ---------- constants & defaults ----------
    # Practical sensor span with proper gain/IT selection
    LUX_MIN_SPEC   = 0.003   # ~lowest practical resolution point
    LUX_MAX_SPEC   = 120000  # ~upper span with low gain / short IT

    # Autolux “comfort” window we try to keep readings within (for manual fallback)
    AUTO_TARGET_MIN = 5.0
    AUTO_TARGET_MAX = 20000.0

    # Default Lux→PPFD conversion (daylight/sunlight ~54; white LED often ~60–70)
    DEFAULT_PPFD_LUX_FACTOR = 54.0  # µmol·m⁻²·s⁻¹ per lux

    def __init__(self, settings, supervisor, i2c_0=None):
        super().__init__(settings, supervisor)
        import board  # noqa: F401  (kept for future pin overrides)
        import busio  # noqa: F401
        import adafruit_veml7700

        # ---- calibration offsets (lux, µmol/m²/s) ----
        # Effective offsets = Device + Manual + System
        self.lux_offset: float = 0.0
        self.ppfd_offset: float = 0.0
        self._load_calibration_offsets(settings)
        # ----------------------------------------------

        # Optional device-level config (safe to omit)
        # NOTE: assumes you already have 's' from settings.get_section("Sensor")
        s = {}
        try:
            if hasattr(self.settings, "get_section"):
                s = self.settings.get_section("Sensor") or {}
        except Exception:
            s = {}

        i2c_addr = int(s.get("I2C_ADDR", 0x10) or 0x10)  # VEML7700 default 0x10
        cfg_gain = (s.get("VEML7700_GAIN") or "").strip().lower()    # "x1","x2","x1/4","x1/8"
        cfg_itms = s.get("VEML7700_IT_MS")                           # 25,50,100,200,400,800
        # Autolux toggles (used if driver supports)
        cfg_auto_gain = str(s.get("VEML7700_AUTO_GAIN", "true")).strip().lower() in ("1","true","yes","on")
        cfg_auto_it   = str(s.get("VEML7700_AUTO_IT",   "true")).strip().lower() in ("1","true","yes","on")
        # Optional non-linear correction
        cfg_nl_enable = str(s.get("VEML7700_NONLINEAR", "true")).strip().lower() in ("1","true","yes","on")
        # Lux->PPFD factor override (float)
        try:
            cfg_ppfd_factor = float(s.get("VEML7700_PPFD_LUXFACTOR", self.DEFAULT_PPFD_LUX_FACTOR))
        except Exception:
            cfg_ppfd_factor = self.DEFAULT_PPFD_LUX_FACTOR

        try:
            # Match co2 template style: use helper to locate the bus by known address
            self.i2c = self._find_sensor_bus(address=i2c_addr)
            if not self.i2c:
                raise RuntimeError(f"VEML7700 not found on any available I2C bus (addr=0x{i2c_addr:02X})")

            self.veml = adafruit_veml7700.VEML7700(self.i2c, address=i2c_addr)

            # Try to enable driver-provided auto modes if present
            self._has_autolux_attr = hasattr(self.veml, "autolux")
            self._has_auto_gain    = hasattr(self.veml, "auto_gain")
            self._has_auto_it      = hasattr(self.veml, "auto_integration_time")

            # Apply explicit manual settings (if provided)
            try:
                if cfg_gain:
                    self.veml.gain = cfg_gain            # "x1","x2","x1/4","x1/8"
            except Exception as _e:
                if DEBUG:
                    printDM(f"Ignoring VEML7700_GAIN='{cfg_gain}': {_e}", location="VEML7700Sensor")

            try:
                if cfg_itms:
                    self.veml.integration_time = int(cfg_itms)  # ms
            except Exception as _e:
                if DEBUG:
                    printDM(f"Ignoring VEML7700_IT_MS='{cfg_itms}': {_e}", location="VEML7700Sensor")

            # Turn on auto modes when supported (unless user forced manual)
            try:
                if self._has_auto_gain:
                    self.veml.auto_gain = bool(cfg_auto_gain)
                if self._has_auto_it:
                    self.veml.auto_integration_time = bool(cfg_auto_it)
            except Exception as _e:
                if DEBUG:
                    printDM(f"Ignoring auto settings: {_e}", location="VEML7700Sensor")

            self.present = True

            # --- runtime flags / config ---
            self._apply_nonlinear = bool(cfg_nl_enable)
            self._ppfd_factor     = float(cfg_ppfd_factor)

            # --- snapshot state (single read per cycle) ---
            self._snapshot_ts   = None
            self._snapshot_lux  = None        # corrected lux (pre-calibration)
            self._snapshot_raw  = None        # raw lux from sensor
            self._snapshot_alux = None        # autolux if driver exposes it
            self._snapshot_valid = False

            # --- DLI running state (O(1) memory) ---
            self._dli_umol_accum = 0.0        # micromoles·m⁻² accumulated since local midnight
            self._dli_day_key    = self._day_key(time.localtime())
            self._last_sample_t  = None       # monotonic seconds of last update

            # Measurements (all getters safe for None)
            # Note: names are stable to feed downstream gauge/DB code
            self.measurements = [
                ("Light Intensity", "lux",          lambda: self._safe_lux(corrected=True),  1),
                ("Auto Light",      "lux",          lambda: self._safe_autolux(),             1),
                ("PPFD",            "µmol/m²/s",    lambda: self._safe_ppfd(),                0),
                ("DLI",             "mol/m²/day",   lambda: self._safe_dli(),                 2),
            ]

        except Exception as e:
            self.present = False
            printDM(f"Sensor init failed: {e}", location="VEML7700Sensor")
            return

        # BaseSensor book-keeping
        self.meas_types    = [name for name, *_ in self.measurements]
        self.unit_map      = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw    = {name: None for name in self.meas_types}
        self.current_values= {name: None for name in self.meas_types}

    # ------------------------------------------------------------------
    # Calibration loading
    # ------------------------------------------------------------------
    def _load_calibration_offsets(self, settings) -> None:
        """
        Load lux and PPFD offsets from sensor settings.

        Expected TOML shape (VEML7700 devices):

        [Calibration]
        CALIBRATED   = false
        CALIB_STATUS = "Not Calibrated"

        [Calibration.Device]
        LUX_OFFSET  = 0.0
        PPFD_OFFSET = 0.0

        [Calibration.Manual]
        LUX_OFFSET  = 0.0
        PPFD_OFFSET = 0.0

        [Calibration.System]
        LUX_OFFSET  = 0.0
        PPFD_OFFSET = 0.0
        """
        # ---- resolve underlying config dict from SettingsWrapper/dict ----
        try:
            # Sensorius: 'settings' is usually a SettingsWrapper
            if hasattr(settings, "get"):
                # SettingsWrapper: settings.get("Calibration") → dict or None
                root = settings.get("Calibration", {}) or {}
            elif isinstance(settings, dict):
                root = settings.get("Calibration", {}) or {}
            else:
                root = {}
        except Exception:
            root = {}

        cal_root = root or {}

        device_cal = cal_root.get("Device", {}) or {}
        manual_cal = cal_root.get("Manual", {}) or {}
        system_cal = cal_root.get("System", {}) or {}

        def _safe_float(block, key):
            try:
                return float(block.get(key, 0.0))
            except Exception:
                return 0.0

        # Device offsets
        device_lux  = _safe_float(device_cal, "LUX_OFFSET")
        device_ppfd = _safe_float(device_cal, "PPFD_OFFSET")

        # Manual offsets
        manual_lux  = _safe_float(manual_cal, "LUX_OFFSET")
        manual_ppfd = _safe_float(manual_cal, "PPFD_OFFSET")

        # System offsets
        system_lux  = _safe_float(system_cal, "LUX_OFFSET")
        system_ppfd = _safe_float(system_cal, "PPFD_OFFSET")

        self.lux_offset  = device_lux  + manual_lux  + system_lux
        self.ppfd_offset = device_ppfd + manual_ppfd + system_ppfd

        calib_status = cal_root.get("CALIB_STATUS", "Not Calibrated")
        self.is_calibrated = calib_status

        if DEBUG:
            printDM(
                f"VEML7700 calibration loaded: "
                f"lux_offset={self.lux_offset:.3f}, "
                f"ppfd_offset={self.ppfd_offset:.3f}, "
                f"status='{self.is_calibrated}'",
                location=MODULE,
            )

    def reload_calibration_from_settings(self, settings) -> None:
        """
        Public hook for live calibration reload.

        Called by SensorController (Sensorius) or by the Nodus firmware
        after /update-calibration-values updates settings.toml.

        'settings' should be the same style object passed at __init__:
          - On Sensorius: SettingsWrapper around the per-sensor OrderedDict
          - On Nodus: whatever wrapper/dict you use there
        """
        try:
            self._load_calibration_offsets(settings)
            if DEBUG:
                printDM(
                    (
                        "VEML7700 calibration reloaded: "
                        f"lux_offset={self.lux_offset:.3f}, "
                        f"ppfd_offset={self.ppfd_offset:.3f}, "
                        f"status='{self.is_calibrated}'",
                    ),
                    location=MODULE,
                )      

        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)

    # ---------- per-cycle snapshot ----------
    def prepare_measurement_cycle(self):
        """Refresh a single consistent sensor snapshot for this publish cycle."""
        self._snapshot_valid = False
        self._refresh_snapshot()

    def _refresh_snapshot(self):
        try:
            # Prefer autolux if the driver gives it to us (it already manages gain/IT)
            autolux = None
            if self._has_autolux_attr:
                try:
                    autolux = float(self.veml.autolux)  # may raise or be None on some drivers
                except Exception:
                    autolux = None

            raw_lux = None
            try:
                # Standard property on the driver
                raw_lux = float(self.veml.lux)
            except Exception:
                pass

            # Manual “autolux” fallback: gently adjust gain/IT to keep in target window
            if autolux is None and raw_lux is not None and not (self._has_auto_gain or self._has_auto_it):
                self._manual_autorange(raw_lux)
                # reread after small settle
                time.sleep(0.005)
                try:
                    autolux = float(self.veml.lux)
                except Exception:
                    autolux = raw_lux

            # Pick best candidates
            best_lux = autolux if autolux is not None else raw_lux
            if best_lux is None:
                self._snapshot_valid = False
                return False

            self._snapshot_raw  = float(best_lux)
            corrected = (
                self._apply_nonlinear_correction(self._snapshot_raw)
                if self._apply_nonlinear else
                self._snapshot_raw
            )
            # NOTE: this is *pre-calibration* corrected lux
            self._snapshot_lux  = max(self.LUX_MIN_SPEC, min(corrected, self.LUX_MAX_SPEC))
            self._snapshot_alux = autolux if autolux is not None else None

            # Update DLI accumulator with this sample’s PPFD and Δt (pre-calibration)
            now_mono = time.monotonic()
            self._rollover_dli_if_new_day()
            if self._last_sample_t is not None:
                dt = max(0.0, now_mono - self._last_sample_t)
            else:
                dt = 0.0
            self._last_sample_t = now_mono

            sample_ppfd = self._ppfd_from_lux(self._snapshot_lux)
            # micromoles accumulation
            self._dli_umol_accum += (sample_ppfd * dt)

            self._snapshot_ts    = get_timestamp()
            self._snapshot_valid = True
            return True
        except Exception as e:
            if DEBUG:
                printDM(f"_refresh_snapshot failed: {e}", location="VEML7700Sensor")
            self._snapshot_valid = False
            return False

    def _ensure_snapshot(self):
        if not self._snapshot_valid:
            self._refresh_snapshot()
        return self._snapshot_valid
    
    # ---------- helpers: day rollover & non-linear corr ----------
    def _day_key(self, tm):
        # tuple (year, yday) – robust to DST shifts
        return (tm.tm_year, tm.tm_yday)

    def _rollover_dli_if_new_day(self):
        try:
            tm = time.localtime()
            key = self._day_key(tm)
            if key != self._dli_day_key:
                self._dli_day_key    = key
                self._dli_umol_accum = 0.0
                # keep last_sample_t (continuity of Δt calc)
        except Exception:
            # If RTC not ready, keep accumulating; safe fallback
            pass

    def _apply_nonlinear_correction(self, lux: float) -> float:
        """
        Soft-knee correction that very gently compresses high-lux readings
        to compensate mild sensor non-linearity.
        Formula: L' = L * (1 - a*log10(1 + L/L0))
        with small a; no change in low-lux region, ~5–10% at high sun.
        """
        if lux <= 0:
            return 0.0
        a  = 0.020  # knee aggressiveness
        L0 = 1000.0
        try:
            import math
            factor = 1.0 - a * math.log10(1.0 + (lux / L0))
            # Clamp factor to [0.85, 1.0] to remain conservative
            if factor < 0.85:
                factor = 0.85
            elif factor > 1.0:
                factor = 1.0
            return lux * factor
        except Exception:
            return lux

    def _manual_autorange(self, lux_now: float):
        """
        Minimal manual auto-range: if too high → reduce gain/IT; too low → increase.
        Only used when driver lacks auto_* features and user didn't force manual.
        """
        try:
            # Supported values in Adafruit driver
            gains = ["x1/8", "x1/4", "x1", "x2"]
            it_ms = [25, 50, 100, 200, 400, 800]

            # Read current
            cur_gain = getattr(self.veml, "gain", "x1")
            cur_it   = getattr(self.veml, "integration_time", 100)
            gi = gains.index(cur_gain) if cur_gain in gains else 2
            ii = it_ms.index(cur_it)   if cur_it   in it_ms   else 2

            if lux_now > self.AUTO_TARGET_MAX:
                # decrease sensitivity → shorter IT first, then lower gain
                if ii > 0:
                    self.veml.integration_time = it_ms[ii - 1]
                elif gi > 0:
                    self.veml.gain = gains[gi - 1]
            elif lux_now < self.AUTO_TARGET_MIN:
                # increase sensitivity → higher gain first, then longer IT
                if gi < len(gains) - 1:
                    self.veml.gain = gains[gi + 1]
                elif ii < len(it_ms) - 1:
                    self.veml.integration_time = it_ms[ii + 1]
        except Exception:
            pass

    # ---------- conversions ----------
    def _ppfd_from_lux(self, lux: float | None) -> float | None:
        if lux is None:
            return None
        try:
            # µmol·m⁻²·s⁻¹
            return float(lux) / self._ppfd_factor
        except Exception:
            return None

    # ---------- safe getters for measurement table ----------
    def _safe_lux(self, corrected=True):
        """
        Return calibrated Light Intensity (lux) for measurement table.

        - Uses corrected snapshot lux (with non-linear compression) as "raw"
        - Applies LUX_OFFSET only to the corrected path (the one used by metrics)
        """
        if not self._ensure_snapshot():
            return None

        # base_raw = corrected snapshot (pre-calibration)
        base_raw = self._snapshot_lux if corrected else self._snapshot_raw
        if base_raw is None:
            return None

        if corrected:
            lux = base_raw + self.lux_offset
            lux = max(self.LUX_MIN_SPEC, min(lux, self.LUX_MAX_SPEC))
        else:
            lux = base_raw

        # Update tracking maps only for the corrected metric path
        self.latest_raw["Light Intensity"] = base_raw
        self.current_values["Light Intensity"] = lux
        return lux

    def _safe_autolux(self):
        if not self._ensure_snapshot():
            return None
        # If driver exposed autolux, return it; else fall back to corrected lux
        # NOTE: we intentionally do *not* apply LUX_OFFSET to Auto Light.
        return self._snapshot_alux if (self._snapshot_alux is not None) else self._snapshot_lux

    def _safe_ppfd(self):
        """
        Return calibrated PPFD, derived from calibrated lux plus PPFD_OFFSET.
        """
        if not self._ensure_snapshot():
            return None

        # Derive PPFD from *calibrated* lux
        lux_cal = self._safe_lux(corrected=True)
        if lux_cal is None:
            return None

        base_ppfd = self._ppfd_from_lux(lux_cal)
        if base_ppfd is None:
            return None

        ppfd = base_ppfd + self.ppfd_offset
        if ppfd < 0.0:
            ppfd = 0.0

        self.latest_raw["PPFD"] = base_ppfd
        self.current_values["PPFD"] = ppfd
        return ppfd

    def _safe_dli(self):
        # DLI in mol·m⁻²·day⁻¹ (since local midnight)
        # Uses *pre-calibration* accumulation for stability
        return round(self._dli_umol_accum / 1_000_000.0, 3)

    # ---------- capability flags ----------
    def supports_calibration(self):
        # We now expose device offsets for Light Intensity + PPFD
        return True
