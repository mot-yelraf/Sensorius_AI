from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiOnboardingStore import OnboardingSessionStore, OnboardingStates
from saiOnboardingToken import OnboardingTokenManager


def test_session_store_create_and_active_filter(tmp_path):
    store = OnboardingSessionStore(base_dir=str(tmp_path))
    s1 = store.create_session(
        session_id="sess-1",
        onboard_token_hash="abc",
        token_expires_at=time.time() + 60,
        expected_device_id="dev-a",
    )
    assert s1["state"] == OnboardingStates.AP_DISCOVERED

    store.set_state("sess-1", OnboardingStates.WAITING_MQTT_HELLO)
    store.create_session(
        session_id="sess-2",
        onboard_token_hash="def",
        token_expires_at=time.time() + 60,
    )
    store.set_state("sess-2", OnboardingStates.ONLINE)

    active = store.list_active_sessions()
    ids = {s.get("session_id") for s in active}
    assert "sess-1" in ids
    assert "sess-2" not in ids


def test_token_issue_validate_consume_replay_rejected(tmp_path):
    store = OnboardingSessionStore(base_dir=str(tmp_path))
    mgr = OnboardingTokenManager(store, default_ttl_sec=120)

    issued = mgr.issue_token(session_id="sess-1", expected_device_id="aqi-123")
    token = issued["token"]

    ok, reason = mgr.validate_for_session(session_id="sess-1", token=token, device_id="aqi-123")
    assert ok is True
    assert reason == "ok"

    consumed_ok, consumed_reason, updated = mgr.consume_for_session(
        session_id="sess-1",
        token=token,
        device_id="aqi-123",
    )
    assert consumed_ok is True
    assert consumed_reason == "ok"
    assert updated is not None
    assert bool(updated.get("token_consumed", False)) is True

    # replay should fail
    ok2, reason2 = mgr.validate_for_session(session_id="sess-1", token=token, device_id="aqi-123")
    assert ok2 is False
    assert reason2 == "token_already_used"


def test_token_expiry_and_device_mismatch(tmp_path):
    store = OnboardingSessionStore(base_dir=str(tmp_path))
    mgr = OnboardingTokenManager(store, default_ttl_sec=1)

    issued = mgr.issue_token(session_id="sess-exp", expected_device_id="aqi-good", ttl_sec=1)
    token = issued["token"]

    bad_ok, bad_reason = mgr.validate_for_session(session_id="sess-exp", token=token, device_id="aqi-other")
    assert bad_ok is False
    assert bad_reason == "device_mismatch"

    # force-expire
    store.update_session("sess-exp", token_expires_at=time.time() - 5)
    exp_ok, exp_reason = mgr.validate_for_session(session_id="sess-exp", token=token, device_id="aqi-good")
    assert exp_ok is False
    assert exp_reason == "token_expired"
