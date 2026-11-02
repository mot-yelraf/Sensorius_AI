"""Verify AcuRite counter continuity through gaps, failures and restarts.

Controlled counter observations exercise the real persistence and rolling-sum
paths without USB hardware or assigning the console's daily total to history.
"""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from sensorius.saiAcuRite import AcuRiteUsbIngest
from sensorius.saiDataLogger import saiDataLogger
from sensorius.weather_adapters.observations import Observation, RainPolicy
from test_weather_devices import service
from test_datalogger_migration import _create_legacy_db


def counter_feed(current, monkeypatch):
    start = datetime.now(timezone.utc) - timedelta(days=2)
    sample = {}

    async def read():
        current.adapter._identity = "rain-continuity-test"
        if sample.get("missing"):
            return None
        stamp = start + timedelta(seconds=sample["seconds"])
        return Observation(sample["source"], stamp, stamp, {"Wind Speed": 0},
                           RainPolicy(kind="rolling", modulus=81.92), sample["counter"],
                           stamp if sample["counter"] is not None else None)

    monkeypatch.setattr(current, "read", read)
    monkeypatch.setattr(current.adapter, "status", lambda: {
        "enabled": True, "station": {"reporting": not sample.get("missing")}})

    async def poll(counter, seconds, *, source="acurite:rain-continuity-test:A", missing=False):
        sample.update(counter=counter, seconds=seconds, source=source, missing=missing)
        return await current.poll_once()

    return poll


