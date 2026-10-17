"""Serve an isolated Sensorius dashboard fixture for local Playwright checks.

The host exercises the real dashboard renderer and static assets without
starting hardware, MQTT, service, or persistent production runtime tasks.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="sensorius-playwright-")
os.environ.setdefault("SENSORIUS_PROJECT_ROOT", str(REPO_ROOT))
os.environ.setdefault("SENSORIUS_RUNTIME_ROOT", _RUNTIME_DIR.name)

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from sensorius.saiHtml import get_gauge_config, render_dashboard  # noqa: E402


app = FastAPI()
app.mount("/ui_static", StaticFiles(directory=str(REPO_ROOT / "ui_static")), name="ui_static")
templates = Jinja2Templates(directory=str(REPO_ROOT / "ui_templates"))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Report readiness to the Playwright web-server controller."""
    return "ok"


@app.get("/documentation-capture")
def documentation_capture() -> dict:
    """Identify this isolated capture host without reading machine settings."""
    return {"capture_id": os.environ.get("SENSORIUS_DOCS_CAPTURE_ID", "")}


@app.get("/switch/history")
def switch_history() -> dict:
    """Provide empty switch history without reading a production database."""
    return {"events": []}


@app.websocket("/ws/switch-updates")
async def switch_updates(websocket: WebSocket) -> None:
    """Keep the dashboard's normal switch-status socket open during the check."""
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return


@app.get("/", response_class=HTMLResponse)
def dashboard(battery: str = "", station: str = "", documentation: bool = False, switches: bool = False, switches_first: bool = False) -> HTMLResponse:
    """Render a deterministic dashboard with enough metrics to test interaction."""
    sensor_id = station or ("ecowitt-pr-check" if battery else "aht-pr-check")
    metrics = {
        "Temperature": 72.4,
        "Temperature_F": 72.4,
        "Rel-Humidity": 48.0,
        "Ambient VPD": 1.2,
        "Dew Point": 51.0,
        "CO2": 612.0,
        "Gas": 104.0,
        "Baro-Pressure": 1013.2,
    }
    if documentation:
        sensor_id = "avpd-demo02"
    available = ["lux-demo01", sensor_id] if documentation else [sensor_id]
    values = {sid: dict(metrics) for sid in available}
    controllers = {
        "switch-pr-check": SimpleNamespace(
            is_present=True, switch_id="switch-pr-check", location="Test room",
            switches=["Fan"], last_state={"Fan": False},
            channel_id_for_label={"Fan": "S1-pr-check"},
        ),
    } if switches else {}
    if documentation:
        controllers = {
            sid: SimpleNamespace(
                is_present=True, switch_id=sid, location=location,
                switches=["Fan"], last_state={"Fan": False},
                channel_id_for_label={"Fan": channel},
            )
            for sid, location, channel in [
                ("switch-demo01", "Garden Beds", "s1-demo01"),
                ("switch-demo02", "Greenhouse", "s1-demo02"),
            ]
        }
    html = "".join(
        render_dashboard(
            "All",
            None,
            available,
            values,
            {},
            SimpleNamespace(expected_gauge_map={}),
            switch_controllers=controllers,
            sensor_locations={"lux-demo01": "Garden Beds", "avpd-demo02": "Greenhouse"} if documentation else None,
            dashboard_row_order=["__switches__", sensor_id] if switches_first else None,
            battery_statuses={sensor_id: battery} if battery or station else {},
            gauge_config=get_gauge_config(),
            expected_gauge_map={sid: list(metrics) for sid in available},
            expected_display_style_map={sensor_id: {}},
            display_style="Gauge",
            dashboard_background_theme="fruit" if documentation else "leaf",
            astro_payload={"ok": False, "reason": "documentation_fixture"} if documentation else None,
            biodynamic_payload={"ok": False, "reason": "documentation_fixture"} if documentation else None,
        )
    )
    return HTMLResponse(html)


