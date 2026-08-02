"""Biodynamic planting and day-hint generation."""

from __future__ import annotations

from datetime import date, datetime
import math


_GROUNDING_REMINDER = "Suggestion: prioritize actual plant health, irrigation status, weather, and disease pressure over calendar timing."
_PLANT_PART_LABELS = {
    "root": "Root",
    "roots": "Root",
    "leaf": "Leaf",
    "leaves": "Leaf",
    "leafy": "Leaf",
    "flower": "Flower",
    "flowers": "Flower",
    "fruit": "Fruit",
    "fruits": "Fruit",
    "seed": "Fruit",
    "seeds": "Fruit",
}
_COMMON_PLANT_PART_TERMS = {
    "arugula": "Leaf",
    "basil": "Leaf",
    "beet": "Root",
    "broccoli": "Flower",
    "cabbage": "Leaf",
    "carrot": "Root",
    "cauliflower": "Flower",
    "celery": "Leaf",
    "chard": "Leaf",
    "cilantro": "Leaf",
    "cucumber": "Fruit",
    "dill": "Leaf",
    "eggplant": "Fruit",
    "garlic": "Root",
    "kale": "Leaf",
    "lavender": "Flower",
    "lettuce": "Leaf",
    "melon": "Fruit",
    "onion": "Root",
    "parsley": "Leaf",
    "pea": "Fruit",
    "pepper": "Fruit",
    "potato": "Root",
    "radish": "Root",
    "spinach": "Leaf",
    "squash": "Fruit",
    "tomato": "Fruit",
    "turnip": "Root",
    "zucchini": "Fruit",
}

def _normalize_crop_stage(crop_stage: str | None) -> str:
    stage = str(crop_stage or "general").strip().lower()
    return stage if stage in {"immature", "seedling", "veg", "maturing", "mature", "harvest", "general"} else "general"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "stress", "stressed"}


def _safe_float(value: object) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        out = int(float(str(value).strip()))
    except Exception:
        return default
    return out


def _parse_iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _normalize_plant_part_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in _PLANT_PART_LABELS:
        return _PLANT_PART_LABELS[text]
    for token, label in _PLANT_PART_LABELS.items():
        if token in text:
            return label
    return ""


def _planting_label(planting: dict[str, object]) -> str:
    name = str(planting.get("name") or planting.get("plant") or planting.get("crop") or "Unnamed planting").strip()
    variety = str(planting.get("variety") or "").strip()
    if variety and variety.lower() not in name.lower():
        return f"{name} ({variety})"
    return name


def _planting_part(planting: dict[str, object]) -> str:
    for key in ("plant_part", "biodynamic_part", "part", "plant_type", "type"):
        explicit = _normalize_plant_part_label(planting.get(key))
        if explicit:
            return explicit
    haystack = " ".join(
        str(planting.get(key) or "")
        for key in ("name", "plant", "crop", "variety", "plant_type", "type", "attributes", "notes")
    ).lower()
    for token, label in _COMMON_PLANT_PART_TERMS.items():
        if token in haystack:
            return label
    return ""


def _planting_method(planting: dict[str, object]) -> str:
    method = str(planting.get("start_method") or planting.get("method") or planting.get("started_as") or "").strip().lower()
    if "transplant" in method:
        return "transplant"
    if "seed" in method or "sow" in method:
        return "seed"
    return "start"


def _is_cannabis_planting(planting: dict[str, object]) -> bool:
    haystack = " ".join(
        str(planting.get(key) or "")
        for key in ("name", "plant", "crop", "variety", "plant_type", "type", "attributes", "notes")
    ).lower()
    return any(token in haystack for token in ("cannabis", "hemp", "marijuana", "marihuana"))


def _planting_stage_for_day(planting: dict[str, object], target_date: date) -> str:
    start = _parse_iso_date(planting.get("start_date"))
    harvest = _parse_iso_date(planting.get("expected_harvest_date") or planting.get("harvest_date"))
    harvest_window = max(0, min(_safe_int(planting.get("harvest_window_days"), 3), 60))
    if start is not None and target_date < start:
        return "general"
    if harvest is not None:
        days_to_harvest = (harvest - target_date).days
        if -harvest_window <= days_to_harvest <= 0:
            return "harvest"
        if days_to_harvest < -harvest_window:
            return "mature"
    if start is None:
        return "general"
    days_since_start = (target_date - start).days
    if _is_cannabis_planting(planting):
        immature_days = 21 if _planting_method(planting) == "seed" else 14
        return "immature" if days_since_start <= immature_days else "mature"
    seedling_days = 21 if _planting_method(planting) == "seed" else 10
    if days_since_start <= seedling_days:
        return "seedling"
    if harvest is not None and harvest > start:
        progress = days_since_start / max(1, (harvest - start).days)
        if progress >= 0.78:
            return "maturing"
    return "veg"


