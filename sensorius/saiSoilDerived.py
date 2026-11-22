"""Define the Nodus soil-derived calibration contract.

The hub validates settings and presents Nodus telemetry. The firmware owns
persistent settings and computes the published indices.
"""

import math

from .saiMetricNames import normalize_metric_keys

ACTION = "soil_derived_apply"
CAPABILITY_VERSION = 1
CHANNELS = ("CH1", "CH2")
GROUPS = {
    "SoilDeficit": (
        "Soil Moisture Deficit",
        (
            ("SPD_WET_THRESHOLD_PCT", "Wet threshold", "%"),
            ("SPD_DRY_THRESHOLD_PCT", "Dry threshold", "%"),
        ),
        (("Soil Moisture", "%"), ("Soil Moisture Deficit", "%")),
    ),
    "SoilStress": (
        "Soil Stress Index",
        (
            ("SSI_TEMP_LOW_CRIT_C", "Low critical temperature", "°C"),
            ("SSI_TEMP_LOW_OK_C", "Low acceptable temperature", "°C"),
            ("SSI_TEMP_HIGH_OK_C", "High acceptable temperature", "°C"),
            ("SSI_TEMP_HIGH_CRIT_C", "High critical temperature", "°C"),
            ("SSI_MOISTURE_WEIGHT_PCT", "Moisture weight", "%"),
            ("SSI_TEMP_WEIGHT_PCT", "Temperature weight", "%"),
        ),
        (("Soil Temperature", "°C"), ("Soil Moisture Deficit", "%"), ("Soil Stress Index", "%")),
    ),
    "NPK": (
        "Soil Fertility Index",
        (
            ("N_TARGET", "Nitrogen target", "mg/kg"),
            ("P_TARGET", "Phosphorus target", "mg/kg"),
            ("K_TARGET", "Potassium target", "mg/kg"),
            ("SFI_MIN_WEIGHT_PCT", "Lowest nutrient score weight", "%"),
        ),
        (("Soil Nitrogen", "mg/kg"), ("Soil Phosphorus", "mg/kg"),
         ("Soil Potassium", "mg/kg"), ("Soil Fertility Index", "%")),
    ),
}


class SoilValidationError(ValueError):
    """Carry a soil validation message that can be shown to the operator."""

    def __init__(self, public_message):
        super().__init__(public_message)
        self.public_message = public_message


def group_values(group, raw, *, validate=False):
    """Normalize a complete group, optionally checking coupled write constraints."""
    fields = GROUPS[group][1]
    if not isinstance(raw, dict) or raw.keys() != {key for key, _, _ in fields}:
        raise SoilValidationError("Provide all fields for the selected metric.")
    values = {}
    for key, label, unit in fields:
        values[key] = _field_value(group, key, label, unit, raw[key], validate=validate)
    if validate:
        if group == "SoilDeficit" and values["SPD_WET_THRESHOLD_PCT"] <= values["SPD_DRY_THRESHOLD_PCT"]:
            raise SoilValidationError("Wet threshold must be higher than dry threshold.")
        if group == "SoilStress":
            low_critical = values["SSI_TEMP_LOW_CRIT_C"]
            low_ok = values["SSI_TEMP_LOW_OK_C"]
            high_ok = values["SSI_TEMP_HIGH_OK_C"]
            high_critical = values["SSI_TEMP_HIGH_CRIT_C"]
            if not low_critical < low_ok <= high_ok < high_critical:
                raise SoilValidationError("Temperature thresholds must be ordered from low critical to high critical.")
            if values["SSI_MOISTURE_WEIGHT_PCT"] + values["SSI_TEMP_WEIGHT_PCT"] <= 0:
                raise SoilValidationError("At least one stress weight must be greater than zero.")
    return values


def _field_value(group, key, label, unit, value, *, validate):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise SoilValidationError(f"{label} must be a finite number.")
    if validate and unit == "%" and not 0 <= value <= 100:
        raise SoilValidationError(f"{label} must be between 0 and 100%.")
    if validate and group == "NPK" and key.endswith("_TARGET") and value <= 0:
        raise SoilValidationError("Nutrient targets must be greater than zero.")
    return float(value)


