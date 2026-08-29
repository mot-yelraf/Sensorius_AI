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
import socket
import asyncio
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
from .saiDisplayUnits import (
    DISPLAY_UNIT_SYSTEMS,
    apply_display_units_to_gauge_config,
    normalize_display_unit_system,
)
from .saiEcowitt import EcowittError, EcowittGatewayIngest
from .sensor_modules.station_ecowitt import DEFAULT_POLL_INTERVAL_SEC as ECOWITT_DEFAULT_POLL_INTERVAL_SEC
from .saiRuntimePaths import resolve_runtime_base_dir
from .saiOnboardingStore import OnboardingSessionStore, OnboardingStates
from .saiOnboardingToken import OnboardingTokenManager
from .saiDataLogger import saiDataLogger
try:
    from .saiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None
from .saiStats import saiStats
from .saiHtml import (
    DASHBOARD_BACKGROUND_THEMES,
    DASHBOARD_METRIC_SETS,
    canonicalize_metric_name,
    extend_gauge_config_for_metrics,
    get_gauge_config,
    normalize_dashboard_background_theme,
    normalize_dashboard_metric_set,
    render_dashboard,
)
from .sensor_modules.station_weewx import (
    DEFAULT_DB_PATH as WEEWX_DEFAULT_DB_PATH,
    DEFAULT_MQTT_TOPIC as WEEWX_DEFAULT_MQTT_TOPIC,
    DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID,
    DEFAULT_UPDATE_PERIOD_SEC as WEEWX_DEFAULT_UPDATE_PERIOD_SEC,
    WEEWX_DISPLAY_METRICS,
    WEEWX_DISPLAY_STYLES,
    apply_weewx_station_metadata,
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


GRAPH_MAX_POINTS_PER_SERIES = 900


def _downsample_graph_points(timestamps, values, max_points: int = GRAPH_MAX_POINTS_PER_SERIES):
    """Reduce a graph series while retaining its endpoints and bucket extrema.

    Browser dashboards need a bounded number of points because Chart.js and its
    parsed JSON otherwise retain every database reading for the selected range.
    Min/max buckets preserve short spikes better than simple interval sampling.
    """
    paired_count = min(len(timestamps or ()), len(values or ()))
    limit = max(3, int(max_points or GRAPH_MAX_POINTS_PER_SERIES))
    if paired_count <= limit:
        return list((timestamps or ())[:paired_count]), list((values or ())[:paired_count])

    interior_count = paired_count - 2
    bucket_count = max(1, (limit - 2) // 2)
    bucket_width = max(1, math.ceil(interior_count / bucket_count))
    selected_indexes = [0]
    for start_idx in range(1, paired_count - 1, bucket_width):
        stop_idx = min(paired_count - 1, start_idx + bucket_width)
        numeric_points: list[tuple[int, float]] = []
        for idx in range(start_idx, stop_idx):
            try:
                numeric = float(values[idx])
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                numeric_points.append((idx, numeric))
        if numeric_points:
            min_idx = min(numeric_points, key=lambda item: item[1])[0]
            max_idx = max(numeric_points, key=lambda item: item[1])[0]
            selected_indexes.extend(sorted({min_idx, max_idx}))
        else:
            selected_indexes.append(start_idx)
    selected_indexes.append(paired_count - 1)

    selected_indexes = sorted(set(selected_indexes))
    if len(selected_indexes) > limit:
        stride = (len(selected_indexes) - 1) / float(limit - 1)
        selected_indexes = sorted({selected_indexes[round(i * stride)] for i in range(limit)})
        selected_indexes[0] = 0
        selected_indexes[-1] = paired_count - 1
    return [timestamps[idx] for idx in selected_indexes], [values[idx] for idx in selected_indexes]

MODULE = "saiWebRoutes"
DEBUG = debug_enabled(MODULE)
# Bound by register_routes so background runtime modules can reach app.state
# without importing saiWebServer and creating an import cycle.
app = None
data_logger = saiDataLogger()
statter = saiStats()
_ALL_IANA_TIMEZONES: tuple[str, ...] = tuple(sorted(available_timezones()))
_DB_RETENTION_MIN_DAYS = 30
_DB_RETENTION_MAX_DAYS = 365
_DB_RETENTION_DEFAULT_DAYS = 90


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

def _clamp_db_retention_days(raw: object, default: int = _DB_RETENTION_DEFAULT_DAYS) -> int:
    try:
        days = int(str(raw if raw is not None else default))
    except Exception:
        days = int(default)
    return max(_DB_RETENTION_MIN_DAYS, min(_DB_RETENTION_MAX_DAYS, days))

def _settings_base_path(base_dir: str | Path) -> Path:
    return resolve_runtime_base_dir(base_dir)

def _format_stats_duration(seconds: float | int | None) -> str:
    """Format a compact uptime duration for statistics panes."""
    if seconds is None:
        return "No offline events"
    try:
        total = max(0, int(float(seconds)))
    except Exception:
        return "No offline events"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

def _format_stats_age(epoch: float | int | None, *, empty: str = "No events") -> str:
    """Format a compact age from an epoch timestamp."""
    if epoch is None:
        return empty
    try:
        return f"{_format_stats_duration(time.time() - float(epoch))} ago"
    except Exception:
        return empty

def _format_stats_timestamp(epoch: float | int | None, *, empty: str = "No events") -> str:
    """Format a local timestamp for statistics panes."""
    if epoch is None:
        return empty
    try:
        dt = datetime.fromtimestamp(float(epoch)).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return empty

def _format_switch_state_label(state: object) -> str:
    """Normalize a persisted switch state into a short UI label."""
    if isinstance(state, bool):
        return "On" if state else "Off"
    text = str(state if state is not None else "").strip().lower()
    if text in {"1", "true", "on", "yes"}:
        return "On"
    if text in {"0", "false", "off", "no"}:
        return "Off"
    return str(state or "").strip() or "Unknown"

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
_calibration_progress_cache: dict[str, dict[str, object]] = {}
_switch_status_cache_payload: dict[str, dict] | None = None
_switch_status_cache_until: float = 0.0
_sensor_ids_cache_payload: list[str] | None = None
_sensor_ids_cache_until: float = 0.0
_SENSOR_IDS_CACHE_TTL_SEC = 10.0
_dynamic_switch_monitor_tasks: dict[str, asyncio.Task] = {}
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


_cdp_debug_last_log: float = 0.0
_CDP_DEBUG_MIN_INTERVAL_SEC: float = 30.0
_DASHBOARD_JSON_CACHE_TTL_SEC: float = 2.0
_DASHBOARD_JSON_CACHE: dict[tuple[str, int], tuple[float, dict[str, object]]] = {}
_DASHBOARD_HTML_CACHE_MAX_ENTRIES: int = 2
_DASHBOARD_HTML_CACHE: OrderedDict[str, str] = OrderedDict()
_DASHBOARD_INVENTORY_CACHE_TTL_SEC: float = 2.0
_DASHBOARD_INVENTORY_CACHE: tuple[float, dict[str, object]] | None = None
_DASHBOARD_DISPLAY_SETTINGS_CACHE_TTL_SEC: float = 2.0
_DASHBOARD_DISPLAY_SETTINGS_CACHE: tuple[float, dict[str, object]] | None = None
_BIODYNAMIC_PAYLOAD_CACHE_TTL_SEC: float = 60.0
_NODUS_CONFIG_ACK_TIMEOUT_SEC: float = 5.0
_NODUS_CONFIG_RESULT_TIMEOUT_SEC: float = 20.0
_NODUS_CALIBRATION_ACK_TIMEOUT_SEC: float = 8.0
_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC: float = 20.0
_BIODYNAMIC_PAYLOAD_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_ASTRO_PAYLOAD_CACHE_TTL_SEC: float = 60.0
_ASTRO_PAYLOAD_CACHE: tuple[float, dict[str, object]] | None = None
_DASHBOARD_EXTRAS_FAST_WAIT_SEC: float = 0.05
_SENSOR_LOCATION_CACHE_TTL_SEC: float = 5.0
_SENSOR_LOCATION_CACHE: dict[str, tuple[float, str]] = {}


def _dashboard_json_safe(value):
    """Return a JSONResponse-safe dashboard payload fragment."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _dashboard_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dashboard_json_safe(v) for v in value]
    return value


def _is_unknown_location_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "unknown", "n/a", "na", "none", "-"}

async def register_routes(app, settings, net_mgr, gc_mgr, mqtt_ingest):
    """Register Sensorius UI, API, integration, and diagnostics routes."""
    # Several background services publish dashboard events through
    # saiWebRoutes.app.state.switch_broadcast. Keep that compatibility binding
    # pointed at the actual FastAPI instance created by saiWebServer.
    globals()["app"] = app
    if not str(getattr(app.state, "ui_runtime_instance_id", "") or "").strip():
        app.state.ui_runtime_instance_id = uuid4().hex
    router = APIRouter()
    theme_manager = getattr(app.state, "theme_manager", None)
    if not isinstance(theme_manager, ThemeManager):
        theme_manager = ThemeManager(resolve_runtime_base_dir(getattr(saiSettings, "DEFAULT_BASE_DIR", "system_settings")).parent)
        app.state.theme_manager = theme_manager
    _BIODYNAMIC_PAYLOAD_CACHE.clear()
    _DASHBOARD_HTML_CACHE.clear()
    biodynamic_payload_tasks: dict[str, asyncio.Task] = {}
    biodynamic_payload_cache_lock = threading.Lock()
    biodynamic_payload_cache_generation = 0
    biodynamic_summary_tasks: dict[str, asyncio.Task] = {}
    astro_payload_tasks: dict[str, asyncio.Task] = {}
    astro_payload_cache_lock = threading.Lock()
    astro_payload_cache_generation = 0
    main_loop = asyncio.get_running_loop()
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
        global _DASHBOARD_INVENTORY_CACHE, _DASHBOARD_DISPLAY_SETTINGS_CACHE, _ASTRO_PAYLOAD_CACHE
        global _sensor_ids_cache_payload, _sensor_ids_cache_until
        _DASHBOARD_JSON_CACHE.clear()
        _DASHBOARD_HTML_CACHE.clear()
        _DASHBOARD_INVENTORY_CACHE = None
        _DASHBOARD_DISPLAY_SETTINGS_CACHE = None
        with astro_payload_cache_lock:
            astro_payload_cache_generation += 1
            _ASTRO_PAYLOAD_CACHE = None
            astro_payload_tasks.clear()
        _sensor_ids_cache_payload = None
        _sensor_ids_cache_until = 0.0
        with biodynamic_payload_cache_lock:
            biodynamic_payload_cache_generation += 1
            _BIODYNAMIC_PAYLOAD_CACHE.clear()
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
        global _DASHBOARD_DISPLAY_SETTINGS_CACHE
        now_mono = time.monotonic()
        if _DASHBOARD_DISPLAY_SETTINGS_CACHE and _DASHBOARD_DISPLAY_SETTINGS_CACHE[0] > now_mono:
            return dict(_DASHBOARD_DISPLAY_SETTINGS_CACHE[1])
        fresh_settings = saiSettings(apply_live=False)
        payload = {
            "gauge_size": fresh_settings.get_setting("Display", "gauge_size") or "Small",
            "display_style": fresh_settings.get_setting("Display", "display_style") or "Gauge",
            "unit_system": normalize_display_unit_system(
                fresh_settings.get_setting("Display", "unit_system", "Imperial")
            ),
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
        _DASHBOARD_DISPLAY_SETTINGS_CACHE = (
            now_mono + _DASHBOARD_DISPLAY_SETTINGS_CACHE_TTL_SEC,
            dict(payload),
        )
        return payload

    def _get_cached_biodynamic_payload(anchor: date, generation: int | None = None) -> dict[str, object]:
        cache_key = anchor.isoformat()
        now_mono = time.monotonic()
        with biodynamic_payload_cache_lock:
            cached = _BIODYNAMIC_PAYLOAD_CACHE.get(cache_key)
            if cached and cached[0] > now_mono:
                return dict(cached[1])
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
                _BIODYNAMIC_PAYLOAD_CACHE[cache_key] = (
                    time.monotonic() + _BIODYNAMIC_PAYLOAD_CACHE_TTL_SEC,
                    dict(payload),
                )
        return dict(payload)

    def _get_stale_biodynamic_payload(anchor: date) -> dict[str, object] | None:
        cache_key = anchor.isoformat()
        with biodynamic_payload_cache_lock:
            cached = _BIODYNAMIC_PAYLOAD_CACHE.get(cache_key)
            if not cached:
                return None
            return dict(cached[1])

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
            cached = _BIODYNAMIC_PAYLOAD_CACHE.get(cache_key)
            if cached and cached[0] > now_mono:
                return dict(cached[1])
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
                return dict(await asyncio.wait_for(asyncio.shield(task), timeout=float(cold_wait_sec)))
            except asyncio.TimeoutError:
                return _warming_biodynamic_payload(anchor)
            except Exception:
                return _warming_biodynamic_payload(anchor)
        return dict(await task)

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
        global _ASTRO_PAYLOAD_CACHE
        now_mono = time.monotonic()
        with astro_payload_cache_lock:
            cached = _ASTRO_PAYLOAD_CACHE
            if cached and cached[0] > now_mono:
                return dict(cached[1])
        payload = _build_astro_payload()
        with astro_payload_cache_lock:
            if generation is None or generation == astro_payload_cache_generation:
                _ASTRO_PAYLOAD_CACHE = (
                    time.monotonic() + _ASTRO_PAYLOAD_CACHE_TTL_SEC,
                    dict(payload),
                )
        return dict(payload)

    def _get_stale_astro_payload() -> dict[str, object] | None:
        with astro_payload_cache_lock:
            cached = _ASTRO_PAYLOAD_CACHE
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
            cached = _ASTRO_PAYLOAD_CACHE
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
        ts = data_logger.get_latest_timestamp(sid)
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
            timestamps = data_logger.get_latest_timestamps(clean_ids)
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
        ing = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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
        sm = getattr(app.state, "sensor_map", None)
        if sm is None:
            from . import saiWebRoutes as routes
            sm = getattr(routes, "sensor_map", None)
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
        sensors_from_logger = data_logger.get_available_sensors()
        mqtt_discovered = mqtt_ingest.get_known_devices() if mqtt_ingest else []
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
            if not mqtt_ingest:
                raise Exception("mqtt_ingest missing")
            norm_label = (label or "").strip().lower()
            hit = (mqtt_ingest.nodus_label_to_channel or {}).get((str(switch_id), norm_label))
            if hit:
                return hit
        except Exception:
            pass

        # Fallback: use DB switch_ids table (label -> channel_id derived from switch_key)
        try:
            if not data_logger:
                return None
            target_sid = (switch_id or "").strip().lower()
            target_label = (label or "").strip().lower()
            for row in (data_logger.get_switch_identities() or []):
                sid = str(row.get("switch_id", "")).strip().lower()
                lab = str(row.get("label", "")).strip().lower()
                if sid == target_sid and lab == target_label:
                    ch_id = _switch_channel_id_from_identity_row(row)
                    if ch_id:
                        return ch_id
        except Exception:
            return None

        return None

    _GRAPH_SETUPS_SECTION = "GraphModal"
    _GRAPH_SETUPS_KEY = "SAVED_SETUPS_JSON"
    _GRAPH_LAST_USED_KEY = "LAST_SETUP_NAME"

    def _normalize_graph_setup_name(raw) -> str:
        name = str(raw or "").strip()
        name = re.sub(r"\s+", " ", name)
        return name[:80]

    def _normalize_astral_graph_mode(raw) -> str:
        value = str(raw or "none").strip().lower()
        value = re.sub(r"[\s-]+", "_", value).replace("&", "_")
        if value == "sun":
            return "sun"
        if value == "moon":
            return "moon"
        if value in {"sun_moon", "sun__moon", "sunmoon", "both"}:
            return "sun_moon"
        return "none"

    def _normalize_graph_setup_config(raw) -> dict[str, object]:
        cfg = raw if isinstance(raw, dict) else {}
        out: dict[str, object] = {}
        text_keys = (
            "sensor1_select",
            "sensor2_select",
            "sensor3_select",
            "metric1_select",
            "metric2_select",
            "metric3_select",
            "range",
            "start_time",
            "end_time",
            "switch_select",
        )
        for key in text_keys:
            out[key] = str(cfg.get(key, "") or "").strip()
        out["astral_select"] = _normalize_astral_graph_mode(cfg.get("astral_select", "none"))
        channels_raw = cfg.get("channels", [])
        channels: list[str] = []
        if isinstance(channels_raw, list):
            for item in channels_raw:
                text = str(item or "").strip()
                if text:
                    channels.append(text)
        out["channels"] = channels
        return out

    def _load_graph_setups_state() -> tuple[dict[str, dict[str, object]], str]:
        raw = settings.get_setting(
            _GRAPH_SETUPS_SECTION,
            _GRAPH_SETUPS_KEY,
            "{}",
            reload_if_changed=True,
        )
        last_used = str(
            settings.get_setting(
                _GRAPH_SETUPS_SECTION,
                _GRAPH_LAST_USED_KEY,
                "",
                reload_if_changed=True,
            )
            or ""
        ).strip()

        data: dict[str, dict[str, object]] = {}
        try:
            parsed = json.loads(str(raw or "{}"))
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    name = _normalize_graph_setup_name(k)
                    if not name:
                        continue
                    data[name] = _normalize_graph_setup_config(v)
        except Exception:
            data = {}

        if last_used and last_used not in data:
            last_used = ""
        return data, last_used

    def _save_graph_setups_state(data: dict[str, dict[str, object]], last_used: str):
        stable = OrderedDict()
        for name in sorted(data.keys(), key=lambda x: x.lower()):
            stable[name] = _normalize_graph_setup_config(data[name])
        settings.set_in_memory(
            _GRAPH_SETUPS_SECTION,
            _GRAPH_SETUPS_KEY,
            json.dumps(stable, separators=(",", ":")),
        )
        settings.set_in_memory(
            _GRAPH_SETUPS_SECTION,
            _GRAPH_LAST_USED_KEY,
            (last_used or "").strip(),
        )
        settings.save_settings()

    def _graph_setups_payload(data: dict[str, dict[str, object]], last_used: str) -> dict[str, object]:
        items = [{"name": name, "config": cfg} for name, cfg in sorted(data.items(), key=lambda kv: kv[0].lower())]
        return {"items": items, "last_used": (last_used or "")}

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

    def _is_true_text(v: str | None, default: bool = False) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

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
        global _cdp_debug_last_log
        dashboard_cache_key = str(sensor_id or "All")
        if dashboard_return and not json_only:
            cached_dashboard = _DASHBOARD_HTML_CACHE.get(dashboard_cache_key)
            if cached_dashboard is not None:
                _DASHBOARD_HTML_CACHE.move_to_end(dashboard_cache_key)
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
                sm = getattr(app.state, "sensor_map", None)
                if sm is None:
                    from . import saiWebRoutes as routes
                    sm = getattr(routes, "sensor_map", None)
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
                sc = getattr(app.state, "switch_controllers", None)
                if sc is None:
                    from . import saiWebRoutes as routes
                    sc = getattr(routes, "switch_controllers", None)
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
                cached_loc = _SENSOR_LOCATION_CACHE.get(sid_clean)
                if cached_loc and cached_loc[0] > now_mono:
                    return cached_loc[1]
                previous_loc = cached_loc[1] if cached_loc else ""

                try:
                    loc = sensor_settings_mgr.get_setting(sid, "Sensor.LOCATION", None) if sensor_settings_mgr else None
                    if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                        resolved = loc.strip()
                        _SENSOR_LOCATION_CACHE[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
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
                            _SENSOR_LOCATION_CACHE[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
                            return resolved
                elif isinstance(sm, Iterable):
                    for s in sm:
                        sid_attr = getattr(s, "sensor_id", None)
                        if isinstance(sid_attr, str) and sid_attr.lower() == (sid or "").lower():
                            loc = getattr(s, "location", None)
                            if isinstance(loc, str) and loc.strip() and not _is_unknown_location_value(loc):
                                resolved = loc.strip()
                                _SENSOR_LOCATION_CACHE[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, resolved)
                                return resolved

                if previous_loc and not _is_unknown_location_value(previous_loc):
                    _SENSOR_LOCATION_CACHE[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, previous_loc)
                    return previous_loc

                _SENSOR_LOCATION_CACHE[sid_clean] = (now_mono + _SENSOR_LOCATION_CACHE_TTL_SEC, "Unknown")
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
                    topic_map = getattr(mqtt_ingest, "nodus_switch_topic_map", {}) or {}
                    for topic, meta in topic_map.items():
                        if str((meta or {}).get("switch_id", "") or "").strip().lower() != sw.lower():
                            continue
                        loc = mqtt_ingest.device_location.get(topic)
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
                    sc = getattr(app.state, "switch_controllers", None)
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
                sc = getattr(app.state, "switch_controllers", None)
                if sc is None:
                    from . import saiWebRoutes as routes
                    sc = getattr(routes, "switch_controllers", None)
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

            global _DASHBOARD_INVENTORY_CACHE
            inventory_cached = _DASHBOARD_INVENTORY_CACHE
            now_mono = time.monotonic()
            if inventory_cached and inventory_cached[0] > now_mono:
                inventory_payload = dict(inventory_cached[1])
                local_ids = list(inventory_payload.get("local_ids", []) or [])
                available = list(inventory_payload.get("available", []) or [])
                available_switches = list(inventory_payload.get("available_switches", []) or [])
                renderable_switch_controllers = list(inventory_payload.get("renderable_switch_controllers", []) or [])
                sensor_locations_map = dict(inventory_payload.get("sensor_locations_map", {}) or {})
            else:
                sensors_from_logger = await asyncio.to_thread(data_logger.get_available_sensors)
                mqtt_discovered = mqtt_ingest.get_known_devices()
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
                        switch_ids_discovered = mqtt_ingest.get_known_switch_devices() or []
                        nodus_topic_map = getattr(mqtt_ingest, "nodus_switch_topic_map", {}) or {}
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
                        switch_identity_rows = await asyncio.to_thread(data_logger.get_switch_identities)
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
                    remote_cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
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
                _DASHBOARD_INVENTORY_CACHE = (
                    now_mono + _DASHBOARD_INVENTORY_CACHE_TTL_SEC,
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

            available = _order_sensor_ids_by_metric_position(available)

            if DEBUG:
                now_mono = time.monotonic()
                if (now_mono - _cdp_debug_last_log) >= _CDP_DEBUG_MIN_INTERVAL_SEC:
                    _cdp_debug_last_log = now_mono
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
                    data_logger.get_latest_values_and_timestamps,
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
                    values = await asyncio.to_thread(data_logger.get_latest_values, sid_text)
                    values = values or {}
                except Exception as exc:
                    printDM(
                        f"[dashboard] latest values failed for {sid_text}: {exc}",
                        location=MODULE,
                    )
                    values = {}

                try:
                    timestamp = await asyncio.to_thread(data_logger.get_latest_timestamp, sid_text)
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
                    all_stats_fast = await asyncio.to_thread(statter.get_all_stats_fast)
                    all_stats_fast = all_stats_fast or {}
                    return {sid: (all_stats_fast.get(sid) or {}) for sid in clean_ids}
                except Exception as exc:
                    printDM(
                        f"[dashboard] fast stats failed; falling back per sensor: {exc}",
                        location=MODULE,
                    )

            async def _stats_for_one(sid_text: str):
                try:
                    stats = await asyncio.to_thread(statter.get_24hr_stats, sid_text)
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
        for sid in all_values:
            try:
                configured_metrics = sensor_mgr.get_display_metrics(sid)
            except Exception:
                configured_metrics = []
            metrics = list(configured_metrics or [])
            try:
                stored_metrics = await asyncio.to_thread(data_logger.get_available_metrics, sid)
                stored_metrics = stored_metrics or []
            except Exception:
                stored_metrics = []

            known_metrics: list[str] = []
            known_metrics.extend(stored_metrics)
            vals = all_values.get(sid) or {}
            if vals:
                known_metrics.extend(list(vals.keys()))
            known_metrics.extend(list(mqtt_ingest.expected_gauge_map.get(sid) or []))
            extend_gauge_config_for_metrics(gauge_config, known_metrics)

            known_canonical = {
                canonicalize_metric_name(metric, gauge_config)
                for metric in known_metrics
            }
            ordered_all = [k for k in gauge_config.keys() if k in known_canonical]

            if not all_metrics_mode and not metrics:
                metrics = mqtt_ingest.expected_gauge_map.get(sid)
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
            if (now_mono - _cdp_debug_last_log) >= _CDP_DEBUG_MIN_INTERVAL_SEC:
                _cdp_debug_last_log = now_mono
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
            db_path = getattr(data_logger, "db_path", "sensorius_data.db")
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

        def _db_recent_events_for_key(switch_key: str, limit: int = 5) -> list[str]:
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
                  "time": ["On 2025-09-20 11:25:34-06:00", ...]  # up to 5 events, oldest → newest
                },
                ...
              }

            DB access uses canonical switch keys (including SWITCH_n_ID when available)
            via _switch_key(), while the UI still sees "switch_id::Label".
            """
            states: dict[str, dict] = {}
            seen_db_keys: set[str] = set()

            # A) Local Pi controllers (3-relay etc.)
            if isinstance(switch_controllers, dict):
                for ctrl in switch_controllers.values():
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
                        events = _db_recent_events_for_key(db_key, limit=5)
                        states[ui_key] = {"state": current_state, "time": events}

            # B) Remote Pico / Nodus switches via MQTT cache
            cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
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

                    events = _db_recent_events_for_key(db_key, limit=5)
                    is_on = str(human_state).lower() == "on"
                    states[ui_key] = {"state": is_on, "time": events}

            # C) Any additional keys seen historically in DB (switch_ids table)
            for db_key in _db_list_known_switch_keys():
                if not db_key or db_key in seen_db_keys:
                    continue

                events = _db_recent_events_for_key(db_key, limit=5)
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

        # prefer the instance passed into register_routes; fallback to current if caller passed None
        from .saiMQTTIngest import get_current_ingest as _get_ing
        ing = mqtt_ingest or _get_ing()

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
                    latest_timestamp = bulk_timestamps.get(sid_text) or data_logger.get_latest_timestamp(sid_text)
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
                    latest_timestamp = bulk_timestamps.get(sid_text) or data_logger.get_latest_timestamp(sid_text)
                    latest_values = all_values.get(sid_text) or data_logger.get_latest_values(sid_text) or {}
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
         
        if json_only:
            cache_key = (str(sensor_id or "All"), 1 if include_extras else 0)
            now_mono = time.monotonic()
            cached_json = _DASHBOARD_JSON_CACHE.get(cache_key)
            if cached_json and cached_json[0] > now_mono:
                cached_payload = cached_json[1]
                _ui_profile_log(
                    "dashboard",
                    _route_started,
                    json_only=1,
                    include_extras=int(bool(include_extras)),
                    cache=1,
                    sensor_id=(sensor_id or "All"),
                    sensors=len(available),
                    switches=len(available_switches),
                )
                return JSONResponse(cached_payload)

            timestamps = await asyncio.to_thread(
                lambda: {
                    sid: (bulk_timestamps.get(sid) or data_logger.get_latest_timestamp(sid) or "")
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

            payload: dict[str, object] = {
                "available": available,
                "values": all_values,
                "stats": all_stats,
                "timestamps": timestamps,
                "sensor_id": sensor_id,
                "timestamp": get_timestamp(),
                "locations": sensor_locations,
                "expected_gauge_map": expected_gauge_map,
                "expected_display_style_map": expected_display_style_map,
                "available_switches": available_switches,
                "renderable_switches": renderable_switches,
                "renderable_switches_view": renderable_switches_view,
                "statuses": statuses,
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
            _DASHBOARD_JSON_CACHE[cache_key] = (
                now_mono + _DASHBOARD_JSON_CACHE_TTL_SEC,
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

        dashboard_switch_controllers = globals().get("switch_controllers")
        if dashboard_switch_controllers is None:
            dashboard_switch_controllers = getattr(app.state, "switch_controllers", None)
        if dashboard_switch_controllers is None:
            dashboard_switch_controllers = {}

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
                mqtt_ingest,
                switch_controllers = dashboard_switch_controllers,
                sensor_locations = sensor_locations,
                gauge_config=gauge_config, 
                gauge_size = gaugeSize,
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
        _DASHBOARD_HTML_CACHE[dashboard_cache_key] = rendered_dashboard
        _DASHBOARD_HTML_CACHE.move_to_end(dashboard_cache_key)
        while len(_DASHBOARD_HTML_CACHE) > _DASHBOARD_HTML_CACHE_MAX_ENTRIES:
            _DASHBOARD_HTML_CACHE.popitem(last=False)
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

        ordered = _order_sensor_ids_by_metric_position(_current_dashboard_sensor_ids())
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
        _persist_visible_metric_order(ordered)
        return JSONResponse({"status": "ok", "sensor_id": sensor_id, "moved": True, "order": ordered})

    # graph data for the full-screen selector or the small single-axis gauge overlay
    @router.get("/graph-data", response_class=JSONResponse)
    async def graph_data_api(
        request: Request,
        # legacy / left axis primary (kept)
        sensor_id: str = Query(""),
        metric1: str = Query(""),
        metric2: str = Query(""),
        metric3: str = Query(""),
        metric4: str = Query(""),
        # one sensor per selected metric slot
        sensor_id1: str = Query(""),
        sensor_id2: str = Query(""),
        sensor_id3: str = Query(""),
        sensor_id4: str = Query(""),
        range: str = Query(...),
        start: str | None = Query(None),
        end: str | None = Query(None),
        astral: str = Query("none", description="Optional astral graph: none, sun, moon, or sun_moon"),
        # new, preferred way:
        switch_id: str = Query("", description="Switch ID to draw on/off transitions from"),
        channels: list[str] = Query([], alias="channels"),
        switch_channels: list[str] = Query(
            [],
            alias="switch_channels",
            description="Canonical <switch_id>::<channel label> transition selections",
        ),
        # legacy fallback (kept temporarily for compatibility):
        switches: list[str] = Query([], alias="switches"),
    ):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from fastapi.responses import JSONResponse
        from fastapi import HTTPException
        from .saiUtils import printDM

        MODULE = "graph-data"
        db_path = getattr(data_logger, "db_path", "sensorius_data.db")

        # --- Local zone from settings (seconds offset) ---
        def _local_tz():
            try:
                from .saiSettings import saiSettings
                s = saiSettings()
                off_s = int(s.get_setting("Time", "TZ_OFFSET", 0) or 0)
            except Exception:
                off_s = 0
            return timezone(timedelta(seconds=off_s))

        # --- Compute window in *local* offset and return ISO strings with offset ---
        def _compute_window(range_str: str, start_iso: str | None, end_iso: str | None):
            tz = _local_tz()
            try:
                max_days = max(1, int(os.getenv("SENSORIUS_DB_RETENTION_DAYS", "90")))
            except Exception:
                max_days = 90

            # helper for browser datetime-local or any ISO:
            def _coerce_iso_with_offset(iso_in: str) -> datetime:
                # browsers send 'YYYY-MM-DDTHH:mm' (naive local) → attach tz
                try:
                    dt = datetime.fromisoformat(iso_in)
                except Exception:
                    dt = datetime.strptime(iso_in, "%Y-%m-%dT%H:%M")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
                else:
                    dt = dt.astimezone(tz)
                return dt

            if (range_str or "").lower() == "custom" and start_iso and end_iso:
                start_dt = _coerce_iso_with_offset(start_iso)
                end_dt   = _coerce_iso_with_offset(end_iso)
                span_seconds = int((end_dt - start_dt).total_seconds())
                max_span_seconds = max_days * 86400
                if span_seconds > max_span_seconds:
                    raise ValueError(f"Selected range exceeds max of {max_days} days")
            else:
                now_local = datetime.now(tz)
                # map ranges → seconds
                ranges = {
                    "1h": 3600, "3h": 3*3600, "6h": 6*3600, "12h": 12*3600, "24h": 24*3600,
                    "3d": 3*86400, "7d": 7*86400
                }
                range_norm = (range_str or "24h").lower()
                if range_norm in ranges:
                    span = int(ranges[range_norm])
                else:
                    m_day = re.fullmatch(r"(\d+)d", range_norm)
                    m_hour = re.fullmatch(r"(\d+)h", range_norm)
                    if m_day:
                        span = min(int(m_day.group(1)), max_days) * 86400
                    elif m_hour:
                        span = min(int(m_hour.group(1)), max_days * 24) * 3600
                    else:
                        span = 24 * 3600
                end_dt = now_local
                start_dt = now_local - timedelta(seconds=span)

            # Return offset ISO (e.g., '...-06:00'), seconds span
            since_iso = start_dt.replace(microsecond=0).isoformat()
            until_iso = end_dt.replace(microsecond=0).isoformat()
            span_seconds = int((end_dt - start_dt).total_seconds())
            return since_iso, until_iso, span_seconds, start_dt, end_dt

        def _safe_float(value):
            try:
                fval = float(value)
                return fval if math.isfinite(fval) else None
            except Exception:
                return None

        def _astral_sample_step_seconds(span_seconds: int) -> int:
            if span_seconds <= 6 * 3600:
                return 5 * 60
            if span_seconds <= 7 * 86400:
                return 15 * 60
            if span_seconds <= 14 * 86400:
                return 30 * 60
            if span_seconds <= 30 * 86400:
                return 2 * 3600
            return 4 * 3600

        def _build_astral_graph_payload(mode: str, since_dt: datetime, until_dt: datetime, span_seconds: int) -> dict[str, object]:
            normalized = _normalize_astral_graph_mode(mode)
            payload: dict[str, object] = {
                "ok": False,
                "mode": normalized,
                "series": {},
                "detail": "",
                "lat": None,
                "lon": None,
                "tz": "",
            }
            if normalized == "none":
                payload["ok"] = True
                return payload
            if LocationInfo is None or _astral_elevation is None:
                payload["detail"] = "Astral support unavailable"
                return payload

            wants_sun = normalized in {"sun", "sun_moon"}
            wants_moon = normalized in {"moon", "sun_moon"}
            try:
                settings_obj = saiSettings(apply_live=False)
                resolved = settings_obj.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
            except Exception:
                payload["detail"] = "Astral location unavailable"
                return payload

            lat = _safe_float((resolved or {}).get("lat"))
            lon = _safe_float((resolved or {}).get("lon"))
            altitude = _safe_float((resolved or {}).get("altitude"))
            tz_name = str((resolved or {}).get("tz") or "").strip()
            if lat is None or lon is None:
                payload["detail"] = "Astral coordinates unavailable"
                return payload
            try:
                tzinfo = ZoneInfo(tz_name) if tz_name else since_dt.tzinfo
            except Exception:
                tzinfo = since_dt.tzinfo
            if tzinfo is None:
                tzinfo = timezone.utc
            if not tz_name:
                tz_name = getattr(tzinfo, "key", "") or str(tzinfo)

            try:
                obs = LocationInfo(
                    name="sensorius",
                    region="local",
                    timezone=tz_name or "UTC",
                    latitude=lat,
                    longitude=lon,
                ).observer
                if altitude is not None:
                    obs.elevation = altitude
            except Exception:
                payload["detail"] = "Astral observer unavailable"
                return payload

            start_local = since_dt.astimezone(tzinfo)
            end_local = until_dt.astimezone(tzinfo)
            step = timedelta(seconds=_astral_sample_step_seconds(span_seconds))
            samples: list[datetime] = []
            cursor = start_local
            max_samples = 900
            while cursor <= end_local and len(samples) < max_samples:
                samples.append(cursor)
                cursor = cursor + step
            if samples and samples[-1] < end_local:
                samples.append(end_local)
            if not samples:
                samples = [start_local, end_local]

            series: dict[str, dict[str, list[object]]] = {}
            if wants_sun:
                sun_ts: list[str] = []
                sun_vals: list[float] = []
                for sample_dt in samples:
                    try:
                        elev = float(_astral_elevation(obs, sample_dt))
                    except Exception:
                        elev = float("nan")
                    if math.isfinite(elev):
                        sun_ts.append(sample_dt.replace(microsecond=0).isoformat())
                        sun_vals.append(round(elev, 2))
                if sun_ts:
                    series["sun"] = {"ts": sun_ts, "vals": sun_vals}

            if wants_moon:
                moon_ts: list[str] = []
                moon_vals: list[float] = []
                moon_el_fn = getattr(_astral_moon, "elevation", None) if _astral_moon is not None else None
                position_ts = None
                position_observer = None
                position_moon_body = None
                try:
                    skyfield_runtime = get_skyfield_runtime_if_installed()
                    if skyfield_runtime is not None:
                        _loader, position_ts, position_eph, _constellation_at = skyfield_runtime
                        from skyfield.api import wgs84

                        topo = wgs84.latlon(lat, lon, elevation_m=float(altitude or 0.0))
                        position_observer = position_eph["earth"] + topo
                        position_moon_body = position_eph["moon"]
                except Exception:
                    position_ts = None
                    position_observer = None
                    position_moon_body = None

                for sample_dt in samples:
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
                        moon_ts.append(sample_dt.replace(microsecond=0).isoformat())
                        moon_vals.append(round(moon_elev, 2))
                if moon_ts:
                    series["moon"] = {"ts": moon_ts, "vals": moon_vals}

            payload.update(
                {
                    "ok": bool(series),
                    "series": series,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "tz": tz_name,
                }
            )
            if not series:
                payload["detail"] = "Astral position data unavailable"
            return payload

        # ----- time range (ALL in local offset, matching DB storage) -----
        try:
            since_iso, until_iso, span_seconds, since_dt, until_dt = _compute_window(range, start, end)
        except Exception as e:
            return JSONResponse({"error": f"Invalid time range: {e}"}, status_code=400)

        # ----- normalize inputs (back-compat + new triplets) -----
        sid1 = (sensor_id1 or "").strip() or (sensor_id or "").strip()
        sid2 = (sensor_id2 or "").strip()
        sid3 = (sensor_id3 or "").strip()
        sid4 = (sensor_id4 or "").strip()

        m1 = (metric1 or "").strip()
        m2 = (metric2 or "").strip()
        m3 = (metric3 or "").strip()
        m4 = (metric4 or "").strip()

        pairs: list[tuple[str, str]] = []
        if sid1 and m1: pairs.append((sid1, m1))
        if sid2 and m2: pairs.append((sid2, m2))
        if sid3 and m3: pairs.append((sid3, m3))
        if sid4 and m4: pairs.append((sid4, m4))
        if not pairs and sensor_id and metric1:
            pairs.append((sensor_id, metric1))
            if metric2: pairs.append((sensor_id, metric2))
            if metric3: pairs.append((sensor_id, metric3))
            if metric4: pairs.append((sensor_id, metric4))

        if not pairs:
            raise HTTPException(status_code=400, detail="No sensor/metric selections provided")

        # ----- helpers (use LOCAL OFFSET ISO window for SQL string compare) -----
        def fetch_xy(cur, sid: str, metric_name: str, window_since_iso: str, window_until_iso: str):
            try:
                #printDM(f"[{MODULE}] Query {sid}.{metric_name} {since_iso} → {until_iso} (DB local-offset ISO)", location=MODULE)
                cur.execute(
                    """
                    SELECT timestamp, value
                    FROM readings
                    WHERE sensor_id = ? COLLATE NOCASE
                    AND metric    = ? COLLATE NOCASE
                    AND julianday(timestamp) >= julianday(?)
                    AND julianday(timestamp) <= julianday(?)
                    ORDER BY julianday(timestamp) ASC
                    """,
                    (sid, metric_name, window_since_iso, window_until_iso)
                )
                rows = cur.fetchall()
                ts = [r[0] for r in rows]
                vs = [r[1] for r in rows]
                return _downsample_graph_points(ts, vs)
            except Exception as e:
                printDM(f"[{MODULE}] Error fetching {sid}.{metric_name}: {e}", location=MODULE)
                return [], []

        # ----- data series -----
        series: dict[str, dict] = {}
        simple_avg: dict[str, dict] = {}
        display_names: dict[str, str] = {}
        with _sqlite_connect_with_recovery(db_path, source="graph_series") as conn:
            cur = conn.cursor()
            for sid, metric_name in pairs:
                ts, vs = fetch_xy(cur, sid, metric_name, since_iso, until_iso)
                key = f"{sid}::{metric_name}"
                if not ts or not vs:
                    continue

                try:
                    vis_ts = list(ts)
                    vis_vals = list(vs)
                    if vis_ts:
                        series[key] = {"ts": vis_ts, "vals": vis_vals}
                        numeric_vals: list[float] = []
                        for raw in vis_vals:
                            try:
                                numeric_vals.append(float(raw))
                            except Exception:
                                continue
                        if numeric_vals:
                            avg_val = sum(numeric_vals) / float(len(numeric_vals))
                            simple_avg[key] = {"ts": vis_ts, "vals": [avg_val] * len(vis_ts)}
                        display_names[key] = key
                except Exception as e:
                    printDM(f"[{MODULE}] simple-avg error for {key}: {e}", location=MODULE)
                    if ts and vs:
                        series[key] = {"ts": ts, "vals": vs}
                        display_names[key] = key

        if not series:
            first = pairs[0]
            return JSONResponse(
                content={
                    "series": {},
                    "simple_avg": {},
                    "rolling_ema": {},
                    "display_names": {},
                    "axis_titles": {"y1": "Left", "y2": ""},
                    "window": {
                        "since_iso": since_iso,
                        "until_iso": until_iso,
                        "span_seconds": span_seconds,
                    },
                    "no_data": True,
                    "detail": f"No data found for {first[0]}.{first[1]} in selected range",
                },
                status_code=200,
            )

        response = {
            "series": series,
            "simple_avg": simple_avg,
            "rolling_ema": simple_avg,
            "display_names": display_names,
            "axis_titles": {
                "y1": list(series.keys())[0] if series else "Left",
                "y2": " / ".join(list(series.keys())[1:]) if len(series) > 1 else ""
            },
            "window": {  # local offset window for client pinning
                "since_iso": since_iso,
                "until_iso": until_iso,
                "span_seconds": span_seconds
            }
        }
        astral_mode = _normalize_astral_graph_mode(astral)
        if astral_mode != "none":
            response["astral"] = _build_astral_graph_payload(astral_mode, since_dt, until_dt, span_seconds)

        # ----- switch vertical lines (use the SAME LOCAL window in SQL) -----
        want_switch_lines = bool(switch_channels or (switch_id and channels) or switches)
        if want_switch_lines:
            switch_lines: dict[str, list[tuple[str, int]]] = {}

            def _fetch_transitions_from_sw_events(_switch_id: str, label: str) -> list[tuple[str, int]]:
                """
                Fetch switch transitions for a given (switch_id, label) using the
                canonical DB registry.

                We join sw_events to switch_ids:

                    sw_events.switch_key == switch_ids.switch_key

                This automatically pulls *all* events for that switch/channel,
                whether the key is an older '<switch_id>::<label>' shape or the
                new '<switch_id>::<channel_id>' shape.
                """
                sid = (_switch_id or "").strip()
                lab = (label or "").strip()
                if not sid or not lab:
                    return []

                try:
                    with _sqlite_connect_with_recovery(db_path, source="graph_switch_transitions") as conn2:
                        cur2 = conn2.cursor()
                        cur2.execute(
                            """
                            SELECT e.timestamp, e.state
                            FROM sw_events AS e
                            JOIN switch_ids AS i
                              ON e.switch_key = i.switch_key
                            WHERE i.switch_id = ? COLLATE NOCASE
                              AND i.label     = ? COLLATE NOCASE
                              AND julianday(e.timestamp) >= julianday(?)
                              AND julianday(e.timestamp) <= julianday(?)
                            ORDER BY julianday(e.timestamp) ASC
                            """,
                            (sid, lab, since_iso, until_iso),
                        )
                        rows = cur2.fetchall()
                except Exception as e:
                    printDM(
                        f"[{MODULE}] sw_events join fetch failed for {sid}::{lab}: {e}",
                        location=MODULE,
                    )
                    rows = []

                out: list[tuple[str, int]] = []
                last: int | None = None
                for ts, st in rows:
                    bit = 1 if (st == 1 or str(st).strip().lower() in ("1", "true", "on")) else 0
                    if last is None or bit != last:
                        out.append((ts, bit))
                        last = bit
                return out

            # Optional: support readings-backed switch series as earlier (kept)
            def _fetch_transitions_from_readings(series_id: str, metric_name: str) -> list[tuple[str, int]]:
                try:
                    with _sqlite_connect_with_recovery(db_path, source="graph_reading_transitions") as conn2:
                        cur2 = conn2.cursor()
                        cur2.execute(
                            """
                            SELECT timestamp, value
                            FROM readings
                            WHERE sensor_id = ?
                              AND metric = ? COLLATE NOCASE
                              AND julianday(timestamp) >= julianday(?)
                              AND julianday(timestamp) <= julianday(?)
                            ORDER BY julianday(timestamp) ASC
                            """,
                            (series_id, metric_name, since_iso, until_iso),
                        )
                        rows = cur2.fetchall()
                except Exception:
                    rows = []

                out: list[tuple[str, int]] = []
                last: int | None = None
                for ts, val in rows:
                    try:
                        bit = 1 if float(val) >= 0.5 else 0
                    except Exception:
                        bit = 1 if str(val).strip().lower() in ("1", "true", "on") else 0
                    if last is None or bit != last:
                        out.append((ts, bit))
                        last = bit
                return out

            try:
                if switch_channels:
                    for switch_key in switch_channels:
                        selected_switch_id, separator, label = str(switch_key or "").partition("::")
                        selected_switch_id = selected_switch_id.strip()
                        label = label.strip()
                        if not separator or not selected_switch_id or not label:
                            continue
                        trans = _fetch_transitions_from_sw_events(selected_switch_id, label) \
                                or _fetch_transitions_from_readings(selected_switch_id, label)
                        if trans:
                            switch_lines[f"{selected_switch_id} · {label}"] = trans
                elif switch_id and channels:
                    # New, preferred path: explicit switch_id + list of channel labels
                    for label in channels:
                        trans = _fetch_transitions_from_sw_events(switch_id, label) \
                                or _fetch_transitions_from_readings(switch_id, label)
                        if trans:
                            switch_lines[label] = trans
                elif switches:
                    # Legacy path: "switches" query param contains labels
                    for maybe_label in switches:
                        label = (maybe_label or "").strip()
                        if not label:
                            continue
                        trans = _fetch_transitions_from_sw_events(switch_id, label) if switch_id else []
                        if not trans:
                            trans = _fetch_transitions_from_readings(f"Switch_{sensor_id}", label)
                        if trans:
                            switch_lines[label] = trans
            except Exception as e:
                printDM(f"[{MODULE}] Switch line fetch error: {e}", location=MODULE)

            if switch_lines:
                response["switch_lines"] = switch_lines

        return JSONResponse(content=response)

    @router.get("/graph-setups", response_class=JSONResponse)
    async def api_graph_setups_list():
        try:
            data, last_used = _load_graph_setups_state()
            return JSONResponse(_graph_setups_payload(data, last_used))
        except Exception as exc:
            printDM(f"/graph-setups error: {exc}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/graph-setups/save", response_class=JSONResponse)
    async def api_graph_setups_save(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        name = _normalize_graph_setup_name((body or {}).get("name"))
        if not name:
            return JSONResponse({"error": "name_required"}, status_code=400)

        config = _normalize_graph_setup_config((body or {}).get("config", {}))
        try:
            data, _ = _load_graph_setups_state()
            data[name] = config
            _save_graph_setups_state(data, name)
            return JSONResponse(_graph_setups_payload(data, name))
        except Exception as exc:
            printDM(f"/graph-setups/save error: {exc}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/graph-setups/remove", response_class=JSONResponse)
    async def api_graph_setups_remove(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        name = _normalize_graph_setup_name((body or {}).get("name"))
        if not name:
            return JSONResponse({"error": "name_required"}, status_code=400)

        try:
            data, last_used = _load_graph_setups_state()
            if name not in data:
                return JSONResponse({"error": "not_found"}, status_code=404)
            data.pop(name, None)
            if last_used == name:
                last_used = ""
            _save_graph_setups_state(data, last_used)
            return JSONResponse(_graph_setups_payload(data, last_used))
        except Exception as exc:
            printDM(f"/graph-setups/remove error: {exc}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/graph-setups/use", response_class=JSONResponse)
    async def api_graph_setups_set_last_used(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        name = _normalize_graph_setup_name((body or {}).get("name"))
        if not name:
            return JSONResponse({"error": "name_required"}, status_code=400)

        try:
            data, _ = _load_graph_setups_state()
            if name not in data:
                return JSONResponse({"error": "not_found"}, status_code=404)
            _save_graph_setups_state(data, name)
            return JSONResponse(_graph_setups_payload(data, name))
        except Exception as exc:
            printDM(f"/graph-setups/use error: {exc}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

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
            astral_altitude=astral_altitude,
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
            if mqtt_ingest and hasattr(mqtt_ingest, "_build_time_payload"):
                payload = mqtt_ingest._build_time_payload()
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
                    sm = sensor_map  # provided by app bootstrap
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
        ok = bool(mqtt_ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
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
                allow_removed = getattr(mqtt_ingest, "allow_nodus_devices", None)
                if callable(allow_removed):
                    allow_removed([device_id], persist=True)
                refresh_retained = getattr(mqtt_ingest, "refresh_nodus_retained_metadata", None)
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
            today_local = datetime.now(getattr(data_logger, "local_tz", ZoneInfo("America/Denver"))).date()
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
            notes, daily_summaries = await asyncio.gather(
                asyncio.to_thread(data_logger.get_biodynamic_notes_for_range, range_start, range_end),
                asyncio.to_thread(data_logger.get_biodynamic_daily_summaries_for_range, range_start, range_end),
            )
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
                            data_logger.get_biodynamic_daily_summaries_for_range,
                            range_start,
                            range_end,
                        )
            except Exception as exc:
                if DEBUG:
                    printDM(f"[api_biodynamic_calendar] visible summary repair skipped: {exc}", location=MODULE)
            payload["notes"] = notes
            payload["daily_summaries"] = daily_summaries
        else:
            notes, daily_summaries = await asyncio.gather(
                asyncio.to_thread(data_logger.get_biodynamic_notes_for_month, anchor),
                asyncio.to_thread(data_logger.get_biodynamic_daily_summaries_for_month, anchor),
            )
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
                    db_path=str(getattr(data_logger, "db_path", "sensorius_data.db") or "sensorius_data.db"),
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

        ok = await asyncio.to_thread(data_logger.save_biodynamic_note, normalized_date, note_text)
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
    @router.get("/hayd", response_class=JSONResponse)
    async def how_are_you_doing():
        _status = "ok"
        return {"STATUS": _status}

    @router.get("/debug/switch-controllers", response_class=JSONResponse)
    async def debug_switch_controllers(request: Request):
        """
        Return the currently registered switch controllers (debug only).
        """
        try:
            _require_protected_access(request)
            out = []
            sc = globals().get("switch_controllers")
            if isinstance(sc, dict):
                for k, ctrl in sc.items():
                    out.append({
                        "key": k,
                        "switch_id": getattr(ctrl, "switch_id", None),
                        "location": getattr(ctrl, "location", None),
                        "labels": list(getattr(ctrl, "get_switch_names", lambda: [])() or []),
                        "is_present": bool(getattr(ctrl, "is_present", False)),
                    })
            elif sc:
                out.append({
                    "key": None,
                    "switch_id": getattr(sc, "switch_id", None),
                    "location": getattr(sc, "location", None),
                    "labels": list(getattr(sc, "get_switch_names", lambda: [])() or []),
                    "is_present": bool(getattr(sc, "is_present", False)),
                })
            return {"count": len(out), "items": out}
        except HTTPException:
            raise
        except Exception as e:
            printDM(f"[debug_switch_controllers] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.get("/debug/automation-state", response_class=JSONResponse)
    async def debug_automation_state(
        request: Request,
        switch_id: str = Query(...),
        label: str = Query(...),
    ):
        """
        Debug helper to inspect Advanced automation enabled state for a switch/label.
        """
        try:
            _require_protected_access(request)
            from .saiAutomationManager import AutomationManager
            sid = (switch_id or "").strip()
            lbl = (label or "").strip()
            switch_key = f"{sid}::{lbl}" if sid and lbl else ""
            mgr = AutomationManager("automation_settings")
            path = mgr._path_for_hostname(sid) if sid else None
            data = mgr.load(sid) if sid else {}
            adv = (data.get("Advanced") or {}) if isinstance(data, dict) else {}

            state = mgr.get_advanced_state_for_switch_key(sid, switch_key) if switch_key else {}

            return {
                "switch_id": sid,
                "label": lbl,
                "switch_key": switch_key,
                "path": str(path) if path else None,
                "path_exists": bool(path and path.exists()),
                "mtime": path.stat().st_mtime if path and path.exists() else None,
                "advanced_rules": adv,
                "computed_state": state,
            }
        except HTTPException:
            raise
        except Exception as e:
            printDM(f"[debug_automation_state] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

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
                    allow_removed = getattr(mqtt_ingest, "allow_nodus_devices", None)
                    if callable(allow_removed):
                        allow_removed([sensor_id_for_step], persist=True)
                    # match the form you persist in CLIENTS (you showed ".local")
                    host_for_ingest = mdns_hostname(sensor_id_for_step)
                    mqtt_ingest.add_client(host_for_ingest)
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
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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

        return {
            "ip_address": ip_address or "Unknown",
            "broker": broker or "Unknown",
            "broker_status": _broker_health_status(broker, ingest),
        }

    def _display_nodus_board_type(device_id: str, *, device_type: str, settings_block: dict | None = None) -> str:
        board_type = ""
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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

        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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

        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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

        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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
                
            # shared handles established at startup by Sensorius
            global switch_controllers

            def _find_switch_ctrl_by_id(switch_id: str):
                """Return the live SwitchController with matching switch_id, or None."""
                try:
                    sid = (switch_id or "").strip()
                    if not sid:
                        return None
                    if isinstance(switch_controllers, dict):
                        for cand in switch_controllers.values():
                            if getattr(cand, "switch_id", "") in {sid, sid.lower()}:
                                return cand
                    else:
                        if getattr(switch_controllers, "switch_id", "") in {sid, sid.lower()}:
                            return switch_controllers
                except Exception:
                    pass
                return None
                
            # --- make sensor_map accessible anywhere in this handler
            from collections.abc import Iterable
            def _get_sensor_map():
                sm = getattr(app.state, "sensor_map", None)
                if sm is None:
                    from . import saiWebRoutes as routes
                    sm = getattr(routes, "sensor_map", None)
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
                            if hasattr(mqtt_ingest, "device_location") and isinstance(mqtt_ingest.device_location, dict):
                                mqtt_ingest.device_location[topic] = location or "Unknown"
                        except Exception:
                            pass
                        _SENSOR_LOCATION_CACHE.pop(dev_id, None)

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
        try:
            from .saiMQTTIngest import get_current_ingest as _get_ing
            ing = _get_ing()
        except Exception:
            ing = None
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
            ids.update(data_logger.get_available_sensors() or [])
        except Exception:
            pass
        try:
            for row in (data_logger.get_switch_identities() or []):
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

        try:
            from .saiMQTTIngest import get_current_ingest as _get_ing
            ingest = _get_ing()
        except Exception:
            ingest = None
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
        try:
            from .saiMQTTIngest import get_current_ingest as _get_ing
            ingest = _get_ing()
        except Exception:
            ingest = None

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

        try:
            _purge_map(globals().get("switch_controllers"))
        except Exception:
            pass
        try:
            _purge_map(getattr(app.state, "switch_controllers", None))
        except Exception:
            pass

        for key, task in list(_dynamic_switch_monitor_tasks.items()):
            if str(key or "").strip().lower() not in ids_l:
                continue
            _dynamic_switch_monitor_tasks.pop(key, None)
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
        try:
            logger = getattr(app.state, "data_logger", None)
            if logger is not None:
                return logger
        except Exception:
            pass
        return globals().get("data_logger")

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
        return HTMLResponse("<html><body><p>Use the Remove Device button to open the modal.</p></body></html>")

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

        try:
            from .saiMQTTIngest import get_current_ingest as _get_ing
            mqtt_ingest = _get_ing()
        except Exception:
            mqtt_ingest = None

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
        global _switch_status_cache_payload, _switch_status_cache_until
        _switch_status_cache_payload = None
        _switch_status_cache_until = 0.0

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

        overall = f"Removed {len(requested_ids)} device(s)."
        return JSONResponse({"ok": True, "results": results, "summary": overall})

    # submit routes and helpers
    @router.post("/retry-discovery")
    async def retry_discovery(request: Request):
        data = await request.json()
        hostname = data.get("host")
        if hostname in request.app.state.mqtt_ingest.discovery_failures:
            del request.app.state.mqtt_ingest.discovery_failures[hostname]
            request.app.state.mqtt_ingest.device_status[hostname] = "unknown"
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
                    altitude_val = float(raw_altitude)
                except Exception:
                    return _modal_error_response(request, "Altitude must be a numeric value in meters.", status_code=400)
                if not (-500.0 <= altitude_val <= 10000.0):
                    return _modal_error_response(request, "Altitude must be between -500 and 10000 meters.", status_code=400)
                altitude_to_store = f"{altitude_val:.2f}"
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
                resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=3.5) or {}
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

    @router.post("/submit-homeassistant-settings")
    async def submit_homeassistant_settings(request: Request):
        settings = saiSettings()
        try:
            data = await request.json()
        except Exception:
            data = {}

        enabled = bool(data.get("enabled", False))
        use_tls = bool(data.get("use_tls", False))
        broker = str(data.get("broker", "") or "").strip()
        username = str(data.get("username", "") or "").strip()
        password = str(data.get("password", "") or "").strip()
        try:
            port = int(data.get("port", 1883) or 1883)
        except Exception:
            port = 1883
        if port < 1 or port > 65535:
            port = 1883

        settings.replace_setting("HomeAssistant", "ENABLED", enabled)
        settings.replace_setting("HomeAssistant", "USE_TLS", use_tls)
        settings.replace_setting("HomeAssistant", "HA_BROKER", broker)
        settings.replace_setting("HomeAssistant", "HA_MQTTPORT", port)
        settings.replace_setting("HomeAssistant", "HA_USERNAME", username)
        settings.replace_setting("HomeAssistant", "HA_PASSWORD", saiSettings.obfuscate_secret(password))

        return JSONResponse({"status": "ok"})

    def _ecowitt_service(request: Request) -> EcowittGatewayIngest:
        service = getattr(request.app.state, "ecowitt_service", None)
        if service is None:
            service = EcowittGatewayIngest(
                settings=saiSettings(),
                data_logger=getattr(request.app.state, "data_logger", None) or data_logger,
                supervisor=getattr(request.app.state, "supervisor", None),
            )
            request.app.state.ecowitt_service = service
        return service

    @router.post("/ecowitt/discover")
    async def ecowitt_discover(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            result = await _ecowitt_service(request).discover(payload.get("gateway_url", ""))
            return JSONResponse(result)
        except EcowittError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            if DEBUG:
                printDM(f"Ecowitt discovery failed: {exc}", location=MODULE)
            return JSONResponse({"ok": False, "error": "Ecowitt discovery failed."}, status_code=502)

    @router.post("/ecowitt/save")
    async def ecowitt_save(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        service = _ecowitt_service(request)
        try:
            discovery = await service.discover(payload.get("gateway_url", ""))
            await asyncio.to_thread(
                service.save_configuration,
                discovery,
                payload.get("poll_interval_sec", ECOWITT_DEFAULT_POLL_INTERVAL_SEC),
            )
            return JSONResponse({"ok": True, **discovery, "poll_interval_sec": service.poll_interval_sec})
        except EcowittError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            printDM(f"Ecowitt settings save failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": "Failed to save Ecowitt settings."}, status_code=500)

    @router.get("/ecowitt/status")
    async def ecowitt_status(request: Request):
        return JSONResponse(_ecowitt_service(request).status())

    @router.post("/ecowitt/disable")
    async def ecowitt_disable(request: Request):
        try:
            await asyncio.to_thread(_ecowitt_service(request).disable)
            return JSONResponse({"ok": True})
        except Exception as exc:
            printDM(f"Ecowitt disable failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": "Failed to disable Ecowitt integration."}, status_code=500)

    @router.post("/submit-weewx-settings")
    async def submit_weewx_settings(request: Request):
        settings = saiSettings()
        try:
            data = await request.json()
        except Exception:
            data = {}

        mqtt_enabled = bool(data.get("mqtt_enabled", False))
        db_path = str(data.get("db_path", WEEWX_DEFAULT_DB_PATH) or WEEWX_DEFAULT_DB_PATH).strip()
        sensor_id = str(data.get("sensor_id", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID).strip()
        mqtt_topic = str(data.get("mqtt_topic", WEEWX_DEFAULT_MQTT_TOPIC) or WEEWX_DEFAULT_MQTT_TOPIC).strip()
        try:
            update_period_sec = int(data.get("update_period_sec", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        except Exception:
            update_period_sec = int(WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        update_period_sec = max(15, min(3600, update_period_sec))

        if not db_path.startswith("/"):
            return JSONResponse({"ok": False, "error": "WeeWX database path must be absolute."}, status_code=400)
        if not re.match(r"^[A-Za-z0-9._-]+$", sensor_id):
            return JSONResponse({"ok": False, "error": "WeeWX sensor ID may contain only letters, numbers, dot, underscore, and dash."}, status_code=400)
        if not mqtt_topic:
            return JSONResponse({"ok": False, "error": "WeeWX MQTT topic filter is required."}, status_code=400)

        settings.replace_setting("WeeWX", "MQTT_ENABLED", mqtt_enabled)
        settings.replace_setting("WeeWX", "MQTT_TOPIC", mqtt_topic)
        settings.replace_setting("WeeWX", "SENSOR_ID", sensor_id)
        settings.replace_setting("WeeWX", "DB_PATH", db_path)
        settings.replace_setting("WeeWX", "UPDATE_PERIOD_SEC", update_period_sec)
        settings.replace_setting("WeeWX", "ENABLED", False)
        settings.replace_setting("WeeWX", "AUTO_DISCOVER", False)

        try:
            ensure_weewx_sensor_settings(sensor_id, manager=SensorSettingsManager("sensor_settings"))
        except Exception as exc:
            printDM(f"WeeWX sensor settings materialization failed: {exc}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": "Failed to create WeeWX sensor settings."},
                status_code=500,
            )

        applied_live = False
        try:
            from .saiMQTTIngest import get_current_ingest
            ingest = get_current_ingest()
            if ingest is not None and hasattr(ingest, "configure_weewx_mqtt"):
                applied_live = bool(
                    ingest.configure_weewx_mqtt(
                        enabled=mqtt_enabled,
                        topic_filter=mqtt_topic,
                        sensor_id=sensor_id,
                        update_period_sec=update_period_sec,
                    )
                )
        except Exception as exc:
            printDM(f"WeeWX MQTT live reconfigure skipped: {exc}", location=MODULE)

        return JSONResponse({"status": "ok", "restart_required": not applied_live})

    @router.get("/weewx/status")
    async def weewx_status(request: Request):
        settings = saiSettings(apply_live=False)
        sensor_id = str(settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID).strip() or WEEWX_DEFAULT_SENSOR_ID
        db_path = str(settings.get_setting("WeeWX", "DB_PATH", WEEWX_DEFAULT_DB_PATH) or WEEWX_DEFAULT_DB_PATH).strip()
        mqtt_enabled = bool(settings.get_setting("WeeWX", "MQTT_ENABLED", False))
        sqlite_enabled = bool(settings.get_setting("WeeWX", "ENABLED", False))
        auto_discover = bool(settings.get_setting("WeeWX", "AUTO_DISCOVER", False))
        try:
            update_period_sec = float(settings.get_setting("WeeWX", "UPDATE_PERIOD_SEC", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        except Exception:
            update_period_sec = float(WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        update_period_sec = max(15.0, update_period_sec)
        offline_after_sec = update_period_sec * 3.0

        latest_timestamp = None
        latest_age_sec = None
        latest_values = {}
        try:
            latest_timestamp = data_logger.get_latest_timestamp(sensor_id)
            latest_values = data_logger.get_latest_values(sensor_id) or {}
            tz_name = str(settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver")
            _status, latest_age_sec = _weewx_measure_status_from_latest(
                latest_timestamp=latest_timestamp,
                latest_values=latest_values,
                update_period_sec=update_period_sec,
                tz_name=tz_name,
            )
        except Exception:
            latest_timestamp = None
            latest_age_sec = None
            latest_values = {}

        configured = mqtt_enabled or sqlite_enabled or auto_discover
        weewx_measure_status, latest_age_sec = _weewx_measure_status_from_latest(
            latest_timestamp=latest_timestamp,
            latest_values=latest_values,
            update_period_sec=update_period_sec,
            tz_name=str(settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver"),
        )
        receiving = weewx_measure_status == "online"
        stale = weewx_measure_status == "offline"

        if mqtt_enabled:
            mode = "MQTT configured"
        elif sqlite_enabled or auto_discover:
            mode = "SQLite fallback configured"
        elif latest_timestamp:
            mode = "Data present, integration switch disabled"
        else:
            mode = "Not configured"

        if receiving:
            state = "online"
            label = "Receiving WeeWX weather data"
        elif stale:
            state = "offline"
            label = "WeeWX data is stale"
        elif not configured and not latest_timestamp:
            state = "disabled"
            label = "WeeWX integration disabled"
        else:
            state = "unknown"
            label = "Waiting for WeeWX weather data"

        note = ""
        if latest_timestamp and not configured:
            note = "The MQTT interface is disabled, but Sensorius has stored WeeWX station data."
        elif mqtt_enabled:
            note = "MQTT availability is inferred from weather data updates."

        return JSONResponse({
            "state": state,
            "label": label,
            "mode": mode,
            "note": note,
            "sensor_id": sensor_id,
            "db_path": db_path,
            "mqtt_enabled": mqtt_enabled,
            "sqlite_enabled": sqlite_enabled,
            "auto_discover": auto_discover,
            "latest_timestamp": latest_timestamp or "",
            "latest_age_sec": latest_age_sec,
            "offline_after_sec": offline_after_sec,
            "latest_metric_count": len(latest_values or {}),
        })

    @router.post("/submit-farmos-settings")
    async def submit_farmos_settings(request: Request):
        settings = saiSettings()
        try:
            data = await request.json()
        except Exception:
            data = {}

        enabled = bool(data.get("enabled", False))
        base_url = str(data.get("base_url", "") or "").strip().rstrip("/")
        verify_tls = bool(data.get("verify_tls", True))
        access_token = str(data.get("access_token", "") or "").strip()
        client_id = str(data.get("client_id", "farm") or "farm").strip() or "farm"
        client_secret = str(data.get("client_secret", "") or "").strip()
        username = str(data.get("username", "") or "").strip()
        password = str(data.get("password", "") or "").strip()
        log_bundle = str(data.get("log_bundle", "observation") or "observation").strip().lower() or "observation"

        settings.replace_setting("FarmOS", "ENABLED", enabled)
        settings.replace_setting("FarmOS", "BASE_URL", base_url)
        settings.replace_setting("FarmOS", "VERIFY_TLS", verify_tls)
        settings.replace_setting("FarmOS", "ACCESS_TOKEN", saiSettings.obfuscate_secret(access_token))
        settings.replace_setting("FarmOS", "CLIENT_ID", client_id)
        settings.replace_setting("FarmOS", "CLIENT_SECRET", saiSettings.obfuscate_secret(client_secret))
        settings.replace_setting("FarmOS", "USERNAME", username)
        settings.replace_setting("FarmOS", "PASSWORD", saiSettings.obfuscate_secret(password))
        settings.replace_setting("FarmOS", "LOG_BUNDLE", log_bundle)

        return JSONResponse({"status": "ok"})

    @router.get("/farmos/status")
    async def farmos_status(request: Request):
        bridge = getattr(request.app.state, "farmos_bridge", None)
        if bridge and hasattr(bridge, "status_snapshot"):
            try:
                return JSONResponse(bridge.status_snapshot())
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        settings = saiSettings(apply_live=False)
        return JSONResponse({
            "enabled": bool(settings.get_setting("FarmOS", "ENABLED", False)),
            "base_url": str(settings.get_setting("FarmOS", "BASE_URL", "") or "").strip(),
            "verify_tls": bool(settings.get_setting("FarmOS", "VERIFY_TLS", True)),
            "log_bundle": str(settings.get_setting("FarmOS", "LOG_BUNDLE", "observation") or "observation").strip(),
            "queue_depth": 0,
            "has_static_token": bool(str(settings.get_setting("FarmOS", "ACCESS_TOKEN", "") or "").strip()),
            "has_runtime_token": False,
            "last_error": "FarmOS bridge not attached",
        })

    @router.post("/farmos/test")
    async def farmos_test(request: Request):
        bridge = getattr(request.app.state, "farmos_bridge", None)
        if bridge and hasattr(bridge, "test_connection"):
            try:
                result = await bridge.test_connection()
                code = 200 if bool(result.get("ok")) else 502
                return JSONResponse(result, status_code=code)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        return JSONResponse({"ok": False, "error": "FarmOS bridge not attached"}, status_code=503)

    @router.get("/advanced/status")
    async def advanced_status(request: Request):
        env_map = _env_map_with_defaults()
        log_level = str(env_map.get("SENSORIUS_LOG_LEVEL", "DEBUG") or "DEBUG").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            log_level = "DEBUG"

        file_log = _is_true_text(env_map.get("SENSORIUS_FILE_LOG"), default=False)
        log_file = str(env_map.get("SENSORIUS_LOG_FILE", DEFAULT_LOG_FILE) or DEFAULT_LOG_FILE).strip()
        log_file_path = str(Path(log_file).expanduser().resolve())
        dbg_raw = str(env_map.get("SENSORIUS_DEBUG_MODULES", "") or "")
        debug_modules = [m.strip() for m in dbg_raw.split(",") if m.strip()]

        retention_days = _clamp_db_retention_days(env_map.get("SENSORIUS_DB_RETENTION_DAYS", _DB_RETENTION_DEFAULT_DAYS))

        autostart_scope = str(env_map.get("SENSORIUS_AUTOSTART_SCOPE", "user") or "user").strip().lower()
        if autostart_scope not in {"user", "system"}:
            autostart_scope = "user"
        autostart_enabled = _autostart_is_enabled(autostart_scope)
        supervisor = getattr(request.app.state, "supervisor", None)
        runtime_health = None
        if supervisor and hasattr(supervisor, "runtime_status_snapshot"):
            try:
                runtime_health = supervisor.runtime_status_snapshot()
            except Exception:
                runtime_health = None

        return JSONResponse({
            "platform": platform.system(),
            "autostart_scope": autostart_scope,
            "autostart_enabled": bool(autostart_enabled),
            "log_level": log_level,
            "file_log": bool(file_log),
            "log_file_path": log_file_path,
            "debug_module_choices": list(_ADV_DEBUG_MODULE_CHOICES),
            "debug_modules": debug_modules,
            "db_retention_days": retention_days,
            "runtime_health": runtime_health,
            "runtime_instance_id": app.state.ui_runtime_instance_id,
            "autostart_note": "If you manually run 'python Sensorius.py', stop that instance before enabling auto-start to avoid duplicate instances.",
            "autostart_scope_note": "macOS user-level launchctl is default. System-level may require admin privileges.",
        })

    @router.post("/advanced/automations/test-bd-transition", response_class=JSONResponse)
    async def test_biodynamic_transition_alert(request: Request):
        """Broadcast a simulated BD transition without changing calendar or rule state."""
        service = getattr(request.app.state, "biodynamic_calendar_service", None)
        broadcaster = getattr(request.app.state, "switch_broadcast", None)
        if service is None or not callable(getattr(service, "current_transition_sync", None)):
            return JSONResponse(
                {"ok": False, "error": "Biodynamic calendar service is unavailable."},
                status_code=503,
            )
        if not callable(broadcaster):
            return JSONResponse(
                {"ok": False, "error": "Dashboard broadcaster is unavailable."},
                status_code=503,
            )

        current = await asyncio.to_thread(service.current_transition_sync)
        if not isinstance(current, dict) or not current.get("transition_at"):
            return JSONResponse(
                {"ok": False, "error": "Current biodynamic segment is unavailable."},
                status_code=503,
            )

        segment = {
            "sign": str(current.get("sign") or ""),
            "element": str(current.get("element") or ""),
            "plant_part": str(current.get("plant_part") or ""),
            "color": str(current.get("color") or ""),
            "accent": str(current.get("accent") or ""),
        }
        payload = {
            "type": "bd_transition",
            "test": True,
            "transition_at": datetime.now().astimezone().isoformat(),
            "from": dict(segment),
            "to": dict(segment),
        }
        await broadcaster(payload)
        return JSONResponse({"ok": True, "message": "Test BD transition broadcast.", "event": payload})

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
            archive_path = await asyncio.to_thread(data_logger.create_database_archive)
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
        global _sensor_ids_cache_payload, _sensor_ids_cache_until
        now_mono = time.monotonic()
        if _sensor_ids_cache_payload is not None and _sensor_ids_cache_until > now_mono:
            return JSONResponse(list(_sensor_ids_cache_payload))

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
            sm = getattr(app.state, "sensor_map", None)
            if sm is None:
                from . import saiWebRoutes as routes
                sm = getattr(routes, "sensor_map", None)
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
            discovered = [d for d in (mqtt_ingest.get_known_devices() or []) if not _is_switch_id(d)]
            discovered = [_strip_local_suffix(d) for d in discovered]
        except Exception:
            discovered = []
        if DEBUG:
            printDM(f"[{MODULE}] #2 - mqtt sensors {discovered}", location=MODULE)

        # 3) ids that have already logged rows
        logged_ids = []
        try:
            logged_ids = data_logger.get_available_sensors() or []
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
        _sensor_ids_cache_payload = list(merged)
        _sensor_ids_cache_until = time.monotonic() + _SENSOR_IDS_CACHE_TTL_SEC
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
    async def get_metrics(sensor_id: str = Query(...)):
        # Try 1: names seen in DB (distinct metrics for this sensor)
        try:
            names = data_logger.get_available_metrics(sensor_id) or []
            if names:
                return JSONResponse({name: None for name in names})
        except Exception:
            pass

        # Try 2: live local sensor's declared measurements
        try:
            smap = getattr(app.state, "sensor_map", None)
            if smap is None:
                from . import saiWebRoutes as routes
                smap = getattr(routes, "sensor_map", None)
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
                        return JSONResponse({name: None for name in names})
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
    
    def _is_remote_nodus_type(sensor_type: str | None) -> bool:
        return str(sensor_type or "").strip().lower() in ("picow", "pico2w", "nodus", "remote")

    def _load_sensor_settings_with_direct_local_repair(manager: SensorSettingsManager, sensor_id: str) -> dict:
        doc = manager.load(sensor_id) or {}
        if is_direct_local_sensor_id(sensor_id):
            try:
                if manager.ensure_direct_local_type(sensor_id):
                    doc = manager.load(sensor_id) or {}
            except Exception as exc:
                if DEBUG:
                    printDM(f"Direct local sensor type repair failed for {sensor_id}: {exc}", location=MODULE)
        return doc

    def _sensor_id_looks_direct_local(sensor_id: str | None) -> bool:
        return is_direct_local_sensor_id(sensor_id)

    def _active_local_sensor_controller(sensor_id: str | None):
        sid_norm = normalize_sensor_id(str(sensor_id or ""))
        if not sid_norm:
            return None

        def _controller_id(candidate) -> str:
            sensor_obj = getattr(candidate, "sensor", None)
            for obj in (candidate, sensor_obj):
                if obj is None:
                    continue
                value = getattr(obj, "sensor_id", None)
                if value:
                    return normalize_sensor_id(str(value))
            return ""

        maps = [getattr(app.state, "sensor_map", None), globals().get("sensor_map")]
        for smap in maps:
            if not smap:
                continue
            if isinstance(smap, dict):
                direct = smap.get(sid_norm) or smap.get(sid_norm.lower()) or smap.get(sid_norm.upper())
                if direct:
                    return direct
                iterable = list(smap.values())
            else:
                try:
                    iterable = list(smap)
                except TypeError:
                    iterable = []
            for candidate in iterable:
                if _controller_id(candidate).lower() == sid_norm.lower():
                    return candidate
        return None

    def _sensor_uses_remote_calibration(sensor_id: str | None, sensor_block: dict | None) -> bool:
        if _active_local_sensor_controller(sensor_id) is not None:
            return False
        if _sensor_id_looks_direct_local(sensor_id):
            return False
        block = sensor_block if isinstance(sensor_block, dict) else {}
        sensor_type = str(block.get("TYPE", block.get("type", "")) or "").strip().lower()
        return _is_remote_nodus_type(sensor_type)

    _SOIL_TEMP_CAL_KEYS = ("SOIL_TEMP_CAL_VAL",)
    _SOIL_MOISTURE_CAL_KEYS = ("SOIL_MOIST_CAL_VAL", "SOIL_TEMP_MOIST_VAL")
    _SOIL_PH_CAL_KEYS = ("SOIL_PH_CAL_VAL",)
    _SOIL_EC_CAL_KEYS = ("SOIL_EC_CAL_VAL",)

    def _soil_offset_value(section: dict, keys: tuple[str, ...], _get_float, default: float = 0.0) -> float:
        if not isinstance(section, dict):
            return default
        for key in keys:
            if key in section:
                return _get_float(section, key, default)
        return default

    def _soil_device_offsets(device_section: dict, _get_float) -> list[dict]:
        return [
            {
                "key": "soil_moisture_offset",
                "label": "Soil Moisture",
                "unit": "%",
                "value": _soil_offset_value(device_section, _SOIL_MOISTURE_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_temp_offset",
                "label": "Soil Temperature",
                "unit": "°C",
                "value": _soil_offset_value(device_section, _SOIL_TEMP_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_ph_offset",
                "label": "Soil pH",
                "unit": "pH",
                "value": _soil_offset_value(device_section, _SOIL_PH_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_ec_offset",
                "label": "Soil EC",
                "unit": "",
                "value": _soil_offset_value(device_section, _SOIL_EC_CAL_KEYS, _get_float),
            },
        ]

    def _system_altitude_meters() -> float | None:
        def _coerce_altitude(raw) -> float | None:
            try:
                if raw is None or str(raw).strip() == "":
                    return None
                altitude = float(raw)
                return altitude if -500.0 <= altitude <= 10000.0 else None
            except Exception:
                return None

        for source in (
            lambda: saiSettings(apply_live=False).get_setting("Astral", "ALTITUDE", ""),
            lambda: settings.get_setting("Astral", "ALTITUDE", ""),
        ):
            try:
                altitude = _coerce_altitude(source())
                if altitude is not None:
                    return altitude
            except Exception:
                continue
        return None

    def _device_supports_altitude_calibration(device_kind: str) -> bool:
        return str(device_kind or "").strip().lower() in {
            "aqi",
            "bme680",
            "bme688",
            "co2",
            "scd30",
            "scd4x",
            "vpd",
            "avpd",
            "bme280",
            "apvpd",
        }

    def _append_system_altitude_calibration(device_offsets: list[dict], device_kind: str) -> None:
        altitude = _system_altitude_meters()
        if altitude is None or not _device_supports_altitude_calibration(device_kind):
            return
        device_offsets.append(
            {
                "key": "Calibration.Device.ALTITUDE_METERS",
                "label": "System Altitude",
                "unit": "m",
                "value": altitude,
                "readonly": True,
                "force_send": True,
                "title": "Altitude is edited in General Settings.",
            }
        )

    def _apply_device_offsets_shadow(sensor_id: str, device_kind: str, offsets: list[dict]) -> list[str]:
        from collections import OrderedDict
        from collections import OrderedDict as _OD

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = mgr.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            doc = OrderedDict()

        calib = doc.get("Calibration")
        if not isinstance(calib, dict):
            calib = _OD()
            doc["Calibration"] = calib

        def _set_path(path: str, value: float) -> None:
            parts = [p for p in path.split(".") if p]
            if not parts:
                return
            cur = doc
            for seg in parts[:-1]:
                sub = cur.get(seg)
                if not isinstance(sub, dict):
                    sub = _OD()
                    cur[seg] = sub
                cur = sub
            cur[parts[-1]] = value

        applied_keys: list[str] = []
        for item in offsets or []:
            key = str(item.get("key") or "").strip()
            raw_val = item.get("value", 0)
            try:
                val = float(raw_val)
            except Exception:
                continue

            if device_kind == "apvpd" and key in ("ambient_temp_offset", "ambient_rh_offset"):
                if key == "ambient_temp_offset":
                    calib["APVPD_TEMP_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_TEMP_CAL_VAL")
                elif key == "ambient_rh_offset":
                    calib["APVPD_RH_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_RH_CAL_VAL")
                continue

            if device_kind == "soil" and key in (
                "soil_moisture_offset",
                "soil_temp_offset",
                "soil_ph_offset",
                "soil_ec_offset",
            ):
                dev = calib.get("Device")
                if not isinstance(dev, dict):
                    dev = _OD()
                    calib["Device"] = dev

                if key == "soil_moisture_offset":
                    dev["SOIL_MOIST_CAL_VAL"] = val
                    dev["SOIL_TEMP_MOIST_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_MOIST_CAL_VAL")
                elif key == "soil_temp_offset":
                    dev["SOIL_TEMP_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_TEMP_CAL_VAL")
                elif key == "soil_ph_offset":
                    dev["SOIL_PH_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_PH_CAL_VAL")
                elif key == "soil_ec_offset":
                    dev["SOIL_EC_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_EC_CAL_VAL")
                continue

            if not key:
                continue
            _set_path(key, val)
            applied_keys.append(key)

        mgr.save(sensor_id, doc)
        return applied_keys

    def _get_current_device_offset_value(doc: dict, device_kind: str, key: str) -> float | None:
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        calib = doc.get("Calibration") if isinstance(doc, dict) else {}
        if not isinstance(calib, dict):
            calib = {}

        if device_kind == "apvpd" and key in ("ambient_temp_offset", "ambient_rh_offset"):
            if key == "ambient_temp_offset":
                return _to_float(calib.get("APVPD_TEMP_CAL_VAL"))
            if key == "ambient_rh_offset":
                return _to_float(calib.get("APVPD_RH_CAL_VAL"))

        device_cal = calib.get("Device") if isinstance(calib.get("Device"), dict) else {}
        if device_kind == "soil" and key in (
            "soil_moisture_offset",
            "soil_temp_offset",
            "soil_ph_offset",
            "soil_ec_offset",
        ):
            soil_key_map = {
                "soil_moisture_offset": _SOIL_MOISTURE_CAL_KEYS,
                "soil_temp_offset": _SOIL_TEMP_CAL_KEYS,
                "soil_ph_offset": _SOIL_PH_CAL_KEYS,
                "soil_ec_offset": _SOIL_EC_CAL_KEYS,
            }
            for soil_key in soil_key_map[key]:
                current = _to_float(device_cal.get(soil_key))
                if current is not None:
                    return current
            return None

        cur = doc if isinstance(doc, dict) else {}
        for seg in [p for p in str(key or "").split(".") if p]:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(seg)
        return _to_float(cur)

    def _filter_changed_device_offsets(doc: dict, device_kind: str, offsets: list[dict]) -> list[dict]:
        changed: list[dict] = []
        for item in offsets or []:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            force_send = key == "Calibration.Device.ALTITUDE_METERS" and bool(
                item.get("force") or item.get("force_send") or item.get("always_send")
            )
            if key == "Calibration.Device.ALTITUDE_METERS":
                system_altitude = _system_altitude_meters()
                if system_altitude is None:
                    continue
                new_val = system_altitude
            else:
                try:
                    new_val = float(item.get("value", 0))
                except Exception:
                    continue
            current = _get_current_device_offset_value(doc, device_kind, key)
            if not force_send and current is not None and abs(new_val - current) < 1e-9:
                continue
            changed.append({"key": key, "value": new_val})
        return changed

    def _mqtt_calibration_payload_from_offsets(offsets: list[dict]) -> dict:
        return {"offsets": [dict(item) for item in (offsets or [])]}

    def _apply_remote_calibration_patch_shadow(sensor_id: str, patch: dict | None) -> list[str]:
        from collections import OrderedDict

        if not isinstance(patch, dict):
            return []

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = mgr.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            doc = OrderedDict()

        def _ensure_block(parent: dict, name: str) -> dict:
            block = parent.get(name)
            if not isinstance(block, dict):
                block = OrderedDict()
                parent[name] = block
            return block

        changed = False
        applied_keys: list[str] = []
        for update in (patch.get("updates") or []):
            if not isinstance(update, dict):
                continue
            section = str(update.get("section") or "").strip()
            key = str(update.get("key") or "").strip()
            if not key:
                continue
            section_norm = section.lower()
            if section_norm != "calibration" and not section_norm.startswith("calibration."):
                continue

            current = doc
            for segment in [seg for seg in section.split(".") if seg]:
                current = _ensure_block(current, segment)
            value = update.get("value")
            if current.get(key) != value:
                current[key] = value
                changed = True
            applied_keys.append(f"{section}.{key}" if section else key)

        if changed:
            mgr.save(sensor_id, doc)
        return applied_keys

    async def _sync_remote_calibration_shadow(sensor_id: str, message_id: str, *, timeout: float = 3.0) -> tuple[list[str], bool]:
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        if not ingest or not hasattr(ingest, "wait_for_nodus_meta_patch"):
            return [], False
        patch = await ingest.wait_for_nodus_meta_patch(
            message_id,
            source="calibration_set",
            timeout=timeout,
        )
        if not isinstance(patch, dict):
            return [], False
        return _apply_remote_calibration_patch_shadow(sensor_id, patch), True

    async def _remote_calibration_meta_patch_fallback(message_id: str, action: str, *, timeout: float = 3.0) -> dict | None:
        if str(action or "").strip().lower() not in {"apply", "set", "update"}:
            return None
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        if not ingest or not hasattr(ingest, "wait_for_nodus_meta_patch"):
            return None
        patch = await ingest.wait_for_nodus_meta_patch(
            message_id,
            source="calibration_set",
            timeout=timeout,
        )
        if not isinstance(patch, dict):
            return None
        return {
            "message_id": message_id,
            "applied": True,
            "updated": len(patch.get("updates") or []),
            "error": "",
            "meta_patch_fallback": True,
        }

    def _resolve_remote_sensor_target(sensor_id: str) -> dict[str, str]:
        sid = str(sensor_id or "").strip()
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        resolver = getattr(ingest, "resolve_nodus_sensor_target", None)
        target = resolver(sid) if callable(resolver) else {}
        device_id = str((target or {}).get("device_id") or "").strip()
        config_file = str((target or {}).get("config_file") or "").strip()

        try:
            doc = SensorSettingsManager("sensor_settings").load(sid) or {}
        except Exception:
            doc = {}
        nodus_block = doc.get("Nodus") if isinstance(doc.get("Nodus"), dict) else {}
        sensor_block = doc.get("Sensor") if isinstance(doc.get("Sensor"), dict) else {}
        device_id = (
            device_id
            or str(nodus_block.get("DEVICE_ID") or "").strip()
            or sid
        )
        config_file = config_file or str(
            nodus_block.get("CONFIG_FILE") or ""
        ).strip()
        if not config_file:
            sensor_kind = str(sensor_block.get("DEVICE") or "").strip().lower()
            config_file = (
                "sensor_soil.toml"
                if sensor_kind.startswith("soil")
                or sensor_kind in {"rs485", "modbus"}
                else "sensor_i2c.toml"
            )
        return {
            "sensor_id": sid,
            "device_id": device_id,
            "config_file": config_file,
        }

    async def _publish_remote_calibration_command(
        sensor_id: str,
        *,
        action: str,
        payload: dict | None = None,
        ack_timeout: float = _NODUS_CALIBRATION_ACK_TIMEOUT_SEC,
        result_timeout: float = _NODUS_CALIBRATION_RESULT_TIMEOUT_SEC,
    ) -> tuple[bool, str, dict | None, dict | None]:
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_calibration"):
            return False, "MQTT ingest unavailable", None, None

        target = _resolve_remote_sensor_target(sensor_id)
        publish_result = ingest.publish_nodus_calibration(
            target["device_id"],
            action=action,
            payload=payload,
            sensor_id=target["sensor_id"],
            name=target["config_file"],
        )
        if not bool(publish_result.get("ok", False)):
            return False, "Failed to publish calibration command", None, None

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_calibration_ack(message_id, timeout=ack_timeout)
        if not ack or not bool(ack.get("accepted", False)):
            fallback = await _remote_calibration_meta_patch_fallback(message_id, action)
            if fallback is not None:
                return True, "", ack, fallback
            return False, "Calibration command was not acknowledged", ack, None

        result = await ingest.wait_for_calibration_result(message_id, timeout=result_timeout)
        if result is None:
            fallback = await _remote_calibration_meta_patch_fallback(message_id, action)
            if fallback is not None:
                return True, "", ack, fallback
            return False, "Timed out waiting for calibration result", ack, None
        return True, "", ack, result

    async def _publish_remote_device_calibration_offsets(sensor_id: str, offsets: list[dict]) -> tuple[bool, str, int, list[str], bool]:
        target = _resolve_remote_sensor_target(sensor_id)
        target_device = str(target.get("device_id") or "").strip()
        if not target_device:
            return False, "Missing sensor_id.", 400, [], False

        applied_keys: list[str] = []
        shadow_synced = True
        completed_count = 0
        total_count = len(offsets or [])

        async with _get_host_lock(target_device):
            for offset in offsets or []:
                ok, err, ack, result = await _publish_remote_calibration_command(
                    sensor_id,
                    action="apply",
                    payload=_mqtt_calibration_payload_from_offsets([offset]),
                    ack_timeout=_NODUS_CALIBRATION_ACK_TIMEOUT_SEC,
                    result_timeout=_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC,
                )
                if not ok:
                    if completed_count:
                        err = f"{err} after applying {completed_count} of {total_count} calibration value(s)."
                    return False, err, 502, applied_keys, shadow_synced
                if not bool((result or {}).get("applied", False)):
                    err = str((result or {}).get("error") or "Calibration update was rejected.")
                    if completed_count:
                        err = f"{err} after applying {completed_count} of {total_count} calibration value(s)."
                    return False, err, 400, applied_keys, shadow_synced

                completed_count += 1
                message_id = str((result or {}).get("message_id") or (ack or {}).get("message_id") or "").strip()
                one_applied, one_shadow_synced = await _sync_remote_calibration_shadow(sensor_id, message_id)
                applied_keys.extend(one_applied)
                if not one_shadow_synced:
                    shadow_synced = False

        return True, "", 200, applied_keys, shadow_synced

    async def _publish_remote_switch_last_state(
        switch_id: str,
        *,
        channel_index: int,
        channel_id: str,
        channel_label: str,
        new_state: bool,
        system_mgr=None,
        system_root: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        if int(channel_index or 0) <= 0:
            return False, "Missing switch channel index"
        ok = await push_nodus_settings_batch(
            device_id=str(switch_id or "").strip(),
            device_type="switch",
            setting_file_key="switch",
            updates=[("Switch", f"SWITCH_{int(channel_index)}_LAST_STATE", bool(new_state))],
            sensor_file_name=None,
            system_mgr=system_mgr,
            system_root=system_root,
            sys_host_index=sys_host_index,
        )
        if not ok:
            return False, "Failed to publish remote switch config update"
        return True, ""

    def _extract_soil_ph_from_sample(sample: dict, fallback_offset: float = 0.0) -> float | None:
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        raw_ph = _to_float(sample.get("raw_ph"))
        if raw_ph is not None:
            return raw_ph

        corrected_ph = _to_float(sample.get("corrected_ph"))
        if corrected_ph is None:
            values = sample.get("values") if isinstance(sample.get("values"), dict) else {}
            corrected_ph = _to_float(values.get("Soil-pH"))
        if corrected_ph is None:
            return None

        offset = _to_float(sample.get("soil_ph_offset"))
        if offset is None:
            offset = fallback_offset
        return corrected_ph - float(offset or 0.0)

    async def _run_remote_soil_ph_session(sensor_id: str, *, reference_ph: float, sample_count: int = 6, sample_interval_s: float = 10.0) -> tuple[bool, str, dict]:
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_calibration"):
            return False, "MQTT ingest unavailable", {}
        if not hasattr(ingest, "wait_for_calibration_samples"):
            return False, "MQTT ingest does not support calibration sample collection", {}

        session_payload = {
            "reference_ph": float(reference_ph),
            "sample_count": int(sample_count),
            "sample_interval_s": float(sample_interval_s),
        }
        publish_result = ingest.publish_nodus_calibration(
            sensor_id,
            action="soil_ph_session_start",
            payload=session_payload,
        )
        if not bool(publish_result.get("ok", False)):
            return False, "Failed to publish soil pH session start", {}

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_calibration_ack(message_id, timeout=_NODUS_CALIBRATION_ACK_TIMEOUT_SEC)
        if not ack or not bool(ack.get("accepted", False)):
            return False, "Calibration command was not acknowledged", {"ack": ack, "message_id": message_id}

        start_result = await ingest.wait_for_calibration_result(message_id, timeout=_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC)
        if start_result is None:
            return False, "Timed out waiting for soil pH session start result", {"ack": ack, "message_id": message_id}
        if not bool(start_result.get("applied", False)) or not bool(start_result.get("started", False)):
            return False, str(start_result.get("error") or "Soil pH session was rejected."), {
                "ack": ack,
                "message_id": message_id,
                "result": start_result,
            }

        expected_count_raw = start_result.get("sample_count", session_payload["sample_count"])
        interval_raw = start_result.get("sample_interval_s", session_payload["sample_interval_s"])
        try:
            expected_count = max(int(expected_count_raw), 1)
        except Exception:
            expected_count = int(session_payload["sample_count"])
        try:
            sample_interval = max(float(interval_raw), 0.0)
        except Exception:
            sample_interval = float(session_payload["sample_interval_s"])

        sample_timeout = max(15.0, (expected_count * sample_interval) + 10.0)
        samples = await ingest.wait_for_calibration_samples(
            message_id,
            expected_count=expected_count,
            timeout=sample_timeout,
        )
        if len(samples) < expected_count:
            return False, f"Timed out waiting for soil pH samples ({len(samples)}/{expected_count})", {
                "ack": ack,
                "message_id": message_id,
                "result": start_result,
                "samples": samples,
            }

        return True, "", {
            "ack": ack,
            "message_id": message_id,
            "result": start_result,
            "samples": samples,
            "sample_count": expected_count,
            "sample_interval_s": sample_interval,
        }


    # --- Edit Sensor (modal / template) ---
    async def _build_sensor_statistics_payload(sensor_id_value: str) -> dict[str, Any]:
        normalized_id = normalize_sensor_id(str(sensor_id_value or ""))
        offline_events_24h = 0
        last_offline_epoch = None
        data_packets_received = 0
        last_packet_epoch = None
        stats_now = time.time()
        base_id = normalize_hostname_base(normalized_id)
        sensor_stat_aliases = [normalized_id]
        if base_id:
            sensor_stat_aliases.extend([base_id, mdns_hostname(base_id)])

        try:
            counter = getattr(data_logger, "get_sensor_offline_event_count", None)
            if callable(counter):
                offline_events_24h = await asyncio.to_thread(
                    lambda: counter(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            offline_events_24h = 0
        try:
            last_offline = getattr(data_logger, "get_sensor_last_offline_event_epoch", None)
            if callable(last_offline):
                last_offline_epoch = await asyncio.to_thread(
                    lambda: last_offline(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            last_offline_epoch = None
        try:
            packet_counter = getattr(data_logger, "get_sensor_packet_count", None)
            if callable(packet_counter):
                data_packets_received = await asyncio.to_thread(
                    lambda: packet_counter(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            data_packets_received = 0
        try:
            last_packet = getattr(data_logger, "get_sensor_last_packet_epoch", None)
            if callable(last_packet):
                last_packet_epoch = await asyncio.to_thread(
                    lambda: last_packet(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            last_packet_epoch = None

        return {
            "offline_events_24h": offline_events_24h,
            "last_offline_epoch": last_offline_epoch,
            "uptime_since_last_offline_label": _format_stats_duration(
                (stats_now - float(last_offline_epoch))
                if last_offline_epoch is not None
                else None
            ),
            "last_offline_event_label": _format_stats_timestamp(
                last_offline_epoch,
                empty="No offline events",
            ),
            "data_packets_received": data_packets_received,
            "last_packet_epoch": last_packet_epoch,
            "last_packet_received_label": _format_stats_age(
                last_packet_epoch,
                empty="No packets",
            ),
        }

    @router.get("/sensor-settings/statistics", response_class=JSONResponse)
    async def sensor_settings_statistics(
        request: Request,
        sensor_id: str = Query(...),
    ):
        _require_protected_access(request, require_csrf=True)
        normalized_id = normalize_sensor_id(sensor_id)
        if not normalized_id:
            return JSONResponse({"ok": False, "error": "Missing sensor_id"}, status_code=400)
        payload = await _build_sensor_statistics_payload(normalized_id)
        payload["ok"] = True
        payload["sensor_id"] = normalized_id
        return JSONResponse(payload)

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
                    ingest_for_shadow = getattr(request.app.state, "mqtt_ingest", None) or mqtt_ingest
                    expected_metrics = _nodus_expected_metrics_for_sensor(normalized_id, ingest_for_shadow)
                    observed_metrics: list[str] = []
                    try:
                        observed_metrics = list(
                            await asyncio.to_thread(data_logger.get_available_metrics, normalized_id)
                            or []
                        )
                    except Exception:
                        observed_metrics = []
                    if not observed_metrics:
                        try:
                            latest_values = await asyncio.to_thread(data_logger.get_latest_values, normalized_id)
                            if isinstance(latest_values, dict):
                                observed_metrics = [str(k) for k in latest_values.keys() if str(k or "").strip()]
                        except Exception:
                            observed_metrics = []

                    known_live_sensor = bool(expected_metrics or observed_metrics)
                    if not known_live_sensor:
                        try:
                            known_ids = list(await asyncio.to_thread(data_logger.get_available_sensors) or [])
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
                for metric in await asyncio.to_thread(data_logger.get_available_metrics, normalized_id)
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

            ingest = getattr(request.app.state, "mqtt_ingest", None) or mqtt_ingest
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

            cal_mgr = CalibrationManager(data_logger, manager)
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
                ambient_temp_offset=ambient_temp_offset,
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
            page.append(f"<link rel='stylesheet' href='/ui_static/css/app.css?v={SAI_APP_VERSION}'>")
            page.append("<script src='/ui_static/js/sensor_settings_modal.js'></script>")
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
                    mqtt_ingest.add_client(host)   # marks 'pending' and forces an expedited check
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
        _SENSOR_LOCATION_CACHE.pop(old_id, None)

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
                ingest_for_target = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
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

    @router.post("/calibrate")
    async def calibrate_sensor(sensor_id: str = Query(...)):
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager
        import asyncio

        def _get_sensor_map():
            sm = getattr(app.state, "sensor_map", None)
            if sm is None:
                from . import saiWebRoutes as routes
                sm = getattr(routes, "sensor_map", None)
            return sm

        def _resolve_sensor(smap, sid: str):
            if smap is None:
                return None
            sid_norm = normalize_sensor_id(sid)
            if hasattr(smap, "get"):
                hit = smap.get(sid_norm) or smap.get(sid_norm.lower()) or smap.get(sid_norm.upper())
                if hit:
                    return hit
                try:
                    for k, v in getattr(smap, "items")():
                        if str(k).lower() == sid_norm.lower():
                            return v
                except Exception:
                    pass
            try:
                for item in smap:
                    cand = None
                    controller = None
                    if hasattr(item, "sensor") and hasattr(item.sensor, "sensor_id"):
                        cand = getattr(item.sensor, "sensor_id", None)
                        controller = item
                    elif hasattr(item, "sensor_id"):
                        cand = getattr(item, "sensor_id", None)
                        controller = item
                    elif isinstance(item, (tuple, list)) and item:
                        maybe = item[0]
                        if hasattr(maybe, "sensor") and hasattr(maybe.sensor, "sensor_id"):
                            cand = getattr(maybe.sensor, "sensor_id", None)
                            controller = maybe
                        elif hasattr(maybe, "sensor_id"):
                            cand = getattr(maybe, "sensor_id", None)
                            controller = maybe
                    if cand and normalize_sensor_id(str(cand)) == sid_norm:
                        return controller
            except TypeError:
                pass
            return None

        # ---------- 1) Local controller (Pi-attached) ----------
        sensor_map = _get_sensor_map()
        controller = _resolve_sensor(sensor_map, sensor_id)

        if controller and hasattr(controller, "sensor") and hasattr(controller.sensor, "calibrate_plant_sensor"):
            try:
                # Don’t block the HTTP POST for ~60s; fire-and-forget
                _ = asyncio.create_task(controller.sensor.calibrate_plant_sensor())
                return JSONResponse({"status": "started", "source": "local"})
            except Exception as e:
                printDM(f"[calibrate_sensor] local exception: {e}", location="saiWebRoutes")
                return JSONResponse({"status": "error", "message": "Failed to start calibration"}, status_code=500)

        # ---------- 2) Remote Nodus over MQTT ----------
        try:
            mgr = SensorSettingsManager("sensor_settings")
            sid_norm = normalize_sensor_id(sensor_id)
            settings_dict = _load_sensor_settings_with_direct_local_repair(mgr, sid_norm)
            sensor_block = (settings_dict.get("Sensor") or settings_dict.get("sensor") or {})
            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            if _sensor_uses_remote_calibration(sid_norm, sensor_block):
                ok, err, _ack, result = await _publish_remote_calibration_command(
                    sid_norm,
                    action="start",
                    payload=None,
                    ack_timeout=3.0,
                    result_timeout=6.0,
                )
                if not ok:
                    return JSONResponse({"status": "error", "message": err}, status_code=502)

                if bool(result.get("started", False)) or bool(result.get("applied", False)):
                    return JSONResponse({"status": "started", "source": "mqtt", "message_id": result.get("message_id")})

                return JSONResponse(
                    {
                        "status": "error",
                        "message": str(result.get("error") or "Calibration did not start"),
                    },
                    status_code=400,
                )

        except Exception as e:
            printDM(f"[calibrate_sensor] mqtt lookup exception: {e}", location="saiWebRoutes")

        # ---------- 3) Unknown ----------
        return JSONResponse({"status": "error", "message": f"Unknown or unsupported sensor_id: {sensor_id}"}, status_code=404)

    @router.get("/calibration-status")
    async def get_calibration_status(sensor_id: str = Query(...)):
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager

        try:
            sid_norm = normalize_sensor_id(sensor_id)
            mgr = SensorSettingsManager("sensor_settings")
            settings = _load_sensor_settings_with_direct_local_repair(mgr, sid_norm)

            sensor_block = (settings.get("Sensor") or settings.get("sensor") or {})
            cal_block = (settings.get("Calibration") or settings.get("calibration") or {})

            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            uses_remote_calibration = _sensor_uses_remote_calibration(sid_norm, sensor_block)
            hostname = (sensor_block.get("HOSTNAME") or sensor_block.get("hostname") or "").strip()
            if not hostname:
                # fallback: keep full SENSOR_ID as host hint
                host_src = str(sensor_block.get("SENSOR_ID", sensor_block.get("sensor_id", "") ) or "")
                hostname = host_src if host_src else ""

            # Small helper to pull offsets in a way that matches your APVPD schema
            def _extract_offsets():
                temp_offset_val = cal_block.get("APVPD_TEMP_CAL_VAL")
                rh_offset_val = cal_block.get("APVPD_RH_CAL_VAL")

                # fallback to generic keys, then legacy sensor-level keys
                if temp_offset_val is None:
                    temp_offset_val = cal_block.get("TEMP_OFFSET")
                if rh_offset_val is None:
                    rh_offset_val = cal_block.get("RH_OFFSET")

                if temp_offset_val is None:
                    temp_offset_val = sensor_block.get("THP280_PLANT_TEMP_CAL")
                if rh_offset_val is None:
                    rh_offset_val = sensor_block.get("THP280_PLANT_RH_CAL")

                try:
                    temp_offset = float(temp_offset_val or 0.0)
                except Exception:
                    temp_offset = 0.0
                try:
                    rh_offset = float(rh_offset_val or 0.0)
                except Exception:
                    rh_offset = 0.0

                return temp_offset, rh_offset

            # ───────────────── Pi-attached sensors ─────────────────
            if not uses_remote_calibration:
                ctrl = _active_local_sensor_controller(sid_norm)
                if ctrl and hasattr(ctrl, "sensor"):
                    state = getattr(ctrl.sensor, "is_calibrated", "Not Calibrated")
                    toff, roff = _extract_offsets()
                    return JSONResponse(
                        {
                            "status": "ok",
                            "calibrated": state,
                            "temp_offset": toff,
                            "rh_offset": roff,
                        }
                    )

            # ───────────────── Nodus / remote sensors ─────────────────
            ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
            mqtt_state = None
            if uses_remote_calibration and ingest and hasattr(ingest, "get_nodus_calibration_state"):
                mqtt_state = ingest.get_nodus_calibration_state(sid_norm)
            if mqtt_state:
                progress_state = mqtt_state.get("progress") if isinstance(mqtt_state.get("progress"), dict) else None
                status_state = mqtt_state.get("status") if isinstance(mqtt_state.get("status"), dict) else None
                result_state = mqtt_state.get("result") if isinstance(mqtt_state.get("result"), dict) else None
                live = progress_state or status_state or result_state or {}

                temp_offset = live.get("temp_offset")
                rh_offset = live.get("rh_offset")
                if temp_offset is None or rh_offset is None:
                    temp_offset, rh_offset = _extract_offsets()

                status_text = str(live.get("status") or "").strip().lower()
                calibrated_raw = live.get("calibrated")
                if status_text == "in_progress":
                    body = {
                        "status": "ok",
                        "calibrated": "Calibrating",
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                    if live.get("sample_index") is not None and live.get("sample_total") is not None:
                        body["sample_index"] = live.get("sample_index")
                        body["sample_total"] = live.get("sample_total")
                    return JSONResponse(body)

                if isinstance(calibrated_raw, bool):
                    calibrated_label = "Calibrated" if calibrated_raw else "Not Calibrated"
                elif status_text in {"calibrated", "not_calibrated", "idle", "unavailable"}:
                    calibrated_label = "Calibrated" if status_text == "calibrated" else "Not Calibrated"
                else:
                    calibrated_label = "Not Calibrated"

                response = {
                    "status": "ok",
                    "calibrated": calibrated_label,
                    "temp_offset": temp_offset,
                    "rh_offset": rh_offset,
                }
                err_text = str((result_state or {}).get("error") or live.get("error") or "").strip()
                if err_text:
                    response["error"] = err_text
                return JSONResponse(response)

            # 1) Prefer in-memory state; it carries phase + sample counts.
            state = _calibration_progress_cache.get(sid_norm)
            if state:
                phase = str(state.get("phase", "") or "").strip().lower()
                sample_index = state.get("sample_index")
                sample_total = state.get("sample_total")
                cached_calibrated = state.get("calibrated")

                temp_offset, rh_offset = _extract_offsets()

                # While in progress, always report "Calibrating"
                if phase == "in_progress":
                    response_body = {
                        "status": "ok",
                        "calibrated": "Calibrating",
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                    if sample_index is not None and sample_total is not None:
                        response_body["sample_index"] = sample_index
                        response_body["sample_total"] = sample_total
                    return JSONResponse(response_body)

                # After completion (phase == "done"), prefer the in-memory flag
                if phase == "done":
                    if isinstance(cached_calibrated, bool):
                        status_label = "Calibrated" if cached_calibrated else "Not Calibrated"
                    else:
                        # Fallback to TOML if flag not set for some reason
                        temp_offset, rh_offset = _extract_offsets()
                        cal_flag = cal_block.get("CALIBRATED", cal_block.get("calibrated"))
                        if cal_flag is None:
                            cal_flag = sensor_block.get("CALIBRATED", sensor_block.get("calibrated", False))
                        cal_flag = bool(cal_flag)
                        status_label = "Calibrated" if cal_flag else "Not Calibrated"

                    return JSONResponse(
                        {
                            "status": "ok",
                            "calibrated": status_label,
                            "temp_offset": temp_offset,
                            "rh_offset": rh_offset,
                        }
                    )

            # 2) No in-memory state → use final persisted calibration info only
            has_cal_section = bool(cal_block)
            has_cal_flag = (
                "CALIBRATED" in cal_block
                or "calibrated" in cal_block
                or "CALIBRATED" in sensor_block
                or "calibrated" in sensor_block
            )

            if has_cal_section or has_cal_flag:
                temp_offset, rh_offset = _extract_offsets()

                calibrated_flag = cal_block.get("CALIBRATED", cal_block.get("calibrated"))
                if calibrated_flag is None:
                    calibrated_flag = sensor_block.get("CALIBRATED", sensor_block.get("calibrated", False))
                calibrated_flag = bool(calibrated_flag)

                status_label = "Calibrated" if calibrated_flag else "Not Calibrated"

                return JSONResponse(
                    {
                        "status": "ok",
                        "calibrated": status_label,
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                )

            return JSONResponse(
                {"status": "error", "message": "Unable to determine calibration status"},
                status_code=502,
            )
        except Exception as e:
            printDM(f"[/calibration-status] error: {e}", location="saiWebRoutes")
            return JSONResponse({"status": "error", "message": "Internal error"}, status_code=500)

    @router.post("/sensor-event")
    async def sensor_event(event: dict):
        """
        Handles push events (MQTT/HTTP) from Nodus or local controllers.

        Expected payload from Nodus (APVPDSensor):

        Progress:
        {
            "event": "calibration_progress",
            "payload": {
                "status": "in_progress",
                "sensor_id": "<id>",
                "timestamp": <any>,          # may be ISO string or numeric
                "sample_index": <int>,
                "sample_total": <int>
            }
        }

        Result:
        {
            "event": "calibration_result",
            "payload": {
                "status": "success" | "failed",
                "sensor_id": "<id>",
                "timestamp": <any>,          # may be ISO string or numeric
                "calibrated": true|false,
                "temp_offset": <float>,
                "rh_offset": <float>,
                "error": "<optional>"
            }
        }
        """
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager
        from collections import OrderedDict
        import time

        evt_name = str((event or {}).get("event", "")).strip().lower()
        payload = (event or {}).get("payload", {}) or {}

        printDM(
            f"[sensor-event] received event='{evt_name}' for sensor_id='{payload.get('sensor_id', '')}'",
            location="saiWebRoutes",
        )

        # ── normalize sensor_id ──────────────────────────────────────────────────
        sid_raw = str(payload.get("sensor_id", "") or "").strip()
        if not sid_raw:
            return JSONResponse({"status": "error", "message": "payload.sensor_id required"}, status_code=400)
        sensor_id = normalize_sensor_id(sid_raw)

        # Small helpers
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        def _safe_epoch(_value):
            """Always use local epoch time; ignore device-provided timestamps."""
            return time.time()

        mgr = SensorSettingsManager("sensor_settings")

        # =========================================================================
        # 1) calibration_progress  (IN-MEMORY ONLY, no TOML writes)
        # =========================================================================
        if evt_name == "calibration_progress":
            status_text = str(payload.get("status", "") or "").strip().lower()
            sample_index = payload.get("sample_index")
            sample_total = payload.get("sample_total")
            updated_ts = _safe_epoch(payload.get("timestamp"))

            try:
                sample_index_int = int(sample_index) if sample_index is not None else None
            except Exception:
                sample_index_int = None
            try:
                sample_total_int = int(sample_total) if sample_total is not None else None
            except Exception:
                sample_total_int = None

            updated: dict[str, object] = OrderedDict()

            # In-memory scratchpad only; do not touch TOML here
            try:
                _calibration_progress_cache[sensor_id] = {
                    "phase": "in_progress",
                    "calibrated": None,  # not known yet
                    "sample_index": sample_index_int,
                    "sample_total": sample_total_int,
                    "updated_at": updated_ts,
                }
            except Exception as e:
                printDM(f"[sensor-event] progress cache update failed: {e}", location="saiWebRoutes")

            # Live controller nudge so the UI can show "Calibrating" without a reload
            try:
                from .saiWebRoutes import sensor_map
                ctrl = sensor_map.get(sensor_id) if isinstance(sensor_map, dict) else None
                if ctrl and hasattr(ctrl, "sensor") and hasattr(ctrl.sensor, "is_calibrated"):
                    try:
                        ctrl.sensor.is_calibrated = "Calibrating"
                    except Exception:
                        pass
            except Exception as e:
                printDM(f"[sensor-event] progress live controller update skipped: {e}", location="saiWebRoutes")

            return JSONResponse(
                {
                    "status": "ok",
                    "kind": "progress",
                    "sensor_id": sensor_id,
                    "sample_index": sample_index_int,
                    "sample_total": sample_total_int,
                    "updated": updated,
                }
            )

        # =========================================================================
        # 2) calibration_result  (FINAL; persist to TOML)
        # =========================================================================
        if evt_name != "calibration_result":
            # Unknown / unhandled event type
            return JSONResponse({"status": "ignored"})

        status_text = str(payload.get("status", "") or "").strip().lower()  # "success" | "failed"
        calibrated_flag = bool(payload.get("calibrated", status_text == "success"))
        error_text = str(payload.get("error", "") or "").strip()

        temp_offset = _to_float(payload.get("temp_offset"))
        rh_offset = _to_float(payload.get("rh_offset"))
        updated_ts = _safe_epoch(payload.get("timestamp"))

        # Canonical boolean
        calib_status_bool = bool(calibrated_flag)

        # Update in-memory state to "done" (we no longer pop immediately)
        try:
            _calibration_progress_cache[sensor_id] = {
                "phase": "done",
                "calibrated": calib_status_bool,
                "sample_index": None,
                "sample_total": None,
                "updated_at": updated_ts,
            }
        except Exception as e:
            printDM(f"[sensor-event] result cache update failed: {e}", location="saiWebRoutes")

        updated: dict[str, object] = OrderedDict()

        # ── decide which keys to use based on existing schema ────────────────────
        existing = mgr.load(sensor_id) or {}
        sensor_block = (existing.get("Sensor") or existing.get("sensor") or {})
        cal_block = (existing.get("Calibration") or existing.get("calibration") or {})

        # APVPD sensors: prefer APVPD_TEMP_CAL_VAL / APVPD_RH_CAL_VAL
        temp_key = "Calibration.TEMP_OFFSET"
        rh_key = "Calibration.RH_OFFSET"
        if "APVPD_TEMP_CAL_VAL" in cal_block or "APVPD_RH_CAL_VAL" in cal_block:
            temp_key = "Calibration.APVPD_TEMP_CAL_VAL"
            rh_key = "Calibration.APVPD_RH_CAL_VAL"

        # ── persist to local TOML (FINAL ONLY) ───────────────────────────────────
        try:
            mgr.update_setting(sensor_id, "Calibration.CALIBRATED", calib_status_bool)
            updated["Calibration.CALIBRATED"] = calib_status_bool

            if temp_offset is not None:
                mgr.update_setting(sensor_id, temp_key, temp_offset)
                updated[temp_key] = temp_offset
            if rh_offset is not None:
                mgr.update_setting(sensor_id, rh_key, rh_offset)
                updated[rh_key] = rh_offset

            # We do *not* write STATUS, LAST_EVENT, UPDATED_AT, SAMPLE_*, RESULT, ERROR, etc.

        except Exception as e:
            printDM(f"[sensor-event] settings write failed: {e}", location="saiWebRoutes")
            return JSONResponse({"status": "error", "message": f"settings write failed: {e}"}, status_code=500)

        # ── live controller nudge so UI updates instantly (best-effort) ───────────
        try:
            from .saiWebRoutes import sensor_map
            ctrl = sensor_map.get(sensor_id) if isinstance(sensor_map, dict) else None
            if ctrl and hasattr(ctrl, "sensor"):
                if temp_offset is not None and hasattr(ctrl.sensor, "thp280_plant_temp_cal"):
                    setattr(ctrl.sensor, "thp280_plant_temp_cal", float(temp_offset))
                if rh_offset is not None and hasattr(ctrl.sensor, "thp280_plant_rh_cal"):
                    setattr(ctrl.sensor, "thp280_plant_rh_cal", float(rh_offset))
                if hasattr(ctrl.sensor, "is_calibrated"):
                    try:
                        ctrl.sensor.is_calibrated = "Calibrated" if calib_status_bool else "Not Calibrated"
                    except Exception:
                        pass
        except Exception as e:
            printDM(f"[sensor-event] live controller update skipped: {e}", location="saiWebRoutes")

        return JSONResponse(
            {
                "status": "ok",
                "kind": "result",
                "sensor_id": sensor_id,
                "result": status_text,
                "calibrated": calib_status_bool,
                "updated": updated,
            }
        )

    @router.post("/calibration/device/apply", response_class=JSONResponse)
    async def device_calibration_apply(request: Request):
        """
        Apply per-device calibration offsets for a single sensor.

        Expected JSON body from JS:

            {
              "sensor_id": "co2-i2c-1-sensoria-hub-0",
              "device_kind": "co2",
              "offsets": [
                {"key": "Calibration.Device.TEMP_OFFSET", "value": -0.5},
                {"key": "Calibration.Device.RH_OFFSET",   "value":  1.2},
                {"key": "Calibration.Device.CO2_OFFSET",  "value": -700.0}
              ]
            }

        For non-APVPD devices, each 'key' is a dotted path we apply
        into the sensor's TOML (e.g., Calibration.Device.CO2_OFFSET).

        For APVPD, we also accept the simple keys:
          - ambient_temp_offset -> Calibration.APVPD_TEMP_CAL_VAL
          - ambient_rh_offset   -> Calibration.APVPD_RH_CAL_VAL

        For SoilModbusSensor ("soil" device_kind), we accept soil-specific
        short keys which map into [Calibration.Device]:

          - soil_moisture_offset -> Calibration.Device.SOIL_MOIST_CAL_VAL
                                      (legacy SOIL_TEMP_MOIST_VAL is still read)
          - soil_temp_offset     -> Calibration.Device.SOIL_TEMP_CAL_VAL
          - soil_ph_offset       -> Calibration.Device.SOIL_PH_CAL_VAL
          - soil_ec_offset       -> Calibration.Device.SOIL_EC_CAL_VAL
        """
        from .saiUtils import printDM
        from .saiCalibration import notify_sensor_runtime_of_calibration

        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "message": f"Invalid JSON payload: {exc}"},
                status_code=400,
            )

        sensor_id = str(payload.get("sensor_id") or "").strip()
        device_kind = str(payload.get("device_kind") or "").strip().lower()
        offsets = payload.get("offsets") or []

        if not sensor_id:
            return JSONResponse(
                {"status": "error", "message": "Missing sensor_id."},
                status_code=400,
            )
        if not offsets:
            return JSONResponse(
                {"status": "error", "message": "No offsets provided."},
                status_code=400,
            )

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = _load_sensor_settings_with_direct_local_repair(mgr, sensor_id)
        except FileNotFoundError:
            doc = {}

        offsets = _filter_changed_device_offsets(doc, device_kind, offsets)
        if not offsets:
            return JSONResponse(
                {"status": "error", "message": "No calibration changes detected."},
                status_code=400,
            )

        sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
        sensor_type = str(sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()
        uses_remote_calibration = _sensor_uses_remote_calibration(sensor_id, sensor_blk)

        if uses_remote_calibration:
            ok, err, status_code, applied_keys, shadow_synced = await _publish_remote_device_calibration_offsets(
                sensor_id,
                offsets,
            )
            if not ok:
                body = {"status": "error", "message": err, "shadow_synced": shadow_synced}
                if applied_keys:
                    body["applied"] = applied_keys
                return JSONResponse(body, status_code=status_code)
        else:
            try:
                applied_keys = _apply_device_offsets_shadow(sensor_id, device_kind, offsets)
            except Exception as exc:
                printDM(f"[{MODULE}] device_calibration_apply save error: {exc}", location=MODULE)
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"Failed to save device calibration for {sensor_id}.",
                    },
                    status_code=500,
                )
            shadow_synced = True

        if not uses_remote_calibration:
            try:
                supervisor = getattr(request.app.state, "supervisor", None)
                notify_sensor_runtime_of_calibration(supervisor, sensor_id)
            except Exception as exc:
                printDM(
                    f"[{MODULE}] device_calibration_apply reload error for {sensor_id}: {exc}",
                    location=MODULE,
                )


        if uses_remote_calibration and not shadow_synced:
            msg = f"Accepted calibration update for {sensor_id}; awaiting meta patch shadow sync."
        else:
            msg = f"Updated {len(applied_keys)} device calibration value(s) for {sensor_id}."
        return JSONResponse(
            {
                "status": "success",
                "message": msg,
                "applied": applied_keys,
                "shadow_synced": shadow_synced,
            }
        )

    @router.post("/calibration/soil/ph-buffer", response_class=JSONResponse)
    async def soil_ph_buffer_calibration(request: Request):
        """
        Calibrate a soil pH sensor against a known buffer solution by deriving
        the required pH offset from the latest Soil-pH reading.
        """
        from .saiCalibration import notify_sensor_runtime_of_calibration

        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "message": f"Invalid JSON payload: {exc}"},
                status_code=400,
            )

        sensor_id = normalize_sensor_id(str(payload.get("sensor_id") or ""))
        buffer_raw = payload.get("buffer_ph")

        if not sensor_id:
            return JSONResponse(
                {"status": "error", "message": "Missing sensor_id."},
                status_code=400,
            )

        try:
            buffer_ph = float(buffer_raw)
        except Exception:
            return JSONResponse(
                {"status": "error", "message": "buffer_ph must be numeric."},
                status_code=400,
            )

        if buffer_ph not in (4.0, 7.0, 10.0):
            return JSONResponse(
                {"status": "error", "message": "buffer_ph must be one of 4.0, 7.0, or 10.0."},
                status_code=400,
            )

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = _load_sensor_settings_with_direct_local_repair(mgr, sensor_id)
        except FileNotFoundError:
            return JSONResponse(
                {"status": "error", "message": f"Unknown sensor_id '{sensor_id}'."},
                status_code=404,
            )

        sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
        device_kind = str(sensor_blk.get("DEVICE") or sensor_blk.get("device") or "").strip().lower()
        sensor_type = str(sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()
        uses_remote_calibration = _sensor_uses_remote_calibration(sensor_id, sensor_blk)
        if device_kind != "soil":
            return JSONResponse(
                {"status": "error", "message": f"Sensor '{sensor_id}' is not a soil sensor."},
                status_code=400,
            )

        if uses_remote_calibration:
            current_offset = 0.0
            try:
                current_offset = float(
                    ((doc.get("Calibration") or {}).get("Device") or {}).get("SOIL_PH_CAL_VAL", 0.0) or 0.0
                )
            except Exception:
                current_offset = 0.0

            ok, err, session = await _run_remote_soil_ph_session(sensor_id, reference_ph=buffer_ph)
            if not ok:
                return JSONResponse({"status": "error", "message": err}, status_code=502)

            samples = session.get("samples") if isinstance(session.get("samples"), list) else []
            raw_values = [
                value
                for value in (_extract_soil_ph_from_sample(sample, fallback_offset=current_offset) for sample in samples)
                if value is not None
            ]
            if not raw_values:
                return JSONResponse(
                    {
                        "status": "error",
                        "message": "No valid soil pH samples were collected.",
                    },
                    status_code=502,
                )
            current_ph = round(sum(raw_values) / len(raw_values), 4)
            new_offset = round(buffer_ph - current_ph, 4)
            offsets = [{"key": "soil_ph_offset", "value": new_offset}]

            ok, err, _ack, result = await _publish_remote_calibration_command(
                sensor_id,
                action="apply",
                payload=_mqtt_calibration_payload_from_offsets(offsets),
                ack_timeout=3.0,
                result_timeout=8.0,
            )
            if not ok:
                return JSONResponse({"status": "error", "message": err}, status_code=502)
            if not bool(result.get("applied", False)):
                return JSONResponse(
                    {
                        "status": "error",
                        "message": str(result.get("error") or "Calibration update was rejected."),
                    },
                    status_code=400,
                )
            message_id = str((result or {}).get("message_id") or (_ack or {}).get("message_id") or "").strip()
            applied_keys, shadow_synced = await _sync_remote_calibration_shadow(sensor_id, message_id)
        else:
            try:
                latest = data_logger.get_latest_values(sensor_id) or {}
            except Exception as exc:
                return JSONResponse(
                    {"status": "error", "message": f"Failed to read latest values for {sensor_id}: {exc}"},
                    status_code=500,
                )

            current_raw = latest.get("Soil-pH")
            try:
                current_ph = float(current_raw)
            except Exception:
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"No recent Soil-pH reading is available for {sensor_id}.",
                    },
                    status_code=409,
                )

            new_offset = round(buffer_ph - current_ph, 4)
            offsets = [{"key": "soil_ph_offset", "value": new_offset}]
            try:
                applied_keys = _apply_device_offsets_shadow(sensor_id, device_kind, offsets)
            except Exception as exc:
                printDM(f"[{MODULE}] soil_ph_buffer_calibration save error: {exc}", location=MODULE)
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"Failed to save soil pH calibration for {sensor_id}.",
                    },
                    status_code=500,
                )
            shadow_synced = True

        if not uses_remote_calibration:
            try:
                supervisor = getattr(request.app.state, "supervisor", None)
                notify_sensor_runtime_of_calibration(supervisor, sensor_id)
            except Exception as exc:
                printDM(
                    f"[{MODULE}] soil_ph_buffer_calibration reload error for {sensor_id}: {exc}",
                    location=MODULE,
                )

        return JSONResponse(
            {
                "status": "success",
                "message": (
                    f"Calibrated {sensor_id} to pH {buffer_ph:.1f}."
                    if shadow_synced
                    else f"Calibrated {sensor_id} to pH {buffer_ph:.1f}; awaiting meta patch shadow sync."
                ),
                "sensor_id": sensor_id,
                "buffer_ph": buffer_ph,
                "measured_ph": current_ph,
                "soil_ph_offset": new_offset,
                "applied": applied_keys,
                "shadow_synced": shadow_synced,
            }
        )
        
    @router.get("/ui/modal/system-calibration", response_class=HTMLResponse)
    async def modal_system_calibration(
        request: Request,
        sensor_id: str = Query(""),
    ):
        """
        Serve the Device & System Calibration modal as a Jinja template fragment.
        """
        from .saiCalibration import CalibrationManager

        templates = request.app.state.templates

        # All known sensors (for system-calibration list)
        all_sensor_ids = settings.get_all_sensor_ids() or []

        # Which sensor is "in focus" for device calibration?
        start_sensor_id = sensor_id or (all_sensor_ids[0] if all_sensor_ids else "")

        # Load per-sensor TOML via SensorSettingsManager
        sensor_mgr = SensorSettingsManager("sensor_settings")
        sensor_doc = {}
        if start_sensor_id:
            try:
                sensor_doc = sensor_mgr.load(start_sensor_id)
            except FileNotFoundError:
                sensor_doc = {}

        sensor_section = (sensor_doc.get("Sensor") or {}) or {}
        calib_section  = (sensor_doc.get("Calibration") or {}) or {}
        device_section = (calib_section.get("Device") or calib_section.get("device") or {}) or {}

        # Basic identity for left-side panel
        raw_device   = str(sensor_section.get("DEVICE", "") or "")
        device_kind  = raw_device.strip().lower()
        device_label = raw_device or device_kind or "Unknown"
        sensor_location = str(sensor_section.get("LOCATION", "") or "")

        is_apvpd = (device_kind == "apvpd")

        def _get_float(section: dict, key: str, default: float = 0.0) -> float:
            try:
                return float(section.get(key, default) or default)
            except Exception:
                return default

        # APVPD-specific ambient offsets, fallback to 0.0 if missing
        ambient_temp_offset = _get_float(calib_section, "APVPD_TEMP_CAL_VAL", 0.0)
        ambient_rh_offset   = _get_float(calib_section, "APVPD_RH_CAL_VAL", 0.0)
        soil_ph_offset      = _soil_offset_value(device_section, _SOIL_PH_CAL_KEYS, _get_float)

        # ---- Build per-device offsets for non-APVPD devices -----------------
        device_offsets: list[dict] = []

        def _add_offset(key_path: str, label: str, unit: str, field_key: str, default_val: float = 0.0) -> None:
            value = _get_float(device_section, field_key, default_val)
            device_offsets.append(
                {
                    "key": key_path,   # e.g. "Calibration.Device.CO2_OFFSET"
                    "label": label,    # e.g. "CO₂"
                    "unit": unit,      # e.g. "ppm"
                    "value": value,    # numeric value
                }
            )

        # CO2 devices (e.g. SCD30/SCD4x → DEVICE="co2")
        if device_kind in ("co2", "scd30", "scd4x"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.CO2_OFFSET",  "CO₂", "ppm", "CO2_OFFSET")

        # AQI devices (BME680/BME688 → DEVICE="aqi")
        elif device_kind in ("aqi", "bme680", "bme688"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.AQI_OFFSET",  "AQI", "", "AQI_OFFSET")
            _add_offset("Calibration.Device.GAS_OFFSET",  "Gas resistance", "kΩ", "GAS_OFFSET")

        # LUX devices (VEML7700 → DEVICE="veml"; allow "lux" synonym)
        elif device_kind in ("veml", "lux"):
            # User-visible metrics that should be adjustable:
            #   - "Light Intensity" (lux)  → LUX_OFFSET
            #   - "Estimated PPFD" (µmol/m²/s) → PPFD_OFFSET
            _add_offset(
                "Calibration.Device.LUX_OFFSET",
                "Light Intensity",
                "lux",
                "LUX_OFFSET",
            )
            _add_offset(
                "Calibration.Device.PPFD_OFFSET",
                "Estimated PPFD",
                "µmol/m²/s",
                "PPFD_OFFSET",
            )

        # Non-APVPD VPD sensors (DEVICE="vpd" or "avpd")
        elif device_kind in ("vpd", "avpd", "bme280", "aht", "aht10", "ahtx0"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
        elif device_kind in ("soil",):
            device_offsets.extend(_soil_device_offsets(device_section, _get_float))
        _append_system_altitude_calibration(device_offsets, device_kind)

        # NOTE: APVPD still uses the dedicated is_apvpd branch in the template.
        # If you later want APVPD to also use generic device_offsets, you can
        # add "apvpd" to the elif above, but right now we preserve existing behavior.

        # System calibration candidates via CalibrationManager (unchanged)
        cal_mgr = CalibrationManager(data_logger, sensor_mgr)
        candidate_sensors = cal_mgr.get_calibratable_sensors() or []

        context = {
            "request": request,
            "start_sensor_id": start_sensor_id,
            "sensor_location": sensor_location,
            "device_kind": device_kind,
            "device_label": device_label,
            "is_apvpd": is_apvpd,
            "is_soil": (device_kind == "soil"),
            "ambient_temp_offset": ambient_temp_offset,
            "ambient_rh_offset": ambient_rh_offset,
            "soil_ph_offset": soil_ph_offset,
            "device_offsets": device_offsets,
            "candidate_sensors": candidate_sensors,
            "default_range_hours": 24,
        }

        return templates.TemplateResponse(
            "modals/system_calibration.html",
            context,
        )

    @router.post("/system-calibration/preview", response_class=JSONResponse)
    async def system_calibration_preview(
        reference_id: str = Form(...),
        range_hours: int = Form(24),
        sensor_ids: str = Form(""),   # comma-separated list; empty => all calibratable
    ):
        """
        Compute *system calibration* preview values for selected sensors.

        Returns, per sensor:
        - raw & adjusted Temperature
        - raw & adjusted Rel-Humidity
        - sigma (RMS error) for Temperature & RH
        """
        import time
        from .saiCalibration import CalibrationManager
        from .saiSensorSettingsManager import SensorSettingsManager
        from .saiDataLogger import saiDataLogger

        # Parse/normalize inputs
        try:
            hours = int(range_hours)
        except Exception:
            hours = 24
        if hours <= 0:
            hours = 24

        selected_ids = [
            s.strip()
            for s in (sensor_ids or "").split(",")
            if s.strip()
        ]
        selected_set = set(selected_ids) if selected_ids else None

        end_ts = time.time()
        start_ts = end_ts - (hours * 3600.0)

        # Build helpers
        sensor_mgr = SensorSettingsManager("sensor_settings")
        data_logger = saiDataLogger()  # if you already have a global, reuse that instead
        cal_mgr = CalibrationManager(data_logger, sensor_mgr)

        # Compute offsets + sigmas (no disk writes)
        try:
            results = cal_mgr.compute_system_calibration(
                reference_id=reference_id,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            if DEBUG:
                printDM(
                    f"[SystemCal] preview compute failed: ref={reference_id}, "
                    f"hours={hours}, err={exc}",
                    location=MODULE,
                )
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

        # If compute_system_calibration returned nothing, tell the UI explicitly.
        if not results:
            msg = (
                "No sensors had sufficient overlapping Temperature/Rel-Humidity data "
                f"in the last {hours} hours to compute a system calibration preview."
            )
            if DEBUG:
                printDM(f"[SystemCal] preview -> empty results: {msg}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": msg},
                status_code=200,
            )

        # Shape response
        payload: dict[str, Any] = {
            "ok": True,
            "reference_id": reference_id,
            "range_hours": hours,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "sensors": [],
        }

        for sid, res in results.items():
            # Optional: restrict to subset chosen by user
            if selected_set and sid not in selected_set:
                continue

            latest = data_logger.get_latest_values(sid) or {}

            # Prefer ambient, fall back to plant metrics
            raw_temp = None
            for key in ("Temperature", "Plant Temperature"):
                if key in latest:
                    try:
                        raw_temp = float(latest[key])
                    except Exception:
                        raw_temp = None
                    break

            raw_rh = None
            for key in ("Rel-Humidity", "Plant Rel-Humidity"):
                if key in latest:
                    try:
                        raw_rh = float(latest[key])
                    except Exception:
                        raw_rh = None
                    break

            # Adjusted values based on system offsets
            adj_temp = (raw_temp + res.temp_offset) if raw_temp is not None else None
            adj_rh   = (raw_rh + res.rh_offset)     if raw_rh is not None else None

            payload["sensors"].append(
                {
                    "sensor_id": sid,
                    "raw_temp": raw_temp,
                    "adj_temp": adj_temp,
                    "temp_offset": res.temp_offset,
                    "temp_sigma": res.temp_rms,   # σ for temperature

                    "raw_rh": raw_rh,
                    "adj_rh": adj_rh,
                    "rh_offset": res.rh_offset,
                    "rh_sigma": res.rh_rms,       # σ for RH

                    "n_pairs": res.n_pairs,
                }
            )

        # If filtering by selected sensors removed everything, call that out.
        if not payload["sensors"]:
            msg = (
                "Selected sensors had no valid calibration data in the chosen window. "
                "Try a different reference sensor or time range."
            )
            if DEBUG:
                printDM(f"[SystemCal] preview -> no sensors after filter: {msg}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": msg},
                status_code=200,
            )

        if DEBUG:
            printDM(
                f"[SystemCal] preview ok: ref={reference_id}, "
                f"sensors={len(payload['sensors'])}, hours={hours}",
                location=MODULE,
            )

        return JSONResponse(payload)

    @router.post("/system-calibration/apply", response_class=JSONResponse)
    async def system_calibration_apply(request: Request):
        """
        Apply previously-computed SYSTEM calibration offsets for selected sensors.

        Expects JSON body from the UI like:

        {
          "reference_id": "co2-i2c-1-sensoria-hub-0",
          "start_ts": 1733050000.0,
          "end_ts":   1733136400.0,
          "sensors": [
            {
              "sensor_id": "apvpd-luvk44",
              "temp_offset": 3.129,
              "temp_sigma": 0.260,
              "rh_offset": -6.257,
              "rh_sigma": 0.389,
              "n_pairs": 4152
            },
            ...
          ]
        }
        """
        from .saiCalibration import CalibrationManager, SystemCalResult

        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"Invalid JSON payload: {exc}"},
                status_code=400,
            )

        reference_id = str(payload.get("reference_id") or "").strip()
        sensors_data = payload.get("sensors") or []
        start_ts = payload.get("start_ts")
        end_ts = payload.get("end_ts")

        if not sensors_data:
            return JSONResponse(
                {"ok": False, "error": "No sensors provided for calibration."},
                status_code=400,
            )

        # Fallbacks for start/end if omitted
        import time as _time
        now = _time.time()
        if start_ts is None or end_ts is None:
            # default to last 24h if window missing
            end_ts = now
            start_ts = end_ts - 24.0 * 3600.0

        try:
            start_ts = float(start_ts)
            end_ts = float(end_ts)
        except Exception:
            end_ts = now
            start_ts = end_ts - 24.0 * 3600.0

        sensor_mgr = SensorSettingsManager("sensor_settings")
        cal_mgr = CalibrationManager(data_logger, sensor_mgr)

        applied: list[str] = []
        failures: list[dict] = []

        for entry in sensors_data:
            sensor_id = str(entry.get("sensor_id") or "").strip()
            if not sensor_id:
                continue

            try:
                temp_offset = float(entry.get("temp_offset"))
                rh_offset = float(entry.get("rh_offset"))
            except Exception as exc:
                failures.append(
                    {
                        "sensor_id": sensor_id,
                        "error": f"Bad offsets in payload: {exc}",
                    }
                )
                continue

            try:
                temp_sigma = float(entry.get("temp_sigma") or entry.get("temp_rms") or 0.0)
            except Exception:
                temp_sigma = 0.0
            try:
                rh_sigma = float(entry.get("rh_sigma") or entry.get("rh_rms") or 0.0)
            except Exception:
                rh_sigma = 0.0
            try:
                n_pairs = int(entry.get("n_pairs") or 0)
            except Exception:
                n_pairs = 0

            result = SystemCalResult(
                temp_offset=temp_offset,
                rh_offset=rh_offset,
                temp_rms=temp_sigma,
                rh_rms=rh_sigma,
                n_pairs=n_pairs,
                ref_sensor_id=reference_id,
                start_ts=start_ts,
                end_ts=end_ts,
            )

            try:
                doc = _load_sensor_settings_with_direct_local_repair(sensor_mgr, sensor_id)
                sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
                sensor_type = (
                    sensor_blk.get("TYPE")
                    or sensor_blk.get("type")
                    or ""
                )
                sensor_type = str(sensor_type).strip().lower()
                if _sensor_uses_remote_calibration(sensor_id, sensor_blk):
                    ok, err, _ack, mqtt_result = await _publish_remote_calibration_command(
                        sensor_id,
                        action="apply",
                        payload={
                            "calibration": {
                                "system": {
                                    "TEMP_OFFSET": round(temp_offset, 3),
                                    "RH_OFFSET": round(rh_offset, 3),
                                    "REF_SENSOR_ID": reference_id,
                                    "REF_RANGE_HOURS": int((end_ts - start_ts) / 3600.0),
                                    "REF_START_TS": int(start_ts),
                                    "REF_END_TS": int(end_ts),
                                }
                            }
                        },
                        ack_timeout=3.0,
                        result_timeout=8.0,
                    )
                    if not ok:
                        failures.append({"sensor_id": sensor_id, "error": err})
                        continue
                    if not bool(mqtt_result.get("applied", False)):
                        failures.append(
                            {
                                "sensor_id": sensor_id,
                                "error": str(mqtt_result.get("error") or "Calibration update was rejected."),
                            }
                        )
                        continue
                    message_id = str((mqtt_result or {}).get("message_id") or (_ack or {}).get("message_id") or "").strip()
                    await _sync_remote_calibration_shadow(sensor_id, message_id)
                else:
                    cal_mgr.apply_system_calibration(sensor_id, result)
                applied.append(sensor_id)

            except Exception as exc:
                failures.append(
                    {
                        "sensor_id": sensor_id,
                        "error": str(exc),
                    }
                )
                continue

        return JSONResponse(
            {
                "ok": True,
                "reference_id": reference_id,
                "applied": applied,
                "failures": failures,
            }
        )

    @router.post("/update-calibration-values")
    async def update_calibration_values(request: Request):
        """
        Body:
        {
            "sensor_id": "co2-i2c-1-sensoria-hub-0",
            "offsets": [...],
            "calibration": {...},    # optional nested form
            "is_remote": false       # optional hint
        }
        """
        from .saiCalibration import apply_calibration_updates_local, notify_sensor_runtime_of_calibration
        
        payload = await request.json()
        sensor_id = (payload.get("sensor_id") or "").strip()
        if not sensor_id:
            return JSONResponse({"error": "sensor_id required"}, status_code=400)

        offsets = payload.get("offsets")
        calib   = payload.get("calibration")
        if DEBUG:
            printDM(f"/update-calibration-values called with payload={payload}", location=MODULE)
    
        # 1) persist to local sensor_settings/<sensor_id>/sensor.toml
        if offsets is not None:
            apply_calibration_updates_local(sensor_id, offsets)
        elif calib is not None:
            apply_calibration_updates_local(sensor_id, calib)
        else:
            return JSONResponse({"error": "no offsets provided"}, status_code=400)

        # 2) notify local runtime (if this is a local Pi sensor)
        supervisor = request.app.state.supervisor if hasattr(request.app.state, "supervisor") else None
        notify_sensor_runtime_of_calibration(supervisor, sensor_id)

        return JSONResponse({"ok": True})

    # ----------- Switch and Switch Automations ----------
    # ----------- Switch Controls are a must -------------
    def _iter_switch_controllers(web_server) -> Iterable[object]:
        """Yield only controllers that implement switch behavior."""
        controllers = getattr(web_server, "controllers", [])
        for candidate in controllers:
            # Accept anything that looks like a switch controller
            if hasattr(candidate, "get_switch_names") and callable(candidate.get_switch_names):
                yield candidate

    def _normalize_label(label: Optional[str]) -> Optional[str]:
        if not label:
            return None
        return label.strip()

    def _normalize_switch_id(switch_id: Optional[str]) -> Optional[str]:
        if not switch_id:
            return None
        return switch_id.strip().lower()

    def _controller_matches(
        ctrl: object,
        target_label: str,
        target_switch_id: Optional[str],
        target_location: Optional[str] = None,
    ) -> bool:
        try:
            names = set(n.strip() for n in ctrl.get_switch_names() or [])
        except Exception:
            names = set()
        if target_label not in names:
            return False

        if target_switch_id:
            ctrl_switch_id = getattr(ctrl, "switch_id", None)

            if ctrl_switch_id and _normalize_switch_id(ctrl_switch_id) != target_switch_id:
                return False

        if target_location:
            # Optional: allow narrowing by location if provided
            ctrl_loc = getattr(ctrl, "location", None)
            if (ctrl_loc or "").strip().lower() != (target_location or "").strip().lower():
                return False

        return True

    def _resolve_target_from_payload(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Accepts any of:
          - {"key": "switch_id::channel_id"}
          - {"key": "switch_id::label"}     # legacy/tolerated
          - {"switch_id": "...", "label": "..."}
          - {"switch_id": "...", "name": "..."}  # tolerant of older field names
          - {"label": "..."}                 # legacy (ambiguous if duplicates exist)
        Returns (switch_id, label, location) normalized.
        """
        raw_key = payload.get("key") or payload.get("switch_key")
        switch_id = payload.get("switch_id")
        label = payload.get("label") or payload.get("name")
        location = payload.get("location")

        if raw_key and "::" in raw_key:
            switch_id_part, label_part = raw_key.split("::", 1)
            switch_id = switch_id or switch_id_part
            label = label or label_part

        return (
            _normalize_switch_id(switch_id),
            _normalize_label(label),
            (location or None),
        )
    
    def _switch_key(switch_id: str, label: str) -> str:
        """
        Canonical switch key builder: use saiDataLogger.build_switch_key if present,
        otherwise fall back to '<switch_id>::<channel_id-or-label>'.
        """
        sid = (switch_id or "").strip()
        lab = (label or "").strip()
        ch_id = _resolve_channel_id_from_label(sid, lab)
        if _build_switch_key is not None:
            try:
                return _build_switch_key(sid, ch_id or lab)
            except Exception:
                pass
        return f"{sid}::{ch_id or lab}"

    def _switch_channel_display_name(channel: dict) -> str:
        channel_id = str((channel or {}).get("channel_id") or "").strip()
        index = str((channel or {}).get("index") or "").strip()
        label = str((channel or {}).get("label") or "").strip()
        if channel_id and "-" in channel_id:
            prefix = channel_id.split("-", 1)[0].strip()
            if prefix:
                return prefix.upper()
        if index:
            return f"S{index}"
        return label or "Switch"

    def _switch_channels_from_settings(settings_dict: dict) -> list[dict]:
        sw_section = (settings_dict or {}).get("Switch", {}) or {}

        def _has_install_marker(val) -> bool:
            if val is None:
                return False
            if isinstance(val, bool):
                return val
            return str(val).strip() != ""

        sw_type = str(sw_section.get("TYPE", "") or "").strip().lower()
        has_en_keys = (
            ("SWITCH_1_ENABLE_PIN" in sw_section) or ("SWITCH_2_ENABLE_PIN" in sw_section)
            or ("SWITCH_1_EN" in sw_section) or ("SWITCH_2_EN" in sw_section)
        )

        def _enable_value(i: int):
            return sw_section.get(f"SWITCH_{i}_ENABLE_PIN", sw_section.get(f"SWITCH_{i}_EN", ""))

        indices_found: set[int] = set()
        for key in sw_section.keys():
            m = re.match(r"^SWITCH_(\d+)(?:_LABEL|_ENABLE_PIN|_EN|_PIN|_Trigger)?$", str(key))
            if m:
                indices_found.add(int(m.group(1)))
        if not indices_found:
            indices_found.add(1)

        render_indices: list[int] = []
        for i in sorted(indices_found):
            label_key = f"SWITCH_{i}_LABEL"
            pin_key = f"SWITCH_{i}_PIN"
            if sw_type in ("picow", "pico2w") or has_en_keys:
                if _has_install_marker(_enable_value(i)):
                    render_indices.append(i)
            else:
                if str(sw_section.get(label_key, "")).strip() and str(sw_section.get(pin_key, "")).strip():
                    render_indices.append(i)
        channel_indices = render_indices or [1]
        return [
            {
                "index": idx,
                "label": str(sw_section.get(f"SWITCH_{idx}_LABEL", "") or ""),
                "channel_id": str(sw_section.get(f"SWITCH_{idx}_CHANNEL_ID", "") or ""),
            }
            for idx in channel_indices
        ]

    async def _build_switch_statistics_payload(switch_id_value: str, channels: list[dict]) -> dict[str, Any]:
        sid = str(switch_id_value or "").strip()
        offline_events_24h = 0
        last_offline_epoch = None
        last_packet_epoch = None
        stats_now = time.time()
        base_id = normalize_hostname_base(sid)
        switch_stat_aliases = [sid]
        if base_id:
            switch_stat_aliases.extend([base_id, mdns_hostname(base_id)])
        for ch in channels or []:
            channel_id = str((ch or {}).get("channel_id") or "").strip()
            if channel_id:
                switch_stat_aliases.append(channel_id)
                channel_base = normalize_hostname_base(channel_id)
                if channel_base:
                    switch_stat_aliases.extend([channel_base, mdns_hostname(channel_base)])

        try:
            counter = getattr(data_logger, "get_sensor_offline_event_count", None)
            if callable(counter):
                offline_events_24h = await asyncio.to_thread(
                    lambda: counter(sid, aliases=switch_stat_aliases)
                )
        except Exception:
            offline_events_24h = 0
        try:
            last_offline = getattr(data_logger, "get_sensor_last_offline_event_epoch", None)
            if callable(last_offline):
                last_offline_epoch = await asyncio.to_thread(
                    lambda: last_offline(sid, aliases=switch_stat_aliases)
                )
        except Exception:
            last_offline_epoch = None

        channel_state_stats: list[dict[str, Any]] = []
        channel_last_event = getattr(data_logger, "get_switch_last_event_for_channel", None)
        for ch in channels or []:
            channel_id = str((ch or {}).get("channel_id") or "").strip()
            display_name = _switch_channel_display_name(ch)
            event = None
            if channel_id and callable(channel_last_event):
                try:
                    event = await asyncio.to_thread(lambda cid=channel_id: channel_last_event(cid))
                except Exception:
                    event = None
            event_epoch = event.get("ts_epoch") if isinstance(event, dict) else None
            state_value = _format_switch_state_label(event.get("state")) if isinstance(event, dict) else ""
            if event_epoch is not None:
                try:
                    last_packet_epoch = max(float(last_packet_epoch or 0.0), float(event_epoch))
                except Exception:
                    pass
            channel_state_stats.append(
                {
                    "channel_id": channel_id,
                    "label": str((ch or {}).get("label") or "").strip(),
                    "display_name": display_name,
                    "row_label": f"{display_name} current state, age:",
                    "state": state_value,
                    "state_epoch": event_epoch,
                    "state_age_label": (
                        f"{state_value}, {_format_stats_duration(stats_now - float(event_epoch))}"
                        if event_epoch is not None
                        else "No state changes"
                    ),
                }
            )

        return {
            "offline_events_24h": offline_events_24h,
            "last_offline_epoch": last_offline_epoch,
            "uptime_since_last_offline_label": _format_stats_duration(
                (stats_now - float(last_offline_epoch))
                if last_offline_epoch is not None
                else None
            ),
            "last_offline_event_label": _format_stats_timestamp(
                last_offline_epoch,
                empty="No offline events",
            ),
            "switch_last_packet_epoch": last_packet_epoch,
            "switch_last_packet_received_label": _format_stats_age(
                last_packet_epoch,
                empty="No packets",
            ),
            "switch_channel_state_stats": channel_state_stats,
        }

    @router.get("/switch-settings/statistics", response_class=JSONResponse)
    async def switch_settings_statistics(
        request: Request,
        switch_id: str = Query(...),
    ):
        _require_protected_access(request, require_csrf=True)
        sid = str(switch_id or "").strip()
        if not sid:
            return JSONResponse({"ok": False, "error": "Missing switch_id"}, status_code=400)
        manager = SwitchSettingsManager("switch_settings")
        settings_dict = await asyncio.to_thread(lambda: manager.load(sid) or {})
        channels = _switch_channels_from_settings(settings_dict)
        payload = await _build_switch_statistics_payload(sid, channels)
        payload["ok"] = True
        payload["switch_id"] = sid
        return JSONResponse(payload)

    @router.get("/edit-switch", response_class=HTMLResponse)
    async def edit_switch_page(
        request: Request,
        switch_id: str = Query(...),
        embed: int = Query(0),
    ):
        _route_started = time.monotonic()
        from .saiSwitchSettingsManager import SwitchSettingsManager

        manager = SwitchSettingsManager("switch_settings")
        settings_dict = manager.load(switch_id) or {}
        if not settings_dict:
            # Still return a minimal modal for 404
            html = (
                "<div class='modal-backdrop' style='display:flex'>"
                "<div class='modal'>"
                "<div class='modal-body'>"
                f"<h3>No settings for '{switch_id}'</h3>"
                "</div></div></div>"
            )
            return HTMLResponse(html, status_code=404)

        sw = (settings_dict or {}).get("Switch", {}) or {}
        channels = _switch_channels_from_settings(settings_dict)
        channel_indices = [int(ch.get("index") or 1) for ch in channels]

        ingest = getattr(request.app.state, "mqtt_ingest", None) or mqtt_ingest
        nodus_firmware_version = ""
        switch_type = str(sw.get("TYPE", "") or "").strip().lower()
        if (
            ingest
            and switch_type in ("picow", "pico2w", "nodus", "remote", "mqtt")
            and hasattr(ingest, "get_nodus_firmware_version")
        ):
            try:
                nodus_firmware_version = str(
                    ingest.get_nodus_firmware_version(switch_id, device_type="switch")
                ).strip()
            except Exception:
                nodus_firmware_version = ""
        nodus_board_type = _display_nodus_board_type(
            switch_id,
            device_type="switch",
            settings_block=sw,
        )

        network_info = await _build_device_network_info(switch_id, device_type="switch")
        switch_statistics = await _build_switch_statistics_payload(switch_id, channels)

        # ---- render Jinja template to an HTML snippet ----
        templates = request.app.state.templates
        template = templates.get_template("modals/switch_settings.html")
        modal_html = template.render(
            switch_id=switch_id,
            settings=settings_dict,
            channel_indices=channel_indices,
            channels=channels,
            nodus_firmware_version=nodus_firmware_version,
            nodus_board_type=nodus_board_type,
            can_restart_device=(switch_type in ("picow", "pico2w", "nodus", "remote", "mqtt")),
            network_info=network_info,
            **switch_statistics,
        )

        # ---- embed=1 → just the modal markup (used by dashboard JS) ----
        if embed:
            _ui_profile_log("edit-switch", _route_started, embed=1, switch_id=switch_id, channels=len(channel_indices))
            return HTMLResponse(modal_html)

        # ---- full-page fallback (keeps existing behavior & JS wiring) ----
        page: list[str] = []
        page.append("<html><head><title>Edit Switch</title>")
        # ensure app.css is loaded so modal styles look correct
        page.append(f"<link rel='stylesheet' href='/ui_static/css/app.css?v={SAI_APP_VERSION}'>")
        page.append("</head><body>")
        page.append("<div id='modalHost'></div>")
        modal_json = json.dumps(modal_html).replace("</", "<\\/")
        page.append(f"<script>var __MODAL_HTML__ = {modal_json};</script>")
        page.append("<script>")
        page.append("  (function(){")
        page.append("    if (window.showSwitchSettingsModal) {")
        page.append("      window.showSwitchSettingsModal(__MODAL_HTML__);")
        page.append("    } else {")
        page.append("      var host = document.getElementById('modalHost');")
        page.append("      if (host) {")
        page.append("        host.innerHTML = __MODAL_HTML__;")
        page.append("        var modal = document.getElementById('switchSettingsModal');")
        page.append("        var backdrop = modal && modal.closest('.modal-backdrop');")
        page.append("        if (backdrop) backdrop.style.display = 'flex';")
        page.append("        if (window.activateInlineScripts) {")
        page.append("          window.activateInlineScripts(host);")
        page.append("        } else {")
        page.append("          host.querySelectorAll('script').forEach(function(oldScript) {")
        page.append("            var script = document.createElement('script');")
        page.append("            script.textContent = oldScript.textContent || '';")
        page.append("            oldScript.replaceWith(script);")
        page.append("          });")
        page.append("        }")
        page.append("        var settingsButton = host.querySelector('#switchMenuSettings');")
        page.append("        var infoButton = host.querySelector('#switchMenuStatistics');")
        page.append("        var settingsPane = host.querySelector('#switchSettingsPane');")
        page.append("        var infoPane = host.querySelector('#switchStatisticsPane');")
        page.append("        function selectSwitchPane(showInfo) {")
        page.append("          if (settingsPane) settingsPane.hidden = showInfo;")
        page.append("          if (infoPane) infoPane.hidden = !showInfo;")
        page.append("          if (settingsPane) settingsPane.style.display = showInfo ? 'none' : '';")
        page.append("          if (infoPane) infoPane.style.display = showInfo ? '' : 'none';")
        page.append("          if (settingsButton) settingsButton.classList.toggle('active', !showInfo);")
        page.append("          if (infoButton) infoButton.classList.toggle('active', showInfo);")
        page.append("        }")
        page.append("        if (settingsButton) settingsButton.addEventListener('click', function(){ selectSwitchPane(false); });")
        page.append("        if (infoButton) infoButton.addEventListener('click', function(){ selectSwitchPane(true); });")
        page.append("        selectSwitchPane(false);")
        page.append("        if (window.openSwitchSettingsModal) window.openSwitchSettingsModal();")
        page.append("        if (window.switchModalWire) window.switchModalWire();")
        page.append("      }")
        page.append("    }")
        page.append("  })();")
        page.append("</script>")
        page.append("</body></html>")

        _ui_profile_log("edit-switch", _route_started, embed=0, switch_id=switch_id, channels=len(channel_indices))
        return HTMLResponse(content="\n".join(page))

    @router.post("/submit-switch-settings")
    async def submit_switch_settings(request: Request):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        form = await request.form()

        def normalize_switch_id(raw: str) -> str:
            s = (raw or "").strip().replace(" ", "-")
            return "".join(ch for ch in s if ch.isalnum() or ch in "-_").lower() or "unknown-switch"

        def deep_merge_ordered(base: OrderedDict, update: dict | OrderedDict) -> OrderedDict:
            for k, v in (update or {}).items():
                if isinstance(v, dict):
                    if k not in base or not isinstance(base.get(k), dict):
                        base[k] = OrderedDict()
                    if not isinstance(base[k], OrderedDict):
                        base[k] = OrderedDict(base[k])
                    deep_merge_ordered(base[k], v)
                else:
                    base[k] = v
            return base

        # ---- validate ----
        switch_id_in_form = form.get("switch_id")
        if not switch_id_in_form:
            return _modal_error_response(request, "Missing switch_id", status_code=400)

        old_id = normalize_switch_id(switch_id_in_form)
        manager = SwitchSettingsManager("switch_settings")
        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)

        location_value = (form.get("location", "") or "").strip()
        new_id = old_id

        existing_doc = manager.load(old_id) or OrderedDict()
        if not isinstance(existing_doc, OrderedDict):
            existing_doc = OrderedDict(existing_doc)
        previous_doc = copy.deepcopy(existing_doc)
        existing_doc.setdefault("Switch", OrderedDict())

        # --- dynamically collect editable channel indices from the form
        idxs = set()
        pat = re.compile(r"^SWITCH_(\d+)_(?:LABEL|LAST_STATE|OVERRIDE_SCRIPT)$")
        for key in form.keys():
            m = pat.match(key)
            if m:
                idxs.add(int(m.group(1)))
        channel_indices = sorted(idxs)

        sw_block = OrderedDict({
            "SWITCH_LOCATION": location_value or existing_doc["Switch"].get("SWITCH_LOCATION", "Unknown"),
        })

        # Merge per-channel updates
        for i in channel_indices:
            label_key   = f"SWITCH_{i}_LABEL"
            last_state_key = f"SWITCH_{i}_LAST_STATE"
            override_key = f"SWITCH_{i}_OVERRIDE_SCRIPT"
            if label_key in form:
                sw_block[label_key] = (form.get(label_key, "") or "").strip()
            if last_state_key in form:
                sw_block[last_state_key] = str(form.get(last_state_key, "") or "").strip().lower() in {"1", "true", "on", "yes"}
            if override_key in form:
                sw_block[override_key] = str(form.get(override_key, "") or "").strip().lower() in {"1", "true", "on", "yes"}

        merged_doc = deep_merge_ordered(OrderedDict(existing_doc), OrderedDict({"Switch": sw_block}))

        base_dir = Path(getattr(manager, "base_dir", "switch_settings"))
        old_dir = base_dir / old_id
        new_dir = base_dir / new_id

        switch_type = str((merged_doc.get("Switch", {}) or {}).get("TYPE", "") or "").strip().lower()
        if switch_type in ("picow", "pico2w", "nodus"):
            existing_switch_block = previous_doc.get("Switch", {}) if isinstance(previous_doc, dict) else {}
            remote_updates = [
                ("Switch", key, value)
                for key, value in sw_block.items()
                if not key.endswith("_LAST_STATE")
                if not _nodus_values_match(
                    existing_switch_block.get(key) if isinstance(existing_switch_block, dict) else None,
                    value,
                )
            ]
            remote_last_state_updates: list[dict[str, Any]] = []
            for key, value in sw_block.items():
                if not key.endswith("_LAST_STATE"):
                    continue
                if _nodus_values_match(
                    existing_switch_block.get(key) if isinstance(existing_switch_block, dict) else None,
                    value,
                ):
                    continue
                match = re.fullmatch(r"SWITCH_(\d+)_LAST_STATE", str(key or ""))
                if not match:
                    continue
                idx = int(match.group(1))
                current_switch_block = merged_doc.get("Switch", {}) if isinstance(merged_doc, dict) else {}
                previous_label = str(existing_switch_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_label = previous_label or str(current_switch_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_id = str(current_switch_block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                remote_last_state_updates.append(
                    {
                        "index": idx,
                        "channel_id": channel_id,
                        "channel_label": channel_label,
                        "new_state": bool(value),
                    }
                )
            if remote_updates:
                ok = await push_nodus_settings_batch(
                    device_id=old_id,
                    device_type="switch",
                    setting_file_key="switch",
                    updates=remote_updates,
                    sensor_file_name=None,
                    system_mgr=system_mgr,
                    system_root=system_root,
                    sys_host_index=sys_host_index,
                )
                if not ok:
                    if _wants_modal_json(request):
                        return JSONResponse(
                            {"ok": False, "error": "Failed to apply remote switch settings."},
                            status_code=502,
                        )
                    return PlainTextResponse("Failed to apply remote switch settings.", status_code=502)
            for update in remote_last_state_updates:
                ok, err = await _publish_remote_switch_last_state(
                    old_id,
                    channel_index=int(update.get("index") or 0),
                    channel_id=str(update.get("channel_id") or "").strip(),
                    channel_label=str(update.get("channel_label") or "").strip(),
                    new_state=bool(update.get("new_state")),
                    system_mgr=system_mgr,
                    system_root=system_root,
                    sys_host_index=sys_host_index,
                )
                if not ok:
                    err_msg = err or "Failed to apply remote switch state."
                    if _wants_modal_json(request):
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": err_msg,
                                "channel_id": str(update.get("channel_id") or "").strip(),
                                "channel_label": str(update.get("channel_label") or "").strip(),
                            },
                            status_code=502,
                        )
                    return PlainTextResponse(err_msg, status_code=502)

        # Persist the local shadow only after remote applies/commands succeed.
        try:
            manager.save(old_id, merged_doc)   # assumes save can accept a full doc
        except TypeError:
            # Fallback if manager.save expects only partials: try 'save_full' if provided.
            if hasattr(manager, "save_full"):
                manager.save_full(old_id, merged_doc)
            else:
                # Last resort: re-save section-by-section in a deterministic order
                for section_name, section_map in merged_doc.items():
                    manager.save(old_id, OrderedDict([(section_name, section_map)]))

        _invalidate_dashboard_caches()
        try:
            active_controllers = getattr(app.state, "switch_controllers", None) or switch_controllers
            controller_values = active_controllers.values() if isinstance(active_controllers, dict) else [active_controllers]
            for controller in controller_values:
                if str(getattr(controller, "switch_id", "") or "").strip().lower() == old_id:
                    controller.location = str((merged_doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "") or "")
        except Exception:
            pass

        if _wants_modal_json(request):
            return JSONResponse({"ok": True, "message": "Switch settings saved.", "switch_id": new_id})
        return RedirectResponse(url="/", status_code=303)

    @router.post("/switch-settings/restart-device")
    async def restart_switch_device(request: Request):
        form = await request.form()
        switch_id = str(form.get("switch_id") or "").strip().replace(" ", "-")
        switch_id = "".join(ch for ch in switch_id if ch.isalnum() or ch in "-_").lower()
        if not switch_id:
            return JSONResponse({"ok": False, "error": "Missing switch_id"}, status_code=400)

        manager = SwitchSettingsManager("switch_settings")
        doc = manager.load(switch_id) or {}
        switch_type = str(((doc.get("Switch", {}) or {}).get("TYPE", "") or "")).strip().lower()
        if switch_type not in {"picow", "pico2w", "nodus", "remote", "mqtt"}:
            return JSONResponse({"ok": False, "error": "Restart is only available for Nodus switches."}, status_code=400)

        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)
        target_device = str(
            _read_hostname_from_system_settings(
                switch_id,
                system_mgr,
                system_root,
                device_type="switch",
                sys_host_index=sys_host_index,
            )
            or switch_id
        ).strip()
        printDM(
            f"[restart-request] device_type=switch device_id={switch_id} target={target_device} via=webui",
            location=MODULE,
            level="info",
        )
        ok, message = await _request_nodus_device_restart(
            target_device=target_device,
            device_id=switch_id,
            device_type="switch",
            restart_mode="soft",
        )
        printDM(
            f"[restart-result] device_type=switch device_id={switch_id} target={target_device} ok={ok} message={message}",
            location=MODULE,
            level="info" if ok else "warning",
        )
        status_code = 200 if ok else 502
        payload_key = "message" if ok else "error"
        return JSONResponse(
            {"ok": ok, payload_key: message, "switch_id": switch_id, "target_device": target_device},
            status_code=status_code,
        )

    # Advanced Automation helpers
    # switch id helpers
    # --- switch updates WS (lightweight) ---
    _SWITCH_SOCKETS: Set[WebSocket] = set()

    async def _switch_broadcast(payload: Dict[str, Any]) -> None:
        if DEBUG:
            try:
                printDM(
                    f"[switch-ws] broadcast type={payload.get('type')} key={payload.get('key')} ui_key={payload.get('ui_key', '')} clients={len(_SWITCH_SOCKETS)}",
                    location=MODULE,
                )
            except Exception:
                pass
        stale: list[WebSocket] = []
        for ws in list(_SWITCH_SOCKETS):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            try:
                _SWITCH_SOCKETS.discard(ws)
            except Exception:
                pass

    @router.websocket("/ws/switch-updates")
    async def ws_switch_updates(ws: WebSocket):
        await ws.accept()
        _SWITCH_SOCKETS.add(ws)
        if DEBUG:
            printDM(f"[switch-ws] client connected total={len(_SWITCH_SOCKETS)}", location=MODULE)
        try:
            # keep alive; client never needs to send data
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _SWITCH_SOCKETS.discard(ws)
            if DEBUG:
                printDM(f"[switch-ws] client disconnected total={len(_SWITCH_SOCKETS)}", location=MODULE)

    # expose for other modules without import cycles
    app.state.switch_broadcast = _switch_broadcast

    @router.get("/switch-chooser", response_class=HTMLResponse)
    async def switch_chooser(request: Request):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        import html
        from urllib.parse import quote

        mgr = SwitchSettingsManager("switch_settings")
        ids = mgr.list_switches() or []
        items = "\n".join(
            f'<li><a href="/edit-switch?switch_id={quote(sid)}">{html.escape(sid)}</a></li>'
            for sid in ids
        ) or "<li>No switches found. Add a device first.</li>"
        html_doc = f"""
        <!doctype html><html><head>
          <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
          <title>Select a switch — Sensorius</title>
          <style>body{{font-family:system-ui;margin:2rem}} a{{text-decoration:none}} li{{margin:.4rem 0}}</style>
        </head><body>
          <h2>Select a switch</h2>
          <ul>{items}</ul>
        </body></html>
        """
        return HTMLResponse(html_doc)

    @router.get("/switch-info", response_class=JSONResponse)
    async def api_switch_info(switch_id: str = Query(...)):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        try:
            mgr = SwitchSettingsManager("switch_settings")
            dat = mgr.load(switch_id)
            if not dat:
                return JSONResponse({"error": f"switch_id '{switch_id}' not found"}, status_code=404)

            sw = (dat.get("Switch") or {})

            # 1) Collect labels from SWITCH_<n>_LABEL in the new schema.
            labels: dict[int, str] = {}
            channel_ids: dict[int, str] = {}
            import re
            for k, v in sw.items():
                ks = str(k).strip()
                m = re.fullmatch(r"SWITCH_(\d+)_LABEL", ks, flags=re.IGNORECASE)
                if m:
                    idx = int(m.group(1))
                    label_text = ("" if v is None else str(v)).strip()
                    if not label_text:
                        continue
                    labels[idx] = label_text
                    channel_id_text = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                    if channel_id_text:
                        channel_ids[idx] = channel_id_text

            # 2) Determine channel count from CHANNELS (if present) or from the max SWITCH_<n> index
            try:
                raw_channels = next((sw.get(k) for k in ("CHANNELS", "channels", "Channels") if k in sw), 0)
                channels = int(raw_channels) if str(raw_channels).strip() else 0
            except Exception:
                channels = 0

            if not channels:
                channels = max(labels.keys(), default=1)

            astral_status = {
                "ok": False,
                "source": "",
                "lat": None,
                "lon": None,
                "tz": "",
                "message": "",
            }
            try:
                settings_local = saiSettings(apply_live=False)
                resolved = settings_local.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5) or {}
                lat = resolved.get("lat")
                lon = resolved.get("lon")
                tz_name = str(resolved.get("tz") or "").strip()
                source = str(resolved.get("source") or "").strip()
                ok = lat is not None and lon is not None and bool(tz_name)
                astral_status = {
                    "ok": bool(ok),
                    "source": source,
                    "lat": float(lat) if lat is not None else None,
                    "lon": float(lon) if lon is not None else None,
                    "tz": tz_name,
                    "message": (
                        f"Astral location ready ({source or 'resolved'})."
                        if ok else
                        "Astral location is not currently resolved. Astral automations will evaluate false until location/timezone is available."
                    ),
                }
            except Exception as exc:
                astral_status = {
                    "ok": False,
                    "source": "",
                    "lat": None,
                    "lon": None,
                    "tz": "",
                    "message": f"Astral location check failed: {exc}",
                }

            return {
                "switch_id": switch_id,
                "channels": channels,
                "labels": labels,
                "channel_ids": channel_ids,
                "astral_status": astral_status,
            }
        except Exception as exc:
            printDM(f"/switch-info error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/advanced/automations", response_class=JSONResponse)
    async def api_list_advanced_automations(switch_id: str = Query(...)):
        from .saiAutomationManager import AutomationManager
        def _parse_enabled(value) -> bool:
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "off", "no", ""}
            return bool(value)
        def _rule_targets_switch(payload: object, requested_switch_id: str) -> bool:
            wanted_sid = str(requested_switch_id or "").strip().lower()
            if wanted_sid == "__all__":
                return True
            if not wanted_sid:
                return False
            if not isinstance(payload, dict):
                return False
            script_json = (
                str(payload.get("script_json", ""))
                or str(payload.get("script", ""))
                or str(payload.get("json", ""))
                or ""
            )
            if not script_json:
                return False
            try:
                parsed = json.loads(script_json)
            except Exception:
                return False
            if not isinstance(parsed, dict):
                return False
            actions = parsed.get("actions") or []
            if isinstance(actions, dict):
                actions = [actions]
            for action in actions:
                if not isinstance(action, dict):
                    continue
                switch_key = str(action.get("switch_key", "") or "").strip()
                if not switch_key:
                    continue
                if "::" in switch_key:
                    sid_part, _suffix = switch_key.split("::", 1)
                    if str(sid_part or "").strip().lower() == wanted_sid:
                        return True
                    continue
                # Backward compatibility for older rules that stored only a label/channel suffix.
                return True
            return False
        try:
            mgr = AutomationManager("automation_settings")
            data = mgr.load(switch_id) or {}
            legacy_getter = getattr(mgr, "get_legacy_rule_ids", None)
            legacy_rule_ids = (
                set(legacy_getter(switch_id) or set())
                if callable(legacy_getter)
                else set()
            )

            adv = (
                data.get("Advanced")
                or data.get("advanced")
                or data.get("Triggers")
                or data.get("triggers")
                or {}
            )
            items = []
            # Sort by key for stable display
            for rule_id in sorted(adv.keys()):
                payload = adv[rule_id]
                if isinstance(payload, dict):
                    if not _rule_targets_switch(payload, switch_id):
                        continue
                    enabled = _parse_enabled(payload.get("enabled", True))
                    script_json = (
                        str(payload.get("script_json", ""))
                        or str(payload.get("script", ""))
                        or str(payload.get("json", ""))
                        or ""
                    )
                else:
                    continue
                items.append({
                    "rule_id": rule_id,
                    "enabled": enabled,
                    "script_json": script_json,
                    "legacy": str(rule_id) in legacy_rule_ids,
                })

            return {"switch_id": switch_id, "items": items}
        except Exception as exc:
            printDM(f"/advanced/automations error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/automation-context", response_class=JSONResponse)
    async def automation_context():
        """Return the system-wide actor directory used by the automation editor."""
        mgr = SwitchSettingsManager("switch_settings")
        actors: list[dict[str, str]] = []
        switch_ids = mgr.list_switches() or []
        for sid in switch_ids:
            doc = mgr.load(sid) or {}
            sw = doc.get("Switch") or {}
            for idx in range(1, 33):
                label = str(sw.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_id = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                if not label:
                    continue
                actors.append({
                    "type": "switch",
                    "switch_id": sid,
                    "label": label,
                    "channel_id": channel_id,
                    "value": f"{sid}::{channel_id or label}",
                    "display": f"{sid}:{label}",
                })

        email_enabled = _is_true_text(
            str(
                os.environ.get(
                    "SENSORIUS_EMAIL_ENABLED",
                    _env_map_with_defaults().get("SENSORIUS_EMAIL_ENABLED", "false"),
                )
                or ""
            )
        )
        return {
            "actors": actors,
            "email_enabled": email_enabled,
            "executor_switch_id": "__system__",
        }

    def _resolve_automation_target(switch_id: str, switch_key: str) -> tuple[str, str, int | None]:
        sid = switch_id
        suffix = str(switch_key or "").strip()
        if "::" in suffix:
            sid, suffix = suffix.split("::", 1)
            sid = str(sid or switch_id).strip() or switch_id
            suffix = str(suffix or "").strip()
        label = suffix
        channel_index = None
        try:
            switch_mgr = SwitchSettingsManager("switch_settings")
            doc = switch_mgr.load(sid) or {}
            sw_map = doc.get("Switch") or {}
            for idx in range(1, 33):
                cand_label = str(sw_map.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                cand_channel_id = str(sw_map.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                if not cand_label:
                    continue
                if suffix.lower() == cand_label.lower() or (cand_channel_id and suffix.lower() == cand_channel_id.lower()):
                    label = cand_label
                    channel_index = idx
                    break
        except Exception:
            pass
        return sid, label, channel_index

    async def _broadcast_automation_states(app: FastAPI, mgr, switch_id: str, switch_keys: set[str]) -> None:
        if not hasattr(app.state, "switch_broadcast"):
            return
        for switch_key in (switch_keys or set()):
            state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
            sid, label, _channel_index = _resolve_automation_target(switch_id, switch_key)
            await app.state.switch_broadcast({
                "type": "automation_toggle",
                "switch_id": sid,
                "label": label,
                "enabled": bool(state.get("enabled_any", False)),
            })

    @router.post("/advanced/automations/enable", response_class=JSONResponse)
    async def api_enable_advanced_automation(
        request: Request,
        switch_id: str = Form(...),
        rule_id: str = Form(...),
        enabled: str = Form("true"),  # accept str, coerce below
    ):
        from .saiAutomationManager import AutomationManager
        try:
            truthy = str(enabled).strip().lower() in {"1", "true", "on", "yes"}
            mgr = AutomationManager("automation_settings")
            ok = mgr.set_rule_enabled(switch_id, section="Advanced", rule_id=rule_id, enabled=truthy)
            if ok:
                data = mgr.load(switch_id) or {}
                adv = (data.get("Advanced") or {})
                payload = adv.get(rule_id) if isinstance(adv, dict) else None
                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                # Sync per-channel override flags based on aggregate enabled state
                try:
                    switch_mgr = SwitchSettingsManager("switch_settings")
                    for switch_key in switch_keys:
                        state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                        eff_enabled = bool(state.get("enabled_any", False))
                        sid, label, channel_index = _resolve_automation_target(switch_id, switch_key)
                        if sid and channel_index:
                            override_val = (not eff_enabled)
                            switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_val)
                            try:
                                sc = globals().get("switch_controllers")
                                if isinstance(sc, dict):
                                    for ctrl in sc.values():
                                        if getattr(ctrl, "switch_id", None) == sid:
                                            if isinstance(getattr(ctrl, "override_script", None), dict):
                                                ctrl.override_script[label] = override_val
                                elif sc and getattr(sc, "switch_id", None) == sid:
                                    if isinstance(getattr(sc, "override_script", None), dict):
                                        sc.override_script[label] = override_val
                            except Exception:
                                pass
                except Exception:
                    pass

                await _broadcast_automation_states(request.app, mgr, switch_id, switch_keys)
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/enable error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.post("/advanced/automations/delete", response_class=JSONResponse)
    async def api_delete_advanced_automation(
        request: Request,
        switch_id: str = Form(...),
        rule_id: str = Form(...),
    ):
        from .saiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("automation_settings")
            data = mgr.load(switch_id) or {}
            adv = (data.get("Advanced") or {})
            payload = adv.get(rule_id) if isinstance(adv, dict) else None
            switch_keys: set[str] = set()
            if isinstance(payload, dict):
                script_json = payload.get("script_json", "")
                try:
                    script = json.loads(str(script_json))
                    for action in (script.get("actions") or []):
                        if not isinstance(action, dict):
                            continue
                        switch_key = (action.get("switch_key") or "").strip()
                        if switch_key:
                            switch_keys.add(switch_key)
                except Exception:
                    pass

            ok = mgr.delete_rule(switch_id, section="Advanced", rule_id=rule_id)
            if ok:
                await _broadcast_automation_states(request.app, mgr, switch_id, switch_keys)
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/delete error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.post("/submit-advanced-trigger")
    async def submit_advanced_trigger(request: Request):
        """
        Persists an Advanced trigger script to automation_settings/automations.toml
        Accepts form or JSON payloads.

        Expected fields:
          - type            (required)
          - switch_id       (required)
          - channel         (e.g., "1") OR switch_selector (fallback)
          - rule_id         (optional; default: "SWITCH_<channel>_Advanced"; made unique if collides)
          - script_json     (required) JSON string built in the Switch Automations pane
          - enabled         (optional; default: true)
        """
        global _switch_status_cache_payload, _switch_status_cache_until
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        def norm_switch_id(raw: str) -> str:
            s = (raw or "").strip().replace(" ", "-")
            return "".join(ch for ch in s if ch.isalnum() or ch in "-_").lower() or "unknown-switch"

        def parse_bool(v: str | None, default: bool = True) -> bool:
            if v is None:
                return default
            return str(v).strip().lower() in ("1", "true", "on", "yes")

        def error_response(message: str, status_code: int = 400):
            if request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"ok": False, "error": message}, status_code=status_code)
            return HTMLResponse(f"<h3>{message}</h3><a href='/'>Return</a>", status_code=status_code)

        async def read_payload():
            ctype = request.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                j = await request.json()
                return {k: ("" if v is None else v) for k, v in j.items()}
            form = await request.form()
            return {k: ("" if v is None else v) for k, v in form.items()}

        # ---------- read & basic validation ----------
        payload = await read_payload()

        raw_switch_id = str(payload.get("switch_id", "")).strip()
        if not raw_switch_id:
            return HTMLResponse("<h3>Missing switch_id</h3><a href='/'>Return</a>", status_code=400)
        switch_id = norm_switch_id(raw_switch_id)

        # channel can come as 'channel' or 'switch_selector'
        channel_str = str(payload.get("channel", payload.get("switch_selector", "1"))).strip() or "1"
        try:
            channel = int(channel_str)
        except Exception:
            channel = 1

        # script JSON required
        script_json_raw = str(payload.get("script_json", "")).strip()
        if not script_json_raw:
            return HTMLResponse("<h3>Missing script_json</h3><a href='/'>Return</a>", status_code=400)

        # small size guard (64KB)
        if len(script_json_raw.encode("utf-8")) > 65536:
            return HTMLResponse("<h3>script_json too large</h3><a href='/'>Return</a>", status_code=413)

        enabled = parse_bool(str(payload.get("enabled", "true")))
        rule_id = str(payload.get("rule_id", "")).strip()

        # Load switch settings early; needed for action switch-key normalization.
        settings_mgr = SwitchSettingsManager("switch_settings")
        sw_doc = settings_mgr.load(switch_id) or {}
        switch_map = sw_doc.get("Switch") or {}

        # ---------- normalize/compact script ----------
        # Try to parse & normalize a bit; if it fails, store raw string (your runtime can validate later)
        is_json_request = request.headers.get("content-type", "").startswith("application/json")

        compact_script = script_json_raw
        try:
            parsed = json.loads(script_json_raw) if script_json_raw else {}

            def _num(v, typ=float, default=None):
                try:
                    return typ(v)
                except Exception:
                    return default

            # CONDITIONS (your UI already matches these keys)
            normalized_conditions = []
            save_anchor_epoch = int(time.time())
            for c in parsed.get("conditions", []) or []:
                cond_type = str(c.get("type", "")).strip().lower()

                # normalize days-of-week for time-of-day conditions (0–6 = Mon–Sun)
                raw_days = c.get("days") or []
                days_norm: list[int] = []
                for d in raw_days:
                    try:
                        n = int(d)
                    except Exception:
                        continue
                    if 0 <= n <= 6:
                        days_norm.append(n)

                duration_min = _num(c.get("duration_min"), int, None)
                freq_hours = _num(c.get("freq_hours"), int, None)
                period_min = _num(c.get("period_min"), int, None)
                if period_min is None and isinstance(freq_hours, int) and freq_hours > 0:
                    period_min = freq_hours * 60
                anchor_epoch = _num(c.get("anchor_epoch"), int, None)
                if cond_type == "timer":
                    if duration_min is None or duration_min <= 0:
                        return error_response("Timer duration must be at least 1 minute.", status_code=400)
                    if period_min is None or period_min <= 0:
                        return error_response("Timer Every value is invalid.", status_code=400)
                    if duration_min >= period_min:
                        return error_response("Timer duration must be less than Every.", status_code=400)
                    if period_min < 60:
                        anchor_epoch = save_anchor_epoch

                astral_event_raw = str(c.get("astral_event", c.get("event", "sunrise"))).strip().lower()
                astral_event_aliases = {
                    "sunrise-sunset": "sunrise_to_sunset",
                    "sunset-sunrise": "sunset_to_sunrise",
                }
                astral_event = astral_event_aliases.get(astral_event_raw, astral_event_raw)
                if astral_event not in {"sunrise", "sunset", "sunrise_to_sunset", "sunset_to_sunrise"}:
                    astral_event = "sunrise"

                normalized_condition = {
                    "type":   cond_type,  # 'sensor' / 'time' / 'astral' / 'timer' / 'or'
                    "sensor": str(c.get("sensor",  c.get("sensor_id", ""))).strip(),
                    "metric": str(c.get("metric",  "")).strip(),
                    "op":     str(c.get("op",      ">")).strip(),
                    "value":  _num(c.get("value"), float, None),
                    "hyst":   _num(c.get("hyst"),  float, None),
                    "start":  str(c.get("start",   "")).strip(),
                    "end":    str(c.get("end",     "")).strip(),
                    "astral_event": astral_event,
                    "offset_min": _num(c.get("offset_min", c.get("offset_minutes")), int, 0),
                    # new optional fields
                    "days":        days_norm or None,
                    "duration_min": duration_min,
                    "freq_hours":   freq_hours,
                    "period_min":   period_min,
                    "anchor_epoch": anchor_epoch,
                }
                if cond_type == "bd_transitions":
                    normalized_condition["executor_switch_id"] = norm_switch_id(
                        str(c.get("executor_switch_id", switch_id) or switch_id)
                    )
                normalized_conditions.append(normalized_condition)

            # ACTIONS: accept UI shape (switch_label/set) OR legacy (switch/state/delay)
            raw_actions = parsed.get("actions", [])
            if isinstance(raw_actions, dict):  # legacy single-action shape
                raw_actions = [raw_actions]

            normalized_actions = []
            label_to_channel_id: dict[str, str] = {}
            channel_num_to_label: dict[int, str] = {}
            try:
                for n in range(1, 33):
                    label = str((switch_map or {}).get(f"SWITCH_{n}_LABEL", "") or "").strip()
                    channel_id = str((switch_map or {}).get(f"SWITCH_{n}_CHANNEL_ID", "") or "").strip()
                    if label:
                        channel_num_to_label[n] = label
                        if channel_id:
                            label_to_channel_id[label.lower()] = channel_id
            except Exception:
                pass

            def _automation_action_key(raw_key: str) -> str:
                text = str(raw_key or "").strip()
                if not text:
                    return ""
                if "::" in text:
                    sid_part, suffix_part = text.split("::", 1)
                    sid_part = str(sid_part or switch_id).strip() or switch_id
                    suffix_part = str(suffix_part or "").strip()
                else:
                    sid_part = switch_id
                    suffix_part = text

                m = re.fullmatch(r"CH(\d+)", suffix_part, flags=re.IGNORECASE)
                if m:
                    label = channel_num_to_label.get(int(m.group(1)), "")
                    suffix_part = label or suffix_part

                channel_id = label_to_channel_id.get(suffix_part.lower(), "")
                suffix_final = channel_id or suffix_part
                return f"{sid_part}::{suffix_final}" if sid_part and suffix_final else ""

            for a in (raw_actions or []):
                action_type = str(a.get("type", "switch") or "switch").strip().lower()
                if action_type == "none":
                    normalized_actions.append({
                        "type": "none",
                        "executor_switch_id": norm_switch_id(
                            str(a.get("executor_switch_id", switch_id) or switch_id)
                        ),
                    })
                    continue
                if action_type == "notify":
                    notify_to = str(a.get("to", "") or "").strip()
                    executor_switch_id = norm_switch_id(
                        str(a.get("executor_switch_id", switch_id) or switch_id)
                    )
                    if "@" not in notify_to or "\r" in notify_to or "\n" in notify_to:
                        return error_response("Notify actions require a valid To email address.", status_code=400)
                    normalized_actions.append({
                        "type": "notify",
                        "to": notify_to,
                        "executor_switch_id": executor_switch_id,
                    })
                    continue
                raw_set = a.get("set", a.get("state", "off"))
                set_on = str(raw_set).strip().lower() in {"on", "true", "1"}
                switch_key = str(a.get("switch_key", a.get("switch_label", ""))).strip()
                switch_key = _automation_action_key(switch_key)
                raw_revert_action = str(a.get("revert_action", "") or "").strip().lower()
                revert_action = "do_nothing" if raw_revert_action == "do_nothing" else "previous_state"

                normalized_actions.append({
                    "type":       "switch",
                    "switch_key": switch_key,
                    "switch":     _num(a.get("switch"), int, None),  # optional numeric channel (legacy)
                    "set":        set_on,
                    "revert_action": revert_action,
                    "delay_s":    _num(a.get("delay_s", a.get("delay")), int, 0) or 0,
                })

            try:
                # Rewrite any action switch_key that references CHn or labels -> canonical switch_id::channel_id
                for a in normalized_actions:
                    if str(a.get("type", "switch") or "switch").strip().lower() == "switch":
                        a["switch_key"] = _automation_action_key(a.get("switch_key") or "")
            except Exception:
                pass

            normalized_logic = str(parsed.get("logic", "AND")).upper()
            if normalized_logic not in ("AND", "OR"):
                normalized_logic = "AND"

            normalized = {
                "logic": normalized_logic,
                "conditions": normalized_conditions,
                "actions": normalized_actions
            }

            compact_payload = {
                "name":       parsed.get("name", ""),
                "enabled":    bool(parsed.get("enabled", True)),
                "conditions": normalized_conditions,
                "actions":    normalized_actions,  #
                #"logic": normalized_logic,  # include only if your runtime still needs it
            }
            compact_script = json.dumps(compact_payload, separators=(",", ":"))
            printDM(f"[{MODULE}] Parsed Advanced script: {compact_script}", location=MODULE)
        except Exception as e:
            printDM(f"[{MODULE}] Advanced script JSON parse failed; storing raw. Error: {e}", location=MODULE)

        # ---------- verify/adjust channel against settings ----------
        # derive available indices from keys when CHANNELS missing
        indices = []
        for k in switch_map.keys():
            m = re.match(r"^SWITCH_(\d+)$", str(k))
            if m:
                try:
                    indices.append(int(m.group(1)))
                except Exception:
                    pass
        if not indices:
            # fallback to CHANNELS or at least [1]
            ch_count = 0
            try:
                ch_count = int(switch_map.get("CHANNELS", 0))
            except Exception:
                ch_count = 0
            indices = list(range(1, (ch_count or 1) + 1))

        if channel not in indices:
            # clamp to a valid one (prefer 1)
            channel = indices[0] if indices else 1
            printDM(f"[{MODULE}] Adjusted channel to {channel} for {switch_id}", location=MODULE)

        # ---------- rule_id handling ----------
        incoming_rule_id = rule_id
        if not rule_id:
            rule_id = f"SWITCH_{channel}_Advanced"

        # only auto-generated ids should be uniquified; explicit ids from the UI
        # represent an edit-in-place of an existing automation.
        def _unique_rule_id(existing_ids: set[str], base: str) -> str:
            if base not in existing_ids:
                return base
            i = 2
            while True:
                cand = f"{base}_{i}"
                if cand not in existing_ids:
                    return cand
                i += 1

        try:
            trig_mgr = AutomationManager("automation_settings")
            existing = trig_mgr.load(switch_id) or {}
            existing_advanced = (existing or {}).get("Advanced", {}) or {}
            existing_ids = set(existing_advanced.keys())
            final_rule_id = rule_id if incoming_rule_id else _unique_rule_id(existing_ids, rule_id)
            previous_rule = (
                existing_advanced.get(final_rule_id)
                if isinstance(existing_advanced, dict)
                else None
            )
            trig_mgr.upsert_advanced_rule(
                hostname=switch_id,
                rule_id=final_rule_id,
                enabled=enabled,
                script=compact_script,
            )

            printDM(f"[{MODULE}] Saved Advanced trigger {rule_id} -> {final_rule_id} for {switch_id}", location=MODULE)
            # The system-wide editor persists under "__system__", while actions
            # target real switch IDs. Reload each old/new action target so edits
            # take effect without restarting Sensorius.
            def _rule_target_switch_ids(rule_payload: object) -> set[str]:
                if not isinstance(rule_payload, dict):
                    return set()
                script_payload = rule_payload.get("script_json", "")
                try:
                    script_doc = (
                        script_payload
                        if isinstance(script_payload, dict)
                        else json.loads(str(script_payload))
                    )
                except Exception:
                    return set()
                target_ids: set[str] = set()
                for action in (script_doc.get("actions") or []):
                    if not isinstance(action, dict):
                        continue
                    action_key = str(action.get("switch_key", "") or "").strip()
                    if "::" not in action_key:
                        continue
                    target_id = action_key.split("::", 1)[0].strip()
                    if target_id:
                        target_ids.add(target_id)
                return target_ids

            reload_switch_ids = _rule_target_switch_ids(previous_rule)
            reload_switch_ids.update(
                _rule_target_switch_ids({"script_json": compact_script})
            )
            if switch_id != "__system__":
                reload_switch_ids.add(switch_id)

            try:
                sc = getattr(request.app.state, "switch_controllers", None)
                if not isinstance(sc, dict):
                    sc = globals().get("switch_controllers")
                if not isinstance(sc, dict):
                    sc = {}
                globals()["switch_controllers"] = sc
                request.app.state.switch_controllers = sc

                eval_controllers: list[object] = []
                switch_mgr = SwitchSettingsManager("switch_settings")
                sensor_source = getattr(request.app.state, "sensor_map", None)
                if sensor_source is None:
                    sensor_source = globals().get("sensor_map") or []
                sensor_candidates = (
                    sensor_source.values()
                    if isinstance(sensor_source, dict)
                    else sensor_source
                )

                for target_switch_id in sorted(reload_switch_ids):
                    found_ctrl = next(
                        (
                            ctrl
                            for ctrl in sc.values()
                            if str(getattr(ctrl, "switch_id", "") or "").strip().lower()
                            == target_switch_id.lower()
                        ),
                        None,
                    )

                    if found_ctrl is None:
                        sw_doc = switch_mgr.load(target_switch_id) or {}
                        if isinstance(sw_doc, dict) and (sw_doc.get("Switch") or {}):
                            from .saiSwitch import build_switch_controller

                            sw_loc = str(
                                (sw_doc.get("Switch") or {}).get(
                                    "SWITCH_LOCATION", ""
                                )
                                or ""
                            ).strip().lower()
                            sensor_match = None
                            for candidate in (sensor_candidates or []):
                                try:
                                    if (
                                        str(
                                            getattr(candidate, "location", "") or ""
                                        ).strip().lower()
                                        == sw_loc
                                    ):
                                        sensor_match = candidate
                                        break
                                except Exception:
                                    continue

                            candidate_ctrl = build_switch_controller(
                                switch_settings=sw_doc,
                                supervisor=None,
                                sensor=sensor_match,
                                data_logger=data_logger,
                            )
                            if bool(getattr(candidate_ctrl, "is_present", False)):
                                sc[target_switch_id] = candidate_ctrl
                                found_ctrl = candidate_ctrl

                    if found_ctrl is None:
                        continue

                    rules_cache = getattr(found_ctrl, "_rules_cache", None)
                    if isinstance(rules_cache, dict):
                        rules_cache["mtime"] = None
                    eval_controllers.append(found_ctrl)

                    monitor_name = f"{target_switch_id} Controladora Monitor"
                    supervisor = getattr(request.app.state, "supervisor", None)
                    supervised_names = getattr(supervisor, "_task_names", set())
                    existing_task = _dynamic_switch_monitor_tasks.get(
                        target_switch_id
                    )
                    if (
                        monitor_name not in supervised_names
                        and (existing_task is None or existing_task.done())
                    ):
                        _dynamic_switch_monitor_tasks[target_switch_id] = (
                            asyncio.create_task(
                                found_ctrl.run_controladora_monitor(
                                    getattr(found_ctrl, "sensor", None)
                                ),
                                name=f"{monitor_name} (dynamic)",
                            )
                        )
                        printDM(
                            f"[{MODULE}] started dynamic switch monitor for "
                            f"{target_switch_id}",
                            location=MODULE,
                        )

                # Evaluate immediately; the monitor remains the steady-state path.
                for eval_ctrl in eval_controllers:
                    current_values_map = getattr(eval_ctrl, "values", {}) or {}
                    eval_advanced = getattr(
                        eval_ctrl, "_evaluate_and_apply_advanced", None
                    )
                    if callable(eval_advanced):
                        eval_advanced(current_values_map)

                _switch_status_cache_payload = None
                _switch_status_cache_until = 0.0
            except Exception as _reload_exc:
                if DEBUG:
                    printDM(
                        f"[{MODULE}] live automation reload failed: {_reload_exc}",
                        location=MODULE,
                    )

            # Sync per-channel override flags based on aggregate enabled state.
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                payload = None
                try:
                    data = trig_mgr.load(switch_id) or {}
                    adv = (data.get("Advanced") or {})
                    payload = adv.get(final_rule_id) if isinstance(adv, dict) else None
                except Exception:
                    payload = None

                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                for switch_key in switch_keys:
                    state = trig_mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                    eff_enabled = bool(state.get("enabled_any", False))
                    sid, label, channel_index = _resolve_automation_target(switch_id, switch_key)
                    if sid and channel_index:
                        override_val = (not eff_enabled)
                        switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_val)
                        try:
                            sc = globals().get("switch_controllers")
                            if isinstance(sc, dict):
                                for ctrl in sc.values():
                                    if getattr(ctrl, "switch_id", None) == sid and isinstance(getattr(ctrl, "override_script", None), dict):
                                        ctrl.override_script[label] = override_val
                            elif sc and getattr(sc, "switch_id", None) == sid and isinstance(getattr(sc, "override_script", None), dict):
                                sc.override_script[label] = override_val
                        except Exception:
                            pass
            except Exception:
                pass

            # Broadcast updated automation state so UI button reflects enabled/disabled without refresh.
            try:
                app = request.app
                from .saiAutomationManager import AutomationManager
                mgr = AutomationManager("automation_settings")
                try:
                    data = mgr.load(switch_id) or {}
                    adv = (data.get("Advanced") or {}) if isinstance(data, dict) else {}
                    payload = adv.get(final_rule_id) if isinstance(adv, dict) else None
                except Exception:
                    payload = None

                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                await _broadcast_automation_states(app, mgr, switch_id, switch_keys)
            except Exception:
                pass
        except Exception as e:
            printDM(f"[{MODULE}] ⚠️ Failed to save Advanced trigger {rule_id} for {switch_id}: {e}", location=MODULE)
            if is_json_request:
                return JSONResponse({"ok": False, "error": str(e) or "Failed saving Advanced trigger."}, status_code=500)
            return HTMLResponse("<h3>Failed saving Advanced trigger.</h3><a href='/'>Return</a>", status_code=500)

        # Success response (JSON for JSON requests; redirect for browser form posts)
        if is_json_request:
            return JSONResponse({"ok": True, "switch_id": switch_id, "rule_id": final_rule_id})
        return RedirectResponse(url="/", status_code=303)

    @router.get("/switch-status-update")
    async def switch_status_api():
        """
        Returns a JSON object keyed by *UI* switch key "<switch_id>::<label>".
          {
            "sensoria-hub-0::Light": {
              "state": <bool>,
              "time": ["On 2025-08-11 12:34:56", ...]  # up to 5, chronological
            },
            "switch-4oz31s::Fan": { ... }
          }
        """

        global _switch_status_cache_payload, _switch_status_cache_until
        now = time.monotonic()
        if _switch_status_cache_payload is not None and now < _switch_status_cache_until:
            return JSONResponse(_switch_status_cache_payload)

        ctrl_by_switch_id: dict[str, object] = {}
        try:
            if switch_controllers and isinstance(switch_controllers, dict):
                for ctrl in switch_controllers.values():
                    sid = str(getattr(ctrl, "switch_id", "") or "").strip()
                    if sid:
                        ctrl_by_switch_id[sid] = ctrl
        except Exception:
            ctrl_by_switch_id = {}

        def _timer_snapshot(sid: str, label: str) -> dict:
            try:
                ctrl = ctrl_by_switch_id.get(str(sid or "").strip())
                getter = getattr(ctrl, "get_auto_off_status", None)
                if callable(getter):
                    return dict(getter(label) or {})
            except Exception:
                pass
            return {
                "timer_seconds": 0,
                "timer_enabled": False,
                "timer_deadline_epoch": None,
                "timer_remaining_s": 0,
            }
        def _build_switch_status_payload_sync(
            local_entries: list[dict[str, object]],
            remote_cache: dict[str, dict],
            identity_rows: list[dict[str, object]],
        ) -> dict[str, dict]:
            states: dict[str, dict] = {}
            pending_set = copy.deepcopy(getattr(mqtt_ingest, "_pending_set", {}) or {})
            pending_ttl_s = 15.0

            def _event_origin_tag(source: object) -> str:
                raw = str(source or "").strip()
                src = raw.lower()
                if not src:
                    return ""
                if src.startswith("mqtt-auto:"):
                    detail = raw.split(":", 1)[1].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src.startswith("mqtt-manual"):
                    return "manual"
                if src.startswith("auto/rule:"):
                    detail = raw.split(":", 1)[1].strip()
                    if detail.lower().endswith("/mqtt"):
                        detail = detail[:-5].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src == "ui" or any(token in src for token in ("manual", "/ui", "ui/", "user")):
                    return "manual"
                if any(token in src for token in ("auto", "rule", "timer", "automation", "schedule")):
                    return "auto"
                return ""

            def _format_events(switch_key: str, sensor_id: str | None, limit: int = 5) -> list[str]:
                evs = data_logger.get_last_switch_events(
                    switch_key,
                    sensor_id=sensor_id,
                    limit=limit,
                    include_source=True,
                )
                out: list[str] = []
                for state_str, ts, source in evs:
                    label = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                    origin_tag = _event_origin_tag(source)
                    suffix = f" ({origin_tag})" if origin_tag else ""
                    out.append(f"{label} {ts}{suffix}")
                return out

            def _format_events_remote(switch_key: str, limit: int = 5) -> list[str]:
                return _format_events(switch_key, None, limit=limit)

            known_channel_ids_by_sid: dict[str, set[str]] = {}
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                ch = _switch_channel_id_from_identity_row(row)
                if not sid or not ch:
                    continue
                known_channel_ids_by_sid.setdefault(sid, set()).add(ch.lower())

            def _db_key_for_label(sid: str, label: str) -> str:
                try:
                    target_sid = (sid or "").strip().lower()
                    target_label = (label or "").strip().lower()
                    for row in (identity_rows or []):
                        rsid = str(row.get("switch_id", "")).strip().lower()
                        rlab = str(row.get("label", "")).strip().lower()
                        if rsid == target_sid and rlab == target_label:
                            sk = str(row.get("switch_key", "")).strip()
                            if sk:
                                return sk
                except Exception:
                    pass
                try:
                    return _switch_key(sid, label)
                except Exception:
                    return f"{sid}::{label}"

            def _cache_state_for(sid: str, label: str, db_key: str) -> bool | None:
                try:
                    ch_map = remote_cache.get(sid, {}) or {}
                    human_state = ch_map.get(label)
                    if human_state is None:
                        ch_id = _resolve_channel_id_from_label(sid, label) or ""
                        human_state = ch_map.get(ch_id)
                    if human_state is None:
                        return None
                    txt = str(human_state).strip().lower()
                    if txt == "on":
                        return True
                    if txt == "off":
                        return False
                except Exception:
                    pass
                return None

            def _pending_state_for(sid: str, label: str, db_key: str) -> bool | None:
                try:
                    now_ts = time.time()
                    pending = pending_set.get((str(sid or ""), str(label or "")))
                    if pending is None:
                        channel_id = _resolve_channel_id_from_label(sid, label) or ""
                        for (psid, plabel), meta in pending_set.items():
                            if str(psid or "").strip() != str(sid or "").strip():
                                continue
                            meta_channel = str((meta or {}).get("channel_id") or "").strip()
                            if meta_channel and meta_channel == channel_id:
                                pending = meta
                                break
                    if not isinstance(pending, dict):
                        return None
                    pending_ts = float(pending.get("ts") or 0.0)
                    if pending_ts <= 0.0 or (now_ts - pending_ts) > pending_ttl_s:
                        return None
                    if "state" not in pending:
                        return None
                    return bool(pending.get("state"))
                except Exception:
                    return None

            def _remote_liveness_payload(sid: str) -> dict:
                try:
                    sid_text = str(sid or "").strip()
                    if not sid_text:
                        return {}
                    dev_map = getattr(mqtt_ingest, "device_type", {}) or {}
                    host_to_peer_ids = getattr(mqtt_ingest, "host_to_peer_ids", {}) or {}
                    looks_remote = (
                        sid_text.startswith("switch-")
                        or str(dev_map.get(sid_text) or "").strip().lower() == "nodus"
                        or sid_text in (remote_cache or {})
                        or any(sid_text in (peers or []) for peers in host_to_peer_ids.values())
                    )
                    if not looks_remote:
                        return {}
                    getter = getattr(mqtt_ingest, "get_nodus_liveness", None)
                    if not callable(getter):
                        return {}
                    snapshot = getter(sid_text, device_type="switch")
                    state = str(snapshot.get("state") or "unknown").strip().lower()
                    return {
                        "availability": state,
                        "online": state == "online",
                        "liveness_reason": str(snapshot.get("reason") or ""),
                        "last_seen_s": snapshot.get("last_seen_s"),
                        "last_heartbeat_s": snapshot.get("last_heartbeat_s"),
                    }
                except Exception:
                    return {}

            for entry in local_entries:
                switch_id = str(entry.get("switch_id", "") or "").strip()
                label = str(entry.get("label", "") or "").strip()
                ui_key = str(entry.get("ui_key", "") or "").strip()
                db_key = str(entry.get("db_key", "") or "").strip() or ui_key
                sensor_lineage = str(entry.get("sensor_lineage", "") or "").strip() or None
                live_bool = bool(entry.get("live_bool", False))
                events = _format_events(db_key, sensor_lineage, limit=5)
                payload = {"state": live_bool, "time": events}
                if DEBUG and switch_id.lower() == "sensoria-hub-0" and label.lower() == "fan":
                    printDM(
                        f"[switch-status-update] {ui_key} live={live_bool} last_state={bool(entry.get('last_state', False))} db_key={db_key} events_head={(events[-1] if events else 'none')}",
                        location=MODULE,
                    )
                states[db_key] = dict(payload)
                if ui_key and ui_key not in states:
                    states[ui_key] = dict(payload)

            seen_ui_keys: set[str] = set()
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                label = str(row.get("label", "")).strip()
                db_key = str(row.get("switch_key", "")).strip()
                if not (sid and label and db_key):
                    continue
                ui_key = f"{sid}::{label}"
                cached_bool = _cache_state_for(sid, label, db_key)
                pending_bool = _pending_state_for(sid, label, db_key)
                if pending_bool is not None:
                    latest_bool = pending_bool
                elif cached_bool is not None:
                    latest_bool = cached_bool
                else:
                    latest = data_logger.get_latest_switch_state(db_key)
                    latest_bool = (latest == "On") if latest is not None else False
                events = _format_events_remote(db_key, limit=5) or _format_events(db_key, None, limit=5)
                payload = {"state": latest_bool, "time": events}
                payload.update(_remote_liveness_payload(sid))
                states[db_key] = dict(payload)
                states[ui_key] = dict(payload)
                seen_ui_keys.add(ui_key)
                ch_id = _switch_channel_id_from_identity_row(row)
                alias_key = f"{ch_id}::{label}" if ch_id else ""
                if alias_key and alias_key not in states:
                    states[alias_key] = dict(payload)
                    seen_ui_keys.add(alias_key)

            for remote_switch_id, ch_map in (remote_cache or {}).items():
                if not isinstance(ch_map, dict):
                    continue
                for channel_label, human_state in ch_map.items():
                    if str(channel_label or "").strip().lower() in known_channel_ids_by_sid.get(str(remote_switch_id), set()):
                        continue
                    ui_key = f"{remote_switch_id}::{channel_label}"
                    if ui_key in seen_ui_keys:
                        continue
                    db_key = _db_key_for_label(remote_switch_id, channel_label)
                    cached_bool = _cache_state_for(remote_switch_id, channel_label, db_key)
                    pending_bool = _pending_state_for(remote_switch_id, channel_label, db_key)
                    if pending_bool is not None:
                        latest_bool = pending_bool
                    elif cached_bool is not None:
                        latest_bool = cached_bool
                    else:
                        latest = data_logger.get_latest_switch_state(db_key)
                        latest_bool = (latest == "On") if latest is not None else (str(human_state).lower() == "on")
                    events = _format_events_remote(db_key, limit=5) or _format_events(db_key, None, limit=5)
                    payload = {"state": latest_bool, "time": events}
                    payload.update(_remote_liveness_payload(remote_switch_id))
                    if db_key:
                        states[db_key] = dict(payload)
                    states[ui_key] = payload

            return states

        try:
            local_entries: list[dict[str, object]] = []
            timer_snapshots: dict[str, dict] = {}

            if switch_controllers and isinstance(switch_controllers, dict):
                for ctrl in switch_controllers.values():
                    if not getattr(ctrl, "is_present", False):
                        continue
                    if not isinstance(getattr(ctrl, "last_state", None), dict):
                        continue

                    switch_id = str(getattr(ctrl, "switch_id", "") or "").strip()
                    if not switch_id:
                        continue
                    sensor_lineage = f"Switch_{switch_id}"
                    for label, is_on in ctrl.last_state.items():
                        try:
                            db_key = ctrl._switch_key(label)
                        except Exception:
                            db_key = _switch_key(switch_id, label)
                        ui_key = f"{switch_id}::{label}"
                        try:
                            live_bool = bool(ctrl.get_state(label))
                        except Exception:
                            live_bool = bool(is_on)
                        timer_info = _timer_snapshot(switch_id, label)
                        timer_snapshots[ui_key] = dict(timer_info)
                        canonical_key = str(db_key or "").strip() or ui_key
                        timer_snapshots[canonical_key] = dict(timer_info)
                        local_entries.append({
                            "switch_id": switch_id,
                            "label": label,
                            "ui_key": ui_key,
                            "db_key": db_key,
                            "sensor_lineage": sensor_lineage,
                            "live_bool": live_bool,
                            "last_state": bool(is_on),
                        })

            remote_cache = copy.deepcopy(getattr(mqtt_ingest, "_switch_state_cache", {}) or {})
            identity_rows = await asyncio.to_thread(lambda: list(data_logger.get_switch_identities() or []))
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                label = str(row.get("label", "")).strip()
                db_key = str(row.get("switch_key", "")).strip()
                if not (sid and label):
                    continue
                ui_key = f"{sid}::{label}"
                timer_info = _timer_snapshot(sid, label)
                timer_snapshots[ui_key] = dict(timer_info)
                if db_key:
                    timer_snapshots[db_key] = dict(timer_info)
                if db_key and "::" in db_key:
                    ch_id = _switch_channel_id_from_identity_row(row)
                    alias_key = f"{ch_id}::{label}" if ch_id else ""
                    if alias_key:
                        timer_snapshots[alias_key] = dict(timer_info)
            for remote_switch_id, ch_map in remote_cache.items():
                if not isinstance(ch_map, dict):
                    continue
                for channel_label in ch_map.keys():
                    ui_key = f"{remote_switch_id}::{channel_label}"
                    timer_snapshots.setdefault(ui_key, dict(_timer_snapshot(remote_switch_id, str(channel_label or ""))))

            states = await asyncio.to_thread(
                _build_switch_status_payload_sync,
                local_entries,
                remote_cache,
                identity_rows,
            )
            for key, timer_info in timer_snapshots.items():
                if key in states:
                    states[key].update(timer_info)

            _switch_status_cache_payload = states
            _switch_status_cache_until = time.monotonic() + _SWITCH_STATUS_CACHE_TTL_SEC
            return JSONResponse(states)

        except Exception as e:
            printDM(f"switch-status-update error: {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/switch/toggle")
    async def toggle_switch(
        request: Request,
        switch_name: str = Query(...),        # legacy: label-only still supported
        switch_key: str | None = Query(None), # new: "switch_id::channel_id"
        switch_id: str | None = Query(None),  # new: switch_id sent separately
        ):
        """
        Toggle a switch identified by either:
          1) switch_key="switch_id::channel_id"  (preferred)
          2) switch_id + switch_name        (explicit device + label)
          3) switch_name="<label>"          (legacy; ambiguous if duplicates exist)
        """
        import time
        global _switch_status_cache_payload, _switch_status_cache_until
        _require_protected_access(request, require_csrf=True)

        def _norm_label(s: str | None) -> str | None:
            return s.strip() if s else None

        def _norm_switch_id(s: str | None) -> str | None:
            return s.strip() if s else None

        def _ctrl_switch_id(ctrl) -> str | None:
            val = getattr(ctrl, "switch_id", None)
            if isinstance(val, str) and val.strip():
                return val.strip()
            return None

        def _ctrl_labels(ctrl) -> list[str]:
            """
            Support both full SwitchController objects and remote presenter objects.
            """
            try:
                getter = getattr(ctrl, "get_switch_names", None)
                if callable(getter):
                    raw = getter() or []
                else:
                    raw = getattr(ctrl, "switches", []) or []
                return [str(s).strip() for s in raw if str(s).strip()]
            except Exception:
                return []

        def _looks_remote(ctrl) -> bool:
            # Heuristics that cover MQTTSwitch and your remote ids
            return (
                bool(getattr(ctrl, "switch_topics", None)) or
                bool(getattr(getattr(ctrl, "switch", None), "switch_topics", None)) or
                str(getattr(ctrl, "switch_id", "")).startswith("switch-")
            )

        def _remote_liveness_snapshot(target_id: str | None, *, channel_id: str | None = None) -> dict:
            try:
                getter = getattr(mqtt_ingest, "get_nodus_liveness", None)
                if callable(getter):
                    return dict(getter(str(target_id or channel_id or "").strip(), device_type="switch") or {})
            except Exception:
                pass
            return {"state": "unknown", "reason": "liveness_unavailable"}

        def _remote_liveness_block_response(target_id: str | None, *, channel_id: str | None = None):
            snapshot = _remote_liveness_snapshot(target_id, channel_id=channel_id)
            state = str(snapshot.get("state") or "unknown").strip().lower()
            if state == "online":
                return None
            return JSONResponse(
                {
                    "error": "device_offline",
                    "message": "Nodus device is not reporting; wait for heartbeat or data before toggling.",
                    "switch_id": str(target_id or ""),
                    "channel_id": str(channel_id or ""),
                    "availability": state,
                    "liveness_reason": str(snapshot.get("reason") or ""),
                    "last_seen_s": snapshot.get("last_seen_s"),
                    "last_heartbeat_s": snapshot.get("last_heartbeat_s"),
                },
                status_code=503,
            )

        # even if the db does not have a state value, set the state
        def _desired_toggle_from_db(
            data_logger,
            switch_id: str | None,
            label: str,
            ctrl,
            *,
            prefer_live_current: bool = False,
            live_current: bool | None = None,
        ) -> bool:
            """
            Returns desired new_state:
            - For remote/Nodus switches, flip the live/cache state first.
            - Else if DB has a latest state (using the controller's canonical DB key): flip it.
            - Else: flip the *actual controller* state so the first click always changes something.
            """
            if prefer_live_current and live_current is not None:
                new_state = not bool(live_current)
                if DEBUG:
                    printDM(
                        f"[toggle_switch] live state {switch_id}::{label}={bool(live_current)!r} -> new_state={new_state}",
                        location="saiWebRoutes",
                    )
                return new_state

            # 1) Prefer DB if present, using ctrl._switch_key() so the key matches what logging uses
            try:
                switch_key = None
                try:
                    # Canonical DB identity: uses saiDataLogger.build_switch_key under the hood
                    switch_key = ctrl._switch_key(label)
                except Exception:
                    # Fallback for older controllers: best-effort label-based key
                    if switch_id:
                        switch_key = f"{switch_id}::{label}"

                if switch_key:
                    last = data_logger.get_latest_switch_state(switch_key)  # "On" | "Off" | None
                    if last is not None:
                        last_on = (isinstance(last, str) and last.strip().lower() == "on")
                        new_state = (not last_on)
                        if DEBUG:
                            printDM(
                                f"[toggle_switch] DB says {switch_key}={last!r} → new_state={new_state}",
                                location="saiWebRoutes",
                            )
                        return new_state
            except Exception as e:
                printDM(
                    f"[toggle_switch] DB lookup failed for {switch_id}::{label}: {e}",
                    location="saiWebRoutes",
                )

            # 2) Fallback: flip the controller’s live state
            try:
                live_on = bool(ctrl.get_state(label))
                return (not live_on)
            except Exception:
                # Last-ditch default: turn ON
                return True

        try:
            label_raw     = _norm_label(switch_name)
            key_raw       = _norm_label(switch_key)
            switch_id_raw = _norm_switch_id(switch_id)
            key_suffix_raw = None

            if DEBUG:
                printDM(f"[toggle_switch] Requested: '{switch_name}', key={switch_key}, switch_id={switch_id}", location=MODULE)

            if not label_raw and not key_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            # Resolve from switch_key if provided
            if key_raw and "::" in key_raw:
                sid_part, label_part = key_raw.split("::", 1)
                switch_id_raw = _norm_switch_id(sid_part) or switch_id_raw
                key_suffix_raw = _norm_label(label_part)
                label_raw     = _norm_label(label_part) or label_raw

            if not label_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            try:
                identity_rows = list(data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows = []

            def _label_from_key_suffix(sid_value: str | None, suffix_value: str | None) -> str | None:
                sid_l = str(sid_value or "").strip().lower()
                suffix_l = str(suffix_value or "").strip().lower()
                if not sid_l or not suffix_l:
                    return None
                for row in identity_rows:
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    if r_sid != sid_l:
                        continue
                    r_label = str(row.get("label", "")).strip()
                    r_channel = _switch_channel_id_from_identity_row(row)
                    if suffix_l in {r_label.lower(), r_channel.lower()} and r_label:
                        return r_label
                return None

            resolved_label = _label_from_key_suffix(switch_id_raw, key_suffix_raw)
            if resolved_label:
                label_raw = resolved_label
            label_q_lower = label_raw.lower()

            def _switch_id_matches(ctrl, wanted_sid: str | None, label: str) -> bool:
                """
                Match explicit switch_id against controller id and known aliases:
                - host switch_id (e.g. switch-en1n8i)
                - channel_id from switch_key suffix (e.g. S1-en1n8i)
                """
                if not wanted_sid:
                    return True

                wanted_l = wanted_sid.lower()
                ctrl_sid = _ctrl_switch_id(ctrl)
                if ctrl_sid and ctrl_sid.lower() == wanted_l:
                    return True

                aliases: set[str] = set()
                if ctrl_sid:
                    aliases.add(ctrl_sid.lower())

                try:
                    ch_map = getattr(ctrl, "channel_id_for_label", {}) or {}
                    channel_id = str(ch_map.get(label, "") or "").strip().lower()
                    if channel_id:
                        aliases.add(channel_id)
                except Exception:
                    pass

                label_l = (label or "").strip().lower()
                for row in identity_rows:
                    r_label = str(row.get("label", "")).strip().lower()
                    if r_label != label_l:
                        continue
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    r_channel = _switch_channel_id_from_identity_row(row).lower()

                    # Bridge either direction if controller id is host-id or channel-id.
                    if ctrl_sid and r_sid == ctrl_sid and r_channel:
                        aliases.add(r_channel)
                    if ctrl_sid and r_channel and r_channel == ctrl_sid and r_sid:
                        aliases.add(r_sid)

                return wanted_l in aliases

            # ---- Find matching controllers ----
            sc_map = switch_controllers if isinstance(switch_controllers, dict) else {}
            matches: list[tuple[object, str]] = []

            for ctrl in sc_map.values():
                ctrl_labels = _ctrl_labels(ctrl)
                if not ctrl_labels:
                    if DEBUG:
                        printDM("[toggle_switch] skipping switch with no labels", location=MODULE)
                    continue

                match_label = next((lbl for lbl in ctrl_labels if (lbl or "").lower() == label_q_lower), None)
                if not match_label:
                    continue

                if switch_id_raw and not _switch_id_matches(ctrl, switch_id_raw, match_label):
                    continue

                matches.append((ctrl, match_label))

            if not matches:
                # Direct channel-ID fallback for remote Nodus switches (e.g., S1-xxxxxx).
                try:
                    looks_channel = bool(re.match(r"^S\d+-[A-Za-z0-9._-]+$", str(switch_id_raw or "").strip()))
                    if looks_channel and mqtt_ingest:
                        channel_id = str(switch_id_raw or "").strip()
                        resolved_sid = ""
                        resolved_db_key = ""
                        resolved_label = label_raw or channel_id
                        for row in identity_rows:
                            r_key = str(row.get("switch_key", "") or "").strip()
                            r_sid = str(row.get("switch_id", "") or "").strip()
                            r_label = str(row.get("label", "") or "").strip()
                            r_channel = _switch_channel_id_from_identity_row(row)
                            if not r_channel:
                                continue
                            if r_channel.lower() != channel_id.lower():
                                continue
                            if label_raw and r_label and r_label.lower() != label_raw.lower():
                                continue
                            resolved_sid = r_sid or resolved_sid
                            resolved_db_key = r_key or resolved_db_key
                            resolved_label = r_label or resolved_label
                            if resolved_sid and resolved_db_key:
                                break

                        current_on = None
                        try:
                            cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
                            if resolved_sid:
                                raw_state = (cache.get(resolved_sid, {}) or {}).get(channel_id)
                                if raw_state is None:
                                    raw_state = (cache.get(resolved_sid, {}) or {}).get(channel_id.lower())
                                if raw_state is not None:
                                    current_on = (str(raw_state).strip().lower() == "on")
                            if current_on is None:
                                for _sid, chmap in cache.items():
                                    if not isinstance(chmap, dict):
                                        continue
                                    raw_state = chmap.get(channel_id)
                                    if raw_state is None:
                                        raw_state = chmap.get(channel_id.lower())
                                    if raw_state is not None:
                                        current_on = (str(raw_state).strip().lower() == "on")
                                        break
                        except Exception:
                            current_on = None

                        if current_on is None and resolved_db_key:
                            try:
                                last = data_logger.get_latest_switch_state(resolved_db_key)
                                if last is not None:
                                    current_on = (str(last).strip().lower() == "on")
                            except Exception:
                                current_on = None

                        new_state = (not current_on) if current_on is not None else True
                        blocked = _remote_liveness_block_response(resolved_sid or "", channel_id=channel_id)
                        if blocked is not None:
                            return blocked
                        ok = bool(mqtt_ingest.set_switch_by_channel_id(resolved_sid or "", channel_id, new_state))
                        if ok:
                            ts = time.time()
                            # Remote fallback path: do not persist UI-originated events.
                            # sw_events should reflect only confirmed MQTT state/event messages.
                            # Invalidate short-lived switch status cache after a state change request.
                            _switch_status_cache_payload = None
                            _switch_status_cache_until = 0.0
                            return {"state": bool(new_state), "time": ts}
                except Exception as e:
                    printDM(f"[toggle_switch] channel fallback failed: {e}", location=MODULE)

                if DEBUG:
                    printDM(f"[toggle_switch] No match found for: label='{label_raw}', switch_id='{switch_id_raw}'", location=MODULE)
                return JSONResponse({"error": "switch_not_found"}, status_code=404)

            if len(matches) > 1 and not switch_id_raw:
                options = []
                for ctrl, _ml in matches:
                    try:
                        options.append({
                            "location": getattr(ctrl, "location", None),
                            "labels": _ctrl_labels(ctrl),
                            "switch_id": getattr(ctrl, "switch_id", None),
                        })
                    except Exception:
                        pass
                return JSONResponse(
                    {
                        "error": "ambiguous_switch",
                        "message": "Multiple devices have this label. Provide switch_key='switch_id::channel_id' or pass switch_id.",
                        "options": options,
                    },
                    status_code=409,
                )

            # Target resolved
            ctrl, matched_label = matches[0]
            # Use the controller-resolved id for command publish. The incoming
            # switch_id can be an alias (e.g. channel_id) used only for matching.
            sid = _ctrl_switch_id(ctrl) or switch_id_raw or getattr(ctrl, "switch_id", None)

            # Read current state; fall back to controller cache if needed
            try:
                current = bool(ctrl.get_state(matched_label))
            except Exception:
                current = bool((getattr(ctrl, "last_state", {}) or {}).get(matched_label, False))

            # Decide path: direct GPIO vs remote/MQTT. Remote toggles should
            # invert the live Nodus cache/controller state before consulting
            # historical DB rows, because old bad parses or missed retained
            # events can leave sw_events stale.
            remote = _looks_remote(ctrl)

            # Manual toggles are blocked while any Advanced automation for this switch is enabled.
            try:
                from .saiAutomationManager import AutomationManager
                am = AutomationManager("automation_settings")
                automation_switch_id = _ctrl_switch_id(ctrl) or sid or ""
                try:
                    channel_id = str((getattr(ctrl, "channel_id_for_label", {}) or {}).get(matched_label, "") or "").strip()
                except Exception:
                    channel_id = ""
                suffix = channel_id or matched_label
                automation_switch_key = (
                    f"{automation_switch_id}::{suffix}" if automation_switch_id else f"::{suffix}"
                )
                automation_state = am.get_advanced_state_for_switch_key(automation_switch_id, automation_switch_key)
                if bool(automation_state.get("enabled_any", False)):
                    return JSONResponse(
                        {
                            "error": "automation_enabled",
                            "message": "Automation is enabled for this switch. Disable automation before toggling manually.",
                            "switch_id": automation_switch_id,
                            "label": matched_label,
                        },
                        status_code=423,
                    )
            except Exception:
                pass

            new_state = _desired_toggle_from_db(
                data_logger,
                sid,
                matched_label,
                ctrl,
                prefer_live_current=remote,
                live_current=current,
            )
            response_state = bool(new_state)

            ok = False

            def _event_origin_tag(source: object) -> str:
                raw = str(source or "").strip()
                src = raw.lower()
                if not src:
                    return ""
                if src.startswith("mqtt-auto:"):
                    detail = raw.split(":", 1)[1].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src.startswith("mqtt-manual"):
                    return "manual"
                if src.startswith("auto/rule:"):
                    detail = raw.split(":", 1)[1].strip()
                    if detail.lower().endswith("/mqtt"):
                        detail = detail[:-5].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src == "ui" or any(token in src for token in ("manual", "/ui", "ui/", "user")):
                    return "manual"
                if any(token in src for token in ("auto", "rule", "timer", "automation", "schedule")):
                    return "auto"
                return ""

            def _recent_events_payload(limit: int = 5) -> list[str]:
                if remote or not sid:
                    return []
                try:
                    db_key = ctrl._switch_key(matched_label)
                except Exception:
                    db_key = f"{sid}::{matched_label}"
                sensor_lineage = f"Switch_{sid}" if sid else None
                rows = data_logger.get_last_switch_events(
                    db_key,
                    sensor_id=sensor_lineage,
                    limit=limit,
                    include_source=True,
                )
                events: list[str] = []
                for state_str, ts_text, source in rows:
                    label_text = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                    origin_tag = _event_origin_tag(source)
                    suffix = f" ({origin_tag})" if origin_tag else ""
                    events.append(f"{label_text} {ts_text}{suffix}")
                return events

            def _timer_response_payload(state_value: bool, ts_value, *, note: str | None = None) -> dict:
                payload = {
                    "state": bool(state_value),
                    "time": ts_value if isinstance(ts_value, str) else "",
                    "switch_id": sid or "",
                    "label": matched_label,
                    "ui_key": f"{sid}::{matched_label}" if sid else "",
                }
                events = _recent_events_payload()
                if events:
                    payload["events"] = events
                try:
                    getter = getattr(ctrl, "get_auto_off_status", None)
                    if callable(getter):
                        payload.update(dict(getter(matched_label) or {}))
                except Exception:
                    pass
                if note:
                    payload["note"] = note
                return payload

            if not remote:
                # Direct GPIO on this Pi
                try:
                    ok = bool(ctrl.set_state(matched_label, new_state, force=True))
                except Exception as e:
                    printDM(f"[toggle_switch] ctrl.set_state (direct) failed: {e}", location=MODULE)
            else:
                # Remote (Pico2 W via MQTT) — publish via injected mqtt_ingest
                if not mqtt_ingest:
                    return JSONResponse({"error": "mqtt_not_ready", "detail": "ingest_not_injected"}, status_code=503)

                if getattr(mqtt_ingest, "client", None) is None:
                    return JSONResponse({"error": "mqtt_not_ready", "detail": "ingest_client_none"}, status_code=503)

                # Primary: use ingest helper (finds correct base topic and emits {"set": "..."} JSON)
                if sid:
                    try:
                        channel_id = str((getattr(ctrl, "channel_id_for_label", {}) or {}).get(matched_label, "") or "").strip()
                    except Exception:
                        channel_id = ""
                    blocked = _remote_liveness_block_response(sid, channel_id=channel_id)
                    if blocked is not None:
                        return blocked
                    try:
                        ok = bool(mqtt_ingest.set_switch(sid, matched_label, new_state, event_origin="manual"))
                    except Exception as e:
                        printDM(f"[toggle_switch] ingest.set_switch error: {e}", location=MODULE)
                if ok:
                    try:
                        sync_toggle = getattr(ctrl, "sync_manual_toggle_result", None)
                        if callable(sync_toggle):
                            sync_toggle(matched_label, bool(new_state), previous_state=bool(current))
                        else:
                            ctrl.last_state[matched_label] = bool(new_state)
                    except Exception as e:
                        printDM(f"[toggle_switch] remote timer sync failed: {e}", location=MODULE)
                    response_state = bool(new_state)

            # ...after we've tried to set the state (ok = ctrl.set_state(...) or MQTT path)...
            if not ok:
                # Distinguish a guard/no-op from a real failure by checking the *effective* state
                try:
                    effective = bool(ctrl.get_state(matched_label))
                except Exception:
                    effective = bool((getattr(ctrl, "last_state", {}) or {}).get(matched_label, False))

                note = None
                # If the effective state equals the current state we started from,
                # it’s either a no-op (asked to set same state) or blocked by min-on/off guard.
                if effective == current:
                    # Best-effort hint: if we asked to flip but it didn't, likely guard-window.
                    note = "guard_window" if new_state != current else "noop"
                    # Return 200 so the UI treats this as handled (no error toast).
                    ts_map = getattr(ctrl, "last_set_time", {}) or {}
                    ts = ts_map.get(matched_label, time.time())
                    return JSONResponse(_timer_response_payload(effective, ts, note=note), status_code=200)

                # Otherwise, this really did fail (hardware/driver issue)
                reason = "mqtt_not_ready" if remote else "failed_to_toggle"
                return JSONResponse({"error": reason}, status_code=503 if reason == "mqtt_not_ready" else 500)

            # Persist SWITCH_n_LAST_STATE
            try:
                if sid and not remote:
                    mgr = SwitchSettingsManager("switch_settings")
                    # Prefer controller helper if available
                    idx = None
                    try:
                        idx = ctrl.get_channel_index(matched_label)
                    except Exception:
                        idx = None
                    if not idx:
                        # Fallback: walk ordered names
                        ordered = list(ctrl.get_switch_names() or [])
                        idx = next((i + 1 for i, nm in enumerate(ordered) if (nm or "").strip().lower() == label_q_lower), None)
                    if idx:
                        mgr.update_setting(sid, f"SWITCH_{idx}_LAST_STATE", bool(new_state))
            except Exception as e:
                printDM(f"[toggle_switch] persist failed for '{matched_label}': {e}", location=MODULE)

            # Prefer controller’s recorded set time if available
            ts_map = getattr(ctrl, "last_set_time", {}) or {}
            ts = ts_map.get(matched_label, time.time())
            # Persist UI-originated switch events only for local/direct controllers.
            # For remote/Nodus, history should be written only from confirmed MQTT event/state ingest.
            if not remote:
                try:
                    if sid:
                        try:
                            db_key = ctrl._switch_key(matched_label)
                        except Exception:
                            # Fallback for older controllers
                            db_key = f"{sid}::{matched_label}"

                        data_logger.log_switch_event(
                            switch_key=db_key,
                            is_on=bool(new_state),
                            source="ui",
                            sensor_id=f"Switch_{sid}",
                        )
                except Exception as e:
                    printDM(f"[toggle_switch] failed to log sw_event for {matched_label}: {e}", location=MODULE)

            # Let controller optionally record/log an event
            try:
                rec = getattr(ctrl, "record_event", None)
                if callable(rec) and not remote:
                    rec(matched_label, "on" if new_state else "off", ts)
            except Exception:
                pass

            # Invalidate short-lived switch status cache after a state change.
            _switch_status_cache_payload = None
            _switch_status_cache_until = 0.0

            return _timer_response_payload(response_state, ts)

        except Exception as e:
            printDM(f"[toggle_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/switch/timer")
    async def set_switch_timer(
        request: Request,
        switch_id: str = Query(...),
        switch_name: str = Query(...),
    ):
        global _switch_status_cache_payload, _switch_status_cache_until
        try:
            _require_protected_access(request, require_csrf=True)
            data = await request.json()
        except Exception:
            data = {}

        try:
            seconds = int(data.get("seconds", 0) or 0)
        except Exception:
            return JSONResponse({"error": "invalid_seconds"}, status_code=400)

        if seconds != 0 and not (30 <= seconds <= 9999):
            return JSONResponse({"error": "invalid_seconds", "detail": "Use 0 or 30-9999 seconds."}, status_code=400)

        sid = str(switch_id or "").strip()
        label_raw = str(switch_name or "").strip()
        if not sid or not label_raw:
            return JSONResponse({"error": "bad_request"}, status_code=400)

        sc_map = switch_controllers if isinstance(switch_controllers, dict) else {}
        match_ctrl = None
        match_label = ""
        for ctrl in sc_map.values():
            ctrl_sid = str(getattr(ctrl, "switch_id", "") or "").strip()
            if ctrl_sid != sid:
                continue
            try:
                labels = list(ctrl.get_switch_names() or [])
            except Exception:
                labels = list(getattr(ctrl, "switches", []) or [])
            found = next((lbl for lbl in labels if str(lbl or "").strip().lower() == label_raw.lower()), None)
            if found:
                match_ctrl = ctrl
                match_label = str(found).strip()
                break

        if not match_ctrl or not match_label:
            return JSONResponse({"error": "switch_not_found"}, status_code=404)

        setter = getattr(match_ctrl, "set_auto_off_seconds", None)
        getter = getattr(match_ctrl, "get_auto_off_status", None)
        if not callable(setter) or not callable(getter):
            return JSONResponse({"error": "timer_not_supported"}, status_code=400)

        applied = int(setter(match_label, seconds))
        try:
            current_state = bool(match_ctrl.get_state(match_label))
        except Exception:
            current_state = bool((getattr(match_ctrl, "last_state", {}) or {}).get(match_label, False))

        payload = {
            "ok": True,
            "switch_id": sid,
            "label": match_label,
            "ui_key": f"{sid}::{match_label}",
            "state": current_state,
            "timer_seconds": applied,
        }
        try:
            payload.update(dict(getter(match_label) or {}))
        except Exception:
            pass

        _switch_status_cache_payload = None
        _switch_status_cache_until = 0.0
        return JSONResponse(payload)

    @router.post("/switch/override")
    async def override_switch(
        request: Request,
        switch_name: str | None = Query(None),
        switch_key: str | None = Query(None),
        switch_id: str | None = Query(None),
    ):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        try:
            _require_protected_access(request, require_csrf=True)
            data = await request.json()
            desired_rule_enabled = bool(data.get("enabled", False))  # ← interpret as RULE state

            label_q   = (switch_name or "").strip()
            key_q     = (switch_key or "").strip()
            switch_id_q = (switch_id or "").strip().lower() if switch_id else None
            key_suffix_q = ""

            if DEBUG:
                printDM(f"[override_switch] name='{label_q}', key='{key_q}', switch_id='{switch_id_q}', rule.enabled={desired_rule_enabled}", location=MODULE)

            # Resolve from switch_key if present
            if key_q and "::" in key_q:
                sid_part, lbl_part = key_q.split("::", 1)
                switch_id_q = (sid_part or "").strip().lower() or switch_id_q
                key_suffix_q = (lbl_part or "").strip()
                label_q     = (lbl_part or "").strip() or label_q

            if not label_q:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            try:
                identity_rows_for_override = list(data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows_for_override = []
            if switch_id_q and key_suffix_q:
                suffix_l = key_suffix_q.lower()
                for row in identity_rows_for_override:
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    if r_sid != switch_id_q:
                        continue
                    r_label = str(row.get("label", "") or "").strip()
                    r_channel = _switch_channel_id_from_identity_row(row)
                    if suffix_l in {r_label.lower(), r_channel.lower()} and r_label:
                        label_q = r_label
                        break

            label_lower = label_q.lower()

            # Find the one controller that matches
            def _ctrl_labels(ctrl) -> list[str]:
                try:
                    getter = getattr(ctrl, "get_switch_names", None)
                    if callable(getter):
                        raw = getter() or []
                    else:
                        raw = getattr(ctrl, "switches", []) or []
                    return [str(s).strip() for s in raw if str(s).strip()]
                except Exception:
                    return []

            matches = []
            for ctrl in (switch_controllers or {}).values():
                ctrl_labels = _ctrl_labels(ctrl)
                if not ctrl_labels:
                    if DEBUG:
                        printDM("[override_switch] skipping switch with no labels", location=MODULE)
                    continue

                match_label = next((lbl for lbl in ctrl_labels if lbl.lower() == label_lower), None)
                if not match_label:
                    continue

                ctrl_sid = getattr(ctrl, "switch_id", None)
                if switch_id_q and (not ctrl_sid or ctrl_sid.lower() != switch_id_q):
                    continue

                matches.append((ctrl, match_label))

            if not matches:
                return JSONResponse({"error": "switch_not_found"}, status_code=404)
            if len(matches) > 1 and not switch_id_q:
                options = []
                for ctrl, _ in matches:
                    options.append({
                        "switch_id": getattr(ctrl, "switch_id", None),
                        "location": getattr(ctrl, "location", None),
                        "labels": _ctrl_labels(ctrl),
                    })
                return JSONResponse({"error": "ambiguous_switch", "options": options}, status_code=409)

            ctrl, matched_label = matches[0]

            # Resolve channel index for SWITCH_{n}_OVERRIDE_SCRIPT
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                sid = getattr(ctrl, "switch_id", None)
                channel_index = None
                if sid:
                    ordered = switch_mgr.get_switch_channel_names(sid)  # ['Fan','Light',...]
                    channel_index = next((i + 1 for i, nm in enumerate(ordered) if (nm or '').strip().lower() == label_lower), None)
                # Fallback: parse switch.toml directly if needed
                if sid and not channel_index:
                    try:
                        doc = switch_mgr.load(sid) or {}
                        sw_map = doc.get("Switch") or {}
                        for k, v in sw_map.items():
                            if not str(k).startswith("SWITCH_"):
                                continue
                            parts = str(k).split("_")
                            if len(parts) != 2 or not parts[1].isdigit():
                                continue
                            if str(v).strip().lower() == label_lower:
                                channel_index = int(parts[1])
                                break
                    except Exception:
                        channel_index = None
                # Fallback: map label -> channel_id from switch_ids, then match SWITCH_n_ID
                if sid and not channel_index:
                    try:
                        channel_id = None
                        for row in identity_rows_for_override:
                            if str(row.get("switch_id", "")).strip().lower() != sid.lower():
                                continue
                            if str(row.get("label", "")).strip().lower() != label_lower:
                                continue
                            channel_id = _switch_channel_id_from_identity_row(row)
                            if channel_id:
                                break
                        if channel_id:
                            doc = switch_mgr.load(sid) or {}
                            sw_map = doc.get("Switch") or {}
                            for k, v in sw_map.items():
                                if not str(k).startswith("SWITCH_") or not str(k).endswith("_CHANNEL_ID"):
                                    continue
                                parts = str(k).split("_")
                                if len(parts) != 4 or not parts[1].isdigit():
                                    continue
                                if str(v).strip() == channel_id:
                                    channel_index = int(parts[1])
                                    break
                    except Exception:
                        channel_index = None
            except Exception as e:
                printDM(f"[override_switch] failed resolving channel index: {e}", location=MODULE)
                channel_index = None

            # --- Persist BOTH states atomically: Advanced rule + switch override flag ---
            try:
                am = AutomationManager()
                sid = getattr(ctrl, "switch_id", None) or ""
                # Canonical Advanced switch_key
                switch_key_full = (key_q or "").strip()
                if not switch_key_full:
                    # fall back to constructing it from sid + label
                    switch_key_full = f"{sid}::{matched_label}" if sid else f"::{matched_label}"

                # 1) Update Advanced rule enabled flag
                updated = am.set_advanced_enabled_for_switch_key(sid, switch_key_full, desired_rule_enabled)
                if not updated:
                    # No matching Advanced rule for this switch_key -> report to client
                    return JSONResponse({"error": "automation_rule_not_found"}, status_code=404)

                agg_state = am.get_advanced_state_for_switch_key(sid, switch_key_full)
                effective_rule_enabled = bool(agg_state.get("enabled_any", False))

                # 2) Update switch.toml override to the inverse of rule.enabled
                override_value = (not effective_rule_enabled)
                if sid and channel_index:
                    switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_value)

                # 3) Update in-memory map if present
                try:
                    override_map = getattr(ctrl, "override_script", None)
                    if isinstance(override_map, dict):
                        override_map[matched_label] = override_value
                except Exception:
                    pass

                # Invalidate short-lived switch status cache after a state-affecting update.
                global _switch_status_cache_payload, _switch_status_cache_until
                _switch_status_cache_payload = None
                _switch_status_cache_until = 0.0

            except Exception as e:
                printDM(f"[override_switch] persist failed for '{matched_label}': {e}", location=MODULE)
                return JSONResponse({"error": "persist_failed"}, status_code=500)

            # notify any listeners that an automation rule changed
            try:
                app = request.app
                if hasattr(app.state, "switch_broadcast"):
                    await app.state.switch_broadcast({
                        "type": "automation_toggle",
                        "switch_id": sid,
                        "label": matched_label,
                        "enabled": bool(effective_rule_enabled),
                    })
            except Exception as e:
                printDM(f"[override_switch] broadcast failed: {e}", location=MODULE)

            # Return both states so UI can reflect the RULE state
            return {
                "status": "ok",
                "enabled": effective_rule_enabled,
                "override": (not effective_rule_enabled),
            }

        except Exception as e:
            printDM(f"[override_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

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
            archive_path = await asyncio.to_thread(data_logger.archive_and_create_new_database)
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

    @router.get("/network-status", response_class=JSONResponse)
    async def network_status_api():
        try:
            connected = net_mgr.is_connected()
            current_ssid = net_mgr.get_current_ssid()
            result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False)
            ip_output = result.stdout.strip()
            ip_addr = ip_output.split()[0] if ip_output else "N/A"

            return JSONResponse({
                "connected": connected,
                "ssid": current_ssid,
                "ip": ip_addr,
                "mode": "AP" if not connected and ip_addr.startswith("192.168.4.") else "Client"
            })
        except Exception as e:
            printDM(f"[network_status] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.get("/debug/mqtt-retained-commands", response_class=JSONResponse)
    async def debug_mqtt_retained_commands(
        timeout: float = Query(1.0, ge=0.2, le=5.0),
        limit: int = Query(64, ge=1, le=200),
    ):
        ing = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        scanner = getattr(ing, "scan_retained_command_topics", None)
        if not ing or not callable(scanner):
            return JSONResponse({"ok": False, "error": "mqtt_ingest_unavailable"}, status_code=503)
        try:
            result = await asyncio.to_thread(scanner, timeout=timeout, limit=limit)
            status_code = 200 if bool(result.get("ok", False)) else 503
            return JSONResponse(result, status_code=status_code)
        except Exception as e:
            printDM(f"[debug_mqtt_retained_commands] {e}", location=MODULE)
            return JSONResponse({"ok": False, "error": "internal_error"}, status_code=500)

    @router.get("/debug", response_class=HTMLResponse)
    async def debug_page():
        return HTMLResponse("<h2>Debug route loaded</h2>")

    @router.get("/keep-alive", response_class=JSONResponse)
    async def keep_alive():
        return {"ready": True}

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
    app.include_router(create_stats_router(settings, gc_mgr))
    app.include_router(router)
    return router
