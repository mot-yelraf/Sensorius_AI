"""Pytest coverage for daily summary generation and persistence.

These tests verify summary text ordering, repair of incomplete rows, and date
window refresh behavior for the persisted daily summary service.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import saiDailySummary


class _FakeSettings:
    def get_setting(self, section, key, default=None):
        if section == "Time" and key in {"TZ", "tz"}:
            return "America/Denver"
        return default

    def resolve_astral_location(self, persist_if_auto=False, timeout_sec=0):
        return {"lat": None, "lon": None, "tz": "America/Denver"}


class _FakeLogger:
    def __init__(self):
        self.saved = {}

    def get_biodynamic_daily_summary(self, summary_date: str) -> str:
        return self.saved.get(summary_date, "")

    def save_biodynamic_daily_summary(self, summary_date: str, summary_text: str) -> bool:
        self.saved[summary_date] = summary_text
        return True


def test_build_summary_text_includes_hints_then_astral(monkeypatch):
    monkeypatch.setattr(
        saiDailySummary,
        "get_biodynamic_payload",
        lambda anchor: {
            "ok": True,
            "calendar": [
                {
                    "date": "2026-03-08",
                    "dominant_sign": "Leo",
                    "dominant_element": "Fire",
                    "dominant_plant_part": "Fruit",
                    "segments": [
                        {"start": "00:00", "end": "08:07", "sign": "Leo"},
                        {"start": "08:07", "end": "23:59", "sign": "Virgo"},
                    ],
                }
            ],
        },
    )

    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
    )

    text = service.build_summary_text(date(2026, 3, 8))

    assert "Biodynamic Hints" in text
    assert "Suggestion: favor fruiting, seed-setting, and ripening observations." in text
    assert "prioritize actual plant health" in text
    assert "Astral Notes" in text
    assert "Astral location unavailable." in text
    assert "Biodynamic: Leo Moon | Fire / Fruit" in text
    assert "Zodiac: Leo" in text
    assert "24 hr Metrics for 2026-03-07" not in text
    assert text.index("Biodynamic Hints") < text.index("Astral Notes")


def test_build_hint_lines_supports_stage_lunar_flags_and_plant_state():
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
    )
    lines = service._build_hint_lines(
        date(2026, 3, 8),
        {
            "dominant_sign": "Virgo",
            "dominant_plant_part": "Root",
            "moon_direction": "descending",
            "lunar_node": True,
            "perigee": "true",
        },
        crop_stage="seedling",
        plant_state={"stress": True, "vpd": "2.1"},
    )

    assert "Suggestion: favorable for transplanting, root establishment, and reducing transplant shock." in lines
    assert any(line.startswith("Timing: descending Moon") for line in lines)
    assert any(line.startswith("Caution: biodynamic calendars often treat lunar nodes") for line in lines)
    assert any(line.startswith("Caution: perigee") for line in lines)
    assert any(line.startswith("Plant Condition: visible or measured stress") for line in lines)
    assert any(line.startswith("Plant Condition: elevated VPD") for line in lines)
    assert lines[-1].startswith("Suggestion: prioritize actual plant health")


def test_build_hint_lines_falls_back_for_unknown_stage_and_part():
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
    )

    leaf_lines = service._build_hint_lines(
        date(2026, 3, 8),
        {"dominant_sign": "Cancer", "dominant_plant_part": "Leaf"},
        crop_stage="unknown",
    )
    unknown_lines = service._build_hint_lines(
        date(2026, 3, 8),
        {"dominant_sign": "Ophiuchus", "dominant_plant_part": ""},
    )

    assert "Suggestion: favor irrigation timing, canopy recovery, and leafy-growth observations." in leaf_lines
    assert "Suggestion: use Ophiuchus Moon as a planning cue rather than a rigid rule." in unknown_lines


def test_ensure_summary_for_date_writes_once(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
    )

    assert service.ensure_summary_for_date(date(2026, 3, 8)) is True
    assert service.ensure_summary_for_date(date(2026, 3, 8)) is False
    assert "2026-03-08" in logger.saved


def test_ensure_summary_for_date_repairs_incomplete_existing_row(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    logger.saved["2026-03-08"] = "Astral\nAstral location unavailable."
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
    )

    assert service.ensure_summary_for_date(date(2026, 3, 8)) is True
    assert "Biodynamic Hints" in logger.saved["2026-03-08"]
    assert "Astral Notes" in logger.saved["2026-03-08"]


def test_unavailable_summary_is_detected_for_location_repair():
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
    )
    stale = (
        "Biodynamic Hints\n"
        "Suggestion: no biodynamic hint available for this day.\n\n"
        "Astral Notes\n"
        "Astral location unavailable.\n"
        "Biodynamic: unavailable (location_unavailable)"
    )

    assert service._summary_is_complete(date(2026, 3, 8), stale) is True
    assert service.summary_needs_location_repair(stale) is True


def test_ensure_summary_repairs_transient_location_failure_after_resolution(monkeypatch):
    monkeypatch.setattr(
        saiDailySummary,
        "get_biodynamic_payload",
        lambda anchor: {
            "ok": True,
            "calendar": [
                {
                    "date": anchor.isoformat(),
                    "dominant_sign": "Taurus",
                    "dominant_element": "Earth",
                    "dominant_plant_part": "Root",
                    "segments": [],
                }
            ],
        },
    )

    class _ResolvedSettings(_FakeSettings):
        def resolve_astral_location(self, persist_if_auto=False, timeout_sec=0):
            return {"lat": 32.79, "lon": -108.2749, "tz": "America/Denver"}

    logger = _FakeLogger()
    logger.saved["2026-03-08"] = (
        "Biodynamic Hints\n"
        "Suggestion: no biodynamic hint available for this day.\n\n"
        "Astral Notes\nAstral location unavailable.\n"
        "Biodynamic: unavailable (location_unavailable)"
    )
    service = saiDailySummary.DailySummaryService(settings=_ResolvedSettings(), data_logger=logger)

    assert service.ensure_summary_for_date(date(2026, 3, 8)) is True
    assert "favor root-zone work" in logger.saved["2026-03-08"]
    assert "location_unavailable" not in logger.saved["2026-03-08"]


def test_ensure_summaries_for_window_refreshes_today_only_when_future_rows_are_complete(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    logger.saved["2026-03-08"] = "Biodynamic Hints\nold today\n\nAstral Notes\nold today"
    logger.saved["2026-03-09"] = (
        "Biodynamic Hints\n"
        "Suggestion: no biodynamic hint available for this day.\n\n"
        "Astral Notes\n"
        "future intact"
    )

    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
    )

    writes = service.ensure_summaries_for_window(date(2026, 3, 8), days=3, refresh_start=True)

    assert writes == 2
    assert "old today" not in logger.saved["2026-03-08"]
    assert "future intact" in logger.saved["2026-03-09"]
    assert "Biodynamic Hints" in logger.saved["2026-03-10"]
    assert "Astral Notes" in logger.saved["2026-03-10"]


def test_ensure_summaries_for_window_crosses_month_boundary(monkeypatch):
    calls = []

    def _fake_payload(anchor):
        calls.append(anchor.isoformat())
        return {
            "ok": True,
            "calendar": [
                {
                    "date": anchor.isoformat(),
                    "dominant_sign": "Leo",
                    "dominant_element": "Fire",
                    "dominant_plant_part": "Fruit",
                    "segments": [
                        {"start": "00:00", "end": "23:59", "sign": "Leo"},
                    ],
                }
            ],
        }

    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", _fake_payload)
    logger = _FakeLogger()
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
    )

    writes = service.ensure_summaries_for_window(date(2026, 3, 25), days=29, refresh_start=True)

    assert writes == 29
    assert "2026-03-31" in logger.saved
    assert "2026-04-01" in logger.saved
    assert "2026-04-22" in logger.saved
    assert calls[0] == "2026-03-25"
    assert calls[-1] == "2026-04-22"


def test_annual_forecast_window_anchors_to_month_start(monkeypatch):
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
    )
    calls = []

    def _fake_window(start_date, *, days=saiDailySummary.DEFAULT_FORECAST_DAYS, refresh_start=True):
        calls.append((start_date, days, refresh_start))
        return 3

    monkeypatch.setattr(service, "ensure_summaries_for_window", _fake_window)

    assert saiDailySummary.DEFAULT_FORECAST_DAYS == 366
    assert service.forecast_window_start(date(2026, 5, 25)) == date(2026, 5, 1)
    assert service.ensure_forecast_window(date(2026, 5, 25)) == 3
    assert calls == [(date(2026, 5, 1), saiDailySummary.DEFAULT_FORECAST_DAYS, True)]


class _FakeSupervisor:
    def __init__(self):
        self.feed_calls = []
        self.issues = []

    def feedthedogs(self, name, error=False):
        self.feed_calls.append((name, bool(error)))

    def report_issue(self, task_name, message, *, recommend_restart=True, issue_type="warning"):
        self.issues.append(
            {
                "task_name": task_name,
                "message": message,
                "recommend_restart": recommend_restart,
                "issue_type": issue_type,
            }
        )


def test_daily_summary_run_reports_recoverable_error_without_marking_failed(monkeypatch):
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
        supervisor=_FakeSupervisor(),
    )

    def _boom(_start_date, *, days=saiDailySummary.DEFAULT_FORECAST_DAYS, refresh_start=True):
        raise RuntimeError("summary write failed")

    async def _stop_sleep(_total_sleep_s, heartbeat_every_s=20.0):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "ensure_summaries_for_window", _boom)
    monkeypatch.setattr(service, "_sleep_with_heartbeat", _stop_sleep)

    try:
        asyncio.run(service.run())
    except asyncio.CancelledError:
        pass

    assert ("Daily Summary Writer", True) not in service.supervisor.feed_calls
    assert service.supervisor.issues
    assert service.supervisor.issues[0]["task_name"] == "Daily Summary Writer"
    assert service.supervisor.issues[0]["recommend_restart"] is False


def test_daily_summary_window_work_feeds_watchdog_while_threaded(monkeypatch):
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=_FakeLogger(),
        supervisor=_FakeSupervisor(),
    )
    release = threading.Event()
    calls = {"n": 0}

    def _blocked_window(_start_date):
        release.wait(timeout=1.0)
        return 7

    def _feed_watchdog(*, error=False):
        calls["n"] += 1
        service.supervisor.feedthedogs("Daily Summary Writer", error=error)
        if calls["n"] >= 2:
            release.set()

    monkeypatch.setattr(service, "ensure_summaries_for_window", _blocked_window)
    monkeypatch.setattr(service, "_feed_watchdog", _feed_watchdog)

    result = asyncio.run(service._ensure_summaries_for_window_async(date(2026, 3, 8), heartbeat_every_s=0.1))

    assert result == 7
    assert len(service.supervisor.feed_calls) >= 2
