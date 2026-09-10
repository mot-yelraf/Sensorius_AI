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
def dashboard() -> HTMLResponse:
    """Render a deterministic dashboard with enough metrics to test interaction."""
    sensor_id = "aht-pr-check"
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
    html = "".join(
        render_dashboard(
            "All",
            None,
            [sensor_id],
            {sensor_id: metrics},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={sensor_id: list(metrics)},
            expected_display_style_map={sensor_id: {}},
            display_style="Gauge",
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
def system_settings() -> HTMLResponse:
    """Render the actual settings dialog without production settings or hardware."""
    return HTMLResponse(templates.get_template("modals/system_settings.html").render(
        app_name_long="Sensorius AI", app_version="playwright", custom_themes={},
        display_style="Gauge", metric_set="Pick 6", gauge_size="Small",
    ))


@app.get("/edit-sensor", response_class=HTMLResponse)
def sensor_settings() -> HTMLResponse:
    """Render a sensor's real settings and calibration panes for mobile checks."""
    return HTMLResponse(templates.get_template("modals/sensor_settings.html").render(
        sensor_id="aht-pr-check", current_metrics=["Temperature"] + [""] * 5,
        metric_options=["", "Temperature"], settings={}, location="Greenhouse",
    ))


@app.get("/edit-switch", response_class=HTMLResponse)
def switch_settings() -> HTMLResponse:
    """Render a remote switch dialog without contacting a device."""
    return HTMLResponse(templates.get_template("modals/switch_settings.html").render(
        switch_id="switch-pr-check", settings={"Switch": {"SWITCH_LOCATION": "Greenhouse"}},
        channels=[{"index": 0, "label": "Pump"}], channel_indices=[0],
    ))


@app.get("/weather-forecast", response_class=HTMLResponse)
def weather_forecast(request: Request, units: str = "Metric"):
    """Render the real Caelus page with a complete, deterministic six-day outlook."""
    from sensorius.saiWeatherForecastApp import build_weather_display_forecast

    start = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    forecast = build_weather_display_forecast({
        "ok": True, "provider": "nws",
        "current_24h": {"overall": "Cloudy early, clearing late", "precip_probability": 55},
        "hourly": [{
            "time": (start + timedelta(hours=hour)).isoformat(),
            "local_time": (start + timedelta(hours=hour)).isoformat(),
            "temp_c": 30 - hour / 2, "precip_probability": 55,
            "symbol": "partlycloudy_day",
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
        "latest": {}, "moon": {"updated_at": start.isoformat()},
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
