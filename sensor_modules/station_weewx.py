"""WeeWX station adapter helpers for SQLite and MQTT-backed data sources."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SENSOR_ID = "weewx-station"
DEFAULT_DB_PATH = "/var/lib/weewx/weewx.sdb"
DEFAULT_CONFIG_PATHS = ("/etc/weewx/weewx.conf", "/home/weewx/weewx.conf")
DEFAULT_POLL_INTERVAL_SEC = 60.0
DEFAULT_MQTT_TOPIC = "weewx/#"
DEFAULT_UPDATE_PERIOD_SEC = 300.0
INHG_TO_HPA = 33.8638866667
KPH_TO_MPH = 0.6213711922
CM_TO_IN = 0.3937007874
WEEWX_RAIN_24H_METRIC = "Rain Last 24h"

WEEWX_DISPLAY_METRICS = [
    "Temperature_F",
    "Rel-Humidity",
    "Rain",
    WEEWX_RAIN_24H_METRIC,
    "Wind Direction",
    "Baro-Pressure",
]

WEEWX_DISPLAY_STYLES = [
    "Graph24hr",
    "Graph24hr",
    "Gauge",
    "Gauge",
    "Gauge",
    "Gauge",
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
    WEEWX_RAIN_24H_METRIC: {
        "unit": "in",
        "display_precision": 2,
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
    WEEWX_RAIN_24H_METRIC: 3,
    "Rain Rate": 2,
}


@dataclass(frozen=True)
class WeeWXReading:
    """Normalized station reading using Sensorius metric names."""

    timestamp: Any
    values: dict[str, float]


@dataclass(frozen=True)
class WeeWXStationMetadata:
    """Station identity read from a local WeeWX configuration file."""

    config_path: str
    station_type: str = ""
    driver: str = ""
    model: str = ""


def _clean_weewx_config_value(value: str) -> str:
    text = str(value or "").split("#", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _parse_weewx_config_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current_section = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if line.startswith("[["):
                current_section = ""
                continue
            current_section = line.strip("[]").strip()
            if current_section:
                sections.setdefault(current_section, {})
            continue
        if not current_section or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        sections.setdefault(current_section, {})[key] = _clean_weewx_config_value(raw_value)
    return sections


def parse_weewx_station_metadata(config_text: str, *, config_path: str = "") -> WeeWXStationMetadata:
    """Parse the active WeeWX station type, driver, and model from config text."""
    sections = _parse_weewx_config_sections(config_text)
    station_type = sections.get("Station", {}).get("station_type", "").strip()
    station_section = sections.get(station_type, {}) if station_type else {}
    return WeeWXStationMetadata(
        config_path=str(config_path or "").strip(),
        station_type=station_type,
        driver=str(station_section.get("driver", "") or "").strip(),
        model=str(station_section.get("model", "") or "").strip(),
    )


def read_weewx_station_metadata(config_paths: list[str | Path] | tuple[str | Path, ...] | None = None) -> WeeWXStationMetadata | None:
    """Return local WeeWX station metadata when a readable config is present."""
    for raw_path in config_paths or DEFAULT_CONFIG_PATHS:
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_file():
            continue
        try:
            return parse_weewx_station_metadata(path.read_text(errors="replace"), config_path=str(path))
        except Exception:
            continue
    return None


def apply_weewx_station_metadata(sensor_block: dict[str, Any], metadata: WeeWXStationMetadata | None = None) -> bool:
    """Copy WeeWX station identity into a Sensorius [Sensor] block."""
    if not isinstance(sensor_block, dict):
        return False
    station = metadata or read_weewx_station_metadata()
    if station is None:
        return False

    changed = False
    for key, value in (
        ("STATION_MODEL", station.model),
        ("STATION_TYPE", station.station_type),
        ("STATION_DRIVER", station.driver),
    ):
        text = str(value or "").strip()
        if text and sensor_block.get(key) != text:
            sensor_block[key] = text
            changed = True
    return changed


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
