"""Focused tests for dashboard gauge metadata."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiHtml import get_gauge_config, render_dashboard, render_graph_modal


def test_soil_fertility_index_gauge_config_uses_contract_scale_and_zones():
    cfg = get_gauge_config()["Soil Fertility Index"]

    assert cfg["unit"] == "%"
    assert cfg["min"] == 0
    assert cfg["max"] == 100
    assert cfg["ticks"] == [0, 25, 50, 75, 100]
    assert cfg["zones"] == [
        {"strokeStyle": "#f00", "min": 0, "max": 50},
        {"strokeStyle": "#ffcc00", "min": 50, "max": 75},
        {"strokeStyle": "#66cc66", "min": 75, "max": 100},
    ]


def test_fullscreen_graph_uses_soil_fertility_gauge_zone_backgrounds():
    html = "".join(render_graph_modal(switch_installed=False))

    assert "const GRAPH_GAUGE_CONFIG = " in html
    assert '"Soil Fertility Index"' in html
    assert "const gaugeZonesBackgroundGraph" in html
    assert "soilFertilityGaugeZones(graphMetricNameFromKey(k))" in html
    assert "gaugeZonesBackgroundGraph: { zonesByAxis: gaugeZonesByAxis }" in html
    assert "y1Opts.min = gaugeAxisBounds.y1.min" in html


def test_dashboard_micrograph_uses_soil_fertility_gauge_scale():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["soil-123"],
            {"soil-123": {"Soil Fertility Index": 82.0}},
            {"soil-123": {"Soil Fertility Index": {"min": 70.0, "avg": 82.0, "max": 92.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"soil-123": ["Soil Fertility Index"]},
            expected_display_style_map={"soil-123": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "isSoilFertilityIndex ? gaugeConfig?.['Soil Fertility Index']" in html
    assert "yScaleOptions.min = cfgMin" in html
    assert "yScaleOptions.max = cfgMax" in html


def test_moon_position_footer_falls_back_to_nearest_moon_event():
    astro_payload = {
        "ok": True,
        "lat": 40.0,
        "lon": -105.0,
        "tz": "America/Denver",
        "sunrise": "05:59",
        "sunset": "20:23",
        "sun_noon": "13:11",
        "sun_points": [],
        "moon_points": [],
        "moon_phase_value": 18.5,
        "moon_phase_label": "Waning Gibbous",
        "moon_lit_pct": 78,
        "moon_rise": "00:30",
        "moon_set": "10:32",
        "moon_rise_today": "",
        "moon_set_today": "10:32",
        "moon_declination": None,
        "moon_position_source": "",
        "moon_next_phase_label": "3rd Quarter",
        "moon_next_phase_date": "2026-06-08",
        "moon_visible_angle": None,
        "moon_reference_angle": None,
    }
    html = "".join(
        render_dashboard(
            "All",
            None,
            [],
            {},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={},
            astro_payload=astro_payload,
        )
    )

    assert "const moonEventRaw = (sameDay, nearest) => {" in html
    assert "const mrRaw = moonEventRaw(data && data.moon_rise_today, data && data.moon_rise);" in html
    assert "moonRiseEl.textContent = fmtSun(mrRaw);" in html
    assert '"moon_rise": "00:30"' in html
