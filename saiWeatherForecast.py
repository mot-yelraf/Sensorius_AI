"""Weather forecast service helpers for Sensorius dashboard cards.

The service uses Astral-resolved latitude/longitude from ``saiSettings`` and
keeps a small SQLite cache so the dashboard can continue to show the last
forecast when remote forecast providers are unavailable.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from saiUtils import debug_enabled, printDM

try:
    from __init__ import __version__ as SAI_APP_VERSION
except Exception:
    SAI_APP_VERSION = "v0.0.0"


MODULE = "saiWeatherForecast"
DEBUG = debug_enabled(MODULE)

MET_LOCATION_FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_CACHE_TABLE = "weather_forecast"
FORECAST_REFRESH_SEC = 6 * 60 * 60
FORECAST_COORD_TOLERANCE_DEG = 0.05
USER_AGENT = f"Sensorius/{SAI_APP_VERSION} weather-forecast"


def _safe_float(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt_obj: datetime | None = None) -> str:
    return (dt_obj or _utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, *, tz_name: str = "UTC") -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt_obj = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt_obj.tzinfo is None:
        try:
            dt_obj = dt_obj.replace(tzinfo=ZoneInfo(tz_name or "UTC"))
        except Exception:
            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
    return dt_obj


def _to_local(dt_obj: datetime, tz_name: str) -> datetime:
    try:
        return dt_obj.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:
        return dt_obj.astimezone(timezone.utc)


def _c_to_f(value: float) -> float:
    return (value * 9.0 / 5.0) + 32.0


def _mps_to_mph(value: float) -> float:
    return value * 2.2369362921


def _range(values: list[float]) -> tuple[float, float] | None:
    clean = [v for v in values if math.isfinite(v)]
    if not clean:
        return None
    return min(clean), max(clean)


def _format_temp_range(hours: list[dict[str, Any]]) -> str:
    temp_range = _range([float(h["temp_c"]) for h in hours if _safe_float(h.get("temp_c")) is not None])
    if temp_range is None:
        return "--"
    lo_c, hi_c = temp_range
    return f"{round(_c_to_f(lo_c))}-{round(_c_to_f(hi_c))}\u00b0F / {lo_c:.1f}-{hi_c:.1f}\u00b0C"


def _format_rh_range(hours: list[dict[str, Any]]) -> str:
    rh_range = _range([float(h["rh"]) for h in hours if _safe_float(h.get("rh")) is not None])
    if rh_range is None:
        return "--"
    return f"~{round(rh_range[0])}-{round(rh_range[1])}%"


def _wind_descriptor(avg_mps: float) -> str:
    if avg_mps < 2.0:
        return "light"
    if avg_mps < 5.0:
        return "light/moderate"
    if avg_mps < 8.0:
        return "moderate"
    return "breezy"


def _format_wind(hours: list[dict[str, Any]]) -> str:
    values = [float(h["wind_mps"]) for h in hours if _safe_float(h.get("wind_mps")) is not None]
    wind_range = _range(values)
    if wind_range is None:
        return "--"
    lo_mps, hi_mps = wind_range
    avg_mps = sum(values) / len(values)
    return (
        f"Mostly {_wind_descriptor(avg_mps)}\n"
        f"~{round(lo_mps)}-{round(hi_mps)} m/s / {round(_mps_to_mph(lo_mps))}-{round(_mps_to_mph(hi_mps))} mph"
    )


def _cloud_phrase(avg_cloud: float | None) -> str:
    if avg_cloud is None:
        return "Mixed skies"
    if avg_cloud < 20:
        return "Clear"
    if avg_cloud < 45:
        return "Mostly clear"
    if avg_cloud < 75:
        return "Partly cloudy"
    return "Cloudy"


def _period_name(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "overnight"


def _precip_phrase(total_mm: float, max_hour_mm: float) -> str:
    if total_mm < 0.05 and max_hour_mm < 0.05:
        return ""
    if total_mm < 3.0 and max_hour_mm < 1.2:
        return "light rain/showers"
    if total_mm < 12.0 and max_hour_mm < 4.0:
        return "rain/showers"
    return "heavy rain/showers"


def _overall_summary(hours: list[dict[str, Any]]) -> str:
    if not hours:
        return "Forecast unavailable"

    third = max(1, len(hours) // 3)
    early = hours[:third]
    late = hours[-third:]

    def _avg_cloud(items: list[dict[str, Any]]) -> float | None:
        values = [float(h["cloud"]) for h in items if _safe_float(h.get("cloud")) is not None]
        return (sum(values) / len(values)) if values else None

    early_cloud = _avg_cloud(early)
    late_cloud = _avg_cloud(late)
    phrases = [f"{_cloud_phrase(early_cloud)} early"]

    precip_hours = [h for h in hours if (_safe_float(h.get("precip_mm")) or 0.0) >= 0.05]
    if precip_hours:
        total_mm = sum(float(h.get("precip_mm") or 0.0) for h in hours)
        max_mm = max(float(h.get("precip_mm") or 0.0) for h in hours)
        phrase = _precip_phrase(total_mm, max_mm)
        local_hours = [int(h.get("local_hour") or 0) for h in precip_hours]
        period_counts: dict[str, int] = {}
        for hour in local_hours:
            period = _period_name(hour)
            period_counts[period] = period_counts.get(period, 0) + 1
        period = sorted(period_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        if phrase:
            phrases.append(f"{phrase} {period}")

    if early_cloud is not None and late_cloud is not None and early_cloud - late_cloud >= 25 and late_cloud < 65:
        phrases.append("clearing overnight" if _period_name(int(late[-1].get("local_hour") or 0)) == "overnight" else "clearing late")
    elif not precip_hours and late_cloud is not None:
        late_phrase = _cloud_phrase(late_cloud)
        if late_phrase != _cloud_phrase(early_cloud):
            phrases.append(late_phrase.lower() + " late")

    text = ", ".join(dict.fromkeys(phrases))
    return text[:1].upper() + text[1:]


def _date_label(local_date: date) -> str:
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{weekdays[local_date.weekday()]} {months[local_date.month - 1]} {local_date.day}"


def _summarize_hours(hours: list[dict[str, Any]], *, label: str = "") -> dict[str, Any]:
    return {
        "label": label,
        "forecast": _overall_summary(hours),
        "overall": _overall_summary(hours),
        "temp_range": _format_temp_range(hours),
        "wind": _format_wind(hours),
        "rh_range": _format_rh_range(hours),
        "precip_mm": round(sum(float(h.get("precip_mm") or 0.0) for h in hours), 2),
        "hour_count": len(hours),
    }


def _normalize_hour(record: dict[str, Any], *, tz_name: str) -> dict[str, Any] | None:
    dt_obj = _parse_datetime(record.get("time"), tz_name=tz_name)
    if dt_obj is None:
        return None
    local_dt = _to_local(dt_obj, tz_name)
    temp_c = _safe_float(record.get("temp_c"))
    if temp_c is None:
        return None
    return {
        "time": dt_obj.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "local_time": local_dt.replace(microsecond=0).isoformat(),
        "local_date": local_dt.date().isoformat(),
        "local_hour": local_dt.hour,
        "temp_c": temp_c,
        "rh": _safe_float(record.get("rh")),
        "wind_mps": _safe_float(record.get("wind_mps")),
        "cloud": _safe_float(record.get("cloud")),
        "precip_mm": _safe_float(record.get("precip_mm")) or 0.0,
        "symbol": str(record.get("symbol") or "").strip(),
    }


def normalize_met_forecast(payload: dict[str, Any], *, tz_name: str) -> list[dict[str, Any]]:
    """Normalize MET Norway Location Forecast payload into hourly rows."""
    rows: list[dict[str, Any]] = []
    props = payload.get("properties") if isinstance(payload, dict) else None
    timeseries = props.get("timeseries") if isinstance(props, dict) else None
    if not isinstance(timeseries, list):
        return rows
    for item in timeseries:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        instant = data.get("instant") if isinstance(data.get("instant"), dict) else {}
        details = instant.get("details") if isinstance(instant.get("details"), dict) else {}
        next_1h = data.get("next_1_hours") if isinstance(data.get("next_1_hours"), dict) else {}
        next_6h = data.get("next_6_hours") if isinstance(data.get("next_6_hours"), dict) else {}
        precip = None
        symbol = ""
        for block, divisor in ((next_1h, 1.0), (next_6h, 6.0)):
            block_details = block.get("details") if isinstance(block.get("details"), dict) else {}
            precip = _safe_float(block_details.get("precipitation_amount"))
            summary = block.get("summary") if isinstance(block.get("summary"), dict) else {}
            symbol = str(summary.get("symbol_code") or symbol or "").strip()
            if precip is not None:
                precip = precip / divisor
                break
        row = _normalize_hour(
            {
                "time": item.get("time"),
                "temp_c": details.get("air_temperature"),
                "rh": details.get("relative_humidity"),
                "wind_mps": details.get("wind_speed"),
                "cloud": details.get("cloud_area_fraction"),
                "precip_mm": precip or 0.0,
                "symbol": symbol,
            },
            tz_name=tz_name,
        )
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda row: row["time"])


def normalize_open_meteo_forecast(payload: dict[str, Any], *, tz_name: str) -> list[dict[str, Any]]:
    """Normalize Open-Meteo forecast payload into hourly rows."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    rhs = hourly.get("relative_humidity_2m")
    precip = hourly.get("precipitation")
    clouds = hourly.get("cloud_cover")
    winds = hourly.get("wind_speed_10m")
    if not isinstance(times, list):
        return []
    rows: list[dict[str, Any]] = []
    for idx, raw_time in enumerate(times):
        def _at(values: object) -> object:
            return values[idx] if isinstance(values, list) and idx < len(values) else None

        row = _normalize_hour(
            {
                "time": raw_time,
                "temp_c": _at(temps),
                "rh": _at(rhs),
                "wind_mps": _at(winds),
                "cloud": _at(clouds),
                "precip_mm": _at(precip) or 0.0,
            },
            tz_name=tz_name,
        )
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda row: row["time"])


