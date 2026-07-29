"""Pytest coverage for switch controller runtime behavior.

These tests exercise auto-off timing, remote-state refresh, rule evaluation,
astral conditions, and controller selection behavior.
"""

import os
import sys
import time
import types
import asyncio
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

import sensorius.saiSwitch as saiSwitch
from sensorius.saiSwitch import (
    RemoteSwitchController,
    SwitchController,
    build_switch_controller,
    is_remote_switch_settings,
)
from sensorius.saiUtils import SettingsWrapper


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
    ctrl._advanced_revert_cooldown = set()
    ctrl._advanced_active_actions = {}
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


def test_time_window_treats_midnight_to_midnight_as_all_day():
    ctrl = _make_controller()

    assert SwitchController._time_in_window(ctrl, "00:00", "00:00", "00:00") is True
    assert SwitchController._time_in_window(ctrl, "00:00", "00:00", "12:34") is True
    assert SwitchController._time_in_window(ctrl, "00:00", "00:00", "23:59") is True


def test_process_auto_off_timers_turns_channel_off(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True
    ctrl.auto_off_seconds["Fan"] = 30
    ctrl.auto_off_deadline["Fan"] = 100.0
    calls = []

    def _fake_set_state(label, state, force=False, event_source="manual/ui"):
        calls.append((label, state, force, event_source))
        ctrl.last_state[label] = state
        SwitchController._sync_auto_off_state(ctrl, label, state, restart=state)
        return True

    ctrl.set_state = _fake_set_state
    monkeypatch.setattr(saiSwitch.time, "time", lambda: 100.5)

    SwitchController._process_auto_off_timers(ctrl)

    assert calls == [("Fan", False, True, "manual/timer")]
    assert ctrl.last_state["Fan"] is False
    assert ctrl.auto_off_deadline["Fan"] is None


def test_process_auto_off_timers_clears_deadline_after_single_expiry_attempt(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True
    ctrl.auto_off_seconds["Fan"] = 30
    ctrl.auto_off_deadline["Fan"] = 100.0
    calls = []

    def _fake_set_state(label, state, force=False, event_source="manual/ui"):
        calls.append((label, state, force, event_source))
        return False

    ctrl.set_state = _fake_set_state
    monkeypatch.setattr(saiSwitch.time, "time", lambda: 100.5)

    SwitchController._process_auto_off_timers(ctrl)

    assert calls == [("Fan", False, True, "manual/timer")]
    assert ctrl.auto_off_deadline["Fan"] is None
    assert ctrl.last_state["Fan"] is True


def test_set_state_restarts_auto_off_only_on_new_on_transition(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = True
    ctrl.auto_off_seconds["Fan"] = 30
    ctrl.auto_off_deadline["Fan"] = 150.0
    ctrl.get_state = lambda label: ctrl.last_state[label]
    ctrl._set_switch_state = lambda _name, _on, event_origin="manual", event_label="": True
    ctrl._log = lambda _name, _on, source="manual/ui": None

    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 50.0)

    assert SwitchController.set_state(ctrl, "Fan", True, force=True) is True
    assert ctrl.auto_off_deadline["Fan"] == 150.0


def test_set_state_auto_rule_does_not_start_manual_timer(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl.last_state["Fan"] = False
    ctrl.auto_off_seconds["Fan"] = 60
    ctrl.auto_off_deadline["Fan"] = None
    ctrl.get_state = lambda label: ctrl.last_state[label]
    ctrl._set_switch_state = lambda _name, _on, event_origin="manual", event_label="": True
    ctrl._log = lambda _name, _on, source="manual/ui": None

    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 50.0)

    assert SwitchController.set_state(ctrl, "Fan", True, force=True, event_source="auto/rule:TOD") is True
    assert ctrl.auto_off_deadline["Fan"] is None


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

    def _fake_sync(label, is_on, *, restart=False, allow_create_if_missing=True):
        called.append((label, is_on, restart))
        SwitchController._sync_auto_off_state(
            ctrl,
            label,
            is_on,
            restart=restart,
            allow_create_if_missing=allow_create_if_missing,
        )

    ctrl._sync_auto_off_state = _fake_sync

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 120.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is True
    assert called == [("Fan", True, False)]
    assert ctrl.auto_off_deadline["Fan"] == 150.0


def test_remote_ingest_on_refresh_does_not_create_new_deadline_after_expiry(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"sw1": {"CH1": "on"}})
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.last_state = {"Fan": False}
    ctrl.auto_off_seconds = {"Fan": 30}
    ctrl.auto_off_deadline = {"Fan": None}
    ctrl.get_switch_names = lambda: ["Fan"]

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 120.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is True
    assert ctrl.auto_off_deadline["Fan"] is None


def test_remote_ingest_prefers_pending_on_over_stale_cached_off(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.mqtt_ingest = types.SimpleNamespace(
        _switch_state_cache={"sw1": {"CH1": "off"}},
        _pending_set={("sw1", "Fan"): {"ts": 100.0, "state": True, "channel_id": "CH1"}},
    )
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.last_state = {"Fan": True}
    ctrl.auto_off_seconds = {"Fan": 60}
    ctrl.auto_off_deadline = {"Fan": 160.0}
    ctrl.get_switch_names = lambda: ["Fan"]

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 105.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is True
    assert ctrl.auto_off_deadline["Fan"] == pytest.approx(160.0)


def test_remote_ingest_uses_pending_state_without_cached_topic_state(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.mqtt_ingest = types.SimpleNamespace(
        _switch_state_cache={},
        _pending_set={("sw1", "Fan"): {"ts": 100.0, "state": False, "channel_id": "CH1"}},
    )
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.last_state = {"Fan": True}
    ctrl.auto_off_seconds = {"Fan": 60}
    ctrl.auto_off_deadline = {"Fan": 160.0}
    ctrl.get_switch_names = lambda: ["Fan"]

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 105.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is False
    assert ctrl.auto_off_deadline["Fan"] is None


def test_remote_ingest_stale_cached_off_clears_deadline_without_pending(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.mqtt_ingest = types.SimpleNamespace(
        _switch_state_cache={"sw1": {"CH1": "off"}},
        _pending_set={},
    )
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.last_state = {"Fan": True}
    ctrl.auto_off_seconds = {"Fan": 60}
    ctrl.auto_off_deadline = {"Fan": 160.0}
    ctrl.get_switch_names = lambda: ["Fan"]

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 105.0)

    RemoteSwitchController._refresh_state_from_ingest(ctrl)

    assert ctrl.last_state["Fan"] is False
    assert ctrl.auto_off_deadline["Fan"] is None


def test_sync_manual_toggle_result_starts_auto_off_on_new_on_transition(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.last_state = {"Fan": False}
    ctrl.last_set_time = {"Fan": 0.0}
    ctrl.auto_off_seconds = {"Fan": 60}
    ctrl.auto_off_deadline = {"Fan": None}

    monkeypatch.setattr(saiSwitch.time, "time", lambda: 100.0)
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 55.0)

    RemoteSwitchController.sync_manual_toggle_result(ctrl, "Fan", True, previous_state=False)

    assert ctrl.last_state["Fan"] is True
    assert ctrl.last_set_time["Fan"] == pytest.approx(55.0)
    assert ctrl.auto_off_deadline["Fan"] == pytest.approx(160.0)


def test_sync_manual_toggle_result_clears_auto_off_on_manual_off(monkeypatch: pytest.MonkeyPatch):
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.last_state = {"Fan": True}
    ctrl.last_set_time = {"Fan": 10.0}
    ctrl.auto_off_seconds = {"Fan": 60}
    ctrl.auto_off_deadline = {"Fan": 160.0}

    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 75.0)

    RemoteSwitchController.sync_manual_toggle_result(ctrl, "Fan", False, previous_state=True)

    assert ctrl.last_state["Fan"] is False
    assert ctrl.last_set_time["Fan"] == pytest.approx(75.0)
    assert ctrl.auto_off_deadline["Fan"] is None


def test_remote_log_skips_manual_ui_history_writes():
    logged = []
    ctrl = RemoteSwitchController.__new__(RemoteSwitchController)
    ctrl.is_remote = True
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.data_logger = types.SimpleNamespace(
        log_switch_event=lambda **kwargs: logged.append(kwargs)
    )

    RemoteSwitchController._log(ctrl, "Fan", False)

    assert logged == []


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
    ctrl._set_switch_state = lambda _name, _on, event_origin="manual", event_label="": False
    logged = []
    ctrl._log = lambda name, on, source="manual/ui": logged.append((name, on, source))

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
    ctrl.set_state = lambda label, desired, **_kwargs: calls.append((label, desired))
    ctrl.get_state = lambda _label: True

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == []


def test_advanced_notify_actor_reports_triggered_and_cleared_edges(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    ctrl.switch_id = "__system__"
    rule_on = {"value": True}
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "notify1": {
                "enabled": True,
                "script_json": {
                    "name": "High temperature",
                    "enabled": True,
                    "conditions": [{
                        "type": "sensor",
                        "sensor": "sensor-1",
                        "metric": "temperature",
                        "op": ">",
                        "value": 25,
                        "hyst": 1,
                    }],
                    "actions": [{
                        "type": "notify",
                        "to": "grower@example.com",
                        "executor_switch_id": "__system__",
                    }],
                },
            }
        }
    }
    sent = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(saiSwitch.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        saiSwitch.SMTPEmailSender,
        "send",
        lambda _self, subject, body, **kwargs: sent.append((subject, body, kwargs)),
    )
    monkeypatch.setattr(saiSwitch.socket, "gethostname", lambda: "sensorius-hub-3")

    def values():
        return {"sensor-1": {"temperature": 30 if rule_on["value"] else 20}}

    ctrl._get_values_for_sensor = lambda sensor_id, current: current.get(sensor_id, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, values())
    SwitchController._evaluate_and_apply_advanced(ctrl, values())
    assert len(sent) == 1
    assert (
        sent[0][0]
        == "Sensorius ACTIVATED: High temperature: temperature was 30°C"
    )
    assert sent[0][2]["to_addresses"] == ("grower@example.com",)
    assert "State: ACTIVATED" in sent[0][1]
    assert "Hub: sensorius-hub-3" in sent[0][1]
    assert "Group 1: TRUE" in sent[0][1]
    assert (
        "[TRUE] Sensor sensor-1; value 30; temperature > 25; hysteresis 1"
        in sent[0][1]
    )
    assert "trigger > 26; clear <= 24" in sent[0][1]
    assert sent[0][1].index("Conditions (AND within each group") < sent[0][1].index(
        "State: ACTIVATED"
    )
    assert "Configured actors:" in sent[0][1]
    assert "Notify: email grower@example.com" in sent[0][1]

    rule_on["value"] = False
    SwitchController._evaluate_and_apply_advanced(ctrl, values())
    assert len(sent) == 2
    assert sent[1][0] == "Sensorius CLEARED: High temperature: temperature was 20°C"
    assert "State: CLEARED" in sent[1][1]
    assert "Group 1: FALSE" in sent[1][1]
    assert (
        "[FALSE] Sensor sensor-1; value 20; temperature > 25; hysteresis 1"
        in sent[1][1]
    )

    rule_on["value"] = True
    SwitchController._evaluate_and_apply_advanced(ctrl, values())
    assert len(sent) == 3
    assert (
        sent[2][0]
        == "Sensorius ACTIVATED: High temperature: temperature was 30°C"
    )


def test_automation_notification_reports_or_groups_and_switch_actions(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    monkeypatch.setattr(saiSwitch.socket, "gethostname", lambda: "samhain.local")
    conditions = [
        {
            "type": "time",
            "start": "06:00",
            "end": "18:00",
            "days": [0, 1, 2, 3, 4],
        },
        {
            "type": "timer",
            "duration_min": 5,
            "period_min": 60,
        },
    ]

    subject, body = SwitchController._build_automation_notification(
        ctrl,
        rule_id="daylight-notification",
        rule_name="Daylight Notification",
        triggered=True,
        evaluated_groups=[
            {"result": False, "conditions": [(conditions[0], False)]},
            {"result": True, "conditions": [(conditions[1], True)]},
        ],
        actions=[
            {
                "type": "switch",
                "switch_key": "switch-yuk0nv::LEDS",
                "set": False,
                "revert_action": "previous_state",
                "delay_s": 3,
            },
            {
                "type": "notify",
                "to": "grower@example.com",
            },
        ],
        current_values_map={},
    )

    assert subject == "Sensorius ACTIVATED: Daylight Notification"
    assert "State: ACTIVATED" in body
    assert "Group 1: FALSE" in body
    assert "[FALSE] Time of day 06:00-18:00; Mon,Tue,Wed,Thu,Fri" in body
    assert "\nOR\nGroup 2: TRUE" in body
    assert "[TRUE] Timer active 5 min every 60 min" in body
    assert (
        "Switch switch-yuk0nv:LEDS: Off; revert Previous State; delay 3 sec"
        in body
    )
    assert "Notify: email grower@example.com" in body


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

    import sensorius.saiSwitchSettingsManager as saiSwitchSettingsManager
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
    monotonic_now = {"value": 100.0}
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: monotonic_now["value"])

    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 1}],
                },
            }
        }
    }
    calls = []
    state = {"Fan": False}

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []
    assert ctrl._advanced_delay_due
    assert ctrl._advanced_revert_cooldown == set()

    monotonic_now["value"] = 101.1

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]
    assert ("rule1", "Fan", "sw1::Fan", True) in ctrl._advanced_active_actions


