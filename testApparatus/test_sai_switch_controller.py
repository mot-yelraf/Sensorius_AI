import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if "board" not in sys.modules:
    sys.modules["board"] = types.SimpleNamespace()
if "digitalio" not in sys.modules:
    sys.modules["digitalio"] = types.SimpleNamespace(
        DigitalInOut=object,
        Direction=types.SimpleNamespace(INPUT=0, OUTPUT=1),
        Pull=types.SimpleNamespace(UP=1, DOWN=0),
    )
if "paho" not in sys.modules:
    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod = types.ModuleType("paho.mqtt.client")
    mqtt_pkg.client = mqtt_client_mod
    paho_mod.mqtt = mqtt_pkg
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client_mod

import saiSwitch
from saiSwitch import SwitchController
from saiUtils import SettingsWrapper


def _make_controller() -> SwitchController:
    ctrl = SwitchController.__new__(SwitchController)
    ctrl.override_script = {}
    ctrl.last_state = {"Fan": False}
    ctrl.last_set_time = {"Fan": 0.0}
    ctrl.min_on_time = 0
    ctrl.min_off_time = 0
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl._advanced_delay_due = {}
    ctrl.mqtt = None
    ctrl.switch = types.SimpleNamespace()
    return ctrl


def test_set_state_does_not_publish_or_log_on_backend_failure():
    class FakeMQTT:
        def __init__(self):
            self.publishes = []

        def is_connected(self):
            return True

        def publish(self, topic, payload):
            self.publishes.append((topic, payload))

    ctrl = _make_controller()
    ctrl.mqtt = FakeMQTT()
    ctrl._set_switch_state = lambda _name, _on: False
    logged = []
    ctrl._log = lambda name, on: logged.append((name, on))

    ok = SwitchController.set_state(ctrl, "Fan", True, force=True)

    assert ok is False
    assert ctrl.last_state["Fan"] is False
    assert logged == []
    assert ctrl.mqtt.publishes == []


def test_evaluate_scripts_updates_memory_only_after_success():
    ctrl = _make_controller()
    ctrl.script_rules = {"Fan": {"conditions": [{"type": "time"}]}}
    ctrl._evaluate_script = lambda _rule, _values, _state: True
    ctrl._set_switch_state = lambda _name, _on: False

    SwitchController.evaluate_and_apply_scripts(ctrl, {})
    assert ctrl.last_state["Fan"] is False

    ctrl._set_switch_state = lambda _name, _on: True
    SwitchController.evaluate_and_apply_scripts(ctrl, {})
    assert ctrl.last_state["Fan"] is True


def test_rules_enabled_parses_string_false_and_maps_channel_id(monkeypatch: pytest.MonkeyPatch):
    updates = []

    class FakeMgr:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_setting(self, switch_id, key, value):
            updates.append((switch_id, key, value))

    class FakePath:
        def exists(self):
            return True

        def stat(self):
            return types.SimpleNamespace(st_mtime=123.0)

    import saiSwitchSettingsManager

    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", FakeMgr)

    ctrl = _make_controller()
    ctrl.settings = {"Switch": {"SWITCH_1": "Fan"}}
    ctrl.switch = types.SimpleNamespace(get_switch_names=lambda: ["Fan"])
    ctrl.script_rules = {}
    ctrl.override_script = {"Fan": False}
    ctrl._get_triggers_path = lambda: FakePath()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": "false",
                "script_json": '{"actions":[{"switch_key":"sw1::CH1"}]}',
            }
        }
    }

    enabled = SwitchController._rules_enabled(ctrl)

    assert enabled is False
    assert ctrl.override_script["Fan"] is True
    assert updates == [("sw1", "SWITCH_1_OVERRIDE_SCRIPT", True)]


def test_advanced_delay_is_non_blocking(monkeypatch: pytest.MonkeyPatch):
    def _fail_sleep(*_args, **_kwargs):
        raise AssertionError("time.sleep should not be called in advanced evaluation")

    monkeypatch.setattr(saiSwitch.time, "sleep", _fail_sleep)

    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "delay_s": 1}],
                },
            }
        }
    }
    calls = []
    ctrl.set_state = lambda label, desired: calls.append((label, desired))
    ctrl.get_state = lambda _label: False

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []
    assert ctrl._advanced_delay_due

    for key in list(ctrl._advanced_delay_due.keys()):
        ctrl._advanced_delay_due[key] = time.monotonic() - 0.1

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True)]


def test_init_applies_refreshed_settings_for_settings_wrapper(monkeypatch: pytest.MonkeyPatch):
    refreshed = {
        "Switch": {
            "TYPE": "pi",
            "SWITCH_ID": "sw1",
            "SWITCH_LOCATION": "Lab",
            "SWITCH_1": "Fan",
            "SWITCH_1_ID": "CH1",
        }
    }

    class FakeLogger:
        def upsert_switch_identity(self, **_kwargs):
            return None

    class FakeMQTT:
        def is_connected(self):
            return False

    class FakeSwitch:
        def __init__(self):
            self.is_present = True
            self._names = ["Fan"]
            self.calls = []

        def get_switch_names(self):
            return list(self._names)

        def set_state(self, name, desired):
            self.calls.append((name, desired))
            return True

    seen_settings = []

    def _fake_create_switch(settings, mqtt_client):
        seen_settings.append(settings)
        return FakeSwitch()

    monkeypatch.setattr(saiSwitch, "saiDataLogger", lambda: FakeLogger())
    monkeypatch.setattr(saiSwitch, "get_mqtt_client", lambda _sid: FakeMQTT())
    monkeypatch.setattr(saiSwitch, "create_switch", _fake_create_switch)
    monkeypatch.setattr(
        __import__("saiSwitchFactory"),
        "ensure_switch_settings_for_host",
        lambda _sid, _loc: refreshed,
    )

    cfg = SettingsWrapper(
        {
            "Switch": {
                "TYPE": "pi",
                "SWITCH_ID": "sw1",
                "SWITCH_LOCATION": "Lab",
                "SWITCH_1": "OldLabel",
            }
        }
    )
    ctrl = SwitchController(switch_settings=cfg, supervisor=None, sensor=None)

    assert ctrl.settings.get_setting("Switch", "SWITCH_1") == "Fan"
    assert seen_settings
    assert seen_settings[0].get_setting("Switch", "SWITCH_1") == "Fan"
