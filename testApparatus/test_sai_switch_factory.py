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

import saiSwitchFactory
from saiSwitchFactory import MQTTSwitch


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