def test_advanced_previous_state_reverts_when_rule_stops_matching():
    ctrl = _make_controller()
    state = {"Fan": False}
    calls = []
    rule_on = {"value": True}

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00" if rule_on["value"] else "00:01"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]

    rule_on["value"] = False
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False), ("Fan", False, True)]


def test_advanced_rule_matches_switch_id_case_insensitively():
    ctrl = _make_controller()
    ctrl.switch_id = "SWITCH-X943FM"
    ctrl.channel_id_for_label = {"Pump": "S2-x943fm"}
    ctrl.last_state = {"Pump": False}
    calls = []

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "switch-x943fm::S2-x943fm", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        ctrl.last_state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: ctrl.last_state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Pump", True, False, "auto/rule")]


def test_advanced_previous_state_recovers_revert_after_restart_from_history():
    ctrl = _make_controller()
    state = {"Fan": True}
    calls = []

    ctrl._advanced_active_actions = {}
    ctrl._advanced_delay_due = {}
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "name": "TOD",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "00:01"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-03-31 11:30:03", "mqtt-auto:TOD"),
            ("Off", "2026-03-31 11:25:06", "mqtt-auto:TOD"),
        ]
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Fan", False, True, "auto/rule:TOD")]


def test_advanced_previous_state_history_recovery_runs_once_per_inactive_period():
    ctrl = _make_controller()
    calls = []
    rule_on = {"value": False}

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "name": "TOD",
                    "enabled": True,
                    "conditions": [
                        {
                            "type": "time",
                            "start": "00:00",
                            "end": "24:00" if rule_on["value"] else "00:01",
                        }
                    ],
                    "actions": [
                        {
                            "switch_key": "sw1::Fan",
                            "set": True,
                            "revert_action": "previous_state",
                            "delay_s": 0,
                        }
                    ],
                },
            }
        }
    }
    ctrl._recover_advanced_revert_from_history = lambda *_a: calls.append("recover") or True

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == ["recover"]

    rule_on["value"] = True
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    rule_on["value"] = False
    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == ["recover", "recover"]


