import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSensorSettingsManager import SensorSettingsManager
import saiWebRoutes
from saiWebRoutes import ensure_weewx_sensor_settings
from sensor_modules.station_weewx import (
    WEEWX_DISPLAY_METRICS,
    WeeWXStationMetadata,
    apply_weewx_station_metadata,
    parse_weewx_station_metadata,
)
from Sensorius import is_remote_sensor_settings


def test_weewx_station_metadata_parser_reads_model_and_driver():
    metadata = parse_weewx_station_metadata(
        """
debug = 0

[Station]
    station_type = AcuRite

[AcuRite]
    model = AcuRite 01536
    driver = weewx.drivers.acurite

[[IgnoredNestedSection]]
    model = Wrong Model
""",
        config_path="/etc/weewx/weewx.conf",
    )

    assert metadata.config_path == "/etc/weewx/weewx.conf"
    assert metadata.station_type == "AcuRite"
    assert metadata.model == "AcuRite 01536"
    assert metadata.driver == "weewx.drivers.acurite"


def test_weewx_station_metadata_applies_to_sensor_block():
    sensor_block = {"TYPE": "weewx", "DEVICE": "weewx"}
    changed = apply_weewx_station_metadata(
        sensor_block,
        WeeWXStationMetadata(
            config_path="/etc/weewx/weewx.conf",
            station_type="AcuRite",
            model="AcuRite 01536",
            driver="weewx.drivers.acurite",
        ),
    )

    assert changed is True
    assert sensor_block["STATION_MODEL"] == "AcuRite 01536"
    assert sensor_block["STATION_TYPE"] == "AcuRite"
    assert sensor_block["STATION_DRIVER"] == "weewx.drivers.acurite"


def test_weewx_seed_uses_station_display_defaults(tmp_path):
    manager = SensorSettingsManager(str(tmp_path / "sensor_settings"))

    manager.seed_from_factory("weewx-station", device="weewx", location="Weather Station")
    doc = manager.load("weewx-station")

    assert doc["Sensor"]["DEVICE"] == "weewx"
    assert doc["Sensor"]["SENSOR_ID"] == "weewx-station"
    assert doc["Sensor"]["LOCATION"] == "Weather Station"
    assert [doc["Display"][f"METRIC_{idx}"] for idx in range(1, 7)] == WEEWX_DISPLAY_METRICS


def test_weewx_settings_are_not_built_as_local_sensor_controller():
    assert is_remote_sensor_settings({"Sensor": {"TYPE": "weewx", "DEVICE": "weewx"}}) is True


def test_weewx_materializer_creates_missing_sensor_toml(tmp_path):
    manager = SensorSettingsManager(str(tmp_path / "sensor_settings"))

    ensure_weewx_sensor_settings("weewx-station", manager=manager)
    doc = manager.load("weewx-station")

    assert doc["Sensor"]["TYPE"] == "weewx"
    assert doc["Sensor"]["DEVICE"] == "weewx"
    assert doc["Sensor"]["SENSOR_ID"] == "weewx-station"
    assert [doc["Display"][f"METRIC_{idx}"] for idx in range(1, 7)] == WEEWX_DISPLAY_METRICS


def test_weewx_materializer_records_station_metadata(tmp_path, monkeypatch):
    manager = SensorSettingsManager(str(tmp_path / "sensor_settings"))
    metadata = WeeWXStationMetadata(
        config_path="/etc/weewx/weewx.conf",
        station_type="AcuRite",
        model="AcuRite 01536",
        driver="weewx.drivers.acurite",
    )
    monkeypatch.setattr(
        saiWebRoutes,
        "apply_weewx_station_metadata",
        lambda sensor_block: apply_weewx_station_metadata(sensor_block, metadata),
    )

    ensure_weewx_sensor_settings("weewx-station", manager=manager)
    doc = manager.load("weewx-station")

    assert doc["Sensor"]["STATION_MODEL"] == "AcuRite 01536"
    assert doc["Sensor"]["STATION_TYPE"] == "AcuRite"
    assert doc["Sensor"]["STATION_DRIVER"] == "weewx.drivers.acurite"