def ordered_soil_updates(group, current, requested):
    """Plan changed keys so each intermediate device state passes validation."""
    remaining = {key: value for key, value in requested.items() if current[key] != value}
    updates = []
    while remaining:
        for key, value in remaining.items():
            candidate = {**current, key: value}
            try:
                group_values(group, candidate, validate=True)
            except ValueError:
                continue
            updates.append((key, value))
            current = candidate
            del remaining[key]
            break
        else:
            raise SoilValidationError("These settings cannot be saved one field at a time while keeping valid values.")
    return updates


def normalize_soil_derived(raw):
    """Read advertised groups for the single-key calibration protocol."""
    if not isinstance(raw, dict):
        return {}
    version = raw.get("version")
    if type(version) is not int or version != CAPABILITY_VERSION:
        return {}
    channels = raw.get("channels")
    if not isinstance(channels, dict):
        return {}
    result = {}
    active_channels = [channel for channel in CHANNELS if channel in channels]
    for channel in active_channels:
        row = channels[channel]
        expected_prefix = f"{channel} " if len(active_channels) > 1 else ""
        if not isinstance(row, dict) or row.get("metric_prefix") != expected_prefix:
            continue
        raw_groups = row.get("groups")
        if not isinstance(raw_groups, dict):
            continue
        groups = {}
        for group, settings in raw_groups.items():
            if group not in GROUPS or not isinstance(settings, list):
                continue
            fields = GROUPS[group][1]
            if len(settings) != len(fields):
                continue
            raw_values = {field[0]: value for field, value in zip(fields, settings)}
            try:
                groups[group] = group_values(group, raw_values)
            except (ValueError, OverflowError):
                continue
        if groups:
            result[channel] = {"metric_prefix": row["metric_prefix"], "groups": groups}
    return result


def soil_group_snapshot(group, values):
    """Encode one confirmed group in retained metadata field order."""
    return [values[key] for key, _, _ in GROUPS[group][1]]


def soil_update_value(section, key, value):
    """Validate one scalar update and return its probe, group, and effective value."""
    if not isinstance(section, str):
        raise ValueError("Select a soil calibration section.")
    group, _, channel = section.partition(".")
    if group not in GROUPS or channel not in CHANNELS:
        raise ValueError("Select a supported soil group and physical channel.")
    for setting, label, unit in GROUPS[group][1]:
        if key == setting:
            return channel, group, _field_value(group, setting, label, unit, value, validate=True)
    raise ValueError("Unknown soil calibration field.")


def soil_patch_update(patch):
    """Decode a single device-confirmed soil calibration key from a meta patch."""
    if patch.get("schema") != "nodus-meta-patch/v1" or patch.get("source") not in (
        "calibration_set", "calibration_local"
    ):
        return None
    updates = patch.get("updates")
    if not isinstance(updates, list) or len(updates) != 1 or not isinstance(updates[0], dict):
        return None
    update = updates[0]
    sensor_id = update.get("sensor_id")
    if update.get("name") != "sensor_soil.toml" or not isinstance(sensor_id, str) or not sensor_id:
        return None
    section = update.get("section")
    key = update.get("key")
    try:
        _, _, value = soil_update_value(section, key, update.get("value"))
    except (ValueError, OverflowError):
        return None
    return sensor_id, section, key, value


def soil_form_channels(channels, latest, temperature_unit):
    """Pair active settings with the last reported readings for the same probe."""
    def temperature(value):
        return value if temperature_unit == "°C" else round(value * 1.8 + 32, 10)

    result = []
    for channel, row in channels.items():
        prefix = row["metric_prefix"]
        readings = normalize_metric_keys(
            {key[len(prefix):]: value for key, value in latest.items() if key.startswith(prefix)},
            ("Soil Temperature",),
        )
        groups = []
        for group, (label, fields, metrics) in GROUPS.items():
            values = row["groups"].get(group)
            if values is None:
                continue
            display_readings = []
            for metric, unit in metrics:
                value = readings.get(metric)
                if type(value) in (int, float) and math.isfinite(value):
                    if unit == "°C":
                        value = temperature(value)
                    value = round(value, 3)
                else:
                    value = None
                display_readings.append({
                    "label": metric,
                    "unit": temperature_unit if unit == "°C" else unit,
                    "value": value,
                })
            groups.append({
                "key": group,
                "label": label,
                "fields": [
                    {"key": key, "label": field, "unit": temperature_unit if unit == "°C" else unit,
                     "value": temperature(values[key]) if unit == "°C" else values[key]}
                    for key, field, unit in fields
                ],
                "readings": display_readings,
            })
        result.append({"key": channel, "groups": groups})
    return result
