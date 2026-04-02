"""Daily summary generation for astral, biodynamic, and calendar guidance.

This module builds the persisted daily summary text shown by the Sensorius UI.
It combines Astral-derived sun and moon data, biodynamic hints, and date-window
maintenance helpers so summaries can be generated once and refreshed when needed.
"""

from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from saiBiodynamics import get_biodynamic_payload
from saiUtils import debug_enabled, printDM

try:
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun
    from astral import moon as _astral_moon
except Exception:
    LocationInfo = None
    _astral_sun = None
    _astral_moon = None

MODULE = "saiDailySummary"
DEBUG = debug_enabled(MODULE)
DEFAULT_FORECAST_DAYS = 29


def _moon_phase_name(phase_val: float) -> str:
    p = phase_val % 28.0

    def _circular_dist(a: float, b: float, cycle: float = 28.0) -> float:
        d = abs(a - b) % cycle
        return min(d, cycle - d)

    if _circular_dist(p, 0.0) <= 1.0:
        return "New Moon"
    if _circular_dist(p, 7.0) <= 1.0:
        return "1st Quarter"
    if _circular_dist(p, 14.0) <= 1.0:
        return "Full Moon"
    if _circular_dist(p, 21.0) <= 1.0:
        return "3rd Quarter"
    if 0.0 < p < 7.0:
        return "Waxing Crescent"
    if 7.0 < p < 14.0:
        return "Waxing Gibbous"
    if 14.0 < p < 21.0:
        return "Waning Gibbous"
    return "Waning Crescent"


