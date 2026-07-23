"""Pytest coverage for Home Assistant MQTT bridge behavior.

These tests validate switch command handling and metadata refresh behavior for
the Home Assistant bridge integration.
"""

import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.saiHomeAssistantMqtt import (
    HomeAssistantTopicMap,
    build_sensor_metric_discovery_payload,
    rPiHomeAssistantBridge,
)


class _FakeSettings:
    def get_setting(self, section, key, default=None):
        if section == "HomeAssistant" and key == "ENABLED":
            return True
        return default


class _FakeMqttClients:
    def __init__(self):
        self.text_publishes = []
        self.device_type = {}
        self.liveness = {}

    def publish_text(self, topic, payload, qos=0, retain=False):
        self.text_publishes.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
        return True

    def get_nodus_liveness(self, device_id, *, device_type=None):
        return self.liveness.get(device_id, {"state": "unknown"})

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
    def get_switch_identities(self):
        return []


def _sensor_discovery_payload_for(metric_name):
    _, payload = build_sensor_metric_discovery_payload(
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        sensor_id="co2-ph244",
        sensor_name="co2-ph244",
        metric_name=metric_name,
    )
    return payload


@pytest.mark.parametrize(
    "metric_name,unit,device_class,state_class",
    [
        ("CO2", "ppm", "carbon_dioxide", "measurement"),
        ("Rel-Humidity", "%", "humidity", "measurement"),
        ("Humidity", "g/m³", "absolute_humidity", "measurement"),
        ("Temperature", "°C", "temperature", "measurement"),
        ("Temperature_F", "°F", "temperature", "measurement"),
        ("Dew Point", "°C", "temperature", "measurement"),
        ("Dew Point_F", "°F", "temperature", "measurement"),
        ("Dew Point Deficit", "°C", "temperature_delta", "measurement"),
        ("DewVPD Risk", "%", None, "measurement"),
        ("Ambient VPD", "kPa", None, "measurement"),
        ("Baro-Pressure", "hPa", "atmospheric_pressure", "measurement"),
        ("Plant Rel-Humidity", "%", "humidity", "measurement"),
        ("Plant VPD", "kPa", None, "measurement"),
        ("Wind Speed", "mph", "wind_speed", "measurement"),
        ("Wind Direction", "°", "wind_direction", "measurement_angle"),
        ("Rain Last 24h", "in", "precipitation", "measurement"),
        ("Rain Rate", "in/h", "precipitation_intensity", "measurement"),
        ("Soil EC", "mS/cm", "conductivity", "measurement"),
        ("PM2.5", "µg/m³", "pm25", "measurement"),
    ],
)
def test_sensor_discovery_publishes_metric_units(metric_name, unit, device_class, state_class):
    payload = _sensor_discovery_payload_for(metric_name)

    assert payload["unit_of_measurement"] == unit
    assert payload["state_class"] == state_class
    if device_class is None:
        assert "device_class" not in payload
    else:
        assert payload["device_class"] == device_class


def test_sensor_discovery_metadata_handles_legacy_dewvpd_alias():
    payload = _sensor_discovery_payload_for("dewVPD Risk")

    assert payload["unit_of_measurement"] == "%"
    assert "device_class" not in payload
    assert 'value_json.values.get("dewVPD Risk")' in payload["value_template"]
    assert 'value_json.values.get("DewVPD Risk")' in payload["value_template"]


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


def test_nodus_sensor_discovery_availability_uses_liveness_offline():
    mqtt = _FakeMqttClients()
    mqtt.device_type["aqi-test123"] = "nodus"
    mqtt.liveness["aqi-test123"] = {"state": "offline"}
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=mqtt,
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={},
        data_logger=_FakeDataLogger(),
    )

    assert bridge._sensor_availability_for_discovery("aqi-test123") == "offline"


def test_nodus_sensor_discovery_unknown_startup_does_not_publish_offline():
    mqtt = _FakeMqttClients()
    mqtt.device_type["aqi-test123"] = "nodus"
    mqtt.liveness["aqi-test123"] = {"state": "unknown"}
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=mqtt,
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={},
        data_logger=_FakeDataLogger(),
    )

    assert bridge._sensor_availability_for_discovery("aqi-test123") == "online"


def test_local_switch_discovery_availability_stays_online_without_nodus_liveness():
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=_FakeMqttClients(),
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={},
        data_logger=_FakeDataLogger(),
    )

    assert bridge._switch_availability_for_discovery("sensoria-hub-0") == "online"


def test_nodus_liveness_degraded_publishes_ha_online():
    mqtt = _FakeMqttClients()
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=mqtt,
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={},
        data_logger=_FakeDataLogger(),
    )

    bridge.handle_nodus_liveness_change(
        "aqi-test123",
        "degraded",
        {"state": "degraded", "peer_ids": ["aqi-test123"]},
    )

    assert mqtt.text_publishes[-1] == {
        "topic": "sensorius/sensor/aqi-test123/availability",
        "payload": "online",
        "qos": 0,
        "retain": True,
    }


def test_nodus_liveness_unknown_does_not_publish_ha_availability():
    mqtt = _FakeMqttClients()
    bridge = rPiHomeAssistantBridge(
        mqtt_clients=mqtt,
        settings=_FakeSettings(),
        topic_map=HomeAssistantTopicMap(node_id="node-1"),
        switch_controllers={},
        data_logger=_FakeDataLogger(),
    )

    bridge.handle_nodus_liveness_change(
        "aqi-test123",
        "unknown",
        {"state": "unknown", "peer_ids": ["aqi-test123"]},
    )

    assert mqtt.text_publishes == []