def _planting_entries_for_day(plantings: list[dict[str, object]] | None, target_date: date) -> list[dict[str, object]]:
    if not isinstance(plantings, list):
        return []
    entries: list[dict[str, object]] = []
    for idx, planting in enumerate(plantings):
        if not isinstance(planting, dict):
            continue
        start = _parse_iso_date(planting.get("start_date"))
        harvest = _parse_iso_date(planting.get("expected_harvest_date") or planting.get("harvest_date"))
        if start is None and harvest is None:
            continue
        priority = 999
        reason = ""
        days_from_start: int | None = None
        days_to_harvest: int | None = None
        if start is not None:
            days_from_start = (target_date - start).days
            if days_from_start == 0:
                priority, reason = 0, "start_due"
            elif -7 <= days_from_start < 0:
                priority, reason = min(priority, 30 + abs(days_from_start)), "upcoming_start"
            elif 0 < days_from_start <= 21:
                priority, reason = min(priority, 40 + days_from_start), "recent_start"
        if harvest is not None:
            days_to_harvest = (harvest - target_date).days
            harvest_window = max(0, min(_safe_int(planting.get("harvest_window_days"), 3), 60))
            if days_to_harvest == 0:
                priority, reason = min(priority, 0), "harvest_due"
            elif 0 < days_to_harvest <= 14:
                priority, reason = min(priority, 10 + days_to_harvest), "upcoming_harvest"
            elif -harvest_window <= days_to_harvest < 0:
                priority, reason = min(priority, 15 + abs(days_to_harvest)), "harvest_window"
        if reason == "" and start is not None and start <= target_date and (harvest is None or target_date <= harvest):
            priority, reason = 80, "active"
        if reason == "":
            continue
        entries.append(
            {
                "planting": planting,
                "start": start,
                "harvest": harvest,
                "priority": priority,
                "reason": reason,
                "days_from_start": days_from_start,
                "days_to_harvest": days_to_harvest,
                "stage": _planting_stage_for_day(planting, target_date),
                "part": _planting_part(planting),
                "method": _planting_method(planting),
                "index": idx,
            }
        )
    return sorted(entries, key=lambda row: (int(row["priority"]), int(row["index"])))


def _stage_from_planting_entries(entries: list[dict[str, object]]) -> str:
    stage_priority = {"harvest": 0, "immature": 1, "seedling": 2, "maturing": 3, "mature": 4, "veg": 5, "general": 6}
    stage = next(
        (
            str(row.get("stage") or "")
            for row in sorted(entries, key=lambda item: stage_priority.get(str(item.get("stage") or "general"), 9))
            if str(row.get("stage") or "") in stage_priority
        ),
        "",
    )
    return stage if stage in {"immature", "seedling", "veg", "maturing", "mature", "harvest"} else ""


def _days_away_phrase(days: int | None, *, past: str) -> str:
    if days is None:
        return ""
    if days == 0:
        return "today"
    if days > 0:
        return f"in {days} day{'s' if days != 1 else ''}"
    return f"{abs(days)} day{'s' if abs(days) != 1 else ''} {past}"


def _planting_alignment(day_part: str, planting_part: str) -> str:
    if not planting_part or not day_part:
        return "Use the configured crop timing as the practical anchor."
    if planting_part.lower() == day_part.lower():
        return f"{planting_part} focus aligns with this {day_part.title()} day."
    return f"{planting_part} focus does not match this {day_part.title()} day; use plant health and schedule pressure first."


