import os
import sys
import time
import types
from datetime import datetime, timedelta

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
if not hasattr(sys.modules["paho.mqtt.client"], "Client"):
    sys.modules["paho.mqtt.client"].Client = object

import saiSwitch
from saiSwitch import (
    RemoteSwitchController,
    SwitchController,
    build_switch_controller,
    is_remote_switch_settings,
)
from saiUtils import SettingsWrapper


def _make_controller() -> SwitchController:
    ctrl = SwitchController.__new__(SwitchController)
    ctrl.override_script = {}
    ctrl.last_state = {"Fan": False}
    ctrl.last_set_time = {"Fan": 0.0}
    ctrl.auto_off_seconds = {"Fan": 0}
    ctrl.auto_off_deadline = {"Fan": None}
    ctrl.min_on_time = 0
    ctrl.min_off_time = 0
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl._advanced_delay_due = {}
    ctrl.mqtt = None
    ctrl.switch = types.SimpleNamespace()
    return ctrl


def test_set_auto_off_seconds_tracks_runtime_only_deadline(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 100.0)

    applied = SwitchController.set_auto_off_seconds(ctrl, "Fan", 45)

    assert applied == 45
    assert ctrl.auto_off_seconds["Fan"] == 45
    assert ctrl.auto_off_deadline["Fan"] == pytest.approx(145.0)

    applied = SwitchController.set_auto_off_seconds(ctrl, "Fan", 0)
    assert applied == 0
    assert ctrl.auto_off_deadline["Fan"] is None


def test_process_auto_off_timers_turns_channel_off(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True
    ctrl.auto_off_seconds["Fan"] = 30
    ctrl.auto_off_deadline["Fan"] = 100.0
    calls = []

    def _fake_set_state(label, state, force=False):
        calls.append((label, state, force))
        ctrl.last_state[label] = state
        SwitchController._sync_auto_off_state(ctrl, label, state, restart=state)
        return True

    ctrl.set_state = _fake_set_state
    monkeypatch.setattr(saiSwitch.time, "time", lambda: 100.5)

    SwitchController._process_auto_off_timers(ctrl)

    assert calls == [("Fan", False, True)]
    assert ctrl.last_state["Fan"] is False
    assert ctrl.auto_off_deadline["Fan"] is None


def test_set_state_restarts_auto_off_only_on_new_on_transition(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True
    ctrl.auto_off_seconds["Fan"] = 30
    ctrl.auto_off_deadline["Fan"] = 150.0
    ctrl.get_state = lambda label: ctrl.last_state[label]
    ctrl._set_switch_state = lambda _name, _on: True
    ctrl._log = lambda _name, _on: None

    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 50.0)

    assert SwitchController.set_state(ctrl, "Fan", True, force=True) is True
    assert ctrl.auto_off_deadline["Fan"] == 150.0


def test_remote_ingest_refresh_does_not_restart_active_auto_off(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"sw1": {"CH1": "on"}})
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.last_state = {"Fan": False}
    ctrl.auto_off_seconds = {"Fan": 30}
    ctrl.auto_off_deadline = {"Fan": 150.0}
    ctrl.get_switch_names = lambda: ["Fan"]

    called = []

    def _fake_sync(label, is_on, *, restart=False):
        called.append((label, is_on, restart))
        SwitchController._sync_auto_off_state(ctrl, label, is_on, restart=restart)

    ctrl._sync_auto_off_state = _fake_sync

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 120.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is True
    assert called == [("Fan", True, False)]
    assert ctrl.auto_off_deadline["Fan"] == 150.0


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


def test_advanced_rule_does_nothing_when_conditions_are_false():
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "00:01"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "delay_s": 0}],
                },
            }
        }
    }
    calls = []
    ctrl.set_state = lambda label, desired: calls.append((label, desired))
    ctrl.get_state = lambda _label: True

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == []


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


