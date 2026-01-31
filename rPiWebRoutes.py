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
import base64, zlib
import re
import tomllib
from collections import OrderedDict
import shutil, httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from rPiUtils import printDM, debug_enabled, get_timestamp, normalize_sensor_id
from rPiSettings import rPiSettings
from rPiDataLogger import rPiDataLogger
try:
    from rPiDataLogger import build_switch_key as _build_switch_key
except Exception:
    _build_switch_key = None
from rPiStats import rPiStats
from rPiHtml import render_dashboard, get_gauge_config
from rPiFastStats import FastStats
from rPiSensorSettingsManager import SensorSettingsManager
from rPiSwitchSettingsManager import SwitchSettingsManager
from rPiAddDevice import HUB_SETTINGS_PATH, _SENSOR_BASE_DIR, _SWITCH_BASE_DIR, _SYS_BASE_DIR

MODULE = "rPiWebRoutes"
DEBUG = debug_enabled(MODULE)
data_logger = rPiDataLogger()
statter = rPiStats()

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

async def register_routes(app, settings, net_mgr, gc_mgr, mqtt_ingest):
    router = APIRouter()
    default_sensor_id = settings.get_all_sensor_ids()[0] if settings.get_all_sensor_ids() else ""
    # On startup
    fastStats = FastStats(data_logger, statter, hz=1.0)
    asyncio.create_task(fastStats.start())

    def _resolve_channel_id_from_label(switch_id: str, label: str) -> str | None:
        try:
            if not mqtt_ingest:
                return None
            norm_label = (label or "").strip().lower()
            return (mqtt_ingest.nodus_label_to_channel or {}).get((str(switch_id), norm_label))
        except Exception:
            return None

    @router.get("/", response_class=HTMLResponse)
    async def current_data_page(request: Request, sensor_id: str = Query(None), json_only: bool = Query(False)):
        try:
            seen = set()
            available = []

            def _strip_local_suffix(name: str) -> str:
                s = (name or "").strip()
                return s[:-6] if s.endswith(".local") else s

            def _is_switch_id(name: str) -> bool:
                n = (name or "").strip().lower()
                return n.startswith("switch_") or n.startswith("switch-")

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

            # --- make sensor_map accessible anywhere in this handler
            from collections.abc import Iterable
            def _get_sensor_map():
                sm = getattr(app.state, "sensor_map", None)
                if sm is None:
                    import rPiWebRoutes as routes
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
                    from rPiSensorSettingsManager import SensorSettingsManager
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

            sensors_from_logger = data_logger.get_available_sensors()
            mqtt_discovered = mqtt_ingest.get_known_devices()

            local_ids = _get_local_sensor_ids()
            available = _normalize_available(local_ids, list(mqtt_discovered))

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
                printDM(f"local_ids: {local_ids}", location=f"{MODULE}:cdp")
                printDM(f"available sensors: {available}", location=f"{MODULE}:cdp")

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

        from rPiSettings import rPiSettings
        fresh_settings = rPiSettings(apply_live=False)
        gaugeSize = fresh_settings.get_setting("Display", "gauge_size") or "Small"
        gauge_config = get_gauge_config()
        displayStyle = fresh_settings.get_setting("Display", "display_style") or "Gauge"

        from rPiSensorSettingsManager import SensorSettingsManager
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
                metrics = list(gauge_config.keys())
            expected_gauge_map[sid] = metrics

        # Use the location map we already built
        sensor_locations = { sid: sensor_locations_map.get(sid, "Unknown") for sid in all_values }

        if DEBUG:
            printDM(f"sensor_locations: {sensor_locations}", location=f"{MODULE}:cdp")

        try:
            from rPiDataLogger import build_switch_key as _build_switch_key
        except Exception:
            _build_switch_key = None

        def _switch_key(switch_id: str, label: str) -> str:
            sid = (switch_id or "").strip()
            lab = (label or "").strip()
            ch_id = _resolve_channel_id_from_label(sid, lab)
            if _build_switch_key is not None:
                try:
                    if ch_id:
                        return _build_switch_key(sid, lab, ch_id)
                    # new-style signature
                    return _build_switch_key(switch_id=sid, label=lab)
                except TypeError:
                    # old-style (sid, label)
                    return _build_switch_key(sid, lab)
            # fallback: current behavior
            return f"{sid}::{lab}"

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
                        ORDER BY timestamp DESC
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

        switch_status = _collect_switch_status()

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
        from rPiMQTTIngest import get_current_ingest as _get_ing
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
                if isinstance(st, str) and st.strip().lower() in {"online", "offline", "pending"}:
                    return st.strip().lower()
            except Exception:
                pass

            # 2) Ask the ingest instance we were given
            try:
                if ing is not None and hasattr(ing, "get_measure_status") and callable(ing.get_measure_status):
                    st = ing.get_measure_status(sid)  # should accept either sid or host
                    if isinstance(st, str) and st.strip().lower() in {"online", "offline", "pending"}:
                        return st.strip().lower()
            except Exception:
                pass

            # 3) Fallback: normalize host and consult ingest.device_status
            try:
                dev_map = getattr(ing, "device_status", {}) or {}
                host = _host_base_from_sid(sid)
                for key in (host, f"{host}.local"):
                    st = dev_map.get(key or "")
                    if isinstance(st, str) and st.strip().lower() in {"online", "offline", "pending"}:
                        return st.strip().lower()
            except Exception:
                pass

            return "pending"
         
        if json_only:
            timestamps = {
                sid: data_logger.get_latest_timestamp(sid) or ""
                for sid in all_values
            }
            statuses = { sid: _resolve_meas_status_for_sid(sid) for sid in available }
            
            return JSONResponse({
                "available": available,
                "values": all_values,
                "stats": all_stats,
                "timestamps": timestamps, 
                "sensor_id": sensor_id,
                "timestamp": get_timestamp(),
                "locations": sensor_locations,
                "expected_gauge_map": expected_gauge_map,
                "switch_status": switch_status, 
                "statuses": statuses, 
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
        from rPiUtils import printDM

        MODULE = "graph-data"
        db_path = getattr(data_logger, "db_path", "sensorius_data.db")

        # --- Local zone from settings (seconds offset) ---
        def _local_tz():
            try:
                from rPiSettings import rPiSettings
                s = rPiSettings()
                off_s = int(s.get_setting("Time", "TZ_OFFSET", 0) or 0)
            except Exception:
                off_s = 0
            return timezone(timedelta(seconds=off_s))

        # --- Compute window in *local* offset and return ISO strings with offset ---
        def _compute_window(range_str: str, start_iso: str | None, end_iso: str | None):
            tz = _local_tz()
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
            else:
                now_local = datetime.now(tz)
                # map ranges → seconds
                ranges = {
                    "1h": 3600, "3h": 3*3600, "6h": 6*3600, "12h": 12*3600, "24h": 24*3600,
                    "3d": 3*86400, "7d": 7*86400, "30d": 30*86400
                }
                span = int(ranges.get((range_str or "24h").lower(), 24*3600))
                end_dt = now_local
                start_dt = now_local - timedelta(seconds=span)

            # Return offset ISO (e.g., '...-06:00'), seconds span
            since_iso = start_dt.replace(microsecond=0).isoformat()
            until_iso = end_dt.replace(microsecond=0).isoformat()
            span_seconds = int((end_dt - start_dt).total_seconds())
            return since_iso, until_iso, span_seconds

        # ----- time range (ALL in local offset, matching DB storage) -----
        try:
            since_iso, until_iso, span_seconds = _compute_window(range, start, end)
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
        def fetch_xy(cur, sid: str, metric_name: str):
            try:
                #printDM(f"[{MODULE}] Query {sid}.{metric_name} {since_iso} → {until_iso} (DB local-offset ISO)", location=MODULE)
                cur.execute(
                    """
                    SELECT timestamp, value
                    FROM readings
                    WHERE sensor_id = ? COLLATE NOCASE
                    AND metric    = ? COLLATE NOCASE
                    AND timestamp >= ?
                    AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (sid, metric_name, since_iso, until_iso)
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
        display_names: dict[str, str] = {}
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            for sid, metric_name in pairs:
                ts, vs = fetch_xy(cur, sid, metric_name)
                key = f"{sid}::{metric_name}"
                if ts and vs:
                    series[key] = {"ts": ts, "vals": vs}
                    display_names[key] = key

        if not series:
            first = pairs[0]
            raise HTTPException(status_code=404, detail=f"No data found for {first[0]}.{first[1]} in selected range")

        response = {
            "series": series,
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
                              AND e.timestamp >= ?
                              AND e.timestamp <= ?
                            ORDER BY e.timestamp ASC
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
                              AND timestamp >= ?
                              AND timestamp <= ?
                            ORDER BY timestamp ASC
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

    @router.get("/edit-system", response_class=HTMLResponse)
    async def edit_pi_settings_page(request: Request):
        from rPiSettings import rPiSettings
        from rPiUtils import html_escape
        from rPiHtml import APP_NAME_LONG, APP_VERSION

        settings = rPiSettings(apply_live=False)
        templates = request.app.state.templates

        # Prepare values for the template (let Jinja handle escaping)
        hostname   = settings.get_setting("Network", "HOSTNAME", "") or ""
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
        gauge_size = settings.get_setting("Display", "gauge_size", "") or "Small"
        display_style = settings.get_setting("Display", "display_style", "") or "Gauge"
        ha_enabled = bool(settings.get_setting("HomeAssistant", "ENABLED", False))
        ha_username = settings.get_setting("HomeAssistant", "HA_USERNAME", "") or ""
        ha_password = settings.get_setting("HomeAssistant", "HA_PASSWORD", "") or ""
        ha_broker = settings.get_setting("HomeAssistant", "BROKER", "") or ""
        ha_port = settings.get_setting("HomeAssistant", "PORT", 1883) or 1883

        clients = settings.get_all_clients() or []
        client_list = "\n".join(clients)

        templates = request.app.state.templates
        system_modal_html = templates.get_template("modals/system_settings.html").render(
            app_name_long=APP_NAME_LONG,
            app_version=APP_VERSION,
            hostname=hostname,
            broker=broker,
            tz=tz,
            tz_offset=tz_offset,
            tz_name=tz_name,
            gauge_size=gauge_size,
            display_style=display_style,
            client_list=client_list,
            ha_enabled=ha_enabled,
            ha_username=ha_username,
            ha_password=ha_password,
            ha_broker=ha_broker,
            ha_port=ha_port,
        )

        html_parts: list[str] = []
        html_parts.append("<!DOCTYPE html>")
        html_parts.append("<html><head><title>System Settings</title>")
        # make sure app.css is included so shared styles (including .modal, .button, etc.) are available
        html_parts.append("<link rel='stylesheet' href='/ui_static/css/app.css'>")
        html_parts.append("</head><body>")

        # 1) System settings modal via template
        html_parts.append(system_modal_html)

        # 2) Onboarding progress modal + JS (so Add Device works)
        system_onboard_progress_modal_html = templates.get_template("modals/system_onboard_progress.html").render()
        html_parts.append(system_onboard_progress_modal_html)


        # 3) Device Locations modal + JS via template
        system_device_locations_modal_html = templates.get_template("modals/system_device_locations.html").render()
        html_parts.append(system_device_locations_modal_html)

        # 4) Remove Device modal + JS via template
        system_remove_modal_html = templates.get_template("modals/system_remove_device.html").render()
        html_parts.append(system_remove_modal_html)

        # 5) Home Assistant integration modal + JS via template
        system_ha_modal_html = templates.get_template("modals/system_ha_integration.html").render(
            ha_enabled=ha_enabled,
            ha_username=ha_username,
            ha_password=ha_password,
            ha_broker=ha_broker,
            ha_port=ha_port,
        )
        html_parts.append(system_ha_modal_html)

        html_parts.append("</body></html>")

        return HTMLResponse(content="\n".join(html_parts))

    # /itaot helpers
    CONTENT_ENCODING = "base64+zlib"
    ITAOT_VERSION = "0.2"

    def _compress_b64_bytes(raw: bytes) -> str:
        """zlib-compress + base64-encode -> str."""
        return base64.b64encode(zlib.compress(raw)).decode("ascii")

    def _compressed_b64_or_none(path: Path) -> str | None:
        try:
            raw = path.read_bytes()
            return _compress_b64_bytes(raw)
        except Exception as ex:
            if DEBUG:
                printDM(f"itaot: could not include file {path}: {ex}", location="rPiWebRoutes:itaot")
            return None

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
    async def identify_topic():
        """
        Identify-this-Pi-and-its-topics.

        Multi-sensor response (preferred):
        {
          "version": "0.3",
          "origin": "pi",
          "hostname": "<pi-hostname>",
          "content_encoding": "base64+zlib",
          "sensors": [
            {"SENSOR_ID": "...", "DEVICE": "...", "SERIAL_NUM": "...", "LOCATION": "...",
             "mqtt_sensor_topic": "...", "display_metrics": [...]},
            ...
          ],
          "switches": [
            {"SWITCH_ID": "...", "channels": ["Fan","Light"],
             "mqtt_switch_topics": { "GP28": "switch/<id>-GP28/event", ... }},
            ...
          ],
          "files": [ ... ]
        }

        Single-sensor response (Pico schema on the device itself) is a single object.
        """
        try:
            # System identity
            hostname = settings.get_setting("Network", "HOSTNAME") or "unknown-pi"

            # Sensor descriptors
            sensor_ids = settings.get_all_sensor_ids() or []
            if DEBUG:
                printDM(f"/itaot sensor_ids → {sensor_ids}", location="rPiWebRoutes:itaot")

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
                        printDM(f"/itaot: failed loading settings for {sensor_id}: {ex}", location="rPiWebRoutes:itaot")

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
                        printDM(f"/itaot: {sensor_id} not active; advertising from TOML", location="rPiWebRoutes:itaot")
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
                            # Derive channel names from keys like SWITCH_1="Fan", ignoring *_PIN/_LAST_STATE/_OVERRIDE_SCRIPT
                            channel_names = []
                            for k, v in (sw_blk or {}).items():
                                if not isinstance(v, str):
                                    continue
                                if k.startswith("SWITCH_") and not any(k.endswith(suf) for suf in ("_PIN", "_LAST_STATE", "_OVERRIDE_SCRIPT")) and k not in ("SWITCH_ID","SWITCH_LOCATION","SWITCH_EN_PIN","SWITCH_ACTIVE","DEVICE","SERIAL_NUM"):
                                    channel_names.append(v)
                    except Exception as ex:
                        channel_names = []
                        switch_location = "Unknown"
                        if DEBUG:
                            printDM(f"/itaot: switch '{switch_id}' load failed: {ex}", location="rPiWebRoutes:itaot")

                    switches_payload.append({
                        "SWITCH_ID": switch_id,
                        "SWITCH_LOCATION": switch_location,
                        "channels": channel_names,
                        "mqtt_switch_topic": _topic_for_switch(switch_id),
                    })

            except Exception as ex:
                if DEBUG:
                    printDM(f"/itaot: switch settings probe failed: {ex}", location="rPiWebRoutes:itaot")

            # Compose files[] with compressed TOMLs
            files_payload: list[dict] = []

            # Prefer the live system settings path via rPiSettings
            try:
                active_settings_path = settings.get_active_settings_path()
            except Exception:
                active_settings_path = None

            if active_settings_path:
                settings_blob = _compressed_b64_or_none(Path(active_settings_path))
            else:
                settings_blob = _compressed_b64_or_none(Path(r"settings.toml"))

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
                sensor_blob = _compressed_b64_or_none(sensor_toml_path)
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
                    switch_id = switch["SWITCH_ID"]
                    switch_toml_path = Path(r"switch_settings") / switch_id / "switch.toml"
                    switch_blob = _compressed_b64_or_none(switch_toml_path)
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
                    printDM(f"/itaot: switch files compose failed: {ex}", location="rPiWebRoutes:itaot")

            # Build multi-sensor payload
            multi_payload = {
                "version": ITAOT_VERSION,
                "origin": "pi",
                "hostname": hostname,
                "content_encoding": CONTENT_ENCODING,
                "sensors": sensors_payload,
                "switches": switches_payload,
                "files": files_payload,
            }

            # Single vs multi-sensor response (preserve Pico back-compat)
            if len(sensors_payload) == 1 and not switches_payload:
                one = dict(sensors_payload[0])
                one["content_encoding"] = CONTENT_ENCODING
                one["files"] = files_payload
                return one

            return multi_payload

        except Exception as e:
            return PlainTextResponse(f"Internal error in /itaot: {e}", status_code=500)

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

    # lightweight connection/health check
    @router.get("/hayd", response_class=JSONResponse)
    async def how_are_you_doing():
        _status = "ok"
        return {"STATUS": _status}

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
            printDM(f"[ws_onboard_progress] {e}", location="rPiWebRoutes")
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
        import rPiAddDevice
        form = await request.form()
        # Pull what your System Setup dialog already posts.
        # These names are examples; keep them aligned with your current form fields:
        sensor_type = form.get("sensor_type", "")
        location    = form.get("location", "Unknown")
        local_ssid  = form.get("local_ssid", "Unknown")
        # You may already assemble a richer onboarding payload; pass it through:
        payload_json = form.get("payload_json")  # optional richer JSON blob

        job_id = uuid4().hex
        printDM(f"[onboard-start] job_id={job_id} sensor_type={sensor_type} location={location}", location="rPiWebRoutes")

        async def run_flow():
            # Step 1: AP connect
            label1 = "Sensor Setup connection established"
            ok1 = False
            try:
                ok1 = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: rPiAddDevice.connect_to_sensor_ap(
                        rPiAddDevice.PICOW_AP_SSID,
                        rPiAddDevice.PICOW_AP_PASSWORD,
                        attempts=3
                    )
                )
            except Exception as e:
                printDM(f"[onboard] connect_to_sensor_ap failed: {e}", location="rPiWebRoutes")
            await _emit(job_id, 1, bool(ok1), label1)

            sensor_id_for_step = "Unknown"

            # Step 2: configure + reboot (no reconnect here)
            label2 = "configured and rebooting"
            ok2 = False
            if ok1:
                try:
                    ok2, maybe_sensor_id = await asyncio.get_event_loop().run_in_executor(
                        None, rPiAddDevice.perform_picow_configure_and_reboot
                    )
                    if maybe_sensor_id:
                        sensor_id_for_step = maybe_sensor_id
                except Exception as e:
                    printDM(f"[onboard] perform_picow_configure_and_reboot failed: {e}", location="rPiWebRoutes")
            await _emit(job_id, 2, bool(ok2), f"{sensor_id_for_step} {label2}")

            # Step 3: reconnect Pi to its local SSID (and update CLIENTS if step 2 succeeded)
            ok3 = False
            conn_ssid = "Unknown"
            label3 = f"Reconnecting to {conn_ssid}"
            try:
                ok3, conn_ssid = await asyncio.get_event_loop().run_in_executor(None, rPiAddDevice.reconnect_to_pi)
                label3 = f"Reconnecting to {conn_ssid}"
            except Exception as e:
                printDM(f"[onboard] reconnect_to_pi failed: {e}", location="rPiWebRoutes")
            await _emit(job_id, 3, bool(ok3), label3)

            if ok2 and sensor_id_for_step:
                try:
                    rPiAddDevice.update_hub_clients(rPiAddDevice.HUB_SETTINGS_PATH, sensor_id_for_step)
                except Exception as e:
                    printDM(f"[onboard] update_hub_clients failed: {e}", location="rPiWebRoutes")
                # Nudge ingest discovery immediately (no restart required)
                try:
                    # match the form you persist in CLIENTS (you showed ".local")
                    host_for_ingest = f"{sensor_id_for_step}.local"
                    mqtt_ingest.add_client(host_for_ingest)
                    printDM(f"[onboard] nudged discovery for {host_for_ingest}", location="rPiWebRoutes")
                except Exception as e:
                    printDM(f"[onboard] add_client nudge failed: {e}", location="rPiWebRoutes")


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
                app_settings = rPiSettings(apply_live=False)
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
                    from rPiSensorSettingsManager import SensorSettingsManager
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
            candidates.append(f"http://{hostname}.local:8000/set-nodus-setting")
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


    # ---------- user-defined constants ----------
    LOCATIONS_ROUTE_TAG = "device-locations"
    # ----- view and edit device locations -------
    @router.get("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def list_device_locations(request: Request) -> JSONResponse:
        try:
            app_settings = rPiSettings(apply_live=False)
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
                printDM(f"system_dir={system_dir}", location="rPiWebRoutes.list_device_locations")
                printDM(f"sensor_dir={sensor_dir}", location="rPiWebRoutes.list_device_locations")
                printDM(f"switch_dir={switch_dir}", location="rPiWebRoutes.list_device_locations")


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
                        printDM(f"sensor: {sensor_id}, sw_loc: {loc}", location="rPiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"sensor_mgr error: {e}", location="rPiWebRoutes.list_device_locations")

            switch_items = []
            try:
                for switch_id in switch_mgr.list_switches():
                    if (switch_id or "").lower() == "factory":
                        continue
                    doc = switch_mgr.load(switch_id) or {}
                    sw_loc = (doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "") or "Unknown"
                    switch_items.append({"id": switch_id, "type": "switch", "location": sw_loc})
                    if DEBUG:
                        printDM(f"switch: {switch_id}, sw_loc: {sw_loc}", location="rPiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"switch_mgr error: {e}", location="rPiWebRoutes.list_device_locations")

            items = sensor_items + switch_items

            if DEBUG:
                printDM(f"sensors={len(sensor_items)} switches={len(switch_items)} total={len(items)}", location="rPiWebRoutes.DeviceLocations")

            return JSONResponse(items)

        except Exception as e:
            printDM(f"Failed to list device locations: {e}", location="rPiWebRoutes.list_device_locations")
            # Return an empty list (200) rather than 500 so the modal stays open with a visible message
            # If you prefer to signal error, keep 500 — but then ensure the client does NOT auto-close the modal.
            return JSONResponse({"error": "failed"}, status_code=500)
        
    @router.post("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def save_device_locations(request: Request) -> JSONResponse:
        """
        Accepts: [{"id": "...", "type": "sensor"|"switch", "location": "..."}]
        (Also tolerates {"switch_location": "..."} for switches.)
        Saves LOCATION/SWITCH_LOCATION locally and updates live objects in memory.
        NEW: If the device is a remote Nodus (TYPE == "picow" or "pico2w"), also POSTs the update
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
                app_settings = rPiSettings(apply_live=False)
                system_root = getattr(app_settings, "system_dir", None) or getattr(app_settings, "settings_root", None)
            except Exception:
                system_root = "system_settings"
            updated = {"sensor": 0, "switch": 0, "nodus_pushed": 0}

            sys_host_index = _build_system_hostname_index(system_root)
            if DEBUG:
                printDM(f"[save_device_locations] system hostname index: {sys_host_index}", location=MODULE)
                
            # shared handles established at startup by rPiSensorius
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
                    import rPiWebRoutes as routes
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

                        # enqueue remote push if this is a Nodus/Pico2 W
                        dev_kind = (sensor_mgr.get_setting(dev_id, "Sensor.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w"):
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


                        # enqueue remote push if this is a Nodus/Pico2 W
                        dev_kind = (switch_mgr.get_setting(dev_id, "Switch.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w"):
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
        s = (name or "").strip()
        if not s:
            return None
        return s[:-6] if s.endswith(".local") else s

    def _collect_ingest_ids() -> list[str]:
        """
        Aggregate IDs from MQTT discovery so the remove list reflects in-memory state.
        """
        try:
            from rPiMQTTIngest import get_current_ingest as _get_ing
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
                suffix = key.removeprefix("SWITCH_")
                if not suffix.isdigit():
                    continue
                label = str(val or "").strip()
                id_key = f"SWITCH_{suffix}_ID"
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
            from rPiSensorSettingsManager import SensorSettingsManager
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
        stats = {"topics_cleared": 0}
        if not mqtt_ingest:
            return stats
        try:
            from rPiHomeAssistantMqtt import slugify, HomeAssistantTopicMap
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

        metrics = _collect_sensor_metrics(device_id, data_logger, mqtt_ingest)
        channels = _collect_switch_channels(device_id, mqtt_ingest)

        topics: list[str] = []
        for metric in metrics:
            object_id = f"{device_id}__{slugify(metric)}"
            topics.append(topic_map.sensor_discovery_topic(object_id))
        for ch in channels:
            ch_id = str(ch.get("channel_id") or "").strip()
            label = str(ch.get("label") or "").strip()
            if ch_id:
                topics.append(topic_map.switch_discovery_topic(f"{device_id}__{ch_id}"))
            if label:
                topics.append(topic_map.switch_discovery_topic(f"{device_id}__{slugify(label)}"))

        if not topics:
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_sensor_metrics", None) or set()
                for key in list(known):
                    if key.startswith(f"{device_id}::"):
                        metric_slug = key.split("::", 1)[1]
                        topics.append(topic_map.sensor_discovery_topic(f"{device_id}__{metric_slug}"))
            except Exception:
                pass
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_switch_channels", None) or set()
                for key in list(known):
                    if key.startswith(f"{device_id}::"):
                        ch_id = key.split("::", 1)[1]
                        topics.append(topic_map.switch_discovery_topic(f"{device_id}__{ch_id}"))
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
        stats = {"topics_cleared": 0}
        if not mqtt_ingest:
            return stats
        client = mqtt_ingest.client
        base_topic = getattr(mqtt_ingest, "base_topic", "") or ""

        topics: set[str] = set()
        try:
            for topic, dev in (mqtt_ingest.topic_dev_id_map or {}).items():
                if dev == device_id and topic:
                    topics.add(topic)
        except Exception:
            pass
        for suffix in ("data", "state", "availability"):
            topics.add(f"nodus/{device_id}/{suffix}")
            if base_topic:
                topics.add(f"{base_topic}/nodus/{device_id}/{suffix}")
        try:
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_command_topics or {}).items():
                if sw_id == device_id and topic:
                    topics.add(topic)
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_state_topics or {}).items():
                if sw_id == device_id and topic:
                    topics.add(topic)
            for (sw_id, _ch_id), topic in (mqtt_ingest.nodus_switch_event_topics or {}).items():
                if sw_id == device_id and topic:
                    topics.add(topic)
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

        try:
            base = ing._normalize_host_key(device_id) if hasattr(ing, "_normalize_host_key") else device_id
        except Exception:
            base = device_id

        for key in (device_id, base, f"{base}.local"):
            if not key:
                continue
            for dname in ("device_type", "expected_gauge_map", "latest_meta"):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            for dname in ("device_status", "last_mqtt_seen", "nodus_availability"):
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
                if base in mapping:
                    mapping.pop(base, None); _bump()
                for host, peers in list(mapping.items()):
                    if device_id in (peers or []):
                        peers[:] = [p for p in peers if p != device_id]
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
        for key, path in [("sensor", Path(_SENSOR_BASE_DIR)/device_id),
                          ("switch", Path(_SWITCH_BASE_DIR)/device_id),
                          ("system", Path(_SYS_BASE_DIR)/device_id)]:
            try:
                if path.exists():
                    shutil.rmtree(path)
                    removed[key]=True
            except Exception as e:
                printDM(f"[remove-device] rmtree {path}: {e}", location=MODULE)
        return removed

    def _get_db_path()->str:
        try:
            from rPiDataLogger import rPiDataLogger  # type: ignore
            if hasattr(rPiDataLogger,"DB_PATH"): return getattr(rPiDataLogger,"DB_PATH")
            if hasattr(rPiDataLogger,"get_db_path"): return rPiDataLogger.get_db_path()  # type: ignore
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
        from rPiSensorSettingsManager import SensorSettingsManager
        mgr = SensorSettingsManager("sensor_settings")
        locations: dict[str, str] = {}
        for sid in mgr.list_ids():
            loc = mgr.get_setting(sid, "Sensor.LOCATION", "Unknown")
            locations[sid] = loc or "Unknown"
        return locations

    #remove device routes
    @router.get("/remove-device-list")
    async def remove_device_list():
        devices = await asyncio.to_thread(_collect_removable_ids)
        return JSONResponse({"devices": devices})

    @router.get("/remove-device")
    async def remove_device_modal_hint():
        """
        Kept for compatibility in case someone navigates to /remove-device.
        We just return a tiny page that instructs to use the modal button.
        """
        return HTMLResponse("<html><body><p>Use the Remove Device button to open the modal.</p></body></html>")

    @router.post("/remove-device")
    async def remove_device_post(request: Request):
        """
        Accepts JSON: {"device_ids": ["id1","id2",...]} or form with multiple 'device_ids'
        Executes removal for each device; returns JSON summary.
        """
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

        results = {}
        try:
            from rPiMQTTIngest import get_current_ingest as _get_ing
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
            request.app.state.mqtt_ingest.device_status[hostname] = "pending"
            return JSONResponse(content={"status": "retrying"})
        return JSONResponse(content={"status": "unknown host"}, status_code=400)

    @router.post("/submit-pi-setup")
    async def submit_pi_setup(request: Request):
        from fastapi.responses import RedirectResponse
        form = await request.form()
        settings = rPiSettings()

        #settings.replace_setting("Network", "SSID", form.get("ssid", ""))
        #settings.replace_setting("Network", "PASSWORD", form.get("password", ""))
        #settings.replace_setting("Network", "HOSTNAME", form.get("hostname", ""))

        settings.replace_setting("SensorNetwork", "BROKER", form.get("broker", ""))
        client_lines = form.get("clients", "").splitlines()
        client_list = [c.strip() for c in client_lines if c.strip()]
        settings.replace_setting("SensorNetwork", "CLIENTS", client_list)

        settings.replace_setting("Time", "TZ", form.get("tz", ""))
        settings.replace_setting("Time", "TZ_OFFSET", int(form.get("tzOffset", 0)))
        settings.replace_setting("Time", "TZ_NAME", form.get("tzName", ""))

        settings.replace_setting("Display", "gauge_size", form.get("gauge_size", ""))
        settings.replace_setting("Display", "display_style", form.get("display_style", ""))

        return RedirectResponse(url="/?refresh=true", status_code=303)        

    @router.post("/submit-homeassistant-settings")
    async def submit_homeassistant_settings(request: Request):
        settings = rPiSettings()
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
        settings.replace_setting("HomeAssistant", "BROKER", broker)
        settings.replace_setting("HomeAssistant", "PORT", port)
        settings.replace_setting("HomeAssistant", "HA_USERNAME", username)
        settings.replace_setting("HomeAssistant", "HA_PASSWORD", password)

        return JSONResponse({"status": "ok"})

    @router.get("/sensor-ids", response_class=JSONResponse)
    async def list_sensor_ids():
        # helpers (re-use same patterns used elsewhere in this file)
        def _strip_local_suffix(h: str) -> str:
            s = (h or "").strip()
            return s[:-6] if s.endswith(".local") else s

        def _is_switch_id(name: str) -> bool:
            return (name or "").strip().lower().startswith("switch-")

        def _is_valid_sensor_id(name: str) -> bool:
            s = (name or "").strip()
            if not s:
                return False
            if _is_switch_id(s):
                return False
            return bool(re.match(r"^[A-Za-z0-9._-]+$", s))

        # 1) local sensors (from app.state or module var set by rPiSensoria)
        def _get_local_sensor_ids() -> list[str]:
            sm = getattr(app.state, "sensor_map", None)
            if sm is None:
                import rPiWebRoutes as routes
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
        """
        try:
            logged_ids = data_logger.get_available_sensors() or []
        except Exception:
            logged_ids = []
        logged_ids = []
        if DEBUG:
            printDM(f"[{MODULE}] #3 - data_logger sensors {logged_ids}", location=MODULE)
        """

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
                import rPiWebRoutes as routes
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
    #     import rPiAddDevice
    #     preview = rPiAddDevice.begin_onboarding_preview()
    #     from rPiHtml import render_setup_modal
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
            from rPiSensorSettingsManager import SensorSettingsManager
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

        # Normalize to something resolvable via mDNS if no dot present
        if "." not in hostname and not hostname.endswith(".local"):
            hostname = f"{hostname}.local"

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


    # --- Edit Sensor (modal / template) ---
    @router.get("/edit-sensor", response_class=HTMLResponse)
    async def edit_sensor_page(
        request: Request,
        sensor_id: str = Query(...),
        embed: int = Query(0),
    ):
        from rPiSensorSettingsManager import SensorSettingsManager
        from rPiUtils import normalize_sensor_id, printDM, html_escape
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
                    location="rPiWebRoutes",
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

            # Render template
            templates = request.app.state.templates
            template = templates.get_template("modals/sensor_settings.html")
            modal_html = template.render(
                sensor_id=normalized_id,
                settings=settings_dict,
                metric_options=metric_options,
                current_metrics=current_metrics,
                location=location,
            )

            if embed:
                # just return snippet for dashboard JS
                return HTMLResponse(modal_html)

            # Full-page fallback (used rarely)
            page: list[str] = []
            page.append("<!DOCTYPE html>")
            page.append("<html><head><title>Edit Sensor</title>")
            page.append("<link rel='stylesheet' href='/ui_static/css/app.css'>")
            page.append("</head><body>")
            page.append("<div id='modalHost'></div>")
            page.append(f"<script>var __MODAL_HTML__ = {json.dumps(modal_html)};</script>")
            page.append("<script>")
            page.append("  (function(){")
            page.append("    var host = document.getElementById('modalHost') || document.body;")
            page.append("    host.innerHTML = __MODAL_HTML__;")
            page.append("  })();")
            page.append("</script>")
            page.append("</body></html>")
            return HTMLResponse(content="\n".join(page))

        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            printDM(f"[{MODULE}] Exception: {e}\n{tb}", location=MODULE)
            return HTMLResponse(
                f"<h3>Internal Error:<br>{html_escape(str(e))}</h3>"
                f"<pre>{html_escape(tb)}</pre><a href='/'>Return</a>",
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
            return f"http://{host}.local:8000"

        # Build a proper updates[] payload for Nodus
        def _nodus_updates_from_display(device_file: str, display_block: dict) -> dict:
            """
            device_file: 'sensor_i2c.toml' | 'sensor_soil.toml' | 'sensor.toml'
            display_block: OrderedDict({'METRIC_1': '...', ...})
            Returns: {'updates': [ {...}, ... ]}
            """
            # decide 'file' selector and optional filename
            file_select = "sensor"          # we are changing a sensor*.toml
            name_field  = device_file       # tell Nodus which concrete sensor file to touch

            order = ("METRIC_1","METRIC_2","METRIC_3","METRIC_4","METRIC_5","METRIC_6")
            updates = []
            for key in order:
                val = (display_block or {}).get(key, "")
                updates.append({
                    "file":    file_select,
                    "name":    name_field,
                    "section": "Display",
                    "key":     key,
                    "value":   (val or "")
                })
            return {"updates": updates}

        async def push_updates_to_picow(base_dir: Path, sensor_id_norm: str, device_file: str,
                                        merged_doc: OrderedDict, metric_list: list[str]) -> None:
            mgr = SensorSettingsManager(str(base_dir))
            live_doc = mgr.load(sensor_id_norm) or {}
            target_url = resolve_hostname(sensor_id_norm, live_doc).rstrip("/") + "/set-nodus-setting"

            # Only send the relevant blocks to the Pico2 W
            from collections import OrderedDict as OD
            payload_doc = OD()
            if "Sensor" in merged_doc:
                payload_doc["Sensor"] = OD()
                for key in ("DEVICE", "SENSOR_ID", "LOCATION"):
                    val = merged_doc["Sensor"].get(key, "")
                    payload_doc["Sensor"][key] = val

            if metric_list:
                payload_doc["Display"] = OD((f"METRIC_{i}", (metric_list[i-1] if i-1 < len(metric_list) else ""))
                                            for i in range(1, 7))

            payload = _nodus_updates_from_display(device_file, payload_doc.get("Display", {}))

            timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(target_url, json=payload)
                    if resp.status_code != 200:
                        printDM(f"[{MODULE}] Nodus update returned {resp.status_code}: {resp.text[:200]}",
                                location="rPiWebRoutes")
                    else:
                        try:
                            host = resolve_hostname(sensor_id_norm, live_doc)
                            mqtt_ingest.add_client(host)   # marks 'pending' and forces an expedited check
                            await mqtt_ingest.force_refresh_device_metadata(sensor_id_norm)
                        except Exception:
                            pass
                        printDM(f"[{MODULE}] Pushed payload {payload} to {device_file} to Nodus @ {target_url}",
                                location="rPiWebRoutes")
            except Exception as e:
                printDM(f"[{MODULE}] Failed to push {device_file} to Nodus ({target_url}): {e}",
                        location="rPiWebRoutes")

        # ---------- validate form ----------
        sensor_id_in_form = form.get("sensor_id")
        if not sensor_id_in_form:
            return HTMLResponse("<h3>Missing sensor_id</h3><a href='/'>Return</a>", status_code=400)

        old_id = normalize_sensor_id(sensor_id_in_form)
        manager = SensorSettingsManager("sensor_settings")

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
                printDM(f"[{MODULE}] Renamed settings directory: {old_id} → {new_id}", location="rPiWebRoutes")
            except Exception as e:
                printDM(f"[{MODULE}] Failed to rename {old_id}→{new_id}: {e}", location="rPiWebRoutes")

        # ---------- If Pico2 W-backed, push only the relevant blocks ----------
        live_dir = new_dir if new_id != old_id else old_dir
        try:
            sensor_type = detect_sensor_type(live_dir)  # 'picow' / 'pico2w' / 'pi' / None
        except Exception:
            sensor_type = None

        if sensor_type in ("picow", "pico2w"):
            device_toml = guess_device_toml(device_value)
            await push_updates_to_picow(base_dir, new_id, device_toml, merged_doc, metric_list)

        return RedirectResponse(url="/", status_code=303)

    @router.post("/calibrate")
    async def calibrate_sensor(sensor_id: str = Query(...)):
        from rPiUtils import normalize_sensor_id, printDM
        from rPiSensorSettingsManager import SensorSettingsManager
        import asyncio, functools, socket, httpx  # asyncio/functools/socket for IPv4 resolve

        def _get_sensor_map():
            sm = getattr(app.state, "sensor_map", None)
            if sm is None:
                import rPiWebRoutes as routes
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
                printDM(f"[calibrate_sensor] local exception: {e}", location="rPiWebRoutes")
                return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

        # ---------- 2) Remote Nodus (Pico2 W) proxy ----------
        try:
            mgr = SensorSettingsManager("sensor_settings")
            sid_norm = normalize_sensor_id(sensor_id)
            settings_dict = mgr.load(sid_norm) or {}
            sensor_block = (settings_dict.get("Sensor") or settings_dict.get("sensor") or {})
            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()

            # Prefer explicit HOSTNAME; else fallback to FULL SENSOR_ID (NOT the prefix)
            hostname = (sensor_block.get("HOSTNAME") or sensor_block.get("hostname") or "").strip()
            if not hostname:
                hostname = str(sensor_block.get("SENSOR_ID", sensor_block.get("sensor_id", "") or sid_norm)).strip()

            if dev_type in ("picow", "pico2w") and hostname:
                # Resolve IPv4 for .local (mDNS often flaky); try IP first
                async def _ipv4_first(host: str, port: int, timeout: float = 2.0):
                    loop = asyncio.get_running_loop()
                    try:
                        infos = await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                functools.partial(socket.getaddrinfo, host, port,
                                                  family=socket.AF_INET, type=socket.SOCK_STREAM)
                            ),
                            timeout=timeout
                        )
                        if infos:
                            return infos[0][4][0]
                    except Exception:
                        return None
                    return None

                tried = []
                targets = []

                ip = await _ipv4_first(f"{hostname}.local", 8000, timeout=2.0)
                if ip:
                    targets.append(f"http://{ip}:8000")
                targets.extend((
                    f"http://{hostname}.local:8000",
                    f"http://{hostname}:8000",
                ))

                last_err = None
                for base in targets:
                    url = f"{base}/start-calibration"
                    tried.append(url)
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.post(url, json={"sensor_id": sensor_id})
                        if resp.status_code == 200:
                            # Treat any 200 as a “started” signal for the UI
                            printDM(f"[calibrate_sensor] proxied OK -> {url}", location="rPiWebRoutes")
                            return JSONResponse({"status": "started", "source": "remote", "url": url})
                        last_err = f"{resp.status_code} {resp.text}"
                    except Exception as e:
                        last_err = str(e)

                return JSONResponse(
                    {"status": "error",
                     "message": f"Could not reach Nodus for {sensor_id}. Tried: {tried}. Last error: {last_err}"},
                    status_code=502,
                )

        except Exception as e:
            printDM(f"[calibrate_sensor] proxy lookup exception: {e}", location="rPiWebRoutes")

        # ---------- 3) Unknown ----------
        return JSONResponse({"status": "error", "message": f"Unknown or unsupported sensor_id: {sensor_id}"}, status_code=404)

    @router.get("/calibration-status")
    async def get_calibration_status(sensor_id: str = Query(...)):
        from rPiUtils import normalize_sensor_id, printDM
        from rPiSensorSettingsManager import SensorSettingsManager
        import httpx

        try:
            sid_norm = normalize_sensor_id(sensor_id)
            mgr = SensorSettingsManager("sensor_settings")
            settings = mgr.load(sid_norm) or {}

            sensor_block = (settings.get("Sensor") or settings.get("sensor") or {})
            cal_block = (settings.get("Calibration") or settings.get("calibration") or {})

            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            hostname = (sensor_block.get("HOSTNAME") or sensor_block.get("hostname") or "").strip()
            if not hostname:
                # fallback: derive host prefix from SENSOR_ID
                host_src = str(sensor_block.get("SENSOR_ID", sensor_block.get("sensor_id", "") ) or "")
                hostname = host_src.split("-", 1)[0] if host_src else ""

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
                from rPiWebRoutes import sensor_map
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

            # 3) Fallback: proxy directly to Nodus (legacy behavior)
            if hostname:
                for url in (
                    f"http://{hostname}.local:8000/calibration-status",
                    f"http://{hostname}:8000/calibration-status",
                ):
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.get(url)
                        if resp.status_code == 200:
                            return JSONResponse(resp.json())
                    except Exception:
                        continue

            return JSONResponse(
                {"status": "error", "message": "Unable to determine calibration status"},
                status_code=502,
            )
        except Exception as e:
            printDM(f"[/calibration-status] error: {e}", location="rPiWebRoutes")
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

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
        from rPiUtils import normalize_sensor_id, printDM
        from rPiSensorSettingsManager import SensorSettingsManager
        from collections import OrderedDict
        import time

        evt_name = str((event or {}).get("event", "")).strip().lower()
        payload = (event or {}).get("payload", {}) or {}

        printDM(
            f"[sensor-event] received event='{evt_name}' for sensor_id='{payload.get('sensor_id', '')}'",
            location="rPiWebRoutes",
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
                printDM(f"[sensor-event] progress cache update failed: {e}", location="rPiWebRoutes")

            # Live controller nudge so the UI can show "Calibrating" without a reload
            try:
                from rPiWebRoutes import sensor_map
                ctrl = sensor_map.get(sensor_id) if isinstance(sensor_map, dict) else None
                if ctrl and hasattr(ctrl, "sensor") and hasattr(ctrl.sensor, "is_calibrated"):
                    try:
                        ctrl.sensor.is_calibrated = "Calibrating"
                    except Exception:
                        pass
            except Exception as e:
                printDM(f"[sensor-event] progress live controller update skipped: {e}", location="rPiWebRoutes")

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
            printDM(f"[sensor-event] result cache update failed: {e}", location="rPiWebRoutes")

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
            printDM(f"[sensor-event] settings write failed: {e}", location="rPiWebRoutes")
            return JSONResponse({"status": "error", "message": f"settings write failed: {e}"}, status_code=500)

        # ── live controller nudge so UI updates instantly (best-effort) ───────────
        try:
            from rPiWebRoutes import sensor_map
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
            printDM(f"[sensor-event] live controller update skipped: {e}", location="rPiWebRoutes")

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
        from collections import OrderedDict
        from rPiSensorSettingsManager import SensorSettingsManager
        from rPiUtils import printDM
        from rPiCalibration import notify_sensor_runtime_of_calibration

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
            doc = mgr.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            doc = OrderedDict()

        # Ensure top-level [Calibration] exists
        from collections import OrderedDict as _OD

        calib = doc.get("Calibration")
        if not isinstance(calib, dict):
            calib = _OD()
            doc["Calibration"] = calib

        def _set_path(path: str, value: float) -> None:
            """
            Generic dotted-path setter:

              "Calibration.Device.CO2_OFFSET" ->
                  doc["Calibration"]["Device"]["CO2_OFFSET"] = value

            Creates intermediate dicts as needed.
            """
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

        for item in offsets:
            key = str(item.get("key") or "").strip()
            raw_val = item.get("value", 0)

            try:
                val = float(raw_val)
            except Exception:
                # Skip non-numeric entries
                continue

            # ---- APVPD special-case keys from the left-side pane ----
            if device_kind == "apvpd" and key in ("ambient_temp_offset", "ambient_rh_offset"):
                if key == "ambient_temp_offset":
                    calib["APVPD_TEMP_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_TEMP_CAL_VAL")
                elif key == "ambient_rh_offset":
                    calib["APVPD_RH_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_RH_CAL_VAL")
                continue

            # ---- SoilModbusSensor: soil-specific short keys -> [Calibration.Device] ----
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

            # ---- Generic dotted keys from device_offsets (co2 / aqi / veml / vpd / etc.) ----
            if not key:
                continue

            _set_path(key, val)
            applied_keys.append(key)

        try:
            mgr.save(sensor_id, doc)
        except Exception as exc:
            printDM(f"[{MODULE}] device_calibration_apply save error: {exc}", location=MODULE)
            return JSONResponse(
                {
                    "status": "error",
                    "message": f"Failed to save device calibration for {sensor_id}.",
                },
                status_code=500,
            )

        # 1) Ask local runtime to reload calibration for this sensor_id
        try:
            supervisor = getattr(request.app.state, "supervisor", None)
            notify_sensor_runtime_of_calibration(supervisor, sensor_id)
        except Exception as exc:
            printDM(
                f"[{MODULE}] device_calibration_apply reload error for {sensor_id}: {exc}",
                location=MODULE,
            )

        # 2) If this is a Nodus sensor, also forward device offsets to its own
        #    /update-calibration-values endpoint so it can apply at the device.
        try:
            # Inspect Sensor.TYPE from the same doc we just saved
            sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
            sensor_type = str(
                sensor_blk.get("TYPE") or sensor_blk.get("type") or ""
            ).strip().lower()

            if sensor_type in ("picow", "pico2w", "nodus", "remote"):
                base_url = resolve_nodus_base_url(sensor_id)
                if base_url:
                    # Reuse a subset of the original payload; Nodus-side route
                    # expects sensor_id + offsets; is_remote is just a hint.
                    nodus_payload = {
                        "sensor_id": sensor_id,
                        "offsets": offsets,
                        "is_remote": True,
                    }
                    await forward_calibration_to_nodus(base_url, nodus_payload)
        except Exception as exc:
            if DEBUG:
                printDM(
                    f"[{MODULE}] device_calibration_apply Nodus forward error for {sensor_id}: {exc}",
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
        from rPiCalibration import CalibrationManager

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
        from rPiCalibration import CalibrationManager
        from rPiSensorSettingsManager import SensorSettingsManager
        from rPiDataLogger import rPiDataLogger

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
        data_logger = rPiDataLogger()  # if you already have a global, reuse that instead
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
        import asyncio
        from rPiCalibration import CalibrationManager, SystemCalResult

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

        # We'll collect Nodus pushes separately so we can run them concurrently.
        nodus_jobs: list[tuple[str, dict, SystemCalResult]] = []

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
                # 1) Update the Pi-side sensor_settings/<sensor_id>/sensor.toml
                cal_mgr.apply_system_calibration(sensor_id, result)
                applied.append(sensor_id)

                # 2) If this is a Nodus (picow/pico2w) sensor, schedule push of same values
                doc = sensor_mgr.load(sensor_id) or {}
                sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
                sensor_type = (
                    sensor_blk.get("TYPE")
                    or sensor_blk.get("type")
                    or ""
                )
                sensor_type = str(sensor_type).strip().lower()
                if sensor_type in ("picow", "pico2w"):
                    nodus_jobs.append((sensor_id, doc, result))

            except Exception as exc:
                failures.append(
                    {
                        "sensor_id": sensor_id,
                        "error": str(exc),
                    }
                )
                continue

        async def _push_system_cal_to_nodus(
            sensor_id: str,
            doc: dict,
            result: SystemCalResult,
        ) -> bool:
            """
            Push the *system* calibration into the Nodus sensor's sensor*.toml
            via /set-nodus-setting. We keep this best-effort and log failures.
            """
            from math import isnan

            # Heuristic: choose which sensor*.toml to touch
            sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
            sensor_file_name = "sensor.toml"
            i2c_keys = ("I2C_SCL", "I2C_SDA", "I2C_BUS", "I2C_ADDR")
            uart_keys = ("UART_TX", "UART_RX", "UART_BUS", "RS485_DIR_PIN", "MODBUS_ADDR")
            if any(k in sensor_blk for k in i2c_keys):
                sensor_file_name = "sensor_i2c.toml"
            elif any(k in sensor_blk for k in uart_keys):
                sensor_file_name = "sensor_soil.toml"

            # Normalize values for remote write
            temp_off = 0.0 if result.temp_offset is None or isnan(result.temp_offset) else float(result.temp_offset)
            rh_off = 0.0 if result.rh_offset is None or isnan(result.rh_offset) else float(result.rh_offset)
            range_hours = int((result.end_ts - result.start_ts) / 3600.0)

            # Keys to push. We mirror what apply_system_calibration wrote locally.
            items = [
                ("Calibration", "CALIBRATED", True),
                ("Calibration", "CALIB_STATUS", "Calibrated (system)"),
                ("Calibration.System", "TEMP_OFFSET", round(temp_off, 3)),
                ("Calibration.System", "RH_OFFSET", round(rh_off, 3)),
                ("Calibration.System", "REF_SENSOR_ID", result.ref_sensor_id),
                ("Calibration.System", "REF_RANGE_HOURS", range_hours),
                ("Calibration.System", "REF_START_TS", int(result.start_ts)),
                ("Calibration.System", "REF_END_TS", int(result.end_ts)),
            ]

            ok_all = True
            for section, key, value in items:
                try:
                    await push_nodus_setting_simple(
                        device_id=sensor_id,
                        device_type="sensor",
                        setting_file_key="sensor",
                        section=section,
                        key=key,
                        value=value,
                        sensor_file_name=sensor_file_name,
                        system_mgr=None,
                        system_root=None,
                        ip_hint=None,
                        sys_host_index=None,
                    )
                except Exception as exc:
                    ok_all = False
                    if DEBUG:
                        printDM(
                            f"[{MODULE}] system_calibration_apply: Nodus push failed "
                            f"for {sensor_id} {section}.{key}: {exc}",
                            location=MODULE,
                        )
            return ok_all

        # Kick off Nodus pushes, if any
        if nodus_jobs:
            tasks = [
                _push_system_cal_to_nodus(sid, doc, res)
                for (sid, doc, res) in nodus_jobs
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

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
        from rPiCalibration import apply_calibration_updates_local, notify_sensor_runtime_of_calibration
        
        payload = await request.json()
        sensor_id = (payload.get("sensor_id") or "").strip()
        if not sensor_id:
            return JSONResponse({"error": "sensor_id required"}, status_code=400)

        offsets = payload.get("offsets")
        calib   = payload.get("calibration")
        meta    = payload.get("meta", {})
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

        # 3) if this sensor is a Nodus device, forward update to its /update-calibration-values
        try:
            from rPiSettings import rPiSettings
            sys_settings = rPiSettings()
            # Use SensorSettingsManager or a 'device_map' to resolve host/URL for this sensor_id
            # Example: device_map[sensor_id] -> "http://nodus-1234.local:8000"
            base_url = resolve_nodus_base_url(sensor_id)
            if base_url:
                await forward_calibration_to_nodus(base_url, payload)
        except Exception:
            pass

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
        Canonical switch key builder: use rPiDataLogger.build_switch_key if present,
        otherwise fall back to '<switch_id>::<label>'.
        """
        sid = (switch_id or "").strip()
        lab = (label or "").strip()
        ch_id = _resolve_channel_id_from_label(sid, lab)
        if _build_switch_key is not None:
            try:
                if ch_id:
                    return _build_switch_key(sid, lab, ch_id)
                return _build_switch_key(sid, lab)
            except Exception:
                pass
        return f"{sid}::{lab}"

    @router.get("/edit-switch", response_class=HTMLResponse)
    async def edit_switch_page(
        request: Request,
        switch_id: str = Query(...),
        embed: int = Query(0),
    ):
        from rPiSwitchSettingsManager import SwitchSettingsManager

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

        # ---- helper: extract enabled channel indices (same semantics as rPiHtml._extract_channel_indices) ----
        sw = (settings_dict or {}).get("Switch", {}) or {}

        def _truthy(val) -> bool:
            s = str(val).strip().lower()
            return s not in ("", "0", "false", "no", "off", "none", "null")

        def _extract_channel_indices(sw_section: dict) -> list[int]:
            indices_found: set[int] = set()
            for key in sw_section.keys():
                m = re.match(r"^SWITCH_(\d+)(?:_EN|_Trigger)?$", str(key))
                if m:
                    indices_found.add(int(m.group(1)))
            if not indices_found:
                return [1]

            render_indices: list[int] = []
            for i in sorted(indices_found):
                en_key = f"SWITCH_{i}_EN"
                label_key = f"SWITCH_{i}"
                if en_key in sw_section:
                    if _truthy(sw_section.get(en_key)):
                        render_indices.append(i)
                else:
                    if str(sw_section.get(label_key, "")).strip():
                        render_indices.append(i)
            return render_indices or [1]

        channel_indices = _extract_channel_indices(sw)
        channels = [
            {
                "index": idx,
                "label": str(sw.get(f"SWITCH_{idx}", "") or ""),
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
        from rPiSwitchSettingsManager import SwitchSettingsManager
        from rPiAutomationManager import AutomationManager

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
        pat = re.compile(r"^SWITCH_(\d+)(?:_Trigger)?$")
        for key in form.keys():
            m = pat.match(key)
            if m:
                idxs.add(int(m.group(1)))
        channel_indices = sorted(idxs) or [1]

        # Optional: store CHANNELS
        sw_block = OrderedDict({
            "DEVICE":          device_value or existing_doc["Switch"].get("DEVICE", ""),
            "SWITCH_ID":       new_id or existing_doc["Switch"].get("SWITCH_ID", old_id),
            "SWITCH_LOCATION": location_value or existing_doc["Switch"].get("SWITCH_LOCATION", "Unknown"),
            "CHANNELS":        len(channel_indices),
        })
        if "BROKER" in existing_doc.get("Switch", {}) or broker_value:
            sw_block["BROKER"] = broker_value or existing_doc["Switch"].get("BROKER", "")

        # Merge per-channel updates
        for i in channel_indices:
            label_key   = f"SWITCH_{i}"
            trigger_key = f"SWITCH_{i}_Trigger"
            if label_key in form:
                sw_block[label_key] = (form.get(label_key, "") or "").strip()
            if trigger_key in form:
                sw_block[trigger_key] = (form.get(trigger_key, "") or "").strip()

            # If you also POST hidden BASIC JSONs, pick them up here for automations.toml later:
            basic_json_key = f"BASIC_{i}_JSON"
            if basic_json_key in form:
                # You can parse and persist into automations.toml with your AutomationsManager
                pass

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

        # ---- handle directory rename if SWITCH_ID changed ----
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

        # -----------------------------
        # Persist Basic triggers to automations.toml via rPiAutomationManager
        # -----------------------------
        target_switch_id = new_id
        final_switch_map = (merged_doc.get("Switch") or {})
        trig_mgr = AutomationManager(base_dir="switch_settings")

        basic_keys = [name for name in form.keys() if name.startswith("BASIC_") and name.endswith("_JSON")]
        for name in basic_keys:
            try:
                idx = int(name.split("_")[1])
            except Exception:
                continue
            raw = (form.get(name) or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception as e:
                printDM(f"[{MODULE}] Invalid BASIC payload for channel {idx}: {e}", location=MODULE)
                continue

            condition = {
                "sensor_id": payload.get("sensor_id", ""),
                "metric": payload.get("metric", ""),
                "op": payload.get("op", ">"),
                "threshold": payload.get("threshold", 0),
                "hysteresis": payload.get("hysteresis", 0),
                "min_interval_sec": payload.get("min_interval_sec", 0),
            }
            rule_name = (payload.get("name") or "").strip()
            enabled_flag = bool(payload.get("enabled", True))

            ch_label = (final_switch_map.get(f"SWITCH_{idx}", "") or f"SWITCH_{idx}").strip()
            action = {"switch_key": f"{target_switch_id}::{ch_label}", "set": True}

            rule_id = f"SWITCH_{idx}_Basic"
            try:
                trig_mgr.upsert_basic_rule(
                    hostname=target_switch_id,
                    rule_id=rule_id,
                    enabled=enabled_flag,
                    condition=condition,
                    action=action,
                    name=rule_name,
                )
                printDM(f"[{MODULE}] Saved Basic trigger {rule_id} for {target_switch_id}", location=MODULE)
            except Exception as e:
                printDM(f"[{MODULE}] Failed to save Basic trigger {rule_id}: {e}", location=MODULE)

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
        from rPiSwitchSettingsManager import SwitchSettingsManager
        import html
        from urllib.parse import quote

        mgr = SwitchSettingsManager("switch_settings")
        ids = mgr.list_switches() or []
        items = "\n".join(
            f'<li><a href="/advanced-trigger?switch_id={quote(sid)}">{html.escape(sid)}</a></li>'
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
        from rPiSwitchSettingsManager import SwitchSettingsManager
        try:
            mgr = SwitchSettingsManager("switch_settings")
            dat = mgr.load(switch_id)
            if not dat:
                return JSONResponse({"error": f"switch_id '{switch_id}' not found"}, status_code=404)

            sw = (dat.get("Switch") or {})

            # 1) Collect labels from SWITCH_<n> or (fallback) SWITCH_<n>_LABEL
            labels: dict[int, str] = {}
            import re
            for k, v in sw.items():
                ks = str(k).strip()
                m = re.fullmatch(r"SWITCH_(\d+)", ks, flags=re.IGNORECASE)
                if m:
                    idx = int(m.group(1))
                    label_text = ("" if v is None else str(v)).strip()
                    if label_text:
                        labels[idx] = label_text
                    continue
                m2 = re.fullmatch(r"SWITCH_(\d+)_LABEL", ks, flags=re.IGNORECASE)
                if m2:
                    idx = int(m2.group(1))
                    label_text = ("" if v is None else str(v)).strip()
                    if label_text and idx not in labels:
                        labels[idx] = label_text

            # 2) Determine channel count from CHANNELS (if present) or from the max SWITCH_<n> index
            try:
                raw_channels = next((sw.get(k) for k in ("CHANNELS", "channels", "Channels") if k in sw), 0)
                channels = int(raw_channels) if str(raw_channels).strip() else 0
            except Exception:
                channels = 0

            if not channels:
                channels = max(labels.keys(), default=1)

            # 3) Ensure every [1..channels] has a label (fallback to CH<i> only if unnamed)
            for i in range(1, channels + 1):
                labels.setdefault(i, f"CH{i}")

            return {"switch_id": switch_id, "channels": channels, "labels": labels}
        except Exception as exc:
            printDM(f"/switch-info error: {exc}", location="rPiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/advanced/automations", response_class=JSONResponse)
    async def api_list_advanced_automations(switch_id: str = Query(...)):
        from rPiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("switch_settings")
            data = mgr.load(switch_id)
            if not data:
                return JSONResponse({"error": f"switch_id '{switch_id}' not found"}, status_code=404)

            adv = data.get("Advanced") or {}
            items = []
            # Sort by key for stable display
            for rule_id in sorted(adv.keys()):
                payload = adv[rule_id]
                if isinstance(payload, dict):
                    enabled = bool(payload.get("enabled", True))
                    script_json = str(payload.get("script_json", "")) or ""
                else:
                    enabled = True
                    script_json = str(payload or "")
                items.append({"rule_id": rule_id, "enabled": enabled, "script_json": script_json})
            return {"switch_id": switch_id, "items": items}
        except Exception as exc:
            printDM(f"/advanced/automations error: {exc}", location="rPiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.post("/advanced/automations/enable", response_class=JSONResponse)
    async def api_enable_advanced_automation(
        switch_id: str = Form(...),
        rule_id: str = Form(...),
        enabled: str = Form("true"),  # accept str, coerce below
    ):
        from rPiAutomationManager import AutomationManager
        try:
            truthy = str(enabled).strip().lower() in {"1", "true", "on", "yes"}
            mgr = AutomationManager("switch_settings")
            ok = mgr.set_rule_enabled(switch_id, section="Advanced", rule_id=rule_id, enabled=truthy)
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/enable error: {exc}", location="rPiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.post("/advanced/automations/delete", response_class=JSONResponse)
    async def api_delete_advanced_automation(
        switch_id: str = Form(...),
        rule_id: str = Form(...),
    ):
        from rPiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("switch_settings")
            ok = mgr.delete_rule(switch_id, section="Advanced", rule_id=rule_id)
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/delete error: {exc}", location="rPiWebRoutes")
            return JSONResponse({"error": str(exc)}, status_code=500)

    @router.get("/switch-advanced", response_class=JSONResponse)
    async def get_advanced_script(switch_id: str = Query(...), channel: int = Query(1)):
        """
        Returns the current Advanced script JSON (normalized) for SWITCH_<channel>_Advanced,
        or {} if not present.
        """
        from rPiSwitchSettingsManager import SwitchSettingsManager
        try:
            from rPiAutomationManager import AutomationManager, load_triggers
        except Exception:
            from rPiAutomationManager import load_triggers
            AutomationManager = None  # type: ignore

        def _coerce_int(x, default=1):
            try: return int(str(x).strip())
            except Exception: return default

        ch = _coerce_int(channel, 1)
        settings_mgr = SwitchSettingsManager("switch_settings")
        data = {}
        try:
            data = load_triggers(settings_mgr, switch_id) or {}
        except Exception:
            data = {}

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
        Persists an Advanced trigger script to switch_settings/<switch_id>/automations.toml
        Accepts form or JSON payloads.

        Expected fields:
          - type            (required)
          - switch_id       (required)
          - channel         (e.g., "1") OR switch_selector (fallback)
          - rule_id         (optional; default: "SWITCH_<channel>_Advanced"; made unique if collides)
          - script_json     (required) JSON string built in the Advanced modal
          - enabled         (optional; default: true)
        """
        from rPiSwitchSettingsManager import SwitchSettingsManager
        try:
            # Prefer class API
            from rPiAutomationManager import AutomationManager, load_triggers, save_triggers
        except Exception:
            # Fallback module functions
            from rPiAutomationManager import load_automations as load_triggers, save_automations as save_triggers
            AutomationManager = None  # type: ignore

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
                    "type":   cond_type,  # 'sensor' / 'time' / 'timer' / 'or'
                    "sensor": str(c.get("sensor",  c.get("sensor_id", ""))).strip(),
                    "metric": str(c.get("metric",  "")).strip(),
                    "op":     str(c.get("op",      ">")).strip(),
                    "value":  _num(c.get("value"), float, None),
                    "hyst":   _num(c.get("hyst"),  float, None),
                    "start":  str(c.get("start",   "")).strip(),
                    "end":    str(c.get("end",     "")).strip(),
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
        settings_mgr = SwitchSettingsManager("switch_settings")
        sw_doc = settings_mgr.load(switch_id) or {}
        switch_map = sw_doc.get("Switch") or {}
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
            if AutomationManager is not None:
                trig_mgr = AutomationManager("switch_settings")
                # Peek existing to ensure uniqueness (manager helpers)
                existing = None
                try:
                    # If class exposes loader, use it; otherwise fallback to module fn
                    existing = load_triggers(settings_mgr, switch_id)
                except Exception:
                    existing = None
                existing_ids = set((existing or {}).get("Advanced", {}).keys())
                final_rule_id = _unique_rule_id(existing_ids, rule_id)
                trig_mgr.upsert_advanced_rule(
                    hostname=switch_id,
                    rule_id=final_rule_id,
                    enabled=enabled,
                    script=compact_script,
                )
            else:
                # Fallback: manual merge & save
                data = load_triggers(settings_mgr, switch_id) or {}
                adv = data.get("Advanced", {})
                existing_ids = set(adv.keys())
                final_rule_id = _unique_rule_id(existing_ids, rule_id)
                adv[final_rule_id] = compact_script  # keep same shape as before (string value)
                data["Advanced"] = adv
                save_triggers(settings_mgr, switch_id, data)

            printDM(f"[{MODULE}] Saved Advanced trigger {rule_id} -> {final_rule_id} for {switch_id}", location=MODULE)
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

        states: dict[str, dict] = {}

        def _format_events(switch_key: str, sensor_id: str | None, limit: int = 5) -> list[str]:
            evs = data_logger.get_last_switch_events(switch_key, sensor_id=sensor_id, limit=limit)
            out: list[str] = []
            for state_str, ts in evs:
                label = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                out.append(f"{label} {ts}")
            return out  # oldest → newest

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

            # --- B) Remote Pico2 W / Nodus switches via MQTT cache ---
            # mqtt_ingest._switch_state_cache: { switch_id: { channel_label: "on"/"off" } }
            cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
            for remote_switch_id, ch_map in cache.items():
                if not isinstance(ch_map, dict):
                    continue
                sensor_lineage = f"Switch_{remote_switch_id}"
                for channel_label, human_state in ch_map.items():
                    # UI key is still label-based
                    ui_key = f"{remote_switch_id}::{channel_label}"

                    # DB key is ID-based when available
                    db_key = _switch_key(remote_switch_id, channel_label)

                    latest = data_logger.get_latest_switch_state(db_key, sensor_id=sensor_lineage)
                    latest_bool = (latest == "On") if latest is not None else (str(human_state).lower() == "on")
                    events = _format_events(db_key, sensor_lineage, limit=5)
                    states[ui_key] = {"state": latest_bool, "time": events}

            return JSONResponse(states)

        except Exception as e:
            printDM(f"switch-status-update error: {e}", location=MODULE)
            return JSONResponse({"error": str(e)}, status_code=500)

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

        def _norm_label(s: str | None) -> str | None:
            return s.strip() if s else None

        def _norm_switch_id(s: str | None) -> str | None:
            return s.strip().lower() if s else None

        def _ctrl_switch_id(ctrl) -> str | None:
            val = getattr(ctrl, "switch_id", None)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
            return None

        def _slugify(text: str) -> str:
            return (text or "").strip().lower().replace(" ", "_")

        def _looks_remote(ctrl) -> bool:
            # Heuristics that cover MQTTSwitch and your remote ids
            return (
                bool(getattr(ctrl, "switch_topics", None)) or
                bool(getattr(getattr(ctrl, "switch", None), "switch_topics", None)) or
                str(getattr(ctrl, "switch_id", "")).startswith("switch-")
            )

        def _publish_label_set_direct(set_client, sid: str, label: str, new_state: bool) -> bool:
            """
            Last-resort: publish to label-slug set topic:
                switch/<sid>/<label_slug>/set
            Accepts {"set":"on|off"} payload which your Pico2 W handler supports.
            """
            try:
                if not set_client:
                    return False
                label_slug = _slugify(label)
                topic = f"switch/{sid}/{label_slug}/set"
                payload = json.dumps({"set": "on" if new_state else "off", "timestamp": time.time()})
                set_client.publish(topic, payload, qos=0, retain=False)
                if DEBUG:
                    printDM(f"[toggle_switch] fallback publish → {topic} {payload}", location=MODULE)
                return True
            except Exception as e:
                printDM(f"[toggle_switch] fallback publish failed: {e}", location=MODULE)
                return False

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
                    # Canonical DB identity: uses rPiDataLogger.build_switch_key under the hood
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
                                location="rPiWebRoutes",
                            )
                        return new_state
            except Exception as e:
                printDM(
                    f"[toggle_switch] DB lookup failed for {switch_id}::{label}: {e}",
                    location="rPiWebRoutes",
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

            # ---- Find matching controllers ----
            sc_map = switch_controllers if isinstance(switch_controllers, dict) else {}
            matches: list[tuple[object, str]] = []

            for ctrl in sc_map.values():
                try:
                    ctrl_labels = [s.strip() for s in (ctrl.get_switch_names() or [])]
                except Exception as e:
                    printDM(f"[toggle_switch] skipping non-controller: {e}", location=MODULE)
                    continue

                match_label = next((lbl for lbl in ctrl_labels if (lbl or "").lower() == label_q_lower), None)
                if not match_label:
                    continue

                if switch_id_raw and (_ctrl_switch_id(ctrl) or "") != switch_id_raw:
                    continue

                matches.append((ctrl, match_label))

            if not matches:
                if DEBUG:
                    printDM(f"[toggle_switch] No match found for: label='{label_raw}', switch_id='{switch_id_raw}'", location=MODULE)
                return JSONResponse({"error": "switch_not_found"}, status_code=404)

            if len(matches) > 1 and not switch_id_raw:
                options = []
                for ctrl, _ml in matches:
                    try:
                        options.append({
                            "location": getattr(ctrl, "location", None),
                            "labels": list(ctrl.get_switch_names() or []),
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
            sid = switch_id_raw or _ctrl_switch_id(ctrl) or getattr(ctrl, "switch_id", None)

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

                ingest_client = getattr(mqtt_ingest, "client", None)
                if ingest_client is None:
                    return JSONResponse({"error": "mqtt_not_ready", "detail": "ingest_client_none"}, status_code=503)

                # Primary: use ingest helper (finds correct base topic and emits {"set": "..."} JSON)
                if sid:
                    try:
                        ok = bool(mqtt_ingest.set_switch(sid, matched_label, new_state))
                    except Exception as e:
                        printDM(f"[toggle_switch] ingest.set_switch error: {e}", location=MODULE)

                # Fallback: publish to label-slug set topic directly with ingest client
                if not ok and sid:
                    ok = _publish_label_set_direct(ingest_client, sid, matched_label, new_state)

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
            try:
                if sid:
                    try:
                        db_key = ctrl._switch_key(matched_label)
                    except Exception:
                        # Fallback for older controllers
                        db_key = f"{sid}::{matched_label}"

                    state_text = "On" if bool(new_state) else "Off"
                    data_logger.log_switch_event(
                        switch_key=db_key,
                        is_on=bool(new_state),
                        source="ui",
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

            return {"state": bool(new_state), "time": ts}

        except Exception as e:
            printDM(f"[toggle_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.post("/switch/override")
    async def override_switch(
        request: Request,
        switch_name: str = Query(...),
        switch_key: str | None = Query(None),
        switch_id: str | None = Query(None),
    ):
        from rPiSwitchSettingsManager import SwitchSettingsManager
        from rPiAutomationManager import AutomationManager

        try:
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
            matches = []
            for ctrl in (switch_controllers or {}).values():
                try:
                    ctrl_labels = [s.strip() for s in (ctrl.get_switch_names() or [])]
                except Exception as e:
                    printDM(f"[override_switch] skipping non-controller: {e}", location=MODULE)
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
                        "labels": list((ctrl.get_switch_names() or [])),
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

                # 2) Update switch.toml override to the inverse of rule.enabled
                override_value = (not desired_rule_enabled)
                if sid and channel_index:
                    switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_value)

                # 3) Update in-memory map if present
                try:
                    override_map = getattr(ctrl, "override_script", None)
                    if isinstance(override_map, dict):
                        override_map[matched_label] = override_value
                except Exception:
                    pass

            except Exception as e:
                printDM(f"[override_switch] persist failed for '{matched_label}': {e}", location=MODULE)
                return JSONResponse({"error": "persist_failed"}, status_code=500)

            # notify any listeners that an automation rule changed
            try:
                app = request.app
                if hasattr(app.state, "switch_broadcast"):
                    await app.state.switch_broadcast({
                        "type": "automation_toggle",
                        "switch_id": switch_id_q,
                        "label": matched_label,
                        "enabled": bool(desired_rule_enabled),
                    })
            except Exception as e:
                printDM(f"[override_switch] broadcast failed: {e}", location=MODULE)

            # Return both states so UI can reflect the RULE state
            return {
                "status": "ok",
                "enabled": desired_rule_enabled,
                "override": (not desired_rule_enabled),
            }

        except Exception as e:
            printDM(f"[override_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------ system utilities -------
    @router.get("/clear-data", response_class=HTMLResponse)
    async def clear_data_page(confirm: bool = Query(False)):
        if confirm:
            data_logger.clear_all_readings()
            return HTMLResponse("<html><body><h3>✅ All sensor data cleared.</h3><a href='/'>Return to Dashboard</a></body></html>")
        else:
            return HTMLResponse(
                "<html><body><h3>Confirm Clear</h3>"
                "<p>This will permanently delete all stored sensor data.</p>"
                "<a href='/clear-data?confirm=true'>Yes, clear data</a><br>"
                "<a href='/'>Ok</a>"
                "</body></html>"
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
            return JSONResponse({"error": str(e)}, status_code=500)

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


    from rPiStats import create_stats_router
    app.include_router(create_stats_router(settings, gc_mgr))
    app.include_router(router)
    return router
