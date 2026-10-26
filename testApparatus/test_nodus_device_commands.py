"""Exercise device command sequencing without routes, storage, or a broker.

Concurrent child-device batches must share their physical host's pacing lock.
Failed acceptance or application must stop subsequent writes, and restarts must
retain the legacy transport fallback and correlated result requirements.
"""

import asyncio
from types import SimpleNamespace

import pytest

from sensorius.saiNodusDeviceCommands import NodusDeviceCommands


class Transport:
    def __init__(self, *, ack=None, result=None):
        self.sent = []
        self.waited = []
        self.ack = {"accepted": True} if ack is None else ack
        self.result = {"applied": True} if result is None else result

    def publish_nodus_config(self, device_id, *, payload, restart=False):
        mid = f"cfg-{len(self.sent)}"
        self.sent.append((device_id, payload, restart))
        return {"ok": True, "message_id": mid}

    async def wait_for_config_ack(self, message_id, timeout=3):
        self.waited.append(("ack", message_id, timeout))
        return self.ack

    async def wait_for_config_result(self, message_id, timeout=8):
        self.waited.append(("result", message_id, timeout))
        return self.result


def commands(transport, *, locks=None):
    locks = {} if locks is None else locks
    return NodusDeviceCommands(
        get_ingest=lambda: transport,
        resolve_hostname=lambda *args, **kwargs: "physical-host",
        get_host_lock=lambda host: locks.setdefault(host, asyncio.Lock()),
        ack_timeout=0.2, result_timeout=0.4,
    )


def batch(device_id="child", updates=None):
    return dict(device_id=device_id, device_type="sensor", setting_file_key="sensor",
                sensor_file_name="sensor.toml",
                updates=[("Sensor", "LOCATION", "Room")] if updates is None else updates)


@pytest.mark.asyncio
async def test_batches_share_physical_host_lock_until_each_result_is_applied():
    transport = Transport()
    locked = asyncio.Event()
    release = asyncio.Event()
    original_wait = transport.wait_for_config_result

    async def wait(message_id, timeout=8):
        if message_id == "cfg-0":
            locked.set()
            await release.wait()
        return await original_wait(message_id, timeout)

    transport.wait_for_config_result = wait
    locks = {}
    first = commands(transport, locks=locks)
    second = commands(transport, locks=locks)
    task = asyncio.create_task(first.push_settings_batch(**batch(updates=[
        ("Sensor", "LOCATION", "Room"), ("Sensor", "LABEL", "First"),
    ])))
    await asyncio.wait_for(locked.wait(), timeout=1)
    other = asyncio.create_task(second.push_settings_batch(**batch("second-child")))
    try:
        await asyncio.sleep(0)
        assert len(transport.sent) == 1
        release.set()
        assert await asyncio.wait_for(task, timeout=1)
        assert await asyncio.wait_for(other, timeout=1)
    finally:
        release.set()
        await asyncio.gather(task, other)

    assert [row[1]["updates"][0]["sensor_id"] for row in transport.sent] == [
        "child", "child", "second-child",
    ]
    assert all(target == "physical-host" and not restart
               for target, _, restart in transport.sent)
    assert transport.sent[0][1] == {"updates": [{
        "section": "Sensor", "key": "LOCATION", "value": "Room",
        "sensor_id": "child", "name": "sensor.toml",
    }]}
    assert transport.waited == [
        (kind, f"cfg-{index}", timeout)
        for index in range(3) for kind, timeout in (("ack", 0.2), ("result", 0.4))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["publish", "ack", "result", "timeout"])
async def test_batch_stops_on_failure_and_releases_host_lock(failure):
    transport = Transport()
    if failure == "publish":
        def reject(device_id, **kwargs):
            transport.sent.append((device_id, kwargs))
            return {"ok": False}
        transport.publish_nodus_config = reject
    elif failure == "ack":
        transport.ack = {"accepted": False}
    elif failure == "result":
        transport.result = {"applied": False}
    else:
        transport.result = None
    locks = {}
    assert not await commands(transport, locks=locks).push_settings_batch(**batch(updates=[
        ("Sensor", "LOCATION", "Room"), ("Sensor", "LABEL", "Never sent"),
    ]))
    assert len(transport.sent) == 1
    assert not locks["physical-host"].locked()


@pytest.mark.asyncio
async def test_empty_batch_and_missing_transport_do_not_publish():
    transport = Transport()
    assert await commands(transport).push_settings_batch(**batch(updates=[]))
    assert transport.sent == []
    assert not await commands(None).push_settings_batch(**batch())


@pytest.mark.asyncio
async def test_service_resolves_current_transport_for_each_operation():
    owner = SimpleNamespace(transport=Transport())
    service = NodusDeviceCommands(
        get_ingest=lambda: owner.transport,
        resolve_hostname=lambda *args, **kwargs: "physical-host",
        get_host_lock=lambda host: asyncio.Lock(), ack_timeout=0.2, result_timeout=0.4,
    )
    assert await service.push_settings_batch(**batch())
    first = owner.transport
    owner.transport = Transport()
    assert await service.push_settings_batch(**batch())
    assert len(first.sent) == len(owner.transport.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("applied", [True, False])
async def test_restart_preserves_fallback_and_requires_device_result(legacy, applied):
    transport = Transport(ack={"ok": "yes"}, result={"rebooting": applied})
    sent = []

    def publish_json(topic, payload, **options):
        sent.append((topic, payload, options))
        return True

    def publish_restart(device_id, *, restart_mode, message_id):
        sent.append((device_id, restart_mode, message_id))
        return {"ok": True, "message_id": message_id}

    if legacy:
        transport.publish_json = publish_json
    else:
        transport.publish_nodus_restart = publish_restart
    ok, message = await commands(transport).request_restart(
        target_device="physical-host", device_id="child", device_type="sensor",
    )
    assert ok is applied
    mid = transport.waited[0][1]
    assert transport.waited == [("ack", mid, 0.2), ("result", mid, 0.4)]
    if legacy:
        assert sent == [("nodus/physical-host/config/set", {
            "message_id": mid, "payload": {}, "restart": True, "restart_mode": "soft",
        }, {"qos": 1, "retain": False, "use_ha_client": False})]
    else:
        assert sent == [("physical-host", "soft", mid)]
    assert message == ("Device restarting..." if applied else "Restart request was not applied by the device")
