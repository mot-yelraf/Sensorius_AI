"""Exercise integrated biodynamic cache, parity, and calendar edge cases.

These tests protect the shared calculation path used by the dashboard,
full-screen calendar, daily summaries, and biodynamic automations.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sensorius import saiBiodynamics
from sensorius.biodynamic_calendar import core


def _cache_key(month: int, *, timezone_name: str = "America/Denver") -> tuple[str, str, str, str]:
    return (f"2026-{month:02d}-01", "39.7392", "-104.9903", timezone_name)


def _cached_payload(index: int, *, today: str = "2026-09-04") -> dict[str, object]:
    return {
        "ok": True,
        "current": {"timestamp": f"{today}T12:00:00-06:00"},
        "calendar": [{"date": f"2026-{index:02d}-01"}],
    }


def test_integrated_payload_cache_is_bounded_and_returns_defensive_copies(monkeypatch):
    monkeypatch.setattr(core, "_PAYLOAD_CACHE_MAX", 2)
    with core._PAYLOAD_CACHE_LOCK:
        core._PAYLOAD_CACHE.clear()
    try:
        core._payload_cache_set(_cache_key(1), _cached_payload(1), now_monotonic=100.0)
        core._payload_cache_set(_cache_key(2), _cached_payload(2), now_monotonic=100.0)
        first = core._payload_cache_get(
            _cache_key(1),
            now_monotonic=100.0,
            current_date="2026-09-04",
        )
        assert first is not None
        first["calendar"][0]["date"] = "contaminated"

        clean = core._payload_cache_get(
            _cache_key(1),
            now_monotonic=100.0,
            current_date="2026-09-04",
        )
        assert clean is not None
        assert clean["calendar"][0]["date"] == "2026-01-01"

        core._payload_cache_set(_cache_key(3), _cached_payload(3), now_monotonic=100.0)
        assert list(core._PAYLOAD_CACHE) == [_cache_key(1), _cache_key(3)]
    finally:
        core.clear_biodynamic_payload_cache()


def test_integrated_payload_cache_supports_concurrent_access(monkeypatch):
    monkeypatch.setattr(core, "_PAYLOAD_CACHE_MAX", 16)
    core.clear_biodynamic_payload_cache()

    def exercise(index: int):
        key = (f"2026-{(index % 12) + 1:02d}-01", str(index % 4), "0", "UTC")
        payload = {
            "current": {"timestamp": "2026-09-04T12:00:00+00:00"},
            "calendar": [{"index": index}],
        }
        core._payload_cache_set(key, payload, now_monotonic=100.0)
        return core._payload_cache_get(key, now_monotonic=100.0, current_date="2026-09-04")

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(exercise, range(100)))
        assert any(result is not None for result in results)
        assert len(core._PAYLOAD_CACHE) <= 16
    finally:
        core.clear_biodynamic_payload_cache()


def test_dashboard_entrypoint_uses_the_integrated_calculation_engine(monkeypatch):
    expected = {"ok": True, "calendar": [{"date": "canonical"}]}
    calls = []

    monkeypatch.setattr(
        saiBiodynamics,
        "_resolve_location",
        lambda: (39.7392, -104.9903, "America/Denver", 1609.0),
    )

    def build(anchor, *, config):
        calls.append((anchor, config))
        return expected

    monkeypatch.setattr("sensorius.biodynamic_calendar.get_biodynamic_payload", build)

    payload = saiBiodynamics.get_biodynamic_payload(date(2026, 9, 1))

    assert payload is expected
    assert calls[0][0] == date(2026, 9, 1)
    assert calls[0][1].timezone_name == "America/Denver"


def test_month_arithmetic_handles_leap_year_and_year_rollover():
    assert core._add_months(date(2024, 2, 29), 0) == date(2024, 2, 1)
    assert core._add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert core._add_months(date(2027, 1, 1), -1) == date(2026, 12, 1)


def test_segment_timeline_preserves_dst_boundary_offsets():
    tzinfo = ZoneInfo("America/Denver")
    days = [{"date": "2026-03-08", "segments": [{"start": "00:00", "end": "24:00", "sign": "Pisces"}]}]

    segment = core._build_segment_timeline(days, tzinfo)[0]

    assert segment["start_local"].utcoffset() == timedelta(hours=-7)
    assert segment["end_local"].utcoffset() == timedelta(hours=-6)
    elapsed = segment["end_local"].astimezone(timezone.utc) - segment["start_local"].astimezone(timezone.utc)
    assert elapsed == timedelta(hours=23)


def test_daylight_handles_twilight_failure_and_polar_day_night():
    reykjavik = core.BiodynamicConfig(64.1466, -21.9426, "Atlantic/Reykjavik")
    midsummer = core._daylight_for_day(date(2026, 6, 21), ZoneInfo(reykjavik.timezone_name), reykjavik)
    assert midsummer["sunrise"]
    assert midsummer["sunset"]
    assert 1200 < midsummer["daylight_minutes"] < 1440

    longyearbyen = core.BiodynamicConfig(78.2232, 15.6469, "Arctic/Longyearbyen")
    polar_day = core._daylight_for_day(date(2026, 6, 21), ZoneInfo(longyearbyen.timezone_name), longyearbyen)
    polar_night = core._daylight_for_day(date(2026, 12, 21), ZoneInfo(longyearbyen.timezone_name), longyearbyen)
    assert polar_day["daylight_minutes"] == 1440
    assert polar_day["polar_condition"] == "polar_day"
    assert polar_night["daylight_minutes"] == 0
    assert polar_night["polar_condition"] == "polar_night"


def test_southern_hemisphere_daylight_seasons_are_not_northern_inverted():
    sydney = core.BiodynamicConfig(-33.8688, 151.2093, "Australia/Sydney")
    tzinfo = ZoneInfo(sydney.timezone_name)

    december = core._daylight_for_day(date(2026, 12, 21), tzinfo, sydney)
    june = core._daylight_for_day(date(2026, 6, 21), tzinfo, sydney)

    assert december["daylight_minutes"] > june["daylight_minutes"]
    assert december["sunrise"] and december["sunset"]
    assert june["sunrise"] and june["sunset"]


def test_integrated_astro_payload_includes_moon_data():
    config = core.BiodynamicConfig(39.7392, -104.9903, "America/Denver")

    payload = core.get_astro_payload(
        config=config,
        target_date=date(2026, 9, 4),
        include_graphs=False,
    )

    assert payload["ok"] is True
    assert payload["moon_phase_label"]
    assert payload["moon_phase_value"] is not None
    assert payload["moon_lit_pct"] is not None
    assert payload["cosmic_attributes"]


def test_off_period_overlay_clips_cleanly_across_midnight():
    tzinfo = ZoneInfo("UTC")
    day_start = datetime(2026, 12, 31, tzinfo=tzinfo)
    day_end = day_start + timedelta(days=1)
    segments = [
        {
            "start": "00:00",
            "end": "24:00",
            "sign": "Gemini",
            "element": "Air",
            "plant_part": "Flower",
            "color": "#fff",
            "accent": "#000",
            "kind": "sign",
        }
    ]
    interval = core._Interval(day_start - timedelta(hours=1), day_start + timedelta(hours=1), "lunar_node")

    overlaid = core._apply_off_overlays(segments, day_start, day_end, [interval])

    assert overlaid[0]["start"] == "00:00"
    assert overlaid[0]["end"] == "01:00"
    assert overlaid[0]["off_kind"] == "lunar_node"
    assert overlaid[1]["start"] == "01:00"
