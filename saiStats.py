"""24-hour stats service and API routes for sensor telemetry.

This module provides:
- a DB-backed stats engine that computes min/avg/max per metric
- epoch-based time-window filtering (last 24 hours) for timezone-safe correctness
- a batched all-sensor aggregation path used by live websocket broadcasters
- a FastAPI `/stats` route that executes DB work off the event loop
"""
import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import time

from fastapi import Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from saiUtils import printDM, debug_enabled

MODULE = "saiStats"
DEBUG = debug_enabled(MODULE)

class saiStats:
    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = db_path
        self._stats_cache_ttl_sec = 5.0
        self._stats_cache: dict[str, tuple[float, dict]] = {}
        self._all_stats_cache: tuple[float, dict] | None = None

    def _since_epoch_24h(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()

    def get_stats_for_range(self, sensor_id, start_epoch: float, end_epoch: float):
        results = {}

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                WITH filtered AS (
                    SELECT metric, value, timestamp, ts_epoch AS ts
                    FROM readings
                    WHERE sensor_id = ? COLLATE NOCASE
                      AND value IS NOT NULL
                      AND ts_epoch >= ?
                      AND ts_epoch < ?
                ),
                ranked AS (
                    SELECT
                        metric,
                        value,
                        timestamp,
                        ROW_NUMBER() OVER (
                            PARTITION BY metric
                            ORDER BY value ASC, ts ASC, timestamp ASC
                        ) AS rn_min,
                        ROW_NUMBER() OVER (
                            PARTITION BY metric
                            ORDER BY value DESC, ts DESC, timestamp DESC
                        ) AS rn_max,
                        AVG(value) OVER (PARTITION BY metric) AS avg_value
                    FROM filtered
                )
                SELECT
                    metric,
                    MAX(CASE WHEN rn_min = 1 THEN value END) AS min_val,
                    MAX(CASE WHEN rn_min = 1 THEN timestamp END) AS min_ts,
                    MAX(avg_value) AS avg_val,
                    MAX(CASE WHEN rn_max = 1 THEN value END) AS max_val,
                    MAX(CASE WHEN rn_max = 1 THEN timestamp END) AS max_ts
                FROM ranked
                GROUP BY metric
                """,
                (sensor_id, float(start_epoch), float(end_epoch)),
            )
            for metric, min_val, min_ts, avg_val, max_val, max_ts in cursor.fetchall():
                results[metric] = {
                    "min": min_val,
                    "min_ts": min_ts,
                    "avg": avg_val,
                    "max": max_val,
                    "max_ts": max_ts,
                }

        return results

    def get_24hr_stats(self, sensor_id):
        sid = str(sensor_id or "").strip()
        now_mono = time.monotonic()
        cached = self._stats_cache.get(sid)
        if cached and cached[0] > now_mono:
            return dict(cached[1])
        since_epoch = self._since_epoch_24h()
        result = self.get_stats_for_range(sensor_id, since_epoch, datetime.now(timezone.utc).timestamp() + 1.0)
        self._stats_cache[sid] = (now_mono + self._stats_cache_ttl_sec, dict(result))
        return result

    def get_all_stats_fast(self):
        """Return 24h stats for all sensors in one DB pass for websocket broadcasting."""
        now_mono = time.monotonic()
        if self._all_stats_cache and self._all_stats_cache[0] > now_mono:
            return {
                sensor_id: dict(metrics)
                for sensor_id, metrics in self._all_stats_cache[1].items()
            }
        results = {}
        since_epoch = self._since_epoch_24h()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                WITH filtered AS (
                    SELECT
                        sensor_id,
                        metric,
                        value,
                        timestamp,
                        ts_epoch AS ts
                    FROM readings
                    WHERE value IS NOT NULL
                      AND ts_epoch >= ?
                ),
                ranked AS (
                    SELECT
                        sensor_id,
                        metric,
                        value,
                        timestamp,
                        ROW_NUMBER() OVER (
                            PARTITION BY sensor_id, metric
                            ORDER BY value ASC, ts ASC, timestamp ASC
                        ) AS rn_min,
                        ROW_NUMBER() OVER (
                            PARTITION BY sensor_id, metric
                            ORDER BY value DESC, ts DESC, timestamp DESC
                        ) AS rn_max,
                        AVG(value) OVER (PARTITION BY sensor_id, metric) AS avg_value
                    FROM filtered
                )
                SELECT
                    sensor_id,
                    metric,
                    MAX(CASE WHEN rn_min = 1 THEN value END) AS min_val,
                    MAX(CASE WHEN rn_min = 1 THEN timestamp END) AS min_ts,
                    MAX(avg_value) AS avg_val,
                    MAX(CASE WHEN rn_max = 1 THEN value END) AS max_val,
                    MAX(CASE WHEN rn_max = 1 THEN timestamp END) AS max_ts
                FROM ranked
                GROUP BY sensor_id, metric
                """,
                (since_epoch,),
            )
            for sensor_id, metric, min_val, min_ts, avg_val, max_val, max_ts in cursor.fetchall():
                sensor_stats = results.setdefault(sensor_id, {})
                sensor_stats[metric] = {
                    "min": min_val,
                    "min_ts": min_ts,
                    "avg": avg_val,
                    "max": max_val,
                    "max_ts": max_ts,
                }

        self._all_stats_cache = (now_mono + self._stats_cache_ttl_sec, dict(results))
        return results

def create_stats_router(settings, gc_mgr):
    from saiDataLogger import saiDataLogger
    router = APIRouter()
    statter = saiStats()
    data_logger = saiDataLogger()

    @router.get("/stats", response_class=JSONResponse)
    async def get_24hr_stats(sensor_id: str = Query(None)):
        all_sensor_ids = settings.get_all_sensor_ids()
        available = data_logger.get_available_sensors()
        valid_ids = sorted(set(all_sensor_ids) | set(available))

        if not sensor_id or sensor_id not in valid_ids:
            if all_sensor_ids:
                sensor_id = all_sensor_ids[0]
            elif available:
                sensor_id = available[0]
            else:
                return JSONResponse({"error": "No sensors available"}, status_code=404)

        try:
            stats = await asyncio.to_thread(statter.get_24hr_stats, sensor_id)
        except Exception as exc:
            printDM(f"/stats failed for sensor_id={sensor_id}: {exc}", location=MODULE)
            return JSONResponse({"error": "Unable to fetch stats"}, status_code=500)
        return JSONResponse(stats)

    return router
