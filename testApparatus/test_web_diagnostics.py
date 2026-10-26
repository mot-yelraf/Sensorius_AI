"""Verify extracted statistics and diagnostics through their HTTP contracts.

Controlled app contexts detect service leakage and duplicate stats construction.
Real route registration also exercises the existing API-key and same-origin gates.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRouter
from httpx import ASGITransport, AsyncClient
import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiRuntimeServices import RuntimeServices
from sensorius.saiStats import create_stats_router
from sensorius.saiWebDiagnostics import (
    AdvancedStatusConfiguration, DeviceStatistics, register_statistics_diagnostics_routes,
)


class Logger:
    db_path = ":memory:"

    def __init__(self, count):
        self.count = count
        self.aliases = []

    def get_available_sensors(self):
        return ["sensor"]

    def get_sensor_offline_event_count(self, sid, *, aliases):
        self.aliases.append((sid, aliases))
        return self.count

    def get_sensor_packet_count(self, sid, *, aliases):
        return self.count * 10

    def get_switch_last_event_for_channel(self, channel):
        return {"state": "on", "ts_epoch": 1000}


def build_app(count=1, *, net_mgr=None, ingest=None, environment=None):
    logger = Logger(count)
    statter = SimpleNamespace(get_24hr_stats=lambda sid: {"Temperature": {"avg": count}})
    controller = SimpleNamespace(switch_id=f"hub-{count}", location="Room", is_present=True,
                                 get_switch_names=lambda: ["Fan"])
    services = RuntimeServices(data_logger=logger, statter=statter,
                               switch_controllers={"hub": controller}, mqtt_ingest=ingest)
    app = FastAPI()
    services.attach(app)
    app.state.ui_runtime_instance_id = f"runtime-{count}"
    router = APIRouter()
    config = AdvancedStatusConfiguration(
        read_environment=environment or (lambda: {}), autostart_is_enabled=lambda scope: False,
        debug_module_choices=("saiWebRoutes",), default_log_file="sensorius.log",
    )
    manager = SimpleNamespace(load=lambda sid: {"Switch": {
        "TYPE": "pico2w", "SWITCH_1_EN": 1, "SWITCH_1_CHANNEL_ID": "S1-hub", "SWITCH_1_LABEL": "Fan",
    }})
    register_statistics_diagnostics_routes(
        router, services=services, statistics=DeviceStatistics(services), net_mgr=net_mgr,
        require_protected_access=lambda request, **kwargs: None, advanced=config,
        switch_settings_manager=lambda root: manager,
    )
    app.include_router(router)
    app.include_router(create_stats_router(SimpleNamespace(get_all_sensor_ids=lambda: ["sensor"]),
                                         None, data_logger=logger, statter=statter))
    return app, services


@pytest.mark.asyncio
async def test_apps_keep_their_statistics_and_controller_owners(monkeypatch):
    # A supplied runtime stats object must prevent construction of another engine.
    monkeypatch.setattr("sensorius.saiStats.saiStats", lambda **kwargs: pytest.fail("Created duplicate stats engine"))
    app_a, services_a = build_app(1)
    app_b, _ = build_app(2)
    for app, count in ((app_a, 1), (app_b, 2), (app_a, 1)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/sensor-settings/statistics", params={"sensor_id": "sensor.local"})
            assert response.status_code == 200
            assert response.json()["data_packets_received"] == count * 10
            assert response.json()["offline_events_24h"] == count
            assert (await client.get("/stats")).json() == {"Temperature": {"avg": count}}
            controllers = (await client.get("/debug/switch-controllers")).json()
            assert controllers["items"][0]["switch_id"] == f"hub-{count}"
            status = (await client.get("/advanced/status")).json()
            assert status["runtime_instance_id"] == f"runtime-{count}"
            assert status["log_file_path"] == str(Path("sensorius.log").resolve())
    services_a.switch_controllers.clear()
    async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        assert (await client.get("/debug/switch-controllers")).json() == {"count": 0, "items": []}
        payload = (await client.get("/switch-settings/statistics", params={"switch_id": "hub"})).json()
        assert payload["switch_channel_state_stats"][0]["state"] == "On"
        assert payload["switch_channel_state_stats"][0]["display_name"] == "S1"
        assert (await client.get("/switch-settings/statistics", params={"switch_id": " "})).status_code == 400
        assert (await client.get("/sensor-settings/statistics")).status_code == 422


@pytest.mark.asyncio
async def test_blocking_diagnostic_probes_run_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    threads = []

    def off_thread(value):
        threads.append(threading.get_ident())
        return value

    net = SimpleNamespace(is_connected=lambda: off_thread(False),
                          get_current_ssid=lambda: off_thread("Nodus_Setup"))
    monkeypatch.setattr("sensorius.saiWebDiagnostics.subprocess.run",
                        lambda *args, **kwargs: off_thread(SimpleNamespace(stdout="192.168.4.1 10.0.0.2")))
    app, _ = build_app(net_mgr=net, environment=lambda: off_thread({
        "SENSORIUS_LOG_LEVEL": "invalid", "SENSORIUS_FILE_LOG": "yes",
        "SENSORIUS_DB_RETENTION_DAYS": "9999", "SENSORIUS_AUTOSTART_SCOPE": "invalid",
    }))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/network-status")).json() == {
            "connected": False, "ssid": "Nodus_Setup", "ip": "192.168.4.1", "mode": "AP",
        }
        status = (await client.get("/advanced/status")).json()
        assert status["log_level"] == "DEBUG" and status["file_log"] is True
        assert status["db_retention_days"] == 365 and status["autostart_scope"] == "user"
        assert (await client.get("/hayd")).json() == {"STATUS": "ok"}
        assert (await client.get("/keep-alive")).json() == {"ready": True}
        assert (await client.get("/debug")).text == "<h2>Debug route loaded</h2>"
    assert threads and all(thread != loop_thread for thread in threads)


@pytest.mark.asyncio
async def test_retained_scan_validation_and_failure_contracts():
    calls = []

    def scan(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "retained_commands": []}

    app, services = build_app(ingest=SimpleNamespace(scan_retained_command_topics=scan))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/debug/mqtt-retained-commands?timeout=0.1")).status_code == 422
        assert not calls
        assert (await client.get("/debug/mqtt-retained-commands?timeout=0.3&limit=2")).json()["ok"]
        assert calls == [{"timeout": 0.3, "limit": 2}]
        services.mqtt_ingest = None
        response = await client.get("/debug/mqtt-retained-commands")
        assert response.status_code == 503 and response.json()["error"] == "mqtt_ingest_unavailable"
        services.mqtt_ingest = SimpleNamespace(scan_retained_command_topics=lambda **kwargs: {"ok": False})
        assert (await client.get("/debug/mqtt-retained-commands")).status_code == 503
        services.mqtt_ingest = SimpleNamespace(scan_retained_command_topics=lambda **kwargs: 1 / 0)
        response = await client.get("/debug/mqtt-retained-commands")
        assert response.status_code == 500 and response.json() == {"ok": False, "error": "internal_error"}
        response = await client.get("/network-status")
        assert response.status_code == 500 and response.json() == {"error": "internal_error"}


@pytest.mark.asyncio
async def test_real_registration_preserves_api_key_and_same_origin_access(tmp_path, monkeypatch):
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAI_WEB_API_KEY", "fixture-key")
    with saiDataLogger(str(tmp_path / "diagnostics.db")) as logger:
        app, *_ = await _build_app(tmp_path, monkeypatch, data_logger=logger)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/debug/switch-controllers")).status_code == 401
            assert (await client.get("/debug/switch-controllers", headers={"x-api-key": "fixture-key"})).status_code == 200
            url = "/sensor-settings/statistics?sensor_id=sensor"
            assert (await client.get(url)).status_code == 401
            assert (await client.get(url, headers={"sec-fetch-site": "same-origin"})).status_code == 200
            assert (await client.get("/debug/switch-controllers", headers={"sec-fetch-site": "same-origin"})).status_code == 401
            response = await client.get("/debug/automation-state?switch_id=hub&label=Fan", headers={"x-api-key": "fixture-key"})
            assert response.status_code == 200 and response.json()["switch_key"] == "hub::Fan"
