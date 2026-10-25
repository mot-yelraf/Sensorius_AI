"""Verify Nodus liveness decisions without MQTT, storage, or hardware.

Fixed-clock cases preserve timeout boundaries and the distinction between live
reports, retained replay, explicit availability, and device clock drift.
"""

import pytest

from sensorius.saiNodusLiveness import NodusLiveness

NOW = 1000.0
HOST = "aht-test"


def snapshot(liveness):
    return liveness.snapshot(HOST, base=HOST, keys=[HOST, f"{HOST}.local"])


@pytest.mark.parametrize("age,state", [
    (0, "online"), (60, "online"), (60.01, "degraded"),
    (89.99, "degraded"), (90, "offline"), (91, "offline"),
])
@pytest.mark.parametrize("source", ["heartbeat", "report"])
def test_timeout_boundaries(age, state, source):
    liveness = NodusLiveness(clock=lambda: NOW)
    if source == "heartbeat":
        liveness.last_heartbeat_ts[HOST] = NOW - age
    else:
        liveness.record_seen(HOST, ts=NOW - age, report=True)
    result = snapshot(liveness)
    assert result["state"] == state
    reason = f"heartbeat_{state}" if source == "heartbeat" else {
        "online": "report_recent", "degraded": "report_stale", "offline": "report_timeout",
    }[state]
    assert result["reason"] == reason


def test_retained_replay_cannot_establish_online_state():
    liveness = NodusLiveness(clock=lambda: NOW)
    liveness.record_seen(HOST, retain=True, report=True)
    assert snapshot(liveness) == {
        "device_id": HOST, "host": HOST, "state": "unknown", "reason": "no_recent_mqtt",
        "availability": None, "last_seen_s": None, "last_report_s": None,
        "last_heartbeat_s": None, "retained_seen_s": 0.0, "heartbeat_stale": False,
        "heartbeat_interval_s": 30.0, "peer_ids": [HOST],
    }
    liveness.nodus_availability[HOST] = "online"
    assert snapshot(liveness)["state"] == "unknown"
    liveness.record_seen(HOST)
    assert snapshot(liveness)["reason"] == "availability_only"
    liveness.record_seen(HOST, report=True)
    assert snapshot(liveness)["state"] == "online"


def test_offline_availability_remains_authoritative_until_recovery():
    liveness = NodusLiveness(clock=lambda: NOW)
    liveness.last_heartbeat_ts[HOST] = NOW
    liveness.record_seen(HOST, report=True)
    liveness.nodus_availability[HOST] = "offline"
    assert snapshot(liveness)["reason"] == "availability_offline"
    liveness.nodus_availability[HOST] = "online"
    assert snapshot(liveness)["state"] == "online"


def test_new_report_overrides_older_heartbeat_and_stale_diagnostic():
    liveness = NodusLiveness(clock=lambda: NOW)
    liveness.last_heartbeat_ts[HOST] = NOW - 100
    liveness.heartbeat_stale[HOST] = True
    assert snapshot(liveness)["state"] == "offline"
    liveness.record_seen(HOST, report=True)
    result = snapshot(liveness)
    assert result["state"] == "online"
    assert result["reason"] == "report_recent"
    assert result["heartbeat_stale"] is True


@pytest.mark.parametrize("timestamp,retained,stale", [
    (None, False, False), (None, True, True),
    (NOW - 90, True, False), (NOW - 90.01, True, True),
    (NOW + 15, False, False), (NOW + 15.01, False, True),
    (NOW - 500, False, False),
])
def test_heartbeat_freshness_boundaries(timestamp, retained, stale):
    liveness = NodusLiveness(clock=lambda: NOW)
    assert liveness.heartbeat_is_stale(timestamp, retain=retained) is stale


def test_old_live_device_clock_uses_receipt_time_but_retained_keeps_payload_time():
    liveness = NodusLiveness(clock=lambda: NOW)
    assert liveness.heartbeat_liveness_timestamp(
        NOW - 500, retain=False, now_ts=NOW, interval_s=30,
    ) == NOW
    assert liveness.heartbeat_liveness_timestamp(
        NOW - 500, retain=True, now_ts=NOW, interval_s=30,
    ) == NOW - 500


def test_physical_host_availability_does_not_inherit_child_offline_state():
    liveness = NodusLiveness(clock=lambda: NOW)
    liveness.last_heartbeat_ts[HOST] = NOW
    liveness.nodus_availability["lux-child"] = "offline"
    keys = [HOST, "lux-child"]
    physical = liveness.snapshot(HOST, base=HOST, keys=keys, availability_keys=[HOST])
    child = liveness.snapshot("lux-child", base=HOST, keys=keys, availability_keys=["lux-child"])
    assert physical["state"] == "online"
    assert child["reason"] == "availability_offline"


@pytest.mark.parametrize("text,data,expected", [
    ("up", None, "online"), ("dead", None, "offline"),
    ("", {"status": True}, "online"), ("", {"state": False}, "offline"),
    ("", {"availability": "ready"}, "online"), ("mystery", {}, None),
])
def test_legacy_availability_shapes(text, data, expected):
    assert NodusLiveness().parse_availability_payload(text, data) == expected
