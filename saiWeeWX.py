"""WeeWX SQLite archive ingest for Linux/Raspberry Pi Sensorius installs."""

import asyncio
import os
import platform
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sensor_modules.station_weewx import (
    DEFAULT_DB_PATH,
    DEFAULT_POLL_INTERVAL_SEC,
    DEFAULT_SENSOR_ID,
    WEEWX_FIELD_MAP,
    normalize_weewx_values,
)
from saiUtils import debug_enabled, printDM

MODULE = "saiWeeWX"
DEBUG = debug_enabled(MODULE)
TASK_NAME = "WeeWX Archive Ingest"
HEARTBEAT_INTERVAL_SEC = 20.0


@dataclass(frozen=True)
class WeeWXArchiveReading:
    """Normalized latest archive row from a WeeWX SQLite database."""

    date_time: int
    values: dict[str, float]


class WeeWXArchiveIngest:
    """Poll a WeeWX archive database and mirror station data into Sensorius."""

    def __init__(self, *, settings, data_logger, supervisor=None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        tz_name = str(settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver")
        try:
            self.local_tz = ZoneInfo(tz_name)
        except Exception:
            self.local_tz = ZoneInfo("America/Denver")
        self._last_source_epoch: int | None = None
        self._last_missing_log_mono = 0.0

    def _feed_watchdog(self, *, error: bool = False) -> None:
        sup = getattr(self, "supervisor", None)
        if sup and hasattr(sup, "feedthedogs"):
            sup.feedthedogs(TASK_NAME, error=error)

    async def _sleep_with_heartbeat(self, total_sleep_s: float) -> None:
        remaining = max(float(total_sleep_s), 0.0)
        while remaining > 0.0:
            self._feed_watchdog()
            chunk = min(HEARTBEAT_INTERVAL_SEC, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    @property
    def db_path(self) -> str:
        return str(self.settings.get_setting("WeeWX", "DB_PATH", DEFAULT_DB_PATH) or DEFAULT_DB_PATH)

    @property
    def sensor_id(self) -> str:
        return str(self.settings.get_setting("WeeWX", "SENSOR_ID", DEFAULT_SENSOR_ID) or DEFAULT_SENSOR_ID).strip() or DEFAULT_SENSOR_ID

    @property
    def poll_interval_sec(self) -> float:
        try:
            return max(15.0, float(self.settings.get_setting("WeeWX", "POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC) or DEFAULT_POLL_INTERVAL_SEC))
        except Exception:
            return DEFAULT_POLL_INTERVAL_SEC

    def should_run(self) -> bool:
        enabled = bool(self.settings.get_setting("WeeWX", "ENABLED", False))
        auto_discover = bool(self.settings.get_setting("WeeWX", "AUTO_DISCOVER", True))
        if enabled:
            return True
        if not auto_discover:
            return False
        return platform.system().lower() == "linux" and Path(self.db_path).exists()

    def read_latest_archive(self) -> WeeWXArchiveReading | None:
        """Return the newest WeeWX archive row with Sensorius metric names."""
        path = self.db_path
        if not os.path.exists(path):
            return None

        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(archive)").fetchall()
                if len(row) > 1 and row[1]
            }
            if "dateTime" not in cols:
                return None
            selected = ["dateTime"]
            selected.extend(name for name in WEEWX_FIELD_MAP.keys() if name in cols)
            if "barometer" in cols:
                selected.append("barometer")
            col_sql = ", ".join(f'"{name}"' for name in selected)
            row = conn.execute(
                f"""
                SELECT {col_sql}
                FROM archive
                ORDER BY dateTime DESC
                LIMIT 1
                """
            ).fetchone()

        if not row or row["dateTime"] is None:
            return None

        values = normalize_weewx_values({key: row[key] for key in row.keys()})

        if not values:
            return None
        return WeeWXArchiveReading(date_time=int(row["dateTime"]), values=values)

    def _sensorius_latest_epoch(self) -> float | None:
        try:
            ts = self.data_logger.get_latest_timestamp(self.sensor_id)
            if not ts:
                return None
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.local_tz)
            return dt.timestamp()
        except Exception:
            return None

    def import_latest_once(self) -> bool:
        """Import the newest WeeWX archive row if Sensorius has not seen it."""
        reading = self.read_latest_archive()
        if reading is None:
            return False
        latest_epoch = self._sensorius_latest_epoch()
        if self._last_source_epoch and reading.date_time <= self._last_source_epoch:
            return False
        if latest_epoch is not None and reading.date_time <= latest_epoch:
            self._last_source_epoch = reading.date_time
            return False

        self.data_logger.log_readings(reading.date_time, self.sensor_id, reading.values)
        self._last_source_epoch = reading.date_time
        if DEBUG:
            printDM(
                f"Imported WeeWX archive row for {self.sensor_id}: {reading.date_time}, values: {reading.values}",
                location=MODULE,
            )
        return True

    async def run(self):
        """Poll WeeWX archive forever; intended for TaskSupervisor."""
        while True:
            self._feed_watchdog()
            if not self.should_run():
                now = time.monotonic()
                if now - self._last_missing_log_mono >= 300.0:
                    self._last_missing_log_mono = now
                    if DEBUG:
                        printDM(f"WeeWX archive not enabled or unavailable at {self.db_path}", location=MODULE)
                await self._sleep_with_heartbeat(self.poll_interval_sec)
                continue
            try:
                await asyncio.to_thread(self.import_latest_once)
                self._feed_watchdog()
            except Exception as exc:
                self._feed_watchdog(error=True)
                printDM(f"WeeWX archive import failed: {exc}", location=MODULE)
            await self._sleep_with_heartbeat(self.poll_interval_sec)
