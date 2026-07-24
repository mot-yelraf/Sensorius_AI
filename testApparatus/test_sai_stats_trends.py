"""Focused coverage for database-backed dashboard metric trends."""

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


def test_trends_require_six_samples_spanning_five_minutes(tmp_path):
    db_path = tmp_path / "short-trend-history.db"
    _seed_readings_db(db_path, samples=5)

    stats = saiStats(str(db_path)).get_24hr_stats("trend-sensor")

    assert "trend" not in stats["Temperature"]
    assert "trend" not in stats["Baro-Pressure"]
