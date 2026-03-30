"""Pytest coverage for MQTT ingest authentication selection.

These tests verify the ingest layer chooses the correct shared or split MQTT
credentials when Home Assistant and Sensorius broker settings differ.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if "paho" not in sys.modules:
    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod = types.ModuleType("paho.mqtt.client")
    mqtt_pkg.client = mqtt_client_mod
    mqtt_client_mod.Client = object
    paho_mod.mqtt = mqtt_pkg
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client_mod

import saiMQTTIngest as ingest_mod


class _FakeClient:
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.auth = None
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)


class _FakeSettings:
    def __init__(self, values):
        self.values = values

    def get_setting(self, section, key, default=None, **_kwargs):
        return self.values.get((section, key), default)

    @staticmethod
    def deobfuscate_secret(value):
        return value


def test_shared_client_prefers_homeassistant_credentials(monkeypatch):
    monkeypatch.setattr(ingest_mod.mqtt, "Client", _FakeClient)
    settings = _FakeSettings(
        {
            ("HomeAssistant", "HA_BROKER"): "broker.local",
            ("HomeAssistant", "HA_MQTTPORT"): 1883,
            ("HomeAssistant", "HA_USERNAME"): "ha-user",
            ("HomeAssistant", "HA_PASSWORD"): "ha-pass",
            ("MQTT", "USERNAME"): "",
            ("MQTT", "PASSWORD"): "",
        }
    )

    ingest = ingest_mod.saiMQTTIngest(
        broker="broker.local",
        settings=settings,
        data_logger=object(),
    )

    assert ingest.ha_client is ingest.client
    assert ingest.client.auth == ("ha-user", "ha-pass")


def test_split_clients_keep_distinct_auth_sections(monkeypatch):
    monkeypatch.setattr(ingest_mod.mqtt, "Client", _FakeClient)
    settings = _FakeSettings(
        {
            ("HomeAssistant", "HA_BROKER"): "ha-broker.local",
            ("HomeAssistant", "HA_MQTTPORT"): 1883,
            ("HomeAssistant", "HA_USERNAME"): "ha-user",
            ("HomeAssistant", "HA_PASSWORD"): "ha-pass",
            ("MQTT", "USERNAME"): "mqtt-user",
            ("MQTT", "PASSWORD"): "mqtt-pass",
        }
    )

    ingest = ingest_mod.saiMQTTIngest(
        broker="ingest-broker.local",
        settings=settings,
        data_logger=object(),
    )

    assert ingest.ha_client is not ingest.client
    assert ingest.client.auth == ("mqtt-user", "mqtt-pass")
    assert ingest.ha_client.auth == ("ha-user", "ha-pass")