def test_advanced_idle_debug_output_is_throttled(monkeypatch: pytest.MonkeyPatch):
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {"Advanced": {}}
    monotonic_now = {"value": 100.0}
    messages = []

    monkeypatch.setattr(saiSwitch, "DEBUG", True)
    monkeypatch.setattr(
        saiSwitch.time,
        "monotonic",
        lambda: monotonic_now["value"],
    )
    monkeypatch.setattr(
        saiSwitch,
        "printDM",
        lambda message, **_kwargs: messages.append(message),
    )

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    monotonic_now["value"] = 105.0
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    monotonic_now["value"] = 160.0
    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert messages == [
        "[advanced] sw1: evaluating 0 rule(s)",
        "[advanced] sw1: evaluating 0 rule(s)",
    ]


def test_advanced_previous_state_recovery_prefers_remote_ingest_current_state():
    ctrl = _make_controller()
    ctrl.is_remote = True
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Fan": "CH1"}
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"sw1": {"CH1": "on"}})
    state = {"Fan": False}
    calls = []

    ctrl._advanced_active_actions = {}
    ctrl._advanced_delay_due = {}
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "name": "TOD",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "00:01"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-03-31 11:30:03", "mqtt-auto:TOD"),
            ("Off", "2026-03-31 11:25:06", "mqtt-auto:TOD"),
        ]
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Fan", False, True, "auto/rule:TOD")]


