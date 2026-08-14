"""Normalize Ecowitt LAN HTTP payloads into Sensorius station metrics.

The helpers preserve stable metric names, units, gauge definitions, sensor
identities, and rain-source settings for dashboard and persistence consumers.
"""

from __future__ import annotations

import copy
import re
from typing import Any


DEFAULT_POLL_INTERVAL_SEC = 300
MIN_POLL_INTERVAL_SEC = 60
MAX_POLL_INTERVAL_SEC = 3600
ECOWITT_DISPLAY_METRICS = [
    "Temperature_F",
    "Rel-Humidity",
    "Rain",
    "Rain Last 24h",
    "Wind Direction",
    "Gateway Baro-Pressure",
]
ECOWITT_DISPLAY_STYLES = ["Graph24hr", "Graph24hr", "Gauge", "Gauge", "Gauge", "Gauge"]


def _simple_gauge(unit: str, minimum: float, maximum: float, ticks: list[float], *, precision: int = 1) -> dict:
    return {
        "unit": unit,
        "display_precision": precision,
        "min": minimum,
        "max": maximum,
        "ticks": ticks,
        "zones": [{"strokeStyle": "#add8e6", "min": minimum, "max": maximum}],
    }


ECOWITT_GAUGE_CONFIG = {
    "Wind Gust": _simple_gauge("mph", 0, 100, [0, 10, 25, 40, 60, 80, 100]),
    "Daily Maximum Wind": _simple_gauge("mph", 0, 100, [0, 10, 25, 40, 60, 80, 100]),
    "Wind Chill": _simple_gauge("°C", -40, 60, [-40, -20, 0, 20, 40, 60]),
    "Wind Chill_F": _simple_gauge("°F", -40, 140, [-40, 0, 32, 60, 90, 120, 140]),
    "Heat Index": _simple_gauge("°C", -20, 70, [-20, 0, 20, 30, 40, 55, 70]),
    "Heat Index_F": _simple_gauge("°F", 0, 160, [0, 32, 60, 80, 100, 130, 160]),
    "Absolute Baro-Pressure": _simple_gauge("hPa", 700, 1100, [700, 800, 900, 1000, 1100]),
    "Gateway Baro-Pressure": _simple_gauge("hPa", 700, 1100, [700, 800, 900, 1000, 1100]),
    "Gateway Absolute Baro-Pressure": _simple_gauge("hPa", 700, 1100, [700, 800, 900, 1000, 1100]),
    "Solar Radiation": _simple_gauge("W/m²", 0, 1500, [0, 250, 500, 750, 1000, 1250, 1500]),
    "UV Radiation": _simple_gauge("µW/m²", 0, 2000, [0, 400, 800, 1200, 1600, 2000]),
    "UV Index": _simple_gauge("UV index", 0, 15, [0, 3, 6, 8, 11, 15]),
    "Rain Event": _simple_gauge("in", 0, 20, [0, 1, 2, 5, 10, 15, 20], precision=3),
    "Rain Day": _simple_gauge("in", 0, 20, [0, 1, 2, 5, 10, 15, 20], precision=3),
    "Rain Week": _simple_gauge("in", 0, 50, [0, 5, 10, 20, 30, 40, 50], precision=3),
    "Rain Month": _simple_gauge("in", 0, 100, [0, 10, 25, 50, 75, 100], precision=3),
    "Rain Year": _simple_gauge("in", 0, 200, [0, 25, 50, 100, 150, 200], precision=3),
    "Rain Total": _simple_gauge("in", 0, 1000, [0, 100, 250, 500, 750, 1000], precision=3),
    "PM1": _simple_gauge("µg/m³", 0, 500, [0, 50, 100, 150, 250, 350, 500]),
    "PM2.5": _simple_gauge("µg/m³", 0, 500, [0, 50, 100, 150, 250, 350, 500]),
    "PM4": _simple_gauge("µg/m³", 0, 500, [0, 50, 100, 150, 250, 350, 500]),
    "PM10": _simple_gauge("µg/m³", 0, 500, [0, 50, 100, 150, 250, 350, 500]),
    "Lightning Distance": _simple_gauge("mi", 0, 50, [0, 5, 10, 20, 30, 40, 50]),
    "Lightning Count": _simple_gauge("strikes", 0, 100, [0, 10, 25, 50, 75, 100], precision=0),
    "LDS Distance": _simple_gauge("mm", 0, 5000, [0, 1000, 2000, 3000, 4000, 5000]),
    "Leak Status": _simple_gauge("", 0, 1, [0, 1], precision=0),
    "Leaf Wetness": _simple_gauge("%", 0, 100, [0, 20, 40, 60, 80, 100]),
}


