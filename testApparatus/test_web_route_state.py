"""Verify route-cache ownership through independently registered applications.

Warm caches must survive registration or invalidation of another app. Calibration
progress and switch status are exercised over HTTP to detect shared mutable state.
"""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from sensorius import saiWebRoutes as routes
from testApparatus.test_nodus_settings_schema_writes import _build_app


@pytest.mark.asyncio
async def test_cache_reads_and_invalidation_are_scoped_to_the_request_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first_root, second_root = tmp_path / 'first', tmp_path / 'second'
    first_root.mkdir()
    second_root.mkdir()
    first, *_ = await _build_app(first_root, monkeypatch)
    a = first.state.route_state
    a.sensor_ids_payload = ['sensor-a']
    a.sensor_ids_until = float('inf')
    a.switch_status_payload = {'S1-a::Fan': {'state': True}}
    a.switch_status_until = float('inf')
    a.dashboard_json['cached'] = (float('inf'), {'owner': 'a'})
    a.biodynamic_payloads['cached'] = (float('inf'), {'owner': 'a'})

    second, *_ = await _build_app(second_root, monkeypatch)
    b = second.state.route_state
    assert a is not b
    assert a.biodynamic_payloads['cached'][1] == {'owner': 'a'}
    b.sensor_ids_payload = ['sensor-b']
    b.sensor_ids_until = float('inf')
    b.switch_status_payload = {'S1-b::Fan': {'state': False}}
    b.switch_status_until = float('inf')
    b.dashboard_json['cached'] = (float('inf'), {'owner': 'b'})
    b.biodynamic_payloads['cached'] = (float('inf'), {'owner': 'b'})

    for app, sid, key in ((first, 'sensor-a', 'S1-a::Fan'), (second, 'sensor-b', 'S1-b::Fan'),
                          (first, 'sensor-a', 'S1-a::Fan')):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/sensor-ids')).json() == [sid]
            assert key in (await client.get('/switch-status-update')).json()

    async with AsyncClient(transport=ASGITransport(app=first), base_url='http://test') as client:
        response = await client.post('/sensor-event', json={
            'event': 'calibration_progress',
            'payload': {'sensor_id': 'shared-sensor', 'sample_index': 2, 'sample_total': 6},
        })
        assert response.status_code == 200
        assert a.calibration_progress['shared-sensor']['sample_index'] == 2
        assert b.calibration_progress == {}

        settings = SimpleNamespace(get_setting=lambda section, key, default=None: default,
                                   set_many_in_memory=lambda updates: None, save_settings=lambda: None)
        monkeypatch.setattr(routes, 'saiSettings', lambda **kwargs: settings)
        response = await client.post('/dashboard/switch-order', json={'order': ['room-a']})
        assert response.status_code == 200
        assert a.dashboard_json == {}
        assert a.biodynamic_payloads == {}
        assert a.sensor_ids_payload is None
        assert b.dashboard_json['cached'][1] == {'owner': 'b'}
        assert b.biodynamic_payloads['cached'][1] == {'owner': 'b'}
        assert b.sensor_ids_payload == ['sensor-b']
        assert b.switch_status_payload == {'S1-b::Fan': {'state': False}}


@pytest.mark.asyncio
async def test_extracted_route_failures_keep_exception_details_server_side(tmp_path, monkeypatch):
    from testApparatus.test_nodus_settings_schema_writes import _REAL_SWITCH_SETTINGS_MANAGER

    monkeypatch.chdir(tmp_path)
    app, *_ = await _build_app(tmp_path, monkeypatch)
    private_detail = 'private-path-and-credentials'

    def fail(*args, **kwargs):
        raise ValueError(private_detail)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/graph-data', params={
            'range': 'custom', 'start': private_detail, 'end': '2026-10-26T12:00',
        })
        assert response.status_code == 400
        assert private_detail not in response.text
        for path in ('/calibration/device/apply', '/calibration/soil/ph-buffer', '/system-calibration/apply'):
            response = await client.post(path, content='{'+private_detail,
                                         headers={'Content-Type': 'application/json'})
            assert response.status_code == 400
            assert private_detail not in response.text
        monkeypatch.setattr('sensorius.saiWebCalibration.calibration_input_offsets', fail)
        response = await client.post('/calibration/device/apply', json={
            'sensor_id': 'sensor', 'device_kind': 'soil', 'offsets': [{'key': 'soil_ph_offset', 'value': 1}],
        })
        assert response.status_code == 400
        assert private_detail not in response.text
        monkeypatch.setattr(_REAL_SWITCH_SETTINGS_MANAGER, 'load', fail)
        response = await client.get('/switch-info', params={'switch_id': 'switch-test'})
        assert response.status_code == 500
        assert response.json() == {'error': 'Unable to read switch information.'}
