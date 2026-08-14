"""Protect dashboard sensor metric row expand and collapse controls."""

from __future__ import annotations

from types import SimpleNamespace

from sensorius.saiHtml import get_gauge_config, render_dashboard


def test_dashboard_sensor_rows_include_responsive_collapse_controls():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["aht-test123"],
            {"aht-test123": {"Temperature": 72.0, "Humidity": 45.0}},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"aht-test123": ["Temperature", "Humidity"]},
        )
    )

    assert "class='sensor-collapse-toggle'" in html
    assert "M7.2 3.9A1.5 1.5" in html
    assert "aria-controls='row_aht-test123'" in html
    assert "window.refreshSensorRowCollapse = function(group)" in html
    assert "Math.abs(card.offsetTop - firstTop) > 2" in html
    assert "row.classList.toggle('is-collapsed', !expanded)" in html
    assert "button.hidden = !hasMultipleRows" in html
    assert "window.addEventListener('resize'" in html
