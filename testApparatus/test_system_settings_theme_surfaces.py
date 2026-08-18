"""Verify settings inset panels inherit the selected dashboard theme."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_inset_surfaces_use_dashboard_theme_colors():
    css = (ROOT / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")

    dialog_panels = css[
        css.index("body.dashboard-page #setupPiModal #automationEditorWrap .section,") :
        css.index("body.dashboard-page #setupPiModal .integration-state,")
    ]
    assert "#automationEditorWrap > .pane-footer" in dialog_panels
    assert ".locations-scroll" in dialog_panels
    assert ".remove-scroll" in dialog_panels
    assert "background:var(--dashboard-dialog-panel)" in dialog_panels
    assert "border-color:var(--dashboard-card-border)" in dialog_panels

    cards = css[
        css.index("body.dashboard-page #setupPiModal .integration-state,") :
        css.index("body.dashboard-page #setupPiModal .integration-state-detail,")
    ]
    assert ".location-item" in cards
    assert ".adv-debug-scroll" in cards
    assert ".sai-system-dialog-device-list" in cards
    assert "#add-mac-hint" in cards
    assert ".onboard-steps .step.pending" in cards
    assert "background:var(--dashboard-card-bg)" in cards
    assert "color:var(--dashboard-card-text)" in cards


def test_affected_settings_panels_use_the_shared_themed_classes():
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(
        encoding="utf-8"
    )

    for element_id in (
        "nodus-wifi-device-list",
        "locations-list",
        "ecowitt-sensor-list",
        "ota-package-summary",
        "ota-device-list",
        "remove-device-list",
        "weewx-runtime-state",
        "adv-debug-modules",
    ):
        assert f'id="{element_id}"' in template

    assert 'id="automationEditorWrap"' in template
    assert template.count('class="section"') >= 2


def test_nodus_onboarding_theme_surfaces_cover_macos_linux_and_raspberry_pi():
    css = (ROOT / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")
    template = (ROOT / "ui_templates" / "modals" / "system_settings.html").read_text(
        encoding="utf-8"
    )

    assert 'id="add-mac-hint" class="detail"' in template
    assert "background:var(--dashboard-card-bg, #f4faf6)" in template
    assert template.count('class="step pending"') == 4
    assert "body.dashboard-page #setupPiModal .onboard-steps .step.pending{" in css
    assert "border:1px solid var(--dashboard-card-border)" in css
    assert 'id="add-device-network-field" hidden' in template
    assert 'id="add-device-network" name="target_ap"' in template
    assert 'platformName === "Darwin"' in template


def test_sensor_and_switch_dialog_cards_use_dashboard_theme_layers():
    css = (ROOT / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")

    cards = css[
        css.index("body.dashboard-page #sensorSettingsModal .section,") :
        css.index("body.dashboard-page #sensorSettingsModal .network-info-cell,")
    ]
    assert "#switchSettingsModal .section" in cards
    assert "background:var(--dashboard-card-bg)" in cards
    assert "border-color:var(--dashboard-card-border)" in cards

    rows = css[
        css.index("body.dashboard-page #sensorSettingsModal .network-info-cell,") :
        css.index("body.dashboard-page #sensorSettingsModal .section .muted,")
    ]
    assert "#sensorSettingsModal .statistics-row" in rows
    assert "#switchSettingsModal .network-info-cell" in rows
    assert "#switchSettingsModal .statistics-row" in rows
    assert "background:var(--dashboard-dialog-panel)" in rows
    assert "color:var(--dashboard-card-text)" in rows

    sensor_template = (ROOT / "ui_templates" / "modals" / "sensor_settings.html").read_text(
        encoding="utf-8"
    )
    switch_template = (ROOT / "ui_templates" / "modals" / "switch_settings.html").read_text(
        encoding="utf-8"
    )
    assert sensor_template.count('class="section"') >= 6
    assert 'id="sensor-statistics-pane"' in sensor_template
    assert 'class="network-info-cell"' in sensor_template
    assert 'class="statistics-row"' in sensor_template
    assert 'id="switchStatisticsPane"' in switch_template
    assert 'class="network-info-cell"' in switch_template
    assert 'class="statistics-row"' in switch_template
