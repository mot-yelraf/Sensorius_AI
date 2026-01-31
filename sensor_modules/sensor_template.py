# sensor_modules/sensor_dummy.py
import math
from rPiUtils import printDM, debug_enabled
from sensor_modules.base import BaseSensor, find_sensor_bus

MODULE = "SensorTemplate"
DEBUG = debug_enabled("rPiSensorFactory")

class SensorTemplate(BaseSensor):
    def __init__(self, settings, supervisor):
        super().__init__(settings, supervisor)
        self.present = True
        self.measurements = [
            ("Temperature", "°C", lambda: 22.5 + time.time() % 5, 2),
            ("Rel-Humidity", "%", lambda: 55.0 + time.time() % 10, 2),
            ("Ambient VPD", "kPa", lambda: self.calculate_vpd(22.5, 55.0), 3),
        ]
        self.meas_types = [name for name, *_ in self.measurements]
        self.unit_map = {name: unit for name, unit, *_ in self.measurements}
        self.filtered_data = {name: None for name in self.meas_types}
        self.latest_raw = {name: None for name in self.meas_types}
        self.current_values = {name: None for name in self.meas_types}
