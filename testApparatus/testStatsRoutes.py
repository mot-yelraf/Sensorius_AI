"""Pytest coverage for statistics API route behavior.

These tests seed a lightweight database and verify the stats endpoints return
expected payloads, defaults, and missing-sensor behavior.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sensorius.saiStats import create_stats_router, saiStats as StatsImpl


class _FakeSettings:
    def __init__(self, ids=None):
        self._ids = list(ids or [])

    def get_all_sensor_ids(self):
        return list(self._ids)


class _FakeDataLogger:
    def __init__(self, sensors=None):
        self._sensors = list(sensors or [])

    def get_available_sensors(self):
        return list(self._sensors)


def _seed_stats_db(db_path: str):
    now = datetime.now(timezone.utc)
    rows = [
        # in-window values
        ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).timestamp(), "sensor_001", "temp", 20.0),
        ((now - timedelta(hours=1)).isoformat(), (now - timedelta(hours=1)).timestamp(), "sensor_001", "temp", 30.0),
        # ignored null
        ((now - timedelta(minutes=30)).isoformat(), (now - timedelta(minutes=30)).timestamp(), "sensor_001", "temp", None),
        # old value (outside 24h)
        ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).timestamp(), "sensor_001", "temp", 999.0),
    ]

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ts_epoch REAL,
                sensor_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL
            )
            """
        )
        cur.executemany(
            "INSERT INTO readings (timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


@pytest_asyncio.fixture
async def stats_client(tmp_path, monkeypatch):
    db_path = tmp_path / "stats.db"
    _seed_stats_db(str(db_path))

    class _TestStats(StatsImpl):
        def __init__(self, db_path="sensorius_data.db"):
            super().__init__(db_path=str(db_path_obj))

    db_path_obj = db_path

    def _fake_logger_ctor(*args, **kwargs):
        return _FakeDataLogger(["sensor_001"])

    monkeypatch.setattr("sensorius.saiStats.saiStats", _TestStats)
    monkeypatch.setattr("sensorius.saiDataLogger.saiDataLogger", _fake_logger_ctor)

    app = FastAPI()
    app.include_router(create_stats_router(_FakeSettings(["sensor_001"]), gc_mgr=None))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_stats_json(stats_client):
    response = await stats_client.get("/stats", params={"sensor_id": "sensor_001"})
    assert response.status_code == 200
    payload = response.json()

    assert "temp" in payload
    assert payload["temp"]["min"] == 20.0
    assert payload["temp"]["max"] == 30.0
    assert payload["temp"]["avg"] == 25.0


@pytest.mark.asyncio
async def test_stats_defaults_to_known_sensor(stats_client):
    response = await stats_client.get("/stats", params={"sensor_id": "unknown_sensor"})
    assert response.status_code == 200
    assert "temp" in response.json()


@pytest.mark.asyncio
async def test_stats_404_when_no_sensors(monkeypatch):
    class _TestStats(StatsImpl):
        def __init__(self, db_path="sensorius_data.db"):
            super().__init__(db_path=":memory:")

    monkeypatch.setattr("sensorius.saiStats.saiStats", _TestStats)
    monkeypatch.setattr("sensorius.saiDataLogger.saiDataLogger", lambda *args, **kwargs: _FakeDataLogger([]))

    app = FastAPI()
    app.include_router(create_stats_router(_FakeSettings([]), gc_mgr=None))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/stats")

    assert response.status_code == 404
    assert response.json().get("error") == "No sensors available"
