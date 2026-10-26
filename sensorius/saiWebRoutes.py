"""FastAPI routes for Sensorius UI, settings, and device APIs.

Responsibilities:
- render core HTML pages and modal templates for the web UI
- expose REST endpoints for sensors, switches, calibration, and stats
- manage settings updates (system/sensor/switch) and onboarding flows
- integrate with MQTT ingest and data logger for live state and history
"""
from __future__ import annotations #must be first in line

from fastapi import Request, Form, Query, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse, Response, FileResponse
from fastapi.routing import APIRouter
from starlette.responses import StreamingResponse
try:
    # preferred with FastAPI
    from fastapi import WebSocket, WebSocketDisconnect
except ImportError:
    # fallback to Starlette directly
    from starlette.websockets import WebSocket, WebSocketDisconnect
import sqlite3
import copy
from pathlib import Path
from .project_paths import PROJECT_ROOT
from typing import Dict, Any, Set
from uuid import uuid4
import json
from html import escape
import socket
import asyncio
from .saiAutomationActivation import AutomationActivationError, activate_with_baselines
import subprocess
import time
import threading
import os
import sys
import platform
import plistlib
import hmac
import hashlib
import base64, zlib
import re
import tomllib
from urllib.parse import urlparse
from collections import OrderedDict
import shutil, httpx
from datetime import date, datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo, available_timezones
try:
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun, elevation as _astral_elevation, azimuth as _astral_azimuth
    from astral import moon as _astral_moon
    from astral.sidereal import lmst as _astral_lmst
except Exception:
    LocationInfo = None
    _astral_sun = None
    _astral_elevation = None
    _astral_azimuth = None
    _astral_moon = None
    _astral_lmst = None
try:
    import pwd  # POSIX only
except Exception:
    pwd = None
from .saiUtils import (
    DEFAULT_LOG_FILE,
    printDM,
    debug_enabled,
    get_timestamp,
    normalize_sensor_id,
    normalize_hostname_base,
    mdns_hostname,
)
from .saiSettings import saiSettings
from .saiInputUnits import (
    altitude_input_value,
    calibration_input_offsets,
    calibration_presentation,
    input_descriptor,
    native_input_value,
    normalize_condition_inputs,
    set_notification_unit_system,
)
from .saiDisplayUnits import (
    DISPLAY_UNIT_SYSTEMS,
    apply_display_units_to_gauge_config,
    normalize_display_unit_system,
)
from .saiEcowitt import EcowittError, EcowittGatewayIngest, ecowitt_sensor_health
from .sensor_modules.station_ecowitt import DEFAULT_POLL_INTERVAL_SEC as ECOWITT_DEFAULT_POLL_INTERVAL_SEC
from .saiRuntimePaths import resolve_runtime_base_dir
from .saiOnboardingStore import OnboardingSessionStore, OnboardingStates
from .saiOnboardingToken import OnboardingTokenManager
from .saiDataLogger import BiodynamicStorageError, saiDataLogger
try:
    from .saiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None
from .saiRuntimeServices import RuntimeServices
from .saiWebRouteState import RouteState
from .saiWebGraphs import GRAPH_MAX_POINTS_PER_SERIES, _downsample_graph_points, register_graph_routes
from .saiWebCalibration import register_calibration_routes, _SOIL_PH_CAL_KEYS
from .saiWebSwitches import SwitchRouteDependencies, register_switch_events, register_switch_routes
from .saiWebIntegrations import register_integration_routes
from .saiWebDiagnostics import (
    AdvancedStatusConfiguration, DeviceStatistics, register_statistics_diagnostics_routes,
    _DB_RETENTION_MIN_DAYS, _DB_RETENTION_MAX_DAYS, _DB_RETENTION_DEFAULT_DAYS,
    _clamp_db_retention_days, _format_stats_duration, _format_stats_age,
    _format_stats_timestamp, _format_switch_state_label, _is_true_text,
    _switch_channel_display_name, _switch_channels_from_settings,
)
from .saiHtml import (
    DASHBOARD_BACKGROUND_THEMES,
    DASHBOARD_METRIC_SETS,
    canonicalize_metric_name,
    extend_gauge_config_for_metrics,
    get_gauge_config,
    normalize_dashboard_background_theme,
    normalize_dashboard_metric_set,
    render_dashboard,
    order_dashboard_sensor_rows,
)
from .sensor_modules.station_weewx import (
    DEFAULT_DB_PATH as WEEWX_DEFAULT_DB_PATH,
    DEFAULT_MQTT_TOPIC as WEEWX_DEFAULT_MQTT_TOPIC,
    DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID,
    DEFAULT_UPDATE_PERIOD_SEC as WEEWX_DEFAULT_UPDATE_PERIOD_SEC,
    WEEWX_DISPLAY_METRICS,
    WEEWX_DISPLAY_STYLES,
    apply_weewx_station_metadata,
    weewx_battery_status,
)
from .saiFastStats import FastStats
from .saiSensorSettingsManager import SensorSettingsManager, infer_direct_local_device, is_direct_local_sensor_id
from .saiSwitchSettingsManager import SwitchSettingsManager
from .saiBiodynamics import get_biodynamic_payload, get_biodynamic_local_now, get_skyfield_runtime_if_installed
from .saiBiodynamicCalendarApp import BIODYNAMIC_CALENDAR_THEMES, normalize_biodynamic_calendar_theme
from .saiDailySummary import DailySummaryService, DEFAULT_PREWARM_DAYS, get_summary_prewarm_days
from .saiNodusOTA import NodusOTAError, NodusOTAService
from .saiEmailNotifications import EmailConfig, SMTPEmailSender, normalize_notification_rules
from .saiWeatherForecast import get_weather_forecast_payload, normalize_weather_forecast_provider
from .saiRainClimate import RainClimateService
from .saiWeatherClimate import WeatherClimateService
from .saiWeatherForecastApp import build_weather_display_forecast
from .saiWeatherForecastApp import WEATHER_THEMES, normalize_weather_theme
from .saiThemeManager import (
    MAX_UPLOAD_BYTES,
    ThemeManager,
    ThemeValidationError,
    is_custom_theme_selection,
    normalize_theme_selection,
)
from .saiAddDevice import _SENSOR_BASE_DIR, _SWITCH_BASE_DIR, _SYS_BASE_DIR, get_hub_settings_path
from . import __version__ as SAI_APP_VERSION


async def resolve_astral_location_async(settings_obj, *, persist_if_auto: bool, timeout_sec: float) -> dict:
    """Resolve location without blocking unrelated FastAPI requests."""
    return await asyncio.to_thread(
        settings_obj.resolve_astral_location,
        persist_if_auto=persist_if_auto,
        timeout_sec=timeout_sec,
    )


MODULE = "saiWebRoutes"
DEBUG = debug_enabled(MODULE)
# Compatibility aliases populated during route registration, without import-time I/O.
app = None
data_logger = None
statter = None
sensor_map = {}
switch_controllers = {}
_ALL_IANA_TIMEZONES: tuple[str, ...] = tuple(sorted(available_timezones()))


def _sqlite_connect_with_recovery(db_path: str | os.PathLike, *, source: str = "", **kwargs) -> sqlite3.Connection:
    """Open a Sensorius SQLite DB and trigger automatic recovery on corruption errors."""
    def _open_checked() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, **kwargs)
        try:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.DatabaseError:
            try:
                conn.close()
            except Exception:
                pass
            raise
        return conn

    try:
        return _open_checked()
    except sqlite3.DatabaseError as exc:
        if saiDataLogger.recover_database_after_error(db_path, exc, source=source or "saiWebRoutes.sqlite_connect"):
            return _open_checked()
        raise


def _switch_channel_id_from_identity_row(row: dict) -> str:
    """Return channel_id from a current or legacy switch_ids-style row."""
    try:
        channel_id = str((row or {}).get("channel_id", "") or "").strip()
        if channel_id:
            return channel_id
        switch_key = str((row or {}).get("switch_key", "") or "").strip()
        if "::" not in switch_key:
            return ""
        first, suffix = switch_key.split("::", 1)
        first = first.strip()
        suffix = suffix.strip()
        switch_id = str((row or {}).get("switch_id", "") or "").strip()
        label = str((row or {}).get("label", "") or "").strip()
        if switch_id and first.lower() == switch_id.lower():
            return suffix
        if label and suffix.lower() == label.lower():
            return first
        if re.match(r"^[sS]\d+-", first):
            return first
        return suffix
    except Exception:
        return ""


def _settings_base_path(base_dir: str | Path) -> Path:
    return resolve_runtime_base_dir(base_dir)


# In-memory calibration state per sensor_id.
# This never touches disk and is lost on Sensorius restart (which is fine).
# Shape:
#   {
#     "<sensor_id>": {
#         "phase": "idle"|"in_progress"|"done",
#         "calibrated": True|False|None,
#         "sample_index": int|None,
#         "sample_total": int|None,
#         "updated_at": float|None,
#     },
#   }
_SENSOR_IDS_CACHE_TTL_SEC = 10.0
_SWITCH_STATUS_CACHE_TTL_SEC: float = 1.5
def _sensor_latest_age_sec(latest_timestamp: object, *, tz_name: str = "America/Denver") -> float | None:
    if not latest_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(latest_timestamp))
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(str(tz_name or "America/Denver")))
            except Exception:
                dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds())
    except Exception:
        return None


def _weewx_measure_status_from_latest(
    *,
    latest_timestamp: object,
    latest_values: dict | None,
    update_period_sec: float,
    tz_name: str = "America/Denver",
) -> tuple[str, float | None]:
    latest_age_sec = _sensor_latest_age_sec(latest_timestamp, tz_name=tz_name)
    offline_after_sec = max(15.0, float(update_period_sec or WEEWX_DEFAULT_UPDATE_PERIOD_SEC)) * 3.0
    if latest_age_sec is not None and latest_age_sec <= offline_after_sec and bool(latest_values):
        return "online", latest_age_sec
    if latest_age_sec is not None and latest_age_sec > offline_after_sec:
        return "offline", latest_age_sec
    return "unknown", latest_age_sec


def ensure_weewx_sensor_settings(
    sensor_id: str,
    *,
    location: str = "Weather Station",
    manager: SensorSettingsManager | None = None,
) -> None:
    """Ensure the local WeeWX station has a Sensorius-owned sensor.toml."""
    sid = str(sensor_id or WEEWX_DEFAULT_SENSOR_ID).strip() or WEEWX_DEFAULT_SENSOR_ID
    mgr = manager or SensorSettingsManager("sensor_settings")
    try:
        doc = mgr.load(sid) or OrderedDict()
    except FileNotFoundError:
        mgr.seed_from_factory(sid, device="weewx", location=location)
        doc = mgr.load(sid) or OrderedDict()

    if not isinstance(doc, OrderedDict):
        doc = OrderedDict(doc)

    changed = False
    sensor_block = doc.get("Sensor")
    if not isinstance(sensor_block, dict):
        sensor_block = OrderedDict()
        doc["Sensor"] = sensor_block
        changed = True

    desired_sensor_values = {
        "TYPE": "weewx",
        "DEVICE": "weewx",
        "SENSOR_ID": sid,
    }
    for key, value in desired_sensor_values.items():
        if sensor_block.get(key) != value:
            sensor_block[key] = value
            changed = True
    if not str(sensor_block.get("LOCATION", "") or "").strip():
        sensor_block["LOCATION"] = location or "Weather Station"
        changed = True
    if apply_weewx_station_metadata(sensor_block):
        changed = True

    display_block = doc.get("Display")
    if not isinstance(display_block, dict):
        display_block = OrderedDict()
        doc["Display"] = display_block
        changed = True
    for idx, metric in enumerate(WEEWX_DISPLAY_METRICS[:6], start=1):
        key = f"METRIC_{idx}"
        if key not in display_block:
            display_block[key] = metric
            changed = True
    style_block = display_block.get("Style")
    if not isinstance(style_block, dict):
        style_block = OrderedDict()
        display_block["Style"] = style_block
        changed = True
    for idx in range(1, 7):
        key = f"METRIC_{idx}"
        if key not in style_block:
            style_block[key] = WEEWX_DISPLAY_STYLES[idx - 1]
            changed = True

    if changed:
        mgr.save(sid, doc)


def _infer_nodus_sensor_device(sensor_id: str, metrics: list[str] | None = None) -> str:
    sid = str(sensor_id or "").strip().lower()
    prefix = sid.split("-", 1)[0].strip() if "-" in sid else ""
    if prefix and prefix not in {"sensor", "nodus", "remote", "mqtt"}:
        return prefix

    metric_names = {str(metric or "").strip().lower() for metric in (metrics or [])}
    if any(name.startswith("soil") for name in metric_names):
        return "soil"
    if "co2" in metric_names:
        return "co2"
    if "air quality" in metric_names or "gas" in metric_names:
        return "aqi"
    if metric_names & {"equivalent co2", "tvoc", "voc index", "nox index"}:
        return "voc"
    if "plant vpd" in metric_names or "plant temperature" in metric_names:
        return "apvpd"
    if "light intensity" in metric_names or "estimated ppfd" in metric_names:
        return "lux"
    if "ambient vpd" in metric_names and "rel-humidity" in metric_names:
        return "aht"
    return ""


def _nodus_display_defaults_for_device(device: str, hardware: str = "") -> list[str]:
    base_device = str(device or "").split("_", 1)[0].strip().lower()
    hardware_key = str(hardware or "").strip().lower()
    if hardware_key in {"sgp30", "sgp40", "sgp41"}:
        base_device = hardware_key
    mapping: dict[str, list[str]] = {
        "apvpd": ["Ambient VPD", "Temperature", "Rel-Humidity", "Plant VPD", "Plant Temperature", "Plant Rel-Humidity"],
        "aqi": ["Air Quality", "Temperature", "Rel-Humidity", "Ambient VPD", "Dew Point Deficit", "DewVPD Risk"],
        "avpd": ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure", "Dew Point Deficit", "DewVPD Risk"],
        "aht": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
        "aht10": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
        "ahtx0": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
        "co2": ["CO2", "Temperature", "Rel-Humidity", "Ambient VPD", "Dew Point Deficit", "DewVPD Risk"],
        "lux": ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
        "veml": ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
        "voc": ["VOC Index", "NOx Index", "", "", "", ""],
        "sgp30": ["Equivalent CO2", "TVOC", "", "", "", ""],
        "sgp40": ["VOC Index", "", "", "", "", ""],
        "sgp41": ["VOC Index", "NOx Index", "", "", "", ""],
        "soil": ["Soil Moisture", "Soil Moisture Deficit", "Soil Stress Index", "Soil Temp_C", "Soil pH", "Soil EC"],
    }
    return list(mapping.get(base_device, ["", "", "", "", "", ""]))


def _normalize_six_display_metrics(metrics: list[str] | tuple[str, ...] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in metrics or []:
        metric = str(raw or "").strip()
        if not metric or metric in seen:
            continue
        ordered.append(metric)
        seen.add(metric)
        if len(ordered) >= 6:
            break
    while len(ordered) < 6:
        ordered.append("")
    return ordered[:6]


def _nodus_live_location_for_sensor(sensor_id: str, ingest) -> str:
    sid = str(sensor_id or "").strip()
    if not sid or ingest is None:
        return "Unknown"
    try:
        locations = getattr(ingest, "device_location", {}) or {}
        if not isinstance(locations, dict):
            return "Unknown"
        sid_lower = sid.lower()
        for key, value in locations.items():
            loc = str(value or "").strip()
            if _is_unknown_location_value(loc):
                continue
            key_text = str(key or "").strip()
            key_lower = key_text.lower()
            topic_parts = [part for part in key_lower.split("/") if part]
            if key_lower == sid_lower or sid_lower in topic_parts:
                return loc
    except Exception:
        pass
    return "Unknown"


def _nodus_expected_metrics_for_sensor(sensor_id: str, ingest) -> list[str]:
    sid = str(sensor_id or "").strip()
    if not sid or ingest is None:
        return []
    try:
        expected_map = getattr(ingest, "expected_gauge_map", {}) or {}
        if isinstance(expected_map, dict):
            sid_lower = sid.lower()
            for key, metrics in expected_map.items():
                if str(key or "").strip().lower() == sid_lower:
                    return _normalize_six_display_metrics(list(metrics or []))
    except Exception:
        pass
    return []


def _nodus_sensor_hardware_for_sensor(sensor_id: str, ingest) -> str:
    sid = str(sensor_id or "").strip()
    if not sid or ingest is None:
        return ""
    try:
        getter = getattr(ingest, "get_nodus_sensor_hardware", None)
        if callable(getter):
            return str(getter(sid, device_type="sensor") or "").strip()
    except Exception:
        pass
    return ""


def ensure_live_nodus_sensor_settings(
    sensor_id: str,
    *,
    manager: SensorSettingsManager,
    observed_metrics: list[str] | None = None,
    expected_metrics: list[str] | None = None,
    location: str = "Unknown",
    hardware: str = "",
) -> OrderedDict:
    """Materialize a Nodus sensor shadow for a live MQTT sensor missing sensor.toml."""
    sid = str(sensor_id or "").strip()
    if is_direct_local_sensor_id(sid):
        device = infer_direct_local_device(sid)
        manager.seed_from_factory(
            sensor_id=sid,
            device=device,
            location=location if not _is_unknown_location_value(location) else "Unknown",
        )
        manager.ensure_direct_local_type(sid)
        seeded_path = manager.get_path(sid)
        printDM(f"[edit-sensor] Seeded missing direct local sensor settings for {sid} at {seeded_path}", location=MODULE)
        return manager.load(sid)

    device = _infer_nodus_sensor_device(sid, list(expected_metrics or []) or list(observed_metrics or []))
    base_dir = Path(getattr(manager, "base_dir", "sensor_settings"))
    nodus_dir = base_dir / "factory_nodus"
    use_soil = device == "soil" or sid.lower().startswith("soil")
    tpl_path = nodus_dir / ("sensor_soil.toml.def" if use_soil else "sensor_i2c.toml.def")

    if tpl_path.exists():
        doc = manager._parse_toml_from_disk(tpl_path)
    else:
        doc = OrderedDict()
        doc["Sensor"] = OrderedDict()
        doc["Calibration"] = OrderedDict()
        doc["Display"] = OrderedDict()
        doc["Display"]["Style"] = OrderedDict()

    if not isinstance(doc, OrderedDict):
        doc = OrderedDict(doc)

    sensor_block = doc.get("Sensor")
    if not isinstance(sensor_block, dict):
        sensor_block = OrderedDict()
        doc["Sensor"] = sensor_block
    sensor_block["TYPE"] = "nodus"
    sensor_block["DEVICE"] = device
    sensor_block["SENSOR_ID"] = sid
    sensor_block["LOCATION"] = location if not _is_unknown_location_value(location) else "Unknown"
    if str(hardware or "").strip():
        sensor_block["HARDWARE"] = str(hardware or "").strip()

    display_block = doc.get("Display")
    if not isinstance(display_block, dict):
        display_block = OrderedDict()
        doc["Display"] = display_block
    preferred_metrics = _normalize_six_display_metrics(expected_metrics)
    if not any(preferred_metrics):
        preferred_metrics = _nodus_display_defaults_for_device(device, hardware)
    if not any(preferred_metrics):
        preferred_metrics = _normalize_six_display_metrics(observed_metrics)
    for idx, metric in enumerate(_normalize_six_display_metrics(preferred_metrics), start=1):
        display_block[f"METRIC_{idx}"] = metric

    style_block = display_block.get("Style")
    if not isinstance(style_block, dict):
        style_block = OrderedDict()
        display_block["Style"] = style_block
    for idx in range(1, 7):
        style_block.setdefault(f"METRIC_{idx}", "Graph24hr")

    manager.save(sid, doc)
    seeded_path = manager.get_path(sid)
    printDM(f"[edit-sensor] Seeded missing Nodus sensor settings for {sid} at {seeded_path}", location=MODULE)
    return manager.load(sid)


_CDP_DEBUG_MIN_INTERVAL_SEC: float = 30.0
_DASHBOARD_JSON_CACHE_TTL_SEC: float = 2.0
_DASHBOARD_HTML_CACHE_MAX_ENTRIES: int = 2
_DASHBOARD_INVENTORY_CACHE_TTL_SEC: float = 2.0
_DASHBOARD_DISPLAY_SETTINGS_CACHE_TTL_SEC: float = 2.0
_BIODYNAMIC_PAYLOAD_CACHE_TTL_SEC: float = 60.0
_BIODYNAMIC_PAYLOAD_CACHE_MAX: int = 64
_NODUS_CONFIG_ACK_TIMEOUT_SEC: float = 5.0
_NODUS_CONFIG_RESULT_TIMEOUT_SEC: float = 20.0
_NODUS_CALIBRATION_ACK_TIMEOUT_SEC: float = 8.0
_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC: float = 20.0
_ASTRO_PAYLOAD_CACHE_TTL_SEC: float = 60.0
_DASHBOARD_EXTRAS_FAST_WAIT_SEC: float = 0.05
_SENSOR_LOCATION_CACHE_TTL_SEC: float = 5.0


def _dashboard_json_safe(value):
    """Return a JSONResponse-safe dashboard payload fragment."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _dashboard_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dashboard_json_safe(v) for v in value]
    return value


def _dashboard_payload_is_warming(payload: object) -> bool:
    """Return whether a dashboard extra is only a cold-start placeholder."""
    if not isinstance(payload, dict):
        return False
    return str(payload.get("reason") or "").strip() == "warming" or str(
        payload.get("cache_status") or ""
    ).strip() == "warming"


def _is_unknown_location_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "unknown", "n/a", "na", "none", "-"}

async def register_routes(app, settings, net_mgr, gc_mgr, mqtt_ingest):
    """Register Sensorius UI, API, integration, and diagnostics routes."""
    # Retain the app binding for integrations that still use the web runtime.
    globals()["app"] = app
    services = getattr(app.state, "runtime_services", None)
    if services is None:
        # Support standalone route registration and older integrations.
        logger = getattr(app.state, "data_logger", None)
        if logger is None:
            logger = saiDataLogger()
        services = RuntimeServices(
            data_logger=logger,
            statter=getattr(app.state, "statter", None),
            sensor_map=getattr(app.state, "sensor_map", {}),
            switch_controllers=getattr(app.state, "switch_controllers", {}),
            mqtt_ingest=mqtt_ingest,
            supervisor=getattr(app.state, "supervisor", None),
            farmos_bridge=getattr(app.state, "farmos_bridge", None),
            ecowitt_service=getattr(app.state, "ecowitt_service", None),
            email_notifications=getattr(app.state, "email_notifications", None),
            automation_notifications=getattr(app.state, "automation_notifications", None),
        )
    services.attach(app)
    data_logger = services.data_logger
    statter = services.statter
    sensor_map = services.sensor_map
    switch_controllers = services.switch_controllers
    mqtt_ingest = services.mqtt_ingest
    globals().update(data_logger=data_logger, statter=statter,
                     sensor_map=sensor_map, switch_controllers=switch_controllers)
    if not str(getattr(app.state, "ui_runtime_instance_id", "") or "").strip():
        app.state.ui_runtime_instance_id = uuid4().hex
    route_state = RouteState()
    app.state.route_state = route_state
    router = APIRouter()
    set_notification_unit_system(settings.get_setting("Display", "unit_system", "Imperial"))
    theme_manager = getattr(app.state, "theme_manager", None)
    if not isinstance(theme_manager, ThemeManager):
        theme_manager = ThemeManager(resolve_runtime_base_dir(getattr(saiSettings, "DEFAULT_BASE_DIR", "system_settings")).parent)
        app.state.theme_manager = theme_manager
    route_state.biodynamic_payloads.clear()
    route_state.dashboard_html.clear()
    biodynamic_payload_tasks: dict[str, asyncio.Task] = {}
    biodynamic_payload_cache_lock = threading.Lock()
    biodynamic_payload_cache_generation = 0
    biodynamic_summary_tasks: dict[str, asyncio.Task] = {}
    astro_payload_tasks: dict[str, asyncio.Task] = {}
    astro_payload_cache_lock = threading.Lock()
    astro_payload_cache_generation = 0
    main_loop = asyncio.get_running_loop()
    rain_climate_service = RainClimateService(
        lambda: saiSettings(make_startup_backup=False, apply_live=False),
        resolve_runtime_base_dir(getattr(saiSettings, "DEFAULT_BASE_DIR", "system_settings")).parent
        / "weather_cache" / "rain_climate.json",
    )
    app.state.rain_climate_service = rain_climate_service
    app.add_event_handler("shutdown", rain_climate_service.close)

    @router.get("/api/rain-climate")
    async def api_rain_climate():
        """Return cached rain gauge limits while history loads in the background."""
        return rain_climate_service.snapshot()

    weather_climate_service = WeatherClimateService(
        lambda: saiSettings(make_startup_backup=False, apply_live=False),
        rain_climate_service.cache_path.with_name("weather_climate.json"),
    )
    app.state.weather_climate_service = weather_climate_service
    app.add_event_handler("shutdown", weather_climate_service.close)

    @router.get("/api/weather-climate")
    async def api_weather_climate():
        """Return historical daily means and extremes for today's date at the hub location."""
        return weather_climate_service.snapshot()

    daily_summary_service = DailySummaryService(settings=settings, data_logger=data_logger)
    app.state.daily_summary_service = daily_summary_service
    ota_service = getattr(app.state, "nodus_ota_service", None)
    if ota_service is None:
        ota_service = NodusOTAService(settings=settings, mqtt_ingest=mqtt_ingest)
        app.state.nodus_ota_service = ota_service
    onboarding_store = OnboardingSessionStore(base_dir=getattr(saiSettings, "DEFAULT_BASE_DIR", "system_settings"))
    onboarding_tokens = OnboardingTokenManager(onboarding_store, default_ttl_sec=600)
    _v2_session_tasks: Dict[str, asyncio.Task] = {}
    default_sensor_id = settings.get_all_sensor_ids()[0] if settings.get_all_sensor_ids() else ""
    # On startup
    fastStats = FastStats(data_logger, statter, hz=1.0)
    asyncio.create_task(fastStats.start())
    if not getattr(app.state, "_faststats_shutdown_registered", False):
        app.add_event_handler("shutdown", fastStats.stop)
        app.state._faststats_shutdown_registered = True

    def _ui_profile_log(route_name: str, started_mono: float, **fields) -> None:
        if not DEBUG:
            return
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        extras = [f"{key}={value}" for key, value in fields.items() if value not in (None, "")]
        detail = f" {' '.join(extras)}" if extras else ""
        printDM(f"[webui-profile] {route_name} took {elapsed_ms:.1f}ms{detail}", location=MODULE)

    def _invalidate_dashboard_caches() -> None:
        nonlocal biodynamic_payload_cache_generation, astro_payload_cache_generation
        rain_climate_service.invalidate()
        weather_climate_service.invalidate()
        route_state.dashboard_json.clear()
        route_state.dashboard_html.clear()
        route_state.dashboard_inventory = None
        route_state.dashboard_display_settings = None
        with astro_payload_cache_lock:
            astro_payload_cache_generation += 1
            route_state.astro_payload = None
            astro_payload_tasks.clear()
        route_state.sensor_ids_payload = None
        route_state.sensor_ids_until = 0.0
        with biodynamic_payload_cache_lock:
            biodynamic_payload_cache_generation += 1
            route_state.biodynamic_payloads.clear()
            biodynamic_payload_tasks.clear()
        try:
            from .saiBiodynamics import clear_biodynamic_payload_cache
            clear_biodynamic_payload_cache()
        except Exception:
            pass
        try:
            calendar_service = getattr(app.state, "biodynamic_calendar_service", None)
            if calendar_service is not None:
                calendar_service.clear_dynamic_cache()
        except Exception:
            pass

    def _wants_modal_json(request: Request) -> bool:
        accept = str(request.headers.get("accept", "") or "").lower()
        requested_with = str(request.headers.get("x-requested-with", "") or "").lower()
        return requested_with == "xmlhttprequest" or "application/json" in accept

    def _modal_error_response(request: Request, message: str, *, status_code: int = 400):
        if _wants_modal_json(request):
            return JSONResponse({"ok": False, "error": str(message or "")}, status_code=status_code)
        return PlainTextResponse(str(message or ""), status_code=status_code)

    def _get_sensor_settings_manager() -> SensorSettingsManager | None:
        cached = getattr(app.state, "_sensor_settings_manager", None)
        if cached is not None:
            return cached
        mgr = None
        try:
            mgr = SensorSettingsManager("sensor_settings")
        except TypeError:
            try:
                mgr = SensorSettingsManager()
            except Exception:
                mgr = None
        except Exception:
            try:
                mgr = SensorSettingsManager()
            except Exception:
                mgr = None
        app.state._sensor_settings_manager = mgr
        return mgr

    def _get_switch_settings_manager() -> SwitchSettingsManager | None:
        cached = getattr(app.state, "_switch_settings_manager", None)
        if cached is not None:
            return cached
        try:
            mgr = SwitchSettingsManager("switch_settings")
        except Exception:
            mgr = None
        app.state._switch_settings_manager = mgr
        return mgr

    def _get_cached_display_settings() -> dict[str, object]:
        now_mono = time.monotonic()
        if route_state.dashboard_display_settings and route_state.dashboard_display_settings[0] > now_mono:
            return dict(route_state.dashboard_display_settings[1])
        fresh_settings = saiSettings(apply_live=False)
        payload = {
            "pressure_altitude": fresh_settings.get_setting("Astral", "ALTITUDE", ""),
            "gauge_size": fresh_settings.get_setting("Display", "gauge_size") or "Small",
            "display_style": fresh_settings.get_setting("Display", "display_style") or "Gauge",
            "unit_system": normalize_display_unit_system(
                fresh_settings.get_setting("Display", "unit_system", "Imperial")
            ),
            "sensor_row_order": fresh_settings.get_setting("Display", "sensor_row_order", "location_device"),
            "switch_row_order": fresh_settings.get_setting("Display", "switch_row_order", "location_device"),
            "switch_card_order": fresh_settings.get_setting("Display", "switch_card_order", []),
            "metric_set": normalize_dashboard_metric_set(
                fresh_settings.get_setting("Display", "metric_set", "Pick 6")
            ),
            "dashboard_background_theme": normalize_theme_selection(
                theme_manager,
                "sensorius",
                fresh_settings.get_setting("Display", "background_theme", "leaf"),
                "leaf",
                normalize_dashboard_background_theme,
            ),
            "weather_forecast_provider": normalize_weather_forecast_provider(
                fresh_settings.get_setting("WeatherForecast", "PROVIDER", "met_no")
            ),
            "weather_forecast_theme": normalize_theme_selection(
                theme_manager,
                "caelus",
                fresh_settings.get_setting("WeatherForecast", "THEME", "pollinator"),
                "pollinator",
                normalize_weather_theme,
            ),
            "gauge_config": apply_display_units_to_gauge_config(
                get_gauge_config(),
                fresh_settings.get_setting("Display", "unit_system", "Imperial"),
            ),
        }
        set_notification_unit_system(str(payload["unit_system"]))
        route_state.dashboard_display_settings = (
            now_mono + _DASHBOARD_DISPLAY_SETTINGS_CACHE_TTL_SEC,
            dict(payload),
        )
        return payload

    def _get_cached_biodynamic_payload(anchor: date, generation: int | None = None) -> dict[str, object]:
        cache_key = anchor.isoformat()
        now_mono = time.monotonic()
        with biodynamic_payload_cache_lock:
            cached = route_state.biodynamic_payloads.get(cache_key)
            if cached and cached[0] > now_mono:
                route_state.biodynamic_payloads.move_to_end(cache_key)
                return copy.deepcopy(cached[1])
        calendar_service = getattr(app.state, "biodynamic_calendar_service", None)
        if calendar_service is not None and hasattr(calendar_service.settings, "resolve_astral_location"):
            config, _location = calendar_service.location()
            payload = (
                calendar_service.build_month_sync(anchor, config)
                if config is not None
                else {"ok": False, "reason": "location_unavailable", "calendar": []}
            )
        else:
            payload = get_biodynamic_payload(anchor)
        with biodynamic_payload_cache_lock:
            if generation is None or generation == biodynamic_payload_cache_generation:
                route_state.biodynamic_payloads[cache_key] = (
                    time.monotonic() + _BIODYNAMIC_PAYLOAD_CACHE_TTL_SEC,
                    copy.deepcopy(payload),
                )
                route_state.biodynamic_payloads.move_to_end(cache_key)
                while len(route_state.biodynamic_payloads) > _BIODYNAMIC_PAYLOAD_CACHE_MAX:
                    route_state.biodynamic_payloads.popitem(last=False)
        return copy.deepcopy(payload)

    def _get_stale_biodynamic_payload(anchor: date) -> dict[str, object] | None:
        cache_key = anchor.isoformat()
        with biodynamic_payload_cache_lock:
            cached = route_state.biodynamic_payloads.get(cache_key)
            if not cached:
                return None
            route_state.biodynamic_payloads.move_to_end(cache_key)
            return copy.deepcopy(cached[1])

    def _warming_biodynamic_payload(anchor: date) -> dict[str, object]:
        return {
            "ok": False,
            "reason": "warming",
            "tz": "",
            "source": "skyfield",
            "month_label": anchor.strftime("%B %Y"),
            "weekday_labels": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "current": {},
            "upcoming": [],
            "calendar": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cache_status": "warming",
        }

    async def _get_cached_biodynamic_payload_async(
        anchor: date,
        *,
        allow_stale: bool = False,
        cold_wait_sec: float = _DASHBOARD_EXTRAS_FAST_WAIT_SEC,
    ) -> dict[str, object]:
        cache_key = anchor.isoformat()
        now_mono = time.monotonic()
        with biodynamic_payload_cache_lock:
            cached = route_state.biodynamic_payloads.get(cache_key)
            if cached and cached[0] > now_mono:
                route_state.biodynamic_payloads.move_to_end(cache_key)
                return copy.deepcopy(cached[1])
            generation = biodynamic_payload_cache_generation

        stale_payload = _get_stale_biodynamic_payload(anchor)
        task = biodynamic_payload_tasks.get(cache_key)
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(_get_cached_biodynamic_payload, anchor, generation))
            biodynamic_payload_tasks[cache_key] = task

            def _discard_biodynamic_task(done_task, key=cache_key) -> None:
                if biodynamic_payload_tasks.get(key) is done_task:
                    biodynamic_payload_tasks.pop(key, None)
                try:
                    if not done_task.cancelled():
                        done_task.exception()
                except Exception:
                    pass

            task.add_done_callback(_discard_biodynamic_task)

        if allow_stale and stale_payload is not None:
            return stale_payload
        if allow_stale:
            try:
                return copy.deepcopy(await asyncio.wait_for(asyncio.shield(task), timeout=float(cold_wait_sec)))
            except asyncio.TimeoutError:
                return _warming_biodynamic_payload(anchor)
            except Exception:
                return _warming_biodynamic_payload(anchor)
        return copy.deepcopy(await task)

    def _request_biodynamic_summary_window(today_local: date) -> tuple[str, float]:
        window_start = today_local.replace(day=1)
        month_key = window_start.isoformat()
        if getattr(app.state, "_biodynamic_summary_window_month", "") == month_key:
            return "cached", 0.0

        started = time.monotonic()
        task = biodynamic_summary_tasks.get(month_key)
        if task is not None and not task.done():
            return "warming", 0.0

        async def _warm_summary_window() -> None:
            try:
                await asyncio.to_thread(
                    daily_summary_service.ensure_summaries_for_window,
                    window_start,
                    days=get_summary_prewarm_days(),
                    refresh_start=True,
                )
                setattr(app.state, "_biodynamic_summary_window_month", month_key)
                if DEBUG:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    printDM(
                        f"[biodynamic-summary] warmed {month_key} in {elapsed_ms:.1f}ms",
                        location=MODULE,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if DEBUG:
                    printDM(f"[biodynamic-summary] warm skipped for {month_key}: {exc}", location=MODULE)

        task = asyncio.create_task(_warm_summary_window(), name=f"BiodynamicSummaryWarm:{month_key}")
        biodynamic_summary_tasks[month_key] = task

        def _discard_summary_task(done_task, key=month_key) -> None:
            if biodynamic_summary_tasks.get(key) is done_task:
                biodynamic_summary_tasks.pop(key, None)
            try:
                if not done_task.cancelled():
                    done_task.exception()
            except Exception:
                pass

        task.add_done_callback(_discard_summary_task)
        return "scheduled", (time.monotonic() - started) * 1000.0

    def _get_cached_astro_payload(generation: int | None = None) -> dict[str, object]:
        now_mono = time.monotonic()
        with astro_payload_cache_lock:
            cached = route_state.astro_payload
            if cached and cached[0] > now_mono:
                return dict(cached[1])
        payload = _build_astro_payload()
        with astro_payload_cache_lock:
            if generation is None or generation == astro_payload_cache_generation:
                route_state.astro_payload = (
                    time.monotonic() + _ASTRO_PAYLOAD_CACHE_TTL_SEC,
                    dict(payload),
                )
        return dict(payload)

    def _get_stale_astro_payload() -> dict[str, object] | None:
        with astro_payload_cache_lock:
            cached = route_state.astro_payload
            if not cached:
                return None
            return dict(cached[1])

    def _warming_astro_payload() -> dict[str, object]:
        return {
            "ok": False,
            "reason": "warming",
            "lat": None,
            "lon": None,
            "tz": "",
            "sunrise": "",
            "sunset": "",
            "sun_noon": "",
            "sun_points": [],
            "moon_points": [],
            "moon_phase_value": None,
            "moon_phase_label": "",
            "moon_lit_pct": None,
            "moon_rise": "",
            "moon_set": "",
            "moon_rise_today": "",
            "moon_set_today": "",
            "moon_declination": None,
            "moon_position_source": "",
            "moon_next_phase_label": "",
            "moon_next_phase_date": "",
            "moon_visible_angle": None,
            "moon_reference_angle": None,
            "position_29d": [],
            "cache_status": "warming",
        }

    async def _get_cached_astro_payload_async(
        *,
        allow_stale: bool = False,
        cold_wait_sec: float = _DASHBOARD_EXTRAS_FAST_WAIT_SEC,
    ) -> dict[str, object]:
        task_key = "default"
        now_mono = time.monotonic()
        with astro_payload_cache_lock:
            cached = route_state.astro_payload
            if cached and cached[0] > now_mono:
                return dict(cached[1])
            generation = astro_payload_cache_generation

        stale_payload = _get_stale_astro_payload()
        task = astro_payload_tasks.get(task_key)
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(_get_cached_astro_payload, generation))
            astro_payload_tasks[task_key] = task

            def _discard_astro_task(done_task, key=task_key) -> None:
                if astro_payload_tasks.get(key) is done_task:
                    astro_payload_tasks.pop(key, None)
                try:
                    if not done_task.cancelled():
                        done_task.exception()
                except Exception:
                    pass

            task.add_done_callback(_discard_astro_task)

        if allow_stale and stale_payload is not None:
            return stale_payload
        if allow_stale:
            try:
                return dict(await asyncio.wait_for(asyncio.shield(task), timeout=float(cold_wait_sec)))
            except asyncio.TimeoutError:
                return _warming_astro_payload()
            except Exception:
                return _warming_astro_payload()
        return dict(await task)

    def _is_recent_sensor(sid: str, window: timedelta = timedelta(minutes=10)) -> bool:
        ts = services.data_logger.get_latest_timestamp(sid)
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            return False
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return dt >= (now - window)

    def _filter_recent_sensors(sensor_ids: list[str], window: timedelta = timedelta(minutes=10)) -> list[str]:
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in sensor_ids or []:
            sid = str(raw or "").strip()
            key = sid.lower()
            if not sid or key in seen:
                continue
            seen.add(key)
            clean_ids.append(sid)
        if not clean_ids:
            return []

        try:
            timestamps = services.data_logger.get_latest_timestamps(clean_ids)
        except Exception:
            return [sid for sid in clean_ids if _is_recent_sensor(sid, window=window)]

        result: list[str] = []
        for sid in clean_ids:
            ts = timestamps.get(sid)
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            if dt >= (now - window):
                result.append(sid)
        return result

    _METRIC_POSITION_SECTION = "MetricPosition"

    def _strip_local_suffix(name: str) -> str:
        return normalize_hostname_base(name)

    def _is_switch_id(name: str) -> bool:
        s = (name or "").strip()
        n = s.lower()
        if n.startswith("switch_") or n.startswith("switch-"):
            return True
        return bool(re.match(r"^S\d+-[A-Za-z0-9][A-Za-z0-9._-]*$", s))

    def _is_valid_sensor_id(name: str) -> bool:
        s = (name or "").strip()
        if not s:
            return False
        if _is_switch_id(s):
            return False
        return bool(re.match(r"^[A-Za-z0-9._-]+$", s))

    def _sensor_shadow_is_remote_nodus(sid: str) -> bool:
        sid_text = str(sid or "").strip()
        if not sid_text:
            return False
        if is_direct_local_sensor_id(sid_text):
            return False
        mgr = _get_sensor_settings_manager()
        if mgr is None:
            return False
        try:
            doc = mgr.load(sid_text) or {}
        except Exception:
            return False
        sensor_block = doc.get("Sensor") if isinstance(doc, dict) else {}
        if not isinstance(sensor_block, dict):
            return False
        sensor_type = str(sensor_block.get("TYPE") or "").strip().lower()
        return sensor_type in {"nodus", "picow", "pico2w", "remote", "mqtt"}

    def _get_remote_nodus_sensor_shadow_ids() -> list[str]:
        mgr = _get_sensor_settings_manager()
        if mgr is None or not hasattr(mgr, "list_ids"):
            return []
        out: list[str] = []
        try:
            ids = mgr.list_ids() or []
        except Exception:
            return []
        for raw_sid in ids:
            sid = str(raw_sid or "").strip()
            if not _is_valid_sensor_id(sid):
                continue
            if _sensor_shadow_is_remote_nodus(sid):
                out.append(sid)
        return out

    def _mqtt_sensor_looks_live(sid: str) -> bool:
        sid_text = str(sid or "").strip()
        if not sid_text:
            return False
        ing = services.mqtt_ingest
        if ing is None:
            return False
        try:
            getter = getattr(ing, "get_nodus_liveness", None)
            if not callable(getter):
                return False
            snapshot = getter(sid_text) or {}
        except Exception:
            return False
        if not isinstance(snapshot, dict):
            return False
        state = str(snapshot.get("state") or "").strip().lower()
        if state in {"online", "degraded"}:
            return True
        for key in ("last_report_s", "last_heartbeat_s", "last_seen_s"):
            raw_age = snapshot.get(key)
            if raw_age is None:
                continue
            try:
                if float(raw_age) <= 600.0:
                    return True
            except Exception:
                continue
        return False

    def _is_dashboard_visible_sensor(sid: str) -> bool:
        if _is_recent_sensor(sid):
            return True
        return _sensor_shadow_is_remote_nodus(sid) and _mqtt_sensor_looks_live(sid)

    def _normalize_available_sensor_ids(sensor_ids_local: list[str], discovered: list[str]) -> list[str]:
        order_preserve: list[str] = []
        seen_local: set[str] = set()

        def _add(x: str):
            if x not in seen_local:
                seen_local.add(x)
                order_preserve.append(x)

        for sid in sensor_ids_local:
            if _is_valid_sensor_id(sid):
                _add(sid)

        for host in discovered or []:
            base = _strip_local_suffix(host)
            if _is_valid_sensor_id(base):
                _add(base)

        return sorted(order_preserve)

    def _get_dashboard_sensor_map():
        sm = services.sensor_map
        return sm

    def _get_local_sensor_ids() -> list[str]:
        from collections.abc import Iterable

        sm = _get_dashboard_sensor_map()
        if isinstance(sm, dict):
            return [k for k in sm.keys() if isinstance(k, str) and k.strip()]
        if isinstance(sm, Iterable):
            ids = []
            for s in sm:
                sid = getattr(s, "sensor_id", None)
                if isinstance(sid, str) and sid.strip():
                    ids.append(sid)
            return ids
        return []

    def _current_dashboard_sensor_ids() -> list[str]:
        sensors_from_logger = services.data_logger.get_available_sensors()
        mqtt_discovered = services.mqtt_ingest.get_known_devices() if services.mqtt_ingest else []
        merged_local = list(_get_local_sensor_ids() or [])
        for sid in (sensors_from_logger or []):
            if sid and sid not in merged_local:
                merged_local.append(sid)
        for sid in _get_remote_nodus_sensor_shadow_ids():
            if sid and sid not in merged_local:
                merged_local.append(sid)
        available = _normalize_available_sensor_ids(merged_local, list(mqtt_discovered or []))
        return [sid for sid in available if _is_dashboard_visible_sensor(sid)]

    def _load_metric_position_section() -> OrderedDict[str, int]:
        fresh_settings = saiSettings(apply_live=False)
        raw = fresh_settings.get_section(_METRIC_POSITION_SECTION, reload_if_changed=True)
        out: OrderedDict[str, int] = OrderedDict()
        if not isinstance(raw, dict):
            return out
        for sensor_id, pos_raw in raw.items():
            sid = str(sensor_id or "").strip()
            if not sid:
                continue
            try:
                pos = int(pos_raw)
            except Exception:
                continue
            if pos < 1:
                continue
            out[sid] = pos
        return out

    def _save_metric_position_map(position_map: OrderedDict[str, int]) -> bool:
        desired: OrderedDict[str, int] = OrderedDict()
        seen: set[str] = set()
        used_positions: set[int] = set()
        for raw_sid, raw_pos in (position_map or {}).items():
            sid = str(raw_sid or "").strip()
            if not sid or sid in seen:
                continue
            try:
                pos = int(raw_pos)
            except Exception:
                continue
            if pos < 1 or pos in used_positions:
                continue
            seen.add(sid)
            used_positions.add(pos)
            desired[sid] = pos

        fresh_settings = saiSettings(apply_live=False)
        current = fresh_settings.get_section(_METRIC_POSITION_SECTION, reload_if_changed=True)
        current_items = list(current.items()) if isinstance(current, dict) else []
        desired_items = list(desired.items())
        if current_items == desired_items:
            return False

        if desired:
            fresh_settings.settings[_METRIC_POSITION_SECTION] = desired
        else:
            fresh_settings.settings.pop(_METRIC_POSITION_SECTION, None)
        fresh_settings._dirty = True
        fresh_settings.save_settings()
        _invalidate_dashboard_caches()
        return True

    def _persist_visible_metric_order(sensor_ids: list[str]) -> bool:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in sensor_ids or []:
            sid = str(raw or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            deduped.append(sid)

        stored = _load_metric_position_section()
        hidden_by_pos: dict[int, str] = {}
        taken_positions: set[int] = set()
        for sid, pos in stored.items():
            if sid in seen:
                continue
            hidden_by_pos[pos] = sid
            taken_positions.add(pos)

        next_pos = 1
        desired: dict[str, int] = {}
        for sid in deduped:
            while next_pos in taken_positions:
                next_pos += 1
            desired[sid] = next_pos
            taken_positions.add(next_pos)
            next_pos += 1

        merged: list[tuple[str, int]] = list(desired.items()) + [
            (sid, pos) for pos, sid in hidden_by_pos.items()
        ]
        merged.sort(key=lambda item: (item[1], item[0].lower()))
        return _save_metric_position_map(OrderedDict(merged))

    def _order_sensor_ids_by_metric_position(sensor_ids: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in sensor_ids or []:
            sid = str(raw or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            deduped.append(sid)

        stored = _load_metric_position_section()
        with_saved = [sid for sid in deduped if sid in stored]
        with_saved.sort(key=lambda sid: (stored.get(sid, 10**9), sid.lower()))
        new_ids = [sid for sid in deduped if sid not in stored]
        return with_saved + new_ids

    def _effective_dashboard_row_order(row_ids, locations=None):
        ordered = _order_sensor_ids_by_metric_position(row_ids)
        if _get_cached_display_settings().get("sensor_row_order") == "existing":
            return ordered
        if locations is None:
            mgr = _get_sensor_settings_manager()
            locations = {}
            for sid in ordered:
                if sid == "__switches__":
                    continue
                try:
                    locations[sid] = mgr.get_setting(sid, "Sensor.LOCATION", "Unknown") if mgr else "Unknown"
                except FileNotFoundError:
                    locations[sid] = "Unknown"
                cached = route_state.sensor_locations.get(sid)
                if cached:
                    locations[sid] = cached[1]
        return order_dashboard_sensor_rows(ordered, locations)

    def _traditional_full_moon_name(phase_date) -> str:
        names_by_month = {
            1: "Wolf Moon",
            2: "Snow Moon",
            3: "Worm Moon",
            4: "Pink Moon",
            5: "Flower Moon",
            6: "Strawberry Moon",
            7: "Buck Moon",
            8: "Sturgeon Moon",
            9: "Harvest Moon",
            10: "Hunter's Moon",
            11: "Beaver Moon",
            12: "Cold Moon",
        }
        return names_by_month.get(getattr(phase_date, "month", None), "Full Moon")

    def _moon_phase_name(phase_val: float, phase_date=None) -> str:
        p = phase_val % 28.0

        def _circular_dist(a: float, b: float, cycle: float = 28.0) -> float:
            d = abs(a - b) % cycle
            return min(d, cycle - d)

        if _circular_dist(p, 0.0) <= 1.0:
            return "New Moon"
        if _circular_dist(p, 7.0) <= 1.0:
            return "1st Quarter"
        if _circular_dist(p, 14.0) <= 1.0:
            return _traditional_full_moon_name(phase_date)
        if _circular_dist(p, 21.0) <= 1.0:
            return "3rd Quarter"
        if 1.0 < p < 6.0:
            return "Waxing Crescent"
        if 8.0 < p < 13.0:
            return "Waxing Gibbous"
        if 15.0 < p < 20.0:
            return "Waning Gibbous"
        return "Waning Crescent"

    def _moon_local_canvas_angle(moon_az: float, moon_el: float, sun_az: float, sun_el: float) -> float | None:
        def _unit_from_az_el(az_deg: float, el_deg: float) -> tuple[float, float, float]:
            az = math.radians(az_deg)
            el = math.radians(el_deg)
            return (
                math.cos(el) * math.sin(az),
                math.cos(el) * math.cos(az),
                math.sin(el),
            )

        def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
            return (a[0] * b[0]) + (a[1] * b[1]) + (a[2] * b[2])

        def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
            return (
                (a[1] * b[2]) - (a[2] * b[1]),
                (a[2] * b[0]) - (a[0] * b[2]),
                (a[0] * b[1]) - (a[1] * b[0]),
            )

        def _normalized(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
            mag = math.sqrt(_dot(v, v))
            if not math.isfinite(mag) or mag < 1e-9:
                return None
            return (v[0] / mag, v[1] / mag, v[2] / mag)

        if not all(math.isfinite(v) for v in (moon_az, moon_el, sun_az, sun_el)):
            return None

        moon_vec = _unit_from_az_el(moon_az, moon_el)
        sun_vec = _unit_from_az_el(sun_az, sun_el)
        bright_vec = _normalized(tuple(sun_vec[i] - (_dot(sun_vec, moon_vec) * moon_vec[i]) for i in range(3)))
        if bright_vec is None:
            return None

        zenith = (0.0, 0.0, 1.0)
        screen_up = _normalized(tuple(zenith[i] - (_dot(zenith, moon_vec) * moon_vec[i]) for i in range(3)))
        if screen_up is None:
            north = (0.0, 1.0, 0.0)
            screen_up = _normalized(tuple(north[i] - (_dot(north, moon_vec) * moon_vec[i]) for i in range(3)))
        if screen_up is None:
            return None

        screen_right = _normalized(_cross(moon_vec, screen_up))
        if screen_right is None:
            return None

        canvas_x = _dot(bright_vec, screen_right)
        canvas_y = -_dot(bright_vec, screen_up)
        return (math.degrees(math.atan2(canvas_y, canvas_x)) + 360.0) % 360.0

    def _build_astro_payload() -> dict[str, object]:
        out: dict[str, object] = {
            "ok": False,
            "lat": None,
            "lon": None,
            "tz": "",
            "sunrise": "",
            "sunset": "",
            "sun_noon": "",
            "sun_points": [],
            "moon_points": [],
            "moon_phase_value": None,
            "moon_phase_label": "",
            "moon_lit_pct": None,
            "moon_rise": "",
            "moon_set": "",
            "moon_rise_today": "",
            "moon_set_today": "",
            "moon_declination": None,
            "moon_position_source": "",
            "moon_next_phase_label": "",
            "moon_next_phase_date": "",
            "moon_visible_angle": None,
            "moon_reference_angle": None,
            "position_29d": [],
        }
        if (
            LocationInfo is None
            or _astral_sun is None
            or _astral_elevation is None
            or _astral_azimuth is None
            or _astral_moon is None
            or _astral_lmst is None
        ):
            return out

        def _hm_for_minute(day_start: datetime, minute: int) -> str:
            if minute >= 1440:
                return "24:00"
            return (day_start + timedelta(minutes=minute)).strftime("%H:%M")

        try:
            s = saiSettings(apply_live=False)
            resolved = s.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
            resolved_lat = resolved.get("lat")
            resolved_lon = resolved.get("lon")
            resolved_altitude = _safe_float(resolved.get("altitude"))
            resolved_tz = str(resolved.get("tz") or "").strip()
            if resolved_lat is None or resolved_lon is None or not resolved_tz:
                return out

            tzinfo = ZoneInfo(resolved_tz)
            now_local = datetime.now(tzinfo)
            obs = LocationInfo(
                name="sensorius",
                region="local",
                timezone=resolved_tz,
                latitude=resolved_lat,
                longitude=resolved_lon,
            ).observer
            if resolved_altitude is not None:
                obs.elevation = resolved_altitude

            sun_map = _astral_sun(obs, date=now_local.date(), tzinfo=tzinfo)
            sunrise = sun_map.get("sunrise")
            sunset = sun_map.get("sunset")
            noon = sun_map.get("noon")
            if not isinstance(sunrise, datetime) or not isinstance(sunset, datetime):
                return out

            day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            pts: list[dict[str, object]] = []
            for minute in range(0, 1441, 10):
                sample_dt = day_start + timedelta(minutes=minute)
                try:
                    elev = float(_astral_elevation(obs, sample_dt))
                except Exception:
                    elev = float("nan")
                if math.isfinite(elev):
                    pts.append({"m": minute, "t": _hm_for_minute(day_start, minute), "e": round(elev, 2)})

            moon_val = float(_astral_moon.phase(now_local.date()))
            moon_lit_pct = int(
                round((0.5 * (1 - math.cos((2 * math.pi * (moon_val % 28.0)) / 28.0))) * 100)
            )
            moon_points: list[dict[str, object]] = []
            moon_declination = None
            moon_position_source = ""
            try:
                skyfield_runtime = get_skyfield_runtime_if_installed()
                if skyfield_runtime is not None:
                    _loader, ts, eph, _constellation_at = skyfield_runtime
                    from skyfield.api import wgs84

                    topo = wgs84.latlon(
                        float(resolved_lat),
                        float(resolved_lon),
                        elevation_m=float(resolved_altitude or 0.0),
                    )
                    observer_sf = eph["earth"] + topo
                    moon_body = eph["moon"]
                    for minute in range(0, 1441, 10):
                        sample_dt = day_start + timedelta(minutes=minute)
                        t = ts.from_datetime(sample_dt.astimezone(timezone.utc))
                        apparent = observer_sf.at(t).observe(moon_body).apparent()
                        alt, az, _distance = apparent.altaz()
                        _ra, dec, _radec_distance = apparent.radec()
                        elev = float(alt.degrees)
                        azimuth = float(az.degrees)
                        declination = float(dec.degrees)
                        if all(math.isfinite(v) for v in (elev, azimuth, declination)):
                            moon_points.append(
                                {
                                    "m": minute,
                                    "t": _hm_for_minute(day_start, minute),
                                    "e": round(elev, 2),
                                    "az": round(azimuth, 2),
                                    "d": round(declination, 2),
                                }
                            )
                    now_t = ts.from_datetime(now_local.astimezone(timezone.utc))
                    now_apparent = observer_sf.at(now_t).observe(moon_body).apparent()
                    _now_ra, now_dec, _now_dist = now_apparent.radec()
                    now_declination = float(now_dec.degrees)
                    if math.isfinite(now_declination):
                        moon_declination = round(now_declination, 2)
                    if moon_points:
                        moon_position_source = "skyfield"
            except Exception:
                moon_points = []
                moon_declination = None
                moon_position_source = ""
            if not moon_points:
                try:
                    moon_az_fn = getattr(_astral_moon, "azimuth", None)
                    moon_el_fn = getattr(_astral_moon, "elevation", None)
                    if callable(moon_az_fn) and callable(moon_el_fn):
                        for minute in range(0, 1441, 10):
                            sample_dt = day_start + timedelta(minutes=minute)
                            sample_utc = sample_dt.astimezone(timezone.utc)
                            elev = float(moon_el_fn(obs, sample_utc))
                            azimuth = float(moon_az_fn(obs, sample_utc))
                            if all(math.isfinite(v) for v in (elev, azimuth)):
                                moon_points.append(
                                    {
                                        "m": minute,
                                        "t": _hm_for_minute(day_start, minute),
                                        "e": round(elev, 2),
                                        "az": round(azimuth, 2),
                                    }
                                )
                    if moon_points:
                        moon_position_source = "astral"
                except Exception:
                    moon_points = []
                    moon_position_source = ""
            moon_visible_angle = None
            moon_reference_angle = None
            try:
                moon_az_fn = getattr(_astral_moon, "azimuth", None)
                moon_el_fn = getattr(_astral_moon, "elevation", None)
                moon_obs_dt = now_local.astimezone(timezone.utc)
                moon_az = float(moon_az_fn(obs, moon_obs_dt)) if callable(moon_az_fn) else float("nan")
                moon_el = float(moon_el_fn(obs, moon_obs_dt)) if callable(moon_el_fn) else float("nan")
                sun_az = float(_astral_azimuth(obs, now_local))
                sun_el = float(_astral_elevation(obs, now_local))
                moon_pos = _astral_moon.moon_position(_astral_moon.julianday_2000(moon_obs_dt))
                moon_ra = float(moon_pos.right_ascension)
                moon_dec = float(moon_pos.declination)

                if all(math.isfinite(v) for v in (moon_az, moon_el, sun_az, sun_el)):
                    lat_rad = math.radians(float(resolved_lat))
                    moon_az_rad = math.radians(moon_az)
                    moon_el_rad = math.radians(moon_el)
                    sun_az_rad = math.radians(sun_az)
                    sun_el_rad = math.radians(sun_el)

                    sin_sun_dec = (
                        (math.sin(sun_el_rad) * math.sin(lat_rad))
                        + (math.cos(sun_el_rad) * math.cos(lat_rad) * math.cos(sun_az_rad))
                    )
                    sun_dec = math.asin(max(-1.0, min(1.0, sin_sun_dec)))
                    sun_hour_angle = math.atan2(
                        -math.sin(sun_az_rad) * math.cos(sun_el_rad),
                        (math.sin(sun_el_rad) * math.cos(lat_rad))
                        - (math.cos(sun_el_rad) * math.sin(lat_rad) * math.cos(sun_az_rad)),
                    )
                    lst_rad = math.radians(float(_astral_lmst(now_local, float(resolved_lon))))
                    sun_ra = (lst_rad - sun_hour_angle) % (2 * math.pi)

                    chi_num = math.cos(sun_dec) * math.sin(sun_ra - moon_ra)
                    chi_den = (
                        (math.sin(sun_dec) * math.cos(moon_dec))
                        - (math.cos(sun_dec) * math.sin(moon_dec) * math.cos(sun_ra - moon_ra))
                    )
                    bright_limb_angle = math.degrees(math.atan2(chi_num, chi_den)) % 360.0

                    parallactic_angle = math.degrees(
                        math.atan2(
                            math.sin(moon_az_rad),
                            (math.tan(lat_rad) * math.cos(moon_el_rad))
                            - (math.sin(moon_el_rad) * math.cos(moon_az_rad)),
                        )
                    )

                    moon_reference_angle = round(bright_limb_angle, 2)
                    local_canvas_angle = _moon_local_canvas_angle(moon_az, moon_el, sun_az, sun_el)
                    if local_canvas_angle is not None:
                        moon_visible_angle = round(local_canvas_angle, 2)
                    else:
                        moon_visible_angle = round((bright_limb_angle + parallactic_angle) % 360.0, 2)
            except Exception:
                moon_visible_angle = None
                moon_reference_angle = None

            moon_rise = ""
            moon_set = ""
            moon_rise_today = ""
            moon_set_today = ""
            try:
                mr_fn = getattr(_astral_moon, "moonrise", None)
                ms_fn = getattr(_astral_moon, "moonset", None)

                def _event_for_day(fn, d):
                    if not callable(fn):
                        return ""
                    try:
                        ev = fn(obs, date=d, tzinfo=tzinfo)
                    except Exception:
                        return ""
                    if not isinstance(ev, datetime):
                        return ""
                    if ev.tzinfo is None:
                        ev = ev.replace(tzinfo=tzinfo)
                    else:
                        ev = ev.astimezone(tzinfo)
                    return ev.strftime("%H:%M") if ev.date() == d else ""

                def _pick_nearest_event(fn):
                    if not callable(fn):
                        return ""
                    candidates: list[datetime] = []
                    for offset in (-1, 0, 1, 2):
                        d = now_local.date() + timedelta(days=offset)
                        try:
                            ev = fn(obs, date=d, tzinfo=tzinfo)
                        except Exception:
                            continue
                        if isinstance(ev, datetime):
                            if ev.tzinfo is None:
                                ev = ev.replace(tzinfo=tzinfo)
                            candidates.append(ev)
                    if not candidates:
                        return ""
                    future = [ev for ev in candidates if ev >= now_local]
                    chosen = min(future) if future else max(candidates)
                    return chosen.strftime("%H:%M")

                moon_rise_today = _event_for_day(mr_fn, now_local.date())
                moon_set_today = _event_for_day(ms_fn, now_local.date())
                moon_rise = _pick_nearest_event(mr_fn)
                moon_set = _pick_nearest_event(ms_fn)
            except Exception:
                moon_rise = ""
                moon_set = ""
                moon_rise_today = ""
                moon_set_today = ""

            moon_next_phase_label = ""
            moon_next_phase_date = ""
            try:
                phase_targets = (
                    ("New Moon", 0.0),
                    ("1st Quarter", 7.0),
                    ("Full Moon", 14.0),
                    ("3rd Quarter", 21.0),
                )
                phase_cycle = 28.0
                current_phase = moon_val % phase_cycle
                for label, target in phase_targets:
                    if current_phase < target:
                        moon_next_phase_label = label
                        break
                if not moon_next_phase_label:
                    moon_next_phase_label = "New Moon"
                target_phase = next(target for label, target in phase_targets if label == moon_next_phase_label)

                best_date = None
                best_key = None
                for day_offset in range(-15, 17):
                    d = now_local.date() + timedelta(days=day_offset)
                    try:
                        pv = float(_astral_moon.phase(d)) % phase_cycle
                    except Exception:
                        continue
                    dist = abs(pv - target_phase)
                    dist = min(dist, phase_cycle - dist)
                    # Prefer the closest phase match in the current lunation window.
                    # If two dates are equally close, prefer an upcoming date.
                    candidate_key = (dist, abs(day_offset), day_offset < 0)
                    if best_key is None or candidate_key < best_key:
                        best_key = candidate_key
                        best_date = d
                if best_date is not None:
                    moon_next_phase_date = best_date.isoformat()
                    if moon_next_phase_label == "Full Moon":
                        moon_next_phase_label = _traditional_full_moon_name(best_date)
            except Exception:
                moon_next_phase_label = ""
                moon_next_phase_date = ""

            position_29d: list[dict[str, object]] = []
            try:
                sample_minutes = range(0, 1441, 120)
                range_start = day_start
                moon_el_fn = getattr(_astral_moon, "elevation", None)
                position_ts = None
                position_observer = None
                position_moon_body = None
                try:
                    skyfield_runtime = get_skyfield_runtime_if_installed()
                    if skyfield_runtime is not None:
                        _loader, position_ts, position_eph, _constellation_at = skyfield_runtime
                        from skyfield.api import wgs84

                        topo = wgs84.latlon(
                            float(resolved_lat),
                            float(resolved_lon),
                            elevation_m=float(resolved_altitude or 0.0),
                        )
                        position_observer = position_eph["earth"] + topo
                        position_moon_body = position_eph["moon"]
                except Exception:
                    position_ts = None
                    position_observer = None
                    position_moon_body = None

                for day_offset in range(29):
                    graph_day_start = range_start + timedelta(days=day_offset)
                    sun_day: list[list[float | int]] = []
                    moon_day: list[list[float | int]] = []
                    graph_date = graph_day_start.date()
                    graph_moon_phase: float | None = None
                    graph_moon_lit_pct: int | None = None
                    graph_moon_visible_angle: float | None = None
                    for minute in sample_minutes:
                        sample_dt = graph_day_start + timedelta(minutes=minute)
                        try:
                            sun_elev = float(_astral_elevation(obs, sample_dt))
                        except Exception:
                            sun_elev = float("nan")
                        if math.isfinite(sun_elev):
                            sun_day.append([minute, round(sun_elev, 2)])

                        moon_elev = float("nan")
                        try:
                            if position_ts is not None and position_observer is not None and position_moon_body is not None:
                                t = position_ts.from_datetime(sample_dt.astimezone(timezone.utc))
                                apparent = position_observer.at(t).observe(position_moon_body).apparent()
                                alt, _az, _distance = apparent.altaz()
                                moon_elev = float(alt.degrees)
                            elif callable(moon_el_fn):
                                moon_elev = float(moon_el_fn(obs, sample_dt.astimezone(timezone.utc)))
                        except Exception:
                            moon_elev = float("nan")
                        if math.isfinite(moon_elev):
                            moon_day.append([minute, round(moon_elev, 2)])

                    try:
                        graph_moon_phase = float(_astral_moon.phase(graph_date))
                        graph_moon_lit_pct = int(
                            round((0.5 * (1 - math.cos((2 * math.pi * (graph_moon_phase % 28.0)) / 28.0))) * 100)
                        )
                    except Exception:
                        graph_moon_phase = None
                        graph_moon_lit_pct = None

                    try:
                        moon_az_fn = getattr(_astral_moon, "azimuth", None)
                        moon_el_fn_for_angle = getattr(_astral_moon, "elevation", None)
                        graph_moon_dt = graph_day_start.astimezone(timezone.utc)
                        graph_moon_az = float(moon_az_fn(obs, graph_moon_dt)) if callable(moon_az_fn) else float("nan")
                        graph_moon_el = (
                            float(moon_el_fn_for_angle(obs, graph_moon_dt))
                            if callable(moon_el_fn_for_angle)
                            else float("nan")
                        )
                        graph_sun_az = float(_astral_azimuth(obs, graph_day_start))
                        graph_sun_el = float(_astral_elevation(obs, graph_day_start))
                        graph_angle = _moon_local_canvas_angle(graph_moon_az, graph_moon_el, graph_sun_az, graph_sun_el)
                        if graph_angle is not None:
                            graph_moon_visible_angle = round(graph_angle, 2)
                    except Exception:
                        graph_moon_visible_angle = None

                    position_29d.append(
                        {
                            "date": graph_date.isoformat(),
                            "label": graph_day_start.strftime("%b%d"),
                            "sun": sun_day,
                            "moon": moon_day,
                            "moon_phase_value": round(graph_moon_phase, 2) if graph_moon_phase is not None else None,
                            "moon_lit_pct": graph_moon_lit_pct,
                            "moon_visible_angle": graph_moon_visible_angle,
                        }
                    )
            except Exception:
                position_29d = []

            out.update(
                {
                    "ok": True,
                    "lat": round(float(resolved_lat), 6),
                    "lon": round(float(resolved_lon), 6),
                    "tz": resolved_tz,
                    "sunrise": sunrise.strftime("%H:%M"),
                    "sunset": sunset.strftime("%H:%M"),
                    "sun_noon": noon.strftime("%H:%M") if isinstance(noon, datetime) else "",
                    "sun_points": pts,
                    "moon_points": moon_points,
                    "moon_phase_value": round(moon_val, 2),
                    "moon_phase_label": _moon_phase_name(moon_val, now_local.date()),
                    "moon_lit_pct": moon_lit_pct,
                    "moon_rise": moon_rise,
                    "moon_set": moon_set,
                    "moon_rise_today": moon_rise_today,
                    "moon_set_today": moon_set_today,
                    "moon_declination": moon_declination,
                    "moon_position_source": moon_position_source,
                    "moon_next_phase_label": moon_next_phase_label,
                    "moon_next_phase_date": moon_next_phase_date,
                    "moon_visible_angle": moon_visible_angle,
                    "moon_reference_angle": moon_reference_angle,
                    "position_29d": position_29d,
                }
            )
            return out
        except Exception:
            return out

    def _resolve_channel_id_from_label(switch_id: str, label: str) -> str | None:
        try:
            if not services.mqtt_ingest:
                raise Exception("mqtt_ingest missing")
            norm_label = (label or "").strip().lower()
            hit = (services.mqtt_ingest.nodus_label_to_channel or {}).get((str(switch_id), norm_label))
            if hit:
                return hit
        except Exception:
            pass

        # Fallback: use DB switch_ids table (label -> channel_id derived from switch_key)
        try:
            if not services.data_logger:
                return None
            target_sid = (switch_id or "").strip().lower()
            target_label = (label or "").strip().lower()
            for row in (services.data_logger.get_switch_identities() or []):
                sid = str(row.get("switch_id", "")).strip().lower()
                lab = str(row.get("label", "")).strip().lower()
                if sid == target_sid and lab == target_label:
                    ch_id = _switch_channel_id_from_identity_row(row)
                    if ch_id:
                        return ch_id
        except Exception:
            return None

        return None


    _ENV_PATH = Path(
        os.environ.get("SENSORIUS_ENV_FILE") or PROJECT_ROOT / ".env"
    ).expanduser().resolve()
    _ENV_DEF_PATH = PROJECT_ROOT / ".env.def"
    _AUTOSTART_LABEL = "com.sensorius.app"
    _AUTOSTART_SERVICE = "sensorius.service"
    _AUTOSTART_TASK = "SensoriusAutoStart"
    _ADV_DEBUG_MODULE_CHOICES = [
        "Sensorius",
        "saiSensor",
        "saiMQTTIngest",
        "saiHtml",
        "saiSwitch",
        "saiTaskSupervisor",
        "saiAutomationManager",
        "saiSwitchFactory",
        "saiDataLogger",
        "saiWebRoutes",
        "saiAddDevice",
        "saiSettings",
        "saiCalibration",
    ]

    def _read_env_pairs(path: Path) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        try:
            if not path.exists():
                return out
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                out.append((key.strip(), val.strip()))
        except Exception:
            return out
        return out

    def _env_map_with_defaults() -> dict[str, str]:
        merged: dict[str, str] = {}
        for k, v in _read_env_pairs(_ENV_DEF_PATH):
            merged[k] = v
        for k, v in _read_env_pairs(_ENV_PATH):
            merged[k] = v
        return merged

    def _write_env_updates(updates: dict[str, str]) -> None:
        lines: list[str] = []
        if _ENV_PATH.exists():
            try:
                lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []
        elif _ENV_DEF_PATH.exists():
            try:
                lines = _ENV_DEF_PATH.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []

        keys_seen: set[str] = set()
        out_lines: list[str] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in raw:
                out_lines.append(raw)
                continue
            key = raw.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                keys_seen.add(key)
            else:
                out_lines.append(raw)

        for key, val in updates.items():
            if key not in keys_seen:
                out_lines.append(f"{key}={val}")

        _ENV_PATH.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
        for key, val in updates.items():
            os.environ[str(key)] = str(val)
        try:
            os.chmod(_ENV_PATH, 0o600)
        except Exception:
            pass
        try:
            if os.name == "posix" and os.geteuid() == 0 and pwd is not None:
                target_user = (os.environ.get("SUDO_USER") or "").strip()
                if target_user:
                    pw = pwd.getpwnam(target_user)
                    os.chown(_ENV_PATH, pw.pw_uid, pw.pw_gid)
        except Exception:
            pass

    def _bool_text(v: bool) -> str:
        return "true" if bool(v) else "false"


    def _run_quiet(cmd: list[str]) -> tuple[bool, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            ok = (p.returncode == 0)
            msg = (p.stdout or p.stderr or "").strip()
            return ok, msg
        except Exception as ex:
            return False, str(ex)

    def _autostart_paths() -> dict[str, Path]:
        home = Path.home()
        return {
            "linux_user": home / ".config" / "systemd" / "user" / _AUTOSTART_SERVICE,
            "linux_system": Path("/etc/systemd/system") / _AUTOSTART_SERVICE,
            "mac_user": home / "Library" / "LaunchAgents" / f"{_AUTOSTART_LABEL}.plist",
            "mac_system": Path("/Library/LaunchDaemons") / f"{_AUTOSTART_LABEL}.plist",
            "win_user": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Sensorius_autostart.cmd",
        }

    def _autostart_is_enabled(scope: str) -> bool:
        scope = "system" if str(scope).lower() == "system" else "user"
        sys_name = platform.system().lower()
        paths = _autostart_paths()
        if sys_name == "linux":
            if scope == "user":
                ok, out = _run_quiet(["systemctl", "--user", "is-enabled", _AUTOSTART_SERVICE])
            else:
                ok, out = _run_quiet(["systemctl", "is-enabled", _AUTOSTART_SERVICE])
            if ok:
                return "enabled" in out.lower()
            path_key = "linux_system" if scope == "system" else "linux_user"
            return paths[path_key].exists()
        if sys_name == "darwin":
            path_key = "mac_system" if scope == "system" else "mac_user"
            return paths[path_key].exists()
        if sys_name == "windows":
            if scope == "system":
                ok, _ = _run_quiet(["schtasks", "/Query", "/TN", _AUTOSTART_TASK])
                return ok
            return paths["win_user"].exists()
        return False

    def _autostart_apply(enabled: bool, scope: str) -> tuple[bool, str]:
        scope = "system" if str(scope).lower() == "system" else "user"
        sys_name = platform.system().lower()
        paths = _autostart_paths()
        project_dir = PROJECT_ROOT
        python_exe = Path(sys.executable).resolve()
        sensorius_py = (project_dir / "Sensorius.py").resolve()

        if sys_name == "linux":
            path_key = "linux_system" if scope == "system" else "linux_user"
            unit_path = paths[path_key]
            unit_text = "\n".join([
                "[Unit]",
                "Description=Sensorius Service",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={project_dir}",
                f"ExecStart={python_exe} {sensorius_py}",
                "Restart=on-failure",
                "RestartSec=3",
                f"EnvironmentFile={_ENV_PATH}",
                "",
                "[Install]",
                "WantedBy=default.target" if scope == "user" else "WantedBy=multi-user.target",
                "",
            ])
            try:
                unit_path.parent.mkdir(parents=True, exist_ok=True)
                if enabled:
                    unit_path.write_text(unit_text, encoding="utf-8")
                    if scope == "user":
                        _run_quiet(["systemctl", "--user", "daemon-reload"])
                        ok, msg = _run_quiet(["systemctl", "--user", "enable", "--now", _AUTOSTART_SERVICE])
                    else:
                        _run_quiet(["systemctl", "daemon-reload"])
                        ok, msg = _run_quiet(["systemctl", "enable", "--now", _AUTOSTART_SERVICE])
                    return ok, msg or "Enabled"
                else:
                    if scope == "user":
                        _run_quiet(["systemctl", "--user", "disable", "--now", _AUTOSTART_SERVICE])
                        _run_quiet(["systemctl", "--user", "daemon-reload"])
                    else:
                        _run_quiet(["systemctl", "disable", "--now", _AUTOSTART_SERVICE])
                        _run_quiet(["systemctl", "daemon-reload"])
                    if unit_path.exists():
                        unit_path.unlink()
                    return True, "Disabled"
            except Exception as ex:
                return False, str(ex)

        if sys_name == "darwin":
            path_key = "mac_system" if scope == "system" else "mac_user"
            plist_path = paths[path_key]
            plist_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_AUTOSTART_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_exe}</string>
    <string>{sensorius_py}</string>
  </array>
  <key>WorkingDirectory</key><string>{project_dir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
"""
            try:
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                uid = str(os.getuid())
                domain = "system" if scope == "system" else f"gui/{uid}"
                if enabled:
                    plist_path.write_text(plist_text, encoding="utf-8")
                    _run_quiet(["launchctl", "bootout", domain, str(plist_path)])
                    ok, msg = _run_quiet(["launchctl", "bootstrap", domain, str(plist_path)])
                    if not ok:
                        ok, msg = _run_quiet(["launchctl", "load", str(plist_path)])
                    return ok, msg or "Enabled"
                else:
                    _run_quiet(["launchctl", "bootout", domain, str(plist_path)])
                    _run_quiet(["launchctl", "unload", str(plist_path)])
                    if plist_path.exists():
                        plist_path.unlink()
                    return True, "Disabled"
            except Exception as ex:
                return False, str(ex)

        if sys_name == "windows":
            try:
                if scope == "system":
                    if enabled:
                        cmd = f'"{python_exe}" "{sensorius_py}"'
                        ok, msg = _run_quiet(["schtasks", "/Create", "/F", "/SC", "ONSTART", "/TN", _AUTOSTART_TASK, "/TR", cmd, "/RU", "SYSTEM"])
                        return ok, msg or "Enabled"
                    _run_quiet(["schtasks", "/Delete", "/F", "/TN", _AUTOSTART_TASK])
                    return True, "Disabled"
                startup_cmd = paths["win_user"]
                if enabled:
                    startup_cmd.parent.mkdir(parents=True, exist_ok=True)
                    content = "\n".join([
                        "@echo off",
                        f'cd /d "{project_dir}"',
                        f'start "" "{python_exe}" "{sensorius_py}"',
                        "",
                    ])
                    startup_cmd.write_text(content, encoding="utf-8")
                    return True, "Enabled"
                if startup_cmd.exists():
                    startup_cmd.unlink()
                return True, "Disabled"
            except Exception as ex:
                return False, str(ex)

        return False, f"Unsupported platform: {platform.system()}"

    def _scan_for_ssid(target_ssid: str) -> tuple[bool, str, list[str]]:
        ssid = str(target_ssid or "").strip()
        from . import saiAddDevice as _add_device

        def _matching_ssids(values: list[str]) -> list[str]:
            unique = {str(value or "").strip() for value in values if str(value or "").strip()}
            if ssid:
                return [ssid] if ssid in unique else []
            return sorted(value for value in unique if _add_device.is_nodus_setup_ssid(value))

        sys_name = platform.system().lower()
        try:
            if sys_name == "linux":
                iface = ""
                try:
                    from . import saiAddDevice as _add_device
                    iface = str(getattr(_add_device, "_wifi_interface_name", lambda: "")() or "").strip()
                except Exception:
                    iface = ""

                errors: list[str] = []
                if iface:
                    rescan = subprocess.run(
                        ["nmcli", "dev", "wifi", "rescan", "ifname", iface],
                        capture_output=True, text=True, timeout=12
                    )
                    if rescan.returncode != 0:
                        errors.append((rescan.stderr or rescan.stdout or "nmcli rescan failed").strip())

                list_commands = []
                if iface:
                    list_commands.append(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface])
                list_commands.append(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"])

                for cmd in list_commands:
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                    if p.returncode != 0:
                        errors.append((p.stderr or p.stdout or "nmcli list failed").strip())
                        continue
                    lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
                    matches = _matching_ssids(lines)
                    if matches:
                        return True, "ok", matches

                detail = "; ".join(x for x in errors if x).strip()
                return False, detail or "ok", []

            if sys_name == "darwin":
                airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
                if not Path(airport).exists():
                    return False, "airport tool not found", []
                # Prefer plist output (-x) for robust parsing across spacing/alignment changes.
                p_xml = subprocess.run([airport, "-s", "-x"], capture_output=True, timeout=8)
                if p_xml.returncode == 0 and p_xml.stdout:
                    try:
                        rows = plistlib.loads(p_xml.stdout)
                        scanned_ssids: list[str] = []
                        for row in (rows or []):
                            if not isinstance(row, dict):
                                continue
                            candidate = str(row.get("SSID_STR") or row.get("SSID") or "").strip()
                            if candidate:
                                scanned_ssids.append(candidate)
                        matches = _matching_ssids(scanned_ssids)
                        return bool(matches), "ok", matches
                    except Exception:
                        pass

                # Fallback to text output if plist parsing isn't available on this host.
                p = subprocess.run([airport, "-s"], capture_output=True, text=True, timeout=8)
                if p.returncode != 0:
                    err = p.stderr or p.stdout
                    if not err and p_xml.returncode != 0:
                        err = (p_xml.stderr or b"").decode(errors="ignore")
                    return False, (err or "airport scan failed").strip(), []
                scanned_ssids = []
                for ln in (p.stdout or "").splitlines():
                    line = ln.rstrip()
                    if not line or line.lstrip().startswith("SSID "):
                        continue
                    bssid = re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", line)
                    candidate = line[:bssid.start()].strip() if bssid else line.strip()
                    if candidate:
                        scanned_ssids.append(candidate)
                matches = _matching_ssids(scanned_ssids)
                return bool(matches), "ok", matches

            if sys_name == "windows":
                p = subprocess.run(
                    ["netsh", "wlan", "show", "networks", "mode=bssid"],
                    capture_output=True, text=True, timeout=10
                )
                if p.returncode != 0:
                    return False, (p.stderr or p.stdout or "netsh scan failed").strip(), []
                scanned_ssids = []
                for ln in (p.stdout or "").splitlines():
                    m = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*)$", ln, flags=re.IGNORECASE)
                    if m and m.group(1).strip():
                        scanned_ssids.append(m.group(1).strip())
                matches = _matching_ssids(scanned_ssids)
                return bool(matches), "ok", matches
        except Exception as ex:
            return False, str(ex), []
        return False, f"unsupported platform: {platform.system()}", []

    @router.get("/", response_class=HTMLResponse)
    async def current_data_page(
        request: Request,
        sensor_id: str = Query(None),
        json_only: bool = Query(False),
        include_extras: bool = Query(False),
        dashboard_return: bool = Query(False),
    ):
        _route_started = time.monotonic()
        phase_ms: dict[str, float] = {}
        _phase_started = time.monotonic()
        dashboard_cache_key = str(sensor_id or "All")
        if json_only:
            cache_key = (str(sensor_id or "All"), 1 if include_extras else 0)
            now_mono = time.monotonic()
            cached_json = route_state.dashboard_json.get(cache_key)
            if cached_json and cached_json[0] > now_mono:
                cached_payload = cached_json[1]
                _ui_profile_log(
                    "dashboard",
                    _route_started,
                    json_only=1,
                    include_extras=int(bool(include_extras)),
                    cache=1,
                    sensor_id=(sensor_id or "All"),
                )
                return JSONResponse(cached_payload)

        if dashboard_return and not json_only:
            cached_dashboard = route_state.dashboard_html.get(dashboard_cache_key)
            if cached_dashboard is not None:
                route_state.dashboard_html.move_to_end(dashboard_cache_key)
                _ui_profile_log(
                    "dashboard",
                    _route_started,
                    shell_cache=1,
                    sensor_id=dashboard_cache_key,
                )
                return HTMLResponse(
                    content=cached_dashboard,
                    headers={
                        "Cache-Control": "no-store, max-age=0",
                        "Pragma": "no-cache",
                        "X-Sensorius-Dashboard-Shell": "hit",
                    },
                )
        try:
            seen = set()
            available = []

            def _strip_local_suffix(name: str) -> str:
                return normalize_hostname_base(name)

            def _is_switch_id(name: str) -> bool:
                s = (name or "").strip()
                n = s.lower()
                if n.startswith("switch_") or n.startswith("switch-"):
                    return True
                # Nodus per-channel IDs (case-sensitive canonical form), e.g. "S1-en1n8i"
                return bool(re.match(r"^S\d+-[A-Za-z0-9][A-Za-z0-9._-]*$", s))

            def _is_channel_switch_id(name: str) -> bool:
                s = (name or "").strip()
                return bool(re.match(r"^[sS]\d+-[A-Za-z0-9][A-Za-z0-9._-]*$", s))

            def _canonical_channel_id(name: str | None) -> str:
                """
                Normalize channel-id prefix to canonical uppercase form: S<idx>-<serial>.
                Leaves non-channel ids unchanged.
                """
                s = (name or "").strip()
                m = re.match(r"^[sS](\d+)-(.+)$", s)
                if not m:
                    return s
                return f"S{m.group(1)}-{m.group(2)}"

            def _identity_row_channel_id(row: dict) -> str:
                ch_id = _canonical_channel_id(str((row or {}).get("channel_id", "") or "").strip())
                if ch_id:
                    return ch_id
                candidate = _canonical_channel_id(_switch_channel_id_from_identity_row(row))
                return candidate if _is_channel_switch_id(candidate) else ""

            def _is_valid_sensor_id(name: str) -> bool:
                s = (name or "").strip()
                if not s:
                    return False
                if _is_switch_id(s):
                    return False
                return bool(re.match(r"^[A-Za-z0-9._-]+$", s))

            def _normalize_available(sensor_ids_local: list[str], discovered: list[str]) -> list[str]:
                order_preserve = []
                seen_local = set()

                def _add(x: str):
                    if x not in seen_local:
                        seen_local.add(x)
                        order_preserve.append(x)

                for sid in sensor_ids_local:
                    if _is_valid_sensor_id(sid):
                        _add(sid)

                for host in discovered or []:
                    base = _strip_local_suffix(host)
                    if _is_valid_sensor_id(base):
                        _add(base)

                return sorted(order_preserve)
            
            def _normalize_switch_ids(values: list[str], allowed_extra: set[str] | None = None) -> list[str]:
                out = []
                seen_switch = set()
                allowed_extra_l = {str(x).strip().lower() for x in (allowed_extra or set()) if str(x).strip()}
                for raw in values or []:
                    sid = _strip_local_suffix(raw)
                    sid = _canonical_channel_id(sid)
                    sid_l = sid.lower()
                    looks_channel = bool(re.match(r"^[sS]\d+-", sid))
                    # Never allow malformed channel IDs through the allowed-extra bypass.
                    if looks_channel and not _is_switch_id(sid):
                        continue
                    if not _is_switch_id(sid) and sid_l not in allowed_extra_l:
                        continue
                    sid_key = sid_l
                    if sid_key not in seen_switch:
                        seen_switch.add(sid_key)
                        out.append(sid)
                return sorted(out)

            # --- make sensor_map accessible anywhere in this handler
            from collections.abc import Iterable
            def _get_sensor_map():
                sm = services.sensor_map
                return sm

            def _get_local_sensor_ids() -> list[str]:
                sm = _get_sensor_map()
                if isinstance(sm, dict):
                    return [k for k in sm.keys() if isinstance(k, str) and k.strip()]
                if isinstance(sm, Iterable):
                    ids = []
                    for s in sm:
                        sid = getattr(s, "sensor_id", None)
                        if isinstance(sid, str) and sid.strip():
                            ids.append(sid)
                    return ids
                return []

            def _get_local_switch_ids() -> list[str]:
                sc = services.switch_controllers
                out = []
                if isinstance(sc, dict):
                    for ctrl in sc.values():
                        sid = str(getattr(ctrl, "switch_id", "") or "").strip()
                        if sid:
                            out.append(sid)
                elif sc:
                    sid = str(getattr(sc, "switch_id", "") or "").strip()
                    if sid:
                        out.append(sid)
                return out

            # --- define before first use
            def resolve_location_for_sid(sid: str) -> str:
                """
                1) disk settings
                2) in-memory sensor_map
                3) 'Unknown'
                """
                sid_clean = str(sid or "").strip()
                now_mono = time.monotonic()
                cached_loc = route_state.sensor_locations.get(sid_clean)
                if cached_loc and cached_loc[0] > now_mono:
                    return cached_loc[1]
                previous_loc = cached_loc[1] if cached_loc else ""

                try:
                    loc = sensor_settings_mgr.get_setting(sid, "Sensor.LOCATION", None) if sensor_settings_mgr else None
                    if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                        resolved = loc.strip()
                        route_state.sensor_locations[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
                        return resolved
                except Exception:
                    pass

                sm = _get_sensor_map()
                if isinstance(sm, dict):
                    sensor_obj = sm.get(sid) or sm.get((sid or "").lower())
                    if sensor_obj and getattr(sensor_obj, "location", None):
                        loc = sensor_obj.location
                        if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                            resolved = loc.strip()
                            route_state.sensor_locations[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
                            return resolved
                elif isinstance(sm, Iterable):
                    for s in sm:
                        sid_attr = getattr(s, "sensor_id", None)
                        if isinstance(sid_attr, str) and sid_attr.lower() == (sid or "").lower():
                            loc = getattr(s, "location", None)
                            if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                                resolved = loc.strip()
                                route_state.sensor_locations[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
                                return resolved

                if previous_loc and not _is_unknown_location_value(previous_loc):
                    route_state.sensor_locations[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, previous_loc)
                    return previous_loc

                route_state.sensor_locations[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, "Unknown")
                return "Unknown"

            def resolve_location_for_switch_id(sw_id: str) -> str:
                """
                1) live MQTT topic map/device_location
                2) disk switch settings
                3) in-memory switch controllers
                4) 'Unknown'
                """
                sw = str(sw_id or "").strip()
                if not sw:
                    return "Unknown"

                try:
                    topic_map = getattr(services.mqtt_ingest, "nodus_switch_topic_map", {}) or {}
                    for topic, meta in topic_map.items():
                        if str((meta or {}).get("switch_id", "") or "").strip().lower() != sw.lower():
                            continue
                        loc = services.mqtt_ingest.device_location.get(topic)
                        if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                            return loc.strip()
                except Exception:
                    pass

                try:
                    loc = switch_settings_mgr.get_setting(sw, "Switch.SWITCH_LOCATION", None) if switch_settings_mgr else None
                    if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                        return loc.strip()
                except Exception:
                    pass

                try:
                    sc = services.switch_controllers
                    if isinstance(sc, dict):
                        for ctrl in sc.values():
                            sid = str(getattr(ctrl, "switch_id", "") or "").strip()
                            if sid.lower() != sw.lower():
                                continue
                            loc = getattr(ctrl, "location", None)
                            if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                                return loc.strip()
                except Exception:
                    pass

                return "Unknown"

            sensor_settings_mgr = _get_sensor_settings_manager()
            switch_settings_mgr = _get_switch_settings_manager()

            def _switch_enable_field_value(sw_block: dict, idx: int):
                return sw_block.get(f"SWITCH_{idx}_ENABLE_PIN", sw_block.get(f"SWITCH_{idx}_EN", ""))

            def _switch_has_install_marker(val) -> bool:
                if val is None:
                    return False
                if isinstance(val, bool):
                    return val
                return str(val).strip() != ""

            def _switch_labels_from_settings(switch_id: str) -> list[str]:
                if not switch_settings_mgr:
                    return []
                try:
                    doc = switch_settings_mgr.load(switch_id) or {}
                    sw_block = doc.get("Switch", {}) if isinstance(doc, dict) else {}
                    if not isinstance(sw_block, dict):
                        return []

                    sw_type = str(sw_block.get("TYPE", "") or "").strip().lower()
                    has_en_keys = (
                        ("SWITCH_1_ENABLE_PIN" in sw_block)
                        or ("SWITCH_2_ENABLE_PIN" in sw_block)
                        or ("SWITCH_1_EN" in sw_block)
                        or ("SWITCH_2_EN" in sw_block)
                    )
                    labels: list[str] = []
                    if sw_type in ("picow", "pico2w", "nodus", "remote", "mqtt") or has_en_keys:
                        for idx in range(1, 9):
                            label = str(sw_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                            enabled = _switch_has_install_marker(_switch_enable_field_value(sw_block, idx))
                            if label and enabled:
                                labels.append(label)
                    else:
                        for idx in range(1, 33):
                            label = str(sw_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                            pin = sw_block.get(f"SWITCH_{idx}_PIN", None)
                            if label and isinstance(pin, (int, float)):
                                labels.append(label)
                    return labels
                except Exception:
                    return []

            def _live_switch_labels_for(switch_id: str) -> list[str]:
                target = str(switch_id or "").strip().lower()
                if not target:
                    return []
                sc = services.switch_controllers
                ctrls = sc.values() if isinstance(sc, dict) else ([sc] if sc else [])
                labels: list[str] = []
                for ctrl in ctrls:
                    try:
                        if bool(getattr(ctrl, "is_remote", False)):
                            continue
                        sid = str(getattr(ctrl, "switch_id", "") or "").strip().lower()
                        if sid != target or not getattr(ctrl, "is_present", False):
                            continue
                        for label in list(getattr(ctrl, "switches", []) or []):
                            label_text = str(label or "").strip()
                            if label_text:
                                labels.append(label_text)
                    except Exception:
                        continue
                return labels

            def _switch_has_renderable_channels(
                switch_id: str,
                *,
                identity_rows: list[dict],
                nodus_topic_map: dict,
                remote_cache: dict,
            ) -> bool:
                sw = str(switch_id or "").strip()
                if not sw:
                    return False
                sw_l = sw.lower()

                if _live_switch_labels_for(sw):
                    return True
                if _switch_labels_from_settings(sw):
                    return True

                try:
                    for cached_id, ch_map in (remote_cache or {}).items():
                        if str(cached_id or "").strip().lower() != sw_l:
                            continue
                        if not isinstance(ch_map, dict):
                            continue
                        for key in ch_map.keys():
                            if str(key or "").strip():
                                return True
                except Exception:
                    pass

                try:
                    for meta in (nodus_topic_map or {}).values():
                        if str((meta or {}).get("switch_id", "") or "").strip().lower() != sw_l:
                            continue
                        label = str((meta or {}).get("label", "") or "").strip()
                        channel_id = str((meta or {}).get("channel_id", "") or "").strip()
                        if label or channel_id:
                            return True
                except Exception:
                    pass

                try:
                    for row in identity_rows or []:
                        if str((row or {}).get("switch_id", "") or "").strip().lower() != sw_l:
                            continue
                        label = str((row or {}).get("label", "") or "").strip()
                        channel_id = _identity_row_channel_id(row)
                        if label and channel_id:
                            return True
                except Exception:
                    pass

                return False

            inventory_cached = route_state.dashboard_inventory
            now_mono = time.monotonic()
            if inventory_cached and inventory_cached[0] > now_mono:
                inventory_payload = dict(inventory_cached[1])
                local_ids = list(inventory_payload.get("local_ids", []) or [])
                available = list(inventory_payload.get("available", []) or [])
                available_switches = list(inventory_payload.get("available_switches", []) or [])
                renderable_switch_controllers = list(inventory_payload.get("renderable_switch_controllers", []) or [])
                sensor_locations_map = dict(inventory_payload.get("sensor_locations_map", {}) or {})
            else:
                sensors_from_logger = await asyncio.to_thread(services.data_logger.get_available_sensors)
                mqtt_discovered = services.mqtt_ingest.get_known_devices()
                local_ids = _get_local_sensor_ids()
                # Include sensors that have logged data, even if discovery missed /itaot.
                merged_local = list(local_ids or [])
                for sid in (sensors_from_logger or []):
                    if sid and sid not in merged_local:
                        merged_local.append(sid)
                for sid in _get_remote_nodus_sensor_shadow_ids():
                    if sid and sid not in merged_local:
                        merged_local.append(sid)
                available = _normalize_available(merged_local, list(mqtt_discovered))

                # Keep recent local sensors plus live Nodus shadows learned over MQTT.
                available = [sid for sid in available if _is_dashboard_visible_sensor(sid)]

                # Build a switch inventory for debug visibility (local + discovered + DB identities).
                available_switches = []
                renderable_switch_controllers = []
                try:
                    switch_ids_local = []
                    try:
                        switch_ids_local = await asyncio.to_thread(switch_settings_mgr.list_switches) if switch_settings_mgr else []
                        switch_ids_local = switch_ids_local or []
                    except Exception:
                        switch_ids_local = []
                    switch_ids_live = _get_local_switch_ids()

                    switch_ids_discovered = []
                    switch_ids_discovered_channels = []
                    nodus_topic_map = {}
                    try:
                        switch_ids_discovered = services.mqtt_ingest.get_known_switch_devices() or []
                        nodus_topic_map = getattr(services.mqtt_ingest, "nodus_switch_topic_map", {}) or {}
                        for meta in nodus_topic_map.values():
                            sw_id = str((meta or {}).get("switch_id", "") or "").strip()
                            if sw_id:
                                switch_ids_discovered.append(sw_id)
                            ch_id = str((meta or {}).get("channel_id", "") or "").strip()
                            if ch_id:
                                switch_ids_discovered_channels.append(_canonical_channel_id(ch_id))
                    except Exception:
                        switch_ids_discovered = []
                        switch_ids_discovered_channels = []
                        nodus_topic_map = {}

                    switch_ids_db = []
                    switch_ids_db_controllers = []
                    switch_identity_rows = []
                    try:
                        switch_identity_rows = await asyncio.to_thread(services.data_logger.get_switch_identities)
                        for row in (switch_identity_rows or []):
                            sw_id = str(row.get("switch_id", "")).strip()
                            label = str(row.get("label", "") or "").strip()
                            ch_id = _identity_row_channel_id(row)
                            if sw_id and label and ch_id:
                                switch_ids_db_controllers.append(sw_id)
                            if ch_id:
                                switch_ids_db.append(ch_id)
                    except Exception:
                        switch_ids_db = []
                        switch_ids_db_controllers = []
                        switch_identity_rows = []

                    nodus_channels = list(switch_ids_discovered_channels) + list(switch_ids_db)
                    available_switches = _normalize_switch_ids(
                        nodus_channels,
                        allowed_extra=set(nodus_channels),
                    )
                    allowed_controller_ids = set(
                        list(switch_ids_local)
                        + list(switch_ids_live)
                        + list(switch_ids_discovered)
                        + list(switch_ids_db_controllers)
                    )
                    raw_renderable_switch_controllers = _normalize_switch_ids(
                        list(switch_ids_local)
                        + list(switch_ids_live)
                        + list(switch_ids_discovered)
                        + list(switch_ids_db_controllers),
                        allowed_extra=allowed_controller_ids,
                    )
                    remote_cache = getattr(services.mqtt_ingest, "_switch_state_cache", {}) or {}
                    renderable_switch_controllers = [
                        swid for swid in raw_renderable_switch_controllers
                        if _switch_has_renderable_channels(
                            swid,
                            identity_rows=list(switch_identity_rows or []),
                            nodus_topic_map=nodus_topic_map,
                            remote_cache=remote_cache,
                        )
                    ]
                except Exception:
                    available_switches = []
                    renderable_switch_controllers = []

                sensor_locations_map = await asyncio.to_thread(
                    lambda: {sid: resolve_location_for_sid(sid) for sid in available}
                )
                route_state.dashboard_inventory = (
                    time.monotonic() + _DASHBOARD_INVENTORY_CACHE_TTL_SEC,
                    {
                        "local_ids": list(local_ids),
                        "available": list(available),
                        "available_switches": list(available_switches),
                        "renderable_switch_controllers": list(renderable_switch_controllers),
                        "sensor_locations_map": dict(sensor_locations_map),
                    },
                )

            # ---- Optional location filter via sensor_id='loc:<Location>' ----
            selected_location = None
            if isinstance(sensor_id, str) and sensor_id.startswith("loc:"):
                selected_location = sensor_id[4:].strip().lower()
                available = [
                    sid for sid, loc in sensor_locations_map.items()
                    if (loc or "").strip().lower() == selected_location
                ]

            available = _effective_dashboard_row_order(available, sensor_locations_map)

            if DEBUG:
                now_mono = time.monotonic()
                if (now_mono - route_state.profile_last_log) >= _CDP_DEBUG_MIN_INTERVAL_SEC:
                    route_state.profile_last_log = now_mono
                    printDM(f"local_ids: {local_ids}", location=f"{MODULE}:cdp")
                    printDM(f"available sensors: {available}", location=f"{MODULE}:cdp")
                    printDM(f"available switches: {available_switches}", location=f"{MODULE}:cdp")

            phase_ms["inventory"] = (time.monotonic() - _phase_started) * 1000.0

        except Exception as e:
            printDM(f"Exception in current_data_page route definition: {e}", location=f"{MODULE}:cdp")
            raise

        sensor = None
        if not sensor_id or sensor_id == "All":
            sensor_id = "All"
        else:
            sm = _get_sensor_map()
            if isinstance(sm, dict):
                sensor = sm.get(sensor_id) or sm.get(sensor_id.lower())
            elif isinstance(sm, Iterable):
                for s in sm:
                    sid_attr = getattr(s, "sensor_id", None)
                    if isinstance(sid_attr, str) and sid_attr.lower() == sensor_id.lower():
                        sensor = s
                        break
            if DEBUG and sensor is None:
                printDM(f"[cdp] No sensor object found for '{sensor_id}' (map type: {type(sm).__name__})", location=f"{MODULE}")

        # -------- values & stats (compute ONCE) --------
        async def _latest_values_for_sensor_ids(sensor_ids: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
            clean_ids: list[str] = []
            seen_ids: set[str] = set()
            for raw_sid in sensor_ids or []:
                sid_text = str(raw_sid or "").strip()
                sid_key = sid_text.lower()
                if not sid_text or sid_key in seen_ids:
                    continue
                seen_ids.add(sid_key)
                clean_ids.append(sid_text)
            if not clean_ids:
                return {}, {}

            try:
                values, timestamps = await asyncio.to_thread(
                    services.data_logger.get_latest_values_and_timestamps,
                    clean_ids,
                )
                return values or {}, timestamps or {}
            except Exception as exc:
                printDM(
                    f"[dashboard] bulk latest values failed; falling back per sensor: {exc}",
                    location=MODULE,
                )

            async def _latest_for_one(sid_text: str):
                try:
                    values = await asyncio.to_thread(services.data_logger.get_latest_values, sid_text)
                    values = values or {}
                except Exception as exc:
                    printDM(
                        f"[dashboard] latest values failed for {sid_text}: {exc}",
                        location=MODULE,
                    )
                    values = {}

                try:
                    timestamp = await asyncio.to_thread(services.data_logger.get_latest_timestamp, sid_text)
                    timestamp = timestamp or ""
                except Exception as exc:
                    printDM(
                        f"[dashboard] latest timestamp failed for {sid_text}: {exc}",
                        location=MODULE,
                    )
                    timestamp = ""

                return sid_text, values, timestamp

            pairs = await asyncio.gather(
                *[_latest_for_one(sid_text) for sid_text in clean_ids],
                return_exceptions=True,
            )
            values_out: dict[str, dict] = {}
            timestamps_out: dict[str, str] = {}
            for item in pairs:
                if isinstance(item, Exception):
                    printDM(f"[dashboard] per-sensor latest fallback failed: {item}", location=MODULE)
                    continue
                sid_text, values, timestamp = item
                values_out[sid_text] = values or {}
                if timestamp:
                    timestamps_out[sid_text] = timestamp
            return values_out, timestamps_out

        async def _stats_for_sensor_ids(sensor_ids: list[str], *, prefer_fast: bool = True) -> dict[str, dict]:
            clean_ids = [str(sid or "").strip() for sid in (sensor_ids or []) if str(sid or "").strip()]
            if not clean_ids:
                return {}

            if prefer_fast:
                try:
                    all_stats_fast = await asyncio.to_thread(services.statter.get_all_stats_fast)
                    all_stats_fast = all_stats_fast or {}
                    return {sid: (all_stats_fast.get(sid) or {}) for sid in clean_ids}
                except Exception as exc:
                    printDM(
                        f"[dashboard] fast stats failed; falling back per sensor: {exc}",
                        location=MODULE,
                    )

            async def _stats_for_one(sid_text: str):
                try:
                    stats = await asyncio.to_thread(services.statter.get_24hr_stats, sid_text)
                    return sid_text, (stats or {})
                except Exception as exc:
                    printDM(
                        f"[dashboard] stats failed for {sid_text}: {exc}",
                        location=MODULE,
                    )
                    return sid_text, {}

            pairs = await asyncio.gather(
                *[_stats_for_one(sid_text) for sid_text in clean_ids],
                return_exceptions=True,
            )
            stats_out: dict[str, dict] = {}
            for item in pairs:
                if isinstance(item, Exception):
                    printDM(f"[dashboard] per-sensor stats fallback failed: {item}", location=MODULE)
                    continue
                sid_text, stats = item
                stats_out[sid_text] = stats or {}
            return stats_out

        bulk_values: dict[str, dict] = {}
        bulk_timestamps: dict[str, str] = {}
        if not sensor_id or sensor_id == "All" or (isinstance(sensor_id, str) and sensor_id.startswith("loc:")):
            bulk_values, bulk_timestamps = await _latest_values_for_sensor_ids(available)
            all_values = {sid: (bulk_values.get(sid) or {}) for sid in available}
            all_stats = await _stats_for_sensor_ids(available, prefer_fast=True)
        else:
            sid = sensor_id
            bulk_values, bulk_timestamps = await _latest_values_for_sensor_ids([sid])
            v_sid, v = sid, (bulk_values.get(sid) or {})
            all_values = {v_sid: (v or {})}
            all_stats = await _stats_for_sensor_ids([sid], prefer_fast=False)
        phase_ms["values_stats"] = (time.monotonic() - _phase_started) * 1000.0 - phase_ms.get("inventory", 0.0)

        display_settings = _get_cached_display_settings()
        gaugeSize = str(display_settings.get("gauge_size") or "Small")
        gauge_config = dict(display_settings.get("gauge_config") or {})
        displayStyle = str(display_settings.get("display_style") or "Gauge")
        dashboardMetricSet = normalize_dashboard_metric_set(display_settings.get("metric_set") or "Pick 6")
        all_metrics_mode = dashboardMetricSet == "All"
        dashboardBackgroundTheme = normalize_theme_selection(
            theme_manager,
            "sensorius",
            display_settings.get("dashboard_background_theme") or "leaf",
            "leaf",
            normalize_dashboard_background_theme,
        )
        weatherForecastProvider = normalize_weather_forecast_provider(display_settings.get("weather_forecast_provider") or "met_no")
        weatherForecastTheme = normalize_theme_selection(
            theme_manager,
            "caelus",
            display_settings.get("weather_forecast_theme") or "pollinator",
            "pollinator",
            normalize_weather_theme,
        )
        try:
            configured_weewx_id = str(
                settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID)
                or WEEWX_DEFAULT_SENSOR_ID
            ).strip() or WEEWX_DEFAULT_SENSOR_ID
        except Exception:
            configured_weewx_id = WEEWX_DEFAULT_SENSOR_ID

        def _is_weewx_dashboard_sensor(sid: str) -> bool:
            sid_l = str(sid or "").strip().lower()
            return (
                bool(sid_l)
                and (
                    sid_l == configured_weewx_id.lower()
                    or sid_l == WEEWX_DEFAULT_SENSOR_ID.lower()
                    or sid_l.startswith("weewx")
                )
            )

        from .saiCalibration import CalibrationManager
        sensor_mgr = sensor_settings_mgr or _get_sensor_settings_manager()
        expected_gauge_map = {}
        expected_display_style_map = {}
        pressure_sensor_context = {}
        for sid in all_values:
            if sensor_mgr is not None:
                try:
                    pressure_doc = await asyncio.to_thread(sensor_mgr.load, sid)
                except FileNotFoundError:
                    pressure_doc = {}
                pressure_sensor_context[sid] = {
                    "device": (pressure_doc.get("Sensor") or {}).get("DEVICE", ""),
                    "altitude": ((pressure_doc.get("Calibration") or {}).get("Device") or {}).get("ALTITUDE_METERS"),
                    "weewx": _is_weewx_dashboard_sensor(sid),
                }
            try:
                configured_metrics = sensor_mgr.get_display_metrics(sid)
            except Exception:
                configured_metrics = []
            metrics = list(configured_metrics or [])
            try:
                stored_metrics = await asyncio.to_thread(services.data_logger.get_available_metrics, sid)
                stored_metrics = stored_metrics or []
            except Exception:
                stored_metrics = []

            known_metrics: list[str] = []
            known_metrics.extend(stored_metrics)
            vals = all_values.get(sid) or {}
            if vals:
                known_metrics.extend(list(vals.keys()))
            known_metrics.extend(list(services.mqtt_ingest.expected_gauge_map.get(sid) or []))
            extend_gauge_config_for_metrics(gauge_config, known_metrics)

            known_canonical = {
                canonicalize_metric_name(metric, gauge_config)
                for metric in known_metrics
            }
            ordered_all = [k for k in gauge_config.keys() if k in known_canonical]

            if not all_metrics_mode and not metrics:
                metrics = services.mqtt_ingest.expected_gauge_map.get(sid)
            if not all_metrics_mode and not metrics:
                # If display metrics are blank, prefer per-sensor stored metrics
                # rather than rendering every gauge_config metric.
                if stored_metrics:
                    ordered = [k for k in gauge_config.keys() if k in stored_metrics]
                    extras = [k for k in stored_metrics if k not in gauge_config]
                    metrics = ordered + extras

            if not metrics:
                vals = all_values.get(sid) or {}
                if vals:
                    ordered = [k for k in gauge_config.keys() if k in vals]
                    extras = [k for k in vals.keys() if k not in gauge_config]
                    metrics = ordered + extras
            sid_text = str(sid or "").strip()
            if (not all_metrics_mode) and _is_weewx_dashboard_sensor(sid_text):
                metrics = list(configured_metrics or WEEWX_DISPLAY_METRICS)
            metrics.extend([metric for metric in ordered_all if metric not in metrics])
            # Preserve the configured six-card summary order, followed by every
            # additional known metric that the row can reveal on demand.
            deduped: list[str] = []
            seen = set()
            for metric in (metrics or []):
                m = canonicalize_metric_name(metric, gauge_config)
                if not m or m in seen or m not in gauge_config:
                    continue
                seen.add(m)
                deduped.append(m)
            expected_gauge_map[sid] = deduped
            try:
                raw_styles = sensor_mgr.get_display_styles(sid, default_style="Gauge")
            except Exception:
                raw_styles = ["Gauge"] * 6
            style_map = {
                f"METRIC_{idx + 1}": str(raw_styles[idx] or "Gauge")
                for idx in range(6)
            }
            for idx in range(6, len(deduped)):
                style_map[f"METRIC_{idx + 1}"] = displayStyle
            expected_display_style_map[sid] = style_map
        phase_ms["expected_metrics"] = (
            (time.monotonic() - _phase_started) * 1000.0
            - phase_ms.get("inventory", 0.0)
            - phase_ms.get("values_stats", 0.0)
        )

        # Keep a full location map for the location dropdown, even when a single
        # sensor_id view narrows all_values to one sensor.
        sensor_locations = dict(sensor_locations_map)
        # Ensure any actively rendered sensor IDs are still represented.
        for sid in all_values:
            sensor_locations.setdefault(sid, resolve_location_for_sid(sid))

        if DEBUG:
            now_mono = time.monotonic()
            if (now_mono - route_state.profile_last_log) >= _CDP_DEBUG_MIN_INTERVAL_SEC:
                route_state.profile_last_log = now_mono
                printDM(f"sensor_locations: {sensor_locations}", location=f"{MODULE}:cdp")

        try:
            from .saiDataLogger import build_switch_key as _build_switch_key
        except Exception:
            _build_switch_key = None

        def _switch_key(switch_id: str, label: str) -> str:
            sid = (switch_id or "").strip()
            lab = (label or "").strip()
            ch_id = _resolve_channel_id_from_label(sid, lab)
            if _build_switch_key is not None:
                try:
                    return _build_switch_key(sid, ch_id or lab)
                except Exception:
                    pass
            return f"{sid}::{ch_id or lab}"

        def _format_ts_no_micros(ts: str) -> str:
            """
            2025-09-20T12:33:21.827483-06:00 → 2025-09-20 12:33:21-06:00
            """
            if not isinstance(ts, str):
                return ""
            # strip .###### just before Z, an offset, or end
            no_micros = re.sub(r"\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$)", "", ts)
            return no_micros.replace("T", " ")

        def _db_conn():
            # use the same DB the data logger is using
            db_path = getattr(services.data_logger, "db_path", "sensorius_data.db")
            return _sqlite_connect_with_recovery(db_path, source="current_data_page._db_conn")

        def _db_list_known_switch_keys(limit: int = 500) -> list[str]:
            """
            Pull any keys we already know from the DB (switch_ids table).
            """
            keys: list[str] = []
            try:
                with _db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT switch_key FROM switch_ids ORDER BY switch_key COLLATE NOCASE LIMIT ?", (limit,))
                    rows = cur.fetchall()
                    keys = [r[0] for r in rows if r and r[0]]
            except Exception:
                pass
            return keys

        def _db_recent_events_for_key(switch_key: str, limit: int = 10) -> list[str]:
            """
            Read last N events from sw_events for this switch_key.
            Return formatted lines like: 'On 2025-09-20 11:25:34-06:00'
            """
            out: list[str] = []
            try:
                with _db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT timestamp, state, source, sensor_id
                        FROM sw_events
                        WHERE switch_key = ?
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (switch_key, limit),
                    )
                    rows = cur.fetchall()
                for ts, st, _src, _sid in rows:
                    is_on = bool(st) if isinstance(st, (int, bool)) else (str(st).lower() in ("1","true","on"))
                    out.append(f"{'On' if is_on else 'Off'} { _format_ts_no_micros(ts) }")
                out.reverse()  # chronological order
            except Exception:
                pass
            return out

        def _collect_switch_status() -> dict[str, dict]:
            """
            Build a complete status map for the UI:

              {
                "sensoria-hub-0::Fan": {
                  "state": <bool>,
                  "time": ["On 2025-09-20 11:25:34-06:00", ...]  # up to 10 events, oldest → newest
                },
                ...
              }

            DB access uses canonical switch keys (including SWITCH_n_ID when available)
            via _switch_key(), while the UI still sees "switch_id::Label".
            """
            states: dict[str, dict] = {}
            seen_db_keys: set[str] = set()

            # A) Local Pi controllers (3-relay etc.)
            if isinstance(services.switch_controllers, dict):
                for ctrl in services.switch_controllers.values():
                    switch_id = getattr(ctrl, "switch_id", "") or ""
                    if not switch_id:
                        continue

                    labels = list(getattr(ctrl, "switches", []) or [])
                    last_state_map = dict(getattr(ctrl, "last_state", {}) or {})

                    for label in labels:
                        if not label:
                            continue

                        ui_key = f"{switch_id}::{label}"
                        db_key = _switch_key(switch_id, label)                        
                        seen_db_keys.add(db_key)

                        current_state = bool(last_state_map.get(label, False))
                        events = _db_recent_events_for_key(db_key, limit=10)
                        states[ui_key] = {"state": current_state, "time": events}

            # B) Remote Pico / Nodus switches via MQTT cache
            cache = getattr(services.mqtt_ingest, "_switch_state_cache", {}) or {}
            for remote_switch_id, ch_map in cache.items():
                if not isinstance(ch_map, dict):
                    continue

                for channel_label, human_state in ch_map.items():
                    if not channel_label:
                        continue

                    ui_key = f"{remote_switch_id}::{channel_label}"
                    db_key = _switch_key(remote_switch_id, channel_label)
                    seen_db_keys.add(db_key)

                    if ui_key in states:
                        continue  # don't override local

                    events = _db_recent_events_for_key(db_key, limit=10)
                    is_on = str(human_state).lower() == "on"
                    states[ui_key] = {"state": is_on, "time": events}

            # C) Any additional keys seen historically in DB (switch_ids table)
            for db_key in _db_list_known_switch_keys():
                if not db_key or db_key in seen_db_keys:
                    continue

                events = _db_recent_events_for_key(db_key, limit=10)
                if events:
                    last_line = events[-1]
                    is_on = last_line.strip().lower().startswith("on ")
                else:
                    is_on = False

                # We don't reliably know the original human label here,
                # so just expose the DB key as-is for legacy/historical cases.
                ui_key = db_key
                states[ui_key] = {"state": is_on, "time": events}

            return states

        # ---- measurement status helpers (local vs MQTT) ----
        def _active_sensor_for(sid: str):
            sm = _get_sensor_map()
            sid_l = (sid or "").lower()
            if isinstance(sm, dict):
                return sm.get(sid) or sm.get(sid_l) or sm.get(sid_l.replace("_", "-"))
            if isinstance(sm, Iterable):
                for obj in sm:
                    if getattr(obj, "sensor_id", "").lower() == sid_l:
                        return obj
            return None

        ing = services.mqtt_ingest

        def _host_base_from_sid(sid: str) -> str | None:
            """
            Map a sensor_id to the ingest's 'host key' convention:
              - Pi local IDs like 'avpd-i2c-0-sensoria-hub-0' → 'sensoria-hub-0'
              - MQTT Nodus IDs like 'apvpd-luvk44' → 'apvpd-luvk44'
            """
            s = (sid or "").strip()
            if not s:
                return None
            if ("-i2c-" in s) or (s.count("-") >= 3):
                return s.rsplit("-", 1)[-1].strip()
            return s

        def _resolve_meas_status_for_sid(sid: str) -> str:
            sid_text = str(sid or "").strip()
            # 1) Direct/local SensorController.status
            try:
                sc = _active_sensor_for(sid_text)
                base = getattr(sc, "sensor", sc)
                st = getattr(base, "meas_status", None)
                if isinstance(st, str) and st.strip().lower() in {"online", "degraded", "offline", "unknown", "migration_required"}:
                    return st.strip().lower()
            except Exception:
                pass

            # 2) Ask the ingest instance we were given
            try:
                if ing is not None and hasattr(ing, "get_measure_status") and callable(ing.get_measure_status):
                    st = ing.get_measure_status(sid_text)  # should accept either sid or host
                    if isinstance(st, str):
                        status = st.strip().lower()
                        if status in {"online", "degraded", "offline", "migration_required"}:
                            return status
            except Exception:
                pass

            # 3) Fallback: normalize host and consult ingest.device_status
            try:
                dev_map = getattr(ing, "device_status", {}) or {}
                host = _host_base_from_sid(sid_text)
                base = normalize_hostname_base(host)
                for key in (host, base, mdns_hostname(base)):
                    st = dev_map.get(key or "")
                    if isinstance(st, str):
                        status = st.strip().lower()
                        if status in {"online", "degraded", "offline", "migration_required"}:
                            return status
            except Exception:
                pass

            try:
                current_settings = saiSettings(apply_live=False)
                if sid_text.lower().startswith("ecowitt-"):
                    service = getattr(app.state, "ecowitt_service", None)
                    snapshot = service.status() if service is not None and hasattr(service, "status") else {}
                    snapshot_sensor_id = str(snapshot.get("sensor_id", "") or "").strip()
                    snapshot_state = str(snapshot.get("state", "") or "").strip().lower()
                    if snapshot_sensor_id.lower() == sid_text.lower() and snapshot_state in {"online", "offline"}:
                        return snapshot_state
                    latest_timestamp = bulk_timestamps.get(sid_text) or services.data_logger.get_latest_timestamp(sid_text)
                    if latest_timestamp:
                        latest_dt = datetime.fromisoformat(str(latest_timestamp))
                        now_dt = datetime.now(latest_dt.tzinfo) if latest_dt.tzinfo else datetime.now()
                        poll_interval = float(
                            current_settings.get_setting(
                                "Ecowitt", "POLL_INTERVAL_SEC", ECOWITT_DEFAULT_POLL_INTERVAL_SEC
                            ) or ECOWITT_DEFAULT_POLL_INTERVAL_SEC
                        )
                        if (now_dt - latest_dt).total_seconds() <= max(180.0, poll_interval * 3.0):
                            return "online"
                        return "offline"
                weewx_id = str(
                    current_settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID)
                    or WEEWX_DEFAULT_SENSOR_ID
                ).strip() or WEEWX_DEFAULT_SENSOR_ID
                if sid_text.lower() == weewx_id.lower() or sid_text.lower().startswith("weewx"):
                    update_period = float(
                        current_settings.get_setting("WeeWX", "UPDATE_PERIOD_SEC", WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
                        or WEEWX_DEFAULT_UPDATE_PERIOD_SEC
                    )
                    tz_name = str(current_settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver")
                    latest_timestamp = bulk_timestamps.get(sid_text) or services.data_logger.get_latest_timestamp(sid_text)
                    latest_values = all_values.get(sid_text) or services.data_logger.get_latest_values(sid_text) or {}
                    st, _age = _weewx_measure_status_from_latest(
                        latest_timestamp=latest_timestamp,
                        latest_values=latest_values,
                        update_period_sec=update_period,
                        tz_name=tz_name,
                    )
                    if st in {"online", "offline"}:
                        return st
            except Exception:
                pass

            return "unknown"
         
        battery_statuses = {}
        for sid in available:
            if sid.startswith("ecowitt-"):
                battery_statuses[sid] = ecowitt_sensor_health(services.data_logger, sid)["battery_status"]
            elif _is_weewx_dashboard_sensor(sid):
                battery_statuses[sid] = (
                    weewx_battery_status(all_values.get(sid) or {})
                    if _resolve_meas_status_for_sid(sid) == "online"
                    else "UNKNOWN"
                )

        if json_only:
            timestamps = await asyncio.to_thread(
                lambda: {
                    sid: (bulk_timestamps.get(sid) or services.data_logger.get_latest_timestamp(sid) or "")
                    for sid in all_values
                }
            )
            phase_ms["timestamps"] = (
                (time.monotonic() - _phase_started) * 1000.0
                - phase_ms.get("inventory", 0.0)
                - phase_ms.get("values_stats", 0.0)
                - phase_ms.get("expected_metrics", 0.0)
            )
            statuses = { sid: _resolve_meas_status_for_sid(sid) for sid in available }
            renderable_switches = [
                sid for sid in (renderable_switch_controllers or [])
                if sid and (not _is_channel_switch_id(sid))
            ]
            # View-aware switch list so frontend can detect missing switch containers
            # without false positives from other locations.
            renderable_switches_view = list(renderable_switches)
            try:
                target_loc = ""
                if isinstance(sensor_id, str) and sensor_id.startswith("loc:"):
                    target_loc = sensor_id[4:].strip()
                elif sensor_id and sensor_id != "All":
                    target_loc = sensor_locations.get(sensor_id) or resolve_location_for_sid(sensor_id)

                if isinstance(target_loc, str) and target_loc.strip():
                    loc_norm = target_loc.strip().lower()
                    renderable_switches_view = [
                        swid for swid in renderable_switches
                        if (resolve_location_for_switch_id(swid) or "").strip().lower() == loc_norm
                    ]
            except Exception:
                renderable_switches_view = list(renderable_switches)

            phase_ms["switch_view"] = (
                (time.monotonic() - _phase_started) * 1000.0
                - phase_ms.get("inventory", 0.0)
                - phase_ms.get("values_stats", 0.0)
                - phase_ms.get("expected_metrics", 0.0)
                - phase_ms.get("timestamps", 0.0)
            )

            from .saiMetricNames import normalize_metric_keys, normalize_metric_stats
            all_values = {sid: normalize_metric_keys(metrics, expected_gauge_map.get(sid, ())) for sid, metrics in all_values.items()}
            all_stats = {sid: normalize_metric_stats(metrics, expected_gauge_map.get(sid, ())) for sid, metrics in all_stats.items()}
            payload: dict[str, object] = {
                "available": available,
                "dashboard_row_order": _effective_dashboard_row_order(available + ["__switches__"], sensor_locations),
                "values": all_values,
                "stats": all_stats,
                "timestamps": timestamps,
                "sensor_id": sensor_id,
                "timestamp": get_timestamp(),
                "locations": sensor_locations,
                "expected_gauge_map": expected_gauge_map,
                "expected_display_style_map": expected_display_style_map,
                "pressure_altitude": display_settings.get("pressure_altitude"),
                "pressure_sensor_context": pressure_sensor_context,
                "available_switches": available_switches,
                "renderable_switches": renderable_switches,
                "renderable_switches_view": renderable_switches_view,
                "statuses": statuses,
                "battery_statuses": battery_statuses,
            }

            if include_extras:
                extras_started = time.monotonic()
                month_anchor = datetime.now().date().replace(day=1)
                astro_payload, biodynamic_payload = await asyncio.gather(
                    _get_cached_astro_payload_async(allow_stale=True),
                    _get_cached_biodynamic_payload_async(month_anchor, allow_stale=True),
                )
                payload["astro"] = astro_payload
                payload["biodynamic"] = biodynamic_payload
                phase_ms["extras"] = (time.monotonic() - extras_started) * 1000.0
            else:
                phase_ms["extras"] = 0.0

            payload = _dashboard_json_safe(payload)
            route_state.dashboard_json[cache_key] = (
                time.monotonic() + _DASHBOARD_JSON_CACHE_TTL_SEC,
                payload,
            )
            _ui_profile_log(
                "dashboard",
                _route_started,
                json_only=1,
                include_extras=int(bool(include_extras)),
                sensor_id=(sensor_id or "All"),
                sensors=len(available),
                switches=len(available_switches),
                inventory_ms=f"{phase_ms.get('inventory', 0.0):.1f}",
                values_ms=f"{phase_ms.get('values_stats', 0.0):.1f}",
                metrics_ms=f"{phase_ms.get('expected_metrics', 0.0):.1f}",
                timestamps_ms=f"{phase_ms.get('timestamps', 0.0):.1f}",
                switches_ms=f"{phase_ms.get('switch_view', 0.0):.1f}",
                extras_ms=f"{phase_ms.get('extras', 0.0):.1f}",
            )
            return JSONResponse(payload)

        dashboard_switch_controllers = services.switch_controllers

        dashboard_month_anchor = datetime.now().date().replace(day=1)
        astro_payload, biodynamic_payload = await asyncio.gather(
            _get_cached_astro_payload_async(allow_stale=True),
            _get_cached_biodynamic_payload_async(dashboard_month_anchor, allow_stale=True),
        )

        render_started = time.monotonic()
        rendered_dashboard = await asyncio.to_thread(
            lambda: "".join(render_dashboard(
                sensor_id, 
                sensor, 
                available,
                all_values, 
                all_stats, 
                services.mqtt_ingest,
                switch_controllers = dashboard_switch_controllers,
                sensor_locations = sensor_locations,
                gauge_config=gauge_config, 
                gauge_size = gaugeSize,
                battery_statuses = battery_statuses,
                dashboard_row_order = _effective_dashboard_row_order(available + ["__switches__"], sensor_locations),
                switch_row_order = display_settings.get("switch_row_order", "location_device"),
                switch_card_order = display_settings.get("switch_card_order", []),
                pressure_altitude=display_settings.get("pressure_altitude"),
                pressure_sensor_context=pressure_sensor_context,
                expected_gauge_map = expected_gauge_map,
                expected_display_style_map = expected_display_style_map,
                display_style = displayStyle,
                dashboard_background_theme = dashboardBackgroundTheme,
                dashboard_custom_theme_style = theme_manager.style_attribute(
                    theme_manager.style_values("sensorius", dashboardBackgroundTheme)
                ),
                dashboard_metric_set = dashboardMetricSet,
                weather_forecast_provider = weatherForecastProvider,
                weather_forecast_theme = weatherForecastTheme,
                astro_payload=astro_payload,
                biodynamic_payload=biodynamic_payload,
            ))
        )
        phase_ms["render"] = (time.monotonic() - render_started) * 1000.0
        extras_are_warming = _dashboard_payload_is_warming(astro_payload) or _dashboard_payload_is_warming(
            biodynamic_payload
        )
        if not extras_are_warming:
            route_state.dashboard_html[dashboard_cache_key] = rendered_dashboard
            route_state.dashboard_html.move_to_end(dashboard_cache_key)
            while len(route_state.dashboard_html) > _DASHBOARD_HTML_CACHE_MAX_ENTRIES:
                route_state.dashboard_html.popitem(last=False)
        _ui_profile_log(
            "dashboard",
            _route_started,
            json_only=int(bool(json_only)),
            include_extras=int(bool(include_extras)),
            sensor_id=(sensor_id or "All"),
            sensors=len(available),
            switches=len(available_switches),
            inventory_ms=f"{phase_ms.get('inventory', 0.0):.1f}",
            values_ms=f"{phase_ms.get('values_stats', 0.0):.1f}",
            metrics_ms=f"{phase_ms.get('expected_metrics', 0.0):.1f}",
            render_ms=f"{phase_ms.get('render', 0.0):.1f}",
        )
        return HTMLResponse(
            content=rendered_dashboard,
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    _dashboard_metric_position_lock = asyncio.Lock()

    @router.post("/dashboard/metric-position")
    async def dashboard_metric_position(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        sensor_id = str(body.get("sensor_id", "") or "").strip()
        direction = str(body.get("direction", "") or "").strip().lower()
        if not sensor_id:
            return JSONResponse({"error": "missing_sensor_id"}, status_code=400)
        if direction not in {"up", "down"}:
            return JSONResponse({"error": "invalid_direction"}, status_code=400)

        async with _dashboard_metric_position_lock:
            row_ids = await asyncio.to_thread(_current_dashboard_sensor_ids)
            if body.get("include_switches") is True or sensor_id == "__switches__":
                row_ids.append("__switches__")
            ordered = await asyncio.to_thread(_effective_dashboard_row_order, row_ids)
            if sensor_id not in ordered:
                return JSONResponse({"error": "sensor_not_found"}, status_code=404)

            idx = ordered.index(sensor_id)
            if direction == "up":
                if idx <= 0:
                    return JSONResponse({"status": "ok", "sensor_id": sensor_id, "moved": False, "order": ordered})
                swap_idx = idx - 1
            else:
                if idx >= (len(ordered) - 1):
                    return JSONResponse({"status": "ok", "sensor_id": sensor_id, "moved": False, "order": ordered})
                swap_idx = idx + 1

            ordered[idx], ordered[swap_idx] = ordered[swap_idx], ordered[idx]
            await asyncio.to_thread(_persist_visible_metric_order, ordered)
            fresh_settings = saiSettings(apply_live=False)
            await asyncio.to_thread(fresh_settings.replace_setting, "Display", "sensor_row_order", "existing")
            _invalidate_dashboard_caches()
            return JSONResponse({"status": "ok", "sensor_id": sensor_id, "moved": True, "order": ordered})

    @router.post("/dashboard/switch-order")
    async def dashboard_switch_order(request: Request):
        """Persist a manual order for location-grouped dashboard switch cards."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_order"}, status_code=400)
        order = body.get("order") if isinstance(body, dict) else None
        if (not isinstance(order, list) or not order or len(order) > 256
                or any(not isinstance(key, str) or not key.strip() or len(key) > 256 for key in order)):
            return JSONResponse({"error": "invalid_order"}, status_code=400)
        order = list(dict.fromkeys(key.strip().lower() for key in order))

        def save_order():
            fresh_settings = saiSettings(apply_live=False)
            previous = fresh_settings.get_setting("Display", "switch_card_order", [])
            # Keep positions for temporarily hidden/offline locations after visible cards.
            if isinstance(previous, list):
                order.extend(key for key in previous if isinstance(key, str) and key not in order)
            fresh_settings.set_many_in_memory([
                ("Display", "switch_card_order", order),
                ("Display", "switch_row_order", "existing"),
            ])
            fresh_settings.save_settings()

        async with _dashboard_metric_position_lock:
            await asyncio.to_thread(save_order)
            _invalidate_dashboard_caches()
        return JSONResponse({"status": "ok", "order": order})

    # graph data for the full-screen selector or the small single-axis gauge overlay


    def _safe_float(v) -> float | None:
        try:
            return float(v)
        except Exception:
            return None

    def _timezone_suggestions(
        *,
        lon_hint: float | None,
        preferred: list[str] | None = None,
        limit: int = 80,
    ) -> list[str]:
        """
        Produce a practical shortlist of IANA timezones.
        If longitude is known, rank by proximity to expected UTC offset.
        """
        preferred_list = [str(tz).strip() for tz in (preferred or []) if str(tz).strip()]
        seen: set[str] = set()
        out: list[str] = []

        def _push(tz_name: str):
            if tz_name in _ALL_IANA_TIMEZONES and tz_name not in seen:
                seen.add(tz_name)
                out.append(tz_name)

        for tz_name in preferred_list:
            _push(tz_name)

        if lon_hint is None:
            for fallback in (
                "America/New_York",
                "America/Chicago",
                "America/Denver",
                "America/Los_Angeles",
                "America/Phoenix",
                "America/Anchorage",
                "Pacific/Honolulu",
                "UTC",
            ):
                _push(fallback)
            for tz_name in _ALL_IANA_TIMEZONES:
                if len(out) >= max(10, limit):
                    break
                _push(tz_name)
            return out[:max(10, limit)]

        # Approximate UTC offset from longitude for candidate ranking.
        expected_sec = int(round((lon_hint / 15.0) * 3600))
        now_utc = datetime.now(ZoneInfo("UTC"))
        ranked: list[tuple[int, str]] = []
        for tz_name in _ALL_IANA_TIMEZONES:
            try:
                tzinfo = ZoneInfo(tz_name)
                off = now_utc.astimezone(tzinfo).utcoffset()
                off_sec = int(off.total_seconds()) if off is not None else 0
                score = abs(off_sec - expected_sec)
                if tz_name.startswith("Etc/"):
                    score += 3600
                ranked.append((score, tz_name))
            except Exception:
                continue
        ranked.sort(key=lambda x: (x[0], x[1]))
        for _score, tz_name in ranked:
            if len(out) >= max(10, limit):
                break
            _push(tz_name)
        return out[:max(10, limit)]

    @router.get("/timezone-options", response_class=JSONResponse)
    async def timezone_options(
        lat: str | None = Query(None),
        lon: str | None = Query(None),
        limit: int = Query(80, ge=10, le=200),
    ):
        settings_local = saiSettings(apply_live=False)
        resolved = settings_local.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        current_tz = str(settings_local.get_setting("Time", "TZ", "") or "").strip()
        detected_tz = str(resolved.get("tz") or "").strip()

        lon_val = _safe_float(lon)
        if lon_val is None:
            lon_val = _safe_float(resolved.get("lon"))
        lat_val = _safe_float(lat)
        if lat_val is None:
            lat_val = _safe_float(resolved.get("lat"))

        preferred = [current_tz, detected_tz]
        candidates = _timezone_suggestions(lon_hint=lon_val, preferred=preferred, limit=limit)

        return JSONResponse({
            "timezones": candidates,
            "recommended": candidates[0] if candidates else "",
            "detected": detected_tz,
            "lat": lat_val,
            "lon": lon_val,
        })

    @router.get("/api/themes", response_class=JSONResponse)
    async def list_custom_themes(section: str = Query("")):
        """List custom theme collections and safe palette choices."""
        normalized_section = str(section or "").strip().lower()
        if normalized_section and normalized_section not in {"sensorius", "caelus", "biodynamic"}:
            return JSONResponse({"ok": False, "error": "Theme section is not supported."}, status_code=400)
        return JSONResponse({
            "ok": True,
            "themes": theme_manager.list_themes(normalized_section or None),
            "palettes": theme_manager.palettes(),
        })

    @router.post("/api/themes", response_class=JSONResponse)
    async def create_custom_theme(request: Request):
        """Create a custom theme collection from one to five uploaded images."""
        try:
            form = await request.form()
            uploads = list(form.getlist("images"))
            image_names = [str(value or "") for value in form.getlist("image_names")]
            palettes = [str(value or "") for value in form.getlist("palettes")]
            if len(uploads) != len(image_names) or len(uploads) != len(palettes):
                raise ThemeValidationError("Every image requires a name and palette.")
            image_inputs = []
            for index, upload in enumerate(uploads):
                if not callable(getattr(upload, "read", None)):
                    raise ThemeValidationError("Choose a valid image file.")
                content = await upload.read(MAX_UPLOAD_BYTES + 1)
                image_inputs.append({
                    "name": image_names[index],
                    "palette": palettes[index],
                    "content": content,
                })
            created = await asyncio.to_thread(
                theme_manager.create_theme,
                section=str(form.get("section") or ""),
                name=form.get("name"),
                images=image_inputs,
            )
            return JSONResponse({"ok": True, "theme": created})
        except ThemeValidationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            printDM(f"Custom theme creation failed: {exc}", location=MODULE)
            return JSONResponse({"ok": False, "error": "Could not create the custom theme."}, status_code=500)

    @router.delete("/api/themes/{theme_id}", response_class=JSONResponse)
    async def delete_custom_theme(theme_id: str):
        """Delete a custom collection and restore defaults if it was selected."""
        try:
            deleted = await asyncio.to_thread(theme_manager.delete_theme, theme_id)
            if not deleted:
                return JSONResponse({"ok": False, "error": "Custom theme was not found."}, status_code=404)
            active_settings = settings
            if not callable(getattr(active_settings, "replace_setting", None)):
                active_settings = saiSettings()
            fallbacks = (
                ("Display", "background_theme", "leaf"),
                ("WeatherForecast", "THEME", "pollinator"),
                ("Display", "biodynamic_calendar_theme", "garden_tools"),
            )
            for settings_section, key, fallback in fallbacks:
                selected = str(active_settings.get_setting(settings_section, key, "") or "")
                if selected.startswith(f"custom:{theme_id}:"):
                    active_settings.replace_setting(settings_section, key, fallback)
            return JSONResponse({"ok": True})
        except ThemeValidationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/edit-system", response_class=HTMLResponse)
    async def edit_pi_settings_page(request: Request):
        _route_started = time.monotonic()
        from .saiSettings import saiSettings
        from .saiHtml import APP_NAME_LONG, APP_VERSION

        settings = saiSettings(apply_live=False)
        templates = request.app.state.templates

        # Prepare values for the template (let Jinja handle escaping)
        hostname   = settings.get_setting("Network", "HOSTNAME", "") or ""
        httpport   = settings.get_setting("Network", "HTTPPORT", 8000) or 8000
        broker     = settings.get_setting("SensorNetwork", "BROKER", "") or ""
        mqttport   = settings.get_setting("SensorNetwork", "MQTTPORT", 1883) or 1883
        sensornetwork_use_tls = bool(settings.get_setting("SensorNetwork", "USE_TLS", False))
        tz         = (
            settings.get_setting("Time", "TZ", "")
            or ""
        )
        tz_offset  = (
            settings.get_setting("Time", "TZ_OFFSET", "")
            or ""
        )
        tz_name    = (
            settings.get_setting("Time", "TZ_NAME", "")
            or ""
        )
        astral_lat = str(settings.get_setting("Astral", "LATITUDE", "") or "").strip()
        astral_lon = str(settings.get_setting("Astral", "LONGITUDE", "") or "").strip()
        astral_altitude = str(settings.get_setting("Astral", "ALTITUDE", "") or "").strip()
        astral_location_name = str(settings.get_setting("Astral", "LOCATION_NAME", "") or "").strip()
        astral_sunrise = "--"
        astral_sunset = "--"
        astral_daylight = "--"
        astral_noon = "--"
        gauge_size = settings.get_setting("Display", "gauge_size", "") or "Small"
        display_style = settings.get_setting("Display", "display_style", "") or "Gauge"
        unit_system = normalize_display_unit_system(
            settings.get_setting("Display", "unit_system", "Imperial")
        )
        metric_set = normalize_dashboard_metric_set(
            settings.get_setting("Display", "metric_set", "Pick 6")
        )
        dashboard_background_theme = normalize_theme_selection(
            theme_manager,
            "sensorius",
            settings.get_setting("Display", "background_theme", "leaf"),
            "leaf",
            normalize_dashboard_background_theme,
        )
        biodynamic_calendar_theme = normalize_theme_selection(
            theme_manager,
            "biodynamic",
            settings.get_setting("Display", "biodynamic_calendar_theme", "garden_tools"),
            "garden_tools",
            normalize_biodynamic_calendar_theme,
        )
        weather_forecast_provider = normalize_weather_forecast_provider(
            settings.get_setting("WeatherForecast", "PROVIDER", "met_no")
        )
        weather_forecast_theme = normalize_theme_selection(
            theme_manager,
            "caelus",
            settings.get_setting("WeatherForecast", "THEME", "pollinator"),
            "pollinator",
            normalize_weather_theme,
        )
        weather_forecast_sensor_id = str(
            settings.get_setting("WeatherForecast", "CURRENT_SENSOR_ID", "") or ""
        ).strip()
        ha_enabled = bool(settings.get_setting("HomeAssistant", "ENABLED", False))
        ha_username = settings.get_setting("HomeAssistant", "HA_USERNAME", "") or ""
        ha_password_raw = settings.get_setting("HomeAssistant", "HA_PASSWORD", "") or ""
        ha_password = saiSettings.deobfuscate_secret(ha_password_raw)
        ha_broker = (
            settings.get_setting("HomeAssistant", "HA_BROKER", "")
            or settings.get_setting("HomeAssistant", "BROKER", "")
            or ""
        )
        ha_port = (
            settings.get_setting("HomeAssistant", "HA_MQTTPORT", 1883)
            or settings.get_setting("HomeAssistant", "PORT", 1883)
            or 1883
        )
        ha_use_tls = bool(settings.get_setting("HomeAssistant", "USE_TLS", False))
        farm_enabled = bool(settings.get_setting("FarmOS", "ENABLED", False))
        farm_base_url = settings.get_setting("FarmOS", "BASE_URL", "") or ""
        farm_verify_tls = bool(settings.get_setting("FarmOS", "VERIFY_TLS", True))
        farm_access_token_raw = settings.get_setting("FarmOS", "ACCESS_TOKEN", "") or ""
        farm_access_token = saiSettings.deobfuscate_secret(farm_access_token_raw)
        farm_client_id = settings.get_setting("FarmOS", "CLIENT_ID", "farm") or "farm"
        farm_client_secret_raw = settings.get_setting("FarmOS", "CLIENT_SECRET", "") or ""
        farm_client_secret = saiSettings.deobfuscate_secret(farm_client_secret_raw)
        farm_username = settings.get_setting("FarmOS", "USERNAME", "") or ""
        farm_password_raw = settings.get_setting("FarmOS", "PASSWORD", "") or ""
        farm_password = saiSettings.deobfuscate_secret(farm_password_raw)
        farm_log_bundle = settings.get_setting("FarmOS", "LOG_BUNDLE", "observation") or "observation"
        weewx_mqtt_enabled = bool(settings.get_setting("WeeWX", "MQTT_ENABLED", False))
        weewx_db_path = settings.get_setting("WeeWX", "DB_PATH", WEEWX_DEFAULT_DB_PATH) or WEEWX_DEFAULT_DB_PATH
        weewx_sensor_id = settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID
        weewx_mqtt_topic = settings.get_setting("WeeWX", "MQTT_TOPIC", WEEWX_DEFAULT_MQTT_TOPIC) or WEEWX_DEFAULT_MQTT_TOPIC
        weewx_update_period_sec = settings.get_setting("WeeWX", "UPDATE_PERIOD_SEC", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC
        ecowitt_gateway_url = settings.get_setting("Ecowitt", "GATEWAY_URL", "") or ""
        ecowitt_poll_interval_sec = settings.get_setting(
            "Ecowitt", "POLL_INTERVAL_SEC", ECOWITT_DEFAULT_POLL_INTERVAL_SEC
        ) or ECOWITT_DEFAULT_POLL_INTERVAL_SEC
        email_env = _env_map_with_defaults()

        def _email_env_value(name: str, default: str = "") -> str:
            return str(os.environ.get(name, email_env.get(name, default)) or "").strip()

        email_enabled = _is_true_text(_email_env_value("SENSORIUS_EMAIL_ENABLED", "false"))
        email_smtp_host = _email_env_value("SENSORIUS_EMAIL_SMTP_HOST", "smtp.gmail.com")
        email_smtp_port = _email_env_value("SENSORIUS_EMAIL_SMTP_PORT", "465")
        email_security = _email_env_value("SENSORIUS_EMAIL_SECURITY", "ssl").lower()
        email_username = _email_env_value("SENSORIUS_EMAIL_USERNAME", "")
        email_password_configured = bool(_email_env_value("SENSORIUS_EMAIL_APP_PASSWORD", ""))
        email_from = _email_env_value("SENSORIUS_EMAIL_FROM", "") or email_username
        email_to = _email_env_value("SENSORIUS_EMAIL_TO", "")
        notification_rules = normalize_notification_rules(
            settings.get_setting("Notifications", "RULES_JSON", "[]")
        )
        notification_rules_json = json.dumps(notification_rules, separators=(",", ":"))

        clients = settings.get_all_clients() or []
        client_list = "\n".join(clients)

        def _format_hhmm(dt_obj: datetime | None) -> str:
            if dt_obj is None:
                return "--"
            return dt_obj.strftime("%H:%M")

        resolved = settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        resolved_lat = resolved.get("lat")
        resolved_lon = resolved.get("lon")
        resolved_altitude = _safe_float(resolved.get("altitude"))
        resolved_tz = str(resolved.get("tz") or "").strip()
        tz_options = _timezone_suggestions(
            lon_hint=_safe_float(resolved_lon),
            preferred=[str(tz or "").strip(), resolved_tz],
            limit=80,
        )

        if resolved_lat is not None and resolved_lon is not None:
            astral_lat = astral_lat or f"{resolved_lat:.6f}"
            astral_lon = astral_lon or f"{resolved_lon:.6f}"

        if LocationInfo is not None and _astral_sun is not None and resolved_lat is not None and resolved_lon is not None and resolved_tz:
            try:
                tzinfo = ZoneInfo(resolved_tz)
                now_local = datetime.now(tzinfo)
                loc = LocationInfo(
                    name="sensorius",
                    region="local",
                    timezone=resolved_tz,
                    latitude=resolved_lat,
                    longitude=resolved_lon,
                )
                observer = loc.observer
                if resolved_altitude is not None:
                    observer.elevation = resolved_altitude
                sun_map = _astral_sun(observer, date=now_local.date(), tzinfo=tzinfo)
                sunrise_dt = sun_map.get("sunrise")
                sunset_dt = sun_map.get("sunset")
                noon_dt = sun_map.get("noon")
                astral_sunrise = _format_hhmm(sunrise_dt)
                astral_sunset = _format_hhmm(sunset_dt)
                astral_noon = _format_hhmm(noon_dt)
                if isinstance(sunrise_dt, datetime) and isinstance(sunset_dt, datetime):
                    if sunset_dt >= sunrise_dt:
                        span = sunset_dt - sunrise_dt
                        total_min = int(span.total_seconds() // 60)
                        astral_daylight = f"{total_min // 60:02d}:{total_min % 60:02d}"
            except Exception:
                pass

        templates = request.app.state.templates
        system_modal_html = templates.get_template("modals/system_settings.html").render(
            app_name_long=APP_NAME_LONG,
            app_version=APP_VERSION,
            runtime_instance_id=app.state.ui_runtime_instance_id,
            hostname=hostname,
            httpport=httpport,
            broker=broker,
            mqttport=mqttport,
            sensornetwork_use_tls=sensornetwork_use_tls,
            tz=tz,
            tz_offset=tz_offset,
            tz_name=tz_name,
            tz_options=tz_options,
            gauge_size=gauge_size,
            display_style=display_style,
            unit_system=unit_system,
            sensor_row_order=settings.get_setting("Display", "sensor_row_order", "location_device"),
            switch_row_order=settings.get_setting("Display", "switch_row_order", "location_device"),
            metric_set=metric_set,
            dashboard_background_theme=dashboard_background_theme,
            biodynamic_calendar_theme=biodynamic_calendar_theme,
            weather_forecast_provider=weather_forecast_provider,
            weather_forecast_theme=weather_forecast_theme,
            custom_themes={
                "sensorius": theme_manager.list_themes("sensorius"),
                "caelus": theme_manager.list_themes("caelus"),
                "biodynamic": theme_manager.list_themes("biodynamic"),
            },
            theme_palettes=theme_manager.palettes(),
            weather_forecast_sensor_id=weather_forecast_sensor_id,
            astral_lat=astral_lat,
            astral_lon=astral_lon,
            astral_altitude=altitude_input_value(astral_altitude, unit_system),
            astral_altitude_unit="ft" if unit_system == "Imperial" else "m",
            astral_location_name=astral_location_name,
            astral_sunrise=astral_sunrise,
            astral_sunset=astral_sunset,
            astral_daylight=astral_daylight,
            astral_noon=astral_noon,
            client_list=client_list,
            ha_enabled=ha_enabled,
            ha_use_tls=ha_use_tls,
            ha_username=ha_username,
            ha_password=ha_password,
            ha_broker=ha_broker,
            ha_port=ha_port,
            farm_enabled=farm_enabled,
            farm_base_url=farm_base_url,
            farm_verify_tls=farm_verify_tls,
            farm_access_token=farm_access_token,
            farm_client_id=farm_client_id,
            farm_client_secret=farm_client_secret,
            farm_username=farm_username,
            farm_password=farm_password,
            farm_log_bundle=farm_log_bundle,
            weewx_mqtt_enabled=weewx_mqtt_enabled,
            weewx_db_path=weewx_db_path,
            weewx_sensor_id=weewx_sensor_id,
            weewx_mqtt_topic=weewx_mqtt_topic,
            weewx_update_period_sec=weewx_update_period_sec,
            weewx_broker=broker or "localhost",
            weewx_port=mqttport,
            ecowitt_gateway_url=ecowitt_gateway_url,
            ecowitt_poll_interval_sec=ecowitt_poll_interval_sec,
            email_enabled=email_enabled,
            email_smtp_host=email_smtp_host,
            email_smtp_port=email_smtp_port,
            email_security=email_security,
            email_username=email_username,
            email_password_configured=email_password_configured,
            email_from=email_from,
            email_to=email_to,
            notification_rules_json=notification_rules_json,
            onboarding_v2_mqtt_enabled=_onboarding_v2_enabled(),
        )

        fragment_parts: list[str] = []
        fragment_parts.append(f"<link rel='stylesheet' href='/ui_static/css/app.css?v={APP_VERSION}'>")
        fragment_parts.append(f"<link rel='stylesheet' href='/ui_static/css/mobile_settings.css?v={APP_VERSION}'>")
        fragment_parts.append(f"<script src='/ui_static/js/mobile_settings.js?v={APP_VERSION}'></script>")
        fragment_parts.append(f"<script src='/ui_static/js/draggable_modals.js?v={APP_VERSION}'></script>")
        fragment_parts.append(f"<script type='module' src='/ui_static/js/advanced_automation.js?v={APP_VERSION}'></script>")
        fragment_parts.append(system_modal_html)
        fragment_html = "\n".join(fragment_parts)

        embed = str(request.query_params.get("embed", "")).strip().lower() in {"1", "true", "yes"}
        if embed:
            _ui_profile_log("edit-system", _route_started, embed=1, clients=len(clients))
            return HTMLResponse(
                content=fragment_html,
                headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
            )

        html_parts: list[str] = []
        html_parts.append("<!DOCTYPE html>")
        html_parts.append("<html><head><title>General Settings</title>")
        html_parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        html_parts.append("</head><body>")
        html_parts.append(fragment_html)
        html_parts.append("</body></html>")
        _ui_profile_log("edit-system", _route_started, embed=0, clients=len(clients))
        return HTMLResponse(
            content="\n".join(html_parts),
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    # /itaot helpers
    CONTENT_ENCODING = "base64+zlib"

    def _compress_b64_bytes(raw: bytes) -> str:
        """zlib-compress + base64-encode -> str."""
        return base64.b64encode(zlib.compress(raw)).decode("ascii")

    _SECRET_TOML_KEY_RE = re.compile(
        r"(?im)^(\s*(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|MQTT_PASSWORD|HA_PASSWORD)\s*=\s*).*$"
    )
    _DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def _redact_toml_secrets(raw: bytes) -> bytes:
        """Best-effort redaction for sensitive TOML keys before export."""
        try:
            text = raw.decode("utf-8", errors="replace")
            redacted = _SECRET_TOML_KEY_RE.sub(r'\1"***REDACTED***"', text)
            return redacted.encode("utf-8")
        except Exception:
            return raw

    def _compressed_b64_or_none(path: Path, *, redact_secrets: bool = False) -> str | None:
        try:
            raw = path.read_bytes()
            if redact_secrets:
                raw = _redact_toml_secrets(raw)
            return _compress_b64_bytes(raw)
        except Exception as ex:
            if DEBUG:
                printDM(f"itaot: could not include file {path}: {ex}", location="saiWebRoutes:itaot")
            return None

    def _is_valid_device_id(device_id: str) -> bool:
        sid = (device_id or "").strip()
        if sid in {".", ".."}:
            return False
        return bool(_DEVICE_ID_RE.fullmatch(sid))

    def _safe_child_path(base_dir: Path, device_id: str) -> Path | None:
        """Resolve base/device_id and reject traversal outside base."""
        if not _is_valid_device_id(device_id):
            return None
        try:
            base_resolved = base_dir.resolve()
            target = (base_resolved / device_id).resolve()
            if base_resolved == target or base_resolved in target.parents:
                return target
        except Exception:
            return None
        return None

    def _get_web_api_key() -> str:
        key = os.getenv("SAI_WEB_API_KEY", "").strip()
        if key:
            return key
        candidates = (
            ("Security", "WEB_API_KEY"),
            ("Web", "WEB_API_KEY"),
            ("Web", "API_KEY"),
        )
        for section, field in candidates:
            try:
                val = (settings.get_setting(section, field, "") or "").strip()
                if val:
                    return val
            except Exception:
                continue
        return ""

    def _extract_api_key(request: Request) -> str:
        hdr = (request.headers.get("x-api-key") or "").strip()
        if hdr:
            return hdr
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _is_same_origin(request: Request) -> bool:
        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site == "same-origin":
            return True
        host = (request.url.hostname or "").strip().lower()
        if not host:
            return False
        for header in ("origin", "referer"):
            raw = (request.headers.get(header) or "").strip()
            if not raw:
                continue
            try:
                parsed = urlparse(raw)
            except Exception:
                return False
            src = (parsed.hostname or "").strip().lower()
            if src and src == host:
                return True
            return False
        return False

    def _require_protected_access(request: Request, *, require_csrf: bool = False) -> None:
        expected_key = _get_web_api_key()
        if not expected_key:
            # No key configured -> preserve current open-by-default behavior.
            return None

        supplied_key = _extract_api_key(request)
        if supplied_key and hmac.compare_digest(supplied_key, expected_key):
            return None

        # For browser-driven same-origin form/XHR flows, allow CSRF-marked routes
        # without requiring clients to inject API-key headers.
        if require_csrf and _is_same_origin(request):
            return None

        raise HTTPException(status_code=401, detail="unauthorized")

    def _ota_error_response(exc: Exception, *, status_code: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status_code)

    @router.get("/api/nodus-ota/devices", response_class=JSONResponse)
    async def api_nodus_ota_devices(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "devices": ota_service.list_devices()})

    @router.get("/api/nodus-ota/packages", response_class=JSONResponse)
    async def api_nodus_ota_packages(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "packages": ota_service.list_packages()})

    @router.post("/api/nodus-ota/package/inspect", response_class=JSONResponse)
    async def api_nodus_ota_package_inspect(request: Request):
        _require_protected_access(request, require_csrf=True)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        package_ref = str((payload or {}).get("package_ref") or "").strip()
        try:
            package = await asyncio.to_thread(ota_service.inspect_package, package_ref)
            return JSONResponse({"ok": True, "package": package.summary()})
        except NodusOTAError as exc:
            return _ota_error_response(exc)

    def _ota_folder_roots() -> list[str]:
        roots: list[str] = []
        seen: set[str] = set()

        def _append_root(candidate: object) -> None:
            try:
                path = Path(candidate).expanduser().resolve()
            except Exception:
                return
            try:
                if not path.exists() or not path.is_dir():
                    return
            except Exception:
                return
            text = str(path)
            if text and text not in seen:
                roots.append(text)
                seen.add(text)

        _append_root(getattr(ota_service, "package_root", Path.cwd() / "ota_packages"))
        try:
            home = str(Path.home())
            if home:
                _append_root(home)
        except Exception:
            pass
        return roots

    def _ota_folder_listing(raw_path: str | None = None) -> dict[str, Any]:
        roots = _ota_folder_roots()
        fallback = roots[0] if roots else str(Path.cwd())
        path_text = str(raw_path or "").strip() or fallback
        try:
            path = Path(path_text).expanduser().resolve()
        except Exception:
            path = Path(fallback).resolve()
        if not path.exists() or not path.is_dir():
            path = Path(fallback).resolve()

        dirs = []
        error = ""

        def _is_hidden_dir(candidate: Path) -> bool:
            name = candidate.name
            if name.startswith("."):
                return True
            if os.name == "nt":
                try:
                    attrs = getattr(candidate.stat(), "st_file_attributes", 0)
                    return bool(attrs & 0x2)
                except Exception:
                    return False
            return False

        try:
            for child in path.iterdir():
                try:
                    if child.is_dir() and not _is_hidden_dir(child):
                        dirs.append({"name": child.name, "path": str(child.resolve())})
                except OSError:
                    continue
        except Exception as exc:
            error = str(exc)
        dirs.sort(key=lambda item: item["name"].lower())
        valid = (path / "manifest.json").is_file() and (path / "files").is_dir()
        parent = ""
        try:
            if path.parent != path:
                parent = str(path.parent)
        except Exception:
            parent = ""
        return {
            "path": str(path),
            "parent": parent,
            "directories": dirs,
            "valid_package": valid,
            "error": error,
        }

    @router.get("/api/nodus-ota/package/browse", response_class=JSONResponse)
    async def api_nodus_ota_package_browse(request: Request, path: str = ""):
        _require_protected_access(request, require_csrf=True)
        listing = await asyncio.to_thread(_ota_folder_listing, path)
        return JSONResponse({"ok": True, "folder": listing})

    @router.post("/api/nodus-ota/jobs", response_class=JSONResponse)
    async def api_nodus_ota_start_job(request: Request):
        _require_protected_access(request, require_csrf=True)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            job = ota_service.start_job(
                str((payload or {}).get("package_ref") or ""),
                list((payload or {}).get("device_ids") or []),
                concurrency=int((payload or {}).get("concurrency") or 1),
                force_version_mismatch=bool((payload or {}).get("force_version_mismatch", False)),
                chunk_size=int((payload or {}).get("chunk_size") or 1024),
            )
            return JSONResponse({"ok": True, "job": job})
        except NodusOTAError as exc:
            return _ota_error_response(exc)

    @router.get("/api/nodus-ota/jobs", response_class=JSONResponse)
    async def api_nodus_ota_jobs(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "jobs": ota_service.list_jobs()})

    @router.get("/api/nodus-ota/jobs/{job_id}", response_class=JSONResponse)
    async def api_nodus_ota_job(request: Request, job_id: str):
        _require_protected_access(request, require_csrf=True)
        try:
            return JSONResponse({"ok": True, "job": ota_service.job_snapshot(job_id)})
        except NodusOTAError as exc:
            return _ota_error_response(exc, status_code=404 if "not_found" in str(exc) else 400)

    @router.post("/api/nodus-ota/jobs/{job_id}/cancel", response_class=JSONResponse)
    async def api_nodus_ota_cancel_job(request: Request, job_id: str):
        _require_protected_access(request, require_csrf=True)
        try:
            return JSONResponse({"ok": True, "job": ota_service.cancel_job(job_id)})
        except NodusOTAError as exc:
            return _ota_error_response(exc, status_code=404 if "not_found" in str(exc) else 400)

    def _sensor_metrics_from_display_block(display_block) -> list[str]:
        """
        Normalize the [Display] block into an ordered list of metric names.

        Accepts:
          - dict with keys METRIC_1..METRIC_6 -> returns their *values* (non-empty)
          - list/tuple of strings               -> returns cleaned list
          - anything else                       -> returns []

        Always returns a list of up to 6 unique, non-empty strings.
        """
        # Already a list? Clean it and return.
        if isinstance(display_block, (list, tuple)):
            out = [str(x).strip() for x in display_block if str(x).strip()]
            # de-dupe in order, cap at 6
            seen, ordered = set(), []
            for m in out:
                if m not in seen:
                    seen.add(m)
                    ordered.append(m)
                if len(ordered) >= 6:
                    break
            return ordered

        # Dict path: extract values from METRIC_1..METRIC_6 in order.
        if isinstance(display_block, dict):
            order = ["METRIC_1","METRIC_2","METRIC_3","METRIC_4","METRIC_5","METRIC_6"]
            raw = [str(display_block.get(k, "")).strip() for k in order]
            out = [m for m in raw if m]   # drop empties
            # de-dupe in order, cap at 6
            seen, ordered = set(), []
            for m in out:
                if m not in seen:
                    seen.add(m)
                    ordered.append(m)
                if len(ordered) >= 6:
                    break
            return ordered

        return []


    def _topic_for_sensor(sensor_id: str) -> str:
        return f"sensor/{sensor_id}/data"

    def _topic_for_switch(switch_id: str) -> str:
        return f"switch/{switch_id}/state"

    def _build_time_payload() -> dict:
        try:
            tz_name = (settings.get_setting("Time", "TZ", "") or "").strip() or "America/Denver"
            tz_short = (settings.get_setting("Time", "TZ_NAME", "") or "").strip()
            tz_offset = settings.get_setting("Time", "TZ_OFFSET", None)
        except Exception:
            tz_name = "America/Denver"
            tz_short = ""
            tz_offset = None

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Denver")

        now = datetime.now(tz)
        if tz_offset is None:
            try:
                tz_offset = int(now.utcoffset().total_seconds())
            except Exception:
                tz_offset = 0
        else:
            try:
                tz_offset = int(tz_offset)
            except Exception:
                tz_offset = 0

        if not tz_short:
            try:
                tz_short = now.tzname() or ""
            except Exception:
                tz_short = ""

        import time as _time
        return {
            "epoch": _time.time(),
            "iso": now.isoformat(),
            "tz": tz_name,
            "tz_offset": tz_offset,
            "tz_name": tz_short,
        }

    @router.get("/time-request", response_class=JSONResponse)
    async def time_request():
        """
        Full JSON time payload for Nodus devices via HTTP.
        """
        try:
            if services.mqtt_ingest and hasattr(services.mqtt_ingest, "_build_time_payload"):
                payload = services.mqtt_ingest._build_time_payload()
            else:
                payload = _build_time_payload()
        except Exception:
            payload = _build_time_payload()
        return JSONResponse(payload or {}, status_code=200)

    @router.get("/itaot", response_class=JSONResponse)
    async def identify_topic(request: Request, include_files: bool = Query(False)):
        """
        Identify-this-Pi-and-its-topics.

        Multi-sensor response (preferred):
        {
          "origin": "pi",
          "hostname": "<pi-hostname>",
          "content_encoding": "base64+zlib",
          "sensors": [
            {"SENSOR_ID": "...", "DEVICE": "...", "SERIAL_NUM": "...", "LOCATION": "...",
             "mqtt_sensor_topic": "...", "display_metrics": [...]},
            ...
          ],
          "switches": [
            {"SWITCH_DEVICE_ID": "...", "channels": ["Fan","Light"],
             "mqtt_switch_topics": { "GP28": "switch/<id>-GP28/event", ... }},
            ...
          ],
          "files": [ ... ]
        }

        Single-sensor response (Pico schema on the device itself) is a single object.
        """
        try:
            _require_protected_access(request)
            # System identity
            hostname = settings.get_setting("Network", "HOSTNAME") or "unknown-pi"

            # Sensor descriptors
            sensor_ids = settings.get_all_sensor_ids() or []
            if DEBUG:
                printDM(f"/itaot sensor_ids → {sensor_ids}", location="saiWebRoutes:itaot")

            sensor_mgr = SensorSettingsManager("sensor_settings")
            sensors_payload: list[dict] = []

            # --- helper: resolve active sensor object whether sensor_map is a dict or a list ---
            def _active_sensor_for(sid: str):
                sid_l = (sid or "").lower()
                try:
                    sm = services.sensor_map  # provided by app bootstrap
                except NameError:
                    sm = None
                if isinstance(sm, dict):
                    return sm.get(sid_l) or sm.get(sid) or sm.get(sid_l.replace("_", "-"))
                if isinstance(sm, (list, tuple, set)):
                    for obj in (sm or []):
                        if getattr(obj, "sensor_id", "").lower() == sid_l:
                            return obj
                return None

            for sensor_id in sensor_ids:
                sensor_obj = _active_sensor_for(sensor_id)

                # Load Display (optional) for UI metric hints
                try:
                    sensor_settings_doc = sensor_mgr.load(sensor_id) or {}
                except Exception as ex:
                    sensor_settings_doc = {}
                    if DEBUG:
                        printDM(f"/itaot: failed loading settings for {sensor_id}: {ex}", location="saiWebRoutes:itaot")

                display_block = sensor_settings_doc.get("Display", {}) if isinstance(sensor_settings_doc, dict) else {}
                metrics_list = _sensor_metrics_from_display_block(display_block)

                if sensor_obj is None:
                    # Fallback to TOML to still advertise configured sensors
                    sensor_blk = sensor_settings_doc.get("Sensor", {}) if isinstance(sensor_settings_doc, dict) else {}
                    sid   = sensor_blk.get("SENSOR_ID", sensor_id)
                    dev   = sensor_blk.get("DEVICE", "")
                    sn    = sensor_blk.get("SERIAL_NUM", "")
                    loc   = sensor_blk.get("LOCATION", "Unknown")
                    sensors_payload.append({
                        "SENSOR_ID": sid,
                        "DEVICE": dev,
                        "SERIAL_NUM": sn,
                        "LOCATION": loc,
                        "mqtt_sensor_topic": _topic_for_sensor(sid),
                        "display_metrics": metrics_list,
                        "metrics": metrics_list,
                    })
                    if DEBUG:
                        printDM(f"/itaot: {sensor_id} not active; advertising from TOML", location="saiWebRoutes:itaot")
                else:
                    sensors_payload.append({
                        "SENSOR_ID": sensor_obj.sensor_id,
                        "DEVICE":     sensor_obj.device,
                        "SERIAL_NUM": sensor_obj.serial_num,
                        "LOCATION":   sensor_obj.location,
                        "mqtt_sensor_topic": _topic_for_sensor(sensor_obj.sensor_id),
                        "display_metrics": metrics_list,
                        "metrics": metrics_list,
                    })

            # Switch descriptors
            switches_payload: list[dict] = []
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                switch_ids = switch_mgr.list_switches()
                for switch_id in (switch_ids or []):
                    try:
                        sw_doc = switch_mgr.load(switch_id) or {}
                        sw_blk = sw_doc.get("Switch", {}) if isinstance(sw_doc, dict) else {}
                        switch_location = sw_blk.get("SWITCH_LOCATION", "Unknown")

                        if hasattr(switch_mgr, "get_switch_channel_names"):
                            channel_names = switch_mgr.get_switch_channel_names(sw_doc) or []
                        else:
                            # Derive channel names from keys like SWITCH_1_LABEL="Fan", ignoring non-label keys.
                            channel_names = []
                            for k, v in (sw_blk or {}).items():
                                if not isinstance(v, str):
                                    continue
                                if k.startswith("SWITCH_") and k.endswith("_LABEL"):
                                    channel_names.append(v)
                    except Exception as ex:
                        channel_names = []
                        switch_location = "Unknown"
                        if DEBUG:
                            printDM(f"/itaot: switch '{switch_id}' load failed: {ex}", location="saiWebRoutes:itaot")

                    switches_payload.append({
                        "SWITCH_DEVICE_ID": switch_id,
                        "SWITCH_LOCATION": switch_location,
                        "channels": channel_names,
                        "mqtt_switch_topic": _topic_for_switch(switch_id),
                    })

            except Exception as ex:
                if DEBUG:
                    printDM(f"/itaot: switch settings probe failed: {ex}", location="saiWebRoutes:itaot")

            files_payload: list[dict] = []
            if include_files:
                # Prefer the live system settings path via saiSettings
                try:
                    active_settings_path = settings.get_active_settings_path()
                except Exception:
                    active_settings_path = None

                if active_settings_path:
                    settings_blob = _compressed_b64_or_none(Path(active_settings_path), redact_secrets=True)
                else:
                    settings_blob = _compressed_b64_or_none(Path(r"settings.toml"), redact_secrets=True)

                if settings_blob:
                    files_payload.append({
                        "name": "settings.toml",
                        "device_id": hostname,
                        "kind": "system",
                        "encoding": CONTENT_ENCODING,
                        "data": settings_blob,
                    })

                # Include each local sensor's sensor.toml
                for sensor_id in sensor_ids:
                    sensor_toml_path = _settings_base_path("sensor_settings") / sensor_id / "sensor.toml"
                    sensor_blob = _compressed_b64_or_none(sensor_toml_path, redact_secrets=True)
                    if sensor_blob:
                        files_payload.append({
                            "name": "sensor.toml",
                            "device_id": sensor_id,
                            "kind": "sensor",
                            "encoding": CONTENT_ENCODING,
                            "data": sensor_blob,
                        })

                # Include the Pi switch config(s) if present
                try:
                    for switch in (switches_payload or []):
                        switch_id = switch["SWITCH_DEVICE_ID"]
                        switch_toml_path = _settings_base_path("switch_settings") / switch_id / "switch.toml"
                        switch_blob = _compressed_b64_or_none(switch_toml_path, redact_secrets=True)
                        if switch_blob:
                            files_payload.append({
                                "name": "switch.toml",
                                "device_id": switch_id,
                                "kind": "switch",
                                "encoding": CONTENT_ENCODING,
                                "data": switch_blob,
                            })
                except Exception as ex:
                    if DEBUG:
                        printDM(f"/itaot: switch files compose failed: {ex}", location="saiWebRoutes:itaot")

            # Build multi-sensor payload
            multi_payload = {
                "app_version": SAI_APP_VERSION,
                "origin": "pi",
                "hostname": hostname,
                "content_encoding": CONTENT_ENCODING,
                "include_files": bool(include_files),
                "sensors": sensors_payload,
                "switches": switches_payload,
                "files": files_payload,
            }

            # Single vs multi-sensor response (preserve Pico back-compat)
            if len(sensors_payload) == 1 and not switches_payload:
                one = dict(sensors_payload[0])
                one["app_version"] = SAI_APP_VERSION
                one["content_encoding"] = CONTENT_ENCODING
                one["files"] = files_payload
                return one

            return multi_payload

        except HTTPException:
            raise
        except Exception as e:
            printDM(f"[/itaot] error: {e}", location=MODULE)
            return PlainTextResponse("Internal error in /itaot", status_code=500)

    # ---- onboarding progress plumbing ----
    _ONBOARD_SOCKETS: Dict[str, Set[WebSocket]] = {}
    def _get_ws_set(job_id: str) -> Set[WebSocket]:
        return _ONBOARD_SOCKETS.setdefault(job_id, set())

    async def _broadcast(job_id: str, payload: Dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in list(_get_ws_set(job_id)):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            try: _get_ws_set(job_id).discard(ws)
            except Exception: pass

    async def _emit(job_id: str, step: int, ok: bool, label: str, detail: str = ""):
        await _broadcast(job_id, {
            "step": step,          # 1..3
            "ok": ok,              # True/False
            "label": label,        # short title
            "detail": detail,      # optional
        })

    def _build_v2_time_payload() -> Dict[str, Any]:
        time_doc: Dict[str, Any] = {}
        for key in ("TZ", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP"):
            try:
                raw = settings.get_setting("Time", key, None)
            except Exception:
                raw = None
            if raw is not None:
                time_doc[key] = str(raw or "")
        try:
            raw_offset = settings.get_setting("Time", "TZ_OFFSET", None)
        except Exception:
            raw_offset = None
        if raw_offset is not None:
            try:
                time_doc["TZ_OFFSET"] = int(raw_offset)
            except Exception:
                time_doc["TZ_OFFSET"] = 0
        return time_doc

    def _build_v2_bootstrap_payload(
        *,
        onboard_token: str,
        ssid: str,
        password: str,
        hostname: str,
        broker_host_override: str | None = None,
    ) -> Dict[str, Any]:
        def _normalize_broker_host(raw_host: str, fallback_host: str) -> str:
            from . import saiAddDevice
            return saiAddDevice._hub_broker_hostname(raw_host, fallback_host)

        instance_id = (
            str(settings.get_setting("Network", "HOSTNAME", "") or "").strip()
            or socket.gethostname().strip()
            or "sensorius"
        )
        fallback_broker_host = instance_id
        broker_host = _normalize_broker_host(
            broker_host_override if broker_host_override is not None else settings.get_setting("SensorNetwork", "BROKER", ""),
            fallback_broker_host,
        )
        broker_port_raw = settings.get_setting("SensorNetwork", "MQTTPORT", 1883)
        try:
            broker_port = int(broker_port_raw)
        except Exception:
            broker_port = 1883
        if broker_port <= 0:
            broker_port = 1883
        payload: Dict[str, Any] = {
            "onboard_token": onboard_token,
            "ssid": ssid,
            "password": password,
            "hostname": hostname,
            "mqtt": {
                "broker_host": broker_host,
                "broker_port": broker_port,
            },
        }
        time_doc = _build_v2_time_payload()
        if time_doc:
            payload["time"] = time_doc
        return payload

    def _onboarding_v2_enabled() -> bool:
        raw = settings.get_setting("Onboarding", "ONBOARDING_V2_MQTT", None)
        if raw is None:
            raw = settings.get_setting("Onboarding", "onboarding_v2_mqtt", True)
        if isinstance(raw, bool):
            return raw
        text = str(raw or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return True

    def _onboarding_timeouts() -> Dict[str, float]:
        hello_timeout = float(settings.get_setting("Onboarding", "HELLO_TIMEOUT_SEC", 240) or 240)
        ack_timeout = float(settings.get_setting("Onboarding", "ACK_TIMEOUT_SEC", 10) or 10)
        result_timeout = float(settings.get_setting("Onboarding", "RESULT_TIMEOUT_SEC", 30) or 30)
        retry_backoff = float(settings.get_setting("Onboarding", "CONFIG_SET_BACKOFF_MS", 1500) or 1500) / 1000.0
        max_attempts = int(settings.get_setting("Onboarding", "CONFIG_SET_MAX_ATTEMPTS", 3) or 3)
        return {
            "hello_timeout_sec": max(5.0, hello_timeout),
            "ack_timeout_sec": max(2.0, ack_timeout),
            "result_timeout_sec": max(5.0, result_timeout),
            "retry_backoff_sec": max(0.25, retry_backoff),
            "max_attempts": max(1, max_attempts),
        }

    def _emit_onboarding_event(event_name: str, *, session_id: str = "", device_id: str = "", detail: str = "") -> None:
        payload = {
            "event": str(event_name or "").strip(),
            "session_id": str(session_id or "").strip(),
            "device_id": str(device_id or "").strip(),
            "detail": str(detail or "").strip(),
            "ts": time.time(),
        }
        try:
            printDM(f"[onboarding_event] {json.dumps(payload, separators=(',', ':'))}", location=MODULE)
        except Exception:
            pass

    def _build_v2_config_payload(device_id: str) -> Dict[str, Any]:
        """
        Build full config payload from hub-side settings managers.
        Falls back to empty section dicts when per-device docs do not exist yet.
        """
        def _to_jsonable(value: Any) -> Any:
            if isinstance(value, dict):
                return {str(k): _to_jsonable(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_to_jsonable(v) for v in value]
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            try:
                return str(value)
            except Exception:
                return None

        network_doc: Dict[str, Any] = {}
        sensor_doc: Dict[str, Any] = {}
        switch_doc: Dict[str, Any] = {}
        mqtt_doc: Dict[str, Any] = {}
        display_doc: Dict[str, Any] = {}
        calibration_doc: Dict[str, Any] = {}
        time_doc: Dict[str, Any] = {}

        try:
            sys_settings = saiSettings(apply_live=False, device_id=device_id)
            network_doc = dict(sys_settings.get_section("Network") or {})
            mqtt_doc = dict(sys_settings.get_section("MQTT") or {})
            display_doc = dict(sys_settings.get_section("Display") or {})
            calibration_doc = dict(sys_settings.get_section("Calibration") or {})
        except Exception:
            pass
        time_doc = _build_v2_time_payload()
        try:
            sensor_doc = dict(SensorSettingsManager(base_dir_name=_SENSOR_BASE_DIR).load(device_id) or {})
        except Exception:
            sensor_doc = {}
        try:
            switch_doc = dict(SwitchSettingsManager(base_dir=_SWITCH_BASE_DIR).load(device_id) or {})
        except Exception:
            switch_doc = {}

        settings_map: Dict[str, Any] = {}
        if isinstance(network_doc, dict) and network_doc:
            settings_map["Network"] = _to_jsonable(network_doc)
        if isinstance(mqtt_doc, dict) and mqtt_doc:
            settings_map["MQTT"] = _to_jsonable(mqtt_doc)
        if isinstance(time_doc, dict) and time_doc:
            settings_map["Time"] = _to_jsonable(time_doc)
        if isinstance(display_doc, dict) and display_doc:
            settings_map["Display"] = _to_jsonable(display_doc)
        if isinstance(calibration_doc, dict) and calibration_doc:
            settings_map["Calibration"] = _to_jsonable(calibration_doc)
        if isinstance(sensor_doc, dict) and sensor_doc:
            settings_map["Sensor"] = _to_jsonable(sensor_doc)
        if isinstance(switch_doc, dict) and switch_doc:
            settings_map["Switch"] = _to_jsonable(switch_doc)
        return {"settings": settings_map}

    def _publish_config_set_for_session(session_id: str, device_id: str) -> bool:
        session = onboarding_store.get_session(session_id)
        if not session:
            return False
        policy = _onboarding_timeouts()
        message_id = str(session.get("message_id", "") or "").strip()
        config_version = int(session.get("config_version", 1) or 1)
        payload_block = session.get("config_payload")
        if not isinstance(payload_block, dict):
            payload_block = _build_v2_config_payload(device_id)

        payload_bytes = json.dumps(payload_block, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checksum = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
        if not message_id:
            message_id = f"cfg-{int(time.time())}-{uuid4().hex[:8]}"
        token_secret = str(session.get("onboard_token_secret", "") or "")
        onboard_token = saiSettings.deobfuscate_secret(token_secret).strip() if token_secret else ""
        if not onboard_token:
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_onboard_token")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, device_id=device_id, detail="missing_onboard_token")
            return False

        envelope = {
            "message_id": message_id,
            "onboard_token": onboard_token,
            "config_version": config_version,
            "checksum": checksum,
            "payload": payload_block,
        }
        topic = f"nodus/{device_id}/config/set"
        ok = bool(services.mqtt_ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
        now = time.time()
        if not ok:
            _emit_onboarding_event("onboarding_failed", session_id=session_id, device_id=device_id, detail="config_publish_failed")
            return False
        current_retry = int(session.get("retry_count", 0) or 0)
        onboarding_store.update_session(
            session_id,
            state=OnboardingStates.WAITING_CONFIG_ACK,
            device_id=device_id,
            message_id=message_id,
            config_version=config_version,
            config_payload=payload_block,
            retry_count=current_retry + 1,
            last_config_sent_at=now,
            ack_deadline_at=now + float(policy["ack_timeout_sec"]),
        )
        _emit_onboarding_event("onboarding_config_sent", session_id=session_id, device_id=device_id, detail=message_id)
        return True

    async def _v2_session_monitor(session_id: str) -> None:
        policy = _onboarding_timeouts()
        while True:
            await asyncio.sleep(0.5)
            session = onboarding_store.get_session(session_id)
            if not session:
                return
            state = str(session.get("state", "") or "").strip()
            if state in {OnboardingStates.ONLINE, OnboardingStates.FAILED}:
                return

            now = time.time()
            token_exp = float(session.get("token_expires_at", 0.0) or 0.0)
            if token_exp and now > token_exp and state != OnboardingStates.ONLINE:
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="token_expired")
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="token_expired")
                return

            if state == OnboardingStates.WAITING_MQTT_HELLO:
                hello_deadline = float(session.get("hello_deadline_at", 0.0) or 0.0)
                if hello_deadline and now > hello_deadline:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="hello_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="hello_timeout")
                    return
                continue

            if state == OnboardingStates.WAITING_CONFIG_ACK:
                ack_deadline = float(session.get("ack_deadline_at", 0.0) or 0.0)
                if not ack_deadline or now <= ack_deadline:
                    continue
                retries = int(session.get("retry_count", 0) or 0)
                if retries >= int(policy["max_attempts"]):
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="config_ack_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="config_ack_timeout")
                    return
                await asyncio.sleep(float(policy["retry_backoff_sec"]))
                device_id = str(session.get("device_id", "") or session.get("expected_device_id", "") or "").strip()
                if not device_id:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_device_id")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="missing_device_id")
                    return
                _publish_config_set_for_session(session_id, device_id)
                continue

            if state == OnboardingStates.WAITING_CONFIG_RESULT:
                result_deadline = float(session.get("result_deadline_at", 0.0) or 0.0)
                if result_deadline and now > result_deadline:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="config_result_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="config_result_timeout")
                    return

    def _ensure_v2_session_monitor(session_id: str) -> None:
        def _spawn() -> None:
            existing = _v2_session_tasks.get(session_id)
            if existing and not existing.done():
                return
            _v2_session_tasks[session_id] = asyncio.create_task(_v2_session_monitor(session_id))

        existing = _v2_session_tasks.get(session_id)
        if existing and not existing.done():
            return
        try:
            running = asyncio.get_running_loop()
            if running is main_loop:
                _spawn()
                return
        except Exception:
            pass
        try:
            main_loop.call_soon_threadsafe(_spawn)
        except Exception:
            pass

    def _handle_onboarding_event(event: Dict[str, Any]) -> None:
        event_type = str(event.get("event_type", "") or "").strip()
        device_id = str(event.get("device_id", "") or "").strip()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not event_type or not device_id:
            return

        if event_type == "onboarding_hello":
            token = str(payload.get("onboard_token", "") or "").strip()
            if not token:
                return

            # Always resolve by token validation first so parallel/restarted sessions
            # with the same device id do not fail the wrong session.
            session = None
            for candidate in onboarding_store.list_active_sessions():
                sid = str(candidate.get("session_id", "") or "").strip()
                if not sid:
                    continue
                ok, _ = onboarding_tokens.validate_for_session(
                    session_id=sid,
                    token=token,
                    device_id=device_id,
                )
                if ok:
                    session = candidate
                    break
            if not session:
                return

            sid = str(session.get("session_id", "") or "").strip()
            ok, reason, _updated = onboarding_tokens.consume_for_session(
                session_id=sid,
                token=token,
                device_id=device_id,
            )
            if not ok:
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)
                return
            try:
                allow_removed = getattr(services.mqtt_ingest, "allow_nodus_devices", None)
                if callable(allow_removed):
                    allow_removed([device_id], persist=True)
                refresh_retained = getattr(services.mqtt_ingest, "refresh_nodus_retained_metadata", None)
                if callable(refresh_retained):
                    refresh_result = refresh_retained(device_id)
                    if not bool((refresh_result or {}).get("ok", False)):
                        printDM(
                            f"[onboarding] retained metadata refresh incomplete for {device_id}: {refresh_result}",
                            location=MODULE,
                        )
            except Exception as exc:
                printDM(f"[onboarding] failed to allow and refresh re-onboarded device {device_id}: {exc}", location=MODULE)
            onboarding_store.set_device_id(sid, device_id)
            onboarding_store.update_session(sid, last_hello_at=time.time(), device_id=device_id)
            _emit_onboarding_event("onboarding_hello_received", session_id=sid, device_id=device_id)
            onboarding_store.set_state(sid, OnboardingStates.CONFIG_SENDING)
            if not _publish_config_set_for_session(sid, device_id):
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason="config_publish_failed")
                return
            _ensure_v2_session_monitor(sid)
            return

        if event_type == "onboarding_config_ack":
            msg_id = str(payload.get("message_id", "") or "").strip()
            if not msg_id:
                return
            session = onboarding_store.find_active_by_device_and_message(device_id, msg_id)
            if not session:
                return
            sid = str(session.get("session_id", "") or "").strip()
            if not sid:
                return
            accepted = bool(payload.get("accepted", True))
            if not accepted:
                reason = str(payload.get("error", "") or "config_rejected")
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)
                return
            result_deadline = time.time() + float(_onboarding_timeouts()["result_timeout_sec"])
            onboarding_store.update_session(sid, state=OnboardingStates.WAITING_CONFIG_RESULT, result_deadline_at=result_deadline)
            _emit_onboarding_event("onboarding_config_ack", session_id=sid, device_id=device_id, detail=msg_id)
            return

        if event_type == "onboarding_config_result":
            msg_id = str(payload.get("message_id", "") or "").strip()
            applied = bool(payload.get("applied", False))
            if not msg_id:
                return
            session = onboarding_store.find_active_by_device_and_message(device_id, msg_id)
            if not session:
                return
            sid = str(session.get("session_id", "") or "").strip()
            if not sid:
                return
            _emit_onboarding_event("onboarding_config_result", session_id=sid, device_id=device_id, detail=msg_id)
            if applied:
                onboarding_store.update_session(sid, state=OnboardingStates.ONLINE, last_seen=time.time())
                onboarding_tokens.invalidate_session_token(sid)
                _emit_onboarding_event("onboarding_online", session_id=sid, device_id=device_id, detail=msg_id)
            else:
                reason = str(payload.get("error", "") or "config_apply_failure")
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)

    try:
        mqtt_ingest.set_onboarding_event_handler(_handle_onboarding_event)
    except Exception as e:
        if DEBUG:
            printDM(f"Failed to set onboarding MQTT callback: {e}", location=MODULE)
    for _session in onboarding_store.list_active_sessions():
        _sid = str(_session.get("session_id", "") or "").strip()
        if _sid:
            _ensure_v2_session_monitor(_sid)

    @router.post("/onboard-device/v2/prepare")
    async def onboard_prepare_v2(request: Request):
        """Create a resumable onboarding session before Wi-Fi is switched."""
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)

        form = await request.form()
        requested_device_id = str(form.get("device_id", "") or "").strip()
        session_id = uuid4().hex
        issued = onboarding_tokens.issue_token(
            session_id=session_id,
            expected_device_id=requested_device_id,
            ttl_sec=600,
        )
        onboarding_store.set_state(session_id, OnboardingStates.AP_DISCOVERED)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "state": OnboardingStates.AP_DISCOVERED,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": requested_device_id,
            }
        )

    @router.post("/onboard-device/v2/start")
    async def onboard_start_v2(request: Request):
        """
        V2 bootstrap start:
        1) Connect to Nodus AP
        2) Issue short-lived single-use token (stored hashed)
        3) POST /itaot-init with minimal bootstrap payload
        4) Persist WAITING_MQTT_HELLO state for resume/correlation
        """
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)

        from . import saiAddDevice
        form = await request.form()
        factory_target_ap = (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
        target_ap = str(form.get("target_ap", "") or "").strip() or factory_target_ap
        target_ap_password_raw = form.get("target_ap_password")
        if target_ap_password_raw is None and saiAddDevice.is_nodus_setup_ssid(target_ap):
            target_ap_password = str(saiAddDevice.PICOW_AP_PASSWORD or "")
        else:
            target_ap_password = str(target_ap_password_raw or "")
        local_ssid = str(form.get("local_ssid", "") or "").strip()
        local_password = str(form.get("local_password", "") or "")
        requested_device_id = str(form.get("device_id", "") or "").strip()
        hostname = requested_device_id or f"nodus-{uuid4().hex[:8]}"
        sys_name = platform.system().lower()

        prepared_session_id = str(form.get("session_id", "") or "").strip()
        prepared_session = onboarding_store.get_session(prepared_session_id) if prepared_session_id else None
        if prepared_session_id:
            if not prepared_session:
                return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)
            prepared_state = str(prepared_session.get("state", "") or "").strip()
            if prepared_state != OnboardingStates.AP_DISCOVERED:
                return JSONResponse(
                    {"ok": False, "session_id": prepared_session_id, "error": "session_not_ready", "state": prepared_state},
                    status_code=409,
                )
            session_id = prepared_session_id
            token_secret = str(prepared_session.get("onboard_token_secret", "") or "")
            token = saiSettings.deobfuscate_secret(token_secret).strip() if token_secret else ""
            if not token:
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_onboard_token")
                return JSONResponse(
                    {"ok": False, "session_id": session_id, "error": "missing_onboard_token"},
                    status_code=409,
                )
            if not requested_device_id:
                requested_device_id = str(prepared_session.get("expected_device_id", "") or "").strip()
                if requested_device_id:
                    hostname = requested_device_id
            issued = {"expires_at": prepared_session.get("token_expires_at")}
        else:
            session_id = uuid4().hex
            issued = onboarding_tokens.issue_token(
                session_id=session_id,
                expected_device_id=requested_device_id,
                ttl_sec=600,
            )
            token = issued["token"]
            onboarding_store.set_state(session_id, OnboardingStates.AP_DISCOVERED)

        if not local_ssid and sys_name != "darwin":
            try:
                resolved_ssid, resolved_password = await asyncio.get_event_loop().run_in_executor(
                    None,
                    saiAddDevice.resolve_pi_wifi_credentials,
                )
                local_ssid = (resolved_ssid or "").strip()
                if not local_password:
                    local_password = resolved_password or ""
            except Exception:
                pass

        if sys_name == "darwin" and saiAddDevice.is_nodus_setup_ssid(local_ssid):
            # A manual macOS join happens before Add is clicked, so resolving
            # credentials now can only discover the setup AP, not the network
            # that the Nodus must ultimately join.
            local_ssid = ""
            local_password = ""

        async def _restore_local_wifi_on_failure() -> None:
            if sys_name == "darwin":
                return
            restore_ssid = (local_ssid or "").strip()
            if not restore_ssid:
                return
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.reconnect_to_network(restore_ssid, local_password, max_attempts=3, delay_sec=1.5),
                )
            except Exception as e:
                printDM(f"[onboard-v2] failed to restore Wi-Fi '{restore_ssid}': {e}", location="saiWebRoutes")

        async def _restore_local_wifi_on_success() -> tuple[bool, str]:
            restore_ssid = (local_ssid or "").strip()
            if not restore_ssid:
                return False, ""
            try:
                ok_restore, resolved_ssid = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.reconnect_to_network(restore_ssid, local_password, max_attempts=5, delay_sec=2.0),
                )
                return bool(ok_restore), str(resolved_ssid or restore_ssid).strip()
            except Exception as e:
                printDM(f"[onboard-v2] failed to restore Wi-Fi '{restore_ssid}' after bootstrap: {e}", location="saiWebRoutes")
                return False, restore_ssid

        ok_ap = False
        current_ap_ssid = ""
        meta_result: dict[str, Any] | None = None
        if sys_name == "darwin":
            # networksetup returns -3900 and stale association state on current
            # macOS even when the host has joined the Nodus AP. Treat the Wi-Fi
            # address plus the Nodus HTTP endpoint as the source of truth.
            on_nodus_subnet = await asyncio.get_event_loop().run_in_executor(
                None,
                saiAddDevice.mac_wifi_is_on_nodus_subnet,
            )
            if on_nodus_subnet:
                meta_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.get_itaot_meta(timeout_sec=3.0),
                )
                ok_ap = bool(meta_result.get("ok", False))
                if ok_ap:
                    current_ap_ssid = target_ap
        else:
            try:
                current_ap_ssid = await asyncio.get_event_loop().run_in_executor(
                    None,
                    getattr(saiAddDevice, "_get_current_ssid"),
                )
            except Exception:
                current_ap_ssid = ""

        if sys_name == "linux":
            can_control_network, permission_detail = await asyncio.get_event_loop().run_in_executor(
                None,
                saiAddDevice.linux_network_control_permission_status,
            )
            if not can_control_network:
                reason = "network_control_not_authorized"
                detail = (
                    "Sensorius is not authorized to control NetworkManager "
                    f"({permission_detail}). Grant NetworkManager control to the sensorius.service user, "
                    "then restart Sensorius."
                )
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=reason)
                return JSONResponse(
                    {
                        "ok": False,
                        "session_id": session_id,
                        "state": OnboardingStates.FAILED,
                        "error": reason,
                        "detail": detail,
                    },
                    status_code=403,
                )

        ok_ap = ok_ap or (current_ap_ssid or "").strip() == target_ap
        if not ok_ap:
            if sys_name == "darwin":
                reason = "manual_ap_join_required"
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=reason)
                return JSONResponse(
                    {
                        "ok": False,
                        "session_id": session_id,
                        "state": OnboardingStates.FAILED,
                        "error": reason,
                        "detail": "Join a Nodus setup network in macOS Wi-Fi, then click Add again.",
                    },
                    status_code=409,
                )
            try:
                ok_ap = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.connect_to_sensor_ap(target_ap, target_ap_password, attempts=3),
                )
            except Exception as e:
                await _restore_local_wifi_on_failure()
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=f"ap_connect_error:{e}")
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"ap_connect_error:{e}")
                return JSONResponse(
                    {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "ap_connect_error"},
                    status_code=502,
                )

        if not ok_ap:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="ap_connect_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="ap_connect_failed")
            detail = ""
            if sys_name == "darwin":
                detail = f"Could not join {target_ap} automatically. Join it from Other Networks, then click Add again."
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "ap_connect_failed",
                    "detail": detail,
                },
                status_code=502,
            )

        if not local_ssid:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_local_ssid")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="missing_local_ssid")
            return JSONResponse(
                {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "missing_local_ssid"},
                status_code=400,
            )

        if meta_result is None:
            meta_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: saiAddDevice.get_itaot_meta(timeout_sec=5.0),
            )
        meta_body = meta_result.get("body") if isinstance(meta_result, dict) else None
        if not bool(meta_result.get("ok", False)):
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="meta_fetch_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"meta_fetch_failed:{meta_result.get('error', '')}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "meta_fetch_failed",
                    "detail": str(meta_result.get("error", "")),
                },
                status_code=502,
            )

        meta_device_id = saiAddDevice._extract_device_id_from_meta(meta_body or {}, requested_device_id or hostname)
        if meta_device_id:
            hostname = meta_device_id
        if not requested_device_id:
            requested_device_id = meta_device_id

        onboarding_store.set_state(session_id, OnboardingStates.INIT_SENDING)
        _emit_onboarding_event("onboarding_init_sent", session_id=session_id, detail=hostname)
        init_payload = _build_v2_bootstrap_payload(
            onboard_token=token,
            ssid=local_ssid,
            password=local_password,
            hostname=hostname,
        )
        init_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: saiAddDevice.post_itaot_init(init_payload, timeout_sec=11.0),
        )
        init_response_indeterminate = bool(init_result.get("indeterminate", False))
        if not bool(init_result.get("ok", False)) and not init_response_indeterminate:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="INIT_FAILED")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"INIT_FAILED:{init_result.get('error', '')}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "INIT_FAILED",
                    "detail": str(init_result.get("error", "")),
                },
                status_code=502,
            )

        onboarding_store.update_session(
            session_id,
            local_ssid=local_ssid,
            init_response_indeterminate=init_response_indeterminate,
            init_response_error=(
                str(init_result.get("error", "")) if init_response_indeterminate else ""
            ),
        )
        onboarding_store.set_state(session_id, OnboardingStates.INIT_SENT)
        _emit_onboarding_event(
            "onboarding_init_response_indeterminate" if init_response_indeterminate else "onboarding_init_ack",
            session_id=session_id,
            detail=hostname,
        )
        restore_pending = False
        if sys_name == "darwin":
            ok_restore, _restored_ip = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: saiAddDevice.wait_for_macos_local_network(timeout_sec=15.0),
            )
            restored_ssid = local_ssid
            restore_pending = not ok_restore
        else:
            ok_restore, restored_ssid = await _restore_local_wifi_on_success()
        if not ok_restore and sys_name != "darwin":
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="local_wifi_restore_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"local_wifi_restore_failed:{restored_ssid or local_ssid}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "local_wifi_restore_failed",
                    "detail": str(restored_ssid or local_ssid or ""),
                },
                status_code=502,
            )

        onboarding_store.update_session(
            session_id,
            state=OnboardingStates.WAITING_REBOOT,
            local_ssid=str(restored_ssid or local_ssid or "").strip(),
            local_wifi_restored_at=time.time() if ok_restore else None,
            local_wifi_restore_pending=restore_pending,
        )
        hello_deadline = time.time() + float(_onboarding_timeouts()["hello_timeout_sec"])
        onboarding_store.update_session(session_id, state=OnboardingStates.WAITING_MQTT_HELLO, hello_deadline_at=hello_deadline)
        _ensure_v2_session_monitor(session_id)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "state": OnboardingStates.WAITING_MQTT_HELLO,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": requested_device_id,
                "local_ssid": str(restored_ssid or local_ssid or "").strip(),
                "local_wifi_restore_pending": restore_pending,
                "init_response_indeterminate": init_response_indeterminate,
            }
        )

    @router.get("/api/biodynamic-calendar", response_class=JSONResponse)
    async def api_biodynamic_calendar(month: str = Query("", description="Month anchor in YYYY-MM or YYYY-MM-DD")):
        _route_started = time.monotonic()
        anchor: date
        try:
            raw = str(month or "").strip()
            if not raw:
                anchor = get_biodynamic_local_now().date().replace(day=1)
            elif len(raw) == 7:
                anchor = datetime.strptime(raw, "%Y-%m").date().replace(day=1)
            else:
                anchor = datetime.fromisoformat(raw).date().replace(day=1)
        except Exception:
            return JSONResponse({"error": "invalid_month"}, status_code=400)

        summary_status = "skipped"
        summary_ms = 0.0
        try:
            today_local = datetime.now(getattr(services.data_logger, "local_tz", ZoneInfo("America/Denver"))).date()
            summary_status, summary_ms = _request_biodynamic_summary_window(today_local)
        except Exception as exc:
            if DEBUG:
                printDM(f"[api_biodynamic_calendar] daily summary backfill skipped: {exc}", location=MODULE)

        payload_started = time.monotonic()
        payload = await _get_cached_biodynamic_payload_async(anchor)
        payload_ms = (time.monotonic() - payload_started) * 1000.0
        notes_started = time.monotonic()
        calendar_days = payload.get("calendar") or []
        visible_dates = [
            datetime.fromisoformat(str(day.get("date"))).date()
            for day in calendar_days
            if isinstance(day, dict) and day.get("date")
        ]
        if visible_dates:
            range_start = min(visible_dates)
            range_end = max(visible_dates)
            try:
                notes, daily_summaries = await asyncio.gather(
                    asyncio.to_thread(
                        services.data_logger.get_biodynamic_notes_for_range,
                        range_start,
                        range_end,
                        raise_on_error=True,
                    ),
                    asyncio.to_thread(services.data_logger.get_biodynamic_daily_summaries_for_range, range_start, range_end),
                )
            except BiodynamicStorageError:
                return JSONResponse({"ok": False, "error": "storage_unavailable"}, status_code=503)
            try:
                resolved = await asyncio.to_thread(
                    settings.resolve_astral_location,
                    persist_if_auto=False,
                    timeout_sec=2.5,
                )
                if resolved.get("lat") is not None and resolved.get("lon") is not None and resolved.get("tz"):
                    repair_dates = [
                        visible_date
                        for visible_date in visible_dates
                        if daily_summary_service.summary_needs_location_repair(
                            daily_summaries.get(visible_date.isoformat(), "")
                        )
                    ]
                    if repair_dates:
                        await asyncio.to_thread(daily_summary_service.repair_summaries_for_dates, repair_dates)
                        daily_summaries = await asyncio.to_thread(
                            services.data_logger.get_biodynamic_daily_summaries_for_range,
                            range_start,
                            range_end,
                        )
            except Exception as exc:
                if DEBUG:
                    printDM(f"[api_biodynamic_calendar] visible summary repair skipped: {exc}", location=MODULE)
            payload["notes"] = notes
            payload["daily_summaries"] = daily_summaries
        else:
            try:
                notes, daily_summaries = await asyncio.gather(
                    asyncio.to_thread(services.data_logger.get_biodynamic_notes_for_month, anchor, raise_on_error=True),
                    asyncio.to_thread(services.data_logger.get_biodynamic_daily_summaries_for_month, anchor),
                )
            except BiodynamicStorageError:
                return JSONResponse({"ok": False, "error": "storage_unavailable"}, status_code=503)
            payload["notes"] = notes
            payload["daily_summaries"] = daily_summaries
        notes_ms = (time.monotonic() - notes_started) * 1000.0
        _ui_profile_log(
            "api-biodynamic-calendar",
            _route_started,
            anchor=anchor.isoformat(),
            summary_status=summary_status,
            summary_ms=f"{summary_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
            notes_ms=f"{notes_ms:.1f}",
        )
        return JSONResponse(payload)

    @router.get("/api/weather-alerts", response_class=JSONResponse)
    async def api_weather_alerts():
        """Return cached official NWS weather events for the configured location."""
        service = getattr(services.data_logger, "weather_alert_service", None)
        payload = service.snapshot() if service else {
            "enabled": False, "active": False, "events": [], "message": "", "reason": "unavailable",
        }
        payload["pending_alerts"] = service.pending_alerts() if service else []
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.post("/api/weather-alerts/{alert_id}/dismiss")
    async def dismiss_weather_alert(alert_id: str):
        """Acknowledge a weather Alert across browsers and application restarts."""
        service = getattr(services.data_logger, "weather_alert_service", None)
        if service is None or not service.dismiss_alert(alert_id):
            return JSONResponse({"ok": False}, status_code=404)
        return JSONResponse({"ok": True})

    @router.get("/api/weather-forecast", response_class=JSONResponse)
    async def api_weather_forecast(
        days: int = Query(6, ge=1, le=6),
        force_refresh: bool = Query(False),
    ):
        _route_started = time.monotonic()
        try:
            forecast_app_service = getattr(app.state, "weather_forecast_app_service", None)
            canonical_forecast = getattr(forecast_app_service, "canonical_forecast", None)
            if callable(canonical_forecast):
                payload = await canonical_forecast(force_refresh=bool(force_refresh))
            else:
                forecast_settings = saiSettings(make_startup_backup=False, apply_live=False)
                payload = await get_weather_forecast_payload(
                    forecast_settings,
                    db_path=str(getattr(services.data_logger, "db_path", "sensorius_data.db") or "sensorius_data.db"),
                    force_refresh=bool(force_refresh),
                    min_days=int(days),
                    timeout_sec=8.0,
                )
            display_unit_system = normalize_display_unit_system(
                settings.get_setting("Display", "unit_system", "Imperial")
            )
            display_forecast = build_weather_display_forecast(payload, display_unit_system)
            payload = copy.deepcopy(payload)
            current_24h = payload.get("current_24h")
            if isinstance(current_24h, dict):
                current_24h["synopsis"] = display_forecast.get("synopsis")
                current_24h["temp_range"] = display_forecast.get("temp_range") or current_24h.get("temp_range")
            payload["unit_system"] = display_unit_system
            if forecast_app_service is not None:
                payload["current_readings"] = await asyncio.to_thread(forecast_app_service.current_readings)
            if isinstance(payload.get("days"), list):
                payload["days"] = payload["days"][:days]
            _ui_profile_log(
                "api-weather-forecast",
                _route_started,
                ok=bool(payload.get("ok")),
                provider=str(payload.get("provider") or ""),
                stale=bool(payload.get("stale", False)),
            )
            return JSONResponse(payload)
        except Exception as exc:
            if DEBUG:
                printDM(f"[api_weather_forecast] failed: {exc}", location=MODULE)
            return JSONResponse(
                {
                    "ok": False,
                    "stale": False,
                    "reason": "forecast_failed",
                    "detail": str(exc),
                    "current_24h": {},
                    "days": [],
                }
            )

    @router.post("/api/biodynamic-note", response_class=JSONResponse)
    async def api_biodynamic_note(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        note_date = str((payload or {}).get("date", "") or "").strip()
        note_text = str((payload or {}).get("note", "") or "")
        if not note_date:
            return JSONResponse({"error": "missing_date"}, status_code=400)
        try:
            normalized_date = datetime.fromisoformat(note_date).date().isoformat()
        except Exception:
            return JSONResponse({"error": "invalid_date"}, status_code=400)

        ok = await asyncio.to_thread(services.data_logger.save_biodynamic_note, normalized_date, note_text)
        if not ok:
            return JSONResponse({"error": "save_failed"}, status_code=500)
        return JSONResponse({"ok": True, "date": normalized_date, "note": note_text.strip()})

    @router.get("/onboard-device/v2/session/{session_id}")
    async def onboard_session_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"error": "onboarding_v2_disabled"}, status_code=409)
        session = onboarding_store.get_session(session_id)
        if not session:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        safe = dict(session)
        safe.pop("onboard_token_hash", None)
        safe.pop("onboard_token_secret", None)
        return JSONResponse(safe)

    @router.get("/onboard-device/v2/sessions")
    async def onboard_sessions_v2(active_only: bool = Query(True)):
        if not _onboarding_v2_enabled():
            return JSONResponse({"error": "onboarding_v2_disabled"}, status_code=409)
        sessions = onboarding_store.list_active_sessions() if active_only else onboarding_store.list_sessions()
        redacted: list[Dict[str, Any]] = []
        for s in sessions:
            one = dict(s)
            one.pop("onboard_token_hash", None)
            one.pop("onboard_token_secret", None)
            redacted.append(one)
        return JSONResponse({"count": len(redacted), "items": redacted})

    @router.post("/onboard-device/v2/retry/{session_id}")
    async def onboard_retry_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)
        session = onboarding_store.get_session(session_id)
        if not session:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        state = str(session.get("state", "") or "").strip()
        device_id = str(session.get("device_id", "") or session.get("expected_device_id", "") or "").strip()
        if state == OnboardingStates.WAITING_MQTT_HELLO:
            hello_deadline = time.time() + float(_onboarding_timeouts()["hello_timeout_sec"])
            onboarding_store.update_session(session_id, hello_deadline_at=hello_deadline)
            _ensure_v2_session_monitor(session_id)
            return JSONResponse({"ok": True, "session_id": session_id, "state": state})
        if state in {OnboardingStates.WAITING_CONFIG_ACK, OnboardingStates.CONFIG_SENDING} and device_id:
            if not _publish_config_set_for_session(session_id, device_id):
                return JSONResponse({"ok": False, "error": "config_publish_failed"}, status_code=502)
            _ensure_v2_session_monitor(session_id)
            return JSONResponse({"ok": True, "session_id": session_id, "state": OnboardingStates.WAITING_CONFIG_ACK})
        return JSONResponse({"ok": False, "error": "retry_not_allowed_for_state", "state": state}, status_code=409)

    @router.post("/onboard-device/v2/restart/{session_id}")
    async def onboard_restart_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)
        old = onboarding_store.get_session(session_id)
        if not old:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        expected_device_id = str(old.get("expected_device_id", "") or old.get("device_id", "") or "").strip()
        new_id = uuid4().hex
        issued = onboarding_tokens.issue_token(
            session_id=new_id,
            expected_device_id=expected_device_id,
            ttl_sec=600,
        )
        onboarding_store.update_session(
            new_id,
            state=OnboardingStates.AP_DISCOVERED,
            restart_of=session_id,
        )
        return JSONResponse(
            {
                "ok": True,
                "session_id": new_id,
                "state": OnboardingStates.AP_DISCOVERED,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": expected_device_id,
            }
        )

    # lightweight connection/health check


    # ws client subscribes with job_id
    @router.websocket("/ws/onboard/{job_id}")
    async def ws_onboard_progress(websocket: WebSocket, job_id: str):
        await websocket.accept()
        _get_ws_set(job_id).add(websocket)
        try:
            # greet (lets UI mark “connected to progress channel” if desired)
            await websocket.send_json({"hello": True, "job_id": job_id})
            # keep alive until client disconnects
            while True:
                # optional ping/pong or just sleep
                await asyncio.sleep(30)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            printDM(f"[ws_onboard_progress] {e}", location="saiWebRoutes")
        finally:
            try: _get_ws_set(job_id).discard(websocket)
            except Exception: pass

    @router.get("/onboard-device", response_class=HTMLResponse)
    async def onboard_device_page():
        # serve a tiny page that only contains the modal+script block from step (2)
        # (You can inline the exact same HTML/JS or import a renderer if you prefer)
        html = """
        <html><head><title>Add Device</title></head><body style="background:#F5FFFA;">
        <!-- Paste the same modal + <script> from step (2) here -->
        <script>window.onload = function(){ openOnboardModal(); };</script>
        </body></html>
        """
        return HTMLResponse(html)

    @router.post("/onboard-device")
    async def onboard_start(request: Request):
        """
        Starts the Pico2 W (or switch) onboarding flow and returns a job_id.
        Frontend then connects to /ws/onboard/{job_id} to receive step updates.
        """
        from . import saiAddDevice
        form = await request.form()
        # Pull what your System Setup dialog already posts.
        # These names are examples; keep them aligned with your current form fields:
        sensor_type = form.get("sensor_type", "")
        location    = form.get("location", "Unknown")
        local_ssid  = form.get("local_ssid", "Unknown")
        factory_target_ap = (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
        target_ap = str(form.get("target_ap", "") or "").strip() or factory_target_ap
        target_ap_password_raw = form.get("target_ap_password")
        if target_ap_password_raw is None and saiAddDevice.is_nodus_setup_ssid(target_ap):
            target_ap_password = str(saiAddDevice.PICOW_AP_PASSWORD or "")
        else:
            target_ap_password = str(target_ap_password_raw or "")
        # You may already assemble a richer onboarding payload; pass it through:
        payload_json = form.get("payload_json")  # optional richer JSON blob

        job_id = uuid4().hex
        printDM(f"[onboard-start] job_id={job_id} sensor_type={sensor_type} location={location}", location="saiWebRoutes")

        async def run_flow():
            # Step 1: AP connect
            label1 = f"{target_ap} connection established"
            ok1 = False
            try:
                ok1 = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.connect_to_sensor_ap(
                        target_ap,
                        target_ap_password,
                        attempts=3
                    )
                )
            except Exception as e:
                printDM(f"[onboard] connect_to_sensor_ap failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 1, bool(ok1), label1)

            sensor_id_for_step = "Unknown"

            # Step 2: configure + reboot (no reconnect here)
            label2 = "configured and rebooting"
            ok2 = False
            if ok1:
                try:
                    ok2, maybe_sensor_id = await asyncio.get_event_loop().run_in_executor(
                        None, saiAddDevice.perform_picow_configure_and_reboot
                    )
                    if maybe_sensor_id:
                        sensor_id_for_step = maybe_sensor_id
                except Exception as e:
                    printDM(f"[onboard] perform_picow_configure_and_reboot failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 2, bool(ok2), f"{sensor_id_for_step} {label2}")

            # Step 3: reconnect Pi to its local SSID (and update CLIENTS if step 2 succeeded)
            ok3 = False
            conn_ssid = "Unknown"
            label3 = f"Reconnecting to {conn_ssid}"
            try:
                ok3, conn_ssid = await asyncio.get_event_loop().run_in_executor(None, saiAddDevice.reconnect_to_pi)
                label3 = f"Reconnecting to {conn_ssid}"
            except Exception as e:
                printDM(f"[onboard] reconnect_to_pi failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 3, bool(ok3), label3)

            if ok2 and sensor_id_for_step:
                try:
                    saiAddDevice.update_hub_clients(saiAddDevice.get_hub_settings_path(), sensor_id_for_step)
                except Exception as e:
                    printDM(f"[onboard] update_hub_clients failed: {e}", location="saiWebRoutes")
                # Nudge ingest discovery immediately (no restart required)
                try:
                    allow_removed = getattr(services.mqtt_ingest, "allow_nodus_devices", None)
                    if callable(allow_removed):
                        allow_removed([sensor_id_for_step], persist=True)
                    # match the form you persist in CLIENTS (you showed ".local")
                    host_for_ingest = mdns_hostname(sensor_id_for_step)
                    services.mqtt_ingest.add_client(host_for_ingest)
                    printDM(f"[onboard] nudged discovery for {host_for_ingest}", location="saiWebRoutes")
                except Exception as e:
                    printDM(f"[onboard] add_client nudge failed: {e}", location="saiWebRoutes")


            await _broadcast(job_id, {"done": True})

        asyncio.create_task(run_flow())
        return JSONResponse({"job_id": job_id})

    # ---------------- Nodus hostname + POST helpers ----------------
    _nodus_host_locks: dict[str, asyncio.Lock] = {}
    def _get_host_lock(host: str) -> asyncio.Lock:
        lock = _nodus_host_locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            _nodus_host_locks[host] = lock
        return lock
    
    def _normalize_system_settings_root(system_root: str | None) -> str:
        """Return the absolute `system_settings` directory for a candidate root."""
        import os

        text = str(system_root or "").strip()
        if not text:
            return str(_settings_base_path("system_settings"))
        try:
            root_path = _settings_base_path(text)
            root = str(root_path)
        except Exception:
            return text
        if os.path.basename(root) == "system_settings":
            return root
        nested = os.path.join(root, "system_settings")
        if os.path.isdir(nested):
            return os.path.abspath(nested)
        return root

    def _resolve_system_settings_root(system_mgr=None) -> str:
        """Resolve the on-disk `system_settings` root from manager or app settings."""
        import os

        candidates: list[str | None] = []
        class_default_root = getattr(saiSettings, "DEFAULT_BASE_DIR", None)
        if class_default_root:
            try:
                if os.path.isabs(str(class_default_root)):
                    candidates.append(str(class_default_root))
            except Exception:
                pass
        try:
            app_settings = saiSettings(apply_live=False)
            candidates.extend([
                getattr(app_settings, "base_dir", None),
                getattr(app_settings, "system_dir", None),
                getattr(app_settings, "settings_root", None),
            ])
        except Exception:
            pass
        if system_mgr is not None:
            candidates.extend([
                getattr(system_mgr, "base_dir", None),
                getattr(system_mgr, "system_dir", None),
                getattr(system_mgr, "settings_root", None),
            ])

        for cand in candidates:
            root = _normalize_system_settings_root(cand)
            if os.path.isdir(root):
                return root
        return _normalize_system_settings_root("system_settings")

    def _build_system_hostname_index(system_root: str) -> dict[str, str]:
        """
        Scan system_settings/*/settings.toml and return {serial_suffix: HOSTNAME}.
        E.g. {'nz6g89': 'aqi-nz6g89', 'oe2ed6': 'aqi-oe2ed6', ...}
        """
        import os
        try:
            import tomllib
        except Exception:
            tomllib = None

        serial_to_host: dict[str, str] = {}
        if not tomllib:
            return serial_to_host

        system_root = _normalize_system_settings_root(system_root)
        try:
            for name in os.listdir(system_root):
                if name in {"factory", "__pycache__"}:
                    continue
                settings_path = os.path.join(system_root, name, "settings.toml")
                if not os.path.isfile(settings_path):
                    continue
                try:
                    with open(settings_path, "rb") as f:
                        data = tomllib.load(f) or {}
                    host = (data.get("Network", {}).get("HOSTNAME") or "").strip()
                    if host and "-" in host:
                        serial = host.rsplit("-", 1)[-1]
                        prev = str(serial_to_host.get(serial) or "").strip()
                        # Prefer the sensor-hosted device name over a synthetic
                        # switch-only hostname when both folders exist for the
                        # same physical Sensor+Switch Nodus serial.
                        if prev and prev.lower().startswith("switch-") and not host.lower().startswith("switch-"):
                            serial_to_host[serial] = host
                        elif not prev or not host.lower().startswith("switch-"):
                            serial_to_host[serial] = host
                except Exception:
                    continue
        except Exception:
            pass
        return serial_to_host

    def _read_hostname_from_system_settings(
        device_id: str,
        system_mgr=None,
        system_root: str | None = None,
        *,
        device_type: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> str | None:
        """
        Truth source: system_settings/<system-id>/settings.toml → [Network].HOSTNAME.

        For sensors, <system-id> == sensor-id.
        For switches on a sensor+switch Nodus, <system-id> == sensor-id with same serial
        (we find it via sys_host_index).
        For switch-only Nodus, <system-id> == 'switch-<serial>'.
        """
        ingest = services.mqtt_ingest
        try:
            if ingest and getattr(ingest, 'resolve_nodus_hostname', None):
                host = ingest.resolve_nodus_hostname(device_id, device_type=device_type)
                if host:
                    return host
        except Exception:
            pass

        dev_id = (device_id or "").strip()
        if not dev_id:
            return None

        serial = (dev_id.rsplit("-", 1)[-1] if "-" in dev_id else dev_id).strip()

        def _sensor_settings_manager():
            mgr_cls = globals().get("SensorSettingsManager", None)
            if mgr_cls is not None:
                try:
                    return mgr_cls("sensor_settings")
                except TypeError:
                    return mgr_cls()
            try:
                from .saiSensorSettingsManager import SensorSettingsManager as _SensorSettingsManager
                return _SensorSettingsManager("sensor_settings")
            except TypeError:
                return _SensorSettingsManager()
            except Exception:
                return None

        # Child sensor shadows persist the physical Nodus identity. This keeps
        # settings and restart commands routed correctly before retained meta
        # has rebuilt the ingest maps after a Sensorius restart.
        if (device_type or "").lower() == "sensor":
            try:
                sm = _sensor_settings_manager()
                doc = sm.load(dev_id) if sm else {}
                nodus = doc.get("Nodus", {}) if isinstance(doc, dict) else {}
                host = str((nodus or {}).get("DEVICE_ID") or "").strip()
                if host:
                    return host
            except Exception:
                pass

        def _paired_sensor_host_for_switch() -> str | None:
            if not serial:
                return None
            try:
                sm = _sensor_settings_manager()
                if not sm:
                    return None
                for sid in (sm.list_ids() or []):
                    sid = str(sid or "").strip()
                    if not sid or sid.lower().startswith("switch-"):
                        continue
                    if sid.endswith(f"-{serial}"):
                        paired_host = _read_hostname_from_system_settings(
                            sid,
                            system_mgr,
                            system_root,
                            device_type="sensor",
                            sys_host_index=sys_host_index,
                        )
                        if paired_host:
                            return str(paired_host).strip()
            except Exception:
                pass
            return None

        # For shared Sensor+Switch Nodus devices, prefer the physical host
        # derived from the system hostname index before consulting any stale
        # switch-specific folder that may still advertise "switch-<serial>".
        if (device_type or "").lower() == "switch" and serial:
            indexed_host = str((sys_host_index or {}).get(serial) or "").strip()
            if indexed_host:
                return indexed_host
            paired_host = _paired_sensor_host_for_switch()
            if paired_host:
                return paired_host

        # Fast-path for sensors: try manager then direct file
        if (device_type or "").lower() == "sensor":
            try:
                if system_mgr and hasattr(system_mgr, "get_setting"):
                    host = (system_mgr.get_setting(dev_id, "Network.HOSTNAME", "") or "").strip()
                    if host:
                        return host
            except Exception:
                pass

        # Resolve system_root
        if not system_root:
            system_root = _resolve_system_settings_root(system_mgr)
        else:
            system_root = _normalize_system_settings_root(system_root)

        import os
        try:
            import tomllib
        except Exception:
            tomllib = None

        # Direct file read for sensors and true switch-only devices.
        # For shared Sensor+Switch Nodus devices we intentionally prefer the
        # paired sensor host above, before consulting the switch folder here.
        settings_path = os.path.join(system_root, dev_id, "settings.toml")
        if os.path.isfile(settings_path) and tomllib:
            try:
                with open(settings_path, "rb") as f:
                    data = tomllib.load(f) or {}
                host = (data.get("Network", {}).get("HOSTNAME") or "").strip()
                if host:
                    return host
            except Exception:
                pass

        # If it's a switch and ingest didn't know yet, derive host from paired sensor id
        if (device_type or "").lower() == "switch":
            if serial:
                paired_host = _paired_sensor_host_for_switch()
                if paired_host:
                    return paired_host
                # 2a) Try to match any known mqtt_clients like '<anything>-<serial>'
                try:
                    if ingest and getattr(ingest, 'mqtt_clients', None):
                        for cand in (ingest.mqtt_clients or []):
                            cand = str(cand)
                            if cand.endswith(f"-{serial}"):
                                return cand  # bare hostname, e.g. "aqi-nz6g89"
                except Exception:
                    pass

                # 2b) Try to match any known sensor_id in sensor settings
                try:
                    sm = _sensor_settings_manager()
                    if not sm:
                        return None
                    for sid in (sm.list_ids() or []):
                        sid = str(sid)
                        if sid.lower().startswith("switch-"):
                            continue
                        if sid.endswith(f"-{serial}"):
                            return sid  # use the sensor_id as the host (bare)
                except Exception:
                    pass

        return None

    def _read_system_settings_doc(system_root: str, device_id: str | None) -> dict[str, Any]:
        dev = normalize_hostname_base(str(device_id or "").strip()) or str(device_id or "").strip()
        if not dev:
            return {}
        root = _normalize_system_settings_root(system_root)
        path = os.path.join(root, dev, "settings.toml")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _settings_value(section: dict | None, *keys: str) -> str:
        if not isinstance(section, dict):
            return ""
        for key in keys:
            value = section.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _broker_from_network_doc(doc: dict[str, Any]) -> str:
        mqtt_doc = doc.get("MQTT") if isinstance(doc, dict) else {}
        sensor_network_doc = doc.get("SensorNetwork") if isinstance(doc, dict) else {}
        network_doc = doc.get("Network") if isinstance(doc, dict) else {}
        broker = (
            _settings_value(mqtt_doc, "BROKER", "broker")
            or _settings_value(sensor_network_doc, "BROKER", "broker")
            or _settings_value(network_doc, "BROKER", "broker")
        )
        if broker:
            return broker
        try:
            return str(settings.get_setting("SensorNetwork", "BROKER", "") or "").strip()
        except Exception:
            return ""

    def _broker_health_status(broker: str, ingest=None) -> str:
        if not str(broker or "").strip():
            return "Not configured"
        if ingest is None:
            return "Unavailable"

        connected: bool | None = None
        try:
            client = getattr(ingest, "client", None)
            checker = getattr(client, "is_connected", None)
            if callable(checker):
                connected = bool(checker())
        except Exception:
            connected = None

        if connected is None:
            try:
                event = getattr(ingest, "_connected_evt", None)
                is_set = getattr(event, "is_set", None)
                if callable(is_set):
                    connected = bool(is_set())
            except Exception:
                connected = None

        if connected is True:
            return "Connected"
        if connected is False:
            return "Disconnected"
        return "Unknown"

    def _runtime_ipv4_from_ingest(
        device_id: str,
        *,
        device_type: str,
        source_host: str,
        host_base: str,
        ingest=None,
    ) -> str:
        if ingest is None:
            return ""

        def _norm_ipv4(value) -> str:
            raw = str(value or "").strip()
            if not raw:
                return ""
            parts = raw.split(".")
            if len(parts) != 4:
                return ""
            for part in parts:
                if not part.isdigit():
                    return ""
                try:
                    num = int(part)
                except Exception:
                    return ""
                if num < 0 or num > 255:
                    return ""
            return raw

        def _add_candidate(raw_value, bucket: list[str]) -> None:
            raw = str(raw_value or "").strip()
            if not raw:
                return
            for value in (raw, normalize_hostname_base(raw), mdns_hostname(normalize_hostname_base(raw) or raw)):
                text = str(value or "").strip()
                if text and text not in bucket:
                    bucket.append(text)

        candidates: list[str] = []
        _add_candidate(device_id, candidates)
        _add_candidate(source_host, candidates)
        _add_candidate(host_base, candidates)
        try:
            _add_candidate(ingest.resolve_nodus_hostname(device_id, device_type=device_type), candidates)
        except Exception:
            pass

        try:
            candidate_set = {str(item or "").strip() for item in candidates if str(item or "").strip()}
            candidate_bases = {normalize_hostname_base(item) or item for item in candidate_set}
            for host, peers in (getattr(ingest, "host_to_peer_ids", {}) or {}).items():
                peer_set = {str(peer or "").strip() for peer in (peers or []) if str(peer or "").strip()}
                host_base_candidate = normalize_hostname_base(str(host or "")) or str(host or "").strip()
                peer_bases = {normalize_hostname_base(peer) or peer for peer in peer_set}
                if (
                    host in candidate_set
                    or host_base_candidate in candidate_bases
                    or bool(peer_set & candidate_set)
                    or bool(peer_bases & candidate_bases)
                ):
                    _add_candidate(host, candidates)
                    for peer in peer_set:
                        _add_candidate(peer, candidates)
        except Exception:
            pass

        for attr in ("_host_ipv4addr",):
            try:
                mapping = getattr(ingest, attr, {}) or {}
                for key in candidates:
                    ip = _norm_ipv4(mapping.get(key))
                    if ip:
                        return ip
            except Exception:
                pass

        def _meta_ipv4(meta: dict | None) -> str:
            if not isinstance(meta, dict):
                return ""
            network_meta = meta.get("network") if isinstance(meta.get("network"), dict) else {}
            for container in (network_meta, meta):
                for key in ("ipv4addr", "IPV4ADDR", "IPv4Addr", "ipv4"):
                    ip = _norm_ipv4(container.get(key))
                    if ip:
                        return ip
            return ""

        def _meta_has_identifier(meta: dict, raw_value: str) -> bool:
            want = str(raw_value or "").strip().lower()
            if not want or not isinstance(meta, dict):
                return False
            observed: set[str] = set()

            def _observe(value) -> None:
                text = str(value or "").strip()
                if not text:
                    return
                observed.add(text.lower())
                normalized = normalize_hostname_base(text)
                if normalized:
                    observed.add(normalized.lower())

            _observe(meta.get("device_id"))
            _observe(meta.get("hostname"))
            sensor_meta = meta.get("sensor") if isinstance(meta.get("sensor"), dict) else {}
            _observe(sensor_meta.get("sensor_id"))
            switch_meta = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
            _observe(switch_meta.get("device_id"))
            _observe(switch_meta.get("switch_device_id"))
            channels = switch_meta.get("channels")
            if isinstance(channels, list):
                for row in channels:
                    if isinstance(row, dict):
                        _observe(row.get("channel_id"))
            return want in observed

        try:
            cache = getattr(ingest, "discovery_cache", {}) or {}
            for key in candidates:
                meta = cache.get(key) or cache.get(normalize_hostname_base(key) or key)
                ip = _meta_ipv4(meta)
                if ip:
                    return ip
            for meta in cache.values():
                if not isinstance(meta, dict):
                    continue
                if any(_meta_has_identifier(meta, key) for key in candidates):
                    ip = _meta_ipv4(meta)
                    if ip:
                        return ip
        except Exception:
            pass

        try:
            mapping = getattr(ingest, "_host_ip_cache", {}) or {}
            for key in candidates:
                ip = _norm_ipv4(mapping.get(key))
                if ip:
                    return ip
        except Exception:
            pass

        return ""

    async def _build_device_network_info(device_id: str, *, device_type: str) -> dict[str, str]:
        """
        Build read-only broker details for the Info panes.

        Switches on a combined sensor+switch Nodus use the paired sensor host,
        matching the settings push/restart behavior when locating the broker
        setting for that physical device.
        """
        sid = str(device_id or "").strip()
        ingest = services.mqtt_ingest
        system_root = _resolve_system_settings_root()
        sys_host_index = _build_system_hostname_index(system_root)
        source_host = _read_hostname_from_system_settings(
            sid,
            system_root=system_root,
            device_type=device_type,
            sys_host_index=sys_host_index,
        )
        source_host = str(source_host or "").strip()
        if not source_host:
            source_host = normalize_hostname_base(sid) or sid
        host_base = normalize_hostname_base(source_host) or source_host

        doc = _read_system_settings_doc(system_root, host_base)
        if not doc and sid and sid != host_base:
            doc = _read_system_settings_doc(system_root, sid)
        broker = _broker_from_network_doc(doc)
        ip_address = _runtime_ipv4_from_ingest(
            sid,
            device_type=device_type,
            source_host=source_host,
            host_base=host_base,
            ingest=ingest,
        )

        if device_type == "sensor" and sid.startswith("ecowitt-"):
            ip_address = ecowitt_sensor_health(services.data_logger, sid)["gateway_ip"]

        return {
            "ip_address": ip_address or "Unknown",
            "broker": broker or "Unknown",
            "broker_status": _broker_health_status(broker, ingest),
        }

    def _display_nodus_board_type(device_id: str, *, device_type: str, settings_block: dict | None = None) -> str:
        board_type = ""
        ingest = services.mqtt_ingest
        if ingest and hasattr(ingest, "get_nodus_board_type"):
            try:
                board_type = str(
                    ingest.get_nodus_board_type(device_id, device_type=device_type)
                ).strip()
            except Exception:
                board_type = ""
        if not board_type and isinstance(settings_block, dict):
            for key in ("MCU", "mcu", "BOARD_TYPE", "board_type", "BOARDTYPE", "boardtype", "BOARD", "board"):
                board_type = str(settings_block.get(key) or "").strip()
                if board_type:
                    break
        if not board_type and isinstance(settings_block, dict):
            device_class = str(settings_block.get("TYPE") or settings_block.get("type") or "").strip().lower()
            if device_class in {"nodus", "picow", "pico2w", "remote", "mqtt"}:
                board_type = "pico2w"
            elif device_type == "sensor" and device_class == "pi":
                board_type = "rPi"
        return board_type

    def _resolve_sensor_controller(sensor_id: str):
        sid_norm = normalize_sensor_id(sensor_id)
        smap = _get_dashboard_sensor_map()
        if smap is None:
            return None
        if hasattr(smap, "get"):
            try:
                hit = smap.get(sensor_id) or smap.get(sid_norm) or smap.get(sid_norm.lower())
                if hit:
                    return hit
            except Exception:
                pass
            try:
                for key, value in smap.items():
                    if normalize_sensor_id(str(key or "")) == sid_norm:
                        return value
            except Exception:
                pass
        try:
            for item in smap:
                controller = item
                candidate = getattr(item, "sensor_id", None)
                sensor_obj = getattr(item, "sensor", None)
                if not candidate and sensor_obj is not None:
                    candidate = getattr(sensor_obj, "sensor_id", None)
                if candidate and normalize_sensor_id(str(candidate)) == sid_norm:
                    return controller
        except TypeError:
            return None
        except Exception:
            return None
        return None

    def _local_sensor_hardware_from_controller(sensor_id: str) -> str:
        controller = _resolve_sensor_controller(sensor_id)
        sensor_obj = getattr(controller, "sensor", None) if controller is not None else None
        if sensor_obj is None:
            sensor_obj = controller
        if sensor_obj is None:
            return ""

        for attr in ("hardware", "sensor_hardware", "model", "sensor_model", "_co2_model"):
            value = str(getattr(sensor_obj, attr, "") or "").strip()
            if value:
                return value

        if getattr(sensor_obj, "bme680", None) is not None:
            return "BME680"
        if getattr(sensor_obj, "thp280_plant", None) is not None:
            return "BME280"
        if getattr(sensor_obj, "thp280", None) is not None:
            return "BME280"
        if getattr(sensor_obj, "aht", None) is not None:
            return "AHTx0"
        if getattr(sensor_obj, "veml", None) is not None:
            return "VEML7700"
        return ""

    def _local_sensor_hardware_from_settings(settings_block: dict | None) -> str:
        if not isinstance(settings_block, dict):
            return ""
        device_kind = str(settings_block.get("DEVICE") or settings_block.get("device") or "").strip().lower()
        hardware_by_device = {
            "apvpd": "BME280",
            "avpd": "BME280",
            "vpd": "BME280",
            "bme280": "BME280",
            "aqi": "BME680",
            "bme680": "BME680",
            "bme688": "BME688",
            "aht": "AHTx0",
            "aht10": "AHT10",
            "ahtx0": "AHTx0",
            "co2": "SCD30/SCD4x",
            "scd30": "SCD30",
            "scd4x": "SCD4x",
            "veml": "VEML7700",
            "lux": "VEML7700",
            "dummy": "Dummy",
            "test": "Dummy",
        }
        return hardware_by_device.get(device_kind, "")

    def _display_nodus_sensor_hardware(device_id: str, *, settings_block: dict | None = None) -> str:
        sensor_hardware = ""
        ingest = services.mqtt_ingest
        if ingest and hasattr(ingest, "get_nodus_sensor_hardware"):
            try:
                sensor_hardware = str(
                    ingest.get_nodus_sensor_hardware(device_id, device_type="sensor")
                ).strip()
            except Exception:
                sensor_hardware = ""
        if not sensor_hardware and isinstance(settings_block, dict):
            for key in ("HARDWARE", "hardware", "SENSOR_HARDWARE", "sensor_hardware"):
                sensor_hardware = str(settings_block.get(key) or "").strip()
                if sensor_hardware:
                    break
        if not sensor_hardware and isinstance(settings_block, dict):
            device_class = str(settings_block.get("TYPE") or settings_block.get("type") or "").strip().lower()
            if device_class == "pi":
                sensor_hardware = _local_sensor_hardware_from_controller(device_id)
                if not sensor_hardware:
                    sensor_hardware = _local_sensor_hardware_from_settings(settings_block)
        return sensor_hardware
    
    async def push_nodus_setting_simple(
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        section: str,
        key: str,
        value,
        sensor_file_name: str | None,
        system_mgr=None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        update = {"section": section, "key": key, "value": value}
        if setting_file_key == "sensor" and sensor_file_name:
            update["name"] = sensor_file_name
        elif setting_file_key == "switch":
            update["name"] = "switch.toml"
        return await push_nodus_settings_batch(
            device_id=device_id,
            device_type=device_type,
            setting_file_key=setting_file_key,
            updates=[(section, key, value)],
            sensor_file_name=sensor_file_name,
            system_mgr=system_mgr,
            system_root=system_root,
            ip_hint=ip_hint,
            sys_host_index=sys_host_index,
        )

    async def _publish_nodus_config_update(
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        update_payload: dict[str, Any],
    ) -> bool:
        def _config_result_applied(result_payload: dict[str, Any] | None) -> bool:
            # Gate the next queued update on Nodus confirming the apply completed.
            return isinstance(result_payload, dict) and result_payload.get("applied") is True

        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_config"):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] mqtt_ingest unavailable; skipping publish",
                    location=MODULE,
                )
            return False

        if DEBUG:
            client_desc = ""
            try:
                if hasattr(ingest, "_describe_publish_client"):
                    client_desc = str(ingest._describe_publish_client(use_ha_client=False) or "")
            except Exception:
                client_desc = ""
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] preparing publish target={target_device} topic=nodus/{target_device}/config/set update={str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()} {client_desc}".strip(),
                location=MODULE,
            )

        publish_result = ingest.publish_nodus_config(
            target_device,
            payload={"updates": [update_payload]},
            restart=False,
        )
        if not bool(publish_result.get("ok", False)):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] failed to publish config/set to nodus/{target_device}/config/set",
                    location=MODULE,
                )
            return False

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_config_ack(message_id, timeout=_NODUS_CONFIG_ACK_TIMEOUT_SEC)
        if not ack or not bool(ack.get("accepted", False)):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config ack failed for nodus/{target_device}/config/set message_id={message_id}: {ack}",
                    location=MODULE,
                )
            return False

        result = await ingest.wait_for_config_result(message_id, timeout=_NODUS_CONFIG_RESULT_TIMEOUT_SEC)
        if result is None:
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config result timeout for nodus/{target_device}/config/set message_id={message_id}",
                    location=MODULE,
                )
            return False
        if not _config_result_applied(result):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config apply failed for nodus/{target_device}/config/set message_id={message_id}: {result}",
                    location=MODULE,
                )
            return False

        if DEBUG:
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] OK via MQTT nodus/{target_device}/config/set: {str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()}",
                location=MODULE,
            )
        return True

    async def _request_nodus_device_restart(
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        restart_mode: str = "soft",
    ) -> tuple[bool, str]:
        def _truthy(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value or "").strip().lower() in {"1", "true", "yes", "on", "ok", "accepted", "applied", "rebooting", "restarted"}

        def _restart_ack_accepted(ack_payload: dict[str, Any] | None) -> bool:
            if not isinstance(ack_payload, dict):
                return False
            if "accepted" in ack_payload:
                return _truthy(ack_payload.get("accepted"))
            if "ok" in ack_payload:
                return _truthy(ack_payload.get("ok"))
            return True

        def _restart_result_applied(result_payload: dict[str, Any] | None) -> bool:
            if not isinstance(result_payload, dict):
                return False
            for key in ("applied", "restarted", "rebooting", "accepted", "ok", "success"):
                if key in result_payload and _truthy(result_payload.get(key)):
                    return True
            return False

        ingest = services.mqtt_ingest
        if not ingest:
            return False, "MQTT ingest unavailable"

        message_id = f"rst-{int(time.time())}-{uuid4().hex[:8]}"
        publish_result: dict[str, Any] | None = None

        if hasattr(ingest, "publish_nodus_restart"):
            publish_result = ingest.publish_nodus_restart(
                target_device,
                restart_mode=restart_mode,
                message_id=message_id,
            )
        elif hasattr(ingest, "publish_json"):
            topic = f"nodus/{target_device}/config/set"
            envelope = {
                "message_id": message_id,
                "payload": {},
                "restart": True,
                "restart_mode": restart_mode,
            }
            ok = bool(ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
            publish_result = {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}
        else:
            return False, "MQTT publish unavailable"

        if not bool((publish_result or {}).get("ok", False)):
            return False, "Failed to publish restart request"

        ack = None
        if hasattr(ingest, "wait_for_config_ack"):
            ack = await ingest.wait_for_config_ack(message_id, timeout=_NODUS_CONFIG_ACK_TIMEOUT_SEC)
        if not isinstance(ack, dict):
            return False, "Restart request was not acknowledged by the device"
        if not _restart_ack_accepted(ack):
            error_text = str(ack.get("error") or "").strip()
            return False, error_text or "Restart request was rejected by the device"

        result = None
        if hasattr(ingest, "wait_for_config_result"):
            result = await ingest.wait_for_config_result(message_id, timeout=_NODUS_CONFIG_RESULT_TIMEOUT_SEC)
        if result is None:
            return False, "Restart request timed out waiting for device result"
        if not _restart_result_applied(result):
            error_text = str((result or {}).get("error") or "").strip()
            return False, error_text or "Restart request was not applied by the device"

        if DEBUG:
            printDM(
                f"[restart_nodus_device:{device_type}:{device_id}] target={target_device} message_id={message_id} ack={ack} result={result}",
                location=MODULE,
            )
        return True, "Device restarting..."

    def _nodus_wifi_inventory() -> list[dict[str, Any]]:
        """Return deduplicated physical Nodus targets and current MQTT liveness."""
        ingest = services.mqtt_ingest
        if not ingest:
            return []
        try:
            raw_devices = ota_service.list_devices()
        except Exception:
            raw_devices = []

        inventory: list[dict[str, Any]] = []
        seen: set[str] = set()
        known_physical_ids: set[str] = set()
        known_ids: set[str] = set()
        for getter_name in ("get_known_devices", "get_known_switch_devices"):
            getter = getattr(ingest, getter_name, None)
            if not callable(getter):
                continue
            try:
                known_ids.update(
                    normalize_hostname_base(str(value or ""))
                    for value in (getter() or [])
                )
            except Exception:
                pass
        known_ids.discard("")
        sensor_hosts = getattr(ingest, "nodus_sensor_hosts", {}) or {}
        resolver = getattr(ingest, "resolve_nodus_hostname", None)
        for known_id in known_ids:
            physical = normalize_hostname_base(str(sensor_hosts.get(known_id) or ""))
            if not physical and callable(resolver):
                try:
                    physical = normalize_hostname_base(
                        str(
                            resolver(
                                known_id,
                                device_type="switch" if known_id.startswith("switch-") else None,
                            )
                            or ""
                        )
                    )
                except Exception:
                    physical = ""
            known_physical_ids.add(str(physical or known_id).lower())

        command_supported = all(
            callable(getattr(ingest, name, None))
            for name in ("publish_nodus_config", "wait_for_config_ack", "wait_for_config_result")
        )
        for raw in raw_devices:
            if not isinstance(raw, dict):
                continue
            device_id = normalize_hostname_base(
                str(raw.get("device_id") or raw.get("host") or "")
            )
            key = str(device_id or "").strip().lower()
            if not device_id or key in seen:
                continue
            if key not in known_physical_ids:
                continue
            removed = getattr(ingest, "is_nodus_device_removed", None)
            if callable(removed):
                try:
                    if removed(device_id):
                        continue
                except Exception:
                    pass
            seen.add(key)
            liveness = {}
            getter = getattr(ingest, "get_nodus_liveness", None)
            if callable(getter):
                try:
                    liveness = getter(device_id) or {}
                except Exception:
                    liveness = {}
            state = str(liveness.get("state") or raw.get("status") or "unknown").strip().lower()
            eligible = state == "online" and command_supported
            reason = ""
            if not command_supported:
                reason = "configuration commands unavailable"
            elif state != "online":
                reason = f"device is {state or 'unknown'}"
            inventory.append(
                {
                    "device_id": device_id,
                    "status": state or "unknown",
                    "eligible": eligible,
                    "reason": reason,
                    "firmware_version": str(raw.get("firmware_version") or ""),
                    "last_seen_s": liveness.get("last_seen_s", raw.get("last_seen_s")),
                }
            )
        return sorted(inventory, key=lambda row: str(row.get("device_id") or "").lower())

    def _nodus_wifi_response(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    def _validate_nodus_wifi_credentials(ssid: str, password: str) -> str:
        try:
            ssid_bytes = ssid.encode("utf-8", errors="strict")
        except UnicodeError:
            return "SSID contains invalid text."
        if not ssid_bytes or len(ssid_bytes) > 32:
            return "SSID must contain between 1 and 32 UTF-8 bytes."
        password_ok = 8 <= len(password) <= 63 or bool(re.fullmatch(r"[0-9A-Fa-f]{64}", password))
        if not password_ok:
            return "Password must contain 8 to 63 characters, or be a 64-digit hexadecimal key."
        return ""

    @router.get("/api/nodus-wifi/current", response_class=JSONResponse)
    async def api_nodus_wifi_current(request: Request):
        """Read the host's active Wi-Fi credentials for transient form display."""
        _require_protected_access(request, require_csrf=True)
        try:
            from . import saiAddDevice

            ssid, password = await asyncio.to_thread(saiAddDevice.resolve_pi_wifi_credentials)
        except Exception:
            ssid, password = "", ""
        return _nodus_wifi_response(
            {
                "ok": True,
                "ssid": str(ssid or ""),
                "password": str(password or ""),
                "password_available": bool(password),
            }
        )

    @router.get("/api/nodus-wifi/devices", response_class=JSONResponse)
    async def api_nodus_wifi_devices(request: Request):
        """List physical Nodus devices eligible for a coordinated Wi-Fi update."""
        _require_protected_access(request, require_csrf=True)
        devices = _nodus_wifi_inventory()
        return _nodus_wifi_response(
            {
                "ok": True,
                "devices": devices,
                "eligible_count": sum(1 for row in devices if row.get("eligible") is True),
            }
        )

    @router.post("/api/nodus-wifi/update", response_class=JSONResponse)
    async def api_nodus_wifi_update(request: Request):
        """Stage new credentials on online Nodus devices, then restart successes."""
        _require_protected_access(request, require_csrf=True)
        try:
            body = await request.json()
        except Exception:
            body = {}
        ssid = str((body or {}).get("ssid") or "")
        password = str((body or {}).get("password") or "")
        validation_error = _validate_nodus_wifi_credentials(ssid, password)
        if validation_error:
            return _nodus_wifi_response({"ok": False, "error": validation_error}, status_code=400)

        requested_raw = (body or {}).get("device_ids")
        if requested_raw is not None and not isinstance(requested_raw, list):
            return _nodus_wifi_response({"ok": False, "error": "device_ids must be a list."}, status_code=400)
        requested = [str(value or "").strip() for value in (requested_raw or []) if str(value or "").strip()]
        invalid = [value for value in requested if not _is_valid_device_id(value)]
        if invalid:
            return _nodus_wifi_response({"ok": False, "error": "One or more device IDs are invalid."}, status_code=400)

        inventory = _nodus_wifi_inventory()
        inventory_by_id = {str(row.get("device_id") or "").lower(): row for row in inventory}
        if requested_raw is None:
            requested_keys = [key for key, row in inventory_by_id.items() if row.get("eligible") is True]
        else:
            requested_keys = list(dict.fromkeys(value.lower() for value in requested))

        results: dict[str, dict[str, Any]] = {}
        targets: list[str] = []
        for key in requested_keys:
            row = inventory_by_id.get(key)
            device_id = str((row or {}).get("device_id") or key)
            if not row:
                results[key] = {
                    "device_id": device_id,
                    "status": "unavailable",
                    "staged": False,
                    "restarted": False,
                    "message": "Device is no longer available.",
                }
            elif row.get("eligible") is not True:
                results[key] = {
                    "device_id": device_id,
                    "status": "skipped",
                    "staged": False,
                    "restarted": False,
                    "message": str(row.get("reason") or "Device is not online."),
                }
            else:
                targets.append(device_id)

        if not targets:
            return _nodus_wifi_response(
                {
                    "ok": False,
                    "error": "No online Nodus devices are eligible for the update.",
                    "results": list(results.values()),
                },
                status_code=409,
            )

        ingest = services.mqtt_ingest
        concurrency = asyncio.Semaphore(4)

        async def _stage_one(device_id: str) -> dict[str, Any]:
            async with concurrency:
                try:
                    staged = await push_nodus_settings_batch(
                        device_id=device_id,
                        device_type="wifi",
                        setting_file_key="system",
                        updates=[
                            ("Network", "SSID", ssid),
                            ("Network", "PASSWORD", password),
                        ],
                        sensor_file_name=None,
                    )
                    if not staged:
                        raise RuntimeError("configuration_not_applied")
                    return {
                        "device_id": device_id,
                        "status": "staged",
                        "staged": True,
                        "restarted": False,
                        "message": "Credentials staged and confirmed.",
                    }
                except Exception as exc:
                    error_code = str(exc or "stage_failed")
                    safe_messages = {
                        "configuration_not_applied": "Device did not confirm both credential updates.",
                    }
                    if DEBUG:
                        printDM(
                            f"[nodus-wifi:{device_id}] credential staging failed error={error_code}",
                            location=MODULE,
                        )
                    return {
                        "device_id": device_id,
                        "status": "failed",
                        "staged": False,
                        "restarted": False,
                        "message": safe_messages.get(error_code, "Credential update failed."),
                    }

        staged_rows = await asyncio.gather(*(_stage_one(device_id) for device_id in targets))
        for row in staged_rows:
            results[str(row.get("device_id") or "").lower()] = row

        staged_devices = [str(row.get("device_id") or "") for row in staged_rows if row.get("staged") is True]

        async def _restart_one(device_id: str) -> dict[str, Any]:
            async with concurrency:
                ok, message = await _request_nodus_device_restart(
                    target_device=device_id,
                    device_id=device_id,
                    device_type="system",
                )
                if ok:
                    return {
                        "device_id": device_id,
                        "status": "restarting",
                        "staged": True,
                        "restarted": True,
                        "message": "Credentials confirmed; device is restarting.",
                    }
                return {
                    "device_id": device_id,
                    "status": "restart_failed",
                    "staged": True,
                    "restarted": False,
                    "message": str(message or "Credentials were staged, but restart failed."),
                }

        restarted_rows = await asyncio.gather(*(_restart_one(device_id) for device_id in staged_devices))
        for row in restarted_rows:
            results[str(row.get("device_id") or "").lower()] = row

        ordered_results = [results[key] for key in requested_keys if key in results]
        all_restarted = bool(ordered_results) and all(row.get("restarted") is True for row in ordered_results)
        return _nodus_wifi_response(
            {
                "ok": all_restarted,
                "requested": len(requested_keys),
                "staged": sum(1 for row in ordered_results if row.get("staged") is True),
                "restarting": sum(1 for row in ordered_results if row.get("restarted") is True),
                "results": ordered_results,
            }
        )

    async def push_nodus_settings_batch(
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        updates: list[tuple[str, str, Any]],
        sensor_file_name: str | None,
        system_mgr=None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        hostname = _read_hostname_from_system_settings(
            device_id, system_mgr, system_root,
            device_type=device_type, sys_host_index=sys_host_index
        )
        target_device = str(hostname or device_id or "").strip()
        if not target_device:
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] no MQTT target device resolved; skipping publish",
                    location=MODULE,
                )
            return False

        updates_payload: list[dict[str, Any]] = []
        for section, key, value in (updates or []):
            item = {"section": section, "key": key, "value": value}
            if setting_file_key == "sensor":
                item["sensor_id"] = str(device_id or "").strip()
                if sensor_file_name:
                    item["name"] = sensor_file_name
            elif setting_file_key == "switch":
                item["name"] = "switch.toml"
            updates_payload.append(item)
        if not updates_payload:
            return True
        # Serialize config writes per physical Nodus host. This preserves the
        # one-key-at-a-time behavior even when multiple routes/tasks target the
        # same device concurrently (for example paired sensor + switch updates).
        async with _get_host_lock(target_device):
            for update_payload in updates_payload:
                ok = await _publish_nodus_config_update(
                    target_device=target_device,
                    device_id=device_id,
                    device_type=device_type,
                    update_payload=update_payload,
                )
                if not ok:
                    return False
        return True

    def _nodus_values_match(previous: Any, current: Any) -> bool:
        if previous == current:
            return True
        if previous is None and current in (None, ""):
            return True
        if current is None and previous in (None, ""):
            return True
        return False


    # ---------- user-defined constants ----------
    LOCATIONS_ROUTE_TAG = "device-locations"
    # ----- view and edit device locations -------
    @router.get("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def list_device_locations(request: Request) -> JSONResponse:
        try:
            system_dir = _resolve_system_settings_root()

            sensor_settings = SensorSettingsManager()
            sensor_dir = getattr(sensor_settings, "base_dir", None)
            if not sensor_dir:
                sensor_dir = "sensor_settings"

            switch_settings = SwitchSettingsManager()
            switch_dir = getattr(switch_settings, "base_dir", None)
            if not switch_dir:
                switch_dir = "switch_settings"

            # Log the resolved paths
            if DEBUG:
                printDM(f"system_dir={system_dir}", location="saiWebRoutes.list_device_locations")
                printDM(f"sensor_dir={sensor_dir}", location="saiWebRoutes.list_device_locations")
                printDM(f"switch_dir={switch_dir}", location="saiWebRoutes.list_device_locations")


            # Instantiate managers, tolerating different ctor signatures
            try:
                sensor_mgr = SensorSettingsManager(sensor_dir)
            except TypeError:
                sensor_mgr = SensorSettingsManager()  # falls back to default if ctor takes no args

            try:
                switch_mgr = SwitchSettingsManager(switch_dir)
            except TypeError:
                switch_mgr = SwitchSettingsManager()

            sensor_items = []
            try:
                for sensor_id in sensor_mgr.list_ids():
                    if (sensor_id or "").lower() == "factory":
                        continue
                    doc = sensor_mgr.load(sensor_id) or {}
                    loc = (doc.get("Sensor", {}) or {}).get("LOCATION", "Unknown") or "Unknown"
                    sensor_items.append({"id": sensor_id, "type": "sensor", "location": loc})
                    if DEBUG:
                        printDM(f"sensor: {sensor_id}, sw_loc: {loc}", location="saiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"sensor_mgr error: {e}", location="saiWebRoutes.list_device_locations")

            switch_items = []
            try:
                for switch_id in switch_mgr.list_switches():
                    if (switch_id or "").lower() == "factory":
                        continue
                    doc = switch_mgr.load(switch_id) or {}
                    sw_loc = (doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "") or "Unknown"
                    switch_items.append({"id": switch_id, "type": "switch", "location": sw_loc})
                    if DEBUG:
                        printDM(f"switch: {switch_id}, sw_loc: {sw_loc}", location="saiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"switch_mgr error: {e}", location="saiWebRoutes.list_device_locations")

            items = sensor_items + switch_items

            if DEBUG:
                printDM(f"sensors={len(sensor_items)} switches={len(switch_items)} total={len(items)}", location="saiWebRoutes.DeviceLocations")

            return JSONResponse(items)

        except Exception as e:
            printDM(f"Failed to list device locations: {e}", location="saiWebRoutes.list_device_locations")
            # Return an empty list (200) rather than 500 so the modal stays open with a visible message
            # If you prefer to signal error, keep 500 — but then ensure the client does NOT auto-close the modal.
            return JSONResponse({"error": "failed"}, status_code=500)
        
    @router.post("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def save_device_locations(request: Request) -> JSONResponse:
        """
        Accepts: [{"id": "...", "type": "sensor"|"switch", "location": "..."}]
        (Also tolerates {"switch_location": "..."} for switches.)
        Saves LOCATION/SWITCH_LOCATION locally and updates live objects in memory.
        For remote Nodus devices, publishes MQTT config updates and waits for ack/result.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, list):
                return JSONResponse({"error": "invalid_payload"}, status_code=400)

            sensor_mgr = SensorSettingsManager("sensor_settings")
            switch_mgr = SwitchSettingsManager("switch_settings")
            SystemSettingsMgr = globals().get("SystemSettingsManager", None)
            system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
            system_root = _resolve_system_settings_root(system_mgr)
            updated = {"sensor": 0, "switch": 0, "nodus_pushed": 0}

            sys_host_index = _build_system_hostname_index(system_root)
            if DEBUG:
                printDM(f"[save_device_locations] system hostname index: {sys_host_index}", location=MODULE)
                
            # Shared controller map supplied by the runtime context.
            def _find_switch_ctrl_by_id(switch_id: str):
                """Return the live SwitchController with matching switch_id, or None."""
                try:
                    sid = (switch_id or "").strip()
                    if not sid:
                        return None
                    if isinstance(services.switch_controllers, dict):
                        for cand in services.switch_controllers.values():
                            if getattr(cand, "switch_id", "") in {sid, sid.lower()}:
                                return cand
                    else:
                        if getattr(services.switch_controllers, "switch_id", "") in {sid, sid.lower()}:
                            return services.switch_controllers
                except Exception:
                    pass
                return None
                
            # --- make sensor_map accessible anywhere in this handler
            from collections.abc import Iterable
            def _get_sensor_map():
                sm = services.sensor_map
                return sm
            sensor_map = _get_sensor_map()

            # We’ll gather Nodus POSTs to run concurrently for speed
            nodus_tasks: list[tuple[dict[str, Any], Any]] = []

            for entry in payload:
                try:
                    dev_id   = (entry.get("id")   or "").strip()
                    dev_type = (entry.get("type") or "").strip().lower()

                    raw_loc = entry.get("location")
                    raw_sw  = entry.get("switch_location")
                    location = (raw_sw if (dev_type == "switch" and raw_sw is not None) else raw_loc) or ""
                    location = location.strip()

                    if not dev_id or dev_type not in {"sensor", "switch"}:
                        continue

                    if dev_type == "sensor":
                        # persist + live update
                        doc = sensor_mgr.load(dev_id) or {}
                        prev_location = str(((doc.get("Sensor", {}) or {}).get("LOCATION", "")) or "").strip()
                        if _nodus_values_match(prev_location, location):
                            if DEBUG:
                                printDM(
                                    f"[save_device_locations] sensor {dev_id} unchanged; skipping",
                                    location=MODULE,
                                )
                            continue
                        doc.setdefault("Sensor", {})["LOCATION"] = location
                        sensor_mgr.save(dev_id, doc)
                        updated["sensor"] += 1

                        try:
                            if isinstance(sensor_map, dict):
                                sobj = sensor_map.get(dev_id) or sensor_map.get(dev_id.lower())
                                if sobj is not None:
                                    setattr(sobj, "location", location)
                        except Exception as e:
                            printDM(f"[save_device_locations] sensor live update failed for {dev_id}: {e}",
                                    location=MODULE)

                        try:
                            type(sensor_mgr).invalidate_cache(dev_id, "sensor_settings")
                        except Exception:
                            pass

                        # enqueue remote push if this is a remote Nodus device
                        dev_kind = (sensor_mgr.get_setting(dev_id, "Sensor.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w", "nodus"):
                            sblk = (doc.get("Sensor") or {}) if isinstance(doc, dict) else {}
                            sensor_file_name = None
                            nodus_block = (
                                doc.get("Nodus")
                                if isinstance(doc.get("Nodus"), dict)
                                else {}
                            )
                            sensor_file_name = str(
                                nodus_block.get("CONFIG_FILE") or ""
                            ).strip() or None
                            if any(k in sblk for k in ("I2C_SCL","I2C_SDA","I2C_BUS","I2C_ADDR")):
                                sensor_file_name = sensor_file_name or "sensor_i2c.toml"
                            elif any(k in sblk for k in ("UART_TX","UART_RX","UART_BUS","RS485_DIR_PIN","MODBUS_ADDR")):
                                sensor_file_name = sensor_file_name or "sensor_soil.toml"

                            resolved_host = _read_hostname_from_system_settings(
                                dev_id,
                                system_mgr,
                                system_root,
                                device_type="sensor",
                                sys_host_index=sys_host_index,
                            )
                            if DEBUG:
                                printDM(f"[save_device_locations] sensor {dev_id} resolved host: {resolved_host}", location=MODULE)

                            nodus_tasks.append((
                                {
                                    "id": dev_id,
                                    "type": "sensor",
                                    "location": location,
                                    "target_host": str(resolved_host or dev_id or "").strip(),
                                    "file": sensor_file_name or "",
                                },
                                push_nodus_setting_simple(
                                    device_id=dev_id,
                                    device_type="sensor",
                                    setting_file_key="sensor",
                                    section="Sensor",
                                    key="LOCATION",
                                    value=location,
                                    sensor_file_name=sensor_file_name,
                                    system_mgr=system_mgr,
                                    system_root=system_root,
                                    sys_host_index=sys_host_index,
                                )
                            ))

                        try:
                            topic = f"sensor/{dev_id}/data"
                            if hasattr(services.mqtt_ingest, "device_location") and isinstance(services.mqtt_ingest.device_location, dict):
                                services.mqtt_ingest.device_location[topic] = location or "Unknown"
                        except Exception:
                            pass
                        route_state.sensor_locations.pop(dev_id, None)

                    else:  # switch
                        doc = switch_mgr.load(dev_id) or {}
                        prev_location = str(((doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "")) or "").strip()
                        if _nodus_values_match(prev_location, location):
                            if DEBUG:
                                printDM(
                                    f"[save_device_locations] switch {dev_id} unchanged; skipping",
                                    location=MODULE,
                                )
                            continue
                        doc.setdefault("Switch", {})["SWITCH_LOCATION"] = location
                        switch_mgr.save(dev_id, doc)
                        updated["switch"] += 1

                        try:
                            ctrl = _find_switch_ctrl_by_id(dev_id)
                            if ctrl and getattr(ctrl, "is_present", False):
                                setattr(ctrl, "location", location)
                        except Exception as e:
                            printDM(f"[save_device_locations] switch live update failed for {dev_id}: {e}",
                                    location=MODULE)

                        try:
                            type(switch_mgr).invalidate_cache(dev_id, "switch_settings")
                        except Exception:
                            pass


                        # enqueue remote push if this is a remote Nodus device
                        dev_kind = (switch_mgr.get_setting(dev_id, "Switch.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w", "nodus"):
                            resolved_host = _read_hostname_from_system_settings(
                                dev_id,
                                system_mgr,
                                system_root,
                                device_type="switch",
                                sys_host_index=sys_host_index,
                            )
                            if DEBUG:
                                printDM(f"[save_device_locations] switch {dev_id} resolved host: {resolved_host}", location=MODULE)

                            # Optional consistency hint: if host looks like 'switch-...' but a paired sensor exists with a different host
                            try:
                                serial = dev_id.rsplit("-", 1)[-1] if "-" in dev_id else dev_id
                                # If sensor_mgr exposes list_ids, check for paired sensor
                                paired_sensor_host = None
                                if hasattr(sensor_mgr, "list_ids"):
                                    for sid in (sensor_mgr.list_ids() or []):
                                        sid = str(sid)
                                        if sid.lower().startswith("switch-"):
                                            continue
                                        if sid.endswith(f"-{serial}"):
                                            paired_sensor_host = _read_hostname_from_system_settings(sid, system_mgr, system_root)
                                            break
                                if DEBUG and resolved_host and resolved_host.startswith("switch-") and paired_sensor_host and paired_sensor_host != resolved_host:
                                    printDM(
                                        f"[save_device_locations] WARNING: switch {dev_id} HOSTNAME='{resolved_host}' differs from paired sensor HOSTNAME='{paired_sensor_host}'. "
                                        f"Update system_settings/{dev_id}/settings.toml [Network].HOSTNAME to the shared Nodus hostname.",
                                        location=MODULE
                                    )
                            except Exception:
                                pass

                            nodus_tasks.append((
                                {
                                    "id": dev_id,
                                    "type": "switch",
                                    "location": location,
                                    "target_host": str(resolved_host or dev_id or "").strip(),
                                    "file": "switch.toml",
                                },
                                push_nodus_setting_simple(
                                    device_id=dev_id,
                                    device_type="switch",
                                    setting_file_key="switch",
                                    section="Switch",
                                    key="SWITCH_LOCATION",
                                    value=location,
                                    sensor_file_name=None,
                                    system_mgr=system_mgr,
                                    system_root=system_root,
                                    sys_host_index=sys_host_index,
                                )
                            ))

                    if DEBUG:
                        printDM(f"dev_type: {dev_type}, dev_id: {dev_id}, dev_loc: {location}",
                                location=f"{MODULE}.save_device_locations")

                except Exception as row_err:
                    printDM(f"[save_device_locations] row error: {row_err}", location=MODULE)

            _invalidate_dashboard_caches()

            # Run all remote pushes
            remote_failures = 0
            remote_results: list[dict[str, Any]] = []
            if nodus_tasks:
                try:
                    async def _run_nodus_push(meta: dict[str, Any], coro):
                        try:
                            ok = await coro
                            result = dict(meta)
                            result["ok"] = bool(ok is True)
                            if not result["ok"]:
                                result["error"] = "remote_apply_failed"
                            return result
                        except Exception as exc:
                            result = dict(meta)
                            result["ok"] = False
                            result["error"] = f"{type(exc).__name__}: {exc}"
                            return result

                    results = await asyncio.gather(
                        *[_run_nodus_push(meta, coro) for meta, coro in nodus_tasks],
                        return_exceptions=False,
                    )
                    remote_results = [dict(r) for r in results]
                    pushed = sum(1 for r in remote_results if r.get("ok") is True)
                    updated["nodus_pushed"] = pushed
                    remote_failures = max(len(remote_results) - pushed, 0)
                    if DEBUG:
                        failures = [r for r in remote_results if r.get("ok") is not True]
                        if failures:
                            printDM(f"[save_device_locations] nodus push failures: {failures}", location=MODULE)
                except Exception as e:
                    if DEBUG:
                        printDM(f"[save_device_locations] nodus push gather error: {e}", location=MODULE)
                    remote_failures = len(nodus_tasks)
                    remote_results = [
                        {
                            **meta,
                            "ok": False,
                            "error": f"gather_error: {e}",
                        }
                        for meta, _coro in nodus_tasks
                    ]

            if remote_failures:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "nodus_remote_apply_failed",
                        "updated": updated,
                        "results": remote_results,
                    },
                    status_code=502,
                )
            return JSONResponse({"ok": True, "updated": updated, "results": remote_results})

        except Exception as e:
            printDM(f"Failed to save device locations: {e}", location=f"{MODULE}.save_device_locations")
            return JSONResponse({"error": "failed"}, status_code=500)

    # 'remove a device' helpers
    def _normalize_dev_id(name: str | None) -> str | None:
        base = normalize_hostname_base(name)
        return base or None

    def _collect_ingest_ids() -> list[str]:
        """
        Aggregate IDs from MQTT discovery so the remove list reflects in-memory state.
        """
        ing = services.mqtt_ingest
        if not ing:
            return []

        ids: set[str] = set()
        try:
            ids.update(ing.get_known_devices() or [])
        except Exception:
            pass
        try:
            ids.update(ing.get_known_switch_devices() or [])
        except Exception:
            pass
        # Intentionally exclude raw mqtt_clients / host_to_peer_ids here.
        # Those caches can retain stale aliases and peer IDs after deletion, which
        # repopulates "Remove Device" with ghost entries.

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _enumerate_dirs(base_dir:str)->list[str]:
        try:
            p=_settings_base_path(base_dir)
            if not p.exists(): return []
            return [d.name for d in p.iterdir() if d.is_dir()]
        except Exception:
            return []

    def _collect_settings_ids() -> list[str]:
        ids: set[str] = set()
        try:
            sensor_mgr = SensorSettingsManager("sensor_settings")
            ids.update(sensor_mgr.list_ids() or [])
        except Exception:
            pass
        try:
            switch_mgr = SwitchSettingsManager("switch_settings")
            ids.update(switch_mgr.list_switches() or [])
        except Exception:
            pass
        try:
            ids.update(_enumerate_dirs(_SYS_BASE_DIR))
        except Exception:
            pass

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _collect_db_ids() -> list[str]:
        ids: set[str] = set()
        try:
            ids.update(services.data_logger.get_available_sensors() or [])
        except Exception:
            pass
        try:
            for row in (services.data_logger.get_switch_identities() or []):
                switch_id = str((row or {}).get("switch_id") or "").strip()
                if switch_id:
                    ids.add(switch_id)
        except Exception:
            pass

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _persisted_nodus_sensor_groups() -> tuple[dict[str, str], dict[str, list[str]]]:
        """Read child-to-physical Nodus identity from persisted sensor shadows."""
        child_hosts: dict[str, str] = {}
        host_children: dict[str, list[str]] = {}
        try:
            sensor_mgr = SensorSettingsManager("sensor_settings")
            for raw_sensor_id in sensor_mgr.list_ids() or []:
                sensor_id = _normalize_dev_id(raw_sensor_id)
                if not sensor_id:
                    continue
                try:
                    doc = sensor_mgr.load(raw_sensor_id) or {}
                except Exception:
                    continue
                nodus = doc.get("Nodus") if isinstance(doc.get("Nodus"), dict) else {}
                host = _normalize_dev_id(nodus.get("DEVICE_ID"))
                if not host:
                    continue
                child_hosts[sensor_id] = host
                children = host_children.setdefault(host, [])
                if sensor_id not in children:
                    children.append(sensor_id)
        except Exception:
            pass
        return child_hosts, host_children

    def _collect_removable_ids() -> list[str]:
        """
        Aggregate IDs that are candidates for removal from:
          - MQTT discovery (in-memory)
          - sensor, switch, and system settings
          - database-backed sensor and switch identities

        Excludes:
          - our own hub host folder
          - special folders like '__pycache__'
          - 'factory' metadata folders
        """
        ids: set[str] = set(_collect_ingest_ids())
        ids.update(_collect_settings_ids())
        ids.update(_collect_db_ids())

        # do not list hub host folder (our own hostname)
        try:
            hub_name = Path(get_hub_settings_path()).parent.name
            if hub_name:
                ids.discard(hub_name)
                ids.discard(_normalize_dev_id(hub_name) or hub_name)
        except Exception:
            pass

        # filter out known non-removable / meta folders
        banned = {"__pycache__", "factory"}
        filtered_ids = [
            dev_id
            for dev_id in ids
            if dev_id
            and dev_id.lower() not in banned
            and not dev_id.lower().startswith("factory")
            and not dev_id.lower().startswith("template")
        ]

        ingest = services.mqtt_ingest
        persisted_hosts, _ = _persisted_nodus_sensor_groups()
        sensor_hosts = dict(persisted_hosts)
        if ingest:
            sensor_hosts.update(getattr(ingest, "nodus_sensor_hosts", {}) or {})
        collapsed: set[str] = set()
        for dev_id in filtered_ids:
            physical = str(sensor_hosts.get(dev_id) or "").strip()
            collapsed.add(physical or dev_id)
        filtered_ids = list(collapsed)

        return sorted(filtered_ids)

    def _collect_removable_details(device_ids: list[str]) -> dict[str, dict[str, Any]]:
        ingest = services.mqtt_ingest

        _, persisted_groups = _persisted_nodus_sensor_groups()
        now_ts = time.time()
        details: dict[str, dict[str, Any]] = {}

        def _resolved_host(dev_id: str) -> str:
            base = normalize_hostname_base(dev_id) or str(dev_id or "").strip()
            if not ingest:
                return base
            resolver = getattr(ingest, "resolve_nodus_hostname", None)
            if callable(resolver):
                try:
                    resolved = resolver(base, device_type="switch" if base.startswith("switch-") else None)
                    host = normalize_hostname_base(str(resolved or ""))
                    if host:
                        return host
                except Exception:
                    pass
            return base

        def _last_seen_seconds(dev_id: str, host: str) -> float | None:
            latest = 0.0
            if ingest:
                seen = getattr(ingest, "last_mqtt_seen", {}) or {}
                candidates = {dev_id, host}
                base = normalize_hostname_base(host)
                if base:
                    candidates.add(base)
                    candidates.add(mdns_hostname(base))
                try:
                    for peer in (getattr(ingest, "host_to_peer_ids", {}) or {}).get(base or host, []) or []:
                        peer_base = normalize_hostname_base(str(peer or ""))
                        if peer_base:
                            candidates.add(peer_base)
                            candidates.add(mdns_hostname(peer_base))
                except Exception:
                    pass
                for key in candidates:
                    try:
                        latest = max(latest, float(seen.get(key, 0.0) or 0.0))
                    except Exception:
                        continue
                if latest:
                    return round(max(now_ts - latest, 0.0), 1)

            if _is_switch_id(dev_id):
                return None

            try:
                logger = _active_data_logger()
                last_packet = getattr(logger, "get_sensor_last_packet_epoch", None)
                if callable(last_packet):
                    aliases = [dev_id]
                    base = normalize_hostname_base(dev_id)
                    if base:
                        aliases.extend([base, mdns_hostname(base)])
                    epoch = last_packet(dev_id, aliases=aliases)
                    if epoch is not None:
                        return round(max(now_ts - float(epoch), 0.0), 1)
            except Exception:
                pass
            return None

        def _device_url(host: str) -> str:
            if not host:
                return ""
            ip = ""
            if ingest:
                base = normalize_hostname_base(host) or host
                for attr in ("_host_ipv4addr", "_host_ip_cache"):
                    try:
                        mapping = getattr(ingest, attr, {}) or {}
                        ip = str(mapping.get(base) or mapping.get(host) or mapping.get(mdns_hostname(base)) or "").strip()
                        if ip:
                            break
                    except Exception:
                        pass
            return f"http://{ip or mdns_hostname(host)}:8000"

        for raw in device_ids:
            dev_id = normalize_hostname_base(raw) or str(raw or "").strip()
            if not dev_id:
                continue
            host = _resolved_host(dev_id)
            details[dev_id] = {
                "url": _device_url(host),
                "last_seen_s": _last_seen_seconds(dev_id, host),
            }
            children = list(persisted_groups.get(host, []) or [])
            if ingest:
                live_children = (
                    (getattr(ingest, "nodus_host_sensors", {}) or {}).get(host, [])
                    or []
                )
                for child in live_children:
                    if child not in children:
                        children.append(child)
            if children:
                details[dev_id]["sensors"] = children
        return details

    def _toml_escape(s:str)->str:
        return s.replace("\\","\\\\").replace('"','\\"')

    def _toml_join_list(str_list:list[str])->str:
        inner=", ".join(f"\"{_toml_escape(s)}\"" for s in str_list)
        return f"[{inner}]"

    def _remove_client_from_hub_settings(device_id:str)->bool:
        # Clients list is no longer authoritative; nothing to remove.
        return True

    def _collect_related_device_ids(device_id: str, *, mqtt_ingest=None) -> list[str]:
        """
        Expand a remove target into related Nodus IDs (host + peer switch IDs).
        This ensures remove-device purges retained topics for the full Nodus identity set.
        """
        out: list[str] = []
        seen: set[str] = set()

        def _add(raw: str | None) -> None:
            val = str(raw or "").strip()
            if not val:
                return
            key = val.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(val)

        def _alias_set(raw: str | None) -> set[str]:
            val = str(raw or "").strip()
            aliases: set[str] = set()
            if not val:
                return aliases
            aliases.add(val.lower())
            base = normalize_hostname_base(val)
            if base:
                aliases.add(base.lower())
                aliases.add(mdns_hostname(base).lower())
            return aliases

        def _add_nodus_suffix_peers(raw: str | None) -> None:
            """
            Nodus switch and channel IDs are derived from the same short suffix
            as the host sensor ID. If in-memory host_to_peer_ids is already gone,
            use that suffix to clear retained host, switch, and channel topics
            that could reseed the removed device.
            """
            val = str(raw or "").strip()
            if not val or "-" not in val:
                return
            prefix_l = val.split("-", 1)[0].lower()
            suffix = val.rsplit("-", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9]{5,32}", suffix):
                return
            sensor_prefixes = ("apvpd", "avpd", "aqi", "aht", "aht10", "ahtx0", "co2", "lux", "veml", "soil")
            is_switch_peer = prefix_l == "switch" or re.fullmatch(r"s\d+", prefix_l, flags=re.IGNORECASE)
            is_sensor_peer = prefix_l in sensor_prefixes
            if is_sensor_peer:
                _add(f"switch-{suffix}")
                for idx in range(1, 9):
                    _add(f"S{idx}-{suffix}")
                return
            if not is_switch_peer:
                return
            for sensor_prefix in sensor_prefixes:
                _add(f"{sensor_prefix}-{suffix}")
            _add(f"switch-{suffix}")
            for idx in range(1, 9):
                _add(f"S{idx}-{suffix}")

        _add(device_id)
        _add_nodus_suffix_peers(device_id)
        if not mqtt_ingest:
            return out

        wanted = _alias_set(device_id)
        try:
            norm = getattr(mqtt_ingest, "_normalize_host_key", None)
            if callable(norm):
                host_base = norm(device_id)
                if host_base:
                    wanted |= _alias_set(host_base)
                    _add(host_base)
        except Exception:
            pass

        mapping = getattr(mqtt_ingest, "host_to_peer_ids", None)
        if not isinstance(mapping, dict):
            mapping = {}

        for host, peers in list(mapping.items()):
            host_aliases = _alias_set(host)
            peer_aliases: set[str] = set()
            for peer in (peers or []):
                peer_aliases |= _alias_set(peer)
            if wanted & (host_aliases | peer_aliases):
                _add(host)
                for peer in (peers or []):
                    _add(peer)

        # Also include per-channel IDs (e.g. "S1-xxxxxx") tied to related
        # switch hosts so DB/topic cleanup can purge channel-keyed rows.
        for related in list(out):
            _add_nodus_suffix_peers(related)
            try:
                for row in (_collect_switch_channels(related, mqtt_ingest=mqtt_ingest) or []):
                    _add(str((row or {}).get("channel_id") or "").strip())
            except Exception:
                pass

        return out

    def _collect_switch_channels(device_id: str, mqtt_ingest=None) -> list[dict]:
        """
        Return switch channel dicts: [{"channel_id": "...", "label": "..."}].
        """
        channels: list[dict] = []
        try:
            mgr = SwitchSettingsManager("switch_settings")
            doc = mgr.load(device_id) or {}
            sw = doc.get("Switch", {}) or {}
            for key, val in sw.items():
                if not isinstance(key, str) or not key.startswith("SWITCH_"):
                    continue
                m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
                if not m:
                    continue
                label = str(val or "").strip()
                suffix = m.group(1)
                id_key = f"SWITCH_{suffix}_CHANNEL_ID"
                channel_id = str(sw.get(id_key, "") or "").strip()
                if channel_id:
                    channels.append({"channel_id": channel_id, "label": label})
        except Exception:
            pass

        if mqtt_ingest and not channels:
            try:
                for info in (mqtt_ingest.nodus_switch_topic_map or {}).values():
                    if info and info.get("switch_id") == device_id:
                        ch_id = str(info.get("channel_id") or "").strip()
                        label = str(info.get("label") or "").strip()
                        if ch_id:
                            channels.append({"channel_id": ch_id, "label": label})
            except Exception:
                pass
            try:
                for (sw_id, ch_id), _topic in (mqtt_ingest.nodus_switch_command_topics or {}).items():
                    if sw_id == device_id and ch_id:
                        channels.append({"channel_id": ch_id, "label": ""})
            except Exception:
                pass

        # de-dupe, preserve order
        seen: set[str] = set()
        out: list[dict] = []
        for entry in channels:
            ch_id = str(entry.get("channel_id", "") or "").strip()
            if ch_id and ch_id not in seen:
                seen.add(ch_id)
                out.append(entry)
        return out

    def _collect_sensor_metrics(device_id: str, data_logger, mqtt_ingest=None) -> list[str]:
        metrics: list[str] = []
        try:
            from .saiSensorSettingsManager import SensorSettingsManager
            mgr = SensorSettingsManager("sensor_settings")
            metrics = mgr.get_display_metrics(device_id) or []
        except Exception:
            metrics = []

        if not metrics and mqtt_ingest:
            try:
                metrics = list(mqtt_ingest.expected_gauge_map.get(device_id) or [])
            except Exception:
                metrics = []

        if not metrics and data_logger:
            try:
                latest = data_logger.get_latest_values(device_id) or {}
                metrics = list(latest.keys())
            except Exception:
                metrics = []

        # de-dupe, preserve order, drop blanks
        seen: set[str] = set()
        out: list[str] = []
        for m in metrics:
            val = str(m or "").strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
        return out

    def _clear_ha_entities(device_id: str, *, mqtt_ingest=None, data_logger=None) -> dict:
        stats = {"topics_cleared": 0, "ids_expanded": []}
        if not mqtt_ingest:
            return stats
        try:
            from .saiHomeAssistantMqtt import slugify, HomeAssistantTopicMap
        except Exception:
            return stats

        topic_map = getattr(mqtt_ingest, "topic_map", None)
        if not topic_map:
            try:
                node_id = socket.gethostname()
                base_topic = getattr(mqtt_ingest, "base_topic", "sensorius")
                topic_map = HomeAssistantTopicMap(node_id=node_id, base_topic=base_topic, discovery_prefix="homeassistant")
            except Exception:
                topic_map = None
        if not topic_map:
            return stats

        topics: list[str] = []
        ids = _collect_related_device_ids(device_id, mqtt_ingest=mqtt_ingest)
        stats["ids_expanded"] = ids
        for dev_id in ids:
            metrics = _collect_sensor_metrics(dev_id, data_logger, mqtt_ingest)
            channels = _collect_switch_channels(dev_id, mqtt_ingest)
            for metric in metrics:
                object_id = f"{dev_id}__{slugify(metric)}"
                topics.append(topic_map.sensor_discovery_topic(object_id))
            for ch in channels:
                ch_id = str(ch.get("channel_id") or "").strip()
                label = str(ch.get("label") or "").strip()
                if ch_id:
                    topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{ch_id}"))
                if label:
                    topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{slugify(label)}"))

        if not topics:
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_sensor_metrics", None) or set()
                for key in list(known):
                    for dev_id in ids:
                        if key.startswith(f"{dev_id}::"):
                            metric_slug = key.split("::", 1)[1]
                            topics.append(topic_map.sensor_discovery_topic(f"{dev_id}__{metric_slug}"))
            except Exception:
                pass
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_switch_channels", None) or set()
                for key in list(known):
                    for dev_id in ids:
                        if key.startswith(f"{dev_id}::"):
                            ch_id = key.split("::", 1)[1]
                            topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{ch_id}"))
            except Exception:
                pass

        if not topics:
            return stats

        client = mqtt_ingest.ha_client or mqtt_ingest.client
        for topic in topics:
            try:
                info = client.publish(topic, payload="", qos=0, retain=True)
                if getattr(info, "rc", 0) == 0:
                    stats["topics_cleared"] += 1
            except Exception:
                pass

        return stats

    def _clear_retained_mqtt_topics(device_id: str, *, mqtt_ingest=None) -> dict:
        stats = {"topics_cleared": 0, "ids_expanded": []}
        if not mqtt_ingest:
            return stats
        client = mqtt_ingest.client
        base_topic = getattr(mqtt_ingest, "base_topic", "") or ""
        ids = _collect_related_device_ids(device_id, mqtt_ingest=mqtt_ingest)
        ids_l = {i.lower() for i in ids}
        stats["ids_expanded"] = ids

        topics: set[str] = set()
        try:
            for topic, dev in (mqtt_ingest.topic_dev_id_map or {}).items():
                if str(dev or "").strip().lower() in ids_l and topic:
                    topics.add(topic)
        except Exception:
            pass
        for dev_id in ids:
            for suffix in ("data", "state", "availability"):
                topics.add(f"nodus/{dev_id}/{suffix}")
                topics.add(f"switch/{dev_id}/{suffix}")
                if base_topic:
                    topics.add(f"{base_topic}/nodus/{dev_id}/{suffix}")
                    topics.add(f"{base_topic}/switch/{dev_id}/{suffix}")
            for suffix in (
                "meta",
                "meta/switch",
                "meta/patch",
                "status/heartbeat",
                "config/ack",
                "config/result",
                "fwupdate/result",
                "event/calibration_status",
                "event/calibration_progress",
                "event/calibration_sample",
                "event/calibration_result",
            ):
                topics.add(f"nodus/{dev_id}/{suffix}")
                if base_topic:
                    topics.add(f"{base_topic}/nodus/{dev_id}/{suffix}")
        try:
            for topic_map_name in (
                "nodus_switch_command_topics",
                "nodus_switch_state_topics",
                "nodus_switch_event_topics",
                "nodus_switch_ack_topics",
                "nodus_switch_result_topics",
                "nodus_switch_availability_topics",
            ):
                for (sw_id, _ch_id), topic in (getattr(mqtt_ingest, topic_map_name, {}) or {}).items():
                    if str(sw_id or "").strip().lower() in ids_l and topic:
                        topics.add(topic)
        except Exception:
            pass
        try:
            for topic, meta in (mqtt_ingest.nodus_switch_topic_map or {}).items():
                sw_id = str((meta or {}).get("switch_id") or "").strip().lower()
                if sw_id in ids_l and topic:
                    topics.add(topic)
        except Exception:
            pass
        try:
            for (sw_id, _label), base in (getattr(mqtt_ingest, "_set_base_by_label", {}) or {}).items():
                if str(sw_id or "").strip().lower() in ids_l and base:
                    topics.add(f"{base}/set")
        except Exception:
            pass

        for topic in topics:
            try:
                info = client.publish(topic, payload="", qos=0, retain=True)
                if getattr(info, "rc", 0) == 0:
                    stats["topics_cleared"] += 1
            except Exception:
                pass
        return stats

    def _purge_ingest_cache(device_id: str, *, mqtt_ingest=None, data_logger=None) -> dict:
        stats = {"ingest_keys_cleared": 0}
        ing = mqtt_ingest
        if not ing:
            return stats

        def _bump():
            stats["ingest_keys_cleared"] += 1

        ids = _collect_related_device_ids(device_id, mqtt_ingest=ing)
        ids_l = {str(i or "").strip().lower() for i in ids if str(i or "").strip()}
        expanded_keys: set[str] = set()
        for dev_id in ids:
            try:
                base = ing._normalize_host_key(dev_id) if hasattr(ing, "_normalize_host_key") else dev_id
            except Exception:
                base = dev_id
            for key in (dev_id, base, mdns_hostname(base)):
                if key:
                    expanded_keys.add(key)

        for key in expanded_keys:
            if not key:
                continue
            for dname in (
                "device_type",
                "expected_gauge_map",
                "latest_meta",
                "fwupdate_result_by_device",
                "calibration_status_by_sensor",
                "calibration_progress_by_sensor",
                "calibration_sample_by_sensor",
                "calibration_event_result_by_sensor",
                "nodus_firmware_versions",
                "nodus_board_types",
                "nodus_sensor_hardware",
                "_host_ip_cache",
                "_host_ipv4addr",
            ):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            for dname in ("device_status", "last_mqtt_seen", "last_nodus_report_seen", "retained_mqtt_seen", "nodus_availability", "last_heartbeat_ts", "last_heartbeat_payload", "heartbeat_interval_s_by_host", "heartbeat_stale"):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            s = getattr(ing, "mqtt_clients", None)
            if isinstance(s, set) and key in s:
                s.discard(key); _bump()
            d = getattr(ing, "discovery_cache", None)
            if isinstance(d, dict) and key in d:
                d.pop(key, None); _bump()

        # Remove from topic maps keyed by device_id
        try:
            for topic, dev in list((ing.topic_dev_id_map or {}).items()):
                if str(dev or "").strip().lower() in ids_l:
                    ing.topic_dev_id_map.pop(topic, None); _bump()
        except Exception:
            pass

        def _topic_matches_removed_id(topic: str) -> bool:
            parts = [str(part or "").strip().lower() for part in str(topic or "").split("/")]
            return bool(set(parts) & ids_l)

        try:
            locations = getattr(ing, "device_location", None)
            if isinstance(locations, dict):
                for topic in list(locations.keys()):
                    if _topic_matches_removed_id(topic):
                        locations.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            registered = getattr(ing, "registered_topics", None)
            if isinstance(registered, set):
                for topic in list(registered):
                    if "+" not in str(topic) and "#" not in str(topic) and _topic_matches_removed_id(topic):
                        registered.discard(topic); _bump()
        except Exception:
            pass

        try:
            for topic, meta in list((ing.switch_topic_meta or {}).items()):
                if str((meta or {}).get("switch_id") or "").strip().lower() in ids_l:
                    ing.switch_topic_meta.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_control_map or {}).keys()):
                if key and str((key[0] if len(key) > 0 else "") or "").strip().lower() in ids_l:
                    ing.switch_control_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_channel_map or {}).keys()):
                if key and str((key[0] if len(key) > 0 else "") or "").strip().lower() in ids_l:
                    ing.switch_channel_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for topic, info in list((ing.nodus_switch_topic_map or {}).items()):
                if str((info or {}).get("switch_id") or "").strip().lower() in ids_l:
                    ing.nodus_switch_topic_map.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for map_name in (
                "nodus_switch_command_topics",
                "nodus_switch_state_topics",
                "nodus_switch_event_topics",
                "nodus_switch_availability_topics",
                "nodus_switch_ack_topics",
                "nodus_switch_result_topics",
            ):
                topic_map = getattr(ing, map_name, None)
                if not isinstance(topic_map, dict):
                    continue
                for key in list(topic_map.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel_id = str((key[1] if isinstance(key, tuple) and len(key) > 1 else "") or "").strip().lower()
                    if switch_id in ids_l or channel_id in ids_l:
                        topic_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            channel_topics = getattr(ing, "nodus_channel_command_topics", None)
            if isinstance(channel_topics, dict):
                for channel_id in list(channel_topics.keys()):
                    if str(channel_id or "").strip().lower() in ids_l:
                        channel_topics.pop(channel_id, None); _bump()
        except Exception:
            pass

        try:
            sensor_topics = getattr(ing, "nodus_sensor_topics", None)
            if isinstance(sensor_topics, dict):
                for sensor_id in list(sensor_topics.keys()):
                    if str(sensor_id or "").strip().lower() in ids_l:
                        sensor_topics.pop(sensor_id, None); _bump()
        except Exception:
            pass

        for map_name in ("nodus_sensor_hosts", "nodus_sensor_config_files"):
            try:
                mapping = getattr(ing, map_name, None)
                if isinstance(mapping, dict):
                    for sensor_id in list(mapping.keys()):
                        if str(sensor_id or "").strip().lower() in ids_l:
                            mapping.pop(sensor_id, None)
                            _bump()
            except Exception:
                pass
        try:
            mapping = getattr(ing, "nodus_host_sensors", None)
            if isinstance(mapping, dict):
                for host in list(mapping.keys()):
                    host_l = str(host or "").strip().lower()
                    children = {
                        str(child or "").strip().lower()
                        for child in (mapping.get(host) or [])
                    }
                    if host_l in ids_l or children & ids_l:
                        mapping.pop(host, None)
                        _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_known_switch_ids", None)
            if isinstance(s, set):
                for sid in list(s):
                    if str(sid or "").strip().lower() in ids_l:
                        s.discard(sid); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_switch_state_cache", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    if str(key or "").strip().lower() in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_pending_set", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    sid = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    if sid in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_set_base_by_label", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    if switch_id in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "nodus_label_to_channel", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    sid = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel = str(d.get(key) or "").strip().lower()
                    if sid in ids_l or channel in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_sensor_metrics", None)
            if isinstance(s, set):
                for key in list(s):
                    key_l = str(key or "").strip().lower()
                    if any(key_l.startswith(f"{dev}::") for dev in ids_l):
                        s.discard(key); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_switch_channels", None)
            if isinstance(s, set):
                for key in list(s):
                    key_l = str(key or "").strip().lower()
                    if any(key_l.startswith(f"{dev}::") for dev in ids_l):
                        s.discard(key); _bump()
        except Exception:
            pass

        try:
            mapping = getattr(ing, "host_to_peer_ids", None)
            if isinstance(mapping, dict):
                for dev_id in ids:
                    dev_base = normalize_hostname_base(dev_id) or dev_id
                    if dev_base in mapping:
                        mapping.pop(dev_base, None); _bump()
                for host, peers in list(mapping.items()):
                    peer_set_l = {str(p or "").strip().lower() for p in (peers or [])}
                    if peer_set_l & {i.lower() for i in ids}:
                        peers[:] = [p for p in peers if str(p or "").strip().lower() not in {i.lower() for i in ids}]
                        _bump()
        except Exception:
            pass

        for set_name in ("_live_sensor_shadow_seeded", "_dashboard_inventory_notified_sensors"):
            try:
                values = getattr(ing, set_name, None)
                if isinstance(values, set):
                    for value in list(values):
                        if str(value or "").strip().lower() in ids_l:
                            values.discard(value); _bump()
            except Exception:
                pass

        try:
            attempts = getattr(ing, "_live_sensor_shadow_attempt_at", None)
            if isinstance(attempts, dict):
                for key in list(attempts.keys()):
                    if str(key or "").strip().lower() in ids_l:
                        attempts.pop(key, None); _bump()
        except Exception:
            pass

        try:
            persisted = getattr(ing, "_last_persisted_switch_state", None)
            if isinstance(persisted, dict):
                for key in list(persisted.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel_id = str((key[1] if isinstance(key, tuple) and len(key) > 1 else "") or "").strip().lower()
                    if switch_id in ids_l or channel_id in ids_l:
                        persisted.pop(key, None); _bump()
        except Exception:
            pass

        try:
            if data_logger:
                for dev_id in ids:
                    if dev_id in data_logger.sensor_values:
                        data_logger.sensor_values.pop(dev_id, None); _bump()
                    if dev_id in getattr(data_logger, "sensor_stats", {}):
                        data_logger.sensor_stats.pop(dev_id, None); _bump()
        except Exception:
            pass

        return stats

    def _purge_switch_controller_state(device_ids: list[str]) -> dict:
        stats = {"switch_controllers_cleared": 0, "switch_monitors_cancelled": 0}
        ids_l = {str(i or "").strip().lower() for i in (device_ids or []) if str(i or "").strip()}
        if not ids_l:
            return stats

        def _purge_map(sc_map) -> None:
            if not isinstance(sc_map, dict):
                return
            for key, ctrl in list(sc_map.items()):
                key_l = str(key or "").strip().lower()
                sid_l = str(getattr(ctrl, "switch_id", "") or "").strip().lower()
                channel_ids = set()
                try:
                    channel_ids.update(str(v or "").strip().lower() for v in (getattr(ctrl, "channel_id_for_label", {}) or {}).values())
                except Exception:
                    pass
                if key_l in ids_l or sid_l in ids_l or bool(channel_ids & ids_l):
                    sc_map.pop(key, None)
                    stats["switch_controllers_cleared"] += 1

        _purge_map(services.switch_controllers)

        for key, task in list(route_state.dynamic_switch_monitor_tasks.items()):
            if str(key or "").strip().lower() not in ids_l:
                continue
            route_state.dynamic_switch_monitor_tasks.pop(key, None)
            try:
                if task and not task.done():
                    task.cancel()
                stats["switch_monitors_cancelled"] += 1
            except Exception:
                pass

        return stats

    def _delete_device_dirs(device_id:str)->dict:
        removed={"sensor":False,"switch":False,"system":False, "ids_deleted":[]}
        targets = [
            ("sensor", _safe_child_path(_settings_base_path(_SENSOR_BASE_DIR), device_id)),
            ("switch", _safe_child_path(_settings_base_path(_SWITCH_BASE_DIR), device_id)),
            ("system", _safe_child_path(_settings_base_path(_SYS_BASE_DIR), device_id)),
        ]
        for key, path in targets:
            try:
                if path is None:
                    continue
                if path.exists():
                    shutil.rmtree(path)
                    removed[key]=True
                    removed["ids_deleted"].append(device_id)
                    if key == "sensor":
                        SensorSettingsManager.invalidate_cache(device_id, str(_settings_base_path(_SENSOR_BASE_DIR)))
                    elif key == "switch":
                        SwitchSettingsManager.invalidate_cache(device_id, str(_settings_base_path(_SWITCH_BASE_DIR)))
            except Exception as e:
                printDM(f"[remove-device] rmtree {path}: {e}", location=MODULE)
        return removed

    def _delete_device_dirs_many(device_ids: list[str])->dict:
        removed={"sensor":False,"switch":False,"system":False, "ids_deleted":[]}
        seen: set[str] = set()
        for did in device_ids:
            dev = str(did or "").strip()
            if not dev or dev.lower() in seen:
                continue
            seen.add(dev.lower())
            one = _delete_device_dirs(dev)
            for key in ("sensor", "switch", "system"):
                removed[key] = bool(removed.get(key)) or bool(one.get(key))
            for deleted_id in (one.get("ids_deleted") or []):
                if deleted_id not in removed["ids_deleted"]:
                    removed["ids_deleted"].append(deleted_id)
        return removed

    def _active_data_logger():
        return services.data_logger

    def _get_db_path()->str:
        try:
            active_logger = _active_data_logger()
            db_path = str(getattr(active_logger, "db_path", "") or "").strip()
            if db_path:
                return db_path
        except Exception:
            pass
        try:
            from .saiDataLogger import saiDataLogger  # type: ignore
            if hasattr(saiDataLogger,"DB_PATH"): return getattr(saiDataLogger,"DB_PATH")
            if hasattr(saiDataLogger,"get_db_path"): return saiDataLogger.get_db_path()  # type: ignore
        except Exception:
            pass
        if Path("sensorius_data.db").exists(): return "sensorius_data.db"
        if Path("data/sensorius_data.db").exists(): return "data/sensorius_data.db"
        return "sensorius_data.db"

    def _table_has_column(cur:sqlite3.Cursor, table:str, column:str)->bool:
        try:
            cur.execute(f'PRAGMA table_info("{table}")')
            return column in [r[1] for r in cur.fetchall()]
        except Exception:
            return False

    def _purge_device_from_db(device_id:str)->dict:
        db_path=_get_db_path()
        stats={"db_path":db_path,"rows_deleted":0,"tables":[]}
        purged_tables: set[str] = set()
        try:
            active_logger = _active_data_logger()
            purge_sensor_data = getattr(active_logger, "purge_sensor_data", None)
            if callable(purge_sensor_data):
                sensor_stats = purge_sensor_data(device_id)
                stats["rows_deleted"] += int(sensor_stats.get("rows_deleted", 0) or 0)
                for entry in (sensor_stats.get("tables", []) or []):
                    if isinstance(entry, dict):
                        purged_tables.update(str(k) for k in entry.keys())
                    stats["tables"].append(entry)
                logger_db_path = str(getattr(active_logger, "db_path", "") or "").strip()
                if logger_db_path:
                    db_path = logger_db_path
                    stats["db_path"] = logger_db_path
        except Exception as e:
            printDM(f"[remove-device] active logger sensor purge failed: {e}", location=MODULE)
        if not Path(db_path).exists(): return stats
        try:
            conn = _sqlite_connect_with_recovery(db_path, source="remove_device_purge")
            cur=conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables=[r[0] for r in cur.fetchall()]
            target_cols=["sensor_id","device_id","client_id","switch_id","switch_key"]
            like_cols=["topic","source","channel","switch_key"]
            for t in tables:
                if t in purged_tables:
                    continue
                deleted=0
                for col in target_cols:
                    if _table_has_column(cur,t,col):
                        cur.execute(f'DELETE FROM "{t}" WHERE "{col}"=?',(device_id,))
                        deleted+=cur.rowcount
                for col in like_cols:
                    if _table_has_column(cur,t,col):
                        cur.execute(f'DELETE FROM "{t}" WHERE "{col}" LIKE ?',(f"%{device_id}%",))
                        deleted+=cur.rowcount
                if deleted:
                    stats["tables"].append({t:deleted})
                    stats["rows_deleted"]+=deleted
            conn.commit(); conn.close()
        except Exception as e:
            printDM(f"[remove-device] DB purge failed: {e}", location=MODULE)
        return stats

    def build_sensor_locations_map() -> dict[str, str]:
        from .saiSensorSettingsManager import SensorSettingsManager
        mgr = SensorSettingsManager("sensor_settings")
        locations: dict[str, str] = {}
        for sid in mgr.list_ids():
            loc = mgr.get_setting(sid, "Sensor.LOCATION", "Unknown")
            locations[sid] = loc or "Unknown"
        return locations

    @router.get("/halt-device-list")
    async def halt_device_list(request: Request):
        """List physical devices with explicit MQTT halt capability."""
        _require_protected_access(request, require_csrf=True)
        from .saiNodusHalt import halt_device_ids
        ingest = services.mqtt_ingest
        devices = [device for device in halt_device_ids(ingest) if _is_valid_device_id(device)]
        return JSONResponse({"devices": devices}, headers={"Cache-Control": "no-store"})

    @router.post("/halt-device")
    async def halt_device_post(request: Request):
        """Halt acknowledged selections without removing their settings or data."""
        _require_protected_access(request, require_csrf=True)
        from .saiNodusHalt import halt_device_ids, halt_device
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict) or body.get("acknowledged") is not True:
            return JSONResponse({"error": "Halt acknowledgement required"}, status_code=400)
        ids = body.get("device_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(d, str) or not _is_valid_device_id(d) for d in ids):
            return JSONResponse({"error": "Invalid device_ids"}, status_code=400)
        ids = list(dict.fromkeys(ids))
        ingest = services.mqtt_ingest
        supported = set(halt_device_ids(ingest))
        if any(d not in supported for d in ids):
            return JSONResponse({"error": "Device does not advertise halt support"}, status_code=400)
        if not all(callable(getattr(ingest, name, None)) for name in ("publish_nodus_halt", "wait_for_config_result")):
            return JSONResponse({"error": "MQTT halt unavailable"}, status_code=503)
        results = []
        for device in ids:
            try:
                results.append(await halt_device(ingest, device))
            except Exception:
                results.append({"device_id": device, "halted": False, "error": "Halt failed; device state is unconfirmed"})
        return JSONResponse({"results": results})

    #remove device routes
    @router.get("/remove-device-list")
    async def remove_device_list(request: Request):
        _require_protected_access(request, require_csrf=True)
        devices = await asyncio.to_thread(_collect_removable_ids)
        device_details = await asyncio.to_thread(_collect_removable_details, devices)
        return JSONResponse({"devices": devices, "device_details": device_details})

    @router.get("/remove-device")
    async def remove_device_modal_hint(request: Request):
        """
        Kept for compatibility in case someone navigates to /remove-device.
        We just return a tiny page that instructs to use the modal button.
        """
        _require_protected_access(request, require_csrf=True)
        return HTMLResponse("<html><body><p>Use the Halt or Remove Device button to open the modal.</p></body></html>")

    @router.post("/remove-device")
    async def remove_device_post(request: Request):
        """
        Accepts JSON: {"device_ids": ["id1","id2",...]} or form with multiple 'device_ids'
        Executes removal for each device; returns JSON summary.
        """
        _require_protected_access(request, require_csrf=True)
        device_ids: list[str] = []
        ctype = request.headers.get("content-type","")
        if "application/json" in ctype.lower():
            body = await request.json()
            device_ids = list(body.get("device_ids", []) or [])
        else:
            form = await request.form()
            # supports checkboxes named device_ids
            raw = form.getlist("device_ids")
            device_ids = [x for x in raw if x]

        device_ids = [d.strip() for d in device_ids if (d or "").strip()]
        if not device_ids:
            return JSONResponse({"error": "No device_ids provided"}, status_code=400)
        invalid_ids = [d for d in device_ids if not _is_valid_device_id(d)]
        if invalid_ids:
            return JSONResponse({"error": "invalid_device_id", "count": len(invalid_ids)}, status_code=400)

        mqtt_ingest = services.mqtt_ingest

        requested_ids = list(dict.fromkeys(device_ids))
        related_ids: list[str] = []
        seen_related: set[str] = set()
        cleanup_targets: list[str] = []
        for dev in requested_ids:
            expanded = _collect_related_device_ids(dev, mqtt_ingest=mqtt_ingest)
            if dev not in expanded:
                expanded.insert(0, dev)
            expanded_keys = {
                str(value or "").strip().lower()
                for value in expanded
                if str(value or "").strip()
            }
            if not (expanded_keys & seen_related):
                cleanup_targets.append(dev)
            for raw in expanded:
                value = str(raw or "").strip()
                key = value.lower()
                if value and key not in seen_related:
                    seen_related.add(key)
                    related_ids.append(value)

        known_remote_ids: set[str] = set()
        if mqtt_ingest:
            try:
                known_remote_ids.update(str(value or "").strip().lower() for value in (mqtt_ingest.get_known_devices() or []))
            except Exception:
                pass
            try:
                known_remote_ids.update(str(value or "").strip().lower() for value in (mqtt_ingest.get_known_switch_devices() or []))
            except Exception:
                pass
        remote_remove = bool(known_remote_ids & seen_related)
        if not remote_remove:
            remote_remove = any(
                (not is_direct_local_sensor_id(dev)) and _sensor_shadow_is_remote_nodus(dev)
                for dev in requested_ids
                if not _is_switch_id(dev)
            )
        if not remote_remove:
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                for dev in requested_ids:
                    if not _is_switch_id(dev):
                        continue
                    doc = switch_mgr.load(dev) or {}
                    switch_type = str(((doc.get("Switch") or {}).get("TYPE") or "")).strip().lower()
                    if switch_type in {"nodus", "picow", "pico2w", "remote", "mqtt"}:
                        remote_remove = True
                        break
            except Exception:
                pass

        suppression = {"active": False, "persisted": False, "persistence_supported": False, "added": []}
        if remote_remove and mqtt_ingest:
            suppress = getattr(mqtt_ingest, "suppress_nodus_devices", None)
            if callable(suppress):
                suppression = suppress(related_ids, persist=True) or suppression
                if suppression.get("persistence_supported") and not suppression.get("persisted"):
                    return JSONResponse(
                        {"error": "removed_device_suppression_persist_failed", "device_ids": requested_ids},
                        status_code=500,
                    )

        def _purge_db_many(ids: list[str]) -> dict:
            merged = {"db_path": _get_db_path(), "rows_deleted": 0, "tables": [], "ids_purged": list(ids)}
            for did in ids:
                one = _purge_device_from_db(did)
                merged["rows_deleted"] += int(one.get("rows_deleted", 0) or 0)
                merged["tables"].extend(one.get("tables", []) or [])
            return merged

        active_logger = _active_data_logger()
        ha_stats = {"topics_cleared": 0, "ids_expanded": []}
        mqtt_stats = {"topics_cleared": 0, "ids_expanded": []}
        for dev in cleanup_targets:
            one_ha = await asyncio.to_thread(
                _clear_ha_entities,
                dev,
                mqtt_ingest=mqtt_ingest,
                data_logger=active_logger,
            )
            one_mqtt = await asyncio.to_thread(_clear_retained_mqtt_topics, dev, mqtt_ingest=mqtt_ingest)
            ha_stats["topics_cleared"] += int(one_ha.get("topics_cleared", 0) or 0)
            mqtt_stats["topics_cleared"] += int(one_mqtt.get("topics_cleared", 0) or 0)
            ha_stats["ids_expanded"].extend(one_ha.get("ids_expanded", []) or [])
            mqtt_stats["ids_expanded"].extend(one_mqtt.get("ids_expanded", []) or [])

        removed_dirs, db_stats = await asyncio.gather(
            asyncio.to_thread(_delete_device_dirs_many, related_ids),
            asyncio.to_thread(_purge_db_many, related_ids),
        )
        ok_settings = all(
            await asyncio.gather(
                *[asyncio.to_thread(_remove_client_from_hub_settings, dev) for dev in requested_ids]
            )
        )
        ingest_stats = {"ingest_keys_cleared": 0}
        for dev in cleanup_targets:
            one_ingest = _purge_ingest_cache(dev, mqtt_ingest=mqtt_ingest, data_logger=active_logger)
            ingest_stats["ingest_keys_cleared"] += int(one_ingest.get("ingest_keys_cleared", 0) or 0)
        controller_stats = _purge_switch_controller_state(related_ids)

        _invalidate_dashboard_caches()
        route_state.switch_status_payload = None
        route_state.switch_status_until = 0.0

        remaining = sorted(
            seen_related
            & {str(value or "").strip().lower() for value in await asyncio.to_thread(_collect_removable_ids)}
        )
        summary = (
            f"dirs(sensor={removed_dirs.get('sensor')},switch={removed_dirs.get('switch')},system={removed_dirs.get('system')}), "
            f"db_rows={db_stats.get('rows_deleted',0)}, clients_updated={ok_settings}, "
            f"ha_topics={ha_stats.get('topics_cleared',0)}, mqtt_topics={mqtt_stats.get('topics_cleared',0)}, "
            f"ingest_keys={ingest_stats.get('ingest_keys_cleared',0)}, "
            f"switch_controllers={controller_stats.get('switch_controllers_cleared',0)}, "
            f"switch_monitors={controller_stats.get('switch_monitors_cancelled',0)}, "
            f"suppressed={len(suppression.get('added', []) or [])}"
        )
        printDM(f"[remove-device] {requested_ids}: {summary}", location=MODULE)
        result = {
            "requested_ids": requested_ids,
            "cleanup_targets": cleanup_targets,
            "related_ids": related_ids,
            "dirs": removed_dirs,
            "db": db_stats,
            "clients_updated": ok_settings,
            "ha": ha_stats,
            "mqtt": mqtt_stats,
            "ingest": ingest_stats,
            "controllers": controller_stats,
            "suppression": suppression,
            "remaining_ids": remaining,
            "summary": summary,
        }
        results = {dev: result for dev in requested_ids}
        if remaining:
            return JSONResponse(
                {"error": "remove_device_verification_failed", "remaining_ids": remaining, "results": results},
                status_code=409,
            )

        if remote_remove and mqtt_ingest:
            allow = getattr(mqtt_ingest, "allow_nodus_devices", None)
            if callable(allow):
                released = await asyncio.to_thread(allow, related_ids, persist=True)
                result["suppression_release"] = released
                if suppression.get("persistence_supported") and not released.get("persisted"):
                    return JSONResponse(
                        {"error": "removed_device_suppression_clear_failed", "results": results},
                        status_code=500,
                    )
                suppression["active"] = False

        overall = f"Removed {len(requested_ids)} device(s)."
        return JSONResponse({"ok": True, "results": results, "summary": overall})

    # submit routes and helpers
    @router.post("/retry-discovery")
    async def retry_discovery(request: Request):
        data = await request.json()
        hostname = data.get("host")
        if hostname in services.mqtt_ingest.discovery_failures:
            del services.mqtt_ingest.discovery_failures[hostname]
            services.mqtt_ingest.device_status[hostname] = "unknown"
            return JSONResponse(content={"status": "retrying"})
        return JSONResponse(content={"status": "unknown host"}, status_code=400)

    @router.post("/submit-pi-setup")
    async def submit_pi_setup(request: Request):
        from fastapi.responses import RedirectResponse
        form = await request.form()
        settings = saiSettings()

        #settings.replace_setting("Network", "SSID", form.get("ssid", ""))
        #settings.replace_setting("Network", "PASSWORD", form.get("password", ""))
        #settings.replace_setting("Network", "HOSTNAME", form.get("hostname", ""))

        astral_form_present = any(key in form for key in ("astral_lat", "astral_lon", "astral_altitude"))
        weather_form_present = any(
            key in form
            for key in ("weather_forecast_provider", "weather_forecast_theme", "weather_forecast_sensor_id")
        )

        broker = str(form.get("broker", settings.get_setting("SensorNetwork", "BROKER", "")) or "").strip()
        tz = str(form.get("tz", settings.get_setting("Time", "TZ", "America/Denver")) or "").strip()
        raw_httpport = str(form.get("httpport", settings.get_setting("Network", "HTTPPORT", 8000)) or "").strip()
        raw_mqttport = str(form.get("mqttport", settings.get_setting("SensorNetwork", "MQTTPORT", 1883)) or "").strip()
        sensornetwork_use_tls = str(form.get("sensornetwork_use_tls", "") or "").strip().lower() in ("1", "true", "on", "yes")
        raw_lat = str(form.get("astral_lat", "") or "").strip()
        raw_lon = str(form.get("astral_lon", "") or "").strip()
        raw_altitude = str(form.get("astral_altitude", "") or "").strip()
        raw_astral_location_name = str(form.get("astral_location_name", "") or "").strip()
        gauge_size = str(form.get("gauge_size", "") or "").strip()
        display_style = str(form.get("display_style", "") or "").strip()
        raw_unit_system = str(
            form.get("unit_system", settings.get_setting("Display", "unit_system", "Imperial")) or ""
        ).strip()
        if "unit_system" in form and raw_unit_system not in DISPLAY_UNIT_SYSTEMS:
            return _modal_error_response(request, "Display unit system is not supported.", status_code=400)
        unit_system = normalize_display_unit_system(raw_unit_system)
        dashboard_orders = {}
        for field in ("sensor_row_order", "switch_row_order"):
            value = str(form.get(field, settings.get_setting("Display", field, "location_device")) or "").strip()
            if field in form and value not in {"location_device", "existing"}:
                return _modal_error_response(request, "Dashboard row order is not supported.", status_code=400)
            dashboard_orders[field] = value
        raw_metric_set = str(
            form.get("metric_set", settings.get_setting("Display", "metric_set", "Pick 6")) or ""
        ).strip()
        if "metric_set" in form and raw_metric_set not in DASHBOARD_METRIC_SETS:
            return _modal_error_response(request, "Dashboard metric set is not supported.", status_code=400)
        metric_set = normalize_dashboard_metric_set(raw_metric_set)
        raw_dashboard_background_theme = str(
            form.get(
                "dashboard_background_theme",
                settings.get_setting("Display", "background_theme", "leaf"),
            )
            or ""
        ).strip().lower().replace("-", "_")
        if "dashboard_background_theme" in form:
            custom_dashboard = theme_manager.resolve("sensorius", raw_dashboard_background_theme)
            if raw_dashboard_background_theme not in DASHBOARD_BACKGROUND_THEMES and not custom_dashboard:
                return _modal_error_response(request, "Dashboard background theme is not supported.", status_code=400)
        dashboard_background_theme = normalize_theme_selection(
            theme_manager,
            "sensorius",
            raw_dashboard_background_theme,
            "leaf",
            normalize_dashboard_background_theme,
        )
        raw_biodynamic_calendar_theme = str(
            form.get(
                "biodynamic_calendar_theme",
                settings.get_setting("Display", "biodynamic_calendar_theme", "garden_tools"),
            )
            or ""
        ).strip().lower().replace("-", "_")
        if "biodynamic_calendar_theme" in form:
            custom_biodynamic = theme_manager.resolve("biodynamic", raw_biodynamic_calendar_theme)
            if raw_biodynamic_calendar_theme not in BIODYNAMIC_CALENDAR_THEMES and not custom_biodynamic:
                return _modal_error_response(request, "Biodynamic Calendar theme is not supported.", status_code=400)
        biodynamic_calendar_theme = normalize_theme_selection(
            theme_manager,
            "biodynamic",
            raw_biodynamic_calendar_theme,
            "garden_tools",
            normalize_biodynamic_calendar_theme,
        )
        raw_weather_forecast_provider = str(
            form.get(
                "weather_forecast_provider",
                settings.get_setting("WeatherForecast", "PROVIDER", "met_no"),
            )
            or ""
        ).strip()
        weather_forecast_provider = normalize_weather_forecast_provider(raw_weather_forecast_provider)
        raw_weather_forecast_theme = str(
            form.get("weather_forecast_theme", settings.get_setting("WeatherForecast", "THEME", "pollinator")) or ""
        ).strip().lower()
        if weather_form_present:
            custom_weather = theme_manager.resolve("caelus", raw_weather_forecast_theme)
            if raw_weather_forecast_theme not in WEATHER_THEMES and not custom_weather:
                return _modal_error_response(request, "Weather Forecast theme is not supported.", status_code=400)
        weather_forecast_theme = normalize_theme_selection(
            theme_manager,
            "caelus",
            raw_weather_forecast_theme,
            "pollinator",
            normalize_weather_theme,
        )
        weather_forecast_sensor_id = str(
            form.get(
                "weather_forecast_sensor_id",
                settings.get_setting("WeatherForecast", "CURRENT_SENSOR_ID", ""),
            )
            or ""
        ).strip()
        if weather_form_present and weather_forecast_sensor_id and not re.match(r"^[A-Za-z0-9._-]+$", weather_forecast_sensor_id):
            return _modal_error_response(
                request,
                "Weather Forecast sensor ID may contain only letters, numbers, dot, underscore, and dash.",
                status_code=400,
            )
        email_form_present = "email_enabled" in form
        notification_rules_form_present = "notification_rules_json" in form
        email_enabled = str(form.get("email_enabled", "false") or "").strip().lower() in ("1", "true", "on", "yes")
        email_smtp_host = str(form.get("email_smtp_host", "") or "").strip()
        raw_email_smtp_port = str(form.get("email_smtp_port", "") or "").strip()
        email_security = str(form.get("email_security", "ssl") or "").strip().lower()
        email_username = str(form.get("email_username", "") or "").strip()
        email_password_new = str(form.get("email_app_password", "") or "").strip().replace(" ", "")
        email_from = str(form.get("email_from", "") or "").strip()
        email_to = str(form.get("email_to", "") or "").strip()
        raw_notification_rules = str(form.get("notification_rules_json", "[]") or "[]").strip()
        astral_reset_requested = astral_form_present and not raw_lat and not raw_lon

        if "astral_location_name" in form:
            if "\r" in raw_astral_location_name or "\n" in raw_astral_location_name:
                return _modal_error_response(request, "Community/Location Name cannot contain line breaks.", status_code=400)
            if len(raw_astral_location_name) > 120:
                return _modal_error_response(request, "Community/Location Name must be 120 characters or fewer.", status_code=400)

        email_fields = (email_smtp_host, email_username, email_from, email_to)
        if email_form_present and any("\r" in value or "\n" in value for value in email_fields):
            return _modal_error_response(request, "Email settings cannot contain line breaks.", status_code=400)
        try:
            email_smtp_port = int(raw_email_smtp_port or "465")
        except Exception:
            return _modal_error_response(request, "Email SMTP Port must be a number.", status_code=400)
        if email_form_present and not 1 <= email_smtp_port <= 65535:
            return _modal_error_response(request, "Email SMTP Port must be between 1 and 65535.", status_code=400)
        if email_form_present and email_security not in {"ssl", "starttls"}:
            return _modal_error_response(request, "Email Security must be SSL/TLS or STARTTLS.", status_code=400)
        current_email_password = str(os.environ.get("SENSORIUS_EMAIL_APP_PASSWORD", "") or "").strip()
        if not current_email_password:
            current_email_password = str(_env_map_with_defaults().get("SENSORIUS_EMAIL_APP_PASSWORD", "") or "").strip()
        if email_form_present and email_enabled:
            if not email_smtp_host or not email_username:
                return _modal_error_response(request, "SMTP Server and Username are required when email is enabled.", status_code=400)
            if "@" not in email_from:
                return _modal_error_response(request, "A valid From email address is required.", status_code=400)
            if not email_password_new and not current_email_password:
                return _modal_error_response(request, "A Google App Password is required when email is enabled.", status_code=400)
        if email_form_present and email_to and "@" not in email_to:
            return _modal_error_response(request, "A valid test To email address is required.", status_code=400)
        notification_rules = []
        if notification_rules_form_present:
            try:
                submitted_rules = json.loads(raw_notification_rules or "[]")
            except Exception:
                return _modal_error_response(request, "Notification Rules are not valid JSON.", status_code=400)
            notification_rules = normalize_notification_rules(submitted_rules)
            if isinstance(submitted_rules, list) and len(notification_rules) != len(submitted_rules):
                return _modal_error_response(
                    request,
                    "Every notification rule requires a sensor metric, > or < operator, numeric threshold, and non-negative hysteresis.",
                    status_code=400,
                )

        if ("tz" in form or astral_form_present) and not tz:
            return _modal_error_response(request, "Time zone is required.", status_code=400)
        if "tz" in form or astral_form_present:
            try:
                ZoneInfo(tz)
            except Exception:
                return _modal_error_response(
                    request,
                    f"Invalid timezone '{tz}'. Use a valid IANA timezone (example: America/Denver).",
                    status_code=400,
                )

        httpport = None
        if "httpport" in form:
            try:
                httpport = int(raw_httpport or "8000")
            except Exception:
                return _modal_error_response(request, "HTTP Port must be a number.", status_code=400)
            if httpport < 1 or httpport > 65535:
                return _modal_error_response(request, "HTTP Port must be between 1 and 65535.", status_code=400)
        mqttport = None
        if "mqttport" in form:
            try:
                mqttport = int(raw_mqttport or "1883")
            except Exception:
                return _modal_error_response(request, "MQTT Port must be a number.", status_code=400)
            if mqttport < 1 or mqttport > 65535:
                return _modal_error_response(request, "MQTT Port must be between 1 and 65535.", status_code=400)

        lat_to_store = None
        lon_to_store = None
        altitude_to_store = None
        astral_tz_to_store = None
        astral_source_to_store = None
        astral_provider_to_store = None
        if "astral_altitude" in form:
            if raw_altitude:
                try:
                    altitude_val = native_input_value(raw_altitude, form.get("astral_altitude_unit"), "m")
                except Exception:
                    return _modal_error_response(request, "Altitude must be a finite number with units of m or ft.", status_code=400)
                if not (-500.0 <= altitude_val <= 10000.0):
                    return _modal_error_response(request, "Altitude must be between -500 and 10000 meters.", status_code=400)
                current_altitude = str(settings.get_setting("Astral", "ALTITUDE", "") or "")
                try:
                    unchanged = abs(float(current_altitude) - altitude_val) < 1e-9
                except ValueError:
                    unchanged = False
                altitude_to_store = current_altitude if unchanged else f"{altitude_val:.2f}"
            else:
                altitude_to_store = ""
        if astral_form_present and not raw_lat and not raw_lon:
            lat_to_store = ""
            lon_to_store = ""
            astral_tz_to_store = ""
            astral_source_to_store = ""
            astral_provider_to_store = ""
        elif astral_form_present and (raw_lat or raw_lon):
            if not raw_lat or not raw_lon:
                return _modal_error_response(
                    request,
                    "Latitude and Longitude must both be provided, or both left empty to re-detect Astral location.",
                    status_code=400,
                )
            try:
                lat_val = float(raw_lat)
                lon_val = float(raw_lon)
            except Exception:
                return _modal_error_response(request, "Latitude and Longitude must be numeric values.", status_code=400)
            if not (-90.0 <= lat_val <= 90.0):
                return _modal_error_response(request, "Latitude must be between -90 and 90.", status_code=400)
            if not (-180.0 <= lon_val <= 180.0):
                return _modal_error_response(request, "Longitude must be between -180 and 180.", status_code=400)
            lat_to_store = f"{lat_val:.6f}"
            lon_to_store = f"{lon_val:.6f}"
            astral_tz_to_store = tz
            astral_source_to_store = "manual"
            astral_provider_to_store = ""

        if "httpport" in form and httpport is not None:
            settings.replace_setting("Network", "HTTPPORT", httpport)
        if "broker" in form:
            settings.replace_setting("SensorNetwork", "BROKER", broker)
        if "mqttport" in form and mqttport is not None:
            settings.replace_setting("SensorNetwork", "MQTTPORT", mqttport)
        if "sensornetwork_use_tls" in form:
            settings.replace_setting("SensorNetwork", "USE_TLS", sensornetwork_use_tls)
        if "tz" in form:
            tz_offset, tz_name = settings.timezone_info(tz)
            settings.replace_setting("Time", "TZ", tz)
            settings.replace_setting("Time", "TZ_OFFSET", tz_offset)
            settings.replace_setting("Time", "TZ_NAME", tz_name)
        if astral_tz_to_store is not None:
            settings.replace_setting("Astral", "TIMEZONE", astral_tz_to_store)
        if lat_to_store is not None:
            settings.replace_setting("Astral", "LATITUDE", lat_to_store)
        if lon_to_store is not None:
            settings.replace_setting("Astral", "LONGITUDE", lon_to_store)
        if astral_source_to_store is not None:
            settings.replace_setting("Astral", "SOURCE", astral_source_to_store)
        if astral_provider_to_store is not None:
            settings.replace_setting("Astral", "PROVIDER", astral_provider_to_store)
        if altitude_to_store is not None:
            settings.replace_setting("Astral", "ALTITUDE", altitude_to_store)
        if "astral_location_name" in form:
            settings.replace_setting("Astral", "LOCATION_NAME", raw_astral_location_name)
        if "gauge_size" in form:
            settings.replace_setting("Display", "gauge_size", gauge_size)
        if "display_style" in form:
            settings.replace_setting("Display", "display_style", display_style)
        if "unit_system" in form:
            settings.replace_setting("Display", "unit_system", unit_system)
            set_notification_unit_system(unit_system)
        for field, value in dashboard_orders.items():
            if field in form:
                settings.replace_setting("Display", field, value)
        if "metric_set" in form:
            settings.replace_setting("Display", "metric_set", metric_set)
        if "dashboard_background_theme" in form:
            settings.replace_setting("Display", "background_theme", dashboard_background_theme)
        if "biodynamic_calendar_theme" in form:
            settings.replace_setting("Display", "biodynamic_calendar_theme", biodynamic_calendar_theme)
        if "weather_forecast_provider" in form:
            settings.replace_setting("WeatherForecast", "PROVIDER", weather_forecast_provider)
        if "weather_forecast_theme" in form:
            settings.replace_setting("WeatherForecast", "THEME", weather_forecast_theme)
        if "weather_forecast_sensor_id" in form:
            settings.replace_setting("WeatherForecast", "CURRENT_SENSOR_ID", weather_forecast_sensor_id)
        if notification_rules_form_present:
            settings.replace_setting(
                "Notifications",
                "RULES_JSON",
                json.dumps(notification_rules, separators=(",", ":")),
            )
        if email_form_present:
            email_env_updates = {
                "SENSORIUS_EMAIL_ENABLED": _bool_text(email_enabled),
                "SENSORIUS_EMAIL_SMTP_HOST": email_smtp_host,
                "SENSORIUS_EMAIL_SMTP_PORT": str(email_smtp_port),
                "SENSORIUS_EMAIL_SECURITY": email_security,
                "SENSORIUS_EMAIL_USERNAME": email_username,
                "SENSORIUS_EMAIL_FROM": email_from,
                "SENSORIUS_EMAIL_TO": email_to,
            }
            if email_password_new:
                email_env_updates["SENSORIUS_EMAIL_APP_PASSWORD"] = email_password_new
            _write_env_updates(email_env_updates)

        astral_response: dict[str, object] = {
            "ok": False,
            "source": "",
            "provider": "",
            "error": "",
            "lat": None,
            "lon": None,
            "tz": "",
        }
        message = "General settings saved."
        if astral_reset_requested:
            try:
                resolved = await resolve_astral_location_async(
                    settings,
                    persist_if_auto=True,
                    timeout_sec=3.5,
                ) or {}
            except Exception as exc:
                resolved = {"error": str(exc)}
            resolved_lat = _safe_float(resolved.get("lat"))
            resolved_lon = _safe_float(resolved.get("lon"))
            resolved_tz = str(resolved.get("tz") or "").strip()
            resolved_source = str(resolved.get("source") or "").strip()
            resolved_provider = str(resolved.get("provider") or "").strip()
            resolved_error = str(resolved.get("error") or "").strip()
            astral_ok = resolved_lat is not None and resolved_lon is not None and bool(resolved_tz)
            astral_response = {
                "ok": bool(astral_ok),
                "source": resolved_source,
                "provider": resolved_provider,
                "error": resolved_error,
                "lat": round(float(resolved_lat), 6) if resolved_lat is not None else None,
                "lon": round(float(resolved_lon), 6) if resolved_lon is not None else None,
                "tz": resolved_tz,
            }
            if astral_ok:
                source_label = resolved_provider or resolved_source or "auto"
                message = f"General settings saved. Astral location re-detected ({source_label})."
            else:
                detail = f" Last error: {resolved_error}" if resolved_error else ""
                message = (
                    "General settings saved. Astral location cleared; automatic IP geolocation did not "
                    f"resolve coordinates.{detail} Enter Latitude and Longitude manually."
                )
        elif lat_to_store and lon_to_store:
            astral_response = {
                "ok": True,
                "source": "manual",
                "provider": "",
                "error": "",
                "lat": float(lat_to_store),
                "lon": float(lon_to_store),
                "tz": astral_tz_to_store or tz,
            }

        _invalidate_dashboard_caches()

        if _wants_modal_json(request):
            return JSONResponse({"ok": True, "message": message, "astral": astral_response})
        return RedirectResponse(url="/?refresh=true", status_code=303)

    @router.post("/notifications/test-email")
    async def test_email_notification(request: Request):
        """Send one test message using form values without persisting them."""
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}
            saved = EmailConfig.from_environment()

            def _submitted(name: str, fallback: str) -> str:
                if name not in data:
                    return fallback
                return str(data.get(name) or "").strip()

            raw_port = _submitted("smtp_port", str(saved.port))
            try:
                smtp_port = int(raw_port)
            except Exception:
                return JSONResponse(
                    {"ok": False, "error": "Test email failed: SMTP port must be a number."},
                    status_code=400,
                )
            to_text = _submitted("to", ", ".join(saved.to_addresses))
            recipients = tuple(
                item.strip()
                for item in to_text.replace(";", ",").split(",")
                if item.strip()
            )
            submitted_password = str(data.get("app_password") or "").strip().replace(" ", "")
            test_config = EmailConfig(
                enabled=True,
                smtp_host=_submitted("smtp_host", saved.smtp_host),
                port=smtp_port,
                security=_submitted("security", saved.security).lower(),
                username=_submitted("username", saved.username),
                app_password=submitted_password or saved.app_password,
                from_address=_submitted("from", saved.from_address),
                to_addresses=recipients,
            )
            await asyncio.to_thread(
                SMTPEmailSender().send,
                "Sensorius test notification",
                f"Sensorius email notifications are configured on {socket.gethostname()}.",
                require_enabled=False,
                config=test_config,
            )
            return JSONResponse({"ok": True, "message": "Test email sent."})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"Test email failed: {exc}"},
                status_code=400,
            )


    @router.post("/advanced/save")
    async def advanced_save(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        updates: dict[str, str] = {}
        autostart_present = "autostart_enabled" in body or "autostart_scope" in body
        autostart_enabled = bool(body.get("autostart_enabled", False))
        autostart_scope = str(body.get("autostart_scope", "user") or "user").strip().lower()
        if autostart_present:
            if "autostart_enabled" not in body or "autostart_scope" not in body:
                return JSONResponse({"error": "incomplete_autostart_settings"}, status_code=400)
            if autostart_scope not in {"user", "system"}:
                return JSONResponse({"error": "invalid_autostart_scope"}, status_code=400)
            updates["SENSORIUS_AUTOSTART_SCOPE"] = autostart_scope
            updates["SENSORIUS_AUTOSTART_ENABLED"] = _bool_text(autostart_enabled)

        debug_present = any(key in body for key in ("log_level", "file_log", "debug_modules"))
        if debug_present and not all(key in body for key in ("log_level", "file_log", "debug_modules")):
            return JSONResponse({"error": "incomplete_debug_settings"}, status_code=400)
        log_level = str(body.get("log_level", "DEBUG") or "DEBUG").strip().upper()
        if debug_present and log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            return JSONResponse({"error": "invalid_log_level"}, status_code=400)

        file_log = bool(body.get("file_log", False))
        debug_modules_in = body.get("debug_modules", [])
        if debug_present and not isinstance(debug_modules_in, list):
            return JSONResponse({"error": "invalid_debug_modules"}, status_code=400)
        clean_modules: list[str] = []
        seen: set[str] = set()
        for raw in debug_modules_in:
            m = str(raw or "").strip()
            if not m:
                continue
            if m not in _ADV_DEBUG_MODULE_CHOICES:
                continue
            if m in seen:
                continue
            seen.add(m)
            clean_modules.append(m)

        if debug_present:
            updates["SENSORIUS_LOG_LEVEL"] = log_level
            updates["SENSORIUS_FILE_LOG"] = _bool_text(file_log)
            updates["SENSORIUS_DEBUG_MODULES"] = ",".join(clean_modules)

        if "db_retention_days" in body:
            try:
                retention_days = int(body.get("db_retention_days"))
            except Exception:
                return JSONResponse({"error": "invalid_db_retention_days"}, status_code=400)
            if retention_days < _DB_RETENTION_MIN_DAYS or retention_days > _DB_RETENTION_MAX_DAYS:
                return JSONResponse({"error": "invalid_db_retention_days_range"}, status_code=400)
            updates["SENSORIUS_DB_RETENTION_DAYS"] = str(retention_days)

        if not updates:
            return JSONResponse({"error": "no_advanced_settings"}, status_code=400)

        try:
            _write_env_updates(updates)
        except Exception as ex:
            return JSONResponse({"error": f"env_write_failed: {ex}"}, status_code=500)

        ok, msg = (True, "Not changed")
        if autostart_present:
            ok, msg = _autostart_apply(autostart_enabled, autostart_scope)
        return JSONResponse({
            "status": "ok" if ok else "partial",
            "autostart_applied": bool(ok),
            "autostart_message": msg,
        })

    @router.post("/advanced/database/archive")
    async def advanced_database_archive(request: Request):
        _require_protected_access(request, require_csrf=True)
        try:
            archive_path = await asyncio.to_thread(services.data_logger.create_database_archive)
            archive_path = Path(archive_path)
        except Exception as ex:
            printDM(f"Database archive failed: {ex}", location=MODULE)
            return JSONResponse({"ok": False, "error": str(ex) or "archive_failed"}, status_code=500)

        return FileResponse(
            str(archive_path),
            media_type="application/vnd.sqlite3",
            filename=archive_path.name,
            headers={"X-Sensorius-Archive-Path": str(archive_path)},
        )

    @router.get("/scan-nodus-setup")
    async def scan_nodus_setup(ssid: str = Query(None)):
        target_ssid = (ssid or "").strip()
        factory_target_ssid = "Nodus_Setup"
        ap_password = ""
        current_ssid = ""
        current_is_nodus_setup = False
        try:
            from . import saiAddDevice
            factory_target_ssid = (getattr(saiAddDevice, "PICOW_AP_SSID", "") or "").strip() or "Nodus_Setup"
            ap_password = str(getattr(saiAddDevice, "PICOW_AP_PASSWORD", "") or "")
            if platform.system().lower() != "darwin":
                current_ssid = await asyncio.to_thread(getattr(saiAddDevice, "_get_current_ssid", lambda: ""))
            current_is_nodus_setup = saiAddDevice.is_nodus_setup_ssid(current_ssid)
        except Exception:
            pass
        found, msg, matching_ssids = await asyncio.to_thread(_scan_for_ssid, target_ssid)
        if not target_ssid and current_is_nodus_setup and current_ssid not in matching_ssids:
            matching_ssids = sorted([*matching_ssids, current_ssid])
            found = True
        if not target_ssid and matching_ssids:
            target_ssid = matching_ssids[0]
        elif not target_ssid:
            target_ssid = factory_target_ssid
        sys_name = platform.system()
        manual_join_required = False
        if sys_name.lower() == "darwin":
            manual_join_required = True
            already_connected = bool(target_ssid) and (current_ssid or "").strip() == target_ssid
            found = bool(found) or already_connected
            if already_connected:
                msg = f"macOS connected to {target_ssid}"
            elif found:
                msg = f"{target_ssid} found. Join it in macOS Wi-Fi, then click Add."
            else:
                detail = str(msg or "").strip()
                if not detail or detail == "ok":
                    msg = "Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
                elif "airport tool not found" in detail.lower():
                    msg = "Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
                else:
                    msg = f"{detail}. Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
        return JSONResponse({
            "ssid": target_ssid,
            "ssids": matching_ssids,
            "password": ap_password,
            "found": bool(found),
            "platform": sys_name,
            "current_ssid": str(current_ssid or "").strip(),
            "manual_join_required": manual_join_required,
            "message": msg,
        })

    @router.get("/sensor-ids", response_class=JSONResponse)
    async def list_sensor_ids():
        now_mono = time.monotonic()
        if route_state.sensor_ids_payload is not None and route_state.sensor_ids_until > now_mono:
            return JSONResponse(list(route_state.sensor_ids_payload))

        # helpers (re-use same patterns used elsewhere in this file)
        def _strip_local_suffix(h: str) -> str:
            return normalize_hostname_base(h)

        def _is_switch_id(name: str) -> bool:
            return (name or "").strip().lower().startswith("switch-")

        def _is_valid_sensor_id(name: str) -> bool:
            s = (name or "").strip()
            if not s:
                return False
            if _is_switch_id(s):
                return False
            return bool(re.match(r"^[A-Za-z0-9._-]+$", s))

        # 1) local sensors (from app.state or module var set by Sensorius)
        def _get_local_sensor_ids() -> list[str]:
            sm = services.sensor_map
            ids = []
            if isinstance(sm, dict):
                ids = [k for k in sm.keys() if isinstance(k, str)]
            else:
                from collections.abc import Iterable
                if isinstance(sm, Iterable):
                    for s in sm:
                        sid = getattr(s, "sensor_id", None)
                        if isinstance(sid, str) and sid.strip():
                            ids.append(sid)
            return ids

        local_ids = _get_local_sensor_ids()
        if DEBUG:
            printDM(f"[{MODULE}] #1 - local sensors {local_ids}", location=MODULE)

        # 2) discovered devices via MQTT (both sensors + switches) → filter
        discovered = []
        try:
            discovered = [d for d in (services.mqtt_ingest.get_known_devices() or []) if not _is_switch_id(d)]
            discovered = [_strip_local_suffix(d) for d in discovered]
        except Exception:
            discovered = []
        if DEBUG:
            printDM(f"[{MODULE}] #2 - mqtt sensors {discovered}", location=MODULE)

        # 3) ids that have already logged rows
        logged_ids = []
        try:
            logged_ids = services.data_logger.get_available_sensors() or []
        except Exception:
            logged_ids = []
        if DEBUG:
            printDM(f"[{MODULE}] #3 - data_logger sensors {logged_ids}", location=MODULE)

        # merge + sanitize + dedupe, then sort for stable UI
        merged = []
        seen = set()

        def _add(x: str):
            x = (x or "").strip()
            if not _is_valid_sensor_id(x):
                return
            if x not in seen:
                seen.add(x)
                merged.append(x)

        shadow_ids = _get_remote_nodus_sensor_shadow_ids()
        if DEBUG:
            printDM(f"[{MODULE}] #3b - nodus shadows {shadow_ids}", location=MODULE)

        for src in (local_ids, discovered, logged_ids, shadow_ids):
            for sid in src:
                _add(sid)
                
        if DEBUG:
            printDM(f"[{MODULE}] #4 - merged sensors {merged}", location=MODULE)

        # A directly connected Pi sensor is authoritative while its controller
        # remains in the live sensor map.  Do not hide it during the brief
        # startup window before the first reading has reached SQLite; settings
        # selectors use this endpoint and otherwise retain an empty list.
        local_id_set = {str(sid or "").strip() for sid in local_ids if str(sid or "").strip()}
        merged = sorted([
            sid for sid in merged
            if sid in local_id_set or _is_dashboard_visible_sensor(sid)
        ])
        route_state.sensor_ids_payload = list(merged)
        route_state.sensor_ids_until = time.monotonic() + _SENSOR_IDS_CACHE_TTL_SEC
        return JSONResponse(merged)

    @router.get("/sensor-directory", response_class=JSONResponse)
    async def sensor_directory():
        sensor_ids_response = await list_sensor_ids()
        try:
            sensor_ids = json.loads(sensor_ids_response.body.decode("utf-8"))
        except Exception:
            sensor_ids = []

        try:
            sensor_settings_mgr = SensorSettingsManager("sensor_settings")
        except Exception:
            sensor_settings_mgr = None

        items = []
        for sid in (sensor_ids or []):
            sid_text = str(sid or "").strip()
            if not sid_text:
                continue
            location = ""
            try:
                location = str(sensor_settings_mgr.get_setting(sid_text, "Sensor.LOCATION", "") or "").strip() if sensor_settings_mgr else ""
            except Exception:
                location = ""
            items.append({
                "id": sid_text,
                "location": location or "Unknown",
                "label": location or sid_text,
            })

        return JSONResponse(items)

    @router.get("/sensor-metrics", response_class=JSONResponse)
    async def get_metrics(sensor_id: str = Query(...), input_units: bool = False):
        """List native metric identities, optionally with unit metadata for editing."""
        def metric_response(names):
            unit_system = saiSettings(apply_live=False).get_setting("Display", "unit_system", "Imperial")
            return JSONResponse({name: input_descriptor(name, unit_system) if input_units else None for name in names})
        # Try 1: names seen in DB (distinct metrics for this sensor)
        try:
            names = services.data_logger.get_available_metrics(sensor_id) or []
            if names:
                return metric_response(names)
        except Exception:
            pass

        # Try 2: live local sensor's declared measurements
        try:
            smap = services.sensor_map
            controller = None
            if isinstance(smap, dict):
                controller = smap.get(sensor_id)
            else:
                # if it's a list/iterable, try to find by attribute
                from collections.abc import Iterable
                if isinstance(smap, Iterable):
                    for s in smap:
                        sid = getattr(s, "sensor_id", None)
                        if isinstance(sid, str) and sid.strip().lower() == sensor_id.strip().lower():
                            controller = s
                            break
            if controller is not None:
                # SensorController.measurements → list[dict] with "name" keys
                meas = getattr(controller, "measurements", None)
                if not meas and hasattr(controller, "sensor"):
                    meas = getattr(controller.sensor, "measurements", None)
                if isinstance(meas, (list, tuple)):
                    names = [m.get("name") for m in meas if isinstance(m, dict) and m.get("name")]
                    names = [n for n in names if isinstance(n, str)]
                    if names:
                        return metric_response(names)
        except Exception:
            pass

        # Fallback: empty result (no known metrics yet)
        return JSONResponse({})

    # Simple page with onboard link and JS to open modal
    # @router.get("/sensor-setup", response_class=HTMLResponse)
    # async def sensor_setup_page():
    #     lines = []

    #     lines.append("<!DOCTYPE html><head><title>Sensor Setup</title></head><body>")
    #     lines.append("<a href='#' onclick='openSetupModal()'>Onboard Sensor</a>")

    #     lines.append("""
    #     <script>
    #     function openSetupModal() {
    #         document.getElementById('setupModal').style.display = 'block';
    #     }

    #     function closeSetupModal() {
    #         document.getElementById('setupModal').style.display = 'none';
    #     }
        
    #     window.onload = function() {
    #         openSetupModal();
    #     };
    #     </script>
    #     """)

    #     """
    #     import saiAddDevice
    #     preview = saiAddDevice.begin_onboarding_preview()
    #     from saiHtml import render_setup_modal
    #     lines.append(render_setup_modal(preview))
    #     """
    #     lines.append("</body></html>")
    #     return "\n".join(lines)
    

    # Shared builders keep settings panes and statistics endpoints consistent.
    statistics = DeviceStatistics(services)
    _build_sensor_statistics_payload = statistics.sensor_payload
    _build_switch_statistics_payload = statistics.switch_payload

    # --- Edit Sensor (modal / template) ---


    @router.get("/edit-sensor", response_class=HTMLResponse)
    async def edit_sensor_page(
        request: Request,
        sensor_id: str = Query(...),
        embed: int = Query(0),
    ):
        _route_started = time.monotonic()
        from .saiSensorSettingsManager import SensorSettingsManager
        from .saiCalibration import CalibrationManager
        from .saiUtils import normalize_sensor_id, printDM, html_escape
        import json

        MODULE = "edit-sensor"

        try:
            normalized_id = normalize_sensor_id(sensor_id)

            manager = SensorSettingsManager("sensor_settings")
            try:
                settings_dict = manager.load(normalized_id)
            except FileNotFoundError:
                configured_weewx_id = str(
                    settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID)
                    or WEEWX_DEFAULT_SENSOR_ID
                ).strip() or WEEWX_DEFAULT_SENSOR_ID
                if normalized_id.lower() == configured_weewx_id.lower() or normalized_id.lower().startswith("weewx"):
                    ensure_weewx_sensor_settings(normalized_id, manager=manager)
                    settings_dict = manager.load(normalized_id)
                else:
                    ingest_for_shadow = services.mqtt_ingest
                    expected_metrics = _nodus_expected_metrics_for_sensor(normalized_id, ingest_for_shadow)
                    observed_metrics: list[str] = []
                    try:
                        observed_metrics = list(
                            await asyncio.to_thread(services.data_logger.get_available_metrics, normalized_id)
                            or []
                        )
                    except Exception:
                        observed_metrics = []
                    if not observed_metrics:
                        try:
                            latest_values = await asyncio.to_thread(services.data_logger.get_latest_values, normalized_id)
                            if isinstance(latest_values, dict):
                                observed_metrics = [str(k) for k in latest_values.keys() if str(k or "").strip()]
                        except Exception:
                            observed_metrics = []

                    known_live_sensor = bool(expected_metrics or observed_metrics)
                    if not known_live_sensor:
                        try:
                            known_ids = list(await asyncio.to_thread(services.data_logger.get_available_sensors) or [])
                            known_live_sensor = any(str(sid or "").strip().lower() == normalized_id.lower() for sid in known_ids)
                        except Exception:
                            known_live_sensor = False
                    if not known_live_sensor and ingest_for_shadow is not None:
                        try:
                            getter = getattr(ingest_for_shadow, "get_known_devices", None)
                            known_devices = list(getter() or []) if callable(getter) else []
                            known_live_sensor = any(str(sid or "").strip().lower() == normalized_id.lower() for sid in known_devices)
                        except Exception:
                            known_live_sensor = False

                    if not known_live_sensor:
                        return HTMLResponse(
                            f"<h3>No settings found for sensor '{html_escape(sensor_id)}'</h3><a href='/'>Return</a>",
                            status_code=404,
                        )

                    settings_dict = ensure_live_nodus_sensor_settings(
                        normalized_id,
                        manager=manager,
                        observed_metrics=observed_metrics,
                        expected_metrics=expected_metrics,
                        location=_nodus_live_location_for_sensor(normalized_id, ingest_for_shadow),
                        hardware=_nodus_sensor_hardware_for_sensor(normalized_id, ingest_for_shadow),
                    )
            if not settings_dict:
                return HTMLResponse(
                    f"<h3>❌ No settings found for sensor '{html_escape(sensor_id)}'</h3><a href='/'>Return</a>",
                    status_code=404,
                )
            if is_direct_local_sensor_id(normalized_id):
                try:
                    if manager.ensure_direct_local_type(normalized_id):
                        settings_dict = manager.load(normalized_id)
                except Exception as exc:
                    if DEBUG:
                        printDM(f"Direct local sensor type repair failed for {normalized_id}: {exc}", location=MODULE)

            pre_sensor_section = settings_dict.get("Sensor", {}) or {}
            pre_sensor_type = str(pre_sensor_section.get("TYPE", "") or "").strip().lower()
            pre_device_kind = str(pre_sensor_section.get("DEVICE", "") or "").strip().lower()
            pre_is_weewx = (
                pre_sensor_type == "weewx"
                or pre_device_kind == "weewx"
                or normalized_id.lower().startswith("weewx")
            )
            if pre_is_weewx:
                try:
                    ensure_weewx_sensor_settings(normalized_id, manager=manager)
                    settings_dict = manager.load(normalized_id)
                except Exception as exc:
                    if DEBUG:
                        printDM(f"WeeWX sensor metadata refresh failed for {normalized_id}: {exc}", location=MODULE)

            # --- Build metric options (from DB, fallback to Display block) ---
            gauge_config = get_gauge_config()
            available_metrics = [
                canonicalize_metric_name(metric, gauge_config)
                for metric in await asyncio.to_thread(services.data_logger.get_available_metrics, normalized_id)
            ]

            display_block = settings_dict.get("Display", {}) or {}
            current_metrics_any_case: list[str] = []
            for i in range(1, 7):
                current = (
                    display_block.get(f"METRIC_{i}")
                    or display_block.get(f"metric_{i}")
                    or ""
                )
                current = canonicalize_metric_name(current, gauge_config)
                if current and current not in current_metrics_any_case:
                    current_metrics_any_case.append(current)
            if not available_metrics and current_metrics_any_case:
                available_metrics = sorted(set(current_metrics_any_case))

            metric_options = [""]
            if available_metrics:
                for m in sorted(set(available_metrics)):
                    metric_options.append(m)

            # current metrics array aligned with indices 1..6
            current_metrics: list[str] = []
            for i in range(1, 7):
                val = (
                    display_block.get(f"METRIC_{i}")
                    or display_block.get(f"metric_{i}")
                    or ""
                )
                val = canonicalize_metric_name(val, gauge_config)
                current_metrics.append(val)

            display_style_options = ["Gauge", "Graph6hr", "Graph24hr"]
            current_metric_styles = manager.get_display_styles(normalized_id, default_style="Gauge")

            # location
            sensor_section = settings_dict.get("Sensor", {}) or {}
            sensor_type = str(sensor_section.get("TYPE", "") or "").strip().lower()
            raw_device = str(sensor_section.get("DEVICE", "") or "")
            device_kind = raw_device.strip().lower()
            is_weewx = sensor_type == "weewx" or device_kind == "weewx" or normalized_id.lower().startswith("weewx")
            if is_weewx:
                for metric in WEEWX_DISPLAY_METRICS:
                    canonical = canonicalize_metric_name(metric, gauge_config)
                    if canonical and canonical not in metric_options:
                        metric_options.append(canonical)

            location = (
                sensor_section.get("LOCATION")
                or sensor_section.get("location")
                or "Unknown"
            )

            # --- Calibration context (used by split-pane sensor modal) ---
            calib_section = (settings_dict.get("Calibration") or {}) or {}
            device_section = (calib_section.get("Device") or calib_section.get("device") or {}) or {}
            device_label = raw_device or device_kind or "Unknown"
            is_apvpd = (device_kind == "apvpd")

            def _get_float(section: dict, key: str, default: float = 0.0) -> float:
                try:
                    return float(section.get(key, default) or default)
                except Exception:
                    return default

            ambient_temp_offset = _get_float(calib_section, "APVPD_TEMP_CAL_VAL", 0.0)
            ambient_rh_offset = _get_float(calib_section, "APVPD_RH_CAL_VAL", 0.0)
            soil_ph_offset = _soil_offset_value(device_section, _SOIL_PH_CAL_KEYS, _get_float)

            device_offsets: list[dict] = []

            def _add_offset(
                key_path: str,
                label: str,
                unit: str,
                field_key: str,
                default_val: float = 0.0,
            ) -> None:
                value = _get_float(device_section, field_key, default_val)
                device_offsets.append(
                    {
                        "key": key_path,
                        "label": label,
                        "unit": unit,
                        "value": value,
                    }
                )

            if device_kind in ("co2", "scd30", "scd4x"):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")
                _add_offset("Calibration.Device.CO2_OFFSET", "CO₂", "ppm", "CO2_OFFSET")
            elif device_kind in ("aqi", "bme680", "bme688"):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")
                _add_offset("Calibration.Device.AQI_OFFSET", "AQI", "", "AQI_OFFSET")
                _add_offset("Calibration.Device.GAS_OFFSET", "Gas resistance", "kΩ", "GAS_OFFSET")
            elif device_kind in ("veml", "lux"):
                _add_offset("Calibration.Device.LUX_OFFSET", "Light Intensity", "lux", "LUX_OFFSET")
                _add_offset("Calibration.Device.PPFD_OFFSET", "Estimated PPFD", "µmol/m²/s", "PPFD_OFFSET")
            elif device_kind in ("vpd", "avpd", "bme280", "aht", "aht10", "ahtx0"):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")
            elif device_kind in ("soil",):
                device_offsets.extend(_soil_device_offsets(device_section, _get_float))
            _append_system_altitude_calibration(device_offsets, device_kind)

            ingest = services.mqtt_ingest
            nodus_firmware_version = ""
            if (
                ingest
                and sensor_type in ("picow", "pico2w", "nodus", "remote")
                and hasattr(ingest, "get_nodus_firmware_version")
            ):
                try:
                    nodus_firmware_version = str(
                        ingest.get_nodus_firmware_version(normalized_id, device_type="sensor")
                    ).strip()
                except Exception:
                    nodus_firmware_version = ""
            nodus_board_type = _display_nodus_board_type(
                normalized_id,
                device_type="sensor",
                settings_block=sensor_section,
            )
            nodus_sensor_hardware = _display_nodus_sensor_hardware(
                normalized_id,
                settings_block=sensor_section,
            )
            weewx_station_model = ""
            if is_weewx:
                for key in ("STATION_MODEL", "station_model", "MODEL", "model", "STATION_TYPE", "station_type"):
                    weewx_station_model = str(sensor_section.get(key) or "").strip()
                    if weewx_station_model:
                        break

            network_info = await _build_device_network_info(normalized_id, device_type="sensor")
            sensor_statistics = await _build_sensor_statistics_payload(normalized_id)

            cal_mgr = CalibrationManager(services.data_logger, manager)
            candidate_sensors = await asyncio.to_thread(cal_mgr.get_calibratable_sensors) or []

            # Render template
            templates = request.app.state.templates
            template = templates.get_template("modals/sensor_settings.html")
            modal_html = template.render(
                sensor_id=normalized_id,
                settings=settings_dict,
                metric_options=metric_options,
                current_metrics=current_metrics,
                display_style_options=display_style_options,
                current_metric_styles=current_metric_styles,
                location=location,
                device_kind=device_kind,
                device_label=device_label,
                is_apvpd=is_apvpd,
                is_soil=(device_kind == "soil"),
                **calibration_presentation(device_offsets, ambient_temp_offset,
                    saiSettings(apply_live=False).get_setting("Display", "unit_system", "Imperial")),
                ambient_rh_offset=ambient_rh_offset,
                nodus_firmware_version=nodus_firmware_version,
                nodus_board_type=nodus_board_type,
                nodus_sensor_hardware=nodus_sensor_hardware,
                weewx_station_model=weewx_station_model,
                soil_ph_offset=soil_ph_offset,
                device_offsets=device_offsets,
                candidate_sensors=candidate_sensors,
                default_range_hours=24,
                supports_device_calibration=not is_weewx,
                supports_system_calibration=not is_weewx,
                can_restart_device=(sensor_type in ("picow", "pico2w", "nodus", "remote", "mqtt")),
                network_info=network_info,
                **sensor_statistics,
            )

            if embed:
                # just return snippet for dashboard JS
                _ui_profile_log("edit-sensor", _route_started, embed=1, sensor_id=normalized_id, candidates=len(candidate_sensors))
                return HTMLResponse(modal_html)

            # Full-page fallback (used rarely)
            page: list[str] = []
            page.append("<!DOCTYPE html>")
            page.append("<html><head><title>Edit Sensor</title>")
            page.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
            page.append(f"<link rel='stylesheet' href='/ui_static/css/app.css?v={SAI_APP_VERSION}'>")
            page.append(f"<link rel='stylesheet' href='/ui_static/css/mobile_settings.css?v={SAI_APP_VERSION}'>")
            page.append(f"<script src='/ui_static/js/mobile_settings.js?v={SAI_APP_VERSION}'></script>")
            page.append(f"<script src='/ui_static/js/sensor_settings_modal.js?v={SAI_APP_VERSION}'></script>")
            page.append("<script src='/ui_static/js/system_calibration.js'></script>")
            page.append("</head><body>")
            page.append("<div id='modalHost'></div>")
            modal_json = json.dumps(modal_html).replace("</", "<\\/")
            page.append(f"<script>var __MODAL_HTML__ = {modal_json};</script>")
            page.append("<script>")
            page.append("  (function(){")
            page.append("    var host = document.getElementById('modalHost') || document.body;")
            page.append("    host.innerHTML = __MODAL_HTML__;")
            page.append("    var modal = document.getElementById('sensorSettingsModal');")
            page.append("    var backdrop = modal && modal.closest('.modal-backdrop');")
            page.append("    if (backdrop) backdrop.style.display = 'flex';")
            page.append("    if (modal && window.initSensorSettingsModal) window.initSensorSettingsModal(modal);")
            page.append("    if (modal && window.initSystemCalibrationModal) window.initSystemCalibrationModal(modal);")
            page.append("  })();")
            page.append("</script>")
            page.append("</body></html>")
            _ui_profile_log("edit-sensor", _route_started, embed=0, sensor_id=normalized_id, candidates=len(candidate_sensors))
            return HTMLResponse(content="\n".join(page))

        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            printDM(f"[{MODULE}] Exception: {e}\n{tb}", location=MODULE)
            return HTMLResponse(
                "<h3>Internal Error</h3><a href='/'>Return</a>",
                status_code=500,
            )


    # --- Save Local or Remote Sensor (modal) ---
    @router.post("/submit-sensor-settings")
    async def submit_sensor_settings(request: Request):

        form = await request.form()

        # ---------- helpers ----------
        def deep_merge_ordered(base: OrderedDict, update: dict | OrderedDict) -> OrderedDict:
            """
            Recursively merge 'update' into 'base' (both mapping-like).
            Preserves existing section/key order; adds new keys at the end.
            """
            for k, v in (update or {}).items():
                if isinstance(v, dict):
                    if k not in base or not isinstance(base.get(k), dict):
                        base[k] = OrderedDict()
                    # ensure nested is OrderedDict
                    if not isinstance(base[k], OrderedDict):
                        base[k] = OrderedDict(base[k])
                    deep_merge_ordered(base[k], v)
                else:
                    base[k] = v
            return base

        def guess_device_toml(device_name: str) -> str:
            dn = (device_name or "").strip().lower()
            if dn.startswith("soil") or dn in {"soil", "soil4in1", "rs485", "modbus"}:
                return "sensor_soil.toml"
            return "sensor_i2c.toml"

        def detect_sensor_type(settings_dir: Path) -> str | None:
            mgr = SensorSettingsManager(str(settings_dir.parent))
            doc = mgr.load(settings_dir.name) or {}
            sensor_blk = doc.get("Sensor", {})
            t = (sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()
            return t or None

        def resolve_hostname(sensor_id_norm: str, local_doc: dict) -> str:
            net = local_doc.get("Network", {}) if isinstance(local_doc, dict) else {}
            host = (net.get("HOSTNAME") or "").strip()
            if not host:
                host = (local_doc.get("Sensor", {}).get("HOSTNAME") or "").strip()
            if not host:
                host = sensor_id_norm
            return mdns_hostname(host)

        def _sensor_updates_for_nodus(
            existing_doc: OrderedDict,
            merged_doc: OrderedDict,
            metric_list: list[str],
            metric_style_list: list[str],
        ) -> list[tuple[str, str, Any]]:
            updates: list[tuple[str, str, Any]] = []
            existing_sensor_block = existing_doc.get("Sensor", {}) if isinstance(existing_doc, dict) else {}
            sensor_block = merged_doc.get("Sensor", {}) if isinstance(merged_doc, dict) else {}
            if isinstance(sensor_block, dict):
                for key in ("LOCATION",):
                    if key in sensor_block and not _nodus_values_match(
                        existing_sensor_block.get(key) if isinstance(existing_sensor_block, dict) else None,
                        sensor_block.get(key, ""),
                    ):
                        updates.append(("Sensor", key, sensor_block.get(key, "")))

            if metric_list:
                existing_display_block = existing_doc.get("Display", {}) if isinstance(existing_doc, dict) else {}
                for idx in range(1, 7):
                    value = metric_list[idx - 1] if idx - 1 < len(metric_list) else ""
                    key = f"METRIC_{idx}"
                    # Explicit blank metric submissions must still be pushed to
                    # Nodus so stale remote values get cleared even when the
                    # local shadow file omitted the key entirely.
                    if value == "" and not (
                        isinstance(existing_display_block, dict) and key in existing_display_block
                    ):
                        updates.append(("Display", key, value))
                        continue
                    if not _nodus_values_match(
                        existing_display_block.get(key) if isinstance(existing_display_block, dict) else None,
                        value,
                    ):
                        updates.append(("Display", key, value))

            if metric_style_list:
                existing_display_block = existing_doc.get("Display", {}) if isinstance(existing_doc, dict) else {}
                existing_style_block = existing_display_block.get("Style", {}) if isinstance(existing_display_block, dict) else {}
                merged_display_block = merged_doc.get("Display", {}) if isinstance(merged_doc, dict) else {}
                merged_style_block = merged_display_block.get("Style", {}) if isinstance(merged_display_block, dict) else {}
                for idx in range(1, 7):
                    key = f"METRIC_{idx}"
                    value = metric_style_list[idx - 1] if idx - 1 < len(metric_style_list) else ""
                    merged_value = (
                        merged_style_block.get(key) if isinstance(merged_style_block, dict) else value
                    )
                    if not _nodus_values_match(
                        existing_style_block.get(key) if isinstance(existing_style_block, dict) else None,
                        merged_value,
                    ):
                        updates.append(("Display.Style", key, merged_value))
            return updates

        async def push_updates_to_picow(base_dir: Path, sensor_id_norm: str, device_file: str,
                                        merged_doc: OrderedDict, metric_list: list[str], metric_style_list: list[str],
                                        *,
                                        previous_doc: OrderedDict | dict | None = None,
                                        lookup_device_id: str,
                                        system_mgr=None,
                                        system_root: str | None = None,
                                        sys_host_index: dict[str, str] | None = None) -> None:
            mgr = SensorSettingsManager(str(base_dir))
            live_doc = mgr.load(sensor_id_norm) or {}
            prior_doc = previous_doc if isinstance(previous_doc, dict) else live_doc
            updates = _sensor_updates_for_nodus(prior_doc, merged_doc, metric_list, metric_style_list)
            if not updates:
                return

            try:
                ok = await push_nodus_settings_batch(
                    device_id=lookup_device_id,
                    device_type="sensor",
                    setting_file_key="sensor",
                    updates=updates,
                    sensor_file_name=device_file,
                    system_mgr=system_mgr,
                    system_root=system_root,
                    sys_host_index=sys_host_index,
                )
                if not ok:
                    printDM(f"[{MODULE}] Failed to push one or more sensor updates for {sensor_id_norm}",
                            location="saiWebRoutes")
                    return
                try:
                    host = resolve_hostname(sensor_id_norm, live_doc)
                    services.mqtt_ingest.add_client(host)   # marks 'pending' and forces an expedited check
                except Exception:
                    pass
                printDM(f"[{MODULE}] Pushed sensor updates to {device_file} for Nodus {lookup_device_id}",
                        location="saiWebRoutes")
            except Exception as e:
                printDM(f"[{MODULE}] Failed to push {device_file} to Nodus ({lookup_device_id}): {e}",
                        location="saiWebRoutes")

        # ---------- validate form ----------
        sensor_id_in_form = form.get("sensor_id")
        if not sensor_id_in_form:
            return _modal_error_response(request, "Missing sensor_id", status_code=400)

        old_id = normalize_sensor_id(sensor_id_in_form)
        manager = SensorSettingsManager("sensor_settings")
        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)

        location_value = (form.get("location", "") or "").strip()
        new_id = old_id

        # ---------- Load full current doc & build merged update ----------
        existing_doc = manager.load(old_id) or OrderedDict()

        # Ensure top-level section order; if missing, seed them
        if not isinstance(existing_doc, OrderedDict):
            existing_doc = OrderedDict(existing_doc)
        previous_doc = copy.deepcopy(existing_doc)
        for section in ("Sensor", "Calibration", "Display"):
            existing_doc.setdefault(section, OrderedDict())

        # Prepare updates for [Sensor]
        sensor_updates = {
            "Sensor": OrderedDict({
                "LOCATION": location_value or existing_doc["Sensor"].get("LOCATION", "Unknown"),
            })
        }

        # Only modify [Display] if at least one metric_* is present in the form
        metric_keys_present = any(f"metric_{i}" in form for i in range(1, 7))
        style_keys_present = any(f"display_style_{i}" in form for i in range(1, 7))
        display_updates: dict = {}
        metric_list: list[str] = []
        metric_style_list: list[str] = []
        if metric_keys_present:
            gauge_config = get_gauge_config()
            metric_list = [
                canonicalize_metric_name((form.get(f"metric_{i}", "") or "").strip(), gauge_config)
                for i in range(1, 7)
            ]
            display_updates = {"Display": {f"METRIC_{i}": metric_list[i-1] for i in range(1, 7)}}
        if style_keys_present:
            allowed_styles = {"Gauge", "Graph6hr", "Graph24hr"}
            metric_style_list = []
            for i in range(1, 7):
                raw_style = str(form.get(f"display_style_{i}", "") or "").strip()
                style_value = raw_style if raw_style in allowed_styles else "Gauge"
                metric_style_list.append(style_value)
            display_updates.setdefault("Display", {})
            display_updates["Display"]["Style"] = {
                f"METRIC_{i}": metric_style_list[i - 1] for i in range(1, 7)
            }
        # Deep-merge the changes into the existing doc
        merged_doc = deep_merge_ordered(OrderedDict(existing_doc), sensor_updates)
        if display_updates:
            merged_doc = deep_merge_ordered(merged_doc, display_updates)

        # ---------- Persist FULL merged doc ----------
        # Important: write the entire merged doc so we never truncate the file.
        try:
            manager.save(old_id, merged_doc)   # assumes save can accept a full doc
        except TypeError:
            # Fallback if manager.save expects only partials: try 'save_full' if provided.
            if hasattr(manager, "save_full"):
                manager.save_full(old_id, merged_doc)
            else:
                # Last resort: re-save section-by-section in a deterministic order
                # (still preserves everything in merged_doc)
                for section_name, section_map in merged_doc.items():
                    manager.save(old_id, OrderedDict([(section_name, section_map)]))
        _invalidate_dashboard_caches()
        route_state.sensor_locations.pop(old_id, None)

        base_dir = Path(getattr(manager, "base_dir", "sensor_settings"))
        old_dir = base_dir / old_id
        new_dir = base_dir / new_id

        # ---------- If Pico2 W-backed, push only the relevant blocks ----------
        live_dir = new_dir if new_id != old_id else old_dir
        try:
            sensor_type = detect_sensor_type(live_dir)  # 'picow' / 'pico2w' / 'pi' / None
        except Exception:
            sensor_type = None

        if sensor_type in ("picow", "pico2w", "nodus"):
            sensor_device = str((merged_doc.get("Sensor", {}) or {}).get("DEVICE", "") or "")
            nodus_block = (
                merged_doc.get("Nodus")
                if isinstance(merged_doc.get("Nodus"), dict)
                else {}
            )
            device_toml = str(nodus_block.get("CONFIG_FILE") or "").strip()
            if not device_toml:
                ingest_for_target = services.mqtt_ingest
                resolver = getattr(ingest_for_target, "resolve_nodus_sensor_target", None)
                if callable(resolver):
                    target_info = resolver(old_id) or {}
                    device_toml = str(target_info.get("config_file") or "").strip()
            if not device_toml:
                device_toml = guess_device_toml(sensor_device)
            await push_updates_to_picow(
                base_dir,
                new_id,
                device_toml,
                merged_doc,
                metric_list,
                metric_style_list,
                previous_doc=previous_doc,
                lookup_device_id=old_id,
                system_mgr=system_mgr,
                system_root=system_root,
                sys_host_index=sys_host_index,
            )

        if _wants_modal_json(request):
            return JSONResponse({"ok": True, "message": "Sensor settings saved.", "sensor_id": new_id})
        return RedirectResponse(url="/", status_code=303)

    @router.post("/sensor-settings/restart-device")
    async def restart_sensor_device(request: Request):
        form = await request.form()
        sensor_id = normalize_sensor_id(form.get("sensor_id"))
        if not sensor_id:
            return JSONResponse({"ok": False, "error": "Missing sensor_id"}, status_code=400)

        manager = SensorSettingsManager("sensor_settings")
        doc = manager.load(sensor_id) or {}
        sensor_type = str(((doc.get("Sensor", {}) or {}).get("TYPE", "") or "")).strip().lower()
        if sensor_type not in {"picow", "pico2w", "nodus", "remote", "mqtt"}:
            return JSONResponse({"ok": False, "error": "Restart is only available for Nodus sensors."}, status_code=400)

        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)
        target_device = str(
            _read_hostname_from_system_settings(
                sensor_id,
                system_mgr,
                system_root,
                device_type="sensor",
                sys_host_index=sys_host_index,
            )
            or sensor_id
        ).strip()
        printDM(
            f"[restart-request] device_type=sensor device_id={sensor_id} target={target_device} via=webui",
            location=MODULE,
            level="info",
        )
        ok, message = await _request_nodus_device_restart(
            target_device=target_device,
            device_id=sensor_id,
            device_type="sensor",
            restart_mode="soft",
        )
        printDM(
            f"[restart-result] device_type=sensor device_id={sensor_id} target={target_device} ok={ok} message={message}",
            location=MODULE,
            level="info" if ok else "warning",
        )
        status_code = 200 if ok else 502
        payload_key = "message" if ok else "error"
        return JSONResponse(
            {"ok": ok, payload_key: message, "sensor_id": sensor_id, "target_device": target_device},
            status_code=status_code,
        )


    # Bind dashboard notifications before registering the route groups.
    _switch_broadcast = register_switch_events(router, app=app, services=services, debug=DEBUG)


    # ------ system utilities -------
    @router.get("/clear-data", response_class=HTMLResponse)
    async def clear_data_page(request: Request):
        _require_protected_access(request)
        return RedirectResponse(url="/new-database", status_code=303)

    @router.post("/clear-data", response_class=HTMLResponse)
    async def clear_data_post(request: Request, confirm: bool = Form(False)):
        _require_protected_access(request, require_csrf=True)
        return RedirectResponse(url="/new-database", status_code=303)

    @router.get("/new-database", response_class=HTMLResponse)
    async def new_database_page(request: Request):
        _require_protected_access(request)
        return HTMLResponse(
            "<html><body><h3>Confirm New Database</h3>"
            "<p>This will archive the current SQLite database, delete the active database files, and create a new empty database.</p>"
            "<p>This is an intentionally drastic recovery action.</p>"
            "<form method='post' action='/new-database'>"
            "<input type='hidden' name='confirm' value='true'>"
            "<button type='submit'>Yes, create new database</button>"
            "</form>"
            "<a href='/'>Cancel</a>"
            "</body></html>"
        )

    @router.post("/new-database", response_class=HTMLResponse)
    async def new_database_post(request: Request, confirm: bool = Form(False)):
        _require_protected_access(request, require_csrf=True)
        if not confirm:
            return HTMLResponse(
                "<html><body><h3>Missing confirmation.</h3><a href='/'>Return</a></body></html>",
                status_code=400,
            )
        try:
            archive_path = await asyncio.to_thread(services.data_logger.archive_and_create_new_database)
        except Exception as exc:
            printDM(f"[new-database] failed: {exc}", location=MODULE)
            return HTMLResponse(
                "<html><body><h3>New database failed.</h3>"
                f"<p>{str(exc)}</p>"
                "<a href='/'>Return to Dashboard</a></body></html>",
                status_code=500,
            )
        return HTMLResponse(
            "<html><body><h3>New database created.</h3>"
            f"<p>Archived previous database at {archive_path}</p>"
            "<a href='/'>Return to Dashboard</a></body></html>"
        )


    # WS endpoint
    @app.websocket("/ws/live")
    async def live_ws(ws: WebSocket):
        await fastStats.add(ws)
        try:
            while True:
                await ws.receive_text()  # or ping/pong; ignore content
        except:
            pass
        finally:
            await fastStats.remove(ws)


    from .saiBiodynamicCalendarApp import register_biodynamic_calendar_routes
    from .saiWeatherForecastApp import register_weather_forecast_app_routes
    from .saiStats import create_stats_router
    register_biodynamic_calendar_routes(
        router,
        app=app,
        settings=settings,
        data_logger=data_logger,
    )
    register_weather_forecast_app_routes(
        router,
        app=app,
        settings=settings,
        data_logger=data_logger,
        sensor_settings_manager=SensorSettingsManager("sensor_settings"),
    )
    register_graph_routes(
        router, services=services, settings=settings,
        sqlite_connect=_sqlite_connect_with_recovery,
        settings_factory=lambda *args, **kwargs: saiSettings(*args, **kwargs),
    )
    calibration_helpers = register_calibration_routes(
        router, services=services, route_state=route_state, settings=settings,
        settings_factory=lambda *args, **kwargs: saiSettings(*args, **kwargs),
        sensor_settings_factory=lambda *args, **kwargs: SensorSettingsManager(*args, **kwargs),
        get_host_lock=_get_host_lock,
        ack_timeout=_NODUS_CALIBRATION_ACK_TIMEOUT_SEC,
        result_timeout=_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC,
        debug=DEBUG,
    )
    _soil_offset_value = calibration_helpers.soil_offset_value
    _soil_device_offsets = calibration_helpers.soil_device_offsets
    _append_system_altitude_calibration = calibration_helpers.append_system_altitude_calibration
    register_switch_routes(
        router, services=services, route_state=route_state, settings=settings,
        settings_factory=lambda *args, **kwargs: saiSettings(*args, **kwargs),
        switch_settings_factory=lambda *args, **kwargs: SwitchSettingsManager(*args, **kwargs),
        get_system_settings_manager=lambda: globals().get("SystemSettingsManager", None),
        status_cache_ttl=_SWITCH_STATUS_CACHE_TTL_SEC, debug=DEBUG,
        dependencies=SwitchRouteDependencies(
            build_device_network_info=_build_device_network_info,
            build_switch_statistics_payload=_build_switch_statistics_payload,
            build_system_hostname_index=_build_system_hostname_index,
            display_nodus_board_type=_display_nodus_board_type,
            env_map_with_defaults=_env_map_with_defaults,
            invalidate_dashboard_caches=_invalidate_dashboard_caches,
            modal_error_response=_modal_error_response,
            nodus_values_match=_nodus_values_match,
            read_hostname_from_system_settings=_read_hostname_from_system_settings,
            request_nodus_device_restart=_request_nodus_device_restart,
            require_protected_access=_require_protected_access,
            resolve_channel_id_from_label=_resolve_channel_id_from_label,
            resolve_system_settings_root=_resolve_system_settings_root,
            switch_broadcast=_switch_broadcast,
            ui_profile_log=_ui_profile_log,
            wants_modal_json=_wants_modal_json,
            push_nodus_settings_batch=push_nodus_settings_batch,
            switch_channel_id_from_identity_row=_switch_channel_id_from_identity_row,
        ),
    )
    register_integration_routes(
        router, services=services, settings_factory=saiSettings,
        ecowitt_factory=EcowittGatewayIngest,
        ensure_weewx_settings=lambda sensor_id: ensure_weewx_sensor_settings(
            sensor_id, manager=SensorSettingsManager("sensor_settings")),
        evaluate_weewx_status=_weewx_measure_status_from_latest,
        dashboard_broadcast=_switch_broadcast,
        invalidate_dashboard_caches=_invalidate_dashboard_caches,
        debug=DEBUG,
    )
    register_statistics_diagnostics_routes(
        router, services=services, statistics=statistics, net_mgr=net_mgr,
        require_protected_access=_require_protected_access,
        advanced=AdvancedStatusConfiguration(
            read_environment=_env_map_with_defaults,
            autostart_is_enabled=_autostart_is_enabled,
            debug_module_choices=tuple(_ADV_DEBUG_MODULE_CHOICES),
            default_log_file=DEFAULT_LOG_FILE,
        ),
        switch_settings_manager=SwitchSettingsManager,
    )
    app.include_router(create_stats_router(settings, gc_mgr, data_logger=data_logger, statter=statter))
    app.include_router(router)
    return router
