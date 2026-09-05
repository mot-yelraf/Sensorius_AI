"""Verify farmOS connection reuse, cancellation, and bounded retry accounting.

HTTP responses are deterministic; no external farmOS server is contacted.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from sensorius.saiFarmOSBridge import saiFarmOSBridge


class Settings:
    def get_setting(self, section, key, default=None):
        return {'ENABLED': True, 'BASE_URL': 'https://farm.invalid', 'QUEUE_MAX': 10, 'ACCESS_TOKEN': 'test'}.get(key, default)


def bridge():
    return saiFarmOSBridge(settings=Settings(), data_logger=SimpleNamespace(add_readings_listener=lambda callback: None))


def test_queue_overflow_and_requeue_remain_bounded_and_observable():
    service = bridge()
    for index in range(12):
        service._enqueue({'sensor_id': str(index)})
    assert service.status_snapshot()['queue_depth'] == 10
    assert service.status_snapshot()['dropped_count'] == 2
    item = service._pop()
    service._enqueue({'sensor_id': 'latest'})
    service._push_front(item)
    assert service.status_snapshot()['queue_depth'] == 10
    assert service.status_snapshot()['dropped_count'] == 3


@pytest.mark.asyncio
async def test_worker_reuses_http_client_and_retries_same_item(monkeypatch):
    import sensorius.saiFarmOSBridge as module
    service = bridge()
    for index in range(2):
        service._enqueue({'sensor_id': str(index), 'timestamp': '2026-09-05T12:00:00+00:00', 'values': {'Temp': 21}})
    requests = []
    clients = []
    real_client = httpx.AsyncClient

    def response(request):
        requests.append(request)
        return httpx.Response(500 if len(requests) == 1 else 201)

    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(response), **kwargs)
        clients.append(client)
        return client

    async def sleep(delay):
        if service._exported_count == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(module.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert len(clients) == 1
    assert clients[0].is_closed
    assert len(requests) == 3
    assert requests[0].content == requests[1].content
    assert service.status_snapshot()['retry_count'] == 1
    assert service.status_snapshot()['exported_count'] == 2
    assert service.status_snapshot()['queue_depth'] == 0


@pytest.mark.asyncio
async def test_cancellation_requeues_inflight_item(monkeypatch):
    service = bridge()
    service._enqueue({'sensor_id': 'one', 'values': {'Temp': 21}})
    async def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(service, '_post_item', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert service.status_snapshot()['queue_depth'] == 1
