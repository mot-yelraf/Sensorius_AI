"""Tests for the long-running Web UI metric probe helpers."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from testApparatus import webui_metric_probe


def test_summarize_backend_payload_counts_metrics_and_latest_timestamp():
    payload = {
        "values": {
            "sensor-a": {"Temperature": 72.1, "Humidity": 41.0},
            "sensor-b": {"Ambient VPD": 1.2},
        },
        "timestamps": {
            "sensor-a": "2026-07-01T10:01:00",
            "sensor-b": "2026-07-01T10:02:00",
        },
    }

    summary = webui_metric_probe.summarize_backend_payload(payload)

    assert summary["sensor_count"] == 2
    assert summary["metric_count"] == 3
    assert summary["latest_timestamp"] == "2026-07-01T10:02:00"
    assert summary["values_hash"]


def test_classify_sample_reports_frontend_stall_when_backend_is_current():
    browser = {
        "last_json_age_sec": 120.0,
        "last_json_latest_timestamp": "2026-07-01T10:00:00",
        "update_in_flight": True,
        "update_in_flight_age_sec": 120.0,
        "probe": {"last_json_fetch_ok": True, "last_json_fetch_age_sec": 120.0},
    }
    backend = {
        "ok": True,
        "latest_timestamp": "2026-07-01T10:02:00",
    }

    alerts = webui_metric_probe.classify_sample(browser, backend, stall_sec=75.0)
    kinds = {item["kind"] for item in alerts}

    assert "frontend_json_stale" in kinds
    assert "frontend_timestamp_lag" in kinds
    assert "update_gauges_stuck" in kinds


def test_classify_sample_reports_backend_unreachable_first():
    alerts = webui_metric_probe.classify_sample({}, {"ok": False, "error": "connect failed"}, stall_sec=75.0)

    assert alerts == [{"kind": "backend_unreachable", "detail": "connect failed"}]
