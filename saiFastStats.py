"""Fast rolling stats calculator for sensor dashboards.

Computes cached summary stats at a fixed cadence for low-latency UI reads.
"""
try:
    # preferred with FastAPI
    from fastapi import WebSocket, WebSocketDisconnect
except ImportError:
    # fallback to Starlette directly
    from starlette.websockets import WebSocket, WebSocketDisconnect

import asyncio, orjson

class FastStats:
    def __init__(self, datalogger, statter, hz=1.0):
        self.data_logger = datalogger            # saiDataLogger (has RAM snapshots)
        self.statter = statter          # your async statter for stats
        self.subs: set[WebSocket] = set()
        self.period = 1.0 / hz
        self._task = None

    async def start(self):
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        # single producer at a fixed cadence, regardless of subscriber count
        while True:
            # build payload from RAM snapshots (O(1)), avoid DB here
            payload = {
                "values":  self.data_logger.sensor_values,   # already in RAM
                "stats": await self.statter.get_all_stats_fast(),  # async & cached
            }
            blob = orjson.dumps(payload)  # C-speed, non-blocking
            # fan-out; drop slow sockets
            dead = []
            for ws in self.subs:
                try:
                    await ws.send_bytes(blob)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.subs.discard(ws)
            await asyncio.sleep(self.period)

    async def add(self, ws: WebSocket):
        await ws.accept()
        self.subs.add(ws)

    async def remove(self, ws: WebSocket):
        self.subs.discard(ws)
        try: await ws.close()
        except: pass
