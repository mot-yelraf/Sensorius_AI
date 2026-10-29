"""Verify AcuRite calibration affects persisted values without changing identity."""

from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from jinja2 import Environment, FileSystemLoader

from sensorius.saiHtml import dashboard_sensor_label, render_dashboard, get_gauge_config
from sensorius.weather_adapters.acurite_calibration import calibrate_acurite_measurements, system_altitude_meters
from sensorius.weather_adapters.observations import Observation
from test_weather_devices import service
from testApparatus.test_nodus_settings_schema_writes import _build_app, _HubSettingsWithAltitude, _REAL_SENSOR_SETTINGS_MANAGER


def test_outdoor_offsets_and_altitude_preserve_absolute_pressure_and_raw_cache():
    altitude = 1624
    absolute = 1013.25 * (1 - altitude / 44330) ** 5.255
    raw = {"Temperature": 20, "Temperature_F": 68, "Rel-Humidity": 65,
           "Humidity": 999, "Ambient VPD": 999, "Console Temperature": 25,
           "Console Absolute Baro-Pressure": absolute}
    calibrated = calibrate_acurite_measurements(raw, {"TEMP_OFFSET": 1, "RH_OFFSET": 5}, altitude)
    assert calibrated["Temperature"] == 21
    assert calibrated["Temperature_F"] == 69.8
    assert calibrated["Rel-Humidity"] == 70
    assert calibrated["Console Temperature"] == 25
    assert calibrated["Console Absolute Baro-Pressure"] == absolute
    assert calibrated["Baro-Pressure"] == pytest.approx(1013.25, abs=.1)
    assert calibrated["Humidity"] == pytest.approx(12.82, abs=.05)
    assert calibrated["Ambient VPD"] == pytest.approx(.746, abs=.01)
    assert raw["Temperature"] == 20 and raw["Humidity"] == 999
    assert calibrate_acurite_measurements(raw, {"TEMP_OFFSET": 1, "RH_OFFSET": 5}, altitude) == calibrated


@pytest.mark.parametrize("altitude", [None, "", "nan", float("inf"), True, -501, 10001])
def test_missing_invalid_altitude_never_labels_absolute_pressure_as_relative(altitude):
    result = calibrate_acurite_measurements({"Console Absolute Baro-Pressure": 800}, {}, system_altitude_meters(altitude))
    assert result == {"Console Absolute Baro-Pressure": 800}


@pytest.mark.parametrize("altitude", [-100, 0, 1600])
def test_zero_and_negative_altitude_are_valid(altitude):
    result = calibrate_acurite_measurements({"Temperature": 0, "Rel-Humidity": 0, "Console Absolute Baro-Pressure": 1000}, {}, system_altitude_meters(altitude))
    assert result["Temperature_F"] == 32
    assert result["Rel-Humidity"] == 0
    assert result["Baro-Pressure"] == pytest.approx(1000 / (1 - altitude / 44330) ** 5.255, abs=.1)


def test_rh_clamps_and_missing_outdoor_fields_do_not_gain_derived_values():
    assert calibrate_acurite_measurements({"Rel-Humidity": 98}, {"RH_OFFSET": 10}, None) == {"Rel-Humidity": 100}
    assert calibrate_acurite_measurements({"Rel-Humidity": 1}, {"RH_OFFSET": -10}, None) == {"Rel-Humidity": 0}
    assert calibrate_acurite_measurements({"Humidity": 10, "Ambient VPD": 2}, {}, None) == {}
    with pytest.raises(ValueError, match="finite"):
        calibrate_acurite_measurements({}, {"TEMP_OFFSET": float("nan")}, None)


@pytest.mark.asyncio
async def test_calibrations_reload_before_storage_and_listener_delivery(service):
    current, settings, logger, manager = service
    assert await current.poll_once()
    sid = current.status()["sensor_id"]
    doc = manager.load(sid)
    doc["Calibration"]["Device"].update(TEMP_OFFSET=1, RH_OFFSET=-10)
    manager.save(sid, doc)
    settings.replace_setting("Astral", "ALTITUDE", 300)
    delivered = []
    logger.add_readings_listener(lambda *args: delivered.append(args))
    current._last_write = float("-inf")
    assert await current.poll_once()
    values = logger.get_latest_values(sid)
    assert values["Temperature"] == 21
    assert values["Temperature_F"] == 69.8
    assert values["Rel-Humidity"] == 55
    assert values["Baro-Pressure"] == pytest.approx(1013.25 / (1 - 300 / 44330) ** 5.255, abs=.1)
    assert values["Console Absolute Baro-Pressure"] == pytest.approx(1013.25, abs=.1)
    assert delivered[-1][2]["Baro-Pressure"] == values["Baro-Pressure"]
    assert manager.load(sid)["Calibration"]["Device"]["ALTITUDE_METERS"] == 300
    assert manager.get_display_metrics(sid)[-1] == "Baro-Pressure"
    settings.replace_setting("Astral", "ALTITUDE", 600)
    current._last_write = float("-inf")
    assert await current.poll_once()
    assert manager.load(sid)["Calibration"]["Device"]["ALTITUDE_METERS"] == 600
    assert logger.get_latest_values(sid)["Temperature"] == 21
    assert logger.get_latest_values(sid)["Baro-Pressure"] > values["Baro-Pressure"]


