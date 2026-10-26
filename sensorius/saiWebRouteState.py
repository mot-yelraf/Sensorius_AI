"""Own mutable caches and tracking state for one registered web application.

Each route registration creates its own state. Cache lifetimes, payload formats,
and refresh scheduling remain with the existing handlers; no app can invalidate
another app's cached dashboard or calibration data through module globals.
"""

from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class RouteState:
    """Hold independently allocated route caches and dynamic monitor tracking."""

    calibration_progress: dict = field(default_factory=dict)
    switch_status_payload: dict | None = None
    switch_status_until: float = 0.0
    sensor_ids_payload: list[str] | None = None
    sensor_ids_until: float = 0.0
    dynamic_switch_monitor_tasks: dict = field(default_factory=dict)
    profile_last_log: float = 0.0
    dashboard_json: dict = field(default_factory=dict)
    dashboard_html: OrderedDict = field(default_factory=OrderedDict)
    dashboard_inventory: tuple | None = None
    dashboard_display_settings: tuple | None = None
    biodynamic_payloads: OrderedDict = field(default_factory=OrderedDict)
    astro_payload: tuple | None = None
    sensor_locations: dict[str, tuple[float, str]] = field(default_factory=dict)
