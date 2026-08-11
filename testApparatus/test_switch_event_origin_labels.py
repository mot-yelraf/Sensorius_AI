"""Preserve switch-event origin labels across dashboard update paths.

The assertions inspect generated UI source for merge, websocket, and JSON
refresh behavior.
"""

from pathlib import Path


def test_dashboard_event_merge_keeps_detailed_origin_labels():
    source = (Path(__file__).resolve().parents[1] / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert 'yield "    if (/\\\\((manual|auto)(\\\\s*-\\\\s*[^)]*)?\\\\)/.test(text)) return 2;"' in source
    assert "const origin = _originFromSource(evt.source);" in source


def test_switch_card_events_display_twelve_hour_times_without_changing_raw_identity():
    source = (Path(__file__).resolve().parents[1] / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert "function formatSwitchEventDisplayLine(line)" in source
    assert "const hour12 = ((hour24 + 11) % 12) + 1;" in source
    assert "${hour24 < 12 ? 'AM' : 'PM'}" in source
    assert "li.dataset.rawEvent=textLine;" in source
    assert "li.textContent=formatSwitchEventDisplayLine(textLine);" in source
    assert "li.dataset.rawEvent || li.textContent" in source


def test_switch_websocket_prefers_ui_key_for_live_updates():
    source = (Path(__file__).resolve().parents[1] / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert "const uiKey = msg.ui_key || key;" in source
    assert "updateSwitchVisuals(label, data, uiKey);" in source
    assert "appendSwitchEventLine(uiKey, line);" in source
    assert "updateSwitchVisuals(label, data, key);" not in source
    assert "appendSwitchEventLine(key, line);" not in source


def test_dashboard_json_refresh_also_triggers_switch_status_refresh():
    source = (Path(__file__).resolve().parents[1] / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")

    assert "if (typeof refreshAndApplySwitchStatus === 'function') {" in source
    assert "if (!window.__lastSwitchStatusFromGaugesAt || (nowMs - window.__lastSwitchStatusFromGaugesAt) >= 12000) {" in source
    assert "window.__lastSwitchStatusFromGaugesAt = nowMs;" in source
    assert "setTimeout(() => refreshAndApplySwitchStatus(), 0);" in source
