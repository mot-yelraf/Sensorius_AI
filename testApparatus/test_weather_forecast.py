from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saiWeatherForecast import (
    build_forecast_payload,
    load_weather_forecast_cache,
    normalize_met_forecast,
    save_weather_forecast_cache,
)


def _sample_met_payload() -> dict:
    tzinfo = ZoneInfo("America/Denver")
    start = datetime(2026, 6, 4, 0, 0, tzinfo=tzinfo)
    timeseries = []
    for idx in range(24 * 6):
        local_dt = start + timedelta(hours=idx)
        hour = local_dt.hour
        day_idx = idx // 24
        if day_idx == 0:
            temp_c = 14.3 + ((22.8 - 14.3) * (hour / 23.0))
        elif day_idx == 1:
            temp_c = 15.1 + ((26.5 - 15.1) * (hour / 23.0))
        else:
            temp_c = 13.0 + ((25.0 - 13.0) * (hour / 23.0))
        precip_mm = 0.2 if 13 <= hour <= 16 else 0.0
        cloud = 88.0 if hour < 8 else (62.0 if hour < 18 else 24.0)
        timeseries.append(
            {
                "time": local_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "data": {
                    "instant": {
                        "details": {
                            "air_temperature": round(temp_c, 1),
                            "relative_humidity": round(64.0 - (30.0 * (hour / 23.0)), 1),
                            "wind_speed": round(2.0 + (2.0 * (hour / 23.0)), 1),
                            "cloud_area_fraction": cloud,
                        }
                    },
                    "next_1_hours": {
                        "summary": {"symbol_code": "lightrainshowers" if precip_mm else "cloudy"},
                        "details": {"precipitation_amount": precip_mm},
                    },
                },
            }
        )
    return {"properties": {"timeseries": timeseries}}


def test_met_forecast_is_summarized_for_dashboard_card():
    hourly = normalize_met_forecast(_sample_met_payload(), tz_name="America/Denver")
    payload = build_forecast_payload(
        provider="met_no",
        latitude=32.7701,
        longitude=-108.2803,
        tz_name="America/Denver",
        hourly=hourly,
        location_source="settings",
        retrieved_utc="2026-06-04T06:25:00Z",
    )

    current = payload["current_24h"]
    assert payload["ok"] is True
    assert current["temp_range"] == "58-73°F / 14.3-22.8°C"
    assert current["wind"] == "Mostly light/moderate\n~2-4 m/s / 4-9 mph"
    assert current["rh_range"] == "~34-64%"
    assert "Cloudy early" in current["overall"]
    assert "light rain/showers afternoon" in current["overall"]
    assert "clearing overnight" in current["overall"]

    first_day = payload["days"][0]
    assert first_day["label"] == "Fri Jun 5"
    assert first_day["temp_range"] == "59-80°F / 15.1-26.5°C"


def test_weather_forecast_cache_round_trip(tmp_path):
    hourly = normalize_met_forecast(_sample_met_payload(), tz_name="America/Denver")
    payload = build_forecast_payload(
        provider="met_no",
        latitude=32.7701,
        longitude=-108.2803,
        tz_name="America/Denver",
        hourly=hourly,
        location_source="settings",
        retrieved_utc="2026-06-04T06:25:00Z",
    )
    db_path = tmp_path / "forecast.db"

    save_weather_forecast_cache(str(db_path), payload)

    loaded = load_weather_forecast_cache(str(db_path), latitude=32.7702, longitude=-108.2804)
    assert loaded is not None
    assert loaded["provider"] == "met_no"
    assert loaded["current_24h"]["temp_range"] == payload["current_24h"]["temp_range"]

    assert load_weather_forecast_cache(str(db_path), latitude=33.0, longitude=-108.2804) is None
