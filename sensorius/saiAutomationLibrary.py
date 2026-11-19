"""Describe crop-independent library starters and prepare validated imports.

The bundled examples use ordinary garden crops. Templates contain no prescribed
cultivation targets: users supply their own sensor bindings and thresholds.
Imports produce ordinary disabled Advanced rules, with independent provenance.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import uuid
from zoneinfo import ZoneInfo

from .saiInputUnits import normalize_condition_inputs

REVISION = 1
ENVIRONMENTS = ("Indoor", "Outdoor", "Greenhouse")
PARTS = {"Root": "earth", "Leaf": "water", "Flower": "air", "Fruit": "fire"}
STAGES = {"seedling": "Seedling", "immature": "Immature", "mature": "Mature", "harvest": "Ready to Harvest"}
CROPS = [
    {"id": f"{environment.lower()}-{name.lower()}", "name": name,
     "environment": environment, "part": part}
    for environment, examples in (
        ("Indoor", (("Radish", "Root"), ("Basil", "Leaf"), ("Calendula", "Flower"), ("Tomato", "Fruit"))),
        ("Outdoor", (("Carrot", "Root"), ("Kale", "Leaf"), ("Sunflower", "Flower"), ("Strawberry", "Fruit"))),
        ("Greenhouse", (("Beet", "Root"), ("Lettuce", "Leaf"), ("Snapdragon", "Flower"), ("Pepper", "Fruit"))),
    ) for name, part in examples
]
_METRICS = {
    "temperature": ("Temperature", ["Temperature", "Temperature_F"]),
    "humidity": ("Relative humidity", ["Rel-Humidity", "Humidity"]),
    "vpd": ("VPD", ["Ambient VPD", "VPD"]),
    "moisture": ("Soil moisture", ["Soil Moisture"]),
}
TEMPLATES = [
    {"id": f"{role}-{direction}", "title": f"{title} {direction} alert", "kind": "sensor",
     "role": role, "metrics": metrics, "op": op,
     "description": f"Alert when the selected {title.lower()} reading is {direction} relative to your threshold."}
    for role, (title, metrics) in _METRICS.items()
    for direction, op in (("low", "<"), ("high", ">"))
] + [
    {"id": "bd-tending", "title": "Favorable BD tending period", "kind": "bd",
     "description": "Alert when a matching Root, Leaf, Flower, or Fruit calendar period begins."},
    {"id": "stage-start", "title": "Stage start reminder", "kind": "start",
     "description": "A one-time reminder at the start of your chosen stage window."},
]


def catalog() -> dict:
    """Return the small versioned catalog without expanding every combination."""
    return {"revision": REVISION, "crops": CROPS, "templates": TEMPLATES,
            "stages": STAGES, "environments": ENVIRONMENTS, "parts": list(PARTS)}


def normalize_stage_window(start: object, end: object) -> tuple[str, str]:
    """Validate an optional half-open date window, including both endpoints."""
    if not start and not end:
        return "", ""
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except (ValueError, TypeError) as exc:
        raise ValueError("Enter both stage dates as YYYY-MM-DD.") from exc
    if first >= last:
        raise ValueError("Stage end must be after stage start (end is exclusive).")
    return first.isoformat(), last.isoformat()


def prepare_import(payload: dict, *, timezone: str, plantings: list[dict]) -> list[dict]:
    """Validate a selection and resolve it into independent disabled rules."""
    from .saiCalendarAutomation import normalize_calendar_datetime

    if not isinstance(payload, dict):
        raise ValueError("Import must be an object.")
    try:
        batch = str(uuid.UUID(str(payload.get("batch_id", ""))))
    except ValueError as exc:
        raise ValueError("Import needs a valid batch ID.") from exc
    entries = payload.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("Select between 1 and 100 automations.")
    ZoneInfo(timezone)
    crops = {crop["id"]: crop for crop in CROPS}
    templates = {template["id"]: template for template in TEMPLATES}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    prepared, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid library selection.")
        crop = crops.get(entry.get("crop_id"))
        template = templates.get(entry.get("template_id"))
        stage = entry.get("stage")
        if crop is None or template is None or stage not in STAGES:
            raise ValueError("Unknown crop, stage, or library template.")
        key = f"{crop['id']}:{stage}:{template['id']}"
        if key in seen:
            raise ValueError("A template was selected more than once.")
        seen.add(key)
        planting_id = str(entry.get("planting_id") or "")
        planting = next((item for item in plantings if str(item.get("id")) == planting_id), None) if planting_id else None
        if planting_id and planting is None:
            raise ValueError("The selected planting no longer exists.")
        start, end = normalize_stage_window(entry.get("start"), entry.get("end"))
        if planting:
            if planting.get("plant_part") and planting["plant_part"] != crop["part"]:
                raise ValueError("Planting classification does not match the selected crop profile.")
            if planting.get("environment") and planting["environment"] != crop["environment"]:
                raise ValueError("Planting environment does not match the selected crop profile.")
            schedule = (planting.get("stage_schedule") or {}).get(stage) or {}
            start, end = normalize_stage_window(schedule.get("start"), schedule.get("end"))
            if not start:
                raise ValueError(f"Set the {STAGES[stage]} stage window in BD Calendar Plantings first.")
        gate = {"type": "calendar", "calendar_kind": "date_range", "calendar_start": start,
                "calendar_end": end, "calendar_timezone": timezone}
        conditions = [gate] if start else []
        if template["kind"] == "sensor":
            metric, sensor = str(entry.get("metric") or ""), str(entry.get("sensor") or "").strip()
            if not sensor or len(sensor) > 128 or metric not in template["metrics"]:
                raise ValueError("Select a compatible sensor and metric for each sensor alert.")
            try:
                value, hyst = float(entry["value"]), float(entry.get("hyst", 0))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("Enter a threshold and non-negative hysteresis.") from exc
            if not math.isfinite(value) or not math.isfinite(hyst) or hyst < 0:
                raise ValueError("Enter finite thresholds and non-negative hysteresis.")
            sensor_condition = {"type": "sensor", "sensor": sensor, "metric": metric,
                                "op": template["op"], "value": value, "hyst": hyst}
            if entry.get("unit") and metric in {"Temperature", "Temperature_F"}:
                sensor_condition.update(value_unit=entry["unit"], hyst_unit=entry["unit"])
            normalized = normalize_condition_inputs(sensor_condition)
            if not all(math.isfinite(float(normalized[field])) for field in ("value", "hyst")):
                raise ValueError("Converted thresholds and hysteresis must be finite.")
            conditions.append(normalized)
        elif template["kind"] == "bd":
            conditions.append({"type": "calendar", "calendar_kind": "bd_transitions",
                               "calendar_event": PARTS[crop["part"]], "executor_switch_id": "__system__"})
        else:
            if not start:
                raise ValueError("Stage start reminders require a stage window.")
            target = normalize_calendar_datetime(start + "T00:00", timezone)
            conditions = [{"type": "calendar", "calendar_kind": "date_time", "calendar_at": target,
                           "calendar_local": start + "T00:00", "calendar_timezone": timezone,
                           "executor_switch_id": "__system__"}]
        prefix = str(entry.get("name") or (planting or {}).get("name") or crop["name"]).strip()
        if not prefix or len(prefix) > 80:
            raise ValueError("Use a planting name of 1 to 80 characters.")
        variety = str(entry.get("variety") or (planting or {}).get("variety") or "").strip()
        if len(variety) > 80:
            raise ValueError("Use a variety name of at most 80 characters.")
        if variety and variety.casefold() not in prefix.casefold():
            prefix += f" ({variety})"
        script = {"name": f"{prefix} / {STAGES[stage]} / {template['title']}", "enabled": False,
                  "conditions": conditions, "actions": [{"type": "none", "executor_switch_id": "__system__"}]}
        source = {"template_id": template["id"], "revision": REVISION, "crop_id": crop["id"],
                  "environment": crop["environment"], "part": crop["part"], "stage": stage, "variety": variety,
                  "planting_id": planting_id, "batch_id": batch, "digest": digest}
        prepared.append({"rule_id": "auto-library-" + uuid.uuid5(uuid.UUID(batch), key).hex,
                         "enabled": False, "script_json": json.dumps(script, separators=(",", ":")),
                         "library_json": json.dumps(source, separators=(",", ":"))})
    return prepared
