"""Exercise dashboard query compatibility and bounded historical lookups.

Large synthetic history is inserted directly only for query-work regression
checks; functional packets use the normal logger write path.
"""

import importlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiStats import saiStats


@pytest.fixture
def logger(tmp_path):
    saiDataLogger._schema_ready = False
    instance = saiDataLogger(db_path=str(tmp_path / "dashboard.db"))
    yield instance
    instance.close()
    saiDataLogger._schema_ready = False


def test_latest_packet_preserves_case_lookup_ties_and_missing_sensors(logger):
    stamp = datetime.now().replace(microsecond=0)
    logger.log_readings(stamp.isoformat(), "Sensor-A", {"Temperature": 10.0})
    latest = (stamp + timedelta(minutes=1)).isoformat()
    logger.log_readings(latest, "Sensor-A", {"Temperature": 20.0})
    logger.log_readings(latest, "sensor-a", {"CO2": 700.0})
    logger.sensor_values.clear()
    logger.sensor_timestamps.clear()

    values, timestamps = logger.get_latest_values_and_timestamps(["SENSOR-A", "missing"])

    assert values["SENSOR-A"]["Temperature"] == 20.0
    assert values["SENSOR-A"]["CO2"] == 700.0
    assert "missing" not in values
    assert "missing" not in timestamps
    logger.sensor_timestamps.clear()
    assert logger.get_latest_timestamps(["SENSOR-A", "missing"]) == timestamps
    assert logger.get_available_sensors() == ["Sensor-A", "sensor-a"]


def test_latest_packet_seeks_past_large_history(logger, monkeypatch):
    logger.log_readings(datetime.now().isoformat(), "sensor-a", {"Temperature": 20.0})
    # A direct historical seed avoids thousands of irrelevant writer callbacks.
    with logger._writer_conn:
        logger._writer_conn.executemany(
            "INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
            [("2000-01-01", float(i), "sensor-a", "Temperature", 1.0) for i in range(5000)],
        )
    logger.sensor_values.clear()
    logger.sensor_timestamps.clear()
    original_open = logger._open_conn
    steps = []

    def tracked_open():
        conn = original_open()
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        return conn

    monkeypatch.setattr(logger, "_open_conn", tracked_open)
    values, _ = logger.get_latest_values_and_timestamps(["sensor-a"])
    assert values["sensor-a"]["Temperature"] == 20.0
    assert len(steps) < 20, "Latest packet retrieval scanned historical readings"


def test_trends_keep_case_distinct_histories_and_use_bounded_index(logger):
    now = datetime.now().replace(microsecond=0)
    for index in range(6):
        stamp = (now - timedelta(minutes=5-index)).isoformat()
        logger.log_readings(stamp, "Sensor-A", {"Temperature": 10.0 + index})
        logger.log_readings(stamp, "sensor-a", {"Temperature": 30.0 - index})
    # Old rows should not be visited by the recent-data join.
    with logger._writer_conn:
        logger._writer_conn.executemany(
            "INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
            [("2000-01-01", float(i), "Sensor-A", "Temperature", 1.0) for i in range(5000)],
        )
    statter = saiStats(logger.db_path)
    steps = []
    with sqlite3.connect(logger.db_path) as conn:
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        trends = statter._metric_trends(conn)
    assert trends["Sensor-A"]["Temperature"]["samples"] == 6
    assert trends["sensor-a"]["Temperature"]["samples"] == 6
    assert trends["Sensor-A"]["Temperature"]["rate_per_hour"] > 0
    assert trends["sensor-a"]["Temperature"]["rate_per_hour"] < 0
    assert len(steps) < 30, "Trend join scanned historical readings"


def test_stats_cache_lifetime_starts_after_computation(logger, monkeypatch):
    module = importlib.import_module("sensorius.saiStats")
    statter = saiStats(logger.db_path)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    calls = []

    def slow_trends(*args, **kwargs):
        calls.append(1)
        clock[0] += 10.0
        return {}

    monkeypatch.setattr(statter, "_metric_trends", slow_trends)
    assert statter.get_all_stats_fast() == {}
    assert statter.get_all_stats_fast() == {}
    assert len(calls) == 1