def stored_values(logger, sensor_id):
    return logger.sensor_values[sensor_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", [60, 180, 300, 3600, 86400])
async def test_valid_counter_increases_survive_reporting_gaps(service, monkeypatch, gap):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    assert await poll(35.75, 0)
    assert await poll(35.76, gap)
    values = stored_values(logger, current._sensor_id)
    assert values["Rain"] == pytest.approx(.01)
    assert values["Rain Last 24h"] == pytest.approx(.01)


@pytest.mark.asyncio
async def test_missing_rain_reports_and_usb_outages_preserve_counter(service, monkeypatch):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    await poll(35.75, 0)
    for seconds in (60, 120, 180):
        await poll(None, seconds)
        assert stored_values(logger, current._sensor_id)["Rain"] is None
    assert not await poll(None, 240, missing=True)
    await poll(35.87, 300)
    assert stored_values(logger, current._sensor_id)["Rain"] == pytest.approx(.12)


@pytest.mark.asyncio
async def test_restart_restores_durable_counter_without_replaying_rain(service, monkeypatch):
    current, settings, logger, manager = service
    poll = counter_feed(current, monkeypatch)
    await poll(35.75, 0)
    await poll(35.76, 180)
    sid = current._sensor_id
    checkpoint = logger.get_rain_counter_checkpoint(sid)
    logger.close()
    reopened = saiDataLogger(logger.db_path)
    try:
        restarted = AcuRiteUsbIngest(settings=settings, data_logger=reopened, sensor_manager=manager)
        poll = counter_feed(restarted, monkeypatch)
        # Use the exact persisted sample time, including subsecond precision.
        async def read(counter, stamp):
            restarted.adapter._identity = "rain-continuity-test"
            return Observation(checkpoint[0], stamp, stamp, {"Wind Speed": 0},
                               RainPolicy(kind="rolling", modulus=81.92), counter, stamp)
        monkeypatch.setattr(restarted, "read", lambda: read(35.76, checkpoint[2]))
        assert await restarted.poll_once()
        assert stored_values(reopened, sid)["Rain"] is None
        monkeypatch.setattr(restarted, "read", lambda: read(35.80, checkpoint[2] + timedelta(seconds=180)))
        assert await restarted.poll_once()
        assert stored_values(reopened, sid)["Rain"] == pytest.approx(.04)
        assert stored_values(reopened, sid)["Rain Last 24h"] == pytest.approx(.05)
        assert "Rain Counter" not in reopened.get_available_metrics(sid)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_checkpoint_failure_rolls_back_readings_and_rain(service, monkeypatch):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    notifications = []
    logger.add_readings_listener(lambda *args: notifications.append(args))
    await poll(35.75, 0)
    sid = current._sensor_id
    baseline = logger.get_rain_counter_checkpoint(sid)
    with sqlite3.connect(logger.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        conn.execute("CREATE TRIGGER reject_rain_checkpoint BEFORE UPDATE ON weather_rain_counters BEGIN SELECT RAISE(ABORT, 'test checkpoint failure'); END")
    with pytest.raises(RuntimeError, match="saved"):
        await poll(35.87, 180)
    assert logger.get_rain_counter_checkpoint(sid) == baseline
    assert current._baseline == baseline
    assert len(notifications) == 1
    with sqlite3.connect(logger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == before
        conn.execute("DROP TRIGGER reject_rain_checkpoint")
    await poll(35.87, 180)
    assert stored_values(logger, sid)["Rain"] == pytest.approx(.12)
    assert stored_values(logger, sid)["Rain Last 24h"] == pytest.approx(.12)
    assert len(notifications) == 2


@pytest.mark.asyncio
async def test_wrap_reset_and_changed_source_have_separate_baselines(service, monkeypatch):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    await poll(81.90, 0)
    await poll(.02, 180)
    assert stored_values(logger, current._sensor_id)["Rain"] == pytest.approx(.04)
    await poll(.01, 240)
    assert stored_values(logger, current._sensor_id)["Rain"] is None
    await poll(.03, 300)
    assert stored_values(logger, current._sensor_id)["Rain"] == pytest.approx(.02)
    await poll(50, 360, source="acurite:rain-continuity-test:B")
    assert stored_values(logger, current._sensor_id)["Rain"] is None
    await poll(50.01, 420, source="acurite:rain-continuity-test:B")
    assert stored_values(logger, current._sensor_id)["Rain"] == pytest.approx(.01)


@pytest.mark.asyncio
async def test_old_samples_and_long_gaps_do_not_invent_recent_rain(service, monkeypatch):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    await poll(35.75, 60)
    baseline = logger.get_rain_counter_checkpoint(current._sensor_id)
    await poll(35.74, 0)
    assert logger.get_rain_counter_checkpoint(current._sensor_id) == baseline
    assert stored_values(logger, current._sensor_id)["Rain"] is None
    await poll(35.90, 86461)
    assert stored_values(logger, current._sensor_id)["Rain"] is None
    await poll(35.91, 86521)
    assert stored_values(logger, current._sensor_id)["Rain"] == pytest.approx(.01)


@pytest.mark.asyncio
async def test_multiple_real_increments_in_one_minute_are_all_counted(service, monkeypatch):
    current, _, logger, _ = service
    poll = counter_feed(current, monkeypatch)
    await poll(35.75, 0)
    await poll(35.76, 10)
    await poll(35.78, 20)
    assert stored_values(logger, current._sensor_id)["Rain Last 24h"] == pytest.approx(.03)
    await poll(35.78, 20)
    assert stored_values(logger, current._sensor_id)["Rain"] is None
    assert stored_values(logger, current._sensor_id)["Rain Last 24h"] == pytest.approx(.03)
    await poll(35.78, 80)
    assert stored_values(logger, current._sensor_id)["Rain"] == 0


def test_rain_checkpoint_migration_preserves_legacy_history(tmp_path):
    path = tmp_path / "legacy-rain.db"
    _create_legacy_db(str(path))
    stamp = datetime.now(timezone.utc)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO readings(timestamp, sensor_id, metric, value) VALUES (?, ?, ?, ?)",
                     (stamp.isoformat(), "acurite-legacy", "Rain", .12))
    with saiDataLogger(path) as logger:
        assert logger.get_rain_counter_checkpoint("acurite-legacy") is None
        assert logger.get_metric_sum_for_window("acurite-legacy", "Rain", end_epoch=stamp.timestamp()) == pytest.approx(.12)
        assert logger.log_readings(stamp, "acurite-legacy", {"Rain": .01},
                                   rain_checkpoint=("acurite:legacy:A", 35.76, stamp))
    with saiDataLogger(path) as reopened:
        assert reopened.get_rain_counter_checkpoint("ACURITE-LEGACY") == ("acurite:legacy:A", 35.76, stamp)
        assert reopened.get_metric_sum_for_window("acurite-legacy", "Rain", end_epoch=stamp.timestamp()) == pytest.approx(.13)
