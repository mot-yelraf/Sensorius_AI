"""Exercise command correlation without an MQTT client or device firmware.

These cases cover asynchronous and threaded receipt ordering, switch request
expiry, and the separation between device confirmation and command acceptance.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sensorius.saiMQTTCommandLifecycle import MQTTCommandLifecycle
from sensorius.saiMQTTIngest import saiMQTTIngest


def queue_switch(lifecycle, *, mid="cfg-1", state=True):
    assert lifecycle._begin_switch_config_command("hub", "S1", state) == "send"
    lifecycle._mark_switch_config_command_published("hub", "S1", mid, "nodus/S1/config/set")
    lifecycle.track_switch_request("hub", "Fan", "S1", state, event_origin="auto", event_label="Cooling")


@pytest.mark.asyncio
async def test_threaded_ack_and_result_waits_are_correlated_and_copied():
    lifecycle = MQTTCommandLifecycle()
    queue_switch(lifecycle)
    waiter = asyncio.create_task(lifecycle.wait_for_config_ack("cfg-1", timeout=1))
    await asyncio.to_thread(lifecycle.record_config_reply, "S2", "ack",
                            {"message_id": "other", "accepted": True}, topic="nodus/S2/config/ack")
    await asyncio.sleep(0)
    assert not waiter.done()
    await asyncio.to_thread(lifecycle.record_config_reply, "S1", "ack",
                            {"message_id": "cfg-1", "accepted": True}, topic="nodus/S1/config/ack")
    ack = await asyncio.wait_for(waiter, timeout=2)
    assert ack["device_id"] == "S1" and ack["accepted"]
    ack["accepted"] = False
    assert lifecycle.config_ack_by_message["cfg-1"]["accepted"]
    # A result received before its waiter must still be available. Neither an
    # ACK nor a successful result alone confirms the physical relay state.
    lifecycle.record_config_reply("S1", "result", {
        "message_id": "cfg-1", "applied": True, "persistence_mode": "volatile",
    }, topic="nodus/S1/config/result")
    assert (await lifecycle.wait_for_config_result("cfg-1"))["applied"]
    status = lifecycle.get_switch_command_status("hub", "Fan")
    assert status["pending"] and "not saved" in status["command_warning"]
    lifecycle.clear_pending_switch_set("hub", channel_id="S1", observed_state=False)
    assert lifecycle.get_switch_command_status("hub", "Fan")["pending"]
    lifecycle.clear_pending_switch_set("hub", channel_id="S1", observed_state=True)
    assert not lifecycle.get_switch_command_status("hub", "Fan")["pending"]
    assert lifecycle._pending_switch_meta("hub", channel_id="S1")["event_label"] == "Cooling"


def test_switch_expiry_rejection_cooldown_and_origin_window():
    now = [100.0]
    lifecycle = MQTTCommandLifecycle(clock=lambda: now[0])
    queue_switch(lifecycle)
    assert lifecycle._begin_switch_config_command("hub", "s1", True) == "coalesced"
    assert lifecycle._begin_switch_config_command("hub", "s1", False) == "blocked"
    lifecycle.record_config_reply("S1", "ack", {
        "message_id": "cfg-1", "accepted": False, "error": "busy",
    }, topic="nodus/S1/config/ack")
    assert lifecycle.get_switch_command_status("hub", "Fan")["command_error"] == "busy"
    assert not lifecycle._switch_config_command_inflight
    queue_switch(lifecycle, mid="cfg-2")
    lifecycle._mark_switch_config_command_failed("hub", "S1")
    now[0] = 104.99
    assert lifecycle._begin_switch_config_command("hub", "S1", True) == "blocked"
    now[0] = 105.0
    assert lifecycle._begin_switch_config_command("hub", "S1", True) == "send"
    now[0] = 150.0
    assert lifecycle._begin_switch_config_command("hub", "S1", False) == "send"
    assert "did not confirm" in lifecycle.get_switch_command_status("hub", "Fan")["command_error"]
    assert "cfg-2" not in lifecycle._switch_config_command_by_message
    lifecycle.clear_pending_switch_set("hub", channel_id="S1", observed_state=True)
    now[0] = 157.99
    assert lifecycle._pending_switch_meta("hub", label="Fan")["event_origin"] == "auto"
    now[0] = 158.01
    assert lifecycle._pending_switch_meta("hub", label="Fan") is None


def test_simultaneous_duplicate_requests_share_one_slot():
    lifecycle = MQTTCommandLifecycle()
    ready = threading.Barrier(2)

    def begin():
        ready.wait(timeout=2)
        return lifecycle._begin_switch_config_command("hub", "S1", True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(begin) for _ in range(2)]
        assert sorted(request.result(timeout=3) for request in requests) == ["coalesced", "send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["wait_for_config_ack", "wait_for_config_result",
                                    "wait_for_calibration_ack", "wait_for_calibration_result",
                                    "wait_for_nodus_meta_patch"])
async def test_wait_timeout_and_cancellation_do_not_fabricate_receipts(method):
    lifecycle = MQTTCommandLifecycle()
    wait = getattr(lifecycle, method)
    assert await wait("missing", timeout=0.001) is None
    task = asyncio.create_task(wait("missing", timeout=60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lifecycle.config_ack_by_message
    assert not lifecycle.calibration_ack_by_message
    assert not lifecycle.meta_patch_by_message


@pytest.mark.asyncio
async def test_meta_source_filter_and_partial_sample_timeout():
    lifecycle = MQTTCommandLifecycle()
    lifecycle.record_meta_patch("cfg", {"message_id": "cfg", "source": "config_set"})
    assert await lifecycle.wait_for_nodus_meta_patch(" cfg ", source=" CONFIG_SET ")
    assert await lifecycle.wait_for_nodus_meta_patch("cfg", source="calibration_set", timeout=0.001) is None
    lifecycle.calibration_samples_by_message["cal"] = [{"sample_index": 2, "values": {"pH": 7}}]
    rows = await lifecycle.wait_for_calibration_samples("cal", expected_count=3, timeout=0.001)
    assert rows == [{"sample_index": 2, "values": {"pH": 7}}]
    # Existing sampling contracts permit a highest index to signal completion.
    assert await lifecycle.wait_for_calibration_samples("cal", expected_count=2) == rows
    assert await lifecycle.wait_for_calibration_samples("missing", timeout=0) == []


def test_ingest_compatibility_aliases_share_and_replace_owned_state():
    ingest = saiMQTTIngest.__new__(saiMQTTIngest)
    replacement = {}
    ingest.config_ack_by_message = replacement
    lifecycle = ingest._get_command_lifecycle()
    assert lifecycle.config_ack_by_message is replacement
    lifecycle.record_config_reply("S1", "ack", {"message_id": "m", "accepted": True}, topic="ack")
    assert replacement["m"]["accepted"]
    assert ingest._config_lock is lifecycle._config_lock
    ingest._pending_set = {("hub", "Fan"): {"channel_id": "S1", "state": True, "ts": 0}}
    assert ingest._pending_set is lifecycle._pending_set
