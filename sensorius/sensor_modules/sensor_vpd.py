"""Ambient BME280-based temperature, humidity, and VPD sensor backend.

This module wraps a single BME280-family device and exposes calibrated ambient
metrics such as temperature, relative humidity, dew point, barometric pressure,
and vapor-pressure deficit for Sensorius.
"""

from ..saiUtils import printDM, debug_enabled
from .base import BAROMETRIC_PRESSURE_PRECISION, BaseSensor, find_sensor_bus

MODULE = "VPDSensor"
DEBUG = debug_enabled("saiSensorFactory")

class VPDSensor(BaseSensor):
    def __init__(self, settings, supervisor, i2c_0=None):
        super().__init__(settings, supervisor)
        from adafruit_bme280.advanced import Adafruit_BME280_I2C, IIR_FILTER_X4

        # ---- calibration offsets (°C and %RH) ----
        # These are the *effective* offsets = Device + Manual + System
        self.temp_offset_c = 0.0
        self.rh_offset_pct = 0.0
        self.altitude_meters = None
        self._load_calibration_offsets(settings)
        # -----------------------------------------

        try:
            self.i2c = self._find_sensor_bus(address=0x76)
            if not self.i2c:
                raise RuntimeError("BME280 not found on any available I2C bus")

            self.thp280 = Adafruit_BME280_I2C(self.i2c, address=0x76)
            self.present = True

            # NOTE: all metrics below use *calibrated* temp/RH via helpers.
            self.measurements = [
                ("Temperature",      "°C",  lambda: self._get_calibrated_temp_c(), 2),
                ("Temperature_F",    "°F",  lambda: self._get_calibrated_temp_f(), 1),
                ("Rel-Humidity",     "%",   lambda: self._get_calibrated_rh(), 2),
                ("Humidity",         "g/m³", lambda: self._get_calibrated_abs_humidity(), 1),
                ("Dew Point",        "°C",  lambda: self._get_calibrated_dewpoint_c(), 2),
                ("Dew Point_F",      "°F",  lambda: self._get_calibrated_dewpoint_f(), 1),
                ("Dew Point Deficit", "°C", lambda: self._get_calibrated_dewpoint_depression(), 2),
                ("DewVPD Risk",      "%",   lambda: self._get_calibrated_dewvpd_risk(), 1),
                (
                    "Ambient VPD",
                    "kPa",
                    lambda: self._get_calibrated_vpd(),
                    3,
                ),
                (
                    "Baro-Pressure",
                    "hPa",
                    lambda: self._altitude_adjusted_pressure_hpa(self.thp280.pressure),
                    BAROMETRIC_PRESSURE_PRECISION,
                ),
            ]
        except Exception as e:
            self.present = False
            printDM(f"VPD sensor init failed: {e}")
            return

        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}

    # ------------------------------------------------------------------
    # Calibration loading
    # ------------------------------------------------------------------
    def _load_calibration_offsets(self, settings) -> None:
        """
        Load temperature and humidity offsets from the sensor settings.

        Expected TOML shape:

        [Calibration]
        CALIBRATED = false
        CALIB_STATUS = "Not Calibrated"

        [Calibration.Device]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        APVPD_TEMP_CAL_VAL = 0.0
        APVPD_RH_CAL_VAL   = 0.0

        ALTITUDE_METERS = 0.0

        [Calibration.Manual]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        NOTES       = ""

        [Calibration.System]
        TEMP_OFFSET     = 0.0
        RH_OFFSET       = 0.0
        REF_SENSOR_ID   = ""
        REF_RANGE_HOURS = 24
        REF_START_TS    = 0
        REF_END_TS      = 0
        REF_NOTE        = ""
        """
        # --- normalize to a Calibration-root dict ---
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

        # Device offsets (could include APVPD_TEMP_CAL_VAL, APVPD_RH_CAL_VAL in future)
        device_temp = _safe_float(device_cal, "TEMP_OFFSET")
        device_rh   = _safe_float(device_cal, "RH_OFFSET")

        # Manual offsets (user-entered)
        manual_temp = _safe_float(manual_cal, "TEMP_OFFSET")
        manual_rh   = _safe_float(manual_cal, "RH_OFFSET")

        # System offsets (24h snapshot)
        system_temp = _safe_float(system_cal, "TEMP_OFFSET")
        system_rh   = _safe_float(system_cal, "RH_OFFSET")

        # Effective offsets that actually get applied
        self.temp_offset_c = device_temp + manual_temp + system_temp
        self.rh_offset_pct = device_rh + manual_rh + system_rh
        self._load_device_altitude_meters(settings)

        calib_status = cal_root.get("CALIB_STATUS", "Not Calibrated")

        printDM(
            f"VPDSensor calibration loaded: "
            f"temp_offset_c={self.temp_offset_c:.3f}, "
            f"rh_offset_pct={self.rh_offset_pct:.3f}, "
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
                        f"VPDSensor calibration loaded: "
                        f"temp_offset_c={self.temp_offset_c:.3f}, "
                        f"rh_offset_pct={self.rh_offset_pct:.3f}, "
                        f"altitude_meters={self.altitude_meters}, "
                    ),
                    location=MODULE,
                )          

        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)

    # ------------------------------------------------------------------
    # Calibrated metric helpers
    # ------------------------------------------------------------------
    def _get_raw_temp_c(self) -> float:
        return self.thp280.temperature

    def _get_raw_rh(self) -> float:
        # clamp raw RH for sanity, before applying offset
        return self._clamp_if_number(self.thp280.relative_humidity, 0.0, 100.0)

    def _get_calibrated_temp_c(self) -> float:
        raw_temp = self._get_raw_temp_c()
        if raw_temp is None:
            self.latest_raw["Temperature"] = None
            self.current_values["Temperature"] = None
            return None
        temp_c = raw_temp + self.temp_offset_c
        # Optional: store raw/current for diagnostics
        self.latest_raw["Temperature"] = raw_temp
        self.current_values["Temperature"] = temp_c
        return temp_c

    def _get_calibrated_temp_f(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        if temp_c is None:
            self.current_values["Temperature_F"] = None
            return None
        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        self.current_values["Temperature_F"] = temp_f
        return temp_f

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

    def _get_calibrated_dewpoint_f(self) -> float:
        dewpoint_c = self._get_calibrated_dewpoint_c()
        if dewpoint_c is None:
            return None
        return (dewpoint_c * 9.0 / 5.0) + 32.0

    def _get_calibrated_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            return None
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        return depression

    def _get_calibrated_dewvpd_risk(self) -> float:
        """
        Combined risk score (0-100%) from VPD and dewpoint depression.
        VPD carries more weight than dewpoint spread.
        """
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
