"""Test direct SGP30, SGP40, and SGP41 sensor support without hardware."""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.sensor_modules.base import BaseSensor
from sensorius.sensor_modules.sensor_sgp import SGPSensor
import sensorius.saiSensorFactory as sensor_factory


class _Settings:
    def __init__(self, device, metrics=()):
        self.device = device
        self.metrics = tuple(metrics)

    def get_setting(self, section, key, default=None):
        if section == "Sensor" and key == "DEVICE":
            return self.device
        return default

    def get_section(self, section):
        if section == "Display":
            return {
                f"METRIC_{index}": metric
                for index, metric in enumerate(self.metrics, start=1)
            }
        return {}

    def get_all_settings(self):
        return {}


class _SGP30:
    def __init__(self, _i2c, address=0x58):
        self.address = address
        self.compensation_calls = []

    def set_iaq_relative_humidity(self, *, celsius, relative_humidity):
        self.compensation_calls.append((celsius, relative_humidity))

    def iaq_measure(self):
        return 612, 27


class _SGP40:
    def __init__(self, _i2c, address=0x59):
        self.address = address
        self.compensation_calls = []

    def measure_index(self, *, temperature=None, relative_humidity=None):
        self.compensation_calls.append((temperature, relative_humidity))
        return 118


class _SGP41:
    def __init__(self, _i2c, address=0x59):
        self.address = address
        self.conditioning_calls = 0
        self.compensation_calls = []
        self.raw_voc = 30123
        self.raw_nox = 20123

    def conditioning(self, *, temperature=None, humidity=None):
        self.conditioning_calls += 1
        self.compensation_calls.append(("conditioning", temperature, humidity))

    def measure_index(self, *, temperature=None, humidity=None):
        self.compensation_calls.append(("measure", temperature, humidity))
        return 101, 22


def _install_sgp41_modules(monkeypatch):
    package = ModuleType("adafruit_sgp41")
    driver_module = ModuleType("adafruit_sgp41.sgp41")
    driver_module.SGP41 = _SGP41
    monkeypatch.setitem(sys.modules, "adafruit_sgp41", package)
    monkeypatch.setitem(sys.modules, "adafruit_sgp41.sgp41", driver_module)


def _build(monkeypatch, device, metrics=()):
    monkeypatch.setattr(BaseSensor, "_find_sensor_bus", lambda self, **_kwargs: object())
    monkeypatch.setitem(
        sys.modules,
        "adafruit_sgp30",
        SimpleNamespace(Adafruit_SGP30=_SGP30),
    )
    monkeypatch.setitem(sys.modules, "adafruit_sgp40", SimpleNamespace(SGP40=_SGP40))
    _install_sgp41_modules(monkeypatch)
    return SGPSensor(_Settings(device, metrics), supervisor=None)


def test_sgp30_emits_equivalent_co2_and_tvoc(monkeypatch):
    sensor = _build(monkeypatch, "sgp30", ("Equivalent CO2", "TVOC", "NOx Index"))

    values, units, timestamp = sensor.read_sensor_data()

    assert sensor.hardware == "SGP30"
    assert values == {"Equivalent CO2": 612, "TVOC": 27}
    assert units == {"Equivalent CO2": "ppm", "TVOC": "ppb"}
    assert timestamp is not None
    assert sensor.display_metrics == ["Equivalent CO2", "TVOC", "", "", "", ""]


def test_sgp40_emits_only_voc_index(monkeypatch):
    sensor = _build(monkeypatch, "sgp40", ("VOC Index", "NOx Index"))

    values, units, _timestamp = sensor.read_sensor_data()

    assert sensor.hardware == "SGP40"
    assert values == {"VOC Index": 118}
    assert units == {"VOC Index": "index"}
    assert sensor.display_metrics == ["VOC Index", "", "", "", "", ""]


def test_sgp41_conditions_then_computes_voc_and_nox_indexes(monkeypatch):
    sensor = _build(monkeypatch, "sgp41", ("VOC Index", "NOx Index"))

    for _ in range(10):
        assert sensor.read_sensor_data() == (None, None, None)
    values, units, _timestamp = sensor.read_sensor_data()

    assert sensor.hardware == "SGP41"
    assert sensor.driver.conditioning_calls == 10
    assert values == {"VOC Index": 101, "NOx Index": 22}
    assert units == {"VOC Index": "index", "NOx Index": "index"}


@pytest.mark.parametrize(
    ("device", "expected_call"),
    [
        ("sgp30", (24.5, 53.0)),
        ("sgp40", (24.5, 53.0)),
        ("sgp41", ("conditioning", 24.5, 53.0)),
    ],
)
def test_sgp_uses_companion_temperature_and_humidity(
    monkeypatch,
    device,
    expected_call,
):
    sensor = _build(monkeypatch, device)
    sensor.set_compensation_provider(lambda: (24.5, 53.0))

    sensor.read_sensor_data()

    assert sensor.driver.compensation_calls[-1] == expected_call


def test_factory_discovers_sgp_addresses_as_voc_sensors(monkeypatch):
    monkeypatch.setattr(
        sensor_factory,
        "_scan_pi_i2c_busses",
        lambda: {"i2c-1": {0x58}, "i2c-0": {0x59}},
    )
    monkeypatch.setattr(sensor_factory, "_read_chip_id", lambda _bus, _addr: None)

    found = sensor_factory.find_sensors()

    assert sensor_factory.DeviceDescriptor("voc", "i2c-1", (0x58,)) in found
    assert sensor_factory.DeviceDescriptor("voc", "i2c-0", (0x59,)) in found
