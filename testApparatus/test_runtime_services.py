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
