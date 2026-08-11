"""Validate and normalize persisted calendar application data.

These helpers constrain notes and settings to the schemas expected by storage
and route code while providing safe defaults for older or partial records.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from uuid import uuid4


MAX_NOTE_LENGTH = 4000


def _safe_float(value: object) -> float | None:
    try:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        parsed = float(text)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _safe_int(value: object, *, minimum: int = 0, maximum: int = 10000) -> int | None:
    try:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        out = int(float(text))
    except Exception:
        return None
    return out if minimum <= out <= maximum else None


def _short_text(value: object, *, limit: int = 200) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit]


def _valid_date_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except Exception:
        return ""


def _normalize_note(day_iso: object, note: object) -> tuple[str, str]:
    clean_date = _valid_date_text(day_iso)
    if not clean_date:
        raise ValueError("Note date must use YYYY-MM-DD.")
    clean_text = str(note or "").strip()
    if len(clean_text) > MAX_NOTE_LENGTH:
        raise ValueError(f"Note must be {MAX_NOTE_LENGTH} characters or fewer.")
    return clean_date, clean_text


def _normalize_start_method(value: object) -> str:
    text = str(value or "").strip().lower()
    if "transplant" in text:
        return "transplant"
    if "seed" in text or "sow" in text:
        return "seed"
    return "seed"


def _normalize_plant_part(value: object) -> str:
    text = str(value or "").strip().lower()
    labels = {
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
    if text in labels:
        return labels[text]
    for token, label in labels.items():
        if token in text:
            return label
    return ""


def _normalize_planting(raw: dict[str, object], *, fallback_id: str = "") -> tuple[dict[str, object] | None, str]:
    if not isinstance(raw, dict):
        return None, "Planting must be an object."

    name = _short_text(raw.get("name") or raw.get("plant") or raw.get("crop"), limit=80)
    if not name:
        return None, "Plant name is required."

    start_date = _valid_date_text(raw.get("start_date") or raw.get("started_on") or raw.get("date"))
    if not start_date:
        return None, "Start date is required."

    expected_harvest_date = _valid_date_text(raw.get("expected_harvest_date") or raw.get("harvest_date"))
    days_to_maturity = _safe_int(raw.get("days_to_maturity"), minimum=1, maximum=730)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    if not expected_harvest_date and days_to_maturity is not None:
        expected_harvest_date = (start_dt + timedelta(days=days_to_maturity)).isoformat()
    if expected_harvest_date:
        harvest_dt = datetime.strptime(expected_harvest_date, "%Y-%m-%d").date()
        if harvest_dt < start_dt:
            return None, "Expected harvest date must be on or after the start date."
        if days_to_maturity is None:
            days_to_maturity = max(1, (harvest_dt - start_dt).days)

    planting_id = _short_text(raw.get("id"), limit=80) or fallback_id or f"planting-{uuid4().hex[:12]}"
    harvest_window_days = _safe_int(raw.get("harvest_window_days"), minimum=0, maximum=90)

    return {
        "id": planting_id,
        "name": name,
        "variety": _short_text(raw.get("variety"), limit=80),
        "plant_type": _short_text(raw.get("plant_type") or raw.get("type"), limit=80),
        "plant_part": _normalize_plant_part(raw.get("plant_part") or raw.get("biodynamic_part") or raw.get("part")),
        "start_method": _normalize_start_method(raw.get("start_method") or raw.get("method")),
        "start_date": start_date,
        "expected_harvest_date": expected_harvest_date,
        "days_to_maturity": days_to_maturity,
        "harvest_window_days": 3 if harvest_window_days is None else harvest_window_days,
        "location": _short_text(raw.get("location") or raw.get("bed"), limit=100),
        "attributes": _short_text(raw.get("attributes"), limit=240),
        "notes": _short_text(raw.get("notes"), limit=240),
    }, ""


def _truthy_text(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on"}
    return bool(value)


def _valid_lat_lon(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
    )


def _valid_altitude(value: object) -> float | None:
    altitude = _safe_float(value)
    if altitude is None:
        return None
    return altitude if -500.0 <= altitude <= 10000.0 else None
