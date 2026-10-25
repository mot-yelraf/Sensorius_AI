"""Verify dashboard notification wiring across runtime and thread boundaries.

Exercise the registered websocket broadcaster with real event producers while
keeping settings, storage, and hardware isolated from the developer's hub.
"""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiEmailNotifications import AutomationNotificationService
from sensorius.saiMQTTIngest import saiMQTTIngest
from sensorius.saiSwitch import SwitchController, build_switch_controller
from testApparatus.test_ecowitt_routes import _FastStats, _Settings


@pytest.mark.asyncio
async def test_registered_dashboard_receives_controller_and_mqtt_events(tmp_path, monkeypatch):
    from sensorius import saiWebRoutes as routes

    monkeypatch.setattr(routes, "FastStats", _FastStats)
    with saiDataLogger(str(tmp_path / "events.db")) as logger:
        monkeypatch.setattr(routes, "data_logger", logger)
        controller = SwitchController.__new__(SwitchController)
        controller.switch_id = "hub"
        controller.channel_id_for_label = {"Fan": "CH1"}
        controller.data_logger = logger
        controller.get_auto_off_status = lambda name: {"auto_off_remaining_s": None}
        ingest = saiMQTTIngest.__new__(saiMQTTIngest)
        ingest.data_logger = logger
        ingest._loop = asyncio.get_running_loop()
        app = FastAPI()
        registered_routes = []
        include_router = app.include_router

        def capture_router(router, **kwargs):
            registered_routes.extend(router.routes)
            include_router(router, **kwargs)

        monkeypatch.setattr(app, "include_router", capture_router)
        app.state.switch_controllers = {"hub": controller}
        app.state.ecowitt_service = SimpleNamespace()
        await routes.register_routes(app, _Settings(), object(), object(), ingest)
        assert controller.dashboard_broadcast is app.state.switch_broadcast
        assert ingest.dashboard_broadcast is app.state.switch_broadcast
        assert app.state.ecowitt_service.dashboard_broadcast is app.state.switch_broadcast

        received = []
        connected = asyncio.Event()
        delivered = asyncio.Event()

        class Socket:
            async def accept(self):
                connected.set()

            async def receive_text(self):
                await asyncio.Future()

            async def send_json(self, payload):
                received.append(payload)
                if len(received) == 4:
                    delivered.set()

        endpoint = next(route.endpoint for route in registered_routes
                        if getattr(route, "path", None) == "/ws/switch-updates")
        task = asyncio.create_task(endpoint(Socket()))
        try:
            await asyncio.wait_for(connected.wait(), timeout=2)
            controller._log("Fan", True, source="manual/ui")
            controller._broadcast_biodynamic_transition({"transition_at": "now", "from": {}, "to": {}})
            await asyncio.to_thread(ingest._broadcast_dashboard_inventory_changed,
                                    host=" nodus-hub ", sensor_id=" sensor-1 ")
            await asyncio.to_thread(ingest._broadcast_switch_event,
                                    switch_id="remote", channel_id="CH2", label="Pump",
                                    is_on=False, source="mqtt-nodus", timestamp="observed")
            await asyncio.wait_for(delivered.wait(), timeout=2)
            by_type = {row["type"]: row for row in received}
            local = next(row for row in received if row.get("key") == "hub::CH1")
            assert local["ui_key"] == "hub::Fan"
            assert local["state"] is True and local["source"] == "manual/ui"
            assert logger.get_latest_switch_state("hub::CH1") == "On"
            remote = next(row for row in received if row.get("key") == "remote::CH2")
            assert remote == {
                "type": "switch_event", "key": "remote::CH2", "ui_key": "remote::Pump",
                "state": False, "timestamp": "observed", "source": "mqtt-nodus",
                "state_confirmed": True, "legacy_ui_key": "CH2::Pump",
            }
            assert by_type["dashboard_inventory_changed"]["host"] == "nodus-hub"
            assert by_type["dashboard_inventory_changed"]["sensor_id"] == "sensor-1"
            assert by_type["bd_transition"]["transition_at"] == "now"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("create_first", [False, True])
def test_system_notification_controller_gets_callback(create_first):
    service = AutomationNotificationService(data_logger=object())
    if create_first:
        service._get_controller()

    async def broadcast(payload):
        pass

    service.set_dashboard_broadcast(broadcast)
    assert service._get_controller().dashboard_broadcast is broadcast
    service.set_dashboard_broadcast(None)
    assert service._get_controller().dashboard_broadcast is None


def test_remote_factory_preserves_callback_without_web_runtime(tmp_path):
    async def broadcast(payload):
        pass

    with saiDataLogger(str(tmp_path / "remote.db")) as logger:
        controller = build_switch_controller(
            switch_settings={"Switch": {"TYPE": "nodus", "SWITCH_DEVICE_ID": "remote"}},
            data_logger=logger, dashboard_broadcast=broadcast,
        )
        assert controller.dashboard_broadcast is broadcast


def test_unbound_notifications_do_not_interrupt_persistence(tmp_path):
    with saiDataLogger(str(tmp_path / "unbound.db")) as logger:
        controller = SwitchController.__new__(SwitchController)
        controller.data_logger = logger
        controller.switch_id = "hub"
        controller.channel_id_for_label = {"Fan": "CH1"}
        controller._log("Fan", False)
        assert logger.get_latest_switch_state("hub::CH1") == "Off"
        ingest = saiMQTTIngest.__new__(saiMQTTIngest)
        ingest._broadcast_dashboard_inventory_changed(sensor_id="new")
        ingest._broadcast_switch_event(switch_id="remote", channel_id="CH2",
                                       label="Pump", is_on=True, source="mqtt")