def _planting_hint_lines(day: dict[str, object], plantings: list[dict[str, object]] | None) -> list[str]:
    target_date = _parse_iso_date(day.get("date"))
    if target_date is None:
        return []
    configured = [row for row in (plantings or []) if isinstance(row, dict)]
    if not configured:
        return []
    entries = _planting_entries_for_day(plantings, target_date)
    if not entries:
        return ["Plant Plan: no configured seed, transplant, or harvest dates are close to the selected day."]

    day_part = str(day.get("dominant_plant_part") or "").strip().lower()
    lines: list[str] = []
    for entry in entries[:5]:
        planting = entry["planting"]
        label = _planting_label(planting)
        method = str(entry.get("method") or "start")
        part = str(entry.get("part") or "")
        alignment = _planting_alignment(day_part, part)
        reason = str(entry.get("reason") or "")
        days_from_start = entry.get("days_from_start") if isinstance(entry.get("days_from_start"), int) else None
        days_to_harvest = entry.get("days_to_harvest") if isinstance(entry.get("days_to_harvest"), int) else None
        if reason == "start_due":
            action = "transplant" if method == "transplant" else "sow" if method == "seed" else "start"
            lines.append(f"Plant Plan: {action.capitalize()} {label} on the selected day. {alignment}")
        elif reason == "upcoming_start":
            action = "transplant" if method == "transplant" else "sow" if method == "seed" else "start"
            days_until_start = abs(days_from_start) if days_from_start is not None else None
            phrase = _days_away_phrase(days_until_start, past="ago")
            lines.append(f"Plant Plan: {label} is scheduled to {action} {phrase}; use this day for bed, media, label, and water planning.")
        elif reason == "recent_start":
            days_since_start = max(0, days_from_start) if days_from_start is not None else None
            phrase = f"{days_since_start} day{'s' if days_since_start != 1 else ''} ago" if days_since_start is not None else "recently"
            stage = str(entry.get("stage") or "")
            stage_note = f" and is in {stage} stage" if stage in {"immature", "seedling", "veg", "maturing", "mature"} else ""
            lines.append(f"Plant Plan: {label} was started {phrase}{stage_note}; check establishment, moisture consistency, and early vigor. {alignment}")
        elif reason == "harvest_due":
            lines.append(f"Plant Plan: {label} reaches its expected harvest date on the selected day. {alignment}")
        elif reason == "upcoming_harvest":
            phrase = _days_away_phrase(days_to_harvest, past="ago")
            lines.append(f"Plant Plan: {label} is expected to harvest {phrase}; use hints for ripeness checks, irrigation restraint, and harvest logistics.")
        elif reason == "harvest_window":
            phrase = _days_away_phrase(days_to_harvest, past="past expected harvest")
            lines.append(f"Plant Plan: {label} is {phrase}; inspect maturity and quality before waiting for a better calendar window.")
        else:
            stage = str(entry.get("stage") or "general")
            lines.append(f"Plant Plan: {label} is in {stage} stage. {alignment}")

        location = str(planting.get("location") or planting.get("bed") or "").strip()
        attributes = str(planting.get("attributes") or planting.get("notes") or "").strip()
        details = "; ".join(part for part in (f"location: {location}" if location else "", attributes) if part)
        if details:
            lines.append(f"Plant Attributes: {label}: {details[:180]}")
    if len(entries) > 5:
        lines.append(f"Plant Plan: {len(entries) - 5} additional configured plantings are near this date.")
    return lines