def test_advanced_previous_state_recovers_when_latest_row_is_not_rule_specific():
    ctrl = _make_controller()
    ctrl.is_remote = True
    ctrl.switch_id = "sw1"
    ctrl.channel_id_for_label = {"Pump": "CH2"}
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"sw1": {"CH2": "on"}})
    state = {"Pump": False}
    calls = []

    ctrl._advanced_active_actions = {}
    ctrl._advanced_delay_due = {}
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "name": "Pump On",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "00:01"}],
                    "actions": [{"switch_key": "sw1::CH2", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-04-01 12:10:05", "mqtt-nodus"),
            ("On", "2026-04-01 07:00:21", "mqtt-auto:Pump On"),
            ("Off", "2026-03-31 12:10:31", "mqtt-auto:Pump On"),
        ]
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Pump", False, True, "auto/rule:Pump On")]


def test_advanced_previous_state_restores_from_persisted_runtime_ownership():
    ctrl = _make_controller()
    ctrl.switch_id = "switch-x943fm"
    ctrl.last_state = {"Pump": True}
    ctrl.channel_id_for_label = {"Pump": "S2-x943fm"}
    ctrl.settings = {
        "Runtime": {
            "ADVANCED_ACTIVE_ACTIONS_JSON": (
                '[{"activated_at":123.0,"desired":true,"revert_action":"previous_state",'
                '"revert_to":false,"rule_id":"auto-cuqitkkg4pi","rule_name":"Pump On",'
                '"switch_key":"switch-x943fm::S2-x943fm","target_label":"Pump"}]'
            )
        }
    }
    calls = []

    SwitchController._restore_advanced_runtime_state(ctrl)
    assert ("auto-cuqitkkg4pi", "Pump", "switch-x943fm::S2-x943fm", True) in ctrl._advanced_active_actions

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "auto-cuqitkkg4pi": {
                "enabled": True,
                "script_json": {
                    "name": "Pump On",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "00:01"}],
                    "actions": [{"switch_key": "switch-x943fm::S2-x943fm", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-03-31 07:00:41", "mqtt"),
            ("Off", "2026-03-30 12:10:31", "mqtt"),
        ]
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        ctrl.last_state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: ctrl.last_state[label]
    ctrl._persist_advanced_runtime_state = lambda: None

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Pump", False, True, "auto/rule:Pump On")]
    assert ctrl._advanced_active_actions == {}


def test_advanced_runtime_ownership_persists_only_previous_state_actions(monkeypatch: pytest.MonkeyPatch):
    saved = []

    class FakeMgr:
        def __init__(self, *_args, **_kwargs):
            pass

        def set_setting(self, switch_id, dotted_key, value):
            saved.append((switch_id, dotted_key, value))

    import sensorius.saiSwitchSettingsManager as saiSwitchSettingsManager
    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", FakeMgr)

    ctrl = _make_controller()
    ctrl.switch_id = "sw1"
    ctrl._advanced_active_actions = {
        ("rule1", "Fan", "sw1::Fan", True): {
            "rule_id": "rule1",
            "rule_name": "Daily Timer",
            "target_label": "Fan",
            "switch_key": "sw1::Fan",
            "desired": True,
            "revert_action": "previous_state",
            "revert_to": False,
            "activated_at": 123.0,
        },
        ("rule2", "Fan", "sw1::Fan", False): {
            "rule_id": "rule2",
            "rule_name": "No Persist",
            "target_label": "Fan",
            "switch_key": "sw1::Fan",
            "desired": False,
            "revert_action": "do_nothing",
            "revert_to": True,
            "activated_at": 124.0,
        },
    }
    ctrl._advanced_active_actions_persisted_json = None

    SwitchController._persist_advanced_runtime_state(ctrl)

    assert saved == [
        (
            "sw1",
            "Runtime.ADVANCED_ACTIVE_ACTIONS_JSON",
            '[{"activated_at":123.0,"desired":true,"revert_action":"previous_state","revert_to":false,"rule_id":"rule1","rule_name":"Daily Timer","switch_key":"sw1::Fan","target_label":"Fan"}]',
        )
    ]


def test_advanced_previous_state_bootstraps_from_time_start_transition():
    ctrl = _make_controller()
    ctrl.is_remote = True
    ctrl.switch_id = "switch-x943fm"
    ctrl.channel_id_for_label = {"Pump": "S2-x943fm"}
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"switch-x943fm": {"S2-x943fm": "on"}})
    ctrl._advanced_active_actions = {}
    ctrl._advanced_delay_due = {}
    state = {"Pump": True}
    calls = []

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "auto-cuqitkkg4pi": {
                "enabled": True,
                "script_json": {
                    "name": "Pump On",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "07:00", "end": "12:10"}],
                    "actions": [{"switch_key": "switch-x943fm::S2-x943fm", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        local_tz=saiSwitch.ZoneInfo("America/Denver"),
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-03-31T07:00:41.450445-06:00", "mqtt"),
            ("On", "2026-03-31T07:00:40.603158-06:00", "manual/ui"),
            ("Off", "2026-03-30T12:10:31.357796-06:00", "mqtt"),
        ],
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Pump", False, True, "auto/rule:Pump On")]


def test_advanced_previous_state_bootstrap_does_not_grab_manual_on_state():
    ctrl = _make_controller()
    ctrl.is_remote = True
    ctrl.switch_id = "switch-x943fm"
    ctrl.channel_id_for_label = {"Fan": "S1-x943fm"}
    ctrl.mqtt_ingest = types.SimpleNamespace(_switch_state_cache={"switch-x943fm": {"S1-x943fm": "on"}})
    ctrl._advanced_active_actions = {}
    ctrl._advanced_delay_due = {}
    state = {"Fan": True}
    calls = []

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "auto-hg7mpdm267i": {
                "enabled": True,
                "script_json": {
                    "name": "Daily Timer",
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "07:00", "end": "17:00"}],
                    "actions": [{"switch_key": "switch-x943fm::S1-x943fm", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }
    ctrl.data_logger = types.SimpleNamespace(
        local_tz=saiSwitch.ZoneInfo("America/Denver"),
        get_last_switch_events=lambda *_a, **_k: [
            ("On", "2026-03-31T10:56:45.407794-06:00", "mqtt-nodus-state"),
            ("Off", "2026-03-31T04:05:26.295025-06:00", "manual/ui"),
        ],
    )

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force, event_source))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == []


