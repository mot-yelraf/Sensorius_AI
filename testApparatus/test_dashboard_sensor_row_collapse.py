"""Protect dashboard sensor metric row expand and collapse controls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sensorius.saiHtml import get_gauge_config, render_dashboard


def test_dashboard_sensor_rows_include_responsive_collapse_controls():
    metrics = [
        "Temperature",
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Dew Point",
        "CO2",
        "Gas",
        "Baro-Pressure",
    ]
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["aht-test123"],
            {"aht-test123": {metric: 1.0 for metric in metrics}},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"aht-test123": metrics},
        )
    )

    assert "class='sensor-collapse-toggle'" in html
    assert "class='settings-gear-icon'" in html
    assert "class='dashboard-graph-icon'" in html
    assert "M7.2 3.9A1.5 1.5" in html
    assert "fill='currentColor'" in html
    assert "stroke='currentColor' stroke-width='2.2'" not in html
    assert "aria-controls='row_aht-test123'" in html
    assert "data-sensor-expanded='false'" in html


def test_dashboard_sensor_rows_use_spaced_theme_tiles_without_glow():
    css = (
        Path(__file__).resolve().parents[1] / "ui_static" / "css" / "app.css"
    ).read_text(encoding="utf-8")
    toggle = css[css.index(".sensor-collapse-toggle{"):css.index(".sensor-collapse-toggle[hidden]")]

    assert "border:0" in toggle
    assert "background:transparent" in toggle
    assert "color:var(--dashboard-card-text)" in toggle
    assert "border-radius:4px" in toggle
    assert "box-shadow" not in toggle
    assert "body.dashboard-page .sensor-group-header{" in css
    assert "background:var(--dashboard-card-bg)" in css
    assert "border-radius:12px" in css
    header = css[css.index(".sensor-group-header{"):css.index(".sensor-group-title{")]
    assert "width:fit-content" in header
    assert "max-width:calc(100% - 10px)" in header
    assert "margin:10px auto 8px" in header
    assert "gap:10px" in css
    assert "width:calc(100% - 10px)" in css
    assert "text-shadow:0 1px 3px" not in css
    assert "drop-shadow(0 0 5px" not in css


def test_alphabetical_sensor_order_preserves_switch_slot_and_handles_missing_locations():
    from sensorius.saiHtml import order_dashboard_sensor_rows

    assert order_dashboard_sensor_rows(
        ["z", "__switches__", "b", "a", "unknown"],
        {"z": " attic ", "b": "Attic", "a": "Garden", "unknown": " "},
    ) == ["b", "__switches__", "z", "a", "unknown"]


def test_switch_devices_sort_within_location_and_preserve_channel_order(monkeypatch):
    from sensorius import saiSwitchSettingsManager

    class EmptySwitchSettings:
        def __init__(self, *args, **kwargs):
            pass

        def list_switches(self):
            return []

        def load(self, switch_id):
            return {}

    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", EmptySwitchSettings)
    controllers = {
        sid: SimpleNamespace(
            switch_id=sid, location=location, is_present=True,
            switches=["Pump", "Fan"], last_state={}, channel_id_for_label={},
        )
        for sid, location in [("switch-z", "Attic"), ("switch-b", "Attic"), ("switch-a", "Garden")]
    }

    def render(mode, card_order=None):
        return "".join(render_dashboard(
            "All", None, [], {}, {}, SimpleNamespace(expected_gauge_map={}),
            switch_controllers=controllers, switch_row_order=mode, switch_card_order=card_order, expected_gauge_map={},
        ))

    alphabetical = render("location_device")
    existing = render("existing")
    assert "data-switch-ids='switch-b,switch-z'" in alphabetical
    assert "data-switch-ids='switch-z,switch-b'" in existing
    assert alphabetical.index("data-switch-ids='switch-b,switch-z'") < alphabetical.index("data-switch-ids='switch-a'")
    switch_cards = alphabetical[alphabetical.index("data-switch-ids='switch-b,switch-z'"):]
    assert switch_cards.index(">Pump<") < switch_cards.index(">Fan<")

    manual = render("existing", ["garden", "attic"])
    assert manual.index("data-switch-ids='switch-a'") < manual.index("data-switch-ids='switch-z,switch-b'")
