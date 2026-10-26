"""Check that service and callback contracts reject incompatible dependencies.

Mypy is an optional developer tool. These examples check actual implementation
compatibility as well as async and keyword-only mistakes without importing or
starting any runtime services in the checked program.
"""

import pytest


def test_web_dependency_contracts_accept_real_services_and_reject_wrong_callbacks(tmp_path):
    mypy = pytest.importorskip("mypy.api")
    sample = tmp_path / "web_dependencies.py"
    valid = '''
import asyncio
from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiMQTTIngest import saiMQTTIngest
from sensorius.saiSettings import saiSettings
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
from sensorius.saiRuntimeServices import RuntimeServices
from sensorius.saiWebRouteState import RouteState
from sensorius.saiWebSwitches import register_switch_routes, SwitchRouteDependencies
from sensorius.saiNodusDeviceCommands import NodusCommandTransport, NodusDeviceCommands
from sensorius.saiWebContracts import (
    DashboardBroadcast, HostLock, ProtectedAccess, RestartDevice, PushSettingsBatch,
    SettingsFactory, SecretSettingsFactory, SensorSettingsFactory, SwitchSettingsFactory,
)
from fastapi import APIRouter, Request

async def broadcast(payload: dict) -> None:
    pass

def host_lock(host: str) -> asyncio.Lock:
    return asyncio.Lock()

def check_access(request: Request, *, require_csrf: bool = False) -> None:
    pass

def wire(logger: saiDataLogger, ingest: saiMQTTIngest, commands: NodusDeviceCommands) -> None:
    transport: NodusCommandTransport = ingest
    services = RuntimeServices(data_logger=logger, mqtt_ingest=ingest)
    services.bind_dashboard_broadcast(broadcast)
    restart: RestartDevice = commands.request_restart
    batch: PushSettingsBatch = commands.push_settings_batch
    access: ProtectedAccess = check_access
    lock: HostLock = host_lock
    settings: SettingsFactory = saiSettings
    secret_settings: SecretSettingsFactory = saiSettings
    sensors: SensorSettingsFactory = SensorSettingsManager
    switches: SwitchSettingsFactory = SwitchSettingsManager

def wire_routes(router: APIRouter, services: RuntimeServices, state: RouteState,
                dependencies: SwitchRouteDependencies) -> None:
    register_switch_routes(
        router, services=services, route_state=state, settings=saiSettings(),
        dependencies=dependencies, settings_factory=saiSettings,
        switch_settings_factory=SwitchSettingsManager,
        get_system_settings_manager=lambda: None, status_cache_ttl=0.5,
    )
'''
    options = ["--explicit-package-bases", "--follow-imports=silent",
               "--cache-dir", str(tmp_path / "mypy-cache"), str(sample)]
    sample.write_text(valid)
    stdout, stderr, status = mypy.run(options)
    assert status == 0, stdout + stderr

    invalid = '''
def sync_broadcast(payload: dict) -> None:
    pass

async def async_lock(host: str) -> asyncio.Lock:
    return asyncio.Lock()

def access_without_csrf(request: Request) -> None:
    pass

def future_broadcast(payload: dict) -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()

bad_broadcast: DashboardBroadcast = sync_broadcast
bad_future: DashboardBroadcast = future_broadcast
bad_lock: HostLock = async_lock
bad_access: ProtectedAccess = access_without_csrf
bad_settings: SettingsFactory = object
bad_services = RuntimeServices(data_logger=object(), mqtt_ingest=object())
'''
    sample.write_text(valid + invalid)
    stdout, stderr, status = mypy.run(options)
    assert status == 1, stdout + stderr
    invalid_lines = (valid + invalid).splitlines()
    for expression in ("bad_broadcast:", "bad_future:", "bad_lock:", "bad_access:", "bad_settings:", "bad_services ="):
        line = next(index for index, text in enumerate(invalid_lines, start=1) if expression in text)
        assert f"{sample}:{line}: error:" in stdout, stdout + stderr
