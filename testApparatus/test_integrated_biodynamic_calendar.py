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


def test_current_transition_reuses_persisted_month_until_window_end():
    logger = _Logger()
    service = calendar_app.BiodynamicCalendarService(settings=_Settings(), data_logger=logger)
    config, _location = service.location()
    assert config is not None
    now_local = calendar_app.datetime.now(calendar_app.ZoneInfo(config.timezone_name))
    anchor = now_local.date().replace(day=1)
    logger.cache[(service._month_cache_key(anchor), service._location_key(config))] = {
        "ok": True,
        "calendar": [
            {
                "date": now_local.date().isoformat(),
                "segments": [
                    {
                        "start": "00:00",
                        "end": "24:00",
                        "sign": "Taurus",
                        "element": "Earth",
                        "plant_part": "Root",
                        "color": "#123456",
                        "accent": "#abcdef",
                    }
                ],
            }
        ],
    }
    reads = {"count": 0}
    original_get = logger.get_biodynamic_calendar_cache

    def _counted_get(key, location):
        reads["count"] += 1
        return original_get(key, location)

    logger.get_biodynamic_calendar_cache = _counted_get

    first = service.current_transition_sync()
    second = service.current_transition_sync()

    assert first["transition_at"].startswith(now_local.date().isoformat())
    assert first["plant_part"] == "Root"
    assert second == first
    assert reads["count"] == 1


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
    calendar_icon = (root / "ui_static" / "biodynamic_calendar" / "bd-calendar-icon-512.svg").read_text(
        encoding="utf-8"
    )
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
    assert 'class="hero-calendar-mark"' in template
    assert '/ui_static/biodynamic_calendar/bd-calendar-icon-512.svg' in template
    assert "grid-template-columns: minmax(320px, 1fr) 214px minmax(320px, 1fr);" in stylesheet
    assert "border-radius: 50%;" in stylesheet
    assert "object-fit: contain;" in stylesheet
    assert "transform: scale(1.24);" not in stylesheet
    assert 'viewBox="61 61 390 390"' in calendar_icon
    assert '<circle cx="256" cy="256" r="182" fill="none" stroke="#173f35" stroke-width="26"/>' in calendar_icon
    for plant_part in ("root", "leaf", "flower", "fruit", "rest"):
        assert f'id="legend-icon-{plant_part}"' in template
        assert template.count(f'href="#legend-icon-{plant_part}"') == 2
    assert ".legend-root { color: #644817; }" in stylesheet
    assert ".legend-leaf { color: #2f6eb8; }" in stylesheet
    assert ".legend-flower { color: #d8ac00; }" in stylesheet
    assert ".legend-fruit { color: #d64b3b; }" in stylesheet
    assert ".legend-rest { color: #6d7680; }" in stylesheet
    assert 'class="day-number"' in javascript
    assert "plantPartIconMarkup(partLabel)" in javascript
    assert ".day-part-icon" in stylesheet
    assert "top: 6px;" in stylesheet
    assert 'id="planetaryAttributes"' in template
    assert "cosmic.planet_zodiac" in javascript
    assert "Current Major Aspects" in javascript
    assert "Planet Zodiac" in javascript
    assert 'dashboardReturn.classList.add("is-loading")' in javascript
    assert 'window.requestAnimationFrame(() => {' in javascript
    assert 'window.requestAnimationFrame(() => window.location.assign(destination))' in javascript
    assert "window.location.assign('/calendar')" in dashboard
    assert "url.port = '8765'" not in dashboard
    assert "state.followCurrentMonth || state.selectionPinned" in javascript
    assert "monthlyPrintHints" not in javascript


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
    assert '/ui_static/biodynamic_calendar/bd-calendar-icon-512.svg' in page.text
    assert "Back to Sensorius" not in page.text
    assert "/ui_static/biodynamic_calendar/app.js" in page.text
    assert payload.status_code == 200
    assert payload.json()["month_label"] == "March 2026"
    assert payload.json()["astro"]["ok"] is True