def test_eval_astral_condition_threshold_logic(monkeypatch: pytest.MonkeyPatch):
    class FakeLocationInfo:
        def __init__(self, **_kwargs):
            self.observer = object()

    ctrl = _make_controller()
    ctrl._resolve_astral_location = lambda: {"lat": 40.0, "lon": -105.0, "tz": "UTC"}

    monkeypatch.setattr(saiSwitch, "LocationInfo", FakeLocationInfo)

    def _sun_past(_observer, date=None, tzinfo=None):
        now = datetime.now(tzinfo)
        return {
            "sunrise": now - timedelta(minutes=5),
            "sunset": now + timedelta(hours=6),
        }

    monkeypatch.setattr(saiSwitch, "_astral_sun", _sun_past)
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunrise", "offset_min": 0},
    ) is True

    def _sun_future(_observer, date=None, tzinfo=None):
        now = datetime.now(tzinfo)
        return {
            "sunrise": now + timedelta(hours=2),
            "sunset": now + timedelta(hours=6),
        }

    monkeypatch.setattr(saiSwitch, "_astral_sun", _sun_future)
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunrise", "offset_min": 0},
    ) is False


def test_resolve_astral_location_delegates_to_settings(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl._astral_location_cache = {"value": None, "expires_at": 0.0}

    class FakeSettings:
        def __init__(self, apply_live=False):
            assert apply_live is False

        def resolve_astral_location(self, *, persist_if_auto=False, timeout_sec=0):
            assert persist_if_auto is True
            assert timeout_sec == 2.5
            return {"lat": 40.0, "lon": -105.0, "tz": "America/Denver", "source": "manual"}

    import sys

    monkeypatch.setitem(sys.modules, "saiSettings", types.SimpleNamespace(saiSettings=FakeSettings))

    resolved = SwitchController._resolve_astral_location(ctrl)

    assert resolved == {"lat": 40.0, "lon": -105.0, "tz": "America/Denver", "source": "manual"}
    assert ctrl._astral_location_cache["value"] == resolved


def test_advanced_rule_supports_astral_condition(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "astral", "astral_event": "sunset", "offset_min": 10}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "delay_s": 0}],
                },
            }
        }
    }
    monkeypatch.setattr(ctrl, "_eval_astral_condition", lambda _cond: True)
    ctrl.get_state = lambda _label: False
    calls = []
    ctrl.set_state = lambda label, desired: calls.append((label, desired))

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True)]


def test_advanced_evaluation_ignores_actions_for_other_switch_ids():
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "sw-other::Fan", "set": True, "delay_s": 0}],
                },
            }
        }
    }
    ctrl.get_state = lambda _label: False
    calls = []
    ctrl.set_state = lambda label, desired: calls.append((label, desired))

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []


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


def test_is_remote_switch_settings():
    assert is_remote_switch_settings({"Switch": {"TYPE": "pico2w"}}) is True
    assert is_remote_switch_settings({"Switch": {"TYPE": "nodus"}}) is True
    assert is_remote_switch_settings({"Switch": {"TYPE": "pi"}}) is False


def test_build_switch_controller_selects_remote(monkeypatch: pytest.MonkeyPatch):
    class FakeRemote:
        def __init__(self, **kwargs):
            self.kind = "remote"
            self.kwargs = kwargs

    class FakeLocal:
        def __init__(self, **kwargs):
            self.kind = "local"
            self.kwargs = kwargs

    monkeypatch.setattr(saiSwitch, "RemoteSwitchController", FakeRemote)
    monkeypatch.setattr(saiSwitch, "SwitchController", FakeLocal)

    remote = build_switch_controller(switch_settings={"Switch": {"TYPE": "nodus"}}, supervisor=None, sensor=None)
    local = build_switch_controller(switch_settings={"Switch": {"TYPE": "pi"}}, supervisor=None, sensor=None)

    assert remote.kind == "remote"
    assert local.kind == "local"


def test_remote_switch_controller_refreshes_state_from_ingest_cache():
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.switch_id = "switch-1"
    ctrl.channel_id_for_label = {"Fan": "S1-abc"}
    ctrl.last_state = {"Fan": False}
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"switch-1": {"S1-abc": "ON"}})
    ctrl.get_switch_names = lambda: ["Fan"]

    assert RemoteSwitchController.get_state(ctrl, "Fan") is True
