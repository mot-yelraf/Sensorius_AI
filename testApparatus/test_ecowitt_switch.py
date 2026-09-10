"""Exercise AC1100 discovery, confirmation, persistence, and switch contracts.

HTTP responses model the documented Ecowitt IoT API without physical hardware;
SQLite and settings use isolated real files to verify event and identity behavior.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiEcowitt import EcowittError, EcowittGatewayIngest
from sensorius.saiEcowittSwitch import (
    EcowittSwitchController, discover_smart_plugs, query_interval,
    save_smart_plugs, supports_smart_plugs,
)
from sensorius.saiSwitch import build_switch_controller
from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
from testApparatus.test_ecowitt_ingest import _Settings

DISCOVERY = dict(sensor_id="ecowitt-aabbccddeeff", gateway_url="http://gateway.test",
                 gateway_model="GW1200A_V1.0.0", smart_plugs=[dict(id=2257, model=2)])
SID = "ecowitt-aabbccddeeff-ac1100-000008d1"


@pytest.fixture
def plug(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sensorius.saiSwitch.get_mqtt_client", lambda *args: None)
    save_smart_plugs(DISCOVERY)
    manager = SwitchSettingsManager()
    logger = saiDataLogger(str(tmp_path / "events.db"))
    controller = build_switch_controller(switch_settings=manager.load(SID), data_logger=logger)
    settings = _Settings()
    settings.values.update({("Ecowitt", "ENABLED"): True,
                            ("Ecowitt", "GATEWAY_URL"): DISCOVERY["gateway_url"],
                            ("Ecowitt", "SENSOR_ID"): DISCOVERY["sensor_id"]})
    service = EcowittGatewayIngest(settings=settings, data_logger=logger)
    service.switch_controllers = {SID: controller}
    controller.service = service
    return controller, manager, logger


@pytest.mark.parametrize("model,expected", [("Version: GW1200A_V1.0.0", True), ("GW1200", True),
                                               ("GW1100A_V2.3.1", False), ("GW2000", False)])
def test_supported_gateway(model, expected):
    assert supports_smart_plugs(model) is expected


@pytest.mark.parametrize("value", [14, 61, "15.5", True, None, "bad"])
def test_interval_rejects_invalid_values(value):
    with pytest.raises(EcowittError):
        query_interval(value)


@pytest.mark.parametrize("value", [15, 30, 60, "45"])
def test_interval_accepts_bounds(value):
    assert query_interval(value) == int(value)


@pytest.mark.asyncio
async def test_discovery_filters_other_models_and_preserves_offline_plugs():
    async def handler(request):
        assert request.url.path == "/get_iot_device_list"
        return httpx.Response(200, json={"command": [
            {"model": 1, "id": 123, "rfnet_state": 1},
            {"model": 2, "id": 2257, "rfnet_state": 1},
            {"model": 2, "id": 2258, "rfnet_state": 0},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plugs = await discover_smart_plugs(client, "http://gateway.test")
    assert [p["id"] for p in plugs] == [2257, 2258]
    assert [p["online"] for p in plugs] == [True, False]


def test_factory_and_rediscovery_preserve_user_settings(plug):
    ctrl, manager, _ = plug
    assert isinstance(ctrl, EcowittSwitchController)
    assert not ctrl.confirmed
    assert not ctrl.set_state("Plug", True)
    manager.update_setting(SID, "SWITCH_1_LABEL", "Heater")
    manager.update_setting(SID, "SWITCH_LOCATION", "Greenhouse")
    old_channel = manager.load(SID)["Switch"]["SWITCH_1_CHANNEL_ID"]
    save_smart_plugs(DISCOVERY)
    assert ctrl.get_switch_names() == ["Heater"]
    assert ctrl.location == "Greenhouse"
    assert ctrl.channel_id_for_label["Heater"] == old_channel
    assert "SWITCH_1_PIN" not in manager.load(SID)["Switch"]


@pytest.mark.asyncio
async def test_poll_events_only_on_observed_changes_and_label_rename(plug, monkeypatch):
    ctrl, manager, logger = plug
    state = 1
    async def request(command):
        assert command == {"cmd": "read_device"}
        return {"command": [{"model": 2, "id": 2257, "ac_status": state, "warning": 0}]}
    monkeypatch.setattr(ctrl, "_request", request)
    assert await ctrl.poll_status() is True
    assert ctrl.available
    await ctrl.poll_status()
    events = logger.get_last_switch_events(ctrl._switch_key("Plug"), limit=10)
    assert len(events) == 1
    manager.update_setting(SID, "SWITCH_1_LABEL", "Heater")
    assert ctrl.get_state("Heater") is True
    state = 0
    assert await ctrl.poll_status() is False
    assert logger.get_latest_switch_state(ctrl._switch_key("Heater")) == "Off"
    assert manager.load(SID)["Switch"]["SWITCH_1_LAST_STATE"] is False


@pytest.mark.asyncio
async def test_command_uses_http_and_waits_for_relay_confirmation(plug, monkeypatch):
    ctrl, _, logger = plug
    calls = []
    states = iter([0, 0, 1])
    async def handler(request):
        command = json.loads(request.content)["command"][0]
        calls.append(command)
        assert request.url.path == "/parse_quick_cmd_iot"
        assert command["id"] == 2257 and command["model"] == 2
        if command["cmd"] == "read_device":
            return httpx.Response(200, json={"command": [{"model": 2, "id": 2257,
                                                         "ac_status": next(states), "warning": 0}]})
        return httpx.Response(200, json={"command": []})
    original_client = httpx.AsyncClient
    monkeypatch.setattr("sensorius.saiEcowittSwitch.httpx.AsyncClient",
                        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    async def no_wait(*args):
        return None
    monkeypatch.setattr("sensorius.saiEcowittSwitch.asyncio.sleep", no_wait)
    await ctrl.poll_status()
    ctrl.min_off_time = 0
    assert ctrl.set_state("Plug", True, event_source="auto/rule:Heating")
    assert not ctrl.get_state("Plug")
    assert not ctrl.set_state("Plug", False)
    assert await ctrl.command_task is True
    assert ctrl.get_state("Plug")
    assert [c["cmd"] for c in calls] == ["read_device", "quick_run", "read_device", "read_device"]
    events = logger.get_last_switch_events(ctrl._switch_key("Plug"), limit=10, include_source=True)
    assert len(events) == 2
    assert any(row[2] == "auto/rule:Heating" for row in events)


@pytest.mark.asyncio
async def test_failed_confirmation_does_not_log_requested_state(plug, monkeypatch):
    ctrl, _, logger = plug
    async def request(command):
        return {"command": [{"model": 2, "id": 2257, "ac_status": 0}]}
    async def no_wait(*args):
        return None
    monkeypatch.setattr(ctrl, "_request", request)
    monkeypatch.setattr("sensorius.saiEcowittSwitch.asyncio.sleep", no_wait)
    await ctrl.poll_status()
    ctrl.min_off_time = 0
    assert ctrl.set_state("Plug", True)
    assert await ctrl.command_task is False
    assert not ctrl.get_state("Plug")
    assert "did not confirm" in ctrl.last_error
    assert len(logger.get_last_switch_events(ctrl._switch_key("Plug"), limit=10)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"command": []}, {"command": [{"model": 2, "id": 999, "ac_status": 1}]},
    {"command": [{"model": 2, "id": 2257, "ac_status": 1, "warning": 128}]},
])
async def test_missing_or_offline_device_cannot_be_controlled(plug, monkeypatch, payload):
    ctrl, _, _ = plug
    async def request(command):
        return payload
    monkeypatch.setattr(ctrl, "_request", request)
    assert await ctrl.poll_status() is None
    assert not ctrl.available
    assert not ctrl.set_state("Plug", True)


@pytest.mark.asyncio
async def test_activation_is_idempotent_and_disable_blocks_commands(plug):
    ctrl, _, _ = plug
    service = ctrl.service
    service.switch_controllers.clear()
    registered = []
    service.supervisor = SimpleNamespace(add=lambda *args, **kw: registered.append(kw["name"]))
    await service.activate_smart_plugs()
    await service.activate_smart_plugs()
    assert len(service.switch_controllers) == 1
    assert len(registered) == 1
    service.disable()
    ctrl.online = ctrl.confirmed = True
    assert not ctrl.available
    assert not ctrl.set_state("Plug", True)


@pytest.mark.asyncio
async def test_real_routes_toggle_status_and_edit_without_mqtt(plug, monkeypatch):
    from fastapi import FastAPI
    import sensorius.saiWebRoutes as routes
    from testApparatus.test_ecowitt_routes import _FastStats, _Settings as RouteSettings

    ctrl, manager, logger = plug
    monkeypatch.setattr(routes, "FastStats", _FastStats)
    monkeypatch.setattr(routes, "data_logger", logger)
    monkeypatch.setattr(routes, "switch_controllers", {SID: ctrl}, raising=False)
    monkeypatch.setattr(routes, "_switch_status_cache_payload", None)
    monkeypatch.delenv("SENSORIUS_API_KEY", raising=False)
    state = 0
    async def request(command):
        nonlocal state
        if command["cmd"] == "quick_run":
            state = 1
        elif command["cmd"] == "quick_stop":
            state = 0
        return {"command": [{"model": 2, "id": 2257, "ac_status": state}]}
    async def no_wait(*args):
        pass
    monkeypatch.setattr(ctrl, "_request", request)
    monkeypatch.setattr("sensorius.saiEcowittSwitch.asyncio.sleep", no_wait)
    await ctrl.poll_status()
    ctrl.min_off_time = ctrl.min_on_time = 0
    app = FastAPI()
    app.state.switch_controllers = {SID: ctrl}
    app.state.ecowitt_service = ctrl.service
    await routes.register_routes(app, RouteSettings(), object(), object(), None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", headers={"Origin": "http://test"}) as client:
        response = await client.post("/switch/toggle", params={"switch_id": SID, "switch_name": "Plug",
                                     "switch_key": f"{ctrl.channel_id_for_label['Plug']}::Plug"})
        assert response.status_code == 200, response.text
        assert response.json()["state"] is True
        assert len(logger.get_last_switch_events(ctrl._switch_key("Plug"), limit=10)) == 2
        response = await client.get("/switch-status-update")
        assert response.status_code == 200, response.text
        status = response.json()[f"{SID}::Plug"]
        assert status["online"] and status["confirmed"] and status["state"]
        assert response.json()[f"{ctrl.channel_id_for_label['Plug']}::Plug"]["online"] is True
        response = await client.post("/submit-switch-settings", data={
            "switch_id": SID, "location": "Greenhouse", "SWITCH_1_LABEL": "Heater",
        }, headers={"Accept": "application/json"})
        assert response.status_code in (200, 303), response.text
        assert ctrl.get_switch_names() == ["Heater"]
        assert ctrl.get_state("Heater") is True
        assert manager.load(SID)["Switch"]["SWITCH_LOCATION"] == "Greenhouse"
        assert len(logger.get_last_switch_events(ctrl._switch_key("Heater"), limit=10)) == 2
        monkeypatch.setattr("sensorius.saiAutomationManager.AutomationManager.get_advanced_state_for_switch_key",
                            lambda *args: {"enabled_any": True})
        response = await client.post("/switch/toggle", params={"switch_id": SID, "switch_name": "Heater"})
        assert response.status_code == 423
        assert ctrl.get_state("Heater") is True


@pytest.mark.asyncio
async def test_restart_queries_state_without_sending_commands_or_duplicate_events(plug, monkeypatch):
    ctrl, manager, logger = plug
    requests = []
    async def request(command):
        requests.append(command["cmd"])
        return {"command": [{"model": 2, "id": 2257, "ac_status": 1}]}
    monkeypatch.setattr(ctrl, "_request", request)
    await ctrl.poll_status()
    restarted = build_switch_controller(switch_settings=manager.load(SID), data_logger=logger)
    restarted.service = ctrl.service
    monkeypatch.setattr(restarted, "_request", request)
    assert not restarted.confirmed
    assert not restarted.set_state("Plug", False)
    await restarted.poll_status()
    assert requests == ["read_device", "read_device"]
    assert len(logger.get_last_switch_events(restarted._switch_key("Plug"), limit=10)) == 1


@pytest.mark.asyncio
async def test_homeassistant_publishes_confirmed_state_after_command(plug, monkeypatch):
    from sensorius.saiHomeAssistantMqtt import rPiHomeAssistantBridge
    ctrl, _, _ = plug
    ctrl.confirmed = ctrl.online = True
    ctrl.last_query = __import__('time').monotonic()
    ctrl.min_off_time = 0
    async def request(command):
        return {"command": [{"model": 2, "id": 2257, "ac_status": 0}]}
    async def no_wait(*args):
        pass
    monkeypatch.setattr(ctrl, "_request", request)
    monkeypatch.setattr("sensorius.saiEcowittSwitch.asyncio.sleep", no_wait)
    published = []
    bridge = rPiHomeAssistantBridge.__new__(rPiHomeAssistantBridge)
    bridge.switch_controllers = {SID: ctrl}
    async def publish(*args):
        published.append(args)
    bridge.publish_switch_state = publish
    await bridge._handle_switch_command_async(SID, ctrl.channel_id_for_label["Plug"], True)
    assert published == [(SID, ctrl.channel_id_for_label["Plug"], False)]
    assert ctrl.last_error == "Gateway did not confirm the requested plug state."
