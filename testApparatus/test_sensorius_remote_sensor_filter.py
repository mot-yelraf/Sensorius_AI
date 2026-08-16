"""Pytest coverage for filtering remote sensors out of local controller startup.

These tests ensure Sensorius does not build local sensor controllers for settings
that describe MQTT-remote devices.
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


if "paho" not in sys.modules:
    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod = types.ModuleType("paho.mqtt.client")
    mqtt_client_mod.Client = type("Client", (), {})
    mqtt_pkg.client = mqtt_client_mod
    paho_mod.mqtt = mqtt_pkg
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client_mod


import Sensorius


class _FakeSensorMgr:
    def __init__(self, *_args, **_kwargs):
        pass

    def load(self, sensor_id):
        docs = {
            "co2-local": {
                "Sensor": {
                    "TYPE": "pi",
                    "DEVICE": "co2",
                    "SENSOR_ID": "co2-local",
                    "LOCATION": "Lab",
                }
            },
            "co2-remote": {
                "Sensor": {
                    "TYPE": "nodus",
                    "DEVICE": "co2",
                    "SENSOR_ID": "co2-remote",
                    "LOCATION": "Tent",
                }
            },
        }
        return docs.get(sensor_id, {})


class _FakeSensorController:
    def __init__(self, config, supervisor, gc_mgr, data_logger=None):
        self.sensor_id = config.get_setting("Sensor", "SENSOR_ID")
        self.supervisor = supervisor
        self.gc_mgr = gc_mgr
        self.data_logger = data_logger


class _CompensatedSensor:
    def __init__(self):
        self.provider = None

    def set_compensation_provider(self, provider):
        self.provider = provider


@pytest.mark.asyncio
async def test_build_sensor_controllers_skips_remote_sensor_settings(monkeypatch):
    monkeypatch.setattr(Sensorius, "SensorSettingsManager", _FakeSensorMgr)
    monkeypatch.setattr(Sensorius, "SensorController", _FakeSensorController)

    sensors = await Sensorius.build_sensor_controllers(
        ["co2-local", "co2-remote"],
        supervisor=object(),
        gc_mgr=object(),
        data_logger=object(),
    )

    assert [sensor.sensor_id for sensor in sensors] == ["co2-local"]


def test_local_sgp_compensation_prefers_same_location_controller():
    sgp = SimpleNamespace(sensor=_CompensatedSensor(), location="Grow Room")
    other_room = SimpleNamespace(
        sensor=SimpleNamespace(current_values={"Temperature": 19.0, "Rel-Humidity": 40.0}),
        location="Office",
    )
    same_room = SimpleNamespace(
        sensor=SimpleNamespace(current_values={"Temperature": 25.5, "Rel-Humidity": 61.0}),
        location="Grow Room",
    )

    Sensorius._wire_local_sgp_compensation([sgp, other_room, same_room])

    assert sgp.sensor.provider() == (25.5, 61.0)
