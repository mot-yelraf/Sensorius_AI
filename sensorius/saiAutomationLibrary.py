"""Describe library starters and recipes and prepare validated imports.

Generic starters leave thresholds to the user. Recipes offer editable targets
for their own stages and use the same sensor bindings and import path.
Imports produce ordinary disabled Advanced rules, with independent provenance.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import math
import uuid
from zoneinfo import ZoneInfo

from .saiInputUnits import normalize_condition_inputs

REVISION = 3
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
_MUSHROOM_STAGES = {
    "incubation": {
        "name": "Bag Incubation",
        "description": "Keep the closed bags at 65–70°F with filtered gas exchange and space to shed heat. Ordinary room light is fine.",
        "templates": ["mushroom-incubation-low", "mushroom-incubation-high"],
    },
    "pinning": {
        "name": "Pinning",
        "description": "Aim for 62–68°F and around 90% RH near the block. Increase fresh-air exchange and use gentle indirect light on a day/night schedule.",
        "templates": ["mushroom-temperature-low", "mushroom-temperature-high",
                      "mushroom-pinning-humidifier", "mushroom-vpd-low", "mushroom-pinning-vpd-high",
                      "mushroom-fan-schedule", "mushroom-lights"],
    },
    "fruiting": {
        "name": "Fruiting",
        "description": "Aim for 62–68°F, 85–90% RH, and CO₂ below 800 ppm. Continue the pinning light schedule and fresh-air exchange.",
        "templates": ["mushroom-temperature-low", "mushroom-temperature-high",
                      "mushroom-fruiting-humidifier", "mushroom-humidity-high",
                      "mushroom-vpd-low", "mushroom-fruiting-vpd-high", "mushroom-fruiting-fan",
                      "mushroom-fan-schedule", "mushroom-lights"],
    },
}
CROPS.append({"id": "indoor-oyster-mushroom", "name": "Oyster Mushroom",
              "environment": "Indoor", "part": "Fruit", "recipe": _MUSHROOM_STAGES})

_DRYING_HOURS = 10 * 24
_INITIAL_DRYING_HOURS = 48
_CANNABIS_STAGES = {
    "drying": {
        "name": "Drying",
        "description": "18–21°C for 10 days. Target 40% RH for the first 48 hours, then 60% RH.",
        "hours": [0, _DRYING_HOURS],
        "templates": ["cannabis-temperature-low", "cannabis-temperature-high",
                      "cannabis-initial-rh-low", "cannabis-initial-rh-high",
                      "cannabis-drying-rh-low", "cannabis-drying-rh-high"],
    },
    "curing": {
        "name": "Curing",
        "description": "18–21°C and 60% RH starting 10 days after drying began.",
        "hours": [_DRYING_HOURS, None],
        "templates": ["cannabis-temperature-low", "cannabis-temperature-high",
                      "cannabis-curing-rh-low", "cannabis-curing-rh-high"],
    },
}
CROPS.append({"id": "indoor-cannabis", "name": "Cannabis", "environment": "Indoor",
              "part": "Flower", "recipe": _CANNABIS_STAGES})

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
_MUSHROOM_METRICS = {
    "temperature": _METRICS["temperature"][1],
    "relative humidity": ["Rel-Humidity"],
    "vpd": _METRICS["vpd"][1],
    "CO₂": ["CO2"],
}
TEMPLATES += [
    {"id": f"mushroom-{key}", "title": title, "kind": "sensor", "recipe_only": True,
     "role": role, "metrics": _MUSHROOM_METRICS[role], "op": op, "default_value": value,
     **({"default_unit": "°F"} if role == "temperature" else {})}
    for key, title, role, op, value in (
        ("incubation-low", "Temperature low alert", "temperature", "<", 65),
        ("incubation-high", "Temperature high alert", "temperature", ">", 70),
        ("temperature-low", "Temperature low alert", "temperature", "<", 62),
        ("temperature-high", "Temperature high alert", "temperature", ">", 68),
        ("pinning-humidifier", "Turn on humidifier", "relative humidity", "<", 90),
        ("fruiting-humidifier", "Turn on humidifier", "relative humidity", "<", 85),
        ("humidity-high", "Relative humidity high alert", "relative humidity", ">", 90),
        # Air VPD at 62°F / 90% RH, 68°F / 90% RH, and 68°F / 85% RH, in kPa.
        ("vpd-low", "VPD low alert", "vpd", "<", 0.190),
        ("pinning-vpd-high", "VPD high alert", "vpd", ">", 0.234),
        ("fruiting-vpd-high", "VPD high alert", "vpd", ">", 0.351),
        ("fruiting-fan", "Turn on fan (CO₂)", "CO₂", ">=", 800),
    )
] + [
    {"id": "mushroom-fan-schedule", "title": "Turn on fan (scheduled)", "kind": "time", "recipe_only": True,
     "description": "A daily fresh-air reminder during the time window you choose."},
    {"id": "mushroom-lights", "title": "Turn on lights", "kind": "time", "recipe_only": True,
     "description": "A daily reminder at the start of your light period."},
]
TEMPLATES += [
    {"id": f"cannabis-temperature-{direction}", "title": f"Temperature {direction} alert",
     "kind": "sensor", "recipe_only": True, "role": "temperature",
     "metrics": _METRICS["temperature"][1], "op": op, "default_value": value, "default_unit": "°C"}
    for direction, op, value in (("low", "<", 18), ("high", ">", 21))
] + [
    {"id": f"cannabis-{phase}-rh-{direction}", "title": f"Relative humidity {direction} alert{suffix}",
     "kind": "sensor", "recipe_only": True, "role": "relative humidity",
     "metrics": ["Rel-Humidity"], "op": op, "default_value": value,
     **({"hours": hours} if hours else {})}
    for phase, value, suffix, hours in (
        ("initial", 40, " (first 48 hours)", [0, _INITIAL_DRYING_HOURS]),
        ("drying", 60, " (after 48 hours)", [_INITIAL_DRYING_HOURS, _DRYING_HOURS]),
        ("curing", 60, "", None),
    )
    for direction, op in (("low", "<"), ("high", ">"))
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
    drying_started = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid library selection.")
        crop = crops.get(entry.get("crop_id"))
        template = templates.get(entry.get("template_id"))
        stage = entry.get("stage")
        if crop is None or template is None:
            raise ValueError("Unknown crop, stage, or library template.")
        recipe = crop.get("recipe")
        if recipe:
            if stage not in recipe or template["id"] not in recipe[stage]["templates"]:
                raise ValueError("Unknown stage or template for this recipe.")
            recipe_stage = recipe[stage]
            stage_name = recipe_stage["name"]
        else:
            if stage not in STAGES or template.get("recipe_only"):
                raise ValueError("Unknown crop, stage, or library template.")
            stage_name = STAGES[stage]
            recipe_stage = {}
        key = f"{crop['id']}:{stage}:{template['id']}"
        if key in seen:
            raise ValueError("A template was selected more than once.")
        seen.add(key)
        planting_id = str(entry.get("planting_id") or "")
        if recipe and planting_id:
            raise ValueError("Use manual stage dates for this recipe.")
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
        conditions = []
        if "hours" in recipe_stage:
            if start:
                raise ValueError("Drying and curing windows use the shared drying start time.")
            try:
                if drying_started is None:
                    local_start = normalize_calendar_datetime(payload.get("drying_started_at"), timezone)
                    drying_started = datetime.fromisoformat(local_start).astimezone(UTC)
                first_hour, end_hour = template.get("hours", recipe_stage["hours"])
                begins = (drying_started + timedelta(hours=first_hour)).isoformat()
                ends = (drying_started + timedelta(hours=end_hour)).isoformat() if end_hour is not None else ""
            except (ValueError, OverflowError) as exc:
                raise ValueError("Enter a valid drying start date and time in the hub timezone.") from exc
            conditions.append({"type": "calendar", "calendar_kind": "datetime_range",
                               "calendar_start": begins, "calendar_end": ends})
        elif start:
            conditions.append({"type": "calendar", "calendar_kind": "date_range", "calendar_start": start,
                               "calendar_end": end, "calendar_timezone": timezone})
        if template["kind"] == "sensor":
            metric, sensor = str(entry.get("metric") or ""), str(entry.get("sensor") or "").strip()
            if not sensor or len(sensor) > 128 or metric not in template["metrics"]:
                raise ValueError("Select a compatible sensor and metric for each sensor alert.")
            try:
                using_default = "value" not in entry
                value = float(template["default_value"] if using_default else entry["value"])
                hyst = float(entry.get("hyst", 0))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("Enter a threshold and non-negative hysteresis.") from exc
            if not math.isfinite(value) or not math.isfinite(hyst) or hyst < 0:
                raise ValueError("Enter finite thresholds and non-negative hysteresis.")
            sensor_condition = {"type": "sensor", "sensor": sensor, "metric": metric,
                                "op": template["op"], "value": value, "hyst": hyst}
            if metric in {"Temperature", "Temperature_F"}:
                value_unit = template.get("default_unit") if using_default else entry.get("unit")
                sensor_condition.update(value_unit=value_unit, hyst_unit=entry.get("unit"))
            normalized = normalize_condition_inputs(sensor_condition)
            if not all(math.isfinite(float(normalized[field])) for field in ("value", "hyst")):
                raise ValueError("Converted thresholds and hysteresis must be finite.")
            conditions.append(normalized)
        elif template["kind"] == "time":
            start_time, end_time = entry.get("time_start"), entry.get("time_end")
            try:
                valid_times = all(isinstance(value, str) and len(value) == 5
                                  and time.fromisoformat(value).isoformat(timespec="minutes") == value
                                  for value in (start_time, end_time))
            except (TypeError, ValueError):
                valid_times = False
            if not valid_times or start_time == end_time:
                raise ValueError("Enter distinct start and end times (HH:MM).")
            conditions.append({"type": "time", "start": start_time, "end": end_time})
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
        script = {"name": f"{prefix} / {stage_name} / {template['title']}", "enabled": False,
                  "conditions": conditions, "actions": [{"type": "none", "executor_switch_id": "__system__"}]}
        source = {"template_id": template["id"], "revision": REVISION, "crop_id": crop["id"],
                  "environment": crop["environment"], "part": crop["part"], "stage": stage, "variety": variety,
                  "planting_id": planting_id, "batch_id": batch, "digest": digest}
        prepared.append({"rule_id": "auto-library-" + uuid.uuid5(uuid.UUID(batch), key).hex,
                         "enabled": False, "script_json": json.dumps(script, separators=(",", ":")),
                         "library_json": json.dumps(source, separators=(",", ":"))})
    return prepared
