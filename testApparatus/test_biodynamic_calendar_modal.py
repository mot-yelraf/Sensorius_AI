"""Pytest coverage for biodynamic calendar modal and API defaults.

These tests validate local-date selection, default month resolution, and the
spillover summary data returned for the biodynamic calendar experience.
"""

import os
import sys
import asyncio
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiWebRoutes as saiWebRoutes

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
    source = Path(__file__).resolve().parents[1] / "sensorius" / "saiHtml.py"
    text = source.read_text(encoding="utf-8")

    assert "const hasSelectedDay = !!(st.selectedDate && days.some((d) => d && d.date === st.selectedDate));" in text
    assert "const today = days.find((d) => d && d.in_month && d.is_today) || null;" in text
    assert "const defaultDay = today || firstInMonth || null;" in text
    assert "function bioTodayIso(){ const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }" in text
    assert "const preferredDate = monthKey === bioMonthKeyFromDate(new Date(`${todayIso}T00:00:00`)) ? todayIso : '';" in text
    assert "window.__bioMonthCache = window.__bioMonthCache || {};" in text
    assert "async function fetchBiodynamicMonth(monthKey, forceRefresh=false){" in text
    assert "function prefetchBiodynamicAdjacentMonths(monthKey){" in text
    assert "setBioMonthLoading(true);" in text
    assert "setBioSummaryLoading(true);" in text
    assert "scheduleBioSummaryRefresh(dateIso);" in text
    assert "fetchBiodynamicMonth(st.month, true);" in text
    assert "function bioTodayIso(){ return new Date().toISOString().slice(0,10); }" not in text
    assert "await loadBiodynamicMonth(monthKey, preferredDate);" in text
    assert ".bio-day{min-height:43px;height:43px;border:1px solid #d7d0bf;border-radius:6px;padding:3px;background:#fff;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;box-sizing:border-box;}" in text
    assert ".bio-day.today:not(.selected){box-shadow:inset 0 0 0 1px rgba(39,49,58,.45);}" in text
    assert ".bio-day.out{opacity:.62;filter:saturate(.42) brightness(1.02);}" in text
    assert ".bio-legend{display:flex;align-items:center;justify-content:center;" in text
    assert ".bio-legend-transition{background:linear-gradient(90deg,#d64b3b 0 50%,#644817 50% 100%)}" in text
    assert "<div class='bio-legend' aria-label='Biodynamic calendar color legend'>" in text
    summary_markup = "<div class='bio-modal-summary' id='bioModalSummary'></div>"
    calendar_markup = "<div class='bio-calendar' id='bioModalCalendar'></div>"
    assert text.index(summary_markup) < text.index("Biodynamic calendar color legend") < text.index(calendar_markup)
    for label in ("Root", "Leaf", "Flower", "Fruit", "Rest", "Transition"):
        assert f"</span>{label}</span>" in text
    assert ".bio-day-num{font-size:.66rem;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.88);border:1px solid rgba(39,49,58,.18);color:#27313a;box-shadow:0 1px 2px rgba(39,49,58,.18);}" in text
    assert ".bio-day-meta{" not in text
    assert ".bio-print-day-num{font-size:9pt;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.9);border:1px solid rgba(39,49,58,.18);color:#27313a;}" in text
    assert ".bio-status{" not in text
    assert ".bio-main{display:flex;flex-direction:column;align-items:center;gap:.08rem;width:100%;align-self:center;min-width:0;overflow:hidden;text-align:center;}" in text
    assert "#bioCurrentSign{font-size:.74rem;" in text
    assert "#bioCurrentElement{font-size:.74rem;" in text
    assert "#bioDateLine{font-size:.74rem;" in text
    assert "#bioWindow{font-size:.70rem;" in text
    assert ".bio-daylight{font-size:.58rem;line-height:1.02;text-align:center;color:#3c464d;min-height:1.02em;margin-top:-.08rem;margin-bottom:.3rem;font-variant-numeric:tabular-nums;}" in text
    assert "white-space:pre;overflow-wrap:normal;word-break:normal;font-variant-numeric:tabular-nums;" in text
    assert "#bioBox{width:230px;box-sizing:border-box;overflow:hidden;align-items:stretch;}" in text
    assert "#bioBox .astro-card{width:100%;min-width:0;align-items:stretch;box-sizing:border-box;height:100%;}" in text
    assert "#bioBox .astro-title,#bioCurrentSign,#bioCurrentElement,.bio-window,.bio-daylight,#bioUpcoming{width:100%;box-sizing:border-box;}" in text
    assert ".astro-box .dashboard-card-spinner{display:none;position:absolute;top:.48rem;right:.52rem;width:16px;height:16px;border-width:2px;z-index:2;background:rgba(255,255,255,.45);}" in text
    assert ".astro-box.card-loading .dashboard-card-spinner{display:inline-block;}" in text
    assert "<span class='spinner dashboard-card-spinner' aria-hidden='true'></span>" in text
    assert "<div class='bio-main' id='bioCurrentPanel'>" in text
    assert "<div class='bio-daylight' id='bioDaylightLine'>Hours of Daylight: --</div>" in text
    assert "<button type='button' class='bio-open-btn' id='bioOpenBtn' aria-label='Open biodynamic calendar' title='View Calendar'>" in text
    assert "<div class='moon-view-toggle' role='group' aria-label='Moon view mode' title='Local sky view or Reference moon diagram'>" in text
    assert "<button type='button' class='moon-view-btn active' id='moonViewLocal' data-moon-view='local' aria-pressed='true' title='Local sky view or Reference moon diagram'>Local</button>" in text
    assert "<button type='button' class='moon-view-btn' id='moonViewReference' data-moon-view='reference' aria-pressed='false' title='Local sky view or Reference moon diagram'>Ref</button>" in text
    assert text.index("<div class='astro-title'>Biodynamic Calendar</div>") < text.index("<div class='bio-window' id='bioDateLine'>Loading biodynamic date...</div>") < text.index("<div class='bio-daylight' id='bioDaylightLine'>Hours of Daylight: --</div>") < text.index("<div class='bio-main' id='bioCurrentPanel'>")
    assert text.index("<div class='astro-box' id='moonBox' aria-live='polite' role='button'") < text.index("<div class='astro-box' id='sunBox' aria-live='polite' role='button'")
    assert "#bioCurrentBadge" not in text
    assert "openBtn.style.background = color;" in text
    assert "openBtn.style.color = textOnHex(color);" in text
    assert "const bioNowParts = () => {" in text
    assert "return { iso: d.toISOString(), dayKey: `${parts.year}-${parts.month}-${parts.day}`, minuteOfDay: Math.max(0, Math.min(1439, (hour24 * 60) + minute)) };" in text
    assert "let __lastBiodynamicMinuteKey = '';" in text
    assert "const biodynamicMinuteKey = `${now.getFullYear()}-${now.getMonth()+1}-${now.getDate()}-${now.getHours()}-${now.getMinutes()}`;" in text
    assert "if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);" in text
    assert "const dashboardExtrasRefreshMs = 60000;" in text
    assert "const dashboardExtrasWarmRetryMs = 5000;" in text
    assert "const findActiveBiodynamicSegment = (payload, fallback) => {" in text
    assert "if (nowParts.minuteOfDay >= startMin && nowParts.minuteOfDay < endMin) {" in text
    assert "const fallbackCurrent = (data && data.current && typeof data.current === 'object') ? data.current : {};" in text
    assert "const cur = findActiveBiodynamicSegment(data || {}, fallbackCurrent);" in text
    assert "if (!data || !data.ok || !cur.sign) {" in text
    assert "if (!data || !data.ok || !data.current || !data.current.sign) {" not in text
    assert "if (cur.timestamp && !document.getElementById('biodynamicCalendarModal')) window.__bioModalState.month = bioMonthKeyFromDate(new Date(cur.timestamp));" in text
    assert "weekday: 'long'," in text
    assert "const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];" in text
    assert "return `${parts.weekday || '--'}, ${monthName} ${parts.day}, ${parts.year}`;" in text
    assert "const buildBiodynamicWindowText = (payload, currentIso, current) => {" in text
    assert "if (reason === 'warming') return '';" in text
    assert "Biodynamic calendar is warming in the background." not in text
    assert "const extrasWarming = !!window.__dashboardExtrasWarming;" in text
    assert "const wantExtras = !lastExtrasRefreshAt || ((now - lastExtrasRefreshAt) >= dashboardExtrasRefreshMs) || (extrasWarming && (!lastExtrasWarmAt || ((now - lastExtrasWarmAt) >= dashboardExtrasWarmRetryMs)));" in text
    assert "const astroWarming = data && data.astro && isDashboardWarmingPayload(data.astro);" in text
    assert "const biodynamicWarming = data && data.biodynamic && isDashboardWarmingPayload(data.biodynamic);" in text
    assert "function setBiodynamicCardLoading(isLoading){" in text
    assert "const keepExistingBiodynamic = warming && biodynamicData && biodynamicData.ok;" in text
    assert "setBiodynamicCardLoading(warming && !keepExistingBiodynamic);" in text
    assert "if (keepExistingBiodynamic) return;" in text
    assert "const biodynamicWarming = isDashboardWarmingPayload(data);" in text
    assert "window.__lastExtrasRefreshAtMs = now;" in text
    assert "return `${sign} Moon: ${fmtHm(seg && seg.start)} to ${fmtHm(seg && seg.end)}`;" in text
    assert "return rows.length ? rows.join('\\\\n') : `${sign} Moon: ${fmtIsoHm(current && current.window_start)} to ${fmtIsoHm(current && current.window_end)}`;" in text
    assert "dateEl.textContent = fmtIsoDate(cur.timestamp);" in text
    assert "windowEl.textContent = buildBiodynamicWindowText(data, cur.timestamp, cur);" in text
    assert "const daylightEl = document.getElementById('bioDaylightLine');" in text
    assert "const findBiodynamicDay = (payload, currentIso) => {" in text
    assert "return `${Math.floor(total / 60)} Hrs ${total % 60} Mins`;" in text
    assert "return `Hours of Daylight: ${label || '--'}`;" in text
    assert "daylightEl.textContent = buildDaylightText(data, cur.timestamp);" in text
    assert "dateEl.textContent = `Current date: ${fmtIsoDate(cur.timestamp)}`;" not in text
    assert "windowEl.textContent = `Current window: ${fmtIsoHm(cur.window_start)} to ${fmtIsoHm(cur.window_end)}`;" not in text
    assert "function renderBiodynamicPrintView(){" in text
    assert "const textOnHex = (hex) => {" in text
    assert "const darkText = '#27313a';" in text
    assert "return contrast(luminance, darkLuminance) >= contrast(luminance, 1) ? darkText : '#fff';" in text
    assert "const biodynamicIsLightRest = (item) => {" in text
    assert "return sign === 'rest' || element === 'pause' || part === 'rest' || kind === 'off';" in text
    assert "const biodynamicTextColor = (item) => {" in text
    assert "const bgColor = String((item && (item.accent || item.dominant_accent || item.color)) || '');" in text
    assert "return bgColor ? textOnHex(bgColor) : '#fff';" in text
    assert "const clearBiodynamicTextContrast = (els) => {" in text
    assert "const applyBiodynamicTextContrast = (els, fallbackColor, gradient) => {" in text
    assert "el.style.backgroundClip = 'text';" in text
    assert "el.style.webkitBackgroundClip = 'text';" in text
    assert "el.style.webkitTextFillColor = 'transparent';" in text
    assert "const buildRollingTextGradient = (payload, currentIso) => {" in text
    assert "const textColor = biodynamicTextColor(seg);" in text
    assert "stops.push(`${textColor} ${startPct}%`, `${textColor} ${endPct}%`);" in text
    assert "const mainTextColor = biodynamicTextColor(cur);" in text
    assert "const mainTextGradient = buildRollingTextGradient(data, cur.timestamp);" in text
    assert "applyBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, daylightEl, upcomingEl], mainTextColor, mainTextGradient);" in text
    assert "const titleEl = boxEl.querySelector('.astro-title');" in text
    assert "clearBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, daylightEl, upcomingEl]);" in text
    assert "panelEl.style.background = 'transparent';" in text
    assert "panelEl.style.borderColor = 'transparent';" in text
    assert ".bio-print-block{font-size:9pt;line-height:1.35;color:#27313a;white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.2em;text-align:left;}" in text
    assert ".bio-summary-card .bio-summary-output{height:auto;max-height:none;min-height:78px;flex:1 1 auto;}" in text
    assert ".bio-modal-side{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto 190px 190px;" in text
    assert "document.body.classList.add('bio-printing');" in text
    assert "window.print();" in text
    assert "function bioRunPrint(){" in text
    assert "window.printBiodynamicReport = function(){ bioRunPrint(); };" in text
    assert "window.printBiodynamicCalendar" not in text
    assert "window.printBiodynamicNotes" not in text
    assert "document.getElementById('bioPrintReportBtn').addEventListener('click', () => { if (window.printBiodynamicReport) window.printBiodynamicReport(); });" in text
    assert "function bioForecastSummaryText(){" in text
    assert "const lines = ['24hr Forecast'];" in text
    assert "function bioSummaryWithForecast(dateIso, summaryText){" in text
    assert "if (window.__weatherForecastEnabled !== false && dateIso && dateIso === bioTodayIso()) parts.push(bioForecastSummaryText());" in text
    assert "const summaryText = bioSummaryWithForecast(day.date, summaries[day.date] || '');" in text
    assert "const storedSummary = dateIso ? String(summaries[dateIso] || '').trim() : '';" in text
    assert "const summaryText = storedSummary ? bioSummaryWithForecast(dateIso, storedSummary) : '';" in text
    assert "if (typeof bioRefreshOpenSummary === 'function') bioRefreshOpenSummary();" in text
    assert "<button type='button' class='bio-print-btn' id='bioPrintReportBtn'>Print Report</button>" in text
    assert "<button type='button' class='bio-print-btn' id='bioPrintCalendarBtn'>Print Calendar</button>" not in text
    assert "<button type='button' class='bio-print-btn' id='bioPrintNotesBtn'>Print Notes</button>" not in text
    assert text.index("<button type='button' class='bio-save-btn' id='bioSaveNoteBtn'>Save Note</button>") < text.index("<button type='button' class='bio-print-btn' id='bioPrintReportBtn'>Print Report</button>")
    assert "<div class='bio-note-card bio-summary-card'>" in text
    assert "<div class='bio-selected-date' id='bioSelectedDate'>Select a day</div>" in text
    assert "aria-label='Loading biodynamic calendar'" in text
    assert "aria-label='Loading daily summary'" in text
    assert "id='bioSummaryDate'" not in text
    assert "id='bioNoteDate'" not in text
    assert "<div class='bio-note-card bio-notes-card'>" in text
    assert text.index("<div class='bio-note-meta' id='bioNoteMeta'></div>") < text.index("<div id='bioDailySummary' class='bio-summary-output'></div>")
    assert "<div class='bio-note-title'>Daily Notes</div>" in text
    assert "<div class='bio-note-title'>Your Notes</div>" not in text
    assert "<div class='bio-print-sheet' id='bioPrintReportSheet' aria-hidden='true'></div>" in text
    assert "<div class='bio-print-sheet' id='bioPrintCalendarSheet' aria-hidden='true'></div>" not in text
    assert "<div class='bio-print-sheet' id='bioPrintNotesSheet' aria-hidden='true'></div>" not in text
    assert "const reportEl = document.getElementById('bioPrintReportSheet');" in text
    assert "<div class='bio-print-title'>Biodynamic Calendar Report</div>" in text
    assert "<div class='bio-print-section-title'>Calendar</div><div class='bio-print-calendar'>${grid}</div><div class='bio-print-section-title'>Daily Summary and Notes</div>" in text
    assert "function biodynamicCompanionUrl(){" in text
    assert "url.protocol = 'http:';" in text
    assert "url.port = '8765';" in text
    assert "url.search = '?source=sensorius';" in text
    assert ".bd-companion-overlay{position:fixed;inset:0;z-index:9999;" in text
    assert ".bd-companion-frame{width:100%;min-width:0;flex:1 1 auto;border:0;background:#fff;}" in text
    assert "window.closeBiodynamicCompanion = function(){" in text
    assert "function openBiodynamicCompanion(url){" in text
    assert "closeBtn.textContent = 'Dashboard';" in text
    assert "closeBtn.setAttribute('aria-label', 'Return to Sensorius dashboard');" in text
    assert "frame.src = url;" in text
    assert "window.openBiodynamicCalendar = async function(){" in text
    assert "fetch('/api/biodynamic-calendar-companion', { cache:'no-store' });" in text
    assert "openBiodynamicCompanion(biodynamicCompanionUrl());" in text
    assert "window.location.assign(biodynamicCompanionUrl());" not in text
    assert "if (window.openBiodynamicCalendarModal) await window.openBiodynamicCalendarModal(); else setBioOpenButtonLoading(false);" in text
    assert "@media print{@page{size:portrait;margin:.35in}@page bio-report{size:portrait;margin:.35in}body.bio-printing *{visibility:hidden !important}" in text
    assert "body.bio-printing #bioPrintReportSheet{display:block !important;position:absolute;left:0;top:0;width:100%;padding:.08in;background:#fff;color:#000;box-sizing:border-box;page:bio-report}" in text
    assert "bio-print-calendar-mode" not in text
    assert "bio-print-notes-mode" not in text
    assert "function hasOpenBackdropModal() {" in text
    assert "if (hasOpenBackdropModal()) {" in text
    assert "deferredLayoutRefresh = false;" in text
    assert "scheduleLayoutRefresh(reason, sig);" in text
    assert "function bioDayBackground(day){" in text
    assert "dividerStops.push(`transparent ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${lineEnd.toFixed(2)}%`, `transparent ${lineEnd.toFixed(2)}%`);" in text
    assert "return `${overlay}, ${base}`;" in text
    assert "const style = `background:${bioDayBackground(day)};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};`;" in text
    assert "const signLabel = String(day.dominant_sign_abbr || day.dominant_sign || '').trim() || '--';" not in text
    assert "const partLabel = String(day.dominant_plant_part || '').trim() || '--';" not in text
    assert "<span class='bio-day-meta'>" not in text
    assert "class='bio-print-day-part'" not in text
    assert "class='bio-print-day-meta'" not in text
    assert "<button type='button' class='bio-nav-btn' id='bioPrevMonthBtn' aria-label='Previous month' title='Previous month'>&lt;</button>" in text
    assert "<button type='button' class='bio-nav-btn' id='bioNextMonthBtn' aria-label='Next month' title='Next month'>&gt;</button>" in text