def build_forecast_payload(
    *,
    provider: str,
    latitude: float,
    longitude: float,
    tz_name: str,
    hourly: list[dict[str, Any]],
    location_source: str = "",
    retrieved_utc: str | None = None,
) -> dict[str, Any]:
    """Build the dashboard-oriented forecast product from hourly rows."""
    retrieved = _parse_datetime(retrieved_utc or _iso_utc(), tz_name="UTC") or _utc_now()
    now_local = _to_local(retrieved, tz_name)
    now_cutoff = now_local - timedelta(minutes=90)
    future_hours = [
        row for row in hourly
        if (_parse_datetime(row.get("local_time"), tz_name=tz_name) or now_local) >= now_cutoff
    ]
    future_hours = sorted(future_hours, key=lambda row: row.get("local_time") or row.get("time") or "")
    next_24 = future_hours[:24] if len(future_hours) >= 24 else future_hours

    today_key = now_local.date().isoformat()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in future_hours:
        key = str(row.get("local_date") or "")
        if not key:
            continue
        grouped.setdefault(key, []).append(row)

    days: list[dict[str, Any]] = []
    for key in sorted(grouped.keys()):
        if key <= today_key:
            continue
        try:
            day_date = date.fromisoformat(key)
        except Exception:
            continue
        summary = _summarize_hours(grouped[key], label=_date_label(day_date))
        summary["date"] = key
        days.append(summary)
        if len(days) >= 6:
            break

    return {
        "ok": bool(next_24),
        "provider": provider,
        "stale": False,
        "retrieved_utc": retrieved.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "refresh_sec": FORECAST_REFRESH_SEC,
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": tz_name,
            "source": location_source,
        },
        "current_24h": _summarize_hours(next_24, label="24 Hour Forecast") if next_24 else {},
        "days": days,
        "hourly": next_24,
    }


