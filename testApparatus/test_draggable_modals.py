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


def test_info_panes_use_compact_stats_layout():
    repo_root = Path(__file__).resolve().parents[1]
    css = (repo_root / "ui_static/css/app.css").read_text(encoding="utf-8")
    sensor_template = (repo_root / "ui_templates" / "modals" / "sensor_settings.html").read_text(encoding="utf-8")
    switch_template = (repo_root / "ui_templates" / "modals" / "switch_settings.html").read_text(encoding="utf-8")

    assert ".compact-info-form{" in css
    assert ".compact-info-form .statistics-row{" in css
    assert ".compact-info-form .network-info-cell{" in css
    sensor_info_pos = sensor_template.index('id="sensor-statistics-pane"')
    assert sensor_template.index('class="form sensor-pane-scroll compact-info-form"', sensor_info_pos)
    assert 'class="form compact-info-form"' in switch_template
    assert ".automation-chooser .modal-footer,\n#automationEditorWrap .modal-footer" in css
    assert 'id="jsonPreview"' not in switch_template


def test_settings_status_feedback_uses_hidden_live_regions_and_common_footers():
    repo_root = Path(__file__).resolve().parents[1]
    css = (repo_root / "ui_static/css/app.css").read_text(encoding="utf-8")
    combined_css = (repo_root / "ui_static/css/combined.css").read_text(encoding="utf-8")
    sensor_template = (repo_root / "ui_templates" / "modals" / "sensor_settings.html").read_text(encoding="utf-8")
    switch_template = (repo_root / "ui_templates" / "modals" / "switch_settings.html").read_text(encoding="utf-8")
    system_template = (repo_root / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    ha_template = (repo_root / "ui_templates" / "modals" / "system_ha_integration.html").read_text(encoding="utf-8")
    locations_template = (repo_root / "ui_templates" / "modals" / "system_device_locations.html").read_text(encoding="utf-8")
    calibration_template = (repo_root / "ui_templates" / "modals" / "system_calibration.html").read_text(encoding="utf-8")

    assert ".sai-live-status{" in css
    assert ".sai-live-status{" in combined_css
    assert "position:absolute !important;" in css
    assert "sensor-pane-footer-status" not in css
    assert ".switch-dialog-content .modal-footer{" in css
    assert "margin-left:-.9rem;" in css
    assert "margin-right:-.9rem;" in css

    assert "#setupPiModal .pane-footer {" in system_template
    assert "margin-left: -.85rem;" in system_template
    assert "margin-right: -.85rem;" in system_template
    assert "height: min(46rem, 90vh);" in system_template
    assert "#setupPiModal .system-settings-body {\n  padding: .85rem 1.15rem;\n  flex: 1 1 auto;" in system_template
    assert "#setupPiModal .settings-pane {\n  display: flex;\n  flex-direction: column;\n  height: 100%;" in system_template
    assert "syncSystemPaneHeight" not in system_template
    assert "--system-settings-pane-height" not in system_template
    for status_id in (
        "system-status",
        "ha-status",
        "weewx-status",
        "farm-status",
        "locations-status",
        "adv-status",
    ):
        line = next(line for line in system_template.splitlines() if f'id="{status_id}"' in line)
        assert "sai-live-status" in line

    assert 'id="sensorSettingsStatus" class="sai-live-status" aria-live="polite"' in sensor_template
    assert 'id="devCalStatus" class="sai-live-status" aria-live="polite"' in sensor_template
    assert 'id="sysCalStatus" class="sai-live-status" aria-live="polite"' in sensor_template
    assert "sensor-pane-footer-status" not in sensor_template

    assert 'id="switchSettingsStatus" class="sai-live-status" aria-live="polite"' in switch_template
    assert 'id="automationSaveStatus" class="sai-live-status" aria-live="polite"' in switch_template
    assert 'class="modal-footer switch-form-footer sensor-pane-footer"' in switch_template

    assert 'id="ha-save-status" class="sai-live-status" aria-live="polite"' in ha_template
    assert 'id="locations-save-status" class="sai-live-status" aria-live="polite"' in locations_template
    assert "min-height:1.2rem" not in ha_template
    assert "min-height:1.2rem" not in locations_template
    assert 'class="sai-live-status"' in calibration_template


def test_obsolete_standalone_advanced_automation_modal_is_removed():
    repo_root = Path(__file__).resolve().parents[1]
    html = (repo_root / "saiHtml.py").read_text(encoding="utf-8")
    routes = (repo_root / "saiWebRoutes.py").read_text(encoding="utf-8")
    automation_js = (repo_root / "ui_static" / "js" / "advanced_automation.js").read_text(encoding="utf-8")
    css = (repo_root / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")

    assert not (repo_root / "ui_templates" / "modals" / "advanced_automation.html").exists()
    assert "/ui/modal/advanced-automation" not in routes
    assert "/switch-advanced" not in routes
    assert "openAdvancedSwitchModal" not in html
    assert "closeAdvancedSwitchModal" not in html
    assert "automationManagerModal" not in automation_js
    assert "jsonPreview" not in automation_js
    assert ".json-preview" not in css
