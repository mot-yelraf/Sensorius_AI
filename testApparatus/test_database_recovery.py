"""Test SQLite corruption detection and bounded automatic recovery.

Each test uses temporary databases; the optional SQLite CLI recovery case is
skipped when the command-line tool is unavailable.
"""

from __future__ import annotations

import os
import importlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiDataLogger as datalogger_module
import sensorius.saiSettings as saiSettings
from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiStats import saiStats


class _StubSettings:
    def __init__(self, apply_live=False):
        self.apply_live = apply_live

    def get_setting(self, section, key):
        if section == "Time" and key in ("TZ", "tz"):
            return "America/Denver"
        return None


def _reset_recovery_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)
    monkeypatch.setattr(saiDataLogger, "_schema_ready", False)
    monkeypatch.setattr(saiDataLogger, "_recovery_last_attempt_by_path", {})
    monkeypatch.setenv("SENSORIUS_DB_AUTO_RECOVER", "1")
    monkeypatch.setenv("SENSORIUS_DB_AUTO_REBUILD_ON_RECOVERY_FAIL", "1")
    monkeypatch.setenv("SENSORIUS_DB_RECOVERY_MIN_INTERVAL_SEC", "0")


def _recovery_dirs(tmp_path: Path) -> list[Path]:
    recovery_root = tmp_path / "database_recovery"
    if not recovery_root.exists():
        return []
    return sorted(path for path in recovery_root.iterdir() if path.is_dir())


def test_sqlite_corruption_marker_detection():
    assert saiDataLogger.is_sqlite_corruption_error("database disk image is malformed")
    assert saiDataLogger.is_sqlite_corruption_error(sqlite3.DatabaseError("file is not a database"))
    assert saiDataLogger.is_sqlite_corruption_error("malformed database schema")
    assert not saiDataLogger.is_sqlite_corruption_error("database is locked")


def test_auto_recovery_can_be_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _reset_recovery_state(monkeypatch)
    monkeypatch.setenv("SENSORIUS_DB_AUTO_RECOVER", "0")

    db_path = tmp_path / "sensorius_data.db"
    db_path.write_bytes(b"not sqlite")

    recovered = saiDataLogger.recover_database_after_error(
        str(db_path),
        sqlite3.DatabaseError("file is not a database"),
        source="test",
    )

    assert recovered is False
    assert not (tmp_path / "database_recovery").exists()
    assert db_path.read_bytes() == b"not sqlite"


def test_corrupt_database_rebuilds_empty_db_when_recover_cli_unavailable(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _reset_recovery_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(datalogger_module.shutil, "which", lambda _name: None)

    db_path = tmp_path / "sensorius_data.db"
    db_path.write_bytes(b"not sqlite")

    logger = saiDataLogger(str(db_path))
    try:
        logger.log_readings("2026-07-05T08:30:00-06:00", "co2-test123", {"Temperature": 24.0})
    finally:
        logger.close()

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            """
            SELECT sensor_id, metric, value
            FROM readings
            WHERE sensor_id = ?
            """,
            ("co2-test123",),
        ).fetchone()

    assert row == ("co2-test123", "Temperature", 24.0)
    recovery_dirs = _recovery_dirs(tmp_path)
    assert len(recovery_dirs) == 1
    assert (recovery_dirs[0] / "sensorius_data.db").exists()
    assert (recovery_dirs[0] / "sensorius_data.db.damaged").exists()


def test_recovery_attempts_are_rate_limited(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _reset_recovery_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SENSORIUS_DB_AUTO_REBUILD_ON_RECOVERY_FAIL", "0")
    monkeypatch.setenv("SENSORIUS_DB_RECOVERY_MIN_INTERVAL_SEC", "3600")
    monkeypatch.setattr(datalogger_module.shutil, "which", lambda _name: None)

    db_path = tmp_path / "sensorius_data.db"
    db_path.write_bytes(b"not sqlite")

    for _idx in range(2):
        recovered = saiDataLogger.recover_database_after_error(
            str(db_path),
            sqlite3.DatabaseError("file is not a database"),
            source="test",
        )
        assert recovered is False

    assert len(_recovery_dirs(tmp_path)) == 1
    assert db_path.read_bytes() == b"not sqlite"


def test_stats_query_triggers_corrupt_database_rebuild(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _reset_recovery_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(datalogger_module.shutil, "which", lambda _name: None)

    db_path = tmp_path / "sensorius_data.db"
    db_path.write_bytes(b"not sqlite")

    stats = saiStats(str(db_path))
    assert stats.get_all_stats_fast() == {}

    with sqlite3.connect(str(db_path)) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='readings'"
        ).fetchone()

    assert table == ("readings",)
    assert len(_recovery_dirs(tmp_path)) == 1


def test_web_route_sqlite_connect_uses_shared_recovery(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _reset_recovery_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(datalogger_module.shutil, "which", lambda _name: None)

    db_path = tmp_path / "sensorius_data.db"
    db_path.write_bytes(b"not sqlite")

    routes = importlib.import_module("sensorius.saiWebRoutes")
    conn = routes._sqlite_connect_with_recovery(str(db_path), source="test-web-connect")
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='readings'"
        ).fetchone()
    finally:
        conn.close()

    assert table == ("readings",)
    assert len(_recovery_dirs(tmp_path)) == 1


def test_sqlite_cli_recovery_preserves_rows_when_available(tmp_path, monkeypatch: pytest.MonkeyPatch):
    sqlite3_bin = shutil.which("sqlite3")
    if not sqlite3_bin:
        pytest.skip("sqlite3 CLI is not installed")
    recover_probe = subprocess.run(
        [sqlite3_bin, ":memory:", ".recover"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if recover_probe.returncode != 0:
        pytest.skip("sqlite3 CLI does not provide functional .recover support")

    _reset_recovery_state(monkeypatch)
    monkeypatch.chdir(tmp_path)

    db_path = tmp_path / "sensorius_data.db"
    logger = saiDataLogger(str(db_path))
    try:
        logger.log_readings("2026-07-05T08:31:00-06:00", "co2-test123", {"Temperature": 25.0})
    finally:
        logger.close()

    recovered = saiDataLogger.recover_database_after_error(
        str(db_path),
        sqlite3.DatabaseError("database disk image is malformed"),
        source="test",
    )

    assert recovered is True
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            """
            SELECT sensor_id, metric, value
            FROM readings
            WHERE sensor_id = ?
            """,
            ("co2-test123",),
        ).fetchone()

    assert row == ("co2-test123", "Temperature", 25.0)
    recovery_dirs = _recovery_dirs(tmp_path)
    assert len(recovery_dirs) == 1
    assert any(path.name.endswith(".recovered.sqlite3") for path in recovery_dirs[0].iterdir())
