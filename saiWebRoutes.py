"""FastAPI routes for Sensorius UI, settings, and device APIs.

Responsibilities:
- render core HTML pages and modal templates for the web UI
- expose REST endpoints for sensors, switches, calibration, and stats
- manage settings updates (system/sensor/switch) and onboarding flows
- integrate with MQTT ingest and data logger for live state and history
"""
from __future__ import annotations #must be first in line

from fastapi import Request, Form, Query, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.routing import APIRouter
from starlette.responses import StreamingResponse
try:
    # preferred with FastAPI
    from fastapi import WebSocket, WebSocketDisconnect
except ImportError:
    # fallback to Starlette directly
    from starlette.websockets import WebSocket, WebSocketDisconnect
import sqlite3
from pathlib import Path
from typing import Dict, Any, Set
from uuid import uuid4
import json
import socket
import asyncio
import subprocess
import time
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
from datetime import date, datetime, timedelta
import math
from zoneinfo import ZoneInfo, available_timezones
try:
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun, elevation as _astral_elevation, azimuth as _astral_azimuth
    from astral import moon as _astral_moon
except Exception:
    LocationInfo = None
    _astral_sun = None
    _astral_elevation = None
    _astral_azimuth = None
    _astral_moon = None
try:
    import pwd  # POSIX only
except Exception:
    pwd = None
from saiUtils import (
    printDM,
    debug_enabled,
    get_timestamp,
    normalize_sensor_id,
    normalize_hostname_base,
    mdns_hostname,
)
from saiSettings import saiSettings
from saiOnboardingStore import OnboardingSessionStore, OnboardingStates
from saiOnboardingToken import OnboardingTokenManager
from saiDataLogger import saiDataLogger
try:
    from saiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None
from saiStats import saiStats
from saiHtml import render_dashboard, get_gauge_config
from saiFastStats import FastStats
from saiSensorSettingsManager import SensorSettingsManager
from saiSwitchSettingsManager import SwitchSettingsManager
from saiBiodynamics import get_biodynamic_payload
from saiAddDevice import HUB_SETTINGS_PATH, _SENSOR_BASE_DIR, _SWITCH_BASE_DIR, _SYS_BASE_DIR
try:
    from __init__ import __version__ as SAI_APP_VERSION
except Exception:
    SAI_APP_VERSION = "v0.0.0"

MODULE = "saiWebRoutes"
DEBUG = debug_enabled(MODULE)
data_logger = saiDataLogger()
statter = saiStats()
_ALL_IANA_TIMEZONES: tuple[str, ...] = tuple(sorted(available_timezones()))

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
_dynamic_switch_monitor_tasks: dict[str, asyncio.Task] = {}
_SWITCH_STATUS_CACHE_TTL_SEC: float = 1.5
_cdp_debug_last_log: float = 0.0
_CDP_DEBUG_MIN_INTERVAL_SEC: float = 5.0

