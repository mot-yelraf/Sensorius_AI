"""Convert user-entered quantities without changing device or rule storage units.

Forms carry explicit input units. Requests without units retain the historical
native-unit interpretation, including Fahrenheit-backed automation metrics.
"""

from __future__ import annotations

import math

from .saiDisplayUnits import display_conversion, normalize_display_unit_system
from .saiHomeAssistantMqtt import metric_meta_for_metric


_NOTIFICATION_UNIT_SYSTEM = "Imperial"


def set_notification_unit_system(unit_system: str) -> None:
    """Refresh the presentation preference without file I/O in switch monitors."""
    global _NOTIFICATION_UNIT_SYSTEM
    _NOTIFICATION_UNIT_SYSTEM = normalize_display_unit_system(unit_system)


TEMPERATURE_CALIBRATION_KEYS = frozenset({
    "ambient_temp_offset", "soil_temp_offset",
    "Calibration.APVPD_TEMP_CAL_VAL", "Calibration.TEMP_OFFSET",
    "Calibration.Device.TEMP_OFFSET", "Calibration.System.TEMP_OFFSET",
    "Calibration.Device.SOIL_TEMP_CAL_VAL", "Sensor.THP280_PLANT_TEMP_CAL",
})


def native_input_value(value, input_unit: str | None, native_unit: str, *, delta=False) -> float:
    """Validate and convert a submitted temperature or elevation to native units."""
    if isinstance(value, bool):
        raise ValueError("A finite numeric value is required.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("A finite numeric value is required.")
    unit = native_unit if input_unit is None else str(input_unit)
    if unit == native_unit:
        return number
    if (unit, native_unit) == ("°F", "°C"):
        result = (number - (0 if delta else 32)) * (5 / 9)
    elif (unit, native_unit) == ("°C", "°F"):
        result = number * (9 / 5) + (0 if delta else 32)
    elif (unit, native_unit) == ("ft", "m"):
        result = number * 0.3048
    else:
        raise ValueError(f"Unsupported input unit {unit!r} for {native_unit or 'this metric'}.")
    if not math.isfinite(result):
        raise ValueError("Converted value must be finite.")
    return result


def input_descriptor(metric: str, unit_system: str) -> dict:
    """Describe temperature editing while leaving other metric units native."""
    metadata = metric_meta_for_metric(metric)
    native = str(metadata.get("unit", ""))
    is_delta = metadata.get("device_class") == "temperature_delta"
    conversion = display_conversion(metric, native, unit_system) if native in {"°C", "°F"} else {
        "unit": native, "factor": 1.0, "offset": 0.0,
    }
    if is_delta:
        conversion["offset"] = 0.0
    return {"native_unit": native, "is_delta": is_delta, **conversion}


def calibration_input_offsets(offsets: list[dict]) -> list[dict]:
    """Normalize explicitly unit-tagged calibration requests before any writes."""
    normalized = []
    for item in offsets:
        key = str(item.get("key", ""))
        unit = ""
        if key in TEMPERATURE_CALIBRATION_KEYS:
            unit = "°C"
        elif key == "Calibration.Device.ALTITUDE_METERS":
            unit = "m"
        # Other offsets keep their legacy native interpretation; tagged units
        # are deliberately unsupported until their keys have an explicit mapping.
        value = native_input_value(item.get("value"), item.get("input_unit"), unit, delta=True)
        normalized.append({k: v for k, v in item.items() if k != "input_unit"} | {"value": value})
    return normalized


def calibration_presentation(offsets: list[dict], ambient: float, unit_system: str) -> dict:
    """Build calibration form values and labels in the selected display units."""
    imperial = normalize_display_unit_system(unit_system) == "Imperial"
    for item in offsets:
        key = item["key"]
        if key in TEMPERATURE_CALIBRATION_KEYS:
            item["input_unit"] = "°F" if imperial else "°C"
            item["unit"] = item["input_unit"]
            item["value"] = float(item["value"]) * (1.8 if imperial else 1)
        elif key == "Calibration.Device.ALTITUDE_METERS":
            item["input_unit"] = "ft" if imperial else "m"
            item["unit"] = item["input_unit"]
            item["value"] = float(item["value"]) / (0.3048 if imperial else 1)
    return {"temperature_input_unit": "°F" if imperial else "°C",
            "ambient_temp_offset": ambient * (1.8 if imperial else 1)}


def normalize_condition_inputs(condition: dict) -> dict:
    """Consume form unit annotations; return an evaluator-compatible condition."""
    result = dict(condition)
    descriptor = input_descriptor(str(result.get("metric", "")), "Metric")
    native = descriptor["native_unit"]
    for field, delta in (("value", False), ("hyst", True)):
        unit = result.pop(f"{field}_unit", None)
        if unit is not None:
            result[field] = native_input_value(
                result.get(field, 0), unit, native,
                delta=delta or descriptor["is_delta"],
            )
            if field == "hyst" and result[field] < 0:
                raise ValueError("Hysteresis must be non-negative.")
    return result


def display_automation_values(metric: str, actual, threshold=None, hyst=None) -> tuple:
    """Format notification quantities in preferred units without mutating rules."""
    meta = input_descriptor(metric, _NOTIFICATION_UNIT_SYSTEM)
    def convert(value, delta=False):
        if value is None:
            return None
        try:
            number = float(value) * meta["factor"] + (0 if delta else meta["offset"])
            return float(f"{number:.12g}")
        except (TypeError, ValueError):
            return value
    return convert(actual), convert(threshold), convert(hyst, True), meta["unit"]


def altitude_input_value(value, unit_system: str):
    """Present a stored elevation; leave invalid configuration visible for repair."""
    if value in (None, ""):
        return ""
    try:
        meters = native_input_value(value, None, "m")
    except (ValueError, TypeError):
        return value
    return meters / 0.3048 if normalize_display_unit_system(unit_system) == "Imperial" else value
