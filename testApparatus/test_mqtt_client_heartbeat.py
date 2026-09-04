"""Pytest coverage for MQTT client heartbeat and liveness publishing.

These tests verify last-will setup, online or offline heartbeat publication, and
degraded-health signaling during connectivity failures.
"""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if "paho" not in sys.modules:
    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod_stub = types.ModuleType("paho.mqtt.client")
    mqtt_client_mod_stub.Client = object
    mqtt_pkg.client = mqtt_client_mod_stub
    paho_mod.mqtt = mqtt_pkg
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client_mod_stub

import sensorius.saiMQTTClient as mqtt_client_mod

class _FakePahoClient:
    def __init__(self, client_id=None, clean_session=True):
        self.client_id = client_id
        self.clean_session = clean_session
        self.on_connect = None
        self.on_disconnect = None
        self.connected = False
        self.pubs = []
        self.wills = []

    def username_pw_set(self, username, password=None):
        return

    def loop_start(self):
        return

    def loop_stop(self):
        return

    def connect(self, *_args, **_kwargs):
        self.connected = True
        return 0

    def disconnect(self):
        self.connected = False
        return

    def is_connected(self):
        return bool(self.connected)

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload, qos, retain))
        return types.SimpleNamespace(rc=0)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.wills.append((topic, payload, qos, retain))
        return


class _FakeSettings:
    def __init__(self):
        self.device_id = "apvpd-test123"
        self.values = {
            ("SensorNetwork", "BROKER"): "broker.local",
            ("MQTT", "BASE_TOPIC"): "nodus",
            ("MQTT", "HEARTBEAT_INTERVAL_S"): 30,
            ("HomeAssistant", "BASE_TOPIC"): "nodus",
            ("HomeAssistant", "PUBLISH_STATE_RETAIN"): True,
        }
        self.broker = "broker.local"

    def get_setting(self, section, key, default=None, **_kwargs):
        return self.values.get((section, key), default)

    @staticmethod
    def deobfuscate_secret(value):
        return value


class _FakeSensor:
    sensor_id = "apvpd-test123"
    publish_interval = 30
    meas_interval = 30
    location = "Test"

    @staticmethod
    def current_data_set():
        return ({"temp": 21.1}, "online", 1760000000)


def _build_client(monkeypatch):
    monkeypatch.setattr(mqtt_client_mod.mqtt, "Client", _FakePahoClient)
    return mqtt_client_mod.saiMQTTClient(_FakeSensor(), _FakeSettings())


def test_reconnect_configures_lwt_and_publishes_online_heartbeat(monkeypatch):
    c = _build_client(monkeypatch)
    asyncio.run(c.mqtt_reconnect())

    assert c.client.wills, "LWT should be configured before connect"
    will_topic, _, _, will_retain = c.client.wills[-1]
    assert will_topic == "nodus/apvpd-test123/status/heartbeat"
    assert will_retain is True

    assert c.client.pubs, "Reconnect should force online heartbeat publish"
    pub_topic, pub_payload, _, pub_retain = c.client.pubs[-1]
    assert pub_topic == "nodus/apvpd-test123/status/heartbeat"
    assert pub_retain is True
    data = json.loads(pub_payload)
    assert data["device_id"] == "apvpd-test123"
    assert data["status"] == "online"
    assert data["heartbeat_interval_s"] == 30


def test_reconnect_offloads_blocking_connect(monkeypatch):
    c = _build_client(monkeypatch)
    offloaded = []

    async def _fake_to_thread(func, *args, **kwargs):
        offloaded.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(mqtt_client_mod.asyncio, "to_thread", _fake_to_thread)
    asyncio.run(c.mqtt_reconnect())

    assert offloaded
    assert offloaded[0][0] == c.client.connect
    assert offloaded[0][1] == ("broker.local", 1883)


def test_close_publishes_offline_heartbeat(monkeypatch):
    c = _build_client(monkeypatch)
    asyncio.run(c.mqtt_reconnect())
    c.client.pubs.clear()
    c.close()

    assert c.client.pubs, "close() should publish an offline heartbeat before disconnect"
    _, payload, _, retain = c.client.pubs[-1]
    assert retain is True
    data = json.loads(payload)
    assert data["status"] == "offline"


def test_mqtt_loop_publishes_degraded_when_dns_unhealthy(monkeypatch):
    c = _build_client(monkeypatch)
    asyncio.run(c.mqtt_reconnect())

    c._last_dns_check_ts = 0.0
    c._last_dns_signal_ok = False
    c._dns_check_interval_s = 0.0
    monkeypatch.setattr(c, "_dns_signal_ok", lambda: False)

    call_count = {"n": 0}

    async def _sleep_once(_sec):
        call_count["n"] += 1
        if call_count["n"] >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(mqtt_client_mod.asyncio, "sleep", _sleep_once)

    try:
        asyncio.run(c.mqtt_loop())
    except asyncio.CancelledError:
        pass

    assert c.client.pubs, "mqtt_loop should publish heartbeat when connected"
    _, payload, _, _ = c.client.pubs[-1]
    data = json.loads(payload)
    assert data["status"] == "degraded"
