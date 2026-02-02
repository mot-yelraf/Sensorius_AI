import sqlite3
from collections import defaultdict

db_path = r"/home/twfarley/saiSensoriusHA/sensorius_data.db"

def table_exists(conn, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,))
    return cur.fetchone() is not None

# ---------- Schema ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()

    for (table_name,) in tables:
        print(f"\n[SCHEMA] Table: {table_name}")
        cursor.execute(f"PRAGMA table_info({table_name});")
        for col in cursor.fetchall():
            col_id, col_name, col_type, not_null, default_val, pk = col
            print(f"  - {col_name} ({col_type}){' PRIMARY KEY' if pk else ''}")

# ---------- Sensor IDs ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "readings"):
        cursor.execute("SELECT DISTINCT sensor_id FROM readings;")
        sensor_ids = cursor.fetchall()
        print("[SENSOR IDS]")
        for (sensor_id,) in sensor_ids:
            print(f" - {sensor_id}")

# ---------- Metrics ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "readings"):
        cursor.execute("SELECT DISTINCT metric FROM readings;")
        metrics = cursor.fetchall()
        print("[METRICS]")
        for (metric,) in metrics:
            print(f" - {metric}")

# ---------- Switch registry ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "switch_ids"):
        cursor.execute("""
            SELECT switch_key, switch_id, label, IFNULL(location, '')
            FROM switch_ids
            ORDER BY switch_key COLLATE NOCASE
        """)
        rows = cursor.fetchall()
        print("[SWITCHES]")
        if not rows:
            print(" (none)")
        for switch_key, switch_id, label, location in rows:
            loc_disp = f" @ {location}" if location else ""
            print(f" - {switch_key}  ({switch_id} :: {label}){loc_disp}")

# ---------- Switch last state per switch_key ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "sw_events"):
        # Latest event row per switch_key using ISO8601 text timestamps
        cursor.execute("""
            SELECT e.switch_key, e.timestamp, e.state, IFNULL(e.source,''), IFNULL(e.sensor_id,'')
            FROM sw_events e
            JOIN (
                SELECT switch_key, MAX(timestamp) AS max_ts
                FROM sw_events
                GROUP BY switch_key
            ) latest
            ON latest.switch_key = e.switch_key AND latest.max_ts = e.timestamp
            ORDER BY e.switch_key COLLATE NOCASE
        """)
        rows = cursor.fetchall()
        print("[SWITCH LAST STATE]")
        if not rows:
            print(" (none)")
        for switch_key, ts, state, source, sid in rows:
            state_str = "On" if int(state) else "Off"
            src = f" src={source}" if source else ""
            sidp = f" sid={sid}" if sid else ""
            print(f" - {switch_key}: {state_str} @ {ts}{src}{sidp}")

# ---------- Recent switch events (last 5 per switch) ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "sw_events"):
        # Collect last N events per switch_key
        N = 5
        cursor.execute("""
            SELECT switch_key, timestamp, state, IFNULL(source,''), IFNULL(sensor_id,'')
            FROM sw_events
            ORDER BY switch_key COLLATE NOCASE, timestamp DESC
        """)
        rows = cursor.fetchall()

        recent_by_key = defaultdict(list)
        for switch_key, ts, state, source, sid in rows:
            if len(recent_by_key[switch_key]) < N:
                recent_by_key[switch_key].append((ts, int(state), source, sid))

        print(f"[RECENT SWITCH EVENTS] (last {N})")
        if not recent_by_key:
            print(" (none)")
        for switch_key in sorted(recent_by_key.keys(), key=lambda k: k.lower()):
            print(f" - {switch_key}")
            for ts, state, source, sid in recent_by_key[switch_key]:
                state_str = "On" if state else "Off"
                src = f" src={source}" if source else ""
                sidp = f" sid={sid}" if sid else ""
                print(f"    · {ts}: {state_str}{src}{sidp}")

with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "switch_ids") and table_exists(conn, "sw_events"):
        cursor.execute("""
            SELECT s.switch_key, s.switch_id, s.label, IFNULL(s.location,'')
            FROM switch_ids AS s
            LEFT JOIN (
                SELECT DISTINCT switch_key FROM sw_events
            ) AS e ON e.switch_key = s.switch_key
            WHERE e.switch_key IS NULL
            ORDER BY s.switch_key COLLATE NOCASE
        """)
        rows = cursor.fetchall()
        print("[SWITCHES WITHOUT EVENTS]")
        if not rows:
            print(" (none)")
        for switch_key, switch_id, label, location in rows:
            loc = f" @ {location}" if location else ""
            print(f" - {switch_key} ({switch_id} :: {label}){loc}")
            
# ---------- Switch event counts ----------
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    if table_exists(conn, "sw_events"):
        cursor.execute("""
            SELECT switch_key, COUNT(*) AS cnt
            FROM sw_events
            GROUP BY switch_key
            ORDER BY switch_key COLLATE NOCASE
        """)
        rows = cursor.fetchall()
        print("[SWITCH EVENT COUNTS]")
        if not rows:
            print(" (none)")
        for switch_key, cnt in rows:
            print(f" - {switch_key}: {cnt}")
# ---------- Time-range report ----------
import logging

