"""Unit coverage for the Web UI profiler helper code."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import profile_webui


def test_select_scenarios_filters_and_deduplicates_by_name():
    selected = profile_webui.select_scenarios("sensor_settings,weather_forecast,fullscreen_graph,calendar_month_selectors,calendar,sensor_settings")

    assert [scenario.name for scenario in selected] == ["sensor_settings", "weather_forecast", "fullscreen_graph", "calendar_month_selectors", "calendar"]


def test_build_js_helper_discovers_current_dashboard_target_shapes():
    helper = profile_webui.build_js_helper(1000)

    assert ".sensor-group[data-sensor-id]" in helper
    assert ".metric-container[data-sensor]" in helper
    assert ".switch-metric-container[data-switch-ids]" in helper
    assert "visibleMetricTargets()" in helper
    assert "if (config.setup)" in helper
    assert "fullscreen_graph_container" in helper
    assert "sensor_settings_links" in helper
    assert "switch_count" in helper
    assert "metric_count" in helper
    assert "weatherForecastModal" in helper
    assert "profileDashboardRefresh" in helper
    assert "overview_image_requested" in helper
    assert "overview.complete" in helper
    assert "pageMetrics()" in helper


def test_modal_scenarios_accept_explicit_targets():
    sensor = next(item for item in profile_webui.SCENARIOS if item.name == "sensor_settings")
    switch = next(item for item in profile_webui.SCENARIOS if item.name == "switch_settings")
    forecast = next(item for item in profile_webui.SCENARIOS if item.name == "weather_forecast")
    fullscreen_graph = next(item for item in profile_webui.SCENARIOS if item.name == "fullscreen_graph")
    month_selectors = next(item for item in profile_webui.SCENARIOS if item.name == "calendar_month_selectors")

    assert "__sensProfilerTargetSensorId" in sensor.js_factory
    assert "__sensProfilerTargetSwitchId" in switch.js_factory
    assert "openWeatherForecastModal" in forecast.js_factory
    assert "forecastFiveDayBtn" in forecast.js_factory
    assert "weatherForecastModal" in forecast.js_factory
    assert ".forecast-days .forecast-day" in forecast.js_factory
    assert "visibleMetricTargets()" in fullscreen_graph.js_factory
    assert "graphButton" in fullscreen_graph.js_factory
    assert "fullscreen_graph_container" in fullscreen_graph.js_factory
    assert "prevBtn" in month_selectors.js_factory
    assert "nextBtn" in month_selectors.js_factory
    assert "monthLabel" in month_selectors.js_factory
    assert "document.getElementById('calendar')" in month_selectors.js_factory
    assert "next month render" in month_selectors.js_factory
    assert "second next month render" in month_selectors.js_factory
    assert "previous month render" in month_selectors.js_factory
    assert "second previous month render" in month_selectors.js_factory
    assert "Calendar month selector sequence mismatch" in month_selectors.js_factory


def test_summary_counts_skipped_and_failed_scenario_samples():
    scenario = next(item for item in profile_webui.SCENARIOS if item.name == "switch_settings")
    summary = profile_webui.build_summary(
        {
            "dashboard": [{"navigation": {"load_event_ms": 12, "dom_content_loaded_ms": 8}}],
            "switch_settings": [
                {
                    "ok": True,
                    "total_ms": 20,
                    "fetch_total_ms": 10,
                    "max_fetch_ms": 10,
                    "long_task_total_ms": 0,
                    "alerts": ["Graph load failed"],
                },
                {"ok": False, "skipped": True, "error": "No switch found on dashboard"},
                {"ok": False, "error": "timeout waiting for switch settings modal"},
            ],
        },
        (scenario,),
    )

    block = summary["switch_settings"]
    assert block["ok_count"] == 1
    assert block["skipped_count"] == 1
    assert block["error_count"] == 1
    assert block["total_ms"]["median_ms"] == 20
    assert block["alerts"] == ["Graph load failed"]


def test_summary_reports_dashboard_refresh_and_renderer_metrics():
    summary = profile_webui.build_summary(
        {
            "dashboard": [
                {
                    "navigation": {"load_event_ms": 12, "dom_content_loaded_ms": 8, "response_end_ms": 5},
                    "page": {"transfer_size": 1200, "decoded_body_size": 2400},
                    "renderer": {"task_ms": 4, "script_ms": 2, "js_heap_used_mb": 12},
                    "refresh": {"ok": True, "total_ms": 20, "overview_image_requested": False},
                },
                {
                    "navigation": {"load_event_ms": 14, "dom_content_loaded_ms": 9, "response_end_ms": 6},
                    "page": {"transfer_size": 1400, "decoded_body_size": 2600},
                    "renderer": {"task_ms": 6, "script_ms": 3, "js_heap_used_mb": 14},
                    "refresh": {"ok": True, "total_ms": 30, "overview_image_requested": True},
                },
            ]
        },
        (),
    )

    assert summary["dashboard"]["refresh_total_ms"]["median_ms"] == 25
    assert summary["dashboard"]["renderer_task_ms"]["median_ms"] == 5
    assert summary["dashboard"]["overview_image_refresh_requests"] == 1


def test_scenario_error_skip_only_marks_optional_missing_targets():
    switch = next(item for item in profile_webui.SCENARIOS if item.name == "switch_settings")
    system = next(item for item in profile_webui.SCENARIOS if item.name == "system_settings")
    forecast = next(item for item in profile_webui.SCENARIOS if item.name == "weather_forecast")
    fullscreen_graph = next(item for item in profile_webui.SCENARIOS if item.name == "fullscreen_graph")

    assert profile_webui.scenario_error_is_skip(switch, "No switch found on dashboard") is True
    assert profile_webui.scenario_error_is_skip(forecast, "Weather forecast disabled on dashboard") is True
    assert profile_webui.scenario_error_is_skip(fullscreen_graph, "No graphable metric found on dashboard") is True
    assert profile_webui.scenario_error_is_skip(system, "No switch found on dashboard") is False