@app.get("/calendar", response_class=HTMLResponse)
def biodynamic_calendar(request: Request):
    """Render the real integrated calendar shell for browser interaction tests."""
    return templates.TemplateResponse(
        request,
        "biodynamic_calendar/index.html",
        {
            "config": None,
            "plantings": [],
            "app_version": "playwright",
            "sensorius_launch": True,
            "biodynamic_calendar_theme": "garden_tools",
            "biodynamic_calendar_resolved_theme": "garden_tools",
            "biodynamic_calendar_automatic_theme": "autumn",
            "biodynamic_calendar_theme_style": "",
            "runtime_instance_id": "playwright",
        },
    )


@app.get("/edit-system", response_class=HTMLResponse)
def system_settings(documentation: bool = False) -> HTMLResponse:
    """Render the actual settings dialog without production settings or hardware."""
    from sensorius import __version__
    from sensorius.saiThemeManager import ThemeManager

    example = {
        "hostname": "hub.example.com", "httpport": 8000,
        "astral_lat": 39.7, "astral_lon": -104.9,
        "dashboard_background_theme": "fruit", "weather_forecast_theme": "desert",
        "biodynamic_calendar_theme": "spring", "theme_palettes": ThemeManager.palettes(),
        "broker": "mqtt.example.com", "mqttport": 1883, "tz": "UTC",
        "astral_location_name": "Example Garden", "email_smtp_host": "smtp.example.com",
        "email_username": "operator@example.com", "email_from": "sensorius@example.com",
        "email_to": "gardener@example.com", "email_enabled": True,
        "email_security": "ssl", "email_password_configured": True,
    } if documentation else {}
    return HTMLResponse(templates.get_template("modals/system_settings.html").render(
        app_name_long="Sensorius Automatio Instrumentorum" if documentation else "Sensorius AI",
        app_version=__version__ if documentation else "playwright", custom_themes={},
        display_style="Gauge", metric_set="Pick 6", gauge_size="Small",
        unit_system="Imperial", astral_altitude=1000, astral_altitude_unit="ft",
        **example,
    ))


@app.get("/edit-sensor", response_class=HTMLResponse)
def sensor_settings(sensor_id: str = "aht-pr-check", documentation: bool = False) -> HTMLResponse:
    """Render a sensor's real settings and calibration panes for mobile checks."""
    from sensorius.saiInputUnits import calibration_presentation
    offsets = [{"key": "Calibration.Device.TEMP_OFFSET", "label": "Temperature", "unit": "°C", "value": 1.0}]
    presentation = calibration_presentation(offsets, 0.0, "Imperial")
    example = {
        "nodus_board_type": "xesp32s3", "nodus_sensor_hardware": "BME280",
        "offline_events_24h": 1, "last_offline_epoch": 1791977400,
        "last_offline_event_label": "2026-10-14 11:30:00 UTC",
        "last_packet_epoch": 1791979195, "data_packets_received": 1200,
    } if documentation else {}
    return HTMLResponse(templates.get_template("modals/sensor_settings.html").render(
        device_kind="aht", device_offsets=offsets, **presentation,
        candidate_sensors=["aht-pr-check", "reference"],
        sensor_id=sensor_id, is_ecowitt=sensor_id.startswith("ecowitt-"),
        battery_status="OK", network_info={"ip_address": "192.0.2.20", "broker": "mqtt.example.com", "broker_status": "Connected"}
        if documentation else {"ip_address": "192.0.2.10"}, current_metrics=["Temperature"] + [""] * 5,
        metric_options=["", "Temperature"], settings={}, location="Greenhouse",
        **example,
    ))


