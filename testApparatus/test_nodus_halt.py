"""Verify capability gating and final-result confirmation for Nodus halt.

All MQTT replies are simulated; these checks never send commands to devices.
"""
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from sensorius.saiNodusHalt import halt_device_ids, halt_device
from sensorius.saiMQTTIngest import saiMQTTIngest as MQTTIngest
from testApparatus.test_nodus_settings_schema_writes import _build_app


def test_capability_requires_explicit_true_and_excludes_removed():
    ingest = SimpleNamespace(discovery_cache={
        'nodus-yes': {'capabilities': {'halt': True}},
        'nodus-string': {'capabilities': {'halt': 'true'}},
        'nodus-number': {'capabilities': {'halt': 1}},
        'nodus-no': {'capabilities': {'halt': False}},
        'nodus-legacy': {},
        'nodus-removed': {'capabilities': {'halt': True}},
    }, is_nodus_device_removed=lambda d: d == 'nodus-removed')
    assert halt_device_ids(ingest) == ['nodus-yes']


def test_publish_halt_uses_unique_non_retained_standalone_envelope():
    ingest = MQTTIngest.__new__(MQTTIngest)
    ingest._config_lock = threading.Lock()
    ingest.config_message_device = {}
    calls = []
    ingest.publish_json = lambda *a, **kw: calls.append((a, kw)) or True
    first = ingest.publish_nodus_halt('nodus-test')
    second = ingest.publish_nodus_halt('nodus-test')
    assert first['message_id'] != second['message_id']
    args, kwargs = calls[0]
    assert args == ('nodus/nodus-test/config/set', {'message_id': first['message_id'], 'payload': {}, 'halt': True})
    assert kwargs == {'qos': 1, 'retain': False, 'use_ha_client': False}
    assert ingest.config_message_device[first['message_id']] == 'nodus-test'


def test_ingest_preserves_final_halt_fields():
    ingest = MQTTIngest.__new__(MQTTIngest)
    ingest.base_topic = ''
    ingest._config_lock = threading.Lock()
    ingest.config_result_by_message = {}
    ingest._clear_switch_config_command_by_message = lambda _id: None
    payload = {'message_id': 'halt-test', 'applied': True, 'halt': True, 'phase': 'halted', 'relays': 'held'}
    assert ingest._maybe_handle_onboarding_topics('nodus/nodus-test/config/result', '', payload)
    result = ingest.config_result_by_message['halt-test']
    assert result['halt'] is True
    assert result['phase'] == 'halted'
    assert result['relays'] == 'held'
    assert result['device_id'] == 'nodus-test'


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,success', [
    ({'applied': True, 'halt': True, 'phase': 'halted', 'relays': 'held'}, True),
    ({'accepted': True}, False),
    ({'applied': True}, False),
    ({'applied': False, 'halt': True, 'error': 'filesystem_sync_failed'}, False),
    ({'applied': True, 'halt': True, 'phase': 'halted', 'device_id': 'other'}, False),
    (None, False),
])
async def test_halt_requires_correlated_final_result(reply, success):
    async def wait(message_id, timeout):
        return None if reply is None else {'message_id': message_id, 'device_id': 'nodus-test', **reply}
    ingest = SimpleNamespace(publish_nodus_halt=lambda d: {'ok': True, 'message_id': 'halt-test'}, wait_for_config_result=wait)
    assert (await halt_device(ingest, 'nodus-test'))['halted'] is success


@pytest.mark.asyncio
async def test_routes_gate_capabilities_and_acknowledgement_before_publish(tmp_path, monkeypatch):
    app, ingest, *_ = await _build_app(tmp_path, monkeypatch)
    ingest.discovery_cache = {'nodus-test': {'capabilities': {'halt': True}}, 'nodus-legacy': {}}
    calls = []
    ingest.publish_nodus_halt = lambda d: calls.append(d) or {'ok': True, 'message_id': 'halt-test'}
    ingest.next_config_result = {'device_id': 'nodus-test', 'applied': True, 'halt': True, 'phase': 'halted', 'relays': 'held'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test', headers={'sec-fetch-site': 'same-origin'}) as client:
        assert (await client.get('/halt-device-list')).json()['devices'] == ['nodus-test']
        for body in ({'device_ids': ['nodus-test']}, {'device_ids': ['nodus-legacy'], 'acknowledged': True}, {'device_ids': ['../bad'], 'acknowledged': True}):
            assert (await client.post('/halt-device', json=body)).status_code == 400
        assert (await client.post('/halt-device', content='{')).status_code == 400
        assert calls == []
        response = await client.post('/halt-device', json={'device_ids': ['nodus-test', 'nodus-test'], 'acknowledged': True})
    assert response.json()['results'][0]['halted'] is True
    assert calls == ['nodus-test']


@pytest.mark.asyncio
async def test_batch_reports_each_device_without_treating_failure_as_halted(tmp_path, monkeypatch):
    app, ingest, *_ = await _build_app(tmp_path, monkeypatch)
    ingest.discovery_cache = {d: {"capabilities": {"halt": True}} for d in ("nodus-one", "nodus-two")}
    calls = []
    ingest.publish_nodus_halt = lambda d: calls.append(d) or {"ok": d == "nodus-two", "message_id": d}

    async def wait(message_id, timeout):
        return {"device_id": message_id, "message_id": message_id, "applied": True, "halt": True, "phase": "halted"}

    ingest.wait_for_config_result = wait
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}) as client:
        response = await client.post("/halt-device", json={"device_ids": ["nodus-one", "nodus-two"], "acknowledged": True})
    assert calls == ["nodus-one", "nodus-two"]
    assert [r["halted"] for r in response.json()["results"]] == [False, True]
