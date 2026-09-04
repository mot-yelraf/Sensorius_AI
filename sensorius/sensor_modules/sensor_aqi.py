"""BME680-based air-quality sensor backend for Sensorius.

This module exposes calibrated air-quality, gas-resistance, temperature,
humidity, dew point, pressure, and ambient VPD readings from a BME680-class
sensor.
"""

import math
from ..saiUtils import printDM, debug_enabled
from .base import BAROMETRIC_PRESSURE_PRECISION, BaseSensor, find_sensor_bus

MODULE = "AQISensor"
DEBUG = debug_enabled("saiSensorFactory")


class AQISensor(BaseSensor):
    """Read BME680 environmental measurements and derived air quality."""

    def __init__(self, settings, supervisor, i2c_0=None):
        super().__init__(settings, supervisor)
        from adafruit_bme680 import Adafruit_BME680_I2C

        # ---- calibration offsets (°C, %RH, AQI units, gas Ohms) ----
        # Effective offsets = Device + Manual + System
        self.temp_offset_c: float = 0.0
        self.rh_offset_pct: float = 0.0
        self.aqi_offset: float = 0.0
        self.gas_offset_ohms: float = 0.0
        self.altitude_meters = None
        self._load_calibration_offsets(settings)
        # ------------------------------------------------------------

        try:
            self.i2c = self._find_sensor_bus(address={0x77, 0x76})
            if not self.i2c:
                raise RuntimeError("BME680 not found on any available I2C bus")

            while not self.i2c.try_lock():
                pass
            try:
                addrs = set(self.i2c.scan() or [])
            finally:
                self.i2c.unlock()
            addr = 0x77 if 0x77 in addrs else 0x76

            self.bme680 = Adafruit_BME680_I2C(self.i2c, address=addr)
            self.bme680.sea_level_pressure = 1013.25
            self.bme680.set_gas_heater(320, 150)
            self.present = True

            # NOTE: all metrics below use *calibrated* values via helpers.
            self.measurements = [
                ("Air Quality",   "AQI",  lambda: self._get_calibrated_aqi(), 1),
                ("Gas",           "Ω",    lambda: self._get_calibrated_gas(), None),
                ("Temperature",   "°C",   lambda: self._get_calibrated_temp_c(), 2),
                ("Rel-Humidity",  "%",    lambda: self._get_calibrated_rh(), 1),
                ("Humidity",      "g/m³", lambda: self._get_calibrated_abs_humidity(), 1),
                ("Dew Point",     "°C",   lambda: self._get_calibrated_dewpoint_c(), 2),
                ("Dew Point Deficit", "°C", lambda: self._get_calibrated_dewpoint_depression(), 2),
                ("DewVPD Risk",   "%",    lambda: self._get_calibrated_dewvpd_risk(), 1),
                ("Ambient VPD",   "kPa",  lambda: self._get_calibrated_vpd(), 3),
                ("Baro-Pressure", "hPa",
                 lambda: self._altitude_adjusted_pressure_hpa(self.bme680.pressure),
                 BAROMETRIC_PRESSURE_PRECISION),
            ]
        except Exception as e:
            self.present = False
            printDM(f"AQI sensor init failed: {e}", location=MODULE)
            return

        self.meas_types     = [n for n, *_ in self.measurements]
        self.unit_map       = {n: u for n, u, *_ in self.measurements}
        self.filtered_data  = {n: None for n in self.meas_types}
        self.latest_raw     = {n: None for n in self.meas_types}
        self.current_values = {n: None for n in self.meas_types}

    # ------------------------------------------------------------------
    # Calibration loading
    # ------------------------------------------------------------------
    def _load_calibration_offsets(self, settings) -> None:
        """
        Load temperature, humidity, AQI and gas offsets from the sensor settings.

        Expected TOML shape (AQI devices):

        [Calibration]
        CALIBRATED   = false
        CALIB_STATUS = "Not Calibrated"

        [Calibration.Device]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        AQI_OFFSET  = 0.0
        GAS_OFFSET  = 0.0    # not exposed in UI (for now)
        ALTITUDE_METERS = 0.0

        [Calibration.Manual]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        AQI_OFFSET  = 0.0
        GAS_OFFSET  = 0.0

        [Calibration.System]
        TEMP_OFFSET     = 0.0
        RH_OFFSET       = 0.0
        AQI_OFFSET      = 0.0
        GAS_OFFSET      = 0.0  # optional; system cal currently focuses on T/RH
        REF_SENSOR_ID   = ""
        REF_RANGE_HOURS = 24
        REF_START_TS    = 0
        REF_END_TS      = 0
        REF_NOTE        = ""
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

        # Device offsets (from Device Calibration modal)
        device_temp = _safe_float(device_cal, "TEMP_OFFSET")
        device_rh   = _safe_float(device_cal, "RH_OFFSET")
        device_aqi  = _safe_float(device_cal, "AQI_OFFSET")
        device_gas  = _safe_float(device_cal, "GAS_OFFSET")

        # Manual offsets (from Manual Calibration, if ever exposed)
        manual_temp = _safe_float(manual_cal, "TEMP_OFFSET")
        manual_rh   = _safe_float(manual_cal, "RH_OFFSET")
        manual_aqi  = _safe_float(manual_cal, "AQI_OFFSET")
        manual_gas  = _safe_float(manual_cal, "GAS_OFFSET")

        # System offsets (from System Calibration preview/apply)
        system_temp = _safe_float(system_cal, "TEMP_OFFSET")
        system_rh   = _safe_float(system_cal, "RH_OFFSET")
        system_aqi  = _safe_float(system_cal, "AQI_OFFSET")
        system_gas  = _safe_float(system_cal, "GAS_OFFSET")

        self.temp_offset_c    = device_temp + manual_temp + system_temp
        self.rh_offset_pct    = device_rh   + manual_rh   + system_rh
        self.aqi_offset       = device_aqi  + manual_aqi  + system_aqi
        self.gas_offset_ohms  = device_gas  + manual_gas  + system_gas
        self._load_device_altitude_meters(settings)

        calib_status = cal_root.get("CALIB_STATUS", "Not Calibrated")

        printDM(
            f"AQI calibration loaded: "
            f"temp_offset_c={self.temp_offset_c:.3f}, "
            f"rh_offset_pct={self.rh_offset_pct:.3f}, "
            f"aqi_offset={self.aqi_offset:.3f}, "
            f"gas_offset_ohms={self.gas_offset_ohms:.1f}, "
            f"altitude_meters={self.altitude_meters}, "
            f"status='{calib_status}'",
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
                        "AQI calibration reloaded: "
                        f"temp_offset_c={self.temp_offset_c:.3f}, "
                        f"rh_offset_pct={self.rh_offset_pct:.3f}, "
                        f"aqi_offset={self.aqi_offset:.3f}, "
                        f"gas_offset_ohms={self.gas_offset_ohms:.1f}, "
                        f"altitude_meters={self.altitude_meters}, "
                    ),
                    location=MODULE,
                )      

        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)

    # ------------------------------------------------------------------
    # Raw helpers
    # ------------------------------------------------------------------
    def _get_raw_temp_c(self) -> float:
        return self.bme680.temperature

    def _get_raw_rh(self) -> float:
        # Clamp raw RH to sane range before applying offset
        return self._clamp_if_number(self.bme680.relative_humidity, 0.0, 100.0)

    def _get_raw_gas_ohms(self) -> float:
        # BME680 gas resistance; clamp to a realistic range
        return self._clamp_if_number(self.bme680.gas, 500, 2_000_000)

    # ------------------------------------------------------------------
    # Calibrated metric helpers
    # ------------------------------------------------------------------
    def _get_calibrated_temp_c(self) -> float:
        raw_temp = self._get_raw_temp_c()
        if raw_temp is None:
            self.latest_raw["Temperature"] = None
            self.current_values["Temperature"] = None
            return None
        temp_c = raw_temp + self.temp_offset_c
        self.latest_raw["Temperature"] = raw_temp
        self.current_values["Temperature"] = temp_c
        return temp_c

    def _get_calibrated_rh(self) -> float:
        raw_rh = self._get_raw_rh()
        if raw_rh is None:
            self.latest_raw["Rel-Humidity"] = None
            self.current_values["Rel-Humidity"] = None
            return None
        rh = raw_rh + self.rh_offset_pct
        rh = self._clamp_if_number(rh, 0.0, 100.0)
        self.latest_raw["Rel-Humidity"] = raw_rh
        self.current_values["Rel-Humidity"] = rh
        return rh

    def _get_calibrated_gas(self) -> float:
        raw_gas = self._get_raw_gas_ohms()
        if raw_gas is None:
            self.latest_raw["Gas"] = None
            self.current_values["Gas"] = None
            return None
        gas = raw_gas + self.gas_offset_ohms
        gas = self._clamp_if_number(gas, 500, 2_000_000)
        self.latest_raw["Gas"] = raw_gas
        self.current_values["Gas"] = gas
        return gas

    def _get_calibrated_abs_humidity(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            self.current_values["Humidity"] = None
            return None
        abs_h = self.calculate_absolute_humidity(temp_c, rh)
        self.current_values["Humidity"] = abs_h
        return abs_h

    def _get_calibrated_vpd(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            self.current_values["Ambient VPD"] = None
            return None
        vpd = self.calculate_vpd(temp_c, rh)
        vpd_clamped = self._clamp_if_number(vpd, 0.0, 5.0)
        self.current_values["Ambient VPD"] = vpd_clamped
        return vpd_clamped

    def _get_calibrated_dewpoint_c(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            return None
        return self.calculate_dewpoint(temp_c, rh)

    def _get_calibrated_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            return None
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        return depression

    def _get_calibrated_dewvpd_risk(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            self.current_values["DewVPD Risk"] = None
            return None
        vpd = self._get_calibrated_vpd()
        if vpd is None:
            self.current_values["DewVPD Risk"] = None
            return None
        risk = self.calculate_dewvpd_risk(temp_c, rh, vpd=vpd)
        risk = self._clamp_if_number(risk, 0.0, 100.0)
        self.current_values["DewVPD Risk"] = risk
        return risk

    def _get_calibrated_aqi(self) -> float:
        """
        Use calibrated gas and RH to estimate AQI, then apply AQI_OFFSET.
        """
        gas = self._get_calibrated_gas()
        rh = self._get_calibrated_rh()
        if gas is None:
            self.latest_raw["Air Quality"] = None
            self.current_values["Air Quality"] = None
            return None
        raw_aqi = self.estimate_aqi(gas, rh_percent=rh)
        if raw_aqi is None:
            self.latest_raw["Air Quality"] = None
            self.current_values["Air Quality"] = None
            return None
        aqi = raw_aqi + self.aqi_offset
        aqi = self._clamp_if_number(aqi, 0.0, 500.0)
        self.latest_raw["Air Quality"] = raw_aqi
        self.current_values["Air Quality"] = aqi
        return aqi

    # ------------------------------------------------------------------
    # AQI estimation (unchanged, but now fed calibrated inputs)
    # ------------------------------------------------------------------
    def estimate_aqi(self, gas_ohms, rh_percent=None):
        """
        Estimate AQI using negative logarithmic mapping from gas resistance,
        with optional RH compensation.

        - gas_ohms: gas resistance in Ohms
        - rh_percent: relative humidity in %, optional
        """
        poor_threshold = 5100       # Low resistance → bad air (AQI ~ 500)
        great_threshold = 995100    # High resistance → clean air (AQI ~ 0)

        if gas_ohms is None or gas_ohms < poor_threshold:
            return 500  # Invalid or extremely poor

        # Apply RH-based compensation (±10%)
        if rh_percent is not None:
            rh_baseline = 45.0
            rh_delta = rh_percent - rh_baseline
            rh_scale = max(-10, min(10, rh_delta * 0.25))  # compensation limited to [-10%, +10%]
            gas_ohms *= 1 + (rh_scale / 100)

        # Clamp after RH adjustment
        gas_ohms = max(poor_threshold, min(gas_ohms, great_threshold))

        # Compute scaled AQI using normalized -log function
        log_base = math.e
        log_val = -math.log(gas_ohms, log_base)
        log_min = -math.log(great_threshold, log_base)
        log_max = -math.log(poor_threshold, log_base)
        scaled = (log_val - log_min) / (log_max - log_min) * 500

        if DEBUG:
            try:
                rh_dbg = float(rh_percent) if rh_percent is not None else None
                rh_text = f"{rh_dbg:.1f}%" if rh_dbg is not None else "N/A"
                printDM(
                    f"[DEBUG] Gas={gas_ohms:.0f}Ω, RH={rh_text} → AQI={scaled:.1f}",
                    location="AQISensor",
                )
            except Exception:
                printDM(
                    f"[DEBUG] Gas={gas_ohms}Ω, RH={rh_percent} → AQI={scaled}",
                    location="AQISensor",
                )

        return round(min(500, max(0, scaled)))

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------
    def supports_calibration(self):
        # AQI + T/RH offsets are now wired
        return True
