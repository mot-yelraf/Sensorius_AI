"""Test CO2 sensor startup readiness and first-read behavior.

Hardware drivers are replaced with fakes so SCD30 and SCD4x timing, altitude,
and missing-value handling can be verified without an I2C device.
"""

import os
import sys
from types import SimpleNamespace


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.sensor_modules.base import BaseSensor
from sensorius.sensor_modules.sensor_co2 import CO2Sensor


class _Settings:
    def __init__(self, i2c_addr=None):
        self.i2c_addr = i2c_addr

    def get_setting(self, section, key, default=None):
        if section == "Sensor" and key == "I2C_ADDR":
            return self.i2c_addr
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


class _FakeSCD4X:
    instances = []

    def __init__(self, _i2c):
        self.data_ready = True
        self.data_available = False
        self.started = False
        self.altitude = 0
        self.__class__.instances.append(self)

    def start_periodic_measurement(self):
        self.started = True


class _FakeSCD30:
    instances = []

    def __init__(self, _i2c):
        self.data_available = True
        self.data_ready = False
        self.altitude = 0
        self.__class__.instances.append(self)


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


def test_scd4x_startup_wait_uses_data_ready(monkeypatch):
    _FakeSCD4X.instances.clear()
    wait_checks = []
    monkeypatch.setitem(sys.modules, "adafruit_scd4x", SimpleNamespace(SCD4X=_FakeSCD4X))
    monkeypatch.setitem(sys.modules, "adafruit_scd30", SimpleNamespace(SCD30=_FakeSCD30))
    monkeypatch.setattr(CO2Sensor, "_find_sensor_bus", lambda self, **_kwargs: object())

    def _fake_wait(self, timeout_s=20.0, interval_s=0.5):
        wait_checks.append((self._co2_model, self._data_ready(), timeout_s, interval_s))
        return self._data_ready()

    monkeypatch.setattr(CO2Sensor, "_wait_for_data_ready", _fake_wait)

    sensor = CO2Sensor(_Settings(), supervisor=None)

    assert sensor.present is True
    assert sensor._co2_model == "SCD4x"
    assert sensor._startup_ready_waited is True
    assert sensor._startup_data_ready is True
    assert wait_checks == [("SCD4x", True, 20.0, 0.5)]
    assert _FakeSCD4X.instances[0].started is True


def test_scd30_startup_wait_uses_data_available(monkeypatch):
    _FakeSCD30.instances.clear()
    wait_checks = []
    monkeypatch.setitem(sys.modules, "adafruit_scd4x", SimpleNamespace(SCD4X=_FakeSCD4X))
    monkeypatch.setitem(sys.modules, "adafruit_scd30", SimpleNamespace(SCD30=_FakeSCD30))
    monkeypatch.setattr(CO2Sensor, "_find_sensor_bus", lambda self, **_kwargs: object())

    def _fake_wait(self, timeout_s=20.0, interval_s=0.5):
        wait_checks.append((self._co2_model, self._data_ready(), timeout_s, interval_s))
        return self._data_ready()

    monkeypatch.setattr(CO2Sensor, "_wait_for_data_ready", _fake_wait)

    sensor = CO2Sensor(_Settings(i2c_addr="0x61"), supervisor=None)

    assert sensor.present is True
    assert sensor._co2_model == "SCD30"
    assert sensor._startup_ready_waited is True
    assert sensor._startup_data_ready is True
    assert wait_checks == [("SCD30", True, 20.0, 0.5)]


def test_first_read_waits_once_when_startup_wait_not_performed(monkeypatch):
    flag_sensor = _FlagSensor(data_ready=False)
    sensor = _co2_with_flag("SCD4x", flag_sensor)
    sensor._first_sample_seen = False
    sensor._startup_ready_waited = False
    sensor._startup_data_ready = False
    sensor.meas_status = ""
    sensor.measurements = []
    sensor.meas_types = []
    sensor.unit_map = {}
    sensor.filtered_data = {}
    sensor.latest_raw = {}
    sensor.current_values = {}
    wait_calls = []

    def _fake_wait(*_args, **_kwargs):
        wait_calls.append(True)
        flag_sensor.data_ready = True
        return True

    monkeypatch.setattr(sensor, "_wait_for_data_ready", _fake_wait)

    values, _units, _ts = sensor.read_sensor_data()

    assert values == {}
    assert wait_calls == [True]
    assert sensor._startup_ready_waited is True
    assert sensor._startup_data_ready is True
    assert sensor.meas_status == "pending"


def test_first_read_does_not_repeat_startup_wait(monkeypatch):
    flag_sensor = _FlagSensor(data_ready=False)
    sensor = _co2_with_flag("SCD4x", flag_sensor)
    sensor._first_sample_seen = False
    sensor._startup_ready_waited = True
    sensor._startup_data_ready = False
    sensor.meas_status = ""
    sensor.measurements = []
    sensor.meas_types = []
    sensor.unit_map = {}
    sensor.filtered_data = {}
    sensor.latest_raw = {}
    sensor.current_values = {}

    def _unexpected_wait(*_args, **_kwargs):
        raise AssertionError("startup wait should not repeat after it has already run")

    monkeypatch.setattr(sensor, "_wait_for_data_ready", _unexpected_wait)

    values, _units, _ts = sensor.read_sensor_data()

    assert values == {}
    assert sensor.meas_status == "pending"


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
