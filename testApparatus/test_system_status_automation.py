"""Exercise system condition boundaries without hardware or live MQTT.

Controlled clocks and liveness snapshots distinguish physical sensor failures
from transport outages. SQLite query-only mode supplies real write failures.
"""

from types import SimpleNamespace
import json
import threading

import pytest

from sensorius.saiAutomationEvaluation import evaluate_condition_groups
from sensorius.saiSystemStatus import (
    SystemStatusService, normalize_system_status_condition, system_status_groups_unknown,
)
from testApparatus.test_automation_evaluation import make_evaluator


class Ingest:
    def __init__(self):
        self.host = {"host": "node", "state": "online", "last_heartbeat_s": 10,
                     "last_report_s": 10, "heartbeat_interval_s": 60}
        self.child = {"host": "node", "state": "offline", "availability": "offline"}

    def get_nodus_liveness(self, device, **kwargs):
        return self.host if device == "node" else self.child


def service():
    now = [0.0]
    ingest = Ingest()
    status = SystemStatusService(data_logger=SimpleNamespace(), mqtt_ingest=ingest, clock=lambda: now[0])
    return status, ingest, now


def observe_until(status, now, condition, seconds):
    while now[0] < seconds:
        now[0] = min(now[0] + 60, seconds)
        result, report = status.evaluate(condition)
    return result, report


def test_sensor_offline_requires_continuous_offline_with_live_host():
    status, ingest, now = service()
    condition = {"status": "sensor_offline", "device_id": "soil"}
    assert status.evaluate(condition)[0] is None
    assert observe_until(status, now, condition, 3600)[0] is None
    assert observe_until(status, now, condition, 3601)[0] is True
    ingest.host.update(state="offline", last_report_s=200, last_heartbeat_s=200)
    assert status.evaluate(condition)[0] is None
    ingest.host.update(state="online", last_heartbeat_s=0)
    assert status.evaluate(condition)[0] is None
    ingest.child.update(state="online", availability="online")
    assert status.evaluate(condition)[0] is False
    ingest.child.update(state="offline", availability="offline")
    assert status.evaluate(condition)[0] is None


@pytest.mark.parametrize("device_type", ["sensor", "switch"])
def test_mqtt_disconnect_uses_physical_host_not_child_offline(device_type):
    status, ingest, now = service()
    condition = {"status": "mqtt_disconnected", "device_id": "device", "device_type": device_type}
    assert status.evaluate(condition)[0] is False
    ingest.host.update(state="offline", last_report_s=181, last_heartbeat_s=181)
    assert status.evaluate(condition)[0] is None
    assert observe_until(status, now, condition, 900)[0] is None
    assert observe_until(status, now, condition, 901)[0] is True
    ingest.host.update(state="online", last_report_s=0)
    assert status.evaluate(condition)[0] is False
    ingest.host.update(state="unknown", last_report_s=None, last_heartbeat_s=None)
    assert status.evaluate(condition)[0] is None


def test_missed_observations_and_restart_do_not_invent_outage_duration():
    status, _, now = service()
    condition = {"status": "sensor_offline", "device_id": "soil", "duration_min": 1}
    assert status.evaluate(condition)[0] is None
    now[0] = 1000
    assert status.evaluate(condition)[0] is None
    assert observe_until(status, now, condition, 1061)[0] is True
    restarted = SystemStatusService(data_logger=SimpleNamespace(), mqtt_ingest=status.mqtt_ingest, clock=lambda: now[0])
    assert restarted.evaluate(condition)[0] is None


