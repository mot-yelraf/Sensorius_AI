import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensor_modules.base import BaseSensor
from sensor_modules.sensor_co2 import CO2Sensor


class _Settings:
    def get_setting(self, section, key, default=None):
        return default

    def get_all_settings(self):
        return {}


class _FlagSensor:
    def __init__(self, *, data_available=None, data_ready=None):
        if data_available is not None:
            self.data_available = data_available
        if data_ready is not None:
            self.data_ready = data_ready


class _AltitudeSensor:
    altitude = 0


def _co2_with_flag(model, flag_sensor):
    sensor = CO2Sensor.__new__(CO2Sensor)
    sensor._co2_model = model
    sensor.scd30 = flag_sensor
    return sensor


def test_scd30_uses_data_available_for_readiness():
    sensor = _co2_with_flag("SCD30", _FlagSensor(data_available=True, data_ready=False))

    assert sensor._data_ready() is True


def test_scd4x_uses_data_ready_for_readiness():
    sensor = _co2_with_flag("SCD4x", _FlagSensor(data_available=True, data_ready=False))

    assert sensor._data_ready() is False


def test_co2_sensor_applies_configured_altitude_to_driver():
    sensor = CO2Sensor.__new__(CO2Sensor)
    sensor._co2_model = "SCD30"
    sensor.scd30 = _AltitudeSensor()
    sensor.altitude_meters = 1624.4

    sensor._apply_configured_altitude()

    assert sensor.scd30.altitude == 1624


def test_base_sensor_keeps_status_pending_when_all_values_are_missing():
    sensor = BaseSensor(_Settings(), supervisor=None)
    sensor.present = True
    sensor.measurements = [("CO2", "ppm", lambda: None, None)]
    sensor.meas_types = ["CO2"]
    sensor.unit_map = {"CO2": "ppm"}
    sensor.filtered_data = {"CO2": None}
    sensor.latest_raw = {"CO2": None}
    sensor.current_values = {"CO2": None}

    values, _units, _ts = sensor.read_sensor_data()

    assert values == {"CO2": None}
    assert sensor.meas_status == "pending"