def test_advanced_action_can_do_nothing_after_delay(monkeypatch: pytest.MonkeyPatch):
    monotonic_now = {"value": 200.0}
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: monotonic_now["value"])

    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "do_nothing", "delay_s": 5}],
                },
            }
        }
    }
    calls = []
    state = {"Fan": False}

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []
    assert ctrl._advanced_delay_due
    assert ctrl._advanced_revert_cooldown == set()

    monotonic_now["value"] = 206.0
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]


def test_advanced_do_nothing_keeps_state_when_rule_stops_matching():
    ctrl = _make_controller()
    state = {"Fan": False}
    calls = []
    rule_on = {"value": True}

    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00" if rule_on["value"] else "00:01"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "do_nothing", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]

    rule_on["value"] = False
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]


def test_advanced_action_invalid_revert_action_defaults_to_do_nothing(monkeypatch: pytest.MonkeyPatch):
    monotonic_now = {"value": 300.0}
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: monotonic_now["value"])

    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "bogus", "delay_s": 5}],
                },
            }
        }
    }
    calls = []
    state = {"Fan": False}

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []
    assert ctrl._advanced_delay_due
    assert ctrl._advanced_revert_cooldown == set()


def test_timer_rule_turns_switch_off_when_window_ends(monkeypatch: pytest.MonkeyPatch):
    monotonic_now = {"value": 400.0}
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: monotonic_now["value"])

    real_strftime = time.strftime
    local_now = {"value": time.struct_time((2026, 3, 28, 10, 0, 30, 5, 87, -1))}
    monkeypatch.setattr(saiSwitch.time, "localtime", lambda: local_now["value"])
    monkeypatch.setattr(
        saiSwitch.time,
        "strftime",
        lambda fmt, tm: f"{tm.tm_hour:02d}:{tm.tm_min:02d}" if fmt == "%H:%M" else real_strftime(fmt, tm),
    )

    ctrl = _make_controller()
    state = {"Fan": False}
    calls = []
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [
                        {"type": "time", "start": "00:00", "end": "00:00"},
                        {"type": "timer", "duration_min": 5, "freq_hours": 1},
                    ],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]

    local_now["value"] = time.struct_time((2026, 3, 28, 10, 5, 30, 5, 87, -1))
    monotonic_now["value"] = 405.0
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False), ("Fan", False, True)]


