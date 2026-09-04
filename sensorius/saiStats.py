"""24-hour stats service and API routes for sensor telemetry.

This module provides:
- a DB-backed stats engine that computes min/avg/max per metric
- epoch-based time-window filtering (last 24 hours) for timezone-safe correctness
- a batched all-sensor aggregation path used by live websocket broadcasters
- a FastAPI `/stats` route that executes DB work off the event loop
"""
import asyncio
from datetime import datetime, timedelta, timezone
import math
import sqlite3
import time

from fastapi import Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from .saiUtils import printDM, debug_enabled

MODULE = "saiStats"
DEBUG = debug_enabled(MODULE)

class saiStats:
    """Compute cached historical statistics and trends from sensor readings."""

    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = db_path
        self._stats_cache_ttl_sec = 5.0
        self._stats_cache: dict[str, tuple[float, dict]] = {}
        self._all_stats_cache: tuple[float, dict] | None = None

    def _recover_if_corrupt(self, exc, source: str) -> bool:
        try:
            from .saiDataLogger import saiDataLogger
            recovered = saiDataLogger.recover_database_after_error(
                self.db_path,
                exc,
                source=f"saiStats.{source}",
            )
            if recovered:
                self._stats_cache.clear()
                self._all_stats_cache = None
            return recovered
        except Exception as recover_exc:
            printDM(
                f"[{source}] DB recovery attempt failed: {recover_exc}",
                location=MODULE,
                level="warning",
            )
            return False

    def _since_epoch_24h(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()

    @staticmethod
    def _pressure_metric(metric_name: str) -> bool:
        name = str(metric_name or "").strip()
        return name in ("Pressure", "Baro-Pressure") or name.endswith(" Baro-Pressure")

    @staticmethod
    def _wind_direction_metric(metric_name: str) -> bool:
        """Return whether a metric contains circular compass-direction values."""
        name = str(metric_name or "").strip().replace("_", " ").replace("-", " ").lower()
        return " ".join(name.split()).endswith("wind direction")

    @staticmethod
    def _circular_mean_degrees(values) -> float | None:
        """Return the circular mean of compass degrees, or None when undefined."""
        directions = []
        for value in values:
            try:
                direction = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(direction):
                directions.append(math.radians(direction % 360.0))
        if not directions:
            return None
        sine_sum = sum(math.sin(direction) for direction in directions)
        cosine_sum = sum(math.cos(direction) for direction in directions)
        if math.hypot(sine_sum, cosine_sum) <= 1e-12 * len(directions):
            return None
        mean = math.degrees(math.atan2(sine_sum, cosine_sum)) % 360.0
        return 0.0 if math.isclose(mean, 360.0, abs_tol=1e-9) else mean

    def _apply_circular_direction_averages(
        self,
        conn: sqlite3.Connection,
        sensor_id: str,
        stats: dict,
        start_epoch: float,
        end_epoch: float | None = None,
    ) -> None:
        """Replace scalar averages for direction metrics with circular means."""
        for metric, metric_stats in stats.items():
            if not self._wind_direction_metric(metric):
                continue
            params: list[object] = [sensor_id, metric, float(start_epoch)]
            end_clause = ""
            if end_epoch is not None:
                end_clause = "AND ts_epoch < ?"
                params.append(float(end_epoch))
            rows = conn.execute(
                f"""
                SELECT value
                FROM readings
                WHERE sensor_id = ? COLLATE NOCASE
                  AND metric = ? COLLATE NOCASE
                  AND value IS NOT NULL
                  AND ts_epoch >= ?
                  {end_clause}
                """,
                tuple(params),
            ).fetchall()
            metric_stats["avg"] = self._circular_mean_degrees(row[0] for row in rows)

    def _metric_trends(
        self,
        conn: sqlite3.Connection,
        *,
        sensor_id: str | None = None,
        window_s: int = 19 * 60,
        pressure_window_s: int = 3 * 60 * 60,
        min_samples: int = 6,
    ) -> dict[str, dict[str, dict]]:
        """Return recent least-squares rates for sensors active in the last 24h."""
        window_s = max(60, int(window_s or 60))
        pressure_window_s = max(window_s, int(pressure_window_s or window_s))
        min_samples = max(2, int(min_samples or 2))
        params: list[object] = [self._since_epoch_24h()]
        sensor_clause = ""
        if sensor_id is not None:
            sensor_clause = "AND sensor_id = ? COLLATE NOCASE"
            params.append(str(sensor_id))
        params.append(pressure_window_s)

        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT sensor_id, MAX(ts_epoch) AS end_ts
                FROM readings
                WHERE value IS NOT NULL
                  AND ts_epoch IS NOT NULL
                  AND ts_epoch >= ?
                  {sensor_clause}
                GROUP BY sensor_id
            )
            SELECT r.sensor_id, r.metric, r.value, r.ts_epoch, latest.end_ts
            FROM readings AS r
            JOIN latest
              ON r.sensor_id = latest.sensor_id
            WHERE r.value IS NOT NULL
              AND r.ts_epoch >= latest.end_ts - ?
              AND r.ts_epoch <= latest.end_ts
            ORDER BY r.ts_epoch ASC
            """,
            params,
        ).fetchall()

        working: dict[tuple[str, str], list[float]] = {}
        for raw_sensor_id, raw_metric, raw_value, raw_timestamp, raw_end_timestamp in rows:
            metric = str(raw_metric or "")
            target_window_s = pressure_window_s if self._pressure_metric(metric) else window_s
            timestamp = float(raw_timestamp)
            end_epoch = float(raw_end_timestamp)
            if timestamp < end_epoch - target_window_s:
                continue
            value = float(raw_value)
            x = (timestamp - end_epoch) / 3600.0
            key = (str(raw_sensor_id or ""), metric)
            item = working.get(key)
            if item is None:
                working[key] = [
                    1.0,
                    x,
                    value,
                    x * x,
                    x * value,
                    timestamp,
                    timestamp,
                    float(target_window_s),
                ]
                continue
            item[0] += 1.0
            item[1] += x
            item[2] += value
            item[3] += x * x
            item[4] += x * value
            item[5] = min(item[5], timestamp)
            item[6] = max(item[6], timestamp)

        trends: dict[str, dict[str, dict]] = {}
        for (result_sensor_id, metric), item in working.items():
            count = int(item[0])
            span_s = max(0, int(item[6] - item[5]))
            denominator = (count * item[3]) - (item[1] * item[1])
            if count < min_samples or span_s < 5 * 60 or denominator == 0:
                continue
            rate = ((count * item[4]) - (item[1] * item[2])) / denominator
            target_window_s = int(item[7])
            trends.setdefault(result_sensor_id, {})[metric] = {
                "rate_per_hour": round(rate, 6),
                "samples": count,
                "window_s": span_s,
                "target_window_s": target_window_s,
                "provisional": bool(
                    self._pressure_metric(metric)
                    and span_s < max(0, target_window_s - 90)
                ),
            }
        return trends

    def get_stats_for_range(self, sensor_id, start_epoch: float, end_epoch: float):
        try:
            return self._get_stats_for_range_impl(sensor_id, start_epoch, end_epoch)
        except sqlite3.DatabaseError as exc:
            if self._recover_if_corrupt(exc, "get_stats_for_range"):
                return {}
            raise

    def _get_stats_for_range_impl(self, sensor_id, start_epoch: float, end_epoch: float):
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
            self._apply_circular_direction_averages(
                conn,
                str(sensor_id),
                results,
                float(start_epoch),
                float(end_epoch),
            )

        return results

    def get_24hr_stats(self, sensor_id):
        sid = str(sensor_id or "").strip()
        now_mono = time.monotonic()
        cached = self._stats_cache.get(sid)
        if cached and cached[0] > now_mono:
            return dict(cached[1])
        since_epoch = self._since_epoch_24h()
        now_epoch = datetime.now(timezone.utc).timestamp()
        result = self.get_stats_for_range(sensor_id, since_epoch, now_epoch + 1.0)
        with sqlite3.connect(self.db_path) as conn:
            trend_sets = self._metric_trends(conn, sensor_id=sid)
            trends = next(iter(trend_sets.values()), {})
        for metric, trend in trends.items():
            if metric in result:
                result[metric]["trend"] = trend
        self._stats_cache[sid] = (now_mono + self._stats_cache_ttl_sec, dict(result))
        return result

    def get_all_stats_fast(self):
        """Return batched 24h stats for all sensors for dashboard updates."""
        try:
            return self._get_all_stats_fast_impl()
        except sqlite3.DatabaseError as exc:
            if self._recover_if_corrupt(exc, "get_all_stats_fast"):
                return {}
            raise

    def _get_all_stats_fast_impl(self):
        """Return batched 24h stats without sorting every reading twice."""
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
                WITH filtered AS MATERIALIZED (
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
                aggregates AS (
                    SELECT
                        sensor_id,
                        metric,
                        MIN(value) AS min_val,
                        AVG(value) AS avg_val,
                        MAX(value) AS max_val
                    FROM filtered
                    GROUP BY sensor_id, metric
                )
                SELECT
                    sensor_id,
                    metric,
                    min_val,
                    (
                        SELECT timestamp
                        FROM filtered AS min_row
                        WHERE min_row.sensor_id = aggregates.sensor_id
                          AND min_row.metric = aggregates.metric
                          AND min_row.value = aggregates.min_val
                        ORDER BY min_row.ts ASC, min_row.timestamp ASC
                        LIMIT 1
                    ) AS min_ts,
                    avg_val,
                    max_val,
                    (
                        SELECT timestamp
                        FROM filtered AS max_row
                        WHERE max_row.sensor_id = aggregates.sensor_id
                          AND max_row.metric = aggregates.metric
                          AND max_row.value = aggregates.max_val
                        ORDER BY max_row.ts DESC, max_row.timestamp DESC
                        LIMIT 1
                    ) AS max_ts
                FROM aggregates
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
            for sensor_id, sensor_stats in results.items():
                self._apply_circular_direction_averages(
                    conn,
                    sensor_id,
                    sensor_stats,
                    since_epoch,
                )
            trends = self._metric_trends(conn)
            for sensor_id, sensor_trends in trends.items():
                sensor_stats = results.get(sensor_id)
                if sensor_stats is None:
                    continue
                for metric, trend in sensor_trends.items():
                    if metric in sensor_stats:
                        sensor_stats[metric]["trend"] = trend

        self._all_stats_cache = (now_mono + self._stats_cache_ttl_sec, dict(results))
        return results

def create_stats_router(settings, gc_mgr, data_logger=None):
    """Create the statistics router, optionally reusing the runtime logger."""
    from .saiDataLogger import saiDataLogger
    router = APIRouter()
    data_logger = data_logger or saiDataLogger()
    statter = saiStats(db_path=getattr(data_logger, "db_path", "sensorius_data.db"))

    @router.get("/stats", response_class=JSONResponse)
    async def get_24hr_stats(sensor_id: str = Query(None)):
        all_sensor_ids = settings.get_all_sensor_ids()
        available = await asyncio.to_thread(data_logger.get_available_sensors)
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
