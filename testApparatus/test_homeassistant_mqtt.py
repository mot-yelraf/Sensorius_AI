"""Pytest coverage for Home Assistant MQTT bridge behavior.

These tests validate switch command handling and metadata refresh behavior for
the Home Assistant bridge integration.
"""

import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiHomeAssistantMqtt import HomeAssistantTopicMap, rPiHomeAssistantBridge


class _FakeSettings:
    def get_setting(self, section, key, default=None):
        if section == "HomeAssistant" and key == "ENABLED":
            return True
        return default


class _FakeMqttClients:
    def __init__(self):
        self.text_publishes = []

    def publish_text(self, topic, payload, qos=0, retain=False):
        self.text_publishes.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
        return True

    def publish_json(self, topic, payload, qos=0, retain=False, use_ha_client=True):
        return True

    def subscribe(self, topic, callback, qos=0):
        return True


class _FakeController:
    def __init__(self):
        self.switch_id = "switch-main"
        self.channel_id_for_label = {"Fan": "serialXYZ_ch1"}
        self.calls = []

    def set_state(self, label, desired_on, force=False):
        self.calls.append((label, desired_on, force))


class _FakeDataLogger:
    pass


def test_switch_command_refreshes_index_after_label_rename():
    ctrl = _FakeController()
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=_FakeMqttClients(),
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={"primary": ctrl},
        data_logger=_FakeDataLogger(),
    )

    # Simulate runtime rename while preserving stable channel_id.
    ctrl.channel_id_for_label = {"Exhaust Fan": "serialXYZ_ch1"}

    asyncio.run(bridge._handle_switch_command_async("switch-main", "serialXYZ_ch1", True))

    assert ctrl.calls == [("Exhaust Fan", True, True)]
