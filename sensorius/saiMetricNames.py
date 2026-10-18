"""Resolve legacy telemetry names and units for presentation queries.

Native metric identities remain available to storage, integrations and rules.
Dashboard selections and history queries can use equivalent names without
mistaking Fahrenheit observations for Celsius observations.
"""

import re


METRIC_FAMILIES = (
    ("Temperature", "Temperature_F"),
    ("Dew Point", "Dew-Point", "Dew Point_F", "Dew-Point_F"),
    ("Plant Temperature", "Plant Temperature_F"),
    ("Plant Dew Point", "Plant Dew Point_F"),
    ("Wind Chill", "Wind Chill_F"),
    ("Heat Index", "Heat Index_F"),
    ("Soil Temperature", "Soil Temp_C", "Soil-Temp", "Soil Temp_F", "Soil-Temp_F"),
    ("Baro-Pressure", "Bar-Pressure"),
    ("Plant Baro-Pressure", "Plant Bar-Pressure"),
    ("Soil Moisture", "Soil-Moisture"),
    ("Soil Moisture Deficit", "SMD"),
    ("Soil Stress Index", "SSI"),
    ("Soil pH", "Soil-pH"),
    ("Soil EC", "Soil-EC"),
    ("Dew Point Deficit", "Dewpoint Deficit", "Dewpoint Depression"),
    ("Plant Dew Point Deficit", "Plant Dewpoint Deficit"),
    ("DewVPD Risk", "dewVPD Risk"),
    ("Plant DewVPD Risk", "Plant dewVPD Risk"),
    ("Estimated PPFD", "PPFD"),
)


def _name_key(metric):
    return re.sub(r"[^a-z0-9]", "", str(metric or "").lower())


_FAMILIES = {_name_key(name): family for family in METRIC_FAMILIES for name in family}
_FAHRENHEIT = {
    _name_key(name): name for name in
    ("Temperature_F", "Dew Point_F", "Plant Temperature_F", "Plant Dew Point_F", "Soil Temp_F", "Wind Chill_F", "Heat Index_F")
}


def native_metric_name(metric: str) -> str:
    """Resolve spelling aliases while preserving native Fahrenheit identities."""
    key = _name_key(metric)
    if key in _FAHRENHEIT:
        return _FAHRENHEIT[key]
    family = _FAMILIES.get(key)
    return family[0] if family else metric


def metric_query_names(metric: str) -> tuple[str, ...]:
    """Return equivalent stored names in order of source preference."""
    family = _FAMILIES.get(_name_key(metric), (metric,))
    # NOCASE SQL already covers casing variants; do not read those rows twice.
    return tuple(dict.fromkeys(name.casefold() for name in family))


def convert_metric_value(value, source: str, target: str, *, rate=False):
    """Convert between equivalent native units, omitting offsets for rates."""
    if value is None or isinstance(value, bool):
        return value
    source_f = _name_key(source) in _FAHRENHEIT
    target_f = _name_key(target) in _FAHRENHEIT
    if source_f == target_f:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if source_f:
        return (numeric - (0 if rate else 32)) * 5 / 9
    return numeric * 9 / 5 + (0 if rate else 32)


def normalize_metric_keys(metrics: dict, requested_metrics=()) -> dict:
    """Copy values with spelling aliases resolved and selected unit views filled."""
    result = {}
    for name, value in (metrics or {}).items():
        native = native_metric_name(name)
        if result.get(native) is None or (name == native and value is not None):
            result[native] = value
    for requested in requested_metrics or ():
        target = native_metric_name(requested)
        if result.get(target) is not None:
            continue
        for source in metric_query_names(target):
            native = native_metric_name(source)
            if result.get(native) is not None:
                result[target] = convert_metric_value(result[native], native, target)
                break
    return result


def normalize_metric_stats(metrics: dict, requested_metrics=()) -> dict:
    """Combine equivalent metric statistics in each requested native unit."""
    grouped = {}
    for name, stats in (metrics or {}).items():
        family = _FAMILIES.get(_name_key(name), (name,))
        grouped.setdefault(family[0], []).append((name, stats))
    targets = dict.fromkeys(native_metric_name(name) for name in list(metrics or {}) + list(requested_metrics or ()))
    result = {}
    for target in targets:
        family = _FAMILIES.get(_name_key(target), (target,))
        parts = []
        for source, raw in grouped.get(family[0], []):
            part = dict(raw)
            for key in ("min", "avg", "max"):
                if key in part:
                    part[key] = convert_metric_value(part[key], source, target)
            if isinstance(part.get("trend"), dict):
                part["trend"] = dict(part["trend"])
                part["trend"]["rate_per_hour"] = convert_metric_value(
                    part["trend"].get("rate_per_hour"), source, target, rate=True,
                )
            parts.append((source, part))
        if not parts:
            continue
        preferred = next((part for source, part in parts if native_metric_name(source) == family[0]), parts[0][1])
        combined = dict(preferred)
        if len(parts) > 1 and all(part.get("samples") and part.get("avg") is not None for _, part in parts):
            total = sum(part["samples"] for _, part in parts)
            combined["samples"] = total
            combined["avg"] = sum(part["avg"] * part["samples"] for _, part in parts) / total
            for kind, pick in (("min", min), ("max", max)):
                valid = [part for _, part in parts if part.get(kind) is not None]
                if valid:
                    part = pick(valid, key=lambda item: item[kind])
                    combined[kind] = part[kind]
                    combined[f"{kind}_ts"] = part.get(f"{kind}_ts")
        result[target] = combined
    return result
