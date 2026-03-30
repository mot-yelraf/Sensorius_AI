"""Ad hoc SQLite inspection helper for Sensorius sensor and switch history.

This utility prints row counts, metric coverage, time ranges, and recent data so
developers can inspect a captured runtime database outside the web UI.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

# ===== User-configurable =====
DB_PATH: str = r"/home/twfarley/saiSensorius/sensor_data.db"
RECENT_ROWS_PER_SENSOR: int = 5
HOURS_FOR_STATS: int = 24
SHOW_SWITCH_TRANSITIONS_SINCE_HOURS: int = 24
# ============================

logger = logging.getLogger("analyze_sensordb")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _as_display_ts(ts_text: str) -> str:
    """
    DB stores timestamps as ISO strings (e.g. 2025-06-10T07:53:37-05:00).
    For display we keep them as-is, with a small nicety for readability.
    """
    if not ts_text:
        return "--"
    # Keep original; also provide a space for readability if no space present.
    return ts_text.replace("T", " ")


def _get_sensor_ids(conn: sqlite3.Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT sensor_id FROM readings ORDER BY sensor_id")
    return [r[0] for r in cur.fetchall()]


def _get_metrics_for_sensor(conn: sqlite3.Connection, sensor_id: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT metric FROM readings WHERE sensor_id = ? ORDER BY metric",
        (sensor_id,),
    )
    return [r[0] for r in cur.fetchall()]


def _get_row_count_for_sensor(conn: sqlite3.Connection, sensor_id: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM readings WHERE sensor_id = ?",
        (sensor_id,),
    )
    row = cur.fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def _get_time_range_for_sensor(conn: sqlite3.Connection, sensor_id: str) -> tuple[str | None, str | None]:
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM readings WHERE sensor_id = ?",
        (sensor_id,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return (row[0], row[1])


def _get_metric_stats(conn: sqlite3.Connection, sensor_id: str, metric: str, since_iso: str | None) -> dict:
    """
    Returns {min, min_ts, avg, max, max_ts} for the metric within time window.
    """
    cur = conn.cursor()
    params = [sensor_id, metric]
    where = "sensor_id = ? AND metric = ?"
    if since_iso:
        where += " AND timestamp >= ?"
        params.append(since_iso)

    # min
    cur.execute(
        f"SELECT value, timestamp FROM readings WHERE {where} ORDER BY value ASC LIMIT 1",
        params,
    )
    row = cur.fetchone()
    min_val, min_ts = (row[0], row[1]) if row else (None, None)

    # max
    cur.execute(
        f"SELECT value, timestamp FROM readings WHERE {where} ORDER BY value DESC LIMIT 1",
        params,
    )
    row = cur.fetchone()
    max_val, max_ts = (row[0], row[1]) if row else (None, None)

    # avg
    cur.execute(
        f"SELECT AVG(value) FROM readings WHERE {where}",
        params,
    )
    row = cur.fetchone()
    avg_val = row[0] if row else None

    return {
        "min": min_val,
        "min_ts": min_ts,
        "avg": avg_val,
        "max": max_val,
        "max_ts": max_ts,
    }


def _get_recent_rows(conn: sqlite3.Connection, sensor_id: str, limit_rows: int) -> list[tuple[str, str, float]]:
    """
    Returns list of (timestamp, metric, value) for most recent rows of a sensor.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT timestamp, metric, value
        FROM readings
        WHERE sensor_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (sensor_id, limit_rows),
    )
    return cur.fetchall()


