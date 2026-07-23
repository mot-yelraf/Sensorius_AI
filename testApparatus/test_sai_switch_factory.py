"""Pytest coverage for switch backend creation and board detection.

This module verifies relay-board probing, settings repair, MQTT switch topic
behavior, and local identity backfill logic.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if "board" not in sys.modules:
    sys.modules["board"] = types.SimpleNamespace(D5=5, D12=12, D23=23, D27=27)
if "digitalio" not in sys.modules:
    sys.modules["digitalio"] = types.SimpleNamespace(
        DigitalInOut=object,
        Direction=types.SimpleNamespace(INPUT=0, OUTPUT=1),
        Pull=types.SimpleNamespace(UP=1, DOWN=0),
    )

import sensorius.saiSwitchFactory as saiSwitchFactory
from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
from sensorius.saiSwitchFactory import MQTTSwitch


class _MsgInfo:
    def __init__(self, rc=0):
        self.rc = rc


class _FakeMQTT:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def publish(self, topic, payload, *args, **kwargs):
        self.calls.append((topic, payload))
        return _MsgInfo(self.rc)


def test_detect_switch_variant_marks_fallback_unconfident(monkeypatch):
    monkeypatch.setattr(saiSwitchFactory, "_probe_grounded", lambda _pin: False)
    d = saiSwitchFactory.detect_switch_variant()
    assert d["detected"] is False
    assert d["template"] == "switch_3_relay"


def test_ensure_switch_settings_does_not_retarget_on_unconfident_probe(monkeypatch):
    class _Mgr:
        def __init__(self, *_args):
            self.retargeted = False

        def get_path(self, _sid):
            return types.SimpleNamespace(exists=lambda: True)

        def load(self, _sid):
            return {
                "Switch": {
                    "SWITCH_EN_PIN": 23,
                    "SWITCH_ACTIVE": "high",
                    "SWITCH_1_PIN": 26,
                }
            }

        def retarget_to_template(self, *_args):
            self.retargeted = True

        def update_setting(self, *_args):
            return None

    mgr = _Mgr()
    monkeypatch.setattr(saiSwitchFactory, "SwitchSettingsManager", lambda *_args, **_kwargs: mgr)
    monkeypatch.setattr(
        saiSwitchFactory,
        "detect_switch_variant",
        lambda: {"template": "switch_3_relay", "en_bcm": 5, "active": "low", "channels": 3, "detected": False},
    )

    saiSwitchFactory.ensure_switch_settings_for_host("hub-1")
    assert mgr.retargeted is False


def test_ensure_switch_settings_retargets_on_confident_probe(monkeypatch):
    class _Mgr:
        def __init__(self, *_args):
            self.retargeted = False

        def get_path(self, _sid):
            return types.SimpleNamespace(exists=lambda: True)

        def load(self, _sid):
            return {
                "Switch": {
                    "SWITCH_EN_PIN": 23,
                    "SWITCH_ACTIVE": "high",
                    "SWITCH_1_PIN": 26,
                }
            }

        def retarget_to_template(self, *_args):
            self.retargeted = True

        def update_setting(self, *_args):
            return None

    mgr = _Mgr()
    monkeypatch.setattr(saiSwitchFactory, "SwitchSettingsManager", lambda *_args, **_kwargs: mgr)
    monkeypatch.setattr(
        saiSwitchFactory,
        "detect_switch_variant",
        lambda: {"template": "switch_3_relay", "en_bcm": 5, "active": "low", "channels": 3, "detected": True},
    )

    saiSwitchFactory.ensure_switch_settings_for_host("hub-1")
    assert mgr.retargeted is True


def test_mqtt_switch_uses_channel_id_topic_and_checks_rc():
    sw = MQTTSwitch(
        settings={
            "Switch": {
                "SWITCH_DEVICE_ID": "switch-1",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-abc",
            }
        },
        mqtt_client=_FakeMQTT(rc=0),
    )
    assert sw.set_state("Fan", True) is True
    assert sw.mqtt.calls[-1] == ("nodus/S1-abc/set", "ON")

    sw_fail = MQTTSwitch(
        settings={
            "Switch": {
                "SWITCH_DEVICE_ID": "switch-1",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-abc",
            }
        },
        mqtt_client=_FakeMQTT(rc=2),
    )
    assert sw_fail.set_state("Fan", True) is False


def test_mqtt_switch_requires_channel_id_and_does_not_fallback_to_slug_topic():
    sw = MQTTSwitch(
        settings={
            "Switch": {
                "SWITCH_DEVICE_ID": "switch-1",
                "SWITCH_1_LABEL": "Grow Light",
            }
        },
        mqtt_client=_FakeMQTT(rc=0),
    )
    assert sw.set_state("Grow Light", False) is False
    assert sw.mqtt.calls == []


def test_mqtt_switch_accepts_switch_n_en_install_markers():
    sw = MQTTSwitch(
        settings={
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": "switch-1",
                "SWITCH_1_LABEL": "Desk Fan",
                "SWITCH_1_CHANNEL_ID": "S1-abc",
                "SWITCH_1_EN": "5",
                "SWITCH_2_LABEL": "Humidifier",
                "SWITCH_2_CHANNEL_ID": "S2-abc",
                "SWITCH_2_EN": "",
            }
        },
        mqtt_client=_FakeMQTT(rc=0),
    )
    assert sw.get_switch_names() == ["Desk Fan"]


def test_local_switch_channel_ids_are_repaired_from_placeholder_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mgr = SwitchSettingsManager("switch_settings")
    mgr.save(
        "hub-1",
        {
            "Switch": {
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "hub-1",
                "SWITCH_LOCATION": "Shelf",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-",
                "SWITCH_2_LABEL": "Light",
                "SWITCH_2_CHANNEL_ID": "S2-",
            }
        },
    )

    assert mgr.ensure_channel_ids_for_switch("hub-1") is True

    sw = mgr.load("hub-1")["Switch"]
    serial = str(sw["DEVICE_SERIAL_NUM"])
    assert len(serial) == 6
    assert sw["SWITCH_1_CHANNEL_ID"] == f"S1-{serial}"
    assert sw["SWITCH_2_CHANNEL_ID"] == f"S2-{serial}"


def test_local_sensor_serial_backfills_from_persisted_host_serial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sw_mgr = SwitchSettingsManager("switch_settings")
    sw_mgr.save(
        "hub-1",
        {
            "Switch": {
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "hub-1",
                "DEVICE_SERIAL_NUM": "abc123",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-abc123",
            }
        },
    )

    sensor_mgr = SensorSettingsManager("sensor_settings")
    sensor_mgr.seed_from_factory(
        sensor_id="avpd-i2c-0-hub-1",
        device="avpd",
        location="Shelf",
        serial_num="",
    )

    sensor = sensor_mgr.load("avpd-i2c-0-hub-1")["Sensor"]
    assert sensor["SERIAL_NUM"] == "abc123"


def test_switch_event_migration_preserves_old_key_reads(tmp_path):
    db = saiDataLogger(str(tmp_path / "sensorius.db"))
    db.upsert_switch_identity(
        switch_key="S1-::Fan",
        switch_id="hub-1",
        label="Fan",
        location="Shelf",
    )
    db.log_switch_event("S1-::Fan", True, source="test", sensor_id="Switch_hub-1")

    moved = db.migrate_switch_keys({"S1-::Fan": "hub-1::S1-abc123"})
    db.upsert_switch_identity(
        switch_key="hub-1::S1-abc123",
        switch_id="hub-1",
        label="Fan",
        location="Shelf",
    )

    assert moved >= 1
    assert db.get_latest_switch_state("hub-1::S1-abc123") == "On"
    assert db.get_latest_switch_state("hub-1::Fan") == "On"
