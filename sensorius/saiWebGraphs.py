"""Register historical graph and saved graph-configuration routes.

Queries, downsampling, and saved setups keep their existing contracts. The owning
runtime and settings instance are supplied explicitly without importing web routes.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Callable
import json
import math
import os
import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from .saiRuntimeServices import RuntimeServices
from .saiUtils import printDM

MODULE = "saiWebRoutes"

from zoneinfo import ZoneInfo
from .saiBiodynamics import get_skyfield_runtime_if_installed
try:
    from astral import LocationInfo
    from astral.sun import elevation as _astral_elevation
    from astral import moon as _astral_moon
except Exception:
    LocationInfo = _astral_elevation = _astral_moon = None

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


def register_graph_routes(router: APIRouter, *, services: RuntimeServices, settings,
                          sqlite_connect: Callable, settings_factory: Callable) -> None:
    """Register graph queries and saved setups for the supplied hub."""
    _sqlite_connect_with_recovery = sqlite_connect
    saiSettings = settings_factory
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


    @router.get("/graph-data", response_class=JSONResponse)
    def graph_data_api(
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
        """Return bounded historical graph data from a FastAPI worker."""
        import sqlite3
        from contextlib import closing
        from datetime import datetime, timedelta, timezone
        from fastapi.responses import JSONResponse
        from fastapi import HTTPException
        from .saiUtils import printDM

        MODULE = "graph-data"
        db_path = getattr(services.data_logger, "db_path", "sensorius_data.db")

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
            printDM(f"Invalid graph time range: {e}", location=MODULE)
            return JSONResponse({"error": "Invalid time range. Check the dates and retention window."}, status_code=400)

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

        # ----- indexed epoch queries with legacy timestamp compatibility -----
        def fetch_xy(cur, sid: str, metric_name: str, window_since_iso: str, window_until_iso: str):
            try:
                from .saiGraphData import fetch_graph_series
                return fetch_graph_series(
                    cur.connection, sid, metric_name, window_since_iso, window_until_iso,
                    max_points=GRAPH_MAX_POINTS_PER_SERIES,
                )
            except Exception as e:
                printDM(f"[{MODULE}] Error fetching {sid}.{metric_name}: {e}", location=MODULE)
                return [], []

        # ----- data series -----
        series: dict[str, dict] = {}
        simple_avg: dict[str, dict] = {}
        display_names: dict[str, str] = {}
        with closing(_sqlite_connect_with_recovery(db_path, source="graph_series")) as conn:
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
                    with closing(_sqlite_connect_with_recovery(db_path, source="graph_switch_transitions")) as conn2:
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
                    with closing(_sqlite_connect_with_recovery(db_path, source="graph_reading_transitions")) as conn2:
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
