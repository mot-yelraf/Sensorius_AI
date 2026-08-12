"""Launch the native full-screen Caelus weather forecast application.

The launcher manages window sizing, navigation, and lifecycle behavior around
the locally served forecast page without owning forecast retrieval itself.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__ as SAI_APP_VERSION
from .saiHtml import canonicalize_metric_name, get_gauge_config
from .saiSensorSettingsManager import SensorSettingsManager
from .saiWeatherAstronomy import astronomy_context
from .saiWeatherForecast import get_weather_forecast_payload


WEATHER_THEMES = {"garden", "island", "river", "desert"}


def normalize_weather_theme(value: object) -> str:
    """Return a supported weather scene theme."""
    theme = str(value or "").strip().lower()
    return theme if theme in WEATHER_THEMES else "garden"


def _metric_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _safe_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _find_metric(values: dict[str, Any], *aliases: str) -> tuple[str, float] | None:
    indexed = {_metric_key(name): (str(name), value) for name, value in (values or {}).items()}
    for alias in aliases:
        match = indexed.get(_metric_key(alias))
        if match is None:
            continue
        numeric = _safe_float(match[1])
        if numeric is not None:
            return match[0], numeric
    return None


def normalize_current_weather_readings(
    sensor_id: str,
    values: dict[str, Any],
    timestamp: object = "",
) -> dict[str, Any]:
    """Map dynamic Sensorius metrics into the weather display contract."""
    temp_f = _find_metric(values, "Temperature_F", "Outdoor Temperature_F", "Air Temperature_F")
    temp_c = _find_metric(values, "Temperature", "Temperature_C", "Outdoor Temperature", "Air Temperature")
    if temp_f is not None:
        temperature_f = temp_f[1]
        temperature_c = (temperature_f - 32.0) * 5.0 / 9.0
    elif temp_c is not None:
        temperature_c = temp_c[1]
        temperature_f = temperature_c * 9.0 / 5.0 + 32.0
    else:
        temperature_f = temperature_c = None

    humidity = _find_metric(values, "Rel-Humidity", "Relative Humidity", "Outdoor Humidity")
    pressure = _find_metric(values, "Baro-Pressure", "Barometric Pressure", "Pressure")
    wind_speed = _find_metric(values, "Wind Speed", "Windspeed")
    wind_gust = _find_metric(values, "Wind Gust", "Wind Gust Speed", "Gust")
    wind_direction = _find_metric(values, "Wind Direction", "Wind Dir")
    rain_today = _find_metric(values, "Rain Last 24h", "Rain Today", "Daily Rain", "Rain")
    rain_rate = _find_metric(values, "Rain Rate", "Rainfall Rate")
    uv = _find_metric(values, "UV Index", "UV")
    solar = _find_metric(values, "Solar Radiation", "Solar")

    return {
        "ok": bool(sensor_id and values),
        "sensor_id": str(sensor_id or ""),
        "timestamp": str(timestamp or ""),
        "temperature_f": round(temperature_f, 1) if temperature_f is not None else None,
        "temperature_c": round(temperature_c, 1) if temperature_c is not None else None,
        "humidity": humidity[1] if humidity else None,
        "pressure_hpa": pressure[1] if pressure else None,
        "wind_speed_mph": wind_speed[1] if wind_speed else None,
        "wind_gust_mph": wind_gust[1] if wind_gust else None,
        "wind_direction_deg": wind_direction[1] if wind_direction else None,
        "rain_today_in": rain_today[1] if rain_today else None,
        "rain_rate_in_h": rain_rate[1] if rain_rate else None,
        "uv": uv[1] if uv else None,
        "solar_radiation": solar[1] if solar else None,
    }


def build_display_metrics(values: dict[str, Any], configured_metrics: list[str]) -> list[dict[str, Any]]:
    """Return the selected sensor's Display Metrics in their configured order."""
    indexed = {_metric_key(name): (str(name), value) for name, value in (values or {}).items()}
    gauge_config = get_gauge_config()
    display_metrics: list[dict[str, Any]] = []
    for configured_name in configured_metrics[:6]:
        name = str(configured_name or "").strip()
        if not name:
            continue
        match = indexed.get(_metric_key(name))
        raw_value = match[1] if match is not None else None
        numeric = _safe_float(raw_value)
        canonical_name = canonicalize_metric_name(name, gauge_config)
        metric_config = gauge_config.get(canonical_name, {})
        precision = metric_config.get("display_precision", 2)
        try:
            precision = max(0, min(6, int(precision)))
        except Exception:
            precision = 2
        display_metrics.append(
            {
                "name": name,
                "value": round(numeric, precision) if numeric is not None else None,
                "unit": str(metric_config.get("unit") or ""),
            }
        )
    return display_metrics


