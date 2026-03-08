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

    def get_available_sensors(self):
        return ["avpd-j21vxj"]

    def get_biodynamic_daily_summary(self, summary_date: str) -> str:
        return self.saved.get(summary_date, "")

    def save_biodynamic_daily_summary(self, summary_date: str, summary_text: str) -> bool:
        self.saved[summary_date] = summary_text
        return True


class _FakeSensorMgr:
    def list_ids(self):
        return ["avpd-j21vxj"]

    def get_display_metrics(self, sensor_id: str):
        assert sensor_id == "avpd-j21vxj"
        return ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure"]


class _FakeStats:
    def get_stats_for_range(self, sensor_id: str, start_epoch: float, end_epoch: float):
        assert sensor_id == "avpd-j21vxj"
        assert end_epoch > start_epoch
        return {
            "Ambient VPD": {"avg": 1.957, "min": 1.901, "max": 2.003},
            "Temperature": {"avg": 23.59, "min": 22.81, "max": 23.63},
            "Rel-Humidity": {"avg": 32.78, "min": 32.40, "max": 33.31},
            "Baro-Pressure": {"avg": 822.4, "min": 822.0, "max": 823.0},
            "DewVPD Risk": {"avg": 48.0, "min": 0.0, "max": 70.0},
        }


def test_build_summary_text_includes_metrics_and_biodynamic(monkeypatch):
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
        sensor_mgr=_FakeSensorMgr(),
        statter=_FakeStats(),
    )

    text = service.build_summary_text(date(2026, 3, 8))

    assert "24 hr Metrics for 2026-03-07" in text
    assert "AVPD-J21VXJ | Ambient VPD (kPa): avg 1.957 | min 1.901 | max 2.003" in text
    assert "AVPD-J21VXJ | Temperature (°C): avg 23.59 | min 22.81 | max 23.63" in text
    assert "Astral & Biodynamic for 2026-03-08" in text
    assert "Astral location unavailable." in text
    assert "Biodynamic: Leo Moon | Fire / Fruit" in text
    assert "Zodiac: Leo" in text
    assert "Biodynamic Hints" in text
    assert "Suggestion: favor fruiting, seed-setting, and ripening observations." in text
    assert "higher VPD suggests avoiding unnecessary stress" in text


def test_ensure_summary_for_date_writes_once(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
        sensor_mgr=_FakeSensorMgr(),
        statter=_FakeStats(),
    )

    assert service.ensure_summary_for_date(date(2026, 3, 8)) is True
    assert service.ensure_summary_for_date(date(2026, 3, 8)) is False
    assert "2026-03-08" in logger.saved


def test_ensure_summary_for_date_repairs_incomplete_existing_row(monkeypatch):
    monkeypatch.setattr(saiDailySummary, "get_biodynamic_payload", lambda anchor: {"ok": False, "reason": "unavailable"})
    logger = _FakeLogger()
    logger.saved["2026-03-08"] = "24 hr Metrics for 2026-03-07\nNo display-metric data found for the previous day."
    service = saiDailySummary.DailySummaryService(
        settings=_FakeSettings(),
        data_logger=logger,
        sensor_mgr=_FakeSensorMgr(),
        statter=_FakeStats(),
    )

    assert service.ensure_summary_for_date(date(2026, 3, 8)) is True
    assert "Astral & Biodynamic for 2026-03-08" in logger.saved["2026-03-08"]
    assert "Biodynamic Hints" in logger.saved["2026-03-08"]
