# sensor_modules/sensor_co2.py
from saiUtils import printDM, debug_enabled
from sensor_modules.base import BaseSensor, find_sensor_bus

MODULE = "SCD30Sensor"
DEBUG = debug_enabled("saiSensorFactory")


class SCD30Sensor(BaseSensor):
    def __init__(self, settings, supervisor, i2c_0=None):
        super().__init__(settings, supervisor)
        import board  # noqa: F401  (kept for future pin overrides)
        import busio  # noqa: F401
        import adafruit_scd30

        # ---- calibration offsets (°C, %RH, ppm) ----
        # Effective offsets = Device + Manual + System
        self.temp_offset_c: float = 0.0
        self.rh_offset_pct: float = 0.0
        self.co2_offset_ppm: float = 0.0
        self._load_calibration_offsets(settings)
        # --------------------------------------------

        try:
            # SCD30 default address is 0x61
            self.i2c = self._find_sensor_bus(address=0x61)
            if not self.i2c:
                raise RuntimeError("SCD30 not found on any available I2C bus")

            self.scd30 = adafruit_scd30.SCD30(self.i2c)

            # Altitude in meters (your site-specific value)
            self.scd30.altitude = 1786

            self.present = True

            # NOTE: all metrics below use *calibrated* T/RH/CO2 via helpers.
            self.measurements = [
                ("CO2", "ppm", lambda: self._get_calibrated_co2(), None),
                ("Temperature", "°C", lambda: self._get_calibrated_temp_c(), 2),
                ("Temperature_F", "°F", lambda: self._get_calibrated_temp_f(), 1),
                ("Rel-Humidity", "%", lambda: self._get_calibrated_rh(), 2),
                (
                    "Humidity",
                    "g/m³",
                    lambda: self._get_calibrated_abs_humidity(),
                    1,
                ),
                ("Dew-Point", "°C", lambda: self._get_calibrated_dewpoint_c(), 2),
                ("Dew-Point_F", "°F", lambda: self._get_calibrated_dewpoint_f(), 1),
                ("Dewpoint Depression", "°C", lambda: self._get_calibrated_dewpoint_depression(), 2),
                ("DewVPD Risk", "%", lambda: self._get_calibrated_dewvpd_risk(), 1),
                (
                    "Ambient VPD",
                    "kPa",
                    lambda: self._get_calibrated_vpd(),
                    3,
                ),
            ]
        except Exception as e:
            self.present = False
            printDM(f"Sensor init failed: {e}", location=MODULE)
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
        Load temperature, humidity and CO2 offsets from the sensor settings.

        Expected TOML shape (CO2 devices):

        [Calibration]
        CALIBRATED   = false
        CALIB_STATUS = "Not Calibrated"

        [Calibration.Device]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        CO2_OFFSET  = 0.0

        [Calibration.Manual]
        TEMP_OFFSET = 0.0
        RH_OFFSET   = 0.0
        CO2_OFFSET  = 0.0

        [Calibration.System]
        TEMP_OFFSET     = 0.0
        RH_OFFSET       = 0.0
        CO2_OFFSET      = 0.0  # optional; system cal currently focuses on T/RH
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
        device_co2  = _safe_float(device_cal, "CO2_OFFSET")

        # Manual offsets (if you expose a Manual section later)
        manual_temp = _safe_float(manual_cal, "TEMP_OFFSET")
        manual_rh   = _safe_float(manual_cal, "RH_OFFSET")
        manual_co2  = _safe_float(manual_cal, "CO2_OFFSET")

        # System offsets (from System Calibration preview/apply)
        system_temp = _safe_float(system_cal, "TEMP_OFFSET")
        system_rh   = _safe_float(system_cal, "RH_OFFSET")
        system_co2  = _safe_float(system_cal, "CO2_OFFSET")

        self.temp_offset_c   = device_temp + manual_temp + system_temp
        self.rh_offset_pct   = device_rh   + manual_rh   + system_rh
        self.co2_offset_ppm  = device_co2  + manual_co2  + system_co2

        calib_status = cal_root.get("CALIB_STATUS", "Not Calibrated")

        if DEBUG:
            printDM(
                f"SCD30 calibration loaded: "
                f"temp_offset_c={self.temp_offset_c:.3f}, "
                f"rh_offset_pct={self.rh_offset_pct:.3f}, "
                f"co2_offset_ppm={self.co2_offset_ppm:.3f}, "
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
                        "SCD30 calibration reloaded: "
                        f"temp_offset_c={self.temp_offset_c:.3f}, "
                        f"rh_offset_pct={self.rh_offset_pct:.3f}, "
                        f"co2_offset_ppm={self.co2_offset_ppm:.3f}, "
                    ),
                    location=MODULE,
                )      

        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)
        
    # ------------------------------------------------------------------
    # Calibrated metric helpers
    # ------------------------------------------------------------------
    def _get_raw_temp_c(self) -> float:
        return self.scd30.temperature

    def _get_raw_rh(self) -> float:
        # clamp raw RH to sane range before offset
        return self._clamp_if_number(self.scd30.relative_humidity, 0.0, 100.0)

    def _get_raw_co2(self) -> float:
        # SCD30.CO2 is ppm; we'll apply offset and clamp later
        return self.scd30.CO2

    def _get_calibrated_temp_c(self) -> float:
        raw_temp = self._get_raw_temp_c()
        temp_c = raw_temp + self.temp_offset_c
        self.latest_raw["Temperature"] = raw_temp
        self.current_values["Temperature"] = temp_c
        return temp_c

    def _get_calibrated_temp_f(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        self.current_values["Temperature_F"] = temp_f
        return temp_f

    def _get_calibrated_rh(self) -> float:
        raw_rh = self._get_raw_rh()
        rh = raw_rh + self.rh_offset_pct
        rh = self._clamp_if_number(rh, 0.0, 100.0)
        self.latest_raw["Rel-Humidity"] = raw_rh
        self.current_values["Rel-Humidity"] = rh
        return rh

    def _get_calibrated_abs_humidity(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        abs_h = self.calculate_absolute_humidity(temp_c, rh)
        self.current_values["Humidity"] = abs_h
        return abs_h

    def _get_calibrated_vpd(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        vpd = self.calculate_vpd(temp_c, rh)
        vpd_clamped = self._clamp_if_number(vpd, 0.0, 5.0)
        self.current_values["Ambient VPD"] = vpd_clamped
        return vpd_clamped

    def _get_calibrated_dewpoint_c(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        dewpoint_c = self.calculate_dewpoint(temp_c, rh)
        self.current_values["Dew-Point"] = dewpoint_c
        return dewpoint_c

    def _get_calibrated_dewpoint_f(self) -> float:
        dewpoint_c = self._get_calibrated_dewpoint_c()
        dewpoint_f = (dewpoint_c * 9.0 / 5.0) + 32.0
        self.current_values["Dew-Point_F"] = dewpoint_f
        return dewpoint_f

    def _get_calibrated_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        self.current_values["Dewpoint Depression"] = depression
        return depression

    def _get_calibrated_dewvpd_risk(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        vpd = self._get_calibrated_vpd()
        risk = self.calculate_dewvpd_risk(temp_c, rh, vpd=vpd)
        risk = self._clamp_if_number(risk, 0.0, 100.0)
        self.current_values["DewVPD Risk"] = risk
        return risk

    def _get_calibrated_co2(self) -> float:
        raw_co2 = self._get_raw_co2()
        co2 = raw_co2 + self.co2_offset_ppm
        # Clamp CO2 to a realistic non-negative range
        co2 = self._clamp_if_number(co2, 0.0, 10000.0)
        self.latest_raw["CO2"] = raw_co2
        self.current_values["CO2"] = co2
        return co2

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------
    def supports_calibration(self):
        # Now that CO2 + T/RH offsets are wired, this can be true
        return True
