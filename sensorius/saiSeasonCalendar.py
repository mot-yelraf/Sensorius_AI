"""Calculate local Wheel of the Year celebrations and equilux dates.

Solar turning points use the installed ephemeris. Traditional cross-quarter
celebrations begin at local midnight; equilux observes the closest local day to
twelve hours of sunrise-to-sunset daylight, at that day's sunrise.
"""

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

SEASON_EVENTS = {
    "imbolc": ("Imbolc", "A celebration of returning light and the first signs of spring."),
    "spring_equinox": ("Spring Equinox / Ostara", "The spring turning point, celebrating balance and new growth."),
    "spring_equilux": ("Spring Equilux", "The local spring date with daylight closest to twelve hours."),
    "beltane": ("Beltane", "A celebration of flowering, vitality, and the approach of summer."),
    "summer_solstice": ("Summer Solstice / Litha", "The summer turning point and the year's longest daylight."),
    "lughnasadh": ("Lughnasadh / Lammas", "The first harvest celebration, associated with grain and gratitude."),
    "autumn_equilux": ("Autumn Equilux", "The local autumn date with daylight closest to twelve hours."),
    "autumn_equinox": ("Autumn Equinox / Mabon", "The autumn turning point, celebrating balance and harvest."),
    "samhain": ("Samhain", "A celebration of the final harvest, ancestors, and the year's darker half."),
    "winter_solstice": ("Winter Solstice / Yule", "The winter turning point and the return of lengthening daylight."),
}


def _event(key: str, at: datetime, config, detail: str = "") -> dict:
    label, description = SEASON_EVENTS[key]
    return {
        "season": key, "name": label, "transition_at": at.isoformat(),
        "event_key": f"{key}:{at.astimezone(timezone.utc).date().isoformat()}",
        "description": description,
        "context": f"{description}\n{detail}\n{config.timezone_name}; "
                   f"location {config.latitude:.4f}, {config.longitude:.4f}.",
    }


def _equilux_events(year: int, config) -> list[dict]:
    from astral import Observer
    from astral.sun import sunrise, sunset

    tz = ZoneInfo(config.timezone_name)
    observer = Observer(config.latitude, config.longitude)
    previous = None
    events = []
    day = date(year, 1, 1)
    while day.year == year:
        try:
            rise = sunrise(observer, date=day, tzinfo=tz)
            setting = sunset(observer, date=day, tzinfo=tz)
            seconds = (setting.astimezone(timezone.utc) - rise.astimezone(timezone.utc)).total_seconds()
            if seconds <= 0:
                seconds += 86400
        except ValueError:
            previous = None
            day += timedelta(days=1)
            continue
        current = (seconds - 43200, rise, setting, seconds)
        if previous and previous[0] * current[0] <= 0 and previous[0] != current[0]:
            best = min((previous, current), key=lambda item: abs(item[0]))
            key = "spring_equilux" if current[0] > previous[0] else "autumn_equilux"
            # Extremely low latitudes can have no twelve-hour crossing at all.
            if not any(event["season"] == key for event in events):
                detail = (f"Sunrise {best[1]:%I:%M %p %Z}; sunset {best[2]:%I:%M %p %Z}; "
                          f"daylight {int(best[3] // 3600)}h {int(best[3] % 3600 // 60)}m. "
                          "Observed at sunrise on the closest local date to twelve hours of daylight.")
                events.append(_event(key, best[1], config, detail))
        previous = current
        day += timedelta(days=1)
    return events


def season_schedule(year: int, config) -> tuple[dict, ...]:
    """Return hemisphere-aware seasonal instants for one local calendar year."""
    from .saiBiodynamics import get_skyfield_runtime_if_installed

    # Retry the full schedule if ephemeris data becomes available after startup.
    return _season_schedule(year, config, get_skyfield_runtime_if_installed() is not None)


@lru_cache(maxsize=6)
def _season_schedule(year: int, config, ephemeris_available: bool) -> tuple[dict, ...]:
    from skyfield import almanac
    from .saiBiodynamics import get_skyfield_runtime_if_installed

    tz = ZoneInfo(config.timezone_name)
    south = config.latitude < 0
    fixed = (("imbolc", 2, 1), ("beltane", 5, 1), ("lughnasadh", 8, 1), ("samhain", 10, 31))
    events = []
    for key, month, day in fixed:
        # Seasonal names follow the local hemisphere, shifting the fire festivals
        # by six months while keeping each traditional month/day convention.
        if south:
            month = (month + 5) % 12 + 1
            if key == "samhain":
                day = 30
        at = datetime(year, month, day, tzinfo=tz)
        events.append(_event(key, at, config, "Traditional calendar observance at local midnight."))
    events.extend(_equilux_events(year, config))
    runtime = get_skyfield_runtime_if_installed() if ephemeris_available else None
    if runtime is not None:
        _, ts, eph, _ = runtime
        start = datetime(year, 1, 1, tzinfo=tz)
        end = datetime(year + 1, 1, 1, tzinfo=tz)
        times, indices = almanac.find_discrete(ts.from_datetime(start), ts.from_datetime(end), almanac.seasons(eph))
        north_keys = ("spring_equinox", "summer_solstice", "autumn_equinox", "winter_solstice")
        for t, index in zip(times, indices):
            key = north_keys[(int(index) + (2 if south else 0)) % 4]
            events.append(_event(key, t.utc_datetime().astimezone(tz), config, "Calculated solar turning point; no offset."))
    else:
        from .saiUtils import printDM

        printDM("Season solstice/equinox events require local calendar ephemeris data.",
                location="saiSeasonCalendar", level="warning")
    return tuple(sorted(events, key=lambda event: datetime.fromisoformat(event["transition_at"])))
