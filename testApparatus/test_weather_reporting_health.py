"""Verify reporting sessions without real USB, gateways, or MQTT.

Controlled clocks distinguish brief retries from confirmed outages; real SQLite
logging checks that only confirmed transitions enter the existing event history.
"""

from types import SimpleNamespace

import pytest

import sensorius.saiWeatherStation as station
from sensorius.saiRuntimeServices import RuntimeServices
from sensorius.saiWebDiagnostics import DeviceStatistics
from sensorius.weather_adapters.acurite import AcuRiteError
from test_weather_devices import service
from test_acurite_adapter import packet


@pytest.fixture
def clock(monkeypatch):
    current = SimpleNamespace(value=1000.0)
    fake = SimpleNamespace(monotonic=lambda: current.value, time=lambda: 1800000000 + current.value)
    monkeypatch.setattr(station, "time", fake)
    monkeypatch.setattr("sensorius.saiWebDiagnostics.time", fake)
    return current


@pytest.mark.asyncio
async def test_usb_retries_preserve_uptime_and_confirm_one_outage(service, clock, monkeypatch):
    current, _, logger, _ = service
    assert await current.poll_once()
    sid = current.status()["sensor_id"]
    stats = DeviceStatistics(RuntimeServices(data_logger=logger, statter=object()))
    first = await stats.sensor_payload(sid)
    assert first["uptime_since_last_offline_label"] == "0s"
    assert first["offline_events_24h"] == 0
    assert first["signal_strength_label"] == "3/3 (outdoor RF)"
    assert "24c0:0003" in first["usb_connection"]
    read = current.adapter.transport.read_reports

    def fail(*args):
        raise AcuRiteError("Transient USB failure")

    monkeypatch.setattr(current.adapter.transport, "read_reports", fail)
    clock.value += 18
    assert not await current.poll_once()
    assert current.status()["state"] == "online"
    assert current.adapter.read() is None
    retry = await stats.sensor_payload(sid)
    assert retry["uptime_since_last_offline_label"] == "18s"
    assert retry["offline_events_24h"] == 0
    assert retry["signal_strength_label"] == "3/3 (outdoor RF)"
    clock.value += 18
    await current.poll_once()
    assert current.status()["state"] == "online"
    assert current.status()["usb_read_failures"] == 2

    monkeypatch.setattr(current.adapter.transport, "read_reports", read)
    clock.value += 18
    await current.poll_once()
    assert current.status()["state"] == "online"
    assert (await stats.sensor_payload(sid))["uptime_since_last_offline_label"] == "54s"
    assert current.status()["usb_read_failures"] == 0

    monkeypatch.setattr(current.adapter.transport, "read_reports", fail)
    for _ in range(3):
        clock.value += 18
        await current.poll_once()
    assert current.status()["state"] == "degraded"
    clock.value += 60
    await current.poll_once()
    assert current.status()["state"] == "offline"
    offline = await stats.sensor_payload(sid)
    assert offline["offline_events_24h"] == 1
    assert offline["uptime_since_last_offline_label"] == "Offline"
    assert offline["uptime_started_epoch"] is None
    clock.value += 18
    await current.poll_once()
    assert logger.get_sensor_offline_event_count(sid) == 1

    monkeypatch.setattr(current.adapter.transport, "read_reports", read)
    clock.value += 18
    await current.poll_once()
    assert current.status()["state"] == "online"
    recovered = await stats.sensor_payload(sid)
    assert recovered["uptime_since_last_offline_label"] == "0s"
    assert recovered["offline_events_24h"] == 1


@pytest.mark.asyncio
async def test_missing_data_disable_and_identity_do_not_invent_uptime(clock):
    events = []
    health = station.StationReportingHealth(SimpleNamespace(
        log_sensor_event=lambda *args, **kwargs: events.append((args, kwargs))))
    raw = {"enabled": True, "station": {"reporting": False}}
    await health.update("acurite-a", raw)
    assert health.decorate(raw)["uptime_started_epoch"] is None
    assert not events
    good = {"enabled": True, "station": {"reporting": True}}
    await health.update("acurite-a", good)
    assert health.decorate(good)["uptime_started_epoch"] is not None
    disabled = {"enabled": False, "station": {"reporting": False}}
    await health.update("acurite-a", disabled)
    assert health.decorate(disabled)["uptime_empty_label"] == "Disabled"
    await health.update("acurite-b", raw)
    assert health.decorate(raw)["uptime_started_epoch"] is None
    assert [item[1]["state"] for item in events] == ["online", "offline"]