@pytest.mark.asyncio
async def test_biodynamic_calendar_api_default_month_uses_biodynamic_local_time(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    captured = []
    window_calls = []

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 3, 22, 9, 15)
            return base.replace(tzinfo=tz) if tz is not None else base

    def _fake_payload(anchor):
        captured.append(anchor)
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
        res_next = await client.get("/api/biodynamic-calendar?month=2026-04")
    await asyncio.sleep(0.05)

    assert res.status_code == 200
    assert res_next.status_code == 200
    assert captured[0].isoformat() == "2026-03-01"
    assert captured[-1].isoformat() == "2026-04-01"
    assert window_calls == [("2026-03-01", saiWebRoutes.DEFAULT_FORECAST_DAYS, True)]


@pytest.mark.asyncio
async def test_biodynamic_calendar_api_concurrent_month_requests_single_flight(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    payload_calls = []

    def _fake_payload(anchor):
        payload_calls.append(anchor)
        time.sleep(0.05)
        return {"ok": True, "calendar": [], "month_label": "", "notes": {}, "daily_summaries": {}}

    class _FakeDailySummaryService:
        def __init__(self, *, settings, data_logger, supervisor=None, sensor_mgr=None, statter=None):
            self.settings = settings
            self.data_logger = data_logger

        def ensure_summaries_for_window(self, start_date, *, days=29, refresh_start=True):
            return 0

    monkeypatch.setattr(saiWebRoutes, "get_biodynamic_payload", _fake_payload)
    monkeypatch.setattr(saiWebRoutes, "DailySummaryService", _FakeDailySummaryService)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_notes_for_month", lambda anchor: {})
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_biodynamic_daily_summaries_for_month", lambda anchor: {})

    app = FastAPI()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), _FakeIngest())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res_one, res_two = await asyncio.gather(
            client.get("/api/biodynamic-calendar?month=2026-05"),
            client.get("/api/biodynamic-calendar?month=2026-05"),
        )

    assert res_one.status_code == 200
    assert res_two.status_code == 200
    assert [item.isoformat() for item in payload_calls] == ["2026-05-01"]


