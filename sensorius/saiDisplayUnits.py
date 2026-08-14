"""Display-unit normalization and conversion helpers.

The helpers in this module adapt dashboard gauge metadata and values for the
user-selected display system without changing canonical sensor readings,
database history, MQTT payloads, or automation thresholds.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


DISPLAY_UNIT_SYSTEMS = ("Imperial", "Metric")
DEFAULT_DISPLAY_UNIT_SYSTEM = "Imperial"


def normalize_display_unit_system(value: object) -> str:
    """Return a supported display-unit system, defaulting to Imperial."""
    text = str(value or "").strip().lower()
    return "Metric" if text == "metric" else DEFAULT_DISPLAY_UNIT_SYSTEM


def _normalized_unit(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("³", "3")


def _temperature_is_delta(metric_name: object) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(metric_name or "").lower())
    return " deficit " in f" {text} " or " delta " in f" {text} " or "difference" in text


def display_conversion(metric_name: object, source_unit: object, unit_system: object) -> dict[str, object]:
    """Describe the affine conversion from one stored unit to a display unit."""
    target = normalize_display_unit_system(unit_system)
    unit = _normalized_unit(source_unit)
    is_delta = _temperature_is_delta(metric_name)

    if target == "Imperial":
        if unit in {"°c", "c", "degc", "celsius"}:
            return {"unit": "°F", "factor": 9.0 / 5.0, "offset": 0.0 if is_delta else 32.0}
        if unit in {"hpa", "mbar"}:
            return {"unit": "inHg", "factor": 0.0295299830714, "offset": 0.0}
        if unit in {"km/h", "kmh", "kph"}:
            return {"unit": "mph", "factor": 0.621371192237, "offset": 0.0}
        if unit in {"m/s", "mps"}:
            return {"unit": "mph", "factor": 2.23693629205, "offset": 0.0}
        if unit == "mm":
            return {"unit": "in", "factor": 1.0 / 25.4, "offset": 0.0}
        if unit in {"mm/hr", "mm/h"}:
            return {"unit": "in/hr", "factor": 1.0 / 25.4, "offset": 0.0}
        if unit == "km":
            return {"unit": "mi", "factor": 0.621371192237, "offset": 0.0}
    else:
        if unit in {"°f", "f", "degf", "fahrenheit"}:
            return {"unit": "°C", "factor": 5.0 / 9.0, "offset": 0.0 if is_delta else -32.0 * 5.0 / 9.0}
        if unit == "inhg":
            return {"unit": "hPa", "factor": 33.8638866667, "offset": 0.0}
        if unit == "mph":
            return {"unit": "km/h", "factor": 1.609344, "offset": 0.0}
        if unit in {"m/s", "mps"}:
            return {"unit": "km/h", "factor": 3.6, "offset": 0.0}
        if unit in {"in", "inch", "inches"}:
            return {"unit": "mm", "factor": 25.4, "offset": 0.0}
        if unit in {"in/hr", "in/h"}:
            return {"unit": "mm/hr", "factor": 25.4, "offset": 0.0}
        if unit in {"mi", "mile", "miles"}:
            return {"unit": "km", "factor": 1.609344, "offset": 0.0}

    return {"unit": str(source_unit or ""), "factor": 1.0, "offset": 0.0}


def convert_display_value(value: object, config: dict[str, Any] | None, *, rate: bool = False):
    """Convert one numeric value using adapted gauge metadata."""
    if value is None or isinstance(value, bool):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(numeric):
        return value
    cfg = config or {}
    factor = float(cfg.get("display_factor", 1.0) or 1.0)
    offset = 0.0 if rate else float(cfg.get("display_offset", 0.0) or 0.0)
    return numeric * factor + offset


def _convert_scale_value(value: object, factor: float, offset: float):
    if isinstance(value, bool):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(numeric):
        return value
    converted = numeric * factor + offset
    return round(converted, 6)


def apply_display_units_to_gauge_config(gauge_config: dict[str, dict], unit_system: object) -> dict[str, dict]:
    """Return a converted copy of gauge definitions for dashboard presentation."""
    adapted = copy.deepcopy(gauge_config or {})
    target = normalize_display_unit_system(unit_system)
    for metric_name, config in adapted.items():
        if not isinstance(config, dict):
            continue
        source_unit = str(config.get("source_unit") or config.get("unit") or "")
        conversion = display_conversion(metric_name, source_unit, target)
        factor = float(conversion["factor"])
        offset = float(conversion["offset"])
        config["source_unit"] = source_unit
        config["unit"] = str(conversion["unit"])
        config["display_factor"] = factor
        config["display_offset"] = offset
        config["display_unit_system"] = target
        for key in ("min", "max"):
            if key in config:
                config[key] = _convert_scale_value(config[key], factor, offset)
        if isinstance(config.get("ticks"), list):
            config["ticks"] = [_convert_scale_value(item, factor, offset) for item in config["ticks"]]
        if isinstance(config.get("zones"), list):
            for zone in config["zones"]:
                if not isinstance(zone, dict):
                    continue
                for key in ("min", "max"):
                    if key in zone:
                        zone[key] = _convert_scale_value(zone[key], factor, offset)
        if config["unit"] == "inHg":
            config["display_precision"] = max(2, int(config.get("display_precision", 1) or 1))
    return adapted
