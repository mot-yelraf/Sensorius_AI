"""HTML rendering helpers, UI fragments, and dashboard display metadata.

This module centralizes Python-rendered HTML helpers used across the Sensorius
web UI. It provides shared naming and branding constants, SVG fragments, metric
canonicalization helpers, gauge configuration metadata, and other small
presentation utilities reused by routes and templates.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .saiWebContracts import SensorMap

import os
import re
from copy import deepcopy
from .saiUtils import printDM, debug_enabled, html_escape, normalize_hostname_base, mdns_hostname
from .saiBiodynamics import get_biodynamic_payload, get_skyfield_runtime_if_installed
from .sensor_modules.station_weewx import DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID, WEEWX_GAUGE_CONFIG
from .sensor_modules.station_ecowitt import ECOWITT_GAUGE_CONFIG, ecowitt_gauge_config_for_metric
from .weather_adapters.metrics import ACURITE_GAUGE_CONFIG
from .saiRainClimate import style_rain_gauges
from .saiDisplayUnits import convert_display_value
from .saiMetricNames import METRIC_FAMILIES, native_metric_name, normalize_metric_keys, normalize_metric_stats
from collections import defaultdict
from pathlib import Path
from . import __version__ as APP_VERSION

MODULE = "saiHtml"
DEBUG = debug_enabled(MODULE)

APP_TITLE = "Sensorius"
APP_NAME_SHORT = f"{APP_TITLE} AI"
APP_NAME_LONG = f"{APP_TITLE} Automatio Instrumentorum"


def dashboard_sensor_label(sensor_id: str) -> str:
    """Shorten weather station UUIDs for display without changing canonical IDs."""
    label = str(sensor_id or "").upper()
    match = re.fullmatch(r"(ECOWITT|ACURITE)-([A-Z0-9]+)", label)
    return f"{match[1]}-{match[2][-6:]}" if match else label


def _json_script(element_id: str, payload: dict) -> str:
    """Serialize inert configuration without allowing HTML script termination."""
    import json
    from .saiUtils import json_safe

    encoded = json.dumps(json_safe(payload), allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/json" id="{element_id}">{encoded}</script>'


def order_dashboard_sensor_rows(row_ids, locations):
    """Sort sensor rows by location and device ID, preserving the switch row slot."""
    sensors = sorted(
        (sid for sid in row_ids if sid != "__switches__"),
        key=lambda sid: ((str(locations.get(sid) or "").strip() or "Unknown").casefold(), sid.casefold(), sid),
    )
    ordered = iter(sensors)
    return [sid if sid == "__switches__" else next(ordered) for sid in row_ids]


def _settings_gear_svg_lines(*, indent: str = "", aria_label: str | None = "Settings", aria_hidden: bool = False):
    attrs = [
        "xmlns='http://www.w3.org/2000/svg'",
        "class='settings-gear-icon'",
        "width='21'",
        "height='21'",
        "viewBox='0 0 24 24'",
        "role='img'",
    ]
    if aria_hidden:
        attrs.append("aria-hidden='true'")
    elif aria_label:
        attrs.append(f"aria-label='{html_escape(aria_label)}'")
    attrs.extend([
        "fill='none'",
        "stroke='currentColor'",
        "stroke-width='2'",
        "stroke-linecap='round'",
        "stroke-linejoin='round'",
    ])
    return (
        f"{indent}<svg {' '.join(attrs)}>",
        f"{indent}  <circle cx='12' cy='12' r='7'></circle>",
        f"{indent}  <rect x='11' y='1' width='2' height='4' rx='1' fill='currentColor' stroke='none'></rect>",
        f"{indent}  <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none'></rect>",
        f"{indent}  <rect x='1' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'></rect>",
        f"{indent}  <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'></rect>",
        f"{indent}  <rect x='11' y='1' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'></rect>",
        f"{indent}  <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'></rect>",
        f"{indent}  <rect x='1' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'></rect>",
        f"{indent}  <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'></rect>",
        f"{indent}  <circle cx='12' cy='12' r='2.25'></circle>",
        f"{indent}</svg>",
    )

def canonicalize_metric_name(metric: str, gauge_config: dict | None = None) -> str:
    """
    Map display/storage metric aliases to the canonical gauge_config key.

    This keeps dashboard rendering stable when settings, MQTT metadata, or DB
    rows use older/spaced/cased variants of the same logical metric.
    """
    name = str(metric or "").strip()
    if not name:
        return ""

    name = native_metric_name(name)
    cfg = gauge_config or get_gauge_config()
    if name in cfg:
        return name

    aliases = {
        "PPFD": "Estimated PPFD",
        "Dewpoint Deficit": "Dew Point Deficit",
        "dewVPD Risk": "DewVPD Risk",
        "Soil-Moisture": "Soil Moisture",
    }
    aliased = aliases.get(name)
    if aliased in cfg:
        return aliased

    def _norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    target = _norm(name)
    for key in cfg.keys():
        if _norm(key) == target:
            return native_metric_name(key)

    return name

def get_gauge_config():
    """Return dashboard gauge definitions keyed by canonical metric name."""
    gauge_config = {
        "Air Quality": {"unit": "AQI", "min": 0, "max": 500, "ticks": [0, 50, 100, 150, 200, 300, 400, 500], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 50}, {"strokeStyle": "#ffcc00", "min": 50, "max": 100}, {"strokeStyle": "#ffa500", "min": 100, "max": 150}, {"strokeStyle": "#ff0000", "min": 150, "max": 200}, {"strokeStyle": "#800080", "min": 200, "max": 300}, {"strokeStyle": "#800000", "min": 300, "max": 500}]},
        "Gas": {"unit": "Ω", "min": 500, "max": 2000500, "ticks": [500, 500500, 1000500, 1500500, 2000500], "zones": [{"strokeStyle": "#f3d2fc", "min": 500, "max": 2000500}]},
        "CO2": {"unit": "ppm", "min": 0, "max": 3000, "ticks": [0, 200, 400, 800, 1200, 1600, 2000, 3000], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 200}, {"strokeStyle": "#ffcc00", "min": 200, "max": 400}, {"strokeStyle": "#66cc66", "min": 400, "max": 1600}, {"strokeStyle": "#ffcc00", "min": 1600, "max": 2000}, {"strokeStyle": "#f00", "min": 2000, "max": 3000}]},
        "Equivalent CO2": {"unit": "ppm", "min": 0, "max": 3000, "ticks": [0, 400, 800, 1200, 1600, 2000, 3000], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 200}, {"strokeStyle": "#ffcc00", "min": 200, "max": 400}, {"strokeStyle": "#66cc66", "min": 400, "max": 1600}, {"strokeStyle": "#ffcc00", "min": 1600, "max": 2000}, {"strokeStyle": "#f00", "min": 2000, "max": 3000}]},
        "TVOC": {"unit": "ppb", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#6aa9df", "min": 0, "max": 100}]},
        "VOC Index": {"unit": "index", "min": 0, "max": 500, "ticks": [0, 50, 100, 150, 200, 300, 400, 500], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 100}, {"strokeStyle": "#ffcc00", "min": 100, "max": 150}, {"strokeStyle": "#ffa500", "min": 150, "max": 200}, {"strokeStyle": "#f00", "min": 200, "max": 300}, {"strokeStyle": "#800080", "min": 300, "max": 400}, {"strokeStyle": "#800000", "min": 400, "max": 500}]},
        "NOx Index": {"unit": "index", "min": 0, "max": 500, "ticks": [0, 50, 100, 150, 200, 300, 400, 500], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 50}, {"strokeStyle": "#ffcc00", "min": 50, "max": 100}, {"strokeStyle": "#ffa500", "min": 100, "max": 150}, {"strokeStyle": "#f00", "min": 150, "max": 200}, {"strokeStyle": "#800080", "min": 200, "max": 300}, {"strokeStyle": "#800000", "min": 300, "max": 500}]},
        "Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Dew Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Dew Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Dew Point Deficit": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Ambient VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Baro-Pressure": {"unit": "hPa", "display_precision": 1, "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
        "Temperature_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Plant Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Plant Dew Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Dew Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Dew Point Deficit": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "Plant DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Plant VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Plant Baro-Pressure": {"unit": "hPa", "display_precision": 1, "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
        "Plant Temperature_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Soil Moisture": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 50}, {"strokeStyle": "#add8e6", "min": 50, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Soil Temp_C": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Soil Temp_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Soil pH": {"unit": "pH", "min": 0, "max": 10, "ticks": [1, 3, 5, 7, 9], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 4.5}, {"strokeStyle": "#3399ff", "min": 4.5, "max": 5.5}, {"strokeStyle": "#66cc66", "min": 5.5, "max": 6.5}, {"strokeStyle": "#ffcc00", "min": 6.5, "max": 7.5}, {"strokeStyle": "#f00", "min": 7.5, "max": 8.5}, {"strokeStyle": "#800000", "min": 8.5, "max": 10}]},
        "Soil EC": {"unit": "mS/cm", "min": 0, "max": 10, "ticks": [0, 2, 4, 6, 8, 10], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 0.8}, {"strokeStyle": "#3399ff", "min": 0.8, "max": 1.8}, {"strokeStyle": "#66cc66", "min": 1.8, "max": 2.5}, {"strokeStyle": "#ffcc00", "min": 2.5, "max": 4.0}, {"strokeStyle": "#800000", "min": 4.0, "max": 10}]},
        "Soil Nitrogen": {"unit": "mg/kg", "min": 0, "max": 150, "ticks": [0, 25, 50, 75, 100, 125, 150], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 25}, {"strokeStyle": "#ffcc00", "min": 25, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 125}, {"strokeStyle": "#3399ff", "min": 125, "max": 150}]},
        "Soil Phosphorus": {"unit": "mg/kg", "min": 0, "max": 60, "ticks": [0, 20, 30, 40, 50, 60], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 36}, {"strokeStyle": "#66cc66", "min": 36, "max": 50}, {"strokeStyle": "#3399ff", "min": 50, "max": 60}]},
        "Soil Potassium": {"unit": "mg/kg", "min": 0, "max": 200, "ticks": [0, 60, 100, 130, 150, 175, 200], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 60}, {"strokeStyle": "#ffcc00", "min": 60, "max": 131}, {"strokeStyle": "#66cc66", "min": 131, "max": 175}, {"strokeStyle": "#3399ff", "min": 175, "max": 200}]},
        "Soil Fertility Index": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 25, 50, 75, 100], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 50}, {"strokeStyle": "#ffcc00", "min": 50, "max": 75}, {"strokeStyle": "#66cc66", "min": 75, "max": 100}]},
        "Soil Moisture Deficit": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#3399ff", "min": 0, "max": 20}, {"strokeStyle": "#03a603", "min": 20, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Soil Stress Index": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#03a603", "min": 0, "max": 30}, {"strokeStyle": "#bf9000", "min": 30, "max": 60}, {"strokeStyle": "#cc7a00", "min": 60, "max": 80}, {"strokeStyle": "#d9534f", "min": 80, "max": 100}]},
        "Light Intensity": {"unit": "lux", "min": 0,  "max": 120000, "ticks": [0, 20000, 40000, 60000, 80000, 100000, 120000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 120000}]},
        "Auto Light": {"unit": "lux", "min": 0,  "max": 120000, "ticks": [0, 20000, 40000, 60000, 80000, 100000, 120000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 120000}]},
        "Estimated PPFD": {"unit": "µmol·m⁻²·s⁻¹", "min": 0, "max": 2000, "ticks": [0, 400, 800, 1200, 1600, 2000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 2000}]},
        "Visible Light Intensity": {"unit": "mol·m⁻²·day⁻¹", "min": 0, "max": 70, "ticks": [0, 10, 20, 30, 40, 50, 60, 70], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 70}]},
    }
    gauge_config["Soil Temperature"] = deepcopy(gauge_config["Soil Temp_C"])
    gauge_config.update(WEEWX_GAUGE_CONFIG)
    gauge_config.update(ECOWITT_GAUGE_CONFIG)
    gauge_config.update(ACURITE_GAUGE_CONFIG)
    # Full-screen graph selectors retain the native names stored in history.
    for family in METRIC_FAMILIES:
        for name in family:
            native = native_metric_name(name)
            if name not in gauge_config and native in gauge_config:
                gauge_config[name] = deepcopy(gauge_config[native])
    return style_rain_gauges(gauge_config)


def extend_gauge_config_for_metrics(gauge_config: dict, metric_names) -> dict:
    """Add supported channel-specific Ecowitt gauges to one dashboard configuration."""
    for metric_name in metric_names or []:
        name = str(metric_name or "").strip()
        if not name or name in gauge_config:
            continue
        config = ecowitt_gauge_config_for_metric(name, gauge_config)
        if config:
            gauge_config[name] = config
    return gauge_config


def _trend_arrow_html(metric: str, trend: dict | None) -> str:
    trend = trend or {}
    return (
        "<span class='trend-arrow' data-metric='{}' data-rate='{}' "
        "data-samples='{}' data-window='{}' data-provisional='{}' "
        "role='img' tabindex='0'>"
        "<svg viewBox='0 0 32 32' aria-hidden='true' focusable='false'>"
        "<path d='M2 16H29M22 10L29 16L22 22'/></svg></span>"
    ).format(
        html_escape(metric),
        html_escape(trend.get("rate_per_hour", "")),
        html_escape(trend.get("samples", 0)),
        html_escape(trend.get("window_s", 0)),
        "1" if trend.get("provisional") else "0",
    )


DASHBOARD_BACKGROUND_THEMES = frozenset({"leaf", "root", "leaf_crop", "flower", "fruit"})
DASHBOARD_METRIC_SETS = frozenset({"Pick 6", "All"})


def normalize_dashboard_background_theme(value: object) -> str:
    """Return a supported dashboard background theme, defaulting to leaf."""
    theme = str(value or "").strip().lower().replace("-", "_")
    return theme if theme in DASHBOARD_BACKGROUND_THEMES else "leaf"


def normalize_dashboard_metric_set(value: object) -> str:
    """Return a supported dashboard metric set, defaulting to Pick 6."""
    compact = str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    return "All" if compact in {"all", "showall"} else "Pick 6"


def render_dashboard(sensor_id, sensor, available, all_values, all_stats, mqtt_ingest, switch_controllers=None, sensor_locations=None, gauge_config=None, gauge_size="Small", expected_gauge_map=None, expected_display_style_map=None, display_style=None, astro_payload=None, biodynamic_payload=None, weather_forecast_provider="met_no", weather_forecast_theme="pollinator", dashboard_background_theme="leaf", dashboard_metric_set="Pick 6", dashboard_custom_theme_style="", pressure_altitude=None, pressure_sensor_context=None, battery_statuses=None, dashboard_row_order=None, switch_row_order="location_device", switch_card_order=None, runtime_sensor_map: "SensorMap | None" = None, sensor_statuses=None, metric_tile_style_map=None, switch_tile_style_map=None):
    """Yield the complete Sensorius dashboard HTML document."""

    requested = expected_gauge_map if expected_gauge_map is not None else getattr(mqtt_ingest, "expected_gauge_map", {})
    all_values = {sid: normalize_metric_keys(metrics, requested.get(sid, ())) for sid, metrics in all_values.items()}
    all_stats = {sid: normalize_metric_stats(metrics, requested.get(sid, ())) for sid, metrics in all_stats.items()}

    import json
    import os
    import re
    import sys
    import math
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from collections import defaultdict
    from .saiUtils import get_timestamp
    from .saiSettings import saiSettings
    from .saiWeatherForecast import normalize_weather_forecast_provider
    from . import saiAddDevice
    from .saiSensorSettingsManager import SensorSettingsManager, METRIC_TILE_STYLES
    from .saiHtml import render_graph_modal
    try:
        from astral import LocationInfo
        from astral.sun import sun as _astral_sun, elevation as _astral_elevation, azimuth as _astral_azimuth
        from astral import moon as _astral_moon
        from astral.sidereal import lmst as _astral_lmst
    except Exception:
        LocationInfo = None
        _astral_sun = None
        _astral_elevation = None
        _astral_azimuth = None
        _astral_moon = None
        _astral_lmst = None
    if isinstance(switch_controllers, dict):
        switch_controllers = {
            (k if isinstance(k, str) else str(k)).lower(): v
            for k, v in switch_controllers.items()
        }
    switch_installed = any(
        ctrl.is_present for ctrl in switch_controllers.values()
    ) if isinstance(switch_controllers, dict) else (
        switch_controllers.is_present if switch_controllers else False
    )

    # Date "no data" warning is useful on Pi hardware, but noisy on non-Pi hosts
    # where local sensor loops are intentionally absent.
    pi_model_path = "/proc/device-tree/model"
    is_pi_platform = False
    try:
        if sys.platform.startswith("linux") and os.path.exists(pi_model_path):
            model = open(pi_model_path, "r", encoding="utf-8", errors="ignore").read().lower()
            is_pi_platform = "raspberry pi" in model
    except Exception:
        is_pi_platform = False

    def _traditional_full_moon_name(phase_date) -> str:
        names_by_month = {
            1: "Wolf Moon",
            2: "Snow Moon",
            3: "Worm Moon",
            4: "Pink Moon",
            5: "Flower Moon",
            6: "Strawberry Moon",
            7: "Buck Moon",
            8: "Sturgeon Moon",
            9: "Harvest Moon",
            10: "Hunter's Moon",
            11: "Beaver Moon",
            12: "Cold Moon",
        }
        return names_by_month.get(getattr(phase_date, "month", None), "Full Moon")

    def _moon_phase_name(phase_val: float, phase_date=None) -> str:
        p = phase_val % 28.0

        def _circular_dist(a: float, b: float, cycle: float = 28.0) -> float:
            d = abs(a - b) % cycle
            return min(d, cycle - d)

        if _circular_dist(p, 0.0) <= 1.0:
            return "New Moon"
        if _circular_dist(p, 7.0) <= 1.0:
            return "1st Quarter"
        if _circular_dist(p, 14.0) <= 1.0:
            return _traditional_full_moon_name(phase_date)
        if _circular_dist(p, 21.0) <= 1.0:
            return "3rd Quarter"
        if 1.0 < p < 6.0:
            return "Waxing Crescent"
        if 8.0 < p < 13.0:
            return "Waxing Gibbous"
        if 15.0 < p < 20.0:
            return "Waning Gibbous"
        return "Waning Crescent"

    def _moon_local_canvas_angle(moon_az: float, moon_el: float, sun_az: float, sun_el: float) -> float | None:
        def _unit_from_az_el(az_deg: float, el_deg: float) -> tuple[float, float, float]:
            az = math.radians(az_deg)
            el = math.radians(el_deg)
            return (
                math.cos(el) * math.sin(az),
                math.cos(el) * math.cos(az),
                math.sin(el),
            )

        def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
            return (a[0] * b[0]) + (a[1] * b[1]) + (a[2] * b[2])

        def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
            return (
                (a[1] * b[2]) - (a[2] * b[1]),
                (a[2] * b[0]) - (a[0] * b[2]),
                (a[0] * b[1]) - (a[1] * b[0]),
            )

        def _normalized(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
            mag = math.sqrt(_dot(v, v))
            if not math.isfinite(mag) or mag < 1e-9:
                return None
            return (v[0] / mag, v[1] / mag, v[2] / mag)

        if not all(math.isfinite(v) for v in (moon_az, moon_el, sun_az, sun_el)):
            return None

        moon_vec = _unit_from_az_el(moon_az, moon_el)
        sun_vec = _unit_from_az_el(sun_az, sun_el)
        bright_vec = _normalized(tuple(sun_vec[i] - (_dot(sun_vec, moon_vec) * moon_vec[i]) for i in range(3)))
        if bright_vec is None:
            return None

        zenith = (0.0, 0.0, 1.0)
        screen_up = _normalized(tuple(zenith[i] - (_dot(zenith, moon_vec) * moon_vec[i]) for i in range(3)))
        if screen_up is None:
            north = (0.0, 1.0, 0.0)
            screen_up = _normalized(tuple(north[i] - (_dot(north, moon_vec) * moon_vec[i]) for i in range(3)))
        if screen_up is None:
            return None

        screen_right = _normalized(_cross(moon_vec, screen_up))
        if screen_right is None:
            return None

        canvas_x = _dot(bright_vec, screen_right)
        canvas_y = -_dot(bright_vec, screen_up)
        return (math.degrees(math.atan2(canvas_y, canvas_x)) + 360.0) % 360.0

    def _build_astro_payload() -> dict:
        out = {
            "ok": False,
            "lat": None,
            "lon": None,
            "tz": "",
            "sunrise": "",
            "sunset": "",
            "sun_noon": "",
            "sun_points": [],
            "moon_points": [],
            "moon_phase_value": None,
            "moon_phase_label": "",
            "moon_lit_pct": None,
            "moon_rise": "",
            "moon_set": "",
            "moon_rise_today": "",
            "moon_set_today": "",
            "moon_declination": None,
            "moon_position_source": "",
            "moon_next_phase_label": "",
            "moon_next_phase_date": "",
            "moon_visible_angle": None,
            "moon_reference_angle": None,
            "position_29d": [],
        }
        if (
            LocationInfo is None
            or _astral_sun is None
            or _astral_elevation is None
            or _astral_azimuth is None
            or _astral_moon is None
            or _astral_lmst is None
        ):
            return out

        def _safe_float(v):
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except Exception:
                return None

        def _hm_for_minute(day_start, minute):
            if minute >= 1440:
                return "24:00"
            return (day_start + timedelta(minutes=minute)).strftime("%H:%M")

        resolved_lat = None
        resolved_lon = None
        resolved_tz = ""
        try:
            s = saiSettings(apply_live=False)
            resolved = s.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5)
            resolved_lat = resolved.get("lat")
            resolved_lon = resolved.get("lon")
            resolved_altitude = _safe_float(resolved.get("altitude"))
            resolved_tz = str(resolved.get("tz") or "").strip()
        except Exception:
            return out

        if resolved_lat is None or resolved_lon is None or not resolved_tz:
            return out

        try:
            tzinfo = ZoneInfo(resolved_tz)
            now_local = datetime.now(tzinfo)
            obs = LocationInfo(
                name="sensorius",
                region="local",
                timezone=resolved_tz,
                latitude=resolved_lat,
                longitude=resolved_lon,
            ).observer
            if resolved_altitude is not None:
                obs.elevation = resolved_altitude
            sun_map = _astral_sun(obs, date=now_local.date(), tzinfo=tzinfo)
            sunrise = sun_map.get("sunrise")
            sunset = sun_map.get("sunset")
            noon = sun_map.get("noon")
            if not isinstance(sunrise, datetime) or not isinstance(sunset, datetime):
                return out

            day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            pts = []
            for minute in range(0, 1441, 10):
                sample_dt = day_start + timedelta(minutes=minute)
                try:
                    elev = float(_astral_elevation(obs, sample_dt))
                except Exception:
                    elev = float("nan")
                if math.isfinite(elev):
                    pts.append({"m": minute, "t": _hm_for_minute(day_start, minute), "e": round(elev, 2)})

            moon_val = float(_astral_moon.phase(now_local.date()))
            moon_lit_pct = int(round((0.5 * (1 - math.cos((2 * math.pi * (moon_val % 28.0)) / 28.0))) * 100))
            moon_points = []
            moon_declination = None
            moon_position_source = ""
            try:
                skyfield_runtime = get_skyfield_runtime_if_installed()
                if skyfield_runtime is not None:
                    _loader, ts, eph, _constellation_at = skyfield_runtime
                    from skyfield.api import wgs84

                    topo = wgs84.latlon(
                        float(resolved_lat),
                        float(resolved_lon),
                        elevation_m=float(resolved_altitude or 0.0),
                    )
                    observer = eph["earth"] + topo
                    moon_body = eph["moon"]
                    for minute in range(0, 1441, 10):
                        sample_dt = day_start + timedelta(minutes=minute)
                        t = ts.from_datetime(sample_dt.astimezone(timezone.utc))
                        apparent = observer.at(t).observe(moon_body).apparent()
                        alt, az, _distance = apparent.altaz()
                        _ra, dec, _radec_distance = apparent.radec()
                        elev = float(alt.degrees)
                        azimuth = float(az.degrees)
                        declination = float(dec.degrees)
                        if all(math.isfinite(v) for v in (elev, azimuth, declination)):
                            moon_points.append({
                                "m": minute,
                                "t": _hm_for_minute(day_start, minute),
                                "e": round(elev, 2),
                                "az": round(azimuth, 2),
                                "d": round(declination, 2),
                            })
                    now_t = ts.from_datetime(now_local.astimezone(timezone.utc))
                    now_apparent = observer.at(now_t).observe(moon_body).apparent()
                    _now_ra, now_dec, _now_dist = now_apparent.radec()
                    now_declination = float(now_dec.degrees)
                    if math.isfinite(now_declination):
                        moon_declination = round(now_declination, 2)
                    if moon_points:
                        moon_position_source = "skyfield"
            except Exception:
                moon_points = []
                moon_declination = None
                moon_position_source = ""
            if not moon_points:
                try:
                    moon_az_fn = getattr(_astral_moon, "azimuth", None)
                    moon_el_fn = getattr(_astral_moon, "elevation", None)
                    if callable(moon_az_fn) and callable(moon_el_fn):
                        for minute in range(0, 1441, 10):
                            sample_dt = day_start + timedelta(minutes=minute)
                            sample_utc = sample_dt.astimezone(timezone.utc)
                            elev = float(moon_el_fn(obs, sample_utc))
                            azimuth = float(moon_az_fn(obs, sample_utc))
                            if all(math.isfinite(v) for v in (elev, azimuth)):
                                moon_points.append({
                                    "m": minute,
                                    "t": _hm_for_minute(day_start, minute),
                                    "e": round(elev, 2),
                                    "az": round(azimuth, 2),
                                })
                    if moon_points:
                        moon_position_source = "astral"
                except Exception:
                    moon_points = []
                    moon_position_source = ""
            moon_visible_angle = None
            moon_reference_angle = None
            try:
                moon_az_fn = getattr(_astral_moon, "azimuth", None)
                moon_el_fn = getattr(_astral_moon, "elevation", None)
                moon_obs_dt = now_local.astimezone(timezone.utc)
                moon_az = float(moon_az_fn(obs, moon_obs_dt)) if callable(moon_az_fn) else float("nan")
                moon_el = float(moon_el_fn(obs, moon_obs_dt)) if callable(moon_el_fn) else float("nan")
                sun_az = float(_astral_azimuth(obs, now_local))
                sun_el = float(_astral_elevation(obs, now_local))
                moon_pos = _astral_moon.moon_position(_astral_moon.julianday_2000(moon_obs_dt))
                moon_ra = float(moon_pos.right_ascension)
                moon_dec = float(moon_pos.declination)

                if all(math.isfinite(v) for v in (moon_az, moon_el, sun_az, sun_el)):
                    lat_rad = math.radians(float(resolved_lat))
                    moon_az_rad = math.radians(moon_az)
                    moon_el_rad = math.radians(moon_el)
                    sun_az_rad = math.radians(sun_az)
                    sun_el_rad = math.radians(sun_el)

                    sin_sun_dec = (
                        (math.sin(sun_el_rad) * math.sin(lat_rad))
                        + (math.cos(sun_el_rad) * math.cos(lat_rad) * math.cos(sun_az_rad))
                    )
                    sun_dec = math.asin(max(-1.0, min(1.0, sin_sun_dec)))
                    sun_hour_angle = math.atan2(
                        -math.sin(sun_az_rad) * math.cos(sun_el_rad),
                        (math.sin(sun_el_rad) * math.cos(lat_rad))
                        - (math.cos(sun_el_rad) * math.sin(lat_rad) * math.cos(sun_az_rad)),
                    )
                    lst_rad = math.radians(float(_astral_lmst(now_local, float(resolved_lon))))
                    sun_ra = (lst_rad - sun_hour_angle) % (2 * math.pi)

                    chi_num = math.cos(sun_dec) * math.sin(sun_ra - moon_ra)
                    chi_den = (
                        (math.sin(sun_dec) * math.cos(moon_dec))
                        - (math.cos(sun_dec) * math.sin(moon_dec) * math.cos(sun_ra - moon_ra))
                    )
                    bright_limb_angle = math.degrees(math.atan2(chi_num, chi_den)) % 360.0

                    parallactic_angle = math.degrees(
                        math.atan2(
                            math.sin(moon_az_rad),
                            (math.tan(lat_rad) * math.cos(moon_el_rad))
                            - (math.sin(moon_el_rad) * math.cos(moon_az_rad)),
                        )
                    )

                    moon_reference_angle = round(bright_limb_angle, 2)
                    local_canvas_angle = _moon_local_canvas_angle(moon_az, moon_el, sun_az, sun_el)
                    if local_canvas_angle is not None:
                        moon_visible_angle = round(local_canvas_angle, 2)
                    else:
                        moon_visible_angle = round((bright_limb_angle + parallactic_angle) % 360.0, 2)
            except Exception:
                moon_visible_angle = None
                moon_reference_angle = None

            moon_rise = ""
            moon_set = ""
            moon_rise_today = ""
            moon_set_today = ""
            try:
                mr_fn = getattr(_astral_moon, "moonrise", None)
                ms_fn = getattr(_astral_moon, "moonset", None)

                def _event_for_day(fn, d):
                    if not callable(fn):
                        return ""
                    try:
                        ev = fn(obs, date=d, tzinfo=tzinfo)
                    except Exception:
                        return ""
                    if not isinstance(ev, datetime):
                        return ""
                    if ev.tzinfo is None:
                        ev = ev.replace(tzinfo=tzinfo)
                    else:
                        ev = ev.astimezone(tzinfo)
                    return ev.strftime("%H:%M") if ev.date() == d else ""

                def _pick_nearest_event(fn):
                    if not callable(fn):
                        return ""
                    candidates = []
                    for offset in (-1, 0, 1, 2):
                        d = now_local.date() + timedelta(days=offset)
                        try:
                            ev = fn(obs, date=d, tzinfo=tzinfo)
                        except Exception:
                            continue
                        if isinstance(ev, datetime):
                            if ev.tzinfo is None:
                                ev = ev.replace(tzinfo=tzinfo)
                            candidates.append(ev)
                    if not candidates:
                        return ""
                    future = [ev for ev in candidates if ev >= now_local]
                    chosen = min(future) if future else max(candidates)
                    return chosen.strftime("%H:%M")

                moon_rise_today = _event_for_day(mr_fn, now_local.date())
                moon_set_today = _event_for_day(ms_fn, now_local.date())
                moon_rise = _pick_nearest_event(mr_fn)
                moon_set = _pick_nearest_event(ms_fn)
            except Exception:
                moon_rise = ""
                moon_set = ""
                moon_rise_today = ""
                moon_set_today = ""

            moon_next_phase_label = ""
            moon_next_phase_date = ""
            try:
                phase_targets = (
                    ("New Moon", 0.0),
                    ("1st Quarter", 7.0),
                    ("Full Moon", 14.0),
                    ("3rd Quarter", 21.0),
                )
                phase_cycle = 28.0
                current_phase = moon_val % phase_cycle
                for label, target in phase_targets:
                    if current_phase < target:
                        moon_next_phase_label = label
                        break
                if not moon_next_phase_label:
                    moon_next_phase_label = "New Moon"
                target_phase = next(target for label, target in phase_targets if label == moon_next_phase_label)

                best_date = None
                best_key = None
                for day_offset in range(-15, 17):
                    d = now_local.date() + timedelta(days=day_offset)
                    try:
                        pv = float(_astral_moon.phase(d)) % phase_cycle
                    except Exception:
                        continue
                    dist = abs(pv - target_phase)
                    dist = min(dist, phase_cycle - dist)
                    # Prefer the closest phase match in the current lunation window.
                    # If two dates are equally close, prefer an upcoming date.
                    candidate_key = (dist, abs(day_offset), day_offset < 0)
                    if best_key is None or candidate_key < best_key:
                        best_key = candidate_key
                        best_date = d
                if best_date is not None:
                    moon_next_phase_date = best_date.isoformat()
                    if moon_next_phase_label == "Full Moon":
                        moon_next_phase_label = _traditional_full_moon_name(best_date)
            except Exception:
                moon_next_phase_label = ""
                moon_next_phase_date = ""

            position_29d = []
            try:
                sample_minutes = range(0, 1441, 120)
                range_start = day_start
                moon_el_fn = getattr(_astral_moon, "elevation", None)
                position_ts = None
                position_observer = None
                position_moon_body = None
                try:
                    skyfield_runtime = get_skyfield_runtime_if_installed()
                    if skyfield_runtime is not None:
                        _loader, position_ts, position_eph, _constellation_at = skyfield_runtime
                        from skyfield.api import wgs84

                        topo = wgs84.latlon(
                            float(resolved_lat),
                            float(resolved_lon),
                            elevation_m=float(resolved_altitude or 0.0),
                        )
                        position_observer = position_eph["earth"] + topo
                        position_moon_body = position_eph["moon"]
                except Exception:
                    position_ts = None
                    position_observer = None
                    position_moon_body = None

                for day_offset in range(29):
                    graph_day_start = range_start + timedelta(days=day_offset)
                    sun_day = []
                    moon_day = []
                    graph_date = graph_day_start.date()
                    graph_moon_phase = None
                    graph_moon_lit_pct = None
                    graph_moon_visible_angle = None
                    for minute in sample_minutes:
                        sample_dt = graph_day_start + timedelta(minutes=minute)
                        try:
                            sun_elev = float(_astral_elevation(obs, sample_dt))
                        except Exception:
                            sun_elev = float("nan")
                        if math.isfinite(sun_elev):
                            sun_day.append([minute, round(sun_elev, 2)])

                        moon_elev = float("nan")
                        try:
                            if position_ts is not None and position_observer is not None and position_moon_body is not None:
                                t = position_ts.from_datetime(sample_dt.astimezone(timezone.utc))
                                apparent = position_observer.at(t).observe(position_moon_body).apparent()
                                alt, _az, _distance = apparent.altaz()
                                moon_elev = float(alt.degrees)
                            elif callable(moon_el_fn):
                                moon_elev = float(moon_el_fn(obs, sample_dt.astimezone(timezone.utc)))
                        except Exception:
                            moon_elev = float("nan")
                        if math.isfinite(moon_elev):
                            moon_day.append([minute, round(moon_elev, 2)])

                    try:
                        graph_moon_phase = float(_astral_moon.phase(graph_date))
                        graph_moon_lit_pct = int(
                            round((0.5 * (1 - math.cos((2 * math.pi * (graph_moon_phase % 28.0)) / 28.0))) * 100)
                        )
                    except Exception:
                        graph_moon_phase = None
                        graph_moon_lit_pct = None

                    try:
                        moon_az_fn = getattr(_astral_moon, "azimuth", None)
                        moon_el_fn_for_angle = getattr(_astral_moon, "elevation", None)
                        graph_moon_dt = graph_day_start.astimezone(timezone.utc)
                        graph_moon_az = float(moon_az_fn(obs, graph_moon_dt)) if callable(moon_az_fn) else float("nan")
                        graph_moon_el = (
                            float(moon_el_fn_for_angle(obs, graph_moon_dt))
                            if callable(moon_el_fn_for_angle)
                            else float("nan")
                        )
                        graph_sun_az = float(_astral_azimuth(obs, graph_day_start))
                        graph_sun_el = float(_astral_elevation(obs, graph_day_start))
                        graph_angle = _moon_local_canvas_angle(graph_moon_az, graph_moon_el, graph_sun_az, graph_sun_el)
                        if graph_angle is not None:
                            graph_moon_visible_angle = round(graph_angle, 2)
                    except Exception:
                        graph_moon_visible_angle = None

                    position_29d.append({
                        "date": graph_date.isoformat(),
                        "label": graph_day_start.strftime("%b%d"),
                        "sun": sun_day,
                        "moon": moon_day,
                        "moon_phase_value": round(graph_moon_phase, 2) if graph_moon_phase is not None else None,
                        "moon_lit_pct": graph_moon_lit_pct,
                        "moon_visible_angle": graph_moon_visible_angle,
                    })
            except Exception:
                position_29d = []

            out.update({
                "ok": True,
                "lat": round(resolved_lat, 6),
                "lon": round(resolved_lon, 6),
                "tz": resolved_tz,
                "sunrise": sunrise.strftime("%H:%M"),
                "sunset": sunset.strftime("%H:%M"),
                "sun_noon": noon.strftime("%H:%M") if isinstance(noon, datetime) else "",
                "sun_points": pts,
                "moon_points": moon_points,
                "moon_phase_value": round(moon_val, 2),
                "moon_phase_label": _moon_phase_name(moon_val, now_local.date()),
                "moon_lit_pct": moon_lit_pct,
                "moon_rise": moon_rise,
                "moon_set": moon_set,
                "moon_rise_today": moon_rise_today,
                "moon_set_today": moon_set_today,
                "moon_declination": moon_declination,
                "moon_position_source": moon_position_source,
                "moon_next_phase_label": moon_next_phase_label,
                "moon_next_phase_date": moon_next_phase_date,
                "moon_visible_angle": moon_visible_angle,
                "moon_reference_angle": moon_reference_angle,
                "position_29d": position_29d,
            })
            return out
        except Exception:
            return out

    if astro_payload is None:
        astro_payload = _build_astro_payload()
    if biodynamic_payload is None:
        biodynamic_payload = get_biodynamic_payload()
    
    def _safe(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", s)
        # ...existing imports at top of render_dashboard...

    UNKNOWN_KEY = "__unknown__"

    def _norm_loc(s: str | None) -> str:
        v = (s or "").strip().lower()
        # treat common unknowns the same way
        if v in ("", "unknown", "n/a", "na", "none", "-"):
            return UNKNOWN_KEY
        return v

    def _has_install_marker(val) -> bool:
        """
        Parse SWITCH_n_ENABLE_PIN install markers.
        For Nodus channels: non-empty means installed.
        """
        if val is None:
            return False
        if isinstance(val, bool):
            return val
        return str(val).strip() != ""

    def _enable_field_value(sw_block: dict, idx: int):
        # Nodus/Pico payloads have used both SWITCH_n_ENABLE_PIN and SWITCH_n_EN.
        return sw_block.get(f"SWITCH_{idx}_ENABLE_PIN", sw_block.get(f"SWITCH_{idx}_EN", ""))

    # ---------- build a unified switches_by_loc once ----------
    from .saiSwitchSettingsManager import SwitchSettingsManager

    sw_mgr = None
    try:
        sw_mgr = SwitchSettingsManager("switch_settings")
    except Exception:
        pass

    # Initialize buckets
    resolved_switch_tile_styles = switch_tile_style_map is not None
    switch_tile_style_map = dict(switch_tile_style_map or {})
    switches_by_loc: dict[str, list] = defaultdict(list)

    # ---- Preload on-disk locations (id → location) to avoid stale in-memory values
    switch_locations_on_disk: dict[str, str] = {}
    first_switch_id_on_disk: str | None = None
    if sw_mgr:
        try:
            all_sw_ids = sw_mgr.list_switches() or []
            if all_sw_ids:
                first_switch_id_on_disk = all_sw_ids[0]
            for _sid in all_sw_ids:
                try:
                    loc = sw_mgr.get_setting(_sid, "Switch.SWITCH_LOCATION", None)
                    if isinstance(loc, str) and loc.strip():
                        switch_locations_on_disk[_sid] = loc.strip()
                except Exception:
                    pass
        except Exception:
            pass

    # 1) Local Pi switches — build a presenter that always uses on-disk location
    local_switch_ids_present: set[str] = set()
    if switch_controllers:
        local_iter = switch_controllers.values() if isinstance(switch_controllers, dict) else [switch_controllers]
        for ctrl in local_iter:
            try:
                if not getattr(ctrl, "is_present", False):
                    continue
                if bool(getattr(ctrl, "is_remote", False)):
                    # Remote/Nodus switches get a richer presenter from MQTT/discovery
                    # below; do not let a stale startup controller shadow it.
                    continue

                sw_id = getattr(ctrl, "switch_id", None)
                # Prefer exact id match; if missing and only one local switch exists, use that
                fresh_loc = None
                if isinstance(sw_id, str) and sw_id.strip():
                    fresh_loc = switch_locations_on_disk.get(sw_id.strip())
                if not fresh_loc and first_switch_id_on_disk and len(switch_locations_on_disk) == 1:
                    fresh_loc = switch_locations_on_disk.get(first_switch_id_on_disk)

                # Last resort: whatever the controller currently holds (should be rare)
                effective_loc = (fresh_loc or getattr(ctrl, "location", None) or "Unknown").strip()
                

                # Build a neutral presenter decoupled from controller .location timing
                channels = list(getattr(ctrl, "switches", []))  # e.g. ["Fan", "Light", "Pump"]
                last_state = dict(getattr(ctrl, "last_state", {}))  # {label: bool}
                last_time  = dict(getattr(ctrl, "last_set_time", {}))
                override   = dict(getattr(ctrl, "override_script", {}))

                local_presenter = SimpleNamespace(
                    switch_id = sw_id or "local-switch",
                    location  = effective_loc,
                    is_present = True,
                    switches   = channels,
                    last_state = last_state,
                    last_set_time = last_time,
                    override_script = override,
                    channel_id_for_label = dict(getattr(ctrl, "channel_id_for_label", {}) or {}),
                    is_ecowitt = bool(getattr(ctrl, "is_ecowitt", False)),
                    available = bool(getattr(ctrl, "available", True)),
                    confirmed = bool(getattr(ctrl, "confirmed", True)),
                )

                loc_key = _norm_loc(effective_loc)
                local_switch_ids_present.add(str(local_presenter.switch_id or "").strip().lower())
                switches_by_loc[loc_key].append(local_presenter)

                if DEBUG:
                    try:
                        bucket_names = list(switches_by_loc.keys())
                        printDM(f"sw_by_loc buckets={bucket_names} total_items={sum(len(v) for v in switches_by_loc.values())}",
                                location=f"{MODULE}.render_dashboard")
                    except Exception:
                        pass
            except Exception:
                pass

    # 2) Add remote Pico2 W switches discovered via MQTT
    # helper to guess a location for a switch if it's not stored
    def _is_unknown_loc(val: str | None) -> bool:
        v = (val or "").strip().lower()
        return v in ("", "unknown", "n/a", "na", "none", "-")

    def _infer_switch_location(sw_id: str) -> str:
        # try settings first
        if sw_mgr:
            try:
                loc = sw_mgr.get_setting(sw_id, "Switch.SWITCH_LOCATION", "")
                if isinstance(loc, str) and loc.strip() and not _is_unknown_loc(loc):
                    return loc.strip()
            except Exception:
                pass
        # try discovery-time location attached to switch topics
        try:
            nodus_map = getattr(mqtt_ingest, "nodus_switch_topic_map", {}) or {}
            for topic, meta in nodus_map.items():
                try:
                    if meta.get("switch_id") == sw_id:
                        loc = (getattr(mqtt_ingest, "device_location", {}) or {}).get(topic)
                        if isinstance(loc, str) and loc.strip() and not _is_unknown_loc(loc):
                            return loc.strip()
                except Exception:
                    continue
        except Exception:
            pass
        # heuristic: share suffix with a sensor id (e.g., '-dzia16')
        tail = sw_id.split("-", 1)[-1] if "-" in sw_id else sw_id
        for _sid, _loc in (sensor_locations or {}).items():
            if _sid.endswith(tail) or tail in _sid:
                return (_loc or "").strip()
        return ""

    def _channels_from_switch_settings(sw_id: str) -> list[str]:
        """
        Fallback channel discovery for remote switches when MQTT has not emitted
        state/event yet and /itaot did not include switch topics.
        """
        if not sw_mgr:
            return []
        try:
            doc = sw_mgr.load(sw_id) or {}
            sw_blk = doc.get("Switch", {}) if isinstance(doc, dict) else {}
            if not isinstance(sw_blk, dict):
                return []

            sw_type = str(sw_blk.get("TYPE", "") or "").strip().lower()
            has_en_keys = (
                ("SWITCH_1_ENABLE_PIN" in sw_blk) or ("SWITCH_2_ENABLE_PIN" in sw_blk)
                or ("SWITCH_1_EN" in sw_blk) or ("SWITCH_2_EN" in sw_blk)
            )
            labels: list[str] = []
            if sw_type in ("picow", "pico2w", "nodus", "remote", "mqtt") or has_en_keys:
                for i in range(1, 9):
                    lbl = str(sw_blk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                    env = _enable_field_value(sw_blk, i)
                    enabled = _has_install_marker(env)
                    if lbl and enabled:
                        labels.append(lbl)
            else:
                for i in range(1, 33):
                    lbl = str(sw_blk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                    pin = sw_blk.get(f"SWITCH_{i}_PIN", None)
                    if lbl and isinstance(pin, (int, float)):
                        labels.append(lbl)
            return labels
        except Exception:
            return []

    def _channel_map_from_switch_settings(sw_id: str) -> dict[str, str]:
        """
        Return label -> channel_id from switch_settings/<sw_id>/switch.toml when available.
        """
        if not sw_mgr:
            return {}
        try:
            doc = sw_mgr.load(sw_id) or {}
            sw_blk = doc.get("Switch", {}) if isinstance(doc, dict) else {}
            if not isinstance(sw_blk, dict):
                return {}

            out: dict[str, str] = {}
            sw_type = str(sw_blk.get("TYPE", "") or "").strip().lower()
            has_en_keys = (
                ("SWITCH_1_ENABLE_PIN" in sw_blk) or ("SWITCH_2_ENABLE_PIN" in sw_blk)
                or ("SWITCH_1_EN" in sw_blk) or ("SWITCH_2_EN" in sw_blk)
            )
            if sw_type in ("picow", "pico2w", "nodus", "remote", "mqtt") or has_en_keys:
                # Pico/Nodus: channel is installed when SWITCH_n_ENABLE_PIN is non-empty.
                for i in range(1, 9):
                    lbl = str(sw_blk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                    cid = str(sw_blk.get(f"SWITCH_{i}_CHANNEL_ID", "") or "").strip()
                    env = _enable_field_value(sw_blk, i)
                    enabled = _has_install_marker(env)
                    if lbl and cid and enabled:
                        out[lbl] = cid
            else:
                # Local Pi relays: channel is enabled when label + numeric pin are present.
                for i in range(1, 33):
                    lbl = str(sw_blk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                    cid = str(sw_blk.get(f"SWITCH_{i}_CHANNEL_ID", "") or "").strip()
                    pin = sw_blk.get(f"SWITCH_{i}_PIN", None)
                    if lbl and cid and isinstance(pin, (int, float)):
                        out[lbl] = cid
            return out
        except Exception:
            return {}

    # read cached state: { "switch-dzia16": {"GP28": "on", "GP27": "off", ...}, ... }
    remote_cache = getattr(mqtt_ingest, "_switch_state_cache", {}) or {}
    try:
        db_switch_rows = list(data_logger.get_switch_identities() or [])
    except Exception:
        db_switch_rows = []

    def _channel_id_from_switch_row(row: dict) -> str:
        cid = str((row or {}).get("channel_id", "") or "").strip()
        if cid:
            m = re.match(r"^[sS](\d+)-(.+)$", cid)
            return f"S{m.group(1)}-{m.group(2)}" if m else cid
        switch_key = str((row or {}).get("switch_key", "") or "").strip()
        if "::" not in switch_key:
            return ""
        candidate = switch_key.split("::", 1)[0].strip()
        m = re.match(r"^[sS](\d+)-(.+)$", candidate)
        return f"S{m.group(1)}-{m.group(2)}" if m else ""

    # include switches discovered via /itaot even if they haven't emitted state yet
    discovered_switches: dict[str, dict[str, str]] = {}  # switch_id -> {label: channel_id}
    try:
        nodus_map = getattr(mqtt_ingest, "nodus_switch_topic_map", {}) or {}
        for meta in nodus_map.values():
            try:
                sw_id = (meta.get("switch_id") or "").strip()
                if not sw_id:
                    continue
                channel_id = (meta.get("channel_id") or "").strip()
                label = (meta.get("label") or channel_id or "").strip()
                if not label:
                    continue
                bucket = discovered_switches.setdefault(sw_id, {})
                bucket.setdefault(label, channel_id or label)
            except Exception:
                continue
    except Exception:
        pass

    all_remote_ids = set(remote_cache.keys()) | set(discovered_switches.keys())
    # Include on-disk switch IDs so a remote switch can render before first MQTT
    # switch state/event packet, as long as settings exist.
    all_remote_ids |= set(switch_locations_on_disk.keys())
    for sw_id in sorted(all_remote_ids):
        try:
            if str(sw_id or "").strip().lower() in local_switch_ids_present:
                continue
            ch_map = remote_cache.get(sw_id, {}) or {}
            label_map = discovered_switches.get(sw_id, {})
            if not label_map and db_switch_rows:
                try:
                    sid_l = str(sw_id or "").strip().lower()
                    mapped: dict[str, str] = {}
                    for row in db_switch_rows:
                        rsid = str(row.get("switch_id", "")).strip().lower()
                        if rsid != sid_l:
                            continue
                        lbl = str(row.get("label", "") or "").strip()
                        cid = _channel_id_from_switch_row(row)
                        if lbl and cid:
                            mapped[lbl] = cid
                    if mapped:
                        label_map = mapped
                except Exception:
                    label_map = label_map or {}

            enabled_map = _channel_map_from_switch_settings(sw_id)
            if enabled_map:
                enabled_labels = set(enabled_map.keys())
                if label_map:
                    # Drop disabled/stale labels from discovery/DB maps.
                    label_map = {lbl: cid for lbl, cid in label_map.items() if lbl in enabled_labels}
                else:
                    label_map = dict(enabled_map)
            if not label_map:
                label_map = dict(enabled_map)

            channels = list(label_map.keys()) if label_map else list(ch_map.keys())
            if not channels:
                channels = _channels_from_switch_settings(sw_id)
            if not channels:
                continue

            last_state = {}
            for label in channels:
                channel_id = label_map.get(label, label)
                raw = ch_map.get(channel_id)
                if raw is None:
                    raw = ch_map.get(label)
                if raw is None:
                    continue
                last_state[label] = (str(raw).lower() == "on")

            # presenter shaped like local controllers so the table code below works unchanged
            presenter = SimpleNamespace(
                switch_id=sw_id,
                location=_infer_switch_location(sw_id),
                is_present=True,
                switches=channels,                 # NOTE: list of channel labels
                channel_id_for_label=dict(label_map or {}),
                last_state=last_state,             # {label: bool}
                last_set_time={ch: "" for ch in channels},
                override_script=defaultdict(bool), # nothing to override yet for remote
            )

            loc_key = _norm_loc(presenter.location)

            # even if we fail to infer a location, put it under empty-key bucket;
            # the rendering loop can still find it if a sensor shares that location later
            switches_by_loc[loc_key].append(presenter)
        except Exception:
            pass
    
    def _db_label_map_for_switch(sw_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            sid_l = str(sw_id or "").strip().lower()
            if not sid_l:
                return out
            for row in (db_switch_rows or []):
                rsid = str(row.get("switch_id", "") or "").strip().lower()
                if rsid != sid_l:
                    continue
                lbl = str(row.get("label", "") or "").strip()
                cid = _channel_id_from_switch_row(row)
                if lbl and cid:
                    out[lbl] = cid
        except Exception:
            return out
        return out

    if DEBUG:
        try:
            bucket_names = list(switches_by_loc.keys())
            printDM(
                f"sw_by_loc buckets={bucket_names} total_items={sum(len(v) for v in switches_by_loc.values())}",
                location=f"{MODULE}.render_dashboard",
            )
        except Exception:
            pass


    GAUGE_SIZES = {
        "Small": {
            "canvas_width": 260,
            "canvas_height": 205,
            "container_width": "260px",
            "font_rem": "1.1rem",
            "font_px": "10px",
            "stats_font": "0.85rem",
        },
        "Large": {
            "canvas_width": 500,
            "canvas_height": 415,
            "container_width": "500px",
            "font_rem": "1.7rem",
            "font_px": "17px",
            "stats_font": "1.7rem",
        }
    }

    layout = GAUGE_SIZES.get(gauge_size, GAUGE_SIZES["Small"])
    
    # --- Resolve display_style: "Gauge" or "Graph6hr" or "Graph24hr" from system settings if not provided ---
    if not display_style:
        try:
            from .saiSettings import saiSettings
            sys_settings = saiSettings()
            display_style = sys_settings.get_displayStyle()  # "Gauge" or "Graph6hr" or "Graph24hr"
        except Exception:
            display_style = "Gauge"

    display_style = (display_style or "Gauge")
    display_style_js = display_style.lower()
    weather_forecast_provider = normalize_weather_forecast_provider(weather_forecast_provider)
    weather_forecast_enabled = weather_forecast_provider != "none"
    weather_forecast_theme = str(weather_forecast_theme or "").strip().lower()
    if weather_forecast_theme not in {"pollinator", "garden", "island", "river", "desert"}:
        weather_forecast_theme = "pollinator"
    dashboard_custom_theme_style = str(dashboard_custom_theme_style or "").strip()
    if dashboard_custom_theme_style:
        dashboard_background_class = "custom"
    else:
        dashboard_background_theme = normalize_dashboard_background_theme(dashboard_background_theme)
        dashboard_background_class = dashboard_background_theme.replace("_", "-")

    yield "<!DOCTYPE html>"
    yield f"<html><head><title>{APP_NAME_LONG}</title>"
    yield "<meta charset='UTF-8'>"
    yield "<link rel='manifest' href='/ui_static/manifest.webmanifest'>"
    yield "<link rel='icon' href='/ui_static/icons/sensorius-192.png' type='image/png' sizes='192x192'>"
    yield "<link rel='icon' href='/ui_static/favicon.svg' type='image/svg+xml'>"
    yield f"<link rel='apple-touch-icon' sizes='180x180' href='/ui_static/apple-touch-icon.png?v={APP_VERSION}'>"
    yield "<script src='https://cdn.jsdelivr.net/npm/gaugeJS@1.3.7/dist/gauge.min.js'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@1.4.0'></script>"
    yield from render_graph_modal(switch_installed=switch_installed, gauge_config=gauge_config)
    # global assets for templates
    yield f"<link rel='stylesheet' href='/ui_static/css/app.css?v={APP_VERSION}'>"
    yield f"<link rel='stylesheet' href='/ui_static/css/mobile_settings.css?v={APP_VERSION}'>"
    yield f"<script src='/ui_static/js/mobile_settings.js?v={APP_VERSION}'></script>"
    yield f"<script src='/ui_static/js/dashboard_order.js?v={APP_VERSION}' defer></script>"
    yield f"<script src='/ui_static/js/at_a_glance_order.js?v={APP_VERSION}' defer></script>"
    yield f"<script src='/ui_static/js/switch_history.js?v={APP_VERSION}'></script>"
    yield f"<script src='/ui_static/js/switch_commands.js?v={APP_VERSION}'></script>"
    yield f"<script src='/ui_static/weather_forecast/moon.js?v={APP_VERSION}' defer></script>"
    yield f"<script src='/ui_static/js/weather_alerts.js?v={APP_VERSION}' defer></script>"
    yield f"<script src='/ui_static/js/draggable_modals.js?v={APP_VERSION}'></script>"
    yield f"<script type='module' src='/ui_static/js/advanced_automation.js?v={APP_VERSION}'></script>"
    yield f"<script src='/ui_static/js/sensor_settings_modal.js?v={APP_VERSION}'></script>"
    body_style = f" style='{html_escape(dashboard_custom_theme_style)}'" if dashboard_custom_theme_style else ""
    yield f"</head><body class='dashboard-page dashboard-theme-{dashboard_background_class}'{body_style}>"
    yield (
      f"<div class='dashboard' "
      f"style='--container-width:{layout['container_width']};"
      f"--stats-font:{layout['stats_font']};"
      f"--canvas-width:{layout['canvas_width']};"
      f"--canvas-height:{layout['canvas_height']};'>"
    )    
    # Sensor settings resolution and lookup map
    metric_tile_style_map = dict(metric_tile_style_map or {})
    sensor_display_map = {}
    sensor_style_map = {}
    if isinstance(expected_gauge_map, dict) and expected_gauge_map:
        for sid in all_values:
            sensor_display_map[sid] = list(expected_gauge_map.get(sid) or [])
            style_block = {}
            if isinstance(expected_display_style_map, dict):
                style_block = expected_display_style_map.get(sid) or {}
            sensor_style_map[sid] = dict(style_block or {})
    else:
        mgr = SensorSettingsManager("sensor_settings")
        sensor_lookup = {s.lower(): s for s in mgr.list_ids()}
        for sid in available:
            normalized = sid.lower()
            if normalized not in sensor_lookup:
                sensor_lookup[normalized] = sid
        for sid in all_values:
            try:
                normalized_id = sid.lower()
                actual_id = sensor_lookup.get(normalized_id)
                if actual_id:
                    try:
                        metrics = mgr.get_display_metrics(actual_id)
                    except Exception:
                        metrics = list(gauge_config.keys())
                    sensor_display_map[sid] = metrics
                    metric_tile_style_map[sid] = mgr.get_metric_tile_style(actual_id)
                    styles = mgr.get_display_styles(actual_id, default_style="Gauge")
                    sensor_style_map[sid] = {
                        f"METRIC_{idx + 1}": styles[idx] if idx < len(styles) else "Gauge"
                        for idx in range(6)
                    }
            except Exception as e:
                printDM(f"Error getting display metrics for {sid}: {e}", location=f"{MODULE}.render_dashboard")

    # --- Location filter dropdown  ---
    # Build the union of known locations from sensors and switches
    # NOTE: switches_by_loc uses normalized keys; keep a display map
    loc_display_map = {}  # norm -> display
    def _titlecase_or_raw(s: str) -> str:
        try:
            t = s.strip()
            return t if not t else t[0].upper() + t[1:]
        except Exception:
            return s

    # From sensors currently in play
    for _sid, _loc in (sensor_locations or {}).items():
        norm = _norm_loc(_loc)
        if norm not in loc_display_map:
            disp = (_loc or "Unknown").strip() or "Unknown"
            loc_display_map[norm] = disp

    # From switches buckets already built above
    for norm_loc_key in switches_by_loc.keys():
        if norm_loc_key not in loc_display_map:
            disp = "Unknown" if norm_loc_key == UNKNOWN_KEY else _titlecase_or_raw(norm_loc_key)
            loc_display_map[norm_loc_key] = disp

    # Sort by display label, but keep 'Unknown' last if present
    known_items = [(k, v) for k, v in loc_display_map.items()]
    known_items.sort(key=lambda kv: (kv[1].lower() == "unknown", kv[1].lower()))

    def _render_switch_cards_for_location(location: str, matched_switches: list):
        """Yield switch cards for one visible location bucket."""
        if DEBUG and matched_switches:
            printDM(
                f"[render_dashboard] switches @ '{location}' matched {len(matched_switches)} controller(s)",
                location="saiHtml",
            )

        rendered_swids_here: set[str] = set()
        switch_rows: list[dict] = []
        switch_ids_here: list[str] = []

        if switch_row_order == "location_device":
            matched_switches = sorted(matched_switches, key=lambda ctrl: str(getattr(ctrl, "switch_id", "") or "").strip().casefold())

        for switch_ctrl in matched_switches:
            sw_id: str = (getattr(switch_ctrl, "switch_id", "") or "").strip()
            sw_id_key: str = sw_id.lower() if sw_id else "__no_switch_id__"

            if sw_id_key in rendered_swids_here:
                if DEBUG:
                    printDM(
                        f"[render_dashboard] skip duplicate render of switch '{sw_id}' in location '{location}'",
                        location="saiHtml",
                    )
                continue
            rendered_swids_here.add(sw_id_key)
            if sw_id:
                switch_ids_here.append(sw_id)

            render_labels = []

            try:
                doc = sw_mgr.load(sw_id) or {}
                sw_blk = doc.get("Switch", {}) if isinstance(doc, dict) else {}
                display = doc.get("Display") or {}
                if not resolved_switch_tile_styles and isinstance(display, dict) and "SWITCH_TILE_STYLE" in display:
                    raw_style = display["SWITCH_TILE_STYLE"]
                    switch_tile_style_map[_norm_loc(location)] = raw_style if raw_style in METRIC_TILE_STYLES else "Theme"

                sw_type = str(sw_blk.get("TYPE", "") or "").strip().lower()
                has_en_keys = (
                    ("SWITCH_1_ENABLE_PIN" in sw_blk) or ("SWITCH_2_ENABLE_PIN" in sw_blk)
                    or ("SWITCH_1_EN" in sw_blk) or ("SWITCH_2_EN" in sw_blk)
                )

                if sw_type in ("picow", "pico2w", "nodus", "remote", "mqtt") or has_en_keys:
                    tmp = []
                    for idx in range(1, 9):
                        label = (sw_blk.get(f"SWITCH_{idx}_LABEL") or "").strip()
                        en_val = _enable_field_value(sw_blk, idx)
                        enabled = _has_install_marker(en_val)
                        if label and enabled:
                            tmp.append(label)
                    if tmp:
                        render_labels = tmp
                else:
                    tmp = []
                    for n in range(1, 33):
                        label = (str(sw_blk.get(f"SWITCH_{n}_LABEL", "") or "").strip())
                        pin = sw_blk.get(f"SWITCH_{n}_PIN", None)
                        if not label:
                            continue
                        if isinstance(pin, (int, float)):
                            tmp.append(label)
                    if tmp:
                        render_labels = tmp

            except Exception:
                pass

            if not render_labels:
                render_labels = list(getattr(switch_ctrl, "switches", []))
            try:
                is_generic_only = bool(render_labels) and all(
                    re.match(r"(?i)^relay\s+\d+$", str(lbl or "").strip()) for lbl in render_labels
                )
            except Exception:
                is_generic_only = False
            candidate_map: dict[str, str] = {}
            try:
                discovered_map = dict(discovered_switches.get(sw_id, {}) or {})
                db_map = _db_label_map_for_switch(sw_id)
                enabled_map = _channel_map_from_switch_settings(sw_id)
                if discovered_map:
                    candidate_map.update(discovered_map)
                for lbl, cid in db_map.items():
                    candidate_map.setdefault(lbl, cid)
                if enabled_map:
                    enabled_labels = set(enabled_map.keys())
                    if candidate_map:
                        candidate_map = {lbl: cid for lbl, cid in candidate_map.items() if lbl in enabled_labels}
                    if not candidate_map:
                        candidate_map = dict(enabled_map)
            except Exception:
                candidate_map = {}

            if (not render_labels) or is_generic_only:
                if candidate_map:
                    render_labels = list(candidate_map.keys())
            elif candidate_map:
                # Merge discovery/DB labels into settings-derived labels so stale
                # local shadow config cannot hide newly added remote channels.
                merged_labels = list(render_labels)
                seen_labels = {str(lbl or "").strip().lower() for lbl in merged_labels if str(lbl or "").strip()}
                for lbl in candidate_map.keys():
                    lbl_text = str(lbl or "").strip()
                    if not lbl_text or lbl_text.lower() in seen_labels:
                        continue
                    merged_labels.append(lbl_text)
                    seen_labels.add(lbl_text.lower())
                render_labels = merged_labels

            if not render_labels:
                switch_rows.append({"empty": True, "switch_ctrl": switch_ctrl, "sw_id": sw_id})
                continue

            for label in render_labels:
                switch_rows.append({
                    "empty": False,
                    "switch_ctrl": switch_ctrl,
                    "sw_id": sw_id,
                    "label": label,
                })

        if not switch_rows:
            return

        switch_ids_attr = ",".join(switch_ids_here)
        multi_switch_card = len(switch_ids_here) > 1
        header_id = _safe(f"{'_'.join(switch_ids_here) if switch_ids_here else location}_header")
        switch_tile_style = switch_tile_style_map.get(_norm_loc(location), 'Theme')
        yield f"<div class='switch-metric-container' data-switch-ids='{switch_ids_attr}' data-switch-location='{html_escape(_norm_loc(location))}' data-tile-style='{html_escape(switch_tile_style)}'>"
        yield f"<div class='switch-card-title-row' data-tile-style='{html_escape(switch_tile_style)}'>"
        yield f"<button type='button' class='switch-card-order-arrow' data-switch-card-move='up' aria-label='Move {html_escape(location)} switch card left' title='Move left' disabled><span aria-hidden='true'>←</span></button>"
        if switch_ids_here:
            header_sw_id = switch_ids_here[0]
            yield f"<h3 id='{header_id}' class='switch-device-title'>{header_sw_id.upper()} "
            yield f"  <a href='javascript:void(0)' onclick='editSwitchSettings(\"{header_sw_id}\")' title='Open {header_sw_id} Settings' style='display:inline-flex; align-items:center; justify-content:center; line-height:1; margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
            yield from _settings_gear_svg_lines(indent="    ")
            yield "  </a>"
            yield f"{location}</h3>"
        else:
            yield f"<h3 id='{header_id}'>SWITCHES {location}</h3>"
        yield f"<button type='button' class='switch-card-order-arrow' data-switch-card-move='down' aria-label='Move {html_escape(location)} switch card right' title='Move right' disabled><span aria-hidden='true'>→</span></button>"
        yield "</div>"

        yield "<div class='switch-container'>"
        yield "<div class='switch-view'>"
        yield "<table class='switch-table'>"
        yield "<thead><tr>"
        yield "<th>Switch</th><th>State</th><th>Events</th>"
        yield "</tr></thead>"
        yield "<tbody>"

        if not multi_switch_card and not any(not row.get("empty") for row in switch_rows):
            yield "<tr><td colspan='3' style='opacity:0.7;'>No enabled switch channels</td></tr>"

        previous_device_id = switch_rows[0]["sw_id"]
        for row in switch_rows:
            sw_id = row["sw_id"]
            if multi_switch_card and sw_id != previous_device_id:
                previous_device_id = sw_id
                yield "<tr class='switch-device-divider'><th colspan='3'><h3 class='switch-device-title'>"
                yield html_escape(sw_id.upper() or "Unknown device")
                if sw_id:
                    settings_action = html_escape(f"editSwitchSettings({json.dumps(sw_id)})")
                    yield f" <a href='javascript:void(0)' onclick='{settings_action}' title='Open {html_escape(sw_id)} Settings' style='display:inline-flex; align-items:center; justify-content:center; line-height:1; margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
                    yield from _settings_gear_svg_lines(indent="", aria_hidden=True)
                    yield "</a>"
                yield f" {html_escape(location)}</h3></th></tr>"
            if row.get("empty"):
                if multi_switch_card:
                    yield "<tr><td colspan='3' style='opacity:0.7;'>No enabled switch channels</td></tr>"
                continue

            switch_ctrl = row["switch_ctrl"]
            label = row["label"]
            safe_label = label.lower().replace(" ", "_")
            is_on = bool(getattr(switch_ctrl, "last_state", {}).get(label, False))
            if DEBUG and str(sw_id).strip().lower() == "sensoria-hub-0" and str(label).strip().lower() == "fan":
                printDM(
                    f"[render_dashboard] {sw_id}::{label} render last_state={is_on}",
                    location=MODULE,
                )
            state_str = "on" if is_on else "off"
            state_confirmed = not (getattr(switch_ctrl, "is_ecowitt", False) and not switch_ctrl.confirmed)
            checked_state_attr = f'aria-checked="{str(is_on).lower()}"' if state_confirmed else ""
            current_state_text = " ON" if is_on else "OFF"
            override_enabled = bool(getattr(switch_ctrl, "override_script", {}).get(label, False))
            checked_attr = "checked" if override_enabled else ""
            last_time_str = getattr(switch_ctrl, "last_set_time", {}).get(label, "")

            channel_id = ""
            try:
                channel_id = str((getattr(switch_ctrl, "channel_id_for_label", {}) or {}).get(label, "") or "").strip()
            except Exception:
                channel_id = ""
            action_sid = channel_id or sw_id
            switch_key = f"{action_sid}::{label}" if action_sid else f"::{label}"
            box_id = f"{sw_id}-{safe_label}_box" if sw_id else f"{safe_label}_box"
            state_id = f"{sw_id}-{safe_label}_state" if sw_id else f"{safe_label}_state"
            time_id = f"{sw_id}-{safe_label}_time" if sw_id else f"{safe_label}_time"
            label_norm = (label or "").strip()
            try:
                timer_state = dict(getattr(switch_ctrl, "get_auto_off_status")(label_norm) or {})
            except Exception:
                timer_state = {}
            timer_seconds = int(timer_state.get("timer_seconds", 0) or 0)
            timer_remaining = int(timer_state.get("timer_remaining_s", 0) or 0)
            timer_ui_key = f"{getattr(switch_ctrl, 'switch_id', '')}::{label_norm}" if getattr(switch_ctrl, "switch_id", "") else f"::{label_norm}"
            timer_safe_key = _safe(f"{getattr(switch_ctrl,'switch_id','')}_{label_norm}_automation")
            timer_input_id = f"{timer_safe_key}_timer_input"
            timer_status_id = f"{timer_safe_key}_timer_status"
            timer_editor_id = f"{timer_safe_key}_timer_editor"
            if timer_seconds <= 0:
                timer_status_text = "Timer disabled"
            elif is_on and timer_remaining > 0:
                timer_status_text = f"Countdown: {timer_remaining}s"
            else:
                timer_status_text = f"Timer set: {timer_seconds}s"

            try:
                from .saiAutomationManager import AutomationManager
                am = AutomationManager()
                sid = getattr(switch_ctrl, "switch_id", "") or ""
                switch_key_full = f"{sid}::{label_norm}" if sid else f"::{label_norm}"
                rule_enabled = am.get_advanced_enabled_for_switch_key(sid, switch_key_full)
            except Exception:
                rule_enabled = False

            automation_enabled = bool(rule_enabled)
            state_cell_classes = "switch-state-td"
            if automation_enabled:
                state_cell_classes += " automation-enabled"

            label_cell = html_escape(label)

            yield "<tr>"
            yield f"<td>{label_cell}</td>"
            yield (
                f"<td class='{state_cell_classes}' "
                f"data-automation-switch-id='{getattr(switch_ctrl, 'switch_id', '')}' "
                f"data-automation-label='{label_norm}' "
                f"data-automation-enabled='{'1' if automation_enabled else '0'}'>"
            )
            yield "<div class='switch-state-cell'>"
            yield (
                f"<button "
                f"  id='{box_id}_btn' "
                f"  class='button switch-state-slider {'green' if is_on else 'black'}{'' if state_confirmed else ' switch-unconfirmed'}' "
                f"  type='button' role='{'switch' if state_confirmed else 'button'}' "
                f"  {checked_state_attr} "
                f"  aria-label='{html_escape(label if state_confirmed else label + ': Unknown')}' "
                f"  aria-disabled='{'true' if automation_enabled else 'false'}' "
                f"  title='{'Automation enabled. Disable automation to toggle manually.' if automation_enabled else f'Toggle state for {label}'}' "
                f"  data-switch-name='{label}' "
                f"  data-switch-key='{switch_key}' "
                f"  data-switch-id='{sw_id}' "
                f"  data-automation-switch-id='{getattr(switch_ctrl, 'switch_id', '')}' "
                f"  data-automation-label='{label_norm}' "
                f"  data-automation-enabled='{'1' if automation_enabled else '0'}' "
                f"  data-state='{state_str}' "
                f"  onclick='toggleSwitchInline(this)'>"
                f"{'Unknown' if getattr(switch_ctrl, 'is_ecowitt', False) and not switch_ctrl.confirmed else ('On' if is_on else 'Off')}"
                f"</button>"
            )
            if getattr(switch_ctrl, "is_ecowitt", False):
                plug_status = "Online" if switch_ctrl.available else ("Offline" if switch_ctrl.confirmed else "Awaiting status")
                yield f"<div class='ecowitt-plug-status detail'>{plug_status}</div>"
            yield "<div class='switch-command-status' role='status' aria-live='polite'></div>"
            yield "".join((
                f"<div class='switch-timer-panel' data-switch-ui-key='{timer_ui_key}' data-switch-id='{getattr(switch_ctrl, 'switch_id', '')}' data-label='{label_norm}'>",
                f"  <div class='switch-timer-summary'>",
                f"    <div id='{timer_status_id}' class='switch-timer-status' data-switch-ui-key='{timer_ui_key}'>{timer_status_text}</div>",
                f"    <button type='button' class='switch-timer-edit-btn' title='Edit timer for {label_norm}' aria-label='Edit timer for {label_norm}' ",
                f"      data-switch-ui-key='{timer_ui_key}' data-editor-id='{timer_editor_id}'>",
                "".join(_settings_gear_svg_lines(indent="      ", aria_hidden=True)),
                f"    </button>",
                f"  </div>",
                f"  <div id='{timer_editor_id}' class='switch-timer-editor' style='display:none;'>",
                f"    <input id='{timer_input_id}' class='switch-timer-input' type='number' min='0' max='9999' step='30' inputmode='numeric' ",
                f"      data-switch-ui-key='{timer_ui_key}' data-switch-id='{getattr(switch_ctrl, 'switch_id', '')}' data-label='{label_norm}' value='{timer_seconds}' />",
                f"    <button type='button' class='button blue switch-timer-confirm-btn' title='Save timer' data-input-id='{timer_input_id}'>Ok</button>",
                f"    <button type='button' class='button black switch-timer-cancel-btn' title='Cancel timer edit' data-input-id='{timer_input_id}' data-editor-id='{timer_editor_id}'>Cancel</button>",
                f"  </div>",
                f"</div>",
            ))
            yield "</div>"
            yield "</td>"

            events_id = _safe(f"{sw_id}_{label_norm}_events") if sw_id else f"{safe_label}_events"
            events_list_id = f"{events_id}_list"
            yield "<td>"
            yield f"  <div id='{events_id}' class='switch-events' aria-label='Recent switch history'>"
            yield "    <div class='switch-history-axis'><span class='switch-history-start'>24hr</span><span>Now</span></div>"
            yield "    <div class='switch-history-chart'><div class='switch-history-states' aria-hidden='true'><span>On</span><span>Off</span></div>"
            yield "      <div class='switch-history-plot' role='button' tabindex='0' aria-label='24 hour switch history; click for 6 hours'><canvas aria-hidden='true'></canvas></div></div>"
            yield "    <div class='switch-history-ticks' aria-hidden='true'></div>"
            yield f"    <ul id='{events_list_id}' class='switch-events-list' hidden data-switch-key='{switch_key}'></ul>"
            yield "    <div class='switch-history-detail' aria-live='polite'>Loading last recorded state…</div>"
            yield f"  </div>"
            yield "</td>"
            yield "</tr>"

        yield "</tbody>"
        yield "</table>"
        yield "</div>"  # .switch-view
        yield "</div>"  # .switch-container
        yield "</div>"  # .switch-metric-container

    # --- measurement status helpers (direct vs MQTT) ---
    def _get_sensor_map():
        """
        Use the owning app's sensor objects to read `sensor.meas_status`.
        Returns either a dict {sensor_id: obj} or an iterable of objs (each with .sensor_id).
        """
        return runtime_sensor_map

    def _active_sensor_for(sid: str):
        """
        Resolve the active sensor object for sid from sensor_map if available.
        """
        sm = _get_sensor_map()
        sid_l = (sid or "").lower()
        if isinstance(sm, dict):
            return sm.get(sid) or sm.get(sid_l) or sm.get(sid_l.replace("_", "-"))
        try:
            from collections.abc import Iterable
            if isinstance(sm, Iterable):
                for obj in sm:
                    if getattr(obj, "sensor_id", "").lower() == sid_l:
                        return obj
        except Exception:
            pass
        return None

    def _hostname_variants_from_sid(sid: str) -> list[str]:
        """
        Our standard SID format is <kind>-<bus>-<hostname>. Return possible host keys for mqtt_ingest.device_status.
        """
        try:
            host = normalize_hostname_base((sid or "").rsplit("-", 1)[-1].strip())
            if not host:
                return []
            return [host, mdns_hostname(host)]
        except Exception:
            return []

    def _resolve_meas_status(sid: str) -> str:
        """
        Order of precedence:
          1) Direct sensor object’s sensor.meas_status if available
          2) MQTT ingest device_status[hostname or hostname.local]
          3) WeeWX rows rendered from stored station data
          4) Fallback: 'unknown'
        Returns one of: 'online' | 'degraded' | 'offline' | 'unknown' | 'migration_required'.
        Explicit app-owned status overrides take precedence over these fallbacks.
        """
        if sid in (sensor_statuses or {}):
            return sensor_statuses[sid]
        sid_text = str(sid or "").strip()
        # 1) direct/local sensor object
        try:
            sensor_obj = _active_sensor_for(sid_text)
            st = getattr(getattr(sensor_obj, "sensor", sensor_obj), "meas_status", None)
            if isinstance(st, str) and st.strip().lower() in {"online", "degraded", "offline", "unknown", "migration_required"}:
                return st.strip().lower()
        except Exception:
            pass

        # 2) mqtt-ingested remote
        try:
            status_fn = getattr(mqtt_ingest, "get_measure_status", None)
            if callable(status_fn):
                st = status_fn(sid_text)
                if isinstance(st, str):
                    status = st.strip().lower()
                    if status in {"online", "degraded", "offline", "migration_required"}:
                        return status
            for host in _hostname_variants_from_sid(sid_text):
                st = (getattr(mqtt_ingest, "device_status", {}) or {}).get(host)
                if isinstance(st, str):
                    status = st.strip().lower()
                    if status in {"online", "degraded", "offline", "migration_required"}:
                        return status
        except Exception:
            pass

        # 3) Locally ingested weather stations do not have a direct sensor object
        # or Nodus heartbeat, so a row with rendered values should not show unknown.
        try:
            sid_lower = sid_text.lower()
            if (
                sid_lower == WEEWX_DEFAULT_SENSOR_ID.lower()
                or sid_lower.startswith("weewx")
                or sid_lower.startswith(("ecowitt-", "acurite-", "davis-", "oregon-"))
            ):
                values = all_values.get(sid_text) or {}
                if values:
                    return "online"
        except Exception:
            pass

        # 4) fallback
        return "unknown"

    def _status_color_hex(status: str) -> str:
        """
        Map status -> color (matches your CSS palette):
          unknown = yellow, online = green, degraded = amber, offline = red, migration_required = blue-gray
        """
        s = (status or "").strip().lower()
        if s == "online":
            return "#28a745"  # green
        if s == "degraded":
            return "#fd7e14"  # amber
        if s == "offline":
            return "#dc3545"  # red
        if s == "migration_required":
            return "#6c757d"  # blue-gray
        return "#ffc107"      # yellow (unknown)

    if DEBUG:
        for sid, metrics in sensor_display_map.items():
            printDM(f"Display Metrics for {sid}: {metrics}", location=f"{MODULE}.render_dashboard")

    yield "<div class='dashboard-content' style='text-align:center; width:100%;'>"

    yield "<h2 id='sensor_header'>"
    yield "<a href='javascript:void(0)' onclick='openGraphModal()' title='View Graph' aria-label='Full Screen Graphs' class='dashboard-header-action'>"
    yield "  <svg xmlns='http://www.w3.org/2000/svg' class='dashboard-graph-icon' width='22' height='22' viewBox='0 0 24 24' role='img'>"
    yield "    <title>Full Screen Graphs</title>"
    yield "    <!-- Y-axis -->"
    yield "    <line x1='2' y1='2' x2='2' y2='22' stroke='black' stroke-width='1'/>"
    yield "    <!-- X-axis -->"
    yield "    <line x1='2' y1='22' x2='22' y2='22' stroke='black' stroke-width='1'/>"
    yield "    <!-- Sine wave -->"
    yield "    <path d='M2 12 C 5 6, 9 6, 12 12 S 19 18, 22 12'"
    yield "          fill='none' stroke='blue' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
    yield "  </svg>"
    yield "</a>"
    yield "<button type='button' class='dashboard-brand-refresh' onclick='window.location.reload()' title='Refresh Sensorius AI' aria-label='Refresh Sensorius AI'>"
    yield f"<img class='dashboard-brand-image' src='/ui_static/01-sensorius-overview-v5.png?v={APP_VERSION}' alt='Sensorius AI'>"
    yield "<span id='refresh_link' class='dashboard-brand-refresh-icon' aria-hidden='true'>⟳</span>"
    yield "</button>"
    yield "<a href='#' onclick='window.editSystemSettings && window.editSystemSettings(); return false;' title='Open General Settings' aria-label='Open General Settings' class='dashboard-header-action'>"
    yield from _settings_gear_svg_lines(indent="    ")
    yield "</a>"
    yield "</h2>"
  
    yield "<p id='update_time'>--</p>"
    yield "<p id='dashboard_refresh_status' role='status' hidden>Live updates delayed. Displayed readings may be out of date. Retrying automatically.</p>"

    yield f"<link rel='stylesheet' href='/ui_static/css/dashboard_cards.css?v={APP_VERSION}'>"

    yield "<div class='dash-top-row'>"
    yield "<div class='astro-box' id='bioBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Biodynamic Calendar</div>"
    yield "    <span class='spinner dashboard-card-spinner' aria-hidden='true'></span>"
    yield "    <div class='bio-window' id='bioDateLine'>Loading biodynamic date...</div>"
    yield "    <div class='bio-daylight' id='bioDaylightLine'></div>"
    yield "    <div class='bio-main' id='bioCurrentPanel'>"
    yield "      <div id='bioCurrentSign'>Loading...</div>"
    yield "      <div id='bioCurrentElement'>Moon sign</div>"
    yield "    </div>"
    yield "    <div class='bio-window' id='bioWindow'>Loading biodynamic window...</div>"
    yield "    <div id='bioUpcoming'>Loading transitions...</div>"
    yield "    <div class='bio-card-actions'>"
    yield "      <button type='button' class='bio-open-btn' id='bioOpenBtn' aria-label='Open biodynamic calendar' title='View Calendar'>"
    yield "        <span class='spinner' aria-hidden='true'></span>"
    yield "        <span class='bio-open-btn-label'>Calendar</span>"
    yield "      </button>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"
    yield "<div class='astro-box' id='moonBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <span class='spinner dashboard-card-spinner' aria-hidden='true'></span>"
    yield "    <div class='moon-head'>"
    yield "      <div class='astro-title'>Moon Phase</div>"
    yield "      <div class='moon-view-toggle' role='group' aria-label='Moon view mode' title='Local sky view or Reference moon diagram'>"
    yield "        <button type='button' class='moon-view-btn active' id='moonViewLocal' data-moon-view='local' aria-pressed='true' title='Local sky view or Reference moon diagram'>Local</button>"
    yield "        <button type='button' class='moon-view-btn' id='moonViewReference' data-moon-view='reference' aria-pressed='false' title='Local sky view or Reference moon diagram'>Ref</button>"
    yield "      </div>"
    yield "    </div>"
    yield "    <div class='astro-date' id='moonDateLine'>--</div>"
    yield "    <div class='moon-layout'>"
    yield "      <div class='moon-side left'>"
    yield "        <span class='moon-label'>Moonrise</span>"
    yield "        <span class='moon-value' id='moonRiseTime'>--</span>"
    yield "        <span class='moon-label'>Moonset</span>"
    yield "        <span class='moon-value' id='moonSetTime'>--</span>"
    yield "      </div>"
    yield "      <canvas id='moonPhaseCanvas' width='88' height='88'></canvas>"
    yield "      <div class='moon-side right'>"
    yield "        <span class='moon-label'>% Lit</span>"
    yield "        <span class='moon-value' id='moonLitPct'>--</span>"
    yield "        <span class='moon-label' id='moonNextPhaseLabel'>Next Phase</span>"
    yield "        <span class='moon-value' id='moonNextPhaseDate'>--</span>"
    yield "      </div>"
    yield "    </div>"
    yield "    <div class='astro-meta' id='moonMeta'>Loading moon data...</div>"
    yield "    <div class='moon-eclipse-line' id='moonEclipse24h' hidden><strong>Eclipse next 24h:</strong> <span id='moonEclipse24hText'>--</span></div>"
    yield "    <div class='bio-card-actions moon-card-actions'>"
    yield "      <button type='button' class='bio-open-btn moon-calendar-btn' id='moonCalendarBtn' aria-label='Open Lunar Calendar' title='View Lunar Calendar'>"
    yield "        <span class='bio-open-btn-label'>Lunar Calendar</span>"
    yield "      </button>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"
    # treat any non 'loc:*' as All (back-compat: direct sensor ids will land here)
    is_loc_filter = isinstance(sensor_id, str) and sensor_id.startswith("loc:")
    yield "<form method='get' class='dash-loc-form' id='displayDeviceBox'>"
    yield "<div class='dash-loc-head'>"
    yield "  <div class='dash-loc-label'>DISPLAY DEVICE BY</div>"
    yield "</div>"
    yield "<select name='sensor_id' id='sensor_id' onchange='this.form.submit()'>"
    yield f"<option value='All' {'selected' if (not is_loc_filter or sensor_id == 'All') else ''}>All Locations</option>"
    for norm, disp in known_items:
        val = f"loc:{disp}"
        sel = "selected" if sensor_id == val else ""
        yield f"<option value='{val}' {sel}>{disp}</option>"
    yield "</select>"
    yield "<button type='button' class='dash-theme-trigger' id='dashboardThemeBtn' aria-label='Preview Sensorius dashboard themes'>Theme</button>"
    yield "</form>"
    yield "<div class='astro-box' id='sunBox' aria-live='polite' role='button' tabindex='0' aria-label='Open 29 Day Sun/Moon Position' title='29 Day Sun/Moon Position'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Sun Position</div>"
    yield "    <div class='astro-date' id='sunDateLine'>--</div>"
    yield "    <span class='spinner dashboard-card-spinner' aria-hidden='true'></span>"
    yield "    <div class='astro-meta astro-times' id='sunMeta'><span id='sunTimeRise'>--</span><span id='sunTimeNoon'>--</span><span id='sunTimeSet'>--</span></div>"
    yield "    <canvas id='sunPathCanvas' width='204' height='108' aria-label='24 hour sun and moon position chart'></canvas>"
    yield "    <div class='astro-meta astro-times moon-position-times' id='moonPositionMeta'><span id='moonTimeRise'>--</span><span id='moonTimeSet'>--</span></div>"
    yield "    <div class='astro-title moon-position-title'>Moon Position</div>"
    yield "    <div class='bio-card-actions'>"
    yield "      <button type='button' class='bio-open-btn' id='sunMoon29Btn' aria-label='Open 29 Day Sun/Moon Position' title='View 29 Day Sun/Moon Position'>"
    yield "        <span class='bio-open-btn-label'>29 Day Graph</span>"
    yield "      </button>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"
    if weather_forecast_enabled:
        yield "<div class='astro-box forecast-scene-pending' id='weatherForecastBox' aria-live='polite'>"
        yield "  <div class='astro-card'>"
        yield "    <div class='astro-title'>24 Hour Forecast</div>"
        yield "    <span class='spinner dashboard-card-spinner' aria-hidden='true'></span>"
        yield "    <div class='astro-date' id='forecastDateLine'>--</div>"
        yield "    <div class='forecast-current'>"
        yield "      <div class='forecast-current-summary' id='forecastOverall' tabindex='0' aria-label='Forecast synopsis'>--</div>"
        yield "      <dl class='forecast-current-grid'>"
        yield "        <dt>Temp</dt><dd id='forecastTempRange'>--</dd>"
        yield "        <dt>RH</dt><dd id='forecastRhRange'>--</dd>"
        yield "        <dt>Wind</dt><dd id='forecastWind' class='forecast-wind-value'>--</dd>"
        yield "        <dt id='forecastPrecipLabel'>Rain</dt><dd id='forecastPrecipChance'>--</dd>"
        yield "      </dl>"
        yield "      <div class='forecast-status' id='forecastStatus'></div>"
        yield "    </div>"
        yield "    <div class='forecast-card-actions'>"
        yield "      <button type='button' class='forecast-open-btn' id='forecastFiveDayBtn' aria-label='Open Caelus weather forecast' title='Caelus Forecast'>"
        yield "        <span class='spinner' aria-hidden='true'></span>"
        yield "        <span class='forecast-open-btn-label'>Caelus Forecast</span>"
        yield "      </button>"
        yield "    </div>"
        yield "  </div>"
        yield "</div>"
    yield "<div class='sun-moon-29-overlay' id='sunMoon29Overlay' aria-hidden='true'>"
    yield "  <div class='astro-box sun-moon-29-card' id='sunMoon29Card' title='29 Day Sun/Moon Position'>"
    yield "    <button type='button' class='sun-moon-29-close' id='sunMoon29Close' aria-label='Close 29 Day Sun/Moon Position' title='Close'>×</button>"
    yield "    <div class='astro-card sun-moon-29-inner'>"
    yield "      <div class='astro-title'>29 Day Sun/Moon Position</div>"
    yield "      <span class='spinner dashboard-card-spinner' aria-hidden='true'></span>"
    yield "      <canvas id='sunMoon29Canvas' width='1120' height='150' aria-label='29 day sun and moon position and moon phase chart'></canvas>"
    yield "      <div class='astro-meta sun-moon-29-meta' id='sunMoon29Meta'>--</div>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"
    yield "<div class='caelus-moon-overlay' id='caelusMoonOverlay' aria-hidden='true'>"
    yield "  <section class='caelus-moon-dialog' id='caelusMoonDialog' role='dialog' aria-modal='true' aria-labelledby='caelusMoonDialogTitle' tabindex='-1'>"
    yield "    <header class='caelus-moon-dialog-head'><div class='caelus-moon-dialog-title'><h2 id='caelusMoonDialogTitle'>Lunar Calendar</h2><div class='caelus-moon-view-toggle' role='group' aria-label='Lunar Calendar view mode'><button type='button' class='caelus-moon-view-btn active' id='caelusMoonViewLocal' data-moon-view='local' aria-pressed='true'>Local</button><button type='button' class='caelus-moon-view-btn' id='caelusMoonViewReference' data-moon-view='reference' aria-pressed='false'>Ref</button></div></div><button type='button' class='caelus-moon-close' id='caelusMoonClose' aria-label='Close Lunar Calendar'>×</button></header>"
    yield "    <p class='caelus-moon-popup-status' id='caelusMoonStatus'>Loading observer-local Moon phases…</p>"
    yield "    <div class='caelus-lunar-layout'>"
    yield "      <div class='caelus-lunar-arc' aria-hidden='true'></div>"
    yield "      <div class='caelus-lunar-side caelus-lunar-side-left' id='caelusPreviousPhases' aria-label='Previous four Moon phases'></div>"
    yield "      <div class='caelus-lunar-current'>"
    yield "        <p class='caelus-live-label'>Moon now · live</p>"
    yield "        <canvas id='caelusCurrentMoonDisk' width='192' height='192' role='img'></canvas>"
    yield "        <h3 id='caelusCurrentMoonName'>Moon</h3>"
    yield "        <p class='caelus-lunar-current-detail'><strong id='caelusCurrentMoonIllumination'>--</strong> illuminated · lunar day <span id='caelusCurrentMoonAge'>--</span><br><span class='caelus-lunar-local' id='caelusMoonLocalView'>Observer-local orientation</span></p>"
    yield "      </div>"
    yield "      <div class='caelus-lunar-side caelus-lunar-side-right' id='caelusUpcomingPhases' aria-label='Upcoming four Moon phases'></div>"
    yield "      <p class='caelus-eclipse-line' id='caelusUpcomingEclipse'><strong>Upcoming eclipse:</strong> <span id='caelusUpcomingEclipseText'>Loading…</span></p>"
    yield "      <div class='caelus-lunar-footer'>"
    yield "        <div class='caelus-lunar-timeline' role='img' aria-label='Moon and Sun events from sunrise to the next sunrise'>"
    yield "          <div class='caelus-lunar-timeline-row caelus-lunar-timeline-row-moon'><span class='caelus-lunar-timeline-title'>Moon</span><div class='caelus-lunar-timeline-track'>"
    yield "            <span class='caelus-lunar-marker' id='caelusMoonriseMarker'><span>Moonrise</span><strong id='caelusMoonrise'>--</strong></span>"
    yield "            <span class='caelus-lunar-marker' id='caelusMoonsetMarker'><span>Moonset</span><strong id='caelusMoonset'>--</strong></span>"
    yield "          </div></div>"
    yield "          <div class='caelus-lunar-timeline-row caelus-lunar-timeline-row-sun'><span class='caelus-lunar-timeline-title'>Sun</span><div class='caelus-lunar-timeline-track'>"
    yield "            <span class='caelus-lunar-marker is-start' id='caelusSunriseMarker' style='--timeline-left:0%'><span>Sunrise</span><strong id='caelusMoonSunrise'>--</strong></span>"
    yield "            <span class='caelus-lunar-marker' id='caelusSunsetMarker'><span>Sunset</span><strong id='caelusMoonSunset'>--</strong></span>"
    yield "            <span class='caelus-lunar-marker is-end' id='caelusNextSunriseMarker' style='--timeline-left:100%'><span>Next sunrise</span><strong id='caelusNextSunrise'>--</strong></span>"
    yield "          </div></div>"
    yield "        </div>"
    yield "        <small id='caelusMoonUpdated'>Updated --</small>"
    yield "      </div>"
    yield "    </div>"
    yield "  </section>"
    yield "</div>"
    yield "</div>"
    
    # Per-sensor gauge blocks
    for sid, sensor_metrics in expected_gauge_map.items():
        sidLower = sid.lower()
        sidUpper = dashboard_sensor_label(sid)
        values = all_values.get(sid) or {}
        stats  = all_stats.get(sid)  or {}
        topic = f"sensor/{sid}/data"
        location = (sensor_locations or {}).get(sid) or ""
        # ---- measurement status indicator ----
        _meas_status = _resolve_meas_status(sid)
        _dot_color   = _status_color_hex(_meas_status)

        default_expanded = normalize_dashboard_metric_set(dashboard_metric_set) == "All"
        expanded_text = "true" if default_expanded else "false"
        yield f"<div class='sensor-group' id='group_{sid}' data-dashboard-row-id='{sid}' data-sensor-id='{sid}' data-sensor-expanded='{expanded_text}' style='width:100%; flex:0 0 100%; display:block;'>"
        header_style = metric_tile_style_map.get(sid, "Theme")
        if header_style not in METRIC_TILE_STYLES:
            header_style = "Theme"
        yield f"<div class='sensor-group-header' data-tile-style='{html_escape(header_style)}'>"
        yield f"<h3 id='{sid}_header' class='sensor-group-title'>"      
        yield (
            f" <button type='button' class='sensor-collapse-toggle' data-sensor-id='{sid}'"
            f"         aria-controls='row_{sid}' aria-expanded='{expanded_text}'"
            f"         aria-label='{'Collapse' if default_expanded else 'Expand'} {sidUpper} metrics' title='{'Collapse' if default_expanded else 'Expand'} {sidUpper} metrics' hidden>"
            f"   <span class='sensor-collapse-icon' aria-hidden='true'>"
            f"     <svg viewBox='0 0 24 24' focusable='false'><path fill='currentColor' d='M7.2 3.9A1.5 1.5 0 0 0 5 5.25v13.5a1.5 1.5 0 0 0 2.2 1.34l11.65-6.75a1.55 1.55 0 0 0 0-2.68z'></path></svg>"
            f"   </span>"
            f" </button>"
        )
        yield (            
            f" <span class='sensor-status-dot' id='{sid}_statusdot' data-sid='{sid}'"
            f"      title='Connection status: {_meas_status}' "
            f"      aria-label='Connections status: {_meas_status}' "
            f"      style='display:inline-block;width:15px;height:15px;"
            f"             border-radius:50%;vertical-align:middle;margin-right:6px;"
            f"             background:{_dot_color};border:1px solid #666;'></span>"
        )
        if sid.startswith(("ecowitt-", "acurite-", "davis-", "oregon-", "weewx")) or sid in (battery_statuses or {}):
            battery_status = (battery_statuses or {}).get(sid, "UNKNOWN")
            if battery_status not in {"OK", "LOW", "UNKNOWN"}:
                battery_status = "UNKNOWN"
            yield f"<img class='sensor-battery-icon' id='{sid}_battery' src='/ui_static/icons/battery-{battery_status.lower()}.svg' alt='Battery Status: {battery_status}' title='Battery Status: {battery_status}' width='26' height='18' style='vertical-align:middle;margin-right:6px'>"
        yield f" {sidUpper} "
        yield f"  <a href='#' onclick=\"window.editSensorSettings && window.editSensorSettings('{sidLower}'); return false;\" title='Open {sid} Settings' style='margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
        yield from _settings_gear_svg_lines(indent="    ")
        yield "  </a>"
        yield (f"{location}")
        yield "</h3>"
        yield (
            f"<div class='sensor-order-wrap' data-sensor-order='{sid}'>"
            f"  <button type='button' class='sensor-order-item' data-move='up' data-sensor-id='{sid}' title='Move {sidUpper} row up' aria-label='Move {sidUpper} row up'><span aria-hidden='true'>↑</span></button>"
            f"  <button type='button' class='sensor-order-item' data-move='down' data-sensor-id='{sid}' title='Move {sidUpper} row down' aria-label='Move {sidUpper} row down'><span aria-hidden='true'>↓</span></button>"
            f"</div>"
        )
        yield "</div>"
        render_metrics = []
        seen_render_metrics = set()
        extend_gauge_config_for_metrics(gauge_config, list(sensor_metrics or []) + list(values.keys()))
        for metric in (sensor_metrics or []):
            canonical_metric = canonicalize_metric_name(metric, gauge_config)
            if canonical_metric in gauge_config and canonical_metric not in seen_render_metrics:
                seen_render_metrics.add(canonical_metric)
                render_metrics.append(canonical_metric)
        yield f"<div class='sensor-row' id='row_{sid}'>"

        # build out the gauges for this sensor based on its configured display metrics; if none, show all available gauges
        for metric_pos, metric in enumerate(render_metrics, start=1):
            config = gauge_config[metric]
            val = values.get(metric)
            if val is None:
                for k in values.keys():
                    if k.lower().replace("-", "").replace("_", "") == metric.lower().replace("-", "").replace("_", ""):
                        val = values[k]
                        break

            display_metric = str(config.get("value_metric") or metric)
            display_config = gauge_config.get(display_metric) or config
            display_unit = str(display_config.get("unit") or config.get("unit") or "")
            display_val_raw = values.get(display_metric)
            if display_val_raw is None and display_metric != metric:
                for k in values.keys():
                    if k.lower().replace("-", "").replace("_", "") == display_metric.lower().replace("-", "").replace("_", ""):
                        display_val_raw = values[k]
                        break
            stat_metric = str(config.get("stats_metric") or metric)
            stat = stats.get(stat_metric, {})
            display_val = convert_display_value(display_val_raw, display_config) if display_val_raw is not None else "--"
            display_precision = display_config.get("display_precision")
            if display_val == "--":
                display_text = "--"
            elif isinstance(display_precision, int):
                try:
                    display_text = f"{float(display_val):.{display_precision}f} {display_unit}".strip()
                except Exception:
                    display_text = f"{display_val} {display_unit}".strip()
            else:
                display_text = f"{display_val} {display_unit}".strip()

            from datetime import datetime

            def _format_stats_timestamp(ts: str) -> str:
                try:
                    dt = datetime.fromisoformat(ts).replace(microsecond=0)
                    if dt.tzinfo:
                        dt = dt.replace(tzinfo=None)
                    hour = dt.strftime("%I").lstrip("0") or "12"
                    return f"{dt:%Y-%m-%d}<br>{hour}:{dt:%M:%S} {dt:%p}"
                except Exception:
                    return ts
        
            min_val = stat.get("min", "--")
            avg_val = stat.get("avg", "--")
            max_val = stat.get("max", "--")
            stat_config = gauge_config.get(stat_metric) or config
            if min_val != "--":
                min_val = convert_display_value(min_val, stat_config)
            if avg_val != "--":
                avg_val = convert_display_value(avg_val, stat_config)
            if max_val != "--":
                max_val = convert_display_value(max_val, stat_config)
            stat_precision = stat_config.get("display_precision")
            if not isinstance(stat_precision, int):
                stat_precision = 1

            def _format_stat_value(stat_value):
                try:
                    return f"{float(stat_value):.{stat_precision}f}"
                except Exception:
                    return stat_value

            min_val = _format_stat_value(min_val)
            avg_val = _format_stat_value(avg_val)
            max_val = _format_stat_value(max_val)
            min_ts = stat.get("min_ts", "--")
            max_ts = stat.get("max_ts", "--")
            if isinstance(min_ts, str):
                min_ts = _format_stats_timestamp(min_ts)
            if isinstance(max_ts, str):
                max_ts = _format_stats_timestamp(max_ts)
            safe_metric = _safe(metric)
            safe_id = f"{sid}_{safe_metric}"
            metric_display_style = (
                (sensor_style_map.get(sid) or {}).get(f"METRIC_{metric_pos}")
                or display_style
            )

            metric_title_prefix = ""
            if str(metric_display_style).lower() == "graph6hr":
                metric_title_prefix = "6hr "
            elif str(metric_display_style).lower() == "graph24hr":
                metric_title_prefix = "24hr "

            tile_style = metric_tile_style_map.get(sid, "Theme")
            if tile_style not in METRIC_TILE_STYLES:
                tile_style = "Theme"
            lazy_metric_visual = not default_expanded and metric_pos > 6
            lazy_visual_attr = " data-lazy-metric-visual='1'" if lazy_metric_visual else ""
            yield f"<div class='metric-container' id='{safe_id}_container' data-sensor='{sid}' data-metric='{metric}' data-display-style='{metric_display_style}' data-tile-style='{html_escape(tile_style)}'{lazy_visual_attr}>"
            yield f"<div class='metric-title'>{metric_title_prefix}{html_escape(config.get('display_label') or metric)} ({config['unit']})</div>"

            yield "<div class='gauge-container'>"
            if not lazy_metric_visual:
                yield f"<div class='gauge-view'><canvas id='{safe_id}Gauge'></canvas></div>"
            yield "</div>"

            yield "<div class='graph-container'>"
            if not lazy_metric_visual:
                yield f"<div class='graph-view'>"
                yield f"<canvas class='micrograph-canvas' width='{layout['canvas_width']}' height='{layout['canvas_height']}'></canvas>"
                yield "</div>"
            yield "</div>"

            yield "<div class='metric-current-value'>"
            yield f"<span id='{safe_id}_val'>{display_text}</span>"
            yield _trend_arrow_html(stat_metric, stat.get("trend"))
            yield "</div>"

            yield f"<div class='metric-stats' id='{safe_id}_stats'>"

            yield f"<div>Min<br><small>{min_val} at<br>{min_ts}</small></div>"
            yield f"<div>Avg<br>{avg_val}</div>"
            yield f"<div>Max<br><small>{max_val} at<br>{max_ts}</small></div>"

            yield "</div>"  # metric-stats
            yield "</div>"  # metric-container

        yield "</div>"  # sensor-row
        yield "</div>"  # sensor-group

    selected_switch_locations: list[tuple[str, str]] = []
    if isinstance(sensor_id, str) and sensor_id.startswith("loc:"):
        loc_disp = sensor_id[4:].strip()
        loc_norm = _norm_loc(loc_disp)
        if switches_by_loc.get(loc_norm):
            selected_switch_locations.append((loc_norm, loc_display_map.get(loc_norm, loc_disp or "Unknown")))
    elif sensor_id and sensor_id != "All":
        loc_disp = (sensor_locations or {}).get(sensor_id) or ""
        loc_norm = _norm_loc(loc_disp)
        if switches_by_loc.get(loc_norm):
            selected_switch_locations.append((loc_norm, loc_display_map.get(loc_norm, loc_disp or "Unknown")))
    else:
        for loc_norm, disp in known_items:
            if switches_by_loc.get(loc_norm):
                selected_switch_locations.append((loc_norm, disp))

    if switch_row_order == "location_device":
        selected_switch_locations.sort(key=lambda item: item[1].strip().casefold())
    elif isinstance(switch_card_order, list):
        saved_positions = {key: index for index, key in enumerate(switch_card_order)}
        selected_switch_locations.sort(key=lambda item: saved_positions.get(item[0], len(saved_positions)))

    if selected_switch_locations:
        yield "<div class='sensor-group switch-group' id='group_switches' data-dashboard-row-id='__switches__' style='width:100%; flex:0 0 100%; display:block;'>"
        switch_header_styles = {switch_tile_style_map.get(loc, "Theme") for loc, _ in selected_switch_locations}
        switch_header_style = next(iter(switch_header_styles)) if len(switch_header_styles) == 1 else "Theme"
        if switch_header_style not in METRIC_TILE_STYLES:
            switch_header_style = "Theme"
        yield f"<div class='sensor-group-header' data-tile-style='{html_escape(switch_header_style)}'>"
        yield "<h3 id='switches_header' class='sensor-group-title'>SWITCHES</h3>"
        yield (
            "<div class='sensor-order-wrap' data-sensor-order='__switches__'>"
            "  <button type='button' class='sensor-order-item' data-move='up' data-row-id='__switches__' title='Move Switches row up' aria-label='Move Switches row up'><span aria-hidden='true'>↑</span></button>"
            "  <button type='button' class='sensor-order-item' data-move='down' data-row-id='__switches__' title='Move Switches row down' aria-label='Move Switches row down'><span aria-hidden='true'>↓</span></button>"
            "</div>"
        )
        yield "</div>"
        yield "<div class='sensor-row switches-row' id='row_switches'>"
        for loc_norm, disp in selected_switch_locations:
            yield from _render_switch_cards_for_location(disp, switches_by_loc.get(loc_norm, []))
        yield "</div>"
        yield "</div>"

    # --- z-index so toasts always appear above stacked modals ---
    yield f"<link rel='stylesheet' href='/ui_static/css/dashboard_notifications.css?v={APP_VERSION}'>"

    
    # Configuration is inert JSON; executable code lives in versioned assets.
    yield _json_script("dashboard-config", {
        "pressureAltitude": pressure_altitude,
        "pressureSensorContext": pressure_sensor_context or {},
        "gaugeConfig": gauge_config,
        "currentValues": all_values,
        "sensorStats": all_stats,
        "expectedGaugeMap": expected_gauge_map,
        "expectedDisplayStyleMap": expected_display_style_map or {},
        "metricTileStyleMap": metric_tile_style_map,
        "dashboardMetricSet": normalize_dashboard_metric_set(dashboard_metric_set),
        "metricCanvasWidth": int(layout['canvas_width']),
        "metricCanvasHeight": int(layout['canvas_height']),
        "astroData": astro_payload,
        "biodynamicData": biodynamic_payload,
        "weatherForecastProvider": weather_forecast_provider,
        "weatherForecastEnabled": weather_forecast_enabled,
        "isPiPlatform": is_pi_platform,
        "displayStyle": display_style_js,
        "sensorUnits": {metric: cfg.get("unit", "") for metric, cfg in get_gauge_config().items()},
        "settingsGearSvg": "".join(_settings_gear_svg_lines(indent="      ", aria_hidden=True)),
        "dashboardRowOrder": dashboard_row_order or [],
    })
    yield "<script type='module'>"
    yield f"import {{ initializeDashboard }} from '/ui_static/js/dashboard_runtime.js?v={APP_VERSION}';"
    yield f"import {{ pressureGaugeConfig }} from '/ui_static/js/pressure_gauges.js?v={APP_VERSION}';"
    yield f"import {{ startRainGauges }} from '/ui_static/js/rain_gauges.js?v={APP_VERSION}';"
    yield "initializeDashboard(JSON.parse(document.getElementById('dashboard-config').textContent), { pressureGaugeConfig, startRainGauges });"
    yield "</script>"

    yield "<div id='modal-host'></div>"
    yield "</div>"
    yield "<section class='dashboard-theme-view' id='dashboardThemeView' aria-label='Sensorius dashboard theme preview' hidden>"
    yield "  <div class='dashboard-theme-toolbar' aria-label='Choose a Sensorius dashboard theme'>"
    yield "    <div class='dashboard-theme-options' role='group' aria-label='Preview a dashboard theme'>"
    yield "      <button type='button' data-dashboard-preview-theme='leaf'>Default</button>"
    yield "      <button type='button' data-dashboard-preview-theme='root'>Root</button>"
    yield "      <button type='button' data-dashboard-preview-theme='leaf-crop'>Leaf</button>"
    yield "      <button type='button' data-dashboard-preview-theme='flower'>Flower</button>"
    yield "      <button type='button' data-dashboard-preview-theme='fruit'>Fruit</button>"
    yield "    </div>"
    yield "    <button type='button' class='dashboard-theme-return' id='closeDashboardThemeBtn'>Return to Dashboard</button>"
    yield "  </div>"
    yield "</section>"
    yield "</body></html>"

def core_helpers_html() -> str:
    """Return shared dashboard styles and client-side helper scripts."""
    # styles sit OUTSIDE <script> to avoid parser issues
    return (
        "<style>"
        ".toast-container{position:fixed; top:16px; left:50%; width:min(92vw,520px); transform:translateX(-50%); z-index:99999; display:flex; flex-direction:column; align-items:center; gap:8px; pointer-events:none}"
        ".toast-container.modal-pane-toast-container{position:absolute; top:12px; width:min(32rem,calc(100% - 1.5rem)); z-index:100000}"
        ".system-settings-content,.switch-dialog-content,.modal .split > section.panel{position:relative}"
        ".toast{max-width:100%; padding:10px 14px; border-radius:8px; background:#222; color:#fff; box-shadow:0 2px 10px rgba(0,0,0,.25); text-align:center; pointer-events:auto}"
        ".toast.ok{background:#1f693a}"
        ".toast.error{background:#8b0000}"
        ".toast.automation-notification-toast{background:#ef8c23;color:#000;cursor:pointer}"
        ".toast.email-failure-toast{cursor:pointer}"
        ".onboard-overlay{z-index:99990}"
        ".onboard-modal{position:relative; z-index:99991}"
        "</style>"
        "<script>"
        "(function(){"
        "  if (typeof window.getToastContainer !== 'function') {"
        "    window.getToastContainer = function(context){"
        "      function visible(el){"
        "        if (!el || !document.body.contains(el)) return false;"
        "        var style = window.getComputedStyle(el);"
        "        if (style.display === 'none' || style.visibility === 'hidden') return false;"
        "        var rect = el.getBoundingClientRect();"
        "        return rect.width > 0 && rect.height > 0;"
        "      }"
        "      var paneSelectors = ['.system-settings-content', '.switch-dialog-content', '.modal-body .split > section.panel'];"
        "      function paneFrom(root){"
        "        if (!root || !document.body.contains(root)) return null;"
        "        var selectors = paneSelectors;"
        "        for (var i = 0; i < selectors.length; i += 1) {"
        "          if (root.matches && root.matches(selectors[i]) && visible(root)) return root;"
        "          var pane = root.querySelector(selectors[i]);"
        "          if (visible(pane)) return pane;"
        "        }"
        "        return null;"
        "      }"
        "      function contextRoot(){"
        "        if (!context) return null;"
        "        var el = context.nodeType === 1 ? context : context.parentElement;"
        "        if (!el || !document.body.contains(el)) return null;"
        "        return el.closest('.modal-backdrop') || el.closest('.system-settings-shell, .modal') || el;"
        "      }"
        "      function activePane(){"
        "        var contextual = paneFrom(contextRoot());"
        "        if (contextual) return contextual;"
        "        var roots = Array.prototype.filter.call(document.querySelectorAll('.modal-backdrop, .system-settings-shell, .modal'), visible);"
        "        for (var i = roots.length - 1; i >= 0; i -= 1) {"
        "          var pane = paneFrom(roots[i]);"
        "          if (pane) return pane;"
        "        }"
        "        var panes = Array.prototype.filter.call(document.querySelectorAll(paneSelectors.join(',')), visible);"
        "        if (panes.length) return panes[panes.length - 1];"
        "        return null;"
        "      }"
        "      var pane = activePane();"
        "      if (pane) {"
        "        var children = Array.prototype.slice.call(pane.children);"
        "        var c = children.find(function(el){ return el.classList && el.classList.contains('toast-container') && el.classList.contains('modal-pane-toast-container'); });"
        "        if (!c) { c = document.createElement('div'); c.className = 'toast-container modal-pane-toast-container'; pane.appendChild(c); }"
        "        return c;"
        "      }"
        "      var bodyToast = document.querySelector('body > .toast-container:not(.modal-pane-toast-container)');"
        "      if (!bodyToast) { bodyToast = document.createElement('div'); bodyToast.className = 'toast-container'; document.body.appendChild(bodyToast); }"
        "      return bodyToast;"
        "    };"
        "  }"
        "  if (typeof window.showToast !== 'function') {"
        "    window.showToast = function(text, type, context){"
        "      var c = window.getToastContainer ? window.getToastContainer(context) : null;"
        "      if (!c) return;"
        "      var t = document.createElement('div'); t.className = 'toast ' + (type||''); t.textContent = text||'';"
        "      c.appendChild(t); setTimeout(function(){ t.remove(); if(!c.children.length) c.remove(); }, 2500);"
        "    };"
        "  }"
        "})();"
        "</script>"
    )

def render_graph_modal(switch_installed=None, gauge_config=None):
    """Yield the graph and switch-event modal markup and scripts."""
    # Read tz offset/name from system settings for client display/normalization
    try:
        from .saiSettings import saiSettings
        _sys = saiSettings()
        tz_offset = int(_sys.get_setting("Time", "TZ_OFFSET", 0) or 0)  # seconds (e.g., -21600)
        tz_name   = str(_sys.get_setting("Time", "TZ_NAME", "") or "")
    except Exception:
        tz_offset, tz_name = 0, ""

    import json as _json
    try:
        max_days = max(1, int(os.getenv("SENSORIUS_DB_RETENTION_DAYS", "90")))
    except Exception:
        max_days = 90

    # ---------- CSS ----------
    yield f"<link rel='stylesheet' href='/ui_static/css/graph_workspace.css?v={APP_VERSION}'>"

    # Switch selections are materialized with the page so remote and local
    # channel labels share the same canonical settings-manager path.
    switch_map: dict[str, list[str]] = {}
    if switch_installed:
        try:
            from .saiSwitchSettingsManager import SwitchSettingsManager

            sw_mgr = SwitchSettingsManager("switch_settings")
            for sid in sw_mgr.list_switches() or []:
                try:
                    doc = sw_mgr.load(sid) or {}
                    if hasattr(sw_mgr, "get_switch_channel_names"):
                        labels = list(sw_mgr.get_switch_channel_names(doc) or [])
                    else:
                        swblk = (doc or {}).get("Switch", {}) if isinstance(doc, dict) else {}
                        labels = []
                        for i in range(1, 7):
                            label = str(swblk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                            enabled = str(
                                swblk.get(f"SWITCH_{i}_ENABLE_PIN", swblk.get(f"SWITCH_{i}_EN", ""))
                                or ""
                            ).strip()
                            pin = str(swblk.get(f"SWITCH_{i}_PIN", "") or "").strip()
                            if label and (enabled or pin):
                                labels.append(label)
                    if labels:
                        switch_map[sid] = labels
                except Exception:
                    pass
        except Exception:
            pass

    # The former graph-setup dialog is intentionally replaced by the full-screen
    # workspace. Selections in this pane are live and are not persisted as sets.
    yield "<div id='fullscreen_graph_container' aria-label='Sensorius Graphum workspace'>"
    yield "  <header class='fullscreen-graph-header'>"
    yield "    <h1 class='fullscreen-graph-title'>Sensorius Graphum</h1>"
    yield "    <button id='fullscreen_graph_dashboard' title='Return to dashboard' onclick='closeFullscreenGraph()' type='button' aria-label='Return to dashboard'>&times;</button>"
    yield "  </header>"
    yield "  <div class='fullscreen-graph-body'>"
    yield "    <aside class='fullscreen-graph-controls' aria-label='Graph selections'>"
    yield "      <section class='graph-control-section'>"
    yield "        <h2 class='graph-control-heading'>Time range</h2>"
    yield "        <div class='fullscreen-time-grid'>"
    range_rows = [
        [("1Hr", "1h"), ("3Hr", "3h"), ("6Hr", "6h"), ("12Hr", "12h")],
        [("24Hr", "24h"), ("3Day", "3d"), ("7Day", "7d"), ("14Day", "14d")],
        [("30Day", "30d"), ("60Day", "60d"), ("90Day", "90d"), ("Custom", "custom")],
    ]

    def _range_visible(value: str) -> bool:
        match = re.fullmatch(r"(\d+)d", value)
        return not match or int(match.group(1)) <= max_days

    for range_options in range_rows:
        visible_options = [(label, val) for label, val in range_options if val == "custom" or _range_visible(val)]
        if not visible_options:
            continue
        for label, val in visible_options:
            checked_attr = " checked" if val == "24h" else ""
            custom_flag = "true" if val == "custom" else "false"
            yield (
                f"          <label><input type='radio' name='range' value='{val}'{checked_attr} "
                f"onchange='toggleCustomTime({custom_flag}); scheduleFullscreenGraphRefresh()'>"
                f"<span>{label}</span></label>"
            )
    yield "        </div>"
    yield "        <div id='custom_time_inputs' class='custom-time-inputs' style='display:none;'>"
    yield "        <label for='start_time'>Start:</label><input type='datetime-local' id='start_time'>"
    yield "        <label for='end_time'>End:</label><input type='datetime-local' id='end_time'>"
    yield "        </div>"
    yield "        <div class='fullscreen-astral-field'>"
    yield "          <label for='astral_select'>Astral</label>"
    yield "          <select id='astral_select' title='Astral graph selection' onchange='scheduleFullscreenGraphRefresh()'>"
    yield "        <option value='none'>None</option>"
    yield "        <option value='sun'>Sun</option>"
    yield "        <option value='moon'>Moon</option>"
    yield "        <option value='sun_moon'>Sun &amp; Moon</option>"
    yield "          </select>"
    yield "        </div>"
    yield "      </section>"
    yield "      <section class='graph-control-section'>"
    yield "        <h2 class='graph-control-heading'><span>Sensors &amp; metrics</span><span id='graphSelectionCount'>0 of 4</span></h2>"
    yield "        <div id='fullscreenSensorOptions' class='graph-option-list'><div class='graph-control-note'>Loading sensors…</div></div>"
    yield "      </section>"
    yield "      <section class='graph-control-section'>"
    yield "        <h2 class='graph-control-heading'>Switches</h2>"
    yield "        <div id='fullscreenSwitchOptions' class='graph-option-list'></div>"
    yield "        <p class='graph-control-note'>Select up to four observations total. Switches appear as ON/OFF transition markers.</p>"
    yield "      </section>"
    yield "    </aside>"
    yield "    <main class='fullscreen-graph-main'>"
    yield "      <div class='fullscreen-graph-summary'>"
    yield "        <div><h2 id='fullscreenGraphRangeTitle'>Last 24 hours</h2><span id='graphLoadingState'>Updating graph…</span></div>"
    yield "        <div id='fullscreenSelectedChips' aria-live='polite'></div>"
    yield "      </div>"
    yield "      <div id='fullscreen_graph_stack'>"
    yield "        <div id='fullscreen_data_panel' class='fullscreen-chart-panel'><canvas id='fullscreen_graph'></canvas></div>"
    yield "        <div id='fullscreen_astral_panel' class='fullscreen-chart-panel'><canvas id='fullscreen_astral_graph' aria-label='Astral sun and moon position graph'></canvas></div>"
    yield "      </div>"
    yield "      <div id='switch_legend' style='display:flex; flex-wrap:wrap; justify-content:center; gap:1rem; margin-top:.45rem;'></div>"
    yield "    </main>"
    yield "  </div>"
    yield "</div>"

    # ---------- Scripts ----------
    yield _json_script("graph-config", {
        "TZ_OFFSET_S": tz_offset,
        "TZ_NAME": tz_name,
        "GRAPH_GAUGE_CONFIG": gauge_config or get_gauge_config(),
        "SWITCH_MAP": switch_map or {},
    })
    yield f"<script src='/ui_static/js/graph_workspace.js?v={APP_VERSION}'></script>"
