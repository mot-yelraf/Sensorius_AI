"""Cover biodynamic lunar timing flags.

The cases protect the observer-local timing and classification behavior used
when biodynamic payloads identify lunar events.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiBiodynamics as saiBiodynamics
from sensorius.biodynamic_calendar import core as biodynamic_core

def test_iau_edge_constellation_sextans_maps_to_leo_for_future_months():
    idx = saiBiodynamics._biodynamic_sign_index_for_constellation("Sex")
    meta = saiBiodynamics._sign_meta(idx)

    assert meta["abbr"] == "Leo"
    assert meta["name"] == "Leo"


def test_lunar_flags_for_day_preserves_specific_event_types():
    tzinfo = ZoneInfo("America/Denver")
    day_start = datetime(2026, 3, 8, tzinfo=tzinfo)
    day_end = day_start + timedelta(days=1)
    intervals = [
        saiBiodynamics._Interval(day_start + timedelta(hours=1), day_start + timedelta(hours=2), "lunar_node"),
        saiBiodynamics._Interval(day_start + timedelta(hours=5), day_start + timedelta(hours=6), "perigee"),
        saiBiodynamics._Interval(day_start + timedelta(hours=10), day_start + timedelta(hours=11), "apogee"),
    ]

    flags = saiBiodynamics._lunar_flags_for_day(day_start, day_end, intervals)

    assert flags["lunar_node"] is True
    assert flags["perigee"] is True
    assert flags["apogee"] is True
    assert [event["type"] for event in flags["lunar_events"]] == ["lunar_node", "perigee", "apogee"]


def test_off_overlay_keeps_calendar_kind_backward_compatible_with_specific_event_label():
    tzinfo = ZoneInfo("America/Denver")
    day_start = datetime(2026, 3, 8, tzinfo=tzinfo)
    day_end = day_start + timedelta(days=1)
    day_segments = [
        {
            "start": "00:00",
            "end": "24:00",
            "sign": "Virgo",
            "element": "Earth",
            "plant_part": "Root",
            "color": "#e5b172",
            "accent": "#644817",
            "kind": "sign",
        }
    ]
    intervals = [
        saiBiodynamics._Interval(day_start + timedelta(hours=8), day_start + timedelta(hours=10), "perigee"),
    ]

    segments = saiBiodynamics._apply_off_overlays(day_segments, day_start, day_end, intervals)
    off_segments = [segment for segment in segments if segment.get("kind") == "off"]

    assert len(off_segments) == 1
    assert off_segments[0]["off_kind"] == "perigee"
    assert off_segments[0]["off_label"] == "Perigee"
    assert off_segments[0]["sign"] == "Rest"


def test_daylight_for_day_formats_hours_and_minutes(monkeypatch):
    tzinfo = ZoneInfo("America/Denver")

    class _FakeLocationInfo:
        def __init__(self, **_kwargs):
            self.observer = SimpleNamespace(elevation=0.0)

    def _fake_sun(_observer, *, date, tzinfo):
        return {
            "sunrise": datetime(date.year, date.month, date.day, 5, 32, tzinfo=tzinfo),
            "sunset": datetime(date.year, date.month, date.day, 20, 31, tzinfo=tzinfo),
        }

    monkeypatch.setattr(saiBiodynamics, "LocationInfo", _FakeLocationInfo)
    monkeypatch.setattr(saiBiodynamics, "_astral_sun", _fake_sun)

    payload = saiBiodynamics._daylight_for_day(date(2026, 6, 23), tzinfo, 40.0, -105.0, 1600.0)

    assert payload["sunrise"] == "05:32"
    assert payload["sunset"] == "20:31"
    assert payload["daylight_minutes"] == 899
    assert payload["daylight_label"] == "14 Hrs 59 Mins"


def test_active_calendar_daylight_formats_hours_and_minutes(monkeypatch):
    tzinfo = ZoneInfo("America/Denver")
    config = biodynamic_core.BiodynamicConfig(40.0, -105.0, tzinfo.key)

    class _FakeLocationInfo:
        def __init__(self, **_kwargs):
            self.observer = SimpleNamespace()

    def _fake_sun(_observer, *, date, tzinfo):
        return {
            "sunrise": datetime(date.year, date.month, date.day, 5, 32, tzinfo=tzinfo),
            "sunset": datetime(date.year, date.month, date.day, 20, 31, tzinfo=tzinfo),
        }

    monkeypatch.setattr(biodynamic_core, "LocationInfo", _FakeLocationInfo)
    monkeypatch.setattr(biodynamic_core, "_astral_sun", _fake_sun)

    payload = biodynamic_core._daylight_for_day(date(2026, 6, 23), tzinfo, config)

    assert payload["sunrise"] == "05:32"
    assert payload["sunset"] == "20:31"
    assert payload["daylight_minutes"] == 899
    assert payload["daylight_label"] == "14 Hrs 59 Mins"
