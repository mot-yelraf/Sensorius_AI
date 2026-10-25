"""Own the shared services supplied to the web runtime.

Startup supplies existing service instances; this context neither starts tasks nor
opens storage. App-state aliases keep existing route and integration callers
compatible while routes migrate away from module globals.
"""

from dataclasses import dataclass, field
from typing import Any

from .saiStats import saiStats


@dataclass
class RuntimeServices:
    """Hold one hub's services and bind their dashboard notification callback."""

    data_logger: Any
    statter: Any = None
    sensor_map: Any = field(default_factory=dict)
    switch_controllers: dict = field(default_factory=dict)
    mqtt_ingest: Any = None
    supervisor: Any = None
    farmos_bridge: Any = None
    ecowitt_service: Any = None
    email_notifications: Any = None
    automation_notifications: Any = None

    def __post_init__(self):
        if self.statter is None:
            self.statter = saiStats(db_path=self.data_logger.db_path)

    def attach(self, app) -> None:
        """Expose this context and compatibility aliases on the FastAPI app."""
        app.state.runtime_services = self
        for name in self.__dataclass_fields__:
            setattr(app.state, name, getattr(self, name))

    def bind_dashboard_broadcast(self, broadcaster) -> None:
        """Connect existing producers, including already-created system actors."""
        for service in (self.mqtt_ingest, self.ecowitt_service, self.email_notifications):
            if service is not None:
                service.dashboard_broadcast = broadcaster
        for controller in self.switch_controllers.values():
            controller.dashboard_broadcast = broadcaster
        if self.automation_notifications is not None:
            self.automation_notifications.set_dashboard_broadcast(broadcaster)
