"""Pytest coverage for MQTT ingest liveness and topic registration behavior.

This module verifies background HTTP metadata defaults, topic subscriptions,
heartbeat handling, and calibration topic tracking in the ingest layer.
"""

import asyncio
import json
import os
import sys
import types
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if "paho" not in sys.modules:
    paho_mod = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_client_mod = types.ModuleType("paho.mqtt.client")
    mqtt_client_mod.Client = object
    mqtt_pkg.client = mqtt_client_mod
    paho_mod.mqtt = mqtt_pkg
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client_mod

import saiMQTTIngest as ingest_mod
import saiSensorSettingsManager
import saiSettings
import saiSwitchSettingsManager


class _FakeClient:
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subs = []
        self.pubs = []

    def username_pw_set(self, username, password=None):
        return

    def subscribe(self, topic, qos=0):
        self.subs.append((topic, qos))
        return (0, 1)

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload, qos, retain))
        return types.SimpleNamespace(rc=0)

    def connect(self, *_args, **_kwargs):
        return 0

    def loop_start(self):
        return

    def disconnect(self):
        return

    def loop_stop(self, force=False):
        return


class _FakeSettings:
    def __init__(self, values=None, sections=None):
        self.values = values or {}
        self.sections = sections or {}

    def get_setting(self, section, key, default=None, **_kwargs):
        return self.values.get((section, key), default)

    def get_section(self, name, reload_if_changed=False):
        return self.sections.get(name, {})

    @staticmethod
    def deobfuscate_secret(value):
        return value


class _Logger:
    def __init__(self):
        self.switch_identities = []
        self.sensors = set()
        self.readings = []
        self.switch_events = []
        self.pruned_switch_identity_calls = []

    def log_readings(self, *args, **kwargs):
        self.readings.append((args, kwargs))
        return

    def log_switch_event(self, *, switch_key, is_on, timestamp=None, source=None, sensor_id=None):
        self.switch_events.append(
            {
                "switch_key": switch_key,
                "is_on": bool(is_on),
                "timestamp": timestamp,
                "source": source,
                "sensor_id": sensor_id,
            }
        )
        return

    def register_sensor(self, sensor_id):
        self.sensors.add(sensor_id)

    def upsert_switch_identity(self, *, switch_key, switch_id, label, location=None):
        self.switch_identities.append(
            {
                "switch_key": switch_key,
                "switch_id": switch_id,
                "label": label,
                "location": location,
            }
        )

    def get_switch_channel_id(self, switch_id, label):
        want_sid = str(switch_id or "").strip().lower()
        want_label = str(label or "").strip().lower()
        for row in self.switch_identities:
            rsid = str(row.get("switch_id", "") or "").strip().lower()
            rlabel = str(row.get("label", "") or "").strip().lower()
            if rsid == want_sid and rlabel == want_label:
                channel_id = str(row.get("channel_id", "") or "").strip()
                if channel_id:
                    return channel_id
                switch_key = str(row.get("switch_key", "") or "").strip()
                if "::" in switch_key:
                    return switch_key.split("::", 1)[0].strip()
        return None

    def get_switch_identities(self):
        return list(self.switch_identities)

    def prune_switch_identities(self, *, switch_id: str, valid_channel_ids):
        valid = {str(cid or "").strip() for cid in (valid_channel_ids or []) if str(cid or "").strip()}
        self.pruned_switch_identity_calls.append(
            {"switch_id": switch_id, "valid_channel_ids": sorted(valid)}
        )
        kept = []
        for row in self.switch_identities:
            if str(row.get("switch_id", "") or "").strip() != str(switch_id or "").strip():
                kept.append(row)
                continue
            switch_key = str(row.get("switch_key", "") or "").strip()
            channel_id = switch_key.split("::", 1)[0].strip() if "::" in switch_key else ""
            if channel_id in valid:
                kept.append(row)
        removed = len(self.switch_identities) - len(kept)
        self.switch_identities = kept
        return removed

    def get_latest_switch_state(self, switch_key: str, sensor_id: str | None = None):
        want_key = str(switch_key or "").strip().lower()
        want_sensor = None if sensor_id is None else str(sensor_id).strip().lower()
        for row in reversed(self.switch_events):
            row_key = str(row.get("switch_key", "") or "").strip().lower()
            row_sensor = None if row.get("sensor_id") is None else str(row.get("sensor_id")).strip().lower()
            if row_key != want_key:
                continue
            if want_sensor is not None and row_sensor != want_sensor:
                continue
            return "On" if row.get("is_on") else "Off"
        return None

    def get_latest_switch_state_by_source_prefix(self, switch_key: str, *, source_prefix: str, sensor_id: str | None = None):
        want_key = str(switch_key or "").strip().lower()
        want_prefix = str(source_prefix or "").strip().lower()
        want_sensor = None if sensor_id is None else str(sensor_id).strip().lower()
        for row in reversed(self.switch_events):
            row_key = str(row.get("switch_key", "") or "").strip().lower()
            row_source = str(row.get("source", "") or "").strip().lower()
            row_sensor = None if row.get("sensor_id") is None else str(row.get("sensor_id")).strip().lower()
            if row_key != want_key:
                continue
            if want_sensor is not None and row_sensor != want_sensor:
                continue
            if not row_source.startswith(want_prefix):
                continue
            return "On" if row.get("is_on") else "Off"
        return None


class _Msg:
    def __init__(self, topic, payload, retain=False, qos=0):
        self.topic = topic
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.retain = retain
        self.qos = qos


def _build_ingest(monkeypatch, *, sections=None, values=None):
    monkeypatch.setattr(ingest_mod.mqtt, "Client", _FakeClient)
    merged_values = {
        ("HomeAssistant", "BASE_TOPIC"): "sensorius",
        ("HomeAssistant", "HA_BROKER"): "broker.local",
        ("HomeAssistant", "HA_MQTTPORT"): 1883,
    }
    if values:
        merged_values.update(values)
    settings = _FakeSettings(values=merged_values, sections=sections or {})
    return ingest_mod.saiMQTTIngest(
        broker="broker.local",
        settings=settings,
        data_logger=_Logger(),
    )