def test_real_ingest_keeps_child_offline_separate_from_host_outage(monkeypatch):
    import sensorius.saiMQTTIngest as ingest_module
    from testApparatus.test_mqtt_ingest_liveness import _build_ingest

    ingest = _build_ingest(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ingest_module.time, "time", lambda: now[0])
    ingest.nodus_sensor_hosts = {"soil-child": "node"}
    ingest.nodus_host_sensors = {"node": ["soil-child"]}
    ingest.host_to_peer_ids["node"] = ["soil-child", "switch-node"]
    ingest.nodus_availability["soil-child"] = "offline"
    ingest.last_heartbeat_ts["node"] = now[0]
    status = SystemStatusService(data_logger=SimpleNamespace(), mqtt_ingest=ingest, clock=lambda: now[0])
    sensor = {"status": "sensor_offline", "device_id": "soil-child", "duration_min": 1}
    mqtt = {"status": "mqtt_disconnected", "device_id": "switch-node", "device_type": "switch", "duration_min": 1}
    assert status.evaluate(sensor)[0] is None
    for _ in range(2):
        now[0] += 31
        ingest.last_heartbeat_ts["node"] = now[0]
        offline, _ = status.evaluate(sensor)
        assert status.evaluate(mqtt)[0] is False
    assert offline is True
    ingest.last_heartbeat_ts["node"] = now[0] - 1000
    ingest._record_host_seen("node", ts=now[0], retain=False, report=False)
    assert status.evaluate(sensor)[0] is True
    assert status.evaluate(mqtt)[0] is False
    now[0] += 180
    assert status.evaluate(sensor)[0] is None
    assert status.evaluate(mqtt)[0] is None
    for _ in range(2):
        now[0] += 31
        disconnected, _ = status.evaluate(mqtt)
    assert disconnected is True


@pytest.mark.asyncio
async def test_disk_probe_uses_database_filesystem_and_expires(tmp_path, monkeypatch):
    status, _, now = service()
    status.data_logger.db_path = str(tmp_path / "active.db")

    def usage(path):
        assert path == tmp_path
        assert threading.current_thread() is not threading.main_thread()
        return SimpleNamespace(free=1024**3)

    monkeypatch.setattr("sensorius.saiSystemStatus.shutil.disk_usage", usage)
    await status.refresh_disk()
    assert status.evaluate({"status": "disk_free"})[0] is False
    status.disk_free -= 1
    assert status.evaluate({"status": "disk_free"})[0] is True
    assert status.evaluate({"status": "disk_free", "threshold_gib": .5})[0] is False
    now[0] = 91
    assert status.evaluate({"status": "disk_free"})[0] is None


@pytest.mark.parametrize("condition", [
    {"status": "bad"}, {"threshold_gib": "NaN"}, {"threshold_gib": 0},
    {"status": "db_write_errors", "failures": 1.5},
    {"status": "sensor_offline", "device_id": "s", "duration_min": -1},
    {"status": "sensor_offline", "device_id": "s", "device_type": "switch"},
    {"status": "mqtt_disconnected", "device_id": "../private"},
])
def test_reject_invalid_system_conditions(condition):
    with pytest.raises(ValueError):
        normalize_system_status_condition(condition)


def test_unknown_conditions_respect_and_or_logic():
    evaluator = make_evaluator(system_status=lambda cond: (None, "Unavailable"))
    unknown = {"type": "system_status"}
    false = {"type": "sensor", "sensor": "s", "metric": "Soil-Temp", "value": 100}
    true = {**false, "value": 0}
    evaluate = lambda groups: evaluate_condition_groups(groups, evaluator.evaluate, target_label="Fan")
    assert system_status_groups_unknown(evaluate([[unknown]]))
    assert not system_status_groups_unknown(evaluate([[unknown, false]]))
    assert system_status_groups_unknown(evaluate([[unknown], [false]]))
    assert not system_status_groups_unknown(evaluate([[unknown], [true]]))


def test_all_sensors_tracks_each_outage_and_includes_new_discoveries(monkeypatch):
    status, ingest, now = service()
    ingest.nodus_sensor_hosts = {"healthy": "node", "failed": "other-node"}
    ingest._known_switch_ids = {"switch-node"}
    faults = {"healthy": False, "failed": True, "switch-node": True}
    monkeypatch.setattr(status, "_device_fault", lambda condition: faults[condition["device_id"]])
    condition = {"status": "sensor_offline", "device_id": "__all__", "duration_min": 1}
    assert normalize_system_status_condition(condition)["device_id"] == "__all__"
    assert status.evaluate(condition)[0] is None
    now[0] = 61
    result, report = status.evaluate(condition)
    assert result is True and "failed" in report and "switch-node" not in report
    faults["failed"] = False
    faults["new-sensor"] = True
    ingest.nodus_sensor_hosts["new-sensor"] = "new-node"
    assert status.evaluate(condition)[0] is None
    now[0] = 122
    result, report = status.evaluate(condition)
    assert result is True and "new-sensor" in report
    faults["new-sensor"] = False
    assert status.evaluate(condition)[0] is False