def test_timer_rule_uses_anchor_epoch_for_minute_periods(monkeypatch: pytest.MonkeyPatch):
    monotonic_now = {"value": 500.0}
    epoch_now = {"value": 1000.0}
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: monotonic_now["value"])
    monkeypatch.setattr(saiSwitch.time, "time", lambda: epoch_now["value"])

    real_strftime = time.strftime
    local_now = {"value": time.struct_time((2026, 4, 18, 10, 7, 0, 5, 108, -1))}
    monkeypatch.setattr(saiSwitch.time, "localtime", lambda: local_now["value"])
    monkeypatch.setattr(
        saiSwitch.time,
        "strftime",
        lambda fmt, tm: f"{tm.tm_hour:02d}:{tm.tm_min:02d}" if fmt == "%H:%M" else real_strftime(fmt, tm),
    )

    ctrl = _make_controller()
    state = {"Fan": False}
    calls = []
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [
                        {"type": "time", "start": "00:00", "end": "00:00"},
                        {"type": "timer", "duration_min": 4, "period_min": 15, "anchor_epoch": 1000},
                    ],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False)]

    epoch_now["value"] = 1241.0
    monotonic_now["value"] = 504.0
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True, False), ("Fan", False, True)]


def test_timer_rule_rejects_duration_equal_to_period(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(saiSwitch.time, "monotonic", lambda: 700.0)

    real_strftime = time.strftime
    local_now = time.struct_time((2026, 4, 18, 11, 0, 0, 5, 108, -1))
    monkeypatch.setattr(saiSwitch.time, "localtime", lambda: local_now)
    monkeypatch.setattr(
        saiSwitch.time,
        "strftime",
        lambda fmt, tm: f"{tm.tm_hour:02d}:{tm.tm_min:02d}" if fmt == "%H:%M" else real_strftime(fmt, tm),
    )

    ctrl = _make_controller()
    state = {"Fan": False}
    calls = []
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule1": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [
                        {"type": "time", "start": "00:00", "end": "00:00"},
                        {"type": "timer", "duration_min": 15, "period_min": 15, "anchor_epoch": 1000},
                    ],
                    "actions": [{"switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state", "delay_s": 0}],
                },
            }
        }
    }

    def _fake_set_state(label, desired, force=False, event_source="manual/ui"):
        calls.append((label, desired, force))
        state[label] = desired
        return True

    ctrl.set_state = _fake_set_state
    ctrl.get_state = lambda label: state[label]

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []


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


