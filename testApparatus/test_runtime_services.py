"""Verify service ownership across route registration and web startup.

Use isolated storage and real registration to catch duplicate database services,
callback omissions, and accidental dependencies on another app's route globals.
"""

import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiEmailNotifications import AutomationNotificationService, EmailNotificationService
from sensorius.saiRuntimeServices import RuntimeServices
from sensorius.saiWebServer import WebServerController
from testApparatus.test_ecowitt_routes import _FastStats, _Settings


def test_importing_routes_does_not_construct_storage():
    result = subprocess.run(
        [sys.executable, "-c", """
from sensorius.saiDataLogger import saiDataLogger

def unexpected_storage(*args, **kwargs):
    raise AssertionError('Importing routes opened storage')

saiDataLogger.__init__ = unexpected_storage
from sensorius import saiWebRoutes
assert saiWebRoutes.data_logger is None
assert saiWebRoutes.statter is None
"""],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_owned_services_survive_registration_and_prewarm(tmp_path, monkeypatch):
    from sensorius import saiWebRoutes as routes

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(routes, "FastStats", _FastStats)
    # Preserve compatibility aliases after this test registers multiple apps.
    for name in ("app", "data_logger", "statter", "sensor_map", "switch_controllers"):
        monkeypatch.setattr(routes, name, getattr(routes, name, None), raising=False)
    with saiDataLogger(str(tmp_path / "owned.db")) as logger:
        email = EmailNotificationService(settings=_Settings(), data_logger=logger)
        automation = AutomationNotificationService(data_logger=logger)
        actor = automation._get_controller()
        controller = SimpleNamespace()
        ingest = SimpleNamespace()
        services = RuntimeServices(
            data_logger=logger, sensor_map=[], switch_controllers={"hub": controller},
            mqtt_ingest=ingest, ecowitt_service=SimpleNamespace(),
            email_notifications=email, automation_notifications=automation,
        )
        assert services.statter.db_path == logger.db_path
        web = WebServerController(_Settings(), None, None, None, ingest, runtime_services=services)
        await web.initialize_server()
        assert web.app.state.runtime_services is services
        assert web.app.state.data_logger is logger
        assert web.app.state.statter is services.statter
        assert web.app.state.sensor_map is services.sensor_map
        assert web.app.state.switch_controllers is services.switch_controllers
        assert web.app.state.daily_summary_service.data_logger is logger
        broadcast = web.app.state.switch_broadcast
        for producer in (controller, ingest, services.ecowitt_service, email, actor):
            assert producer.dashboard_broadcast is broadcast

        # A later app can replace compatibility globals without redirecting prewarm.
        with saiDataLogger(str(tmp_path / "other.db")) as other_logger:
            other = RuntimeServices(data_logger=other_logger)
            other_web = WebServerController(_Settings(), None, None, None, None, runtime_services=other)
            await other_web.initialize_server()
            assert routes.data_logger is other_logger
            calls = []
            monkeypatch.setattr(logger, "get_available_sensors", lambda: ["sensor"])
            monkeypatch.setattr(logger, "get_latest_values", lambda sid: calls.append(("values", sid)))
            async def stats(sid):
                calls.append(("stats", sid))
            monkeypatch.setattr(services.statter, "get_24hr_stats", stats)
            await web._prewarm()
            assert calls == [("values", "sensor"), ("stats", "sensor")]
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_routes_keep_own_services_after_another_app_registers(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sensorius import saiWebRoutes as routes
    from sensorius import saiMQTTIngest as mqtt_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(routes, "FastStats", _FastStats)
    for name in ("app", "data_logger", "statter", "sensor_map", "switch_controllers"):
        monkeypatch.setattr(routes, name, getattr(routes, name, None), raising=False)
    monkeypatch.setattr(mqtt_module, "get_current_ingest",
                        lambda: pytest.fail("Route consulted the process-wide ingest singleton"))

    class Controller:
        switch_id = "desk-hub"
        location = "Desk"
        channel_id_for_label = {"Fan": "S1-desk"}

        def __init__(self):
            self.last_state = {"Fan": False}
            self.last_set_time = {"Fan": 0.0}
            self.calls = []

        def get_switch_names(self):
            return ["Fan"]

        def get_state(self, label):
            return self.last_state[label]

        def set_state(self, label, on, force=False):
            self.calls.append((label, on))
            self.last_state[label] = on
            return True

        def _switch_key(self, label):
            return f"S1-desk::{label}"

    with saiDataLogger(str(tmp_path / "first.db")) as first_logger, \
            saiDataLogger(str(tmp_path / "second.db")) as second_logger:
        hubs = []
        for logger, metric in [(first_logger, "Temperature"), (second_logger, "Humidity")]:
            sensor = SimpleNamespace(measurements=[{"name": metric}],
                                     sensor=SimpleNamespace(is_calibrated="Not Calibrated"))
            controller = Controller()
            ingest = SimpleNamespace(discovery_failures={"device": "timeout"}, device_status={},
                                     get_known_devices=lambda: [], get_known_switch_devices=lambda: [])
            services = RuntimeServices(data_logger=logger, sensor_map={"sensor": sensor},
                                       switch_controllers={"desk-hub": controller}, mqtt_ingest=ingest)
            app = FastAPI()
            services.attach(app)
            await routes.register_routes(app, _Settings(), None, None, ingest)
            hubs.append((app, services, metric))

        # App-state aliases are intentionally stale: runtime routes must consult
        # the owner, even when a controller map is replaced with an empty map.
        first_app, first, _ = hubs[0]
        other_app, second, _ = hubs[1]
        assert routes.sensor_map is second.sensor_map
        for app, services, metric in (hubs[0], hubs[1], hubs[0]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                   headers={"referer": "http://test/"}) as client:
                response = await client.get('/sensor-metrics', params={'sensor_id': 'sensor'})
                assert response.json() == {metric: None}
                assert (await client.get('/remove-device-list')).status_code == 200
        async with AsyncClient(transport=ASGITransport(app=first_app), base_url="http://test",
                               headers={"referer": "http://test/"}) as client:
            response = await client.post('/sensor-event', json={
                'event': 'calibration_progress', 'payload': {'sensor_id': 'sensor', 'sample_index': 1}})
            assert response.status_code == 200
            assert first.sensor_map['sensor'].sensor.is_calibrated == 'Calibrating'
            assert second.sensor_map['sensor'].sensor.is_calibrated == 'Not Calibrated'
            assert (await client.post('/retry-discovery', json={'host': 'device'})).status_code == 200
            assert first.mqtt_ingest.discovery_failures == {}
            assert second.mqtt_ingest.discovery_failures == {'device': 'timeout'}
            response = await client.post('/switch/toggle?switch_name=Fan&switch_id=desk-hub')
            assert response.status_code == 200, response.text
            assert first.switch_controllers['desk-hub'].calls == [('Fan', True)]
            assert second.switch_controllers['desk-hub'].calls == []
            first.sensor_map = {}
            assert (await client.get('/sensor-metrics', params={'sensor_id': 'sensor'})).json() == {}


@pytest.mark.asyncio
async def test_standalone_registration_does_not_adopt_previous_app_globals(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from sensorius import saiWebRoutes as routes

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(routes, 'FastStats', _FastStats)
    for name in ('app', 'data_logger', 'statter', 'sensor_map', 'switch_controllers'):
        monkeypatch.setattr(routes, name, getattr(routes, name, None), raising=False)
    with saiDataLogger(str(tmp_path / 'previous.db')) as previous:
        monkeypatch.setattr(routes, 'data_logger', previous)
        monkeypatch.setattr(routes, 'sensor_map', {'foreign': object()})
        monkeypatch.setattr(routes, 'switch_controllers', {'foreign': object()})
        app = FastAPI()
        await routes.register_routes(app, _Settings(), None, None, None)
        services = app.state.runtime_services
        try:
            assert services.data_logger is not previous
            assert services.sensor_map == {}
            assert services.switch_controllers == {}
            assert services.mqtt_ingest is None
        finally:
            services.data_logger.close()
