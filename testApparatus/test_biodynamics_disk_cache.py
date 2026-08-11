"""Cover biodynamic payload disk caching.

The tests exercise cache reuse, invalidation, and persistence boundaries for
calendar payloads without relying on a live application process.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiBiodynamics as saiBiodynamics

def test_biodynamic_prewarm_month_anchors_are_ordered_by_ui_value():
    anchors = saiBiodynamics.biodynamic_prewarm_month_anchors(
        date(2026, 7, 15),
        past_months=3,
        future_months=4,
    )

    assert [item.isoformat() for item in anchors] == [
        "2026-07-01",
        "2026-06-01",
        "2026-08-01",
        "2026-09-01",
        "2026-10-01",
        "2026-11-01",
        "2026-05-01",
        "2026-04-01",
    ]


def _install_fake_biodynamics(monkeypatch, tmp_path):
    payload_calls = []

    monkeypatch.setenv("SENSORIUS_BIODYNAMIC_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(saiBiodynamics, "_resolve_location", lambda: (32.79, -108.2749, "UTC", 1798.0))
    monkeypatch.setattr(saiBiodynamics, "_skyfield_runtime", lambda: (None, object(), object(), object()))
    monkeypatch.setattr(saiBiodynamics, "_moon_direction", lambda *_args, **_kwargs: "ascending")
    monkeypatch.setattr(saiBiodynamics, "_daylight_for_day", lambda *_args, **_kwargs: {})

    def _fake_calendar(month_anchor, tzinfo, ts, eph, constellation_at, now_local, lat, lon, altitude):
        payload_calls.append(month_anchor)
        day = date(2026, 6, 1)
        return (
            [
                {
                    "date": day.isoformat(),
                    "day": 1,
                    "weekday": "Mon",
                    "in_month": True,
                    "is_today": False,
                    "segments": [
                        {
                            "start": "00:00",
                            "end": "24:00",
                            "sign": "Aries",
                            "element": "Fire",
                            "plant_part": "Fruit",
                            "color": "#f19707",
                            "accent": "#d64b3b",
                            "kind": "sign",
                        }
                    ],
                    "dominant_sign": "Aries",
                    "dominant_element": "Fire",
                    "dominant_plant_part": "Fruit",
                    "dominant_color": "#f19707",
                    "dominant_accent": "#d64b3b",
                    "moon_direction": "ascending",
                }
            ],
            [],
        )

    monkeypatch.setattr(saiBiodynamics, "_build_calendar", _fake_calendar)
    return payload_calls


def test_biodynamic_payload_reuses_disk_cache_after_memory_clear(tmp_path, monkeypatch):
    payload_calls = _install_fake_biodynamics(monkeypatch, tmp_path)

    saiBiodynamics.clear_biodynamic_payload_cache()
    first = saiBiodynamics.get_biodynamic_payload(date(2026, 6, 1))
    assert first["ok"] is True
    assert [item.isoformat() for item in payload_calls] == ["2026-06-01"]

    saiBiodynamics.clear_biodynamic_payload_cache()
    second = saiBiodynamics.get_biodynamic_payload(date(2026, 6, 1))
    assert second["ok"] is True
    assert second["calendar"][0]["date"] == "2026-06-01"
    assert [item.isoformat() for item in payload_calls] == ["2026-06-01"]
    assert list(tmp_path.glob("*.json"))


def test_biodynamic_payload_ignores_expired_disk_cache(tmp_path, monkeypatch):
    payload_calls = _install_fake_biodynamics(monkeypatch, tmp_path)

    saiBiodynamics.clear_biodynamic_payload_cache()
    first = saiBiodynamics.get_biodynamic_payload(date(2026, 6, 1))
    assert first["ok"] is True
    cache_files = list(tmp_path.glob("*.json"))
    assert len(cache_files) == 1

    old_mtime = time.time() - saiBiodynamics._PAYLOAD_DISK_CACHE_MAX_AGE_SEC - 10
    os.utime(cache_files[0], (old_mtime, old_mtime))

    saiBiodynamics.clear_biodynamic_payload_cache()
    second = saiBiodynamics.get_biodynamic_payload(date(2026, 6, 1))
    assert second["ok"] is True
    assert [item.isoformat() for item in payload_calls] == ["2026-06-01", "2026-06-01"]