def get_hint_lines_for_day(
    day: dict | None,
    crop_stage: str | None = None,
    plant_state: dict | None = None,
    plantings: list[dict[str, object]] | None = None,
) -> list[str]:
    lines = ["Biodynamic Hints"]
    if not isinstance(day, dict):
        lines.append("Suggestion: no biodynamic hint available for this day.")
        return lines

    part = str(day.get("dominant_plant_part") or "").strip().lower()
    sign = str(day.get("dominant_sign") or "--").strip()
    target_date = _parse_iso_date(day.get("date"))
    planting_entries = _planting_entries_for_day(plantings, target_date) if target_date is not None else []
    stage = _normalize_crop_stage(crop_stage or _stage_from_planting_entries(planting_entries))
    part_hints = {
        "root": {
            "immature": ["Suggestion: suitable for immature-stage establishment checks, gentle irrigation review, and avoiding unnecessary root disturbance."],
            "seedling": ["Suggestion: suitable for transplant timing, root establishment checks, and gentle media moisture review."],
            "veg": ["Suggestion: suitable for soil cultivation, root-zone feeding, compost teas, and drainage checks."],
            "maturing": ["Suggestion: suitable for stabilizing irrigation and avoiding unnecessary root disturbance."],
            "mature": ["Suggestion: suitable for root crop harvest, storage checks, and restrained soil work."],
            "harvest": ["Suggestion: suitable for root crop lifting, curing, sorting, and storage preparation."],
            "general": ["Suggestion: suitable for root-zone work, soil amendments, transplanting, and root crop attention."],
        },
        "leaf": {
            "immature": ["Suggestion: suitable for immature-stage establishment checks, moisture consistency, and early leaf health review."],
            "seedling": ["Suggestion: suitable for moisture consistency, gentle hardening, and early leaf health checks."],
            "veg": ["Suggestion: suitable for leafy growth support, irrigation tuning, foliar feeding, and canopy inspection."],
            "maturing": ["Suggestion: suitable for maintaining canopy function without pushing excess soft growth."],
            "mature": ["Suggestion: suitable for leafy harvest, quality checks, and disease scouting."],
            "harvest": ["Suggestion: suitable for harvesting leafy greens and herbs when freshness is the priority."],
            "general": ["Suggestion: suitable for irrigation, leafy crops, foliar feeding, and canopy health review."],
        },
        "flower": {
            "immature": ["Suggestion: suitable for immature-stage establishment checks and structure observation; avoid forcing flower-stage decisions too early."],
            "seedling": ["Suggestion: suitable for observing early vigor; avoid forcing bloom too early."],
            "veg": ["Suggestion: suitable for pruning, trellising, airflow work, and pre-flower structure."],
            "maturing": ["Suggestion: suitable for bloom support, pollinator observation, and gentle flower-stage inputs."],
            "mature": ["Suggestion: suitable for flower harvest, aroma checks, seed-set observation, and pollination review."],
            "harvest": ["Suggestion: suitable for harvesting flowers, herbs, and aromatic crops for quality."],
            "general": ["Suggestion: suitable for flowering crops, pollination, pruning for airflow, and bloom observation."],
        },
        "fruit": {
            "immature": ["Suggestion: suitable for immature-stage establishment checks, early vigor observation, and gentle training only if plants are stable."],
            "seedling": ["Suggestion: suitable for planning fruiting crop placement and checking early vigor."],
            "veg": ["Suggestion: suitable for training fruiting plants, trellising, and balanced feeding."],
            "maturing": ["Suggestion: suitable for fruit set checks, ripening support, and avoiding stress swings."],
            "mature": ["Suggestion: suitable for fruit harvest, seed saving, ripeness checks, and quality assessment."],
            "harvest": ["Suggestion: suitable for fruit, seed, and grain harvest when flavor and storage quality matter."],
            "general": ["Suggestion: suitable for fruiting crops, seed work, ripeness checks, and reproductive growth support."],
        },
    }
    if part in part_hints:
        lines.extend(part_hints[part].get(stage) or part_hints[part]["general"])
    else:
        lines.append(f"Suggestion: use {sign} Moon as a planning cue rather than a rigid rule.")

    moon_direction = str(day.get("moon_direction") or "").strip().lower()
    if moon_direction == "ascending":
        lines.append("Timing: ascending Moon emphasizes above-ground growth, grafting, harvest of aerial parts, and observation of upward sap tendency.")
    elif moon_direction == "descending":
        lines.append("Timing: descending Moon emphasizes transplanting, pruning, soil work, compost, and root-zone recovery.")

    if _truthy(day.get("lunar_node")):
        lines.append("Caution: keep lunar node windows observational; avoid major sowing, transplanting, pruning, or harvest decisions near the node.")
    if _truthy(day.get("perigee")):
        lines.append("Caution: perigee can intensify conditions; avoid high-risk interventions if plants are already stressed.")
    if _truthy(day.get("apogee")):
        lines.append("Observation: apogee favors restraint for sensitive crops; avoid over-interpreting weak plant signals.")

    state = plant_state if isinstance(plant_state, dict) else {}
    if _truthy(state.get("stress")):
        lines.append("Plant Condition: stress is reported; delay non-essential biodynamic timing actions until the plant stabilizes.")
    vpd = (
        _safe_float(state.get("vpd"))
        or _safe_float(state.get("plant_vpd"))
        or _safe_float(state.get("Plant VPD"))
        or _safe_float(state.get("ambient_vpd"))
        or _safe_float(state.get("Ambient VPD"))
    )
    if vpd is not None and vpd > 1.8:
        lines.append("Plant Condition: VPD is above 1.8; prioritize climate and irrigation correction before calendar-driven actions.")

    lines.extend(_planting_hint_lines(day, plantings))
    lines.append(_GROUNDING_REMINDER)
    return lines


def _hint_lines_for_day(day: dict | None) -> list[str]:
    return get_hint_lines_for_day(day)
