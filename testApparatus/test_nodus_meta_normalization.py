"""Verify metadata conversion independently of MQTT and settings persistence.

Contract cases preserve slot positions, identity precedence, topic filtering,
legacy conversion, credential redaction, and companion merge behavior.
"""

import copy

import pytest

from sensorius import saiNodusMeta as meta


def test_prepare_copies_and_redacts_without_discarding_unknown_fields():
    payload = {
        "schema": " NODUS-META/V1 ", "device_id": "aht-test",
        "network": {"hostname": "hub", "ssid": "sample", "SSID": "sample",
                    "password": "sample", "PASSWORD": "sample", "ipv4addr": "192.0.2.1"},
        "sensor": {"sensor_id": "aht-test", "calibration": {"offset": 1}},
        "extension": {"future": [1, 2]},
    }
    before = copy.deepcopy(payload)
    result = meta.prepare_nodus_meta(payload)
    assert result["network"] == {"hostname": "hub", "ipv4addr": "192.0.2.1"}
    result["sensor"]["calibration"]["offset"] = 5
    result["extension"]["future"].append(3)
    assert payload == before


@pytest.mark.parametrize("payload", [None, [], "meta", {"schema": "nodus-meta/v2"}])
def test_prepare_rejects_unsupported_input(payload):
    assert meta.prepare_nodus_meta(payload) is None


def test_schema_omission_and_blank_metadata_remain_accepted():
    assert meta.prepare_nodus_meta({}) == {}
    assert meta.prepare_nodus_meta({"schema": ""}) == {"schema": ""}


def test_metric_slots_keep_blanks_duplicates_and_native_metric_names():
    raw = ["Temperature_F", "", "Temperature_F", "Soil-pH", None, "Gas", "extra"]
    assert meta.normalize_display_metrics(raw, preserve_slots=True) == [
        "Temperature_F", "", "Temperature_F", "Soil-pH", "", "Gas",
    ]
    assert meta.normalize_display_metrics(raw) == ["Temperature_F", "Soil-pH", "Gas", "extra"]
    assert list(meta.metric_slot_map(raw).values()) == [
        "Temperature_F", "", "Temperature_F", "Soil-pH", "", "Gas",
    ]
    assert meta.normalize_display_metrics({"METRIC_2": "Temperature_F"}, preserve_slots=True) == [
        "", "Temperature_F", "", "", "", "",
    ]


def test_style_aliases_keep_six_slots_and_the_configured_fallback():
    assert meta.normalize_display_styles([" gauge ", "6h", "24hr", "invalid"]) == [
        "Gauge", "Graph6hr", "Graph24hr", "Graph24hr", "Graph24hr", "Graph24hr",
    ]
    assert meta.normalize_display_styles(["", None]) == []
    assert meta.normalize_display_styles(["invalid"], default_style="Gauge") == ["Gauge"] * 6


@pytest.mark.parametrize("payload,expected", [
    ({"mcu": "esp32s3", "board": "pico2w"}, "esp32s3"),
    ({"MCU": "pico2w"}, "pico2w"),
    ({"TYPE": "xesp32s3"}, "xesp32s3"),
    ({"type": "nodus"}, "pico2w"), (None, ""),
])
def test_board_identity_precedence_and_legacy_fallback(payload, expected):
    assert meta.extract_board_type(payload) == expected


def test_sensor_identity_precedence_and_prefix_fallback():
    payload = {"serial": "host-serial", "hardware": "host-hardware",
               "sensor": {"SERIAL_NUM": "child-serial", "hardware": "child-hardware"}}
    assert meta.extract_sensor_hardware(payload) == "child-hardware"
    assert meta.extract_sensor_serial(payload["sensor"], payload) == "child-serial"
    assert meta.extract_sensor_serial({}, payload) == "host-serial"
    assert meta.infer_sensor_device_name("explicit", "co2-test") == "explicit"
    assert meta.infer_sensor_device_name("", "co2-test") == "co2"
    assert meta.infer_sensor_device_name("", "sensor-test") == ""


def test_children_are_authoritative_and_invalid_rows_are_ignored():
    payload = {"sensor": {"sensor_id": "primary"},
               "sensors": [None, {"sensor_id": "child-a"}, "bad", {"sensor_id": "child-b"}]}
    before = copy.deepcopy(payload)
    primary, children = meta.sensor_metadata_blocks(payload, "hub")
    assert primary == {"sensor_id": "primary"}
    assert children == [{"sensor_id": "child-a"}, {"sensor_id": "child-b"}]
    assert payload == before
    assert meta.sensor_metadata_blocks({"sensor": primary, "sensors": [None]}, "hub")[1] == [primary]


