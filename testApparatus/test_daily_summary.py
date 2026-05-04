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
    assert "use the biodynamic window as a planning hint" in text
    assert "Astral Notes" in text
    assert "Astral location unavailable." in text
    assert "Biodynamic: Leo Moon | Fire / Fruit" in text
    assert "Zodiac: Leo" in text
    assert "24 hr Metrics for 2026-03-07" not in text
    assert text.index("Biodynamic Hints") < text.index("Astral Notes")


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


def test_ensure_summaries_for_window_refreshes_today_only_when_future_rows_are_complete(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    logger.saved["2026-03-08"] = "Biodynamic Hints\nold today\n\nAstral Notes\nold today"
    logger.saved["2026-03-09"] = "Biodynamic Hints\nfuture intact\n\nAstral Notes\nfuture intact"

    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
    )

    writes = service.ensure_summaries_for_window(date(2026, 3, 8), days=3, refresh_start=True)

    assert writes == 2
    assert "old today" not in logger.saved["2026-03-08"]
    assert logger.saved["2026-03-09"] == "Biodynamic Hints\nfuture intact\n\nAstral Notes\nfuture intact"
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
