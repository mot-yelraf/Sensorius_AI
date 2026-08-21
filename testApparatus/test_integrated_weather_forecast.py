"""Cover the full-screen integrated Caelus Weather Forecast.

The tests verify forecast providers, astronomy context, templates, themes, and
dashboard integration without depending on live weather services.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.templating import Jinja2Templates

import sensorius.saiWeatherForecastApp as weather_app
from sensorius.saiWeatherAstronomy import astronomy_context


ROOT = Path(__file__).resolve().parents[1]


def test_caelus_pollinator_theme_is_supported_and_default():
    routes_source = (ROOT / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")
    factory_source = (ROOT / "system_settings" / "factory" / "settings.toml").read_text(encoding="utf-8")

    assert weather_app.normalize_weather_theme("pollinator") == "pollinator"
    assert weather_app.normalize_weather_theme("unsupported") == "pollinator"
    assert "pollinator" in weather_app.WEATHER_THEMES
    assert routes_source.count('get_setting("WeatherForecast", "THEME", "pollinator")') >= 3
    assert 'THEME = "pollinator"' in factory_source


class _Settings:
    values = {
        ("WeatherForecast", "THEME"): "river",
        ("WeatherForecast", "CURRENT_SENSOR_ID"): "nodus-weather",
        ("WeatherForecast", "PROVIDER"): "met_no",
        ("Astral", "LOCATION_NAME"): "Silver City",
    }

    def get_setting(self, section, key, default=None):
        return self.values.get((section, key), default)

    def resolve_astral_location(self, **_kwargs):
        return {"lat": 32.77, "lon": -108.28, "tz": "America/Denver", "source": "manual"}


class _Logger:
    db_path = ":memory:"

    def get_latest_values(self, sensor_id):
        assert sensor_id == "nodus-weather"
        return {
            "Temperature": 20.0,
            "Rel-Humidity": 45.0,
            "Baro-Pressure": 1012.3,
            "Wind Speed": 3.5,
            "Wind Gust": 8.0,
            "Rain Last 24h": 0.15,
            "UV Index": 2.0,
            "Solar Radiation": 350.0,
        }

    def get_latest_timestamp(self, _sensor_id):
        return "2026-08-07T10:00:00-06:00"


class _SensorSettings:
    def get_display_metrics(self, sensor_id):
        assert sensor_id == "nodus-weather"
        return ["Baro-Pressure", "Temperature", "Rel-Humidity", "Wind Speed"]

    def get_setting(self, sensor_id, key, default=None):
        assert sensor_id == "nodus-weather"
        if key == "Sensor.LOCATION":
            return "Kitchen Garden"
        return default


def _forecast_payload():
    hour = {
        "time": "2026-08-07T16:00:00Z",
        "local_time": "2026-08-07T10:00:00-06:00",
        "local_date": "2026-08-07",
        "temp_c": 20.0,
        "rh": 45.0,
        "wind_mps": 2.0,
        "precip_mm": 0.2,
        "precip_probability": 40.0,
        "symbol": "partlycloudy_day",
    }
    return {
        "ok": True,
        "provider": "met_no",
        "location": {"latitude": 32.77, "longitude": -108.28, "timezone": "America/Denver"},
        "current_24h": {
            "overall": "Partly cloudy early",
            "temp_range": "20.0-25.0°C / 68-77°F",
            "rh_range": "35-55%",
            "wind": "Mostly light\n1-4 m/s / 2-9 mph",
            "precip_probability": 40,
        },
        "hourly": [hour] * 24,
        "days": [
            {
                "date": "2026-08-08",
                "label": "Sat Aug 8",
                "forecast": "Clear early",
                "temp_range": "18.0-28.0°C / 64-82°F",
                "rh_range": "30-60%",
                "wind": "Mostly light",
                "precip_mm": 0.0,
                "precip_probability": 42,
            }
        ],
    }


def test_current_weather_metric_adapter_converts_temperature_and_preserves_zero():
    payload = weather_app.normalize_current_weather_readings(
        "nodus-weather",
        {"Temperature": 0.0, "Rel-Humidity": 0.0, "Wind Speed": 0.0, "Baro-Pressure": 1010.0},
        "2026-08-07T10:00:00",
    )
    assert payload["ok"] is True
    assert payload["temperature_c"] == 0.0
    assert payload["temperature_f"] == 32.0
    assert payload["humidity"] == 0.0
    assert payload["wind_speed_mph"] == 0.0


def test_current_readings_follow_configured_display_metrics_order_and_units():
    metrics = weather_app.build_display_metrics(
        {"Temperature": 0.0, "CO2": 734.2, "Rel-Humidity": 45.125},
        ["CO2", "Temperature", "Rel-Humidity", "Missing Metric"],
    )

    assert [metric["name"] for metric in metrics] == [
        "CO2",
        "Temperature",
        "Rel-Humidity",
        "Missing Metric",
    ]
    assert metrics[0] == {"name": "CO2", "value": 734.2, "unit": "ppm"}
    assert metrics[1] == {"name": "Temperature", "value": 32.0, "unit": "°F"}
    assert metrics[2]["value"] == 45.12
    assert metrics[3]["value"] is None


def test_current_readings_can_override_reported_imperial_units_with_metric():
    metrics = weather_app.build_display_metrics(
        {"Temperature_F": 68.0, "Wind Speed": 10.0, "Rain": 1.0, "WH31 CH1 Temperature_F": 77.0},
        ["Temperature_F", "Wind Speed", "Rain", "WH31 CH1 Temperature_F"],
        "Metric",
    )

    assert metrics[0] == {"name": "Temperature_F", "value": 20.0, "unit": "°C"}
    assert metrics[1] == {"name": "Wind Speed", "value": 16.09, "unit": "km/h"}
    assert metrics[2] == {"name": "Rain", "value": 25.4, "unit": "mm"}
    assert metrics[3] == {"name": "WH31 CH1 Temperature_F", "value": 25.0, "unit": "°C"}


def test_weather_display_forecast_uses_canonical_sensorius_contract():
    payload = weather_app.build_weather_display_forecast(_forecast_payload())
    assert payload["ok"] is True
    assert payload["provider_label"] == "MET Norway"
    assert payload["unit_system"] == "Imperial"
    assert payload["high"] == 68
    assert payload["low"] == 68
    assert payload["temperature_unit"] == "°F"
    assert payload["precipitation"] == pytest.approx(0.19)
    assert payload["precipitation_unit"] == "in"
    assert payload["temp_range"] == "68-77°F"
    assert payload["wind"] == "Mostly light\n2-9 mph"
    assert len(payload["hours"]) == 24
    assert payload["hours"][0]["temperature_f"] == 68
    assert payload["hours"][0]["temperature"] == 68
    assert payload["hours"][0]["temperature_unit"] == "°F"
    assert payload["hours"][0]["precipitation"] == pytest.approx(0.01)
    assert payload["hours"][0]["precipitation_unit"] == "in"
    assert payload["hours"][0]["precip_probability"] == 40
    assert payload["hours"][0]["precip_label"] == "Rain chance"
    assert payload["days"][0]["temp_range"] == "64-82°F"
    assert payload["days"][0]["precip_probability"] == 42
    assert payload["days"][0]["precip_label"] == "Rain chance"


def test_weather_display_forecast_uses_metric_units_when_selected():
    source = _forecast_payload()
    source["days"][0]["wind"] = "Mostly light\n1-4 m/s / 2-9 mph"

    payload = weather_app.build_weather_display_forecast(source, "Metric")

    assert payload["unit_system"] == "Metric"
    assert payload["high"] == 20
    assert payload["low"] == 20
    assert payload["temperature_unit"] == "°C"
    assert payload["precipitation"] == pytest.approx(4.8)
    assert payload["precipitation_unit"] == "mm"
    assert payload["temp_range"] == "20.0-25.0°C"
    assert payload["wind"] == "Mostly light\n4-14 km/h"
    assert payload["hours"][0]["temperature"] == 20
    assert payload["hours"][0]["temperature_unit"] == "°C"
    assert payload["hours"][0]["precipitation"] == pytest.approx(0.2)
    assert payload["hours"][0]["precipitation_unit"] == "mm"
    assert payload["days"][0]["temp_range"] == "18.0-28.0°C"
    assert payload["days"][0]["wind"] == "Mostly light\n4-14 km/h"


def test_snow_forecast_uses_snow_chance_label():
    payload = _forecast_payload()
    payload["current_24h"]["overall"] = "Snow showers"
    payload["hourly"] = [{**payload["hourly"][0], "symbol": "snowshowers_day"}]
    payload["days"][0]["forecast"] = "Snow showers"

    display = weather_app.build_weather_display_forecast(payload)

    assert display["precip_label"] == "Snow chance"
    assert display["hours"][0]["precip_label"] == "Snow chance"
    assert display["days"][0]["precip_label"] == "Snow chance"


def test_observation_time_uses_station_local_ampm_without_seconds_or_offset():
    assert weather_app.format_observation_time(
        "2026-08-12T12:35:15.584178Z", "America/Denver"
    ) == "Aug 12, 2026 · 6:35 AM"


def test_windy_map_defaults_to_radar_overlay():
    query = parse_qs(urlparse(weather_app._windy_url(32.77, -108.28)).query)

    assert query["overlay"] == ["radar"]
    assert query["type"] == ["map"]
    assert query["metricRain"] == ["in"]
    assert query["metricTemp"] == ["°F"]
    assert query["metricWind"] == ["mph"]


def test_windy_map_uses_selected_metric_units():
    query = parse_qs(urlparse(weather_app._windy_url(32.77, -108.28, "Metric")).query)

    assert query["metricRain"] == ["mm"]
    assert query["metricTemp"] == ["°C"]
    assert query["metricWind"] == ["km/h"]


def test_windy_map_requires_deliberate_interaction():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text()
    script = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text()
    moon_script = (ROOT / "ui_static" / "weather_forecast" / "moon.js").read_text()

    assert "/ui_static/weather_forecast/moon.js" in template
    assert "data-windy-interaction" in template
    assert "data-windy-guard" in template
    assert 'tabindex="-1"' in template
    assert 'windyGuard.addEventListener("click"' in script
    assert 'windyInteraction.addEventListener("mouseleave"' in script
    assert 'const MOON_VIEW_STORAGE_KEY = "sensorius.moonViewMode";' in moon_script
    assert "referenceBrightLimbAngle = localBrightLimbAngle - localDiskRotation" in moon_script
    assert "window.CaelusMoon = Object.freeze({getViewMode, renderAllMoonDisks, renderMoonDisk, setViewMode});" in moon_script
    assert "const renderMoonDisk = window.CaelusMoon?.renderMoonDisk" in script
    assert 'label.textContent = "Reference orientation · lunar north up";' in script
    assert 'window.addEventListener("sensorius:moon-view-change"' in script


def test_windy_interaction_prompt_sits_on_map_top_border():
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert "place-items: start center" in css
    assert ".windy-map-guard span { transform: translateY(-50%)" in css


def test_hourly_carousel_keeps_hidden_edge_controls_in_fixed_grid_columns():
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert ".hourly-page-previous { grid-column: 1; }" in css
    assert ".hourly-strip { grid-column: 2;" in css
    assert ".hourly-page-next { grid-column: 3; }" in css


def test_caelus_footer_preserves_title_case_attribution():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text()
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()
    javascript = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text()

    assert '<p class="eyebrow">Caelus Weather Forecast</p>' in template
    assert template.index('<p class="eyebrow">Caelus Weather Forecast</p>') < template.index('data-open-caelus-theme')
    assert 'data-open-caelus-theme aria-label="Preview Caelus themes">Theme</button>' in template
    assert 'id="caelusThemeView"' in template
    assert 'data-caelus-preview-theme="pollinator"' in template
    assert 'data-caelus-preview-theme="garden"' in template
    assert 'data-caelus-preview-theme="desert"' in template
    assert "function openCaelusThemeView()" in javascript
    assert "function closeCaelusThemeView()" in javascript
    assert ".caelus-theme-toolbar" in css
    assert "body.caelus-theme-preview-mode .dashboard-shell" in css
    assert "<p>Created by Peace Hill Studios</p>" in template
    footer_rule = css[css.index(".site-footer p {"):css.index("}", css.index(".site-footer p {"))]
    assert "text-transform" not in footer_rule


def test_lunar_timeline_has_four_local_previous_and_upcoming_phases():
    context = astronomy_context(
        type(
            "AstralSettings",
            (),
            {
                "latitude": 32.77,
                "longitude": -108.28,
                "timezone": "America/Denver",
                "location_name": "Test station",
            },
        )(),
        datetime(2026, 8, 9, 18, tzinfo=timezone.utc),
    )

    assert len(context["previous_phases"]) == 4
    assert len(context["upcoming_phases"]) == 4
    assert [phase["representative_date"] for phase in context["previous_phases"]] == sorted(
        phase["representative_date"] for phase in context["previous_phases"]
    )
    assert [phase["representative_date"] for phase in context["upcoming_phases"]] == sorted(
        phase["representative_date"] for phase in context["upcoming_phases"]
    )
    assert context["previous_phases"][1]["name"] == "Buck Moon"
    assert all("bright_limb_angle" in phase for phase in context["previous_phases"] + context["upcoming_phases"])


def test_sunlight_card_uses_ampm_times_and_current_polar_daylight():
    context = astronomy_context(
        type(
            "AstralSettings",
            (),
            {
                "latitude": 32.77,
                "longitude": -108.28,
                "timezone": "America/Denver",
                "location_name": "Test station",
            },
        )(),
        datetime(2026, 8, 19, 18, tzinfo=timezone.utc),
    )

    assert context["sunrise_display"].endswith(" AM")
    assert context["solar_noon_display"].endswith(" PM")
    assert context["sunset_display"].endswith(" PM")
    assert context["moonrise_display"].endswith((" AM", " PM")) or context["moonrise_display"] == "No rise today"
    assert context["moonset_display"].endswith((" AM", " PM")) or context["moonset_display"] == "No set today"
    timeline_start = datetime.fromisoformat(context["timeline_start_at"])
    timeline_sunset = datetime.fromisoformat(context["timeline_sunset_at"])
    timeline_end = datetime.fromisoformat(context["timeline_end_at"])
    timeline_moonrise = datetime.fromisoformat(context["timeline_moonrise_at"])
    timeline_moonset = datetime.fromisoformat(context["timeline_moonset_at"])
    assert timeline_start < timeline_sunset < timeline_end
    assert timeline_start <= timeline_moonrise <= timeline_end
    assert timeline_start <= timeline_moonset <= timeline_end
    assert context["next_sunrise"] == timeline_end.strftime("%H:%M")
    assert context["north_pole_daylight"] == "24h 00m"
    assert context["south_pole_daylight"] == "0h 00m"
    assert context["next_season_label"] == "September Equinox"
    assert context["next_season_date"] == "Sep 22, 2026"
    assert 0 < len(context["next_eclipses"]) <= 3
    assert context["next_eclipses"][0]["kind"] == "Partial lunar eclipse"
    assert context["next_eclipses"][0]["date"] == "Aug 27, 2026"


def test_sunlight_card_places_times_horizontally_and_shows_poles():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text()
    script = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text()
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert 'id="northPoleDaylight"' in template
    assert 'id="southPoleDaylight"' in template
    assert 'id="nextSeasonLabel"' in template
    assert 'id="nextSeasonDate"' in template
    assert template.index('id="northPoleDaylight"') < template.index('id="nextSeasonHeading"')
    assert 'id="nextEclipseHeading"' in template
    assert 'id="nextEclipseList"' in template
    assert "No visible eclipses for the next 12 months" in template
    assert 'id="daylightHours"' not in template
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert ".daylight-times dd { order: -1;" in css
    assert "function formatSolarTime(value)" in script
    assert 'moon.north_pole_daylight ?? "—"' in script
    assert 'moon.next_season_label ?? "—"' in script
    assert "moon.next_eclipses" in script


def test_sunlight_card_uses_three_semantic_text_colors():
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert "--daylight-title-color: var(--accent);" in css
    assert "--daylight-data-color: var(--ink);" in css
    assert "--daylight-status-color: var(--warm);" in css
    assert ".daylight-card .eyebrow," in css
    assert ".daylight-detail-section h3 { color: var(--daylight-title-color); }" in css
    assert ".daylight-times dd { order: -1; margin: 0; color: var(--daylight-data-color);" in css
    assert ".polar-daylight-note strong { display: block; color: var(--daylight-data-color);" in css
    assert ".eclipse-list time { color: var(--daylight-data-color);" in css
    assert ".daylight-times dt { color: var(--daylight-status-color);" in css
    assert ".next-season-note strong { color: var(--daylight-status-color);" in css
    assert ".eclipse-list strong { color: var(--daylight-status-color);" in css


def test_lunar_strip_uses_dates_and_does_not_symmetrize_local_orientation():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text()
    script = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text()
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert 'data-lunar-period="previous"' in template
    assert 'data-lunar-period="upcoming"' in template
    assert "data-phase-date" in template
    assert 'id="lunarEventTimeline"' in template
    assert 'id="forecastNextSunriseMarker"' in template
    assert 'id="forecastMoonriseMarker"' in template
    assert 'id="forecastMoonsetMarker"' in template
    assert 'id="nextSunriseTime"' in template
    assert 'id="lunarMoonriseTime"' in template
    assert 'id="lunarMoonsetTime"' in template
    assert template.index('<div class="lunar-event-row lunar-event-row-moon">') < template.index(
        '<div class="lunar-event-row lunar-event-row-sun">'
    )
    assert "function renderLunarEventTimeline(moon)" in script
    assert 'positionLunarEventMarker("forecastSunsetMarker", sunsetAt, startAt, endAt);' in script
    assert 'positionLunarEventMarker("forecastMoonriseMarker", moonriseAt, startAt, endAt);' in script
    assert 'positionLunarEventMarker("forecastMoonsetMarker", moonsetAt, startAt, endAt);' in script
    assert ".lunar-event-row-sun .lunar-event-track" in css
    assert ".lunar-event-row-moon .lunar-event-marker strong" in css
    assert "pairedPhaseCycle" not in script
    assert 'updatePhaseStrip("previous", moon.previous_phases || [])' in script


def test_open_meteo_hourly_rows_do_not_show_rain_until_precipitation_hour():
    payload = _forecast_payload()
    payload["provider"] = "open_meteo"
    payload["current_24h"]["overall"] = "Clear early, rain/showers evening"
    payload["hourly"] = []
    for hour in range(11, 24):
        payload["hourly"].append(
            {
                "local_time": f"2026-08-07T{hour:02d}:00:00-06:00",
                "temp_c": 25.0,
                "cloud": 5.0 if hour < 15 else 95.0,
                "precip_mm": 2.9 if hour == 21 else 0.0,
                "precip_probability": 75.0 if hour == 21 else 5.0,
                "symbol": "",
            }
        )

    display = weather_app.build_weather_display_forecast(payload)

    assert display["icon"] == "☀️"
    assert display["icon_key"] == "sunny"
    assert len(display["hours"]) == 13
    assert [row["precipitation_mm"] for row in display["hours"][:10]] == [0.0] * 10
    assert display["hours"][10]["precipitation_mm"] == 2.9
    assert display["hours"][10]["icon_key"] == "rain"
    assert display["hours"][10]["precip_probability"] == 75


def test_caelus_hourly_forecast_uses_twelve_hour_clock_labels():
    payload = _forecast_payload()
    payload["hourly"] = [
        {
            "local_time": f"2026-08-08T{hour:02d}:00:00-06:00",
            "temp_c": 20.0,
            "precip_mm": 0.0,
            "precip_probability": 10.0,
            "symbol": "clearsky_day",
        }
        for hour in range(24)
    ]

    display = weather_app.build_weather_display_forecast(payload)

    assert [row["label"] for row in display["hours"]] == [
        "12 AM", "1 AM", "2 AM", "3 AM", "4 AM", "5 AM", "6 AM", "7 AM",
        "8 AM", "9 AM", "10 AM", "11 AM", "12 PM", "1 PM", "2 PM", "3 PM",
        "4 PM", "5 PM", "6 PM", "7 PM", "8 PM", "9 PM", "10 PM", "11 PM",
    ]


@pytest.mark.asyncio
async def test_integrated_weather_routes_render_dashboard_and_namespaced_apis(monkeypatch):
    async def _fake_forecast(*_args, **_kwargs):
        return _forecast_payload()

    monkeypatch.setattr(weather_app, "get_weather_forecast_payload", _fake_forecast)
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=str(ROOT / "ui_templates"))
    app.state.ui_runtime_instance_id = "test-runtime"
    router = APIRouter()
    weather_app.register_weather_forecast_app_routes(
        router,
        app=app,
        settings=_Settings(),
        data_logger=_Logger(),
        sensor_settings_manager=_SensorSettings(),
    )
    app.include_router(router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/weather-forecast")
        current = await client.get("/api/weather-forecast-app/current-readings")
        forecast = await client.get("/api/weather-forecast-app/forecast")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store, max-age=0"
    assert page.headers["pragma"] == "no-cache"
    assert 'class="dashboard-return"' in page.text
    assert '<span class="dashboard-close-icon" aria-hidden="true">&times;</span>' in page.text
    assert ">Dashboard<" not in page.text
    assert "System Settings" not in page.text
    assert "/ui_static/weather_forecast/app.js" in page.text
    assert "theme-river" in page.text
    assert '<h1 id="station-title">Silver City</h1>' in page.text
    assert "Last observation <time datetime=\"2026-08-07T10:00:00-06:00\">Aug 7, 2026 · 10:00 AM</time>" in page.text
    assert page.text.count("data-hourly-index=") == 24
    assert 'data-hourly-index="7"' in page.text
    assert 'data-hourly-index="8" hidden' in page.text
    assert "data-hourly-previous" in page.text
    assert "data-hourly-next" in page.text
    assert ">40%</strong> Rain chance" in page.text
    assert "40% rain chance" in page.text
    assert "42% rain chance" in page.text
    assert "PoP" not in page.text
    assert 'class="forecast-icon forecast-icon--rain"' in page.text
    assert 'class="forecast-icon forecast-icon--sunny"' in page.text
    assert current.json()["sensor_id"] == "nodus-weather"
    assert current.json()["location"] == "Kitchen Garden"
    assert current.json()["refresh_interval_sec"] == 60
    assert current.json()["temperature_f"] == 68.0
    assert current.json()["display_metrics"][0] == {
        "name": "Baro-Pressure",
        "value": 29.89,
        "unit": "inHg",
    }
    assert "Baro-Pressure" in page.text
    assert '<h2 data-readings-sensor>Kitchen Garden</h2>' in page.text
    readings_panel = page.text[
        page.text.index('class="glass-card readings-panel"'):
        page.text.index('class="glass-card forecast-panel"')
    ]
    assert 'class="station-state readings-footer is-live"' in readings_panel
    assert "Station reporting" in readings_panel
    assert "Station reporting" not in page.text[:page.text.index('id="conditions"')]
    assert "Environmental decisions" not in page.text
    assert 'class="glass-card map-card full-width-map"' in page.text
    assert page.text.index('id="conditions"') < page.text.index('id="map"') < page.text.index('id="moon"')
    assert "overlay=radar" in page.text
    assert forecast.json()["days"][0]["label"] == "Sat Aug 8"


def test_caelus_card_refreshes_use_sensor_hourly_and_sunlight_cadences():
    script = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text(encoding="utf-8")

    assert 'fetch("/api/weather-forecast-app/current-readings", {cache: "no-store"})' in script
    assert "seconds * 1000" in script
    assert 'fetch("/weather-forecast?force_refresh=true", {cache: "no-store"})' in script
    assert "hourMs - (Date.now() % hourMs)" in script
    assert "window.setInterval(refreshAstronomy, 5 * 60 * 1000);" in script


def test_ecowitt_current_readings_refresh_uses_configured_poll_interval():
    class EcowittSettings(_Settings):
        values = {
            **_Settings.values,
            ("Ecowitt", "POLL_INTERVAL_SEC"): 420,
        }

    class EcowittSensorSettings(_SensorSettings):
        def get_setting(self, sensor_id, key, default=None):
            assert sensor_id == "nodus-weather"
            return "ecowitt" if key == "Sensor.DEVICE" else default

    service = weather_app.WeatherForecastAppService(
        settings=EcowittSettings(),
        data_logger=_Logger(),
        sensor_settings_manager=EcowittSensorSettings(),
    )

    assert service.current_readings()["refresh_interval_sec"] == 420


@pytest.mark.asyncio
async def test_weather_forecast_background_warm_is_reused_by_first_page_request(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def _slow_forecast(*_args, **_kwargs):
        calls.append(1)
        started.set()
        await release.wait()
        return _forecast_payload()

    monkeypatch.setattr(weather_app, "get_weather_forecast_payload", _slow_forecast)
    service = weather_app.WeatherForecastAppService(
        settings=_Settings(),
        data_logger=_Logger(),
        sensor_settings_manager=_SensorSettings(),
    )

    service.ensure_background_warm()
    await started.wait()
    forecast_task = asyncio.create_task(service.forecast())
    await asyncio.sleep(0)
    assert calls == [1]

    release.set()
    result = await forecast_task
    assert result["ok"] is True
    assert calls == [1]


def test_dashboard_button_launches_full_screen_weather_app():
    html_source = (ROOT / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    assert "window.location.assign('/weather-forecast')" in html_source


def test_dashboard_background_theme_normalization():
    from sensorius.saiHtml import normalize_dashboard_background_theme, normalize_dashboard_metric_set

    assert normalize_dashboard_background_theme("leaf-crop") == "leaf_crop"
    assert normalize_dashboard_background_theme("flower") == "flower"
    assert normalize_dashboard_background_theme("unsupported") == "leaf"
    assert normalize_dashboard_metric_set("All") == "All"
    assert normalize_dashboard_metric_set("show-all") == "All"
    assert normalize_dashboard_metric_set("unsupported") == "Pick 6"


def test_dashboard_elemental_card_palettes_have_high_contrast():
    def relative_luminance(hex_color: str) -> float:
        channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2])

    def contrast_ratio(background: str, foreground: str) -> float:
        lighter, darker = sorted((relative_luminance(background), relative_luminance(foreground)), reverse=True)
        return (lighter + 0.05) / (darker + 0.05)

    palettes = {
        "earth": ("#efe2c6", "#2f2114"),
        "air": ("#c4dcf8", "#132f38"),
        "water": ("#cbdbed", "#102f44"),
        "fire": ("#fde1d3", "#3b1c12"),
    }
    assert all(contrast_ratio(background, foreground) >= 7 for background, foreground in palettes.values())


def test_dashboard_reuses_detailed_forecast_moon_surface_and_selectable_backgrounds():
    html_source = (ROOT / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    dashboard_css = (ROOT / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "dashboardMoonSurfaceImage.src = '/ui_static/weather_forecast/moon-surface.png?v=1'" in html_source
    assert "surfacePixels[sourceOff] * brightness" in html_source
    assert "const textureRadius = Math.min(w, h) * 0.44" in html_source
    assert "edgeDistance / 1.35" in html_source
    assert "#moonPhaseCanvas{width:88px;height:88px;border:0;border-radius:50%;background:transparent;}" in html_source
    assert "ctx.strokeStyle = 'rgba(255, 240, 198, 0.28)'" not in html_source
    assert "dashboard-theme-{dashboard_background_class}" in html_source
    assert ".dash-loc-form{display:flex;flex-direction:column;align-items:stretch;justify-content:flex-start;gap:.45rem;background:#e6faff" in html_source
    assert ".astro-box{display:flex;align-items:flex-start;justify-content:center;background:#ffffe0" in html_source
    assert 'background-image:url("/ui_static/leaf-pattern.svg")' in dashboard_css
    assert "dashboard-theme-leaf{background-color:#dff5e8" in dashboard_css
    for theme, asset in (
        ("root", "roots-greenhouse.webp"),
        ("leaf-crop", "leaf-greenhouse.webp"),
        ("flower", "flowers-greenhouse.webp"),
        ("fruit", "fruit-greenhouse.webp"),
    ):
        assert f"dashboard-theme-{theme}" in dashboard_css
        assert f'background-image:url("/ui_static/backgrounds/{asset}")' in dashboard_css
        assert (ROOT / "ui_static" / "backgrounds" / asset).is_file()
    assert "background-color:var(--dashboard-card-bg)" in dashboard_css
    assert "color:var(--dashboard-card-text)" in dashboard_css
    assert "--dashboard-dialog-bg:color-mix(in srgb,var(--dashboard-card-bg) 82%,white)" in dashboard_css
    assert "body.dashboard-page #setupPiModal .system-settings-shell" in dashboard_css
    assert "body.dashboard-page .modal input:not([type=\"checkbox\"]):not([type=\"radio\"])" in dashboard_css
    assert ".dash-theme-trigger" in dashboard_css
    assert "align-self:center;\n  width:auto;\n  min-width:118px" in dashboard_css
    assert "border-radius:999px;\n  background:var(--dashboard-card-bg)" in dashboard_css
    assert "letter-spacing:.03em;\n  text-transform:uppercase" in dashboard_css
    assert ".dashboard-theme-toolbar" in dashboard_css
    assert "body.dashboard-theme-preview-mode .dashboard-content" in dashboard_css


def test_dashboard_forecast_combines_selected_sensor_readings_with_system_unit_ranges():
    dashboard_source = (ROOT / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    routes_source = (ROOT / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")

    assert "forecastCurrentTemperature(data)" in dashboard_source
    assert "forecastCurrentHumidity(data)" in dashboard_source
    assert "`${currentValue} : ${range}`" in dashboard_source
    assert 'payload["unit_system"] = display_unit_system' in routes_source
    assert 'payload["current_readings"] = await asyncio.to_thread' in routes_source


def test_current_readings_primary_value_scales_inside_its_card():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text(encoding="utf-8")
    stylesheet = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text(encoding="utf-8")

    assert 'class="reading-primary"' in template
    assert ".readings-panel { container-type: inline-size;" in stylesheet
    assert "grid-template-columns: minmax(5.5rem, 34%) minmax(0, 1fr)" in stylesheet
    assert "font-size: clamp(3.25rem, 15cqw, 6.5rem)" in stylesheet


def test_sunlight_path_contrast_and_sun_position_match_caelus():
    stylesheet = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "ui_static" / "weather_forecast" / "app.js").read_text(encoding="utf-8")

    assert "border: 1px solid var(--daylight-track-color);" in stylesheet
    assert "border-bottom: 1px solid var(--daylight-horizon-color);" in stylesheet
    assert "--daylight-track-color: #765000;" in stylesheet
    assert "--daylight-sun-color: #d96f00;" in stylesheet
    assert "color: var(--daylight-sun-color);" in stylesheet
    assert "bottom: calc(var(--sun-rise, 0) * 5rem);" in stylesheet
    assert "transform: translate(-50%, 50%);" in stylesheet
    assert "Math.sqrt(1 - horizontalOffset ** 2)" in script


def test_weather_forecast_system_settings_are_present():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    stylesheet = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text(encoding="utf-8")
    dashboard_source = (ROOT / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    user_guide = (ROOT / "docs" / "user_guide.md").read_text(encoding="utf-8")
    assert "system-weather-forecast" in template
    assert 'name="weather_forecast_theme"' in template
    assert 'name="weather_forecast_sensor_id"' in template
    weather_section = template[
        template.index('data-runtime-section="system-weather-forecast"'):
        template.index('<div class="settings-pane" id="pane-automations"')
    ]
    assert 'class="weather-forecast-controls"' in weather_section
    assert weather_section.index('for="weather_forecast_sensor_id"') < weather_section.index('for="weather_forecast_provider"')
    display_section = template[
        template.index('data-runtime-section="system-display"'):
        template.index('data-runtime-section="system-general"')
    ]
    assert 'id="weather_forecast_theme"' not in weather_section
    assert display_section.index('id="dashboard_background_theme"') < display_section.index('id="weather_forecast_theme"')
    for theme in ("pollinator", "garden", "island", "river", "desert"):
        assert f'name="weather_forecast_theme" value="{theme}"' in display_section
    caelus_builtin = display_section[
        display_section.index('id="weather_forecast_theme"'):
        display_section.index('{% for theme in custom_themes.caelus')
    ]
    assert caelus_builtin.count('class="thumbnail-option"><input type="radio" name="weather_forecast_theme"') == 5
    assert ".theme-pollinator {" in stylesheet
    assert '--scene-image: url("/ui_static/pollinator-pattern.svg");' in stylesheet
    assert '--scene-repeat: repeat;' in stylesheet
    assert (ROOT / "ui_static" / "pollinator-pattern.svg").is_file()
    assert "Sunny Beach" in display_section
    assert "Ocean Island" not in display_section
    assert "sunny-beach.webp" in template
    assert "desert-clear.webp" in template
    assert "sunny-beach.webp" in stylesheet
    assert "desert-clear.webp" in stylesheet
    assert ".theme-desert .glass-card:not(.lunar-header)" in stylesheet
    assert "--glass: rgba(229, 241, 221, 0.88)" in stylesheet
    assert "--ink: #18382b" in stylesheet
    assert "--muted: rgba(24, 56, 43, 0.78)" in stylesheet
    assert not (ROOT / "ui_static" / "weather_forecast" / "backgrounds" / "island.webp").exists()
    assert not (ROOT / "ui_static" / "weather_forecast" / "backgrounds" / "desert.webp").exists()
    assert "Failed to refresh General Settings state" in dashboard_source
    assert "input[name=\\\"weather_forecast_theme\\\"]:checked" in dashboard_source
    assert template.index('data-runtime-section="system-astral"') < template.index('data-runtime-section="system-weather-forecast"')
    assert template.index('data-runtime-section="system-notifications"') < template.index('data-runtime-section="system-weather-forecast"')
    assert "The forecast uses the Sensorius Astral location." not in template
    assert "Current Readings panel follows the selected sensor's configured **Display Metrics**" in " ".join(user_guide.split())
    assert 'fetch("/sensor-directory"' in template
    assert 'select.dataset.hydrated = rows.length ? "1" : "0"' in template
    assert "hydrateWeatherForecastSensors(attempt + 1)" in template
    assert "if (activePaneId) setActivePane(activePaneId);" in template
    assert 'select.dataset.hydrated === "1" && select.options.length > 1' not in template
    system_pane_activation = template[
        template.index('if (paneId === "pane-system")'):
        template.index('if (paneId === "pane-integrations")')
    ]
    assert "hydrateWeatherForecastSensors();" in system_pane_activation


def test_six_day_dialog_uses_selected_theme_palette():
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text(encoding="utf-8")

    assert ".forecast-dialog {" in css
    assert "background: var(--glass-strong)" in css
    assert ".forecast-detail-grid article" in css
    assert "background: var(--glass)" in css
    assert "color: var(--accent)" in css
    assert ".forecast-dialog > header button { position: relative" in css
    assert "border-radius: 50%" in css
    assert "background: transparent" in css
    assert ".forecast-dialog > header button::before" in css
    assert "transform: translate(-50%, -50%) rotate(45deg)" in css
    assert "transform: translate(-50%, -50%) rotate(-45deg)" in css