# ——— user-defined (top) ———
database_path: str = r"/home/twfarley/saiSensoriusHA/sensorius_data.db"  # override if needed
show_per_metric: bool = True   # set False to skip per-metric ranges
per_metric_limit: int = 50     # safety cap for metrics per sensor
per_switch_limit: int = 200    # safety cap for switch keys

# logger config (reuse your project’s pattern if you already configure loggers)
logger = logging.getLogger("db_time_ranges")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def _fmt(ts: str | None) -> str:
    """Compact formatter: 'YYYY-MM-DD HH:MM:SS[±TZ]' (strip micros, T)."""
    if not ts:
        return "--"
    try:
        # quick microsecond strip before replacing 'T'
        import re
        ts_no_micro = re.sub(r"\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$)", "", ts)
        return ts_no_micro.replace("T", " ")
    except Exception:
        return ts

with sqlite3.connect(database_path) as conn:
    cur = conn.cursor()

    # ---- readings table: overall min/max ----
    if table_exists(conn, "readings"):
        cur.execute("SELECT COUNT(*) FROM readings;")
        readings_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM readings;")
        readings_min, readings_max = cur.fetchone() or (None, None)
        logger.info("[READINGS] rows=%s  range=[%s  →  %s]",
                    readings_count, _fmt(readings_min), _fmt(readings_max))

        # ---- per-sensor min/max ----
        cur.execute("""
            SELECT sensor_id, MIN(timestamp) AS t_min, MAX(timestamp) AS t_max, COUNT(*) AS cnt
            FROM readings
            GROUP BY sensor_id
            ORDER BY sensor_id COLLATE NOCASE
        """)
        per_sensor = cur.fetchall()
        if per_sensor:
            logger.info("[READINGS • per-sensor]")
            for sensor_id, t_min, t_max, cnt in per_sensor:
                logger.info("  - %s  (%s rows)  [%s → %s]",
                            sensor_id, cnt, _fmt(t_min), _fmt(t_max))

        # ---- per-sensor, per-metric (optional) ----
        if show_per_metric:
            logger.info("[READINGS • per-sensor • per-metric] (capped to %d metrics per sensor)", per_metric_limit)
            # gather top metrics per sensor by row count (to keep output sane)
            cur.execute("""
                WITH counts AS (
                  SELECT sensor_id, metric, COUNT(*) AS c
                  FROM readings
                  GROUP BY sensor_id, metric
                )
                SELECT sensor_id, metric
                FROM (
                  SELECT sensor_id, metric, c,
                         ROW_NUMBER() OVER (PARTITION BY sensor_id ORDER BY c DESC) AS rn
                  FROM counts
                )
                WHERE rn <= ?
                ORDER BY sensor_id COLLATE NOCASE, rn ASC
            """, (per_metric_limit,))
            sensor_metric_pairs = cur.fetchall()

            # batch query for each pair
            for sid, metric in sensor_metric_pairs:
                cur.execute("""
                    SELECT COUNT(*) AS cnt, MIN(timestamp), MAX(timestamp)
                    FROM readings
                    WHERE sensor_id = ? COLLATE NOCASE AND metric = ? COLLATE NOCASE
                """, (sid, metric))
                cnt, tmin, tmax = cur.fetchone() or (0, None, None)
                logger.info("    · %s :: %s  (%s rows)  [%s → %s]",
                            sid, metric, cnt, _fmt(tmin), _fmt(tmax))
    else:
        logger.info("[READINGS] table not found")

    # ---- sw_events table: overall & per-switch ----
    if table_exists(conn, "sw_events"):
        cur.execute("SELECT COUNT(*) FROM sw_events;")
        sw_cnt = cur.fetchone()[0]
        cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM sw_events;")
        sw_min, sw_max = cur.fetchone() or (None, None)
        logger.info("[SW_EVENTS] rows=%s  range=[%s  →  %s]", sw_cnt, _fmt(sw_min), _fmt(sw_max))

        logger.info("[SW_EVENTS • per-switch_key] (capped to %d)", per_switch_limit)
        cur.execute("""
            SELECT switch_key, MIN(timestamp) AS t_min, MAX(timestamp) AS t_max, COUNT(*) AS cnt
            FROM sw_events
            GROUP BY switch_key
            ORDER BY switch_key COLLATE NOCASE
            LIMIT ?
        """, (per_switch_limit,))
        for switch_key, t_min, t_max, cnt in cur.fetchall():
            logger.info("  - %s  (%s rows)  [%s → %s]",
                        switch_key, cnt, _fmt(t_min), _fmt(t_max))
    else:
        logger.info("[SW_EVENTS] table not found")

    # ---- overall DB span across both tables (if both exist) ----
    if table_exists(conn, "readings") and table_exists(conn, "sw_events"):
        cur.execute("""
            SELECT MIN(t) AS global_min, MAX(t) AS global_max
            FROM (
              SELECT MIN(timestamp) AS t FROM readings
              UNION ALL
              SELECT MAX(timestamp) AS t FROM readings
              UNION ALL
              SELECT MIN(timestamp) AS t FROM sw_events
              UNION ALL
              SELECT MAX(timestamp) AS t FROM sw_events
            )
        """)
        gmin, gmax = cur.fetchone() or (None, None)
        logger.info("[DB SPAN] [%s  →  %s]", _fmt(gmin), _fmt(gmax))