def test_top_level_sensor_fields_keep_legacy_fallbacks():
    primary, children = meta.sensor_metadata_blocks({
        "mqtt_sensor_topic": "nodus/hub/data", "DEVICE": "aht", "SERIAL_NUM": "s1",
        "metrics": ["Temperature"], "styles": ["Gauge"], "HARDWARE": "aht20",
    }, "hub")
    assert primary["sensor_id"] == "hub"
    assert primary["device"] == "aht" and primary["serial"] == "s1"
    assert primary["hardware"] == "aht20" and primary["display_styles"] == ["Gauge"]
    assert children == [primary]
    assert meta.sensor_metadata_blocks({}, "hub") == ({}, [])


@pytest.mark.parametrize("topic,prefix,expected", [
    (" nodus/hub/data ", "sensorius", "nodus/hub/data"),
    ("sensorius/nodus/hub/data", "sensorius", "sensorius/nodus/hub/data"),
    ("other/nodus/hub/data", "sensorius", ""), ("http://hub/data", "sensorius", ""),
    ("sensorius/nodus/hub/data", "", ""),
])
def test_topic_acceptance_keeps_existing_prefix_rules(topic, prefix, expected):
    assert meta.metadata_topic(topic, prefix) == expected


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("ON", True),
                                          ("0", False), ("no", False), (None, None), (0, None)])
def test_switch_state_coercion_preserves_metadata_conventions(raw, expected):
    assert meta.coerce_switch_state(raw) is expected


def test_location_precedence_ignores_placeholders():
    assert meta.pick_location("n/a", " - ", " Greenhouse ", "Bedroom") == "Greenhouse"
    assert meta.pick_location("unknown", "none", "") == "Unknown"


def test_legacy_conversion_keeps_channel_indexes_topics_and_partial_metadata():
    payload = {"device_id": "aht-test", "MCU": "pico2w", "serial": "host",
               "sensor": {"display_metrics": ["Temperature_F"], "display_styles": ["6hr"]},
               "switch": {"device_id": "switch-test", "channels": [None, {},
                          {"channel_id": "S2-test", "label": "Pump", "index": "bad", "state": False}]}}
    before = copy.deepcopy(payload)
    result = meta.normalize_itaot_meta(payload)
    assert result["sensor"]["sensor_id"] == "aht-test"
    assert result["sensor"]["serial"] == "host"
    assert result["sensor"]["display_styles"] == ["Graph6hr"] + ["Graph24hr"] * 5
    assert result["switch"]["channels"] == [{
        "index": 3, "label": "Pump", "channel_id": "S2-test", "state": False,
        "event_topic": "nodus/S2-test/event", "state_topic": "nodus/S2-test/state",
        "set_topic": "nodus/S2-test/config/set", "availability_topic": "nodus/S2-test/availability",
    }]
    assert payload == before


def test_config_companion_updates_only_matching_child_and_copies_cached_data():
    cached = {"sensor": {"sensor_id": "a", "device": "co2"},
              "sensors": [{"sensor_id": "a", "device": "co2"}, {"sensor_id": "b", "device": "lux"}],
              "switch": {"device_id": "switch-test"}}
    payload = {"sensor": {"sensor_id": "b", "display_metrics": ["Light Intensity"]},
               "time": {"tz": "America/Denver"}}
    before = copy.deepcopy((cached, payload))
    result = meta.merge_config_meta(payload, cached, device_id="hub")
    assert result["sensor"] == payload["sensor"]
    assert result["sensors"][0] == cached["sensors"][0]
    assert result["sensors"][1]["device"] == "lux"
    assert result["sensors"][1]["display_metrics"] == ["Light Intensity"]
    result["time"]["tz"] = "UTC"
    result["switch"]["device_id"] = "changed"
    assert (cached, payload) == before


def test_switch_companion_preserves_sensor_and_group_location():
    cached = {"sensor": {"sensor_id": "a"}, "switch": {"device_id": "switch-test", "location": "Room"},
              "location_group": {"location": "Existing", "members": ["a"]}}
    payload = {"channels": [{"channel_id": "S1-test", "label": "Fan"}], "location": "New"}
    before = copy.deepcopy((cached, payload))
    result = meta.merge_switch_meta(payload, cached, device_id="hub")
    assert result["sensor"] == cached["sensor"]
    assert result["switch"]["device_id"] == "switch-test"
    assert result["switch"]["location"] == "New"
    assert result["switch"]["channel_count"] == 1
    assert result["location_group"] == {"location": "Existing", "members": ["a", "hub", "switch-test"]}
    assert (cached, payload) == before
