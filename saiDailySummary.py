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
DEFAULT_FORECAST_DAYS = 366
_GROUNDING_REMINDER = "Suggestion: prioritize actual plant health, irrigation status, weather, and disease pressure over calendar timing."
_KNOWN_CROP_STAGES = {"seedling", "veg", "maturing", "mature", "harvest"}


def _month_start(anchor_date: date) -> date:
    return anchor_date.replace(day=1)


def _next_month_start(anchor_date: date) -> date:
    month_start = _month_start(anchor_date)
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1, day=1)
    return month_start.replace(month=month_start.month + 1, day=1)


def _normalize_crop_stage(crop_stage: str | None) -> str:
    stage = str(crop_stage or "").strip().lower()
    if stage in _KNOWN_CROP_STAGES:
        return stage
    return "general"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _safe_float(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except Exception:
        return None


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


    def _build_hint_lines(
        self,
        summary_date: date,
        biodynamic_day: dict | None,
        crop_stage: str | None = None,
        plant_state: dict | None = None,
    ) -> list[str]:
        lines = ["Biodynamic Hints"]
        if not isinstance(biodynamic_day, dict):
            lines.append("Suggestion: no biodynamic hint available for this day.")
            return lines

        part = str(biodynamic_day.get("dominant_plant_part") or "").strip().lower()
        sign = str(biodynamic_day.get("dominant_sign") or "--").strip()
        stage = _normalize_crop_stage(crop_stage)

        part_hints = {
            "root": {
                "seedling": [
                    "Suggestion: favorable for transplanting, root establishment, and reducing transplant shock.",
                    "Consider: focus on root-zone biology, gentle watering, and soil contact.",
                    "Suggestion: avoid aggressive top pruning or forcing rapid canopy growth.",
                ],
                "veg": [
                    "Suggestion: favor root expansion, soil-building, and steady structural growth.",
                    "Consider: focus on compost, mulch, and moisture consistency.",
                    "Consider: prep 500 when soil conditions fit the grow program.",
                ],
                "maturing": [
                    "Suggestion: favor root strength and stress resilience over rapid top growth.",
                    "Consider: focus on balanced moisture and mineral availability.",
                ],
                "mature": [
                    "Suggestion: suitable for root crop harvest intended for storage.",
                    "Consider: focus on density, curing, and keeping quality.",
                ],
                "harvest": [
                    "Suggestion: favorable for harvesting root crops intended for curing or long-term storage.",
                ],
                "general": [
                    "Suggestion: favor root-zone work, transplant settling, and soil-building tasks.",
                    "Consider: prep 500 for soil/root vitality when field conditions fit the program.",
                ],
            },
            "leaf": {
                "seedling": [
                    "Suggestion: favorable for early vegetative push and canopy recovery.",
                    "Consider: focus on gentle irrigation and avoiding drought stress.",
                ],
                "veg": [
                    "Suggestion: favor leafy growth, irrigation timing, and canopy development.",
                    "Consider: focus on nitrogen availability, compost teas, and moisture balance.",
                    "Suggestion: avoid overwatering sensitive fruiting crops.",
                ],
                "maturing": [
                    "Suggestion: monitor excessive vegetative growth that may reduce flowering or fruit set.",
                    "Consider: focus on airflow and disease prevention.",
                ],
                "mature": [
                    "Suggestion: suitable for harvesting leafy greens intended for immediate freshness.",
                ],
                "harvest": [
                    "Suggestion: harvest leafy crops for fresh-market quality rather than long storage.",
                ],
                "general": [
                    "Suggestion: favor irrigation timing, canopy recovery, and leafy-growth observations.",
                    "Consider: compost-focused work or gentle moisture-balancing tasks before pushing growth.",
                ],
            },
            "flower": {
                "seedling": [
                    "Suggestion: avoid stressing young plants approaching flower initiation.",
                    "Consider: focus on balanced growth and stable environmental conditions.",
                ],
                "veg": [
                    "Suggestion: suitable for training and preparing plants for reproductive growth.",
                    "Consider: focus on airflow, light penetration, and structural balance.",
                ],
                "maturing": [
                    "Suggestion: favor blossom development, pollination support, and aromatic crops.",
                    "Consider: focus on terpene, aroma, and flower quality.",
                    "Consider: prep 501 only when light conditions and crop vigor support it.",
                ],
                "mature": [
                    "Suggestion: favorable for flower harvest, medicinal herbs, and aromatic crops.",
                ],
                "harvest": [
                    "Suggestion: harvest flowers and herbs for aroma, oils, and drying quality.",
                ],
                "general": [
                    "Suggestion: favor blossom, herb, and aroma-focused crop work.",
                    "Consider: prep 501 only when light conditions and crop stage support a light/canopy emphasis.",
                ],
            },
            "fruit": {
                "seedling": [
                    "Suggestion: avoid overpushing immature fruiting plants.",
                    "Consider: focus on steady structural growth before heavy reproductive demand.",
                ],
                "veg": [
                    "Suggestion: suitable for training and preparing fruiting structure.",
                    "Consider: focus on branch strength and balanced canopy/light penetration.",
                ],
                "maturing": [
                    "Suggestion: favor fruiting, seed-setting, and ripening observations.",
                    "Consider: focus on flavor, resin, sugar development, and reproductive energy.",
                    "Consider: prep 501 when canopy health, maturity, and weather align.",
                ],
                "mature": [
                    "Suggestion: favorable for harvesting fruits and seed crops intended for flavor and vitality.",
                ],
                "harvest": [
                    "Suggestion: harvest fruits and seeds for flavor, ripeness, and seed-saving quality.",
                ],
                "general": [
                    "Suggestion: favor fruiting, seed-setting, and ripening observations.",
                    "Consider: prep 501 only when crop maturity, weather, and canopy condition align.",
                ],
            },
        }

        if part in part_hints:
            lines.extend(part_hints[part].get(stage) or part_hints[part]["general"])
        else:
            lines.append(f"Suggestion: use {sign} Moon as a planning cue rather than a rigid rule.")

        moon_direction = str(biodynamic_day.get("moon_direction") or "").strip().lower()
        if moon_direction == "descending":
            lines.append("Timing: descending Moon is traditionally associated with transplanting, pruning, compost, soil work, and root activity.")
        elif moon_direction == "ascending":
            lines.append("Timing: ascending Moon is traditionally associated with above-ground vitality, grafting, gathering herbs, fruit harvest, and foliar emphasis.")

        if _truthy(biodynamic_day.get("lunar_node")):
            lines.append("Caution: biodynamic calendars often treat lunar nodes as rest or avoidance windows for sowing, transplanting, or major crop work.")
        if _truthy(biodynamic_day.get("perigee")):
            lines.append("Caution: perigee is often treated as a sensitive period; avoid unnecessary plant stress if conditions are already marginal.")
        if _truthy(biodynamic_day.get("apogee")):
            lines.append("Observation: apogee may be treated as a lighter-touch planning window; keep plant condition and weather as the deciding factors.")

        state = plant_state if isinstance(plant_state, dict) else {}
        if _truthy(state.get("stress")):
            lines.append("Plant Condition: visible or measured stress should override biodynamic timing; delay pruning, transplanting, or foliar work if possible.")
        vpd = (
            _safe_float(state.get("vpd"))
            or _safe_float(state.get("plant_vpd"))
            or _safe_float(state.get("Plant VPD"))
            or _safe_float(state.get("ambient_vpd"))
            or _safe_float(state.get("Ambient VPD"))
        )
        if vpd is not None and vpd > 1.8:
            lines.append("Plant Condition: elevated VPD suggests reducing stress before aggressive pruning, transplanting, or spraying.")

        lines.append(_GROUNDING_REMINDER)

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
                    observer = loc.observer
                    try:
                        observer.elevation = float(resolved.get("altitude") or 0.0)
                    except Exception:
                        observer.elevation = 0.0
                    sun_map = _astral_sun(observer, date=summary_date, tzinfo=tzinfo)
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

    def build_summary_text(self, summary_date: date, *, crop_stage: str | None = None, plant_state: dict | None = None) -> str:
        astral_lines, biodynamic_day = self._astral_summary_lines(summary_date)
        parts = [
            "\n".join(self._build_hint_lines(summary_date, biodynamic_day, crop_stage=crop_stage, plant_state=plant_state)),
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
            and (_GROUNDING_REMINDER in body or "Suggestion: no biodynamic hint available for this day." in body)
            and ("Astral Notes" in body or "Astral" in body)
        )

    @staticmethod
    def summary_needs_location_repair(text: str) -> bool:
        """Return whether a persisted summary contains location-related fallback output."""
        body = str(text or "")
        return any(
            marker in body
            for marker in (
                "Suggestion: no biodynamic hint available for this day.",
                "Astral location unavailable.",
                "Biodynamic: unavailable (location_unavailable)",
            )
        )

    def _location_is_resolved(self) -> bool:
        try:
            resolved = self.settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5) or {}
        except Exception:
            return False
        return resolved.get("lat") is not None and resolved.get("lon") is not None and bool(resolved.get("tz"))

    def ensure_summary_for_date(self, summary_date: date, *, force_refresh: bool = False) -> bool:
        summary_iso = summary_date.isoformat()
        if not force_refresh:
            try:
                existing = self.data_logger.get_biodynamic_daily_summary(summary_iso)
            except Exception:
                existing = ""
            if self._summary_is_complete(summary_date, existing):
                if not (self.summary_needs_location_repair(existing) and self._location_is_resolved()):
                    return False

        text = self.build_summary_text(summary_date)
        ok = self.data_logger.save_biodynamic_daily_summary(summary_iso, text)
        if ok and DEBUG:
            printDM(f"[daily-summary] wrote summary for {summary_iso}", location=MODULE)
        return bool(ok)

    def repair_summaries_for_dates(self, summary_dates) -> int:
        """Regenerate specified persisted summaries sequentially for low-resource hosts."""
        writes = 0
        for summary_date in summary_dates:
            if self.ensure_summary_for_date(summary_date, force_refresh=True):
                writes += 1
        return writes

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

    def forecast_window_start(self, anchor_date: date | None = None) -> date:
        """Return the month anchor for the annual biodynamic hint window."""
        base_date = anchor_date or datetime.now(self.local_tz).date()
        return _month_start(base_date)

    def ensure_forecast_window(self, anchor_date: date | None = None) -> int:
        """Materialize the annual biodynamic hint window from the current month."""
        return self.ensure_summaries_for_window(self.forecast_window_start(anchor_date))

    def _feed_watchdog(self, *, error: bool = False) -> None:
        sup = getattr(self, "supervisor", None)
        if sup and hasattr(sup, "feedthedogs"):
            sup.feedthedogs("Daily Summary Writer", error=error)

    def _report_issue(self, message: str, *, recommend_restart: bool = False) -> None:
        sup = getattr(self, "supervisor", None)
        if sup and hasattr(sup, "report_issue"):
            sup.report_issue(
                "Daily Summary Writer",
                message,
                recommend_restart=recommend_restart,
                issue_type="service_warning",
            )

    async def _sleep_with_heartbeat(self, total_sleep_s: float, heartbeat_every_s: float = 20.0) -> None:
        remaining = max(float(total_sleep_s), 0.0)
        while remaining > 0.0:
            self._feed_watchdog()
            chunk = min(heartbeat_every_s, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _ensure_summaries_for_window_async(self, start_date: date, heartbeat_every_s: float = 5.0) -> int:
        """Run summary generation off the event loop while heartbeats continue."""
        task = asyncio.create_task(
            asyncio.to_thread(self.ensure_summaries_for_window, start_date),
            name="DailySummaryService.ensure_summaries_for_window",
        )
        try:
            while True:
                self._feed_watchdog()
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=max(float(heartbeat_every_s), 0.1),
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()

    async def run(self):
        while True:
            try:
                self._feed_watchdog()
                now_local = datetime.now(self.local_tz)
                window_start = self.forecast_window_start(now_local.date())
                await self._ensure_summaries_for_window_async(window_start)
                next_run = datetime.combine(_next_month_start(window_start), dtime(0, 0, 1), self.local_tz)
                sleep_s = max((next_run - now_local).total_seconds(), 1.0)
            except Exception as exc:
                self._feed_watchdog()
                self._report_issue(f"Daily summary writer hit a recoverable error: {exc}", recommend_restart=False)
                printDM(f"[daily-summary] loop error: {exc}", location=MODULE)
                sleep_s = 60.0
            await self._sleep_with_heartbeat(sleep_s)
