from pathlib import Path


def test_dashboard_event_merge_keeps_detailed_origin_labels():
    source = (Path(__file__).resolve().parents[1] / "saiHtml.py").read_text(encoding="utf-8")

    assert 'yield "    if (/\\\\((manual|auto)(\\\\s*-\\\\s*[^)]*)?\\\\)/.test(text)) return 2;"' in source
    assert "const origin = _originFromSource(evt.source);" in source


def test_switch_websocket_prefers_ui_key_for_live_updates():
    source = (Path(__file__).resolve().parents[1] / "saiHtml.py").read_text(encoding="utf-8")

    assert "const uiKey = msg.ui_key || key;" in source
    assert "updateSwitchVisuals(label, data, uiKey);" in source
    assert "appendSwitchEventLine(uiKey, line);" in source
    assert "updateSwitchVisuals(label, data, key);" not in source
    assert "appendSwitchEventLine(key, line);" not in source


def test_dashboard_json_refresh_also_triggers_switch_status_refresh():
    source = (Path(__file__).resolve().parents[1] / "saiHtml.py").read_text(encoding="utf-8")

    assert "if (typeof refreshAndApplySwitchStatus === 'function') {" in source
    assert "if (!window.__lastSwitchStatusFromGaugesAt || (nowMs - window.__lastSwitchStatusFromGaugesAt) >= 12000) {" in source
    assert "window.__lastSwitchStatusFromGaugesAt = nowMs;" in source
    assert "setTimeout(() => refreshAndApplySwitchStatus(), 0);" in source