@app.get("/edit-switch", response_class=HTMLResponse)
def switch_settings(documentation: bool = False) -> HTMLResponse:
    """Render a remote switch dialog without contacting a device."""
    example = {
        "nodus_board_type": "xesp32s3",
        "network_info": {"ip_address": "192.0.2.30", "broker": "mqtt.example.com", "broker_status": "Connected"},
        "offline_events_24h": 0, "switch_last_packet_epoch": 1791979195,
        "switch_channel_state_stats": [
            {"channel_id": "s1-demo", "row_label": "S1 current state, age:", "state": "Off", "state_epoch": 1791977400},
            {"channel_id": "s2-demo", "row_label": "S2 current state, age:", "state": "On", "state_epoch": 1791978600},
        ],
    } if documentation else {}
    return HTMLResponse(templates.get_template("modals/switch_settings.html").render(
        switch_id="switch-demo" if documentation else "switch-pr-check", settings={"Switch": {"SWITCH_LOCATION": "Greenhouse"}},
        channels=[{"index": 0, "label": "Pump"}], channel_indices=[0],
        **example,
    ))


@app.get("/api/weather-alerts")
def weather_alerts():
    """Keep browser checks independent of live NWS weather and locations."""
    return {"enabled": False, "active": False, "events": [], "message": ""}


@app.get("/weather-forecast", response_class=HTMLResponse)
def weather_forecast(request: Request, units: str = "Metric", native: bool = False, battery: str = ""):
    """Render the real Caelus page with a complete, deterministic six-day outlook."""
    from sensorius.saiWeatherForecastApp import build_weather_display_forecast

    start = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    forecast = build_weather_display_forecast({
        "ok": True, "provider": "us" if native else "open_meteo",
        "current_24h": {"overall": "Cloudy early, clearing late", "precip_probability": 55, "rh_range": "35-85%", "wind": "Mostly light\n1-8 m/s / 2-18 mph"},
        "hourly": [{
            "time": (start + timedelta(hours=hour)).isoformat(),
            "local_time": (start + timedelta(hours=hour)).isoformat(),
            "temp_c": 30 - hour / 2, "precip_probability": 55,
            "narrative": "Sunny, with a high near 80. West wind 5 to 10 mph. " * 12 if native else "",
            "narrative_period": "This Afternoon",
            "symbol": "partlycloudy_day" if hour < 5 else "rain", "rh": 55, "wind_mps": 4,
        } for hour in range(24)],
        "days": [{
            "date": (start + timedelta(days=day)).date().isoformat(),
            "label": (start + timedelta(days=day)).strftime("%a %b %d"),
            "forecast": "Partly cloudy", "temp_range": "17.8-30.0°C / 64-86°F",
            "rh_range": "35-85%", "wind": "Mostly light\n1-8 m/s / 2-18 mph",
            "precip_probability": 59,
        } for day in range(1, 7)],
    }, units)
    return templates.TemplateResponse(request, "weather_forecast/index.html", {
        "settings": {"theme": "pollinator", "theme_class": "pollinator"},
        "location": {"name": "Greenhouse", "latitude": 39.7, "longitude": -104.9},
        "latest": {"is_ecowitt": True, "battery_status": battery if battery in {"OK", "LOW"} else "UNKNOWN", "sensor_id": "ecowitt-pr-check", "location": "Weather Station", "ok": True} if battery else {}, "moon": {"updated_at": start.isoformat()},
        "forecast": forecast, "app_version": "playwright",
    })


