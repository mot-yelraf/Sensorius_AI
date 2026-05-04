import sqlite3
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiWeeWX import WeeWXArchiveIngest
from sensor_modules.station_weewx import INHG_TO_HPA, normalize_weewx_mqtt_payload


class _Settings:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def get_setting(self, section, key, default=None, **_kwargs):
        values = {
            ("WeeWX", "ENABLED"): True,
            ("WeeWX", "AUTO_DISCOVER"): False,
            ("WeeWX", "DB_PATH"): self.db_path,
            ("WeeWX", "SENSOR_ID"): "weewx-test",
            ("WeeWX", "POLL_INTERVAL_SEC"): 15,
            ("Time", "TZ"): "America/Denver",
        }
        return values.get((section, key), default)


class _Logger:
    def __init__(self):
        self.rows = []

    def get_latest_timestamp(self, _sensor_id):
        if not self.rows:
            return None
        return self.rows[-1][0]

    def log_readings(self, timestamp, sensor_id, values):
        self.rows.append((timestamp, sensor_id, dict(values)))


def _make_weewx_db(path: Path, date_time: int = 1777908000):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE archive (
                dateTime INTEGER,
                outTemp REAL,
                outHumidity REAL,
                barometer REAL,
                windSpeed REAL,
                windDir REAL,
                rain REAL,
                rainRate REAL,
                dewpoint REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO archive (
                dateTime, outTemp, outHumidity, barometer, windSpeed,
                windDir, rain, rainRate, dewpoint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date_time, 48.6875, 65.625, 30.1655675484948, 2.5, 270, 0.01, 0.2, 39.1),
        )


def test_weewx_archive_ingest_maps_latest_archive_row(tmp_path):
    db_path = tmp_path / "weewx.sdb"
    _make_weewx_db(db_path)

    logger = _Logger()
    ingest = WeeWXArchiveIngest(settings=_Settings(db_path), data_logger=logger)

    assert ingest.import_latest_once() is True

    timestamp, sensor_id, values = logger.rows[0]
    assert timestamp == 1777908000
    assert sensor_id == "weewx-test"
    assert values["Temperature_F"] == 48.7
    assert values["Rel-Humidity"] == 66
    assert values["Wind Speed"] == 2.5
    assert values["Wind Direction"] == 270
    assert values["Rain"] == 0.01
    assert values["Rain Rate"] == 0.2
    assert values["Dew Point_F"] == 39.1
    assert values["Baro-Pressure"] == pytest.approx(round(30.1655675484948 * INHG_TO_HPA, 1))


def test_weewx_archive_ingest_skips_duplicate_latest_row(tmp_path):
    db_path = tmp_path / "weewx.sdb"
    _make_weewx_db(db_path)

    logger = _Logger()
    ingest = WeeWXArchiveIngest(settings=_Settings(db_path), data_logger=logger)

    assert ingest.import_latest_once() is True
    assert ingest.import_latest_once() is False
    assert len(logger.rows) == 1


def test_weewx_mqtt_json_payload_maps_to_sensorius_metrics():
    reading = normalize_weewx_mqtt_payload(
        "weewx/archive",
        '{"dateTime":1777908000,"outTemp":48.6875,"outHumidity":65.625,"barometer":30.1655675484948}',
        base_topic="weewx",
    )

    assert reading is not None
    assert reading.timestamp == 1777908000
    assert reading.values == {
        "Temperature_F": 48.7,
        "Rel-Humidity": 66,
        "Baro-Pressure": pytest.approx(round(30.1655675484948 * INHG_TO_HPA, 1)),
    }


def test_weewx_mqtt_single_field_topic_payload_maps_to_sensorius_metric():
    reading = normalize_weewx_mqtt_payload("weewx/outTemp", "50.55", base_topic="weewx")

    assert reading is not None
    assert reading.values == {"Temperature_F": 50.5}
