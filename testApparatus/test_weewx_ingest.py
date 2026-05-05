import sqlite3
import os
import sys
from pathlib import Path
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiWeeWX import WeeWXArchiveIngest
from saiMQTTIngest import saiMQTTIngest
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


class _Supervisor:
    def __init__(self):
        self.feeds = []

    def feedthedogs(self, task_name, error=False):
        self.feeds.append((task_name, bool(error)))


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


@pytest.mark.asyncio
async def test_weewx_archive_sleep_feeds_watchdog_between_long_polls(monkeypatch, tmp_path):
    db_path = tmp_path / "weewx.sdb"
    _make_weewx_db(db_path)
    supervisor = _Supervisor()
    ingest = WeeWXArchiveIngest(settings=_Settings(db_path), data_logger=_Logger(), supervisor=supervisor)
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("saiWeeWX.HEARTBEAT_INTERVAL_SEC", 20.0)
    monkeypatch.setattr("saiWeeWX.asyncio.sleep", _fast_sleep)

    await ingest._sleep_with_heartbeat(61.0)

    assert supervisor.feeds == [
        ("WeeWX Archive Ingest", False),
        ("WeeWX Archive Ingest", False),
        ("WeeWX Archive Ingest", False),
        ("WeeWX Archive Ingest", False),
    ]


@pytest.mark.asyncio
async def test_weewx_archive_run_feeds_watchdog_before_import_sleep(monkeypatch, tmp_path):
    db_path = tmp_path / "weewx.sdb"
    _make_weewx_db(db_path)
    supervisor = _Supervisor()
    ingest = WeeWXArchiveIngest(settings=_Settings(db_path), data_logger=_Logger(), supervisor=supervisor)

    async def _stop_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("saiWeeWX.asyncio.sleep", _stop_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ingest.run()

    assert supervisor.feeds[:3] == [
        ("WeeWX Archive Ingest", False),
        ("WeeWX Archive Ingest", False),
        ("WeeWX Archive Ingest", False),
    ]


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


def test_weewx_mqtt_metric_loop_payload_maps_to_sensorius_units():
    reading = normalize_weewx_mqtt_payload(
        "weather/loop",
        (
            '{"dateTime":"1777943700.0","windSpeed_kph":"11.2257926074189",'
            '"windDir":"334.33744110638094","barometer_mbar":"1010.6517104141302",'
            '"rain_cm":"0.0","rainRate_cm_per_hour":"0.0",'
            '"dewpoint_C":"0.3904233091127275","outTemp_C":"20.024691358024686",'
            '"outHumidity":"26.88888888888889"}'
        ),
        base_topic="weather",
    )

    assert reading is not None
    assert reading.timestamp == "1777943700.0"
    assert reading.values == {
        "Temperature_F": 68.0,
        "Rel-Humidity": 27,
        "Wind Speed": 7.0,
        "Wind Direction": 334,
        "Rain": 0.0,
        "Rain Rate": 0.0,
        "Dew Point_F": 32.7,
        "Baro-Pressure": 1010.7,
    }


def test_weewx_mqtt_ingest_skips_duplicate_payloads():
    class _MqttLogger(_Logger):
        def get_latest_values(self, sensor_id):
            if not self.rows:
                return {}
            return dict(self.rows[-1][2])

    ingest = saiMQTTIngest.__new__(saiMQTTIngest)
    ingest.weewx_mqtt_enabled = True
    ingest.weewx_mqtt_topic = "weather/#"
    ingest.weewx_sensor_id = "weewx-station"
    ingest.weewx_update_period_sec = 300
    ingest.data_logger = _MqttLogger()
    ingest.expected_gauge_map = {}
    ingest.device_type = {}
    ingest.device_location = {}
    ingest.last_mqtt_seen = {}
    ingest._mark_host_status = lambda *_args, **_kwargs: None

    payload = (
        '{"dateTime":1777943700,"windSpeed_kph":"11.2257926074189",'
        '"windDir":"334.33744110638094","barometer_mbar":"1010.6517104141302",'
        '"rain_cm":"0.0","rainRate_cm_per_hour":"0.0",'
        '"dewpoint_C":"0.3904233091127275","outTemp_C":"20.024691358024686",'
        '"outHumidity":"26.88888888888889"}'
    )

    assert ingest._maybe_handle_weewx_mqtt("weather/loop", payload) is True
    assert ingest._maybe_handle_weewx_mqtt("weather/loop", payload) is True
    assert len(ingest.data_logger.rows) == 1

    repeated_values_new_timestamp = payload.replace("1777943700", "1777943701", 1)
    assert ingest._maybe_handle_weewx_mqtt("weather/loop", repeated_values_new_timestamp) is True
    assert len(ingest.data_logger.rows) == 1


def test_weewx_live_reconfigure_subscribes_runtime_client():
    class _Client:
        def __init__(self):
            self.subscribed = []
            self.unsubscribed = []

        def subscribe(self, topic):
            self.subscribed.append(topic)
            return (0, 1)

        def unsubscribe(self, topic):
            self.unsubscribed.append(topic)
            return (0, 2)

    ingest = saiMQTTIngest.__new__(saiMQTTIngest)
    ingest.client = _Client()
    ingest.registered_topics = {"weewx/#"}
    ingest.weewx_mqtt_enabled = True
    ingest.weewx_mqtt_topic = "weewx/#"
    ingest.weewx_sensor_id = "weewx-station"
    ingest.weewx_update_period_sec = 300

    assert ingest.configure_weewx_mqtt(
        enabled=True,
        topic_filter="weather/#",
        sensor_id="weather-station",
        update_period_sec=120,
    ) is True

    assert ingest.weewx_mqtt_topic == "weather/#"
    assert ingest.weewx_sensor_id == "weather-station"
    assert ingest.weewx_update_period_sec == 120
    assert "weather/#" in ingest.registered_topics
    assert ingest.client.unsubscribed == ["weewx/#"]
    assert ingest.client.subscribed == ["weather/#"]