def _get_switch_transitions(conn: sqlite3.Connection, switch_sensor_id: str, since_iso: str) -> dict[str, list[tuple[str, int]]]:
    """
    For a given switch sensor_id (e.g., 'Switch_airco'), return transition points
    per metric/relay: { "Fan": [(ts, 1), (ts, 0), ...], "Pump": [...], ... }
    A transition is recorded when value changes vs prior row.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT metric
        FROM readings
        WHERE sensor_id = ?
        GROUP BY metric
        ORDER BY metric
        """,
        (switch_sensor_id,),
    )
    metrics = [r[0] for r in cur.fetchall()]
    transitions: dict[str, list[tuple[str, int]]] = {}

    for metric in metrics:
        cur.execute(
            """
            SELECT timestamp, value
            FROM readings
            WHERE sensor_id = ? AND metric = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (switch_sensor_id, metric, since_iso),
        )
        rows = cur.fetchall()
        last_val = None
        events: list[tuple[str, int]] = []
        for ts, val in rows:
            try:
                v_int = int(val) if val is not None else 0
            except Exception:
                # Be tolerant: treat non-numeric as 0/False
                v_int = 0
            if last_val is None or v_int != last_val:
                events.append((ts, v_int))
                last_val = v_int
        if events:
            transitions[metric] = events

    return transitions


def analyze_sensor_db(db_path: str = DB_PATH):
    try:
        with sqlite3.connect(db_path) as conn:
            logger.info("Opened DB: %s", db_path)

            # Overall stats
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM readings")
            total_rows = int(cur.fetchone()[0])
            logger.info("Total rows in readings: %s", total_rows)

            # Sensors present
            sensor_ids = _get_sensor_ids(conn)
            print("=== SENSOR IDS PRESENT IN DATABASE ===")
            for sid in sensor_ids:
                print(f" - {sid}")

            # Per-sensor summary
            print("\n=== PER-SENSOR SUMMARY ===")
            for sid in sensor_ids:
                row_count = _get_row_count_for_sensor(conn, sid)
                t_min, t_max = _get_time_range_for_sensor(conn, sid)
                t_min_d = _as_display_ts(t_min) if t_min else "--"
                t_max_d = _as_display_ts(t_max) if t_max else "--"
                print(f"{sid}: rows={row_count}, range=[{t_min_d} → {t_max_d}]")

            # Metrics per sensor
            print("\n=== METRICS PER SENSOR ===")
            for sid in sensor_ids:
                metrics = _get_metrics_for_sensor(conn, sid)
                print(f"Sensor ID: {sid}")
                for m in metrics:
                    print(f"   - {m}")

            # Per-metric stats (last N hours)
            print(f"\n=== PER-METRIC STATS (last {HOURS_FOR_STATS}h) ===")
            since_dt = datetime.now().astimezone() - timedelta(hours=HOURS_FOR_STATS)
            since_iso = since_dt.isoformat()
            for sid in sensor_ids:
                print(f"\nSensor ID: {sid}")
                metrics = _get_metrics_for_sensor(conn, sid)
                if not metrics:
                    print("  (no metrics)")
                    continue
                for m in metrics:
                    stats = _get_metric_stats(conn, sid, m, since_iso)
                    min_v = stats["min"]
                    max_v = stats["max"]
                    avg_v = stats["avg"]
                    min_ts = _as_display_ts(stats["min_ts"]) if stats["min_ts"] else "--"
                    max_ts = _as_display_ts(stats["max_ts"]) if stats["max_ts"] else "--"
                    avg_str = f"{avg_v:.2f}" if isinstance(avg_v, (int, float)) else "--"
                    print(f"  {m:>16}: min={min_v!s:>8} @ {min_ts} | avg={avg_str:>8} | max={max_v!s:>8} @ {max_ts}")

            # Recent sample rows (per sensor)
            print(f"\n=== RECENT SAMPLE DATA (last {RECENT_ROWS_PER_SENSOR} rows per sensor) ===")
            for sid in sensor_ids:
                print(f"\nSensor ID: {sid}")
                rows = _get_recent_rows(conn, sid, RECENT_ROWS_PER_SENSOR)
                if not rows:
                    print("  (no rows)")
                    continue
                for ts, metric, val in rows:
                    print(f"  {_as_display_ts(ts)} | {metric} = {val}")

            # Switch transitions (only for sensors that look like switches)
            print(f"\n=== SWITCH TRANSITIONS (last {SHOW_SWITCH_TRANSITIONS_SINCE_HOURS}h) ===")
            since_dt_sw = datetime.now().astimezone() - timedelta(hours=SHOW_SWITCH_TRANSITIONS_SINCE_HOURS)
            since_iso_sw = since_dt_sw.isoformat()

            switch_like = [sid for sid in sensor_ids if sid.lower().startswith("switch_")]
            if not switch_like:
                print("  (no switch_* sensor_ids found)")
            for sid in switch_like:
                print(f"\nSwitch Sensor: {sid}")
                trans = _get_switch_transitions(conn, sid, since_iso_sw)
                if not trans:
                    print("  (no transitions in time window)")
                    continue
                for metric_name, events in trans.items():
                    print(f"  {metric_name}:")
                    for ts, v in events:
                        state = "ON " if v else "OFF"
                        print(f"    {_as_display_ts(ts)} → {state}")

            logger.info("Analysis complete.")

    except Exception as e:
        logger.error("ERROR analyzing DB: %s", e)


if __name__ == "__main__":
    analyze_sensor_db()
