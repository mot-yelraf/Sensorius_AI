"""Cache location-specific rainfall history for dashboard gauge scales.

Only daily ERA5 rain totals are downloaded. A compact summary survives restarts;
network and location lookups run outside the dashboard request path.
"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time

import httpx

from .saiUtils import printDM

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
START = date(1991, 1, 1)

RAIN_PERIODS = {
    "Rain": "day", "Rain Last 24h": "day", "Rain Day": "day",
    "Rain Week": "week", "Rain Month": "month", "Rain Year": "year",
}
RAIN_COLORS = ("#add8e6", "#66b2ff", "#0033cc")


def latest_history_date() -> date:
    """Allow for ERA5's five-day publication delay using a UTC cutoff."""
    return datetime.now(timezone.utc).date() - timedelta(days=5)


def rain_gauge_scale(maximum: float) -> dict:
    """Build the screenshot's blue bands and proportional ticks in gauge units."""
    return {
        "min": 0, "max": maximum,
        "ticks": [maximum * fraction for fraction in (0, .2, .4, .6, .8, 1)],
        "zones": [
            {"strokeStyle": color, "min": maximum * low, "max": maximum * high}
            for color, low, high in zip(RAIN_COLORS, (0, .2, .6), (.2, .6, 1))
        ],
    }


def style_rain_gauges(config: dict) -> dict:
    """Apply consistent rain colors and identify climate-scaled accumulation metrics."""
    config = copy.deepcopy(config)
    for metric, gauge in config.items():
        if metric in RAIN_PERIODS or metric in {"Rain Event", "Rain Total"}:
            gauge.update(rain_gauge_scale(gauge["max"]))
            if metric in RAIN_PERIODS:
                gauge["rain_period"] = RAIN_PERIODS[metric]
    return config


def summarize_rain_history(payload: dict, end: date) -> dict[str, float]:
    """Return wettest day, rolling seven days, calendar month and year in mm.

    Require every baseline day to prevent missing rainfall from lowering limits.
    Daily values approximate the rolling 24-hour gauge with local calendar days.
    """
    daily = payload.get("daily") or {}
    dates, values = daily.get("time", []), daily.get("rain_sum", [])
    count = (end - START).days + 1
    if len(dates) != count or len(values) != count:
        raise ValueError("Rain history does not cover the complete requested range")
    months, years = defaultdict(float), defaultdict(float)
    window = deque(maxlen=7)
    max_day = max_week = 0.0
    for index, (day_text, value) in enumerate(zip(dates, values)):
        day = START + timedelta(days=index)
        if day_text != day.isoformat() or isinstance(value, bool) or value is None:
            raise ValueError("Rain history contains a missing or invalid day")
        rain = float(value)
        if not math.isfinite(rain) or rain < 0:
            raise ValueError("Rain history contains an invalid rain total")
        window.append(rain)
        max_day = max(max_day, rain)
        if len(window) == 7:
            max_week = max(max_week, sum(window))
        months[(day.year, day.month)] += rain
        years[day.year] += rain
    return {key: round(value, 3) for key, value in {
        "day": max_day, "week": max_week, "month": max(total for (year, month), total in months.items()
                     if (year, month) < (end.year, end.month) or (end + timedelta(days=1)).month != end.month),
        "year": max(total for year, total in years.items()
                    if year < end.year or (end.month, end.day) == (12, 31))
    }.items()}


