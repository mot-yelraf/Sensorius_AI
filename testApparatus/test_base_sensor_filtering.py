from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensor_modules.base import BaseSensor


class _StubSensor(BaseSensor):
    def __init__(self):
        super().__init__({}, supervisor=None)
        self.present = True
        self.measurements = [
            ("Visible Light Intensity", "mol/m²/day", self._next_dli, 3),
            ("Temperature", "C", self._next_temp, 1),
        ]
        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}
        self.no_filter_metrics = {"Visible Light Intensity"}
        self._dli_samples = iter([0.001, 0.002])
        self._temp_samples = iter([21.0, 28.0])

    def _next_dli(self):
        return next(self._dli_samples)

    def _next_temp(self):
        return next(self._temp_samples)


def test_no_filter_metrics_bypass_iir_smoothing():
    sensor = _StubSensor()

    values_1, _, _ = sensor.read_sensor_data()
    values_2, _, _ = sensor.read_sensor_data()

    assert values_1["Visible Light Intensity"] == 0.001
    assert values_2["Visible Light Intensity"] == 0.002

    assert values_1["Temperature"] == 21.0
    assert values_2["Temperature"] == 22.0
