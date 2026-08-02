"""Focused coverage for the full-screen integrated Biodynamic Calendar."""

import asyncio
import json
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient

import sensorius.saiBiodynamicCalendarApp as calendar_app


class _Settings:
    def resolve_astral_location(self, **_kwargs):
        return {
            "lat": 39.7392,
            "lon": -104.9903,
            "tz": "America/Denver",
            "altitude": 1609.0,
            "source": "manual",
            "provider": "",
            "error": "",
        }


class _Logger:
    def __init__(self):
        self.cache = {}

    def get_biodynamic_calendar_cache(self, key, location):
        return self.cache.get((key, location))

    def save_biodynamic_calendar_cache(self, key, location, payload, **_kwargs):
        self.cache[(key, location)] = dict(payload)
        return True

    def get_biodynamic_plantings(self):
        return []

    def get_biodynamic_notes_for_range(self, *_args):
        return {}

    def save_biodynamic_daily_summary(self, *_args):
        return True


@pytest.mark.asyncio
async def test_integrated_month_build_is_single_flight_and_persisted(monkeypatch):
    calls = []

    def _fake_payload(anchor, *, config):
        calls.append((anchor, config.timezone_name))
        time.sleep(0.03)
        return {"ok": True, "calendar": [], "month_label": "March 2026"}

    monkeypatch.setattr(calendar_app, "get_biodynamic_payload", _fake_payload)
    service = calendar_app.BiodynamicCalendarService(settings=_Settings(), data_logger=_Logger())
    config, location = service.location()
    assert config is not None
    assert location["ok"] is True

    one, two = await asyncio.gather(
        service.month(date(2026, 3, 1), config),
        service.month(date(2026, 3, 1), config),
    )

    assert one["ok"] is True
    assert two["ok"] is True
    assert calls == [(date(2026, 3, 1), "America/Denver")]


def test_integrated_assets_use_namespaced_routes_and_dashboard_navigation():
    root = Path(__file__).resolve().parents[1]
    template = (root / "ui_templates" / "biodynamic_calendar" / "index.html").read_text(encoding="utf-8")
    stylesheet = (root / "ui_static" / "biodynamic_calendar" / "app.css").read_text(encoding="utf-8")
    javascript = (root / "ui_static" / "biodynamic_calendar" / "app.js").read_text(encoding="utf-8")
    dashboard = (root / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert 'id="dashboardReturn" href="/" aria-label="Return to Sensorius dashboard"' in template
    assert 'class="dashboard-return-spinner"' in template
    assert "body {\n  margin: 0;\n  font-family:" in stylesheet
    assert "background: #f5fffa;" in stylesheet
    assert "radial-gradient(circle at top left" not in stylesheet
    assert "/ui_static/biodynamic_calendar/app.js" in template
    assert "/api/biodynamic-calendar-app/calendar" in javascript
    assert "/calendar/report?key=" in javascript
    assert "Sun/Moon Position</h2>" not in template
    assert "Moon Phase</h2>" not in template
    assert '<h2 id="cosmicAttributesTitle">Moon Attributes</h2>' in template
    assert '<h2 id="planetaryAspectsTitle">Planetary Aspects</h2>' in template
    assert 'id="planetaryAttributes"' in template
    assert "cosmic.planet_zodiac" in javascript
    assert "Current Major Aspects" in javascript
    assert "Planet Zodiac" in javascript
    assert 'dashboardReturn.classList.add("is-loading")' in javascript
    assert 'window.requestAnimationFrame(() => {' in javascript
    assert 'window.requestAnimationFrame(() => window.location.assign(destination))' in javascript
    assert "window.location.assign('/calendar')" in dashboard
    assert "url.port = '8765'" not in dashboard


@pytest.mark.asyncio
async def test_legacy_json_state_imports_only_into_empty_tables(tmp_path):
    (tmp_path / "notes.json").write_text(json.dumps({"2026-08-02": "Legacy note"}), encoding="utf-8")
    (tmp_path / "plantings.json").write_text(
        json.dumps([{"id": "legacy-1", "name": "Tomato", "start_date": "2026-05-01"}]),
        encoding="utf-8",
    )

    class _ImportLogger(_Logger):
        def __init__(self):
            super().__init__()
            self.notes = {}
            self.plantings = []

        def get_biodynamic_notes_for_range(self, *_args):
            return dict(self.notes)

        def save_biodynamic_note(self, day_iso, note):
            self.notes[day_iso] = note
            return True

        def get_biodynamic_plantings(self):
            return list(self.plantings)

        def save_biodynamic_planting(self, planting):
            self.plantings.append(dict(planting))
            return True

    logger = _ImportLogger()
    service = calendar_app.BiodynamicCalendarService(
        settings=_Settings(),
        data_logger=logger,
        legacy_root=tmp_path,
    )
    await service.ensure_legacy_import()
    await service.ensure_legacy_import()

    assert logger.notes == {"2026-08-02": "Legacy note"}
    assert [item["id"] for item in logger.plantings] == ["legacy-1"]


@pytest.mark.asyncio
async def test_integrated_calendar_page_and_month_api_render(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        calendar_app,
        "get_biodynamic_payload",
        lambda anchor, *, config: {
            "ok": True,
            "calendar": [{"date": anchor.isoformat(), "segments": [], "in_month": True}],
            "month_label": "March 2026",
        },
    )
    monkeypatch.setattr(calendar_app, "get_astro_payload", lambda *, config: {"ok": True, "tz": config.timezone_name})
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=str(root / "ui_templates"))
    router = APIRouter()
    service = calendar_app.register_biodynamic_calendar_routes(
        router,
        app=app,
        settings=_Settings(),
        data_logger=_Logger(),
    )
    service._legacy_import_done = True
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/calendar")
        payload = await client.get("/api/biodynamic-calendar-app/calendar?month=2026-03")

    assert page.status_code == 200
    assert '<a class="dashboard-return" id="dashboardReturn" href="/"' in page.text
    assert '<span class="dashboard-return-label">Dashboard</span>' in page.text
    assert "Back to Sensorius" not in page.text
    assert "/ui_static/biodynamic_calendar/app.js" in page.text
    assert payload.status_code == 200
    assert payload.json()["month_label"] == "March 2026"
    assert payload.json()["astro"]["ok"] is True
