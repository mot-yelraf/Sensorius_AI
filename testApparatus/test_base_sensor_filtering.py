"""Pytest coverage for base sensor filtering behavior.

The tests in this module verify that metrics flagged to bypass smoothing are
left unsmoothed by the shared `BaseSensor` filter pipeline.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensorius.sensor_modules.base import BAROMETRIC_PRESSURE_PRECISION, BaseSensor


class _StubSensor(BaseSensor):
    def __init__(self):
        super().__init__({}, supervisor=None)
        self.present = True
        self.measurements = [
            ("Visible Light Intensity", "mol/m²/day", self._next_dli, 3),
            ("Temperature", "C", self._next_temp, 1),
            (
                "Baro-Pressure",
                "hPa",
                self._next_pressure,
                BAROMETRIC_PRESSURE_PRECISION,
            ),
        ]
        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}
        self.FILTER_SIZE = 7
        self.IIR_ALPHA = 1.0 / self.FILTER_SIZE
        self.no_filter_metrics = {"Visible Light Intensity"}
        self._dli_samples = iter([0.001, 0.002])
        self._temp_samples = iter([21.0, 28.0])
        self._pressure_samples = iter([1008.85, 1009.04])

    def _next_dli(self):
        return next(self._dli_samples)

    def _next_temp(self):
        return next(self._temp_samples)

    def _next_pressure(self):
        return next(self._pressure_samples)


def test_no_filter_metrics_bypass_iir_smoothing():
    sensor = _StubSensor()

    values_1, _, _ = sensor.read_sensor_data()
    values_2, _, _ = sensor.read_sensor_data()

    assert values_1["Visible Light Intensity"] == 0.001
    assert values_2["Visible Light Intensity"] == 0.002

    assert values_1["Temperature"] == 21.0
    assert values_2["Temperature"] == 22.0
    assert values_1["Baro-Pressure"] == 1008.9
    assert values_2["Baro-Pressure"] == 1008.9
