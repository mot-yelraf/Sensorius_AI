"""Verify notification state follows edited conditions across saves and restarts.

Exercise the real manager, evaluator, delivery queue, and SQLite persistence
with a fake sender so these regressions never send external email.
"""

import asyncio
import copy
import json
import threading

import pytest

from sensorius.saiAutomationManager import AutomationManager
from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiEmailNotifications import AutomationNotificationService, EmailNotificationService


def notification_rule():
    return {
        "name": "Sunlight",
        "enabled": True,
        "conditions": [{"type": "sensor", "sensor": "removed", "metric": "Light Intensity",
                        "op": ">", "value": 1100, "hyst": 100}],
        "actions": [{"type": "notify", "to": "grower@example.com", "executor_switch_id": "__system__"}],
    }


@pytest.mark.parametrize("field,value", [
    ("sensor", "lux"), ("metric", "Auto Light"), ("value", 1000),
    ("hyst", 0), ("op", "<"),
])
def test_condition_edits_get_revision_but_renames_and_resaves_keep_it(tmp_path, field, value):
    manager = AutomationManager(str(tmp_path / "automations"))
    original = notification_rule()
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=original)
    changed = copy.deepcopy(original)
    changed["conditions"][0][field] = value
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=changed)
    revision = manager.load_runtime_advanced("__system__")["sun"]["script_json"]["_notification_revision"]
    changed["name"] = "Renamed"
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=changed)
    assert manager.load_runtime_advanced("__system__")["sun"]["script_json"]["_notification_revision"] == revision
    # Returning to old conditions is another edit, not a return to old sent state.
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=original)
    assert manager.load_runtime_advanced("__system__")["sun"]["script_json"]["_notification_revision"] != revision


@pytest.mark.parametrize("reading,expected_count", [(1809, 1), (0, 0)])
def test_replaced_sensor_does_not_inherit_active_state(tmp_path, monkeypatch, reading, expected_count):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    manager = AutomationManager(str(tmp_path / "automations"))
    rule = notification_rule()
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=rule)
    logger = saiDataLogger(db_path=str(tmp_path / "notifications.db"))
    sent = []

    class Sender:
        def send(self, subject, body, **kwargs):
            sent.append(subject)

    try:
        legacy_id = EmailNotificationService.automation_delivery_id("sun", "grower@example.com")
        logger.set_notification_rule_state(legacy_id, True, None, "")
        service = EmailNotificationService(settings=None, data_logger=logger, sender=Sender(), evaluate_readings=False)
        controller = AutomationNotificationService(data_logger=logger, email_delivery_service=service)._get_controller()
        controller._load_triggers_dict = lambda: {"Advanced": manager.load_runtime_advanced("__system__")}
        controller._get_values_for_sensor = lambda *args: {"Light Intensity": reading}
        rule["conditions"][0]["sensor"] = "lux"
        manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=rule)
        controller._evaluate_and_apply_advanced({})
        assert len(service._queue) == expected_count
        if expected_count:
            asyncio.run(service._deliver_item(service._pop()))
            assert sent[0].startswith("Sensorius ACTIVATED:")
        # A new service reloads SQLite state, and a new manager reloads the TOML revision.
        restarted = EmailNotificationService(settings=None, data_logger=logger, sender=Sender(), evaluate_readings=False)
        controller.email_delivery_service = restarted
        manager = AutomationManager(str(tmp_path / "automations"))
        controller._evaluate_and_apply_advanced({})
        assert restarted._pop() is None
        assert len(sent) == expected_count
    finally:
        logger.close()


def test_edit_discards_queued_and_popped_old_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    logger = saiDataLogger(db_path=str(tmp_path / "notifications.db"))
    sent = []

    class Sender:
        def send(self, *args, **kwargs):
            sent.append(args)

    try:
        service = EmailNotificationService(settings=None, data_logger=logger, sender=Sender())
        args = dict(rule_id="sun", recipient="grower@example.com", subject="old", body="old")
        service.enqueue_automation_transition(**args, triggered=True)
        popped = service._pop()
        service.enqueue_automation_transition(**args, triggered=False)
        assert not service.persisted_automation_state("sun", "grower@example.com", revision="new")
        assert service._pop() is None
        asyncio.run(service._deliver_item(popped))
        assert sent == []
        assert not service._pending_by_rule
        assert not service._pending_automation_targets
    finally:
        logger.close()


def test_editing_legacy_rule_assigns_revision_without_changing_legacy_file(tmp_path):
    legacy_root = tmp_path / "switch_settings"
    legacy = AutomationManager(str(legacy_root / "automations"))
    rule = notification_rule()
    legacy.upsert_advanced_rule("__system__", "sun", enabled=True, script=rule)
    old_bytes = legacy.get_storage_path().read_bytes()
    manager = AutomationManager(str(tmp_path / "automations"), legacy_base_dir=str(legacy_root))
    rule["conditions"][0]["sensor"] = "lux"
    manager.upsert_advanced_rule("__system__", "sun", enabled=True, script=rule)
    assert manager.load_runtime_advanced("__system__")["sun"]["script_json"]["_notification_revision"]
    assert legacy.get_storage_path().read_bytes() == old_bytes


def test_inflight_old_email_cannot_activate_edited_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    logger = saiDataLogger(db_path=str(tmp_path / "notifications.db"))

    async def exercise():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        class Sender:
            def send(self, *args, **kwargs):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)

        service = EmailNotificationService(settings=None, data_logger=logger, sender=Sender())
        service.persisted_automation_state("sun", "grower@example.com")
        service.enqueue_automation_transition(rule_id="sun", recipient="grower@example.com",
                                              triggered=True, subject="old", body="old")
        task = asyncio.create_task(service._deliver_item(service._pop()))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert not service.persisted_automation_state("sun", "grower@example.com", revision="new")
        finally:
            release.set()
            await task
        assert not service.persisted_automation_state("sun", "grower@example.com", revision="new")
        restarted = EmailNotificationService(settings=None, data_logger=logger)
        assert not restarted.persisted_automation_state("sun", "grower@example.com", revision="new")

    try:
        asyncio.run(exercise())
    finally:
        logger.close()


@pytest.mark.asyncio
async def test_editor_save_preserves_server_owned_revision(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from sensorius import saiAutomationManager
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    app, *_ = await _build_app(tmp_path, monkeypatch)
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")

    class TestManager(AutomationManager):
        def __init__(self, _base_dir="automation_settings"):
            super().__init__(str(tmp_path / "automations"))

    monkeypatch.setattr(saiAutomationManager, "AutomationManager", TestManager)
    rule = notification_rule()
    payload = {"switch_id": "__system__", "rule_id": "sun", "enabled": "true"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for sensor in ["removed", "lux", "lux"]:
            rule["conditions"][0]["sensor"] = sensor
            response = await client.post("/submit-advanced-trigger", json={
                **payload, "script_json": json.dumps(rule),
            })
            assert response.status_code == 200, response.text
            saved = TestManager().load_runtime_advanced("__system__")["sun"]["script_json"]
            if sensor == "removed":
                assert "_notification_revision" not in saved
                revision = None
            elif revision is None:
                revision = saved["_notification_revision"]
            else:
                assert saved["_notification_revision"] == revision
