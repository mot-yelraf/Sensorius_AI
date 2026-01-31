"""Stats API endpoints and helpers for sensor history and charts."""
from fastapi import Request, Form, Query
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.routing import APIRouter
from collections import OrderedDict
from datetime import datetime, timedelta
import sqlite3
import json
import os
from rPiUtils import printDM, debug_enabled, get_timestamp
from rPiSettings import rPiSettings

MODULE = "rPiStats"
DEBUG = debug_enabled(MODULE)

class rPiStats:
    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = db_path

    def get_24hr_stats(self, sensor_id):
        results = {}
        now = datetime.utcnow()
        since = (now - timedelta(days=1)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Get distinct metrics from this sensor in last 24h
            cursor.execute("""
                SELECT DISTINCT metric FROM readings
                WHERE sensor_id = ? AND timestamp >= ? COLLATE NOCASE
            """, (sensor_id, since))
            metrics = [row[0] for row in cursor.fetchall()]

            for metric in metrics:
                # Min value + timestamp
                cursor.execute("""
                    SELECT value, timestamp FROM readings
                    WHERE sensor_id = ? AND metric = ? AND timestamp >= ? COLLATE NOCASE
                    ORDER BY value ASC LIMIT 1
                """, (sensor_id, metric, since))
                row = cursor.fetchone()
                min_val, min_ts = row if row else (None, None)

                # Max value + timestamp
                cursor.execute("""
                    SELECT value, timestamp FROM readings
                    WHERE sensor_id = ? AND metric = ? AND timestamp >= ? COLLATE NOCASE
                    ORDER BY value DESC LIMIT 1
                """, (sensor_id, metric, since))
                row = cursor.fetchone()
                max_val, max_ts = row if row else (None, None)

                # Average value
                cursor.execute("""
                    SELECT AVG(value) FROM readings
                    WHERE sensor_id = ? AND metric = ? AND timestamp >= ? COLLATE NOCASE
                """, (sensor_id, metric, since))
                avg_val = cursor.fetchone()[0]

                results[metric] = {
                    "min": min_val,
                    "min_ts": min_ts,
                    "avg": avg_val,
                    "max": max_val,
                    "max_ts": max_ts
                }

        return results

def create_stats_router(settings, gc_mgr):
    from rPiDataLogger import rPiDataLogger
    router = APIRouter()
    statter = rPiStats()
    data_logger = rPiDataLogger()

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

        stats = statter.get_24hr_stats(sensor_id)
        return JSONResponse(stats)

    return router
