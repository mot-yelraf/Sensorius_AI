"""Pytest smoke test for importing `saiSwitch` without GPIO libraries.

The test keeps non-Raspberry Pi environments working by ensuring the switch
module can import cleanly when board-specific dependencies are absent.
"""

import importlib
import os
import sys
import types


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_sai_switch_imports_without_board(monkeypatch):
    monkeypatch.delitem(sys.modules, "board", raising=False)
    monkeypatch.delitem(sys.modules, "digitalio", raising=False)
    monkeypatch.delitem(sys.modules, "saiSwitch", raising=False)

    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod = types.ModuleType("paho.mqtt.client")
    mqtt_pkg.client = mqtt_client_mod
    paho_mod.mqtt = mqtt_pkg
    monkeypatch.setitem(sys.modules, "paho", paho_mod)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", mqtt_client_mod)

    mod = importlib.import_module("saiSwitch")

    assert mod is not None
    assert hasattr(mod, "SwitchController")
