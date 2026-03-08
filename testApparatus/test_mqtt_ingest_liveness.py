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

    def log_readings(self, *args, **kwargs):
        self.readings.append((args, kwargs))
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


def test_registered_topics_include_heartbeat(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    assert "nodus/+/status/heartbeat" in ingest.registered_topics
    assert "nodus/+/meta" in ingest.registered_topics
    assert "nodus/+/calibration/ack" in ingest.registered_topics
    assert "nodus/+/calibration/result" in ingest.registered_topics
    assert "nodus/+/event/calibration_status" in ingest.registered_topics
    assert "sensorius/nodus/+/onboard/hello" in ingest.registered_topics
    assert "sensorius/nodus/+/meta" in ingest.registered_topics
    assert "sensorius/nodus/+/config/ack" in ingest.registered_topics
    assert "sensorius/nodus/+/config/result" in ingest.registered_topics
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


def test_publish_nodus_calibration_uses_mqtt_command_topic(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    result = ingest.publish_nodus_calibration("aqi-123", action="apply", payload={"offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}]})
    assert result["ok"] is True
    assert result["topic"] == "nodus/aqi-123/calibration/set"
    topic, payload, qos, retain = ingest.client.pubs[-1]
    assert topic == "nodus/aqi-123/calibration/set"
    body = json.loads(payload)
    assert body["action"] == "apply"
    assert body["payload"]["offsets"][0]["key"] == "Calibration.Device.TEMP_OFFSET"
    assert qos == 1
    assert retain is False


def test_calibration_topics_update_state_caches(monkeypatch):
    ingest = _build_ingest(monkeypatch)

    ingest._on_message(
        ingest.client,
        None,
        _Msg("nodus/aqi-123/calibration/ack", json.dumps({"message_id": "cal-1", "accepted": True}), retain=False),
    )
    ack = ingest.calibration_ack_by_message.get("cal-1")
    assert ack is not None
    assert ack["accepted"] is True

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/aqi-123/calibration/result",
            json.dumps(
                {
                    "message_id": "cal-1",
                    "applied": True,
                    "updated": 2,
                    "status": {
                        "sensor_id": "aqi-123",
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
    assert result["status"]["sensor_id"] == "aqi-123"
    assert ingest.calibration_status_by_sensor["aqi-123"]["status"] == "calibrated"

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/aqi-123/event/calibration_progress",
            json.dumps(
                {
                    "sensor_id": "aqi-123",
                    "status": "in_progress",
                    "sample_index": 2,
                    "sample_total": 5,
                }
            ),
            retain=False,
        ),
    )
    snapshot = ingest.get_nodus_calibration_state("aqi-123")
    assert snapshot is not None
    assert snapshot["progress"]["sample_index"] == 2

    ingest._on_message(
        ingest.client,
        None,
        _Msg(
            "nodus/aqi-123/event/calibration_result",
            json.dumps(
                {
                    "sensor_id": "aqi-123",
                    "status": "success",
                    "calibrated": True,
                    "temp_offset": 1.25,
                    "rh_offset": -2.5,
                }
            ),
            retain=True,
        ),
    )
    snapshot = ingest.get_nodus_calibration_state("aqi-123")
    assert snapshot is not None
    assert snapshot["result"]["calibrated"] is True


def test_nodus_meta_materializes_switch_mappings(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "serial": "abc123",
            "sensor": {
                "sensor_id": "aqi-123",
                "location": "Veg Tent",
                "data_topic": "nodus/aqi-123/data",
                "availability_topic": "nodus/aqi-123/availability",
                "display_metrics": ["Temperature", "Rel-Humidity", "Temperature", "Ambient VPD"],
            },
            "switch": {
                "switch_device_id": "switch-123",
                "location": "Veg Tent",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-123",
                        "state": "OFF",
                        "event_topic": "nodus/S1-123/event",
                        "state_topic": "nodus/S1-123/state",
                        "set_topic": "nodus/S1-123/set",
                        "availability_topic": "nodus/S1-123/availability",
                    }
                ],
            },
            "location_group": {"location": "Veg Tent", "members": ["aqi-123", "switch-123"]},
        }
    )
    msg = _Msg("nodus/aqi-123/meta", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    meta = ingest.nodus_switch_topic_map.get("nodus/S1-123/state")
    assert meta is not None
    assert meta.get("switch_id") == "switch-123"
    assert meta.get("channel_id") == "S1-123"
    assert ingest.nodus_switch_command_topics.get(("switch-123", "S1-123")) == "nodus/S1-123/set"
    assert ingest.device_location.get("nodus/S1-123/state") == "Veg Tent"
    assert "aqi-123" in ingest.host_to_peer_ids.get("aqi-123", [])
    assert ingest.expected_gauge_map.get("aqi-123") == ["Temperature", "Rel-Humidity", "Ambient VPD"]


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
            "sensor": {"sensor_id": "aqi-123", "data_topic": "nodus/aqi-123/data"},
            "switch": {
                "switch_device_id": "switch-123",
                "channels": [{"index": 1, "label": "Fan", "channel_id": "S1-123", "state_topic": "nodus/S1-123/state"}],
            },
        }
    )
    ingest._on_message(ingest.client, None, _Msg("nodus/aqi-123/meta", payload, retain=True))
    assert ingest.nodus_switch_topic_map == {}
    assert ingest.nodus_switch_command_topics == {}


def test_nodus_meta_accepts_switch_device_id_alias(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = json.dumps(
        {
            "schema": "nodus-meta/v1",
            "device_id": "aqi-x943fm",
            "sensor": {
                "sensor_id": "aqi-x943fm",
                "location": "TestLab",
                "data_topic": "nodus/aqi-x943fm/data",
                "availability_topic": "nodus/aqi-x943fm/availability",
            },
            "switch": {
                "device_id": "switch-x943fm",
                "location": "TestLab",
                "channels": [
                    {
                        "index": 1,
                        "label": "Fan",
                        "channel_id": "S1-x943fm",
                        "state": False,
                        "event_topic": "nodus/S1-x943fm/event",
                        "state_topic": "nodus/S1-x943fm/state",
                        "set_topic": "nodus/S1-x943fm/set",
                        "availability_topic": "nodus/S1-x943fm/availability",
                    }
                ],
            },
        }
    )
    msg = _Msg("nodus/aqi-x943fm/meta", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.nodus_switch_topic_map.get("nodus/S1-x943fm/state", {}).get("switch_id") == "switch-x943fm"
    assert ingest.nodus_switch_command_topics.get(("switch-x943fm", "S1-x943fm")) == "nodus/S1-x943fm/set"


def test_http_itaot_meta_normalizes_to_topic_contract(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    payload = {
        "schema": "itaot-meta/v1",
        "device_id": "aqi-x943fm",
        "sensor": {
            "sensor_id": "aqi-x943fm",
            "location": "TestLab",
        },
        "switch": {
            "device_id": "switch-x943fm",
            "location": "TestLab",
            "channels": [
                {"index": 1, "label": "Fan", "channel_id": "S1-x943fm", "state": False},
                {"index": 2, "label": "Light", "channel_id": "S2-x943fm", "state": False},
            ],
        },
        "location_group": {"location": "TestLab", "members": ["aqi-x943fm", "S1-x943fm", "S2-x943fm"]},
    }

    ok, _ = ingest._parse_and_subscribe_from_http_meta(payload, "aqi-x943fm")
    assert ok is True
    assert ingest.nodus_switch_command_topics.get(("switch-x943fm", "S1-x943fm")) == "nodus/S1-x943fm/set"
    assert ingest.nodus_switch_command_topics.get(("switch-x943fm", "S2-x943fm")) == "nodus/S2-x943fm/set"
    assert ingest.nodus_switch_topic_map.get("nodus/S1-x943fm/state", {}).get("label") == "Fan"


def test_retained_stale_heartbeat_sets_unknown(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    stale_ts = int(time.time()) - 300
    payload = json.dumps(
        {
            "device_id": "aqi-123",
            "status": "online",
            "timestamp": stale_ts,
            "heartbeat_interval_s": 30,
        }
    )
    msg = _Msg("nodus/aqi-123/status/heartbeat", payload, retain=True)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("aqi-123") == "unknown"
    assert ingest.heartbeat_stale.get("aqi-123") is True
    assert "aqi-123" not in ingest.last_heartbeat_ts


def test_fresh_heartbeat_sets_online_and_ts(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    ts_now = int(time.time())
    payload = json.dumps(
        {
            "device_id": "aqi-123",
            "status": "online",
            "timestamp": ts_now,
            "heartbeat_interval_s": 30,
        }
    )
    msg = _Msg("nodus/aqi-123/status/heartbeat", payload, retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("aqi-123") == "online"
    assert ingest.heartbeat_stale.get("aqi-123") is False
    assert int(ingest.last_heartbeat_ts.get("aqi-123", 0)) == ts_now


def test_heartbeat_timeout_state_transitions(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    now_ts = time.time()
    ingest.heartbeat_interval_s_by_host["aqi-123"] = 30.0

    ingest.last_heartbeat_ts["aqi-123"] = now_ts - 20.0
    assert ingest._apply_heartbeat_timeout_state("aqi-123", now_ts=now_ts) == "online"

    ingest.last_heartbeat_ts["aqi-123"] = now_ts - 70.0
    assert ingest._apply_heartbeat_timeout_state("aqi-123", now_ts=now_ts) == "degraded"

    ingest.last_heartbeat_ts["aqi-123"] = now_ts - 95.0
    assert ingest._apply_heartbeat_timeout_state("aqi-123", now_ts=now_ts) == "offline"


def test_recovery_via_data_marks_online_with_stale_heartbeat(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    msg = _Msg("nodus/aqi-123/data", json.dumps({"values": {"Temperature": 21.2}}), retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("aqi-123") == "online"
    assert ingest.heartbeat_stale.get("aqi-123") is True


def test_debug_data_only_data_path_does_not_mark_heartbeat_stale(monkeypatch):
    ingest = _build_ingest(
        monkeypatch,
        values={("SensorNetwork", "NODUS_DEBUG_DATA_ONLY"): True},
    )
    msg = _Msg("nodus/aqi-123/data", json.dumps({"values": {"Temperature": 21.2}}), retain=False)
    ingest._on_message(ingest.client, None, msg)

    assert ingest.device_status.get("aqi-123") == "online"
    assert "aqi-123" not in ingest.heartbeat_stale


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
    assert ingest_live._use_legacy_pollers_for("aqi-legacy") is True
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
    ingest.heartbeat_interval_s_by_host["aqi-123"] = 30.0
    ingest.last_mqtt_seen["aqi-123"] = now_ts - 95.0
    ingest.device_status["aqi-123"] = "online"

    assert ingest._apply_heartbeat_timeout_state("aqi-123", now_ts=now_ts) == "offline"


def test_prefixed_onboarding_topics_are_parsed(monkeypatch):
    ingest = _build_ingest(monkeypatch)
    received = []
    ingest.set_onboarding_event_handler(lambda event: received.append(event))
    msg = _Msg(
        "sensorius/nodus/aqi-123/onboard/hello",
        json.dumps({"onboard_token": "abc"}),
        retain=False,
    )
    ingest._on_message(ingest.client, None, msg)

    assert len(received) == 1
    assert received[0].get("event_type") == "onboarding_hello"
    assert received[0].get("device_id") == "aqi-123"