async def register_routes(app, settings, net_mgr, gc_mgr, mqtt_ingest):
    router = APIRouter()
    main_loop = asyncio.get_running_loop()
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

    def _moon_phase_name(phase_val: float) -> str:
        p = phase_val % 28.0

        def _circular_dist(a: float, b: float, cycle: float = 28.0) -> float:
            d = abs(a - b) % cycle
            return min(d, cycle - d)

        if _circular_dist(p, 0.0) <= 1.0:
            return "New Moon"
        if _circular_dist(p, 7.0) <= 1.0:
            return "1st Quarter"
        if _circular_dist(p, 14.0) <= 1.0:
            return "Full Moon"
        if _circular_dist(p, 21.0) <= 1.0:
            return "3rd Quarter"
        if 1.0 < p < 6.0:
            return "Waxing Crescent"
        if 8.0 < p < 13.0:
            return "Waxing Gibbous"
        if 15.0 < p < 20.0:
            return "Waning Gibbous"
        return "Waning Crescent"

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
            "moon_phase_value": None,
            "moon_phase_label": "",
            "moon_lit_pct": None,
            "moon_rise": "",
            "moon_set": "",
            "moon_next_full": "",
            "moon_visible_angle": None,
        }
        if (
            LocationInfo is None
            or _astral_sun is None
            or _astral_elevation is None
            or _astral_azimuth is None
            or _astral_moon is None
        ):
            return out

        try:
            s = saiSettings(apply_live=False)
            resolved = s.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
            resolved_lat = resolved.get("lat")
            resolved_lon = resolved.get("lon")
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

            sun_map = _astral_sun(obs, date=now_local.date(), tzinfo=tzinfo)
            sunrise = sun_map.get("sunrise")
            sunset = sun_map.get("sunset")
            noon = sun_map.get("noon")
            if not isinstance(sunrise, datetime) or not isinstance(sunset, datetime):
                return out

            pts: list[dict[str, object]] = []
            cur = sunrise
            while cur <= sunset:
                try:
                    elev = float(_astral_elevation(obs, cur))
                except Exception:
                    elev = float("nan")
                if math.isfinite(elev):
                    pts.append({"t": cur.strftime("%H:%M"), "e": round(elev, 2)})
                cur = cur + timedelta(minutes=5)
            if pts and pts[-1]["t"] != sunset.strftime("%H:%M"):
                try:
                    elev_sunset = float(_astral_elevation(obs, sunset))
                except Exception:
                    elev_sunset = 0.0
                pts.append({"t": sunset.strftime("%H:%M"), "e": round(elev_sunset, 2)})

            moon_val = float(_astral_moon.phase(now_local.date()))
            moon_lit_pct = int(
                round((0.5 * (1 - math.cos((2 * math.pi * (moon_val % 28.0)) / 28.0))) * 100)
            )
            moon_visible_angle = None
            try:
                moon_az_fn = getattr(_astral_moon, "azimuth", None)
                moon_el_fn = getattr(_astral_moon, "elevation", None)
                moon_az = float(moon_az_fn(obs, now_local)) if callable(moon_az_fn) else float("nan")
                moon_el = float(moon_el_fn(obs, now_local)) if callable(moon_el_fn) else float("nan")
                sun_az = float(_astral_azimuth(obs, now_local))
                sun_el = float(_astral_elevation(obs, now_local))

                if all(math.isfinite(v) for v in (moon_az, moon_el, sun_az, sun_el)):
                    def _h_to_unit(az_deg: float, el_deg: float) -> tuple[float, float, float]:
                        az = math.radians(az_deg)
                        el = math.radians(el_deg)
                        cel = math.cos(el)
                        return (
                            cel * math.sin(az),  # east
                            cel * math.cos(az),  # north
                            math.sin(el),        # up
                        )

                    def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
                        return (a[0] * b[0]) + (a[1] * b[1]) + (a[2] * b[2])

                    def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
                        return (
                            (a[1] * b[2]) - (a[2] * b[1]),
                            (a[2] * b[0]) - (a[0] * b[2]),
                            (a[0] * b[1]) - (a[1] * b[0]),
                        )

                    def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
                        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

                    def _mul(a: tuple[float, float, float], k: float) -> tuple[float, float, float]:
                        return (a[0] * k, a[1] * k, a[2] * k)

                    def _norm(v: tuple[float, float, float]) -> float:
                        return math.sqrt(_dot(v, v))

                    def _unit(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
                        n = _norm(v)
                        if n <= 1e-9:
                            return None
                        return (v[0] / n, v[1] / n, v[2] / n)

                    moon_vec = _h_to_unit(moon_az, moon_el)
                    sun_vec = _h_to_unit(sun_az, sun_el)
                    zenith = (0.0, 0.0, 1.0)
                    north = (0.0, 1.0, 0.0)

                    up_axis = _unit(_sub(zenith, _mul(moon_vec, _dot(zenith, moon_vec))))
                    if up_axis is None:
                        up_axis = _unit(_sub(north, _mul(moon_vec, _dot(north, moon_vec))))

                    if up_axis is not None:
                        # Build screen-right from the local up axis and moon-view direction.
                        # The previous cross-product order mirrored the phase left/right.
                        right_axis = _unit(_cross(up_axis, moon_vec))
                        limb_vec = _unit(_sub(sun_vec, _mul(moon_vec, _dot(sun_vec, moon_vec))))
                        if right_axis is not None and limb_vec is not None:
                            ang = math.degrees(math.atan2(_dot(limb_vec, up_axis), _dot(limb_vec, right_axis)))
                            moon_visible_angle = round(ang % 360.0, 2)
            except Exception:
                moon_visible_angle = None

            moon_rise = ""
            moon_set = ""
            try:
                mr_fn = getattr(_astral_moon, "moonrise", None)
                ms_fn = getattr(_astral_moon, "moonset", None)

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

                moon_rise = _pick_nearest_event(mr_fn)
                moon_set = _pick_nearest_event(ms_fn)
            except Exception:
                moon_rise = ""
                moon_set = ""

            moon_next_full = ""
            try:
                nf_fn = getattr(_astral_moon, "next_full_moon", None)
                nf = nf_fn(now_local.date()) if callable(nf_fn) else None
                if isinstance(nf, datetime):
                    moon_next_full = nf.date().isoformat()
                elif hasattr(nf, "isoformat"):
                    moon_next_full = str(nf.isoformat())
                if moon_next_full:
                    moon_next_full = moon_next_full[:10]
            except Exception:
                moon_next_full = ""

            if not moon_next_full:
                best_date = None
                for i in range(1, 32):
                    d = now_local.date() + timedelta(days=i)
                    try:
                        pv = float(_astral_moon.phase(d))
                    except Exception:
                        continue
                    dist = abs((pv % 28.0) - 14.0)
                    dist = min(dist, 28.0 - dist)
                    if dist <= 0.6:
                        best_date = d
                        break
                if best_date is not None:
                    moon_next_full = best_date.isoformat()

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
                    "moon_phase_value": round(moon_val, 2),
                    "moon_phase_label": _moon_phase_name(moon_val),
                    "moon_lit_pct": moon_lit_pct,
                    "moon_rise": moon_rise,
                    "moon_set": moon_set,
                    "moon_next_full": moon_next_full,
                    "moon_visible_angle": moon_visible_angle,
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
                    sk = str(row.get("switch_key", "")).strip()
                    if "::" in sk:
                        return sk.split("::", 1)[0].strip()
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

    _ENV_PATH = Path(__file__).resolve().parent / ".env"
    _ENV_DEF_PATH = Path(__file__).resolve().parent / ".env.def"
    _AUTOSTART_LABEL = "com.sensorius.app"
    _AUTOSTART_SERVICE = "sensorius.service"
    _AUTOSTART_TASK = "SensoriusAutoStart"
    _ADV_DEBUG_MODULE_CHOICES = [
        "Sensorius",
        "saiSensor",
        "saiMQTTIngest",
        "saiHtml",
        "saiSwitch",
        "saiWebRoutes",
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
        try:
            os.chmod(_ENV_PATH, 0o644)
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
        project_dir = Path(__file__).resolve().parent
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

    def _scan_for_ssid(target_ssid: str) -> tuple[bool, str]:
        ssid = str(target_ssid or "").strip()
        if not ssid:
            return False, "SSID missing"
        sys_name = platform.system().lower()
        try:
            if sys_name == "linux":
                p = subprocess.run(
                    ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"],
                    capture_output=True, text=True, timeout=8
                )
                if p.returncode != 0:
                    return False, (p.stderr or p.stdout or "nmcli failed").strip()
                lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
                return (ssid in lines), "ok"

            if sys_name == "darwin":
                airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
                if not Path(airport).exists():
                    return False, "airport tool not found"
                # Prefer plist output (-x) for robust parsing across spacing/alignment changes.
                p_xml = subprocess.run([airport, "-s", "-x"], capture_output=True, timeout=8)
                if p_xml.returncode == 0 and p_xml.stdout:
                    try:
                        rows = plistlib.loads(p_xml.stdout)
                        wanted = ssid.strip()
                        for row in (rows or []):
                            if not isinstance(row, dict):
                                continue
                            candidate = str(row.get("SSID_STR") or row.get("SSID") or "").strip()
                            if candidate == wanted:
                                return True, "ok"
                        return False, "ok"
                    except Exception:
                        pass

                # Fallback to text output if plist parsing isn't available on this host.
                p = subprocess.run([airport, "-s"], capture_output=True, text=True, timeout=8)
                if p.returncode != 0:
                    err = p.stderr or p.stdout
                    if not err and p_xml.returncode != 0:
                        err = (p_xml.stderr or b"").decode(errors="ignore")
                    return False, (err or "airport scan failed").strip()
                wanted = ssid.strip()
                for ln in (p.stdout or "").splitlines():
                    line = ln.rstrip()
                    if not line or line.lstrip().startswith("SSID "):
                        continue
                    bssid = re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", line)
                    candidate = line[:bssid.start()].strip() if bssid else line.strip()
                    if candidate == wanted:
                        return True, "ok"
                return False, "ok"

            if sys_name == "windows":
                p = subprocess.run(
                    ["netsh", "wlan", "show", "networks", "mode=bssid"],
                    capture_output=True, text=True, timeout=10
                )
                if p.returncode != 0:
                    return False, (p.stderr or p.stdout or "netsh scan failed").strip()
                for ln in (p.stdout or "").splitlines():
                    m = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*)$", ln, flags=re.IGNORECASE)
                    if m and m.group(1).strip() == ssid:
                        return True, "ok"
                return False, "ok"
        except Exception as ex:
            return False, str(ex)
        return False, f"unsupported platform: {platform.system()}"

    @router.get("/", response_class=HTMLResponse)
    async def current_data_page(request: Request, sensor_id: str = Query(None), json_only: bool = Query(False)):
        global _cdp_debug_last_log
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
                    import saiWebRoutes as routes
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
                    import saiWebRoutes as routes
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
                1) live MQTT map
                2) disk settings
                3) in-memory sensor_map
                4) 'Unknown'
                """
                topic = f"sensor/{sid}/data"
                loc = mqtt_ingest.device_location.get(topic) or mqtt_ingest.device_location.get(sid)
                if isinstance(loc, str) and loc.strip():
                    return loc.strip()

                try:
                    from saiSensorSettingsManager import SensorSettingsManager
                    mgr = SensorSettingsManager("sensor_settings")
                    loc = mgr.get_setting(sid, "Sensor.LOCATION", None)
                    if isinstance(loc, str) and loc.strip():
                        return loc.strip()
                except Exception:
                    pass

                sm = _get_sensor_map()
                if isinstance(sm, dict):
                    sensor_obj = sm.get(sid) or sm.get((sid or "").lower())
                    if sensor_obj and getattr(sensor_obj, "location", None):
                        loc = sensor_obj.location
                        if isinstance(loc, str) and loc.strip():
                            return loc.strip()
                elif isinstance(sm, Iterable):
                    for s in sm:
                        sid_attr = getattr(s, "sensor_id", None)
                        if isinstance(sid_attr, str) and sid_attr.lower() == (sid or "").lower():
                            loc = getattr(s, "location", None)
                            if isinstance(loc, str) and loc.strip():
                                return loc.strip()

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
                        if isinstance(loc, str) and loc.strip():
                            return loc.strip()
                except Exception:
                    pass

                try:
                    from saiSwitchSettingsManager import SwitchSettingsManager
                    mgr = SwitchSettingsManager("switch_settings")
                    loc = mgr.get_setting(sw, "Switch.SWITCH_LOCATION", None)
                    if isinstance(loc, str) and loc.strip():
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
                            if isinstance(loc, str) and loc.strip():
                                return loc.strip()
                except Exception:
                    pass

                return "Unknown"

            sensors_from_logger = data_logger.get_available_sensors()
            mqtt_discovered = mqtt_ingest.get_known_devices()

            local_ids = _get_local_sensor_ids()
            # Include sensors that have logged data, even if discovery missed /itaot.
            merged_local = list(local_ids or [])
            for sid in (sensors_from_logger or []):
                if sid and sid not in merged_local:
                    merged_local.append(sid)
            available = _normalize_available(merged_local, list(mqtt_discovered))

            # Filter to sensors with recent data only.
            available = [sid for sid in available if _is_recent_sensor(sid)]

            # Build a switch inventory for debug visibility (local + discovered + DB identities).
            available_switches = []
            renderable_switch_controllers = []
            try:
                switch_ids_local = []
                try:
                    from saiSwitchSettingsManager import SwitchSettingsManager
                    switch_mgr = SwitchSettingsManager("switch_settings")
                    switch_ids_local = switch_mgr.list_switches() or []
                except Exception:
                    switch_ids_local = []
                switch_ids_live = _get_local_switch_ids()

                switch_ids_discovered = []
                switch_ids_discovered_channels = []
                try:
                    switch_ids_discovered = mqtt_ingest.get_known_switch_devices() or []
                    nodus_topic_map = getattr(mqtt_ingest, "nodus_switch_topic_map", {}) or {}
                    for meta in nodus_topic_map.values():
                        ch_id = str((meta or {}).get("channel_id", "") or "").strip()
                        if ch_id:
                            switch_ids_discovered_channels.append(_canonical_channel_id(ch_id))
                except Exception:
                    switch_ids_discovered = []
                    switch_ids_discovered_channels = []

                switch_ids_db = []
                try:
                    for row in (data_logger.get_switch_identities() or []):
                        ch_id = str(row.get("channel_id", "")).strip()
                        if ch_id:
                            switch_ids_db.append(_canonical_channel_id(ch_id))
                except Exception:
                    switch_ids_db = []

                nodus_channels = list(switch_ids_discovered_channels) + list(switch_ids_db)
                available_switches = _normalize_switch_ids(
                    nodus_channels,
                    allowed_extra=set(nodus_channels),
                )
                renderable_switch_controllers = _normalize_switch_ids(
                    list(switch_ids_local) + list(switch_ids_live) + list(switch_ids_discovered),
                    allowed_extra=set(list(switch_ids_local) + list(switch_ids_live)),
                )
            except Exception:
                available_switches = []
                renderable_switch_controllers = []

            # ---- Build a fresh location map for all 'available' sensors ----
            sensor_locations_map = { sid: resolve_location_for_sid(sid) for sid in available }

            # ---- Optional location filter via sensor_id='loc:<Location>' ----
            selected_location = None
            if isinstance(sensor_id, str) and sensor_id.startswith("loc:"):
                selected_location = sensor_id[4:].strip().lower()
                available = [
                    sid for sid, loc in sensor_locations_map.items()
                    if (loc or "").strip().lower() == selected_location
                ]

            if DEBUG:
                now_mono = time.monotonic()
                if (now_mono - _cdp_debug_last_log) >= _CDP_DEBUG_MIN_INTERVAL_SEC:
                    _cdp_debug_last_log = now_mono
                    printDM(f"local_ids: {local_ids}", location=f"{MODULE}:cdp")
                    printDM(f"available sensors: {available}", location=f"{MODULE}:cdp")
                    printDM(f"available switches: {available_switches}", location=f"{MODULE}:cdp")

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
        async def _values_for(sid: str):
            v = await asyncio.to_thread(data_logger.get_latest_values, sid)
            return sid, (v or {})

        async def _stats_for(sid: str):
            s = await asyncio.to_thread(statter.get_24hr_stats, sid)
            return sid, (s or {})

        if not sensor_id or sensor_id == "All" or (isinstance(sensor_id, str) and sensor_id.startswith("loc:")):
            vals = await asyncio.gather(*[_values_for(sid) for sid in available])
            sts  = await asyncio.gather(*[_stats_for(sid)  for sid in available])
            all_values = {sid: v for sid, v in vals}
            all_stats  = {sid: s for sid, s in sts}
        else:
            sid = sensor_id
            v_sid, v = await _values_for(sid)
            s_sid, s = await _stats_for(sid)
            all_values = {v_sid: (v or {})}
            all_stats  = {s_sid: (s or {})}

        from saiSettings import saiSettings
        fresh_settings = saiSettings(apply_live=False)
        gaugeSize = fresh_settings.get_setting("Display", "gauge_size") or "Small"
        gauge_config = get_gauge_config()
        displayStyle = fresh_settings.get_setting("Display", "display_style") or "Gauge"

        from saiSensorSettingsManager import SensorSettingsManager
        from saiCalibration import CalibrationManager
        sensor_mgr = SensorSettingsManager()
        expected_gauge_map = {}
        for sid in all_values:
            metrics = mqtt_ingest.expected_gauge_map.get(sid)
            if not metrics:
                try:
                    metrics = sensor_mgr.get_display_metrics(sid)
                except Exception:
                    metrics = []
            if not metrics:
                # If display metrics are blank, prefer per-sensor stored metrics
                # rather than rendering every gauge_config metric.
                try:
                    stored_metrics = data_logger.get_available_metrics(sid) or []
                except Exception:
                    stored_metrics = []
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
            # Keep dashboard payloads bounded and stable when discovery metadata is absent.
            deduped: list[str] = []
            seen = set()
            for metric in (metrics or []):
                m = str(metric).strip()
                if not m or m in seen:
                    continue
                seen.add(m)
                deduped.append(m)
                if len(deduped) >= 6:
                    break
            expected_gauge_map[sid] = deduped

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
            from saiDataLogger import build_switch_key as _build_switch_key
        except Exception:
            _build_switch_key = None

        def _switch_key(switch_id: str, label: str) -> str:
            sid = (switch_id or "").strip()
            lab = (label or "").strip()
            ch_id = _resolve_channel_id_from_label(sid, lab)
            if _build_switch_key is not None:
                try:
                    return _build_switch_key(ch_id, lab)
                except Exception:
                    pass
            # fallback: current behavior
            return f"{ch_id}::{lab}"

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
            return sqlite3.connect(db_path)

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
        from saiMQTTIngest import get_current_ingest as _get_ing
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
            # 1) Direct/local SensorController.status
            try:
                sc = _active_sensor_for(sid)
                base = getattr(sc, "sensor", sc)
                st = getattr(base, "meas_status", None)
                if isinstance(st, str) and st.strip().lower() in {"online", "degraded", "offline", "unknown", "migration_required"}:
                    return st.strip().lower()
            except Exception:
                pass

            # 2) Ask the ingest instance we were given
            try:
                if ing is not None and hasattr(ing, "get_measure_status") and callable(ing.get_measure_status):
                    st = ing.get_measure_status(sid)  # should accept either sid or host
                    if isinstance(st, str) and st.strip().lower() in {"online", "degraded", "offline", "unknown", "migration_required"}:
                        return st.strip().lower()
            except Exception:
                pass

            # 3) Fallback: normalize host and consult ingest.device_status
            try:
                dev_map = getattr(ing, "device_status", {}) or {}
                host = _host_base_from_sid(sid)
                base = normalize_hostname_base(host)
                for key in (host, base, mdns_hostname(base)):
                    st = dev_map.get(key or "")
                    if isinstance(st, str) and st.strip().lower() in {"online", "degraded", "offline", "unknown", "migration_required"}:
                        return st.strip().lower()
            except Exception:
                pass

            return "unknown"
         
        if json_only:
            timestamps = {
                sid: data_logger.get_latest_timestamp(sid) or ""
                for sid in all_values
            }
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
            
            return JSONResponse({
                "available": available,
                "values": all_values,
                "stats": all_stats,
                "timestamps": timestamps, 
                "sensor_id": sensor_id,
                "timestamp": get_timestamp(),
                "locations": sensor_locations,
                "expected_gauge_map": expected_gauge_map,
                "available_switches": available_switches,
                "renderable_switches": renderable_switches,
                "renderable_switches_view": renderable_switches_view,
                "statuses": statuses,
                "astro": _build_astro_payload(),
                "biodynamic": get_biodynamic_payload(),
            })


        return StreamingResponse(
            render_dashboard(
                sensor_id, 
                sensor, 
                available,
                all_values, 
                all_stats, 
                mqtt_ingest,
                switch_controllers = switch_controllers,
                sensor_locations = sensor_locations,
                gauge_config=gauge_config, 
                gauge_size = gaugeSize,
                expected_gauge_map = expected_gauge_map,
                display_style = displayStyle,
            ),
            media_type="text/html"
        )

    # graph data full screen with upto three y-axis or the small single y-axis graph overly on top of the gauge
    @router.get("/graph-data", response_class=JSONResponse)
    async def graph_data_api(
        request: Request,
        # legacy / left axis primary (kept)
        sensor_id: str = Query(""),
        metric1: str = Query(""),
        metric2: str = Query(""),
        metric3: str = Query(""),
        # new triplet (one sensor per axis slot)
        sensor_id1: str = Query(""),
        sensor_id2: str = Query(""),
        sensor_id3: str = Query(""),
        range: str = Query(...),
        start: str | None = Query(None),
        end: str | None = Query(None),
        # new, preferred way:
        switch_id: str = Query("", description="Switch ID to draw on/off transitions from"),
        channels: list[str] = Query([], alias="channels"),
        # legacy fallback (kept temporarily for compatibility):
        switches: list[str] = Query([], alias="switches"),
    ):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from fastapi.responses import JSONResponse
        from fastapi import HTTPException
        from saiUtils import printDM

        MODULE = "graph-data"
        db_path = getattr(data_logger, "db_path", "sensorius_data.db")

        # --- Local zone from settings (seconds offset) ---
        def _local_tz():
            try:
                from saiSettings import saiSettings
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

        # ----- time range (ALL in local offset, matching DB storage) -----
        try:
            since_iso, until_iso, span_seconds, since_dt, until_dt = _compute_window(range, start, end)
        except Exception as e:
            return JSONResponse({"error": f"Invalid time range: {e}"}, status_code=400)

        # ----- normalize inputs (back-compat + new triplets) -----
        sid1 = (sensor_id1 or "").strip() or (sensor_id or "").strip()
        sid2 = (sensor_id2 or "").strip()
        sid3 = (sensor_id3 or "").strip()

        m1 = (metric1 or "").strip()
        m2 = (metric2 or "").strip()
        m3 = (metric3 or "").strip()

        pairs: list[tuple[str, str]] = []
        if sid1 and m1: pairs.append((sid1, m1))
        if sid2 and m2: pairs.append((sid2, m2))
        if sid3 and m3: pairs.append((sid3, m3))
        if not pairs and sensor_id and metric1:
            pairs.append((sensor_id, metric1))
            if metric2: pairs.append((sensor_id, metric2))
            if metric3: pairs.append((sensor_id, metric3))

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
                return ts, vs
            except Exception as e:
                printDM(f"[{MODULE}] Error fetching {sid}.{metric_name}: {e}", location=MODULE)
                return [], []

        # ----- data series -----
        series: dict[str, dict] = {}
        simple_avg: dict[str, dict] = {}
        display_names: dict[str, str] = {}
        with sqlite3.connect(db_path) as conn:
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

        # ----- switch vertical lines (use the SAME LOCAL window in SQL) -----
        want_switch_lines = bool((switch_id and channels) or switches)
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
                    with sqlite3.connect(db_path) as conn2:
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
                    with sqlite3.connect(db_path) as conn2:
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
                if switch_id and channels:
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

    @router.get("/edit-system", response_class=HTMLResponse)
    async def edit_pi_settings_page(request: Request):
        from saiSettings import saiSettings
        from saiHtml import APP_NAME_LONG, APP_VERSION

        settings = saiSettings(apply_live=False)
        templates = request.app.state.templates

        # Prepare values for the template (let Jinja handle escaping)
        hostname   = settings.get_setting("Network", "HOSTNAME", "") or ""
        httpport   = settings.get_setting("Network", "HTTPPORT", 8000) or 8000
        broker     = settings.get_setting("SensorNetwork", "BROKER", "") or ""
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
        astral_sunrise = "--"
        astral_sunset = "--"
        astral_daylight = "--"
        astral_noon = "--"
        gauge_size = settings.get_setting("Display", "gauge_size", "") or "Small"
        display_style = settings.get_setting("Display", "display_style", "") or "Gauge"
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

        clients = settings.get_all_clients() or []
        client_list = "\n".join(clients)

        def _format_hhmm(dt_obj: datetime | None) -> str:
            if dt_obj is None:
                return "--"
            return dt_obj.strftime("%H:%M")

        resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=2.5)
        resolved_lat = resolved.get("lat")
        resolved_lon = resolved.get("lon")
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
                sun_map = _astral_sun(loc.observer, date=now_local.date(), tzinfo=tzinfo)
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
            hostname=hostname,
            httpport=httpport,
            broker=broker,
            tz=tz,
            tz_offset=tz_offset,
            tz_name=tz_name,
            tz_options=tz_options,
            gauge_size=gauge_size,
            display_style=display_style,
            astral_lat=astral_lat,
            astral_lon=astral_lon,
            astral_sunrise=astral_sunrise,
            astral_sunset=astral_sunset,
            astral_daylight=astral_daylight,
            astral_noon=astral_noon,
            client_list=client_list,
            ha_enabled=ha_enabled,
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
            onboarding_v2_mqtt_enabled=_onboarding_v2_enabled(),
        )

        fragment_parts: list[str] = []
        fragment_parts.append("<link rel='stylesheet' href='/ui_static/css/app.css'>")
        fragment_parts.append(system_modal_html)
        fragment_html = "\n".join(fragment_parts)

        embed = str(request.query_params.get("embed", "")).strip().lower() in {"1", "true", "yes"}
        if embed:
            return HTMLResponse(content=fragment_html)

        html_parts: list[str] = []
        html_parts.append("<!DOCTYPE html>")
        html_parts.append("<html><head><title>System Settings</title>")
        html_parts.append("</head><body>")
        html_parts.append(fragment_html)
        html_parts.append("</body></html>")
        return HTMLResponse(content="\n".join(html_parts))

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
                    sensor_toml_path = Path(r"sensor_settings") / sensor_id / "sensor.toml"
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
                        switch_toml_path = Path(r"switch_settings") / switch_id / "switch.toml"
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

    def _build_v2_bootstrap_payload(
        *,
        onboard_token: str,
        ssid: str,
        password: str,
        hostname: str,
    ) -> Dict[str, Any]:
        broker_host = str(settings.get_setting("SensorNetwork", "BROKER", "") or "").strip()
        broker_port_raw = settings.get_setting("MQTT", "PORT", 1883)
        try:
            broker_port = int(broker_port_raw)
        except Exception:
            broker_port = 1883
        mqtt_user = str(settings.get_setting("MQTT", "USERNAME", "") or "").strip()
        mqtt_password = str(settings.get_setting("MQTT", "PASSWORD", "") or "")
        use_tls = bool(settings.get_setting("MQTT", "USE_TLS", False))
        instance_id = socket.gethostname().strip() or "sensorius"
        payload: Dict[str, Any] = {
            "onboard_token": onboard_token,
            "ssid": ssid,
            "password": password,
            "hostname": hostname,
            "mqtt": {
                "broker_host": broker_host,
                "broker_port": broker_port,
                "username": mqtt_user,
                "password": mqtt_password,
                "use_tls": use_tls,
                "active_profile": "sensorius",
            },
            "sensorius": {
                "instance_id": instance_id,
                "base_topic": "nodus",
                "reply_topic": f"sensorius/{instance_id}/onboard/reply",
            },
        }
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
        hello_timeout = float(settings.get_setting("Onboarding", "HELLO_TIMEOUT_SEC", 60) or 60)
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

        try:
            sys_settings = saiSettings(apply_live=False, device_id=device_id)
            network_doc = dict(sys_settings.get_section("Network") or {})
            mqtt_doc = dict(sys_settings.get_section("MQTT") or {})
            display_doc = dict(sys_settings.get_section("Display") or {})
            calibration_doc = dict(sys_settings.get_section("Calibration") or {})
        except Exception:
            pass
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

        import saiAddDevice

        form = await request.form()
        target_ap = str(form.get("target_ap", "") or "").strip() or (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
        target_ap_password = str(form.get("target_ap_password", "") or "")
        local_ssid = str(form.get("local_ssid", "") or "").strip()
        local_password = str(form.get("local_password", "") or "")
        requested_device_id = str(form.get("device_id", "") or "").strip()
        hostname = requested_device_id or str(form.get("hostname", "") or "").strip() or f"nodus-{uuid4().hex[:8]}"

        session_id = uuid4().hex
        issued = onboarding_tokens.issue_token(
            session_id=session_id,
            expected_device_id=requested_device_id,
            ttl_sec=600,
        )
        token = issued["token"]
        onboarding_store.set_state(session_id, OnboardingStates.AP_DISCOVERED)

        ok_ap = False
        try:
            ok_ap = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: saiAddDevice.connect_to_sensor_ap(target_ap, target_ap_password, attempts=3),
            )
        except Exception as e:
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=f"ap_connect_error:{e}")
            return JSONResponse(
                {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "ap_connect_error"},
                status_code=502,
            )

        if not ok_ap:
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="ap_connect_failed")
            return JSONResponse(
                {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "ap_connect_failed"},
                status_code=502,
            )

        if not local_ssid:
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

        if not local_ssid:
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_local_ssid")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="missing_local_ssid")
            return JSONResponse(
                {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "missing_local_ssid"},
                status_code=400,
            )

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
        if not bool(init_result.get("ok", False)):
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

        onboarding_store.set_state(session_id, OnboardingStates.INIT_SENT)
        _emit_onboarding_event("onboarding_init_ack", session_id=session_id, detail=hostname)
        onboarding_store.set_state(session_id, OnboardingStates.WAITING_REBOOT)
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
            }
        )

    @router.get("/api/biodynamic-calendar", response_class=JSONResponse)
    async def api_biodynamic_calendar(month: str = Query("", description="Month anchor in YYYY-MM or YYYY-MM-DD")):
        anchor: date
        try:
            raw = str(month or "").strip()
            if not raw:
                anchor = datetime.now().date().replace(day=1)
            elif len(raw) == 7:
                anchor = datetime.strptime(raw, "%Y-%m").date().replace(day=1)
            else:
                anchor = datetime.fromisoformat(raw).date().replace(day=1)
        except Exception:
            return JSONResponse({"error": "invalid_month"}, status_code=400)

        payload = get_biodynamic_payload(anchor)
        payload["notes"] = data_logger.get_biodynamic_notes_for_month(anchor)
        return JSONResponse(payload)

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

        ok = data_logger.save_biodynamic_note(normalized_date, note_text)
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
            from saiAutomationManager import AutomationManager
            sid = (switch_id or "").strip()
            lbl = (label or "").strip()
            switch_key = f"{sid}::{lbl}" if sid and lbl else ""
            mgr = AutomationManager("switch_settings")
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
        import saiAddDevice
        form = await request.form()
        # Pull what your System Setup dialog already posts.
        # These names are examples; keep them aligned with your current form fields:
        sensor_type = form.get("sensor_type", "")
        location    = form.get("location", "Unknown")
        local_ssid  = form.get("local_ssid", "Unknown")
        # You may already assemble a richer onboarding payload; pass it through:
        payload_json = form.get("payload_json")  # optional richer JSON blob

        job_id = uuid4().hex
        printDM(f"[onboard-start] job_id={job_id} sensor_type={sensor_type} location={location}", location="saiWebRoutes")

        async def run_flow():
            # Step 1: AP connect
            target_ap = (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
            label1 = f"{target_ap} connection established"
            ok1 = False
            try:
                ok1 = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.connect_to_sensor_ap(
                        target_ap,
                        saiAddDevice.PICOW_AP_PASSWORD,
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
                    saiAddDevice.update_hub_clients(saiAddDevice.HUB_SETTINGS_PATH, sensor_id_for_step)
                except Exception as e:
                    printDM(f"[onboard] update_hub_clients failed: {e}", location="saiWebRoutes")
                # Nudge ingest discovery immediately (no restart required)
                try:
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
        try:
            if 'mqtt_ingest' in globals() and getattr(mqtt_ingest, 'resolve_nodus_hostname', None):
                host = mqtt_ingest.resolve_nodus_hostname(device_id, device_type=device_type)
                if host:
                    return host
        except Exception:
            pass

        dev_id = (device_id or "").strip()
        if not dev_id:
            return None

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
            try:
                app_settings = saiSettings(apply_live=False)
                system_root = getattr(app_settings, "system_dir", None) or getattr(app_settings, "settings_root", None)
            except Exception:
                system_root = None
        if not system_root:
            system_root = "system_settings"

        import os
        try:
            import tomllib
        except Exception:
            tomllib = None

        # Direct file read for sensors (and for switches that coincidentally have their own folder)
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
            serial = (device_id.rsplit("-", 1)[-1] if "-" in (device_id or "") else (device_id or "")).strip()
            if serial:
                # 2a) Try to match any known mqtt_clients like '<anything>-<serial>'
                try:
                    if 'mqtt_ingest' in globals() and getattr(mqtt_ingest, 'mqtt_clients', None):
                        for cand in (mqtt_ingest.mqtt_clients or []):
                            cand = str(cand)
                            if cand.endswith(f"-{serial}"):
                                return cand  # bare hostname, e.g. "aqi-nz6g89"
                except Exception:
                    pass

                # 2b) Try to match any known sensor_id in sensor settings
                try:
                    from saiSensorSettingsManager import SensorSettingsManager
                    sm = SensorSettingsManager("sensor_settings")
                    for sid in (sm.list_ids() or []):
                        sid = str(sid)
                        if sid.lower().startswith("switch-"):
                            continue
                        if sid.endswith(f"-{serial}"):
                            return sid  # use the sensor_id as the host (bare)
                except Exception:
                    pass

        return None
    
    def _build_post_targets(hostname: str | None, ip_hint: str | None = None) -> list[str]:
        """
        Short, deterministic target list. mDNS → DNS → optional IP.
        """
        candidates = []
        if hostname:
            candidates.append(f"http://{mdns_hostname(hostname)}:8000/set-nodus-setting")
            candidates.append(f"http://{hostname}:8000/set-nodus-setting")
        if ip_hint:
            candidates.append(f"http://{ip_hint}:8000/set-nodus-setting")

        seen = set()
        return [u for u in candidates if (u not in seen and not seen.add(u))]

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
        import httpx

        hostname = _read_hostname_from_system_settings(
            device_id, system_mgr, system_root,
            device_type=device_type, sys_host_index=sys_host_index
        )
        if not hostname:
            if DEBUG:
                printDM(f"[push_nodus_setting:{device_type}:{device_id}] no HOSTNAME in system settings; skipping POST",
                        location=MODULE)
            return False

        targets = _build_post_targets(hostname, ip_hint)
        if DEBUG:
            printDM(f"[push_nodus_setting:{device_type}:{device_id}] host candidates: {targets}", location=MODULE)

        body = {"file": setting_file_key, "section": section, "key": key, "value": value}
        if sensor_file_name and setting_file_key == "sensor":
            body["name"] = sensor_file_name
        if setting_file_key == "switch" and "name" not in body:
            body["name"] = "switch.toml"

        timeout = httpx.Timeout(9.0, connect=2.5, read=7.0, write=2.0, pool=2.5)
        headers = {"Content-Type": "application/json"}

        primary_host = hostname  # bare host like 'aqi-nz6g89'
        host_lock = _get_host_lock(primary_host)

        try:
            async with host_lock:  # <-- serialize per host
                async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                    # small stagger helps right after a sensor write triggers nodus to reload
                    if setting_file_key == "switch":
                        await asyncio.sleep(0.4)
                    # simple two-pass retry over the same targets
                    for attempt in range(2):
                        for url in targets:
                            try:
                                resp = await client.post(url, json=body)
                                try:
                                    j = resp.json()
                                except Exception:
                                    j = {}
                                if resp.status_code == 200 and bool(j.get("success", True)):
                                    if DEBUG:
                                        printDM(f"[push_nodus_setting:{device_type}:{device_id}] OK via {url}: {body}",
                                                location=MODULE)
                                    return True
                                else:
                                    if DEBUG:
                                        detail = (j or resp.text or "").strip()
                                        printDM(f"[push_nodus_setting:{device_type}:{device_id}] HTTP {resp.status_code} via {url}; detail={detail[:160]}",
                                                location=MODULE)
                            except httpx.HTTPError as e:
                                if DEBUG:
                                    printDM(f"[push_nodus_setting:{device_type}:{device_id}] error {url}: {e}", location=MODULE)
                        # brief backoff between passes
                        await asyncio.sleep(0.35)
        except Exception as outer:
            if DEBUG:
                printDM(f"[push_nodus_setting:{device_type}:{device_id}] AsyncClient init/use error: {outer}", location=MODULE)

        return False

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
        ok_all = True
        for section, key, value in (updates or []):
            ok = await push_nodus_setting_simple(
                device_id=device_id,
                device_type=device_type,
                setting_file_key=setting_file_key,
                section=section,
                key=key,
                value=value,
                sensor_file_name=sensor_file_name,
                system_mgr=system_mgr,
                system_root=system_root,
                ip_hint=ip_hint,
                sys_host_index=sys_host_index,
            )
            ok_all = bool(ok) and ok_all
        return ok_all


    # ---------- user-defined constants ----------
    LOCATIONS_ROUTE_TAG = "device-locations"
    # ----- view and edit device locations -------
    @router.get("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def list_device_locations(request: Request) -> JSONResponse:
        try:
            app_settings = saiSettings(apply_live=False)
            # Resolve directories safely
            system_dir = getattr(app_settings, "base_dir", None) or getattr(app_settings, "settings_root", None) or "."
            system_dir = str(system_dir)

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
        NEW: If the device is a remote Nodus (TYPE == "picow", "pico2w", or "nodus"), also POSTs the update
             to the device's socketserver at /set-nodus-setting.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, list):
                return JSONResponse({"error": "invalid_payload"}, status_code=400)

            sensor_mgr = SensorSettingsManager("sensor_settings")
            switch_mgr = SwitchSettingsManager("switch_settings")
            SystemSettingsMgr = globals().get("SystemSettingsManager", None)
            system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
            try:
                app_settings = saiSettings(apply_live=False)
                system_root = getattr(app_settings, "system_dir", None) or getattr(app_settings, "settings_root", None)
            except Exception:
                system_root = "system_settings"
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
                    import saiWebRoutes as routes
                    sm = getattr(routes, "sensor_map", None)
                return sm
            sensor_map = _get_sensor_map()

            # We’ll gather Nodus POSTs to run concurrently for speed
            nodus_tasks = []

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
                            if any(k in sblk for k in ("I2C_SCL","I2C_SDA","I2C_BUS","I2C_ADDR")):
                                sensor_file_name = "sensor_i2c.toml"
                            elif any(k in sblk for k in ("UART_TX","UART_RX","UART_BUS","RS485_DIR_PIN","MODBUS_ADDR")):
                                sensor_file_name = "sensor_soil.toml"

                            # DEBUG: show which hostname we will use (straight from system settings)
                            resolved_host = _read_hostname_from_system_settings(dev_id, system_mgr, system_root)
                            if DEBUG:
                                printDM(f"[save_device_locations] sensor {dev_id} resolved host: {resolved_host}", location=MODULE)

                            nodus_tasks.append(
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
                            )

                        try:
                            topic = f"sensor/{dev_id}/data"
                            if hasattr(mqtt_ingest, "device_location") and isinstance(mqtt_ingest.device_location, dict):
                                mqtt_ingest.device_location[topic] = location or "Unknown"
                        except Exception:
                            pass

                    else:  # switch
                        doc = switch_mgr.load(dev_id) or {}
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
                            resolved_host = _read_hostname_from_system_settings(dev_id, system_mgr, system_root)
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

                            nodus_tasks.append(
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
                            )

                    if DEBUG:
                        printDM(f"dev_type: {dev_type}, dev_id: {dev_id}, dev_loc: {location}",
                                location=f"{MODULE}.save_device_locations")

                except Exception as row_err:
                    printDM(f"[save_device_locations] row error: {row_err}", location=MODULE)

            # Run all remote pushes
            if nodus_tasks:
                try:
                    results = await asyncio.gather(*nodus_tasks, return_exceptions=True)
                    pushed = sum(1 for r in results if (r is True))
                    updated["nodus_pushed"] = pushed
                    # Optional: log failures individually if DEBUG
                    if DEBUG:
                        failures = [str(r) for r in results if r is not True]
                        if failures:
                            printDM(f"[save_device_locations] nodus push failures: {failures}", location=MODULE)
                except Exception as e:
                    if DEBUG:
                        printDM(f"[save_device_locations] nodus push gather error: {e}", location=MODULE)

            return JSONResponse({"ok": True, "updated": updated})

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
            from saiMQTTIngest import get_current_ingest as _get_ing
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
        try:
            ids.update(getattr(ing, "mqtt_clients", []) or [])
        except Exception:
            pass
        try:
            for host, peers in (getattr(ing, "host_to_peer_ids", {}) or {}).items():
                if host:
                    ids.add(host)
                for peer in (peers or []):
                    if peer:
                        ids.add(peer)
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

    def _enumerate_dirs(base_dir:str)->list[str]:
        try:
            p=Path(base_dir)
            if not p.exists(): return []
            return [d.name for d in p.iterdir() if d.is_dir()]
        except Exception:
            return []

    def _collect_removable_ids() -> list[str]:
        """
        Aggregate IDs that are candidates for removal from:
          - MQTT discovery (in-memory)
          - sensor, switch, and system settings directories.

        Excludes:
          - our own hub host folder
          - special folders like '__pycache__'
          - 'factory' metadata folders
        """
        ids: set[str] = set(_collect_ingest_ids())

        for base_dir in (_SENSOR_BASE_DIR, _SWITCH_BASE_DIR, _SYS_BASE_DIR):
            for name in _enumerate_dirs(base_dir):
                if name:
                    ids.add(name)

        # do not list hub host folder (our own hostname)
        try:
            hub_name = Path(HUB_SETTINGS_PATH).parent.name
            if hub_name:
                ids.discard(hub_name)
        except Exception:
            pass

        # filter out known non-removable / meta folders
        banned = {"__pycache__", "factory"}
        filtered_ids = [
            dev_id
            for dev_id in ids
            if dev_id and dev_id.lower() not in banned
        ]

        return sorted(filtered_ids)

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

        _add(device_id)
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
            return out

        for host, peers in list(mapping.items()):
            host_aliases = _alias_set(host)
            peer_aliases: set[str] = set()
            for peer in (peers or []):
                peer_aliases |= _alias_set(peer)
            if wanted & (host_aliases | peer_aliases):
                _add(host)
                for peer in (peers or []):
                    _add(peer)

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
            from saiSensorSettingsManager import SensorSettingsManager
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
            from saiHomeAssistantMqtt import slugify, HomeAssistantTopicMap
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
        try:
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_command_topics or {}).items():
                if str(sw_id or "").strip().lower() in ids_l and topic:
                    topics.add(topic)
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_state_topics or {}).items():
                if str(sw_id or "").strip().lower() in ids_l and topic:
                    topics.add(topic)
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_event_topics or {}).items():
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
            for dname in ("device_type", "expected_gauge_map", "latest_meta"):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            for dname in ("device_status", "last_mqtt_seen", "nodus_availability", "last_heartbeat_ts", "last_heartbeat_payload", "heartbeat_interval_s_by_host", "heartbeat_stale"):
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
                if dev == device_id:
                    ing.topic_dev_id_map.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for topic, meta in list((ing.switch_topic_meta or {}).items()):
                if (meta or {}).get("switch_id") == device_id:
                    ing.switch_topic_meta.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_control_map or {}).keys()):
                if key and key[0] == device_id:
                    ing.switch_control_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_channel_map or {}).keys()):
                if key and key[0] == device_id:
                    ing.switch_channel_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for topic, info in list((ing.nodus_switch_topic_map or {}).items()):
                if (info or {}).get("switch_id") == device_id:
                    ing.nodus_switch_topic_map.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.nodus_switch_command_topics or {}).keys()):
                if key and key[0] == device_id:
                    ing.nodus_switch_command_topics.pop(key, None); _bump()
            for key in list((ing.nodus_switch_state_topics or {}).keys()):
                if key and key[0] == device_id:
                    ing.nodus_switch_state_topics.pop(key, None); _bump()
            for key in list((ing.nodus_switch_event_topics or {}).keys()):
                if key and key[0] == device_id:
                    ing.nodus_switch_event_topics.pop(key, None); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_known_switch_ids", None)
            if isinstance(s, set) and device_id in s:
                s.discard(device_id); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_sensor_metrics", None)
            if isinstance(s, set):
                for key in list(s):
                    if key.startswith(f"{device_id}::"):
                        s.discard(key); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_switch_channels", None)
            if isinstance(s, set):
                for key in list(s):
                    if key.startswith(f"{device_id}::"):
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

        try:
            if data_logger:
                if device_id in data_logger.sensor_values:
                    data_logger.sensor_values.pop(device_id, None); _bump()
                if device_id in getattr(data_logger, "sensor_stats", {}):
                    data_logger.sensor_stats.pop(device_id, None); _bump()
        except Exception:
            pass

        return stats

    def _delete_device_dirs(device_id:str)->dict:
        removed={"sensor":False,"switch":False,"system":False}
        targets = [
            ("sensor", _safe_child_path(Path(_SENSOR_BASE_DIR), device_id)),
            ("switch", _safe_child_path(Path(_SWITCH_BASE_DIR), device_id)),
            ("system", _safe_child_path(Path(_SYS_BASE_DIR), device_id)),
        ]
        for key, path in targets:
            try:
                if path is None:
                    continue
                if path.exists():
                    shutil.rmtree(path)
                    removed[key]=True
            except Exception as e:
                printDM(f"[remove-device] rmtree {path}: {e}", location=MODULE)
        return removed

    def _get_db_path()->str:
        try:
            from saiDataLogger import saiDataLogger  # type: ignore
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
        if not Path(db_path).exists(): return stats
        try:
            conn=sqlite3.connect(db_path)
            cur=conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables=[r[0] for r in cur.fetchall()]
            target_cols=["sensor_id","device_id","client_id"]
            like_cols=["topic","source","channel"]
            for t in tables:
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
        from saiSensorSettingsManager import SensorSettingsManager
        mgr = SensorSettingsManager("sensor_settings")
        locations: dict[str, str] = {}
        for sid in mgr.list_ids():
            loc = mgr.get_setting(sid, "Sensor.LOCATION", "Unknown")
            locations[sid] = loc or "Unknown"
        return locations

    #remove device routes
    @router.get("/remove-device-list")
    async def remove_device_list(request: Request):
        _require_protected_access(request)
        devices = await asyncio.to_thread(_collect_removable_ids)
        return JSONResponse({"devices": devices})

    @router.get("/remove-device")
    async def remove_device_modal_hint(request: Request):
        """
        Kept for compatibility in case someone navigates to /remove-device.
        We just return a tiny page that instructs to use the modal button.
        """
        _require_protected_access(request)
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

        results = {}
        try:
            from saiMQTTIngest import get_current_ingest as _get_ing
            mqtt_ingest = _get_ing()
        except Exception:
            mqtt_ingest = None
        for dev in device_ids:
            removed_dirs, db_stats = await asyncio.gather(
                asyncio.to_thread(_delete_device_dirs, dev),
                asyncio.to_thread(_purge_device_from_db, dev),
            )
            ok_settings = await asyncio.to_thread(_remove_client_from_hub_settings, dev)
            ha_stats = await asyncio.to_thread(_clear_ha_entities, dev, mqtt_ingest=mqtt_ingest, data_logger=data_logger)
            mqtt_stats = await asyncio.to_thread(_clear_retained_mqtt_topics, dev, mqtt_ingest=mqtt_ingest)
            ingest_stats = _purge_ingest_cache(dev, mqtt_ingest=mqtt_ingest, data_logger=data_logger)
            summary = (
                f"dirs(sensor={removed_dirs.get('sensor')},switch={removed_dirs.get('switch')},system={removed_dirs.get('system')}), "
                f"db_rows={db_stats.get('rows_deleted',0)}, clients_updated={ok_settings}, "
                f"ha_topics={ha_stats.get('topics_cleared',0)}, mqtt_topics={mqtt_stats.get('topics_cleared',0)}, "
                f"ingest_keys={ingest_stats.get('ingest_keys_cleared',0)}"
            )
            results[dev] = {
                "dirs": removed_dirs,
                "db": db_stats,
                "clients_updated": ok_settings,
                "ha": ha_stats,
                "mqtt": mqtt_stats,
                "ingest": ingest_stats,
                "summary": summary,
            }
            printDM(f"[remove-device] {dev}: {summary}", location=MODULE)

        overall = f"Removed {len(device_ids)} device(s)."
        return JSONResponse({"results": results, "summary": overall})

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

        broker = str(form.get("broker", "") or "").strip()
        tz = str(form.get("tz", "") or "").strip()
        raw_httpport = str(form.get("httpport", "") or "").strip()
        raw_lat = str(form.get("astral_lat", "") or "").strip()
        raw_lon = str(form.get("astral_lon", "") or "").strip()
        gauge_size = str(form.get("gauge_size", "") or "").strip()
        display_style = str(form.get("display_style", "") or "").strip()

        if not tz:
            return PlainTextResponse("Time zone is required.", status_code=400)
        try:
            ZoneInfo(tz)
        except Exception:
            return PlainTextResponse(f"Invalid timezone '{tz}'. Use a valid IANA timezone (example: America/Denver).", status_code=400)

        try:
            httpport = int(raw_httpport or "8000")
        except Exception:
            return PlainTextResponse("HTTP Port must be a number.", status_code=400)
        if httpport < 1 or httpport > 65535:
            return PlainTextResponse("HTTP Port must be between 1 and 65535.", status_code=400)

        lat_to_store = ""
        lon_to_store = ""
        if raw_lat or raw_lon:
            try:
                lat_val = float(raw_lat)
                lon_val = float(raw_lon)
            except Exception:
                return PlainTextResponse("Latitude and Longitude must be numeric values.", status_code=400)
            if not (-90.0 <= lat_val <= 90.0):
                return PlainTextResponse("Latitude must be between -90 and 90.", status_code=400)
            if not (-180.0 <= lon_val <= 180.0):
                return PlainTextResponse("Longitude must be between -180 and 180.", status_code=400)
            lat_to_store = f"{lat_val:.6f}"
            lon_to_store = f"{lon_val:.6f}"

        tz_offset, tz_name = settings.timezone_info(tz)

        settings.replace_setting("Network", "HTTPPORT", httpport)
        settings.replace_setting("SensorNetwork", "BROKER", broker)
        settings.replace_setting("Time", "TZ", tz)
        settings.replace_setting("Time", "TZ_OFFSET", tz_offset)
        settings.replace_setting("Time", "TZ_NAME", tz_name)
        settings.replace_setting("Astral", "TIMEZONE", tz)
        settings.replace_setting("Astral", "LATITUDE", lat_to_store)
        settings.replace_setting("Astral", "LONGITUDE", lon_to_store)
        settings.replace_setting("Display", "gauge_size", gauge_size)
        settings.replace_setting("Display", "display_style", display_style)

        return RedirectResponse(url="/?refresh=true", status_code=303)        

    @router.post("/submit-homeassistant-settings")
    async def submit_homeassistant_settings(request: Request):
        settings = saiSettings()
        try:
            data = await request.json()
        except Exception:
            data = {}

        enabled = bool(data.get("enabled", False))
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
        settings.replace_setting("HomeAssistant", "HA_BROKER", broker)
        settings.replace_setting("HomeAssistant", "HA_MQTTPORT", port)
        settings.replace_setting("HomeAssistant", "HA_USERNAME", username)
        settings.replace_setting("HomeAssistant", "HA_PASSWORD", saiSettings.obfuscate_secret(password))

        return JSONResponse({"status": "ok"})

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
        dbg_raw = str(env_map.get("SENSORIUS_DEBUG_MODULES", "") or "")
        debug_modules = [m.strip() for m in dbg_raw.split(",") if m.strip()]

        try:
            retention_days = int(str(env_map.get("SENSORIUS_DB_RETENTION_DAYS", "90") or "90"))
        except Exception:
            retention_days = 90
        retention_days = max(30, min(180, retention_days))

        autostart_scope = str(env_map.get("SENSORIUS_AUTOSTART_SCOPE", "user") or "user").strip().lower()
        if autostart_scope not in {"user", "system"}:
            autostart_scope = "user"
        autostart_enabled = _autostart_is_enabled(autostart_scope)

        return JSONResponse({
            "platform": platform.system(),
            "autostart_scope": autostart_scope,
            "autostart_enabled": bool(autostart_enabled),
            "log_level": log_level,
            "file_log": bool(file_log),
            "debug_module_choices": list(_ADV_DEBUG_MODULE_CHOICES),
            "debug_modules": debug_modules,
            "db_retention_days": retention_days,
            "autostart_note": "If you manually run 'python Sensorius.py', stop that instance before enabling auto-start to avoid duplicate instances.",
            "autostart_scope_note": "macOS user-level launchctl is default. System-level may require admin privileges.",
        })

    @router.post("/advanced/save")
    async def advanced_save(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        log_level = str(body.get("log_level", "DEBUG") or "DEBUG").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            return JSONResponse({"error": "invalid_log_level"}, status_code=400)

        file_log = bool(body.get("file_log", False))
        autostart_enabled = bool(body.get("autostart_enabled", False))
        autostart_scope = str(body.get("autostart_scope", "user") or "user").strip().lower()
        if autostart_scope not in {"user", "system"}:
            return JSONResponse({"error": "invalid_autostart_scope"}, status_code=400)

        debug_modules_in = body.get("debug_modules", [])
        if not isinstance(debug_modules_in, list):
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

        try:
            retention_days = int(body.get("db_retention_days", 90))
        except Exception:
            return JSONResponse({"error": "invalid_db_retention_days"}, status_code=400)
        if retention_days < 30 or retention_days > 180:
            return JSONResponse({"error": "invalid_db_retention_days_range"}, status_code=400)

        updates = {
            "SENSORIUS_LOG_LEVEL": log_level,
            "SENSORIUS_FILE_LOG": _bool_text(file_log),
            "SENSORIUS_DEBUG_MODULES": ",".join(clean_modules),
            "SENSORIUS_DB_RETENTION_DAYS": str(retention_days),
            "SENSORIUS_AUTOSTART_SCOPE": autostart_scope,
            "SENSORIUS_AUTOSTART_ENABLED": _bool_text(autostart_enabled),
        }

        try:
            _write_env_updates(updates)
        except Exception as ex:
            return JSONResponse({"error": f"env_write_failed: {ex}"}, status_code=500)

        ok, msg = _autostart_apply(autostart_enabled, autostart_scope)
        return JSONResponse({
            "status": "ok" if ok else "partial",
            "autostart_applied": bool(ok),
            "autostart_message": msg,
        })

    @router.get("/scan-nodus-setup")
    async def scan_nodus_setup(ssid: str = Query(None)):
        target_ssid = (ssid or "").strip()
        if not target_ssid:
            try:
                import saiAddDevice
                target_ssid = (getattr(saiAddDevice, "PICOW_AP_SSID", "") or "").strip()
            except Exception:
                target_ssid = ""
        if not target_ssid:
            target_ssid = "Nodus_Setup"
        found, msg = await asyncio.to_thread(_scan_for_ssid, target_ssid)
        return JSONResponse({
            "ssid": target_ssid,
            "found": bool(found),
            "platform": platform.system(),
            "message": msg,
        })

    @router.get("/sensor-ids", response_class=JSONResponse)
    async def list_sensor_ids():
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
                import saiWebRoutes as routes
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

        for src in (local_ids, discovered, logged_ids):
            for sid in src:
                _add(sid)
                
        if DEBUG:
            printDM(f"[{MODULE}] #4 - merged sensors {merged}", location=MODULE)

        merged = [sid for sid in merged if _is_recent_sensor(sid)]
        return JSONResponse(sorted(merged))

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
                import saiWebRoutes as routes
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
    
    def resolve_nodus_base_url(sensor_id: str) -> str | None:
        """
        Resolve the base URL for a Nodus (Pico 2W) device that owns this sensor_id.

        Reads sensor_settings/<sensor_id>/sensor.toml via SensorSettingsManager and
        expects something like:

        [Sensor]
        TYPE = "pico2w"   # (or "picow" for backward compatibility)
        ...

        [Network]
        HOSTNAME = "nodus-xyz"

        Returns:
        "http://nodus-xyz.local:8000" or None if this is not a Nodus sensor
        or we can't derive a hostname.
        """
        try:
            from saiSensorSettingsManager import SensorSettingsManager
        except Exception as exc:
            if DEBUG:
                printDM(f"resolve_nodus_base_url import failed: {exc}", location=MODULE)
            return None

        try:
            mgr = SensorSettingsManager("sensor_settings")
            cfg = mgr.load(sensor_id) or {}
        except Exception as exc:
            if DEBUG:
                printDM(f"resolve_nodus_base_url failed to load settings for {sensor_id}: {exc}", location=MODULE)
            return None

        sensor_block = (cfg.get("Sensor") or {}) if isinstance(cfg, dict) else {}
        sensor_type  = str(sensor_block.get("TYPE", "") or "").strip().lower()

        # Only treat this as Nodus if TYPE explicitly indicates a Pico/Nodus type
        if sensor_type not in ("picow", "pico2w", "nodus", "remote"):
            # Local Pi sensor or unknown type → no Nodus base URL
            return None

        net_block = (cfg.get("Network") or {}) if isinstance(cfg, dict) else {}
        hostname  = str(net_block.get("HOSTNAME", "") or "").strip()
        if not hostname:
            if DEBUG:
                printDM(f"resolve_nodus_base_url: no Network.HOSTNAME for {sensor_id}", location=MODULE)
            return None

        # Normalize to something resolvable via mDNS
        hostname = mdns_hostname(hostname)

        base_url = f"http://{hostname}:8000"
        if DEBUG:
            printDM(f"resolve_nodus_base_url: {sensor_id} → {base_url}", location=MODULE)
        return base_url
            
    async def forward_calibration_to_nodus(base_url: str, payload: dict) -> bool:
        """
        Forward the calibration payload to the Nodus (Pico 2W) device at base_url.

        base_url: e.g. "http://nodus-xyz.local:8000"
        payload:  same JSON object we received on /update-calibration-values

        Returns True on HTTP < 400, False otherwise.
        """
        import httpx  
        if DEBUG:
            printDM(
                f"forward_calibration_to_nodus called)",
                location=MODULE,
            )
                
        url = f"{base_url.rstrip('/')}/update-calibration-values"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=8.0)
            if resp.status_code >= 400:
                if DEBUG:
                    printDM(
                        f"forward_calibration_to_nodus: {url} → {resp.status_code} {resp.reason_phrase}",
                        location=MODULE,
                    )
                return False
            if DEBUG:
                printDM(
                    f"forward_calibration_to_nodus: {url} success ({resp.status_code})",
                    location=MODULE,
                )
            return True
        except Exception as exc:
            if DEBUG:
                printDM(f"forward_calibration_to_nodus error for {url}: {exc}", location=MODULE)
            return False

    def _is_remote_nodus_type(sensor_type: str | None) -> bool:
        return str(sensor_type or "").strip().lower() in ("picow", "pico2w", "nodus", "remote")

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
                    dev["SOIL_TEMP_MOIST_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_TEMP_MOIST_VAL")
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

    def _mqtt_calibration_payload_from_offsets(offsets: list[dict]) -> dict:
        return {"offsets": [dict(item) for item in (offsets or [])]}

    async def _publish_remote_calibration_command(sensor_id: str, *, action: str, payload: dict | None = None, ack_timeout: float = 3.0, result_timeout: float = 8.0) -> tuple[bool, str, dict | None, dict | None]:
        ingest = getattr(app.state, "mqtt_ingest", None) or mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_calibration"):
            return False, "MQTT ingest unavailable", None, None

        publish_result = ingest.publish_nodus_calibration(sensor_id, action=action, payload=payload)
        if not bool(publish_result.get("ok", False)):
            return False, "Failed to publish calibration command", None, None

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_calibration_ack(message_id, timeout=ack_timeout)
        if not ack or not bool(ack.get("accepted", False)):
            return False, "Calibration command was not acknowledged", ack, None

        result = await ingest.wait_for_calibration_result(message_id, timeout=result_timeout)
        if result is None:
            return False, "Timed out waiting for calibration result", ack, None
        return True, "", ack, result


    # --- Edit Sensor (modal / template) ---
    @router.get("/edit-sensor", response_class=HTMLResponse)
    async def edit_sensor_page(
        request: Request,
        sensor_id: str = Query(...),
        embed: int = Query(0),
    ):
        from saiSensorSettingsManager import SensorSettingsManager
        from saiCalibration import CalibrationManager
        from saiUtils import normalize_sensor_id, printDM, html_escape
        import sqlite3
        import json

        MODULE = "edit-sensor"
        db_path = _get_db_path()

        def fetch_metrics_for_sensor_id(db_sensor_id: str) -> list[str]:
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT DISTINCT metric FROM readings
                        WHERE sensor_id = ? COLLATE NOCASE
                        ORDER BY metric
                        """,
                        (db_sensor_id,),
                    )
                    return [row[0] for row in cursor.fetchall()]
            except Exception as e:
                printDM(
                    f"[{MODULE}] Metric query failed for {db_sensor_id}: {e}",
                    location="saiWebRoutes",
                )
                return []

        try:
            normalized_id = normalize_sensor_id(sensor_id)

            manager = SensorSettingsManager("sensor_settings")
            settings_dict = manager.load(normalized_id)
            if not settings_dict:
                return HTMLResponse(
                    f"<h3>❌ No settings found for sensor '{html_escape(sensor_id)}'</h3><a href='/'>Return</a>",
                    status_code=404,
                )

            # --- Build metric options (from DB, fallback to Display block) ---
            available_metrics = fetch_metrics_for_sensor_id(normalized_id)

            display_block = settings_dict.get("Display", {}) or {}
            current_metrics_any_case: list[str] = []
            for i in range(1, 7):
                current = (
                    display_block.get(f"METRIC_{i}")
                    or display_block.get(f"metric_{i}")
                    or ""
                )
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
                current_metrics.append(val)

            # location
            sensor_section = settings_dict.get("Sensor", {}) or {}
            location = (
                sensor_section.get("LOCATION")
                or sensor_section.get("location")
                or "Unknown"
            )

            # --- Calibration context (used by split-pane sensor modal) ---
            calib_section = (settings_dict.get("Calibration") or {}) or {}
            device_section = (calib_section.get("Device") or calib_section.get("device") or {}) or {}
            raw_device = str(sensor_section.get("DEVICE", "") or "")
            device_kind = raw_device.strip().lower()
            device_label = raw_device or device_kind or "Unknown"
            is_apvpd = (device_kind == "apvpd")

            def _get_float(section: dict, key: str, default: float = 0.0) -> float:
                try:
                    return float(section.get(key, default) or default)
                except Exception:
                    return default

            ambient_temp_offset = _get_float(calib_section, "APVPD_TEMP_CAL_VAL", 0.0)
            ambient_rh_offset = _get_float(calib_section, "APVPD_RH_CAL_VAL", 0.0)

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

            if device_kind in ("co2",):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")
                _add_offset("Calibration.Device.CO2_OFFSET", "CO₂", "ppm", "CO2_OFFSET")
            elif device_kind in ("aqi",):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")
                _add_offset("Calibration.Device.AQI_OFFSET", "AQI", "", "AQI_OFFSET")
                _add_offset("Calibration.Device.GAS_OFFSET", "Gas resistance", "kΩ", "GAS_OFFSET")
            elif device_kind in ("veml", "lux"):
                _add_offset("Calibration.Device.LUX_OFFSET", "Light Intensity", "lux", "LUX_OFFSET")
                _add_offset("Calibration.Device.PPFD_OFFSET", "PPFD", "µmol/m²/s", "PPFD_OFFSET")
            elif device_kind in ("vpd", "avpd"):
                _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
                _add_offset("Calibration.Device.RH_OFFSET", "Rel-Humidity", "%", "RH_OFFSET")

            cal_mgr = CalibrationManager(data_logger, manager)
            candidate_sensors = cal_mgr.get_calibratable_sensors() or []

            # Render template
            templates = request.app.state.templates
            template = templates.get_template("modals/sensor_settings.html")
            modal_html = template.render(
                sensor_id=normalized_id,
                settings=settings_dict,
                metric_options=metric_options,
                current_metrics=current_metrics,
                location=location,
                device_kind=device_kind,
                device_label=device_label,
                is_apvpd=is_apvpd,
                ambient_temp_offset=ambient_temp_offset,
                ambient_rh_offset=ambient_rh_offset,
                device_offsets=device_offsets,
                candidate_sensors=candidate_sensors,
                default_range_hours=24,
            )

            if embed:
                # just return snippet for dashboard JS
                return HTMLResponse(modal_html)

            # Full-page fallback (used rarely)
            page: list[str] = []
            page.append("<!DOCTYPE html>")
            page.append("<html><head><title>Edit Sensor</title>")
            page.append("<link rel='stylesheet' href='/ui_static/css/app.css'>")
            page.append("<script src='/ui_static/js/sensor_settings_modal.js'></script>")
            page.append("<script src='/ui_static/js/system_calibration.js'></script>")
            page.append("</head><body>")
            page.append("<div id='modalHost'></div>")
            page.append(f"<script>var __MODAL_HTML__ = {json.dumps(modal_html)};</script>")
            page.append("<script>")
            page.append("  (function(){")
            page.append("    var host = document.getElementById('modalHost') || document.body;")
            page.append("    host.innerHTML = __MODAL_HTML__;")
            page.append("    var modal = document.getElementById('sensorSettingsModal');")
            page.append("    if (modal && window.initSensorSettingsModal) window.initSensorSettingsModal(modal);")
            page.append("    if (modal && window.initSystemCalibrationModal) window.initSystemCalibrationModal(modal);")
            page.append("  })();")
            page.append("</script>")
            page.append("</body></html>")
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
            return f"http://{mdns_hostname(host)}:8000"

        def _sensor_updates_for_nodus(merged_doc: OrderedDict, metric_list: list[str]) -> list[tuple[str, str, Any]]:
            updates: list[tuple[str, str, Any]] = []
            sensor_block = merged_doc.get("Sensor", {}) if isinstance(merged_doc, dict) else {}
            if isinstance(sensor_block, dict):
                for key in ("DEVICE", "SENSOR_ID", "LOCATION"):
                    if key in sensor_block:
                        updates.append(("Sensor", key, sensor_block.get(key, "")))

            if metric_list:
                for idx in range(1, 7):
                    value = metric_list[idx - 1] if idx - 1 < len(metric_list) else ""
                    updates.append(("Display", f"METRIC_{idx}", value))
            return updates

        async def push_updates_to_picow(base_dir: Path, sensor_id_norm: str, device_file: str,
                                        merged_doc: OrderedDict, metric_list: list[str],
                                        *,
                                        lookup_device_id: str,
                                        system_mgr=None,
                                        system_root: str | None = None,
                                        sys_host_index: dict[str, str] | None = None) -> None:
            mgr = SensorSettingsManager(str(base_dir))
            live_doc = mgr.load(sensor_id_norm) or {}
            updates = _sensor_updates_for_nodus(merged_doc, metric_list)
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
                    await mqtt_ingest.force_refresh_device_metadata(lookup_device_id)
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
            return HTMLResponse("<h3>Missing sensor_id</h3><a href='/'>Return</a>", status_code=400)

        old_id = normalize_sensor_id(sensor_id_in_form)
        manager = SensorSettingsManager("sensor_settings")
        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        try:
            app_settings = saiSettings(apply_live=False)
            system_root = getattr(app_settings, "system_dir", None) or getattr(app_settings, "settings_root", None)
        except Exception:
            system_root = "system_settings"
        sys_host_index = _build_system_hostname_index(system_root)

        device_value   = (form.get("device", "") or "").strip()
        new_id_field   = (form.get("sensor_id_field", old_id) or "").strip()
        location_value = (form.get("location", "") or "").strip()
        new_id = normalize_sensor_id(new_id_field)

        # ---------- Load full current doc & build merged update ----------
        existing_doc = manager.load(old_id) or OrderedDict()

        # Ensure top-level section order; if missing, seed them
        if not isinstance(existing_doc, OrderedDict):
            existing_doc = OrderedDict(existing_doc)
        for section in ("Sensor", "Calibration", "Display"):
            existing_doc.setdefault(section, OrderedDict())

        # Prepare updates for [Sensor]
        sensor_updates = {
            "Sensor": OrderedDict({
                # Keep existing TYPE/SERIAL_NUM if present; only update specific fields
                "DEVICE": device_value or existing_doc["Sensor"].get("DEVICE", ""),
                "SENSOR_ID": new_id or existing_doc["Sensor"].get("SENSOR_ID", old_id),
                "LOCATION": location_value or existing_doc["Sensor"].get("LOCATION", "Unknown"),
            })
        }

        # Only modify [Display] if at least one metric_* is present in the form
        metric_keys_present = any(f"metric_{i}" in form for i in range(1, 7))
        display_updates: dict = {}
        metric_list: list[str] = []
        if metric_keys_present:
            metric_list = [(form.get(f"metric_{i}", "") or "").strip() for i in range(1, 7)]
            display_updates = {"Display": {f"METRIC_{i}": metric_list[i-1] for i in range(1, 7)}}

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

        # ---------- Rename folder if SENSOR_ID actually changed ----------
        base_dir = Path(getattr(manager, "base_dir", "sensor_settings"))
        old_dir = base_dir / old_id
        new_dir = base_dir / new_id
        if new_id != old_id:
            try:
                if old_dir.exists():
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    if new_dir.exists():
                        for item in old_dir.iterdir():
                            dest = new_dir / item.name
                            if dest.exists():
                                if dest.is_file():
                                    dest.unlink()
                                else:
                                    shutil.rmtree(dest)
                            shutil.move(str(item), str(dest))
                        shutil.rmtree(old_dir)
                    else:
                        shutil.move(str(old_dir), str(new_dir))
                printDM(f"[{MODULE}] Renamed settings directory: {old_id} → {new_id}", location="saiWebRoutes")
            except Exception as e:
                printDM(f"[{MODULE}] Failed to rename {old_id}→{new_id}: {e}", location="saiWebRoutes")

        # ---------- If Pico2 W-backed, push only the relevant blocks ----------
        live_dir = new_dir if new_id != old_id else old_dir
        try:
            sensor_type = detect_sensor_type(live_dir)  # 'picow' / 'pico2w' / 'pi' / None
        except Exception:
            sensor_type = None

        if sensor_type in ("picow", "pico2w", "nodus"):
            device_toml = guess_device_toml(device_value)
            await push_updates_to_picow(
                base_dir,
                new_id,
                device_toml,
                merged_doc,
                metric_list,
                lookup_device_id=old_id,
                system_mgr=system_mgr,
                system_root=system_root,
                sys_host_index=sys_host_index,
            )

        return RedirectResponse(url="/", status_code=303)

    @router.post("/calibrate")
    async def calibrate_sensor(sensor_id: str = Query(...)):
        from saiUtils import normalize_sensor_id, printDM
        from saiSensorSettingsManager import SensorSettingsManager
        import asyncio

        def _get_sensor_map():
            sm = getattr(app.state, "sensor_map", None)
            if sm is None:
                import saiWebRoutes as routes
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
            settings_dict = mgr.load(sid_norm) or {}
            sensor_block = (settings_dict.get("Sensor") or settings_dict.get("sensor") or {})
            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            if _is_remote_nodus_type(dev_type):
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
        from saiUtils import normalize_sensor_id, printDM
        from saiSensorSettingsManager import SensorSettingsManager

        try:
            sid_norm = normalize_sensor_id(sensor_id)
            mgr = SensorSettingsManager("sensor_settings")
            settings = mgr.load(sid_norm) or {}

            sensor_block = (settings.get("Sensor") or settings.get("sensor") or {})
            cal_block = (settings.get("Calibration") or settings.get("calibration") or {})

            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
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
            if dev_type == "pi":
                from saiWebRoutes import sensor_map
                ctrl = sensor_map.get(sid_norm)
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
            if ingest and hasattr(ingest, "get_nodus_calibration_state"):
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
        from saiUtils import normalize_sensor_id, printDM
        from saiSensorSettingsManager import SensorSettingsManager
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
                from saiWebRoutes import sensor_map
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
            from saiWebRoutes import sensor_map
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

          - soil_moisture_offset -> Calibration.Device.SOIL_TEMP_MOIST_VAL
          - soil_temp_offset     -> Calibration.Device.SOIL_TEMP_CAL_VAL
          - soil_ph_offset       -> Calibration.Device.SOIL_PH_CAL_VAL
          - soil_ec_offset       -> Calibration.Device.SOIL_EC_CAL_VAL
        """
        from saiUtils import printDM
        from saiCalibration import notify_sensor_runtime_of_calibration

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
            doc = mgr.load(sensor_id) or {}
        except FileNotFoundError:
            doc = {}

        sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
        sensor_type = str(sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()

        if _is_remote_nodus_type(sensor_type):
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

        if not _is_remote_nodus_type(sensor_type):
            try:
                supervisor = getattr(request.app.state, "supervisor", None)
                notify_sensor_runtime_of_calibration(supervisor, sensor_id)
            except Exception as exc:
                printDM(
                    f"[{MODULE}] device_calibration_apply reload error for {sensor_id}: {exc}",
                    location=MODULE,
                )


        msg = f"Updated {len(applied_keys)} device calibration value(s) for {sensor_id}."
        return JSONResponse(
            {
                "status": "success",
                "message": msg,
                "applied": applied_keys,
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
        from saiCalibration import CalibrationManager

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
        if device_kind in ("co2",):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.CO2_OFFSET",  "CO₂", "ppm", "CO2_OFFSET")

        # AQI devices (BME680/BME688 → DEVICE="aqi")
        elif device_kind in ("aqi",):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.AQI_OFFSET",  "AQI", "", "AQI_OFFSET")
            _add_offset("Calibration.Device.GAS_OFFSET",  "Gas resistance", "kΩ", "GAS_OFFSET")

        # LUX devices (VEML7700 → DEVICE="veml"; allow "lux" synonym)
        elif device_kind in ("veml", "lux"):
            # User-visible metrics that should be adjustable:
            #   - "Light Intensity" (lux)  → LUX_OFFSET
            #   - "PPFD" (µmol/m²/s)       → PPFD_OFFSET
            _add_offset(
                "Calibration.Device.LUX_OFFSET",
                "Light Intensity",
                "lux",
                "LUX_OFFSET",
            )
            _add_offset(
                "Calibration.Device.PPFD_OFFSET",
                "PPFD",
                "µmol/m²/s",
                "PPFD_OFFSET",
            )

        # Non-APVPD VPD sensors (DEVICE="vpd" or "avpd")
        elif device_kind in ("vpd", "avpd"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")

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
            "ambient_temp_offset": ambient_temp_offset,
            "ambient_rh_offset": ambient_rh_offset,
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
        from saiCalibration import CalibrationManager
        from saiSensorSettingsManager import SensorSettingsManager
        from saiDataLogger import saiDataLogger

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
        from saiCalibration import CalibrationManager, SystemCalResult

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
                doc = sensor_mgr.load(sensor_id) or {}
                sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
                sensor_type = (
                    sensor_blk.get("TYPE")
                    or sensor_blk.get("type")
                    or ""
                )
                sensor_type = str(sensor_type).strip().lower()
                if _is_remote_nodus_type(sensor_type):
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
        from saiCalibration import apply_calibration_updates_local, notify_sensor_runtime_of_calibration
        
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
          - {"key": "switch_id::label"}
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
        otherwise fall back to '<channel_id>::<label>'.
        """
        sid = (switch_id or "").strip()
        lab = (label or "").strip()
        ch_id = _resolve_channel_id_from_label(sid, lab)
        if _build_switch_key is not None:
            try:
                return _build_switch_key(ch_id, lab)
            except Exception:
                pass
        return f"{ch_id}::{lab}"

    @router.get("/edit-switch", response_class=HTMLResponse)
    async def edit_switch_page(
        request: Request,
        switch_id: str = Query(...),
        embed: int = Query(0),
    ):
        from saiSwitchSettingsManager import SwitchSettingsManager

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

        # ---- helper: extract enabled channel indices (same semantics as saiHtml._extract_channel_indices) ----
        sw = (settings_dict or {}).get("Switch", {}) or {}

        def _has_install_marker(val) -> bool:
            if val is None:
                return False
            if isinstance(val, bool):
                return val
            return str(val).strip() != ""

        def _extract_channel_indices(sw_section: dict) -> list[int]:
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
                return [1]

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
            return render_indices or [1]

        channel_indices = _extract_channel_indices(sw)
        channels = [
            {
                "index": idx,
                "label": str(sw.get(f"SWITCH_{idx}_LABEL", "") or ""),
            }
            for idx in channel_indices
        ]

        # ---- render Jinja template to an HTML snippet ----
        templates = request.app.state.templates
        template = templates.get_template("modals/switch_settings.html")
        modal_html = template.render(
            switch_id=switch_id,
            settings=settings_dict,
            channel_indices=channel_indices,
            channels=channels,
        )

        # ---- embed=1 → just the modal markup (used by dashboard JS) ----
        if embed:
            return HTMLResponse(modal_html)

        # ---- full-page fallback (keeps existing behavior & JS wiring) ----
        page: list[str] = []
        page.append("<html><head><title>Edit Switch</title>")
        # ensure app.css is loaded so modal styles look correct
        page.append("<link rel='stylesheet' href='/ui_static/app.css'>")
        page.append("</head><body>")
        page.append("<div id='modalHost'></div>")
        page.append(f"<script>var __MODAL_HTML__ = {json.dumps(modal_html)};</script>")
        page.append("<script>")
        page.append("  (function(){")
        page.append("    if (window.showSwitchSettingsModal) {")
        page.append("      window.showSwitchSettingsModal(__MODAL_HTML__);")
        page.append("    } else {")
        page.append("      var host = document.getElementById('modalHost');")
        page.append("      if (host) {")
        page.append("        host.innerHTML = __MODAL_HTML__;")
        page.append("        if (window.activateInlineScripts) window.activateInlineScripts(host);")
        page.append("        if (window.openSwitchSettingsModal) window.openSwitchSettingsModal();")
        page.append("        if (window.switchModalWire) window.switchModalWire();")
        page.append("      }")
        page.append("    }")
        page.append("  })();")
        page.append("</script>")
        page.append("</body></html>")

        return HTMLResponse(content="\n".join(page))

    @router.post("/submit-switch-settings")
    async def submit_switch_settings(request: Request):
        from saiSwitchSettingsManager import SwitchSettingsManager
        from saiAutomationManager import AutomationManager

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
            return HTMLResponse("<h3>Missing switch_id</h3><a href='/'>Return</a>", status_code=400)

        old_id = normalize_switch_id(switch_id_in_form)
        manager = SwitchSettingsManager("switch_settings")
        SystemSettingsMgr = globals().get("SystemSettingsManager", None)
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        try:
            app_settings = saiSettings(apply_live=False)
            system_root = getattr(app_settings, "system_dir", None) or getattr(app_settings, "settings_root", None)
        except Exception:
            system_root = "system_settings"
        sys_host_index = _build_system_hostname_index(system_root)

        device_value   = (form.get("device", "") or "").strip()
        new_id_field   = (form.get("switch_id_field", old_id) or "").strip()
        location_value = (form.get("location", "") or "").strip()
        broker_value   = (form.get("broker", "") or "").strip()  # optional/legacy

        new_id = normalize_switch_id(new_id_field)

        existing_doc = manager.load(old_id) or OrderedDict()
        if not isinstance(existing_doc, OrderedDict):
            existing_doc = OrderedDict(existing_doc)
        existing_doc.setdefault("Switch", OrderedDict())

        # --- dynamically collect channel indices from the form
        idxs = set()
        pat = re.compile(r"^SWITCH_(\d+)_(?:LABEL|Trigger)$")
        for key in form.keys():
            m = pat.match(key)
            if m:
                idxs.add(int(m.group(1)))
        channel_indices = sorted(idxs) or [1]

        # Optional: store CHANNELS
        sw_block = OrderedDict({
            "DEVICE":          device_value or existing_doc["Switch"].get("DEVICE", ""),
            "SWITCH_DEVICE_ID": new_id or existing_doc["Switch"].get("SWITCH_DEVICE_ID", old_id),
            "SWITCH_LOCATION": location_value or existing_doc["Switch"].get("SWITCH_LOCATION", "Unknown"),
            "CHANNELS":        len(channel_indices),
        })
        if "BROKER" in existing_doc.get("Switch", {}) or broker_value:
            sw_block["BROKER"] = broker_value or existing_doc["Switch"].get("BROKER", "")

        # Merge per-channel updates
        for i in channel_indices:
            label_key   = f"SWITCH_{i}_LABEL"
            trigger_key = f"SWITCH_{i}_Trigger"
            if label_key in form:
                sw_block[label_key] = (form.get(label_key, "") or "").strip()
            if trigger_key in form:
                sw_block[trigger_key] = (form.get(trigger_key, "") or "").strip()

        merged_doc = deep_merge_ordered(OrderedDict(existing_doc), OrderedDict({"Switch": sw_block}))

        # ---------- Persist FULL merged doc (NEW BLOCK) ----------
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

        # ---- handle directory rename if SWITCH_DEVICE_ID changed ----
        base_dir = Path(getattr(manager, "base_dir", "switch_settings"))
        old_dir = base_dir / old_id
        new_dir = base_dir / new_id
        if new_id != old_id:
            try:
                if old_dir.exists():
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    if new_dir.exists():
                        for item in old_dir.iterdir():
                            dest = new_dir / item.name
                            if dest.exists():
                                if dest.is_file():
                                    dest.unlink()
                                else:
                                    shutil.rmtree(dest)
                            shutil.move(str(item), str(dest))
                        shutil.rmtree(old_dir)
                    else:
                        shutil.move(str(old_dir), str(new_dir))
                printDM(f"[{MODULE}] Renamed switch settings directory: {old_id} → {new_id}", location=MODULE)
            except Exception as e:
                printDM(f"[{MODULE}] Failed to rename {old_id}→{new_id}: {e}", location=MODULE)

        switch_type = str((merged_doc.get("Switch", {}) or {}).get("TYPE", "") or "").strip().lower()
        if switch_type in ("picow", "pico2w", "nodus"):
            remote_updates = [("Switch", key, value) for key, value in sw_block.items()]
            await push_nodus_settings_batch(
                device_id=old_id,
                device_type="switch",
                setting_file_key="switch",
                updates=remote_updates,
                sensor_file_name=None,
                system_mgr=system_mgr,
                system_root=system_root,
                sys_host_index=sys_host_index,
            )

        return RedirectResponse(url="/", status_code=303)

    # Advanced Automation helpers
    # switch id helpers
    # --- switch updates WS (lightweight) ---
    _SWITCH_SOCKETS: Set[WebSocket] = set()

    async def _switch_broadcast(payload: Dict[str, Any]) -> None:
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
        try:
            # keep alive; client never needs to send data
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _SWITCH_SOCKETS.discard(ws)

    # expose for other modules without import cycles
    app.state.switch_broadcast = _switch_broadcast

    @router.get("/switch-chooser", response_class=HTMLResponse)
    async def switch_chooser(request: Request):
        from saiSwitchSettingsManager import SwitchSettingsManager
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
        from saiSwitchSettingsManager import SwitchSettingsManager
        try:
            mgr = SwitchSettingsManager("switch_settings")
            dat = mgr.load(switch_id)
            if not dat:
                return JSONResponse({"error": f"switch_id '{switch_id}' not found"}, status_code=404)

            sw = (dat.get("Switch") or {})

            # 1) Collect labels from SWITCH_<n>_LABEL in the new schema.
            labels: dict[int, str] = {}
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

            # 2) Determine channel count from CHANNELS (if present) or from the max SWITCH_<n> index
            try:
                raw_channels = next((sw.get(k) for k in ("CHANNELS", "channels", "Channels") if k in sw), 0)
                channels = int(raw_channels) if str(raw_channels).strip() else 0
            except Exception:
                channels = 0

            if not channels:
                channels = max(labels.keys(), default=1)

            return {"switch_id": switch_id, "channels": channels, "labels": labels}
        except Exception as exc:
            printDM(f"/switch-info error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/advanced/automations", response_class=JSONResponse)
    async def api_list_advanced_automations(switch_id: str = Query(...)):
        from saiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("switch_settings")
            data = mgr.load(switch_id) or {}

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
                    enabled = bool(payload.get("enabled", True))
                    script_json = (
                        str(payload.get("script_json", ""))
                        or str(payload.get("script", ""))
                        or str(payload.get("json", ""))
                        or ""
                    )
                else:
                    enabled = True
                    script_json = str(payload or "")
                items.append({"rule_id": rule_id, "enabled": enabled, "script_json": script_json})

            return {"switch_id": switch_id, "items": items}
        except Exception as exc:
            printDM(f"/advanced/automations error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.post("/advanced/automations/enable", response_class=JSONResponse)
    async def api_enable_advanced_automation(
        request: Request,
        switch_id: str = Form(...),
        rule_id: str = Form(...),
        enabled: str = Form("true"),  # accept str, coerce below
    ):
        from saiAutomationManager import AutomationManager
        try:
            truthy = str(enabled).strip().lower() in {"1", "true", "on", "yes"}
            mgr = AutomationManager("switch_settings")
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
                        label = switch_key.split("::", 1)[1] if "::" in switch_key else switch_key
                        sid = switch_key.split("::", 1)[0] if "::" in switch_key else switch_id
                        label_lower = (label or "").strip().lower()
                        # Resolve channel index for SWITCH_n_OVERRIDE_SCRIPT
                        channel_index = None
                        try:
                            ordered = switch_mgr.get_switch_channel_names(sid)
                            channel_index = next(
                                (i + 1 for i, nm in enumerate(ordered) if (nm or "").strip().lower() == label_lower),
                                None,
                            )
                        except Exception:
                            channel_index = None
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
                        if sid and not channel_index:
                            try:
                                channel_id = None
                                for row in (data_logger.get_switch_identities() or []):
                                    if str(row.get("switch_id", "")).strip().lower() != str(sid).strip().lower():
                                        continue
                                    if str(row.get("label", "")).strip().lower() != label_lower:
                                        continue
                                    sk = str(row.get("switch_key", "")).strip()
                                    if "::" in sk:
                                        channel_id = sk.split("::", 1)[0].strip()
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

                if hasattr(request.app.state, "switch_broadcast"):
                    for switch_key in switch_keys:
                        state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                        label = switch_key.split("::", 1)[1] if "::" in switch_key else switch_key
                        sid = switch_key.split("::", 1)[0] if "::" in switch_key else switch_id
                        await request.app.state.switch_broadcast({
                            "type": "automation_toggle",
                            "switch_id": sid,
                            "label": label,
                            "enabled": bool(state.get("enabled_any", False)),
                        })
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
        from saiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("switch_settings")
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
            if ok and hasattr(request.app.state, "switch_broadcast"):
                for switch_key in switch_keys:
                    state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                    label = switch_key.split("::", 1)[1] if "::" in switch_key else switch_key
                    sid = switch_key.split("::", 1)[0] if "::" in switch_key else switch_id
                    await request.app.state.switch_broadcast({
                        "type": "automation_toggle",
                        "switch_id": sid,
                        "label": label,
                        "enabled": bool(state.get("enabled_any", False)),
                    })
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/delete error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/switch-advanced", response_class=JSONResponse)
    async def get_advanced_script(switch_id: str = Query(...), channel: int = Query(1)):
        """
        Returns the current Advanced script JSON (normalized) for SWITCH_<channel>_Advanced,
        or {} if not present.
        """
        from saiAutomationManager import AutomationManager

        def _coerce_int(x, default=1):
            try: return int(str(x).strip())
            except Exception: return default

        ch = _coerce_int(channel, 1)
        mgr = AutomationManager("switch_settings")
        data = mgr.load(switch_id) or {}

        # We use the naming convention SWITCH_<n>_Advanced
        rule_id = f"SWITCH_{ch}_Advanced"
        adv = (data.get("Advanced") or {})
        raw = adv.get(rule_id)

        # Older files might store a string (already compact JSON), or a dict
        import json as _json
        normalized = {}
        try:
            if isinstance(raw, dict):
                normalized = raw
            elif isinstance(raw, str) and raw.strip():
                normalized = _json.loads(raw)
        except Exception:
            normalized = {}

        # Ensure shape {logic, conditions[], actions[]}
        logic = str((normalized.get("logic") or "AND")).upper()
        logic = "OR" if logic == "OR" else "AND"
        conditions = list(normalized.get("conditions") or [])
        actions = list(normalized.get("actions") or [])

        return JSONResponse({"script": {"logic": logic, "conditions": conditions, "actions": actions}})

    @router.get("/ui/modal/advanced-automation", response_class=HTMLResponse)
    async def modal_advanced_automation(request: Request, switch_id: str = Query(...)):
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "modals/advanced_automation.html",
            {"request": request, "switch_id": switch_id},
        )

    @router.post("/submit-advanced-trigger")
    async def submit_advanced_trigger(request: Request):
        """
        Persists an Advanced trigger script to switch_settings/automations/automations.toml
        Accepts form or JSON payloads.

        Expected fields:
          - type            (required)
          - switch_id       (required)
          - channel         (e.g., "1") OR switch_selector (fallback)
          - rule_id         (optional; default: "SWITCH_<channel>_Advanced"; made unique if collides)
          - script_json     (required) JSON string built in the Advanced modal
          - enabled         (optional; default: true)
        """
        from saiSwitchSettingsManager import SwitchSettingsManager
        from saiAutomationManager import AutomationManager

        def norm_switch_id(raw: str) -> str:
            s = (raw or "").strip().replace(" ", "-")
            return "".join(ch for ch in s if ch.isalnum() or ch in "-_").lower() or "unknown-switch"

        def parse_bool(v: str | None, default: bool = True) -> bool:
            if v is None:
                return default
            return str(v).strip().lower() in ("1", "true", "on", "yes")

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

                normalized_conditions.append({
                    "type":   cond_type,  # 'sensor' / 'time' / 'astral' / 'timer' / 'or'
                    "sensor": str(c.get("sensor",  c.get("sensor_id", ""))).strip(),
                    "metric": str(c.get("metric",  "")).strip(),
                    "op":     str(c.get("op",      ">")).strip(),
                    "value":  _num(c.get("value"), float, None),
                    "hyst":   _num(c.get("hyst"),  float, None),
                    "start":  str(c.get("start",   "")).strip(),
                    "end":    str(c.get("end",     "")).strip(),
                    "astral_event": str(c.get("astral_event", c.get("event", "sunrise"))).strip().lower(),
                    "offset_min": _num(c.get("offset_min", c.get("offset_minutes")), int, 0),
                    # new optional fields
                    "days":        days_norm or None,
                    "duration_min": _num(c.get("duration_min"), int, None),
                    "freq_hours":   _num(c.get("freq_hours"),   int, None),
                })

            # ACTIONS: accept UI shape (switch_label/set) OR legacy (switch/state/delay)
            raw_actions = parsed.get("actions", [])
            if isinstance(raw_actions, dict):  # legacy single-action shape
                raw_actions = [raw_actions]

            normalized_actions = []
            for a in (raw_actions or []):
                raw_set = a.get("set", a.get("state", "off"))
                set_on = str(raw_set).strip().lower() in {"on", "true", "1"}
                switch_key = str(a.get("switch_key", a.get("switch_label", ""))).strip()
                # (A) If UI gave a bare label (e.g., "Fan"), prefix with switch_id::
                if switch_key and "::" not in switch_key:
                    switch_key = f"{switch_id}::{switch_key}"

                normalized_actions.append({
                    "switch_key": switch_key,
                    "switch":     _num(a.get("switch"), int, None),  # optional numeric channel (legacy)
                    "set":        set_on,
                    "delay_s":    _num(a.get("delay_s", a.get("delay")), int, 0) or 0,
                })

            try:
                # Build channel->label map from settings
                ch_to_label: dict[int, str] = {}
                for k, v in (switch_map or {}).items():
                    m = re.fullmatch(r"SWITCH_(\d+)", str(k))
                    if m and str(v).strip():
                        ch_to_label[int(m.group(1))] = str(v).strip()

                # Rewrite any action switch_key that references CHn → <Label>
                for a in normalized_actions:
                    key = a.get("switch_key") or ""
                    # (B1) Handle ...::CHn
                    m = re.search(r"::CH(\d+)$", key, flags=re.IGNORECASE)
                    if m:
                        idx = int(m.group(1))
                        label = ch_to_label.get(idx)
                        if label:
                            a["switch_key"] = f"{switch_id}::{label}"
                        continue
                    # (B2) Handle bare CHn (no ::) just in case
                    m2 = re.fullmatch(r"CH(\d+)", key, flags=re.IGNORECASE)
                    if m2:
                        idx = int(m2.group(1))
                        label = ch_to_label.get(idx)
                        if label:
                            a["switch_key"] = f"{switch_id}::{label}"
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
        if not rule_id:
            rule_id = f"SWITCH_{channel}_Advanced"

        # ensure uniqueness if the id exists already
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
            trig_mgr = AutomationManager("switch_settings")
            existing = trig_mgr.load(switch_id) or {}
            existing_ids = set((existing or {}).get("Advanced", {}).keys())
            final_rule_id = _unique_rule_id(existing_ids, rule_id)
            trig_mgr.upsert_advanced_rule(
                hostname=switch_id,
                rule_id=final_rule_id,
                enabled=enabled,
                script=compact_script,
            )

            printDM(f"[{MODULE}] Saved Advanced trigger {rule_id} -> {final_rule_id} for {switch_id}", location=MODULE)
            # Invalidate rules cache for the matching switch controller so it reloads immediately.
            try:
                sc = globals().get("switch_controllers")
                if isinstance(sc, dict):
                    for ctrl in sc.values():
                        if getattr(ctrl, "switch_id", None) == switch_id:
                            if hasattr(ctrl, "_rules_cache") and isinstance(ctrl._rules_cache, dict):
                                ctrl._rules_cache["mtime"] = None
                elif sc and getattr(sc, "switch_id", None) == switch_id:
                    if hasattr(sc, "_rules_cache") and isinstance(sc._rules_cache, dict):
                        sc._rules_cache["mtime"] = None
            except Exception:
                pass

            # Ensure a monitor exists for this switch_id even when it was discovered
            # after startup and no supervised controller loop was created.
            try:
                created_ctrl = None
                sc = globals().get("switch_controllers")
                if not isinstance(sc, dict):
                    sc = {}
                    globals()["switch_controllers"] = sc

                found_ctrl = None
                for ctrl in sc.values():
                    if str(getattr(ctrl, "switch_id", "") or "").strip().lower() == str(switch_id).strip().lower():
                        found_ctrl = ctrl
                        break

                if not found_ctrl:
                    sw_mgr = SwitchSettingsManager("switch_settings")
                    sw_doc = sw_mgr.load(switch_id) or {}
                    if isinstance(sw_doc, dict) and (sw_doc.get("Switch") or {}):
                        from saiSwitch import build_switch_controller
                        sw_loc = str((sw_doc.get("Switch") or {}).get("SWITCH_LOCATION", "") or "").strip().lower()
                        sensor_match = None
                        sm = globals().get("sensor_map") or []
                        candidates = sm.values() if isinstance(sm, dict) else sm
                        for s in (candidates or []):
                            try:
                                if str(getattr(s, "location", "") or "").strip().lower() == sw_loc:
                                    sensor_match = s
                                    break
                            except Exception:
                                continue

                        ctrl = build_switch_controller(
                            switch_settings=sw_doc,
                            supervisor=None,
                            sensor=sensor_match,
                        )
                        if bool(getattr(ctrl, "is_present", False)):
                            sc[str(switch_id)] = ctrl
                            try:
                                request.app.state.switch_controllers = sc
                            except Exception:
                                pass
                            found_ctrl = ctrl
                            created_ctrl = ctrl

                if created_ctrl is not None:
                    task_name = f"{switch_id} Controladora Monitor (dynamic)"
                    existing_task = _dynamic_switch_monitor_tasks.get(str(switch_id))
                    if existing_task is None or existing_task.done():
                        _dynamic_switch_monitor_tasks[str(switch_id)] = asyncio.create_task(
                            created_ctrl.run_controladora_monitor(created_ctrl.sensor),
                            name=task_name,
                        )
                        printDM(f"[{MODULE}] started dynamic switch monitor for {switch_id}", location=MODULE)
            except Exception as _ensure_exc:
                if DEBUG:
                    printDM(f"[{MODULE}] dynamic monitor ensure failed for {switch_id}: {_ensure_exc}", location=MODULE)

            # If enabled, ensure override_script is cleared for targeted channels.
            try:
                if enabled:
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
                        label = switch_key.split("::", 1)[1] if "::" in switch_key else switch_key
                        sid = switch_key.split("::", 1)[0] if "::" in switch_key else switch_id
                        label_lower = (label or "").strip().lower()
                        channel_index = None
                        try:
                            ordered = switch_mgr.get_switch_channel_names(sid)
                            channel_index = next(
                                (i + 1 for i, nm in enumerate(ordered) if (nm or "").strip().lower() == label_lower),
                                None,
                            )
                        except Exception:
                            channel_index = None
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
                        if sid and channel_index:
                            switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", False)
            except Exception:
                pass

            # Broadcast updated automation state so UI button reflects enabled/disabled without refresh.
            try:
                app = request.app
                if hasattr(app.state, "switch_broadcast"):
                    from saiAutomationManager import AutomationManager
                    mgr = AutomationManager("switch_settings")
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

                    for switch_key in switch_keys:
                        state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                        label = switch_key.split("::", 1)[1] if "::" in switch_key else switch_key
                        sid = switch_key.split("::", 1)[0] if "::" in switch_key else switch_id
                        await app.state.switch_broadcast({
                            "type": "automation_toggle",
                            "switch_id": sid,
                            "label": label,
                            "enabled": bool(state.get("enabled_any", False)),
                        })
            except Exception:
                pass
        except Exception as e:
            printDM(f"[{MODULE}] ⚠️ Failed to save Advanced trigger {rule_id} for {switch_id}: {e}", location=MODULE)
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

        states: dict[str, dict] = {}

        def _format_events(switch_key: str, sensor_id: str | None, limit: int = 5) -> list[str]:
            evs = data_logger.get_last_switch_events(switch_key, sensor_id=sensor_id, limit=limit)
            out: list[str] = []
            for state_str, ts in evs:
                label = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                out.append(f"{label} {ts}")
            return out  # oldest → newest

        def _format_events_remote(switch_key: str, limit: int = 5) -> list[str]:
            """
            Remote/Nodus history should reflect broker-confirmed rows.
            Ignore stale optimistic UI/manual rows.
            """
            out: list[str] = []
            try:
                import sqlite3
                db_path = getattr(data_logger, "db_path", "sensorius_data.db")
                with sqlite3.connect(db_path) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT timestamp, state
                        FROM sw_events
                        WHERE switch_key = ? COLLATE NOCASE
                          AND LOWER(COALESCE(source, '')) LIKE 'mqtt%'
                        ORDER BY COALESCE(ts_epoch, 0.0) DESC, timestamp DESC
                        LIMIT ?
                        """,
                        (switch_key, limit),
                    )
                    rows = cur.fetchall()
                for ts, st in rows:
                    is_on = bool(st) if isinstance(st, (int, bool)) else (str(st).lower() in ("1", "true", "on"))
                    out.append(f"{'On' if is_on else 'Off'} {ts}")
                return out
            except Exception:
                return []

        try:
            # --- A) Local Pi switch controllers ---
            if switch_controllers and isinstance(switch_controllers, dict):
                for ctrl in switch_controllers.values():
                    if not getattr(ctrl, "is_present", False):
                        continue
                    if not isinstance(getattr(ctrl, "last_state", None), dict):
                        continue

                    switch_id = getattr(ctrl, "switch_id", None)
                    if not switch_id:
                        continue  # cannot form a canonical key
                    sensor_lineage = f"Switch_{switch_id}"

                    for label, is_on in ctrl.last_state.items():
                        # UI key: what the frontend expects (switch_id::Label)
                        ui_key = f"{switch_id}::{label}"

                        # DB key: may include SWITCH_n_ID via build_switch_key
                        try:
                            db_key = ctrl._switch_key(label)
                        except Exception:
                            db_key = _switch_key(switch_id, label)

                        latest = data_logger.get_latest_switch_state(db_key, sensor_id=sensor_lineage)
                        latest_bool = (latest == "On") if latest is not None else bool(is_on)
                        events = _format_events(db_key, sensor_lineage, limit=5)
                        states[ui_key] = {"state": latest_bool, "time": events}

            # --- B) Remote Pico2 W / Nodus switches ---
            # Prefer DB identities (authoritative mapping), then fall back to MQTT cache.
            cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
            try:
                identity_rows = list(data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows = []

            known_channel_ids_by_sid: dict[str, set[str]] = {}
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                sk = str(row.get("switch_key", "")).strip()
                if not sid or "::" not in sk:
                    continue
                ch = sk.split("::", 1)[0].strip()
                if not ch:
                    continue
                known_channel_ids_by_sid.setdefault(sid, set()).add(ch.lower())

            # Resolve a canonical DB key for a (switch_id, label) pair, using switch_ids table if present.
            def _db_key_for_label(sid: str, label: str) -> str:
                try:
                    if data_logger:
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
                # fallback to existing resolver (may use mqtt_ingest map)
                try:
                    return _switch_key(sid, label)
                except Exception:
                    return f"{sid}::{label}"

            def _cache_state_for(sid: str, label: str, db_key: str) -> bool | None:
                try:
                    ch_map = cache.get(sid, {}) or {}
                    human_state = ch_map.get(label)
                    if human_state is None and "::" in db_key:
                        ch_id = db_key.split("::", 1)[0].strip()
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

            # 1) Seed states from DB identities (authoritative mapping)
            seen_ui_keys: set[str] = set()
            try:
                for row in (identity_rows or []):
                    sid = str(row.get("switch_id", "")).strip()
                    label = str(row.get("label", "")).strip()
                    db_key = str(row.get("switch_key", "")).strip()
                    if not (sid and label and db_key):
                        continue

                    ui_key = f"{sid}::{label}"
                    # For remote switches, prefer live ingest cache (/itaot + MQTT state/events).
                    # DB can contain stale 'ui' rows from prior sessions.
                    cached_bool = _cache_state_for(sid, label, db_key)
                    if cached_bool is not None:
                        latest_bool = cached_bool
                    else:
                        latest = data_logger.get_latest_switch_state(db_key)
                        latest_bool = (latest == "On") if latest is not None else False

                    events = _format_events_remote(db_key, limit=5) or _format_events(db_key, None, limit=5)
                    states[ui_key] = {"state": latest_bool, "time": events}
                    seen_ui_keys.add(ui_key)
                    # Also expose alias key keyed by channel_id for UI payload sync.
                    try:
                        if "::" in db_key:
                            ch_id = db_key.split("::", 1)[0].strip()
                            alias_key = f"{ch_id}::{label}" if ch_id else ""
                            if alias_key and alias_key not in states:
                                states[alias_key] = {"state": latest_bool, "time": events}
                                seen_ui_keys.add(alias_key)
                    except Exception:
                        pass
            except Exception:
                pass

            for remote_switch_id, ch_map in cache.items():
                if not isinstance(ch_map, dict):
                    continue
                sensor_lineage = f"Switch_{remote_switch_id}"
                for channel_label, human_state in ch_map.items():
                    # Avoid phantom UI rows such as "switch-<id>::S1-<id>" when
                    # canonical identities already map the channel ID to a label.
                    if str(channel_label or "").strip().lower() in known_channel_ids_by_sid.get(str(remote_switch_id), set()):
                        continue
                    # UI key is still label-based
                    ui_key = f"{remote_switch_id}::{channel_label}"
                    if ui_key in seen_ui_keys:
                        continue

                    # DB key: prefer switch_ids mapping for label → channel_id
                    db_key = _db_key_for_label(remote_switch_id, channel_label)

                    cached_bool = _cache_state_for(remote_switch_id, channel_label, db_key)
                    if cached_bool is not None:
                        latest_bool = cached_bool
                    else:
                        latest = data_logger.get_latest_switch_state(db_key)
                        latest_bool = (latest == "On") if latest is not None else (str(human_state).lower() == "on")
                    events = _format_events_remote(db_key, limit=5) or _format_events(db_key, None, limit=5)
                    states[ui_key] = {"state": latest_bool, "time": events}

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
        switch_key: str | None = Query(None), # new: "switch_id::label"
        switch_id: str | None = Query(None),  # new: switch_id sent separately
        ):
        """
        Toggle a switch identified by either:
          1) switch_key="switch_id::label"  (preferred)
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

        # even if the db does not have a state value, set the state
        def _desired_toggle_from_db(data_logger, switch_id: str | None, label: str, ctrl) -> bool:
            """
            Returns desired new_state:
            - If DB has a latest state (using the controller's canonical DB key): flip it.
            - Else: flip the *actual controller* state so the first click always changes something.
            """
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

            if DEBUG:
                printDM(f"[toggle_switch] Requested: '{switch_name}', key={switch_key}, switch_id={switch_id}", location=MODULE)

            if not label_raw and not key_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            # Resolve from switch_key if provided
            if key_raw and "::" in key_raw:
                sid_part, label_part = key_raw.split("::", 1)
                switch_id_raw = _norm_switch_id(sid_part) or switch_id_raw
                label_raw     = _norm_label(label_part) or label_raw

            if not label_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            label_q_lower = label_raw.lower()
            try:
                identity_rows = list(data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows = []

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
                    r_key = str(row.get("switch_key", "")).strip()
                    r_channel = ""
                    if "::" in r_key:
                        r_channel = r_key.split("::", 1)[0].strip().lower()

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
                            r_channel = str(row.get("channel_id", "") or "").strip()
                            if not r_channel and "::" in r_key:
                                r_channel = r_key.split("::", 1)[0].strip()
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
                        "message": "Multiple devices have this label. Provide switch_key='switch_id::label' or pass switch_id.",
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
            new_state = _desired_toggle_from_db(data_logger, sid, matched_label, ctrl)

            # Decide path: direct GPIO vs remote/MQTT
            remote = _looks_remote(ctrl)
            ok = False

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
                        ok = bool(mqtt_ingest.set_switch(sid, matched_label, new_state))
                    except Exception as e:
                        printDM(f"[toggle_switch] ingest.set_switch error: {e}", location=MODULE)

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
                    return JSONResponse({"state": effective, "time": ts, "note": note}, status_code=200)

                # Otherwise, this really did fail (hardware/driver issue)
                reason = "mqtt_not_ready" if remote else "failed_to_toggle"
                return JSONResponse({"error": reason}, status_code=503 if reason == "mqtt_not_ready" else 500)

            # Persist SWITCH_n_LAST_STATE
            try:
                if sid:
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
                if callable(rec):
                    rec(matched_label, "on" if new_state else "off", ts)
            except Exception:
                pass

            # Invalidate short-lived switch status cache after a state change.
            _switch_status_cache_payload = None
            _switch_status_cache_until = 0.0

            return {"state": bool(new_state), "time": ts}

        except Exception as e:
            printDM(f"[toggle_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.post("/switch/override")
    async def override_switch(
        request: Request,
        switch_name: str | None = Query(None),
        switch_key: str | None = Query(None),
        switch_id: str | None = Query(None),
    ):
        from saiSwitchSettingsManager import SwitchSettingsManager
        from saiAutomationManager import AutomationManager

        try:
            _require_protected_access(request, require_csrf=True)
            data = await request.json()
            desired_rule_enabled = bool(data.get("enabled", False))  # ← interpret as RULE state

            label_q   = (switch_name or "").strip()
            key_q     = (switch_key or "").strip()
            switch_id_q = (switch_id or "").strip().lower() if switch_id else None

            if DEBUG:
                printDM(f"[override_switch] name='{label_q}', key='{key_q}', switch_id='{switch_id_q}', rule.enabled={desired_rule_enabled}", location=MODULE)

            # Resolve from switch_key if present
            if key_q and "::" in key_q:
                sid_part, lbl_part = key_q.split("::", 1)
                switch_id_q = (sid_part or "").strip().lower() or switch_id_q
                label_q     = (lbl_part or "").strip() or label_q

            if not label_q:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

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
                        for row in (data_logger.get_switch_identities() or []):
                            if str(row.get("switch_id", "")).strip().lower() != sid.lower():
                                continue
                            if str(row.get("label", "")).strip().lower() != label_lower:
                                continue
                            sk = str(row.get("switch_key", "")).strip()
                            if "::" in sk:
                                channel_id = sk.split("::", 1)[0].strip()
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
        return HTMLResponse(
            "<html><body><h3>Confirm Clear</h3>"
            "<p>This will permanently delete all stored sensor data.</p>"
            "<form method='post' action='/clear-data'>"
            "<input type='hidden' name='confirm' value='true'>"
            "<button type='submit'>Yes, clear data</button>"
            "</form>"
            "<a href='/'>Cancel</a>"
            "</body></html>"
        )

    @router.post("/clear-data", response_class=HTMLResponse)
    async def clear_data_post(request: Request, confirm: bool = Form(False)):
        _require_protected_access(request, require_csrf=True)
        if not confirm:
            return HTMLResponse(
                "<html><body><h3>Missing confirmation.</h3><a href='/'>Return</a></body></html>",
                status_code=400,
            )
        data_logger.clear_all_readings()
        return HTMLResponse("<html><body><h3>All sensor data cleared.</h3><a href='/'>Return to Dashboard</a></body></html>")

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


    from saiStats import create_stats_router
    app.include_router(create_stats_router(settings, gc_mgr))
    app.include_router(router)
    return router
