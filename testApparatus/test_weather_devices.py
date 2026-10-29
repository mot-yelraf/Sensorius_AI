"""Exercise weather device setup, provenance and shared persistence without hardware."""

import asyncio
from contextlib import closing
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from sensorius.saiAcuRite import AcuRiteUsbIngest
from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.saiWebWeatherDevices import register_weather_device_routes
from sensorius.weather_adapters.observations import Observation
from sensorius.sensor_modules.station_ecowitt import gateway_family, normalize_ecowitt_livedata
from test_acurite_adapter import FakeTransport, packet


class Settings:
    def __init__(self):
        self.values = {}
        self.saves = 0

    def get_setting(self, section, key, default=None, **kwargs):
        return self.values.get((section, key), default)

    def replace_setting(self, section, key, value):
        self.values[section, key] = value

    def set_many_in_memory(self, updates):
        for section, key, value in updates:
            self.values[section, key] = value

    def save_settings(self):
        self.saves += 1


def test_observation_preserves_zero_and_rejects_nonfinite_and_naive_times():
    now = datetime.now(timezone.utc)
    observation = Observation("source", now, now, {"Temperature": 0, "Wind Speed": False, "Rain": float("nan"), "UV": None})
    assert observation.measurements == {"Temperature": 0.0}
    with pytest.raises(ValueError, match="timezone"):
        Observation("source", now.replace(tzinfo=None), now, {})


@pytest.fixture
def service(tmp_path):
    logger = saiDataLogger(str(tmp_path / "weather.db"))
    settings = Settings()
    manager = SensorSettingsManager(str(tmp_path / "sensors"))
    service = AcuRiteUsbIngest(settings=settings, data_logger=logger, transport=FakeTransport(), sensor_manager=manager)
    service.configure({"model": "06006", "poll_interval_sec": 60})
    yield service, settings, logger, manager
    logger.close()


@pytest.mark.asyncio
async def test_acurite_uses_existing_settings_logger_and_preserves_independent_devices(service, monkeypatch):
    service, settings, logger, manager = service
    samples = []
    logger.add_readings_listener(lambda *args: samples.append(args))
    assert await service.poll_once() is True
    sid = service.status()["sensor_id"]
    assert sid.startswith("acurite-")
    assert logger.get_latest_values(sid)["Temperature"] == 20
    assert logger.get_latest_values(sid)["Console Absolute Baro-Pressure"] == pytest.approx(1013.25, abs=.1)
    assert logger.get_latest_values(sid).get("Baro-Pressure") is None
    assert samples[0][0] == sid
    assert manager.get_setting(sid, "Sensor.DEVICE") == "acurite_usb"
    assert manager.get_display_metrics(sid)[0] == "Temperature_F"
    manager.update_setting(sid, "Sensor.LOCATION", "Orchard")
    service.disable()
    assert await service.poll_once() is False
    assert service.status()["state"] == "disabled"
    assert logger.get_latest_values(sid)["Temperature"] == 20
    service.configure({"model": "01536"})
    assert await service.poll_once() is True
    assert service.status()["sensor_id"] == sid
    assert manager.get_setting(sid, "Sensor.LOCATION") == "Orchard"
    assert settings.get_setting("Ecowitt", "ENABLED", False) is False


