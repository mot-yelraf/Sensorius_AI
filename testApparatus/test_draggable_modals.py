"""Test modal assets, layout constraints, and accessibility hooks.

These tests inspect generated HTML, JavaScript, and CSS source rather than
launching a browser.
"""

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_dashboard_loads_draggable_modal_asset():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

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


def test_settings_dialogs_use_titlebar_close_controls_without_dashboard_buttons():
    repo_root = Path(__file__).resolve().parents[1]
    modal_dir = repo_root / "ui_templates" / "modals"
    dashboard_button = re.compile(r">\s*Dashboard\s*</button>", re.IGNORECASE)
    settings_templates = {
        "sensor_settings.html",
        "switch_settings.html",
        "system_calibration.html",
        "system_device_locations.html",
        "system_ha_integration.html",
        "system_remove_device.html",
        "system_settings.html",
    }

    matching_templates = {
        template.name
        for template in modal_dir.glob("*.html")
        if dashboard_button.search(template.read_text(encoding="utf-8"))
    }
    assert not matching_templates

    for template_name in settings_templates:
        template = (modal_dir / template_name).read_text(encoding="utf-8")
        assert template.count('class="settings-title-close') == 1, template_name
        assert 'class="modal-header settings-modal-header"' in template or (
            'class="onboard-title settings-modal-header"' in template
        ) or 'class="system-settings-header settings-modal-header"' in template
        assert 'title="Close"' in template
        assert 'aria-label="Close ' in template

    calibration_js = (repo_root / "ui_static/js/system_calibration.js").read_text(encoding="utf-8")
    assert 'modalEl.querySelector("#sysCalCloseBtn")' in calibration_js
    assert 'if (closeBtn) closeBtn.addEventListener("click", close);' in calibration_js


def test_settings_titlebar_close_control_is_circular_and_keyboard_visible():
    repo_root = Path(__file__).resolve().parents[1]
    for css_name in ("app.css", "combined.css"):
        css = (repo_root / "ui_static/css" / css_name).read_text(encoding="utf-8")
        assert ".settings-modal-header{" in css
        assert ".settings-title-close{" in css
        assert "border-radius:50%;" in css
        assert "background:transparent;" in css
        assert ".settings-title-close::before," in css
        assert "transform:translate(-50%, -50%) rotate(45deg);" in css
        assert ".settings-title-close:focus-visible{" in css


def test_dashboard_window_close_controls_use_transparent_card_color_circles():
    repo_root = Path(__file__).resolve().parents[1]
    dashboard_html = (repo_root / "sensorius/saiHtml.py").read_text(encoding="utf-8")
    caelus_css = (repo_root / "ui_static/weather_forecast/app.css").read_text(encoding="utf-8")
    biodynamic_css = (repo_root / "ui_static/biodynamic_calendar/app.css").read_text(encoding="utf-8")

    assert ".caelus-moon-close{position:relative;width:2rem;height:2rem;padding:0;border:2px solid #7ec4c1;border-radius:50%;background:transparent;" in dashboard_html
    assert ".caelus-moon-close::before,.caelus-moon-close::after" in dashboard_html
    assert "#fullscreen_graph_dashboard{" in dashboard_html
    assert "border:2px solid var(--dashboard-card-border); background:transparent;" in dashboard_html
    assert "color:var(--dashboard-card-text);" in dashboard_html
    assert "#fullscreen_graph_dashboard::before," in dashboard_html

    for stylesheet in (caelus_css, biodynamic_css):
        close_rule = stylesheet[stylesheet.index(".dashboard-return {"):]
        close_rule = close_rule[:close_rule.index("}")]
        assert "border-radius: 50%;" in close_rule
        assert "background: transparent;" in close_rule
        assert "border: 2px solid" in close_rule
        assert ".dashboard-close-icon::before," in stylesheet
        assert "transform: translate(-50%, -50%) rotate(45deg);" in stylesheet


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


def test_automation_definition_scrolls_above_a_fixed_action_footer():
    repo_root = Path(__file__).resolve().parents[1]
    system_template = (repo_root / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    automation_js = (repo_root / "ui_static" / "js" / "advanced_automation.js").read_text(encoding="utf-8")

    editor_start = system_template.index('<div id="automationEditorWrap" hidden>')
    editor_end = system_template.index('</div>\n          </div>', editor_start)
    editor = system_template[editor_start:editor_end]
    form_end = editor.index("</form>")
    footer_start = editor.index('<div class="pane-footer pane-footer-actions-right">')

    assert form_end < footer_start
    assert 'editor.style.display = showChooser ? "none" : "flex";' in automation_js
    assert 'editor.style.display = showChooser ? "none" : "block";' not in automation_js
    assert "#setupPiModal #pane-automations { overflow: hidden; }" in system_template
    assert "background: #fff;" in system_template[
        system_template.index("#setupPiModal #automationEditorWrap > .pane-footer {"):
        system_template.index("#setupPiModal .pane-global-footer")
    ]

    for css_name in ("app.css", "combined.css"):
        css = (repo_root / "ui_static" / "css" / css_name).read_text(encoding="utf-8")
        editor_rule = css[css.index("#automationEditorWrap{"):css.index(".automation-chooser .modal-footer,")]
        assert "flex:1 1 0;" in editor_rule
        assert "height:100%;" in editor_rule
        assert "#automationEditorWrap > form{" in editor_rule
        assert "overflow-y:auto;" in editor_rule
        assert "scrollbar-gutter:stable;" in editor_rule
        assert "#automationEditorWrap > .pane-footer{ flex:0 0 auto; }" in editor_rule


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
    assert "height: min(46rem, calc(100vh - 1.5rem));" in system_template
    assert "#setupPiModal .system-settings-body {\n  padding: .85rem 1.15rem;\n  flex: 1 1 auto;" in system_template
    assert "#setupPiModal .settings-pane {\n  display: flex;\n  flex-direction: column;\n  height: 100%;" in system_template
    assert "overflow-y: auto;" in system_template
    assert "scrollbar-gutter: stable;" in system_template
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
    assert 'id="automationSaveStatus" class="sai-live-status" aria-live="polite"' in system_template
    assert 'class="modal-footer switch-form-footer sensor-pane-footer"' in switch_template

    assert 'id="ha-save-status" class="sai-live-status" aria-live="polite"' in ha_template
    assert 'id="locations-save-status" class="sai-live-status" aria-live="polite"' in locations_template
    assert "min-height:1.2rem" not in ha_template
    assert "min-height:1.2rem" not in locations_template
    assert 'class="sai-live-status"' in calibration_template


def test_obsolete_standalone_advanced_automation_modal_is_removed():
    repo_root = Path(__file__).resolve().parents[1]
    html = (repo_root / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    routes = (repo_root / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")
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


def test_settings_full_page_fallbacks_escape_embedded_scripts_and_open_backdrops():
    repo_root = Path(__file__).resolve().parents[1]
    routes = (repo_root / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")

    assert routes.count('json.dumps(modal_html).replace("</", "<\\\\/")') >= 2
    assert routes.count("if (backdrop) backdrop.style.display = 'flex';") >= 2
    assert "host.querySelectorAll('script').forEach(function(oldScript)" in routes
    assert "function selectSwitchPane(showInfo)" in routes
    assert "if (infoPane) infoPane.hidden = !showInfo;" in routes
