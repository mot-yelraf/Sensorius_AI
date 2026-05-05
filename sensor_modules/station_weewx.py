"""WeeWX station adapter helpers for SQLite and MQTT-backed data sources."""

import json
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_SENSOR_ID = "weewx-station"
DEFAULT_DB_PATH = "/var/lib/weewx/weewx.sdb"
DEFAULT_POLL_INTERVAL_SEC = 60.0
DEFAULT_MQTT_TOPIC = "weewx/#"
DEFAULT_UPDATE_PERIOD_SEC = 300.0
INHG_TO_HPA = 33.8638866667
KPH_TO_MPH = 0.6213711922
CM_TO_IN = 0.3937007874

WEEWX_DISPLAY_METRICS = [
    "Temperature_F",
    "Rel-Humidity",
    "Baro-Pressure",
    "Rain",
    "Wind Speed",
    "Wind Direction",
]

WEEWX_GAUGE_CONFIG = {
    "Wind Speed": {
        "unit": "mph",
        "min": 0,
        "max": 80,
        "ticks": [0, 5, 10, 20, 30, 40, 60, 80],
        "zones": [
            {"strokeStyle": "#66cc66", "min": 0, "max": 10},
            {"strokeStyle": "#ffcc00", "min": 10, "max": 25},
            {"strokeStyle": "#ffa500", "min": 25, "max": 40},
            {"strokeStyle": "#f00", "min": 40, "max": 80},
        ],
    },
    "Wind Direction": {
        "unit": "deg",
        "render": "compass",
        "value_metric": "Wind Speed",
        "stats_metric": "Wind Speed",
        "min": 0,
        "max": 360,
        "ticks": [0, 45, 90, 135, 180, 225, 270, 315, 360],
        "zones": [{"strokeStyle": "#add8e6", "min": 0, "max": 360}],
    },
    "Rain": {
        "unit": "in",
        "display_precision": 1,
        "min": 0,
        "max": 5,
        "ticks": [0, 0.25, 0.5, 1, 2, 3, 5],
        "zones": [
            {"strokeStyle": "#add8e6", "min": 0, "max": 1},
            {"strokeStyle": "#66b2ff", "min": 1, "max": 3},
            {"strokeStyle": "#0033cc", "min": 3, "max": 5},
        ],
    },
    "Rain Rate": {
        "unit": "in/hr",
        "min": 0,
        "max": 5,
        "ticks": [0, 0.25, 0.5, 1, 2, 3, 5],
        "zones": [
            {"strokeStyle": "#add8e6", "min": 0, "max": 0.5},
            {"strokeStyle": "#66b2ff", "min": 0.5, "max": 2},
            {"strokeStyle": "#0033cc", "min": 2, "max": 5},
        ],
    },
}

WEEWX_FIELD_MAP = {
    "outTemp": "Temperature_F",
    "outHumidity": "Rel-Humidity",
    "windSpeed": "Wind Speed",
    "windDir": "Wind Direction",
    "rain": "Rain",
    "rainRate": "Rain Rate",
    "dewpoint": "Dew Point_F",
}

WEEWX_METRIC_PRECISION = {
    "Temperature_F": 1,
    "Dew Point_F": 1,
    "Rel-Humidity": 0,
    "Baro-Pressure": 1,
    "Wind Speed": 1,
    "Wind Direction": 0,
    "Rain": 2,
    "Rain Rate": 2,
}


@dataclass(frozen=True)
class WeeWXReading:
    """Normalized station reading using Sensorius metric names."""

    timestamp: Any
    values: dict[str, float]


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except Exception:
        return None


def _lookup_case_insensitive(data: dict[str, Any], key: str) -> Any:
    if key in data:
        return data[key]
    target = key.lower()
    for raw_key, value in data.items():
        if str(raw_key or "").lower() == target:
            return value
    return None


def _c_to_f(value: float) -> float:
    return (value * 9.0 / 5.0) + 32.0


def normalize_weewx_values(data: dict[str, Any]) -> dict[str, float]:
    """Map WeeWX field names or Sensorius metric names to displayable values."""
    if not isinstance(data, dict):
        return {}

    values: dict[str, float] = {}
    for source_name, metric_name in WEEWX_FIELD_MAP.items():
        val = _to_float(_lookup_case_insensitive(data, source_name))
        if val is None:
            val = _to_float(_lookup_case_insensitive(data, metric_name))
        if val is not None:
            values[metric_name] = round(val, WEEWX_METRIC_PRECISION.get(metric_name, 2))

    metric_transforms = {
        "Temperature_F": (("outTemp_C",), _c_to_f),
        "Dew Point_F": (("dewpoint_C",), _c_to_f),
        "Wind Speed": (("windSpeed_kph",), lambda v: v * KPH_TO_MPH),
        "Rain": (("rain_cm",), lambda v: v * CM_TO_IN),
        "Rain Rate": (("rainRate_cm_per_hour",), lambda v: v * CM_TO_IN),
    }
    for metric_name, (source_names, convert) in metric_transforms.items():
        if metric_name in values:
            continue
        for source_name in source_names:
            val = _to_float(_lookup_case_insensitive(data, source_name))
            if val is not None:
                values[metric_name] = round(convert(val), WEEWX_METRIC_PRECISION.get(metric_name, 2))
                break

    pressure = _to_float(_lookup_case_insensitive(data, "barometer"))
    if pressure is not None:
        values["Baro-Pressure"] = round(pressure * INHG_TO_HPA, WEEWX_METRIC_PRECISION["Baro-Pressure"])
    else:
        pressure = _to_float(_lookup_case_insensitive(data, "barometer_mbar"))
        if pressure is None:
            pressure = _to_float(_lookup_case_insensitive(data, "Baro-Pressure"))
        if pressure is not None:
            values["Baro-Pressure"] = round(pressure, WEEWX_METRIC_PRECISION["Baro-Pressure"])

    return values


def normalize_weewx_mqtt_payload(topic: str, payload_text: str, *, base_topic: str = "") -> WeeWXReading | None:
    """
    Normalize common WeeWX MQTT outputs.

    Supports JSON object payloads and one-field topic payloads such as
    ``weewx/outTemp`` or ``weewx/archive/outTemp``.
    """
    text = str(payload_text or "").strip()
    if not text:
        return None

    topic_text = str(topic or "").strip()
    data: dict[str, Any] = {}
    timestamp = None

    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    if isinstance(obj, dict):
        data = dict(obj.get("values") if isinstance(obj.get("values"), dict) else obj)
        timestamp = (
            obj.get("dateTime")
            or obj.get("timestamp")
            or obj.get("ts")
            or obj.get("time")
        )
    else:
        base = str(base_topic or "").strip().strip("/")
        suffix = topic_text
        if base and suffix.startswith(f"{base}/"):
            suffix = suffix[len(base) + 1:]
        parts = [p for p in suffix.split("/") if p]
        field_name = parts[-1] if parts else ""
        if field_name:
            data[field_name] = text

    values = normalize_weewx_values(data)
    if not values:
        return None
    if timestamp is None:
        timestamp = _lookup_case_insensitive(data, "dateTime")
    return WeeWXReading(timestamp=timestamp, values=values)


def mqtt_topic_matches(topic_filter: str, topic: str) -> bool:
    """Small MQTT wildcard matcher for configured WeeWX topic filters."""
    filt = str(topic_filter or "").strip().strip("/")
    top = str(topic or "").strip().strip("/")
    if not filt or not top:
        return False
    pattern = re.escape(filt).replace("\\+", "[^/]+").replace("\\#", ".*")
    return re.fullmatch(pattern, top) is not None