def _condition_icon(text: object) -> str:
    value = str(text or "").lower()
    if "thunder" in value:
        return "⛈️"
    if "snow" in value or "sleet" in value:
        return "🌨️"
    if "rain" in value or "shower" in value or "drizzle" in value:
        return "🌧️"
    if "fog" in value:
        return "🌫️"
    if "partly" in value or "mostly clear" in value:
        return "🌤️"
    if "cloud" in value or "overcast" in value:
        return "☁️"
    return "☀️"


def _condition_icon_key(text: object) -> str:
    """Return a font-independent icon name for the weather forecast UI."""
    value = str(text or "").lower()
    if "thunder" in value:
        return "thunder"
    if "snow" in value or "sleet" in value:
        return "snow"
    if "rain" in value or "shower" in value or "drizzle" in value:
        return "rain"
    if "fog" in value:
        return "fog"
    if "partly" in value or "mostly clear" in value:
        return "partly-cloudy"
    if "cloud" in value or "overcast" in value:
        return "cloudy"
    return "sunny"


def _precipitation_chance_label(condition: object) -> str:
    """Name a precipitation probability as a rain or snow chance."""
    return "Snow chance" if _condition_icon_key(condition) == "snow" else "Rain chance"


def _hour_temperature_f(hour: dict[str, Any]) -> float | None:
    value = _safe_float(hour.get("temp_c"))
    return None if value is None else value * 9.0 / 5.0 + 32.0


def _format_hour_label(value: object) -> str:
    """Format an ISO forecast timestamp as a portable 12-hour clock label."""
    text = str(value or "")
    try:
        observed_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return "--"
    hour = observed_at.strftime("%I").lstrip("0") or "12"
    minute = observed_at.strftime("%M")
    clock = hour if minute == "00" else f"{hour}:{minute}"
    return f"{clock} {observed_at.strftime('%p')}"


def format_observation_time(value: object, timezone_name: str) -> str:
    """Format an observation in local 12-hour time at minute precision."""
    try:
        observed_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        local_tz = ZoneInfo(timezone_name or "UTC")
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=local_tz)
        local = observed_at.astimezone(local_tz)
    except Exception:
        return "just now"
    date_text = f"{local.strftime('%b')} {local.day}, {local.year}"
    time_text = local.strftime("%I:%M %p").lstrip("0")
    return f"{date_text} · {time_text}"


def _hour_window_condition(hours: list[dict[str, Any]]) -> str:
    """Describe one displayed hourly window without borrowing the daily summary."""
    symbols = [str(row.get("symbol") or "").strip() for row in hours]
    lowered_symbols = [symbol.lower() for symbol in symbols if symbol]
    if any("thunder" in symbol for symbol in lowered_symbols):
        return "Thunderstorms"
    if any(any(token in symbol for token in ("snow", "sleet")) for symbol in lowered_symbols):
        return "Snow"

    precipitation_mm = sum(_safe_float(row.get("precip_mm")) or 0.0 for row in hours)
    if precipitation_mm >= 0.05:
        return "Rain/showers"
    if any(any(token in symbol for token in ("rain", "shower", "drizzle")) for symbol in lowered_symbols):
        return next(symbol for symbol in symbols if symbol)
    if any(symbols):
        return next(symbol for symbol in symbols if symbol)

    cloud_values = [value for value in (_safe_float(row.get("cloud")) for row in hours) if value is not None]
    average_cloud = sum(cloud_values) / len(cloud_values) if cloud_values else None
    if average_cloud is None:
        return "Forecast unavailable"
    if average_cloud >= 85.0:
        return "Cloudy"
    if average_cloud >= 35.0:
        return "Partly cloudy"
    return "Clear"