@app.get("/ac1100-fixture", response_class=HTMLResponse)
def ac1100_dashboard() -> HTMLResponse:
    """Render the actual switch card with an isolated AC1100 definition."""
    from sensorius.saiSwitchSettingsManager import SwitchSettingsManager

    sid = "ecowitt-aabbccddeeff-ac1100-000008d1"
    channel_id = f"{sid}-1"
    SwitchSettingsManager().save(sid, {"Switch": {
        "TYPE": "ecowitt", "DEVICE": "AC1100", "SWITCH_DEVICE_ID": sid,
        "SWITCH_LOCATION": "Greenhouse", "SWITCH_1_LABEL": "Plug",
        "SWITCH_1_CHANNEL_ID": channel_id,
    }})
    controller = SimpleNamespace(
        switch_id=sid, location="Greenhouse", is_present=True, is_ecowitt=True,
        available=True, confirmed=True, switches=["Plug"], last_state={"Plug": False},
        last_set_time={}, override_script={}, channel_id_for_label={"Plug": channel_id},
    )
    return HTMLResponse("".join(render_dashboard(
        "All", None, [], {}, {}, SimpleNamespace(expected_gauge_map={}),
        switch_controllers={sid: controller}, gauge_config=get_gauge_config(),
        expected_gauge_map={}, expected_display_style_map={},
    )))


@app.get("/rain-gauges", response_class=HTMLResponse)
def rain_gauges(unit_system: str = "Metric") -> HTMLResponse:
    """Render rain accumulation gauges with deterministic readings for UI checks."""
    from sensorius.saiDisplayUnits import apply_display_units_to_gauge_config
    from sensorius.saiRainClimate import RAIN_PERIODS

    sensor_id = "rain-pr-check"
    metrics = {metric: .25 for metric in RAIN_PERIODS}
    html = "".join(render_dashboard(
        "All", None, [sensor_id], {sensor_id: metrics}, {},
        SimpleNamespace(expected_gauge_map={}),
        gauge_config=apply_display_units_to_gauge_config(get_gauge_config(), unit_system),
        expected_gauge_map={sensor_id: list(metrics)},
        expected_display_style_map={sensor_id: {}}, display_style="Gauge",
    ))
    return HTMLResponse(html)


@app.get("/api/weather-climate")
def weather_climate() -> dict:
    """Supply deterministic historical statistics without external network access."""
    return {"status": "ready", "date": "2026-09-18", "baseline": "1991–2026",
            "start_date": "1991-01-01", "end_date": "2026-09-13", "averages": {
        "temperature_c": 20, "humidity_pct": 55, "wind_kmh": 16.09344, "rain_mm": 2.54, "samples": 35,
        "extremes": {
            "min": {field: {"value": value, "year": 1993} for field, value in
                    {"temperature_c": 6.7, "humidity_pct": 10, "wind_kmh": 0.3218688, "rain_mm": 0}.items()},
            "max": {field: {"value": value, "year": 2016} for field, value in
                    {"temperature_c": 30.7, "humidity_pct": 100, "wind_kmh": 28.16352, "rain_mm": 21.082}.items()},
        },
    }}


@app.get("/pressure-gauges", response_class=HTMLResponse)
def pressure_gauges(unit_system: str = "Metric", altitude: str = "1609.3", nodus_altitude: str = "1719.0") -> HTMLResponse:
    """Render raw and corrected pressure sensors together for browser checks."""
    from sensorius.saiDisplayUnits import apply_display_units_to_gauge_config

    values = {
        "raw": {"Baro-Pressure": 835.0, "Plant Baro-Pressure": 836.0},
        "corrected": {"Baro-Pressure": 1022.5},
        "nodus": {"Baro-Pressure": 1018.6},
        "ecowitt": {"Gateway Baro-Pressure": 1032.5, "Gateway Absolute Baro-Pressure": 840.0},
    }
    return HTMLResponse("".join(render_dashboard(
        "All", None, list(values), values, {}, SimpleNamespace(expected_gauge_map={}),
        gauge_config=apply_display_units_to_gauge_config(get_gauge_config(), unit_system),
        expected_gauge_map={sid: list(metrics) for sid, metrics in values.items()},
        expected_display_style_map={sid: {} for sid in values}, display_style="Gauge",
        pressure_altitude=altitude,
        pressure_sensor_context={
            "corrected": {"device": "bme280", "altitude": 1609.3},
            "nodus": {"device": "avpd", "altitude": nodus_altitude},
        },
    )))