class DailySummaryService:
    def __init__(
        self,
        *,
        settings,
        data_logger,
        supervisor=None,
        sensor_mgr=None,
        statter=None,
    ):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self.local_tz = self._resolve_tz()

    def _resolve_tz(self) -> ZoneInfo:
        tz_name = (
            self.settings.get_setting("Time", "TZ")
            or self.settings.get_setting("Time", "tz")
            or "America/Denver"
        )
        try:
            return ZoneInfo(str(tz_name))
        except Exception:
            return ZoneInfo("America/Denver")


    def _build_hint_lines(self, summary_date: date, biodynamic_day: dict | None) -> list[str]:
        lines = ["Biodynamic Hints"]
        if not isinstance(biodynamic_day, dict):
            lines.append("Suggestion: no biodynamic hint available for this day.")
            return lines

        part = str(biodynamic_day.get("dominant_plant_part") or "").strip().lower()
        sign = str(biodynamic_day.get("dominant_sign") or "--").strip()

        part_hints = {
            "root": [
                "Suggestion: favor root-zone work, transplant settling, and soil-building tasks.",
                "Consider: prep 500 for soil/root vitality when field conditions fit your program.",
            ],
            "leaf": [
                "Suggestion: favor irrigation timing, canopy recovery, and leafy-growth observations.",
                "Consider: compost-focused work or gentle moisture-balancing tasks before pushing growth.",
            ],
            "flower": [
                "Suggestion: favor blossom, herb, and aroma-focused crop work.",
                "Consider: prep 501 only when light conditions and crop stage support a light/canopy emphasis.",
            ],
            "fruit": [
                "Suggestion: favor fruiting, seed-setting, and ripening observations.",
                "Consider: prep 501 only when crop maturity, weather, and canopy condition align.",
            ],
        }

        lines.extend(part_hints.get(part, [f"Suggestion: use {sign} Moon as a planning cue rather than a rigid rule."]))
        lines.append("Suggestion: use the biodynamic window as a planning hint and let actual plant/environment conditions decide execution.")

        return lines

    def _pick_nearest_event(self, fn, target_date: date, observer, tzinfo) -> str:
        if not callable(fn):
            return ""
        candidates = []
        for offset in (-1, 0, 1):
            d = target_date + timedelta(days=offset)
            try:
                dt = fn(observer, d, tzinfo=tzinfo)
            except TypeError:
                try:
                    dt = fn(observer, date=d, tzinfo=tzinfo)
                except Exception:
                    dt = None
            except Exception:
                dt = None
            if isinstance(dt, datetime):
                candidates.append(dt)
        if not candidates:
            return ""
        center = datetime.combine(target_date, dtime.min, tzinfo)
        best = min(candidates, key=lambda item: abs((item - center).total_seconds()))
        return best.strftime("%H:%M")

    def _astral_summary_lines(self, summary_date: date) -> tuple[list[str], dict | None]:
        lines = ["Astral Notes"]
        biodynamic_day: dict | None = None
        astral_ok = (
            LocationInfo is not None
            and _astral_sun is not None
            and _astral_moon is not None
        )
        if not astral_ok:
            lines.append("Astral data unavailable.")
        else:
            try:
                resolved = self.settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
                lat = resolved.get("lat")
                lon = resolved.get("lon")
                tz_name = str(resolved.get("tz") or self.local_tz.key or "America/Denver").strip()
                if lat is None or lon is None or not tz_name:
                    lines.append("Astral location unavailable.")
                else:
                    tzinfo = ZoneInfo(tz_name)
                    loc = LocationInfo(name="Sensorius", region="", timezone=tz_name, latitude=float(lat), longitude=float(lon))
                    sun_map = _astral_sun(loc.observer, date=summary_date, tzinfo=tzinfo)
                    sunrise = sun_map.get("sunrise")
                    sunset = sun_map.get("sunset")
                    noon = sun_map.get("noon")
                    if isinstance(sunrise, datetime):
                        lines.append(f"Sunrise: {sunrise.strftime('%H:%M')}")
                    if isinstance(sunset, datetime):
                        lines.append(f"Sunset: {sunset.strftime('%H:%M')}")
                    if isinstance(noon, datetime):
                        lines.append(f"Solar Noon: {noon.strftime('%H:%M')}")

                    moon_val = float(_astral_moon.phase(summary_date))
                    moon_lit_pct = int(round((0.5 * (1 - math.cos((2 * math.pi * (moon_val % 28.0)) / 28.0))) * 100))
                    lines.append(f"Moon Phase: {_moon_phase_name(moon_val)} ({moon_lit_pct}% lit)")

                    moonrise = self._pick_nearest_event(getattr(_astral_moon, "moonrise", None), summary_date, loc.observer, tzinfo)
                    moonset = self._pick_nearest_event(getattr(_astral_moon, "moonset", None), summary_date, loc.observer, tzinfo)
                    if moonrise:
                        lines.append(f"Moonrise: {moonrise}")
                    if moonset:
                        lines.append(f"Moonset: {moonset}")
            except Exception as exc:
                lines.append(f"Astral data unavailable: {exc}")

        try:
            payload = get_biodynamic_payload(summary_date)
            if not payload.get("ok"):
                reason = str(payload.get("reason") or "unavailable")
                lines.append(f"Biodynamic: unavailable ({reason})")
                return lines, None
            biodynamic_day = next(
                (row for row in (payload.get("calendar") or []) if row and row.get("date") == summary_date.isoformat()),
                None,
            )
            if not biodynamic_day:
                lines.append("Biodynamic: unavailable for selected date.")
                return lines, None
            sign = str(biodynamic_day.get("dominant_sign") or "--")
            element = str(biodynamic_day.get("dominant_element") or "--")
            part = str(biodynamic_day.get("dominant_plant_part") or "--")
            lines.append(f"Biodynamic: {sign} Moon | {element} / {part}")
            lines.append(f"Zodiac: {sign}")
            segments = biodynamic_day.get("segments") or []
            if segments:
                first = segments[0]
                start = str(first.get("start") or "--")
                end = str(first.get("end") or "--")
                lines.append(f"Current Window: {start} to {end}")
                transitions = [f"{seg.get('start', '--')} {seg.get('sign', '--')}" for seg in segments[1:3] if isinstance(seg, dict)]
                if transitions:
                    lines.append(f"Transitions: {' | '.join(transitions)}")
        except Exception as exc:
            lines.append(f"Biodynamic: unavailable ({exc})")
        return lines, biodynamic_day

    def build_summary_text(self, summary_date: date) -> str:
        astral_lines, biodynamic_day = self._astral_summary_lines(summary_date)
        parts = [
            "\n".join(self._build_hint_lines(summary_date, biodynamic_day)),
            "\n".join(astral_lines),
        ]
        return "\n\n".join(part.strip() for part in parts if part and part.strip())

    def _summary_is_complete(self, summary_date: date, text: str) -> bool:
        body = str(text or "").strip()
        if not body:
            return False
        expected_hints_header = "Biodynamic Hints"
        return (
            expected_hints_header in body
            and ("Astral Notes" in body or "Astral" in body)
        )

    def ensure_summary_for_date(self, summary_date: date, *, force_refresh: bool = False) -> bool:
        summary_iso = summary_date.isoformat()
        if not force_refresh:
            try:
                existing = self.data_logger.get_biodynamic_daily_summary(summary_iso)
            except Exception:
                existing = ""
            if self._summary_is_complete(summary_date, existing):
                return False

        text = self.build_summary_text(summary_date)
        ok = self.data_logger.save_biodynamic_daily_summary(summary_iso, text)
        if ok and DEBUG:
            printDM(f"[daily-summary] wrote summary for {summary_iso}", location=MODULE)
        return bool(ok)

    def ensure_summaries_for_window(
        self,
        start_date: date,
        *,
        days: int = DEFAULT_FORECAST_DAYS,
        refresh_start: bool = True,
    ) -> int:
        total = max(int(days), 0)
        writes = 0
        for offset in range(total):
            target_date = start_date + timedelta(days=offset)
            if self.ensure_summary_for_date(target_date, force_refresh=bool(refresh_start and offset == 0)):
                writes += 1
        return writes

    def _feed_watchdog(self, *, error: bool = False) -> None:
        sup = getattr(self, "supervisor", None)
        if sup and hasattr(sup, "feedthedogs"):
            sup.feedthedogs("Daily Summary Writer", error=error)

    async def _sleep_with_heartbeat(self, total_sleep_s: float, heartbeat_every_s: float = 20.0) -> None:
        remaining = max(float(total_sleep_s), 0.0)
        while remaining > 0.0:
            self._feed_watchdog()
            chunk = min(heartbeat_every_s, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def run(self):
        while True:
            try:
                self._feed_watchdog()
                now_local = datetime.now(self.local_tz)
                self.ensure_summaries_for_window(now_local.date())
                next_run = datetime.combine(now_local.date() + timedelta(days=1), dtime(0, 0, 1), self.local_tz)
                sleep_s = max((next_run - now_local).total_seconds(), 1.0)
            except Exception as exc:
                self._feed_watchdog(error=True)
                printDM(f"[daily-summary] loop error: {exc}", location=MODULE)
                sleep_s = 60.0
            await self._sleep_with_heartbeat(sleep_s)