def test_background_http_meta_discovery_defaults_off(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    assert ingest._allow_background_http_meta_discovery() is False


def test_background_http_meta_discovery_can_be_enabled(monkeypatch):
    ingest = _build_ingest(
        monkeypatch,
        values={("SensorNetwork", "BACKGROUND_HTTP_META_DISCOVERY"): True},
    )
    assert ingest._allow_background_http_meta_discovery() is False


def test_registered_topics_include_heartbeat(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    assert "nodus/+/status/heartbeat" in ingest.registered_topics
    assert "nodus/+/meta" in ingest.registered_topics
    assert "nodus/+/calibration/ack" in ingest.registered_topics
    assert "nodus/+/calibration/result" in ingest.registered_topics
    assert "nodus/+/event/calibration_status" in ingest.registered_topics
    assert "nodus/+/event/calibration_sample" in ingest.registered_topics
    assert "sensorius/nodus/+/onboard/hello" in ingest.registered_topics
    assert "sensorius/nodus/+/meta" in ingest.registered_topics
    assert "sensorius/nodus/+/config/ack" in ingest.registered_topics
    assert "sensorius/nodus/+/config/result" in ingest.registered_topics
    assert "sensorius/nodus/+/event/calibration_sample" in ingest.registered_topics
    assert "sensorius/nodus/+/event/calibration_result" in ingest.registered_topics


def test_debug_data_only_registered_topics(monkeypatch):
    ingest = _build_ingest(
        monkeypatch,
        values={("SensorNetwork", "NODUS_DEBUG_DATA_ONLY"): True},
    )
    assert "nodus/+/data" in ingest.registered_topics
    assert "sensorius/nodus/+/data" in ingest.registered_topics
    assert "nodus/+/status/heartbeat" not in ingest.registered_topics
    assert "nodus/+/meta" not in ingest.registered_topics
    assert "nodus/+/calibration/ack" not in ingest.registered_topics
    assert "sensorius/nodus/+/onboard/hello" not in ingest.registered_topics


def test_meta_does_not_add_redundant_exact_data_subscription_when_wildcard_exists(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    meta = {
        "serial": "ykdvea",
        "sensor": {
            "sensor_id": "apvpd-test123",
            "location": "Lab",
            "data_topic": "nodus/apvpd-test123/data",
            "event_topic": "nodus/apvpd-test123/event",
            "availability_topic": "nodus/apvpd-test123/availability",
        },
    }

    meta_valid, subscribed = ingest._parse_and_subscribe_from_nodus_meta(
        meta,
        topic_device_id="apvpd-test123",
        retain=False,
    )

    assert meta_valid is True
    assert subscribed is True
    assert ("nodus/apvpd-test123/data", 0) not in ingest.client.subs
    assert "nodus/apvpd-test123/data" not in ingest.registered_topics
    assert ingest.nodus_sensor_topics["apvpd-test123"] == "nodus/apvpd-test123/data"


def test_publish_nodus_calibration_uses_mqtt_command_topic(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    result = ingest.publish_nodus_calibration("apvpd-test123", action="apply", payload={"offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}]})
    assert result["ok"] is True
    assert result["topic"] == "nodus/apvpd-test123/calibration/set"
    topic, payload, qos, retain = ingest.client.pubs[-1]
    assert topic == "nodus/apvpd-test123/calibration/set"
    body = json.loads(payload)
    assert body["action"] == "apply"
    assert body["payload"]["offsets"][0]["key"] == "Calibration.Device.TEMP_OFFSET"
    assert qos == 1
    assert retain is False


def test_calibration_sample_topics_are_tracked_by_sensor_and_message(monkeypatch):
    ingest = _build_ingest(monkeypatch)

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/soil-123/event/calibration_sample",
            json.dumps(
                {
                    "message_id": "soil-ph-1",
                    "sensor_id": "soil-123",
                    "sample_index": 2,
                    "sample_count": 4,
                    "reference_ph": 7.0,
                    "status": "ok",
                    "values": {"Soil-pH": 6.81},
                    "soil_ph_offset": 0.2,
                    "corrected_ph": 7.01,
                    "raw_ph": 6.81,
                }
            ),
            retain=False,
        ),
    )

    snapshot = ingest.get_nodus_calibration_state("soil-123")
    assert snapshot is not None
    assert snapshot["sample"]["message_id"] == "soil-ph-1"
    assert snapshot["sample"]["raw_ph"] == 6.81
    assert snapshot["progress"]["sample_index"] == 2
    assert snapshot["progress"]["sample_total"] == 4
    assert ingest.calibration_samples_by_message["soil-ph-1"][0]["sensor_id"] == "soil-123"


def test_set_switch_leaves_remote_cache_unchanged_until_confirmation(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.nodus_switch_command_topics[("sw1", "S1-sw1")] = "nodus/S1-sw1/set"

    ok = ingest.set_switch("sw1", "Fan", False, event_origin="manual")

    assert ok is True
    assert ingest._pending_set.get(("sw1", "Fan")) == {
        "ts": ingest._pending_set[("sw1", "Fan")]["ts"],
        "state": False,
        "channel_id": "S1-sw1",
        "event_origin": "manual",
        "event_label": "",
    }
    assert ingest._switch_state_cache == {}


def test_confirmed_nodus_event_persists_after_optimistic_cache_update(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.nodus_switch_command_topics[("sw1", "S1-sw1")] = "nodus/S1-sw1/set"
    ingest.nodus_switch_topic_map["nodus/S1-sw1/event"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "event",
    }

    assert ingest.set_switch("sw1", "Fan", False, event_origin="manual") is True

    ingest.handle_nodus_switch_topic(
        "nodus/S1-sw1/event",
        json.dumps({"event": {"SWITCH_1": "off"}, "source": "mqtt", "timestamp": 1773318167}),
    )

    assert ingest.data_logger.switch_events[-1]["switch_key"] == "S1-sw1::Fan"
    assert ingest.data_logger.switch_events[-1]["is_on"] is False
    assert ingest.data_logger.switch_events[-1]["source"] == "mqtt-manual"
    assert ingest.data_logger.switch_events[-1]["timestamp"] is None


def test_confirmed_nodus_event_uses_pending_rule_name_for_auto_source(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.nodus_switch_command_topics[("sw1", "S1-sw1")] = "nodus/S1-sw1/set"
    ingest.nodus_switch_topic_map["nodus/S1-sw1/event"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "event",
    }

    assert ingest.set_switch("sw1", "Fan", False, event_origin="auto", event_label="Desk Cooldown") is True

    ingest.handle_nodus_switch_topic(
        "nodus/S1-sw1/event",
        json.dumps({"event": {"SWITCH_1": "off"}, "source": "mqtt", "timestamp": 1773318167}),
    )

    assert ingest.data_logger.switch_events[-1]["source"] == "mqtt-auto:Desk Cooldown"


def test_confirmed_nodus_state_persists_even_after_manual_ui_off(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.data_logger.log_switch_event(
        switch_key="S1-sw1::Fan",
        is_on=True,
        source="mqtt-nodus-state",
        sensor_id="Switch_sw1",
    )
    ingest.data_logger.log_switch_event(
        switch_key="S1-sw1::Fan",
        is_on=False,
        source="manual/ui",
        sensor_id="Switch_sw1",
    )
    ingest.nodus_switch_topic_map["nodus/S1-sw1/state"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "state",
    }

    ingest.handle_nodus_switch_topic("nodus/S1-sw1/state", "OFF")

    assert ingest.data_logger.switch_events[-1]["switch_key"] == "S1-sw1::Fan"
    assert ingest.data_logger.switch_events[-1]["is_on"] is False
    assert ingest.data_logger.switch_events[-1]["source"] == "mqtt-nodus-state"


def test_confirmed_nodus_state_broadcasts_live_switch_event(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.nodus_switch_topic_map["nodus/S1-sw1/state"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "state",
    }

    pushed = []

    async def _fake_broadcast(payload):
        pushed.append(payload)

    monkeypatch.setitem(
        sys.modules,
        "saiWebRoutes",
        types.SimpleNamespace(
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(switch_broadcast=_fake_broadcast)
            )
        ),
    )
    ingest._schedule_coro = lambda coro: asyncio.run(coro)

    ingest.handle_nodus_switch_topic("nodus/S1-sw1/state", "OFF")

    assert pushed
    assert pushed[-1]["type"] == "switch_event"
    assert pushed[-1]["key"] == "sw1::Fan"
    assert pushed[-1]["state"] is False
    assert pushed[-1]["source"] == "mqtt-nodus"


def test_nodus_switch_json_timestamp_is_ignored_for_persist(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest.nodus_switch_topic_map["nodus/S1-sw1/event"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "event",
    }

    ingest.handle_nodus_switch_topic(
        "nodus/S1-sw1/event",
        json.dumps({"event": {"SWITCH_1": "off"}, "source": "mqtt", "timestamp": 946685474}),
    )

    assert ingest.data_logger.switch_events[-1]["switch_key"] == "S1-sw1::Fan"
    assert ingest.data_logger.switch_events[-1]["timestamp"] is None


def test_nodus_event_topic_is_history_only_and_does_not_override_live_state(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-sw1::Fan",
            "switch_id": "sw1",
            "label": "Fan",
            "channel_id": "S1-sw1",
            "location": "lab",
        }
    ]
    ingest._switch_state_cache["sw1"] = {"S1-sw1": "on", "Fan": "on"}
    ingest._pending_set[("sw1", "Fan")] = {
        "ts": time.time(),
        "state": False,
        "channel_id": "S1-sw1",
    }
    ingest.nodus_switch_topic_map["nodus/S1-sw1/event"] = {
        "switch_id": "sw1",
        "channel_id": "S1-sw1",
        "label": "Fan",
        "kind": "event",
    }

    ingest.handle_nodus_switch_topic(
        "nodus/S1-sw1/event",
        json.dumps({"event": {"SWITCH_1": "off"}, "source": "mqtt"}),
    )

    assert ingest.data_logger.switch_events[-1]["switch_key"] == "S1-sw1::Fan"
    assert ingest.data_logger.switch_events[-1]["is_on"] is False
    assert ingest._switch_state_cache["sw1"]["S1-sw1"] == "on"
    assert ingest._switch_state_cache["sw1"]["Fan"] == "on"
    assert ("sw1", "Fan") in ingest._pending_set


def test_calibration_topics_update_state_caches(monkeypatch):
    ingest = _build_ingest(monkeypatch)

    ingest._on_message(
        ingest.client,
        None,
        _Msg("nodus/apvpd-test123/calibration/ack", json.dumps({"message_id": "cal-1", "accepted": True}), retain=False),
    )
    ack = ingest.calibration_ack_by_message.get("cal-1")
    assert ack is not None
    assert ack["accepted"] is True

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/apvpd-test123/calibration/result",
            json.dumps(
                {
                    "message_id": "cal-1",
                    "applied": True,
                    "updated": 2,
                    "status": {
                        "sensor_id": "apvpd-test123",
                        "status": "calibrated",
                        "calibrated": True,
                        "temp_offset": 1.25,
                        "rh_offset": -2.5,
                    },
                    "error": "",
                }
            ),
            retain=False,
        ),
    )
    result = ingest.calibration_result_by_message.get("cal-1")
    assert result is not None
    assert result["applied"] is True
    assert result["status"]["sensor_id"] == "apvpd-test123"
    assert ingest.calibration_status_by_sensor["apvpd-test123"]["status"] == "calibrated"

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/apvpd-test123/event/calibration_progress",
            json.dumps(
                {
                    "sensor_id": "apvpd-test123",
                    "status": "in_progress",
                    "sample_index": 2,
                    "sample_total": 5,
                }
            ),
            retain=False,
        ),
    )
    snapshot = ingest.get_nodus_calibration_state("apvpd-test123")
    assert snapshot is not None
    assert snapshot["progress"]["sample_index"] == 2

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/apvpd-test123/event/calibration_result",
            json.dumps(
                {
                    "sensor_id": "apvpd-test123",
                    "status": "success",
                    "calibrated": True,
                    "temp_offset": 1.25,
                    "rh_offset": -2.5,
                }
            ),
            retain=True,
        ),
    )
    snapshot = ingest.get_nodus_calibration_state("apvpd-test123")
    assert snapshot is not None
    assert snapshot["result"]["calibrated"] is True


def test_nodus_meta_materializes_switch_mappings(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "version": "v1.2.3",
            "serial": "abc123",
            "sensor": {
                "sensor_id": "apvpd-test123",
                "location": "Veg Tent",
                "data_topic": "nodus/apvpd-test123/data",
                "availability_topic": "nodus/apvpd-test123/availability",
                "display_metrics": ["Temperature", "Rel-Humidity", "Temperature", "Ambient VPD"],
            },
            "switch": {
                "switch_device_id": "switch-test123",
                "location": "Veg Tent",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-test123",
                        "state": "OFF",
                        "event_topic": "nodus/S1-test123/event",
                        "state_topic": "nodus/S1-test123/state",
                        "set_topic": "nodus/S1-test123/set",
                        "availability_topic": "nodus/S1-test123/availability",
                    }
                ],
            },
            "location_group": {"location": "Veg Tent", "members": ["apvpd-test123", "switch-test123"]},
        }
    )
    msg = _Msg("nodus/apvpd-test123/meta", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    meta = ingest.nodus_switch_topic_map.get("nodus/S1-test123/state")
    assert meta is not None
    assert meta.get("switch_id") == "switch-test123"
    assert meta.get("channel_id") == "S1-test123"
    assert ingest.nodus_switch_command_topics.get(("switch-test123", "S1-test123")) == "nodus/S1-test123/set"
    assert ingest.device_location.get("nodus/S1-test123/state") == "Veg Tent"
    assert "apvpd-test123" in ingest.host_to_peer_ids.get("apvpd-test123", [])
    assert ingest.expected_gauge_map.get("apvpd-test123") == ["Temperature", "Rel-Humidity", "Ambient VPD"]
    assert ingest.get_nodus_firmware_version("apvpd-test123", device_type="sensor") == "v1.2.3"
    assert ingest.get_nodus_firmware_version("switch-test123", device_type="switch") == "v1.2.3"


def test_switch_firmware_version_falls_back_to_combo_host_suffix(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.nodus_firmware_versions["apvpd-test123"] = "v2.0.1"

    assert ingest.get_nodus_firmware_version("switch-test123", device_type="switch") == "v2.0.1"


def test_nodus_meta_uses_top_level_serial_and_sensor_id_prefix_for_shadow_identity(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    shadow_dir = sensor_root / "avpd-j21vxj"
    shadow_dir.mkdir()
    shadow_path = shadow_dir / "sensor.toml"
    shadow_path.write_text(
        "\n".join(
            [
                "[Sensor]",
                'TYPE = "nodus"',
                'DEVICE = "nodus"',
                'SERIAL_NUM = ""',
                'SENSOR_ID = "avpd-j21vxj"',
                'LOCATION = "Unknown"',
                "",
                "[Display]",
                'METRIC_1 = ""',
                'METRIC_2 = ""',
                'METRIC_3 = ""',
                'METRIC_4 = ""',
                'METRIC_5 = ""',
                'METRIC_6 = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )

    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "type": "nodus",
            "hostname": "avpd-j21vxj",
            "device_id": "avpd-j21vxj",
            "serial": "j21vxj",
            "sensor": {
                "sensor_id": "avpd-j21vxj",
                "location": "Unknown",
                "display_metrics": ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure"],
                "availability_topic": "nodus/avpd-j21vxj/availability",
                "data_topic": "nodus/avpd-j21vxj/data",
                "event_topic": "nodus/avpd-j21vxj/event",
            },
            "switch": {"device_id": "", "channels": [], "location": "Unknown"},
            "location_group": {"location": "Unknown", "members": ["avpd-j21vxj"]},
        }
    )

    ingest._on_message(ingest.client, None, _Msg("nodus/avpd-j21vxj/meta", payload, retain=True))

    saved = shadow_path.read_text(encoding="utf-8")
    assert 'DEVICE = "avpd"' in saved
    assert 'SERIAL_NUM = "j21vxj"' in saved
    assert 'METRIC_1 = "Ambient VPD"' in saved


def test_debug_data_only_ignores_meta(monkeypatch):
    ingest = _build_ingest(
        monkeypatch,
        values={("SensorNetwork", "NODUS_DEBUG_DATA_ONLY"): True},
    )
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "sensor": {"sensor_id": "apvpd-test123", "data_topic": "nodus/apvpd-test123/data"},
            "switch": {
                "switch_device_id": "switch-test123",
                "channels": [{"index": 1, "label": "Fan", "channel_id": "S1-test123", "state_topic": "nodus/S1-test123/state"}],
            },
        }
    )
    ingest._on_message(ingest.client, None, _Msg("nodus/apvpd-test123/meta", payload, retain=True))
    assert ingest.nodus_switch_topic_map == {}
    assert ingest.nodus_switch_command_topics == {}


def test_nodus_meta_accepts_switch_device_id_alias(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": "apvpd-test123",
            "sensor": {
                "sensor_id": "apvpd-test123",
                "location": "TestLab",
                "data_topic": "nodus/apvpd-test123/data",
                "availability_topic": "nodus/apvpd-test123/availability",
            },
            "switch": {
                "device_id": "switch-test123",
                "location": "TestLab",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-test123",
                        "state": False,
                        "event_topic": "nodus/S1-test123/event",
                        "state_topic": "nodus/S1-test123/state",
                        "set_topic": "nodus/S1-test123/set",
                        "availability_topic": "nodus/S1-test123/availability",
                    }
                ],
            },
        }
    )
    msg = _Msg("nodus/apvpd-test123/meta", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.nodus_switch_topic_map.get("nodus/S1-test123/state", {}).get("switch_id") == "switch-test123"
    assert ingest.nodus_switch_command_topics.get(("switch-test123", "S1-test123")) == "nodus/S1-test123/set"


def test_http_itaot_meta_normalizes_to_topic_contract(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = {
        "schema": "itaot-meta/v1",
        "device_id": "apvpd-test123",
        "sensor": {
            "sensor_id": "apvpd-test123",
            "location": "TestLab",
        },
        "switch": {
            "device_id": "switch-test123",
            "location": "TestLab",
            "channels": [
                {"index": 1, "label": "Fan", "channel_id": "S1-test123", "state": False},
                {"index": 2, "label": "Light", "channel_id": "S2-test123", "state": False},
            ],
        },
        "location_group": {"location": "TestLab", "members": ["apvpd-test123", "S1-test123", "S2-test123"]},
    }

    ok, _ = ingest._parse_and_subscribe_from_http_meta(payload, "apvpd-test123")
    assert ok is True
    assert ingest.nodus_switch_command_topics.get(("switch-test123", "S1-test123")) == "nodus/S1-test123/set"
    assert ingest.nodus_switch_command_topics.get(("switch-test123", "S2-test123")) == "nodus/S2-test123/set"
    assert ingest.nodus_switch_topic_map.get("nodus/S1-test123/state", {}).get("label") == "Fan"


def test_nodus_meta_writes_display_styles_to_sensor_toml(tmp_path, monkeypatch):
    real_sensor_manager = saiSensorSettingsManager.SensorSettingsManager
    sensor_root = tmp_path / "sensor_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    system_root.mkdir()

    class _TmpSensorSettingsManager(real_sensor_manager):
        def __init__(self, base_dir_name="sensor_settings"):
            super().__init__(str(sensor_root))

    monkeypatch.setattr(saiSensorSettingsManager, "SensorSettingsManager", _TmpSensorSettingsManager)
    monkeypatch.setattr(saiSettings.saiSettings, "DEFAULT_BASE_DIR", str(system_root), raising=False)

    ingest = _build_ingest(monkeypatch)
    payload = {
        "schema": "nodus-meta/v1",
        "device_id": "apvpd-test123",
        "sensor": {
            "sensor_id": "apvpd-test123",
            "location": "East House",
            "display_metrics": ["Air Quality", "Temperature", "Rel-Humidity"],
            "display_styles": ["graph24hr", "gauge", "invalid-style"],
            "data_topic": "nodus/apvpd-test123/data",
            "event_topic": "nodus/apvpd-test123/event",
            "availability_topic": "nodus/apvpd-test123/availability",
        },
    }

    ok, _ = ingest._parse_and_subscribe_from_http_meta(payload, "apvpd-test123")

    assert ok is True
    saved = real_sensor_manager(str(sensor_root)).load("apvpd-test123")
    assert saved["Display"]["Style"]["METRIC_1"] == "Graph24hr"
    assert saved["Display"]["Style"]["METRIC_2"] == "Gauge"
    assert saved["Display"]["Style"]["METRIC_3"] == "Graph24hr"
    assert saved["Display"]["Style"]["METRIC_4"] == "Graph24hr"


def test_set_switch_by_channel_id_prefers_channel_scoped_config_target(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.mqtt_clients = {"apvpd-test123"}
    ingest.host_to_peer_ids = {"apvpd-test123": ["apvpd-test123", "switch-test123"]}

    ok = ingest.set_switch_by_channel_id("switch-test123", "S1-test123", True)

    assert ok is True
    assert ingest.client.pubs, "expected config/set publish for remote switch toggle"
    topic, payload, qos, retain = ingest.client.pubs[0]
    assert topic == "nodus/S1-test123/config/set"
    assert qos == 1
    assert retain is False
    envelope = json.loads(payload)
    posted = ((envelope.get("payload") or {}).get("updates") or [])
    assert posted == [
        {
            "section": "Switch",
            "key": "SWITCH_1_LAST_STATE",
            "value": True,
            "name": "switch.toml",
        }
    ]


def test_set_switch_does_not_optimistically_mutate_remote_state_cache(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities.append(
        {
            "switch_id": "switch-test123",
            "switch_key": "S1-test123::Fan",
            "channel_id": "S1-test123",
            "label": "Fan",
            "location": "TestLab",
        }
    )
    ingest.mqtt_clients = {"apvpd-test123"}
    ingest.host_to_peer_ids = {"apvpd-test123": ["apvpd-test123", "switch-test123"]}

    ok = ingest.set_switch("switch-test123", "Fan", True, event_origin="manual")

    assert ok is True
    assert ingest._pending_set.get(("switch-test123", "Fan")) == {
        "ts": ingest._pending_set[("switch-test123", "Fan")]["ts"],
        "state": True,
        "channel_id": "S1-test123",
        "event_origin": "manual",
        "event_label": "",
    }
    assert ingest._switch_state_cache == {}


def test_confirmed_switch_state_clears_pending_set(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities.append(
        {
            "switch_id": "switch-test123",
            "switch_key": "S1-test123::Fan",
            "channel_id": "S1-test123",
            "label": "Fan",
            "location": "TestLab",
        }
    )
    ingest._pending_set[("switch-test123", "Fan")] = {
        "ts": time.time(),
        "state": True,
        "channel_id": "S1-test123",
        "event_origin": "manual",
        "event_label": "",
    }
    ingest.nodus_switch_topic_map["nodus/S1-test123/state"] = {
        "switch_id": "switch-test123",
        "channel_id": "S1-test123",
        "label": "Fan",
        "kind": "state",
    }

    ingest.handle_nodus_switch_topic("nodus/S1-test123/state", "ON")

    assert ingest._pending_set == {}
    assert ingest._recent_switch_origin[("switch-test123", "Fan")]["event_origin"] == "manual"


def test_confirmed_nodus_event_keeps_origin_after_state_clears_pending(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities.append(
        {
            "switch_id": "switch-test123",
            "switch_key": "S1-test123::Fan",
            "channel_id": "S1-test123",
            "label": "Fan",
            "location": "TestLab",
        }
    )
    ingest._pending_set[("switch-test123", "Fan")] = {
        "ts": time.time(),
        "state": False,
        "channel_id": "S1-test123",
        "event_origin": "auto",
        "event_label": "Desk Cooldown",
    }
    ingest.nodus_switch_topic_map["nodus/S1-test123/state"] = {
        "switch_id": "switch-test123",
        "channel_id": "S1-test123",
        "label": "Fan",
        "kind": "state",
    }
    ingest.nodus_switch_topic_map["nodus/S1-test123/event"] = {
        "switch_id": "switch-test123",
        "channel_id": "S1-test123",
        "label": "Fan",
        "kind": "event",
    }

    ingest.handle_nodus_switch_topic("nodus/S1-test123/state", "OFF")
    ingest.handle_nodus_switch_topic(
        "nodus/S1-test123/event",
        json.dumps({"event": {"SWITCH_1": "off"}, "source": "mqtt"}),
    )

    assert ingest.data_logger.switch_events[-1]["source"] == "mqtt-auto:Desk Cooldown"


def test_retained_stale_heartbeat_sets_unknown(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    stale_ts = int(time.time()) - 300
    payload = json.dumps(
        {
            "device_id": "apvpd-test123",
            "status": "online",
            "timestamp": stale_ts,
            "heartbeat_interval_s": 30,
        }
    )
    msg = _Msg("nodus/apvpd-test123/status/heartbeat", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("apvpd-test123") == "unknown"
    assert ingest.heartbeat_stale.get("apvpd-test123") is True
    assert "apvpd-test123" not in ingest.last_heartbeat_ts


def test_fresh_heartbeat_sets_online_and_ts(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ts_now = int(time.time())
    payload = json.dumps(
        {
            "device_id": "apvpd-test123",
            "status": "online",
            "timestamp": ts_now,
            "heartbeat_interval_s": 30,
        }
    )
    msg = _Msg("nodus/apvpd-test123/status/heartbeat", payload, retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("apvpd-test123") == "online"
    assert ingest.heartbeat_stale.get("apvpd-test123") is False
    assert int(ingest.last_heartbeat_ts.get("apvpd-test123", 0)) == ts_now


def test_heartbeat_timeout_state_transitions(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    now_ts = time.time()
    ingest.heartbeat_interval_s_by_host["apvpd-test123"] = 30.0

    ingest.last_heartbeat_ts["apvpd-test123"] = now_ts - 20.0
    assert ingest._apply_heartbeat_timeout_state("apvpd-test123", now_ts=now_ts) == "online"

    ingest.last_heartbeat_ts["apvpd-test123"] = now_ts - 70.0
    assert ingest._apply_heartbeat_timeout_state("apvpd-test123", now_ts=now_ts) == "degraded"

    ingest.last_heartbeat_ts["apvpd-test123"] = now_ts - 95.0
    assert ingest._apply_heartbeat_timeout_state("apvpd-test123", now_ts=now_ts) == "offline"


def test_recovery_via_data_marks_online_with_stale_heartbeat(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    msg = _Msg("nodus/apvpd-test123/data", json.dumps({"values": {"Temperature": 21.2}}), retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("apvpd-test123") == "online"
    assert ingest.heartbeat_stale.get("apvpd-test123") is True


def test_debug_data_only_data_path_does_not_mark_heartbeat_stale(monkeypatch):
    ingest = _build_ingest(
        monkeypatch,
        values={("SensorNetwork", "NODUS_DEBUG_DATA_ONLY"): True},
    )
    msg = _Msg("nodus/apvpd-test123/data", json.dumps({"values": {"Temperature": 21.2}}), retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("apvpd-test123") == "online"
    assert "apvpd-test123" not in ingest.heartbeat_stale


def test_live_nodus_data_updates_expected_gauges_from_display_metrics(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    msg = _Msg(
        "nodus/avpd-j21vxj/data",
        json.dumps(
            {
                "values": {
                    "Temperature": 21.2,
                    "Rel-Humidity": 55.1,
                    "Humidity": 10.5,
                    "Dew-Point": 11.1,
                    "Ambient VPD": 1.04,
                    "Baro-Pressure": 850.4,
                },
                "display_metrics": [
                    "Ambient VPD",
                    "Temperature",
                    "Rel-Humidity",
                    "Baro-Pressure",
                    "Ambient VPD",
                    "Baro-Pressure",
                ],
                "bcc_fault": False,
                "free_mem": 123456,
            }
        ),
        retain=False,
    )

    ingest._on_message(ingest.client, None, msg)

    assert ingest.expected_gauge_map.get("avpd-j21vxj") == [
        "Ambient VPD",
        "Temperature",
        "Rel-Humidity",
        "Baro-Pressure",
    ]
    assert ingest.latest_meta.get("avpd-j21vxj", {}).get("display_metrics") == [
        "Ambient VPD",
        "Temperature",
        "Rel-Humidity",
        "Baro-Pressure",
    ]


def test_existing_manual_nodus_shadow_settings_are_backfilled_from_remote_display_metrics(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    shadow_dir = sensor_root / "avpd-j21vxj"
    shadow_dir.mkdir()
    shadow_path = shadow_dir / "sensor.toml"
    shadow_path.write_text(
        "\n".join(
            [
                "[Sensor]",
                'TYPE = "nodus"',
                'DEVICE = "nodus"',
                'SERIAL_NUM = ""',
                'SENSOR_ID = "avpd-j21vxj"',
                'LOCATION = "Unknown"',
                "",
                "[Display]",
                'METRIC_1 = ""',
                'METRIC_2 = ""',
                'METRIC_3 = ""',
                'METRIC_4 = ""',
                'METRIC_5 = ""',
                'METRIC_6 = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )

    ingest._ensure_settings_from_itaot(
        {"HOSTNAME": "avpd-j21vxj"},
        "avpd-j21vxj",
        [
            {
                "sensor_id": "avpd-j21vxj",
                "device_type": "nodus",
                "device": "avpd",
                "sensor_type": "nodus",
                "location": "Unknown",
                "serial": "j21vxj",
                "display_metrics": [
                    "Ambient VPD",
                    "Temperature",
                    "Rel-Humidity",
                    "Baro-Pressure",
                ],
            }
        ],
        [],
    )

    saved = shadow_path.read_text(encoding="utf-8")
    assert 'DEVICE = "avpd"' in saved
    assert 'SERIAL_NUM = "j21vxj"' in saved
    assert 'METRIC_1 = "Ambient VPD"' in saved
    assert 'METRIC_2 = "Temperature"' in saved
    assert 'METRIC_3 = "Rel-Humidity"' in saved
    assert 'METRIC_4 = "Baro-Pressure"' in saved


def test_nodus_shadow_seed_uses_nodus_aligned_defaults_when_display_metrics_missing(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    ingest._ensure_settings_from_itaot(
        {"HOSTNAME": "apvpd-test123"},
        "apvpd-test123",
        [
            {
                "sensor_id": "apvpd-test123",
                "device_type": "nodus",
                "device": "aqi",
                "sensor_type": "nodus",
                "location": "Unknown",
                "serial": "123",
            }
        ],
        [],
    )

    saved = (sensor_root / "apvpd-test123" / "sensor.toml").read_text(encoding="utf-8")
    assert 'METRIC_1 = "Air Quality"' in saved
    assert 'METRIC_2 = "Temperature"' in saved
    assert 'METRIC_3 = "Rel-Humidity"' in saved
    assert 'METRIC_4 = "Ambient VPD"' in saved
    assert 'METRIC_5 = "Dewpoint Deficit"' in saved
    assert 'METRIC_6 = "dewVPD Risk"' in saved


def test_nodus_meta_updates_existing_local_shadow_tomls_from_meta_payload(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    system_dir = system_root / "apvpd-test123"
    system_dir.mkdir()
    (system_dir / "settings.toml").write_text(
        "\n".join(
            [
                "[Network]",
                'SSID = "OldWifi"',
                'PASSWORD = "old-pass"',
                'HOSTNAME = "apvpd-test123"',
                "",
                "[Profile]",
                'ACTIVE_PROFILE = "nodusweb"',
                "",
                "[MQTT]",
                'BROKER = "old-broker"',
                "PORT = 1883",
                "USE_TLS = true",
                'BASE_TOPIC = "old-topic"',
                'USERNAME = "old-user"',
                'PASSWORD = "old-mqtt-pass"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    sensor_dir = sensor_root / "apvpd-test123"
    sensor_dir.mkdir()
    (sensor_dir / "sensor.toml").write_text(
        "\n".join(
            [
                "[Sensor]",
                'TYPE = "nodus"',
                'DEVICE = "nodus"',
                'SERIAL_NUM = ""',
                'SENSOR_ID = "apvpd-test123"',
                'LOCATION = "Unknown"',
                "",
                "[Display]",
                'METRIC_1 = "Old 1"',
                'METRIC_2 = "Old 2"',
                'METRIC_3 = ""',
                'METRIC_4 = ""',
                'METRIC_5 = ""',
                'METRIC_6 = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )

    switch_dir = switch_root / "switch-test123"
    switch_dir.mkdir()
    (switch_dir / "switch.toml").write_text(
        "\n".join(
            [
                "[Switch]",
                'TYPE = "nodus"',
                'DEVICE = "switch"',
                'DEVICE_SERIAL_NUM = ""',
                'SWITCH_DEVICE_ID = "switch-test123"',
                'SWITCH_LOCATION = "Unknown"',
                'SWITCH_1_LABEL = "Relay 1"',
                'SWITCH_1_CHANNEL_ID = "S1-old"',
                'SWITCH_1_ENABLE_PIN = ""',
                "SWITCH_1_LAST_STATE = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": "apvpd-test123",
            "hostname": "apvpd-test123",
            "serial": "ykdvea",
            "type": "nodus",
            "capabilities": {"sensor": True, "switch": True},
            "network": {
                "ssid": "ExampleWiFi",
                "password": "obf1:BASE64NONCE:BASE64CIPHER",
                "hostname": "apvpd-test123",
            },
            "profile": {"active_profile": "sensorius"},
            "mqtt": {
                "broker": "sensorius.local",
                "port": 1883,
                "use_tls": False,
                "username": "",
                "password": "obf1:BASE64NONCE:BASE64CIPHER",
                "base_topic": "nodus",
            },
            "sensor": {
                "sensor_id": "apvpd-test123",
                "location": "Lab",
                "display_metrics": ["CO2", "Temperature", "Rel-Humidity"],
                "data_topic": "nodus/apvpd-test123/data",
                "event_topic": "nodus/apvpd-test123/event",
                "availability_topic": "nodus/apvpd-test123/availability",
            },
            "status": {"heartbeat_topic": "nodus/apvpd-test123/status/heartbeat"},
            "switch": {
                "device_id": "switch-test123",
                "serial": "ykdvea",
                "location": "Lab",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-test123",
                        "enable_pin": "GP5",
                        "pin": "GP28",
                        "state": False,
                        "event_topic": "nodus/S1-test123/event",
                        "state_topic": "nodus/S1-test123/state",
                        "set_topic": "nodus/S1-test123/set",
                        "availability_topic": "nodus/S1-test123/availability",
                    }
                ],
            },
            "location_group": {"location": "Lab", "members": ["apvpd-test123", "S1-test123"]},
            "timestamp": 1763859546,
        }
    )

    ingest._on_message(ingest.client, None, _Msg("nodus/apvpd-test123/meta", payload, retain=True))

    settings_saved = (system_dir / "settings.toml").read_text(encoding="utf-8")
    assert 'SSID = "ExampleWiFi"' in settings_saved
    assert 'PASSWORD = "obf1:BASE64NONCE:BASE64CIPHER"' in settings_saved
    assert 'ACTIVE_PROFILE = "sensorius"' in settings_saved
    assert 'BROKER = "sensorius.local"' in settings_saved
    assert 'USE_TLS = false' in settings_saved
    assert 'BASE_TOPIC = "nodus"' in settings_saved

    sensor_saved = (sensor_dir / "sensor.toml").read_text(encoding="utf-8")
    assert 'DEVICE = "apvpd"' in sensor_saved
    assert 'SERIAL_NUM = "ykdvea"' in sensor_saved
    assert 'LOCATION = "Lab"' in sensor_saved
    assert 'METRIC_1 = "CO2"' in sensor_saved
    assert 'METRIC_2 = "Temperature"' in sensor_saved
    assert 'METRIC_3 = "Rel-Humidity"' in sensor_saved
    assert 'METRIC_4 = ""' in sensor_saved

    switch_saved = (switch_dir / "switch.toml").read_text(encoding="utf-8")
    assert 'DEVICE_SERIAL_NUM = "ykdvea"' in switch_saved
    assert 'SWITCH_LOCATION = "Lab"' in switch_saved
    assert 'SWITCH_1_LABEL = "Fan"' in switch_saved
    assert 'SWITCH_1_CHANNEL_ID = "S1-test123"' in switch_saved
    assert 'SWITCH_1_ENABLE_PIN = "GP5"' in switch_saved
    assert 'SWITCH_1_PIN = "GP28"' in switch_saved
    assert 'SWITCH_1_LAST_STATE = false' in switch_saved
    assert 'SWITCH_1_EN = "1"' not in switch_saved


def test_ensure_settings_from_itaot_overwrites_shadow_locations_when_payload_is_unknown(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    sensor_mgr = real_sensor_mgr(str(sensor_root))
    switch_mgr = real_switch_mgr(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Lab",
            }
        },
    )
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Lab",
            }
        },
    )

    ingest._ensure_settings_from_itaot(
        {"HOSTNAME": "apvpd-test123"},
        "apvpd-test123",
        [
            {
                "sensor_id": "apvpd-test123",
                "device_type": "nodus",
                "device": "co2",
                "sensor_type": "nodus",
                "location": "Unknown",
                "serial": "ykdvea",
                "display_metrics": ["CO2", "Temperature"],
            }
        ],
        [
            {
                "switch_id": "switch-test123",
                "switch_location": "Unknown",
                "switch_type": "nodus",
                "serial": "ykdvea",
                "switch_payload": {"Switch": {}},
            }
        ],
    )

    sensor_saved = sensor_mgr.load("apvpd-test123")
    switch_saved = switch_mgr.load("switch-test123")
    assert sensor_saved["Sensor"]["LOCATION"] == "Unknown"
    assert switch_saved["Switch"]["SWITCH_LOCATION"] == "Unknown"


def test_ensure_settings_from_itaot_resets_sensor_display_metrics_to_meta_defaults(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    sensor_mgr = real_sensor_mgr(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Room A",
            },
            "Display": {
                "METRIC_1": "Old 1",
                "METRIC_2": "Old 2",
                "METRIC_3": "Old 3",
                "METRIC_4": "Old 4",
                "METRIC_5": "Old 5",
                "METRIC_6": "Old 6",
            },
        },
    )

    ingest._ensure_settings_from_itaot(
        {"HOSTNAME": "apvpd-test123"},
        "apvpd-test123",
        [
            {
                "sensor_id": "apvpd-test123",
                "device_type": "nodus",
                "device": "aqi",
                "sensor_type": "nodus",
                "location": "Room A",
                "serial": "123",
            }
        ],
        [],
    )

    sensor_saved = sensor_mgr.load("apvpd-test123")
    assert sensor_saved["Display"]["METRIC_1"] == "Air Quality"
    assert sensor_saved["Display"]["METRIC_2"] == "Temperature"
    assert sensor_saved["Display"]["METRIC_3"] == "Rel-Humidity"
    assert sensor_saved["Display"]["METRIC_4"] == "Ambient VPD"
    assert sensor_saved["Display"]["METRIC_5"] == "Dewpoint Deficit"
    assert sensor_saved["Display"]["METRIC_6"] == "dewVPD Risk"


def test_nodus_meta_clears_switch_shadow_wiring_when_meta_fields_are_blank(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    switch_mgr = real_switch_mgr(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Room A",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-test123",
                "SWITCH_1_ENABLE_PIN": "GP5",
                "SWITCH_1_PIN": "GP28",
                "SWITCH_1_LAST_STATE": True,
            }
        },
    )

    ingest._ensure_settings_from_itaot(
        {"HOSTNAME": "apvpd-test123"},
        "apvpd-test123",
        [],
        [
            {
                "switch_id": "switch-test123",
                "switch_location": "Room A",
                "switch_type": "nodus",
                "serial": "123",
                "switch_payload": {
                    "Switch": {
                        "SWITCH_1_LABEL": "Fan",
                        "SWITCH_1_CHANNEL_ID": "S1-test123",
                        "SWITCH_1_ENABLE_PIN": "",
                        "SWITCH_1_PIN": "",
                        "SWITCH_1_LAST_STATE": False,
                    }
                },
            }
        ],
    )

    switch_saved = switch_mgr.load("switch-test123")
    assert switch_saved["Switch"]["SWITCH_1_ENABLE_PIN"] == ""
    assert switch_saved["Switch"]["SWITCH_1_PIN"] == ""
    assert switch_saved["Switch"]["SWITCH_1_LAST_STATE"] is False


def test_nodus_meta_reconciles_switch_shadow_and_prunes_stale_channels(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ingest.data_logger.switch_identities = [
        {
            "switch_key": "S1-test123::Fan",
            "switch_id": "switch-test123",
            "label": "Fan",
            "location": "Lab",
        },
        {
            "switch_key": "S2-ykdvea::Light",
            "switch_id": "switch-test123",
            "label": "Light",
            "location": "Lab",
        },
    ]

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    switch_dir = switch_root / "switch-test123"
    switch_dir.mkdir()
    (switch_dir / "switch.toml").write_text(
        "\n".join(
            [
                "[Switch]",
                'TYPE = "nodus"',
                'DEVICE = "switch"',
                'DEVICE_SERIAL_NUM = "ykdvea"',
                'SWITCH_DEVICE_ID = "switch-test123"',
                'SWITCH_LOCATION = "Unknown"',
                'SWITCH_1_LABEL = "Fan"',
                'SWITCH_1_CHANNEL_ID = "S1-test123"',
                'SWITCH_1_ENABLE_PIN = "GP5"',
                'SWITCH_1_PIN = "GP28"',
                "SWITCH_1_LAST_STATE = false",
                "SWITCH_1_OVERRIDE_SCRIPT = false",
                'SWITCH_1_EN = "1"',
                'SWITCH_2_LABEL = "Light"',
                'SWITCH_2_CHANNEL_ID = "S2-ykdvea"',
                'SWITCH_2_ENABLE_PIN = "GP10"',
                'SWITCH_2_PIN = "GP21"',
                "SWITCH_2_LAST_STATE = false",
                "SWITCH_2_OVERRIDE_SCRIPT = false",
                'SWITCH_2_EN = "1"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": "apvpd-test123",
            "hostname": "apvpd-test123",
            "serial": "ykdvea",
            "type": "nodus",
            "capabilities": {"sensor": True, "switch": True},
            "network": {"hostname": "apvpd-test123"},
            "profile": {"active_profile": "sensorius"},
            "mqtt": {"broker": "samhain.local", "port": 1883, "use_tls": False, "base_topic": "nodus"},
            "sensor": {
                "sensor_id": "apvpd-test123",
                "location": "Lab",
                "display_metrics": ["CO2", "Temperature", "Rel-Humidity"],
                "data_topic": "nodus/apvpd-test123/data",
                "event_topic": "nodus/apvpd-test123/event",
                "availability_topic": "nodus/apvpd-test123/availability",
            },
            "switch": {
                "device_id": "switch-test123",
                "serial": "ykdvea",
                "location": "Lab",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-test123",
                        "enable_pin": "GP5",
                        "pin": "GP28",
                        "state": False,
                        "event_topic": "nodus/S1-test123/event",
                        "state_topic": "nodus/S1-test123/state",
                        "set_topic": "nodus/S1-test123/set",
                        "availability_topic": "nodus/S1-test123/availability",
                    }
                ],
            },
            "location_group": {"location": "Lab", "members": ["apvpd-test123", "S1-test123"]},
            "timestamp": 1763859546,
        }
    )

    ingest._on_message(ingest.client, None, _Msg("nodus/apvpd-test123/meta", payload, retain=True))

    switch_saved = (switch_dir / "switch.toml").read_text(encoding="utf-8")
    assert 'SWITCH_LOCATION = "Lab"' in switch_saved
    assert 'SWITCH_1_ENABLE_PIN = "GP5"' in switch_saved
    assert 'SWITCH_1_PIN = "GP28"' in switch_saved
    assert 'SWITCH_1_CHANNEL_ID = "S1-test123"' in switch_saved
    assert 'SWITCH_1_EN = "1"' not in switch_saved
    assert 'SWITCH_2_EN = "1"' not in switch_saved
    assert "SWITCH_2_LABEL" not in switch_saved
    assert "SWITCH_2_CHANNEL_ID" not in switch_saved
    assert ingest.data_logger.pruned_switch_identity_calls[-1] == {
        "switch_id": "switch-test123",
        "valid_channel_ids": ["S1-test123"],
    }
    assert {row["switch_key"] for row in ingest.data_logger.get_switch_identities()} == {"S1-test123::Fan"}


def test_ensure_settings_from_itaot_parses_existing_system_toml_with_inline_comments(tmp_path, monkeypatch):
    ingest = _build_ingest(monkeypatch)

    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root = tmp_path / "system_settings"
    sensor_root.mkdir()
    switch_root.mkdir()
    system_root.mkdir()

    real_sensor_mgr = saiSensorSettingsManager.SensorSettingsManager
    real_switch_mgr = saiSwitchSettingsManager.SwitchSettingsManager
    real_settings_cls = saiSettings.saiSettings

    monkeypatch.setattr(
        saiSensorSettingsManager,
        "SensorSettingsManager",
        lambda *_a, **_k: real_sensor_mgr(str(sensor_root)),
    )
    monkeypatch.setattr(
        saiSwitchSettingsManager,
        "SwitchSettingsManager",
        lambda *_a, **_k: real_switch_mgr(str(switch_root)),
    )
    monkeypatch.setattr(real_settings_cls, "DEFAULT_BASE_DIR", str(system_root))

    system_dir = system_root / "apvpd-test123"
    system_dir.mkdir()
    (system_dir / "settings.toml").write_text(
        "\n".join(
            [
                "[Network]",
                'SSID = "ExampleWiFi"',
                'PASSWORD = "obf1:old"',
                'HOSTNAME = "apvpd-test123"',
                "HTTPPORT = 8000",
                "",
                "[Profile]",
                'ACTIVE_PROFILE = "nodusweb"   # nodusweb | sensorius | weewx | homeassistant',
                "",
                "[MQTT]",
                'BROKER = "old-broker"',
                "PORT = 1883",
                "USE_TLS = false",
                'BASE_TOPIC = "nodus"',
                'USERNAME = ""',
                'PASSWORD = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )

    ingest._ensure_settings_from_itaot(
        {
            "HOSTNAME": "apvpd-test123",
            "Profile": {"ACTIVE_PROFILE": "sensorius"},
            "MQTT": {"BROKER": "samhain.local", "PORT": 1883, "USE_TLS": False, "BASE_TOPIC": "nodus"},
        },
        "apvpd-test123",
        [],
        [],
    )

    saved = (system_dir / "settings.toml").read_text(encoding="utf-8")
    assert 'ACTIVE_PROFILE = "sensorius"' in saved
    assert '\\"nodusweb\\"' not in saved
    assert "# nodusweb" not in saved


def test_legacy_poller_gate_and_sunset(monkeypatch):
    ingest_live = _build_ingest(
        monkeypatch,
        sections={
            "SensorNetwork": {
                "LEGACY_FIRMWARE_HOSTS": ["aqi-legacy"],
                "LEGACY_POLLER_SUNSET_DATE": "2099-12-31",
            }
        },
    )
    assert ingest_live._use_legacy_pollers_for("aqi-legacy") is False
    assert ingest_live._use_legacy_pollers_for("aqi-modern") is False

    ingest_expired = _build_ingest(
        monkeypatch,
        sections={
            "SensorNetwork": {
                "LEGACY_FIRMWARE_HOSTS": ["aqi-legacy"],
                "LEGACY_POLLER_SUNSET_DATE": "2000-01-01",
            }
        },
    )
    assert ingest_expired._use_legacy_pollers_for("aqi-legacy") is False


def test_no_heartbeat_activity_ages_to_offline(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    now_ts = time.time()
    ingest.heartbeat_interval_s_by_host["apvpd-test123"] = 30.0
    ingest.last_mqtt_seen["apvpd-test123"] = now_ts - 95.0
    ingest.device_status["apvpd-test123"] = "online"

    assert ingest._apply_heartbeat_timeout_state("apvpd-test123", now_ts=now_ts) == "offline"


def test_prefixed_onboarding_topics_are_parsed(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    received = []
    ingest.set_onboarding_event_handler(lambda event: received.append(event))
    msg = _Msg(
        "sensorius/nodus/apvpd-test123/onboard/hello",
        json.dumps({"onboard_token": "abc"}),
        retain=False,
    )
    ingest._on_message(ingest.client, None, msg)

    assert len(received) == 1
    assert received[0].get("event_type") == "onboarding_hello"
    assert received[0].get("device_id") == "apvpd-test123"
