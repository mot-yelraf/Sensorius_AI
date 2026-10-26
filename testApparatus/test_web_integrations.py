"""Exercise integration routes with isolated runtime owners.

HTTP checks cover live reconfiguration, lazy service wiring, settings persistence,
and the failure contracts used by the existing integration settings UI.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from sensorius.saiRuntimeServices import RuntimeServices
from sensorius.saiSettings import saiSettings
from sensorius.saiWebIntegrations import register_integration_routes
from sensorius.saiWebRoutes import _weewx_measure_status_from_latest


def build_app():
    stored = {}

    class Settings:
        obfuscate_secret = staticmethod(saiSettings.obfuscate_secret)

        def __init__(self, **kwargs):
            pass

        def get_setting(self, section, key, default=None):
            return stored.get(section, {}).get(key, default)

        def replace_setting(self, section, key, value):
            stored.setdefault(section, {})[key] = value

    logger = SimpleNamespace(get_latest_timestamp=lambda sid: None,
                             get_latest_values=lambda sid: {})
    services = RuntimeServices(data_logger=logger, statter=object())
    app = FastAPI()
    services.attach(app)
    created = []
    materialized = []
    invalidations = []
    broadcast = AsyncMock()

    def ecowitt_factory(**kwargs):
        service = SimpleNamespace(
            **kwargs, status=lambda: {"state": "disabled"},
            discover=AsyncMock(return_value={"gateway_url": "http://gateway.local"}),
            save_configuration=lambda *args: None, activate_smart_plugs=AsyncMock(),
            poll_interval_sec=120, smart_plug_interval_sec=30, disable=lambda: None,
        )
        created.append(service)
        return service

    router = APIRouter()
    register_integration_routes(
        router, services=services, settings_factory=Settings,
        ecowitt_factory=ecowitt_factory, ensure_weewx_settings=materialized.append,
        evaluate_weewx_status=_weewx_measure_status_from_latest,
        dashboard_broadcast=broadcast,
        invalidate_dashboard_caches=lambda: invalidations.append(True),
    )
    app.include_router(router)
    return SimpleNamespace(app=app, services=services, stored=stored, created=created,
                           materialized=materialized, invalidations=invalidations,
                           broadcast=broadcast)


def client_for(hub):
    return AsyncClient(transport=ASGITransport(app=hub.app), base_url="http://test")


@pytest.mark.asyncio
async def test_settings_normalization_and_secret_storage():
    hub = build_app()
    async with client_for(hub) as client:
        response = await client.post('/submit-homeassistant-settings', json={
            'enabled': True, 'broker': ' broker.local ', 'port': 99999,
            'username': ' user ', 'password': 'test-secret', 'use_tls': True,
        })
        assert response.json() == {'status': 'ok'}
        ha = hub.stored['HomeAssistant']
        assert ha['HA_MQTTPORT'] == 1883
        assert ha['HA_BROKER'] == 'broker.local'
        assert ha['HA_USERNAME'] == 'user'
        assert ha['HA_PASSWORD'] == saiSettings.obfuscate_secret('test-secret')
        assert ha['ENABLED'] and ha['USE_TLS']
        response = await client.post('/submit-farmos-settings', json={
            'enabled': True, 'base_url': ' https://farm.example/ ',
            'access_token': 'test-token', 'client_secret': 'test-secret',
            'password': 'test-password', 'log_bundle': ' OBSERVATION ',
        })
        assert response.json() == {'status': 'ok'}
        farm = hub.stored['FarmOS']
        assert farm['BASE_URL'] == 'https://farm.example'
        assert farm['LOG_BUNDLE'] == 'observation'
        for key, raw in [('ACCESS_TOKEN', 'test-token'), ('CLIENT_SECRET', 'test-secret'),
                         ('PASSWORD', 'test-password')]:
            assert farm[key] == saiSettings.obfuscate_secret(raw)


@pytest.mark.asyncio
async def test_weewx_live_configuration_stays_with_owning_app():
    first, second = build_app(), build_app()
    calls = []
    first.services.mqtt_ingest = SimpleNamespace(
        configure_weewx_mqtt=lambda **kwargs: calls.append(kwargs) or True)
    payload = {'mqtt_enabled': True, 'mqtt_topic': 'weather/+', 'sensor_id': 'station',
               'db_path': '/tmp/weather.sdb', 'update_period_sec': 1}
    for hub, restart in [(first, False), (second, True), (first, False)]:
        async with client_for(hub) as client:
            response = await client.post('/submit-weewx-settings', json=payload)
            assert response.json() == {'status': 'ok', 'restart_required': restart}
            assert hub.materialized[-1] == 'station'
            assert hub.stored['WeeWX']['UPDATE_PERIOD_SEC'] == 15
            assert hub.stored['WeeWX']['ENABLED'] is False
            status = (await client.get('/weewx/status')).json()
            assert status['state'] == 'unknown'
            assert status['sensor_id'] == 'station'
            assert status['offline_after_sec'] == 45
    assert len(calls) == 2
    assert calls[0] == dict(enabled=True, topic_filter='weather/+', sensor_id='station', update_period_sec=15)


@pytest.mark.asyncio
async def test_weewx_validation_does_not_write_settings():
    hub = build_app()
    async with client_for(hub) as client:
        for invalid in ({'db_path': 'relative.sdb'}, {'sensor_id': '../station'}, {'mqtt_topic': ' '}):
            response = await client.post('/submit-weewx-settings', json=invalid)
            assert response.status_code == 400
            assert not hub.stored
            assert not hub.materialized
        status = (await client.get('/weewx/status')).json()
        assert status['state'] == 'disabled'


@pytest.mark.asyncio
async def test_lazy_ecowitt_service_shares_context_controllers_and_notifications():
    first, second = build_app(), build_app()
    for hub in (first, second, first):
        async with client_for(hub) as client:
            assert (await client.get('/ecowitt/status')).json() == {'state': 'disabled'}
            service = hub.services.ecowitt_service
            assert service is hub.app.state.ecowitt_service
            assert service.data_logger is hub.services.data_logger
            assert service.switch_controllers is hub.services.switch_controllers
            assert service.dashboard_broadcast is hub.broadcast
            assert len(hub.created) == 1
            response = await client.post('/ecowitt/save', json={'gateway_url': 'http://gateway.local'})
            assert response.status_code == 200
            service.activate_smart_plugs.assert_awaited()
            assert hub.invalidations
            assert (await client.post('/ecowitt/disable')).json() == {'ok': True}
    assert first.services.ecowitt_service is not second.services.ecowitt_service


@pytest.mark.asyncio
async def test_farmos_runtime_bridge_and_failure_contracts():
    hub = build_app()
    async with client_for(hub) as client:
        assert (await client.post('/farmos/test')).status_code == 503
        assert (await client.get('/farmos/status')).json()['queue_depth'] == 0
        bridge = SimpleNamespace(status_snapshot=lambda: {'queue_depth': 7},
                                 test_connection=AsyncMock(return_value={'ok': True}))
        hub.services.farmos_bridge = bridge
        assert (await client.get('/farmos/status')).json() == {'queue_depth': 7}
        assert (await client.post('/farmos/test')).status_code == 200
        bridge.test_connection.return_value = {'ok': False}
        assert (await client.post('/farmos/test')).status_code == 502
        bridge.test_connection.side_effect = RuntimeError('connection failed')
        response = await client.post('/farmos/test')
        assert response.status_code == 500
        assert response.json() == {'ok': False, 'error': 'connection failed'}
