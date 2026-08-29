"""Cover the full-screen integrated Biodynamic Calendar.

The tests verify application context, routes, templates, and static integration
for the calendar surface within the main Sensorius runtime.
"""

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
    def __init__(self, biodynamic_calendar_theme: str = "garden_tools"):
        self.biodynamic_calendar_theme = biodynamic_calendar_theme

    def get_setting(self, section, key, default=None):
        if section == "Display" and key == "biodynamic_calendar_theme":
            return self.biodynamic_calendar_theme
        return default

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


def test_seasonal_theme_preferences_normalize_and_rotate_by_month():
    assert calendar_app.normalize_biodynamic_calendar_theme("autumn") == "autumn"
    assert calendar_app.normalize_biodynamic_calendar_theme("garden-tools") == "garden_tools"
    assert calendar_app.normalize_biodynamic_calendar_theme("leaf") == "garden_tools"
    assert calendar_app.biodynamic_calendar_season(3) == "spring"
    assert calendar_app.biodynamic_calendar_season(6) == "summer"
    assert calendar_app.biodynamic_calendar_season(9) == "autumn"
    assert calendar_app.biodynamic_calendar_season(12) == "winter"

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

    assert 'id="dashboardReturn" href="/?dashboard_return=true" aria-label="Return to Sensorius dashboard"' in template
    assert 'class="dashboard-return-spinner"' in template
    assert '<span class="dashboard-close-icon" aria-hidden="true">&times;</span>' in template
    assert '<span class="dashboard-return-label">Dashboard</span>' not in template
    assert "right: 1rem;" in stylesheet
    assert "border-radius: 50%;" in stylesheet
    assert '<footer class="bd-site-footer">' in template
    assert "Created by Peace Hill Studios" in template
    assert template.index('<footer class="bd-site-footer">') < template.index('id="bd-calendar-bootstrap"')
    assert "body {\n  min-height: 100vh;\n  margin: 0;\n  font-family:" in stylesheet
    for theme, color, asset in (
        ("spring", "#e7f3df", "valley-spring.webp"),
        ("summer", "#fff0bd", "valley-summer.webp"),
        ("autumn", "#f3dcc2", "valley-autumn.webp"),
        ("winter", "#dcebf3", "valley-winter.webp"),
    ):
        assert f"body.biodynamic-theme-{theme} {{" in stylesheet
        assert f"--theme-panel: {color};" in stylesheet
        assert f'background-image: url("/ui_static/biodynamic_calendar/backgrounds/{asset}");' in stylesheet
        assert (root / "ui_static" / "biodynamic_calendar" / "backgrounds" / asset).is_file()
    assert "body.biodynamic-theme-garden_tools {" in stylesheet
    assert '--theme-panel: #eadfcf;' in stylesheet
    assert 'background-image: url("/ui_static/garden-tools-pattern.svg");' in stylesheet
    assert (root / "ui_static" / "garden-tools-pattern.svg").is_file()
    assert 'body[class*="biodynamic-theme-"] .panel:not(.lunar-cycle-panel)' in stylesheet
    assert "biodynamic-theme-{{ biodynamic_calendar_resolved_theme" in template
    assert 'data-theme-preference="{{ biodynamic_calendar_theme' in template
    assert "/ui_static/biodynamic_calendar/app.js" in template
    assert "/api/biodynamic-calendar-app/calendar" in javascript
    assert 'id="printBtn" class="header-report" type="button"' in template
    assert 'class="print-icon" viewBox="0 0 24 24"' in template
    assert template.index('id="printBtn"') < template.index('id="headerLocation"')
    assert 'data-open-calendar-theme aria-label="Preview calendar themes">Theme</button>' in template
    assert 'id="calendarThemeView"' in template
    assert 'data-calendar-preview-theme="auto"' in template
    assert 'data-calendar-preview-theme="garden_tools"' in template
    assert 'data-calendar-preview-theme="winter"' in template
    assert 'id="closeCalendarThemeBtn"' in template
    assert 'target="_blank"' not in template
    assert "window.localStorage" not in javascript
    assert "if (stageCurrentMonthReport()) window.print();" in javascript
    assert "function openCalendarThemeView()" in javascript
    assert "function closeCalendarThemeView()" in javascript
    assert 'document.body.classList.contains("calendar-theme-preview-mode")' in javascript
    assert ".calendar-theme-toolbar" in stylesheet
    assert "body.calendar-theme-preview-mode .page" in stylesheet
    assert ".dashboard-return {\n    display: none !important;" in stylesheet
    assert ".bd-site-footer {\n    display: none !important;" in stylesheet
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
    assert ".legend-leaf { color: #277a00; }" in stylesheet
    assert ".legend-flower { color: #d8ac00; }" in stylesheet
    assert '<circle cx="9" cy="9" r="2.3"/>' in template
    assert '<circle cx="11.5" cy="21" r="2.1"/>' in template
    assert ".legend-fruit { color: #4c3a7f; }" in stylesheet
    assert ".legend-rest { color: #6d7680; }" in stylesheet
    assert 'class="day-number"' in javascript
    assert "plantPartIconMarkup(partLabel)" in javascript
    assert ".day-part-icon" in stylesheet
    assert ".day-part-icon.part-leaf { color: #277a00; }" in stylesheet
    assert ".day-part-icon.part-fruit { color: #4c3a7f; fill: currentColor; }" in stylesheet
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


