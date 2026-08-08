"""Focused coverage for the System Settings Advanced pane layout."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_advanced_pane_has_three_collapsible_three_column_sections():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")

    assert template.count('<details class="advanced-section"') == 3
    for section_id, heading in (
        ("adv-section-startup", "Start-up"),
        ("adv-section-database", "Database"),
        ("adv-section-debug", "Debug"),
    ):
        assert f'id="{section_id}" data-runtime-section=' in template
        assert f"<summary>{heading}</summary>" in template
    assert '<details class="advanced-section" id="adv-section-startup" open>' not in template
    assert "grid-template-columns: repeat(3, minmax(220px, 1fr));" in template
    assert "#setupPiModal .advanced-section-grid {" in template


def test_advanced_sections_contain_requested_controls_and_conditional_log_path():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    routes = (ROOT / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")

    for label in (
        "Auto Start on login",
        "Auto-start scope",
        "Maximum Days of Data (30-365)",
        "Archive Database",
        "Renew Database",
        "Sensorius_File_Log",
        "Log Level",
        "Modules to Debug",
    ):
        assert label in template
    assert 'class="adv-log-path" id="adv-log-path" hidden' in template
    assert 'id="adv-log-path-value"' in template
    assert 'if (pathEl) pathEl.hidden = !enabled;' in template
    assert 'if (ev?.target?.id === "adv-file-log")' in template
    assert '"log_file_path": log_file_path' in routes
    assert "Path(log_file).expanduser().resolve()" in routes


def test_collapsible_sections_own_save_actions_and_panes_own_dashboard_action():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")

    advanced = template[template.index('<div class="settings-pane" id="pane-advanced"'):]
    assert advanced.count('class="button blue btn-advanced-save">Save</button>') == 3
    assert advanced.count('class="button black btn-back-system">Dashboard</button>') == 1
    assert 'class="pane-footer pane-global-footer"' in advanced
    assert 'classList.contains("btn-advanced-save")' in template
    assert 'root.querySelectorAll(".btn-advanced-save")' in template

    integrations = template[template.index('<div class="settings-pane" id="pane-integrations"'):template.index('<div class="settings-pane" id="pane-locations"')]
    assert integrations.count('class="pane-footer section-action-footer"') == 3
    assert integrations.count('class="button black btn-back-system">Dashboard</button>') == 1
    assert 'class="pane-footer pane-global-footer"' in integrations


def test_system_sections_include_weather_forecast_in_requested_order():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    system_pane = template[
        template.index('<div class="settings-pane" id="pane-system">'):
        template.index('<div class="settings-pane" id="pane-automations"')
    ]
    expected_sections = (
        'data-runtime-section="system-general"',
        'data-runtime-section="system-wifi"',
        'data-runtime-section="system-astral"',
        'data-runtime-section="system-weather-forecast"',
        'data-runtime-section="system-notifications"',
        'data-runtime-section="system-display"',
    )

    assert all(section in system_pane for section in expected_sections)
    assert [system_pane.index(section) for section in expected_sections] == sorted(
        system_pane.index(section) for section in expected_sections
    )
    assert "integrations-weather-forecast" not in template


def test_sensor_and_switch_footers_match_system_footer_spacing_and_divider():
    app_css = (ROOT / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")
    combined_css = (ROOT / "ui_static" / "css" / "combined.css").read_text(encoding="utf-8")

    expected = (
        ".sensor-pane-footer{\n"
        "  flex:0 0 auto;\n"
        "  margin-top:auto;\n"
        "  padding:.75rem .85rem;\n"
        "  position:relative;\n"
        "  border-top:0;\n"
        "  border-bottom:0;\n"
        "  box-sizing:border-box;"
    )
    assert expected in app_css
    assert expected in combined_css
    for css in (app_css, combined_css):
        assert '.sensor-pane-footer::before{\n  content:"";\n  position:absolute;\n  top:0;\n  left:.85rem;\n  right:.85rem;\n  border-top:1px solid #e6ece8;' in css
        assert ".sensor-pane-footer .button{\n  margin-top:0;\n  margin-bottom:0;" in css
    assert 'class="modal-footer sensor-pane-footer"' in (ROOT / "ui_templates" / "modals" / "sensor_settings.html").read_text(encoding="utf-8")
    assert 'class="modal-footer switch-form-footer sensor-pane-footer"' in (ROOT / "ui_templates" / "modals" / "switch_settings.html").read_text(encoding="utf-8")


def test_all_system_accordions_default_closed_and_use_process_scoped_state():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    routes = (ROOT / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")

    assert template.count("<details") == 12
    assert template.count("data-runtime-section=") == 12
    assert not any(" open>" in line for line in template.splitlines() if "<details" in line)
    assert "window.__sensoriusExpandableSectionState" in template
    assert 'section.addEventListener("toggle"' in template
    assert "runtimeSectionStore.sections[stateKey]" in template
    assert 'syncRuntimeExpandableIdentity(js.runtime_instance_id);' in template
    assert 'if not str(getattr(app.state, "ui_runtime_instance_id", "") or "").strip():' in routes
    assert "app.state.ui_runtime_instance_id = uuid4().hex" in routes
    assert '"runtime_instance_id": app.state.ui_runtime_instance_id' in routes
