"""Serve an isolated Sensorius dashboard fixture for local Playwright checks.

The host exercises the real dashboard renderer and static assets without
starting hardware, MQTT, service, or persistent production runtime tasks.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="sensorius-playwright-")
os.environ.setdefault("SENSORIUS_PROJECT_ROOT", str(REPO_ROOT))
os.environ.setdefault("SENSORIUS_RUNTIME_ROOT", _RUNTIME_DIR.name)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from sensorius.saiHtml import get_gauge_config, render_dashboard  # noqa: E402


app = FastAPI()
app.mount("/ui_static", StaticFiles(directory=str(REPO_ROOT / "ui_static")), name="ui_static")


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