def build_weather_display_forecast(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical Sensorius forecast payload for the full-screen UI."""
    current = payload.get("current_24h") if isinstance(payload.get("current_24h"), dict) else {}
    raw_hours = payload.get("hourly") if isinstance(payload.get("hourly"), list) else []
    hours = []
    normalized_hours = [row for row in raw_hours[:24] if isinstance(row, dict)]
    for raw in normalized_hours:
        local_time = str(raw.get("local_time") or raw.get("time") or "")
        label = _format_hour_label(local_time)
        condition = _hour_window_condition([raw])
        temp_f = _hour_temperature_f(raw)
        hours.append(
            {
                "label": label,
                "icon": _condition_icon(condition),
                "icon_key": _condition_icon_key(condition),
                "precip_label": _precipitation_chance_label(condition),
                "temperature_f": round(temp_f) if temp_f is not None else None,
                "precipitation_mm": round(_safe_float(raw.get("precip_mm")) or 0.0, 1),
                "precip_probability": (
                    round(probability) if (probability := _safe_float(raw.get("precip_probability"))) is not None else None
                ),
            }
        )

    days = []
    for raw in payload.get("days") or []:
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("forecast") or raw.get("overall") or "Forecast unavailable")
        days.append(
            {
                "date": str(raw.get("date") or ""),
                "label": str(raw.get("label") or raw.get("date") or "Day"),
                "summary": summary,
                "icon": _condition_icon(summary),
                "icon_key": _condition_icon_key(summary),
                "precip_label": _precipitation_chance_label(summary),
                "temp_range": str(raw.get("temp_range") or "--"),
                "rh_range": str(raw.get("rh_range") or "--"),
                "wind": str(raw.get("wind") or "--"),
                "precipitation_mm": _safe_float(raw.get("precip_mm")) or 0.0,
                "precip_probability": (
                    round(probability) if (probability := _safe_float(raw.get("precip_probability"))) is not None else None
                ),
            }
        )

    temp_values = [value for value in (_hour_temperature_f(row) for row in raw_hours) if value is not None]
    probabilities = [
        value
        for value in (_safe_float(row.get("precip_probability")) for row in normalized_hours)
        if value is not None
    ]
    current_probability = _safe_float(current.get("precip_probability"))
    if current_probability is None and probabilities:
        current_probability = max(probabilities)
    condition = str(current.get("overall") or current.get("forecast") or "Forecast standing by")
    return {
        "ok": bool(payload.get("ok")),
        "provider": str(payload.get("provider") or ""),
        "provider_label": {"met_no": "MET Norway", "open_meteo": "Open-Meteo", "us": "US · NWS"}.get(
            str(payload.get("provider") or ""), "Forecast"
        ),
        "stale": bool(payload.get("stale", False)),
        "reason": str(payload.get("reason") or ""),
        "condition": condition,
        "icon": _condition_icon(_hour_window_condition(normalized_hours[:3]) if normalized_hours else condition),
        "icon_key": _condition_icon_key(
            _hour_window_condition(normalized_hours[:3]) if normalized_hours else condition
        ),
        "precip_label": _precipitation_chance_label(condition),
        "high_f": round(max(temp_values)) if temp_values else None,
        "low_f": round(min(temp_values)) if temp_values else None,
        "precipitation_mm": round(sum(_safe_float(row.get("precip_mm")) or 0.0 for row in raw_hours), 1),
        "precip_probability": round(current_probability) if current_probability is not None else None,
        "temp_range": str(current.get("temp_range") or "--"),
        "rh_range": str(current.get("rh_range") or "--"),
        "wind": str(current.get("wind") or "--"),
        "hours": hours,
        "days": days,
        "location": payload.get("location") if isinstance(payload.get("location"), dict) else {},
    }


def _windy_url(latitude: float, longitude: float) -> str:
    query = urlencode(
        {
            "lat": f"{latitude:.4f}",
            "lon": f"{longitude:.4f}",
            "detailLat": f"{latitude:.4f}",
            "detailLon": f"{longitude:.4f}",
            "marker": "true",
            "location": "coordinates",
            "type": "map",
            "overlay": "radar",
        }
    )
    return f"https://embed.windy.com/embed2.html?{query}"


class WeatherForecastAppService:
    """Bridge Sensorius settings, readings, forecasts, and astronomy to the weather UI."""

    def __init__(self, *, settings: Any, data_logger: Any, sensor_settings_manager: Any | None = None) -> None:
        self.settings = settings
        self.data_logger = data_logger
        self.sensor_settings_manager = sensor_settings_manager or SensorSettingsManager("sensor_settings")
        self._warm_task: asyncio.Task[dict[str, Any]] | None = None

    def ensure_background_warm(self) -> None:
        """Start a non-blocking forecast cache warm once per app process."""
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._load_forecast(), name="WeatherForecastWarm")

        def _consume_warm_result(done_task: asyncio.Task[dict[str, Any]]) -> None:
            try:
                if not done_task.cancelled():
                    done_task.exception()
            except Exception:
                pass

        self._warm_task.add_done_callback(_consume_warm_result)

    async def _load_forecast(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return await get_weather_forecast_payload(
            self.settings,
            db_path=str(getattr(self.data_logger, "db_path", "sensorius_data.db") or "sensorius_data.db"),
            force_refresh=force_refresh,
            min_days=6,
            timeout_sec=8.0,
        )

    def _reload_settings(self) -> None:
        try:
            self.settings.get_section("WeatherForecast", reload_if_changed=True)
        except Exception:
            pass

    def integration_settings(self) -> dict[str, str]:
        self._reload_settings()
        return {
            "theme": normalize_weather_theme(self.settings.get_setting("WeatherForecast", "THEME", "garden")),
            "sensor_id": str(self.settings.get_setting("WeatherForecast", "CURRENT_SENSOR_ID", "") or "").strip(),
        }

    def location(self) -> dict[str, Any]:
        self._reload_settings()
        try:
            resolved = self.settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5) or {}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "latitude": 0.0, "longitude": 0.0, "timezone": "UTC"}
        try:
            latitude = float(resolved["lat"])
            longitude = float(resolved["lon"])
            timezone_name = str(resolved["tz"])
        except Exception:
            return {"ok": False, "reason": "location_unavailable", "latitude": 0.0, "longitude": 0.0, "timezone": "UTC"}
        configured_name = str(self.settings.get_setting("Astral", "LOCATION_NAME", "") or "").strip()
        return {
            "ok": True,
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone_name,
            "name": configured_name or str(resolved.get("name") or resolved.get("city") or "Sensorius station"),
            "source": str(resolved.get("source") or resolved.get("provider") or ""),
        }

    def astronomy(self) -> dict[str, Any]:
        location = self.location()
        adapter = SimpleNamespace(
            latitude=location["latitude"],
            longitude=location["longitude"],
            timezone=location["timezone"],
            location_name=location.get("name") or "Sensorius station",
        )
        payload = astronomy_context(adapter)
        payload["location"] = location
        return payload

    def current_readings(self) -> dict[str, Any]:
        sensor_id = self.integration_settings()["sensor_id"]
        if not sensor_id:
            payload = normalize_current_weather_readings("", {}, "")
            payload["display_metrics"] = []
            return payload
        values = self.data_logger.get_latest_values(sensor_id) or {}
        timestamp = self.data_logger.get_latest_timestamp(sensor_id) or ""
        payload = normalize_current_weather_readings(sensor_id, values, timestamp)
        try:
            configured_metrics = self.sensor_settings_manager.get_display_metrics(sensor_id)
        except Exception:
            configured_metrics = []
        payload["display_metrics"] = build_display_metrics(values, configured_metrics)
        return payload

    async def canonical_forecast(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return the canonical payload, sharing an active startup warm task."""
        self._reload_settings()
        warm_task = self._warm_task
        if not force_refresh and warm_task is not None and not warm_task.done():
            return await asyncio.shield(warm_task)
        return await self._load_forecast(force_refresh=force_refresh)

    async def forecast(self, *, force_refresh: bool = False) -> dict[str, Any]:
        payload = await self.canonical_forecast(force_refresh=force_refresh)
        return build_weather_display_forecast(payload)


def register_weather_forecast_app_routes(
    router: APIRouter,
    *,
    app: Any,
    settings: Any,
    data_logger: Any,
    sensor_settings_manager: Any | None = None,
) -> WeatherForecastAppService:
    """Register the full-screen weather application and namespaced APIs."""
    service = WeatherForecastAppService(
        settings=settings,
        data_logger=data_logger,
        sensor_settings_manager=sensor_settings_manager,
    )
    app.state.weather_forecast_app_service = service
    app.add_event_handler("startup", service.ensure_background_warm)

    @router.get("/weather-forecast", response_class=HTMLResponse)
    async def weather_forecast_page(request: Request):
        forecast, moon, latest = await asyncio.gather(
            service.forecast(),
            asyncio.to_thread(service.astronomy),
            asyncio.to_thread(service.current_readings),
        )
        location = moon.get("location") if isinstance(moon.get("location"), dict) else service.location()
        return app.state.templates.TemplateResponse(
            request,
            "weather_forecast/index.html",
            {
                "app_version": SAI_APP_VERSION,
                "settings": service.integration_settings(),
                "location": location,
                "latest": latest,
                "latest_observation_time": format_observation_time(
                    latest.get("timestamp"), str(location.get("timezone") or "UTC")
                ),
                "moon": moon,
                "forecast": forecast,
                "windy_iframe_url": _windy_url(float(location.get("latitude") or 0.0), float(location.get("longitude") or 0.0)),
                "runtime_instance_id": str(getattr(request.app.state, "ui_runtime_instance_id", "") or ""),
            },
        )

    @router.get("/api/weather-forecast-app/forecast", response_class=JSONResponse)
    async def api_weather_app_forecast(force_refresh: bool = False):
        return JSONResponse(await service.forecast(force_refresh=force_refresh))

    @router.get("/api/weather-forecast-app/astronomy", response_class=JSONResponse)
    async def api_weather_app_astronomy():
        return JSONResponse(await asyncio.to_thread(service.astronomy))

    @router.get("/api/weather-forecast-app/current-readings", response_class=JSONResponse)
    async def api_weather_app_current_readings():
        return JSONResponse(await asyncio.to_thread(service.current_readings))

    return service
