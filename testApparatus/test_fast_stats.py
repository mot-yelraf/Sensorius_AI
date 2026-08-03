"""Focused coverage for the low-overhead live statistics broadcaster."""

import asyncio
import threading

import pytest

from sensorius.saiFastStats import FastStats


class _DataLogger:
    sensor_values = {"sensor-a": {"Temperature": 21.5}}


class _WebSocket:
    def __init__(self):
        self.accepted = False
        self.closed = False
        self.sent = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def send_bytes(self, _blob):
        self.sent.set()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fast_stats_does_no_work_without_subscribers():
    class Statter:
        calls = 0

        def get_all_stats_fast(self):
            self.calls += 1
            return {}

    statter = Statter()
    broadcaster = FastStats(_DataLogger(), statter, hz=50)
    await broadcaster.start()
    try:
        await asyncio.sleep(0.05)
        assert statter.calls == 0
    finally:
        await broadcaster.stop()


@pytest.mark.asyncio
async def test_fast_stats_runs_sync_database_work_off_event_loop():
    main_thread = threading.get_ident()

    class Statter:
        calls = 0
        worker_thread = None

        def get_all_stats_fast(self):
            self.calls += 1
            self.worker_thread = threading.get_ident()
            return {"sensor-a": {"Temperature": {"avg": 21.5}}}

    statter = Statter()
    websocket = _WebSocket()
    broadcaster = FastStats(_DataLogger(), statter, hz=50)
    await broadcaster.start()
    try:
        await broadcaster.add(websocket)
        await asyncio.wait_for(websocket.sent.wait(), timeout=1.0)
        assert websocket.accepted is True
        assert statter.calls >= 1
        assert statter.worker_thread != main_thread
    finally:
        await broadcaster.stop()


@pytest.mark.asyncio
async def test_fast_stats_returns_to_idle_after_last_disconnect():
    class Statter:
        calls = 0

        def get_all_stats_fast(self):
            self.calls += 1
            return {}

    statter = Statter()
    websocket = _WebSocket()
    broadcaster = FastStats(_DataLogger(), statter, hz=50)
    await broadcaster.start()
    try:
        await broadcaster.add(websocket)
        await asyncio.wait_for(websocket.sent.wait(), timeout=1.0)
        await broadcaster.remove(websocket)
        calls_after_remove = statter.calls
        await asyncio.sleep(0.06)
        assert websocket.closed is True
        assert statter.calls == calls_after_remove
    finally:
        await broadcaster.stop()