def test_leaf_icons_are_green_without_changing_water_background_accent():
    from sensorius import saiBiodynamics
    from sensorius.biodynamic_calendar import core

    root = Path(__file__).resolve().parents[1]
    stylesheet = (root / "ui_static" / "biodynamic_calendar" / "app.css").read_text(encoding="utf-8")
    dashboard = (root / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert ".legend-leaf { color: #277a00; }" in stylesheet
    assert ".day-part-icon.part-leaf { color: #277a00; }" in stylesheet
    assert ".bio-legend-leaf{background:#277a00}" in dashboard
    for signs in (saiBiodynamics._SIGNS, core._SIGNS):
        leaf_signs = [sign for sign in signs if sign["plant_part"] == "Leaf"]
        assert leaf_signs
        assert all(sign["color"] == "#277A00" for sign in leaf_signs)
        assert all(sign["accent"] == "#2f6eb8" for sign in leaf_signs)


def test_fruit_uses_grape_purple_without_changing_fire_background_accent():
    from sensorius import saiBiodynamics
    from sensorius.biodynamic_calendar import core

    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert ".bio-legend-fruit{background:#4c3a7f}" in dashboard
    assert "const actionColors = biodynamicActionColors(cur);" in dashboard
    assert "openBtn.style.color = actionColors.text;" in dashboard
    for signs in (saiBiodynamics._SIGNS, core._SIGNS):
        fruit_signs = [sign for sign in signs if sign["plant_part"] == "Fruit"]
        assert fruit_signs
        assert all(sign["color"] == "#f19707" for sign in fruit_signs)
        assert all(sign["accent"] == "#d64b3b" for sign in fruit_signs)


def test_all_calendar_expandables_default_closed_and_use_process_scoped_state():
    root = Path(__file__).resolve().parents[1]
    template = (root / "ui_templates" / "biodynamic_calendar" / "index.html").read_text(encoding="utf-8")
    javascript = (root / "ui_static" / "biodynamic_calendar" / "app.js").read_text(encoding="utf-8")
    routes = (root / "sensorius" / "saiBiodynamicCalendarApp.py").read_text(encoding="utf-8")

    assert template.count("<details") == 5
    assert template.count("data-runtime-section=") == 5
    assert not any(" open>" in line for line in template.splitlines() if "<details" in line)
    assert 'data-runtime-section="calendar-astral-details"' in javascript
    assert "window.__sensoriusExpandableSectionState" in javascript
    assert 'section.addEventListener("toggle"' in javascript
    assert "new MutationObserver" in javascript
    assert '"runtime_instance_id": str(getattr(request.app.state, "ui_runtime_instance_id", "") or "")' in routes


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
        settings=_Settings("winter"),
        data_logger=_Logger(),
    )
    service._legacy_import_done = True
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/calendar")
        payload = await client.get("/api/biodynamic-calendar-app/calendar?month=2026-03")

    assert page.status_code == 200
    assert '<a class="dashboard-return" id="dashboardReturn" href="/?dashboard_return=true"' in page.text
    assert '<body class="sensorius-launch biodynamic-theme-winter"' in page.text
    assert 'data-theme-preference="winter"' in page.text
    assert '<span class="dashboard-close-icon" aria-hidden="true">&times;</span>' in page.text
    assert '/ui_static/biodynamic_calendar/bd-calendar-icon-512.svg' in page.text
    assert "Back to Sensorius" not in page.text
    assert "/ui_static/biodynamic_calendar/app.js" in page.text
    assert payload.status_code == 200
    assert payload.json()["month_label"] == "March 2026"
    assert payload.json()["astro"]["ok"] is True
