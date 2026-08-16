"""Sensirion SGP30, SGP40, and SGP41 gas sensor backend.

The gas algorithms are serviced once per second while readings are emitted to
Sensorius persistence at the normal one-minute cadence. This preserves the
drivers' required sampling interval without multiplying database rows.
"""

import time

from ..saiUtils import debug_enabled, get_timestamp, printDM
from .base import BaseSensor

MODULE = "SGPSensor"
DEBUG = debug_enabled("saiSensorFactory")

_METRICS_BY_HARDWARE = {
    "SGP30": (("Equivalent CO2", "ppm"), ("TVOC", "ppb")),
    "SGP40": (("VOC Index", "index"),),
    "SGP41": (("VOC Index", "index"), ("NOx Index", "index")),
}


class SGPSensor(BaseSensor):
    """Read one directly connected SGP30, SGP40, or SGP41 sensor."""

    def __init__(self, settings, supervisor):
        super().__init__(settings, supervisor)
        self.meas_interval = 1.0
        self.publish_interval = 60.0
        self.fixed_period_sampling = True
        self.hardware = ""
        self.driver = None
        self.conditioning_remaining = 0
        self._last_emit_at = 0.0
        self._compensation_provider = None

        try:
            configured = str(
                settings.get_setting("Sensor", "DEVICE", "") or ""
            ).strip().lower()
            address = settings.get_setting("Sensor", "I2C_ADDR", None)
            try:
                address = int(address, 0) if isinstance(address, str) else int(address)
            except (TypeError, ValueError):
                address = None

            candidates = self._address_candidates(configured, address)
            self.i2c = self._find_sensor_bus(address=set(candidates))
            if not self.i2c:
                raise RuntimeError("SGP30/SGP40/SGP41 not found on an available I2C bus")

            self.hardware, self.driver = self._start_driver(candidates, configured)
            self.conditioning_remaining = 10 if self.hardware == "SGP41" else 0
            self.present = True
            self._configure_measurements()
        except Exception as exc:
            self.present = False
            printDM(f"SGP sensor init failed: {exc}", location=MODULE)

    @staticmethod
    def _address_candidates(configured, address):
        if address in (0x58, 0x59):
            return (address,)
        if configured == "sgp30":
            return (0x58,)
        if configured in {"sgp40", "sgp41", "sgp4x"}:
            return (0x59,)
        return (0x58, 0x59)

    def _start_driver(self, candidates, configured):
        last_error = None
        if 0x58 in candidates:
            try:
                import adafruit_sgp30

                return "SGP30", adafruit_sgp30.Adafruit_SGP30(
                    self.i2c, address=0x58
                )
            except Exception as exc:
                last_error = exc

        if 0x59 in candidates and configured != "sgp41":
            try:
                import adafruit_sgp40

                return "SGP40", adafruit_sgp40.SGP40(self.i2c, address=0x59)
            except Exception as exc:
                last_error = exc
        if 0x59 in candidates and configured != "sgp40":
            try:
                from adafruit_sgp41.sgp41 import SGP41

                return "SGP41", SGP41(self.i2c, address=0x59)
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"unable to identify SGP hardware: {last_error}")

    def set_compensation_provider(self, provider):
        """Set a callable returning companion ``(temperature_c, humidity_pct)``."""
        self._compensation_provider = provider if callable(provider) else None

    def _compensation_values(self):
        if not callable(self._compensation_provider):
            return None
        try:
            temperature, humidity = self._compensation_provider()
            temperature = float(temperature)
            humidity = float(humidity)
        except (TypeError, ValueError, AttributeError):
            return None
        if not (-45.0 <= temperature <= 130.0 and 0.0 <= humidity <= 100.0):
            return None
        return temperature, humidity

    def _configure_measurements(self):
        metric_units = _METRICS_BY_HARDWARE[self.hardware]
        self.measurements = [
            (name, unit, lambda metric=name: self.current_values.get(metric), None)
            for name, unit in metric_units
        ]
        self.meas_types = [name for name, _unit in metric_units]
        self.unit_map = dict(metric_units)
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}
        allowed = set(self.meas_types)
        self.display_metrics = [
            metric if metric in allowed else "" for metric in self.display_metrics
        ]

    def _sample_driver(self):
        compensation = self._compensation_values()
        if self.hardware == "SGP30":
            if compensation:
                setter = getattr(self.driver, "set_iaq_relative_humidity", None)
                if callable(setter):
                    setter(
                        celsius=compensation[0],
                        relative_humidity=compensation[1],
                    )
            eco2, tvoc = self.driver.iaq_measure()
            return {"Equivalent CO2": int(eco2), "TVOC": int(tvoc)}
        if self.hardware == "SGP40":
            if compensation:
                voc_index = int(
                    self.driver.measure_index(
                        temperature=compensation[0],
                        relative_humidity=compensation[1],
                    )
                )
            else:
                voc_index = int(self.driver.measure_index())
            return {"VOC Index": voc_index if voc_index > 0 else None}
        if self.conditioning_remaining > 0:
            if compensation:
                self.driver.conditioning(
                    temperature=compensation[0],
                    humidity=compensation[1],
                )
            else:
                self.driver.conditioning()
            self.conditioning_remaining -= 1
            return {}
        if compensation:
            voc_index, nox_index = self.driver.measure_index(
                temperature=compensation[0],
                humidity=compensation[1],
            )
        else:
            voc_index, nox_index = self.driver.measure_index()
        return {
            "VOC Index": int(voc_index) if int(voc_index) > 0 else None,
            "NOx Index": int(nox_index) if int(nox_index) > 0 else None,
        }

    def read_sensor_data(self):
        """Service the gas algorithm and emit a reading at most once per minute."""
        ts = get_timestamp()
        try:
            sample = self._sample_driver()
            if not sample:
                self.meas_status = "pending"
                return None, None, None

            for name in self.meas_types:
                value = sample.get(name)
                self.latest_raw[name] = value
                self.filtered_data[name] = value
                self.current_values[name] = value
            self.current_ts = ts
            has_value = any(value is not None for value in sample.values())
            self.meas_status = "online" if has_value else "pending"

            now = time.monotonic()
            if not has_value or (
                self._last_emit_at and now - self._last_emit_at < self.publish_interval
            ):
                return None, None, None
            self._last_emit_at = now
            return dict(self.current_values), dict(self.unit_map), ts
        except Exception as exc:
            self.meas_status = "pending"
            if DEBUG:
                printDM(f"SGP read failed: {exc}", location=MODULE)
            return None, None, None
