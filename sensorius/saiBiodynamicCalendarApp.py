"""Serve integrated Biodynamic Calendar routes and persistence workflows.

The module coordinates calendar rendering, note storage, payload caching, and
background warming for the main Sensorius process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__ as SAI_APP_VERSION
from .biodynamic_calendar import (
    BiodynamicConfig,
    get_astro_payload,
    get_biodynamic_payload,
    get_daily_summary,
)
from .biodynamic_calendar.core import CALCULATION_IMPLEMENTATION_VERSION
from .biodynamic_calendar.storage_validation import MAX_NOTE_LENGTH, _normalize_planting
from .saiUtils import debug_enabled, printDM


MODULE = "saiBiodynamicCalendarApp"
DEBUG = debug_enabled(MODULE)
_MAX_CACHE_ENTRIES = 120
_REGISTERED_SERVICE: "BiodynamicCalendarService | None" = None
_BIODYNAMIC_CALENDAR_THEMES = frozenset({"leaf", "garden_tools", "herbarium", "pollinator", "white"})


def get_registered_biodynamic_calendar_service() -> "BiodynamicCalendarService | None":
    """Return the process-wide calendar service shared by UI and automations."""
    return _REGISTERED_SERVICE


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(float(os.environ.get(name, default)), minimum)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(int(os.environ.get(name, default)), minimum)
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _shift_month(anchor: date, offset: int) -> date:
    month_index = (anchor.year * 12) + (anchor.month - 1) + int(offset)
    year, month_zero = divmod(month_index, 12)
    return date(year, month_zero + 1, 1)


def _normalize_biodynamic_calendar_theme(value: object) -> str:
    theme = str(value or "").strip().lower().replace("-", "_")
    return theme if theme in _BIODYNAMIC_CALENDAR_THEMES else "leaf"


def _cache_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class NoteRequest(BaseModel):
    """Validate a request to create or replace a dated calendar note."""
    model_config = ConfigDict(extra="forbid")

    day: date = Field(alias="date")
    note: str | None = Field(default="", max_length=MAX_NOTE_LENGTH)


class PlantingRequest(BaseModel):
    """Validate and normalize a calendar planting request payload."""
    model_config = ConfigDict(extra="forbid")

    id: Any = None
    name: Any = None
    plant: Any = None
    crop: Any = None
    variety: Any = None
    plant_type: Any = None
    type: Any = None
    plant_part: Any = None
    biodynamic_part: Any = None
    part: Any = None
    start_method: Any = None
    method: Any = None
    start_date: Any = None
    started_on: Any = None
    date: Any = None
    expected_harvest_date: Any = None
    harvest_date: Any = None
    days_to_maturity: Any = None
    harvest_window_days: Any = None
    location: Any = None
    bed: Any = None
    attributes: Any = None
    notes: Any = None


class BiodynamicCalendarService:
    """Serve and prewarm BD Calendar data without blocking Sensorius' event loop."""

    def __init__(self, *, settings, data_logger, supervisor=None, legacy_root: Path | None = None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self.legacy_root = legacy_root or (Path.home() / ".biodynamic_calendar")
        self._build_lock = asyncio.Lock()
        self._sync_build_lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._month_tasks: dict[str, asyncio.Task] = {}
        self._summary_tasks: dict[str, asyncio.Task] = {}
        self._astro_cache: tuple[float, str, dict[str, object]] | None = None
        self._warm_task: asyncio.Task | None = None
        self._transition_cache: tuple[str, float, dict[str, object]] | None = None
        self._legacy_import_lock = asyncio.Lock()
        self._legacy_import_done = False

    def _import_legacy_state_sync(self) -> None:
        """Import former standalone JSON state when the matching DB table is empty."""
        root = self.legacy_root
        try:
            existing_notes = self.data_logger.get_biodynamic_notes_for_range(date(1, 1, 1), date(9999, 12, 31))
        except Exception:
            existing_notes = {}
        notes_path = root / "notes.json"
        if not existing_notes and notes_path.exists():
            try:
                raw_notes = json.loads(notes_path.read_text(encoding="utf-8"))
            except Exception:
                raw_notes = {}
            if isinstance(raw_notes, dict):
                for day_iso, note in raw_notes.items():
                    self.data_logger.save_biodynamic_note(str(day_iso), str(note or ""))

        try:
            existing_plantings = self.data_logger.get_biodynamic_plantings()
        except Exception:
            existing_plantings = []
        plantings_path = root / "plantings.json"
        if not existing_plantings and plantings_path.exists():
            try:
                raw_plantings = json.loads(plantings_path.read_text(encoding="utf-8"))
            except Exception:
                raw_plantings = []
            rows = raw_plantings.get("plantings") if isinstance(raw_plantings, dict) else raw_plantings
            if isinstance(rows, list):
                for index, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    planting, _error = _normalize_planting(row, fallback_id=f"planting-{index + 1}")
                    if planting is not None:
                        self.data_logger.save_biodynamic_planting(planting)

    async def ensure_legacy_import(self) -> None:
        """Run the idempotent standalone-state import once per process."""
        if self._legacy_import_done:
            return
        async with self._legacy_import_lock:
            if self._legacy_import_done:
                return
            await asyncio.to_thread(self._import_legacy_state_sync)
            self._legacy_import_done = True

    def location(self) -> tuple[BiodynamicConfig | None, dict[str, object]]:
        resolved = self.settings.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
        payload = {
            "ok": False,
            "source": str(resolved.get("source") or "none"),
            "provider": str(resolved.get("provider") or ""),
            "error": str(resolved.get("error") or ""),
            "altitude": resolved.get("altitude"),
            "latitude": resolved.get("lat"),
            "longitude": resolved.get("lon"),
            "timezone_name": str(resolved.get("tz") or ""),
            "lat": resolved.get("lat"),
            "lon": resolved.get("lon"),
            "tz": str(resolved.get("tz") or ""),
        }
        try:
            config = BiodynamicConfig(
                latitude=float(resolved["lat"]),
                longitude=float(resolved["lon"]),
                timezone_name=str(resolved["tz"]),
            )
        except Exception:
            return None, payload
        payload["ok"] = True
        return config, payload

    @staticmethod
    def _location_key(config: BiodynamicConfig) -> str:
        return json.dumps(
            {
                "lat": round(float(config.latitude), 6),
                "lon": round(float(config.longitude), 6),
                "tz": str(config.timezone_name),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _month_cache_key(anchor: date) -> str:
        return f"calculation-v{CALCULATION_IMPLEMENTATION_VERSION}:month:{anchor:%Y-%m}"

    def _refresh_month(self, payload: dict[str, object], config: BiodynamicConfig) -> dict[str, object]:
        refreshed = dict(payload)
        now_local = datetime.now(ZoneInfo(config.timezone_name))
        today_iso = now_local.date().isoformat()
        rows = []
        today_row = None
        for source in payload.get("calendar") or []:
            if not isinstance(source, dict):
                continue
            row = dict(source)
            row["is_today"] = str(row.get("date") or "") == today_iso
            if row["is_today"]:
                today_row = row
            rows.append(row)
        refreshed["calendar"] = rows
        if isinstance(today_row, dict):
            current_segment = None
            current_minute = (now_local.hour * 60) + now_local.minute
            for segment in today_row.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                try:
                    start_hour, start_minute = (int(part) for part in str(segment.get("start") or "00:00").split(":"))
                    end_text = str(segment.get("end") or "24:00")
                    end_hour, end_minute = (int(part) for part in end_text.split(":"))
                    start_total = (start_hour * 60) + start_minute
                    end_total = (end_hour * 60) + end_minute
                except Exception:
                    continue
                if start_total <= current_minute < end_total:
                    current_segment = segment
                    break
            if isinstance(current_segment, dict):
                start_text = str(current_segment.get("start") or "00:00")
                end_text = str(current_segment.get("end") or "24:00")
                start_local = datetime.fromisoformat(f"{today_iso}T{start_text}:00").replace(tzinfo=now_local.tzinfo)
                end_local = (
                    datetime.combine(now_local.date() + timedelta(days=1), datetime.min.time(), tzinfo=now_local.tzinfo)
                    if end_text == "24:00"
                    else datetime.fromisoformat(f"{today_iso}T{end_text}:00").replace(tzinfo=now_local.tzinfo)
                )
                refreshed["current"] = {
                    "timestamp": now_local.isoformat(),
                    "sign": str(current_segment.get("sign") or ""),
                    "element": str(current_segment.get("element") or ""),
                    "plant_part": str(current_segment.get("plant_part") or ""),
                    "color": str(current_segment.get("color") or ""),
                    "accent": str(current_segment.get("accent") or ""),
                    "kind": str(current_segment.get("kind") or ""),
                    "off_kind": str(current_segment.get("off_kind") or ""),
                    "off_label": str(current_segment.get("off_label") or ""),
                    "moon_direction": str(today_row.get("moon_direction") or ""),
                    "window_start": start_local.isoformat(),
                    "window_end": end_local.isoformat(),
                    "window_start_hm": start_text,
                    "window_end_hm": end_text,
                    "calendar_basis": "moon apparent position classified against fixed-star constellation boundaries",
                }
        refreshed["generated_at"] = datetime.now(timezone.utc).isoformat()
        return refreshed

    def _load_month(self, anchor: date, config: BiodynamicConfig) -> dict[str, object] | None:
        payload = self.data_logger.get_biodynamic_calendar_cache(
            self._month_cache_key(anchor),
            self._location_key(config),
        )
        if isinstance(payload, dict):
            return self._refresh_month(payload, config)
        return None

    def build_month_sync(self, anchor: date, config: BiodynamicConfig) -> dict[str, object]:
        """Load or synchronously build one persisted month payload."""
        with self._sync_build_lock:
            cached = self._load_month(anchor, config)
            if cached is not None:
                return cached
            payload = get_biodynamic_payload(anchor, config=config)
            if payload.get("ok"):
                self.data_logger.save_biodynamic_calendar_cache(
                    self._month_cache_key(anchor),
                    self._location_key(config),
                    payload,
                    max_entries=_MAX_CACHE_ENTRIES,
                )
            return dict(payload)

    def clear_dynamic_cache(self) -> None:
        """Discard the small current-transition cache after location changes."""
        with self._transition_lock:
            self._transition_cache = None

    def current_transition_sync(self) -> dict[str, object]:
        """Return the current persisted BD segment without rebuilding a month per monitor tick."""
        now_mono = time.monotonic()
        with self._transition_lock:
            cached = self._transition_cache
            if cached and cached[1] > now_mono:
                return dict(cached[2])

            config, _location = self.location()
            if config is None:
                self._transition_cache = ("", now_mono + 30.0, {})
                return {}
            location_key = self._location_key(config)
            now_local = datetime.now(ZoneInfo(config.timezone_name))
            payload = self.build_month_sync(now_local.date().replace(day=1), config)
            current = payload.get("current") if isinstance(payload, dict) else {}
            if not payload.get("ok") or not isinstance(current, dict):
                self._transition_cache = (location_key, now_mono + 30.0, {})
                return {}

            transition_at = str(current.get("window_start") or "").strip()
            if not transition_at:
                self._transition_cache = (location_key, now_mono + 30.0, {})
                return {}
            result = {
                "transition_at": transition_at,
                "window_end": str(current.get("window_end") or "").strip(),
                "sign": str(current.get("sign") or "").strip(),
                "element": str(current.get("element") or "").strip(),
                "plant_part": str(current.get("plant_part") or "").strip(),
                "color": str(current.get("color") or "").strip(),
                "accent": str(current.get("accent") or "").strip(),
            }
            ttl = 300.0
            try:
                window_end = datetime.fromisoformat(str(result["window_end"]))
                ttl = max(1.0, min((window_end - now_local).total_seconds(), 300.0))
            except Exception:
                pass
            self._transition_cache = (location_key, now_mono + ttl, dict(result))
            return result

    async def month(self, anchor: date, config: BiodynamicConfig) -> dict[str, object]:
        cached = await asyncio.to_thread(self._load_month, anchor, config)
        if cached is not None:
            return cached
        key = f"{self._location_key(config)}:{anchor:%Y-%m}"
        task = self._month_tasks.get(key)
        if task is None or task.done():
            async def _build() -> dict[str, object]:
                async with self._build_lock:
                    return await asyncio.to_thread(self.build_month_sync, anchor, config)
            task = asyncio.create_task(_build(), name=f"BDCalendarMonth:{anchor:%Y-%m}")
            self._month_tasks[key] = task
        try:
            return dict(await task)
        finally:
            if self._month_tasks.get(key) is task and task.done():
                self._month_tasks.pop(key, None)

    async def astro(self, config: BiodynamicConfig) -> dict[str, object]:
        location_key = self._location_key(config)
        now_mono = time.monotonic()
        if self._astro_cache and self._astro_cache[0] > now_mono and self._astro_cache[1] == location_key:
            return dict(self._astro_cache[2])
        async with self._build_lock:
            payload = await asyncio.to_thread(get_astro_payload, config=config)
        self._astro_cache = (time.monotonic() + 60.0, location_key, dict(payload))
        return dict(payload)

    def notes_for_payload(self, payload: dict[str, object]) -> dict[str, str]:
        dates = [str(row.get("date")) for row in payload.get("calendar") or [] if isinstance(row, dict) and row.get("date")]
        if not dates:
            return {}
        return self.data_logger.get_biodynamic_notes_for_range(min(dates), max(dates))

    async def daily_summary(self, summary_date: date, config: BiodynamicConfig) -> str:
        plantings = self.data_logger.get_biodynamic_plantings()
        cache_key = (
            f"calculation-v{CALCULATION_IMPLEMENTATION_VERSION}:daily-summary:"
            f"{summary_date.isoformat()}:{_cache_digest(plantings)}"
        )
        location_key = self._location_key(config)
        cached = await asyncio.to_thread(
            self.data_logger.get_biodynamic_calendar_cache,
            cache_key,
            location_key,
        )
        if isinstance(cached, dict):
            return str(cached.get("summary") or "")
        task_key = f"{location_key}:{cache_key}"
        task = self._summary_tasks.get(task_key)
        if task is None or task.done():
            async def _build() -> str:
                async with self._build_lock:
                    summary = await asyncio.to_thread(
                        get_daily_summary,
                        summary_date,
                        config=config,
                        plantings=plantings,
                    )
                    await asyncio.to_thread(
                        self.data_logger.save_biodynamic_calendar_cache,
                        cache_key,
                        location_key,
                        {"ok": True, "date": summary_date.isoformat(), "summary": str(summary or "")},
                    )
                    await asyncio.to_thread(
                        self.data_logger.save_biodynamic_daily_summary,
                        summary_date.isoformat(),
                        str(summary or ""),
                    )
                    return str(summary or "")
            task = asyncio.create_task(_build(), name=f"BDCalendarSummary:{summary_date.isoformat()}")
            self._summary_tasks[task_key] = task
        try:
            return str(await task)
        finally:
            if self._summary_tasks.get(task_key) is task and task.done():
                self._summary_tasks.pop(task_key, None)

    def cached_range(self, anchor: date, months: int, config: BiodynamicConfig) -> tuple[list[dict[str, object]], list[str]]:
        ready: list[dict[str, object]] = []
        missing: list[str] = []
        for offset in range(months):
            month_anchor = _shift_month(anchor, offset)
            payload = self._load_month(month_anchor, config)
            if payload is None:
                missing.append(month_anchor.strftime("%Y-%m"))
            else:
                ready.append(payload)
        return ready, missing

    def ensure_background_warm(self) -> None:
        if not _env_bool("SENSORIUS_BIODYNAMIC_PREWARM_ENABLED", True):
            return
        if self._warm_task is None or self._warm_task.done():
            self._warm_task = asyncio.create_task(self.run_warm_once(), name="BDCalendarWarm")

    async def run_warm_once(self) -> None:
        delay = _env_float("SENSORIUS_BIODYNAMIC_PREWARM_DELAY_SEC", 45.0)
        pause = _env_float("SENSORIUS_BIODYNAMIC_PREWARM_PAUSE_SEC", 3.0)
        future = _env_int("SENSORIUS_BIODYNAMIC_PREWARM_FUTURE_MONTHS", 12)
        past = _env_int("SENSORIUS_BIODYNAMIC_PREWARM_PAST_MONTHS", 1)
        await asyncio.sleep(delay)
        await self.ensure_legacy_import()
        config, _location = await asyncio.to_thread(self.location)
        if config is None:
            return
        today = datetime.now(ZoneInfo(config.timezone_name)).date()
        anchor = today.replace(day=1)
        offsets = [0]
        if past:
            offsets.append(-1)
        if future:
            offsets.append(1)
        offsets.extend(range(2, future + 1))
        offsets.extend(range(-2, -past - 1, -1))
        seen = set()
        for offset in offsets:
            month_anchor = _shift_month(anchor, offset)
            if month_anchor in seen:
                continue
            seen.add(month_anchor)
            try:
                await self.month(month_anchor, config)
                if offset == 0:
                    await self.astro(config)
                    await self.daily_summary(today, config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if DEBUG:
                    printDM(f"Calendar warm skipped {month_anchor:%Y-%m}: {exc}", location=MODULE)
            await asyncio.sleep(pause)

    async def shutdown(self) -> None:
        global _REGISTERED_SERVICE
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
        if _REGISTERED_SERVICE is self:
            _REGISTERED_SERVICE = None


def register_biodynamic_calendar_routes(
    router: APIRouter,
    *,
    app,
    settings,
    data_logger,
) -> BiodynamicCalendarService:
    """Register the full-screen calendar and its namespaced API routes."""
    global _REGISTERED_SERVICE
    service = BiodynamicCalendarService(
        settings=settings,
        data_logger=data_logger,
        supervisor=getattr(app.state, "supervisor", None),
    )
    app.state.biodynamic_calendar_service = service
    _REGISTERED_SERVICE = service
    app.add_event_handler("startup", service.ensure_background_warm)
    app.add_event_handler("shutdown", service.shutdown)

    @router.get("/calendar", response_class=HTMLResponse)
    async def calendar_page(request: Request):
        await service.ensure_legacy_import()
        config, _location = await asyncio.to_thread(service.location)
        biodynamic_calendar_theme = _normalize_biodynamic_calendar_theme(
            settings.get_setting("Display", "biodynamic_calendar_theme", "leaf")
        )
        return app.state.templates.TemplateResponse(
            request,
            "biodynamic_calendar/index.html",
            {
                "config": config,
                "plantings": await asyncio.to_thread(data_logger.get_biodynamic_plantings),
                "app_version": SAI_APP_VERSION,
                "sensorius_launch": True,
                "biodynamic_calendar_theme": biodynamic_calendar_theme,
                "runtime_instance_id": str(getattr(request.app.state, "ui_runtime_instance_id", "") or ""),
            },
        )

    @router.get("/calendar/report", response_class=HTMLResponse)
    async def calendar_report(request: Request):
        return app.state.templates.TemplateResponse(
            request,
            "biodynamic_calendar/print_report.html",
            {"app_version": SAI_APP_VERSION},
        )

    @router.get("/api/biodynamic-calendar-app/calendar", response_class=JSONResponse)
    async def api_calendar(month: str = ""):
        await service.ensure_legacy_import()
        config, location = await asyncio.to_thread(service.location)
        if config is None:
            return JSONResponse({"ok": False, "reason": "config_missing", "calendar": [], "notes": {}, "plantings": [], "location": location})
        try:
            anchor = datetime.strptime(month, "%Y-%m").date().replace(day=1) if month else datetime.now(ZoneInfo(config.timezone_name)).date().replace(day=1)
        except Exception:
            return JSONResponse({"error": "invalid_month"}, status_code=400)
        payload = await service.month(anchor, config)
        payload["astro"] = await service.astro(config)
        payload["notes"] = await asyncio.to_thread(service.notes_for_payload, payload)
        payload["plantings"] = await asyncio.to_thread(data_logger.get_biodynamic_plantings)
        payload["location"] = location
        return JSONResponse(payload)

    @router.get("/api/biodynamic-calendar-app/calendar-range", response_class=JSONResponse)
    async def api_calendar_range(start: str = "", months: int = 13):
        config, location = await asyncio.to_thread(service.location)
        if config is None:
            return JSONResponse({"ok": False, "reason": "config_missing", "months": [], "plantings": [], "location": location})
        try:
            anchor = datetime.strptime(start, "%Y-%m").date().replace(day=1) if start else datetime.now(ZoneInfo(config.timezone_name)).date().replace(day=1)
            month_count = max(1, min(int(months or 13), 36))
        except Exception:
            return JSONResponse({"error": "invalid_range"}, status_code=400)
        ready, missing = await asyncio.to_thread(service.cached_range, anchor, month_count, config)
        if missing:
            service.ensure_background_warm()
        return JSONResponse(
            {
                "ok": bool(ready),
                "reason": "warming" if missing else "",
                "warming": bool(missing),
                "missing_months": missing,
                "start_month": anchor.strftime("%Y-%m"),
                "months_requested": month_count,
                "months": ready,
                "notes": {},
                "plantings": await asyncio.to_thread(data_logger.get_biodynamic_plantings),
                "location": location,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    @router.get("/api/biodynamic-calendar-app/daily-summary", response_class=JSONResponse)
    async def api_daily_summary(day: str = ""):
        config, location = await asyncio.to_thread(service.location)
        if config is None:
            return JSONResponse({"ok": False, "reason": "config_missing", "summary": "", "location": location})
        try:
            summary_date = datetime.strptime(day, "%Y-%m-%d").date()
        except Exception:
            return JSONResponse({"error": "invalid_day"}, status_code=400)
        summary = await service.daily_summary(summary_date, config)
        return JSONResponse({"ok": True, "date": summary_date.isoformat(), "summary": summary})

    @router.post("/api/biodynamic-calendar-app/note", response_class=JSONResponse)
    async def api_note(body: NoteRequest):
        ok = await asyncio.to_thread(data_logger.save_biodynamic_note, body.day.isoformat(), body.note or "")
        return JSONResponse({"ok": ok}, status_code=200 if ok else 500)

    @router.get("/api/biodynamic-calendar-app/plantings", response_class=JSONResponse)
    async def api_plantings():
        return JSONResponse({"ok": True, "plantings": await asyncio.to_thread(data_logger.get_biodynamic_plantings)})

    @router.post("/api/biodynamic-calendar-app/planting", response_class=JSONResponse)
    async def api_save_planting(body: PlantingRequest):
        planting, error = _normalize_planting(body.model_dump(exclude_none=True))
        if planting is None:
            return JSONResponse({"error": "invalid_planting", "reason": error}, status_code=400)
        ok = await asyncio.to_thread(data_logger.save_biodynamic_planting, planting)
        return JSONResponse(
            {"ok": ok, "planting": planting, "plantings": await asyncio.to_thread(data_logger.get_biodynamic_plantings)},
            status_code=200 if ok else 500,
        )

    @router.delete("/api/biodynamic-calendar-app/planting/{planting_id}", response_class=JSONResponse)
    async def api_delete_planting(planting_id: str):
        deleted = await asyncio.to_thread(data_logger.delete_biodynamic_planting, planting_id)
        return JSONResponse({"ok": True, "deleted": deleted, "plantings": await asyncio.to_thread(data_logger.get_biodynamic_plantings)})

    return service