def test_eval_astral_condition_window_modes(monkeypatch: pytest.MonkeyPatch):
    class FakeLocationInfo:
        def __init__(self, **_kwargs):
            self.observer = object()

    ctrl = _make_controller()
    ctrl._resolve_astral_location = lambda: {"lat": 40.0, "lon": -105.0, "tz": "UTC"}

    monkeypatch.setattr(saiSwitch, "LocationInfo", FakeLocationInfo)

    def _sun_daytime(_observer, date=None, tzinfo=None):
        now = datetime.now(tzinfo)
        return {
            "sunrise": now - timedelta(hours=2),
            "sunset": now + timedelta(hours=2),
        }

    monkeypatch.setattr(saiSwitch, "_astral_sun", _sun_daytime)
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunrise_to_sunset", "offset_min": 0},
    ) is True
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunset_to_sunrise", "offset_min": 0},
    ) is False

    def _sun_nighttime(_observer, date=None, tzinfo=None):
        now = datetime.now(tzinfo)
        return {
            "sunrise": now + timedelta(hours=2),
            "sunset": now - timedelta(hours=2),
        }

    monkeypatch.setattr(saiSwitch, "_astral_sun", _sun_nighttime)
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunrise_to_sunset", "offset_min": 0},
    ) is False
    assert SwitchController._eval_astral_condition(
        ctrl,
        {"type": "astral", "astral_event": "sunset_to_sunrise", "offset_min": 0},
    ) is True


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

    monkeypatch.setitem(sys.modules, "sensorius.saiSettings", types.SimpleNamespace(saiSettings=FakeSettings))

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
    ctrl.set_state = lambda label, desired, **_kwargs: calls.append((label, desired))

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == [("Fan", True)]


def test_advanced_rule_detects_biodynamic_from_to_transition(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule-bd": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{
                        "type": "bd_transitions",
                        "executor_switch_id": "sw1",
                    }],
                    "actions": [{
                        "switch_key": "sw1::Fan",
                        "set": True,
                        "delay_s": 0,
                    }],
                },
            }
        }
    }
    segments = iter([
        {
            "transition_at": "2026-07-28T08:00:00-06:00",
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
            "color": "#f19707",
            "accent": "#d64b3b",
        },
        {
            "transition_at": "2026-07-28T10:30:00-06:00",
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
            "color": "#e5b172",
            "accent": "#644817",
        },
    ])
    monkeypatch.setattr(
        ctrl,
        "_get_current_biodynamic_transition",
        lambda: next(segments),
    )
    broadcasts = []
    monkeypatch.setattr(
        ctrl,
        "_broadcast_biodynamic_transition",
        lambda transition: broadcasts.append(transition),
    )
    ctrl.get_state = lambda _label: False
    calls = []
    ctrl.set_state = lambda label, desired, **_kwargs: calls.append((label, desired))

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    assert calls == []
    assert broadcasts == []

    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert calls == [("Fan", True)]
    assert broadcasts == [{
        "transition_at": "2026-07-28T10:30:00-06:00",
        "from": {
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
            "color": "#f19707",
            "accent": "#d64b3b",
        },
        "to": {
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
            "color": "#e5b172",
            "accent": "#644817",
        },
    }]


