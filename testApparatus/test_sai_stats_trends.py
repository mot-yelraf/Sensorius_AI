"""Cover database-backed dashboard metric trends.

The tests exercise historical aggregation and response shaping against
controlled readings so dashboard trends remain stable.
"""

from __future__ import annotations

import sqlite3
import time

from sensorius.saiStats import saiStats


def _seed_readings_db(db_path, *, samples: int = 181) -> None:
    end_epoch = time.time() - 2.0
    start_epoch = end_epoch - ((samples - 1) * 60)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE readings (
                timestamp TEXT,
                ts_epoch REAL,
                sensor_id TEXT,
                metric TEXT,
                value REAL
            )
            """
        )
        rows = []
        for index in range(samples):
            timestamp = start_epoch + (index * 60)
            rows.extend(
                (
                    ("", timestamp, "trend-sensor", "Temperature", 20.0 + (index * 0.1)),
                    ("", timestamp, "trend-sensor", "Baro-Pressure", 1020.0 - (index * 0.1)),
                )
            )
        conn.executemany(
            """
            INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_cold_start_populates_trends_from_existing_database_history(tmp_path):
    db_path = tmp_path / "trend-history.db"
    _seed_readings_db(db_path)

    stats = saiStats(str(db_path)).get_24hr_stats("TREND-SENSOR")

    temperature = stats["Temperature"]["trend"]
    assert temperature["samples"] == 20
    assert temperature["window_s"] == 19 * 60
    assert abs(temperature["rate_per_hour"] - 6.0) < 0.001
    assert temperature["provisional"] is False

    pressure = stats["Baro-Pressure"]["trend"]
    assert pressure["samples"] >= 180
    assert pressure["window_s"] >= 179 * 60
    assert abs(pressure["rate_per_hour"] + 6.0) < 0.001
    assert pressure["provisional"] is False


def test_fast_all_sensor_stats_include_database_backed_trends(tmp_path):
    db_path = tmp_path / "all-trend-history.db"
    _seed_readings_db(db_path, samples=20)

    stats = saiStats(str(db_path)).get_all_stats_fast()

    temperature = stats["trend-sensor"]["Temperature"]["trend"]
    assert temperature["samples"] == 20
    assert temperature["window_s"] == 19 * 60
    assert temperature["rate_per_hour"] > 0
    pressure = stats["trend-sensor"]["Baro-Pressure"]["trend"]
    assert pressure["rate_per_hour"] < 0
    assert pressure["provisional"] is True


def test_fast_all_sensor_stats_preserve_deterministic_extrema_timestamps(tmp_path):
    db_path = tmp_path / "all-stats-extrema.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE readings (
                timestamp TEXT,
                ts_epoch REAL,
                sensor_id TEXT,
                metric TEXT,
                value REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ("2026-08-03T12:00:02Z", 102.0, "sensor-a", "Temperature", 1.0),
                ("2026-08-03T12:00:01Z", 101.0, "sensor-a", "Temperature", 1.0),
                ("2026-08-03T12:00:03Z", 103.0, "sensor-a", "Temperature", 3.0),
                ("2026-08-03T12:00:04Z", 104.0, "sensor-a", "Temperature", 3.0),
                ("2026-08-03T12:00:05Z", 105.0, "sensor-a", "Temperature", 2.0),
            ),
        )

    statter = saiStats(str(db_path))
    statter._since_epoch_24h = lambda: 100.0

    temperature = statter.get_all_stats_fast()["sensor-a"]["Temperature"]

    assert temperature["min"] == 1.0
    assert temperature["min_ts"] == "2026-08-03T12:00:01Z"
    assert temperature["avg"] == 2.0
    assert temperature["max"] == 3.0
    assert temperature["max_ts"] == "2026-08-03T12:00:04Z"


def test_wind_direction_uses_circular_average_in_range_and_fast_stats(tmp_path):
    db_path = tmp_path / "wind-direction-stats.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE readings (
                timestamp TEXT,
                ts_epoch REAL,
                sensor_id TEXT,
                metric TEXT,
                value REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ("2026-08-03T12:00:00Z", 101.0, "weather", "Wind Direction", 350.0),
                ("2026-08-03T12:01:00Z", 102.0, "weather", "Wind Direction", 10.0),
            ),
        )

    statter = saiStats(str(db_path))
    ranged = statter.get_stats_for_range("weather", 100.0, 103.0)
    assert abs(ranged["Wind Direction"]["avg"]) < 0.001

    statter._since_epoch_24h = lambda: 100.0
    fast = statter.get_all_stats_fast()
    assert abs(fast["weather"]["Wind Direction"]["avg"]) < 0.001


def test_opposed_wind_directions_have_no_defined_average():
    assert saiStats._circular_mean_degrees([90.0, 270.0]) is None


def test_metric_trends_ignore_sensors_without_readings_in_last_24_hours(tmp_path):
    db_path = tmp_path / "bounded-trends.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE readings (
                timestamp TEXT,
                ts_epoch REAL,
                sensor_id TEXT,
                metric TEXT,
                value REAL
            )
            """
        )
        rows = []
        for index in range(6):
            rows.extend(
                (
                    ("", 100.0 + (index * 60), "stale-sensor", "Temperature", 10.0 + index),
                    ("", 1100.0 + (index * 60), "recent-sensor", "Temperature", 20.0 + index),
                )
            )
        conn.executemany(
            """
            INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

        statter = saiStats(str(db_path))
        statter._since_epoch_24h = lambda: 1000.0
        trends = statter._metric_trends(conn)

    assert "stale-sensor" not in trends
    assert trends["recent-sensor"]["Temperature"]["samples"] == 6
    assert trends["recent-sensor"]["Temperature"]["window_s"] == 5 * 60


def test_trends_require_six_samples_spanning_five_minutes(tmp_path):
    db_path = tmp_path / "short-trend-history.db"
    _seed_readings_db(db_path, samples=5)

    stats = saiStats(str(db_path)).get_24hr_stats("trend-sensor")

    assert "trend" not in stats["Temperature"]
    assert "trend" not in stats["Baro-Pressure"]