@pytest.mark.asyncio
async def test_biodynamic_calendar_companion_status_endpoint(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    async def _fake_probe():
        return {"ok": True, "port": 8765, "health_path": "/healthz", "source_query": "source=sensorius"}

    monkeypatch.setattr(saiWebRoutes, "_probe_biodynamic_companion_app", _fake_probe)

    app = FastAPI()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), _FakeIngest())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/biodynamic-calendar-companion")

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "port": 8765,
        "health_path": "/healthz",
        "source_query": "source=sensorius",
    }


@pytest.mark.asyncio
async def test_biodynamic_calendar_companion_probe_accepts_root_page_when_health_missing(monkeypatch):
    class _Resp:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    responses = [
        _Resp(404, '{"detail":"Not Found"}'),
        _Resp(200, "<title>Biodynamic Calendar</title><section class='calendar-shell'></section>"),
    ]
    seen_urls = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            seen_urls.append(url)
            return responses.pop(0)

    monkeypatch.setattr(saiWebRoutes.httpx, "AsyncClient", _FakeAsyncClient)

    payload = await saiWebRoutes._probe_biodynamic_companion_app()

    assert payload["ok"] is True
    assert payload["probe_path"] == "/?source=sensorius"
    assert payload["health_status_code"] == 404
    assert seen_urls == [
        "http://127.0.0.1:8765/healthz",
        "http://127.0.0.1:8765/?source=sensorius",
    ]


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
