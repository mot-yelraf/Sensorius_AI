"""Test display-only Imperial and Metric unit overrides."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sensorius.saiDisplayUnits import (
    apply_display_units_to_gauge_config,
    convert_display_value,
    normalize_display_unit_system,
)
from sensorius.saiHtml import get_gauge_config, render_dashboard


def test_display_unit_system_defaults_to_imperial():
    assert normalize_display_unit_system(None) == "Imperial"
    assert normalize_display_unit_system("unknown") == "Imperial"
    assert normalize_display_unit_system("metric") == "Metric"


@pytest.mark.parametrize(
    ("metric", "unit_system", "raw", "unit", "expected"),
    [
        ("Temperature", "Imperial", 0.0, "°F", 32.0),
        ("Temperature_F", "Metric", 68.0, "°C", 20.0),
        ("Dew Point Deficit", "Imperial", 10.0, "°F", 18.0),
        ("Baro-Pressure", "Imperial", 1013.25, "inHg", 29.92),
        ("Wind Speed", "Metric", 10.0, "km/h", 16.09),
        ("Rain", "Metric", 1.0, "mm", 25.4),
        ("Rain Rate", "Metric", 1.0, "mm/hr", 25.4),
        ("Lightning Distance", "Metric", 10.0, "km", 16.09),
    ],
)
def test_display_values_override_source_units(metric, unit_system, raw, unit, expected):
    config = apply_display_units_to_gauge_config(get_gauge_config(), unit_system)[metric]
    assert config["unit"] == unit
    assert convert_display_value(raw, config) == pytest.approx(expected, abs=0.01)


def test_temperature_gauge_geometry_is_converted_with_values():
    config = apply_display_units_to_gauge_config(get_gauge_config(), "Imperial")["Temperature"]

    assert config["min"] == -4.0
    assert config["max"] == 140.0
    assert config["ticks"] == [-4.0, 32.0, 50.0, 68.0, 86.0, 104.0, 140.0]
    assert config["zones"][2]["min"] == 50.0
    assert config["zones"][2]["max"] == 86.0


def test_trend_rate_uses_scale_without_temperature_offset():
    config = apply_display_units_to_gauge_config(get_gauge_config(), "Imperial")["Temperature"]

    assert convert_display_value(1.0, config, rate=True) == pytest.approx(1.8)


def test_dashboard_uses_converted_value_unit_and_gauge_metadata():
    gauge_config = apply_display_units_to_gauge_config(get_gauge_config(), "Metric")
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["weather-one"],
            {"weather-one": {"Temperature_F": 68.0}},
            {"weather-one": {"Temperature_F": {"min": 50.0, "avg": 59.0, "max": 68.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"weather-one": ["Temperature_F"]},
            expected_display_style_map={"weather-one": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
        )
    )

    assert "Temperature_F (°C)" in html
    assert ">20.0 °C</span>" in html
    assert '"display_factor": 0.5555555555555556' in html
    assert "const displayValues = values.map(value => convertForDisplay(value, metricConfig));" in html
    assert "const y = graphDisplayValue(vals[i], metricName, false);" in html


@pytest.mark.parametrize("stored_name", ["Soil Temperature", "Soil Temp_C"])
@pytest.mark.parametrize("unit_system,unit,value", [("Imperial", "°F", "58.64"), ("Metric", "°C", "14.8")])
def test_legacy_soil_selection_reads_current_and_legacy_packets(stored_name, unit_system, unit, value):
    values = {"soil-test": {stored_name: 14.8}}
    html = "".join(render_dashboard(
        "All", None, ["soil-test"], values,
        {"soil-test": {stored_name: {"min": 10, "avg": 12, "max": 14.8}}},
        SimpleNamespace(expected_gauge_map={}),
        gauge_config=apply_display_units_to_gauge_config(get_gauge_config(), unit_system),
        expected_gauge_map={"soil-test": ["Soil Temp_C", "Soil Temperature"]},
        display_style="Graph24hr",
    ))
    assert f"Soil Temperature ({unit})" in html
    assert f">{value} {unit}</span>" in html
    assert html.count("data-metric='Soil Temperature' data-display-style=") == 1
    assert '"Soil Temperature": 14.8' in html
    assert values == {"soil-test": {stored_name: 14.8}}
    if unit_system == "Imperial":
        config = apply_display_units_to_gauge_config(get_gauge_config(), unit_system)
        assert config["Soil Temperature"]["zones"][2]["min"] == 50.0


def test_soil_statistics_combine_both_names_without_changing_native_results(tmp_path):
    from datetime import datetime, timedelta, timezone
    from sensorius.saiDataLogger import saiDataLogger
    from sensorius.saiStats import saiStats
    from sensorius.saiMetricNames import normalize_metric_stats

    now = datetime.now(timezone.utc)
    with saiDataLogger(str(tmp_path / "soil.db")) as logger:
        for minutes, name, value in [(3, "Soil Temp_C", 10), (2, "Soil Temp_C", 12), (1, "Soil Temperature", 20)]:
            logger.log_readings((now - timedelta(minutes=minutes)).isoformat(), "soil", {name: value})
        statter = saiStats(logger.db_path)
        for stats in (statter.get_24hr_stats("soil"), statter.get_all_stats_fast()["soil"]):
            normalized = normalize_metric_stats(stats)
            combined = normalized["Soil Temperature"]
            assert combined["min"] == 10
            assert combined["max"] == 20
            assert combined["avg"] == 14
            assert combined["samples"] == 3
            assert stats["Soil Temp_C"]["avg"] == 11
            assert stats["Soil Temperature"]["avg"] == 20
