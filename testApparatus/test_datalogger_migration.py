"""Pytest coverage for SQLite migration and daily-summary persistence paths.

This module verifies legacy database migration behavior and round-trip storage
for biodynamic daily summaries.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import saiSettings
from saiDataLogger import saiDataLogger


def _create_legacy_db(path: str) -> None:
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        # Legacy schema intentionally omits ts_epoch columns.
        cur.execute(
            """
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sensor_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE sw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                switch_key TEXT NOT NULL,
                state INTEGER NOT NULL,
                source TEXT,
                sensor_id TEXT
            )
            """
        )
        conn.commit()


def test_init_db_migrates_legacy_ts_epoch_columns(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "legacy.db"
    _create_legacy_db(str(db_path))

    logger = saiDataLogger(db_path=str(db_path))
    logger.close()

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(readings)")
        readings_cols = {row[1] for row in cur.fetchall()}
        cur.execute("PRAGMA table_info(sw_events)")
        sw_events_cols = {row[1] for row in cur.fetchall()}

        cur.execute("PRAGMA index_list(readings)")
        readings_indexes = {row[1] for row in cur.fetchall()}
        cur.execute("PRAGMA index_list(sw_events)")
        sw_events_indexes = {row[1] for row in cur.fetchall()}

    assert "ts_epoch" in readings_cols
    assert "ts_epoch" in sw_events_cols
    assert "idx_readings_sid_metric_tse" in readings_indexes
    assert "idx_swe_key_tse" in sw_events_indexes


def test_biodynamic_daily_summaries_round_trip(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "daily-summary.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        assert logger.save_biodynamic_daily_summary("2026-03-08", "24 hr Metrics for 2026-03-07")
        assert logger.save_biodynamic_note("2026-03-08", "User note text")

        summaries = logger.get_biodynamic_daily_summaries_for_month("2026-03-01")
        notes = logger.get_biodynamic_notes_for_month("2026-03-01")

        assert summaries == {"2026-03-08": "24 hr Metrics for 2026-03-07"}
        assert notes == {"2026-03-08": "User note text"}
    finally:
        logger.close()
        saiDataLogger._schema_ready = False
