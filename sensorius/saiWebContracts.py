"""Describe the service and callback boundaries shared by web route groups.

Protocols retain keyword arguments and async return types that plain Callable
annotations would lose. Concrete runtime services are imported only for typing,
so defining these contracts does not initialize hardware, storage, or settings.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from os import PathLike
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from fastapi import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from .saiDataLogger import saiDataLogger
    from .saiEcowitt import EcowittGatewayIngest
    from .saiSensor import SensorController
    from .saiSensorSettingsManager import SensorSettingsManager
    from .saiSettings import saiSettings
    from .saiSwitchSettingsManager import SwitchSettingsManager
    from .saiTaskSupervisor import TaskSupervisor

Payload: TypeAlias = dict[str, Any]
SensorMap: TypeAlias = "dict[str, SensorController] | Iterable[SensorController]"
DashboardBroadcast: TypeAlias = Callable[[Payload], Coroutine[Any, Any, None]]
HostLock: TypeAlias = Callable[[str], asyncio.Lock]


class DashboardProducer(Protocol):
    """Accept the owning dashboard's optional async event callback."""

    dashboard_broadcast: DashboardBroadcast | None


class DashboardBinder(Protocol):
    """Bind notifications for both existing and future automation actors."""

    def set_dashboard_broadcast(self, broadcaster: DashboardBroadcast | None) -> None: ...


class SettingsFactory(Protocol):
    """Construct settings with the keyword options used by route groups."""

    def __call__(self, *, apply_live: bool = True, make_startup_backup: bool = True,
                 device_id: str | None = None) -> saiSettings: ...


class SensorSettingsFactory(Protocol):
    """Construct sensor settings with an optional root for legacy callers."""

    def __call__(self, base_dir_name: str = "sensor_settings") -> SensorSettingsManager: ...


class SwitchSettingsFactory(Protocol):
    """Construct switch settings with positional or keyword root selection."""

    def __call__(self, base_dir: str = "switch_settings") -> SwitchSettingsManager: ...


class SecretSettingsFactory(SettingsFactory, Protocol):
    """Expose settings construction and the existing secret encoding helper."""

    def obfuscate_secret(self, value: str) -> str: ...


class EcowittFactory(Protocol):
    """Construct an integration using the owning runtime's services."""

    def __call__(self, *, settings: saiSettings, data_logger: saiDataLogger,
                 supervisor: TaskSupervisor | None) -> EcowittGatewayIngest: ...


class SystemSettingsReader(Protocol):
    """Read legacy per-device settings without imposing a concrete manager."""

    def get_setting(self, device_id: str, key: str, default: str = "") -> str: ...


SystemSettingsFactory: TypeAlias = Callable[[str], SystemSettingsReader]


class ProtectedAccess(Protocol):
    """Check request access with an optional CSRF requirement."""

    def __call__(self, request: Request, *, require_csrf: bool = False) -> None: ...


class SQLiteConnect(Protocol):
    """Open a database with optional recovery context and SQLite options."""

    def __call__(self, db_path: str | PathLike[str], *, source: str = "",
                 **kwargs: Any) -> sqlite3.Connection: ...


class HostnameResolver(Protocol):
    """Resolve child devices to their physical Nodus host."""

    def __call__(self, device_id: str, system_mgr: SystemSettingsReader | None = None,
                 system_root: str | None = None, *, device_type: str | None = None,
                 sys_host_index: dict[str, str] | None = None) -> str | None: ...


class SystemRootResolver(Protocol):
    """Resolve the settings root for an optional legacy manager."""

    def __call__(self, system_mgr: SystemSettingsReader | None = None) -> str: ...


class DeviceNetworkInfo(Protocol):
    """Build network presentation for a sensor or switch."""

    def __call__(self, device_id: str, *, device_type: str) -> Awaitable[dict[str, str]]: ...


class BoardType(Protocol):
    """Describe advertised board identity with persisted settings as fallback."""

    def __call__(self, device_id: str, *, device_type: str,
                 settings_block: dict | None = None) -> str: ...


class SensorHardware(Protocol):
    """Describe sensor hardware with persisted settings as fallback."""

    def __call__(self, device_id: str, *, settings_block: dict | None = None) -> str: ...


class RestartDevice(Protocol):
    """Request a correlated restart of one resolved physical device."""

    def __call__(self, *, target_device: str, device_id: str, device_type: str,
                 restart_mode: str = "soft") -> Awaitable[tuple[bool, str]]: ...


class PushSettingsBatch(Protocol):
    """Apply an ordered batch under the physical host's shared lock."""

    def __call__(self, *, device_id: str, device_type: str, setting_file_key: str,
                 updates: list[tuple[str, str, Any]], sensor_file_name: str | None,
                 system_mgr: SystemSettingsReader | None = None,
                 system_root: str | None = None, ip_hint: str | None = None,
                 sys_host_index: dict[str, str] | None = None) -> Awaitable[bool]: ...


class ModalError(Protocol):
    """Render an error in the format requested by a modal caller."""

    def __call__(self, request: Request, message: str, *, status_code: int = 400) -> Response: ...


class ProfileLog(Protocol):
    """Record timing and optional diagnostic fields."""

    def __call__(self, route_name: str, started_mono: float, **fields: Any) -> None: ...


class WeeWXStatus(Protocol):
    """Evaluate freshness using the existing station timestamp rules."""

    def __call__(self, *, latest_timestamp: object, latest_values: dict | None,
                 update_period_sec: float, tz_name: str = "America/Denver") -> tuple[str, float | None]: ...


FloatSetting: TypeAlias = Callable[[dict, str, float], float]


class SoilOffset(Protocol):
    """Select the first persisted soil offset from compatible key names."""

    def __call__(self, section: dict, keys: tuple[str, ...], _get_float: FloatSetting,
                 default: float = 0.0) -> float: ...
