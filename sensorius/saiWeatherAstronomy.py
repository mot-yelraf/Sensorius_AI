import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.moon import azimuth as moon_azimuth
from astral.moon import elevation as moon_elevation
from astral.moon import phase
from astral.sun import azimuth as sun_azimuth
from astral.sun import elevation as sun_elevation
from astral.sun import sun

PHASES = (
    ("New moon", "🌑"),
    ("Waxing crescent", "🌒"),
    ("First quarter", "🌓"),
    ("Waxing gibbous", "🌔"),
    ("Full moon", "🌕"),
    ("Waning gibbous", "🌖"),
    ("Last quarter", "🌗"),
    ("Waning crescent", "🌘"),
)
PHASE_AGES = (0.0, 3.5, 7.0, 10.5, 14.0, 17.5, 21.0, 24.5)
FULL_MOON_NAMES = {
    1: "Wolf Moon",
    2: "Snow Moon",
    3: "Worm Moon",
    4: "Pink Moon",
    5: "Flower Moon",
    6: "Strawberry Moon",
    7: "Buck Moon",
    8: "Sturgeon Moon",
    9: "Harvest Moon",
    10: "Hunter's Moon",
    11: "Beaver Moon",
    12: "Cold Moon",
}


def _horizontal_vector(azimuth_degrees: float, elevation_degrees: float) -> tuple[float, float, float]:
    """Return an east, north, up unit vector for horizontal coordinates."""
    azimuth = math.radians(azimuth_degrees)
    elevation = math.radians(elevation_degrees)
    return (
        math.cos(elevation) * math.sin(azimuth),
        math.cos(elevation) * math.cos(azimuth),
        math.sin(elevation),
    )


def _moon_time(at: datetime) -> datetime:
    """Normalize to UTC because Astral's lunar position ignores tzinfo offsets."""
    if at.tzinfo is None:
        return at
    return at.astimezone(timezone.utc).replace(tzinfo=None)


def _moon_azimuth(observer: Any, at: datetime) -> float:
    return float(moon_azimuth(observer, _moon_time(at)))


def _moon_elevation(observer: Any, at: datetime) -> float:
    return float(moon_elevation(observer, _moon_time(at)))


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length < 1e-10:
        raise ValueError("local sky direction is undefined")
    return tuple(component / length for component in vector)  # type: ignore[return-value]


