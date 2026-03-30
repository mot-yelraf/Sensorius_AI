"""Dual-BME280 ambient and plant VPD sensor backend.

This module coordinates either a dual-bus or dual-address BME280 arrangement so
Sensorius can compare ambient and plant-climate conditions and derive plant VPD
metrics from the paired probes.
"""

from saiUtils import printDM, debug_enabled
from sensor_modules.base import BaseSensor, find_sensor_bus

MODULE = "VPDPlantSensor"
DEBUG = debug_enabled("saiSensorFactory")


class VPDPlantSensor(BaseSensor):
    """
    Dual-BME280 configuration:
      1) Preferred (default): two buses, each at 0x76
            i2c-1 -> Ambient, i2c-0 -> Plant
      2) Fallback: same bus with 0x76 (Ambient) and 0x77 (Plant)
    """

    def __init__(self, settings, supervisor):
        super().__init__(settings, supervisor)
        from adafruit_bme280.advanced import Adafruit_BME280_I2C, IIR_FILTER_X4

        # -------- user-defined “top” variables (easy to tweak) --------
        ambient_bus_pref = 1   # i2c-1 considered Ambient in dual-bus mode
        plant_bus_pref   = 0   # i2c-0 considered Plant in dual-bus mode
        addr_default     = 0x76
        addr_alt         = 0x77
        # --------------------------------------------------------------

        try:
            # --- Preferred path: dual-bus (two 0x76 devices on i2c-1 and i2c-0) ---
            buses = self._find_sensor_bus(
                address=addr_default,
                want="both",
                key_style="int",
            )
            if (
                isinstance(buses, dict)
                and (ambient_bus_pref in buses)
                and (plant_bus_pref in buses)
            ):
                if DEBUG:
                    printDM(
                        "Dual-bus mode detected (0x76 on i2c-1 and i2c-0)",
                        location=MODULE,
                    )
                self.i2c_ambient = buses[ambient_bus_pref]
                self.i2c_plant = buses[plant_bus_pref]
                ambient_addr = addr_default
                plant_addr = addr_default
            else:
                # --- Fallback: same-bus dual addresses (0x76 + 0x77) ---
                if DEBUG:
                    printDM(
                        "Dual-bus not found; trying same-bus dual-address fallback",
                        location=MODULE,
                    )

                i2c = self._find_sensor_bus(
                    address={addr_default, addr_alt},
                    want="any",
                )
                if not i2c:
                    raise RuntimeError(
                        "Dual BME280 not detected (need 0x76 on i2c-1 and i2c-0, "
                        "or 0x76+0x77 on one bus)."
                    )

                # Detect which of 0x76 / 0x77 are present on this one bus
                while not i2c.try_lock():
                    pass
                try:
                    addrs = set(i2c.scan() or [])
                finally:
                    i2c.unlock()

                if not ({addr_default, addr_alt} <= (addrs | {None})):
                    # require both addresses on the same bus
                    if addr_default not in addrs or addr_alt not in addrs:
                        raise RuntimeError(
                            f"Same-bus fallback failed: expected 0x76 and 0x77; "
                            f"found {sorted(hex(a) for a in addrs)}"
                        )

                if DEBUG:
                    printDM(
                        f"Same-bus mode: found {sorted(hex(a) for a in addrs)}",
                        location=MODULE,
                    )

                self.i2c_ambient = i2c
                self.i2c_plant = i2c
                ambient_addr = addr_default
                plant_addr = addr_alt

            # --- Create the two BME280 instances ---
            self.thp280 = Adafruit_BME280_I2C(
                self.i2c_ambient,
                address=ambient_addr,
            )
            self.thp280_plant = Adafruit_BME280_I2C(
                self.i2c_plant,
                address=plant_addr,
            )

            self.present = True

            # Filters
            self.thp280.iir_filter = IIR_FILTER_X4
            self.thp280_plant.iir_filter = IIR_FILTER_X4

            # --- Calibration defaults ---
            # Ambient (device/manual/system) offsets
            self.ambient_temp_offset_c = 0.0
            self.ambient_rh_offset_pct = 0.0

            # Plant calibration offsets (APVPD)
            self.thp280_plant_temp_cal = 0.0
            self.thp280_plant_rh_cal = 0.0
            self.is_calibrated = "Not Calibrated"

            # --- Load calibration from sensor.toml (new nested layout) ---
            self._load_calibration_offsets(settings)

            # --- Measurement definitions ---
            # NOTE: ambient metrics use *calibrated* helpers; plant metrics use APVPD offsets only.
            self.measurements = [
                # Ambient
                ("Temperature", "°C", lambda: self._get_calibrated_ambient_temp_c(), 2),
                (
                    "Temperature_F",
                    "°F",
                    lambda: self._get_calibrated_ambient_temp_f(),
                    1,
                ),
                (
                    "Rel-Humidity",
                    "%",
                    lambda: self._get_calibrated_ambient_rh(),
                    2,
                ),
                (
                    "Humidity",
                    "g/m³",
                    lambda: self._get_calibrated_ambient_abs_humidity(),
                    1,
                ),
                (
                    "Dew Point",
                    "°C",
                    lambda: self._get_calibrated_ambient_dewpoint_c(),
                    2,
                ),
                (
                    "Dew Point_F",
                    "°F",
                    lambda: self._get_calibrated_ambient_dewpoint_f(),
                    1,
                ),
                (
                    "Dew Point Deficit",
                    "°C",
                    lambda: self._get_calibrated_ambient_dewpoint_depression(),
                    2,
                ),
                (
                    "DewVPD Risk",
                    "%",
                    lambda: self._get_calibrated_ambient_dewvpd_risk(),
                    1,
                ),
                (
                    "Ambient VPD",
                    "kPa",
                    lambda: self._get_calibrated_ambient_vpd(),
                    3,
                ),
                (
                    "Baro-Pressure",
                    "hPa",
                    lambda: self._clamp_if_number(
                        self.thp280.pressure, 700, 1100
                    ),
                    None,
                ),

                # Plant metrics (APVPD device calibration only)
                (
                    "Plant Temperature",
                    "°C",
                    lambda: self._get_calibrated_plant_temp_c(),
                    2,
                ),
                (
                    "Plant Temperature_F",
                    "°F",
                    lambda: self._get_calibrated_plant_temp_f(),
                    1,
                ),
                (
                    "Plant Rel-Humidity",
                    "%",
                    lambda: self._get_calibrated_plant_rh(),
                    2,
                ),
                (
                    "Plant Humidity",
                    "g/m³",
                    lambda: self._get_calibrated_plant_abs_humidity(),
                    1,
                ),
                (
                    "Plant Dew Point",
                    "°C",
                    lambda: self._get_calibrated_plant_dewpoint_c(),
                    2,
                ),
                (
                    "Plant Dew Point_F",
                    "°F",
                    lambda: self._get_calibrated_plant_dewpoint_f(),
                    1,
                ),
                (
                    "Plant Dewpoint Deficit",
                    "°C",
                    lambda: self._get_calibrated_plant_dewpoint_depression(),
                    2,
                ),
                (
                    "Plant DewVPD Risk",
                    "%",
                    lambda: self._get_calibrated_plant_dewvpd_risk(),
                    1,
                ),
                (
                    "Plant VPD",
                    "kPa",
                    lambda: self._get_calibrated_plant_vpd(),
                    3,
                ),
                (
                    "Plant Baro-Pressure",
                    "hPa",
                    lambda: self._clamp_if_number(
                        self.thp280_plant.pressure, 700, 1100
                    ),
                    None,
                ),
            ]
        except Exception as e:
            self.present = False
            printDM(
                f"Dual VPD sensor init failed: {e}",
                location="VPDPlantSensor",
            )
            return

        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}

    # ------------------------------------------------------------------
    # Calibration loading (ambient + plant)
    # ------------------------------------------------------------------
    def _load_calibration_offsets(self, settings) -> None:
        """
        Load ambient device/manual/system offsets and APVPD plant calibration
        from the sensor settings.

        Expected TOML shape (APVPD devices):

        [Calibration]
        CALIBRATED   = false
        CALIB_STATUS = "Not Calibrated"

        [Calibration.Device]
        TEMP_OFFSET = 0.0        # ambient
        RH_OFFSET   = 0.0        # ambient
        APVPD_TEMP_CAL_VAL = 0.0 # plant
        APVPD_RH_CAL_VAL   = 0.0 # plant

        [Calibration.Manual]
        TEMP_OFFSET = 0.0        # ambient
        RH_OFFSET   = 0.0        # ambient

        [Calibration.System]
        TEMP_OFFSET = 0.0        # ambient
        RH_OFFSET   = 0.0        # ambient
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

        # ---- Ambient offsets (device + manual + system) ----
        device_temp = _safe_float(device_cal, "TEMP_OFFSET")
        manual_temp = _safe_float(manual_cal, "TEMP_OFFSET")
        system_temp = _safe_float(system_cal, "TEMP_OFFSET")

        device_rh = _safe_float(device_cal, "RH_OFFSET")
        manual_rh = _safe_float(manual_cal, "RH_OFFSET")
        system_rh = _safe_float(system_cal, "RH_OFFSET")

        self.ambient_temp_offset_c = device_temp + manual_temp + system_temp
        self.ambient_rh_offset_pct = device_rh + manual_rh + system_rh

        # ---- Plant calibration (APVPD) ----
        def _safe_float_any(*vals):
            for v in vals:
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        continue
            return 0.0

        self.thp280_plant_temp_cal = _safe_float_any(
            device_cal.get("APVPD_TEMP_CAL_VAL"),
            cal_root.get("APVPD_TEMP_CAL_VAL"),
        )
        self.thp280_plant_rh_cal = _safe_float_any(
            device_cal.get("APVPD_RH_CAL_VAL"),
            cal_root.get("APVPD_RH_CAL_VAL"),
        )

        # ---- Calibration status ----
        status_str = cal_root.get("CALIB_STATUS")
        if status_str:
            self.is_calibrated = status_str
        else:
            calibrated_flag = bool(cal_root.get("CALIBRATED", False))
            self.is_calibrated = (
                "Calibrated" if calibrated_flag else "Not Calibrated"
            )

        if DEBUG:
            printDM(
                (
                    "APVPD calibration loaded: "
                    f"CAL_STATUS={self.is_calibrated}, "
                    f"ambient_TEMP_OFFSET={self.ambient_temp_offset_c:.3f}°C, "
                    f"ambient_RH_OFFSET={self.ambient_rh_offset_pct:.3f}%, "
                    f"plant_TEMP_OFFSET={self.thp280_plant_temp_cal:.3f}°C, "
                    f"plant_RH_OFFSET={self.thp280_plant_rh_cal:.3f}%"
                ),
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
                        "APVPD calibration reloaded: "
                        f"CAL_STATUS={self.is_calibrated}, "
                        f"ambient_TEMP_OFFSET={self.ambient_temp_offset_c:.3f}°C, "
                        f"ambient_RH_OFFSET={self.ambient_rh_offset_pct:.3f}%, "
                        f"plant_TEMP_OFFSET={self.thp280_plant_temp_cal:.3f}°C, "
                        f"plant_RH_OFFSET={self.thp280_plant_rh_cal:.3f}%"
                    ),
                    location=MODULE,
                )      

        except Exception as exc:
            printDM(f"reload_calibration_from_settings failed: {exc}", location=MODULE)

    # ------------------------------------------------------------------
    # Ambient calibrated helpers
    # ------------------------------------------------------------------
    def _get_raw_ambient_temp_c(self) -> float:
        return self.thp280.temperature

    def _get_raw_ambient_rh(self) -> float:
        return self._clamp_if_number(self.thp280.relative_humidity, 0.0, 100.0)

    def _get_calibrated_ambient_temp_c(self) -> float:
        raw_temp = self._get_raw_ambient_temp_c()
        if raw_temp is None:
            self.latest_raw["Temperature"] = None
            self.current_values["Temperature"] = None
            return None
        temp_c = raw_temp + self.ambient_temp_offset_c
        self.latest_raw["Temperature"] = raw_temp
        self.current_values["Temperature"] = temp_c
        return temp_c

    def _get_calibrated_ambient_temp_f(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        if temp_c is None:
            self.current_values["Temperature_F"] = None
            return None
        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        self.current_values["Temperature_F"] = temp_f
        return temp_f

    def _get_calibrated_ambient_rh(self) -> float:
        raw_rh = self._get_raw_ambient_rh()
        if raw_rh is None:
            self.latest_raw["Rel-Humidity"] = None
            self.current_values["Rel-Humidity"] = None
            return None
        rh = raw_rh + self.ambient_rh_offset_pct
        rh = self._clamp_if_number(rh, 0.0, 100.0)
        self.latest_raw["Rel-Humidity"] = raw_rh
        self.current_values["Rel-Humidity"] = rh
        return rh

    def _get_calibrated_ambient_abs_humidity(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        rh = self._get_calibrated_ambient_rh()
        if temp_c is None or rh is None:
            self.current_values["Humidity"] = None
            return None
        abs_h = self.calculate_absolute_humidity(temp_c, rh)
        self.current_values["Humidity"] = abs_h
        return abs_h

    def _get_calibrated_ambient_vpd(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        rh = self._get_calibrated_ambient_rh()
        if temp_c is None or rh is None:
            self.current_values["Ambient VPD"] = None
            return None
        vpd = self.calculate_vpd(temp_c, rh)
        vpd_clamped = self._clamp_if_number(vpd, 0.0, 5.0)
        self.current_values["Ambient VPD"] = vpd_clamped
        return vpd_clamped

    def _get_calibrated_ambient_dewpoint_c(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        rh = self._get_calibrated_ambient_rh()
        if temp_c is None or rh is None:
            return None
        return self.calculate_dewpoint(temp_c, rh)

    def _get_calibrated_ambient_dewpoint_f(self) -> float:
        dewpoint_c = self._get_calibrated_ambient_dewpoint_c()
        if dewpoint_c is None:
            return None
        return (dewpoint_c * 9.0 / 5.0) + 32.0

    def _get_calibrated_ambient_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        rh = self._get_calibrated_ambient_rh()
        if temp_c is None or rh is None:
            return None
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        return depression

    def _get_calibrated_ambient_dewvpd_risk(self) -> float:
        temp_c = self._get_calibrated_ambient_temp_c()
        rh = self._get_calibrated_ambient_rh()
        if temp_c is None or rh is None:
            self.current_values["DewVPD Risk"] = None
            return None
        vpd = self._get_calibrated_ambient_vpd()
        if vpd is None:
            self.current_values["DewVPD Risk"] = None
            return None
        risk = self.calculate_dewvpd_risk(temp_c, rh, vpd=vpd)
        risk = self._clamp_if_number(risk, 0.0, 100.0)
        self.current_values["DewVPD Risk"] = risk
        return risk

    # ------------------------------------------------------------------
    # Plant calibrated helpers
    # ------------------------------------------------------------------
    def _get_raw_plant_temp_c(self) -> float:
        return self.thp280_plant.temperature

    def _get_raw_plant_rh(self) -> float:
        return self._clamp_if_number(self.thp280_plant.relative_humidity, 0.0, 100.0)

    def _get_calibrated_plant_temp_c(self) -> float:
        raw_temp = self._get_raw_plant_temp_c()
        if raw_temp is None:
            self.latest_raw["Plant Temperature"] = None
            self.current_values["Plant Temperature"] = None
            return None
        temp_c = raw_temp + self.thp280_plant_temp_cal
        self.latest_raw["Plant Temperature"] = raw_temp
        self.current_values["Plant Temperature"] = temp_c
        return temp_c

    def _get_calibrated_plant_temp_f(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        if temp_c is None:
            self.current_values["Plant Temperature_F"] = None
            return None
        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        self.current_values["Plant Temperature_F"] = temp_f
        return temp_f

    def _get_calibrated_plant_rh(self) -> float:
        raw_rh = self._get_raw_plant_rh()
        if raw_rh is None:
            self.latest_raw["Plant Rel-Humidity"] = None
            self.current_values["Plant Rel-Humidity"] = None
            return None
        rh = raw_rh + self.thp280_plant_rh_cal
        rh = self._clamp_if_number(rh, 0.0, 100.0)
        self.latest_raw["Plant Rel-Humidity"] = raw_rh
        self.current_values["Plant Rel-Humidity"] = rh
        return rh

    def _get_calibrated_plant_abs_humidity(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        rh = self._get_calibrated_plant_rh()
        if temp_c is None or rh is None:
            self.current_values["Plant Humidity"] = None
            return None
        abs_h = self.calculate_absolute_humidity(temp_c, rh)
        self.current_values["Plant Humidity"] = abs_h
        return abs_h

    def _get_calibrated_plant_vpd(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        rh = self._get_calibrated_plant_rh()
        if temp_c is None or rh is None:
            self.current_values["Plant VPD"] = None
            return None
        vpd = self.calculate_vpd(temp_c, rh)
        vpd_clamped = self._clamp_if_number(vpd, 0.0, 5.0)
        self.current_values["Plant VPD"] = vpd_clamped
        return vpd_clamped

    def _get_calibrated_plant_dewpoint_c(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        rh = self._get_calibrated_plant_rh()
        if temp_c is None or rh is None:
            return None
        return self.calculate_dewpoint(temp_c, rh)

    def _get_calibrated_plant_dewpoint_f(self) -> float:
        dewpoint_c = self._get_calibrated_plant_dewpoint_c()
        if dewpoint_c is None:
            return None
        return (dewpoint_c * 9.0 / 5.0) + 32.0

    def _get_calibrated_plant_dewpoint_depression(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        rh = self._get_calibrated_plant_rh()
        if temp_c is None or rh is None:
            return None
        depression = self.calculate_dewpoint_depression(temp_c, rh)
        depression = self._clamp_if_number(depression, 0.0, 30.0)
        return depression

    def _get_calibrated_plant_dewvpd_risk(self) -> float:
        temp_c = self._get_calibrated_plant_temp_c()
        rh = self._get_calibrated_plant_rh()
        if temp_c is None or rh is None:
            self.current_values["Plant DewVPD Risk"] = None
            return None
        vpd = self._get_calibrated_plant_vpd()
        if vpd is None:
            self.current_values["Plant DewVPD Risk"] = None
            return None
        risk = self.calculate_dewvpd_risk(temp_c, rh, vpd=vpd)
        risk = self._clamp_if_number(risk, 0.0, 100.0)
        self.current_values["Plant DewVPD Risk"] = risk
        return risk

    # ------------------------------------------------------------------
    # Common helpers
    # ------------------------------------------------------------------
    def _find_sensor_bus(self, address, want="any", key_style="str"):
        # Delegate to shared helper; kept for drop-in compatibility with older code
        return find_sensor_bus(address=address, want=want, key_style=key_style)

    def supports_calibration(self):
        return True

    async def _notify_calibration_result(
        self,
        success: bool,
        err_msg: str | None = None,
    ) -> None:
        """
        Push a calibration result to Sensorius via MQTT (primary) and
        optionally via HTTP POST if a Sensorius host is known.

        NOTE: This function is left as-is structurally; it still uses
        temp_offset / rh_offset from self.thp280_plant_*_cal. Only the
        TOML persistence has been updated for the new sections.
        """
        from cPyUtils import get_timestamp, printDM as cp_printDM

        payload = {
            "status": "success" if success else "failed",
            "sensor_id": getattr(self, "sensor_id", ""),
            "timestamp": get_timestamp(),
            "calibrated": bool(success),
            "temp_offset": round(
                float(getattr(self, "thp280_plant_temp_cal", 0.0) or 0.0),
                3,
            ),
            "rh_offset": round(
                float(getattr(self, "thp280_plant_rh_cal", 0.0) or 0.0),
                3,
            ),
        }
        if not success and err_msg:
            payload["error"] = str(err_msg)

        # ——— MQTT event (primary) ———
        try:
            # retain=True lets Sensorius see the latest result even if it reconnects later
            if hasattr(self, "mqtt_publish_event"):
                await self.mqtt_publish_event(
                    "calibration_result",
                    payload,
                    retain=True,
                )
                cp_printDM(
                    "Calibration event published via MQTT",
                    location="APVPDSensor",
                )
        except Exception as e:
            cp_printDM(
                f"MQTT publish error: {e}",
                location="APVPDSensor",
            )

        # ——— Optional HTTP callback (best-effort) ———
        try:
            sens_host = ""
            if hasattr(self, "settings") and hasattr(self.settings, "get_netSettings"):
                ssid, password, hostname, broker = self.settings.get_netSettings()
                sens_host = (getattr(self.settings, "SENSORIUS_HOST", "") or "").strip()
                if not sens_host:
                    sens_host = (hostname or "").strip()

            if sens_host:
                import json
                import httpx

                for url in (
                    f"http://{sens_host}.local:8000/sensor-event",
                    f"http://{sens_host}:8000/sensor-event",
                ):
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.post(
                                url,
                                json={
                                    "event": "calibration_result",
                                    "payload": payload,
                                },
                            )
                        if resp.status_code < 300:
                            cp_printDM(
                                "Calibration event POSTed to Sensorius",
                                location="APVPDSensor",
                            )
                            break
                    except Exception:
                        continue
        except Exception as e:
            cp_printDM(
                f"HTTP callback error: {e}",
                location="APVPDSensor",
            )

    async def calibrate_plant_sensor(self):
        """
        Local plant calibration on the Pi:

        - Uses current_data_set() readings for:
            "Temperature", "Plant Temperature",
            "Rel-Humidity", "Plant Rel-Humidity"
        - Computes offsets such that Plant tracks Ambient on average.
        - Persists to:
            [Calibration]
                CALIBRATED = true
                CALIB_STATUS = "Calibrated"
            [Calibration.Device]
                APVPD_TEMP_CAL_VAL
                APVPD_RH_CAL_VAL
        """
        import asyncio

        self.is_calibrated = "Calibrating"
        sample_count = 5
        delay_between = 11

        temp_ref_vals = []
        temp_plant_vals = []
        rh_ref_vals = []
        rh_plant_vals = []

        printDM("Starting plant sensor calibration...", location=MODULE)

        for i in range(sample_count):
            values, _, _ = self.current_data_set()

            temp_ref = values.get("Temperature")
            # metric names match self.measurements ("Plant Temperature", "Plant Rel-Humidity")
            temp_plant = values.get("Plant Temperature")
            rh_ref = values.get("Rel-Humidity")
            rh_plant = values.get("Plant Rel-Humidity")

            if None in (temp_ref, temp_plant, rh_ref, rh_plant):
                printDM(
                    f"Skipping sample {i + 1} due to missing data",
                    location=MODULE,
                )
            else:
                temp_ref_vals.append(float(temp_ref))
                temp_plant_vals.append(float(temp_plant))
                rh_ref_vals.append(float(rh_ref))
                rh_plant_vals.append(float(rh_plant))

            await asyncio.sleep(delay_between)

        if not temp_ref_vals:
            printDM(
                "Calibration failed: no valid samples",
                location=MODULE,
            )
            self.is_calibrated = "Not Calibrated"
            return

        avg_temp_ref = sum(temp_ref_vals) / len(temp_ref_vals)
        avg_temp_plant = sum(temp_plant_vals) / len(temp_plant_vals)
        avg_rh_ref = sum(rh_ref_vals) / len(rh_ref_vals)
        avg_rh_plant = sum(rh_plant_vals) / len(rh_plant_vals)

        self.thp280_plant_temp_cal = avg_temp_ref - avg_temp_plant
        self.thp280_plant_rh_cal = avg_rh_ref - avg_rh_plant
        self.is_calibrated = "Calibrated"

        # --- persist calibration to sensor.toml (new layout) ---
        try:
            if hasattr(self.settings, "replace_setting"):
                # top-level calibration flags
                self.settings.replace_setting(
                    "Calibration",
                    "CALIBRATED",
                    True,
                )
                self.settings.replace_setting(
                    "Calibration",
                    "CALIB_STATUS",
                    self.is_calibrated,
                )
                # device-specific APVPD offsets
                self.settings.replace_setting(
                    "Calibration.Device",
                    "APVPD_TEMP_CAL_VAL",
                    round(self.thp280_plant_temp_cal, 3),
                )
                self.settings.replace_setting(
                    "Calibration.Device",
                    "APVPD_RH_CAL_VAL",
                    round(self.thp280_plant_rh_cal, 3),
                )

            printDM(
                "Calibration persisted to sensor TOML",
                location=MODULE,
            )
        except Exception as persist_err:
            printDM(
                f"Calibration persistence failed: {persist_err}",
                location=MODULE,
            )

        # Optional: notify Sensorius (MQTT/HTTP)
        try:
            await self._notify_calibration_result(True)
        except Exception:
            # Do not break calibration flow on notification issues
            pass

        printDM(
            f"Calibration complete. Temperature offset: {self.thp280_plant_temp_cal:.2f}°C, "
            f"RH offset: {self.thp280_plant_rh_cal:.2f}%",
            location=MODULE,
        )