@pytest.mark.asyncio
async def test_gateway_sessions_reset_on_failure_without_usb_retry_grace(clock):
    health = station.StationReportingHealth(object(), retry_seconds=0)
    good = {"enabled": True, "station": {"reporting": True}}
    bad = {"enabled": True, "station": {"reporting": False}}
    await health.update("ecowitt-a", good)
    started = health.decorate(good)["uptime_started_epoch"]
    clock.value += 60
    assert health.decorate(good)["uptime_started_epoch"] == started
    await health.update("ecowitt-a", bad)
    assert health.decorate(bad)["uptime_started_epoch"] is None
    clock.value += 60
    await health.update("ecowitt-a", good)
    assert health.decorate(good)["uptime_started_epoch"] == started + 120


@pytest.mark.asyncio
async def test_signal_zero_is_available_but_stale_and_missing_signal_are_not(service, clock, monkeypatch):
    current, _, logger, _ = service
    await current.poll_once()
    sid = current.status()["sensor_id"]
    current.adapter.transport.reports = {1: packet("rf_lost")}
    await current.poll_once()
    assert station.station_health(logger, sid)["signal_strength_label"] == "0/3 (outdoor RF)"
    current.adapter._signal_at -= 121
    assert station.station_health(logger, sid)["signal_strength_label"] == "Unavailable"


@pytest.mark.asyncio
async def test_real_settings_and_dashboard_routes_use_usb_health(service, clock, tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path
    from testApparatus.test_nodus_settings_schema_writes import _build_app, _REAL_SENSOR_SETTINGS_MANAGER

    current, _, logger, manager = service
    await current.poll_once()
    sid = current.status()["sensor_id"]
    web_root = tmp_path / "web"
    web_root.mkdir()
    app, ingest, _, sensor_root, _ = await _build_app(web_root, monkeypatch, data_logger=logger)
    ingest.get_measure_status = lambda sid: "unknown"
    _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)).save(sid, manager.load(sid))
    app.state.templates = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "ui_templates")),
        autoescape=select_autoescape(),
    )
    app.state.runtime_services.acurite_service = current
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        for deferred in (0, 1):
            response = await client.get(f"/edit-sensor?sensor_id={sid}&embed=1&defer_details={deferred}")
            assert response.status_code == 200, response.text
            assert "USB Connection:" in response.text
            assert "IP Address:" not in response.text
            assert "Reporting uptime:" in response.text
            assert 'data-stat-value="battery-status"' in response.text
        for prefix, device in (("ecowitt", "ecowitt"), ("davis", "davis_vue"), ("oregon", "wmr100_usb")):
            weather_id = f"{prefix}-test"
            doc = manager.load(sid)
            doc["Sensor"]["DEVICE"] = device
            _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)).save(weather_id, doc)
            for deferred in (0, 1):
                response = await client.get(f"/edit-sensor?sensor_id={weather_id}&embed=1&defer_details={deferred}")
                assert response.status_code == 200, response.text
                assert 'data-stat-value="battery-status"' in response.text
                assert response.text.index('data-stat-value="battery-status"') < response.text.index('data-stat-signal-row')
        response = await client.get(f"/sensor-settings/statistics?sensor_id={sid}")
        assert response.status_code == 200, response.text
        stats = response.json()
        assert stats["has_reporting_uptime"] is True
        assert stats["usb_status"] == "Connected"
        assert stats["signal_strength_label"] == "3/3 (outdoor RF)"
        monkeypatch.setattr(current, "status", lambda: {"sensor_id": sid, "state": "degraded"})
        dashboard = (await client.get("/?json_only=true")).json()
        assert dashboard["statuses"][sid] == "degraded"
