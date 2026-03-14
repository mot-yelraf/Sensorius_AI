from pathlib import Path


def test_biodynamic_calendar_modal_defaults_to_today_when_present():
    source = Path(__file__).resolve().parents[1] / "saiHtml.py"
    text = source.read_text(encoding="utf-8")

    assert "const hasSelectedDay = !!(st.selectedDate && days.some((d) => d && d.date === st.selectedDate));" in text
    assert "const today = days.find((d) => d && d.in_month && d.is_today) || null;" in text
    assert "const defaultDay = today || firstInMonth || null;" in text
    assert "function bioTodayIso(){ return new Date().toISOString().slice(0,10); }" in text
    assert "const preferredDate = monthKey === bioMonthKeyFromDate(new Date(`${todayIso}T00:00:00`)) ? todayIso : '';" in text
    assert "await loadBiodynamicMonth(monthKey, preferredDate);" in text
    assert ".bio-day.today:not(.selected){box-shadow:inset 0 0 0 1px rgba(39,49,58,.45);}" in text
    assert "function renderBiodynamicPrintView(){" in text
    assert ".bio-print-block{font-size:9pt;line-height:1.35;color:#27313a;white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.2em;text-align:left;}" in text
    assert ".bio-summary-card .bio-summary-output{height:236px;max-height:236px;}" in text
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
