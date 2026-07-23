"""AHT10/AHTx0 temperature and humidity sensor backend for Sensorius.

This module wraps an Adafruit AHTx0-family device and exposes calibrated
ambient temperature, relative humidity, and derived humidity/VPD metrics.
"""

from ..saiUtils import printDM, debug_enabled
from .base import BaseSensor

MODULE = "AHTSensor"
DEBUG = debug_enabled("saiSensorFactory")


class AHTSensor(BaseSensor):
    """AHT10/AHTx0 ambient temperature and relative humidity sensor."""

    def __init__(self, settings, supervisor, i2c_0=None):
        super().__init__(settings, supervisor)

        self.temp_offset_c = 0.0
        self.rh_offset_pct = 0.0
        self._load_calibration_offsets(settings)

        try:
            import adafruit_ahtx0

            self.i2c = self._find_sensor_bus(address={0x38, 0x39})
            if not self.i2c:
                raise RuntimeError("AHTx0 sensor not found on any available I2C bus")

            while not self.i2c.try_lock():
                pass
            try:
                addrs = set(self.i2c.scan() or [])
            finally:
                self.i2c.unlock()
            addr = 0x38 if 0x38 in addrs else 0x39

            self.aht = adafruit_ahtx0.AHTx0(self.i2c, address=addr)
            self.present = True

            self.measurements = [
                ("Temperature", "°C", lambda: self._get_calibrated_temp_c(), 2),
                ("Temperature_F", "°F", lambda: self._get_calibrated_temp_f(), 1),
                ("Rel-Humidity", "%", lambda: self._get_calibrated_rh(), 2),
                ("Humidity", "g/m³", lambda: self._get_calibrated_abs_humidity(), 1),
                ("Dew Point", "°C", lambda: self._get_calibrated_dewpoint_c(), 2),
                ("Dew Point_F", "°F", lambda: self._get_calibrated_dewpoint_f(), 1),
                ("Dew Point Deficit", "°C", lambda: self._get_calibrated_dewpoint_depression(), 2),
                ("DewVPD Risk", "%", lambda: self._get_calibrated_dewvpd_risk(), 1),
                ("Ambient VPD", "kPa", lambda: self._get_calibrated_vpd(), 3),
            ]
        except Exception as exc:
            self.present = False
            printDM(f"AHT sensor init failed: {exc}", location=MODULE)
            return

        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}

    def _load_calibration_offsets(self, settings) -> None:
        """Load effective temperature and relative humidity offsets."""
        try:
            if hasattr(settings, "get"):
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

        device_temp = _safe_float(device_cal, "TEMP_OFFSET")
        device_rh = _safe_float(device_cal, "RH_OFFSET")
        manual_temp = _safe_float(manual_cal, "TEMP_OFFSET")
        manual_rh = _safe_float(manual_cal, "RH_OFFSET")
        system_temp = _safe_float(system_cal, "TEMP_OFFSET")
        system_rh = _safe_float(system_cal, "RH_OFFSET")

        self.temp_offset_c = device_temp + manual_temp + system_temp
        self.rh_offset_pct = device_rh + manual_rh + system_rh

        calib_status = cal_root.get("CALIB_STATUS", "Not Calibrated")
        printDM(
            f"AHT calibration loaded: "
            f"temp_offset_c={self.temp_offset_c:.3f}, "
            f"rh_offset_pct={self.rh_offset_pct:.3f}, "
            f"status='{calib_status}'",
            location=MODULE,
        )

    def reload_calibration_from_settings(self, settings) -> None:
        """Reload calibration offsets after sensor settings are updated."""
        try:
            self._load_calibration_offsets(settings)
            if DEBUG:
                printDM(
                    (
                        f"AHT calibration reloaded: "
                        f"temp_offset_c={self.temp_offset_c:.3f}, "
                        f"rh_offset_pct={self.rh_offset_pct:.3f}"
                    ),
                    location=MODULE,
                )
        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)

    def _get_raw_temp_c(self) -> float:
        return self.aht.temperature

    def _get_raw_rh(self) -> float:
        return self._clamp_if_number(self.aht.relative_humidity, 0.0, 100.0)

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
            self.current_values["Dew Point"] = None
            return None
        dewpoint = self.calculate_dewpoint(temp_c, rh)
        self.current_values["Dew Point"] = dewpoint
        return dewpoint

    def _get_calibrated_dewpoint_f(self) -> float:
        dewpoint_c = self._get_calibrated_dewpoint_c()
        if dewpoint_c is None:
            self.current_values["Dew Point_F"] = None
            return None
        dewpoint_f = (dewpoint_c * 9.0 / 5.0) + 32.0
        self.current_values["Dew Point_F"] = dewpoint_f
        return dewpoint_f

    def _get_calibrated_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_temp_c()
        rh = self._get_calibrated_rh()
        if temp_c is None or rh is None:
            self.current_values["Dew Point Deficit"] = None
            return None
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        self.current_values["Dew Point Deficit"] = depression
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

    def supports_calibration(self):
        """AHT devices expose temperature and relative humidity offsets."""
        return True


# Backward-compatible aliases for possible factory mappings/imports.
AHT10Sensor = AHTSensor
AHTx0Sensor = AHTSensor
