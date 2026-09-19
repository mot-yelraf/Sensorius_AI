"""Provide same-calendar-date climate statistics for the Caelus forecast card.

Reuse the rainfall service's background loading and location-specific disk cache,
keeping one compact 366-day climatology instead of the downloaded daily history.
"""

from datetime import date, datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from .saiRainClimate import START, RainClimateService

VARIABLES = {
    "temperature_2m_mean": "temperature_c",
    "relative_humidity_2m_mean": "humidity_pct",
    "wind_speed_10m_mean": "wind_kmh",
    "rain_sum": "rain_mm",
}
EXTREME_VARIABLES = {
    kind: {variable.replace("_mean", f"_{kind}"): field for variable, field in VARIABLES.items()}
    for kind in ("min", "max")
}
ALL_VARIABLES = {**VARIABLES, **EXTREME_VARIABLES["min"], **EXTREME_VARIABLES["max"]}
DAY_KEYS = {(date(2000, 1, 1) + timedelta(days=i)).strftime("%m-%d") for i in range(366)}


def _valid_value(key: str, value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value)
            and (key == "temperature_c" or value >= 0)
            and (key != "humidity_pct" or value <= 100))


def summarize_weather_history(payload: dict, end: date) -> dict:
    """Summarize calendar-date means and extremes, keeping the earliest year for ties."""
    daily = payload.get("daily") or {}
    count = (end - START).days + 1
    if any(len(daily.get(key, [])) != count for key in ("time", *ALL_VARIABLES)):
        raise ValueError("Weather history does not cover the complete baseline")
    totals, samples, extremes = {}, {}, {}
    for index in range(count):
        day = START + timedelta(days=index)
        if daily["time"][index] != day.isoformat():
            raise ValueError("Weather history has missing or duplicate dates")
        key = day.strftime("%m-%d")
        sums = totals.setdefault(key, dict.fromkeys(VARIABLES.values(), 0.0))
        for variable, field in VARIABLES.items():
            value = daily[variable][index]
            if not _valid_value(field, value):
                raise ValueError(f"Weather history contains invalid {variable}")
            sums[field] += value
        records = extremes.setdefault(key, {"min": {}, "max": {}})
        for kind, variables in EXTREME_VARIABLES.items():
            for variable, field in variables.items():
                value = daily[variable][index]
                if not _valid_value(field, value):
                    raise ValueError(f"Weather history contains invalid {variable}")
                previous = records[kind].get(field)
                if previous is None or (value < previous["value"] if kind == "min" else value > previous["value"]):
                    records[kind][field] = {"value": value, "year": day.year}
        samples[key] = samples.get(key, 0) + 1
    return {key: {**{field: round(value / samples[key], 3) for field, value in sums.items()},
                  "samples": samples[key], "extremes": extremes[key]} for key, sums in totals.items()}


class WeatherClimateService(RainClimateService):
    """Cache weather statistics and select today's date in the hub's timezone."""

    daily_variables = ",".join(ALL_VARIABLES)
    expected_units = {"temperature_2m_mean": "°C", "relative_humidity_2m_mean": "%",
                      "wind_speed_10m_mean": "km/h", "rain_sum": "mm",
                      "temperature_2m_min": "°C", "temperature_2m_max": "°C",
                      "relative_humidity_2m_min": "%", "relative_humidity_2m_max": "%",
                      "wind_speed_10m_min": "km/h", "wind_speed_10m_max": "km/h"}

    def _location(self) -> dict:
        location = super()._location()
        ZoneInfo(location["timezone"])
        return location

    def _summarize(self, data: dict, end: date) -> dict:
        return {"calendar_averages": summarize_weather_history(data, end)}

    def _valid_summary(self, payload: dict) -> bool:
        averages = payload.get("calendar_averages", {})
        if set(averages) != DAY_KEYS:
            return False
        end = date.fromisoformat(payload["end_date"])
        samples = dict.fromkeys(DAY_KEYS, 0)
        for offset in range((end - START).days + 1):
            samples[(START + timedelta(days=offset)).strftime("%m-%d")] += 1

        def valid_extremes(key, row):
            for kind in ("min", "max"):
                records = row.get("extremes", {}).get(kind, {})
                for field in VARIABLES.values():
                    record = records.get(field, {})
                    year = record.get("year")
                    if not _valid_value(field, record.get("value")) or type(year) is not int:
                        return False
                    try:
                        occurred = date.fromisoformat(f"{year}-{key}")
                    except ValueError:
                        return False
                    if not START <= occurred <= end:
                        return False
            return all(row["extremes"]["min"][field]["value"] <= row[field] + 0.001
                       and row[field] <= row["extremes"]["max"][field]["value"] + 0.001
                       for field in VARIABLES.values())

        return all(isinstance(row, dict)
                   and row.get("samples") == samples[key]
                   and all(_valid_value(field, row.get(field)) for field in VARIABLES.values())
                   and valid_extremes(key, row)
                   for key, row in averages.items())

    def snapshot(self, *, now: datetime | None = None) -> dict:
        """Return today's averages and extremes, keeping the full climatology server-side."""
        payload = super().snapshot()
        calendar = payload.pop("calendar_averages", {})
        if payload.get("status") == "ready":
            current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(payload["location"]["timezone"]))
            payload["date"] = current.date().isoformat()
            payload["averages"] = calendar[current.strftime("%m-%d")]
        return payload