def _ensure_forecast_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FORECAST_CACHE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retrieved_utc TEXT NOT NULL,
            provider TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT,
            forecast_json TEXT NOT NULL
        )
        """
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{FORECAST_CACHE_TABLE}_retrieved ON {FORECAST_CACHE_TABLE}(retrieved_utc)")


def save_weather_forecast_cache(db_path: str, payload: dict[str, Any]) -> None:
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    lat = _safe_float(location.get("latitude"))
    lon = _safe_float(location.get("longitude"))
    if lat is None or lon is None:
        return
    retrieved_utc = str(payload.get("retrieved_utc") or _iso_utc())
    provider = str(payload.get("provider") or "").strip() or "unknown"
    tz_name = str(location.get("timezone") or "").strip()
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        _ensure_forecast_cache(conn)
        conn.execute(
            f"""
            INSERT INTO {FORECAST_CACHE_TABLE}
                (retrieved_utc, provider, latitude, longitude, timezone, forecast_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                retrieved_utc,
                provider,
                lat,
                lon,
                tz_name,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        conn.commit()


def load_weather_forecast_cache(db_path: str, *, latitude: float, longitude: float) -> dict[str, Any] | None:
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            _ensure_forecast_cache(conn)
            row = conn.execute(
                f"""
                SELECT forecast_json
                FROM {FORECAST_CACHE_TABLE}
                WHERE ABS(latitude - ?) <= ? AND ABS(longitude - ?) <= ?
                ORDER BY retrieved_utc DESC, id DESC
                LIMIT 1
                """,
                (latitude, FORECAST_COORD_TOLERANCE_DEG, longitude, FORECAST_COORD_TOLERANCE_DEG),
            ).fetchone()
    except Exception as exc:
        if DEBUG:
            printDM(f"forecast cache load failed: {exc}", location=MODULE)
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _cache_age_sec(payload: dict[str, Any]) -> float | None:
    retrieved = _parse_datetime(payload.get("retrieved_utc"), tz_name="UTC")
    if retrieved is None:
        return None
    return max(0.0, (_utc_now() - retrieved.astimezone(timezone.utc)).total_seconds())


async def _fetch_met_forecast(latitude: float, longitude: float, *, tz_name: str, timeout_sec: float) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=timeout_sec, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        resp = await client.get(
            MET_LOCATION_FORECAST_URL,
            params={"lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}"},
        )
        resp.raise_for_status()
        return normalize_met_forecast(resp.json(), tz_name=tz_name)


async def _fetch_open_meteo_forecast(latitude: float, longitude: float, *, tz_name: str, timeout_sec: float) -> list[dict[str, Any]]:
    hourly = "temperature_2m,relative_humidity_2m,precipitation,cloud_cover,wind_speed_10m"
    async with httpx.AsyncClient(timeout=timeout_sec, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        resp = await client.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": f"{latitude:.6f}",
                "longitude": f"{longitude:.6f}",
                "hourly": hourly,
                "timezone": tz_name or "auto",
                "forecast_days": 7,
                "temperature_unit": "celsius",
                "wind_speed_unit": "ms",
            },
        )
        resp.raise_for_status()
        return normalize_open_meteo_forecast(resp.json(), tz_name=tz_name)


