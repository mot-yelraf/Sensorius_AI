"""Cache timed lunar milestones and descriptive local sky context.

Calendar automations read a small process-wide snapshot. Ephemeris searches and
location resolution run in a worker so switch monitors never build a schedule.
"""

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .saiUtils import printDM

LUNAR_PHASES = (
    "new_moon", "waxing_crescent", "first_quarter", "waxing_gibbous",
    "full_moon", "waning_gibbous", "last_quarter", "waning_crescent",
)
PHASE_DESCRIPTIONS = (
    "The Moon is near the Sun in the sky; its Earth-facing side is dark.",
    "A growing crescent is illuminated as the Moon waxes toward first quarter.",
    "Half of the lunar disk is illuminated; the Moon is waxing toward full.",
    "More than half of the disk is illuminated and growing toward full moon.",
    "The Earth-facing disk is fully illuminated, opposite the Sun in the sky.",
    "More than half of the disk remains illuminated, decreasing after full moon.",
    "Half of the disk is illuminated; the Moon is waning toward new moon.",
    "A shrinking crescent remains illuminated before the next new moon.",
)


def normalize_calendar_datetime(value: str, timezone_name: str) -> str:
    """Resolve an editor wall time and reject ambiguous or nonexistent DST times."""
    raw = str(value or "")
    if "T" not in raw or len(raw) < 16:
        raise ValueError("Select both a Calendar date and time.")
    local = datetime.fromisoformat(raw)
    if local.tzinfo is not None:
        raise ValueError("Select a local date/time without an explicit UTC offset.")
    tz = ZoneInfo(timezone_name)
    candidates = {}
    for fold in (0, 1):
        aware = local.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == local:
            candidates[utc] = aware
    if len(candidates) != 1:
        raise ValueError("Selected time is ambiguous or nonexistent due to daylight saving; choose another time.")
    return next(iter(candidates.values())).isoformat()


def sky_context(config, at: datetime, phase_degrees: float) -> str:
    """Describe the Moon and the event day's local solar and lunar times."""
    from astral import Observer
    from astral.moon import moonrise, moonset
    from astral.sun import sunrise, sunset

    tz = ZoneInfo(config.timezone_name)
    local = at.astimezone(tz)
    observer = Observer(config.latitude, config.longitude)
    index = int((phase_degrees % 360) / 45) % 8
    illumination = (1 - math.cos(math.radians(phase_degrees))) * 50
    lines = [
        f"{LUNAR_PHASES[index].replace('_', ' ').title()}: {PHASE_DESCRIPTIONS[index]}",
        f"Illumination {illumination:.1f}%; location {config.latitude:.4f}, "
        f"{config.longitude:.4f}; {config.timezone_name}; {local:%b %d, %Y}.",
    ]
    for label, event_fn in (("Moonrise", moonrise), ("Moonset", moonset),
                            ("Sunrise", sunrise), ("Sunset", sunset)):
        try:
            event = event_fn(observer, date=local.date(), tzinfo=tz)
            value = event.strftime("%I:%M %p %Z (UTC%z)") if event else "No event on this local date"
        except ValueError:
            value = "No event on this local date"
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


