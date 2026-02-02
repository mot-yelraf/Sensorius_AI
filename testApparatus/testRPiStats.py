import os
import sqlite3
import pytest
from saiStats import saiStats
from saiDataLogger import saiDataLogger
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TEST_DB = os.getenv("TEST_STATS_DB", "test_sensor_data.db")

@pytest.fixture(scope="module")
def setup_test_db():
    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                sensor_id TEXT,
                metric TEXT,
                value REAL
            )
        """)

        now = datetime.utcnow()
        old = now - timedelta(days=2)
        recent = now - timedelta(hours=1)

        cursor.executemany(
            "INSERT INTO readings (timestamp, sensor_id, metric, value) VALUES (?, ?, ?, ?)",
            [
                (old.isoformat(), "sensor_001", "temp", 20.0),
                (recent.isoformat(), "sensor_001", "temp", 25.0),
                (recent.isoformat(), "sensor_001", "temp", 23.0),
                (recent.isoformat(), "sensor_001", "rh", 50.0),
                (recent.isoformat(), "sensor_001", "rh", 60.0),
                (recent.isoformat(), "sensor_001", "vpd", 0.3),
                (recent.isoformat(), "sensor_001", "vpd", 1.5),
                (recent.isoformat(), "sensor_001", "vpd", 1.8),
                (recent.isoformat(), "sensor_001", "temp", 40.0),
                (recent.isoformat(), "sensor_001", "rh", 70.0),
                (recent.isoformat(), "sensor_001", "vpd", 4.2),
                (recent.isoformat(), "sensor_001", "vpd", 0.0),
                (recent.isoformat(), "sensor_001", "vpd", 5.0)
            ]
        )
        conn.commit()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

def test_get_24hr_stats(setup_test_db):
    stats = saiStats(db_path=TEST_DB)
    results = stats.get_24hr_stats("sensor_001")

    assert "temp" in results
    assert "rh" in results
    assert "vpd" in results

    assert results["temp"]["min"] == 23.0
    assert results["temp"]["max"] == 40.0
    assert round(results["temp"]["avg"], 1) >= 25.0

    assert results["rh"]["min"] == 50.0
    assert results["rh"]["max"] == 70.0
    assert round(results["rh"]["avg"], 1) >= 55.0

    assert results["vpd"]["min"] == 0.0
    assert results["vpd"]["max"] == 5.0
    assert round(results["vpd"]["avg"], 2) >= 1.2

def test_log_and_query_logger(setup_test_db):
    logger = saiDataLogger(db_path=TEST_DB)
    timestamp = datetime.utcnow().isoformat()
    sample = {"temp": 26.4, "rh": 47.2, "vpd": 1.0}

    logger.log_readings(timestamp, "sensor_001", sample)

    # Check manually via SQL
    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM readings WHERE timestamp = ?", (timestamp,))
        count = cursor.fetchone()[0]
        assert count == len(sample)
