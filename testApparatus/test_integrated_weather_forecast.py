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


def _forecast_payload():
    hour = {
        "time": "2026-08-07T16:00:00Z",
        "local_time": "2026-08-07T10:00:00-06:00",
        "local_date": "2026-08-07",
        "temp_c": 20.0,
        "rh": 45.0,
        "wind_mps": 2.0,
        "precip_mm": 0.2,
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
    assert metrics[1] == {"name": "Temperature", "value": 0.0, "unit": "°C"}
    assert metrics[2]["value"] == 45.12
    assert metrics[3]["value"] is None


def test_weather_display_forecast_uses_canonical_sensorius_contract():
    payload = weather_app.build_weather_display_forecast(_forecast_payload())
    assert payload["ok"] is True
    assert payload["provider_label"] == "MET Norway"
    assert payload["hours"][0]["temperature_f"] == 68
    assert payload["days"][0]["temp_range"].endswith("64-82°F")


def test_windy_map_defaults_to_radar_overlay():
    query = parse_qs(urlparse(weather_app._windy_url(32.77, -108.28)).query)

    assert query["overlay"] == ["radar"]
    assert query["type"] == ["map"]


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
    assert "window.CaelusMoon = Object.freeze({renderMoonDisk});" in moon_script
    assert "const renderMoonDisk = window.CaelusMoon?.renderMoonDisk" in script


def test_windy_interaction_prompt_sits_on_map_top_border():
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert "place-items: start center" in css
    assert ".windy-map-guard span { transform: translateY(-50%)" in css


def test_caelus_footer_preserves_title_case_attribution():
    template = (ROOT / "ui_templates" / "weather_forecast" / "index.html").read_text()
    css = (ROOT / "ui_static" / "weather_forecast" / "app.css").read_text()

    assert '<p class="eyebrow">Caelus Weather Forecast</p>' in template
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
        datetime(2026, 8, 10, 18, tzinfo=timezone.utc),
    )

    assert context["sunrise_display"].endswith(" AM")
    assert context["solar_noon_display"].endswith(" PM")
    assert context["sunset_display"].endswith(" PM")
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

    assert 'data-lunar-period="previous"' in template
    assert 'data-lunar-period="upcoming"' in template
    assert "data-phase-date" in template
    assert "pairedPhaseCycle" not in script
    assert 'updatePhaseStrip("previous", moon.previous_phases || [])' in script


def test_open_meteo_hour_windows_do_not_show_rain_until_precipitation_window():
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
                "symbol": "",
            }
        )

    display = weather_app.build_weather_display_forecast(payload)

    assert display["icon"] == "☀️"
    assert display["icon_key"] == "sunny"
    assert [window["precipitation_mm"] for window in display["hours"][:4]] == [0.0, 0.0, 0.0, 2.9]
    assert [window["icon"] for window in display["hours"][:4]] == ["☀️", "🌤️", "☁️", "🌧️"]
    assert [window["icon_key"] for window in display["hours"][:4]] == [
        "sunny",
        "partly-cloudy",
        "cloudy",
        "rain",
    ]


def test_caelus_hourly_forecast_uses_twelve_hour_clock_labels():
    payload = _forecast_payload()
    payload["hourly"] = [
        {
            "local_time": f"2026-08-08T{hour:02d}:00:00-06:00",
            "temp_c": 20.0,
            "precip_mm": 0.0,
            "symbol": "clearsky_day",
        }
        for hour in range(24)
    ]

    display = weather_app.build_weather_display_forecast(payload)

    assert [window["label"] for window in display["hours"]] == [
        "12 AM",
        "3 AM",
        "6 AM",
        "9 AM",
        "12 PM",
        "3 PM",
        "6 PM",
        "9 PM",
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
    assert 'class="dashboard-return"' in page.text
    assert ">Dashboard<" in page.text
    assert "System Settings" not in page.text
    assert "/ui_static/weather_forecast/app.js" in page.text
    assert "theme-river" in page.text
    assert '<h1 id="station-title">Silver City</h1>' in page.text
    assert 'class="forecast-icon forecast-icon--rain"' in page.text
    assert 'class="forecast-icon forecast-icon--sunny"' in page.text
    assert current.json()["sensor_id"] == "nodus-weather"
    assert current.json()["temperature_f"] == 68.0
    assert current.json()["display_metrics"][0] == {
        "name": "Baro-Pressure",
        "value": 1012.3,
        "unit": "hPa",
    }
    assert "Baro-Pressure" in page.text
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
    from sensorius.saiHtml import normalize_dashboard_background_theme

    assert normalize_dashboard_background_theme("garden-tools") == "garden_tools"
    assert normalize_dashboard_background_theme("pollinator") == "pollinator"
    assert normalize_dashboard_background_theme("unsupported") == "leaf"


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
    assert 'background-image:url("/ui_static/leaf-pattern.svg")' in dashboard_css
    assert 'background-image:url("/ui_static/garden-tools-pattern.svg")' in dashboard_css
    assert 'background-image:url("/ui_static/herbarium-pattern.svg")' in dashboard_css
    assert 'background-image:url("/ui_static/pollinator-pattern.svg")' in dashboard_css
    assert "dashboard-theme-leaf{background-color:#dff5e8" in dashboard_css
    assert "dashboard-theme-garden-tools{background-color:#ead8c2" in dashboard_css
    assert "dashboard-theme-herbarium{background-color:#f8dcc4" in dashboard_css
    assert "dashboard-theme-pollinator{background-color:#dceffc" in dashboard_css
    assert "dashboard-theme-pollinator .metric-container" in dashboard_css
    assert "background-color:#c8e4f7" in dashboard_css
    assert "dashboard-theme-white" in dashboard_css
    for pattern in (
        "leaf-pattern.svg",
        "garden-tools-pattern.svg",
        "herbarium-pattern.svg",
        "pollinator-pattern.svg",
    ):
        assert (ROOT / "ui_static" / pattern).is_file()


def test_weather_forecast_system_settings_are_present():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    user_guide = (ROOT / "docs" / "user_guide.md").read_text(encoding="utf-8")
    assert "system-weather-forecast" in template
    assert 'name="weather_forecast_theme"' in template
    assert 'name="weather_forecast_sensor_id"' in template
    weather_section = template[
        template.index('data-runtime-section="system-weather-forecast"'):
        template.index('data-runtime-section="system-notifications"')
    ]
    assert 'class="weather-forecast-controls"' in weather_section
    assert weather_section.index('for="weather_forecast_sensor_id"') < weather_section.index('for="weather_forecast_provider"')
    assert weather_section.index('for="weather_forecast_provider"') < weather_section.index('id="weather_forecast_theme"')
    for theme in ("garden", "island", "river", "desert"):
        assert f'name="weather_forecast_theme" value="{theme}"' in weather_section
    assert template.index('data-runtime-section="system-astral"') < template.index('data-runtime-section="system-weather-forecast"')
    assert template.index('data-runtime-section="system-weather-forecast"') < template.index('data-runtime-section="system-notifications"')
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