def ecowitt_gauge_config_for_metric(metric_name: Any, base_config: dict[str, dict]) -> dict | None:
    """Resolve a fixed or channel-numbered Ecowitt metric to a dashboard gauge definition."""
    name = str(metric_name or "").strip()
    if not name:
        return None
    if name in base_config:
        return copy.deepcopy(base_config[name])
    if name in ECOWITT_GAUGE_CONFIG:
        return copy.deepcopy(ECOWITT_GAUGE_CONFIG[name])
    if name.endswith(" Temperature_F"):
        template = "Temperature_F"
    elif name.endswith(" Temperature"):
        template = "Temperature"
    elif name.endswith(" Rel-Humidity"):
        template = "Rel-Humidity"
    elif name.startswith("Soil Moisture CH"):
        template = "Soil Moisture"
    elif name.startswith("Leaf Wetness CH"):
        template = "Leaf Wetness"
    elif name.startswith("PM2.5 CH"):
        template = "PM2.5"
    elif name.startswith("Soil EC CH"):
        template = "Soil EC"
    elif name.startswith("LDS CH"):
        template = "LDS Distance"
    elif name.startswith("Leak CH"):
        template = "Leak Status"
    else:
        return None
    source = base_config.get(template) or ECOWITT_GAUGE_CONFIG.get(template)
    return copy.deepcopy(source) if source else None

_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
_INVALID_SENSOR_IDS = {"FFFFFFFF", "FFFFFFFE"}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "--"}:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _unit(item: dict[str, Any], value: Any) -> str:
    explicit = str(item.get("unit", "") or "").strip().lower()
    if explicit:
        return explicit.replace("°", "")
    text = str(value or "").strip().lower().replace("°", "")
    match = re.search(r"[a-z%/²0-9]+(?:\s*/\s*[a-z]+)?\s*$", text)
    return match.group(0).replace(" ", "") if match else ""


def _temperature_c(value: float, unit: str) -> float | None:
    u = unit.lower()
    if u in {"c", "degc", "celsius"}:
        return value
    if u in {"f", "degf", "fahrenheit"}:
        return (value - 32.0) * 5.0 / 9.0
    return None


def _pressure_hpa(value: float, unit: str) -> float | None:
    u = unit.lower()
    if u in {"hpa", "mbar", "mb"}:
        return value
    if u in {"inhg", "in/hg"}:
        return value * 33.8638866667
    if u == "pa":
        return value / 100.0
    return None


def _wind_mph(value: float, unit: str) -> float | None:
    u = unit.lower()
    if u in {"mph", "mi/h"}:
        return value
    if u in {"m/s", "mps", "ms"}:
        return value * 2.2369362921
    if u in {"km/h", "kph", "kmh"}:
        return value * 0.6213711922
    if u in {"kn", "kt", "kts", "knot", "knots"}:
        return value * 1.150779448
    return None


def _rain_inches(value: float, unit: str, *, rate: bool = False) -> float | None:
    u = unit.lower().replace("hour", "h").replace("hr", "h")
    if rate:
        u = u.replace("/h", "")
    if u in {"in", "inch", "inches"}:
        return value
    if u in {"mm", "millimeter", "millimeters"}:
        return value / 25.4
    if u in {"cm"}:
        return value / 2.54
    return None


def _conductivity_ms_cm(value: float, unit: str) -> float | None:
    u = unit.lower().replace("µ", "u").replace("μ", "u").replace(" ", "")
    if u in {"ms/cm", "mscm"}:
        return value
    if u in {"us/cm", "uscm"}:
        return value / 1000.0
    return None


def _put_temperature(out: dict[str, float], base: str, value: float, unit: str) -> None:
    celsius = _temperature_c(value, unit)
    if celsius is None:
        return
    out[base] = round(celsius, 2)
    out[f"{base}_F"] = round((celsius * 9.0 / 5.0) + 32.0, 1)


def _derive_outdoor_air_metrics(out: dict[str, float]) -> None:
    """Add Sensorius absolute-humidity and VPD metrics from outdoor T/RH."""
    temperature = out.get("Temperature")
    relative_humidity = out.get("Rel-Humidity")
    if temperature is None or relative_humidity is None:
        return
    try:
        temp_c = float(temperature)
        rh = max(0.0, min(100.0, float(relative_humidity)))
        saturation_pa = 610.78 * 10 ** ((7.5 * temp_c) / (237.3 + temp_c))
        actual_pa = saturation_pa * (rh / 100.0)
        absolute_humidity = (actual_pa * 18.016) / (8314.3 * (temp_c + 273.15)) * 1000.0
        vpd = (1.0 - (rh / 100.0)) * saturation_pa / 1000.0
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return
    out["Humidity"] = round(max(0.0, absolute_humidity), 2)
    out["Ambient VPD"] = round(max(0.0, min(5.0, vpd)), 3)


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id", "") or "").strip().lower()


