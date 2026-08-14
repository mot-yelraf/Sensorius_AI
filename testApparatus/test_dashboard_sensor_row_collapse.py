"""Protect dashboard sensor metric row expand and collapse controls."""

from __future__ import annotations

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
    assert "M7.2 3.9A1.5 1.5" in html
    assert "aria-controls='row_aht-test123'" in html
    assert "window.refreshSensorRowCollapse = function(group)" in html
    assert "Array.from(row.children).filter" in html
    assert "const overflowCards = cards.slice(6)" in html
    assert "row.classList.toggle('is-collapsed', !expanded)" in html
    assert "button.hidden = !hasAdditionalMetrics" in html
    assert "data-sensor-expanded='false'" in html
    assert "window.addEventListener('resize'" in html
    assert "document.addEventListener('DOMContentLoaded', initializeCollapseRows)" in html
    assert "window.setTimeout(initializeCollapseRows, 2000)" in html
