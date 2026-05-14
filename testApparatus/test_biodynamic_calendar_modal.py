"""Pytest coverage for biodynamic calendar modal and API defaults.

These tests validate local-date selection, default month resolution, and the
spillover summary data returned for the biodynamic calendar experience.
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiWebRoutes


class _DummyFastStats:
    def __init__(self, *_args, **_kwargs):
        pass

    async def start(self):
        return

    def stop(self):
        return


class _HubSettings:
    def get_all_sensor_ids(self):
        return []

    def get_setting(self, _section, _key, default=None):
        return default


class _FakeNetMgr:
    pass


class _FakeGcMgr:
    pass


class _FakeIngest:
    def set_onboarding_event_handler(self, handler):
        self.handler = handler

    def get_known_devices(self):
        return []

    def get_known_switch_devices(self):
        return []


def test_biodynamic_calendar_modal_defaults_to_today_when_present():
    source = Path(__file__).resolve().parents[1] / "saiHtml.py"
    text = source.read_text(encoding="utf-8")

    assert "const hasSelectedDay = !!(st.selectedDate && days.some((d) => d && d.date === st.selectedDate));" in text
    assert "const today = days.find((d) => d && d.in_month && d.is_today) || null;" in text
    assert "const defaultDay = today || firstInMonth || null;" in text
    assert "function bioTodayIso(){ const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }" in text
    assert "const preferredDate = monthKey === bioMonthKeyFromDate(new Date(`${todayIso}T00:00:00`)) ? todayIso : '';" in text
    assert "function bioTodayIso(){ return new Date().toISOString().slice(0,10); }" not in text
    assert "await loadBiodynamicMonth(monthKey, preferredDate);" in text
    assert ".bio-day.today:not(.selected){box-shadow:inset 0 0 0 1px rgba(39,49,58,.45);}" in text
    assert ".bio-day.out{opacity:.62;filter:saturate(.42) brightness(1.02);}" in text
    assert ".bio-day-num{font-size:.66rem;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.88);border:1px solid rgba(39,49,58,.18);color:#27313a;box-shadow:0 1px 2px rgba(39,49,58,.18);}" in text
    assert ".bio-print-day-num{font-size:9pt;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.9);border:1px solid rgba(39,49,58,.18);color:#27313a;}" in text
    assert ".bio-status{display:flex;align-items:center;justify-content:center;gap:.14rem;width:min(100%,156px);align-self:center;padding:.24rem .3rem;border-radius:8px;border:0;background:#fffdf6;overflow:hidden;box-sizing:border-box;}" in text
    assert ".bio-main{display:flex;flex-direction:column;align-items:center;gap:.08rem;min-width:0;flex:1 1 auto;overflow:hidden;text-align:center;}" in text
    assert "#bioCurrentBadge" not in text
    assert "openBtn.style.background = color;" in text
    assert "openBtn.style.color = textOnHex(color);" in text
    assert "function renderBiodynamicPrintView(){" in text
    assert "const textOnHex = (hex) => {" in text
    assert "const biodynamicMainTextColor = (item) => {" in text
    assert "if ((element === 'fire' && part === 'fruit') || (element === 'water' && part === 'leaf')) return '#fff';" in text
    assert "if ((element === 'earth' && part === 'root') || (element === 'air' && part === 'flower')) return '#27313a';" in text
    assert "const mainTextColor = biodynamicMainTextColor(cur);" in text
    assert "const titleEl = boxEl.querySelector('.astro-title');" in text
    assert "[titleEl, signEl, elementEl, dateEl, windowEl, upcomingEl].forEach((el) => { if (el) el.style.color = ''; });" in text
    assert "[titleEl, signEl, elementEl, dateEl, windowEl, upcomingEl].forEach((el) => { if (el) el.style.color = mainTextColor; });" in text
    assert "panelEl.style.borderColor = 'transparent';" in text
    assert ".bio-print-block{font-size:9pt;line-height:1.35;color:#27313a;white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.2em;text-align:left;}" in text
    assert ".bio-summary-card .bio-summary-output{height:78px;max-height:78px;}" in text
    assert "document.body.classList.add('bio-printing');" in text
    assert "document.body.classList.add(mode === 'calendar' ? 'bio-print-calendar-mode' : 'bio-print-notes-mode');" in text
    assert "window.print();" in text
    assert "window.printBiodynamicCalendar = function(){ bioRunPrint('calendar'); };" in text
    assert "window.printBiodynamicNotes = function(){ bioRunPrint('notes'); };" in text
    assert "document.getElementById('bioPrintCalendarBtn').addEventListener('click', () => { if (window.printBiodynamicCalendar) window.printBiodynamicCalendar(); });" in text
    assert "document.getElementById('bioPrintNotesBtn').addEventListener('click', () => { if (window.printBiodynamicNotes) window.printBiodynamicNotes(); });" in text
    assert "<button type='button' class='bio-print-btn' id='bioPrintCalendarBtn'>Print Calendar</button>" in text
    assert "<button type='button' class='bio-print-btn' id='bioPrintNotesBtn'>Print Notes</button>" in text
    assert "<div class='bio-note-card bio-summary-card'>" in text
    assert "<div class='bio-print-sheet' id='bioPrintCalendarSheet' aria-hidden='true'></div>" in text
    assert "<div class='bio-print-sheet' id='bioPrintNotesSheet' aria-hidden='true'></div>" in text
    assert "@media print{@page{margin:.2in}@page bio-calendar{size:landscape;margin:.2in}@page bio-notes{size:portrait;margin:.35in}body.bio-printing *{visibility:hidden !important}" in text
    assert "@page bio-calendar{size:landscape;margin:.2in}" in text
    assert "@page bio-notes{size:portrait;margin:.35in}" in text
    assert "body.bio-print-calendar-mode #bioPrintCalendarSheet{display:block !important;page:bio-calendar}" in text
    assert "body.bio-print-notes-mode #bioPrintNotesSheet{display:block !important;page:bio-notes}" in text
    assert "function hasOpenBackdropModal() {" in text
    assert "if (hasOpenBackdropModal()) {" in text
    assert "setTimeout(() => scheduleLayoutRefresh(reason, sig), 1000);" in text
    assert "function bioDayBackground(day){" in text
    assert "dividerStops.push(`transparent ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${lineEnd.toFixed(2)}%`, `transparent ${lineEnd.toFixed(2)}%`);" in text
    assert "return `${overlay}, ${base}`;" in text
    assert "const style = `background:${bioDayBackground(day)};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};`;" in text


@pytest.mark.asyncio
async def test_biodynamic_calendar_api_default_month_uses_biodynamic_local_time(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    captured = {}
    window_calls = []

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 3, 22, 9, 15)
            return base.replace(tzinfo=tz) if tz is not None else base

    def _fake_payload(anchor):
        captured["anchor"] = anchor
        return {"ok": True, "calendar": [], "month_label": "", "notes": {}, "daily_summaries": {}}

    class _FakeDailySummaryService:
        def __init__(self, *, settings, data_logger, supervisor=None, sensor_mgr=None, statter=None):
            self.settings = settings
            self.data_logger = data_logger

        def ensure_summaries_for_window(self, start_date, *, days=29, refresh_start=True):
            window_calls.append((start_date.isoformat(), days, refresh_start))
            return 0

    monkeypatch.setattr(
        saiWebRoutes,
        "get_biodynamic_local_now",
        lambda: datetime(2026, 3, 31, 23, 30, tzinfo=ZoneInfo("America/Denver")),
    )
    monkeypatch.setattr(saiWebRoutes, "datetime", _FixedDateTime)
    monkeypatch.setattr(saiWebRoutes, "get_biodynamic_payload", _fake_payload)
    monkeypatch.setattr(saiWebRoutes, "DailySummaryService", _FakeDailySummaryService)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_notes_for_month", lambda anchor: {})
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_daily_summaries_for_month", lambda anchor: {})

    app = FastAPI()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), _FakeIngest())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/biodynamic-calendar")

    assert res.status_code == 200
    assert captured["anchor"].isoformat() == "2026-03-01"
    assert window_calls == [("2026-03-22", 29, True)]


@pytest.mark.asyncio
async def test_biodynamic_calendar_api_includes_spillover_day_summaries(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    range_calls = []

    def _fake_payload(anchor):
        assert anchor.isoformat() == "2026-03-01"
        return {
            "ok": True,
            "calendar": [
                {"date": "2026-03-29", "in_month": True},
                {"date": "2026-03-30", "in_month": True},
                {"date": "2026-03-31", "in_month": True},
                {"date": "2026-04-01", "in_month": False},
                {"date": "2026-04-02", "in_month": False},
            ],
            "month_label": "March 2026",
        }

    class _FakeDailySummaryService:
        def __init__(self, *, settings, data_logger, supervisor=None, sensor_mgr=None, statter=None):
            self.settings = settings
            self.data_logger = data_logger

        def ensure_summaries_for_window(self, start_date, *, days=29, refresh_start=True):
            return 0

    monkeypatch.setattr(saiWebRoutes, "get_biodynamic_payload", _fake_payload)
    monkeypatch.setattr(saiWebRoutes, "DailySummaryService", _FakeDailySummaryService)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_biodynamic_notes_for_range",
        lambda start_date, end_date: (
            range_calls.append(("notes", start_date.isoformat(), end_date.isoformat())) or {"2026-04-01": "spillover note"}
        ),
    )
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_biodynamic_daily_summaries_for_range",
        lambda start_date, end_date: (
            range_calls.append(("summaries", start_date.isoformat(), end_date.isoformat())) or {"2026-04-01": "spillover summary"}
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_notes_for_month", lambda anchor: {})
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_daily_summaries_for_month", lambda anchor: {})

    app = FastAPI()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), _FakeIngest())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/biodynamic-calendar?month=2026-03")

    assert res.status_code == 200
    body = res.json()
    assert body["daily_summaries"]["2026-04-01"] == "spillover summary"
    assert body["notes"]["2026-04-01"] == "spillover note"
    assert ("notes", "2026-03-29", "2026-04-02") in range_calls
    assert ("summaries", "2026-03-29", "2026-04-02") in range_calls