def _parse_common(payload: dict[str, Any], out: dict[str, float]) -> None:
    for item in payload.get("common_list") or []:
        if not isinstance(item, dict):
            continue
        item_id = _item_id(item)
        raw = item.get("val")
        value = _number(raw)
        if value is None:
            continue
        unit = _unit(item, raw)
        if item_id == "0x02":
            _put_temperature(out, "Temperature", value, unit)
        elif item_id == "0x03":
            _put_temperature(out, "Dew Point", value, unit)
        elif item_id == "0x04":
            _put_temperature(out, "Wind Chill", value, unit)
        elif item_id == "0x05":
            _put_temperature(out, "Heat Index", value, unit)
        elif item_id == "0x07":
            out["Rel-Humidity"] = round(value, 1)
        elif item_id in {"0x08", "0x09"}:
            normalized = _pressure_hpa(value, unit)
            if normalized is not None:
                out["Absolute Baro-Pressure" if item_id == "0x08" else "Baro-Pressure"] = round(normalized, 1)
        elif item_id == "0x0a":
            out["Wind Direction"] = round(value) % 360
        elif item_id in {"0x0b", "0x0c", "0x19"}:
            normalized = _wind_mph(value, unit)
            if normalized is not None:
                name = {"0x0b": "Wind Speed", "0x0c": "Wind Gust", "0x19": "Daily Maximum Wind"}[item_id]
                out[name] = round(normalized, 1)
        elif item_id == "0x15":
            if unit in {"lux", "lx"}:
                out["Light Intensity"] = round(value, 1)
            elif unit in {"w/m2", "w/m²", "wm2"}:
                out["Solar Radiation"] = round(value, 1)
        elif item_id == "0x16":
            out["UV Radiation"] = round(value, 1)
        elif item_id == "0x17":
            out["UV Index"] = round(value, 1)


def _parse_rain(items: Any, out: dict[str, float]) -> None:
    names = {
        "0x0d": "Rain Event",
        "0x0e": "Rain Rate",
        "0x10": "Rain Day",
        "0x11": "Rain Week",
        "0x12": "Rain Month",
        "0x13": "Rain Year",
        "0x14": "Rain Total",
    }
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = names.get(_item_id(item))
        raw = item.get("val")
        value = _number(raw)
        if not name or value is None:
            continue
        normalized = _rain_inches(value, _unit(item, raw), rate=name == "Rain Rate")
        if normalized is not None:
            out[name] = round(normalized, 3 if name != "Rain Rate" else 2)


def _channel(item: dict[str, Any]) -> str:
    raw = str(item.get("channel", item.get("ch", "")) or "").strip()
    return raw if raw.isdigit() else ""


def _parse_channel_arrays(payload: dict[str, Any], out: dict[str, float]) -> None:
    for item in payload.get("ch_aisle") or []:
        if not isinstance(item, dict) or not (channel := _channel(item)):
            continue
        temp = _number(item.get("temp"))
        if temp is not None:
            _put_temperature(out, f"WH31 CH{channel} Temperature", temp, _unit(item, item.get("temp")))
        humidity = _number(item.get("humidity"))
        if humidity is not None:
            out[f"WH31 CH{channel} Rel-Humidity"] = round(humidity, 1)

    for item in payload.get("ch_temp") or []:
        if not isinstance(item, dict) or not (channel := _channel(item)):
            continue
        temp = _number(item.get("temp"))
        if temp is not None:
            _put_temperature(out, f"WH34 CH{channel} Temperature", temp, _unit(item, item.get("temp")))

    for section, prefix, field in (
        ("ch_soil", "Soil Moisture CH", "humidity"),
        ("ch_leaf", "Leaf Wetness CH", "humidity"),
        ("ch_pm25", "PM2.5 CH", "PM25"),
    ):
        for item in payload.get(section) or []:
            if not isinstance(item, dict) or not (channel := _channel(item)):
                continue
            value = _number(item.get(field))
            if value is not None:
                out[f"{prefix}{channel}"] = round(value, 2)

    for item in payload.get("ch_ec") or []:
        if not isinstance(item, dict) or not (channel := _channel(item)):
            continue
        raw = next((item.get(key) for key in ("ec", "EC", "value", "val") if item.get(key) is not None), None)
        value = _number(raw)
        if value is None:
            continue
        normalized = _conductivity_ms_cm(value, _unit(item, raw))
        if normalized is not None:
            out[f"Soil EC CH{channel}"] = round(normalized, 3)

    for item in payload.get("ch_leak") or []:
        if not isinstance(item, dict) or not (channel := _channel(item)):
            continue
        status = str(item.get("status", "") or "").strip().lower()
        if status:
            out[f"Leak CH{channel}"] = 0.0 if status in {"normal", "dry", "0"} else 1.0

    for item in payload.get("ch_lds") or []:
        if not isinstance(item, dict) or not (channel := _channel(item)):
            continue
        for field, label in (("air", "Air Gap"), ("depth", "Depth")):
            value = _number(item.get(field))
            if value is not None:
                out[f"LDS CH{channel} {label}"] = round(value, 1)


