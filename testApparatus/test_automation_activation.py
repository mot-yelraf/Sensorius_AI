"""Exercise baseline preparation without real switches or MQTT commands.

Use the real TOML manager and controlled device confirmations to verify that
activation cannot overtake a pending baseline or leave a failed rule enabled.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from sensorius.saiAutomationActivation import AutomationActivationError, activate_with_baselines
from sensorius.saiAutomationManager import AutomationManager


class Controller:
    switch_id = "switch-test"
    is_remote = False
    channel_id_for_label = {"Fan": "S1-test", "Light": "S2-test"}

    def __init__(self, fan=True, light=False):
        self.states = {"Fan": fan, "Light": light}
        self.calls = []
        self._rules_cache = {"mtime": 1}
        self._advanced_active_actions = {}
        self._advanced_delay_due = {}
        self._advanced_revert_cooldown = set()

    def get_switch_names(self):
        return list(self.states)

    def label_for_channel_id(self, channel):
        return next((label for label, cid in self.channel_id_for_label.items() if cid == channel), "")

    def get_state(self, label):
        return self.states[label]

    def set_state(self, label, state, **kwargs):
        assert self._automation_preparing
        self.calls.append((label, state, kwargs))
        self.states[label] = state
        return True


def script(desired=True, **action):
    return {"enabled": True, "conditions": [{"type": "timer", "duration_min": 15, "period_min": 60}],
            "actions": [{"type": "switch", "switch_key": "switch-test::S1-test", "set": desired,
                         "revert_action": "previous_state", **action}]}


async def activate(manager, ctrl, doc, *, timeout_s=0.2, commit=None):
    return await activate_with_baselines(
        manager, "__system__", {"fan": {"script_json": doc}}, {ctrl.switch_id: ctrl},
        commit or (lambda: manager.upsert_advanced_rule("__system__", "fan", enabled=True, script=doc)),
        lock=asyncio.Lock(), timeout_s=timeout_s,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("desired", [True, False])
async def test_prepares_opposite_state_before_persisting_enabled_rule(tmp_path, desired):
    manager = AutomationManager(str(tmp_path))
    ctrl = Controller(fan=desired)
    doc = script(desired)

    def commit():
        assert ctrl.states["Fan"] is not desired
        manager.upsert_advanced_rule("__system__", "fan", enabled=True, script=doc)

    await activate(manager, ctrl, doc, commit=commit)
    assert ctrl.calls == [("Fan", not desired, {"force": True, "event_source": "auto/baseline"})]
    assert manager.load("__system__")["Advanced"]["fan"]["enabled"]
    assert not ctrl._automation_preparing


@pytest.mark.asyncio
async def test_correct_baseline_and_do_nothing_need_no_commands(tmp_path):
    manager = AutomationManager(str(tmp_path))
    ctrl = Controller(fan=False)
    ctrl.auto_off_deadline = {"Fan": 12345}
    await activate(manager, ctrl, script())
    assert ctrl.auto_off_deadline["Fan"] is None
    ctrl.states["Fan"] = True
    await activate(manager, ctrl, script(revert_action="do_nothing"))
    assert ctrl.calls == []


@pytest.mark.asyncio
async def test_paired_channels_and_label_aliases(tmp_path):
    manager = AutomationManager(str(tmp_path))
    ctrl = Controller(fan=True, light=False)
    doc = script()
    doc["actions"].append({**doc["actions"][0], "switch_key": "switch-test::Light", "set": False})
    await activate(manager, ctrl, doc)
    assert ctrl.states == {"Fan": False, "Light": True}


@pytest.mark.asyncio
async def test_edit_clears_old_ownership_and_failure_leaves_rule_disabled(tmp_path):
    manager = AutomationManager(str(tmp_path))
    doc = script()
    manager.upsert_advanced_rule("__system__", "fan", enabled=True, script=doc)
    ctrl = Controller()
    key = ("fan", "Fan", "switch-test::S1-test", True)
    ctrl._advanced_active_actions[key] = {"revert_to": True}
    ctrl._advanced_delay_due[key] = {"due_at": 100}
    ctrl._advanced_revert_cooldown.add(key)
    ctrl.set_state = lambda *args, **kwargs: False
    with pytest.raises(AutomationActivationError, match="remains disabled"):
        await activate(manager, ctrl, doc)
    assert not manager.load("__system__")["Advanced"]["fan"]["enabled"]
    assert ctrl._advanced_active_actions == ctrl._advanced_delay_due == {}
    assert ctrl._advanced_revert_cooldown == set()
    assert not ctrl._automation_preparing


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "rejected", "timeout"])
async def test_remote_acceptance_requires_confirmation(tmp_path, outcome):
    manager = AutomationManager(str(tmp_path))
    ctrl = Controller()
    ctrl.is_remote = True
    sent = asyncio.Event()
    pending = {"pending": False}
    ctrl.mqtt_ingest = SimpleNamespace(
        get_nodus_liveness=lambda *a, **kw: {"state": "online"},
        get_switch_command_status=lambda *a: dict(pending),
    )

    loop = asyncio.get_running_loop()

    def request(*args, **kwargs):
        pending["pending"] = True
        loop.call_soon_threadsafe(sent.set)
        return True

    ctrl.set_state = request
    task = asyncio.create_task(activate(manager, ctrl, script(), timeout_s=0.25))
    await sent.wait()
    assert "fan" not in manager.load("__system__")["Advanced"]
    assert not task.done()
    if outcome == "confirmed":
        ctrl.states["Fan"] = False
        pending["pending"] = False
        await task
        assert manager.load("__system__")["Advanced"]["fan"]["enabled"]
    else:
        if outcome == "rejected":
            pending.update(pending=False, command_error="relay rejected")
        with pytest.raises(AutomationActivationError):
            await task
        assert "fan" not in manager.load("__system__")["Advanced"]
    assert not ctrl._automation_preparing


@pytest.mark.asyncio
async def test_enabling_saved_disabled_script_prepares_baseline(tmp_path):
    manager = AutomationManager(str(tmp_path))
    doc = script()
    doc["enabled"] = False
    manager.upsert_advanced_rule("__system__", "fan", enabled=False, script=doc)
    ctrl = Controller()
    await activate(manager, ctrl, doc, commit=lambda: manager.set_rule_enabled(
        "__system__", section="Advanced", rule_id="fan", enabled=True))
    assert ctrl.states["Fan"] is False
    saved = manager.load("__system__")["Advanced"]["fan"]
    assert saved["enabled"] and json.loads(saved["script_json"])["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "channel", "conflict", "mixed_conflict", "owner", "offline"])
async def test_preflight_failures_send_no_commands(tmp_path, failure):
    manager = AutomationManager(str(tmp_path))
    ctrl = Controller()
    doc = script()
    if failure == "missing":
        doc["actions"][0]["switch_key"] = "missing::Fan"
    elif failure == "channel":
        doc["actions"][0]["switch_key"] = "switch-test::missing"
    elif failure == "conflict":
        doc["actions"].append({**doc["actions"][0], "set": False})
    elif failure == "mixed_conflict":
        doc["actions"].append({**doc["actions"][0], "switch_key": "switch-test::Fan",
                               "set": False, "revert_action": "do_nothing"})
    elif failure == "owner":
        manager.upsert_advanced_rule("__system__", "other", enabled=True, script=doc)
    elif failure == "offline":
        ctrl.is_remote = True
        ctrl.mqtt_ingest = SimpleNamespace(get_nodus_liveness=lambda *a, **kw: {"state": "offline"})
    with pytest.raises(AutomationActivationError):
        await activate(manager, ctrl, doc)
    assert ctrl.calls == []
    assert "fan" not in manager.load("__system__")["Advanced"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save_on", "save_off", "save_disabled", "enable", "override", "failure",
                                        "save_error", "enable_error", "override_error"])
async def test_http_activation_paths(tmp_path, monkeypatch, operation):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as automation_module
    import sensorius.saiWebRoutes as routes
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    app, _ingest, _system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)

    class Manager(AutomationManager):
        def __init__(self, _base="automation_settings"):
            super().__init__(str(tmp_path / "automation-root"))

    monkeypatch.setattr(automation_module, "AutomationManager", Manager)
    ctrl = Controller(fan=operation != "save_off")
    ctrl.sensor = None
    ctrl.values = {}
    ctrl.override_script = {}
    ctrl._evaluate_and_apply_advanced = lambda values: None
    from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
    SwitchSettingsManager(str(switch_root)).save(ctrl.switch_id, {"Switch": {
        "SWITCH_DEVICE_ID": ctrl.switch_id, "SWITCH_1_LABEL": "Fan",
        "SWITCH_1_CHANNEL_ID": "S1-test", "SWITCH_2_LABEL": "Light", "SWITCH_2_CHANNEL_ID": "S2-test"}})
    app.state.switch_controllers = {ctrl.switch_id: ctrl}
    monkeypatch.setattr(routes, "switch_controllers", app.state.switch_controllers, raising=False)
    app.state.supervisor = SimpleNamespace(_task_names={"switch-test Controladora Monitor"})
    doc = script(operation != "save_off")
    if operation in {"enable", "override", "failure", "enable_error", "override_error"}:
        Manager().upsert_advanced_rule("__system__", "fan", enabled=False, script={**doc, "enabled": False})
    if operation == "failure":
        ctrl.set_state = lambda *a, **kw: False
    if operation.endswith("_error"):
        def unexpected_failure(*args, **kwargs):
            raise RuntimeError("private-database-path-and-internal-details")
        method = "upsert_advanced_rule" if operation == "save_error" else "set_rule_enabled"
        monkeypatch.setattr(Manager, method, unexpected_failure)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        if operation in {"enable", "failure", "enable_error"}:
            response = await client.post("/advanced/automations/enable", data={
                "switch_id": "__system__", "rule_id": "fan", "enabled": "true"})
        elif operation in {"override", "override_error"}:
            response = await client.post("/switch/override", params={
                "switch_id": ctrl.switch_id, "switch_name": "Fan"}, json={"enabled": True})
        else:
            response = await client.post("/submit-advanced-trigger", json={
                "switch_id": "__system__", "rule_id": "fan",
                "enabled": operation != "save_disabled", "script_json": json.dumps(doc)})
    if operation.endswith("_error"):
        assert response.status_code == 500, response.text
        assert "private-database-path-and-internal-details" not in response.text
        assert "error" in response.json()
    elif operation == "failure":
        assert response.status_code == 409, response.text
        assert "remains disabled" in response.json()["error"]
        assert not Manager().load("__system__")["Advanced"]["fan"]["enabled"]
    else:
        assert response.status_code == 200, response.text
        saved = Manager().load("__system__")["Advanced"]["fan"]
        assert saved["enabled"] == (operation != "save_disabled")
        if operation == "save_disabled":
            assert ctrl.calls == []
            assert ctrl.states["Fan"] is True
        else:
            assert ctrl.states["Fan"] == (operation == "save_off")
            assert len(ctrl.calls) == 1


def test_evaluator_pauses_during_baseline_preparation():
    from testApparatus.test_sai_switch_controller import _make_controller, SwitchController

    ctrl = _make_controller()
    ctrl._automation_preparing = True
    ctrl._load_triggers_dict = lambda: pytest.fail("Rules must not run during baseline preparation")
    SwitchController._evaluate_and_apply_advanced(ctrl, {})


@pytest.mark.asyncio
async def test_ecowitt_commands_stay_on_event_loop_and_wait_for_confirmation(tmp_path):
    ctrl = Controller()
    ctrl.is_ecowitt = ctrl.available = True
    ctrl.command_task = None
    ctrl.last_error = ""
    loop = asyncio.get_running_loop()

    async def confirm(state):
        await asyncio.sleep(0)
        ctrl.states["Fan"] = state
        return True

    def set_state(label, state, **kwargs):
        assert asyncio.get_running_loop() is loop
        ctrl.command_task = loop.create_task(confirm(state))
        return True

    ctrl.set_state = set_state
    await activate(AutomationManager(str(tmp_path)), ctrl, script())
    assert ctrl.command_task.done()
    assert ctrl.states["Fan"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("desired", [True, False])
async def test_prepared_timer_applies_and_restores_opposite_baseline(tmp_path, monkeypatch, desired):
    import time
    import sensorius.saiSwitch as switch_module
    from testApparatus.test_sai_switch_controller import _make_controller

    manager = AutomationManager(str(tmp_path))
    ctrl = _make_controller()
    ctrl.switch_id = "switch-test"
    ctrl.get_switch_names = lambda: ["Fan"]
    ctrl.channel_id_for_label = {"Fan": "S1-test"}
    ctrl.last_state["Fan"] = desired
    ctrl._persist_advanced_runtime_state = lambda: None
    ctrl._load_triggers_dict = lambda: {"Advanced": manager.load_runtime_advanced("__system__")}
    commands = []

    def set_state(label, state, **kwargs):
        ctrl.last_state[label] = state
        commands.append(state)
        return True

    ctrl.set_state = set_state
    await activate(manager, ctrl, script(desired))
    assert commands == [not desired]
    monkeypatch.setattr(switch_module.time, "localtime", lambda: time.struct_time((2026, 10, 18, 12, 0, 1, 6, 291, -1)))
    ctrl._evaluate_and_apply_advanced({})
    assert commands == [not desired, desired]
    monkeypatch.setattr(switch_module.time, "localtime", lambda: time.struct_time((2026, 10, 18, 12, 15, 1, 6, 291, -1)))
    ctrl._evaluate_and_apply_advanced({})
    assert commands == [not desired, desired, not desired]
