"""Exercise astronomical event timing, nonblocking snapshots, and email graphics.

The fixed location and dates use local ephemeris data; SMTP is never contacted.
"""

import asyncio
import io
from datetime import datetime, timezone

import pytest

from sensorius.biodynamic_calendar import BiodynamicConfig
from sensorius import saiCalendarAutomation as calendar
from sensorius.saiLunarCalendarGraphic import render_lunar_calendar


def test_phase_schedule_uses_timed_eight_phase_crossings_and_local_sky():
    from sensorius.saiBiodynamics import get_skyfield_runtime_if_installed
    if get_skyfield_runtime_if_installed() is None:
        pytest.skip("Local ephemeris is unavailable")
    from skyfield import almanac
    from astral import Observer
    from sensorius.saiWeatherAstronomy import local_bright_limb_angle, local_lunar_north_angle
    _, ts, eph, _ = get_skyfield_runtime_if_installed()
    events = calendar._phase_schedule("2026-10-13", BiodynamicConfig(40, -105, "America/Denver"))
    assert {event["phase"] for event in events} == set(calendar.LUNAR_PHASES)
    for event in events:
        at = datetime.fromisoformat(event["transition_at"])
        angle = float(almanac.moon_phase(eph, ts.from_datetime(at)).degrees)
        target = event["index"] * 45
        assert abs((angle - target + 180) % 360 - 180) < .001
        assert all(label in event["context"] for label in ("Moonrise", "Moonset", "Sunrise", "Sunset", "Illumination"))
        for phase in [event["graphic"], *event["graphic"]["previous"], *event["graphic"]["upcoming"]]:
            phase_at = datetime.fromisoformat(phase["transition_at"])
            assert phase["bright_limb_angle"] == local_bright_limb_angle(Observer(40, -105), phase_at)
            assert phase["disk_rotation"] == local_lunar_north_angle(Observer(40, -105), phase_at)
    event = next(event for event in events if event["phase"] == "waxing_crescent")
    graphic = render_lunar_calendar(event["graphic"])
    from PIL import Image
    with Image.open(io.BytesIO(graphic)) as image:
        assert image.format == "PNG"
        assert image.size == (1200, 560)


def test_graphic_uses_local_lighting_and_rotates_surface(monkeypatch):
    from PIL import Image
    original_open = Image.open
    surface = Image.new("RGB", (148, 148), "white")
    # An asymmetric surface lets a half-turn prove texture rotation separately.
    for y in range(148):
        for x in range(74):
            surface.putpixel((x, y), (80, 80, 80))
    monkeypatch.setattr(Image, "open", lambda path: surface.copy())
    event = {"phase": "first_quarter", "index": 2, "transition_at": "2026-10-18T12:00:00+00:00",
             "illumination": 50, "bright_limb_angle": 0, "disk_rotation": 0}

    def rendered(**changes):
        return original_open(io.BytesIO(render_lunar_calendar({**event, **changes})))

    with rendered() as image:
        assert image.getpixel((620, 190))[0] > 200
        assert image.getpixel((620, 250))[0] < 15
    with rendered(bright_limb_angle=90) as image:
        assert image.getpixel((630, 230))[0] > 200
        assert image.getpixel((570, 230))[0] < 15
    with rendered(illumination=100, disk_rotation=180) as image:
        assert image.getpixel((570, 220))[0] > 200
        assert image.getpixel((630, 220))[0] < 90


@pytest.mark.asyncio
async def test_snapshot_waits_for_event_time_without_blocking(monkeypatch):
    built = []
    def build():
        built.append(True)
        return {"events": (
            {"phase": "new_moon", "transition_at": "2026-10-10T15:50:00+00:00"},
            {"phase": "waxing_crescent", "transition_at": "2026-10-14T13:14:00+00:00"},
        ), "context": "Local sky"}
    monkeypatch.setattr(calendar, "_build_snapshot", build)
    cache = calendar.CalendarEventCache()
    at = datetime(2026, 10, 14, 13, 13, 59, tzinfo=timezone.utc)
    assert cache.snapshot(at)["lunar_event"] == {}
    await cache._task
    assert built == [True]
    assert cache.snapshot(at)["lunar_event"]["phase"] == "new_moon"
    assert cache.snapshot(at.replace(minute=14, second=0))["lunar_event"]["phase"] == "waxing_crescent"
    assert len(built) == 1


def test_polar_context_reports_absent_events():
    text = calendar.sky_context(BiodynamicConfig(89, 0, "UTC"), datetime(2026, 6, 21, tzinfo=timezone.utc), 180)
    assert "Sunrise: No event on this local date" in text
    assert "Sunset: No event on this local date" in text


@pytest.mark.parametrize("value", ["", "2026-10-14", "2026-03-08T02:30", "2026-11-01T01:30", "2026-10-14T15:00+00:00"])
def test_calendar_datetime_rejects_invalid_or_ambiguous_local_times(value):
    with pytest.raises(ValueError):
        calendar.normalize_calendar_datetime(value, "America/Denver")


def test_calendar_datetime_resolves_local_timezone_and_dst():
    assert calendar.normalize_calendar_datetime("2026-10-14T15:30", "America/Denver") == "2026-10-14T15:30:00-06:00"
    assert calendar.normalize_calendar_datetime("2026-12-14T15:30", "America/Denver") == "2026-12-14T15:30:00-07:00"


def test_season_schedule_has_all_events_and_local_equilux():
    from datetime import timedelta
    from astral import Observer
    from astral.sun import sunrise, sunset
    from zoneinfo import ZoneInfo
    from sensorius.saiSeasonCalendar import season_schedule, SEASON_EVENTS
    from sensorius.saiBiodynamics import get_skyfield_runtime_if_installed
    if get_skyfield_runtime_if_installed() is None:
        pytest.skip("Local ephemeris is unavailable")
    config = BiodynamicConfig(40, -105, "America/Denver")
    events = season_schedule(2026, config)
    assert {event["season"] for event in events} == set(SEASON_EVENTS)
    imbolc = next(event for event in events if event["season"] == "imbolc")
    assert imbolc["transition_at"] == "2026-02-01T00:00:00-07:00"
    tz = ZoneInfo(config.timezone_name)
    observer = Observer(config.latitude, config.longitude)
    def daylight_error(day):
        rise = sunrise(observer, date=day, tzinfo=tz)
        setting = sunset(observer, date=day, tzinfo=tz)
        return abs((setting.astimezone(timezone.utc) - rise.astimezone(timezone.utc)).total_seconds() - 43200)
    for event in events:
        if "equilux" in event["season"]:
            at = datetime.fromisoformat(event["transition_at"])
            assert at == sunrise(observer, date=at.date(), tzinfo=tz)
            assert daylight_error(at.date()) <= daylight_error(at.date() - timedelta(days=1))
            assert daylight_error(at.date()) <= daylight_error(at.date() + timedelta(days=1))
    south = season_schedule(2026, BiodynamicConfig(-34, 151, "Australia/Sydney"))
    assert next(event for event in south if event["season"] == "winter_solstice")["transition_at"].startswith("2026-06")
    assert next(event for event in south if event["season"] == "imbolc")["transition_at"].startswith("2026-08-01")
    equator = season_schedule(2026, BiodynamicConfig(0, 0, "UTC"))
    assert not any("equilux" in event["season"] for event in equator)
