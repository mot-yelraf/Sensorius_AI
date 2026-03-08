"""Biodynamic calendar helpers for Sensorius."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import threading
import time as time_mod
from zoneinfo import ZoneInfo

from saiSettings import saiSettings
from saiUtils import debug_enabled, printDM

MODULE = "saiBiodynamics"
DEBUG = debug_enabled(MODULE)

_SIGNS: tuple[dict[str, str], ...] = (
    {"abbr": "Ari", "name": "Aries", "element": "Fire", "plant_part": "Fruit", "color": "#d64b3b", "accent": "#ffe1dd"},
    {"abbr": "Tau", "name": "Taurus", "element": "Earth", "plant_part": "Root", "color": "#b58a57", "accent": "#f4ead9"},
    {"abbr": "Gem", "name": "Gemini", "element": "Air", "plant_part": "Flower", "color": "#d7b400", "accent": "#fff7cc"},
    {"abbr": "Cnc", "name": "Cancer", "element": "Water", "plant_part": "Leaf", "color": "#3f82d6", "accent": "#dfeeff"},
    {"abbr": "Leo", "name": "Leo", "element": "Fire", "plant_part": "Fruit", "color": "#d64b3b", "accent": "#ffe1dd"},
    {"abbr": "Vir", "name": "Virgo", "element": "Earth", "plant_part": "Root", "color": "#b58a57", "accent": "#f4ead9"},
    {"abbr": "Lib", "name": "Libra", "element": "Air", "plant_part": "Flower", "color": "#d7b400", "accent": "#fff7cc"},
    {"abbr": "Sco", "name": "Scorpio", "element": "Water", "plant_part": "Leaf", "color": "#3f82d6", "accent": "#dfeeff"},
    {"abbr": "Sgr", "name": "Sagittarius", "element": "Fire", "plant_part": "Fruit", "color": "#d64b3b", "accent": "#ffe1dd"},
    {"abbr": "Cap", "name": "Capricorn", "element": "Earth", "plant_part": "Root", "color": "#b58a57", "accent": "#f4ead9"},
    {"abbr": "Aqr", "name": "Aquarius", "element": "Air", "plant_part": "Flower", "color": "#d7b400", "accent": "#fff7cc"},
    {"abbr": "Psc", "name": "Pisces", "element": "Water", "plant_part": "Leaf", "color": "#3f82d6", "accent": "#dfeeff"},
)
_WEEKDAYS: tuple[str, ...] = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_EPHEMERIS_NAME = "de421.bsp"
_EPHEMERIS_RETRY_COOLDOWN_SEC = 900.0
_SKYFIELD_LOCK = threading.Lock()
_ephemeris_last_error = ""
_ephemeris_retry_after_monotonic = 0.0


@dataclass(frozen=True)
class _Segment:
    start_local: datetime
    end_local: datetime
    sign_index: int


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
}


def _resolve_location() -> tuple[float | None, float | None, str]:
    try:
        settings = saiSettings(apply_live=False)
        resolved = settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        lat = resolved.get("lat")
        lon = resolved.get("lon")
        tz_name = str(resolved.get("tz") or "").strip()
        return lat, lon, tz_name
    except Exception:
        return None, None, ""


def _skyfield_data_dir() -> Path:
    data_dir = Path(__file__).resolve().parent / "data" / "skyfield"
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
    abbr = _CONSTELLATION_ALIASES.get(abbr, abbr)
    idx = _SIGN_INDEX_BY_ABBR.get(abbr)
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


def _build_calendar(month_anchor: date, tzinfo: ZoneInfo, ts, eph, constellation_at, now_local: datetime) -> tuple[list[dict[str, object]], list[_Segment]]:
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
                    "end": _format_hm(overlap_end),
                    "sign": meta["name"],
                    "element": meta["element"],
                    "plant_part": meta["plant_part"],
                    "color": meta["color"],
                    "accent": meta["accent"],
                }
            )

        days.append(
            {
                "date": day_date.isoformat(),
                "day": day_date.day,
                "weekday": _WEEKDAYS[(day_date.weekday() + 1) % 7],
                "in_month": day_date.month == month_anchor.month,
                "is_today": day_date == today,
                "segments": day_segments,
                "dominant_sign": (dominant_sign or {}).get("name", ""),
                "dominant_element": (dominant_sign or {}).get("element", ""),
                "dominant_plant_part": (dominant_sign or {}).get("plant_part", ""),
                "dominant_color": (dominant_sign or {}).get("color", "#d8d8d8"),
                "dominant_accent": (dominant_sign or {}).get("accent", "#f2f2f2"),
            }
        )

    return days, segments


def get_biodynamic_payload(target_date: date | None = None) -> dict[str, object]:
    month_anchor = target_date or datetime.now().date()
    payload = _empty_payload(month_anchor)
    payload["ephemeris"] = _ephemeris_status()

    lat, lon, tz_name = _resolve_location()
    if lat is None or lon is None or not tz_name:
        payload["reason"] = "location_unavailable"
        return payload

    try:
        _, ts, eph, constellation_at = _skyfield_runtime()
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        payload["reason"] = reason
        if DEBUG:
            printDM(f"Biodynamics unavailable: {reason}", location=MODULE)
        return payload

    try:
        tzinfo = ZoneInfo(tz_name)
        now_local = datetime.now(tzinfo)
        month_days, month_segments = _build_calendar(month_anchor, tzinfo, ts, eph, constellation_at, now_local)
        current_segment = next(
            (segment for segment in month_segments if segment.start_local <= now_local < segment.end_local),
            month_segments[-1] if month_segments else None,
        )
        current_meta = _sign_meta(current_segment.sign_index) if current_segment else {}
        upcoming: list[dict[str, object]] = []
        for segment in month_segments:
            if segment.start_local <= now_local:
                continue
            meta = _sign_meta(segment.sign_index)
            upcoming.append(
                {
                    "starts_at": segment.start_local.isoformat(),
                    "start_hm": _format_hm(segment.start_local),
                    "sign": meta["name"],
                    "element": meta["element"],
                    "plant_part": meta["plant_part"],
                    "color": meta["color"],
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
                    "sign": current_meta.get("name", ""),
                    "element": current_meta.get("element", ""),
                    "plant_part": current_meta.get("plant_part", ""),
                    "color": current_meta.get("color", ""),
                    "accent": current_meta.get("accent", ""),
                    "window_start": current_segment.start_local.isoformat() if current_segment else "",
                    "window_end": current_segment.end_local.isoformat() if current_segment else "",
                    "window_start_hm": _format_hm(current_segment.start_local) if current_segment else "",
                    "window_end_hm": _format_hm(current_segment.end_local) if current_segment else "",
                    "calendar_basis": "moon apparent position classified against fixed-star constellation boundaries",
                },
                "upcoming": upcoming,
                "calendar": month_days,
                "ephemeris": _ephemeris_status(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return payload
    except Exception as exc:  # pragma: no cover - depends on installed ephemeris/runtime
        payload["reason"] = str(exc) or exc.__class__.__name__
        if DEBUG:
            printDM(f"Biodynamics calculation failed: {exc}", location=MODULE)
        return payload
