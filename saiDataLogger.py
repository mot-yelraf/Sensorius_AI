"""Persistence and query layer for Sensorius telemetry and switch activity.

This module provides a SQLite (WAL) backed data logger that records:
1. Sensor readings in ``readings`` (timestamped metric values by sensor ID).
2. Switch identity metadata in ``switch_ids``.
3. Switch state transitions in ``sw_events``.

It also exposes read/query helpers used by runtime services and the web UI:
- time-series retrieval for calibration and graphing
- latest-value and latest-timestamp lookups
- sensor/metric discovery
- switch event/state history queries

Operational characteristics:
- Uses a dedicated writer connection plus a re-entrant lock for serialized writes.
- Keeps lightweight in-memory snapshots of latest sensor values.
- Applies defensive parsing and error handling to avoid crashing caller paths.
- Includes one-time legacy migration logic for historical switch rows.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from saiUtils import printDM, debug_enabled
import threading
import os
import time
from typing import Optional, Tuple

MODULE = "saiDataLogger"
DEBUG = debug_enabled(MODULE)

LOCAL_TIMEZONE = ZoneInfo("America/Denver")

# ---- legacy prefixes kept for optional migration only -----------------------
SW_EVENT_PREFIX = "switch_event::"
SW_STATE_PREFIX = "switch_state::"

# ---- public API -----------------------
SW_KEY_DELIM = "::"


def _timestamp_to_epoch(ts_value, default_tz: ZoneInfo) -> Optional[float]:
    """
    Convert a timestamp value to POSIX epoch seconds.

    Supports:
    - int/float epoch values
    - ISO-8601 strings (with or without timezone)
    - numeric strings
    Returns None when conversion is not possible.
    """
    if ts_value is None:
        return None
    try:
        if isinstance(ts_value, (int, float)):
            return float(ts_value)
        text = str(ts_value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt.timestamp()
    except Exception:
        return None


def _normalize_timestamp_input(ts_value, default_tz: ZoneInfo) -> Tuple[str, float]:
    """
    Normalize user/runtime timestamp input into (iso_with_tz, epoch_seconds).
    Falls back to current local time if input is invalid.
    """
    now_dt = datetime.now(default_tz)
    now_iso = now_dt.isoformat()
    now_epoch = now_dt.timestamp()

    epoch = _timestamp_to_epoch(ts_value, default_tz)
    if epoch is None:
        return now_iso, now_epoch

    dt = datetime.fromtimestamp(epoch, default_tz)
    return dt.isoformat(), epoch

def build_switch_key(switch_id: str, channel_or_label: str, channel_id: str | None = None) -> str:
    """
    Canonical switch key constructor used across the app.

    Preferred shape:
      - If channel_id is provided and non-empty:
            "<switch_id>::<channel_id>"
      - Otherwise:
            "<switch_id>::<channel_or_label>"

    This keeps older 2-argument calls working:
        build_switch_key("switch-abc", "Fan")
    while allowing richer callers to pass an explicit channel_id:
        build_switch_key("switch-abc", "Fan", channel_id="S1-")
    """
    switch_id_safe = (switch_id or "").strip()

    if channel_id is not None:
        # Prefer explicit channel_id when provided; fall back to channel_or_label if blank
        chan = str(channel_id or "").strip()
        if not chan:
            chan = str(channel_or_label or "").strip()
    else:
        chan = str(channel_or_label or "").strip()

    return f"{switch_id_safe}{SW_KEY_DELIM}{chan}"

class saiDataLogger:
    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = db_path
        self._init_db()
        self._writer_conn = self._open_conn(check_same_thread=False)
        self._writer_lock = threading.RLock()   # serialize writers across sensors
        self._db_retention_days = self._env_int("SENSORIUS_DB_RETENTION_DAYS", 90, minimum=0)
        self._db_retention_prune_interval_sec = 300.0
        self._next_retention_prune_mono = 0.0

        self.sensor_values = {}       # sensor_id → latest values
        self.sensor_stats = {}        # sensor_id → 24h stats
        self.sensor_metric_names = {} # sensor_id → list of expected metric names
        self._on_readings_written: list = []
        self._on_switch_event_written: list = []
        
        from saiSettings import saiSettings
        _settings = saiSettings(apply_live=False)
        TZ = (_settings.get_setting("Time", "TZ")
              or _settings.get_setting("Time", "tz")
              or "America/Denver")
        self.local_tz = ZoneInfo(TZ)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self) -> None:
        """Close long-lived writer connection used by this logger instance."""
        with self._writer_lock:
            conn = getattr(self, "_writer_conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self._writer_conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # Enable Write-Ahead-Logging, add PRAGMA and indexes, plus new switch tables
    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                cur = conn.cursor()

                # ---- Pragmas (persist) ----
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA temp_store=MEMORY;")
                cur.execute("PRAGMA busy_timeout=3000;")
                cur.execute("PRAGMA cache_size=-65536;")
                cur.execute("PRAGMA wal_autocheckpoint=1000;")

                # ---- Sensor readings table (unchanged) -----------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS readings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,            -- ISO8601
                        ts_epoch REAL,                      -- epoch seconds (UTC comparable)
                        sensor_id TEXT NOT NULL,
                        metric TEXT NOT NULL,
                        value REAL
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_readings_sensor_id_nocase
                    ON readings(sensor_id COLLATE NOCASE)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_readings_sid_ts
                    ON readings(sensor_id COLLATE NOCASE, timestamp DESC)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_readings_sid_metric_ts
                    ON readings(sensor_id COLLATE NOCASE, metric COLLATE NOCASE, timestamp)
                """)

                # ---- switch registry -----------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS switch_ids (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        switch_key TEXT NOT NULL UNIQUE,  -- "<switch_id>::<channel_id>"
                        switch_id  TEXT NOT NULL,
                        label      TEXT NOT NULL,         -- user-visible name ("Fan","Light",...)
                        location   TEXT
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_switch_ids_switch_id
                    ON switch_ids(switch_id COLLATE NOCASE)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_switch_ids_label
                    ON switch_ids(label COLLATE NOCASE)
                """)

                # ---- switch events -------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sw_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp  TEXT NOT NULL,         -- ISO8601
                        ts_epoch   REAL,                  -- epoch seconds (UTC comparable)
                        switch_key TEXT NOT NULL,         -- "<switch_id>::<label>"
                        state      INTEGER NOT NULL,      -- 0 = Off, 1 = On
                        source     TEXT,                  -- 'manual','ui','mqtt','rule', etc.
                        sensor_id  TEXT                   -- lineage/host if useful
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_swe_switch_key_ts
                    ON sw_events(switch_key, timestamp DESC)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_swe_ts
                    ON sw_events(timestamp DESC)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_swe_key_nocase_ts
                    ON sw_events(switch_key COLLATE NOCASE, timestamp DESC)
                """)

                # ---- additive column migrations for existing DBs ----
                cur.execute("PRAGMA table_info(readings)")
                reading_cols = {row[1] for row in cur.fetchall()}
                if "ts_epoch" not in reading_cols:
                    cur.execute("ALTER TABLE readings ADD COLUMN ts_epoch REAL")
                cur.execute("PRAGMA table_info(sw_events)")
                swe_cols = {row[1] for row in cur.fetchall()}
                if "ts_epoch" not in swe_cols:
                    cur.execute("ALTER TABLE sw_events ADD COLUMN ts_epoch REAL")

                # Create ts_epoch indexes only after additive migrations above.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_readings_sid_metric_tse
                    ON readings(sensor_id COLLATE NOCASE, metric COLLATE NOCASE, ts_epoch)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_swe_key_tse
                    ON sw_events(switch_key COLLATE NOCASE, ts_epoch DESC)
                """)

                # Backfill missing ts_epoch values incrementally.
                cur.execute("UPDATE readings SET ts_epoch = strftime('%s', timestamp) WHERE ts_epoch IS NULL")
                cur.execute("UPDATE sw_events SET ts_epoch = strftime('%s', timestamp) WHERE ts_epoch IS NULL")

                conn.commit()

                # ---- idempotent migration of legacy rows -------
                self._maybe_migrate_legacy_switch_rows(cur)
                conn.commit()

        except Exception as e:
            printDM(f"_init_db error: {e}", location=__name__)
            raise

    def _maybe_migrate_legacy_switch_rows(self, cur: sqlite3.Cursor) -> None:
        """
        Copy legacy switch rows into sw_events exactly once per DB.

        Legacy shapes:
          1) metric LIKE 'switch_event::<switch_key>'
             where <switch_key> was historically "<switch_id>::<label>".
             We copy the entire suffix as-is into sw_events.switch_key.

          2) metric IN ('Fan','Light','Pump', ...)
             When we can find a matching row in switch_ids.label, we infer
             the corresponding switch_key and copy that.

        New schema:
          - canonical switch_key is "<switch_id>::<channel_id>" where channel_id
            is the stable SWITCH_N_ID (e.g. "S1-123456").
          - This migration path is strictly for older DBs.
        We guard with a tiny marker table so we don't repeat work.
        """
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS _migration_markers (
                    name TEXT PRIMARY KEY
                )
            """)
            cur.execute("SELECT 1 FROM _migration_markers WHERE name='sw_events_v1'")
            already = cur.fetchone()
            if already:
                return

            migrated = 0

            # (1) Prefixed metrics: switch_event::<switch_key>
            cur.execute("""
                SELECT timestamp, sensor_id, metric, value
                FROM readings
                WHERE metric LIKE ? ESCAPE '\\'
            """, (SW_EVENT_PREFIX.replace("_", "\\_") + "%",))
            rows = cur.fetchall()
            for ts, sid, metric, value in rows:
                switch_key = metric[len(SW_EVENT_PREFIX):].strip()
                if not switch_key:
                    continue
                try:
                    state = 1 if int(value) else 0
                except Exception:
                    state = 1 if str(value).lower() in ("1","true","on") else 0
                cur.execute("""
                    INSERT INTO sw_events(timestamp, ts_epoch, switch_key, state, source, sensor_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ts, _timestamp_to_epoch(ts, LOCAL_TIMEZONE), switch_key, state, "legacy_metric", sid))
                migrated += 1

            # (2) Plain label metrics (Fan/Light/Pump/…):
            # If we can find a switch_ids row with that label, use its switch_key.
            # This is best-effort and only for most recent ~10k rows to keep it light.
            cur.execute("SELECT DISTINCT label, switch_key FROM switch_ids")
            label_to_key = {row[0].lower(): row[1] for row in cur.fetchall()}

            if label_to_key:
                # Clamp to a reasonable volume
                cur.execute("""
                    SELECT timestamp, sensor_id, metric, value
                    FROM readings
                    WHERE metric IN (
                        SELECT label FROM switch_ids
                    )
                    ORDER BY id DESC
                    LIMIT 10000
                """)
                rows = cur.fetchall()
                for ts, sid, metric, value in rows:
                    key = label_to_key.get((metric or "").lower())
                    if not key:
                        continue
                    try:
                        state = 1 if int(value) else 0
                    except Exception:
                        state = 1 if str(value).lower() in ("1","true","on") else 0
                    cur.execute("""
                        INSERT INTO sw_events(timestamp, ts_epoch, switch_key, state, source, sensor_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (ts, _timestamp_to_epoch(ts, LOCAL_TIMEZONE), key, state, "legacy_label", sid))
                    migrated += 1

            cur.execute("INSERT OR IGNORE INTO _migration_markers(name) VALUES('sw_events_v1')")
            if migrated and DEBUG:
                printDM(f"[migration] moved {migrated} legacy switch rows -> sw_events", location=MODULE)

        except Exception as e:
            printDM(f"[migration] error: {e}", location=MODULE)

    def _open_conn(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=check_same_thread
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA cache_size=-65536;")
        return conn

    def _ensure_writer(self):
        with self._writer_lock:
            try:
                if self._writer_conn is None:
                    raise RuntimeError("writer connection missing")
                self._writer_conn.execute("SELECT 1")
            except Exception:
                try:
                    if self._writer_conn is not None:
                        self._writer_conn.close()
                except Exception:
                    pass
                self._writer_conn = self._open_conn(check_same_thread=False)

    @staticmethod
    def _env_int(name: str, default: int, minimum: int = 0) -> int:
        raw = os.getenv(name)
        try:
            value = int(raw) if raw is not None else int(default)
        except Exception:
            value = int(default)
        return max(minimum, value)

    def _maybe_prune_old_rows_locked(self) -> None:
        """
        Throttled retention cleanup for readings + switch events.

        Retention window is controlled via SENSORIUS_DB_RETENTION_DAYS.
        - 0 disables pruning.
        - default is 90 days.
        """
        if self._db_retention_days <= 0:
            return
        now_mono = time.monotonic()
        if now_mono < self._next_retention_prune_mono:
            return

        cutoff_epoch = time.time() - (float(self._db_retention_days) * 86400.0)
        try:
            cur = self._writer_conn.cursor()
            cur.execute(
                """
                DELETE FROM readings
                WHERE COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) < ?
                """,
                (cutoff_epoch,),
            )
            readings_deleted = int(cur.rowcount or 0)
            cur.execute(
                """
                DELETE FROM sw_events
                WHERE COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) < ?
                """,
                (cutoff_epoch,),
            )
            sw_events_deleted = int(cur.rowcount or 0)
            if readings_deleted or sw_events_deleted:
                self._writer_conn.commit()
                if DEBUG:
                    printDM(
                        (
                            f"[retention] pruned readings={readings_deleted}, "
                            f"sw_events={sw_events_deleted}, days={self._db_retention_days}"
                        ),
                        location=MODULE,
                    )
            self._next_retention_prune_mono = now_mono + self._db_retention_prune_interval_sec
        except Exception as e:
            # Keep writes alive even if retention cleanup fails.
            self._next_retention_prune_mono = now_mono + self._db_retention_prune_interval_sec
            printDM(f"[retention] prune error: {e}", location=MODULE)
            
    def get_time_series(self, sensor_id: str, metric: str,
                        start_ts: float, end_ts: float):
        """
        Return (timestamps, values) for a given sensor/metric in the
        [start_ts, end_ts] interval.

        - sensor_id: sensor identifier (case-insensitive)
        - metric: metric name (case-sensitive as stored in DB)
        - start_ts / end_ts: POSIX timestamps (float, seconds since epoch)

        Returns:
            (list_of_epoch_ts, list_of_values)
        """
        try:
            # Decide which timezone to interpret "local" in
            tz = getattr(self, "local_tz", LOCAL_TIMEZONE)

            # Convert epoch seconds to local ISO strings so that the
            # SQLite lexicographic range matches chronological order.
            start_dt = datetime.fromtimestamp(float(start_ts), tz)
            end_dt = datetime.fromtimestamp(float(end_ts), tz)
            start_iso = start_dt.isoformat()
            end_iso = end_dt.isoformat()

            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT timestamp, value, ts_epoch
                    FROM readings
                    WHERE LOWER(sensor_id) = LOWER(?)
                      AND metric = ?
                      AND (
                            (ts_epoch IS NOT NULL AND ts_epoch >= ? AND ts_epoch <= ?)
                         OR (ts_epoch IS NULL AND timestamp >= ? AND timestamp <= ?)
                      )
                    ORDER BY COALESCE(ts_epoch, 0.0) ASC, timestamp ASC
                    """,
                    (sensor_id, metric, float(start_ts), float(end_ts), start_iso, end_iso),
                )
                rows = cur.fetchall()

            ts_list = []
            val_list = []

            for ts_text, value, ts_epoch in rows:
                if not ts_text:
                    # No timestamp → ignore row
                    continue

                # Skip NULL / None values, they cannot be used for calibration
                if value is None:
                    if DEBUG:
                        printDM(
                            f"[get_time_series] skipping NULL value for {sensor_id}/{metric} at {ts_text}",
                            location=MODULE,
                        )
                    continue

                try:
                    val = float(value)
                except (TypeError, ValueError):
                    # Non-numeric garbage; skip but do not kill the whole series
                    if DEBUG:
                        printDM(
                            f"[get_time_series] non-numeric value {value!r} for {sensor_id}/{metric} at {ts_text}",
                            location=MODULE,
                        )
                    continue

                parsed_epoch = None
                try:
                    if ts_epoch is not None:
                        parsed_epoch = float(ts_epoch)
                    else:
                        dt = datetime.fromisoformat(ts_text)
                        parsed_epoch = dt.timestamp()
                except Exception:
                    # If parsing fails for some legacy format, skip safely.
                    if DEBUG:
                        printDM(
                            f"[get_time_series] bad timestamp {ts_text!r} for {sensor_id}/{metric}",
                            location=MODULE,
                        )
                    continue

                ts_list.append(parsed_epoch)
                val_list.append(val)

            return ts_list, val_list

        except Exception as e:
            printDM(
                f"[get_time_series] error for sensor_id={sensor_id}, metric={metric}: {e}",
                location=MODULE,
            )
            return [], []
        
    # ------------------------------- SENSOR API -------------------

    def log_readings(self, timestamp, sensor_id, values: dict):
        """Fast writer using a dedicated WAL connection + in-RAM snapshot."""
        self._ensure_writer()
        timestamp, ts_epoch = _normalize_timestamp_input(
            timestamp, getattr(self, "local_tz", LOCAL_TIMEZONE)
        )

        try:
            rows = [(timestamp, ts_epoch, sensor_id, metric, value) for metric, value in values.items()]
            with self._writer_lock:
                self._writer_conn.executemany(
                    "INSERT INTO readings (timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
                    rows
                )
                self._writer_conn.commit()
                self._maybe_prune_old_rows_locked()

            snap = self.sensor_values.get(sensor_id) or {}
            snap.update(values)
            self.sensor_values[sensor_id] = snap

            # Notify post-write listeners (non-blocking; do not break writer path)
            listeners = list(getattr(self, "_on_readings_written", []) or [])
            if listeners:
                for fn in listeners:
                    try:
                        fn(sensor_id, timestamp, dict(values))
                    except Exception as exc:
                        printDM(
                            f"[log_readings] listener error for {sensor_id}: {exc}",
                            location=MODULE,
                        )

            if DEBUG:
                printDM(f"Logged {len(values)} values for {sensor_id}", location=MODULE)
        except Exception as e:
            printDM(f"Error writing sensor data: {e}", location=MODULE)

    def add_readings_listener(self, listener) -> None:
        """
        Listener signature:
          fn(sensor_id: str, timestamp_iso: str, values: dict) -> None
        """
        try:
            if listener and listener not in self._on_readings_written:
                self._on_readings_written.append(listener)
        except Exception:
            pass

    def get_latest_values(self, sensor_id):
        if sensor_id in self.sensor_values and self.sensor_values[sensor_id]:
            return dict(self.sensor_values[sensor_id])
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT timestamp FROM readings WHERE LOWER(sensor_id)=LOWER(?) "
                    "ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC LIMIT 1",
                    (sensor_id,)
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    if DEBUG:
                        printDM(f"[get_latest_values] No data yet for sensor: {sensor_id}", location=MODULE)
                    return {}
                latest_ts = row[0]
                cur.execute(
                    "SELECT metric, value FROM readings "
                    "WHERE LOWER(sensor_id)=LOWER(?) AND timestamp=?",
                    (sensor_id, latest_ts)
                )
                rows = cur.fetchall()
                return {metric: value for metric, value in rows}
        except Exception as e:
            printDM(f"[get_latest_values] Query error for {sensor_id}: {e}", location=MODULE)
            return {}

    def get_available_sensors(self):
        query = "SELECT DISTINCT sensor_id FROM readings ORDER BY sensor_id"
        try:
            with self._open_conn() as conn:
                return [row[0] for row in conn.execute(query).fetchall()]
        except Exception as e:
            printDM(f"Sensor ID query error: {e}", location=MODULE)
            return []

    def get_available_metrics(self, sensor_id):
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT metric FROM readings "
                    "WHERE LOWER(sensor_id)=LOWER(?) "
                    "ORDER BY metric COLLATE NOCASE",
                    (sensor_id,)
                )
                rows = cur.fetchall()
                return [row[0] for row in rows if row and row[0]]
        except Exception as e:
            printDM(f"Error fetching metrics for {sensor_id}: {e}", location=MODULE)
            return []

    def get_latest_timestamp(self, sensor_id):
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT timestamp FROM readings WHERE LOWER(sensor_id)=LOWER(?) "
                    "ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC LIMIT 1",
                    (sensor_id,)
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        except Exception as e:
            printDM(f"Error fetching latest timestamp for {sensor_id}: {e}", location="saiDataLogger")
            return None

    def register_sensor(self, dev_id: str):
        from collections import defaultdict
        if dev_id not in self.sensor_values:
            self.sensor_values[dev_id] = defaultdict(lambda: None)
        if dev_id not in getattr(self, "sensor_stats", {}):
            if not hasattr(self, "sensor_stats"):
                self.sensor_stats = {}
            self.sensor_stats[dev_id] = defaultdict(dict)

    def clear_all_readings(self):
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM readings")
                conn.commit()
                try:
                    cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except Exception:
                    pass
            self.sensor_values.clear()
            printDM("All sensor data cleared from database", location=MODULE)
        except Exception as e:
            printDM(f"Error clearing database: {e}", location=MODULE)

    # ------------------------------- SWITCH API ------------------

    def upsert_switch_identity(self, *, switch_key: str, switch_id: str, label: str, location: str | None = None) -> None:
        """
        Register/update a switch identity row.

        New canonical form:
          - switch_key MUST be "<switch_id>::<channel_id>"
            where channel_id is the stable per-channel ID
            (e.g. SWITCH_1_ID = "S1-123456").

        Notes:
          - `label` is the user-visible name ("Fan","Light",...) and may change
            over time without changing switch_key.
        """
        try:
            with self._open_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO switch_ids(switch_key, switch_id, label, location)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(switch_key) DO UPDATE SET
                        switch_id=excluded.switch_id,
                        label=excluded.label,
                        location=excluded.location
                    """,
                    (switch_key, switch_id, label, location)
                )
                conn.commit()
        except Exception as e:
            printDM(f"[upsert_switch_identity] {switch_key} error: {e}", location=MODULE)

    def log_switch_event(
        self,
        switch_key: str,
        is_on: bool,
        *,
        timestamp: str | None = None,
        sensor_id: str | None = None,
        source: str | None = None,
    ):
        """
        Append a switch event to sw_events.

        - switch_key: '<switch_id>::<channel_id>' (canonical; channel_id = SWITCH_N_ID)
        - is_on: True/False
        - source: optional ('manual','ui','mqtt','rule', etc.)
        - sensor_id: optional lineage/host (kept for joins/filters)
        """
        self._ensure_writer()
        timestamp, ts_epoch = _normalize_timestamp_input(
            timestamp, getattr(self, "local_tz", LOCAL_TIMEZONE)
        )
        numeric = 1 if is_on else 0

        try:
            with self._writer_lock:
                self._writer_conn.execute(
                    """
                    INSERT INTO sw_events(timestamp, ts_epoch, switch_key, state, source, sensor_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (timestamp, ts_epoch, switch_key, numeric, source, sensor_id)
                )
                self._writer_conn.commit()
                self._maybe_prune_old_rows_locked()

            # Notify post-write listeners (non-blocking; do not break writer path)
            listeners = list(getattr(self, "_on_switch_event_written", []) or [])
            if listeners:
                for fn in listeners:
                    try:
                        fn(switch_key, bool(is_on), timestamp, source, sensor_id)
                    except Exception as exc:
                        printDM(
                            f"[log_switch_event] listener error for {switch_key}: {exc}",
                            location=MODULE,
                        )

            if DEBUG:
                # sensor_id can carry channel_code like "oqs3lr-GP28" if you pass it in
                printDM(
                    f"[log_switch_event] key={switch_key} -> {'On' if is_on else 'Off'} src={source or '-'} sid={sensor_id or '-'}",
                    location=MODULE
                )
        except Exception as e:
            printDM(f"[log_switch_event] write error: {e}", location=MODULE)

    def get_last_switch_events(self, switch_key: str, sensor_id: str | None = None, limit: int = 5):
        """
        Return list[(state_str, timestamp)] from sw_events for a given switch_key.
        sensor_id is optional, used only if you want to scope to a particular host.
        """
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                if sensor_id:
                    cur.execute(
                        """
                        SELECT timestamp, state
                        FROM sw_events
                        WHERE switch_key = ? COLLATE NOCASE AND LOWER(sensor_id)=LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (switch_key, sensor_id, limit)
                    )
                else:
                    cur.execute(
                        """
                        SELECT timestamp, state
                        FROM sw_events
                        WHERE switch_key=? COLLATE NOCASE
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (switch_key, limit)
                    )
                rows = cur.fetchall()
                result = []
                for ts, state in rows:
                    is_on = bool(int(state)) if isinstance(state, (int, float)) else str(state).lower() in ("1","true","on")
                    result.append(("On" if is_on else "Off", ts))
                return result
        except Exception as e:
            printDM(f"[get_last_switch_events] query failed: {e}", location=MODULE)
            return []

    def add_switch_event_listener(self, listener) -> None:
        """
        Listener signature:
          fn(switch_key: str, is_on: bool, timestamp_iso: str, source: str|None, sensor_id: str|None) -> None
        """
        try:
            if listener and listener not in self._on_switch_event_written:
                self._on_switch_event_written.append(listener)
        except Exception:
            pass

    def get_latest_switch_state(self, switch_key: str, sensor_id: str | None = None) -> str | None:
        """
        Returns "On"/"Off"/None using the newest sw_events row for the switch_key.
        """
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                if sensor_id:
                    cur.execute(
                        """
                        SELECT state FROM sw_events
                        WHERE switch_key = ? COLLATE NOCASE AND LOWER(sensor_id)=LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (switch_key, sensor_id)
                    )
                else:
                    cur.execute(
                        """
                        SELECT state FROM sw_events
                        WHERE switch_key=? COLLATE NOCASE 
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (switch_key,)
                    )
                row = cur.fetchone()
                if not row:
                    return None
                val = row[0]
                try:
                    is_on = bool(int(val))
                except Exception:
                    is_on = str(val).lower() in ("1","true","on")
                return "On" if is_on else "Off"
        except Exception as e:
            printDM(f"[get_latest_switch_state] query failed: {e}", location=MODULE)
            return None

    #  convenience query
    def get_known_switches(self) -> list[str]:
        """Return list of registered switch_key values ('<switch_id>::<channel_id>'), sorted."""
        try:
            with self._open_conn() as conn:
                rows = conn.execute("SELECT switch_key FROM switch_ids ORDER BY switch_key").fetchall()
                return [r[0] for r in rows]
        except Exception as e:
            printDM(f"[get_known_switches] error: {e}", location=MODULE)
            return []

    def get_switch_identities(self) -> list[dict]:
        """
        Returns list of dicts:
        {"switch_key": "<switch_id>::<channel_id>", "switch_id": "...", "channel_id": "...", "label": "...", "location": "..."}
        """
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    "SELECT switch_key, switch_id, label, location FROM switch_ids ORDER BY switch_id, switch_key"
                ).fetchall()

            results = []
            for r in rows:
                switch_key = r[0] or ""
                switch_id = r[1] or ""
                label = r[2] or ""
                location = r[3] if len(r) > 3 else None

                # switch_key is "<switch_id>::<channel_id>"
                parts = switch_key.split(SW_KEY_DELIM, 1)
                channel_id = parts[1] if len(parts) == 2 else ""

                results.append({
                    "switch_key": switch_key,
                    "switch_id": switch_id,
                    "channel_id": channel_id,
                    "label": label,
                    "location": location,
                })
            return results

        except Exception as e:
            printDM(f"[get_switch_identities] error: {e}", location=MODULE)
            return []
