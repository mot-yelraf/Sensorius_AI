from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiWebRoutes
from saiDataLogger import saiDataLogger


def test_advanced_retention_clamps_to_365_days():
    assert saiWebRoutes._DB_RETENTION_MAX_DAYS == 365
    assert saiWebRoutes._clamp_db_retention_days("999") == 365
    assert saiWebRoutes._clamp_db_retention_days("5") == 30
    assert saiWebRoutes._clamp_db_retention_days("bad") == 90


def test_database_archive_copies_logged_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SENSORIUS_DB_RETENTION_DAYS", "0")
    monkeypatch.setattr(saiDataLogger, "_schema_ready", False)

    db_path = tmp_path / "sensorius_data.db"
    archive_dir = tmp_path / "database_archives"
    logger = saiDataLogger(str(db_path))
    try:
        logger.log_readings("2026-06-16T12:00:00-06:00", "co2-test123", {"Temperature": 24.5})
        archive_path = logger.create_database_archive(archive_dir)
    finally:
        logger.close()

    assert archive_path.parent == archive_dir.resolve()
    assert archive_path.name.startswith("sensorius_data-")
    assert archive_path.suffix == ".sqlite3"

    with sqlite3.connect(archive_path) as conn:
        row = conn.execute(
            """
            SELECT sensor_id, metric, value
            FROM readings
            WHERE sensor_id = ?
            """,
            ("co2-test123",),
        ).fetchone()

    assert row == ("co2-test123", "Temperature", 24.5)
