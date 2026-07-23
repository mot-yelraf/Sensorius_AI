"""Fast in-memory stats broadcaster for live dashboard websocket clients.

Design goals:
- Keep reads low-latency by using RAM snapshots from `saiDataLogger`.
- Broadcast at a fixed cadence independent of subscriber count.
- Degrade safely when clients disconnect or become slow.
- Provide explicit start/stop lifecycle hooks for app shutdown.
"""

import asyncio
from contextlib import suppress

import orjson

try:
    # Preferred with FastAPI apps.
    from fastapi import WebSocket
except ImportError:
    # Fallback for Starlette-only runtime.
    from starlette.websockets import WebSocket

from .saiUtils import debug_enabled, printDM

MODULE = "saiFastStats"
DEBUG = debug_enabled(MODULE)


class FastStats:
    def __init__(self, datalogger, statter, hz=1.0, send_timeout_s=0.75):
        if hz <= 0:
            raise ValueError("FastStats hz must be > 0")
        if send_timeout_s <= 0:
            raise ValueError("FastStats send_timeout_s must be > 0")

        self.data_logger = datalogger
        self.statter = statter
        self.period = 1.0 / float(hz)
        self.send_timeout_s = float(send_timeout_s)
        self.subs: set[WebSocket] = set()

        self._subs_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._warned_stats_fallback = False
        self._warned_stats_unavailable = False

    async def start(self):
        task = self._task
        if task and not task.done():
            return
        self._task = asyncio.create_task(self._run(), name="FastStatsBroadcaster")
        self._task.add_done_callback(self._on_done)

    async def stop(self):
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        async with self._subs_lock:
            sockets = tuple(self.subs)
            self.subs.clear()
        for ws in sockets:
            with suppress(Exception):
                await ws.close()

    async def _run(self):
        try:
            while True:
                try:
                    payload = {
                        # Shallow-copy to avoid iterator/view races with concurrent writers.
                        "values": {
                            str(sensor_id): dict(values or {})
                            for sensor_id, values in (self.data_logger.sensor_values or {}).items()
                        },
                        "stats": await self._get_stats_payload(),
                    }
                    blob = orjson.dumps(payload)
                    await self._broadcast(blob)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    printDM(f"[broadcast] tick error: {exc}", location=MODULE)

                await asyncio.sleep(self.period)
        except asyncio.CancelledError:
            if DEBUG:
                printDM("broadcaster cancelled", location=MODULE)
            raise

    async def _get_stats_payload(self) -> dict:
        # Preferred fast path: statter provides an all-sensors aggregator.
        fast_getter = getattr(self.statter, "get_all_stats_fast", None)
        if callable(fast_getter):
            maybe = fast_getter()
            if asyncio.iscoroutine(maybe):
                result = await maybe
            else:
                result = maybe
            return result or {}

        # Compatibility fallback for older/leaner saiStats implementations.
        per_sensor_getter = getattr(self.statter, "get_24hr_stats", None)
        if callable(per_sensor_getter):
            if not self._warned_stats_fallback:
                printDM(
                    "[broadcast] statter missing get_all_stats_fast; using per-sensor get_24hr_stats fallback",
                    location=MODULE,
                )
                self._warned_stats_fallback = True

            sensor_ids = []
            try:
                if hasattr(self.data_logger, "get_available_sensors"):
                    sensor_ids = list(self.data_logger.get_available_sensors() or [])
                if not sensor_ids:
                    sensor_ids = list((getattr(self.data_logger, "sensor_values", {}) or {}).keys())
            except Exception:
                sensor_ids = []

            async def _fetch_for_sensor(sensor_id: str):
                try:
                    stats = await asyncio.to_thread(per_sensor_getter, sensor_id)
                    return sensor_id, (stats or {})
                except Exception as exc:
                    if DEBUG:
                        printDM(
                            f"[broadcast] stats fallback failed for {sensor_id}: {exc}",
                            location=MODULE,
                        )
                    return sensor_id, {}

            pairs = await asyncio.gather(*[_fetch_for_sensor(sid) for sid in sensor_ids])
            return {sid: stats for sid, stats in pairs}

        if not self._warned_stats_unavailable:
            printDM(
                "[broadcast] no supported stats method on statter; sending empty stats",
                location=MODULE,
            )
            self._warned_stats_unavailable = True
        return {}

    async def _broadcast(self, blob: bytes):
        async with self._subs_lock:
            targets = tuple(self.subs)
        if not targets:
            return

        results = await asyncio.gather(
            *(self._send(ws, blob) for ws in targets),
            return_exceptions=True,
        )
        dead = [
            ws for ws, ok in zip(targets, results)
            if (ok is not True)
        ]
        if not dead:
            return

        async with self._subs_lock:
            for ws in dead:
                self.subs.discard(ws)
        for ws in dead:
            with suppress(Exception):
                await ws.close()

    async def _send(self, ws: WebSocket, blob: bytes) -> bool:
        try:
            await asyncio.wait_for(ws.send_bytes(blob), timeout=self.send_timeout_s)
            return True
        except Exception:
            return False

    async def add(self, ws: WebSocket):
        await ws.accept()
        async with self._subs_lock:
            self.subs.add(ws)

    async def remove(self, ws: WebSocket):
        async with self._subs_lock:
            self.subs.discard(ws)
        with suppress(Exception):
            await ws.close()

    def _on_done(self, task: asyncio.Task):
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception as cb_exc:
            printDM(f"[broadcast] task callback error: {cb_exc}", location=MODULE)
            return
        if exc:
            printDM(f"[broadcast] task exited with error: {exc}", location=MODULE)