@pytest.mark.asyncio
async def test_rain_baseline_is_source_and_sample_specific_and_commits_after_write(service, monkeypatch):
    service, settings, logger, manager = service
    from sensorius.weather_adapters.observations import RainPolicy
    now = datetime.now(timezone.utc)
    current = [0]
    source = ["acurite:console:A"]
    counter = [81.9]
    async def read():
        service.adapter._identity = "console"
        stamp = now + timedelta(seconds=current[0] * 60)
        return Observation(source[0], stamp, stamp, {"Wind Speed": 0},
                           RainPolicy(kind="rolling", modulus=81.92), counter[0], stamp)
    monkeypatch.setattr(service, "read", read)
    monkeypatch.setattr(service.adapter, "status", lambda: {"station": {"reporting": True}})
    assert await service.poll_once()
    sid = service._sensor_id
    assert logger.get_latest_values(sid).get("Rain") is None
    current[0] += 1
    counter[0] = .02
    service._last_write = float("-inf")
    assert await service.poll_once()
    assert logger.get_latest_values(sid)["Rain"] == pytest.approx(.04)
    baseline = service._baseline
    current[0] += 1
    counter[0] = .05
    service._last_write = float("-inf")
    original = logger.log_readings
    monkeypatch.setattr(logger, "log_readings", lambda *args: False)
    with pytest.raises(RuntimeError, match="saved"):
        await service.poll_once()
    assert service._baseline == baseline
    monkeypatch.setattr(logger, "log_readings", original)
    assert await service.poll_once()
    assert logger.get_latest_values(sid)["Rain"] == pytest.approx(.03)
    current[0] += 1
    source[0] = "acurite:console:B"
    service._last_write = float("-inf")
    assert await service.poll_once()
    assert service._baseline[0] == source[0]


def test_acurite_routes_validate_and_enforce_protected_access(service):
    current, settings, logger, manager = service
    app = FastAPI()
    services = SimpleNamespace(acurite_service=current)
    def protect(request, **kwargs):
        if request.headers.get("x-test-key") != "authorized":
            raise HTTPException(401)
    register_weather_device_routes(app, services=services, settings_factory=lambda: settings,
                                   require_protected_access=protect, invalidate_dashboard_caches=lambda: None)
    with TestClient(app) as client:
        assert client.post("/acurite/save", json={}).status_code == 401
        headers = {"x-test-key": "authorized"}
        for payload in ({"model": "unknown"}, {"poll_interval_sec": 0}, {"poll_interval_sec": True}, [], {"usb_serial": "bad\nserial"}):
            previous = dict(settings.values)
            assert client.post("/acurite/save", headers=headers, json=payload).status_code == 400
            assert settings.values == previous
        assert client.post("/acurite/save", headers=headers, json={"model": "02033", "poll_interval_sec": 300}).status_code == 200
        assert settings.get_setting("AcuRite", "MODEL") == "02032"
        assert client.get("/acurite/status").json()["model"] == "02032"
        assert client.post("/acurite/disable", headers=headers).json()["enabled"] is False


