import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSensorSettingsManager import SensorSettingsManager
from saiWebRoutes import ensure_weewx_sensor_settings
from sensor_modules.station_weewx import WEEWX_DISPLAY_METRICS
from Sensorius import is_remote_sensor_settings


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
