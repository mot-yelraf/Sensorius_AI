"""Pytest coverage for SQLite migration and daily-summary persistence paths.

This module verifies legacy database migration behavior and round-trip storage
for biodynamic daily summaries.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import saiSettings
from saiCalibration import CalibrationManager
from saiDataLogger import build_switch_key, saiDataLogger
from sensor_modules.station_weewx import WEEWX_RAIN_24H_METRIC


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

    saiDataLogger._schema_ready = False
    try:
        logger = saiDataLogger(db_path=str(db_path))
        logger.close()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(readings)")
            readings_cols = {row[1] for row in cur.fetchall()}
            cur.execute("PRAGMA table_info(sw_events)")
            sw_events_cols = {row[1] for row in cur.fetchall()}
            cur.execute("PRAGMA table_info(sensor_events)")
            sensor_events_cols = {row[1] for row in cur.fetchall()}

            cur.execute("PRAGMA index_list(readings)")
            readings_indexes = {row[1] for row in cur.fetchall()}
            cur.execute("PRAGMA index_list(sw_events)")
            sw_events_indexes = {row[1] for row in cur.fetchall()}
            cur.execute("PRAGMA index_list(sensor_events)")
            sensor_events_indexes = {row[1] for row in cur.fetchall()}

        assert "ts_epoch" in readings_cols
        assert "ts_epoch" in sw_events_cols
        assert {"ts_epoch", "sensor_id", "event_type", "state", "source"}.issubset(sensor_events_cols)
        assert "idx_readings_sid_metric_tse" in readings_indexes
        assert "idx_swe_key_tse" in sw_events_indexes
        assert "idx_sensor_events_sid_type_state_tse" in sensor_events_indexes
    finally:
        saiDataLogger._schema_ready = False


def test_sensor_offline_event_count_uses_24h_window_and_aliases(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "sensor-events.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        logger.log_sensor_event(
            "apvpd-test123",
            "liveness",
            state="offline",
            timestamp="2026-06-07T12:00:00-06:00",
            source="test",
        )
        logger.log_sensor_event(
            "apvpd-test123.local",
            "liveness",
            state="offline",
            timestamp="2026-06-07T12:05:00-06:00",
            source="test",
        )
        logger.log_sensor_event(
            "apvpd-test123",
            "liveness",
            state="online",
            timestamp="2026-06-07T12:10:00-06:00",
            source="test",
        )
        logger.log_sensor_event(
            "apvpd-test123",
            "liveness",
            state="offline",
            timestamp="2026-06-06T10:00:00-06:00",
            source="test",
        )

        end_epoch = datetime.fromisoformat("2026-06-07T12:30:00-06:00").timestamp()

        assert logger.get_sensor_offline_event_count("apvpd-test123", end_epoch=end_epoch) == 1
        assert (
            logger.get_sensor_offline_event_count(
                "apvpd-test123",
                aliases=["apvpd-test123.local"],
                end_epoch=end_epoch,
            )
            == 2
        )
        assert logger.get_sensor_last_offline_event_epoch("apvpd-test123") == datetime.fromisoformat(
            "2026-06-07T12:00:00-06:00"
        ).timestamp()
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


def test_statistics_packet_counts_use_persisted_sensor_and_switch_rows(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "stats-packets.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        logger.log_readings(
            "2026-06-07T12:00:00-06:00",
            "apvpd-test123",
            {"Temperature": 21.5, "Rel-Humidity": 55.0},
        )
        logger.log_readings(
            "2026-06-07T12:05:00-06:00",
            "apvpd-test123",
            {"Temperature": 21.8},
        )
        logger.log_readings(
            "2026-06-07T12:10:00-06:00",
            "aqi-other",
            {"AQI": 12},
        )

        switch_key = build_switch_key("S1-test123", "Fan")
        logger.upsert_switch_identity(
            switch_key=switch_key,
            switch_id="switch-test123",
            label="Fan",
            location="Veg Tent",
        )
        logger.log_switch_event(
            switch_key,
            True,
            timestamp="2026-06-07T12:00:00-06:00",
            sensor_id="switch-test123",
            source="mqtt",
        )
        logger.log_switch_event(
            switch_key,
            False,
            timestamp="2026-06-07T12:05:00-06:00",
            sensor_id="switch-test123",
            source="mqtt",
        )

        assert logger.get_sensor_packet_count("apvpd-test123") == 2
        assert logger.get_sensor_packet_count("apvpd-test123", since_epoch=datetime.fromisoformat(
            "2026-06-07T12:03:00-06:00"
        ).timestamp()) == 1
        assert logger.get_sensor_last_packet_epoch("apvpd-test123") == datetime.fromisoformat(
            "2026-06-07T12:05:00-06:00"
        ).timestamp()
        assert logger.get_switch_packet_count("switch-test123") == 2
        assert logger.get_switch_packet_count("switch-test123", switch_keys=[switch_key]) == 2
        last_event = logger.get_switch_last_event("switch-test123", switch_keys=[switch_key])
        assert last_event is not None
        assert last_event["switch_key"] == switch_key
        assert last_event["state"] == 0
        assert last_event["ts_epoch"] == datetime.fromisoformat("2026-06-07T12:05:00-06:00").timestamp()
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


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


def test_available_metrics_by_sensor_caches_and_invalidates_on_write(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "metrics-cache.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        logger.log_readings(
            "2026-05-24T12:00:00",
            "co2-ykdvea",
            {"CO2": 700.0, "Temperature": 25.0, "Rel-Humidity": 55.0},
        )
        logger.log_readings(
            "2026-05-24T12:00:00",
            "apvpd-test123",
            {"Temperature": 24.0, "Rel-Humidity": 50.0},
        )

        metrics_by_sensor = logger.get_available_metrics_by_sensor()
        assert metrics_by_sensor["co2-ykdvea"] == ["CO2", "Rel-Humidity", "Temperature"]
        assert logger.get_available_metrics("CO2-YKDVEA") == ["CO2", "Rel-Humidity", "Temperature"]

        logger.log_readings("2026-05-24T12:01:00", "co2-ykdvea", {"Ambient VPD": 1.2})

        assert "Ambient VPD" in logger.get_available_metrics("co2-ykdvea")
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


def test_rain_last_24h_metric_is_derived_from_interval_rain(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "rain-24h.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        logger.log_readings("2026-05-23T12:00:00-06:00", "weewx-station", {"Rain": 0.50})
        logger.log_readings("2026-05-24T11:55:00-06:00", "weewx-station", {"Rain": 0.10})
        logger.log_readings("2026-05-24T11:55:20-06:00", "weewx-station", {"Rain": 0.10})
        logger.log_readings("2026-05-24T12:05:00-06:00", "weewx-station", {"Rain": 0.07})
        logger.log_readings(
            "2026-05-24T12:10:00-06:00",
            "weewx-station",
            {"Rain": 0.03, WEEWX_RAIN_24H_METRIC: 99.0},
        )

        latest = logger.get_latest_values("weewx-station")
        assert latest["Rain"] == 0.03
        assert latest[WEEWX_RAIN_24H_METRIC] == 0.20

        metrics = logger.get_available_metrics("weewx-station")
        assert metrics[metrics.index("Rain") + 1] == WEEWX_RAIN_24H_METRIC

        with sqlite3.connect(str(db_path)) as conn:
            stored = conn.execute(
                """
                SELECT value FROM readings
                WHERE sensor_id = ? AND metric = ?
                ORDER BY COALESCE(ts_epoch, 0.0) DESC
                LIMIT 1
                """,
                ("weewx-station", WEEWX_RAIN_24H_METRIC),
            ).fetchone()
        assert stored is not None
        assert stored[0] == 0.20
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


def test_latest_timestamps_bulk_lookup_uses_one_result_per_sensor(tmp_path, monkeypatch: pytest.MonkeyPatch):
    class _StubSettings:
        def __init__(self, apply_live=False):
            self.apply_live = apply_live

        def get_setting(self, section, key):
            if section == "Time" and key in ("TZ", "tz"):
                return "America/Denver"
            return None

    monkeypatch.setattr(saiSettings, "saiSettings", _StubSettings)

    db_path = tmp_path / "latest-timestamps.db"
    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(db_path))

    try:
        logger.log_readings("2026-05-24T12:00:00", "co2-ykdvea", {"CO2": 700.0})
        logger.log_readings("2026-05-24T12:05:00", "co2-ykdvea", {"CO2": 705.0, "Temperature": 25.0})
        logger.log_readings("2026-05-24T12:03:00", "apvpd-test123", {"Temperature": 24.0})

        logger.sensor_timestamps.clear()
        latest = logger.get_latest_timestamps(["CO2-YKDVEA", "apvpd-test123", "missing"])

        assert latest["CO2-YKDVEA"] == "2026-05-24T12:05:00-06:00"
        assert latest["apvpd-test123"] == "2026-05-24T12:03:00-06:00"
        assert "missing" not in latest
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


def test_calibration_manager_uses_bulk_metrics_lookup_for_calibratable_sensors():
    class _Logger:
        def __init__(self):
            self.bulk_calls = 0
            self.per_sensor_calls = 0

        def get_available_metrics_by_sensor(self):
            self.bulk_calls += 1
            return {
                "co2-ykdvea": ["CO2", "Temperature", "Rel-Humidity"],
                "weather": ["Temperature_F", "Rel-Humidity"],
                "switch": ["State"],
            }

        def get_available_sensors(self):
            raise AssertionError("fallback sensor lookup should not be used")

        def get_available_metrics(self, sensor_id):
            self.per_sensor_calls += 1
            return []

    logger = _Logger()
    manager = CalibrationManager(logger, sensor_mgr=None)

    assert manager.get_calibratable_sensors() == ["co2-ykdvea"]
    assert logger.bulk_calls == 1
    assert logger.per_sensor_calls == 0
