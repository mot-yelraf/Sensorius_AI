"""Pytest coverage for non-Raspberry Pi startup behavior.

The test in this module verifies local sensor configuration bootstrap is safely
skipped when Pi-specific runtime support is unavailable.
"""

import importlib.util
import os
import sys
import types

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


@pytest.mark.asyncio
async def test_ensure_local_sensor_configs_skips_without_pi_runtime(monkeypatch):
    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "board" else original_find_spec(name),
    )

    class _SentinelMgr:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("SensorSettingsManager should not be constructed on non-Pi hosts")

    monkeypatch.setattr(Sensorius, "SensorSettingsManager", _SentinelMgr)

    result = await Sensorius.ensure_local_sensor_configs(settings=object())

    assert result == []
