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

from fastapi import Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from saiUtils import printDM, debug_enabled

MODULE = "saiStats"
DEBUG = debug_enabled(MODULE)

class saiStats:
    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = db_path

    def _since_epoch_24h(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()

    def get_24hr_stats(self, sensor_id):
        results = {}
        since_epoch = self._since_epoch_24h()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                WITH filtered AS (
                    SELECT metric, value, timestamp, COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) AS ts
                    FROM readings
                    WHERE LOWER(sensor_id) = LOWER(?)
                      AND value IS NOT NULL
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?
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
                (sensor_id, since_epoch),
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

    def get_all_stats_fast(self):
        """Return 24h stats for all sensors in one DB pass for websocket broadcasting."""
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
                        COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) AS ts
                    FROM readings
                    WHERE value IS NOT NULL
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?
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