def test_previous_default_pressure_choice_migrates_but_custom_display_survives(service):
    current, settings, logger, manager = service
    sid = "acurite-example"
    previous = ("Temperature_F", "Rel-Humidity", "Wind Speed", "Wind Direction", "Rain", "Console Absolute Baro-Pressure")
    manager.save(sid, {"Sensor": {"LOCATION": "Orchard"}, "Display": {f"METRIC_{i}": metric for i, metric in enumerate(previous, 1)}})
    now = datetime.now(timezone.utc)
    observation = Observation("source", now, now, {"Temperature": 20})
    current._calibrate_observation(sid, observation)
    assert manager.get_display_metrics(sid)[-1] == "Baro-Pressure"
    assert manager.get_setting(sid, "Sensor.LOCATION") == "Orchard"
    manager.update_setting(sid, "Display.METRIC_1", "Wind Speed")
    manager.update_setting(sid, "Display.METRIC_6", "Console Absolute Baro-Pressure")
    current._calibrate_observation(sid, observation)
    assert manager.get_display_metrics(sid)[-1] == "Console Absolute Baro-Pressure"


@pytest.mark.asyncio
async def test_acurite_calibration_modal_and_apply_use_system_altitude_and_no_mqtt(tmp_path, monkeypatch):
    app, ingest, _, sensor_root, _ = await _build_app(tmp_path, monkeypatch, _HubSettingsWithAltitude("1624"))
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "ui_templates")))
    manager = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sid = "acurite-b6a529ffb882c4f"
    manager.save(sid, {"Sensor": {"TYPE": "station", "DEVICE": "acurite_usb", "SENSOR_ID": sid}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/edit-sensor", params={"sensor_id": sid, "embed": 1})
        assert response.status_code == 200, response.text
        assert "Sensor Calibration" in response.text
        for key in ("TEMP_OFFSET", "RH_OFFSET", "ALTITUDE_METERS"):
            assert f'data-key="Calibration.Device.{key}"' in response.text
        assert 'readonly aria-readonly="true"' in response.text
        response = await client.post("/calibration/device/apply", json={"sensor_id": sid, "device_kind": "acurite_usb", "offsets": [
            {"key": "Calibration.Device.TEMP_OFFSET", "value": 1.8, "input_unit": "°F"},
            {"key": "Calibration.Device.RH_OFFSET", "value": -5},
            {"key": "Calibration.Device.ALTITUDE_METERS", "value": 1, "force_send": True},
        ]})
        assert response.status_code == 200, response.text
        assert manager.load(sid)["Calibration"]["Device"] == {"TEMP_OFFSET": 1, "RH_OFFSET": -5, "ALTITUDE_METERS": 1624}
        assert not ingest.calibration_commands
        response = await client.post("/calibration/device/apply", json={"sensor_id": sid, "device_kind": "aqi", "offsets": [
            {"key": "Calibration.Device.AQI_OFFSET", "value": 1},
        ]})
        assert response.status_code == 400


@pytest.mark.parametrize("sid,label", [
    ("ecowitt-f85b1b51804c", "ECOWITT-51804C"),
    ("acurite-b6a529ffb882c4f", "ACURITE-882C4F"),
    ("ecowitt-123", "ECOWITT-123"),
    ("aht-test123", "AHT-TEST123"),
    ("ecowitt-123456-ac1100-1234567", "ECOWITT-123456-AC1100-1234567"),
])
def test_dashboard_label_only_shortens_station_uuid(sid, label):
    assert dashboard_sensor_label(sid) == label
    html = "".join(render_dashboard("All", None, [sid], {sid: {"Temperature": 20}}, {},
        SimpleNamespace(expected_gauge_map={}), gauge_config=get_gauge_config(), expected_gauge_map={sid: ["Temperature"]}))
    assert f" {label} " in html
    assert f"id='{sid}_header'" in html
    assert f"data-sensor-id='{sid}'" in html


def test_station_pressure_is_a_display_label_preserving_stored_metric_identity():
    from sensorius.saiWeatherForecastApp import build_display_metrics
    metric = "Console Absolute Baro-Pressure"
    sid = "acurite-example"
    config = get_gauge_config()
    assert config[metric]["display_label"] == "Station Pressure"
    html = "".join(render_dashboard("All", None, [sid], {sid: {metric: 826}}, {},
        SimpleNamespace(expected_gauge_map={}), gauge_config=config, expected_gauge_map={sid: [metric]}))
    assert "Station Pressure (" in html
    assert f"data-metric='{metric}'" in html
    displayed = build_display_metrics({metric: 826}, [metric], "Metric")
    assert displayed[0]["name"] == "Station Pressure"
    assert displayed[0]["value"] == 826


def test_repeated_calibration_reads_do_not_rewrite_unchanged_settings(service, monkeypatch):
    current, settings, logger, manager = service
    sid = "acurite-example"
    manager.save(sid, {"Sensor": {"DEVICE": "acurite_usb"}})
    settings.replace_setting("Astral", "ALTITUDE", 300)
    writes = []
    original = manager.save
    def save(*args):
        writes.append(args)
        original(*args)
    monkeypatch.setattr(manager, "save", save)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        current._calibrate_observation(sid, Observation("source", now, now, {"Temperature": 20}))
    assert len(writes) == 1