def test_all_mqtt_devices_includes_switches_and_preserves_unknown(monkeypatch):
    status, ingest, now = service()
    condition = {"status": "mqtt_disconnected", "device_id": "__all__", "device_type": "all", "duration_min": 1}
    assert status.evaluate(condition)[0] is None
    ingest.nodus_sensor_hosts = {"sensor": "node"}
    ingest._known_switch_ids = {"switch-node"}
    faults = {"sensor": False, "switch-node": True}
    monkeypatch.setattr(status, "_device_fault", lambda condition: faults[condition["device_id"]])
    assert status.evaluate(condition)[0] is None
    now[0] = 61
    result, report = status.evaluate(condition)
    assert result is True and "switch-node" in report
    faults.update(sensor=None, **{"switch-node": False})
    assert status.evaluate(condition)[0] is None
    faults["sensor"] = False
    assert status.evaluate(condition)[0] is False


def test_real_failed_writes_recovery_and_success_reset(tmp_path, monkeypatch):
    from sensorius.saiDataLogger import saiDataLogger

    with saiDataLogger(str(tmp_path / "data.db")) as logger:
        status = SystemStatusService(data_logger=logger)
        monkeypatch.setattr(logger, "recover_after_db_error", lambda *args, **kwargs: False)
        logger._writer_conn.execute("PRAGMA query_only=ON")
        for _ in range(2):
            assert not logger.log_readings(None, "s", {"Temperature": 20})
        assert status.evaluate({"status": "db_write_errors"})[0] is False
        logger.log_switch_event("CH1::Fan", True)
        assert status.evaluate({"status": "db_write_errors"})[0] is True
        assert logger.database_write_health_snapshot()["consecutive_failures"] == 3
        logger._writer_conn.execute("PRAGMA query_only=OFF")
        logger.log_switch_event("CH1::Fan", False)
        assert logger.database_write_health_snapshot()["consecutive_failures"] == 0
        logger._writer_conn.execute("PRAGMA query_only=ON")

        def recover(*args, **kwargs):
            logger._writer_conn.execute("PRAGMA query_only=OFF")
            return True

        monkeypatch.setattr(logger, "recover_after_db_error", recover)
        assert logger.log_readings(None, "s", {"Temperature": 21})
        assert logger.database_write_health_snapshot()["consecutive_failures"] == 0
        assert logger.get_latest_values("s")["Temperature"] == 21


@pytest.mark.asyncio
async def test_http_round_trip_and_reject_invalid_system_conditions(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as managers
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    app, ingest, *_ = await _build_app(tmp_path, monkeypatch)
    original_manager = managers.AutomationManager

    class Manager(original_manager):
        def __init__(self, _base="automation_settings"):
            super().__init__(str(tmp_path / "automation-test"))

    monkeypatch.setattr(managers, "AutomationManager", Manager)
    ingest.nodus_sensor_hosts = {"soil": "node"}
    ingest._known_switch_ids = {"switch-node"}
    conditions = [
        {"type": "system_status", "status": "disk_free", "threshold_gib": .75},
        {"type": "system_status", "status": "db_write_errors", "failures": 5},
        {"type": "system_status", "status": "sensor_offline", "device_id": "soil"},
        {"type": "system_status", "status": "mqtt_disconnected", "device_id": "switch-node", "device_type": "switch"},
        {"type": "system_status", "status": "sensor_offline", "device_id": "__all__"},
        {"type": "system_status", "status": "mqtt_disconnected", "device_id": "__all__", "device_type": "all"},
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        context = (await client.get("/automation-context")).json()
        assert {item["id"] for item in context["system_status_devices"]} >= {"soil", "switch-node"}
        for index, condition in enumerate(conditions):
            script = {"enabled": False, "name": "Status", "conditions": [condition],
                      "actions": [{"type": "none", "executor_switch_id": "__system__"}]}
            payload = {"switch_id": "__system__", "rule_id": f"status-{index}",
                       "enabled": False, "script_json": json.dumps(script)}
            response = await client.post("/submit-advanced-trigger", json=payload)
            assert response.status_code == 200, response.text
            saved = Manager().load("__system__")["Advanced"][f"status-{index}"]
            assert json.loads(saved["script_json"])["conditions"] == [normalize_system_status_condition(condition)]
        payload["script_json"] = json.dumps({**script, "conditions": [{"type": "system_status", "threshold_gib": -1}]})
        assert (await client.post("/submit-advanced-trigger", json=payload)).status_code == 400
