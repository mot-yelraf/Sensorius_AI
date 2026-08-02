"""Prevent legacy navigation labels from returning to dashboard UI assets."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_BUTTON_LABEL = re.compile(
    r">\s*(?:Home|Back|Back to Sensorius)\s*</button>",
    re.IGNORECASE,
)


def test_navigation_buttons_use_dashboard_label():
    ui_sources = [ROOT / "sensorius" / "saiHtml.py", *(ROOT / "ui_templates").rglob("*.html")]

    violations = []
    for source in ui_sources:
        text = source.read_text(encoding="utf-8")
        if LEGACY_BUTTON_LABEL.search(text):
            violations.append(str(source.relative_to(ROOT)))

    assert not violations, f"Legacy Home/Back button labels found in: {violations}"


def test_generated_calendar_navigation_uses_integrated_route():
    text = (ROOT / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert "closeBtn.textContent = 'Back to Sensorius'" not in text
    assert "window.openBiodynamicCalendar = function(){" in text
    assert "window.requestAnimationFrame(function(){" in text
    assert "window.requestAnimationFrame(function(){ window.location.assign('/calendar'); });" in text
    assert "url.port = '8765'" not in text
    assert ">Dashboard</button>" in text
