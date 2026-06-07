import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_dashboard_loads_draggable_modal_asset():
    from saiHtml import get_gauge_config, render_dashboard

    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
        )
    )

    assert "/ui_static/js/draggable_modals.js" in html


def test_draggable_modal_asset_targets_settings_modal_shells():
    repo_root = Path(__file__).resolve().parents[1]
    js = (repo_root / "ui_static/js/draggable_modals.js").read_text(encoding="utf-8")
    css = (repo_root / "ui_static/css/app.css").read_text(encoding="utf-8")

    assert ".modal, .system-settings-shell, .onboard-modal" in js
    assert ".modal-header, .system-settings-header, .onboard-title" in js
    assert ".system-settings-header" in css
    assert ".onboard-title" in css


def test_sensor_settings_location_input_is_centered_and_constrained():
    repo_root = Path(__file__).resolve().parents[1]
    css = (repo_root / "ui_static/css/app.css").read_text(encoding="utf-8")

    assert ".sensor-location-input{" in css
    assert "#sensorSettingsForm .sensor-location-input{" in css
    assert "width:min(16rem, 100%);" in css
    assert "margin-inline:auto;" in css
