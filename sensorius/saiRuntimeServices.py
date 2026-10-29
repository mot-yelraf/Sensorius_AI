"""Own the shared services supplied to the web runtime.

Startup supplies existing service instances; this context neither starts tasks nor
opens storage. App-state aliases keep existing route and integration callers
compatible while routes migrate away from module globals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import FastAPI

from .saiWebContracts import DashboardBinder, DashboardBroadcast, DashboardProducer, SensorMap

if TYPE_CHECKING:
    from .saiDataLogger import saiDataLogger
    from .saiAcuRite import AcuRiteUsbIngest
    from .saiEcowitt import EcowittGatewayIngest
    from .saiEmailNotifications import EmailNotificationService
    from .saiFarmOSBridge import saiFarmOSBridge
    from .saiMQTTIngest import saiMQTTIngest
    from .saiSwitch import SwitchController
    from .saiTaskSupervisor import TaskSupervisor

from .saiStats import saiStats


@dataclass
class RuntimeServices:
    """Hold one hub's services and bind their dashboard notification callback."""

    data_logger: saiDataLogger
    statter: saiStats | None = None
    sensor_map: SensorMap = field(default_factory=dict)
    switch_controllers: dict[str, SwitchController] = field(default_factory=dict)
    mqtt_ingest: saiMQTTIngest | None = None
    supervisor: TaskSupervisor | None = None
    farmos_bridge: saiFarmOSBridge | None = None
    ecowitt_service: EcowittGatewayIngest | None = None
    email_notifications: EmailNotificationService | None = None
    automation_notifications: DashboardBinder | None = None
    acurite_service: AcuRiteUsbIngest | None = None

    def __post_init__(self) -> None:
        if self.statter is None:
            self.statter = saiStats(db_path=self.data_logger.db_path)

    def attach(self, app: FastAPI) -> None:
        """Expose this context and compatibility aliases on the FastAPI app."""
        app.state.runtime_services = self
        for name in self.__dataclass_fields__:
            setattr(app.state, name, getattr(self, name))

    def bind_dashboard_broadcast(self, broadcaster: DashboardBroadcast | None) -> None:
        """Connect existing producers, including already-created system actors."""
        producers: tuple[DashboardProducer | None, ...] = (
            self.mqtt_ingest, self.ecowitt_service, self.acurite_service, self.email_notifications,
        )
        for service in producers:
            if service is not None:
                service.dashboard_broadcast = broadcaster
        for controller in self.switch_controllers.values():
            controller.dashboard_broadcast = broadcaster
        if self.automation_notifications is not None:
            self.automation_notifications.set_dashboard_broadcast(broadcaster)
