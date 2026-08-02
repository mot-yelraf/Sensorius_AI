"""Persistence and query layer for Sensorius telemetry and switch activity.

This module provides a SQLite (WAL) backed data logger that records:
1. Sensor readings in ``readings`` (timestamped metric values by sensor ID).
2. Switch identity metadata in ``switch_ids``.
3. Sensor liveness/events in ``sensor_events``.
4. Switch state transitions in ``sw_events``.

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
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .saiUtils import printDM, debug_enabled
import threading
import os
import shutil
import subprocess
import time
from typing import Optional, Tuple
import weakref
try:
    from .sensor_modules.station_weewx import WEEWX_RAIN_24H_METRIC
except Exception:
    WEEWX_RAIN_24H_METRIC = "Rain Last 24h"

MODULE = "saiDataLogger"
DEBUG = debug_enabled(MODULE)

LOCAL_TIMEZONE = ZoneInfo("America/Denver")
RAIN_INTERVAL_METRIC = "Rain"
RAIN_24H_WINDOW_SEC = 24 * 60 * 60
RAIN_24H_PRECISION = 3
SENSOR_EVENT_TYPE_LIVENESS = "liveness"
SENSOR_EVENT_STATE_OFFLINE = "offline"
SENSOR_OFFLINE_EVENT_WINDOW_SEC = 24 * 60 * 60
SQLITE_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
)

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

def build_switch_key(switch_id: str, channel_id: str) -> str:
    """
    Canonical switch key constructor used across the app:
      "<switch_id>::<channel_id>"
    """
    sid = str(switch_id or "").strip()
    chan = str(channel_id or "").strip()
    return f"{sid}{SW_KEY_DELIM}{chan}"


def _channel_id_from_switch_key(switch_key: str, switch_id: str = "", label: str = "") -> str:
    """Extract channel_id from either current or legacy switch_key shapes."""
    key = str(switch_key or "").strip()
    if SW_KEY_DELIM not in key:
        return ""
    first, suffix = key.split(SW_KEY_DELIM, 1)
    first = first.strip()
    suffix = suffix.strip()
    sid = str(switch_id or "").strip()
    lab = str(label or "").strip()

    if sid and first.lower() == sid.lower():
        return suffix
    if lab and suffix.lower() == lab.lower():
        return first
    if first.lower().startswith("s") and "-" in first:
        return first
    return suffix

class saiDataLogger:
    _init_lock = threading.RLock()
    _schema_ready = False
    _recovery_lock = threading.RLock()
    _recovery_last_attempt_by_path = {}
    _recovery_instances = weakref.WeakSet()

    def __init__(self, db_path="sensorius_data.db"):
        self.db_path = str(db_path)
        self._writer_lock = threading.RLock()   # serialize writers across sensors
        self._writer_conn = None
        self.__class__._recovery_instances.add(self)
        self._init_db()
        self._writer_conn = self._open_conn(check_same_thread=False)
        self._db_retention_days = self._env_int("SENSORIUS_DB_RETENTION_DAYS", 90, minimum=0)
        self._db_retention_prune_interval_sec = 300.0
        self._next_retention_prune_mono = 0.0

        self.sensor_values = {}       # sensor_id → latest values
        self.sensor_timestamps = {}   # sensor_id → latest timestamp
        self.sensor_stats = {}        # sensor_id → 24h stats
        self.sensor_metric_names = {} # sensor_id → list of expected metric names
        self._on_readings_written: list = []
        self._on_switch_event_written: list = []
        self._available_sensors_cache: tuple[float, list[str]] | None = None
        self._available_metrics_cache: dict[str, tuple[float, list[str]]] = {}
        self._available_metrics_by_sensor_cache: tuple[float, dict[str, list[str]]] | None = None
        self._switch_identities_cache: tuple[float, list[dict]] | None = None
        
        from .saiSettings import saiSettings
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

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        return bool(default)

    @staticmethod
    def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
        raw = os.getenv(name)
        try:
            value = float(raw) if raw is not None else float(default)
        except Exception:
            value = float(default)
        return max(float(minimum), value)

    @classmethod
    def is_sqlite_corruption_error(cls, exc_or_text) -> bool:
        """Return True when an SQLite error text indicates on-disk corruption."""
        text = str(exc_or_text or "").strip().lower()
        if not text:
            return False
        return any(marker in text for marker in SQLITE_CORRUPTION_MARKERS)

    @classmethod
    def _db_path_supported_for_recovery(cls, db_path: str | os.PathLike) -> bool:
        text = str(db_path or "").strip()
        if not text or text == ":memory:":
            return False
        # URI databases can point to memory, shared-cache, or read-only targets.
        # Keep automatic file replacement limited to normal filesystem paths.
        if text.startswith("file:"):
            return False
        return True

    @classmethod
    def _resolve_db_path(cls, db_path: str | os.PathLike) -> Path:
        path = Path(str(db_path)).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)

    @classmethod
    def _db_path_key(cls, db_path: str | os.PathLike) -> str:
        return str(cls._resolve_db_path(db_path))

    @classmethod
    def _db_family_paths(cls, db_path: Path) -> list[Path]:
        return [
            db_path,
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
            db_path.with_name(db_path.name + "-journal"),
        ]

    @classmethod
    def _unique_path(cls, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        for idx in range(2, 10000):
            candidate = parent / f"{stem}-{idx}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}-{int(time.time())}{suffix}"

    @classmethod
    def _make_recovery_dir(cls, source_path: Path) -> Path:
        stamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d-%H%M%S")
        recovery_base = source_path.parent / "database_recovery"
        recovery_dir = recovery_base / f"{source_path.stem}-{stamp}"
        recovery_dir = cls._unique_path(recovery_dir)
        recovery_dir.mkdir(parents=True, exist_ok=False)
        return recovery_dir

    @classmethod
    def _close_registered_writers_for_path(cls, db_path: Path) -> None:
        target_key = cls._db_path_key(db_path)
        for instance in list(cls._recovery_instances):
            try:
                if cls._db_path_key(getattr(instance, "db_path", "")) != target_key:
                    continue
                lock = getattr(instance, "_writer_lock", None)
                if lock is None:
                    continue
                with lock:
                    conn = getattr(instance, "_writer_conn", None)
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    instance._writer_conn = None
                    instance._available_sensors_cache = None
                    instance._available_metrics_cache = {}
                    instance._available_metrics_by_sensor_cache = None
                    instance._switch_identities_cache = None
            except Exception:
                continue

    @classmethod
    def _copy_db_family_to_recovery(cls, source_path: Path, recovery_dir: Path) -> dict[Path, Path]:
        copied: dict[Path, Path] = {}
        for live_path in cls._db_family_paths(source_path):
            if not live_path.exists():
                continue
            dest_path = cls._unique_path(recovery_dir / live_path.name)
            try:
                shutil.copy2(live_path, dest_path)
                copied[live_path] = dest_path
            except Exception as exc:
                printDM(
                    f"[db-recovery] failed to copy {live_path} to {dest_path}: {exc}",
                    location=MODULE,
                    level="warning",
                )
        return copied

    @classmethod
    def _move_live_family_to_recovery(cls, source_path: Path, recovery_dir: Path) -> None:
        for live_path in cls._db_family_paths(source_path):
            if not live_path.exists():
                continue
            dest_path = cls._unique_path(recovery_dir / f"{live_path.name}.damaged")
            try:
                shutil.move(str(live_path), str(dest_path))
            except Exception as exc:
                printDM(
                    f"[db-recovery] failed to quarantine {live_path}: {exc}",
                    location=MODULE,
                    level="warning",
                )

    @classmethod
    def _validate_sqlite_db(cls, db_path: Path) -> bool:
        try:
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row and str(row[0]).strip().lower() == "ok")
        except Exception as exc:
            printDM(
                f"[db-recovery] validation failed for {db_path}: {exc}",
                location=MODULE,
                level="warning",
            )
            return False

    @classmethod
    def _recover_with_sqlite_cli(cls, source_copy: Path, recovered_path: Path, recovery_dir: Path) -> bool:
        sqlite3_name = os.getenv("SENSORIUS_SQLITE3_BIN", "sqlite3") or "sqlite3"
        sqlite3_bin = shutil.which(sqlite3_name)
        if not sqlite3_bin:
            printDM(
                f"[db-recovery] sqlite3 CLI not found; skipping .recover for {source_copy}",
                location=MODULE,
                level="warning",
            )
            return False

        timeout_sec = cls._env_float("SENSORIUS_DB_RECOVERY_TIMEOUT_SEC", 300.0, minimum=1.0)
        sql_path = recovery_dir / f"{source_copy.stem}.recover.sql"
        try:
            with sql_path.open("w", encoding="utf-8") as recover_sql:
                recover_proc = subprocess.run(
                    [sqlite3_bin, str(source_copy), ".recover"],
                    stdout=recover_sql,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            if recover_proc.returncode != 0:
                err = (recover_proc.stderr or "").strip()
                printDM(
                    f"[db-recovery] sqlite3 .recover failed rc={recover_proc.returncode}: {err}",
                    location=MODULE,
                    level="warning",
                )
                return False
            if not sql_path.exists() or sql_path.stat().st_size <= 0:
                printDM(
                    f"[db-recovery] sqlite3 .recover produced no SQL at {sql_path}",
                    location=MODULE,
                    level="warning",
                )
                return False

            with sql_path.open("r", encoding="utf-8", errors="replace") as recover_sql:
                import_proc = subprocess.run(
                    [sqlite3_bin, str(recovered_path)],
                    stdin=recover_sql,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            if import_proc.returncode != 0:
                err = (import_proc.stderr or import_proc.stdout or "").strip()
                printDM(
                    f"[db-recovery] sqlite3 recovered import failed rc={import_proc.returncode}: {err}",
                    location=MODULE,
                    level="warning",
                )
                return False
        except subprocess.TimeoutExpired as exc:
            printDM(
                f"[db-recovery] sqlite3 .recover timed out after {timeout_sec:.1f}s: {exc}",
                location=MODULE,
                level="warning",
            )
            return False
        except Exception as exc:
            printDM(
                f"[db-recovery] sqlite3 .recover error: {exc}",
                location=MODULE,
                level="warning",
            )
            return False

        return cls._validate_sqlite_db(recovered_path)

    @classmethod
    def _initialize_schema_after_recovery(cls, source_path: Path) -> bool:
        try:
            cls._schema_ready = False
            initializer = cls.__new__(cls)
            initializer.db_path = str(source_path)
            initializer._writer_lock = threading.RLock()
            initializer._writer_conn = None
            initializer._init_db()
            return True
        except Exception as exc:
            printDM(
                f"[db-recovery] schema initialization failed for {source_path}: {exc}",
                location=MODULE,
                level="warning",
            )
            cls._schema_ready = False
            return False

    @classmethod
    def recover_database_after_error(
        cls,
        db_path: str | os.PathLike,
        exc_or_text,
        *,
        source: str = "",
    ) -> bool:
        """Attempt automatic recovery when an SQLite error indicates corruption."""
        if not cls.is_sqlite_corruption_error(exc_or_text):
            return False
        return cls.recover_database(
            db_path,
            reason=str(exc_or_text),
            source=source,
        )

    @classmethod
    def recover_database(
        cls,
        db_path: str | os.PathLike,
        *,
        reason: str = "",
        source: str = "",
    ) -> bool:
        """Best-effort SQLite salvage and availability recovery for one DB path."""
        if not cls._env_bool("SENSORIUS_DB_AUTO_RECOVER", True):
            printDM(
                f"[db-recovery] disabled by SENSORIUS_DB_AUTO_RECOVER=0 for {db_path}",
                location=MODULE,
                level="warning",
            )
            return False
        if not cls._db_path_supported_for_recovery(db_path):
            return False

        source_path = cls._resolve_db_path(db_path)
        if not source_path.exists():
            return False

        with cls._recovery_lock:
            now_mono = time.monotonic()
            min_interval = cls._env_float("SENSORIUS_DB_RECOVERY_MIN_INTERVAL_SEC", 300.0, minimum=0.0)
            path_key = cls._db_path_key(source_path)
            last_attempt = float(cls._recovery_last_attempt_by_path.get(path_key, 0.0) or 0.0)
            if last_attempt and (now_mono - last_attempt) < min_interval:
                remaining = min_interval - (now_mono - last_attempt)
                printDM(
                    f"[db-recovery] rate-limited for {source_path}; next attempt in {remaining:.1f}s",
                    location=MODULE,
                    level="warning",
                )
                return False
            cls._recovery_last_attempt_by_path[path_key] = now_mono

            recovery_dir = cls._make_recovery_dir(source_path)
            detail = f" source={source}" if source else ""
            reason_text = f": {reason}" if reason else ""
            printDM(
                f"[db-recovery] corruption detected{detail} at {source_path}{reason_text}; workspace={recovery_dir}",
                location=MODULE,
                level="warning",
            )

            cls._close_registered_writers_for_path(source_path)
            copied = cls._copy_db_family_to_recovery(source_path, recovery_dir)
            source_copy = copied.get(source_path)
            recovered_path = recovery_dir / f"{source_path.stem}.recovered.sqlite3"

            if source_copy and cls._recover_with_sqlite_cli(source_copy, recovered_path, recovery_dir):
                cls._move_live_family_to_recovery(source_path, recovery_dir)
                try:
                    shutil.copy2(recovered_path, source_path)
                except Exception as exc:
                    printDM(
                        f"[db-recovery] failed to install recovered DB {recovered_path}: {exc}",
                        location=MODULE,
                        level="warning",
                    )
                    return False
                cls._close_registered_writers_for_path(source_path)
                if cls._initialize_schema_after_recovery(source_path):
                    printDM(
                        f"[db-recovery] recovered SQLite database from {recovered_path}",
                        location=MODULE,
                        level="warning",
                    )
                    return True
                return False

            if cls._env_bool("SENSORIUS_DB_AUTO_REBUILD_ON_RECOVERY_FAIL", True):
                cls._move_live_family_to_recovery(source_path, recovery_dir)
                cls._close_registered_writers_for_path(source_path)
                if cls._initialize_schema_after_recovery(source_path):
                    printDM(
                        f"[db-recovery] rebuilt empty SQLite database; damaged files kept in {recovery_dir}",
                        location=MODULE,
                        level="warning",
                    )
                    return True

            printDM(
                f"[db-recovery] automatic recovery failed; damaged files copied to {recovery_dir}",
                location=MODULE,
                level="warning",
            )
            return False

    def recover_after_db_error(self, exc_or_text, *, source: str = "") -> bool:
        """Instance wrapper for corruption recovery against this logger's DB."""
        return self.__class__.recover_database_after_error(
            self.db_path,
            exc_or_text,
            source=source,
        )

    def create_database_archive(self, archive_dir: str | os.PathLike | None = None) -> Path:
        """Create a consistent SQLite snapshot and return the archive path."""
        source_path = Path(self.db_path).expanduser()
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        source_path = source_path.resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"database not found: {source_path}")

        target_dir = Path(archive_dir).expanduser() if archive_dir else source_path.parent / "database_archives"
        if not target_dir.is_absolute():
            target_dir = Path.cwd() / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        archive_stamp = datetime.now(getattr(self, "local_tz", LOCAL_TIMEZONE)).strftime("%Y%m%d-%H%M%S")
        archive_base = f"{source_path.stem}-{archive_stamp}"
        archive_path = target_dir / f"{archive_base}.sqlite3"
        suffix = 2
        while archive_path.exists():
            archive_path = target_dir / f"{archive_base}-{suffix}.sqlite3"
            suffix += 1

        try:
            with self._writer_lock:
                self._ensure_writer()
                self._writer_conn.commit()
                with sqlite3.connect(str(archive_path)) as dest:
                    self._writer_conn.backup(dest)
            return archive_path.resolve()
        except Exception:
            try:
                archive_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def archive_and_create_new_database(self, archive_dir: str | os.PathLike | None = None) -> Path:
        """
        Archive the active database, remove the live SQLite file family, and
        initialize a fresh empty database. This is an intentional recovery action.
        """
        source_path = self.__class__._resolve_db_path(self.db_path)
        archive_path = self.create_database_archive(archive_dir=archive_dir)

        with self._writer_lock:
            self.__class__._close_registered_writers_for_path(source_path)
            for live_path in self.__class__._db_family_paths(source_path):
                try:
                    live_path.unlink(missing_ok=True)
                except Exception as exc:
                    printDM(
                        f"[new-database] failed to remove {live_path}: {exc}",
                        location=MODULE,
                        level="warning",
                    )
                    raise
            self.__class__._schema_ready = False
            self._init_db()
            self._writer_conn = self._open_conn(check_same_thread=False)

            self.sensor_values.clear()
            self.sensor_timestamps.clear()
            self.sensor_stats.clear()
            self.sensor_metric_names.clear()
            self._available_sensors_cache = None
            self._available_metrics_cache.clear()
            self._available_metrics_by_sensor_cache = None
            self._switch_identities_cache = None

        printDM(
            f"Archived database to {archive_path} and created a new empty database",
            location=MODULE,
        )
        return archive_path

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _startup_epoch_migration_allowed(self, *, missing_epoch_indexes: set[str]) -> bool:
        if not missing_epoch_indexes:
            return True

        raw_mode = str(os.environ.get("SENSORIUS_DB_STARTUP_EPOCH_INDEXES", "auto") or "auto").strip().lower()
        if raw_mode in {"1", "true", "yes", "on", "force"}:
            return True
        if raw_mode in {"0", "false", "no", "off", "skip"}:
            if DEBUG:
                printDM(
                    f"[migration] skipping startup ts_epoch indexes: {sorted(missing_epoch_indexes)}",
                    location=MODULE,
                )
            return False

        try:
            max_mb = float(os.environ.get("SENSORIUS_DB_STARTUP_INDEX_MAX_MB", "256") or "256")
        except Exception:
            max_mb = 256.0
        try:
            db_mb = os.path.getsize(self.db_path) / (1024 * 1024)
        except Exception:
            db_mb = 0.0

        allowed = db_mb <= max_mb
        if not allowed:
            printDM(
                (
                    "[migration] deferring startup ts_epoch indexes "
                    f"for {db_mb:.1f}MB DB over {max_mb:.1f}MB limit: "
                    f"{sorted(missing_epoch_indexes)}"
                ),
                location=MODULE,
            )
        return allowed

    @staticmethod
    def _metric_key(values: dict, metric_name: str) -> str | None:
        target = str(metric_name or "").strip().lower()
        for key in (values or {}).keys():
            if str(key or "").strip().lower() == target:
                return key
        return None

    @classmethod
    def _strip_derived_input_metrics(cls, values: dict) -> dict:
        clean = dict(values or {})
        key = cls._metric_key(clean, WEEWX_RAIN_24H_METRIC)
        if key is not None:
            clean.pop(key, None)
        return clean

    @staticmethod
    def _sum_metric_window_on_conn(
        conn,
        sensor_id: str,
        metric: str,
        *,
        end_epoch: float,
        window_sec: float,
        dedupe_epoch_minute: bool = False,
    ) -> float | None:
        start_epoch = float(end_epoch) - float(window_sec)
        if dedupe_epoch_minute:
            row = conn.execute(
                """
                SELECT SUM(bucket_value)
                FROM (
                    SELECT
                        CAST(COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) / 60 AS INTEGER) AS epoch_minute,
                        MAX(COALESCE(value, 0)) AS bucket_value
                    FROM readings
                    WHERE LOWER(sensor_id) = LOWER(?)
                      AND LOWER(metric) = LOWER(?)
                      AND value IS NOT NULL
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) <= ?
                    GROUP BY epoch_minute
                )
                """,
                (sensor_id, metric, start_epoch, float(end_epoch)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT SUM(COALESCE(value, 0))
                FROM readings
                WHERE LOWER(sensor_id) = LOWER(?)
                  AND LOWER(metric) = LOWER(?)
                  AND value IS NOT NULL
                  AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?
                  AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) <= ?
                """,
                (sensor_id, metric, start_epoch, float(end_epoch)),
            ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return round(float(row[0]), RAIN_24H_PRECISION)
        except Exception:
            return None

    def get_metric_sum_for_window(
        self,
        sensor_id: str,
        metric: str,
        *,
        end_epoch: float | None = None,
        window_sec: float = RAIN_24H_WINDOW_SEC,
        dedupe_epoch_minute: bool = False,
    ) -> float | None:
        """Return a rolling sum for one metric over an epoch-second window."""
        try:
            end = float(time.time() if end_epoch is None else end_epoch)
            with self._open_conn() as conn:
                return self._sum_metric_window_on_conn(
                    conn,
                    sensor_id,
                    metric,
                    end_epoch=end,
                    window_sec=float(window_sec),
                    dedupe_epoch_minute=bool(dedupe_epoch_minute),
                )
        except Exception as e:
            if DEBUG:
                printDM(f"[get_metric_sum_for_window] Query error for {sensor_id}/{metric}: {e}", location=MODULE)
            return None

    def _derive_rain_window_metrics_on_conn(
        self,
        conn,
        sensor_id: str,
        values: dict,
        *,
        end_epoch: float,
    ) -> dict:
        if self._metric_key(values, RAIN_INTERVAL_METRIC) is None:
            return {}
        total = self._sum_metric_window_on_conn(
            conn,
            sensor_id,
            RAIN_INTERVAL_METRIC,
            end_epoch=float(end_epoch),
            window_sec=RAIN_24H_WINDOW_SEC,
            dedupe_epoch_minute=True,
        )
        if total is None:
            return {}
        return {WEEWX_RAIN_24H_METRIC: total}

    def _with_fallback_derived_metrics(self, sensor_id: str, values: dict) -> dict:
        out = dict(values or {})
        if self._metric_key(out, RAIN_INTERVAL_METRIC) is not None:
            total = self.get_metric_sum_for_window(
                sensor_id,
                RAIN_INTERVAL_METRIC,
                dedupe_epoch_minute=True,
            )
            if total is not None:
                out[WEEWX_RAIN_24H_METRIC] = total
        return out

    @classmethod
    def _with_available_derived_metrics(cls, metrics: list[str]) -> list[str]:
        result = [m for m in (metrics or []) if m]
        if cls._metric_key({m: True for m in result}, RAIN_INTERVAL_METRIC) is None:
            return result
        if cls._metric_key({m: True for m in result}, WEEWX_RAIN_24H_METRIC) is not None:
            return result

        rain_idx = None
        for idx, metric in enumerate(result):
            if str(metric or "").strip().lower() == RAIN_INTERVAL_METRIC.lower():
                rain_idx = idx
                break
        if rain_idx is None:
            result.append(WEEWX_RAIN_24H_METRIC)
        else:
            result.insert(rain_idx + 1, WEEWX_RAIN_24H_METRIC)
        return result

    # Enable Write-Ahead-Logging, add PRAGMA and indexes, plus new switch tables
    def _init_db(self):
        # Multiple logger instances are created during startup; run schema init once per process.
        with self.__class__._init_lock:
            if self.__class__._schema_ready:
                return

            # Tolerate transient lock contention from another process touching the DB at boot.
            attempts = 6
            attempted_recovery = False
            for attempt in range(1, attempts + 1):
                try:
                    with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                        cur = conn.cursor()

                        # ---- Pragmas (persist) ----
                        cur.execute("PRAGMA journal_mode=WAL;")
                        cur.execute("PRAGMA synchronous=NORMAL;")
                        # Keep migration/index temp work off RAM-constrained Pis.
                        cur.execute("PRAGMA temp_store=FILE;")
                        cur.execute("PRAGMA busy_timeout=30000;")
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

                        # ---- sensor liveness/events -------------------------------
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS sensor_events (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp  TEXT NOT NULL,         -- ISO8601
                                ts_epoch   REAL,                  -- epoch seconds (UTC comparable)
                                sensor_id  TEXT NOT NULL,
                                event_type TEXT NOT NULL,         -- 'liveness', etc.
                                state      TEXT,                  -- 'offline', 'online', etc.
                                source     TEXT                   -- 'mqtt_liveness', etc.
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_sensor_events_sid_type_state_tse
                            ON sensor_events(
                                sensor_id COLLATE NOCASE,
                                event_type COLLATE NOCASE,
                                state COLLATE NOCASE,
                                ts_epoch DESC
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_sensor_events_tse
                            ON sensor_events(ts_epoch DESC)
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
                                switch_key TEXT NOT NULL,         -- "<switch_id>::<channel_id>"
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

                        cur.execute("PRAGMA index_list(readings)")
                        reading_indexes = {row[1] for row in cur.fetchall()}
                        cur.execute("PRAGMA index_list(sw_events)")
                        swe_indexes = {row[1] for row in cur.fetchall()}
                        required_epoch_indexes = {
                            "readings": {
                                "idx_readings_sid_tse",
                                "idx_readings_sid_metric_tse",
                                "idx_readings_tse",
                            },
                            "sw_events": {
                                "idx_swe_key_tse",
                                "idx_swe_tse",
                            },
                        }
                        missing_epoch_indexes = (
                            required_epoch_indexes["readings"] - reading_indexes
                        ) | (
                            required_epoch_indexes["sw_events"] - swe_indexes
                        )
                        create_epoch_indexes = self._startup_epoch_migration_allowed(
                            missing_epoch_indexes=missing_epoch_indexes,
                        )

                        # Create ts_epoch indexes only after additive migrations above.
                        if create_epoch_indexes:
                            cur.execute("""
                                CREATE INDEX IF NOT EXISTS idx_readings_sid_tse
                                ON readings(sensor_id COLLATE NOCASE, ts_epoch DESC, timestamp DESC)
                            """)
                            cur.execute("""
                                CREATE INDEX IF NOT EXISTS idx_readings_sid_metric_tse
                                ON readings(sensor_id COLLATE NOCASE, metric COLLATE NOCASE, ts_epoch)
                            """)
                            cur.execute("""
                                CREATE INDEX IF NOT EXISTS idx_readings_tse
                                ON readings(ts_epoch DESC)
                            """)
                            cur.execute("""
                                CREATE INDEX IF NOT EXISTS idx_swe_key_tse
                                ON sw_events(switch_key COLLATE NOCASE, ts_epoch DESC)
                            """)
                            cur.execute("""
                                CREATE INDEX IF NOT EXISTS idx_swe_tse
                                ON sw_events(ts_epoch DESC)
                            """)

                        # ---- biodynamic calendar notes -----------------------
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS biodynamic_notes (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                note_date   TEXT NOT NULL UNIQUE, -- YYYY-MM-DD
                                note_text   TEXT NOT NULL DEFAULT '',
                                created_at  TEXT NOT NULL,        -- ISO8601
                                updated_at  TEXT NOT NULL         -- ISO8601
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_biodynamic_notes_date
                            ON biodynamic_notes(note_date DESC)
                        """)
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS biodynamic_daily_summaries (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                summary_date TEXT NOT NULL UNIQUE, -- YYYY-MM-DD
                                summary_text TEXT NOT NULL DEFAULT '',
                                created_at   TEXT NOT NULL,        -- ISO8601
                                updated_at   TEXT NOT NULL         -- ISO8601
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_biodynamic_daily_summaries_date
                            ON biodynamic_daily_summaries(summary_date DESC)
                        """)
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS biodynamic_plantings (
                                planting_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                variety TEXT,
                                plant_type TEXT,
                                plant_part TEXT,
                                start_method TEXT,
                                start_date TEXT NOT NULL,
                                expected_harvest_date TEXT,
                                days_to_maturity INTEGER,
                                harvest_window_days INTEGER,
                                location TEXT,
                                attributes TEXT,
                                notes TEXT,
                                planting_json TEXT NOT NULL,
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_biodynamic_plantings_dates
                            ON biodynamic_plantings(start_date, expected_harvest_date)
                        """)
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS biodynamic_calendar_cache (
                                cache_key TEXT PRIMARY KEY,
                                location_key TEXT NOT NULL,
                                payload_json TEXT NOT NULL,
                                created_at TEXT NOT NULL
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_biodynamic_calendar_cache_created
                            ON biodynamic_calendar_cache(created_at DESC)
                        """)

                        # ---- email notification edge state -------------------
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS notification_rule_state (
                                rule_id      TEXT PRIMARY KEY,
                                active       INTEGER NOT NULL,
                                last_value   REAL,
                                updated_at   TEXT NOT NULL,
                                last_recovery_sent_epoch REAL,
                                failure_retry_after_epoch REAL
                            )
                        """)
                        cur.execute("PRAGMA table_info(notification_rule_state)")
                        notification_state_cols = {row[1] for row in cur.fetchall()}
                        if "last_recovery_sent_epoch" not in notification_state_cols:
                            cur.execute(
                                "ALTER TABLE notification_rule_state ADD COLUMN last_recovery_sent_epoch REAL"
                            )
                        if "failure_retry_after_epoch" not in notification_state_cols:
                            cur.execute(
                                "ALTER TABLE notification_rule_state ADD COLUMN failure_retry_after_epoch REAL"
                            )
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS notification_email_events (
                                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                                rule_id     TEXT NOT NULL,
                                event_type  TEXT NOT NULL,
                                sent_epoch  REAL NOT NULL
                            )
                        """)
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_notification_email_events_sent
                            ON notification_email_events(sent_epoch)
                        """)

                        # Backfill missing ts_epoch values only when the same startup
                        # migration budget permits it; large live DBs should be
                        # cleaned during explicit maintenance, not boot.
                        if create_epoch_indexes:
                            cur.execute("UPDATE readings SET ts_epoch = strftime('%s', timestamp) WHERE ts_epoch IS NULL")
                            cur.execute("UPDATE sw_events SET ts_epoch = strftime('%s', timestamp) WHERE ts_epoch IS NULL")

                        conn.commit()

                        # ---- idempotent migration of legacy rows -------
                        self._maybe_migrate_legacy_switch_rows(cur)
                        conn.commit()

                    self.__class__._schema_ready = True
                    return

                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < attempts:
                        sleep_s = 0.5 * attempt
                        printDM(
                            f"_init_db locked (attempt {attempt}/{attempts}); retrying in {sleep_s:.1f}s",
                            location=__name__,
                        )
                        time.sleep(sleep_s)
                        continue
                    if (
                        not attempted_recovery
                        and self.recover_after_db_error(e, source="_init_db")
                    ):
                        attempted_recovery = True
                        continue
                    printDM(f"_init_db error: {e}", location=__name__)
                    raise
                except sqlite3.DatabaseError as e:
                    if (
                        not attempted_recovery
                        and self.recover_after_db_error(e, source="_init_db")
                    ):
                        attempted_recovery = True
                        continue
                    printDM(f"_init_db error: {e}", location=__name__)
                    raise
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
            is the stable SWITCH_N_CHANNEL_ID (e.g. "S1-123456").
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
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=check_same_thread
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=FILE;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA cache_size=-65536;")
            return conn

        try:
            return _open()
        except sqlite3.DatabaseError as e:
            if self.recover_after_db_error(e, source="_open_conn"):
                return _open()
            raise

    def _ensure_writer(self):
        with self._writer_lock:
            try:
                if self._writer_conn is None:
                    raise RuntimeError("writer connection missing")
                self._writer_conn.execute("SELECT 1")
            except Exception as exc:
                self.recover_after_db_error(exc, source="_ensure_writer")
                try:
                    if self._writer_conn is not None:
                        self._writer_conn.close()
                except Exception:
                    pass
                self._writer_conn = None
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
                WHERE ts_epoch < ?
                """,
                (cutoff_epoch,),
            )
            readings_deleted = int(cur.rowcount or 0)
            cur.execute(
                """
                DELETE FROM sw_events
                WHERE ts_epoch < ?
                """,
                (cutoff_epoch,),
            )
            sw_events_deleted = int(cur.rowcount or 0)
            cur.execute(
                """
                DELETE FROM sensor_events
                WHERE ts_epoch < ?
                """,
                (cutoff_epoch,),
            )
            sensor_events_deleted = int(cur.rowcount or 0)
            if readings_deleted or sw_events_deleted or sensor_events_deleted:
                self._writer_conn.commit()
                try:
                    self._writer_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except Exception:
                    pass
                if DEBUG:
                    printDM(
                        (
                            f"[retention] pruned readings={readings_deleted}, "
                            f"sw_events={sw_events_deleted}, "
                            f"sensor_events={sensor_events_deleted}, days={self._db_retention_days}"
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
        timestamp, ts_epoch = _normalize_timestamp_input(
            timestamp, getattr(self, "local_tz", LOCAL_TIMEZONE)
        )
        raw_values = self._strip_derived_input_metrics(values or {})

        for attempt in range(2):
            t0 = time.monotonic()
            derived_values = {}
            try:
                self._ensure_writer()
                rows = [(timestamp, ts_epoch, sensor_id, metric, value) for metric, value in raw_values.items()]
                write_start = time.monotonic()
                with self._writer_lock:
                    if rows:
                        self._writer_conn.executemany(
                            "INSERT INTO readings (timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
                            rows
                        )
                    derived_values = self._derive_rain_window_metrics_on_conn(
                        self._writer_conn,
                        sensor_id,
                        raw_values,
                        end_epoch=ts_epoch,
                    )
                    if derived_values:
                        self._writer_conn.executemany(
                            "INSERT INTO readings (timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)",
                            [
                                (timestamp, ts_epoch, sensor_id, metric, value)
                                for metric, value in derived_values.items()
                            ],
                        )
                    self._writer_conn.commit()
                    self._maybe_prune_old_rows_locked()
                write_elapsed = time.monotonic() - write_start
                logged_values = dict(raw_values)
                logged_values.update(derived_values)

                snap = self.sensor_values.get(sensor_id) or {}
                snap.update(logged_values)
                self.sensor_values[sensor_id] = snap
                self.sensor_timestamps[sensor_id] = timestamp
                self._available_sensors_cache = None
                self._available_metrics_cache.pop(str(sensor_id or "").strip().lower(), None)
                self._available_metrics_by_sensor_cache = None

                # Notify post-write listeners (non-blocking; do not break writer path)
                listeners = list(getattr(self, "_on_readings_written", []) or [])
                if listeners:
                    listener_start = time.monotonic()
                    for fn in listeners:
                        try:
                            fn(sensor_id, timestamp, dict(logged_values))
                        except Exception as exc:
                            printDM(
                                f"[log_readings] listener error for {sensor_id}: {exc}",
                                location=MODULE,
                            )
                    listener_elapsed = time.monotonic() - listener_start
                else:
                    listener_elapsed = 0.0

                total_elapsed = time.monotonic() - t0
                if total_elapsed >= 1.5:
                    printDM(
                        (
                            f"[log_readings] slow write for {sensor_id}: total={total_elapsed:.2f}s "
                            f"db={write_elapsed:.2f}s listeners={listener_elapsed:.2f}s rows={len(logged_values)}"
                        ),
                        location=MODULE,
                        level="warning",
                    )

                if DEBUG:
                    printDM(f"Logged {len(logged_values)} values for {sensor_id}", location=MODULE)
                return
            except Exception as e:
                if attempt == 0 and self.recover_after_db_error(e, source="log_readings"):
                    continue
                printDM(f"Error writing sensor data: {e}", location=MODULE)
                return

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

    def get_notification_rule_states(self) -> dict[str, bool]:
        """Return persisted active/normal state for email notification rules."""
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    "SELECT rule_id, active FROM notification_rule_state"
                ).fetchall()
            return {str(rule_id): bool(active) for rule_id, active in rows if rule_id}
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not read notification rule states: {exc}", location=MODULE)
            return {}

    def set_notification_rule_state(
        self,
        rule_id: str,
        active: bool,
        last_value: float | None,
        updated_at: str,
    ) -> None:
        """Persist a notification edge only after its message is delivered."""
        rid = str(rule_id or "").strip()
        if not rid:
            return
        with self._writer_lock:
            self._ensure_writer()
            self._writer_conn.execute(
                """
                INSERT INTO notification_rule_state(rule_id, active, last_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    active=excluded.active,
                    last_value=excluded.last_value,
                    updated_at=excluded.updated_at
                """,
                (rid, 1 if active else 0, last_value, str(updated_at or "")),
            )
            self._writer_conn.commit()

    def get_notification_delivery_guards(self) -> dict[str, dict[str, float]]:
        """Return persisted per-rule recovery and failure cooldown timestamps."""
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT rule_id, last_recovery_sent_epoch, failure_retry_after_epoch
                    FROM notification_rule_state
                    """
                ).fetchall()
            return {
                str(rule_id): {
                    "last_recovery_sent_epoch": float(last_recovery or 0.0),
                    "failure_retry_after_epoch": float(retry_after or 0.0),
                }
                for rule_id, last_recovery, retry_after in rows
                if rule_id
            }
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not read notification delivery guards: {exc}", location=MODULE)
            return {}

    def record_notification_delivery(self, rule_id: str, event_type: str, sent_epoch: float) -> None:
        """Record one successful automated email and clear its failure circuit."""
        rid = str(rule_id or "").strip()
        kind = str(event_type or "").strip().lower()
        if not rid or kind not in {"high", "recovery"}:
            return
        epoch = float(sent_epoch)
        with self._writer_lock:
            self._ensure_writer()
            self._writer_conn.execute(
                """
                INSERT INTO notification_email_events(rule_id, event_type, sent_epoch)
                VALUES (?, ?, ?)
                """,
                (rid, kind, epoch),
            )
            if kind == "recovery":
                self._writer_conn.execute(
                    """
                    UPDATE notification_rule_state
                    SET last_recovery_sent_epoch = ?, failure_retry_after_epoch = NULL
                    WHERE rule_id = ?
                    """,
                    (epoch, rid),
                )
            else:
                self._writer_conn.execute(
                    """
                    UPDATE notification_rule_state
                    SET failure_retry_after_epoch = NULL
                    WHERE rule_id = ?
                    """,
                    (rid,),
                )
            self._writer_conn.execute(
                "DELETE FROM notification_email_events WHERE sent_epoch < ?",
                (epoch - (2 * 24 * 60 * 60),),
            )
            self._writer_conn.commit()

    def record_notification_recovery(self, rule_id: str, recovery_epoch: float) -> None:
        """Persist a recovery transition when recovery email delivery is disabled."""
        rid = str(rule_id or "").strip()
        if not rid:
            return
        with self._writer_lock:
            self._ensure_writer()
            self._writer_conn.execute(
                """
                UPDATE notification_rule_state
                SET last_recovery_sent_epoch = ?
                WHERE rule_id = ?
                """,
                (float(recovery_epoch), rid),
            )
            self._writer_conn.commit()

    def set_notification_failure_cooldown(self, rule_id: str, retry_after_epoch: float) -> None:
        """Persist the next time a failed rule may start another delivery batch."""
        rid = str(rule_id or "").strip()
        if not rid:
            return
        with self._writer_lock:
            self._ensure_writer()
            self._writer_conn.execute(
                """
                INSERT INTO notification_rule_state(
                    rule_id, active, last_value, updated_at, failure_retry_after_epoch
                ) VALUES (?, 0, NULL, '', ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    failure_retry_after_epoch = excluded.failure_retry_after_epoch
                """,
                (rid, float(retry_after_epoch)),
            )
            self._writer_conn.commit()

    def get_notification_rate_limit(
        self,
        now_epoch: float,
        *,
        hourly_cap: int,
        daily_cap: int,
    ) -> tuple[bool, float]:
        """Return whether an automated email may send and the next allowed epoch."""
        now = float(now_epoch)
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT sent_epoch
                    FROM notification_email_events
                    WHERE sent_epoch > ?
                    ORDER BY sent_epoch ASC
                    """,
                    (now - (24 * 60 * 60),),
                ).fetchall()
            daily = [float(row[0]) for row in rows]
            hourly = [epoch for epoch in daily if epoch > now - (60 * 60)]
            retry_at = 0.0
            if hourly_cap > 0 and len(hourly) >= hourly_cap:
                retry_at = max(retry_at, hourly[len(hourly) - hourly_cap] + (60 * 60))
            if daily_cap > 0 and len(daily) >= daily_cap:
                retry_at = max(retry_at, daily[len(daily) - daily_cap] + (24 * 60 * 60))
            return retry_at <= now, retry_at
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not read notification rate window: {exc}", location=MODULE)
            return True, 0.0

    def log_sensor_event(
        self,
        sensor_id: str,
        event_type: str,
        *,
        state: str | None = None,
        timestamp: str | None = None,
        source: str | None = None,
    ) -> None:
        """Append a lightweight sensor event row for liveness/history counters."""
        sid = str(sensor_id or "").strip()
        typ = str(event_type or "").strip().lower()
        st = str(state or "").strip().lower()
        src = str(source or "").strip() or None
        if not sid or not typ:
            return

        timestamp, ts_epoch = _normalize_timestamp_input(
            timestamp, getattr(self, "local_tz", LOCAL_TIMEZONE)
        )
        for attempt in range(2):
            try:
                self._ensure_writer()
                with self._writer_lock:
                    self._writer_conn.execute(
                        """
                        INSERT INTO sensor_events(timestamp, ts_epoch, sensor_id, event_type, state, source)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (timestamp, ts_epoch, sid, typ, st or None, src),
                    )
                    self._writer_conn.commit()
                    self._maybe_prune_old_rows_locked()
                if DEBUG:
                    printDM(
                        f"[log_sensor_event] sid={sid} type={typ} state={st or '-'} src={src or '-'}",
                        location=MODULE,
                    )
                return
            except Exception as e:
                if attempt == 0 and self.recover_after_db_error(e, source="log_sensor_event"):
                    continue
                printDM(f"[log_sensor_event] write error: {e}", location=MODULE)
                return

    @staticmethod
    def _dedupe_identifiers(*values) -> list[str]:
        """Return non-empty identifiers in input order, deduped case-insensitively."""
        candidates: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                iterable = value
            else:
                iterable = (value,)
            for raw in iterable:
                item = str(raw or "").strip()
                key = item.lower()
                if item and key not in seen:
                    candidates.append(item)
                    seen.add(key)
        return candidates

    def get_sensor_offline_event_count(
        self,
        sensor_id: str,
        *,
        window_sec: float = SENSOR_OFFLINE_EVENT_WINDOW_SEC,
        end_epoch: float | None = None,
        aliases: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        """Return recorded offline liveness events for a sensor over a rolling window."""
        candidates = self._dedupe_identifiers(sensor_id, aliases or ())
        if not candidates:
            return 0

        try:
            end = float(time.time() if end_epoch is None else end_epoch)
            start = end - float(window_sec)
            placeholders = ",".join("?" for _ in candidates)
            with self._open_conn() as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM sensor_events
                    WHERE sensor_id COLLATE NOCASE IN ({placeholders})
                      AND event_type = ? COLLATE NOCASE
                      AND state = ? COLLATE NOCASE
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?
                      AND COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) <= ?
                    """,
                    (
                        *candidates,
                        SENSOR_EVENT_TYPE_LIVENESS,
                        SENSOR_EVENT_STATE_OFFLINE,
                        start,
                        end,
                    ),
                ).fetchone()
            return int((row or [0])[0] or 0)
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_sensor_offline_event_count] query error for {sensor_id}: {e}",
                    location=MODULE,
                )
            return 0

    def get_sensor_last_offline_event_epoch(
        self,
        sensor_id: str,
        *,
        aliases: list[str] | tuple[str, ...] | None = None,
    ) -> Optional[float]:
        """Return the newest recorded offline liveness event epoch for a sensor."""
        candidates = self._dedupe_identifiers(sensor_id, aliases or ())
        if not candidates:
            return None

        try:
            placeholders = ",".join("?" for _ in candidates)
            with self._open_conn() as conn:
                row = conn.execute(
                    f"""
                    SELECT ts_epoch, timestamp
                    FROM sensor_events
                    WHERE sensor_id COLLATE NOCASE IN ({placeholders})
                      AND event_type = ? COLLATE NOCASE
                      AND state = ? COLLATE NOCASE
                    ORDER BY COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL), 0.0) DESC
                    LIMIT 1
                    """,
                    (
                        *candidates,
                        SENSOR_EVENT_TYPE_LIVENESS,
                        SENSOR_EVENT_STATE_OFFLINE,
                    ),
                ).fetchone()
            if not row:
                return None
            return _timestamp_to_epoch(
                row[0] if row[0] is not None else row[1],
                getattr(self, "local_tz", LOCAL_TIMEZONE),
            )
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_sensor_last_offline_event_epoch] query error for {sensor_id}: {e}",
                    location=MODULE,
                )
            return None

    def get_sensor_packet_count(
        self,
        sensor_id: str,
        *,
        aliases: list[str] | tuple[str, ...] | None = None,
        since_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> int:
        """Return distinct reading packet timestamps for a sensor."""
        candidates = self._dedupe_identifiers(sensor_id, aliases or ())
        if not candidates:
            return 0

        try:
            placeholders = ",".join("?" for _ in candidates)
            where = [f"sensor_id COLLATE NOCASE IN ({placeholders})"]
            params: list = [*candidates]
            if since_epoch is not None:
                where.append("COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?")
                params.append(float(since_epoch))
            if end_epoch is not None:
                where.append("COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) <= ?")
                params.append(float(end_epoch))

            with self._open_conn() as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT COALESCE(CAST(ts_epoch AS TEXT), timestamp) AS packet_key
                        FROM readings
                        WHERE {" AND ".join(where)}
                        GROUP BY packet_key
                    )
                    """,
                    tuple(params),
                ).fetchone()
            return int((row or [0])[0] or 0)
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_sensor_packet_count] query error for {sensor_id}: {e}",
                    location=MODULE,
                )
            return 0

    def get_sensor_last_packet_epoch(
        self,
        sensor_id: str,
        *,
        aliases: list[str] | tuple[str, ...] | None = None,
    ) -> Optional[float]:
        """Return the newest reading packet epoch for a sensor."""
        candidates = self._dedupe_identifiers(sensor_id, aliases or ())
        if not candidates:
            return None

        try:
            placeholders = ",".join("?" for _ in candidates)
            with self._open_conn() as conn:
                row = conn.execute(
                    f"""
                    SELECT ts_epoch, timestamp
                    FROM readings
                    WHERE sensor_id COLLATE NOCASE IN ({placeholders})
                    ORDER BY COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL), 0.0) DESC
                    LIMIT 1
                    """,
                    tuple(candidates),
                ).fetchone()
            if not row:
                return None
            return _timestamp_to_epoch(
                row[0] if row[0] is not None else row[1],
                getattr(self, "local_tz", LOCAL_TIMEZONE),
            )
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_sensor_last_packet_epoch] query error for {sensor_id}: {e}",
                    location=MODULE,
                )
            return None

    def get_latest_values(self, sensor_id):
        if sensor_id in self.sensor_values and self.sensor_values[sensor_id]:
            values = self._with_fallback_derived_metrics(sensor_id, self.sensor_values[sensor_id])
            self.sensor_values[sensor_id] = dict(values)
            return values
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT timestamp FROM readings WHERE sensor_id = ? COLLATE NOCASE "
                    "ORDER BY ts_epoch DESC, timestamp DESC LIMIT 1",
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
                    "WHERE sensor_id = ? COLLATE NOCASE AND timestamp=?",
                    (sensor_id, latest_ts)
                )
                rows = cur.fetchall()
                values = self._with_fallback_derived_metrics(
                    sensor_id,
                    {metric: value for metric, value in rows},
                )
                if values:
                    self.sensor_values[sensor_id] = dict(values)
                return values
        except Exception as e:
            printDM(f"[get_latest_values] Query error for {sensor_id}: {e}", location=MODULE)
            return {}

    def get_available_sensors(self):
        now_mono = time.monotonic()
        cached = self._available_sensors_cache
        if cached and cached[0] > now_mono:
            return list(cached[1])
        query = "SELECT DISTINCT sensor_id FROM readings ORDER BY sensor_id"
        try:
            with self._open_conn() as conn:
                result = [row[0] for row in conn.execute(query).fetchall()]
                self._available_sensors_cache = (now_mono + 5.0, list(result))
                return result
        except Exception as e:
            printDM(f"Sensor ID query error: {e}", location=MODULE)
            return []

    def get_available_metrics(self, sensor_id):
        sensor_key = str(sensor_id or "").strip().lower()
        now_mono = time.monotonic()
        cached = self._available_metrics_cache.get(sensor_key) if sensor_key else None
        if cached and cached[0] > now_mono:
            return list(cached[1])

        cached_by_sensor = self._available_metrics_by_sensor_cache
        if cached_by_sensor and cached_by_sensor[0] > now_mono:
            for sid, metrics in cached_by_sensor[1].items():
                if str(sid or "").strip().lower() == sensor_key:
                    result = list(metrics)
                    self._available_metrics_cache[sensor_key] = (now_mono + 5.0, result)
                    return result

        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT metric FROM readings "
                    "WHERE sensor_id = ? COLLATE NOCASE "
                    "ORDER BY metric COLLATE NOCASE",
                    (sensor_id,)
                )
                rows = cur.fetchall()
                result = [row[0] for row in rows if row and row[0]]
                result = self._with_available_derived_metrics(result)
                if sensor_key:
                    self._available_metrics_cache[sensor_key] = (now_mono + 5.0, list(result))
                return result
        except Exception as e:
            printDM(f"Error fetching metrics for {sensor_id}: {e}", location=MODULE)
            return []

    def get_available_metrics_by_sensor(self):
        now_mono = time.monotonic()
        cached = self._available_metrics_by_sensor_cache
        if cached and cached[0] > now_mono:
            return {sid: list(metrics) for sid, metrics in cached[1].items()}

        query = (
            "SELECT sensor_id, metric FROM readings "
            "GROUP BY sensor_id COLLATE NOCASE, metric COLLATE NOCASE "
            "ORDER BY sensor_id COLLATE NOCASE, metric COLLATE NOCASE"
        )
        try:
            with self._open_conn() as conn:
                rows = conn.execute(query).fetchall()
            result: dict[str, list[str]] = {}
            seen: dict[str, set[str]] = {}
            for sid, metric in rows:
                sid_text = str(sid or "").strip()
                metric_text = str(metric or "").strip()
                if not sid_text or not metric_text:
                    continue
                bucket = result.setdefault(sid_text, [])
                metric_key = metric_text.lower()
                seen_bucket = seen.setdefault(sid_text, set())
                if metric_key not in seen_bucket:
                    bucket.append(metric_text)
                    seen_bucket.add(metric_key)

            for sid, metrics in list(result.items()):
                result[sid] = self._with_available_derived_metrics(metrics)

            expires = now_mono + 5.0
            self._available_metrics_by_sensor_cache = (
                expires,
                {sid: list(metrics) for sid, metrics in result.items()},
            )
            for sid, metrics in result.items():
                self._available_metrics_cache[str(sid or "").strip().lower()] = (expires, list(metrics))
            return {sid: list(metrics) for sid, metrics in result.items()}
        except Exception as e:
            printDM(f"Error fetching metrics by sensor: {e}", location=MODULE)
            return {}

    def get_latest_timestamp(self, sensor_id):
        cached = self.sensor_timestamps.get(sensor_id)
        if cached:
            return cached
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT timestamp FROM readings WHERE sensor_id = ? COLLATE NOCASE "
                    "ORDER BY ts_epoch DESC, timestamp DESC LIMIT 1",
                    (sensor_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    self.sensor_timestamps[sensor_id] = row[0]
                return row[0] if row and row[0] else None
        except Exception as e:
            printDM(f"Error fetching latest timestamp for {sensor_id}: {e}", location="saiDataLogger")
            return None

    def get_latest_timestamps(self, sensor_ids: list[str]) -> dict[str, str]:
        """Return latest reading timestamps for multiple sensors using one DB query."""
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in sensor_ids or []:
            sid = str(raw or "").strip()
            key = sid.lower()
            if not sid or key in seen:
                continue
            seen.add(key)
            clean_ids.append(sid)
        if not clean_ids:
            return {}

        result: dict[str, str] = {}
        missing: list[str] = []
        for sid in clean_ids:
            cached = self.sensor_timestamps.get(sid)
            if cached:
                result[sid] = cached
            else:
                missing.append(sid)

        if not missing:
            return result

        sid_map = {sid.lower(): sid for sid in missing}
        placeholders = ",".join("?" for _ in sid_map)
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    f"""
                    WITH latest AS (
                        SELECT sensor_id COLLATE NOCASE AS sid_l,
                               MAX(ts_epoch) AS latest_ts_epoch
                        FROM readings
                        WHERE sensor_id COLLATE NOCASE IN ({placeholders})
                        GROUP BY sensor_id COLLATE NOCASE
                    )
                    SELECT r.sensor_id, r.timestamp
                    FROM readings r
                    JOIN latest l
                      ON r.sensor_id = l.sid_l COLLATE NOCASE
                     AND r.ts_epoch = l.latest_ts_epoch
                    ORDER BY r.sensor_id COLLATE NOCASE, r.timestamp DESC
                    """,
                    tuple(sid_map.keys()),
                ).fetchall()
        except Exception as e:
            printDM(f"Error fetching latest timestamps: {e}", location=MODULE)
            rows = []

        for sid_raw, ts in rows:
            sid = sid_map.get(str(sid_raw or "").lower(), str(sid_raw or "").strip())
            if not sid or not ts or sid in result:
                continue
            result[sid] = ts
            self.sensor_timestamps[sid] = ts

        return result

    def get_latest_values_and_timestamps(self, sensor_ids: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
        clean_ids = [str(sid or "").strip() for sid in (sensor_ids or []) if str(sid or "").strip()]
        if not clean_ids:
            return {}, {}

        values_out: dict[str, dict] = {}
        timestamps_out: dict[str, str] = {}
        missing_ids: list[str] = []

        for sid in clean_ids:
            cached_values = self.sensor_values.get(sid)
            cached_ts = self.sensor_timestamps.get(sid)
            if cached_values:
                values_out[sid] = dict(cached_values)
            if cached_ts:
                timestamps_out[sid] = cached_ts
            if not cached_values or not cached_ts:
                missing_ids.append(sid)

        if not missing_ids:
            values_out = {
                sid: self._with_fallback_derived_metrics(sid, values)
                for sid, values in values_out.items()
            }
            for sid, values in values_out.items():
                if values:
                    self.sensor_values[sid] = dict(values)
            return values_out, timestamps_out

        sid_map = {sid.lower(): sid for sid in missing_ids}
        placeholders = ",".join("?" for _ in sid_map)
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    WITH latest AS (
                        SELECT sensor_id COLLATE NOCASE AS sid_l,
                               MAX(ts_epoch) AS latest_ts_epoch
                        FROM readings
                        WHERE sensor_id COLLATE NOCASE IN ({placeholders})
                        GROUP BY sensor_id COLLATE NOCASE
                    )
                    SELECT r.sensor_id, r.timestamp, r.metric, r.value
                    FROM readings r
                    JOIN latest l
                      ON r.sensor_id = l.sid_l COLLATE NOCASE
                     AND r.ts_epoch = l.latest_ts_epoch
                    ORDER BY r.sensor_id COLLATE NOCASE, r.metric
                    """,
                    tuple(sid_map.keys()),
                )
                rows = cur.fetchall()
        except Exception as e:
            printDM(f"[get_latest_values_and_timestamps] query error: {e}", location=MODULE)
            rows = []

        for row in rows:
            sid_raw = row[0]
            sid = sid_map.get(str(sid_raw or "").lower(), str(sid_raw or "").strip())
            if not sid:
                continue
            ts = row[1]
            metric = row[2]
            value = row[3]
            if sid not in values_out:
                values_out[sid] = {}
            if metric:
                values_out[sid][metric] = value
            if ts and sid not in timestamps_out:
                timestamps_out[sid] = ts

        for sid in missing_ids:
            if sid in values_out and values_out[sid]:
                self.sensor_values[sid] = dict(values_out[sid])
            if sid in timestamps_out and timestamps_out[sid]:
                self.sensor_timestamps[sid] = timestamps_out[sid]

        values_out = {
            sid: self._with_fallback_derived_metrics(sid, values)
            for sid, values in values_out.items()
        }
        for sid, values in values_out.items():
            if values:
                self.sensor_values[sid] = dict(values)
        return values_out, timestamps_out

    def register_sensor(self, dev_id: str):
        from collections import defaultdict
        if dev_id not in self.sensor_values:
            self.sensor_values[dev_id] = defaultdict(lambda: None)
        if dev_id not in getattr(self, "sensor_stats", {}):
            if not hasattr(self, "sensor_stats"):
                self.sensor_stats = {}
            self.sensor_stats[dev_id] = defaultdict(dict)

    def purge_sensor_data(self, sensor_id: str, aliases: list[str] | tuple[str, ...] | None = None) -> dict:
        """
        Delete persisted readings and sensor events for one sensor identity.

        Removal must also clear the logger's discovery/latest-value caches,
        otherwise a deleted directly connected sensor can continue to appear on
        the dashboard until the process restarts or cache TTLs expire.
        """
        raw_ids = [sensor_id, *(aliases or ())]
        sensor_ids: list[str] = []
        seen: set[str] = set()
        for raw in raw_ids:
            sid = str(raw or "").strip()
            key = sid.lower()
            if sid and key not in seen:
                seen.add(key)
                sensor_ids.append(sid)

        stats = {"rows_deleted": 0, "tables": [], "ids": list(sensor_ids)}
        if not sensor_ids:
            return stats

        deleted_by_table: dict[str, int] = {"readings": 0, "sensor_events": 0}
        try:
            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.cursor()
                for sid in sensor_ids:
                    cur.execute("DELETE FROM readings WHERE sensor_id = ? COLLATE NOCASE", (sid,))
                    deleted_by_table["readings"] += int(cur.rowcount or 0)
                    cur.execute("DELETE FROM sensor_events WHERE sensor_id = ? COLLATE NOCASE", (sid,))
                    deleted_by_table["sensor_events"] += int(cur.rowcount or 0)
                self._writer_conn.commit()
                try:
                    cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except Exception:
                    pass
        except Exception as e:
            printDM(f"[purge_sensor_data] error for {sensor_ids}: {e}", location=MODULE)
            return stats

        for table, count in deleted_by_table.items():
            if count:
                stats["tables"].append({table: count})
                stats["rows_deleted"] += count

        sensor_keys = {sid.lower() for sid in sensor_ids}
        for cache_name in ("sensor_values", "sensor_timestamps", "sensor_stats", "sensor_metric_names"):
            cache = getattr(self, cache_name, None)
            if isinstance(cache, dict):
                for key in list(cache.keys()):
                    if str(key or "").strip().lower() in sensor_keys:
                        cache.pop(key, None)
        self._available_sensors_cache = None
        self._available_metrics_by_sensor_cache = None
        for key in list(self._available_metrics_cache.keys()):
            if str(key or "").strip().lower() in sensor_keys:
                self._available_metrics_cache.pop(key, None)

        if stats["rows_deleted"]:
            printDM(
                f"Purged sensor data for {sensor_ids}: rows={stats['rows_deleted']}",
                location=MODULE,
            )
        return stats

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
            self.sensor_timestamps.clear()
            self._available_sensors_cache = None
            self._available_metrics_cache.clear()
            self._available_metrics_by_sensor_cache = None
            printDM("All sensor data cleared from database", location=MODULE)
        except Exception as e:
            printDM(f"Error clearing database: {e}", location=MODULE)

    # ------------------------- BIODYNAMIC NOTES API -------------------------

    @staticmethod
    def _normalize_biodynamic_date_range(start_date, end_date) -> tuple[str, str]:
        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date).date()
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date).date()
        return start_date.isoformat(), end_date.isoformat()

    def get_biodynamic_notes_for_month(self, month_anchor) -> dict[str, str]:
        try:
            if isinstance(month_anchor, str):
                month_anchor = datetime.fromisoformat(month_anchor).date()
            month_start = month_anchor.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1, day=1)
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT note_date, note_text
                    FROM biodynamic_notes
                    WHERE note_date >= ? AND note_date < ?
                    ORDER BY note_date ASC
                    """,
                    (month_start.isoformat(), month_end.isoformat()),
                )
                return {
                    str(row["note_date"]): str(row["note_text"] or "")
                    for row in cur.fetchall()
                    if row and row["note_date"]
                }
        except Exception as e:
            printDM(f"[get_biodynamic_notes_for_month] error: {e}", location=MODULE)
            return {}

    def get_biodynamic_notes_for_range(self, start_date, end_date) -> dict[str, str]:
        try:
            start_iso, end_iso = self._normalize_biodynamic_date_range(start_date, end_date)
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT note_date, note_text
                    FROM biodynamic_notes
                    WHERE note_date >= ? AND note_date <= ?
                    ORDER BY note_date ASC
                    """,
                    (start_iso, end_iso),
                )
                return {
                    str(row["note_date"]): str(row["note_text"] or "")
                    for row in cur.fetchall()
                    if row and row["note_date"]
                }
        except Exception as e:
            printDM(f"[get_biodynamic_notes_for_range] error: {e}", location=MODULE)
            return {}

    def get_biodynamic_daily_summaries_for_month(self, month_anchor) -> dict[str, str]:
        try:
            if isinstance(month_anchor, str):
                month_anchor = datetime.fromisoformat(month_anchor).date()
            month_start = month_anchor.replace(day=1)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1, day=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1, day=1)
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT summary_date, summary_text
                    FROM biodynamic_daily_summaries
                    WHERE summary_date >= ? AND summary_date < ?
                    ORDER BY summary_date ASC
                    """,
                    (month_start.isoformat(), month_end.isoformat()),
                )
                return {
                    str(row["summary_date"]): str(row["summary_text"] or "")
                    for row in cur.fetchall()
                    if row and row["summary_date"]
                }
        except Exception as e:
            printDM(f"[get_biodynamic_daily_summaries_for_month] error: {e}", location=MODULE)
            return {}

    def get_biodynamic_daily_summaries_for_range(self, start_date, end_date) -> dict[str, str]:
        try:
            start_iso, end_iso = self._normalize_biodynamic_date_range(start_date, end_date)
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT summary_date, summary_text
                    FROM biodynamic_daily_summaries
                    WHERE summary_date >= ? AND summary_date <= ?
                    ORDER BY summary_date ASC
                    """,
                    (start_iso, end_iso),
                )
                return {
                    str(row["summary_date"]): str(row["summary_text"] or "")
                    for row in cur.fetchall()
                    if row and row["summary_date"]
                }
        except Exception as e:
            printDM(f"[get_biodynamic_daily_summaries_for_range] error: {e}", location=MODULE)
            return {}

    def get_biodynamic_daily_summary(self, summary_date: str) -> str:
        try:
            clean_date = datetime.fromisoformat(str(summary_date).strip()).date().isoformat()
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT summary_text
                    FROM biodynamic_daily_summaries
                    WHERE summary_date = ?
                    LIMIT 1
                    """,
                    (clean_date,),
                )
                row = cur.fetchone()
                return str(row["summary_text"] or "") if row else ""
        except Exception as e:
            printDM(f"[get_biodynamic_daily_summary] error for {summary_date}: {e}", location=MODULE)
            return ""

    def save_biodynamic_note(self, note_date: str, note_text: str) -> bool:
        try:
            date_obj = datetime.fromisoformat(str(note_date).strip()).date()
            clean_date = date_obj.isoformat()
            clean_text = str(note_text or "").strip()
            now_iso = datetime.now(getattr(self, "local_tz", LOCAL_TIMEZONE)).isoformat()
            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.cursor()
                if clean_text:
                    cur.execute(
                        """
                        INSERT INTO biodynamic_notes(note_date, note_text, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(note_date) DO UPDATE SET
                            note_text=excluded.note_text,
                            updated_at=excluded.updated_at
                        """,
                        (clean_date, clean_text, now_iso, now_iso),
                    )
                else:
                    cur.execute("DELETE FROM biodynamic_notes WHERE note_date = ?", (clean_date,))
                self._writer_conn.commit()
            return True
        except Exception as e:
            printDM(f"[save_biodynamic_note] error for {note_date}: {e}", location=MODULE)
            return False

    def save_biodynamic_daily_summary(self, summary_date: str, summary_text: str) -> bool:
        try:
            date_obj = datetime.fromisoformat(str(summary_date).strip()).date()
            clean_date = date_obj.isoformat()
            clean_text = str(summary_text or "").strip()
            now_iso = datetime.now(getattr(self, "local_tz", LOCAL_TIMEZONE)).isoformat()
            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.cursor()
                if clean_text:
                    cur.execute(
                        """
                        INSERT INTO biodynamic_daily_summaries(summary_date, summary_text, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(summary_date) DO UPDATE SET
                            summary_text=excluded.summary_text,
                            updated_at=excluded.updated_at
                        """,
                        (clean_date, clean_text, now_iso, now_iso),
                    )
                else:
                    cur.execute("DELETE FROM biodynamic_daily_summaries WHERE summary_date = ?", (clean_date,))
                self._writer_conn.commit()
            return True
        except Exception as e:
            printDM(f"[save_biodynamic_daily_summary] error for {summary_date}: {e}", location=MODULE)
            return False

    def get_biodynamic_plantings(self) -> list[dict[str, object]]:
        """Return normalized planting records used by the integrated calendar."""
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT planting_json
                    FROM biodynamic_plantings
                    ORDER BY start_date ASC, name ASC
                    """
                ).fetchall()
            plantings = []
            for row in rows:
                try:
                    value = json.loads(str(row["planting_json"] or "{}"))
                except Exception:
                    continue
                if isinstance(value, dict):
                    plantings.append(value)
            return plantings
        except Exception as e:
            printDM(f"[get_biodynamic_plantings] error: {e}", location=MODULE)
            return []

    def save_biodynamic_planting(self, planting: dict[str, object]) -> bool:
        """Insert or update a validated integrated-calendar planting record."""
        try:
            planting_id = str(planting.get("id") or "").strip()
            name = str(planting.get("name") or "").strip()
            start_date = str(planting.get("start_date") or "").strip()
            if not planting_id or not name or not start_date:
                return False
            now_iso = datetime.now(getattr(self, "local_tz", LOCAL_TIMEZONE)).isoformat()
            with self._writer_lock:
                self._ensure_writer()
                self._writer_conn.execute(
                    """
                    INSERT INTO biodynamic_plantings(
                        planting_id, name, variety, plant_type, plant_part, start_method,
                        start_date, expected_harvest_date, days_to_maturity, harvest_window_days,
                        location, attributes, notes, planting_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(planting_id) DO UPDATE SET
                        name=excluded.name,
                        variety=excluded.variety,
                        plant_type=excluded.plant_type,
                        plant_part=excluded.plant_part,
                        start_method=excluded.start_method,
                        start_date=excluded.start_date,
                        expected_harvest_date=excluded.expected_harvest_date,
                        days_to_maturity=excluded.days_to_maturity,
                        harvest_window_days=excluded.harvest_window_days,
                        location=excluded.location,
                        attributes=excluded.attributes,
                        notes=excluded.notes,
                        planting_json=excluded.planting_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        planting_id,
                        name,
                        str(planting.get("variety") or ""),
                        str(planting.get("plant_type") or ""),
                        str(planting.get("plant_part") or ""),
                        str(planting.get("start_method") or ""),
                        start_date,
                        str(planting.get("expected_harvest_date") or ""),
                        planting.get("days_to_maturity"),
                        planting.get("harvest_window_days"),
                        str(planting.get("location") or ""),
                        str(planting.get("attributes") or ""),
                        str(planting.get("notes") or ""),
                        json.dumps(planting, sort_keys=True, separators=(",", ":")),
                        now_iso,
                        now_iso,
                    ),
                )
                self._writer_conn.commit()
            return True
        except Exception as e:
            printDM(f"[save_biodynamic_planting] error: {e}", location=MODULE)
            return False

    def delete_biodynamic_planting(self, planting_id: str) -> bool:
        """Delete one planting record by its stable identifier."""
        try:
            target = str(planting_id or "").strip()
            if not target:
                return False
            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.execute(
                    "DELETE FROM biodynamic_plantings WHERE planting_id = ?",
                    (target,),
                )
                self._writer_conn.commit()
                return int(cur.rowcount or 0) > 0
        except Exception as e:
            printDM(f"[delete_biodynamic_planting] error: {e}", location=MODULE)
            return False

    def get_biodynamic_calendar_cache(self, cache_key: str, location_key: str) -> dict[str, object] | None:
        """Load a persisted calendar calculation for the matching location."""
        try:
            with self._open_conn() as conn:
                row = conn.execute(
                    """
                    SELECT payload_json
                    FROM biodynamic_calendar_cache
                    WHERE cache_key = ? AND location_key = ?
                    LIMIT 1
                    """,
                    (str(cache_key), str(location_key)),
                ).fetchone()
            if not row:
                return None
            payload = json.loads(str(row["payload_json"] or "{}"))
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            printDM(f"[get_biodynamic_calendar_cache] error: {e}", location=MODULE)
            return None

    def save_biodynamic_calendar_cache(
        self,
        cache_key: str,
        location_key: str,
        payload: dict[str, object],
        *,
        max_entries: int = 120,
    ) -> bool:
        """Persist one versioned calendar payload and trim older cache rows."""
        try:
            now_iso = datetime.now(getattr(self, "local_tz", LOCAL_TIMEZONE)).isoformat()
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            with self._writer_lock:
                self._ensure_writer()
                self._writer_conn.execute(
                    """
                    INSERT INTO biodynamic_calendar_cache(cache_key, location_key, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        location_key=excluded.location_key,
                        payload_json=excluded.payload_json,
                        created_at=excluded.created_at
                    """,
                    (str(cache_key), str(location_key), serialized, now_iso),
                )
                self._writer_conn.execute(
                    """
                    DELETE FROM biodynamic_calendar_cache
                    WHERE cache_key NOT IN (
                        SELECT cache_key FROM biodynamic_calendar_cache
                        ORDER BY created_at DESC LIMIT ?
                    )
                    """,
                    (max(1, int(max_entries)),),
                )
                self._writer_conn.commit()
            return True
        except Exception as e:
            printDM(f"[save_biodynamic_calendar_cache] error: {e}", location=MODULE)
            return False

    def clear_biodynamic_calendar_cache(self) -> bool:
        """Clear integrated-calendar calculations after a location change."""
        try:
            with self._writer_lock:
                self._ensure_writer()
                self._writer_conn.execute("DELETE FROM biodynamic_calendar_cache")
                self._writer_conn.commit()
            return True
        except Exception as e:
            printDM(f"[clear_biodynamic_calendar_cache] error: {e}", location=MODULE)
            return False

    # ------------------------------- SWITCH API ------------------

    def upsert_switch_identity(self, *, switch_key: str, switch_id: str, label: str, location: str | None = None) -> None:
        """
        Register/update a switch identity row.

        New canonical form:
          - switch_key MUST be "<switch_id>::<channel_id>"
            where channel_id is the stable per-channel ID
            (e.g. SWITCH_1_CHANNEL_ID = "S1-123456").

        Notes:
          - `label` is the user-visible name ("Fan","Light",...) and may change
            over time without changing switch_key.
        """
        switch_key = str(switch_key or "").strip()
        switch_id = str(switch_id or "").strip()
        label = str(label or "").strip()
        if not switch_key or not switch_id or not label:
            return

        try:
            channel_id = _channel_id_from_switch_key(switch_key, switch_id, label)
            if channel_id:
                switch_key = build_switch_key(switch_id, channel_id)

            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.cursor()

                if channel_id:
                    stale_rows = cur.execute(
                        """
                        SELECT switch_key, label
                        FROM switch_ids
                        WHERE switch_id = ? COLLATE NOCASE
                        """,
                        (switch_id,),
                    ).fetchall()
                    for row in stale_rows or []:
                        old_key = str((row or [""])[0] or "").strip()
                        old_label = str((row or ["", ""])[1] or "").strip()
                        if not old_key or old_key.lower() == switch_key.lower():
                            continue
                        old_channel_id = _channel_id_from_switch_key(
                            old_key,
                            switch_id,
                            old_label or label,
                        )
                        old_suffix = ""
                        if SW_KEY_DELIM in old_key:
                            old_suffix = old_key.split(SW_KEY_DELIM, 1)[1].strip()
                        same_channel = old_channel_id and old_channel_id.lower() == channel_id.lower()
                        same_label = (old_label or old_suffix).lower() == label.lower()
                        same_suffix = old_suffix.lower() in {label.lower(), channel_id.lower()}
                        if not (same_channel or same_label or same_suffix):
                            continue
                        cur.execute(
                            "UPDATE sw_events SET switch_key = ? WHERE switch_key = ? COLLATE NOCASE",
                            (switch_key, old_key),
                        )
                        cur.execute(
                            "DELETE FROM switch_ids WHERE switch_key = ? COLLATE NOCASE",
                            (old_key,),
                        )

                cur.execute(
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
                self._writer_conn.commit()
                self._switch_identities_cache = None
        except Exception as e:
            printDM(f"[upsert_switch_identity] {switch_key} error: {e}", location=MODULE)

    def prune_switch_identities(self, *, switch_id: str, valid_channel_ids: list[str] | set[str] | tuple[str, ...]) -> int:
        """
        Remove stale switch_ids rows for a switch when the current configured
        channel_id set is known.
        """
        sid = str(switch_id or "").strip()
        valid = {str(cid or "").strip() for cid in (valid_channel_ids or []) if str(cid or "").strip()}
        if not sid or not valid:
            return 0
        removed = 0
        try:
            with self._open_conn() as conn:
                rows = conn.execute(
                    "SELECT switch_key FROM switch_ids WHERE switch_id = ?",
                    (sid,),
                ).fetchall()
                stale_keys = []
                for row in rows or []:
                    switch_key = str((row or [""])[0] or "").strip()
                    if not switch_key:
                        continue
                    channel_id = _channel_id_from_switch_key(switch_key, sid)
                    if channel_id and channel_id not in valid:
                        stale_keys.append((switch_key,))
                if stale_keys:
                    conn.executemany("DELETE FROM switch_ids WHERE switch_key = ?", stale_keys)
                    conn.commit()
                    removed = len(stale_keys)
                self._switch_identities_cache = None
        except Exception as e:
            printDM(f"[prune_switch_identities] {sid} error: {e}", location=MODULE)
            return 0
        return removed

    def migrate_switch_keys(self, mapping: dict[str, str] | None) -> int:
        """
        Rewrite switch keys in switch_ids and sw_events.
        Primarily used to migrate placeholder local keys like 'S1-::Fan'
        to stable keys like 'S1-abc123::Fan'.
        """
        key_map = {
            str(old or "").strip(): str(new or "").strip()
            for old, new in (mapping or {}).items()
            if str(old or "").strip() and str(new or "").strip()
        }
        if not key_map:
            return 0

        changed = 0
        try:
            with self._writer_lock:
                self._ensure_writer()
                cur = self._writer_conn.cursor()
                for old_key, new_key in key_map.items():
                    if old_key.lower() == new_key.lower():
                        continue

                    row = cur.execute(
                        "SELECT switch_id, label, location FROM switch_ids WHERE switch_key = ? COLLATE NOCASE LIMIT 1",
                        (old_key,),
                    ).fetchone()
                    if row:
                        cur.execute(
                            """
                            INSERT INTO switch_ids(switch_key, switch_id, label, location)
                            VALUES(?, ?, ?, ?)
                            ON CONFLICT(switch_key) DO UPDATE SET
                                switch_id=excluded.switch_id,
                                label=excluded.label,
                                location=excluded.location
                            """,
                            (new_key, row[0], row[1], row[2]),
                        )
                        cur.execute(
                            "DELETE FROM switch_ids WHERE switch_key = ? COLLATE NOCASE AND switch_key <> ?",
                            (old_key, new_key),
                        )

                    cur.execute(
                        "UPDATE sw_events SET switch_key = ? WHERE switch_key = ? COLLATE NOCASE",
                        (new_key, old_key),
                    )
                    changed += int(cur.rowcount or 0)

                self._writer_conn.commit()
                self._switch_identities_cache = None
        except Exception as e:
            printDM(f"[migrate_switch_keys] error: {e}", location=MODULE)
            return 0

        return changed

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

        - switch_key: '<switch_id>::<channel_id>' (canonical)
        - is_on: True/False
        - source: optional ('manual','ui','mqtt','rule', etc.)
        - sensor_id: optional lineage/host (kept for joins/filters)
        """
        timestamp, ts_epoch = _normalize_timestamp_input(
            timestamp, getattr(self, "local_tz", LOCAL_TIMEZONE)
        )
        numeric = 1 if is_on else 0

        for attempt in range(2):
            try:
                self._ensure_writer()
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
                return
            except Exception as e:
                if attempt == 0 and self.recover_after_db_error(e, source="log_switch_event"):
                    continue
                printDM(f"[log_switch_event] write error: {e}", location=MODULE)
                return

    def get_switch_packet_count(
        self,
        switch_id: str,
        *,
        switch_keys: list[str] | tuple[str, ...] | None = None,
        aliases: list[str] | tuple[str, ...] | None = None,
        since_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> int:
        """Return persisted switch event packets for a switch/controller."""
        switch_ids = self._dedupe_identifiers(switch_id, aliases or ())
        keys = self._dedupe_identifiers(switch_keys or ())
        if keys:
            expanded_keys: list[str] = []
            for key in keys:
                expanded_keys.extend(self._switch_key_alias_candidates(key) or [key])
            keys = self._dedupe_identifiers(expanded_keys)
        if not switch_ids and not keys:
            return 0

        try:
            with self._open_conn() as conn:
                if switch_ids:
                    id_placeholders = ",".join("?" for _ in switch_ids)
                    rows = conn.execute(
                        f"""
                        SELECT switch_key
                        FROM switch_ids
                        WHERE switch_id COLLATE NOCASE IN ({id_placeholders})
                        """,
                        tuple(switch_ids),
                    ).fetchall()
                    keys = self._dedupe_identifiers(keys, [r[0] for r in rows or []])

                key_clauses: list[str] = []
                params: list = []
                if keys:
                    key_placeholders = ",".join("?" for _ in keys)
                    key_clauses.append(f"switch_key COLLATE NOCASE IN ({key_placeholders})")
                    params.extend(keys)
                if switch_ids:
                    sid_placeholders = ",".join("?" for _ in switch_ids)
                    key_clauses.append(f"sensor_id COLLATE NOCASE IN ({sid_placeholders})")
                    params.extend(switch_ids)
                if not key_clauses:
                    return 0

                where = ["(" + " OR ".join(key_clauses) + ")"]
                if since_epoch is not None:
                    where.append("COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) >= ?")
                    params.append(float(since_epoch))
                if end_epoch is not None:
                    where.append("COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL)) <= ?")
                    params.append(float(end_epoch))

                row = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM sw_events
                    WHERE {" AND ".join(where)}
                    """,
                    tuple(params),
                ).fetchone()
            return int((row or [0])[0] or 0)
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_switch_packet_count] query error for {switch_id}: {e}",
                    location=MODULE,
                )
            return 0

    def get_switch_last_event(
        self,
        switch_id: str,
        *,
        switch_keys: list[str] | tuple[str, ...] | None = None,
        aliases: list[str] | tuple[str, ...] | None = None,
    ) -> dict | None:
        """Return the newest persisted switch event row for a switch/controller."""
        switch_ids = self._dedupe_identifiers(switch_id, aliases or ())
        keys = self._dedupe_identifiers(switch_keys or ())
        if keys:
            expanded_keys: list[str] = []
            for key in keys:
                expanded_keys.extend(self._switch_key_alias_candidates(key) or [key])
            keys = self._dedupe_identifiers(expanded_keys)
        if not switch_ids and not keys:
            return None

        try:
            with self._open_conn() as conn:
                if switch_ids:
                    id_placeholders = ",".join("?" for _ in switch_ids)
                    rows = conn.execute(
                        f"""
                        SELECT switch_key
                        FROM switch_ids
                        WHERE switch_id COLLATE NOCASE IN ({id_placeholders})
                        """,
                        tuple(switch_ids),
                    ).fetchall()
                    keys = self._dedupe_identifiers(keys, [r[0] for r in rows or []])

                key_clauses: list[str] = []
                params: list = []
                if keys:
                    key_placeholders = ",".join("?" for _ in keys)
                    key_clauses.append(f"switch_key COLLATE NOCASE IN ({key_placeholders})")
                    params.extend(keys)
                if switch_ids:
                    sid_placeholders = ",".join("?" for _ in switch_ids)
                    key_clauses.append(f"sensor_id COLLATE NOCASE IN ({sid_placeholders})")
                    params.extend(switch_ids)
                if not key_clauses:
                    return None

                row = conn.execute(
                    f"""
                    SELECT switch_key, state, timestamp, ts_epoch, source, sensor_id
                    FROM sw_events
                    WHERE {" OR ".join(key_clauses)}
                    ORDER BY COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL), 0.0) DESC
                    LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
            if not row:
                return None
            epoch = _timestamp_to_epoch(
                row[3] if row[3] is not None else row[2],
                getattr(self, "local_tz", LOCAL_TIMEZONE),
            )
            return {
                "switch_key": row[0],
                "state": row[1],
                "timestamp": row[2],
                "ts_epoch": epoch,
                "source": row[4],
                "sensor_id": row[5],
            }
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_switch_last_event] query error for {switch_id}: {e}",
                    location=MODULE,
                )
            return None

    def get_switch_last_event_for_channel(self, channel_id: str) -> dict | None:
        """Return the newest persisted switch event row for a stable channel ID."""
        ch_id = str(channel_id or "").strip()
        if not ch_id:
            return None

        try:
            with self._open_conn() as conn:
                identities = self.get_switch_identities()
                key_candidates: list[str] = []
                for row in identities or []:
                    row_ch = str(row.get("channel_id", "") or "").strip()
                    if row_ch.lower() != ch_id.lower():
                        continue
                    db_key = str(row.get("switch_key", "") or "").strip()
                    switch_id = str(row.get("switch_id", "") or "").strip()
                    label = str(row.get("label", "") or "").strip()
                    for candidate in (
                        db_key,
                        build_switch_key(switch_id, row_ch) if switch_id and row_ch else "",
                        f"{row_ch}{SW_KEY_DELIM}{label}" if row_ch and label else "",
                        f"{switch_id}{SW_KEY_DELIM}{label}" if switch_id and label else "",
                    ):
                        if candidate and candidate not in key_candidates:
                            key_candidates.append(candidate)
                if key_candidates:
                    placeholders = ",".join("?" for _ in key_candidates)
                    row = conn.execute(
                        f"""
                        SELECT switch_key, state, timestamp, ts_epoch, source, sensor_id
                        FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders})
                        ORDER BY COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL), 0.0) DESC
                        LIMIT 1
                        """,
                        tuple(key_candidates),
                    ).fetchone()
                else:
                    row = None
                if row is None:
                    # Legacy fallback for databases that have events but no switch_ids row.
                    row = conn.execute(
                        """
                        SELECT switch_key, state, timestamp, ts_epoch, source, sensor_id
                        FROM sw_events
                        WHERE switch_key LIKE ?
                        ORDER BY COALESCE(ts_epoch, CAST(strftime('%s', timestamp) AS REAL), 0.0) DESC
                        LIMIT 1
                        """,
                        (f"{ch_id}{SW_KEY_DELIM}%",),
                    ).fetchone()
        except Exception as e:
            if DEBUG:
                printDM(
                    f"[get_switch_last_event_for_channel] query error for {channel_id}: {e}",
                    location=MODULE,
                )
            return None
        if not row:
            return None
        epoch = _timestamp_to_epoch(
            row[3] if row[3] is not None else row[2],
            getattr(self, "local_tz", LOCAL_TIMEZONE),
        )
        return {
            "switch_key": row[0],
            "state": row[1],
            "timestamp": row[2],
            "ts_epoch": epoch,
            "source": row[4],
            "sensor_id": row[5],
        }

    def _switch_key_alias_candidates(self, switch_key: str) -> list[str]:
        key = str(switch_key or "").strip()
        if not key:
            return []

        candidates: list[str] = [key]
        if SW_KEY_DELIM not in key:
            return candidates

        prefix, suffix = key.split(SW_KEY_DELIM, 1)
        prefix = prefix.strip()
        suffix = suffix.strip()
        if not suffix:
            return candidates

        try:
            rows = self.get_switch_identities()
        except Exception:
            return candidates

        prefix_l = prefix.lower()
        suffix_l = suffix.lower()
        for row in rows or []:
            db_key = str(row.get("switch_key", "") or "").strip()
            db_sid = str(row.get("switch_id", "") or "").strip()
            db_label = str(row.get("label", "") or "").strip()
            db_channel = str(row.get("channel_id", "") or "").strip()
            if not db_key:
                continue

            prefix_matches = prefix_l in {
                db_sid.lower(),
                db_channel.lower(),
                db_key.lower(),
            }
            suffix_matches = suffix_l in {
                db_channel.lower(),
                db_label.lower(),
            }
            legacy_channel_label_matches = (
                prefix_l == db_channel.lower() and suffix_l == db_label.lower()
            )
            legacy_switch_label_matches = (
                prefix_l == db_sid.lower() and suffix_l == db_label.lower()
            )
            if not (
                (prefix_matches and suffix_matches)
                or legacy_channel_label_matches
                or legacy_switch_label_matches
            ):
                continue

            for candidate in (
                db_key,
                build_switch_key(db_sid, db_channel) if db_sid and db_channel else "",
                f"{db_channel}{SW_KEY_DELIM}{db_label}" if db_channel and db_label else "",
                f"{db_sid}{SW_KEY_DELIM}{db_label}" if db_sid and db_label else "",
            ):
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

        return candidates

    def get_last_switch_events(
        self,
        switch_key: str,
        sensor_id: str | None = None,
        limit: int = 5,
        *,
        include_source: bool = False,
    ):
        """
        Return list[(state_str, timestamp)] from sw_events for a given switch_key.
        When include_source=True, returns list[(state_str, timestamp, source)].
        sensor_id is optional, used only if you want to scope to a particular host.
        """
        try:
            candidates = self._switch_key_alias_candidates(switch_key)
            if not candidates:
                return []
            placeholders = ",".join(["?"] * len(candidates))
            select_cols = "timestamp, state, source" if include_source else "timestamp, state"
            with self._open_conn() as conn:
                cur = conn.cursor()
                if sensor_id:
                    cur.execute(
                        f"""
                        SELECT {select_cols}
                        FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders}) AND LOWER(sensor_id)=LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (*candidates, sensor_id, limit)
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT {select_cols}
                        FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders})
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (*candidates, limit)
                    )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    ts = row[0]
                    state = row[1]
                    source = row[2] if include_source and len(row) > 2 else None
                    is_on = bool(int(state)) if isinstance(state, (int, float)) else str(state).lower() in ("1","true","on")
                    if include_source:
                        result.append(("On" if is_on else "Off", ts, source))
                    else:
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
            candidates = self._switch_key_alias_candidates(switch_key)
            if not candidates:
                return None
            placeholders = ",".join(["?"] * len(candidates))
            with self._open_conn() as conn:
                cur = conn.cursor()
                if sensor_id:
                    cur.execute(
                        f"""
                        SELECT state FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders}) AND LOWER(sensor_id)=LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (*candidates, sensor_id)
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT state FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders})
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (*candidates,)
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

    def get_latest_switch_state_by_source_prefix(
        self,
        switch_key: str,
        *,
        source_prefix: str,
        sensor_id: str | None = None,
    ) -> str | None:
        """
        Returns "On"/"Off"/None using the newest sw_events row for the switch_key
        whose source starts with source_prefix (case-insensitive).
        """
        try:
            candidates = self._switch_key_alias_candidates(switch_key)
            if not candidates:
                return None
            placeholders = ",".join(["?"] * len(candidates))
            with self._open_conn() as conn:
                cur = conn.cursor()
                if sensor_id:
                    cur.execute(
                        f"""
                        SELECT state FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders})
                          AND LOWER(COALESCE(source, '')) LIKE LOWER(?)
                          AND LOWER(sensor_id)=LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (*candidates, f"{source_prefix}%", sensor_id)
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT state FROM sw_events
                        WHERE switch_key COLLATE NOCASE IN ({placeholders})
                          AND LOWER(COALESCE(source, '')) LIKE LOWER(?)
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT 1
                        """,
                        (*candidates, f"{source_prefix}%")
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
            printDM(f"[get_latest_switch_state_by_source_prefix] query failed: {e}", location=MODULE)
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
        now_mono = time.monotonic()
        cached = self._switch_identities_cache
        if cached and cached[0] > now_mono:
            return [dict(item) for item in cached[1]]
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

                channel_id = _channel_id_from_switch_key(switch_key, switch_id, label)

                results.append({
                    "switch_key": switch_key,
                    "switch_id": switch_id,
                    "channel_id": channel_id,
                    "label": label,
                    "location": location,
                })
            self._switch_identities_cache = (now_mono + 5.0, [dict(item) for item in results])
            return results

        except Exception as e:
            printDM(f"[get_switch_identities] error: {e}", location=MODULE)
            return []