def _resolve_forecast_location(settings: Any, *, timeout_sec: float) -> dict[str, Any]:
    try:
        resolved = settings.resolve_astral_location(persist_if_auto=False, timeout_sec=timeout_sec) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"location_resolve_failed:{exc}"}
    lat = _safe_float(resolved.get("lat"))
    lon = _safe_float(resolved.get("lon"))
    tz_name = str(resolved.get("tz") or "").strip()
    if lat is None or lon is None or not tz_name:
        return {"ok": False, "reason": "location_unavailable", "resolved": resolved}
    return {
        "ok": True,
        "latitude": lat,
        "longitude": lon,
        "timezone": tz_name,
        "source": str(resolved.get("source") or resolved.get("provider") or "").strip(),
    }


async def get_weather_forecast_payload(
    settings: Any,
    *,
    db_path: str = "sensorius_data.db",
    force_refresh: bool = False,
    min_days: int = 1,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    """Return the dashboard forecast payload using live providers plus cache."""
    location = _resolve_forecast_location(settings, timeout_sec=2.5)
    if not location.get("ok"):
        return {
            "ok": False,
            "stale": False,
            "reason": location.get("reason") or "location_unavailable",
            "current_24h": {},
            "days": [],
        }

    lat = float(location["latitude"])
    lon = float(location["longitude"])
    tz_name = str(location["timezone"])
    cached = load_weather_forecast_cache(db_path, latitude=lat, longitude=lon)
    if cached:
        age = _cache_age_sec(cached)
        if age is not None:
            cached["cache_age_sec"] = round(age, 1)
        cached_days = cached.get("days") if isinstance(cached.get("days"), list) else []
        has_requested_day_count = len(cached_days) >= max(1, int(min_days or 1))
        if not force_refresh and age is not None and age <= FORECAST_REFRESH_SEC and has_requested_day_count:
            cached["stale"] = False
            cached["cache_hit"] = True
            return cached

    provider_errors: list[str] = []
    providers = (
        ("met_no", _fetch_met_forecast),
        ("open_meteo", _fetch_open_meteo_forecast),
    )
    for provider, fetcher in providers:
        try:
            hourly = await fetcher(lat, lon, tz_name=tz_name, timeout_sec=timeout_sec)
            payload = build_forecast_payload(
                provider=provider,
                latitude=lat,
                longitude=lon,
                tz_name=tz_name,
                hourly=hourly,
                location_source=str(location.get("source") or ""),
                retrieved_utc=_iso_utc(),
            )
            if payload.get("ok"):
                save_weather_forecast_cache(db_path, payload)
                return payload
            provider_errors.append(f"{provider}:empty_forecast")
        except Exception as exc:
            provider_errors.append(f"{provider}:{exc}")
            if DEBUG:
                printDM(f"forecast provider failed: {provider}: {exc}", location=MODULE)

    if cached:
        cached["stale"] = True
        cached["cache_hit"] = True
        cached["reason"] = "provider_unavailable"
        cached["provider_errors"] = provider_errors
        age = _cache_age_sec(cached)
        if age is not None:
            cached["cache_age_sec"] = round(age, 1)
        return cached

    return {
        "ok": False,
        "stale": False,
        "reason": "provider_unavailable",
        "provider_errors": provider_errors,
        "location": {
            "latitude": lat,
            "longitude": lon,
            "timezone": tz_name,
            "source": str(location.get("source") or ""),
        },
        "current_24h": {},
        "days": [],
    }