GATEWAYS = json.loads((Path(__file__).parent / "fixtures/ecowitt-http-gateways.json").read_text())["gateways"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", GATEWAYS, ids=lambda f: f["family"])
async def test_additional_ecowitt_gateways_discover_normalize_and_persist(fixture, tmp_path, monkeypatch):
    import sensorius.saiEcowitt as module
    from test_ecowitt_ingest import _Client, _Settings
    settings = _Settings()
    client = _Client
    monkeypatch.setattr(client, "responses", {
        "get_version": fixture["version"], "get_network_info": fixture["network"],
        ("get_sensors_info", 1): fixture["inventory"][0],
        ("get_sensors_info", 2): fixture["inventory"][1],
        "get_livedata_info": fixture["live"], "get_rain_totals": fixture["rain_totals"],
    })
    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    monkeypatch.setattr(module, "ensure_ecowitt_sensor_settings", lambda *a, **k: None)
    logger = saiDataLogger(str(tmp_path / "ecowitt.db"))
    try:
        service = module.EcowittGatewayIngest(settings=settings, data_logger=logger)
        discovery = await service.discover("http://" + fixture["network"]["ethIP"])
        assert discovery["gateway_family"] == fixture["family"]
        assert "not-public" not in json.dumps(discovery)
        service.save_configuration(discovery, 60)
        assert await service.poll_once()
        values = logger.get_latest_values(service.sensor_id)
        assert values["Temperature"] == 20
        assert values["Wind Speed"] == 0
        assert values["Gateway Rel-Humidity"] in {40, 45}
        assert service.status()["gateway_family"] == fixture["family"]
        assert service.status()["observation_fresh"] is True
        if fixture["family"] == "GW3000":
            assert values["Rain Day"] == .1
            assert values["Light Intensity"] == 35000
            assert values["Baro-Pressure"] == pytest.approx(1013.3)
        else:
            assert values["Rain Day"] == 1
    finally:
        logger.close()


@pytest.mark.parametrize("version,expected", [("GW3000C_V1", "GW3000"), ("Version: GW2000B_V2", "GW2000"), ("GW30000_V1", ""), ("GW1000_V1", "")])
def test_gateway_family_matches_complete_model(version, expected):
    assert gateway_family(version) == expected


@pytest.mark.asyncio
async def test_unavailable_fields_clear_live_snapshot_while_retaining_history(service, monkeypatch):
    current, settings, logger, manager = service
    assert await current.poll_once()
    sid = current._sensor_id
    assert logger.get_latest_values(sid)["Temperature"] == 20
    async def partial():
        now = datetime.now(timezone.utc)
        return Observation("acurite:console:A", now, now, {"Wind Speed": 0})
    monkeypatch.setattr(current, "read", partial)
    current._last_write = float("-inf")
    assert await current.poll_once()
    assert logger.get_latest_values(sid)["Temperature"] is None
    assert logger.get_latest_values(sid)["Ambient VPD"] is None
    assert logger.get_latest_values(sid)["Wind Speed"] == 0
    with closing(logger._open_conn()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM readings WHERE sensor_id = ? AND metric = 'Temperature' AND value = 20", (sid,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_ecowitt_setup_change_during_acquisition_cannot_mix_device_identity(monkeypatch):
    from sensorius.saiEcowitt import EcowittGatewayIngest
    settings = Settings()
    settings.replace_setting("Ecowitt", "SENSOR_ID", "ecowitt-original")
    settings.replace_setting("Ecowitt", "GATEWAY_URL", "http://192.0.2.1")
    logger = SimpleNamespace()
    current = EcowittGatewayIngest(settings=settings, data_logger=logger)
    async def read():
        settings.replace_setting("Ecowitt", "SENSOR_ID", "ecowitt-new")
        now = datetime.now(timezone.utc)
        return Observation("ecowitt:ecowitt-original:http://192.0.2.1", now, now, {"Temperature": 20})
    monkeypatch.setattr(current, "read", read)
    assert await current.poll_once() is False


@pytest.mark.asyncio
async def test_supervised_acurite_failure_feeds_watchdog_and_remains_cancellable(service, monkeypatch):
    current, settings, logger, manager = service
    fed = []
    current.supervisor = SimpleNamespace(feedthedogs=lambda name: fed.append(name))
    async def fail():
        raise RuntimeError("Temporary transport failure")
    async def cancelled(delay):
        raise asyncio.CancelledError
    monkeypatch.setattr(current, "poll_once", fail)
    monkeypatch.setattr(asyncio, "sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await current.run()
    assert fed == ["AcuRite USB Ingest"]
    assert current.status()["last_error"] == "Temporary transport failure"


@pytest.mark.parametrize("metric,unit,device_class", [
    ("Console Temperature", "°C", "temperature"),
    ("Console Temperature_F", "°F", "temperature"),
    ("Console Absolute Baro-Pressure", "hPa", "atmospheric_pressure"),
])
def test_console_metrics_keep_units_in_homeassistant_discovery(metric, unit, device_class):
    from sensorius.saiHomeAssistantMqtt import metric_meta_for_metric
    metadata = metric_meta_for_metric(metric)
    assert metadata["unit"] == unit
    assert metadata["device_class"] == device_class


def test_dashboard_uses_acurite_health_instead_of_treating_cached_values_as_live():
    from sensorius.saiHtml import render_dashboard, get_gauge_config
    sid = "acurite-test"
    html = "".join(render_dashboard(
        "All", None, [sid], {sid: {"Temperature": 20}}, {},
        SimpleNamespace(expected_gauge_map={}),
        gauge_config=get_gauge_config(), expected_gauge_map={sid: ["Temperature"]},
        sensor_statuses={sid: "offline"}, battery_statuses={sid: "UNKNOWN"},
    ))
    assert "title='Connection status: offline'" in html
    assert f"id='{sid}_battery'" in html