def _cross(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _local_screen_basis(
    observer: Any, at: datetime
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    moon_vector = _horizontal_vector(_moon_azimuth(observer, at), _moon_elevation(observer, at))
    zenith = (0.0, 0.0, 1.0)
    moon_up_dot = _dot(zenith, moon_vector)
    projected_up = tuple(
        zenith[index] - moon_up_dot * moon_vector[index] for index in range(3)
    )
    try:
        screen_up = _normalize(projected_up)  # toward the observer's zenith
    except ValueError:
        north = (0.0, 1.0, 0.0)
        north_dot = _dot(north, moon_vector)
        screen_up = _normalize(
            tuple(north[index] - north_dot * moon_vector[index] for index in range(3))
        )
    screen_right = _normalize(_cross(moon_vector, screen_up))
    return moon_vector, screen_up, screen_right


def local_bright_limb_angle(observer: Any, at: datetime) -> float:
    """Return the bright-limb direction clockwise from the observer's zenith."""
    moon_vector, screen_up, screen_right = _local_screen_basis(observer, at)
    sun_vector = _horizontal_vector(sun_azimuth(observer, at), sun_elevation(observer, at))

    sun_moon_dot = _dot(sun_vector, moon_vector)
    sun_tangent = _normalize(
        tuple(sun_vector[index] - sun_moon_dot * moon_vector[index] for index in range(3))
    )
    clockwise_from_up = math.degrees(
        math.atan2(_dot(sun_tangent, screen_right), _dot(sun_tangent, screen_up))
    )
    return round(clockwise_from_up % 360.0, 2)


def local_lunar_north_angle(observer: Any, at: datetime) -> float:
    """Return lunar north's approximate local-sky rotation from the zenith."""
    moon_vector, screen_up, screen_right = _local_screen_basis(observer, at)
    latitude = math.radians(float(observer.latitude))
    north_celestial_pole = (0.0, math.cos(latitude), math.sin(latitude))
    pole_moon_dot = _dot(north_celestial_pole, moon_vector)
    projected_pole = _normalize(
        tuple(
            north_celestial_pole[index] - pole_moon_dot * moon_vector[index]
            for index in range(3)
        )
    )
    clockwise_from_up = math.degrees(
        math.atan2(_dot(projected_pole, screen_right), _dot(projected_pole, screen_up))
    )
    return round(clockwise_from_up % 360.0, 2)


def _best_local_view(observer: Any, local_date: Any, tzinfo: ZoneInfo) -> datetime:
    """Choose the hourly instant when the Moon is highest on the local date."""
    midnight = datetime.combine(local_date, datetime.min.time(), tzinfo=tzinfo)
    candidates = (midnight + timedelta(hours=hour) for hour in range(24))
    return max(candidates, key=lambda candidate: _moon_elevation(observer, candidate))


def _nearest_phase_date(estimated_date: Any, target_age: float) -> Any:
    """Refine an approximate phase date against Astral's non-uniform cycle."""
    candidates = (estimated_date + timedelta(days=offset) for offset in range(-4, 5))

    def phase_distance(candidate: Any) -> float:
        difference = abs(float(phase(candidate)) - target_age)
        return min(difference, 28.0 - difference)

    return min(candidates, key=phase_distance)


def _phase_name(index: int, phase_date: Any) -> str:
    """Return a familiar display name, including the traditional full-moon name."""
    if index == 4:
        return FULL_MOON_NAMES.get(getattr(phase_date, "month", None), "Full Moon")
    return PHASES[index][0]


def _phase_event(
    observer: Any,
    tzinfo: ZoneInfo,
    phase_date: Any,
    index: int,
) -> dict[str, Any]:
    """Build one phase milestone as it appears from the observer's location."""
    target_age = PHASE_AGES[index]
    view_at = _best_local_view(observer, phase_date, tzinfo)
    return {
        "name": _phase_name(index, phase_date),
        "glyph": PHASES[index][1],
        "index": index,
        "illumination": round((1 - math.cos(2 * math.pi * target_age / 28.0)) * 50),
        "bright_limb_angle": local_bright_limb_angle(observer, view_at),
        "disk_rotation": local_lunar_north_angle(observer, view_at),
        "altitude": round(_moon_elevation(observer, view_at), 1),
        "representative_date": phase_date.isoformat(),
        "date_label": f"{phase_date.strftime('%b')} {phase_date.day}",
    }


def local_phase_timeline(
    observer: Any, tzinfo: ZoneInfo, observed_at: datetime, phase_day: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the four previous and four upcoming local phase milestones."""
    local_date = observed_at.astimezone(tzinfo).date()
    candidates: set[tuple[Any, int]] = set()
    for cycle_offset in range(-2, 3):
        for index, target_age in enumerate(PHASE_AGES):
            days_from_now = target_age + (28.0 * cycle_offset) - phase_day
            estimated_date = (observed_at.astimezone(tzinfo) + timedelta(days=days_from_now)).date()
            phase_date = _nearest_phase_date(estimated_date, target_age)
            candidates.add((phase_date, index))

    ordered = sorted(candidates)
    previous_dates = [item for item in ordered if item[0] < local_date][-4:]
    upcoming_dates = [item for item in ordered if item[0] > local_date][:4]
    previous = [_phase_event(observer, tzinfo, phase_date, index) for phase_date, index in previous_dates]
    upcoming = [_phase_event(observer, tzinfo, phase_date, index) for phase_date, index in upcoming_dates]
    return previous, upcoming


def local_phase_cycle(
    observer: Any, tzinfo: ZoneInfo, observed_at: datetime, phase_day: float
) -> list[dict[str, Any]]:
    """Build observer-local snapshots for the eight phases around this lunation."""
    local_now = observed_at.astimezone(tzinfo)
    current_index = int((phase_day / 28.0) * 8 + 0.5) % 8
    cycle: list[dict[str, Any]] = []
    for index, ((name, glyph), target_age) in enumerate(zip(PHASES, PHASE_AGES)):
        estimated_date = (local_now + timedelta(days=target_age - phase_day)).date()
        representative_date = _nearest_phase_date(estimated_date, target_age)
        view_at = _best_local_view(observer, representative_date, tzinfo)
        illumination = round((1 - math.cos(2 * math.pi * target_age / 28.0)) * 50)
        cycle.append(
            {
                "name": name,
                "glyph": glyph,
                "active": index == current_index,
                "index": index,
                "illumination": illumination,
                "bright_limb_angle": local_bright_limb_angle(observer, view_at),
                "disk_rotation": local_lunar_north_angle(observer, view_at),
                "altitude": round(_moon_elevation(observer, view_at), 1),
                "representative_date": view_at.date().isoformat(),
            }
        )
    return cycle


def moon_phase_context(at: datetime | None = None) -> dict[str, Any]:
    """Return Astral's current lunar phase as a display-ready cycle."""
    observed_at = at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    phase_day = float(phase(observed_at.date()))
    position = phase_day / 28.0
    phase_index = int(position * 8 + 0.5) % 8
    illumination = round((1 - math.cos(2 * math.pi * position)) * 50)
    days_to_full = (14.0 - phase_day) % 28.0
    cycle = [
        {"name": name, "glyph": glyph, "active": index == phase_index, "index": index}
        for index, (name, glyph) in enumerate(PHASES)
    ]
    name, glyph = PHASES[phase_index]
    return {
        "name": name,
        "glyph": glyph,
        "illumination": illumination,
        "age_days": round(phase_day, 1),
        "days_to_full": round(days_to_full, 1),
        "phase_index": phase_index,
        "cycle": cycle,
        "updated_at": observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _clock_label(at: datetime) -> str:
    """Format a solar event time consistently across supported platforms."""
    hour = at.hour % 12 or 12
    suffix = "AM" if at.hour < 12 else "PM"
    return f"{hour}:{at.minute:02d} {suffix}"


def _polar_daylight_duration(local_date: Any, latitude: float) -> str:
    """Return the all-day or all-night sunlight duration at a geographic pole."""
    observer = LocationInfo("Pole", "", "UTC", latitude, 0.0).observer
    reference = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    minutes = 1440 if sun_elevation(observer, reference) > -0.833 else 0
    return f"{minutes // 60}h {minutes % 60:02d}m"


@lru_cache(maxsize=48)
def _next_season_event_for_hour(observed_hour_iso: str, timezone_name: str) -> dict[str, str]:
    """Return the next equinox or solstice in the requested local timezone."""
    from skyfield import almanac

    from .saiBiodynamics import get_skyfield_runtime_if_installed

    runtime = get_skyfield_runtime_if_installed()
    if runtime is None:
        return {"label": "Seasonal event unavailable", "date": "—", "at": ""}
    _loader, ts, eph, _constellation_at = runtime
    observed_hour = datetime.fromisoformat(observed_hour_iso)
    start = ts.from_datetime(observed_hour.astimezone(timezone.utc))
    end = ts.from_datetime((observed_hour + timedelta(days=370)).astimezone(timezone.utc))
    event_times, event_types = almanac.find_discrete(start, end, almanac.seasons(eph))
    if not len(event_times):
        return {"label": "Seasonal event unavailable", "date": "—", "at": ""}
    event_local = event_times[0].utc_datetime().astimezone(ZoneInfo(timezone_name))
    return {
        "label": str(almanac.SEASON_EVENTS_NEUTRAL[int(event_types[0])]),
        "date": f"{event_local.strftime('%b')} {event_local.day}, {event_local.year}",
        "at": event_local.isoformat(),
    }


def _next_season_event(observed_at: datetime, tzinfo: ZoneInfo) -> dict[str, str]:
    observed_hour = observed_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    try:
        return _next_season_event_for_hour(observed_hour.isoformat(), tzinfo.key)
    except Exception:
        return {"label": "Seasonal event unavailable", "date": "—", "at": ""}


def astronomy_context(settings: Any, at: datetime | None = None) -> dict[str, Any]:
    """Combine Astral moon phase and local sunrise/sunset for settings location."""
    observed_at = at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    result = moon_phase_context(observed_at)
    try:
        timezone_name = str(settings.timezone or "UTC")
        tzinfo = ZoneInfo(timezone_name)
        local = observed_at.astimezone(tzinfo)
        location = LocationInfo(
            str(settings.location_name or "Caelus station"),
            "",
            timezone_name,
            float(settings.latitude),
            float(settings.longitude),
        )
        solar = sun(location.observer, date=local.date(), tzinfo=tzinfo)
        effective_sunset = solar["sunset"]
        if effective_sunset <= solar["sunrise"]:
            effective_sunset += timedelta(days=1)
        daylight_seconds = (effective_sunset - solar["sunrise"]).total_seconds()
        daylight_minutes = round(daylight_seconds / 60)
        next_season = _next_season_event(observed_at, tzinfo)
        if local <= solar["sunrise"]:
            daylight_progress = 0
        elif local >= effective_sunset:
            daylight_progress = 100
        else:
            daylight_progress = round((local - solar["sunrise"]).total_seconds() / daylight_seconds * 100)
        result.update(
            sunrise=solar["sunrise"].strftime("%H:%M"),
            sunset=solar["sunset"].strftime("%H:%M"),
            solar_noon=solar["noon"].strftime("%H:%M"),
            sunrise_display=_clock_label(solar["sunrise"]),
            sunset_display=_clock_label(solar["sunset"]),
            solar_noon_display=_clock_label(solar["noon"]),
            daylight_hours=round(daylight_seconds / 3600, 2),
            daylight_duration=f"{daylight_minutes // 60}h {daylight_minutes % 60:02d}m",
            north_pole_daylight=_polar_daylight_duration(observed_at.date(), 90.0),
            south_pole_daylight=_polar_daylight_duration(observed_at.date(), -90.0),
            next_season_label=next_season["label"],
            next_season_date=next_season["date"],
            next_season_at=next_season["at"],
            daylight_progress=daylight_progress,
            sun_is_up=solar["sunrise"] <= local < effective_sunset,
            moon_altitude=round(_moon_elevation(location.observer, observed_at), 1),
            bright_limb_angle=local_bright_limb_angle(location.observer, observed_at),
            disk_rotation=local_lunar_north_angle(location.observer, observed_at),
            cycle=local_phase_cycle(location.observer, tzinfo, observed_at, result["age_days"]),
            timezone=timezone_name,
        )
        previous_phases, upcoming_phases = local_phase_timeline(
            location.observer, tzinfo, observed_at, result["age_days"]
        )
        result.update(
            previous_phases=previous_phases,
            upcoming_phases=upcoming_phases,
        )
        if result["phase_index"] == 4:
            result["name"] = _phase_name(4, local.date())
    except Exception:
        result.update(
            sunrise="—",
            sunset="—",
            solar_noon="—",
            sunrise_display="—",
            sunset_display="—",
            solar_noon_display="—",
            daylight_hours=None,
            daylight_duration="—",
            north_pole_daylight="—",
            south_pole_daylight="—",
            next_season_label="Seasonal event unavailable",
            next_season_date="—",
            next_season_at="",
            daylight_progress=0,
            sun_is_up=False,
            moon_altitude=None,
            bright_limb_angle=0.0,
            disk_rotation=0.0,
            previous_phases=[],
            upcoming_phases=[],
            timezone=str(getattr(settings, "timezone", "UTC") or "UTC"),
        )
    return result