class RainClimateService:
    """Serve a non-blocking climate summary with a bounded persistent cache."""

    daily_variables = "rain_sum"
    expected_units = {"rain_sum": "mm"}

    def __init__(self, settings_factory, cache_path: Path):
        self.settings_factory = settings_factory
        self.cache_path = Path(cache_path)
        self.payload = {"status": "warming"}
        self.task = None
        self.next_check = 0.0

    def snapshot(self) -> dict:
        """Return current status and start background work when necessary."""
        if (self.task is None or self.task.done()) and time.monotonic() >= self.next_check:
            self.task = asyncio.create_task(self._load(), name="RainClimate")
        return dict(self.payload)

    def invalidate(self) -> None:
        """Discard the old location immediately after system settings change."""
        if self.task is not None:
            self.task.cancel()
        self.task = None
        self.payload = {"status": "warming"}
        self.next_check = 0.0

    async def close(self) -> None:
        """Cancel climate retrieval during application shutdown."""
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def _location(self) -> dict:
        settings = self.settings_factory()
        resolved = settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        lat, lon = float(resolved["lat"]), float(resolved["lon"])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Rain climate location is unavailable")
        return {"latitude": round(lat, 4), "longitude": round(lon, 4), "timezone": resolved.get("tz") or "UTC"}

    def _read_cache(self, location: dict) -> dict | None:
        try:
            cached = json.loads(self.cache_path.read_text())
        except FileNotFoundError:
            return None
        if cached.get("schema") != 2 or cached.get("location") != location:
            return None
        try:
            end = date.fromisoformat(cached["end_date"])
            requested_end = date.fromisoformat(cached["requested_end_date"])
        except (KeyError, ValueError, TypeError):
            return None
        if cached.get("start_date") != START.isoformat() or not START <= end <= requested_end <= latest_history_date():
            return None
        return cached if self._valid_summary(cached) else None

    def _valid_summary(self, payload: dict) -> bool:
        maxima = payload.get("maxima_mm", {})
        return (set(maxima) == {"day", "week", "month", "year"}
                and all(not isinstance(v, bool) and isinstance(v, (int, float))
                        and math.isfinite(v) and v >= 0 for v in maxima.values()))

    def _summarize(self, data: dict, end: date) -> dict:
        return {"maxima_mm": summarize_rain_history(data, end)}

    def _available_history(self, data: dict, end: date) -> tuple[dict, date]:
        """Trim unpublished trailing days, while rejecting gaps inside the history."""
        daily = data.get("daily") or {}
        keys = ("time", *self.expected_units)
        count = (end - START).days + 1
        if any(len(daily.get(key, [])) != count for key in keys):
            raise ValueError("Climate history does not cover the requested dates")
        available = count
        while available and any(daily[key][available - 1] is None for key in self.expected_units):
            available -= 1
        # Allow an additional week of publication lag; longer gaps need a retry.
        if count - available > 7:
            raise ValueError("Climate archive is missing more than a week of recent data")
        return {**data, "daily": {key: daily[key][:available] for key in keys}}, end - timedelta(days=count - available)

    def _write_cache(self, payload: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.cache_path)

    async def _load(self) -> None:
        try:
            location = await asyncio.to_thread(self._location)
            if self.payload.get("location") != location:
                self.payload = {"status": "warming", "location": location}
            try:
                cached = await asyncio.to_thread(self._read_cache, location)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                printDM(f"Rain climate cache read failed: {exc}", location="saiRainClimate")
                cached = None
            if cached is not None:
                self.payload = cached
            end = latest_history_date()
            if cached is None or cached["requested_end_date"] != end.isoformat():
                async with httpx.AsyncClient(timeout=45.0) as client:
                    response = await client.get(ARCHIVE_URL, params={
                        **location, "start_date": START.isoformat(), "end_date": end.isoformat(),
                        "daily": self.daily_variables, "models": "era5",
                        "precipitation_unit": "mm", "temperature_unit": "celsius", "wind_speed_unit": "kmh",
                    })
                    response.raise_for_status()
                    data = response.json()
                if any(data.get("daily_units", {}).get(key) != unit for key, unit in self.expected_units.items()):
                    raise ValueError("Climate history has unexpected units")
                requested_end = end
                data, end = await asyncio.to_thread(self._available_history, data, end)
                summary = await asyncio.to_thread(self._summarize, data, end)
                self.payload = {
                    "schema": 2, "status": "ready", "location": location,
                    "start_date": START.isoformat(), "end_date": end.isoformat(),
                    "requested_end_date": requested_end.isoformat(),
                    "baseline": f"1991–{end.year}", "source": "Open-Meteo / ERA5",
                    **summary,
                }
                try:
                    await asyncio.to_thread(self._write_cache, self.payload)
                except OSError as exc:
                    printDM(f"Rain climate cache write failed: {exc}", location="saiRainClimate")
            self.next_check = time.monotonic() + 3600
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.payload.get("status") != "ready":
                self.payload = {"status": "unavailable", "reason": "Historical climate data unavailable"}
            self.next_check = time.monotonic() + 300
            printDM(f"Rain climate unavailable: {exc}", location="saiRainClimate")
