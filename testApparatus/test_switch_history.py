"""Verify bounded switch history preserves identities and complete windows.

Use isolated SQLite data written through the normal switch-event logger,
including legacy rows without an epoch and more than ten transitions.
"""

import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from sensorius.saiDataLogger import saiDataLogger


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = saiDataLogger(str(tmp_path / "history.db"))
    logger.upsert_switch_identity(switch_key="hub::channel-1", switch_id="hub", label="Fan")
    end = datetime(2026, 10, 16, 18, tzinfo=timezone.utc).timestamp()
    for index in range(14):
        when = datetime.fromtimestamp(end - (13 - index) * 3600, timezone.utc).isoformat()
        logger.log_switch_event("hub::channel-1", index % 2 == 0, timestamp=when, source="manual")
    logger.log_switch_event("other::channel-2", True, timestamp=datetime.fromtimestamp(end, timezone.utc).isoformat())
    yield logger, end
    logger.close()


def test_history_reads_all_events_and_resolves_canonical_and_ui_aliases(history):
    logger, end = history
    for key in ("hub::channel-1", "hub::Fan", "channel-1::Fan"):
        events = logger.get_switch_history(key, end - 24 * 3600, end)
        assert len(events) == 14
        assert events[0]["epoch"] == end - 13 * 3600
        assert events[-1]["epoch"] == end
        assert events[0]["state"] is True
        assert events[-1]["state"] is False
        assert all(event["source"] == "manual" for event in events)
    assert len(logger.get_switch_history("hub::Fan", end - 3600, end)) == 2
    assert logger.get_switch_history("missing::Fan", end - 86400, end) == []


def test_history_includes_legacy_null_epoch_without_changing_other_queries(history):
    logger, end = history
    with sqlite3.connect(logger.db_path) as conn:
        conn.execute("UPDATE sw_events SET ts_epoch = NULL WHERE switch_key = ?", ("hub::channel-1",))
    assert len(logger.get_switch_history("hub::Fan", end - 86400, end)) == 14
    assert len(logger.get_last_switch_events("hub::Fan", limit=5)) == 5


@pytest.mark.asyncio
async def test_history_route_validates_windows_and_returns_complete_range(history, monkeypatch):
    import sensorius.saiWebRoutes as routes
    from testApparatus.test_ecowitt_routes import _FastStats, _Settings

    logger, end = history
    monkeypatch.setattr(routes, "FastStats", _FastStats)
    monkeypatch.setattr(routes, "data_logger", logger)
    monkeypatch.setattr(routes.time, "time", lambda: end)
    monkeypatch.delenv("SENSORIUS_API_KEY", raising=False)
    app = FastAPI()
    app.state.data_logger = logger
    await routes.register_routes(app, _Settings(), object(), object(), None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for hours, count in ((24, 14), (6, 7), (1, 2)):
            response = await client.get("/switch/history", params={"switch_key": "channel-1::Fan", "hours": hours})
            assert response.status_code == 200
            assert len(response.json()["events"]) == count
            assert response.json()["start_epoch"] == end - hours * 3600
        assert (await client.get("/switch/history", params={"switch_key": "hub::Fan", "hours": 12})).status_code == 422
        assert (await client.get("/switch/history")).status_code == 422