def test_biodynamic_none_action_generates_toast_without_relay_or_email(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "rule-bd-none": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{
                        "type": "bd_transitions",
                        "executor_switch_id": "sw1",
                    }],
                    "actions": [{
                        "type": "none",
                        "executor_switch_id": "sw1",
                    }],
                },
            }
        }
    }
    segments = iter([
        {
            "transition_at": "2026-07-28T08:00:00-06:00",
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
        },
        {
            "transition_at": "2026-07-28T10:30:00-06:00",
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
    ])
    monkeypatch.setattr(
        ctrl,
        "_get_current_biodynamic_transition",
        lambda: next(segments),
    )
    broadcasts = []
    monkeypatch.setattr(
        ctrl,
        "_broadcast_biodynamic_transition",
        lambda transition: broadcasts.append(transition),
    )
    ctrl.set_state = lambda *_args, **_kwargs: pytest.fail(
        "None actor must not change a relay"
    )
    monkeypatch.setattr(
        saiSwitch,
        "SMTPEmailSender",
        lambda: pytest.fail("None actor must not create an email sender"),
    )

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert len(broadcasts) == 1
    assert broadcasts[0]["from"]["sign"] == "Aries"
    assert broadcasts[0]["to"]["sign"] == "Taurus"


def test_legacy_empty_bd_switch_action_is_treated_as_none_on_hub_controller(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    ctrl._load_triggers_dict = lambda: {
        "Advanced": {
            "legacy-bd-none": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "conditions": [{"type": "bd_transitions"}],
                    "actions": [{
                        "type": "switch",
                        "switch_key": "",
                        "set": False,
                    }],
                },
            }
        }
    }
    monkeypatch.setattr(saiSwitch.socket, "gethostname", lambda: "sw1")
    segments = iter([
        {
            "transition_at": "2026-07-28T08:00:00-06:00",
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
        },
        {
            "transition_at": "2026-07-29T00:00:00-06:00",
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
    ])
    monkeypatch.setattr(
        ctrl,
        "_get_current_biodynamic_transition",
        lambda: next(segments),
    )
    broadcasts = []
    monkeypatch.setattr(
        ctrl,
        "_broadcast_biodynamic_transition",
        lambda transition: broadcasts.append(transition),
    )
    ctrl.set_state = lambda *_args, **_kwargs: pytest.fail(
        "Legacy BD None compatibility must not change a relay"
    )

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert len(broadcasts) == 1
    assert broadcasts[0]["to"]["element"] == "Earth"


def test_biodynamic_transition_email_has_from_to_subject_and_body():
    ctrl = _make_controller()
    transition = {
        "transition_at": "2026-07-28T10:30:00-06:00",
        "from": {
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
        },
        "to": {
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
    }
    condition = {
        "type": "bd_transitions",
        "_bd_transition": transition,
    }

    subject, body = SwitchController._build_automation_notification(
        ctrl,
        rule_id="rule-bd",
        rule_name="BD Notice",
        triggered=True,
        evaluated_groups=[{
            "result": True,
            "conditions": [(condition, True)],
        }],
        actions=[{"type": "notify", "to": "grower@example.com"}],
        current_values_map={},
    )

    assert "BD Transition" in subject
    assert "Aries / Fire / Fruit to Taurus / Earth / Root" in subject
    assert "Jul 28, 2026 10:30 AM" in subject
    assert "From Aries / Fire / Fruit; To Taurus / Earth / Root" in body


def test_biodynamic_notify_sends_transition_without_cleared_followup(
    monkeypatch: pytest.MonkeyPatch,
):
    ctrl = _make_controller()
    rule_doc = {
        "Advanced": {
            "rule-bd": {
                "enabled": True,
                "script_json": {
                    "enabled": True,
                    "name": "BD Notice",
                    "conditions": [{
                        "type": "bd_transitions",
                        "executor_switch_id": "sw1",
                    }],
                    "actions": [{
                        "type": "notify",
                        "to": "grower@example.com",
                        "executor_switch_id": "sw1",
                    }],
                },
            }
        }
    }
    ctrl._load_triggers_dict = lambda: rule_doc
    segments = iter([
        {
            "transition_at": "2026-07-28T08:00:00-06:00",
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
        },
        {
            "transition_at": "2026-07-28T10:30:00-06:00",
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
        {
            "transition_at": "2026-07-28T10:30:00-06:00",
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
    ])
    monkeypatch.setattr(
        ctrl,
        "_get_current_biodynamic_transition",
        lambda: next(segments),
    )
    monkeypatch.setattr(ctrl, "_broadcast_biodynamic_transition", lambda _event: None)
    sent = []

    class FakeSender:
        def send(self, subject, body, to_addresses=()):
            sent.append((subject, body, to_addresses))

    class ImmediateThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(saiSwitch, "SMTPEmailSender", FakeSender)
    monkeypatch.setattr(saiSwitch.threading, "Thread", ImmediateThread)

    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, {})
    SwitchController._evaluate_and_apply_advanced(ctrl, {})

    assert len(sent) == 1
    assert "Aries / Fire / Fruit to Taurus / Earth / Root" in sent[0][0]
    assert sent[0][2] == ("grower@example.com",)


def test_biodynamic_transition_broadcast_reaches_runtime_app(
    monkeypatch: pytest.MonkeyPatch,
):
    from sensorius import saiWebRoutes

    ctrl = _make_controller()
    received = []

    async def fake_broadcast(payload):
        received.append(payload)

    runtime_app = types.SimpleNamespace(
        state=types.SimpleNamespace(switch_broadcast=fake_broadcast)
    )
    monkeypatch.setattr(saiWebRoutes, "app", runtime_app)
    transition = {
        "transition_at": "2026-07-28T10:30:00-06:00",
        "from": {
            "sign": "Aries",
            "element": "Fire",
            "plant_part": "Fruit",
        },
        "to": {
            "sign": "Taurus",
            "element": "Earth",
            "plant_part": "Root",
        },
    }

    async def run_broadcast():
        SwitchController._broadcast_biodynamic_transition(ctrl, transition)
        await asyncio.sleep(0)

    asyncio.run(run_broadcast())

    assert received == [{
        "type": "bd_transition",
        **transition,
    }]


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
    ctrl.set_state = lambda label, desired, **_kwargs: calls.append((label, desired))

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
        __import__("sensorius.saiSwitchFactory", fromlist=["*"]),
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
