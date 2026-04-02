from pathlib import Path


def test_dashboard_event_merge_keeps_detailed_origin_labels():
    source = (Path(__file__).resolve().parents[1] / "saiHtml.py").read_text(encoding="utf-8")

    assert 'yield "    if (/\\\\((manual|auto)(\\\\s*-\\\\s*[^)]*)?\\\\)/.test(text)) return 2;"' in source
    assert "const origin = _originFromSource(evt.source);" in source