def _parse_indoor_and_air(payload: dict[str, Any], out: dict[str, float]) -> None:
    wh25 = next((item for item in (payload.get("wh25") or []) if isinstance(item, dict)), None)
    if wh25:
        temp = _number(wh25.get("intemp"))
        if temp is not None:
            _put_temperature(out, "Gateway Temperature", temp, _unit(wh25, wh25.get("intemp")))
        humidity = _number(wh25.get("inhumi"))
        if humidity is not None:
            out["Gateway Rel-Humidity"] = round(humidity, 1)
        for field, name in (("abs", "Gateway Absolute Baro-Pressure"), ("rel", "Gateway Baro-Pressure")):
            value = _number(wh25.get(field))
            if value is not None:
                normalized = _pressure_hpa(value, _unit({}, wh25.get(field)))
                if normalized is not None:
                    out[name] = round(normalized, 1)

    co2 = next((item for item in (payload.get("co2") or []) if isinstance(item, dict)), None)
    if co2:
        temp = _number(co2.get("temp"))
        if temp is not None:
            _put_temperature(out, "Air Sensor Temperature", temp, _unit(co2, co2.get("temp")))
        for field, name in (
            ("humidity", "Air Sensor Rel-Humidity"), ("PM1", "PM1"), ("PM25", "PM2.5"),
            ("PM4", "PM4"), ("PM10", "PM10"), ("CO2", "CO2"),
        ):
            value = _number(co2.get(field))
            if value is not None:
                out[name] = round(value, 1)

    lightning = next((item for item in (payload.get("lightning") or []) if isinstance(item, dict)), None)
    if lightning:
        distance = _number(lightning.get("distance"))
        if distance is not None:
            unit = _unit({}, lightning.get("distance"))
            out["Lightning Distance"] = round(distance * (0.6213711922 if unit == "km" else 1.0), 1)
        count = _number(lightning.get("count"))
        if count is not None:
            out["Lightning Count"] = round(count)


def normalize_ecowitt_livedata(payload: dict[str, Any], *, rain_source: str = "traditional") -> dict[str, float]:
    """Return canonical Sensorius values from one Ecowitt live-data response."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    _parse_common(payload, out)
    selected_rain = None if rain_source == "none" else (
        payload.get("piezoRain") if rain_source == "piezo" else payload.get("rain")
    )
    _parse_rain(selected_rain, out)
    _parse_indoor_and_air(payload, out)
    _parse_channel_arrays(payload, out)
    _derive_outdoor_air_metrics(out)
    return out


def normalize_sensor_inventory(page_payloads: list[Any]) -> list[dict[str, Any]]:
    """Merge Ecowitt inventory pages and retain valid, usable sensor identities."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for payload in page_payloads:
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id", "") or "").strip()
            normalized_id = raw_id.upper().removeprefix("0X")
            signal = int(_number(item.get("signal")) or 0)
            registered = str(item.get("idst", "1") or "1").strip() != "0"
            if not normalized_id or normalized_id in _INVALID_SENSOR_IDS or not registered:
                continue
            if normalized_id == "0" and signal <= 0:
                continue
            sensor_type = str(item.get("type", "") or "").strip()
            key = (sensor_type, normalized_id)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "id": normalized_id,
                "type": sensor_type,
                "family": str(item.get("img", "") or "").strip(),
                "name": str(item.get("name", "") or "").strip() or "Ecowitt sensor",
                "battery": str(item.get("batt", "") or "").strip(),
                "signal": signal,
                "registered": True,
                "firmware": str(item.get("version", "") or "").strip(),
            })
    return result


def rain_source_from_totals(payload: dict[str, Any] | None) -> str:
    """Return the configured authoritative Ecowitt rain source."""
    priority = str((payload or {}).get("rainFallPriority", "") or "").strip()
    return {"0": "none", "1": "traditional", "2": "piezo"}.get(priority, "traditional")


def rain_reset_hour_from_totals(payload: dict[str, Any] | None) -> int:
    """Return the configured local rain-day reset hour, clamped to 0..23."""
    try:
        value = int(str((payload or {}).get("rstRainDay", "0") or "0").strip())
    except Exception:
        value = 0
    return max(0, min(23, value))


def normalized_gateway_sensor_id(mac: Any) -> str:
    """Build a stable station ID from a gateway MAC address."""
    compact = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(compact) != 12:
        raise ValueError("Gateway did not return a valid MAC address.")
    return f"ecowitt-{compact.lower()}"