@lru_cache(maxsize=2)
def _phase_schedule(day_iso: str, config) -> tuple[dict, ...]:
    from astral import Observer
    from skyfield import almanac
    from .saiBiodynamics import get_skyfield_runtime_if_installed
    from .saiWeatherAstronomy import local_bright_limb_angle, local_lunar_north_angle

    runtime = get_skyfield_runtime_if_installed()
    if runtime is None:
        raise RuntimeError("Local lunar ephemeris is unavailable; enable the Biodynamic Calendar data first")
    _, ts, eph, _ = runtime
    local = datetime.fromisoformat(day_iso).replace(tzinfo=ZoneInfo(config.timezone_name))
    observer = Observer(config.latitude, config.longitude)

    def phase_index(t):
        return (almanac.moon_phase(eph, t).degrees / 45).astype(int) % 8

    phase_index.step_days = 1.0
    times, indices = almanac.find_discrete(
        ts.from_datetime(local - timedelta(days=20)),
        ts.from_datetime(local + timedelta(days=20)), phase_index,
    )
    events = []
    for t, index in zip(times, indices):
        index = int(index)
        at = t.utc_datetime().astimezone(local.tzinfo)
        events.append({
            "transition_at": at.isoformat(), "phase": LUNAR_PHASES[index], "index": index,
            "event_key": f"{LUNAR_PHASES[index]}:{at.astimezone(timezone.utc).date().isoformat()}",
            "description": PHASE_DESCRIPTIONS[index],
            "context": sky_context(config, at, index * 45.0),
            "illumination": (1 - math.cos(index * math.pi / 4)) * 50,
            "bright_limb_angle": local_bright_limb_angle(observer, at),
            "disk_rotation": local_lunar_north_angle(observer, at),
        })
    graphic_keys = ("phase", "index", "transition_at", "illumination",
                    "bright_limb_angle", "disk_rotation")
    for position, event in enumerate(events):
        event["graphic"] = {
            **{key: event[key] for key in graphic_keys}, "context": event["context"],
            "previous": [{key: item[key] for key in graphic_keys}
                         for item in events[max(0, position - 4):position]],
            "upcoming": [{key: item[key] for key in graphic_keys}
                         for item in events[position + 1:position + 5]],
        }
    return tuple(events)


def _build_snapshot() -> dict:
    from .saiBiodynamicCalendarApp import get_registered_biodynamic_calendar_service
    from .saiBiodynamics import get_skyfield_runtime_if_installed
    from skyfield import almanac

    service = get_registered_biodynamic_calendar_service()
    if service is None:
        return {}
    config, _ = service.location()
    if config is None:
        return {}
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(config.timezone_name))
    result = {}
    try:
        result["events"] = _phase_schedule(local.date().isoformat(), config)
        runtime = get_skyfield_runtime_if_installed()
        if runtime is not None:
            _, ts, eph, _ = runtime
            degrees = float(almanac.moon_phase(eph, ts.from_datetime(now)).degrees)
            result["context"] = sky_context(config, now, degrees)
    except Exception as exc:
        printDM(f"[calendar automation] lunar events unavailable: {exc}", location="saiCalendarAutomation", level="warning")
    try:
        from .saiSeasonCalendar import season_schedule

        result["season_events"] = season_schedule(local.year - 1, config) + season_schedule(local.year, config)
    except Exception as exc:
        printDM(f"[calendar automation] seasons unavailable: {exc}", location="saiCalendarAutomation", level="warning")
    return result


class CalendarEventCache:
    """Refresh shared calendar context outside the automation evaluation loop."""

    def __init__(self):
        self._snapshot = {}
        self._task = None
        self._refresh_at = 0.0

    async def _refresh(self):
        try:
            self._snapshot = await asyncio.to_thread(_build_snapshot)
        except Exception as exc:
            self._snapshot = {}
            printDM(f"[calendar automation] {exc}", location="saiCalendarAutomation", level="warning")
        finally:
            self._refresh_at = time.monotonic() + 60.0

    def snapshot(self, at: datetime | None = None) -> dict:
        """Return the latest due phase and sky context without blocking."""
        if time.monotonic() >= self._refresh_at and (self._task is None or self._task.done()):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return {}
            self._task = loop.create_task(self._refresh())
        now = at or datetime.now(timezone.utc)
        events = self._snapshot.get("events", ())
        latest = next((event for event in reversed(events)
                       if datetime.fromisoformat(event["transition_at"]) <= now), {})
        seasons = self._snapshot.get("season_events", ())
        latest_season = next((event for event in reversed(seasons)
                              if datetime.fromisoformat(event["transition_at"]) <= now), {})
        return {"lunar_event": dict(latest), "season_event": dict(latest_season),
                "context": self._snapshot.get("context", "")}


calendar_events = CalendarEventCache()
