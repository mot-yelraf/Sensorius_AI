"""Biodynamic calendar payload generation and caching for Sensorius.

This module computes the biodynamic calendar data shown in the web UI and daily
summary flows. It derives zodiac sign windows, plant-part guidance, moon-node
or perigee intervals, and month-view payloads, while caching ephemeris-backed
results so dashboard and API consumers can reuse them cheaply.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from .project_paths import PROJECT_ROOT
import math
import threading
import time as time_mod
from zoneinfo import ZoneInfo

from .saiSettings import saiSettings
from .saiRuntimePaths import resolve_runtime_base_dir
from .saiUtils import debug_enabled, printDM

try:
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun
except Exception:  # pragma: no cover - optional dependency availability
    LocationInfo = None
    _astral_sun = None

MODULE = "saiBiodynamics"
DEBUG = debug_enabled(MODULE)

_SIGNS: tuple[dict[str, str], ...] = (
    {"abbr": "Ari", "name": "Aries", "element": "Fire", "plant_part": "Fruit", "color": "#f19707", "accent": "#d64b3b"},
    {"abbr": "Tau", "name": "Taurus", "element": "Earth", "plant_part": "Root", "color": "#e5b172", "accent": "#644817"},
    {"abbr": "Gem", "name": "Gemini", "element": "Air", "plant_part": "Flower", "color": "#F7E605", "accent": "#C4DCF8"},
    {"abbr": "Cnc", "name": "Cancer", "element": "Water", "plant_part": "Leaf", "color": "#277A00", "accent": "#2f6eb8"},
    {"abbr": "Leo", "name": "Leo", "element": "Fire", "plant_part": "Fruit", "color": "#f19707", "accent": "#d64b3b"},
    {"abbr": "Vir", "name": "Virgo", "element": "Earth", "plant_part": "Root", "color": "#e5b172", "accent": "#644817"},
    {"abbr": "Lib", "name": "Libra", "element": "Air", "plant_part": "Flower", "color": "#F7E605", "accent": "#C4DCF8"},
    {"abbr": "Sco", "name": "Scorpio", "element": "Water", "plant_part": "Leaf", "color": "#277A00", "accent": "#2f6eb8"},
    {"abbr": "Sgr", "name": "Sagittarius", "element": "Fire", "plant_part": "Fruit", "color": "#f19707", "accent": "#d64b3b"},
    {"abbr": "Cap", "name": "Capricorn", "element": "Earth", "plant_part": "Root", "color": "#e5b172", "accent": "#644817"},
    {"abbr": "Aqr", "name": "Aquarius", "element": "Air", "plant_part": "Flower", "color": "#F7E605", "accent": "#C4DCF8"},
    {"abbr": "Psc", "name": "Pisces", "element": "Water", "plant_part": "Leaf", "color": "#277A00", "accent": "#2f6eb8"},
)
_WEEKDAYS: tuple[str, ...] = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_EPHEMERIS_NAME = "de421.bsp"
_EPHEMERIS_RETRY_COOLDOWN_SEC = 900.0
_SKYFIELD_LOCK = threading.Lock()
_ephemeris_last_error = ""
_ephemeris_retry_after_monotonic = 0.0
_OFF_PERIOD_COLOR = "#5f6770"
_OFF_PERIOD_ACCENT = "#d7dbe0"
_MOON_NODE_WINDOW = timedelta(hours=2)
_PERIGEE_WINDOW = timedelta(hours=12)
_APOGEE_WINDOW = timedelta(hours=12)
_OFF_PERIOD_LABELS = {
    "lunar_node": "Lunar Node",
    "perigee": "Perigee",
    "apogee": "Apogee",
}
_OFF_OVERLAY_KINDS = {"lunar_node", "perigee"}
_PAYLOAD_CACHE_TTL_SEC = 300.0
_PAYLOAD_DISK_CACHE_VERSION = 3
_PAYLOAD_DISK_CACHE_MAX_AGE_SEC = 120 * 86400
_PAYLOAD_DISK_CACHE_CLEANUP_INTERVAL_SEC = 3600.0
_PAYLOAD_DISK_CACHE_ENV = "SENSORIUS_BIODYNAMIC_CACHE_DIR"
_PAYLOAD_CACHE: dict[tuple[str, str, str, str, str], tuple[float, dict[str, object]]] = {}
_PAYLOAD_CACHE_LOCK = threading.Lock()
_PAYLOAD_BUILD_LOCKS: dict[tuple[str, str, str, str, str], threading.Lock] = {}
_payload_disk_cache_cleanup_after = 0.0


def _month_start(anchor: date) -> date:
    return anchor.replace(day=1)


def shift_biodynamic_month(anchor: date, month_delta: int) -> date:
    """Return the month-start date offset by ``month_delta`` months."""
    month_index = (anchor.year * 12) + (anchor.month - 1) + int(month_delta)
    year, month_zero = divmod(month_index, 12)
    return date(year, month_zero + 1, 1)


def biodynamic_prewarm_month_anchors(
    anchor: date | None = None,
    *,
    past_months: int = 3,
    future_months: int = 12,
) -> list[date]:
    """Return month anchors in the order background warmup should build them."""
    base = _month_start(anchor or get_biodynamic_local_now().date())
    try:
        past_count = max(int(past_months), 0)
    except Exception:
        past_count = 0
    try:
        future_count = max(int(future_months), 0)
    except Exception:
        future_count = 0
    offsets: list[int] = [0]
    if past_count > 0:
        offsets.append(-1)
    if future_count > 0:
        offsets.append(1)
    offsets.extend(range(2, future_count + 1))
    offsets.extend(range(-2, -past_count - 1, -1))

    seen: set[date] = set()
    anchors: list[date] = []
    for offset in offsets:
        month_anchor = shift_biodynamic_month(base, offset)
        if month_anchor in seen:
            continue
        seen.add(month_anchor)
        anchors.append(month_anchor)
    return anchors


def clear_biodynamic_payload_cache() -> None:
    """Clear cached biodynamic payloads after location or timezone changes."""
    with _PAYLOAD_CACHE_LOCK:
        _PAYLOAD_CACHE.clear()


def _clone_payload(payload: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(payload)


def _payload_cache_key(
    month_anchor: date,
    lat: float,
    lon: float,
    tz_name: str,
    altitude: float | None,
) -> tuple[str, str, str, str, str]:
    return (
        month_anchor.replace(day=1).isoformat(),
        str(round(float(lat), 4)),
        str(round(float(lon), 4)),
        str(tz_name),
        "" if altitude is None else str(round(float(altitude), 1)),
    )


def _payload_build_lock(cache_key: tuple[str, str, str, str, str]) -> threading.Lock:
    with _PAYLOAD_CACHE_LOCK:
        lock = _PAYLOAD_BUILD_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _PAYLOAD_BUILD_LOCKS[cache_key] = lock
        return lock


def _payload_disk_cache_dir() -> Path:
    override = os.getenv(_PAYLOAD_DISK_CACHE_ENV, "").strip()
    if override:
        cache_dir = Path(override).expanduser()
    else:
        runtime_root = resolve_runtime_base_dir(saiSettings.DEFAULT_BASE_DIR).parent
        cache_dir = runtime_root / "cache" / "biodynamic"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _payload_disk_cache_path(cache_key: tuple[str, str, str, str, str]) -> Path:
    key_payload = {
        "version": _PAYLOAD_DISK_CACHE_VERSION,
        "ephemeris": _EPHEMERIS_NAME,
        "key": list(cache_key),
    }
    digest = hashlib.sha256(json.dumps(key_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return _payload_disk_cache_dir() / f"{digest}.json"


def _cleanup_payload_disk_cache(now_wall: float | None = None) -> None:
    global _payload_disk_cache_cleanup_after
    now_wall = float(now_wall if now_wall is not None else time_mod.time())
    if now_wall < _payload_disk_cache_cleanup_after:
        return
    _payload_disk_cache_cleanup_after = now_wall + _PAYLOAD_DISK_CACHE_CLEANUP_INTERVAL_SEC
    try:
        cutoff = now_wall - _PAYLOAD_DISK_CACHE_MAX_AGE_SEC
        cache_dir = _payload_disk_cache_dir()
        for path in cache_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except Exception:
                continue
    except Exception:
        pass


def _read_payload_disk_cache(cache_key: tuple[str, str, str, str, str]) -> dict[str, object] | None:
    try:
        path = _payload_disk_cache_path(cache_key)
        if not path.exists():
            return None
        try:
            if path.stat().st_mtime < time_mod.time() - _PAYLOAD_DISK_CACHE_MAX_AGE_SEC:
                try:
                    path.unlink()
                except Exception:
                    pass
                return None
        except OSError:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if int(raw.get("version") or 0) != _PAYLOAD_DISK_CACHE_VERSION:
            return None
        if str(raw.get("ephemeris") or "") != _EPHEMERIS_NAME:
            return None
        if list(raw.get("key") or []) != list(cache_key):
            return None
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return None
        return _clone_payload(payload)
    except Exception as exc:
        if DEBUG:
            printDM(f"Biodynamic disk cache read skipped: {exc}", location=MODULE)
        return None


def _write_payload_disk_cache(cache_key: tuple[str, str, str, str, str], payload: dict[str, object]) -> None:
    try:
        now_wall = time_mod.time()
        _cleanup_payload_disk_cache(now_wall)
        path = _payload_disk_cache_path(cache_key)
        tmp_path = path.with_suffix(f".{os.getpid()}.tmp")
        body = {
            "version": _PAYLOAD_DISK_CACHE_VERSION,
            "ephemeris": _EPHEMERIS_NAME,
            "key": list(cache_key),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        tmp_path.write_text(json.dumps(body, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as exc:
        if DEBUG:
            printDM(f"Biodynamic disk cache write skipped: {exc}", location=MODULE)


def _refresh_dynamic_payload_fields(
    payload: dict[str, object],
    month_anchor: date,
    tzinfo: ZoneInfo,
    now_local: datetime,
    ts=None,
    eph=None,
) -> dict[str, object]:
    refreshed = _clone_payload(payload)
    calendar_days = []
    for day in list(refreshed.get("calendar") or []):
        if not isinstance(day, dict):
            continue
        day_copy = dict(day)
        try:
            day_copy["is_today"] = date.fromisoformat(str(day_copy.get("date") or "")) == now_local.date()
        except Exception:
            day_copy["is_today"] = False
        calendar_days.append(day_copy)
    refreshed["calendar"] = calendar_days

    timeline = _build_segment_timeline(calendar_days, tzinfo)
    current_segment = next(
        (segment for segment in timeline if segment["start_local"] <= now_local < segment["end_local"]),
        timeline[-1] if timeline else None,
    )
    upcoming: list[dict[str, object]] = []
    for segment in timeline:
        if segment["start_local"] <= now_local:
            continue
        upcoming.append(
            {
                "starts_at": segment["start_local"].isoformat(),
                "start_hm": segment["start_hm"],
                "sign": segment["sign"],
                "element": segment["element"],
                "plant_part": segment["plant_part"],
                "color": segment["color"],
                "accent": segment["accent"],
                "kind": segment["kind"],
                "off_kind": segment["off_kind"],
                "off_label": segment["off_label"],
            }
        )
        if len(upcoming) >= 3:
            break

    moon_direction = ""
    if ts is not None and eph is not None:
        try:
            moon_direction = _moon_direction(now_local, ts, eph)
        except Exception:
            moon_direction = ""
    if not moon_direction:
        try:
            moon_direction = str((refreshed.get("current") or {}).get("moon_direction") or "")
        except Exception:
            moon_direction = ""

    refreshed["current"] = {
        "timestamp": now_local.isoformat(),
        "sign": str(current_segment.get("sign") or "") if current_segment else "",
        "element": str(current_segment.get("element") or "") if current_segment else "",
        "plant_part": str(current_segment.get("plant_part") or "") if current_segment else "",
        "color": str(current_segment.get("color") or "") if current_segment else "",
        "accent": str(current_segment.get("accent") or "") if current_segment else "",
        "kind": str(current_segment.get("kind") or "") if current_segment else "",
        "off_kind": str(current_segment.get("off_kind") or "") if current_segment else "",
        "off_label": str(current_segment.get("off_label") or "") if current_segment else "",
        "moon_direction": moon_direction,
        "window_start": current_segment["start_local"].isoformat() if current_segment else "",
        "window_end": current_segment["end_local"].isoformat() if current_segment else "",
        "window_start_hm": str(current_segment.get("start_hm") or "") if current_segment else "",
        "window_end_hm": str(current_segment.get("end_hm") or "") if current_segment else "",
        "calendar_basis": "moon apparent position classified against fixed-star constellation boundaries",
    }
    refreshed["upcoming"] = upcoming
    refreshed["month_label"] = month_anchor.strftime("%B %Y")
    refreshed["generated_at"] = datetime.now(timezone.utc).isoformat()
    return refreshed


@dataclass(frozen=True)
class _Segment:
    start_local: datetime
    end_local: datetime
    sign_index: int


@dataclass(frozen=True)
class _Interval:
    start_local: datetime
    end_local: datetime
    kind: str


def _empty_payload(month_date: date | None = None) -> dict[str, object]:
    month_label = ""
    if isinstance(month_date, date):
        month_label = month_date.strftime("%B %Y")
    return {
        "ok": False,
        "reason": "unavailable",
        "tz": "",
        "source": "skyfield",
        "month_label": month_label,
        "weekday_labels": list(_WEEKDAYS),
        "current": {},
        "upcoming": [],
        "calendar": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sign_meta(sign_index: int) -> dict[str, object]:
    meta = dict(_SIGNS[sign_index % len(_SIGNS)])
    meta["sign_index"] = int(sign_index % len(_SIGNS))
    return meta


_SIGN_INDEX_BY_ABBR: dict[str, int] = {
    str(item["abbr"]): idx for idx, item in enumerate(_SIGNS)
}
_CONSTELLATION_ALIASES: dict[str, str] = {
    # Maria Thun-style biodynamic calendars use a 12-constellation zodiac.
    # IAU boundaries can place the Moon briefly in adjacent edge constellations.
    "Oph": "Sco",
    "Cet": "Psc",
    "Aur": "Tau",
    "Ori": "Tau",
    "Sex": "Leo",
}


def _safe_float(value) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _resolve_location() -> tuple[float | None, float | None, str, float | None]:
    try:
        settings = saiSettings(apply_live=False)
        resolved = settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        lat = resolved.get("lat")
        lon = resolved.get("lon")
        tz_name = str(resolved.get("tz") or "").strip()
        altitude = _safe_float(resolved.get("altitude"))
        return lat, lon, tz_name, altitude
    except Exception:
        return None, None, "", None


def get_biodynamic_local_now() -> datetime:
    _lat, _lon, tz_name, _altitude = _resolve_location()
    try:
        tzinfo = ZoneInfo(str(tz_name or "").strip() or "America/Denver")
    except Exception:
        tzinfo = ZoneInfo("America/Denver")
    return datetime.now(tzinfo)


def _skyfield_data_dir() -> Path:
    data_dir = PROJECT_ROOT / "data" / "skyfield"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _ephemeris_path() -> Path:
    return _skyfield_data_dir() / _EPHEMERIS_NAME


def _ephemeris_status() -> dict[str, object]:
    ephemeris_path = _ephemeris_path()
    retry_after = max(0.0, _ephemeris_retry_after_monotonic - time_mod.monotonic())
    return {
        "name": _EPHEMERIS_NAME,
        "path": str(ephemeris_path),
        "installed": ephemeris_path.exists(),
        "last_error": _ephemeris_last_error,
        "retry_after_sec": int(round(retry_after)),
    }


def get_skyfield_runtime_if_installed() -> tuple[object, object, object, object] | None:
    """Return the cached Skyfield runtime only when ephemeris data is local."""
    if not _ephemeris_path().exists():
        return None
    try:
        return _skyfield_runtime()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _skyfield_runtime() -> tuple[object, object, object, object]:
    global _ephemeris_last_error, _ephemeris_retry_after_monotonic
    try:
        from skyfield.api import Loader, load_constellation_map
    except Exception as exc:  # pragma: no cover - import error depends on env
        raise RuntimeError("skyfield_not_installed") from exc

    with _SKYFIELD_LOCK:
        data_dir = _skyfield_data_dir()
        loader = Loader(str(data_dir), verbose=False)
        ts = loader.timescale()
        ephemeris_path = _ephemeris_path()

        if not ephemeris_path.exists():
            now_mono = time_mod.monotonic()
            if now_mono < _ephemeris_retry_after_monotonic:
                retry_sec = int(round(_ephemeris_retry_after_monotonic - now_mono))
                raise RuntimeError(f"ephemeris_download_deferred:{retry_sec}s")
            try:
                if DEBUG:
                    printDM(f"Downloading {_EPHEMERIS_NAME} to {ephemeris_path}", location=MODULE)
                eph = loader(_EPHEMERIS_NAME)
                constellation_at = load_constellation_map()
                _ephemeris_last_error = ""
                _ephemeris_retry_after_monotonic = 0.0
                return loader, ts, eph, constellation_at
            except Exception as exc:
                _ephemeris_last_error = exc.__class__.__name__
                _ephemeris_retry_after_monotonic = now_mono + _EPHEMERIS_RETRY_COOLDOWN_SEC
                raise RuntimeError(f"ephemeris_download_failed:{_ephemeris_last_error}") from exc

        eph = loader(_EPHEMERIS_NAME)
        constellation_at = load_constellation_map()
        _ephemeris_last_error = ""
        _ephemeris_retry_after_monotonic = 0.0
        return loader, ts, eph, constellation_at


def _moon_sign_index(dt_local: datetime, ts, eph, constellation_at) -> int:
    moon = eph["moon"]
    earth = eph["earth"]
    t = ts.from_datetime(dt_local.astimezone(timezone.utc))
    apparent = earth.at(t).observe(moon).apparent()
    abbr = str(constellation_at(apparent))
    idx = _biodynamic_sign_index_for_constellation(abbr)
    return idx


def _biodynamic_sign_index_for_constellation(abbr: str) -> int:
    mapped_abbr = _CONSTELLATION_ALIASES.get(str(abbr), str(abbr))
    idx = _SIGN_INDEX_BY_ABBR.get(mapped_abbr)
    if idx is None:
        raise RuntimeError(f"unsupported_constellation:{abbr}")
    return idx


def _refine_transition(start_local: datetime, end_local: datetime, start_sign: int, ts, eph, constellation_at) -> datetime:
    lo = start_local
    hi = end_local
    for _ in range(20):
        span = (hi - lo).total_seconds()
        if span <= 60.0:
            break
        mid = lo + timedelta(seconds=span / 2.0)
        if _moon_sign_index(mid, ts, eph, constellation_at) == start_sign:
            lo = mid
        else:
            hi = mid
    return hi


def _split_segments(start_local: datetime, end_local: datetime, ts, eph, constellation_at) -> list[_Segment]:
    segments: list[_Segment] = []
    if start_local >= end_local:
        return segments

    current_start = start_local
    current_sign = _moon_sign_index(current_start, ts, eph, constellation_at)
    probe_prev = current_start
    probe = current_start + timedelta(hours=1)

    while probe < end_local:
        probe_sign = _moon_sign_index(probe, ts, eph, constellation_at)
        if probe_sign != current_sign:
            transition = _refine_transition(probe_prev, probe, current_sign, ts, eph, constellation_at)
            segments.append(_Segment(current_start, transition, current_sign))
            current_start = transition
            current_sign = _moon_sign_index(transition + timedelta(minutes=1), ts, eph, constellation_at)
        probe_prev = probe
        probe = probe + timedelta(hours=1)

    segments.append(_Segment(current_start, end_local, current_sign))
    return segments


def _format_hm(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M")


def _moon_latitude_deg(dt_local: datetime, ts, eph) -> float:
    from skyfield.framelib import ecliptic_frame

    moon = eph["moon"]
    earth = eph["earth"]
    t = ts.from_datetime(dt_local.astimezone(timezone.utc))
    apparent = earth.at(t).observe(moon).apparent()
    lat, _, _ = apparent.frame_latlon(ecliptic_frame)
    return float(lat.degrees)


def _moon_distance_km(dt_local: datetime, ts, eph) -> float:
    moon = eph["moon"]
    earth = eph["earth"]
    t = ts.from_datetime(dt_local.astimezone(timezone.utc))
    return float(earth.at(t).observe(moon).distance().km)


def _moon_declination_deg(dt_local: datetime, ts, eph) -> float:
    moon = eph["moon"]
    earth = eph["earth"]
    t = ts.from_datetime(dt_local.astimezone(timezone.utc))
    apparent = earth.at(t).observe(moon).apparent()
    _ra, dec, _distance = apparent.radec()
    return float(dec.degrees)


def _moon_direction(dt_local: datetime, ts, eph) -> str:
    try:
        before = _moon_declination_deg(dt_local - timedelta(hours=6), ts, eph)
        after = _moon_declination_deg(dt_local + timedelta(hours=6), ts, eph)
    except Exception:
        return ""
    return "ascending" if after >= before else "descending"


def _refine_node_crossing(lo: datetime, hi: datetime, ts, eph) -> datetime:
    lo_val = _moon_latitude_deg(lo, ts, eph)
    hi_val = _moon_latitude_deg(hi, ts, eph)
    for _ in range(24):
        span = (hi - lo).total_seconds()
        if span <= 60.0:
            break
        mid = lo + timedelta(seconds=span / 2.0)
        mid_val = _moon_latitude_deg(mid, ts, eph)
        if mid_val == 0.0:
            return mid
        if math.copysign(1.0, lo_val or 1.0) == math.copysign(1.0, mid_val or 1.0):
            lo = mid
            lo_val = mid_val
        else:
            hi = mid
            hi_val = mid_val
    return hi


def _refine_distance_extreme(center: datetime, ts, eph, *, find_min: bool) -> datetime:
    lo = center - timedelta(hours=6)
    hi = center + timedelta(hours=6)
    for _ in range(24):
        if (hi - lo).total_seconds() <= 60.0:
            break
        third = (hi - lo) / 3
        m1 = lo + third
        m2 = hi - third
        d1 = _moon_distance_km(m1, ts, eph)
        d2 = _moon_distance_km(m2, ts, eph)
        prefer_left = d1 <= d2 if find_min else d1 >= d2
        if prefer_left:
            hi = m2
        else:
            lo = m1
    return lo + ((hi - lo) / 2)


def _refine_perigee(center: datetime, ts, eph) -> datetime:
    return _refine_distance_extreme(center, ts, eph, find_min=True)


def _refine_apogee(center: datetime, ts, eph) -> datetime:
    return _refine_distance_extreme(center, ts, eph, find_min=False)


def _build_off_intervals(start_local: datetime, end_local: datetime, ts, eph) -> list[_Interval]:
    intervals: list[_Interval] = []
    if start_local >= end_local:
        return intervals

    probe = start_local - timedelta(hours=24)
    probe_end = end_local + timedelta(hours=24)
    step = timedelta(hours=1)
    prev = probe
    prev_lat = _moon_latitude_deg(prev, ts, eph)
    prev_prev_dist = None
    prev_dist = _moon_distance_km(prev, ts, eph)
    cur = prev + step

    while cur <= probe_end:
        cur_lat = _moon_latitude_deg(cur, ts, eph)
        cur_dist = _moon_distance_km(cur, ts, eph)

        if (prev_lat == 0.0) or (cur_lat == 0.0) or (prev_lat < 0.0 < cur_lat) or (prev_lat > 0.0 > cur_lat):
            event = _refine_node_crossing(prev, cur, ts, eph)
            intervals.append(_Interval(event - _MOON_NODE_WINDOW, event + _MOON_NODE_WINDOW, "lunar_node"))

        if prev_prev_dist is not None and prev_dist <= prev_prev_dist and prev_dist <= cur_dist:
            event = _refine_perigee(prev, ts, eph)
            intervals.append(_Interval(event - _PERIGEE_WINDOW, event + _PERIGEE_WINDOW, "perigee"))
        elif prev_prev_dist is not None and prev_dist >= prev_prev_dist and prev_dist >= cur_dist:
            event = _refine_apogee(prev, ts, eph)
            intervals.append(_Interval(event - _APOGEE_WINDOW, event + _APOGEE_WINDOW, "apogee"))

        prev = cur
        prev_lat = cur_lat
        prev_prev_dist = prev_dist
        prev_dist = cur_dist
        cur = cur + step

    filtered = [
        _Interval(max(iv.start_local, start_local), min(iv.end_local, end_local), iv.kind)
        for iv in intervals
        if iv.end_local > start_local and iv.start_local < end_local
    ]
    filtered.sort(key=lambda iv: iv.start_local)

    merged: list[_Interval] = []
    for iv in filtered:
        if not merged or iv.start_local > merged[-1].end_local or iv.kind != merged[-1].kind:
            merged.append(iv)
        else:
            last = merged[-1]
            merged[-1] = _Interval(last.start_local, max(last.end_local, iv.end_local), last.kind)
    return merged


def _apply_off_overlays(day_segments: list[dict[str, object]], day_start: datetime, day_end: datetime, off_intervals: list[_Interval]) -> list[dict[str, object]]:
    segments = list(day_segments)
    for off in off_intervals:
        overlap_start = max(off.start_local, day_start)
        overlap_end = min(off.end_local, day_end)
        if overlap_end <= overlap_start:
            continue
        next_segments: list[dict[str, object]] = []
        for seg in segments:
            seg_start = datetime.combine(day_start.date(), time.min, tzinfo=day_start.tzinfo) + timedelta(minutes=int(str(seg.get("start", "00:00")).split(":")[0]) * 60 + int(str(seg.get("start", "00:00")).split(":")[1]))
            seg_end_raw = str(seg.get("end", "24:00"))
            if seg_end_raw == "24:00":
                seg_end = day_end
            else:
                seg_end = datetime.combine(day_start.date(), time.min, tzinfo=day_start.tzinfo) + timedelta(minutes=int(seg_end_raw.split(":")[0]) * 60 + int(seg_end_raw.split(":")[1]))
            if seg_end <= overlap_start or seg_start >= overlap_end:
                next_segments.append(seg)
                continue
            if seg_start < overlap_start:
                left = dict(seg)
                left["start"] = _format_hm(seg_start)
                left["end"] = _format_hm(overlap_start)
                next_segments.append(left)
            mid = dict(seg)
            mid["start"] = _format_hm(max(seg_start, overlap_start))
            mid["end"] = "24:00" if overlap_end >= day_end else _format_hm(overlap_end)
            mid["kind"] = "off"
            mid["off_kind"] = off.kind
            mid["off_label"] = _OFF_PERIOD_LABELS.get(off.kind, "Rest")
            mid["sign"] = "Rest"
            mid["element"] = "Pause"
            mid["plant_part"] = "Rest"
            mid["color"] = _OFF_PERIOD_COLOR
            mid["accent"] = _OFF_PERIOD_ACCENT
            next_segments.append(mid)
            if seg_end > overlap_end:
                right = dict(seg)
                right["start"] = _format_hm(overlap_end)
                right["end"] = "24:00" if seg_end >= day_end else _format_hm(seg_end)
                next_segments.append(right)
        segments = next_segments
    return segments


def _lunar_flags_for_day(day_start: datetime, day_end: datetime, off_intervals: list[_Interval]) -> dict[str, object]:
    events: list[dict[str, str]] = []
    flags = {
        "lunar_node": False,
        "perigee": False,
        "apogee": False,
    }
    for interval in off_intervals:
        overlap_start = max(interval.start_local, day_start)
        overlap_end = min(interval.end_local, day_end)
        if overlap_end <= overlap_start:
            continue
        if interval.kind in flags:
            flags[interval.kind] = True
        events.append(
            {
                "type": interval.kind,
                "label": _OFF_PERIOD_LABELS.get(interval.kind, "Rest"),
                "start": _format_hm(overlap_start),
                "end": "24:00" if overlap_end >= day_end else _format_hm(overlap_end),
            }
        )
    return {
        "lunar_node": flags["lunar_node"],
        "perigee": flags["perigee"],
        "apogee": flags["apogee"],
        "lunar_events": events,
    }


def _daylight_for_day(
    day_date: date,
    tzinfo: ZoneInfo,
    lat: float,
    lon: float,
    altitude: float | None,
) -> dict[str, object]:
    out = {
        "sunrise": "",
        "sunset": "",
        "daylight_minutes": None,
        "daylight_label": "",
    }
    if LocationInfo is None or _astral_sun is None:
        return out
    try:
        loc = LocationInfo(
            name="Sensorius",
            region="local",
            timezone=tzinfo.key,
            latitude=float(lat),
            longitude=float(lon),
        )
        observer = loc.observer
        if altitude is not None:
            observer.elevation = altitude
        sun_map = _astral_sun(observer, date=day_date, tzinfo=tzinfo)
        sunrise = sun_map.get("sunrise")
        sunset = sun_map.get("sunset")
        if not isinstance(sunrise, datetime) or not isinstance(sunset, datetime):
            return out
        out["sunrise"] = _format_hm(sunrise)
        out["sunset"] = _format_hm(sunset)
        if sunset < sunrise:
            return out
        total_min = max(0, int((sunset - sunrise).total_seconds() // 60))
        out["daylight_minutes"] = total_min
        out["daylight_label"] = f"{total_min // 60} Hrs {total_min % 60} Mins"
        return out
    except Exception:
        return out


def _segment_bounds_for_day(day_date: date, seg: dict[str, object], tzinfo: ZoneInfo) -> tuple[datetime, datetime] | None:
    start_raw = str(seg.get("start", "00:00"))
    end_raw = str(seg.get("end", "24:00"))
    try:
        start_h, start_m = [int(x) for x in start_raw.split(":", 1)]
        start_dt = datetime.combine(day_date, time.min, tzinfo=tzinfo) + timedelta(hours=start_h, minutes=start_m)
        if end_raw == "24:00":
            end_dt = datetime.combine(day_date, time.min, tzinfo=tzinfo) + timedelta(days=1)
        else:
            end_h, end_m = [int(x) for x in end_raw.split(":", 1)]
            end_dt = datetime.combine(day_date, time.min, tzinfo=tzinfo) + timedelta(hours=end_h, minutes=end_m)
        if end_dt <= start_dt:
            return None
        return start_dt, end_dt
    except Exception:
        return None


def _build_segment_timeline(days: list[dict[str, object]], tzinfo: ZoneInfo) -> list[dict[str, object]]:
    timeline: list[dict[str, object]] = []
    for day in days:
        try:
            day_date = date.fromisoformat(str(day.get("date") or ""))
        except Exception:
            continue
        for seg in list(day.get("segments") or []):
            bounds = _segment_bounds_for_day(day_date, seg, tzinfo)
            if not bounds:
                continue
            start_dt, end_dt = bounds
            timeline.append(
                {
                    "start_local": start_dt,
                    "end_local": end_dt,
                    "start_hm": _format_hm(start_dt),
                    "end_hm": _format_hm(end_dt) if end_dt.date() == day_date else "24:00",
                    "sign": str(seg.get("sign") or ""),
                    "element": str(seg.get("element") or ""),
                    "plant_part": str(seg.get("plant_part") or ""),
                    "color": str(seg.get("color") or ""),
                    "accent": str(seg.get("accent") or ""),
                    "kind": str(seg.get("kind") or "sign"),
                    "off_kind": str(seg.get("off_kind") or ""),
                    "off_label": str(seg.get("off_label") or ""),
                }
            )
    timeline.sort(key=lambda item: item["start_local"])
    return timeline


def _build_calendar(
    month_anchor: date,
    tzinfo: ZoneInfo,
    ts,
    eph,
    constellation_at,
    now_local: datetime,
    lat: float,
    lon: float,
    altitude: float | None,
) -> tuple[list[dict[str, object]], list[_Segment]]:
    month_start = datetime.combine(month_anchor.replace(day=1), time.min, tzinfo=tzinfo)
    if month_anchor.month == 12:
        next_month = month_anchor.replace(year=month_anchor.year + 1, month=1, day=1)
    else:
        next_month = month_anchor.replace(month=month_anchor.month + 1, day=1)
    month_end = datetime.combine(next_month, time.min, tzinfo=tzinfo)

    grid_start = month_start - timedelta(days=(month_start.weekday() + 1) % 7)
    grid_end = grid_start + timedelta(days=42)
    grid_end_dt = datetime.combine(grid_end, time.min, tzinfo=tzinfo)

    segments = _split_segments(grid_start, grid_end_dt, ts, eph, constellation_at)
    off_intervals = _build_off_intervals(grid_start, grid_end_dt, ts, eph)
    days: list[dict[str, object]] = []
    today = now_local.date()

    for day_index in range(42):
        day_date = grid_start.date() + timedelta(days=day_index)
        day_start = datetime.combine(day_date, time.min, tzinfo=tzinfo)
        day_end = day_start + timedelta(days=1)
        day_segments: list[dict[str, object]] = []
        dominant_sign = None
        dominant_seconds = -1.0

        for segment in segments:
            overlap_start = max(segment.start_local, day_start)
            overlap_end = min(segment.end_local, day_end)
            if overlap_end <= overlap_start:
                continue
            span_sec = (overlap_end - overlap_start).total_seconds()
            meta = _sign_meta(segment.sign_index)
            if span_sec > dominant_seconds:
                dominant_seconds = span_sec
                dominant_sign = meta
            day_segments.append(
                {
                    "start": _format_hm(overlap_start),
                    "end": "24:00" if overlap_end >= day_end else _format_hm(overlap_end),
                    "sign": meta["name"],
                    "element": meta["element"],
                    "plant_part": meta["plant_part"],
                    "color": meta["color"],
                    "accent": meta["accent"],
                    "kind": "sign",
                }
            )

        overlay_intervals = [iv for iv in off_intervals if iv.kind in _OFF_OVERLAY_KINDS]
        day_segments = _apply_off_overlays(day_segments, day_start, day_end, overlay_intervals)
        lunar_flags = _lunar_flags_for_day(day_start, day_end, off_intervals)

        day_payload = {
            "date": day_date.isoformat(),
            "day": day_date.day,
            "weekday": _WEEKDAYS[(day_date.weekday() + 1) % 7],
            "in_month": day_date.month == month_anchor.month,
            "is_today": day_date == today,
            "segments": day_segments,
            "dominant_sign": (dominant_sign or {}).get("name", ""),
            "dominant_sign_abbr": (dominant_sign or {}).get("abbr", ""),
            "dominant_element": (dominant_sign or {}).get("element", ""),
            "dominant_plant_part": (dominant_sign or {}).get("plant_part", ""),
            "dominant_color": (dominant_sign or {}).get("color", "#d8d8d8"),
            "dominant_accent": (dominant_sign or {}).get("accent", "#f2f2f2"),
            "moon_direction": _moon_direction(day_start + timedelta(hours=12), ts, eph),
        }
        day_payload.update(lunar_flags)
        day_payload.update(_daylight_for_day(day_date, tzinfo, lat, lon, altitude))
        days.append(day_payload)

    return days, segments


def get_biodynamic_payload(target_date: date | None = None) -> dict[str, object]:
    month_anchor = target_date or get_biodynamic_local_now().date()
    payload = _empty_payload(month_anchor)
    payload["ephemeris"] = _ephemeris_status()

    lat, lon, tz_name, altitude = _resolve_location()
    if lat is None or lon is None or not tz_name:
        payload["reason"] = "location_unavailable"
        return payload

    cache_key = _payload_cache_key(month_anchor, float(lat), float(lon), str(tz_name), altitude)
    now_mono = time_mod.monotonic()
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        payload["reason"] = "invalid_timezone"
        return payload
    now_local = datetime.now(tzinfo)

    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(cache_key)
        if cached and cached[0] > now_mono:
            return _refresh_dynamic_payload_fields(
                cached[1],
                month_anchor,
                tzinfo,
                now_local,
            )

    build_lock = _payload_build_lock(cache_key)
    with build_lock:
        now_mono = time_mod.monotonic()
        with _PAYLOAD_CACHE_LOCK:
            cached = _PAYLOAD_CACHE.get(cache_key)
            if cached and cached[0] > now_mono:
                return _refresh_dynamic_payload_fields(
                    cached[1],
                    month_anchor,
                    tzinfo,
                    now_local,
                )

        disk_payload = _read_payload_disk_cache(cache_key)
        if disk_payload is not None:
            refreshed = _refresh_dynamic_payload_fields(disk_payload, month_anchor, tzinfo, now_local)
            with _PAYLOAD_CACHE_LOCK:
                _PAYLOAD_CACHE[cache_key] = (
                    time_mod.monotonic() + _PAYLOAD_CACHE_TTL_SEC,
                    _clone_payload(refreshed),
                )
            return refreshed

        try:
            _, ts, eph, constellation_at = _skyfield_runtime()
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            payload["reason"] = reason
            if DEBUG:
                printDM(f"Biodynamics unavailable: {reason}", location=MODULE)
            return payload

        try:
            month_days, _month_segments = _build_calendar(
                month_anchor,
                tzinfo,
                ts,
                eph,
                constellation_at,
                now_local,
                float(lat),
                float(lon),
                altitude,
            )
            timeline = _build_segment_timeline(month_days, tzinfo)
            current_segment = next(
                (segment for segment in timeline if segment["start_local"] <= now_local < segment["end_local"]),
                timeline[-1] if timeline else None,
            )
            upcoming: list[dict[str, object]] = []
            for segment in timeline:
                if segment["start_local"] <= now_local:
                    continue
                upcoming.append(
                    {
                        "starts_at": segment["start_local"].isoformat(),
                        "start_hm": segment["start_hm"],
                        "sign": segment["sign"],
                        "element": segment["element"],
                        "plant_part": segment["plant_part"],
                        "color": segment["color"],
                        "accent": segment["accent"],
                        "kind": segment["kind"],
                        "off_kind": segment["off_kind"],
                        "off_label": segment["off_label"],
                    }
                )
                if len(upcoming) >= 3:
                    break

            payload.update(
                {
                    "ok": True,
                    "reason": "",
                    "tz": tz_name,
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                    "month_label": month_anchor.strftime("%B %Y"),
                    "current": {
                        "timestamp": now_local.isoformat(),
                        "sign": str(current_segment.get("sign") or "") if current_segment else "",
                        "element": str(current_segment.get("element") or "") if current_segment else "",
                        "plant_part": str(current_segment.get("plant_part") or "") if current_segment else "",
                        "color": str(current_segment.get("color") or "") if current_segment else "",
                        "accent": str(current_segment.get("accent") or "") if current_segment else "",
                        "kind": str(current_segment.get("kind") or "") if current_segment else "",
                        "off_kind": str(current_segment.get("off_kind") or "") if current_segment else "",
                        "off_label": str(current_segment.get("off_label") or "") if current_segment else "",
                        "moon_direction": _moon_direction(now_local, ts, eph),
                        "window_start": current_segment["start_local"].isoformat() if current_segment else "",
                        "window_end": current_segment["end_local"].isoformat() if current_segment else "",
                        "window_start_hm": str(current_segment.get("start_hm") or "") if current_segment else "",
                        "window_end_hm": str(current_segment.get("end_hm") or "") if current_segment else "",
                        "calendar_basis": "moon apparent position classified against fixed-star constellation boundaries",
                    },
                    "upcoming": upcoming,
                    "calendar": month_days,
                    "ephemeris": _ephemeris_status(),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            with _PAYLOAD_CACHE_LOCK:
                _PAYLOAD_CACHE[cache_key] = (
                    time_mod.monotonic() + _PAYLOAD_CACHE_TTL_SEC,
                    _clone_payload(payload),
                )
            _write_payload_disk_cache(cache_key, payload)
            return payload
        except Exception as exc:  # pragma: no cover - depends on installed ephemeris/runtime
            payload["reason"] = str(exc) or exc.__class__.__name__
            if DEBUG:
                printDM(f"Biodynamics calculation failed: {exc}", location=MODULE)
            return payload
