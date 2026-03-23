from __future__ import annotations

import sys
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
