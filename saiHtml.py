"""HTML rendering helpers, UI fragments, and dashboard display metadata.

This module centralizes Python-rendered HTML helpers used across the Sensorius
web UI. It provides shared naming and branding constants, SVG fragments, metric
canonicalization helpers, gauge configuration metadata, and other small
presentation utilities reused by routes and templates.
"""
import os
import re
from saiUtils import printDM, debug_enabled, html_escape, normalize_hostname_base, mdns_hostname
from saiBiodynamics import get_biodynamic_payload, get_skyfield_runtime_if_installed
from sensor_modules.station_weewx import DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID, WEEWX_GAUGE_CONFIG
from collections import defaultdict
from pathlib import Path
try:
    from __init__ import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "v0.0.0"

MODULE = "saiHtml"
DEBUG = debug_enabled(MODULE)

APP_TITLE = "Sensorius"
APP_NAME_SHORT = f"{APP_TITLE} AI"
APP_NAME_LONG = f"{APP_TITLE} Automatio Instrumentorum"


def _settings_gear_svg_lines(*, indent: str = "", aria_label: str | None = "Settings", aria_hidden: bool = False):
    attrs = [
        "xmlns='http://www.w3.org/2000/svg'",
        "width='14'",
        "height='14'",
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
            return key

    return name

def get_gauge_config():
    gauge_config = {
        "Air Quality": {"unit": "AQI", "min": 0, "max": 500, "ticks": [0, 50, 100, 150, 200, 300, 400, 500], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 50}, {"strokeStyle": "#ffcc00", "min": 50, "max": 100}, {"strokeStyle": "#ffa500", "min": 100, "max": 150}, {"strokeStyle": "#ff0000", "min": 150, "max": 200}, {"strokeStyle": "#800080", "min": 200, "max": 300}, {"strokeStyle": "#800000", "min": 300, "max": 500}]},
        "Gas": {"unit": "Ω", "min": 500, "max": 2000500, "ticks": [500, 500500, 1000500, 1500500, 2000500], "zones": [{"strokeStyle": "#f3d2fc", "min": 500, "max": 2000500}]},
        "CO2": {"unit": "ppm", "min": 0, "max": 3000, "ticks": [0, 200, 400, 800, 1200, 1600, 2000, 3000], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 200}, {"strokeStyle": "#ffcc00", "min": 200, "max": 400}, {"strokeStyle": "#66cc66", "min": 400, "max": 1600}, {"strokeStyle": "#ffcc00", "min": 1600, "max": 2000}, {"strokeStyle": "#f00", "min": 2000, "max": 3000}]},
        "Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Dew Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Dew Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Dew Point Deficit": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Ambient VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Baro-Pressure": {"unit": "hPa", "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
        "Temperature_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Plant Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Plant Dew Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Dew Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Dew Point Deficit": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "Plant DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Plant VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Plant Baro-Pressure": {"unit": "hPa", "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
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
    gauge_config.update(WEEWX_GAUGE_CONFIG)
    return gauge_config

def render_dashboard(sensor_id, sensor, available, all_values, all_stats, mqtt_ingest, switch_controllers=None, sensor_locations=None, gauge_config=None, gauge_size="Small", expected_gauge_map=None, expected_display_style_map=None, display_style=None, astro_payload=None, biodynamic_payload=None):

    import json
    import os
    import re
    import sys
    import math
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from collections import defaultdict
    from saiUtils import get_timestamp
    from saiSettings import saiSettings
    import saiAddDevice
    from saiSensorSettingsManager import SensorSettingsManager
    from saiHtml import render_graph_modal
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
    from saiSwitchSettingsManager import SwitchSettingsManager

    sw_mgr = None
    try:
        sw_mgr = SwitchSettingsManager("switch_settings")
    except Exception:
        pass

    # Initialize buckets
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
                        cid = str(row.get("channel_id", "") or "").strip()
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
                cid = str(row.get("channel_id", "") or "").strip()
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
            from saiSettings import saiSettings
            sys_settings = saiSettings()
            display_style = sys_settings.get_displayStyle()  # "Gauge" or "Graph6hr" or "Graph24hr"
        except Exception:
            display_style = "Gauge"

    display_style = (display_style or "Gauge")
    display_style_js = display_style.lower()

    yield "<!DOCTYPE html>"
    yield f"<html><head><title>{APP_NAME_LONG}</title>"
    yield "<meta charset='UTF-8'>"
    yield "<script src='https://cdn.jsdelivr.net/npm/gaugeJS@1.3.7/dist/gauge.min.js'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns'></script>"
    yield "<script src='https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@1.4.0'></script>"
    yield from render_graph_modal(switch_installed=switch_installed)
    # global assets for templates
    yield "<link rel='stylesheet' href='/ui_static/css/app.css'>"
    yield f"<script src='/ui_static/js/draggable_modals.js?v={APP_VERSION}'></script>"
    yield f"<script type='module' src='/ui_static/js/advanced_automation.js?v={APP_VERSION}'></script>"
    yield "<script src='/ui_static/js/sensor_settings_modal.js'></script>"
    yield "</head><body>"
    yield (
      f"<div class='dashboard' "
      f"style='--container-width:{layout['container_width']};"
      f"--stats-font:{layout['stats_font']};"
      f"--canvas-width:{layout['canvas_width']};"
      f"--canvas-height:{layout['canvas_height']};'>"
    )    
    # Sensor settings resolution and lookup map
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
        label_counts: dict[str, int] = defaultdict(int)

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
                label_counts[str(label or "").strip().lower()] += 1
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
        yield f"<div class='switch-metric-container' data-switch-ids='{switch_ids_attr}'>"
        yield "<div style='text-align:center; width:100%; margin-top:-1.5rem; margin-bottom:-1.0rem;'>"
        if not multi_switch_card and switch_ids_here:
            header_sw_id = switch_ids_here[0]
            yield f"<h3 id='{header_id}'>{header_sw_id.upper()} "
            yield f"  <a href='javascript:void(0)' onclick='editSwitchSettings(\"{header_sw_id}\")' title='Open {header_sw_id} Settings' style='margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
            yield from _settings_gear_svg_lines(indent="    ")
            yield "  </a>"
            yield f"{location}</h3>"
        else:
            header_devices = ", ".join(sw.upper() for sw in switch_ids_here if sw)
            yield f"<h3 id='{header_id}'>SWITCHES <span style='font-size:0.72em; font-weight:normal;'>{header_devices}</span> {location}</h3>"
        yield "</div>"

        yield "<div class='switch-container'>"
        yield "<div class='switch-view'>"
        yield "<table class='switch-table'>"
        yield "<thead><tr>"
        yield "<th>Switch</th><th>State</th><th>Events</th>"
        yield "</tr></thead>"
        yield "<tbody>"

        if not any(not row.get("empty") for row in switch_rows):
            yield "<tr><td colspan='3' style='opacity:0.7;'>No enabled switch channels</td></tr>"

        for row in switch_rows:
            if row.get("empty"):
                continue

            switch_ctrl = row["switch_ctrl"]
            sw_id = row["sw_id"]
            label = row["label"]
            safe_label = label.lower().replace(" ", "_")
            is_on = bool(getattr(switch_ctrl, "last_state", {}).get(label, False))
            if DEBUG and str(sw_id).strip().lower() == "sensoria-hub-0" and str(label).strip().lower() == "fan":
                printDM(
                    f"[render_dashboard] {sw_id}::{label} render last_state={is_on}",
                    location=MODULE,
                )
            state_str = "on" if is_on else "off"
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
                from saiAutomationManager import AutomationManager
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

            label_key = label_norm.lower()
            display_label = label
            if label_counts.get(label_key, 0) > 1 and sw_id:
                display_label = f"{label} ({sw_id.upper()})"

            label_cell = html_escape(display_label)
            if multi_switch_card and sw_id:
                label_cell += (
                    f" <a href='javascript:void(0)' onclick='editSwitchSettings(\"{sw_id}\")' "
                    f"title='Open {sw_id} Settings' style='margin-left:4px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
                )
                label_cell += "".join(_settings_gear_svg_lines(indent="", aria_hidden=True))
                label_cell += "</a>"

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
                f"  class='button {'green' if is_on else 'black'}' "
                f"  title='{'Automation enabled. Disable automation to toggle manually.' if automation_enabled else f'Toggle state for {label}'}' "
                f"  data-switch-name='{label}' "
                f"  data-switch-key='{switch_key}' "
                f"  data-switch-id='{sw_id}' "
                f"  data-automation-switch-id='{getattr(switch_ctrl, 'switch_id', '')}' "
                f"  data-automation-label='{label_norm}' "
                f"  data-automation-enabled='{'1' if automation_enabled else '0'}' "
                f"  data-state='{state_str}' "
                f"  onclick='toggleSwitchInline(this)'>"
                f"{'On' if is_on else 'Off'}"
                f"</button>"
            )
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
            yield f"  <div id='{events_id}' class='switch-events' role='listbox' aria-label='Recent switch events'>"
            yield f"    <ul id='{events_list_id}' class='switch-events-list' data-switch-key='{switch_key}'></ul>"
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
        Try to access the live sensor objects (so we can read `sensor.meas_status`).
        Returns either a dict {sensor_id: obj} or an iterable of objs (each with .sensor_id).
        """
        try:
            import saiWebRoutes as routes
            return getattr(routes, "sensor_map", None)
        except Exception:
            return None

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
        Returns one of: 'online' | 'degraded' | 'offline' | 'unknown' | 'migration_required'
        """
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

        # 3) WeeWX archive/MQTT station data does not always have a live sensor object
        # or Nodus heartbeat, so a row with rendered values should not show unknown.
        try:
            if sid_text.lower() == WEEWX_DEFAULT_SENSOR_ID.lower() or sid_text.lower().startswith("weewx"):
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

    yield "<div style='text-align:center; width:100%;'>"

    yield "<h2 id='sensor_header'>"
    yield "<a href='javascript:void(0)' onclick='openGraphModal()' title='View Graph' style='margin-right:8px; vertical-align:middle;'>"
    yield "  <svg xmlns='http://www.w3.org/2000/svg' width='22' height='22' viewBox='0 0 24 24' role='img'>"
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
    yield f" {APP_NAME_SHORT} "
    yield "<a href='#' onclick='window.editSystemSettings && window.editSystemSettings(); return false;' title='Open System Settings' style='margin-left:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
    yield from _settings_gear_svg_lines(indent="    ")
    yield "</a>"
    yield "</h2>"
  
    yield "<p id='update_time'>--</p>"

    yield "<style>"
    yield ".dash-top-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(206px,230px));justify-content:center;align-items:stretch;column-gap:.75rem;row-gap:.75rem;margin:1rem auto 0;width:min(100%,1198px);}"
    yield ".dash-left-col,.dash-right-col,.dash-side-col{display:contents;}"
    yield ".dash-loc-form{display:flex;flex-direction:column;align-items:stretch;justify-content:flex-start;gap:.45rem;background:#e6faff;border:1px solid #c9ddff;border-radius:10px;padding:.45rem .65rem .55rem;min-height:160px;min-width:230px;width:230px;box-sizing:border-box;}"
    yield ".dash-loc-head{display:flex;align-items:center;justify-content:space-between;gap:.1rem;}"
    yield ".dash-loc-label{font-size:.78rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;opacity:.85;}"
    yield ".astro-box{display:flex;align-items:flex-start;justify-content:center;background:#ffffe0;border:1px solid #ccc;border-radius:10px;padding:.45rem .55rem;min-height:176px;box-sizing:border-box;}"
    yield ".dash-loc-form select{background:#ffffe0;border:1px solid #ccc;}"
    yield ".astro-card{display:flex;flex-direction:column;align-items:center;gap:.2rem;min-width:132px;}"
    yield ".astro-title{font-size:.78rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;opacity:.8;}"
    yield ".astro-meta{font-size:.74rem;line-height:1.25;text-align:center;color:#27313a;min-height:1.9em;white-space:normal;}"
    yield "#sunBox .astro-card{width:100%;min-width:0;box-sizing:border-box;}"
    yield "#moonBox .astro-card{width:100%;min-width:0;align-items:stretch;box-sizing:border-box;}"
    yield "#weatherForecastBox{width:230px;box-sizing:border-box;overflow:hidden;background:#e8f3ff;}"
    yield "#weatherForecastBox .astro-card{width:100%;min-width:0;align-items:stretch;box-sizing:border-box;text-align:left;}"
    yield "#weatherForecastBox .astro-title{width:100%;text-align:center;}"
    yield ".forecast-status{font-size:.55rem;line-height:1.1;text-align:center;color:#51616f;min-height:1.1em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
    yield ".forecast-current{border:1px solid #cfdce8;border-radius:8px;background:#fff;padding:.42rem .48rem;display:flex;flex-direction:column;gap:.3rem;min-width:0;}"
    yield ".forecast-current-summary{font-size:.68rem;line-height:1.16;color:#27313a;text-align:center;min-height:2.35em;overflow-wrap:anywhere;}"
    yield ".forecast-current-grid{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.15rem .4rem;align-items:start;font-size:.64rem;line-height:1.12;}"
    yield ".forecast-current-grid dt{font-weight:700;color:#52606d;margin:0;}"
    yield ".forecast-current-grid dd{margin:0;color:#27313a;overflow-wrap:anywhere;}"
    yield ".forecast-wind-value{white-space:normal;}"
    yield ".forecast-wind-line{display:block;}"
    yield ".forecast-card-actions{margin-top:auto;padding-top:.35rem;display:flex;justify-content:center;}"
    yield ".forecast-open-btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;min-width:136px;border:1px solid #2c5e8f;border-radius:999px;background:#f6fbff;color:#1f496f;padding:.38rem .72rem;font-size:.66rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;cursor:pointer;transition:filter .12s ease,box-shadow .12s ease;white-space:nowrap;}"
    yield ".forecast-open-btn:hover{filter:brightness(1.04);box-shadow:0 1px 3px rgba(0,0,0,.16);}"
    yield ".forecast-open-btn:disabled{opacity:.75;cursor:wait;}"
    yield ".forecast-open-btn .spinner{width:12px;height:12px;border-width:2px;display:none;}"
    yield ".forecast-open-btn.loading .spinner{display:inline-block;}"
    yield "#bioBox{width:230px;box-sizing:border-box;overflow:hidden;}"
    yield "#bioBox .astro-card{width:100%;min-width:0;align-items:stretch;box-sizing:border-box;}"
    yield "#bioBox .astro-title,#bioCurrentSign,#bioCurrentElement,.bio-window,#bioUpcoming{width:100%;box-sizing:border-box;}"
    yield ".moon-layout{width:100%;max-width:100%;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr) 88px minmax(0,1fr);align-items:center;column-gap:.35rem;}"
    yield ".moon-side{font-size:.64rem;line-height:1.14;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:0;}"
    yield ".moon-side.left{text-align:left;}"
    yield ".moon-side.right{text-align:right;}"
    yield ".moon-label{display:block;opacity:.82;}"
    yield ".moon-value{display:block;font-weight:600;margin-bottom:.2rem;}"
    yield "#moonMeta{white-space:nowrap;padding:0 4px;text-align:center;font-size:.69rem;}"
    yield ".bio-main{display:flex;flex-direction:column;align-items:center;gap:.08rem;width:100%;align-self:center;min-width:0;overflow:hidden;text-align:center;}"
    yield "#bioCurrentSign{font-size:.74rem;font-weight:700;line-height:1.02;color:#27313a;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
    yield "#bioCurrentElement{font-size:.74rem;color:#49545d;line-height:1.02;}"
    yield ".bio-window{font-size:.58rem;line-height:1.15;text-align:center;color:#3c464d;min-height:1.35em;padding-top:.16rem;}"
    yield "#bioDateLine{font-size:.74rem;line-height:1.02;padding-top:0;}"
    yield "#bioWindow{font-size:.70rem;line-height:1.15;min-height:2.65em;white-space:pre;overflow-wrap:normal;word-break:normal;font-variant-numeric:tabular-nums;}"
    yield "#bioUpcoming{display:none;font-size:.55rem;line-height:1.1;text-align:center;color:#3c464d;min-height:1.3em;overflow-wrap:anywhere;}"
    yield ".bio-hint{padding-top:.2rem;font-size:.54rem;line-height:1.1;text-align:center;color:#6b7280;letter-spacing:.02em;}"
    yield ".bio-card-actions{margin-top:auto;padding-top:.35rem;display:flex;justify-content:center;}"
    yield ".bio-open-btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;min-width:118px;border:1px solid #27313a;border-radius:999px;background:#fff7d6;color:#27313a;padding:.4rem .8rem;font-size:.7rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;cursor:pointer;transition:filter .12s ease,box-shadow .12s ease;}"
    yield ".bio-open-btn:hover{filter:brightness(1.08);box-shadow:0 1px 3px rgba(0,0,0,.18);}"
    yield ".bio-open-btn:disabled{opacity:.75;cursor:wait;}"
    yield ".bio-open-btn .spinner{width:12px;height:12px;border-width:2px;display:none;}"
    yield ".bio-open-btn.loading .spinner{display:inline-block;}"
    yield ".bio-month{font-size:.9rem;font-weight:700;text-align:center;color:#27313a;padding-top:.15rem;}"
    yield ".bio-calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;margin-top:.35rem;}"
    yield ".bio-weekday{font-size:.53rem;font-weight:700;text-align:center;opacity:.7;text-transform:uppercase;}"
    yield ".bio-day{min-height:43px;height:43px;border:1px solid #d7d0bf;border-radius:6px;padding:3px;background:#fff;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;box-sizing:border-box;}"
    yield ".bio-day.out{opacity:.62;filter:saturate(.42) brightness(1.02);}"
    yield ".bio-day.today:not(.selected){box-shadow:inset 0 0 0 1px rgba(39,49,58,.45);}"
    yield ".bio-day-num{font-size:.66rem;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.88);border:1px solid rgba(39,49,58,.18);color:#27313a;box-shadow:0 1px 2px rgba(39,49,58,.18);}"
    yield ".bio-day-meta{width:100%;font-size:.49rem;font-weight:700;line-height:1.05;color:#27313a;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-shadow:0 1px 0 rgba(255,253,246,.55);}"
    yield ".bio-day.noted{position:relative;}"
    yield ".bio-day.noted::after{content:'';position:absolute;right:3px;bottom:3px;width:5px;height:5px;border-radius:50%;background:#27313a;opacity:.85;}"
    yield ".bio-day.selected{box-shadow:inset 0 0 0 2px #27313a, 0 0 0 1px #27313a;}"
    yield ".bio-modal{width:min(760px,92vw);max-width:760px;padding:0;overflow:hidden;}"
    yield ".bio-modal .modal-header{display:flex;align-items:center;justify-content:space-between;padding:.85rem 1rem;border-bottom:1px solid #ddd;background:#f7f2cf;}"
    yield ".bio-modal .modal-title{margin:0;font-size:1rem;letter-spacing:.02em;text-transform:uppercase;}"
    yield ".bio-modal .modal-body{padding:1rem;background:#fffdf6;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:1rem;}"
    yield ".bio-modal-main{min-width:0;}"
    yield ".bio-modal-summary{display:flex;flex-direction:column;gap:.2rem;align-items:center;text-align:center;font-size:.82rem;line-height:1.2;color:#27313a;overflow-wrap:anywhere;}"
    yield ".bio-modal-side{display:grid;grid-template-columns:minmax(0,1fr);gap:.55rem;align-items:start;align-content:start;}"
    yield ".bio-nav{display:flex;align-items:center;justify-content:space-between;gap:.5rem;}"
    yield ".bio-nav-btn{border:1px solid #c6bb8f;border-radius:8px;background:#fff7d6;padding:.35rem .6rem;font-weight:700;cursor:pointer;}"
    yield ".bio-modal-actions{display:flex;justify-content:center;gap:.45rem;flex-wrap:wrap;margin-top:.55rem;}"
    yield ".bio-print-btn{border:1px solid #c6bb8f;border-radius:8px;background:#fff7d6;color:#27313a;padding:.35rem .7rem;font-weight:700;cursor:pointer;}"
    yield ".bio-note-card{border:1px solid #d4cfbf;border-radius:10px;background:#f7f8fa;padding:.75rem;display:flex;flex-direction:column;gap:.45rem;}"
    yield ".bio-note-date{font-size:.8rem;font-weight:700;color:#27313a;}"
    yield ".bio-note-meta{font-size:.72rem;color:#4f5961;min-height:1.1em;}"
    yield ".bio-note-title{font-size:.72rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;color:#4f5961;}"
    yield ".bio-note-input{width:100%;height:86px;max-height:86px;resize:none;overflow-y:auto;border:1px solid #c8ccd0;border-radius:8px;padding:.65rem;font-size:.625rem;line-height:1.3;background:#fff;box-sizing:border-box;text-align:left;}"
    yield ".bio-summary-card .bio-summary-output{height:78px;max-height:78px;}"
    yield ".bio-summary-output{width:100%;height:58px;max-height:58px;overflow-y:auto;border:1px solid #c8ccd0;border-radius:8px;padding:.65rem;background:#fff;box-sizing:border-box;font-size:.625rem;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.3;color:#27313a;text-align:left;user-select:text;-webkit-user-select:text;}"
    yield ".bio-note-actions{display:flex;align-items:center;justify-content:space-between;gap:.5rem;}"
    yield ".bio-save-btn{border:1px solid #9da7af;border-radius:8px;background:#27313a;color:#fff;padding:.4rem .7rem;font-weight:700;cursor:pointer;}"
    yield ".bio-note-status{font-size:.72rem;color:#4f5961;min-height:1.1em;}"
    yield ".bio-print-sheet{display:none;}"
    yield ".forecast-modal{width:min(720px,92vw);max-width:720px;padding:0;overflow:hidden;}"
    yield ".forecast-modal .modal-header{display:flex;align-items:center;justify-content:space-between;padding:.85rem 1rem;border-bottom:1px solid #cfdce8;background:#dceeff;}"
    yield ".forecast-modal .modal-title{margin:0;font-size:1rem;letter-spacing:.02em;text-transform:uppercase;}"
    yield ".forecast-modal .modal-body{padding:1rem;background:#f7fbff;}"
    yield ".forecast-modal-meta{font-size:.72rem;color:#51616f;text-align:center;margin-bottom:.65rem;}"
    yield ".forecast-days{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.55rem;}"
    yield ".forecast-day{border:1px solid #cfdce8;border-radius:8px;background:#fff;padding:.65rem;min-width:0;display:flex;flex-direction:column;gap:.35rem;}"
    yield ".forecast-day-label{font-size:.75rem;font-weight:800;color:#1f496f;line-height:1.1;}"
    yield ".forecast-day-summary{font-size:.72rem;line-height:1.25;color:#27313a;min-height:2.4em;}"
    yield ".forecast-day-grid{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.22rem .45rem;align-items:start;font-size:.66rem;line-height:1.18;}"
    yield ".forecast-day-grid dt{font-weight:700;color:#52606d;margin:0;}"
    yield ".forecast-day-grid dd{margin:0;color:#27313a;overflow-wrap:anywhere;}"
    yield ".bio-print-title{font-size:18pt;font-weight:700;margin:0 0 .15rem;color:#27313a;text-transform:uppercase;text-align:left;}"
    yield ".bio-print-subtitle{font-size:10pt;color:#4f5961;margin:0 0 .45rem;text-align:left;}"
    yield ".bio-print-calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin-bottom:.35rem;}"
    yield ".bio-print-weekday{font-size:8pt;font-weight:700;text-align:center;text-transform:uppercase;color:#4f5961;padding-bottom:1px;}"
    yield ".bio-print-day{min-height:58px;border:1px solid #d7d0bf;border-radius:6px;padding:4px;background:#fff;box-sizing:border-box;page-break-inside:avoid;break-inside:avoid;}"
    yield ".bio-print-day.out{opacity:.62;filter:saturate(.42) brightness(1.02);}"
    yield ".bio-print-day.today{outline:2px solid #27313a;outline-offset:-2px;}"
    yield ".bio-print-day-head{display:flex;align-items:flex-start;justify-content:space-between;gap:3px;margin-bottom:2px;}"
    yield ".bio-print-day-num{font-size:9pt;font-weight:800;line-height:1;display:inline-flex;align-items:center;justify-content:center;min-width:1.45em;height:1.45em;border-radius:999px;background:rgba(255,253,246,.9);border:1px solid rgba(39,49,58,.18);color:#27313a;}"
    yield ".bio-print-day-part{font-size:7pt;font-weight:700;color:#4f5961;text-transform:uppercase;text-align:right;}"
    yield ".bio-print-day-meta{font-size:6.5pt;line-height:1.1;color:#4f5961;}"
    yield ".bio-print-sections{display:grid;grid-template-columns:1fr;gap:.5rem;text-align:left;justify-items:stretch;}"
    yield ".bio-print-entry{border:1px solid #d4cfbf;border-radius:10px;padding:.65rem;background:#fff;page-break-inside:avoid;break-inside:avoid;text-align:left;}"
    yield ".bio-print-entry.selected{border-color:#27313a;box-shadow:inset 0 0 0 1px #27313a;}"
    yield ".bio-print-entry-head{display:flex;align-items:baseline;justify-content:space-between;gap:.5rem;margin-bottom:.35rem;}"
    yield ".bio-print-entry-date{font-size:11pt;font-weight:700;color:#27313a;}"
    yield ".bio-print-entry-meta{font-size:9pt;color:#4f5961;text-align:right;}"
    yield ".bio-print-label{font-size:8pt;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:#4f5961;margin:.4rem 0 .15rem;text-align:left;}"
    yield ".bio-print-block{font-size:9pt;line-height:1.35;color:#27313a;white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.2em;text-align:left;}"
    yield ".astro-times{width:204px;position:relative;height:1.1em;min-height:1.1em;font-variant-numeric:tabular-nums;margin:0 auto;}"
    yield ".astro-times span{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap;}"    
    yield ".moon-position-times{height:1.05em;min-height:1.05em;font-size:.7rem;}"
    yield ".moon-position-title{font-size:.7rem;margin-top:.02rem;}"
    yield "#sunPathCanvas{display:block;width:204px;height:108px;margin:0 auto;border:1px solid #d5c7a8;border-radius:8px;background:#dff1ff;}"
    yield "#moonPhaseCanvas{width:88px;height:88px;border:1px solid #d5c7a8;border-radius:50%;background:#081322;}"
    yield ".moon-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-bottom:.25rem;}"
    yield ".moon-head .astro-title{margin-bottom:0;}"
    yield ".moon-view-toggle{display:inline-flex;align-items:center;gap:.14rem;padding:.1rem;border:1px solid #d7cfb8;border-radius:999px;background:#f7f1c9;flex-shrink:0;}"
    yield ".moon-view-btn{border:0;border-radius:999px;background:transparent;color:#4f5961;padding:.12rem .38rem;font-size:.58rem;font-weight:700;letter-spacing:.02em;cursor:pointer;line-height:1.1;}"
    yield ".moon-view-btn.active{background:#2e4f89;color:#fff;box-shadow:0 1px 2px rgba(0,0,0,.18);}"
    yield "@media (max-width: 760px){.dash-top-row{grid-template-columns:1fr;justify-items:center}.dash-left-col,.dash-right-col,.dash-side-col{display:block;width:100%;align-items:center}#sunPathCanvas{width:184px;height:98px}.astro-times{width:184px}.astro-card{min-width:120px}.dash-loc-form,.astro-box{min-height:unset}#sunBox .astro-card,#moonBox .astro-card,#bioBox .astro-card,#weatherForecastBox .astro-card,.dash-loc-form{width:206px;min-width:0}.moon-layout{grid-template-columns:minmax(0,1fr) 78px minmax(0,1fr);column-gap:.2rem}#moonPhaseCanvas{width:78px;height:78px}.moon-side{font-size:.6rem}#moonMeta{font-size:.64rem}.forecast-open-btn{min-width:128px;font-size:.62rem}.bio-day{min-height:39px;height:39px}.bio-day-meta{font-size:.45rem}.bio-modal .modal-body{grid-template-columns:1fr}.bio-modal-side{grid-template-columns:1fr}.bio-note-input{height:84px;max-height:84px;font-size:.47rem}.bio-summary-card .bio-summary-output{height:68px;max-height:68px}.bio-summary-output{height:52px;max-height:52px;font-size:.47rem}}"
    yield "@media print{@page{margin:.2in}@page bio-calendar{size:landscape;margin:.2in}@page bio-notes{size:portrait;margin:.35in}body.bio-printing *{visibility:hidden !important}body.bio-printing #bioPrintCalendarSheet,body.bio-printing #bioPrintCalendarSheet *{visibility:visible !important}body.bio-printing #bioPrintNotesSheet,body.bio-printing #bioPrintNotesSheet *{visibility:visible !important}body.bio-printing #bioPrintCalendarSheet,body.bio-printing #bioPrintNotesSheet{display:block !important;position:absolute;left:0;top:0;width:100%;padding:.08in;background:#fff;color:#000;box-sizing:border-box}body.bio-print-calendar-mode #bioPrintCalendarSheet{display:block !important;page:bio-calendar}body.bio-print-calendar-mode #bioPrintNotesSheet{display:none !important}body.bio-print-notes-mode #bioPrintNotesSheet{display:block !important;page:bio-notes}body.bio-print-notes-mode #bioPrintCalendarSheet{display:none !important}body.bio-print-calendar-mode .bio-print-calendar{gap:3px}body.bio-print-calendar-mode .bio-print-day{min-height:54px}body.bio-print-notes-mode .bio-print-sections{gap:.35rem}body.bio-print-notes-mode .bio-print-entry{break-inside:avoid;page-break-inside:avoid}}"
    yield "</style>"

    yield "<div class='dash-top-row'>"
    yield "<div class='dash-left-col'>"
    yield "<div class='astro-box' id='weatherForecastBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>24 Hour Forecast</div>"
    yield "    <div class='forecast-status' id='forecastStatus'>Loading forecast...</div>"
    yield "    <div class='forecast-current'>"
    yield "      <div class='forecast-current-summary' id='forecastOverall'>Loading...</div>"
    yield "      <dl class='forecast-current-grid'>"
    yield "        <dt>Temp</dt><dd id='forecastTempRange'>--</dd>"
    yield "        <dt>RH</dt><dd id='forecastRhRange'>--</dd>"
    yield "        <dt>Wind</dt><dd id='forecastWind' class='forecast-wind-value'>--</dd>"
    yield "      </dl>"
    yield "    </div>"
    yield "    <div class='forecast-card-actions'>"
    yield "      <button type='button' class='forecast-open-btn' id='forecastFiveDayBtn' aria-label='Open six day weather forecast' title='6 Day Forecast'>"
    yield "        <span class='spinner' aria-hidden='true'></span>"
    yield "        <span class='forecast-open-btn-label'>6 Day Forecast</span>"
    yield "      </button>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"
    yield "<div class='astro-box' id='bioBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Biodynamic Calendar</div>"
    yield "    <div class='bio-window' id='bioDateLine'>Loading biodynamic date...</div>"
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
    yield "    <div class='moon-head'>"
    yield "      <div class='astro-title'>Moon Phase</div>"
    yield "      <div class='moon-view-toggle' role='group' aria-label='Moon view mode' title='Local sky view or Reference moon diagram'>"
    yield "        <button type='button' class='moon-view-btn active' id='moonViewLocal' data-moon-view='local' aria-pressed='true' title='Local sky view or Reference moon diagram'>Local</button>"
    yield "        <button type='button' class='moon-view-btn' id='moonViewReference' data-moon-view='reference' aria-pressed='false' title='Local sky view or Reference moon diagram'>Ref</button>"
    yield "      </div>"
    yield "    </div>"
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
    yield "  </div>"
    yield "</div>"
    yield "<div class='astro-box' id='sunBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Sun Position</div>"
    yield "    <div class='astro-meta astro-times' id='sunMeta'><span id='sunTimeRise'>--</span><span id='sunTimeNoon'>--</span><span id='sunTimeSet'>--</span></div>"
    yield "    <canvas id='sunPathCanvas' width='204' height='108' aria-label='24 hour sun and moon position chart'></canvas>"
    yield "    <div class='astro-meta astro-times moon-position-times' id='moonPositionMeta'><span id='moonTimeRise'>--</span><span id='moonTimeSet'>--</span></div>"
    yield "    <div class='astro-title moon-position-title'>Moon Position</div>"
    yield "  </div>"
    yield "</div>"
    # treat any non 'loc:*' as All (back-compat: direct sensor ids will land here)
    is_loc_filter = isinstance(sensor_id, str) and sensor_id.startswith("loc:")
    yield "</div>"
    yield "<div class='dash-right-col'>"
    yield "</div>"
    yield "<div class='dash-side-col'>"
    yield "<form method='get' class='dash-loc-form'>"
    yield "<div class='dash-loc-head'>"
    yield "  <div class='dash-loc-label'>SHOW DEVICE BY LOCATION</div>"
    yield "  <a id='refresh_link' class='refresh-link' href='/' title='Refresh dashboard' aria-label='Refresh dashboard'>⟳</a>"
    yield "</div>"
    yield "<select name='sensor_id' id='sensor_id' onchange='this.form.submit()' style='background-color:#ffffe0;'>"
    yield f"<option value='All' {'selected' if (not is_loc_filter or sensor_id == 'All') else ''}>All Locations</option>"
    for norm, disp in known_items:
        val = f"loc:{disp}"
        sel = "selected" if sensor_id == val else ""
        yield f"<option value='{val}' {sel}>{disp}</option>"
    yield "</select>"
    yield "</form>"
    yield "</div>"
    yield "</div>"
    
    # Per-sensor gauge blocks
    for sid, sensor_metrics in expected_gauge_map.items():
        sidLower = sid.lower()
        sidUpper = sid.upper()
        values = all_values.get(sid) or {}
        stats  = all_stats.get(sid)  or {}
        topic = f"sensor/{sid}/data"
        location = (sensor_locations or {}).get(sid) or ""
        # ---- measurement status indicator ----
        _meas_status = _resolve_meas_status(sid)
        _dot_color   = _status_color_hex(_meas_status)

        yield f"<div class='sensor-group' id='group_{sid}' data-sensor-id='{sid}' style='width:100%; flex:0 0 100%; display:block;'>"
        yield "<div class='sensor-group-header'>"
        yield f"<h3 id='{sid}_header' class='sensor-group-title'>"      
        yield (            
            f" <span class='sensor-status-dot' id='{sid}_statusdot' data-sid='{sid}'"
            f"      title='Connection status: {_meas_status}' "
            f"      aria-label='Connectionss status: {_meas_status}' "
            f"      style='display:inline-block;width:15px;height:15px;"
            f"             border-radius:50%;vertical-align:middle;margin-right:6px;margin-bottom:4px;"
            f"             background:{_dot_color};border:1px solid #666;'></span>"
            f" {sidUpper} "
        )        
        yield f"  <a href='#' onclick=\"window.editSensorSettings && window.editSensorSettings('{sidLower}'); return false;\" title='Open {sid} Settings' style='margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
        yield from _settings_gear_svg_lines(indent="    ")
        yield "  </a>"
        yield (f"{location}")
        yield "</h3>"
        yield (
            f"<div class='sensor-order-wrap' data-sensor-order='{sid}'>"
            f"  <button type='button' class='sensor-order-btn' data-sensor-id='{sid}' aria-haspopup='true' aria-expanded='false' title='Reorder {sidUpper} row' aria-label='Reorder {sidUpper} row'>"
            f"    <span class='sensor-order-bars' aria-hidden='true'><span></span><span></span><span></span></span>"
            f"  </button>"
            f"  <div class='sensor-order-menu' role='menu' aria-label='Reorder {sidUpper} row'>"
            f"    <button type='button' class='sensor-order-item' data-move='up' data-sensor-id='{sid}' title='Move {sidUpper} row up'>Move up</button>"
            f"    <button type='button' class='sensor-order-item' data-move='down' data-sensor-id='{sid}' title='Move {sidUpper} row down'>Move down</button>"
            f"  </div>"
            f"</div>"
        )
        yield "</div>"
        render_metrics = []
        seen_render_metrics = set()
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
            display_unit = str((gauge_config.get(display_metric) or {}).get("unit") or config.get("unit") or "")
            display_val_raw = values.get(display_metric)
            if display_val_raw is None and display_metric != metric:
                for k in values.keys():
                    if k.lower().replace("-", "").replace("_", "") == display_metric.lower().replace("-", "").replace("_", ""):
                        display_val_raw = values[k]
                        break
            stat_metric = str(config.get("stats_metric") or metric)
            stat = stats.get(stat_metric, {})
            display_val = display_val_raw if display_val_raw is not None else "--"
            display_precision = (gauge_config.get(display_metric) or config).get("display_precision")
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

            def _strip_microseconds(ts: str) -> str:
                try:
                    dt = datetime.fromisoformat(ts).replace(microsecond=0)
                    if dt.tzinfo:
                        dt = dt.replace(tzinfo=None)
                    return dt.isoformat(sep=" ")
                except Exception:
                    # Best-effort: remove fractional seconds and any trailing TZ offset
                    try:
                        return re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", re.sub(r"\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$)", "", ts))
                    except Exception:
                        return ts  # leave unchanged if it can’t be parsed
        
            min_val = stat.get("min", "--")
            avg_val = stat.get("avg", "--")
            max_val = stat.get("max", "--")
            min_ts = stat.get("min_ts", "--")
            max_ts = stat.get("max_ts", "--")
            if isinstance(min_ts, str):
                #min_ts = min_ts.replace("T", "<br>")
                min_ts = _strip_microseconds(min_ts).replace(" ", "<br>")
            if isinstance(max_ts, str):
                #max_ts = max_ts.replace("T", "<br>")
                max_ts = _strip_microseconds(max_ts).replace(" ", "<br>")
            try:
                avg_val = f"{float(avg_val):.1f}"
            except Exception:
                pass

            safe_metric = _safe(metric)
            safe_id = f"{sid}_{safe_metric}"
            metric_display_style = (
                (sensor_style_map.get(sid) or {}).get(f"METRIC_{metric_pos}")
                or display_style
            )

            yield f"<div class='metric-container' id='{safe_id}_container' data-sensor='{sid}' data-metric='{metric}' data-display-style='{metric_display_style}'>"
            yield f"<div class='metric-title'>{metric} ({config['unit']})</div>"

            yield "<div class='gauge-container'>"
            yield f"<div class='gauge-view'><canvas id='{safe_id}Gauge'></canvas></div>"
            yield "</div>"

            yield "<div class='graph-container'>"
            yield f"<div class='graph-view'>"
            yield f"<canvas class='micrograph-canvas' width='{layout['canvas_width']}' height='{layout['canvas_height']}'></canvas>"
            yield "</div>"
            yield "</div>"

            yield f"<div class='metric-current-value' id='{safe_id}_val'>{display_text}</div>"

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

    if selected_switch_locations:
        yield "<div class='sensor-group switch-group' id='group_switches' style='width:100%; flex:0 0 100%; display:block;'>"
        yield "<div class='sensor-group-header'>"
        yield "<h3 id='switches_header' class='sensor-group-title'>SWITCHES</h3>"
        yield "</div>"
        yield "<div class='sensor-row switches-row' id='row_switches'>"
        for loc_norm, disp in selected_switch_locations:
            yield from _render_switch_cards_for_location(disp, switches_by_loc.get(loc_norm, []))
        yield "</div>"
        yield "</div>"

    # --- z-index so toasts always appear above stacked modals ---
    yield "<style>"
    yield ".toast-container{position:fixed; top:16px; left:50%; transform:translateX(-50%); z-index:99999}"
    yield ".toast{padding:10px 14px; border-radius:8px; background:#222; color:#fff; box-shadow:0 2px 10px rgba(0,0,0,.25)}"
    yield ".toast.ok{background:#1f693a}"
    yield ".toast.error{background:#8b0000}"
    yield ".onboard-overlay{z-index: 99990}"      # ensure overlay is high
    yield ".onboard-modal{position:relative; z-index: 99991}"
    yield "</style>"

    
    yield "<script type='module'>"
    yield "\"use strict\";"
    
    yield "let stepCount = 0;"
    yield f"const gaugeConfig = {json.dumps(gauge_config)};"
    yield f"const currentValues = {json.dumps(all_values)};"
    yield f"const sensorStats = {json.dumps(all_stats)};"
    yield f"const expectedGaugeMap = {json.dumps(expected_gauge_map)};"
    yield f"const expectedDisplayStyleMap = {json.dumps(expected_display_style_map or {})};"
    yield f"const metricCanvasWidth = {int(layout['canvas_width'])};"
    yield f"const metricCanvasHeight = {int(layout['canvas_height'])};"
    yield f"const astroData = {json.dumps(astro_payload)};"
    yield f"const biodynamicData = {json.dumps(biodynamic_payload)};"
    yield f"const isPiPlatform = {str(is_pi_platform).lower()};"
    yield "const lastTimestamps = {};"
    yield "let lastSensorTimestampChangeMs = Date.now();"
    yield "const sensorTimestampStaleMs = 75000;"
    yield f"const displayStyle = {json.dumps(display_style_js)};"
    yield "window.displayStyle = displayStyle;"

    yield "function toSafe(s) { return (s || '').replace(/[^A-Za-z0-9_-]/g, '_'); }"

    yield "if (window.location.search.includes('refresh=true')) {"
    yield "  window.history.replaceState(null, '', window.location.pathname);"
    yield "  window.location.reload(true);"
    yield "}"

    yield "function closeMenu() {"
    yield "  const menu = document.getElementById('menu');"
    yield "  if (menu) menu.style.display = 'none';"
    yield "}"

    yield "let __lastBiodynamicMinuteKey = '';"
    yield "function updateLocalTime() {"
    yield "  const now = new Date();"
    yield "  const ts = document.getElementById('update_time');"
    yield "  if (ts) {"
    yield "    const formatted = now.toLocaleString('en-CA', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' }).replace(',', '');"
    yield "    ts.textContent = formatted;"
    yield "  }"
    yield "  const biodynamicMinuteKey = `${now.getFullYear()}-${now.getMonth()+1}-${now.getDate()}-${now.getHours()}-${now.getMinutes()}`;"
    yield "  if (biodynamicMinuteKey !== __lastBiodynamicMinuteKey) {"
    yield "    __lastBiodynamicMinuteKey = biodynamicMinuteKey;"
    yield "    if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);"
    yield "  }"
    yield "}"

    yield "function drawSunPath(data){"
    yield "  const c = document.getElementById('sunPathCanvas');"
    yield "  const meta = document.getElementById('sunMeta');"
    yield "  const riseEl = document.getElementById('sunTimeRise');"
    yield "  const noonEl = document.getElementById('sunTimeNoon');"
    yield "  const setEl = document.getElementById('sunTimeSet');"
    yield "  const moonMeta = document.getElementById('moonPositionMeta');"
    yield "  const moonRiseEl = document.getElementById('moonTimeRise');"
    yield "  const moonSetEl = document.getElementById('moonTimeSet');"
    yield "  if (!c || !meta || !riseEl || !noonEl || !setEl || !moonMeta || !moonRiseEl || !moonSetEl) return;"
    yield "  const ctx = c.getContext('2d');"
    yield "  ctx.clearRect(0,0,c.width,c.height);"
    yield "  const toMin = (hhmm) => {"
    yield "    if (typeof hhmm === 'number' && Number.isFinite(hhmm)) return Math.max(0, Math.min(1440, hhmm));"
    yield "    const m = String(hhmm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return null;"
    yield "    const hh = parseInt(m[1], 10);"
    yield "    const mm = parseInt(m[2], 10);"
    yield "    if (hh === 24 && mm === 0) return 1440;"
    yield "    return Math.max(0, Math.min(1440, (hh * 60) + mm));"
    yield "  };"
    yield "  const fmtSun = (hhmm) => {"
    yield "    const m = String(hhmm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return '--';"
    yield "    const hh = parseInt(m[1], 10);"
    yield "    const mm = m[2];"
    yield "    const ap = hh < 12 ? 'A' : 'P';"
    yield "    const h12 = (hh % 12) || 12;"
    yield "    return `${h12}:${mm}${ap}`;"
    yield "  };"
    yield "  const placeLabel = (el, minutes, fallback) => {"
    yield "    const raw = Number.isFinite(minutes) ? minutes : fallback;"
    yield "    const p = Number.isFinite(raw) ? (raw / 1440) : 0.5;"
    yield "    const clamped = Math.max(0.06, Math.min(0.94, p));"
    yield "    el.style.left = `${(clamped * 100).toFixed(2)}%`;"
    yield "  };"
    yield "  const pointMinute = (p) => {"
    yield "    const raw = Number(p && p.m);"
    yield "    if (Number.isFinite(raw)) return Math.max(0, Math.min(1440, raw));"
    yield "    return toMin(p && p.t);"
    yield "  };"
    yield "  const pointList = (items) => {"
    yield "    if (!Array.isArray(items)) return [];"
    yield "    return items.map((p) => ({m: pointMinute(p), e: Number(p && p.e)})).filter((p) => Number.isFinite(p.m) && Number.isFinite(p.e)).sort((a,b) => a.m - b.m);"
    yield "  };"
    yield "  const interpElev = (pts, minutes) => {"
    yield "    if (!Array.isArray(pts) || !pts.length || !Number.isFinite(minutes)) return NaN;"
    yield "    if (minutes <= pts[0].m) return pts[0].e;"
    yield "    for (let i = 1; i < pts.length; i++) {"
    yield "      if (minutes <= pts[i].m) {"
    yield "        const a = pts[i-1], b = pts[i];"
    yield "        const span = Math.max(1, b.m - a.m);"
    yield "        const f = (minutes - a.m) / span;"
    yield "        return a.e + ((b.e - a.e) * f);"
    yield "      }"
    yield "    }"
    yield "    return pts[pts.length - 1].e;"
    yield "  };"
    yield "  const sr = toMin(data && data.sunrise);"
    yield "  const ss = toMin(data && data.sunset);"
    yield "  const nn = toMin(data && data.sun_noon);"
    yield "  const mrTodayRaw = data && typeof data.moon_rise_today === 'string' ? data.moon_rise_today : '';"
    yield "  const msTodayRaw = data && typeof data.moon_set_today === 'string' ? data.moon_set_today : '';"
    yield "  const mr = toMin(mrTodayRaw);"
    yield "  const ms = toMin(msTodayRaw);"
    yield "  riseEl.textContent = fmtSun(data && data.sunrise);"
    yield "  noonEl.textContent = fmtSun(data && data.sun_noon);"
    yield "  setEl.textContent = fmtSun(data && data.sunset);"
    yield "  moonRiseEl.textContent = fmtSun(mrTodayRaw);"
    yield "  moonSetEl.textContent = fmtSun(msTodayRaw);"
    yield "  placeLabel(riseEl, sr, 360);"
    yield "  placeLabel(noonEl, Number.isFinite(nn) ? nn : 720, 720);"
    yield "  placeLabel(setEl, ss, 1080);"
    yield "  placeLabel(moonRiseEl, mr, 390);"
    yield "  placeLabel(moonSetEl, ms, 1050);"
    yield "  const padX = 8, padY = 8;"
    yield "  const w = c.width - padX*2, h = c.height - padY*2;"
    yield "  const yBase = padY + (h * 0.54);"
    yield "  ctx.fillStyle = '#dff1ff';"
    yield "  ctx.fillRect(padX, padY, w, yBase - padY);"
    yield "  ctx.fillStyle = '#000000';"
    yield "  ctx.fillRect(padX, yBase, w, (c.height - padY) - yBase);"
    yield "  ctx.strokeStyle = '#8fa4b3';"
    yield "  ctx.lineWidth = 1;"
    yield "  ctx.beginPath();"
    yield "  ctx.moveTo(padX, yBase);"
    yield "  ctx.lineTo(c.width - padX, yBase);"
    yield "  ctx.stroke();"
    yield "  const sunPoints = pointList(data && data.sun_points);"
    yield "  const moonPoints = pointList(data && data.moon_points);"
    yield "  const allElevs = sunPoints.concat(moonPoints).map((p) => p.e).filter((v) => Number.isFinite(v));"
    yield "  const maxElev = Math.max(20, ...allElevs.filter((v) => v > 0));"
    yield "  const minElev = Math.min(-18, ...allElevs.filter((v) => v < 0));"
    yield "  const abovePx = Math.max(1, yBase - padY - 2);"
    yield "  const belowPx = Math.max(1, (c.height - padY) - yBase - 2);"
    yield "  const yForElev = (e) => {"
    yield "    if (!Number.isFinite(e)) return yBase;"
    yield "    if (e >= 0) return yBase - (Math.min(1, e / maxElev) * abovePx);"
    yield "    return yBase + (Math.min(1, Math.abs(e) / Math.abs(minElev)) * belowPx);"
    yield "  };"
    yield "  const dayAmp = h * 0.46;"
    yield "  const nightAmp = h * 0.24;"
    yield "  const xForMin = (m) => padX + ((Math.max(0, Math.min(1440, m)) / 1440) * w);"
    yield "  const fallbackSunYForMin = (m) => {"
    yield "    if (m <= sr){"
    yield "      const denom = Math.max(1, sr);"
    yield "      const frac = m / denom;"
    yield "      return yBase + (nightAmp * Math.cos((Math.PI * frac) / 2));"
    yield "    }"
    yield "    if (m < ss){"
    yield "      const frac = (m - sr) / Math.max(1, (ss - sr));"
    yield "      return yBase - (dayAmp * Math.sin(Math.PI * frac));"
    yield "    }"
    yield "    const denom = Math.max(1, (1440 - ss));"
    yield "    const frac = (m - ss) / denom;"
    yield "    return yBase + (nightAmp * Math.sin((Math.PI * frac) / 2));"
    yield "  };"
    yield "  const buildSunDisplayPoints = () => {"
    yield "    if (!data || !data.ok || !Number.isFinite(sr) || !Number.isFinite(ss) || sr >= ss) return [];"
    yield "    const peakMin = Number.isFinite(nn) ? Math.max(sr + 1, Math.min(ss - 1, nn)) : ((sr + ss) / 2);"
    yield "    const peakElev = Math.max(20, ...sunPoints.map((p) => p.e).filter((v) => Number.isFinite(v)));"
    yield "    const nightDepth = Math.max(8, Math.abs(Math.min(-8, ...sunPoints.map((p) => p.e).filter((v) => Number.isFinite(v) && v < 0))));"
    yield "    const built = [];"
    yield "    for (let m = 0; m <= 1440; m += 5) {"
    yield "      let e = 0;"
    yield "      if (m <= sr) {"
    yield "        const f = sr > 0 ? Math.max(0, Math.min(1, m / sr)) : 1;"
    yield "        e = -nightDepth * Math.cos((Math.PI * f) / 2);"
    yield "      } else if (m <= peakMin) {"
    yield "        const f = Math.max(0, Math.min(1, (m - sr) / Math.max(1, peakMin - sr)));"
    yield "        e = peakElev * Math.sin((Math.PI * f) / 2);"
    yield "      } else if (m <= ss) {"
    yield "        const f = Math.max(0, Math.min(1, (m - peakMin) / Math.max(1, ss - peakMin)));"
    yield "        e = peakElev * Math.cos((Math.PI * f) / 2);"
    yield "      } else {"
    yield "        const f = Math.max(0, Math.min(1, (m - ss) / Math.max(1, 1440 - ss)));"
    yield "        e = -nightDepth * Math.sin((Math.PI * f) / 2);"
    yield "      }"
    yield "      built.push({m, e});"
    yield "    }"
    yield "    return built;"
    yield "  };"
    yield "  const buildExtremaDisplayPoints = (pts) => {"
    yield "    if (!Array.isArray(pts) || pts.length < 4) return pts || [];"
    yield "    let maxP = pts[0];"
    yield "    let minP = pts[0];"
    yield "    for (const p of pts) {"
    yield "      if (p.e > maxP.e) maxP = p;"
    yield "      if (p.e < minP.e) minP = p;"
    yield "    }"
    yield "    const keys = [pts[0], maxP, minP, pts[pts.length - 1]].sort((a, b) => a.m - b.m).filter((p, idx, arr) => idx === 0 || Math.abs(p.m - arr[idx - 1].m) >= 1);"
    yield "    if (keys.length < 3) return pts;"
    yield "    const built = [];"
    yield "    for (let i = 0; i < keys.length - 1; i++) {"
    yield "      const a = keys[i];"
    yield "      const b = keys[i + 1];"
    yield "      const span = Math.max(1, b.m - a.m);"
    yield "      for (let m = a.m; m < b.m; m += 5) {"
    yield "        const f = Math.max(0, Math.min(1, (m - a.m) / span));"
    yield "        const eased = 0.5 - (0.5 * Math.cos(Math.PI * f));"
    yield "        built.push({m, e: a.e + ((b.e - a.e) * eased)});"
    yield "      }"
    yield "    }"
    yield "    built.push(keys[keys.length - 1]);"
    yield "    return built;"
    yield "  };"
    yield "  const drawPath = (pts, color, width, dash) => {"
    yield "    if (!Array.isArray(pts) || pts.length < 2) return false;"
    yield "    ctx.save();"
    yield "    ctx.strokeStyle = color;"
    yield "    ctx.lineWidth = width;"
    yield "    ctx.lineCap = 'round';"
    yield "    ctx.lineJoin = 'round';"
    yield "    ctx.setLineDash(Array.isArray(dash) ? dash : []);"
    yield "    const xy = pts.map((p) => ({x: xForMin(p.m), y: yForElev(p.e)}));"
    yield "    ctx.beginPath();"
    yield "    ctx.moveTo(xy[0].x, xy[0].y);"
    yield "    for (let i = 1; i < xy.length - 1; i++) {"
    yield "      const p = xy[i];"
    yield "      const next = xy[i + 1];"
    yield "      const midX = (p.x + next.x) / 2;"
    yield "      const midY = (p.y + next.y) / 2;"
    yield "      ctx.quadraticCurveTo(p.x, p.y, midX, midY);"
    yield "    }"
    yield "    const last = xy[xy.length - 1];"
    yield "    const prev = xy[xy.length - 2];"
    yield "    ctx.quadraticCurveTo(prev.x, prev.y, last.x, last.y);"
    yield "    ctx.stroke();"
    yield "    ctx.restore();"
    yield "    return true;"
    yield "  };"
    yield "  const sunDisplayPoints = buildSunDisplayPoints();"
    yield "  const moonDisplayPoints = buildExtremaDisplayPoints(moonPoints);"
    yield "  const drewSun = drawPath(sunDisplayPoints, '#69bdf2', 2.1, []);"
    yield "  if (!drewSun && data && data.ok && Number.isFinite(sr) && Number.isFinite(ss) && sr < ss){"
    yield "    ctx.strokeStyle = '#69bdf2';"
    yield "    ctx.lineWidth = 2.1;"
    yield "    ctx.lineCap = 'round';"
    yield "    ctx.beginPath();"
    yield "    for (let m = 0; m <= 1440; m += 10){"
    yield "      const x = xForMin(m);"
    yield "      const y = fallbackSunYForMin(m);"
    yield "      if (m === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);"
    yield "    }"
    yield "    ctx.stroke();"
    yield "  }"
    yield "  drawPath(moonDisplayPoints, '#f3d34a', 1.65, []);"
    yield "  const now = new Date();"
    yield "  const curMin = (now.getHours() * 60) + now.getMinutes();"
    yield "  const xNow = xForMin(curMin);"
    yield "  const sunElevNow = interpElev(sunDisplayPoints, curMin);"
    yield "  const yNow = Number.isFinite(sunElevNow) ? yForElev(sunElevNow) : ((Number.isFinite(sr) && Number.isFinite(ss) && sr < ss) ? fallbackSunYForMin(curMin) : yBase);"
    yield "  ctx.fillStyle = '#ffff00';"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(xNow, yNow, 3.9, 0, Math.PI*2);"
    yield "  ctx.fill();"
    yield "  ctx.strokeStyle = '#ff8c00';"
    yield "  ctx.lineWidth = 1;"
    yield "  ctx.stroke();"
    yield "  const moonElevNow = interpElev(moonDisplayPoints, curMin);"
    yield "  if (Number.isFinite(moonElevNow)){"
    yield "    const yMoonNow = yForElev(moonElevNow);"
    yield "    ctx.fillStyle = '#fff8df';"
    yield "    ctx.beginPath();"
    yield "    ctx.arc(xNow, yMoonNow, 3.2, 0, Math.PI*2);"
    yield "    ctx.fill();"
    yield "    ctx.strokeStyle = '#d1b94c';"
    yield "    ctx.lineWidth = 1;"
    yield "    ctx.stroke();"
    yield "  }"
    yield "  const moonDec = Number(data && data.moon_declination);"
    yield "  const source = String((data && data.moon_position_source) || '').trim();"
    yield "  if (Number.isFinite(moonDec)) c.title = `Moon declination ${moonDec.toFixed(1)} deg${source ? ` (${source})` : ''}`; else c.removeAttribute('title');"
    yield "}"

    yield "function getMoonViewMode(){"
    yield "  const localBtn = document.getElementById('moonViewLocal');"
    yield "  const refBtn = document.getElementById('moonViewReference');"
    yield "  if (refBtn && refBtn.classList.contains('active')) return 'reference';"
    yield "  if (localBtn && localBtn.classList.contains('active')) return 'local';"
    yield "  return 'local';"
    yield "}"
    yield ""
    yield "function setMoonViewMode(mode){"
    yield "  const localBtn = document.getElementById('moonViewLocal');"
    yield "  const refBtn = document.getElementById('moonViewReference');"
    yield "  const isReference = mode === 'reference';"
    yield "  if (localBtn){"
    yield "    localBtn.classList.toggle('active', !isReference);"
    yield "    localBtn.setAttribute('aria-pressed', !isReference ? 'true' : 'false');"
    yield "  }"
    yield "  if (refBtn){"
    yield "    refBtn.classList.toggle('active', isReference);"
    yield "    refBtn.setAttribute('aria-pressed', isReference ? 'true' : 'false');"
    yield "  }"
    yield "}"
    yield ""
    yield "function drawMoonPhase(data){"
    yield "  const c = document.getElementById('moonPhaseCanvas');"
    yield "  const meta = document.getElementById('moonMeta');"
    yield "  const riseEl = document.getElementById('moonRiseTime');"
    yield "  const setEl = document.getElementById('moonSetTime');"
    yield "  const litEl = document.getElementById('moonLitPct');"
    yield "  const nextPhaseLabelEl = document.getElementById('moonNextPhaseLabel');"
    yield "  const nextPhaseDateEl = document.getElementById('moonNextPhaseDate');"
    yield "  if (!c || !meta || !riseEl || !setEl || !litEl || !nextPhaseLabelEl || !nextPhaseDateEl) return;"
    yield "  const ctx = c.getContext('2d');"
    yield "  ctx.clearRect(0,0,c.width,c.height);"
    yield "  const fmtRaw = (v) => (typeof v === 'string' && v.trim() ? v.trim() : '--');"
    yield "  const fmtMoonTime = (v) => {"
    yield "    const s = fmtRaw(v);"
    yield "    const m = s.match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return s;"
    yield "    const hh = parseInt(m[1], 10);"
    yield "    const mm = m[2];"
    yield "    const ap = hh < 12 ? 'A' : 'P';"
    yield "    const h12 = (hh % 12) || 12;"
    yield "    return `${h12}:${mm}${ap}`;"
    yield "  };"
    yield "  const fmtMoonDate = (v) => {"
    yield "    const s = fmtRaw(v);"
    yield "    const m = s.match(/^(\\d{4})-(\\d{2})-(\\d{2})$/);"
    yield "    if (!m) return s;"
    yield "    const yy = m[1].slice(-2);"
    yield "    const mon = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][Math.max(0, Math.min(11, parseInt(m[2],10)-1))];"
    yield "    return `${m[3]}${mon}${yy}`;"
    yield "  };"
    yield "  if (!data || !data.ok || typeof data.moon_phase_value !== 'number'){"
    yield "    meta.textContent = 'Moon data unavailable';"
    yield "    riseEl.textContent = '--';"
    yield "    setEl.textContent = '--';"
    yield "    litEl.textContent = '--';"
    yield "    nextPhaseLabelEl.textContent = 'Next Phase';"
    yield "    nextPhaseDateEl.textContent = '--';"
    yield "    return;"
    yield "  }"
    yield "  const w = c.width, h = c.height;"
    yield "  const r = Math.min(w, h) / 2 - 1;"
    yield "  const x = w / 2, y = h / 2;"
    yield "  const phase = ((data.moon_phase_value % 28) + 28) % 28;"
    yield "  const illum = 0.5 * (1 - Math.cos((2*Math.PI*phase)/28));"
    yield "  const lat = Number(data.lat || 0);"
    yield "  const hemisphereFlip = lat < 0 ? -1 : 1;"
    yield "  const phaseAngle = (2 * Math.PI * phase) / 28;"
    yield "  const rawVisibleAngle = data ? data.moon_visible_angle : null;"
    yield "  const visibleAngle = (typeof rawVisibleAngle === 'number' && Number.isFinite(rawVisibleAngle)) ? rawVisibleAngle : NaN;"
    yield "  const hasVisibleAngle = Number.isFinite(visibleAngle);"
    yield "  const rawReferenceAngle = data ? data.moon_reference_angle : null;"
    yield "  const referenceAngle = (typeof rawReferenceAngle === 'number' && Number.isFinite(rawReferenceAngle)) ? rawReferenceAngle : NaN;"
    yield "  const hasReferenceAngle = Number.isFinite(referenceAngle);"
    yield "  const moonViewMode = getMoonViewMode();"
    yield "  const isReferenceMode = moonViewMode === 'reference';"
    yield "  const useVisibleAngle = !isReferenceMode && hasVisibleAngle;"
    yield "  const useReferenceAngle = isReferenceMode && hasReferenceAngle;"
    yield "  const limbStrength = Math.abs(Math.sin(phaseAngle));"
    yield "  const sourceAngleDeg = useVisibleAngle ? visibleAngle : (isReferenceMode ? 0 : (hemisphereFlip < 0 ? -60 : 60));"
    yield "  const rotationDeg = sourceAngleDeg;"
    yield "  if (useVisibleAngle) c.setAttribute('data-visible-angle', visibleAngle.toFixed(2)); else c.removeAttribute('data-visible-angle');"
    yield "  if (useReferenceAngle) c.setAttribute('data-reference-angle', referenceAngle.toFixed(2)); else c.removeAttribute('data-reference-angle');"
    yield "  const sx = limbStrength;"
    yield "  const sz = -Math.cos(phaseAngle);"
    yield "  const phaseCanvas = document.createElement('canvas');"
    yield "  phaseCanvas.width = w; phaseCanvas.height = h;"
    yield "  const phaseCtx = phaseCanvas.getContext('2d');"
    yield "  const image = phaseCtx.createImageData(w, h);"
    yield "  const pix = image.data;"
    yield "  for (let py = 0; py < h; py++) {"
    yield "    for (let px = 0; px < w; px++) {"
    yield "      const dx = (px + 0.5 - x) / r;"
    yield "      const dy = (py + 0.5 - y) / r;"
    yield "      const rr = dx*dx + dy*dy;"
    yield "      const off = (py * w + px) * 4;"
    yield "      if (rr > 1) { pix[off+3] = 0; continue; }"
    yield "      const dz = Math.sqrt(Math.max(0, 1 - rr));"
    yield "      const dot = (dx * sx) + (dz * sz);"
    yield "      const edge = Math.max(-1, Math.min(1, dot / 0.06));"
    yield "      const blend = (edge + 1) * 0.5;"
    yield "      const litMix = Math.pow(blend, 0.82);"
    yield "      const rim = Math.pow(Math.max(0, dz), 0.65);"
    yield "      const darkR = 74, darkG = 78, darkB = 86;"
    yield "      const litR = 244, litG = 242, litB = 234;"
    yield "      const bodyR = darkR + ((litR - darkR) * litMix);"
    yield "      const bodyG = darkG + ((litG - darkG) * litMix);"
    yield "      const bodyB = darkB + ((litB - darkB) * litMix);"
    yield "      const rimBoost = 0.12 + (0.10 * rim);"
    yield "      pix[off+0] = Math.round(Math.max(0, Math.min(255, bodyR + (litMix * 10) + (rimBoost * 18))));"
    yield "      pix[off+1] = Math.round(Math.max(0, Math.min(255, bodyG + (litMix * 9) + (rimBoost * 16))));"
    yield "      pix[off+2] = Math.round(Math.max(0, Math.min(255, bodyB + (litMix * 7) + (rimBoost * 10))));"
    yield "      pix[off+3] = 255;"
    yield "    }"
    yield "  }"
    yield "  phaseCtx.putImageData(image, 0, 0);"
    yield "  const maria = [{x:-0.28,y:-0.24,r:0.22,a:0.15},{x:0.06,y:-0.1,r:0.17,a:0.12},{x:-0.12,y:0.18,r:0.2,a:0.11},{x:0.26,y:0.12,r:0.12,a:0.1},{x:0.18,y:-0.34,r:0.1,a:0.1}];"
    yield "  phaseCtx.save();"
    yield "  phaseCtx.translate(x, y);"
    yield "  phaseCtx.beginPath();"
    yield "  phaseCtx.arc(0, 0, r, 0, Math.PI * 2);"
    yield "  phaseCtx.clip();"
    yield "  for (const m of maria) {"
    yield "    phaseCtx.fillStyle = `rgba(88, 92, 100, ${m.a})`;"
    yield "    phaseCtx.beginPath();"
    yield "    phaseCtx.arc(m.x * r, m.y * r, m.r * r, 0, Math.PI * 2);"
    yield "    phaseCtx.fill();"
    yield "  }"
    yield "  phaseCtx.restore();"
    yield "  ctx.save();"
    yield "  ctx.translate(x, y);"
    yield "  ctx.rotate((rotationDeg * Math.PI) / 180);"
    yield "  ctx.drawImage(phaseCanvas, -x, -y);"
    yield "  ctx.restore();"
    yield "  ctx.strokeStyle = '#58524a';"
    yield "  ctx.lineWidth = 1.15;"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(x, y, r, 0, Math.PI * 2);"
    yield "  ctx.stroke();"
    yield "  const litPct = Number.isFinite(Number(data.moon_lit_pct)) ? `${Math.round(Number(data.moon_lit_pct))}%` : `${(illum*100).toFixed(0)}%`;"
    yield "  meta.textContent = `${data.moon_phase_label || 'Moon'} • ${isReferenceMode ? 'Reference diagram' : 'Local sky view'}`;"
    yield "  riseEl.textContent = fmtMoonTime(data.moon_rise);"
    yield "  setEl.textContent = fmtMoonTime(data.moon_set);"
    yield "  litEl.textContent = litPct;"
    yield "  nextPhaseLabelEl.textContent = fmtRaw(data.moon_next_phase_label) === '--' ? 'Next Phase' : fmtRaw(data.moon_next_phase_label);"
    yield "  nextPhaseDateEl.textContent = fmtMoonDate(data.moon_next_phase_date);"
    yield "}"
    yield ""
    yield "document.addEventListener('click', function(ev){"
    yield "  const btn = ev.target instanceof Element ? ev.target.closest('[data-moon-view]') : null;"
    yield "  if (!btn) return;"
    yield "  const mode = btn.getAttribute('data-moon-view') === 'reference' ? 'reference' : 'local';"
    yield "  setMoonViewMode(mode);"
    yield "  if (typeof astroData !== 'undefined') drawMoonPhase(astroData);"
    yield "});"
    yield ""
    yield "function forecastEsc(value){"
    yield "  return String(value == null ? '' : value).replace(/[&<>\"']/g, (ch) => {"
    yield "    if (ch === '&') return '&amp;';"
    yield "    if (ch === '<') return '&lt;';"
    yield "    if (ch === '>') return '&gt;';"
    yield "    if (ch === '\"') return '&quot;';"
    yield "    return '&#39;';"
    yield "  });"
    yield "}"
    yield "function forecastProviderLabel(data){"
    yield "  const provider = String((data && data.provider) || '').trim();"
    yield "  if (provider === 'met_no') return 'MET Norway';"
    yield "  if (provider === 'open_meteo') return 'Open-Meteo';"
    yield "  return provider || '';"
    yield "}"
    yield "function forecastUnavailableText(data){"
    yield "  const reason = String((data && data.reason) || '').trim();"
    yield "  if (reason === 'location_unavailable') return 'Set Astral location to enable forecast.';"
    yield "  if (reason === 'provider_unavailable') return 'Forecast provider unavailable.';"
    yield "  if (reason === 'forecast_failed') return 'Forecast unavailable.';"
    yield "  return reason ? `Forecast unavailable: ${reason}` : 'Forecast unavailable.';"
    yield "}"
    yield "function setForecastText(id, value){"
    yield "  const el = document.getElementById(id);"
    yield "  if (el) el.textContent = value || '--';"
    yield "}"
    yield "function forecastWindParts(value){"
    yield "  const text = String(value || '').trim();"
    yield "  if (!text || text === '--') return { description:'--', speeds:'' };"
    yield "  const lines = text.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);"
    yield "  if (lines.length >= 2) return { description:lines[0].replace(/,\\s*$/, ''), speeds:lines.slice(1).join(' ') };"
    yield "  const m = text.match(/^(.*?)(?:,\\s*)?(~?\\d+(?:[-–]\\d+)?\\s*m\\/s\\s*\\/\\s*.+)$/i);"
    yield "  if (m) return { description:String(m[1] || '').trim().replace(/,\\s*$/, '') || '--', speeds:String(m[2] || '').trim() };"
    yield "  const idx = text.search(/~?\\d+(?:[-–]\\d+)?\\s*m\\/s/i);"
    yield "  if (idx > 0) return { description:text.slice(0, idx).trim().replace(/,\\s*$/, '') || '--', speeds:text.slice(idx).trim() };"
    yield "  return { description:text.replace(/,\\s*$/, ''), speeds:'' };"
    yield "}"
    yield "function forecastWindHtml(value){"
    yield "  const parts = forecastWindParts(value);"
    yield "  const desc = forecastEsc(parts.description || '--');"
    yield "  const speeds = String(parts.speeds || '').trim();"
    yield "  return speeds ? `<span class='forecast-wind-line'>${desc}</span><span class='forecast-wind-line'>${forecastEsc(speeds)}</span>` : `<span class='forecast-wind-line'>${desc}</span>`;"
    yield "}"
    yield "function setForecastWind(value){"
    yield "  const el = document.getElementById('forecastWind');"
    yield "  if (el) el.innerHTML = forecastWindHtml(value || '--');"
    yield "}"
    yield "function renderWeatherForecast(data){"
    yield "  window.__weatherForecastPayload = data || null;"
    yield "  const statusEl = document.getElementById('forecastStatus');"
    yield "  const cur = (data && data.current_24h && typeof data.current_24h === 'object') ? data.current_24h : null;"
    yield "  if (!data || !data.ok || !cur) {"
    yield "    if (statusEl) statusEl.textContent = 'Unavailable';"
    yield "    setForecastText('forecastOverall', forecastUnavailableText(data));"
    yield "    setForecastText('forecastTempRange', '--');"
    yield "    setForecastWind('--');"
    yield "    setForecastText('forecastRhRange', '--');"
    yield "    return;"
    yield "  }"
    yield "  const provider = forecastProviderLabel(data);"
    yield "  const cacheText = data.stale ? 'Cached' : 'Live';"
    yield "  if (statusEl) statusEl.textContent = provider ? `${cacheText} - ${provider}` : cacheText;"
    yield "  setForecastText('forecastOverall', cur.overall || cur.forecast || '--');"
    yield "  setForecastText('forecastTempRange', cur.temp_range || '--');"
    yield "  setForecastWind(cur.wind || '--');"
    yield "  setForecastText('forecastRhRange', cur.rh_range || '--');"
    yield "}"
    yield "async function loadWeatherForecast(){"
    yield "  try {"
    yield "    const resp = await fetch('/api/weather-forecast?days=1', { cache:'no-store' });"
    yield "    const payload = await resp.json().catch(() => ({}));"
    yield "    if (!resp.ok) throw new Error(`forecast_${resp.status}`);"
    yield "    renderWeatherForecast(payload);"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load weather forecast', e);"
    yield "    renderWeatherForecast({ ok:false, reason:'forecast_failed' });"
    yield "  }"
    yield "}"
    yield "function setForecastButtonLoading(isLoading){"
    yield "  const btn = document.getElementById('forecastFiveDayBtn');"
    yield "  if (!btn) return;"
    yield "  btn.disabled = !!isLoading;"
    yield "  btn.classList.toggle('loading', !!isLoading);"
    yield "  btn.setAttribute('aria-busy', isLoading ? 'true' : 'false');"
    yield "}"
    yield "function forecastMetaText(data){"
    yield "  if (!data || !data.ok) return forecastUnavailableText(data);"
    yield "  const provider = forecastProviderLabel(data) || 'forecast provider';"
    yield "  const loc = (data.location && typeof data.location === 'object') ? data.location : {};"
    yield "  const lat = Number(loc.latitude);"
    yield "  const lon = Number(loc.longitude);"
    yield "  const coord = (Number.isFinite(lat) && Number.isFinite(lon)) ? `${lat.toFixed(4)}, ${lon.toFixed(4)}` : '';"
    yield "  const stale = data.stale ? 'Cached forecast' : 'Forecast';"
    yield "  return `${stale} from ${provider}${coord ? ` - ${coord}` : ''}`;"
    yield "}"
    yield "function renderForecastDayCard(day){"
    yield "  const label = forecastEsc(day && (day.label || day.date) || 'Day');"
    yield "  const summary = forecastEsc(day && (day.forecast || day.overall) || '--');"
    yield "  const temp = forecastEsc(day && day.temp_range || '--');"
    yield "  const wind = forecastWindHtml(day && day.wind || '--');"
    yield "  const rh = forecastEsc(day && day.rh_range || '--');"
    yield "  return `<section class='forecast-day'><div class='forecast-day-label'>${label}</div><div class='forecast-day-summary'>${summary}</div><dl class='forecast-day-grid'><dt>Temp</dt><dd>${temp}</dd><dt>RH</dt><dd>${rh}</dd><dt>Wind</dt><dd class='forecast-wind-value'>${wind}</dd></dl></section>`;"
    yield "}"
    yield "window.openWeatherForecastModal = async function(){"
    yield "  try {"
    yield "    if (!window.BackdropModal) return;"
    yield "    setForecastButtonLoading(true);"
    yield "    const resp = await fetch('/api/weather-forecast?days=6', { cache:'no-store' });"
    yield "    const payload = await resp.json().catch(() => ({}));"
    yield "    if (!resp.ok) throw new Error(`forecast_${resp.status}`);"
    yield "    renderWeatherForecast(payload);"
    yield "    const days = Array.isArray(payload.days) ? payload.days : [];"
    yield "    const rows = days.length ? days.map(renderForecastDayCard).join('') : `<section class='forecast-day'><div class='forecast-day-label'>6 Day Forecast</div><div class='forecast-day-summary'>${forecastEsc(forecastUnavailableText(payload))}</div></section>`;"
    yield "    const html = `"
    yield "<div class='modal-backdrop' style='display:none'>"
    yield "  <div class='modal forecast-modal' id='weatherForecastModal'>"
    yield "    <div class='modal-header'>"
    yield "      <h3 class='modal-title'>6 Day Forecast</h3>"
    yield "      <button type='button' onclick='window.BackdropModal && window.BackdropModal.close(\"weatherForecastModal\")' style='border:none;background:transparent;font-size:1.2rem;cursor:pointer;' aria-label='Close'>&times;</button>"
    yield "    </div>"
    yield "    <div class='modal-body'>"
    yield "      <div class='forecast-modal-meta'>${forecastEsc(forecastMetaText(payload))}</div>"
    yield "      <div class='forecast-days'>${rows}</div>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>`;"
    yield "    window.BackdropModal.close('weatherForecastModal');"
    yield "    window.BackdropModal.openFromHtml(html, 'weatherForecastModal');"
    yield "  } catch (e) {"
    yield "    console.error('Failed to open weather forecast modal', e);"
    yield "    if (typeof window.showToast === 'function') window.showToast('Failed to load weather forecast', 'error');"
    yield "  } finally {"
    yield "    setForecastButtonLoading(false);"
    yield "  }"
    yield "};"
    yield ""
    yield "function drawBiodynamic(data){"
    yield "  const signEl = document.getElementById('bioCurrentSign');"
    yield "  const elementEl = document.getElementById('bioCurrentElement');"
    yield "  const openBtn = document.getElementById('bioOpenBtn');"
    yield "  const boxEl = document.getElementById('bioBox');"
    yield "  const panelEl = document.getElementById('bioCurrentPanel');"
    yield "  const dateEl = document.getElementById('bioDateLine');"
    yield "  const windowEl = document.getElementById('bioWindow');"
    yield "  const upcomingEl = document.getElementById('bioUpcoming');"
    yield "  if (!signEl || !elementEl || !openBtn || !boxEl || !panelEl || !dateEl || !windowEl || !upcomingEl) return;"
    yield "  const titleEl = boxEl.querySelector('.astro-title');"
    yield "  const tzName = String((data && data.tz) || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');"
    yield "  const fmtParts = (iso) => {"
    yield "    if (!iso) return null;"
    yield "    const d = new Date(iso);"
    yield "    if (!Number.isFinite(d.getTime())) return null;"
    yield "    const dtf = new Intl.DateTimeFormat('en-CA', {"
    yield "      timeZone: tzName,"
    yield "      weekday: 'long',"
    yield "      year: 'numeric',"
    yield "      month: '2-digit',"
    yield "      day: '2-digit',"
    yield "      hour: 'numeric',"
    yield "      minute: '2-digit',"
    yield "      hour12: false,"
    yield "    });"
    yield "    const parts = dtf.formatToParts(d);"
    yield "    const out = {};"
    yield "    for (const part of parts) {"
    yield "      if (part.type !== 'literal') out[part.type] = part.value;"
    yield "    }"
    yield "    return out;"
    yield "  };"
    yield "  const fmtIsoHm = (iso) => {"
    yield "    const parts = fmtParts(iso);"
    yield "    if (!parts || !parts.hour || !parts.minute) return '--';"
    yield "    let hour24 = parseInt(parts.hour, 10);"
    yield "    if (!Number.isFinite(hour24)) return '--';"
    yield "    if (hour24 >= 24) hour24 = 0;"
    yield "    const hour12 = ((hour24 + 11) % 12) + 1;"
    yield "    const suffix = hour24 >= 12 ? 'PM' : 'AM';"
    yield "    return `${hour12}:${parts.minute} ${suffix}`;"
    yield "  };"
    yield "  const fmtHm = (hm) => {"
    yield "    const m = String(hm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return '--';"
    yield "    let hour24 = parseInt(m[1], 10);"
    yield "    if (!Number.isFinite(hour24)) return '--';"
    yield "    if (hour24 >= 24) hour24 = 0;"
    yield "    const hour12 = ((hour24 + 11) % 12) + 1;"
    yield "    const suffix = hour24 >= 12 ? 'PM' : 'AM';"
    yield "    return `${hour12}:${m[2]} ${suffix}`;"
    yield "  };"
    yield "  const fmtIsoDate = (iso) => {"
    yield "    const parts = fmtParts(iso);"
    yield "    if (!parts || !parts.year || !parts.month || !parts.day) return '--';"
    yield "    const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];"
    yield "    const monthName = months[Math.max(0, Math.min(11, parseInt(parts.month, 10) - 1))] || '--';"
    yield "    return `${parts.weekday || '--'}, ${monthName} ${parts.day}, ${parts.year}`;"
    yield "  };"
    yield "  const isoDayKey = (iso) => {"
    yield "    const parts = fmtParts(iso);"
    yield "    if (!parts || !parts.year || !parts.month || !parts.day) return '';"
    yield "    return `${parts.year}-${parts.month}-${parts.day}`;"
    yield "  };"
    yield "  const toMinutes = (hhmm) => {"
    yield "    const m = String(hhmm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return null;"
    yield "    const out = (parseInt(m[1], 10) * 60) + parseInt(m[2], 10);"
    yield "    return Math.max(0, Math.min(1440, out));"
    yield "  };"
    yield "  const bioNowParts = () => {"
    yield "    const d = new Date();"
    yield "    const parts = fmtParts(d.toISOString());"
    yield "    if (!parts || !parts.year || !parts.month || !parts.day || !parts.hour || !parts.minute) return null;"
    yield "    let hour24 = parseInt(parts.hour, 10);"
    yield "    const minute = parseInt(parts.minute, 10);"
    yield "    if (!Number.isFinite(hour24) || !Number.isFinite(minute)) return null;"
    yield "    if (hour24 >= 24) hour24 = 0;"
    yield "    return { iso: d.toISOString(), dayKey: `${parts.year}-${parts.month}-${parts.day}`, minuteOfDay: Math.max(0, Math.min(1439, (hour24 * 60) + minute)) };"
    yield "  };"
    yield "  const lightenHex = (hex, factor) => {"
    yield "    const m = String(hex || '').trim().match(/^#?([0-9a-f]{6})$/i);"
    yield "    if (!m) return String(hex || '#fffdf6');"
    yield "    const raw = m[1];"
    yield "    const mix = (v) => {"
    yield "      const n = parseInt(v, 16);"
    yield "      return Math.max(0, Math.min(255, Math.round(n + ((255 - n) * factor))));"
    yield "    };"
    yield "    const r = mix(raw.slice(0, 2));"
    yield "    const g = mix(raw.slice(2, 4));"
    yield "    const b = mix(raw.slice(4, 6));"
    yield "    return `#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`;"
    yield "  };"
    yield "  const textOnHex = (hex) => {"
    yield "    const m = String(hex || '').trim().match(/^#?([0-9a-f]{6})$/i);"
    yield "    if (!m) return '#27313a';"
    yield "    const raw = m[1];"
    yield "    const toLinear = (pair) => {"
    yield "      const c = parseInt(pair, 16) / 255;"
    yield "      return c <= 0.03928 ? (c / 12.92) : Math.pow((c + 0.055) / 1.055, 2.4);"
    yield "    };"
    yield "    const r = toLinear(raw.slice(0, 2));"
    yield "    const g = toLinear(raw.slice(2, 4));"
    yield "    const b = toLinear(raw.slice(4, 6));"
    yield "    const luminance = (0.2126 * r) + (0.7152 * g) + (0.0722 * b);"
    yield "    const contrast = (a, b) => {"
    yield "      const hi = Math.max(a, b);"
    yield "      const lo = Math.min(a, b);"
    yield "      return (hi + 0.05) / (lo + 0.05);"
    yield "    };"
    yield "    const darkText = '#27313a';"
    yield "    const darkRaw = darkText.slice(1);"
    yield "    const darkLuminance = (0.2126 * toLinear(darkRaw.slice(0, 2))) + (0.7152 * toLinear(darkRaw.slice(2, 4))) + (0.0722 * toLinear(darkRaw.slice(4, 6)));"
    yield "    return contrast(luminance, darkLuminance) >= contrast(luminance, 1) ? darkText : '#fff';"
    yield "  };"
    yield "  const biodynamicIsLightRest = (item) => {"
    yield "    const sign = String((item && item.sign) || '').trim().toLowerCase();"
    yield "    const element = String((item && item.element) || '').trim().toLowerCase();"
    yield "    const part = String((item && item.plant_part) || '').trim().toLowerCase();"
    yield "    const kind = String((item && item.kind) || '').trim().toLowerCase();"
    yield "    return sign === 'rest' || element === 'pause' || part === 'rest' || kind === 'off';"
    yield "  };"
    yield "  const biodynamicTextColor = (item) => {"
    yield "    if (biodynamicIsLightRest(item)) return '#27313a';"
    yield "    const bgColor = String((item && (item.accent || item.dominant_accent || item.color)) || '');"
    yield "    return bgColor ? textOnHex(bgColor) : '#fff';"
    yield "  };"
    yield "  const clearBiodynamicTextContrast = (els) => {"
    yield "    els.forEach((el) => {"
    yield "      if (!el) return;"
    yield "      el.style.color = '';"
    yield "      el.style.textShadow = '';"
    yield "      el.style.backgroundImage = '';"
    yield "      el.style.backgroundSize = '';"
    yield "      el.style.backgroundClip = '';"
    yield "      el.style.webkitBackgroundClip = '';"
    yield "      el.style.webkitTextFillColor = '';"
    yield "    });"
    yield "  };"
    yield "  const applyBiodynamicTextContrast = (els, fallbackColor, gradient) => {"
    yield "    els.forEach((el) => {"
    yield "      if (!el) return;"
    yield "      el.style.color = fallbackColor;"
    yield "      el.style.textShadow = 'none';"
    yield "      if (!gradient) {"
    yield "        el.style.backgroundImage = '';"
    yield "        el.style.backgroundSize = '';"
    yield "        el.style.backgroundClip = '';"
    yield "        el.style.webkitBackgroundClip = '';"
    yield "        el.style.webkitTextFillColor = '';"
    yield "        return;"
    yield "      }"
    yield "      el.style.backgroundImage = gradient;"
    yield "      el.style.backgroundSize = '100% 100%';"
    yield "      el.style.backgroundClip = 'text';"
    yield "      el.style.webkitBackgroundClip = 'text';"
    yield "      el.style.webkitTextFillColor = 'transparent';"
    yield "    });"
    yield "  };"
    yield "  const buildRollingTextGradient = (payload, currentIso) => {"
    yield "    if (!currentIso) return null;"
    yield "    const dayKey = isoDayKey(currentIso);"
    yield "    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];"
    yield "    const today = days.find((d) => d && d.date === dayKey);"
    yield "    const segments = Array.isArray(today && today.segments) ? today.segments : [];"
    yield "    if (!segments.length) return null;"
    yield "    const stops = [];"
    yield "    for (const seg of segments) {"
    yield "      let startMin = toMinutes(seg && seg.start);"
    yield "      let endMin = toMinutes(seg && seg.end);"
    yield "      if (!Number.isFinite(startMin)) startMin = 0;"
    yield "      if (!Number.isFinite(endMin)) endMin = 1440;"
    yield "      if (endMin <= startMin) endMin = 1440;"
    yield "      const startPct = ((startMin / 1440) * 100).toFixed(2);"
    yield "      const endPct = ((endMin / 1440) * 100).toFixed(2);"
    yield "      const textColor = biodynamicTextColor(seg);"
    yield "      stops.push(`${textColor} ${startPct}%`, `${textColor} ${endPct}%`);"
    yield "    }"
    yield "    return stops.length ? `linear-gradient(90deg, ${stops.join(', ')})` : null;"
    yield "  };"
    yield "  const buildRollingGradient = (data, currentIso) => {"
    yield "    if (!currentIso) return '#ffffe0';"
    yield "    const dayKey = isoDayKey(currentIso);"
    yield "    const days = Array.isArray(data && data.calendar) ? data.calendar : [];"
    yield "    const today = days.find((d) => d && d.date === dayKey);"
    yield "    const segments = Array.isArray(today && today.segments) ? today.segments : [];"
    yield "    const fallback = String((today && today.dominant_accent) || '#ffffe0');"
    yield "    if (!segments.length) return fallback;"
    yield "    const slices = [];"
    yield "    const pushSlice = (startMin, endMin, accent) => {"
    yield "      if (!Number.isFinite(startMin) || !Number.isFinite(endMin) || endMin <= startMin) return;"
    yield "      slices.push({ start: startMin, end: endMin, color: String(accent || fallback) });"
    yield "    };"
    yield "    for (const seg of segments) {"
    yield "      let startMin = toMinutes(seg && seg.start);"
    yield "      let endMin = toMinutes(seg && seg.end);"
    yield "      if (!Number.isFinite(startMin)) startMin = 0;"
    yield "      if (!Number.isFinite(endMin)) endMin = 1440;"
    yield "      if (endMin <= startMin) endMin = 1440;"
    yield "      const accent = String((seg && (seg.accent || lightenHex(seg.color || '#d8d8d8', 0.78))) || fallback);"
    yield "      pushSlice(startMin, endMin, accent);"
    yield "    }"
    yield "    if (!slices.length) return '#ffffe0';"
    yield "    const stops = [];"
    yield "    for (const seg of slices) {"
    yield "      const color = String(seg.color || '#fffdf6');"
    yield "      const startPct = ((seg.start / 1440) * 100).toFixed(2);"
    yield "      const endPct = ((seg.end / 1440) * 100).toFixed(2);"
    yield "      stops.push(`${color} ${startPct}%`, `${color} ${endPct}%`);"
    yield "    }"
    yield "    return `linear-gradient(90deg, ${stops.join(', ')})`;"
    yield "  };"
    yield "  const buildBiodynamicWindowText = (payload, currentIso, current) => {"
    yield "    const dayKey = isoDayKey(currentIso);"
    yield "    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];"
    yield "    const today = days.find((d) => d && d.date === dayKey);"
    yield "    const segments = Array.isArray(today && today.segments) ? today.segments : [];"
    yield "    const rows = segments.map((seg) => {"
    yield "      const sign = String((seg && seg.sign) || '').trim() || '--';"
    yield "      return `${sign} Moon: ${fmtHm(seg && seg.start)} to ${fmtHm(seg && seg.end)}`;"
    yield "    }).filter((line) => line && line !== '-- Moon: -- to --');"
    yield "    const sign = String((current && current.sign) || '').trim() || '--';"
    yield "    return rows.length ? rows.join('\\n') : `${sign} Moon: ${fmtIsoHm(current && current.window_start)} to ${fmtIsoHm(current && current.window_end)}`;"
    yield "  };"
    yield "  const findActiveBiodynamicSegment = (payload, fallback) => {"
    yield "    const nowParts = bioNowParts();"
    yield "    if (!nowParts) return Object.assign({}, fallback || {});"
    yield "    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];"
    yield "    const today = days.find((d) => d && d.date === nowParts.dayKey);"
    yield "    const segments = Array.isArray(today && today.segments) ? today.segments : [];"
    yield "    for (const seg of segments) {"
    yield "      let startMin = toMinutes(seg && seg.start);"
    yield "      let endMin = toMinutes(seg && seg.end);"
    yield "      if (!Number.isFinite(startMin)) startMin = 0;"
    yield "      if (!Number.isFinite(endMin)) endMin = 1440;"
    yield "      if (endMin <= startMin) endMin = 1440;"
    yield "      if (nowParts.minuteOfDay >= startMin && nowParts.minuteOfDay < endMin) {"
    yield "        return Object.assign({}, fallback || {}, seg || {}, { timestamp: nowParts.iso, window_start_hm: String((seg && seg.start) || ''), window_end_hm: String((seg && seg.end) || '') });"
    yield "      }"
    yield "    }"
    yield "    return Object.assign({}, fallback || {}, { timestamp: nowParts.iso });"
    yield "  };"
    yield "  const buildUnavailableMessage = (payload) => {"
    yield "    const reason = String((payload && payload.reason) || '').trim();"
    yield "    const eph = (payload && payload.ephemeris && typeof payload.ephemeris === 'object') ? payload.ephemeris : {};"
    yield "    if (reason === 'location_unavailable') return 'Set Astral latitude/longitude/timezone to enable biodynamics.';"
    yield "    if (reason === 'skyfield_not_installed') return 'Skyfield is not installed on this Sensorius host.';"
    yield "    if (reason.startsWith('ephemeris_download_failed:')) return `Automatic ephemeris download failed. Next retry in ${eph.retry_after_sec || 0}s.`;"
    yield "    if (reason.startsWith('ephemeris_download_deferred:')) return `Waiting to retry ephemeris download in ${eph.retry_after_sec || 0}s.`;"
    yield "    return reason ? `Status: ${reason}` : 'No biodynamic data available.';"
    yield "  };"
    yield "  const fallbackCurrent = (data && data.current && typeof data.current === 'object') ? data.current : {};"
    yield "  const cur = findActiveBiodynamicSegment(data || {}, fallbackCurrent);"
    yield "  if (!data || !data.ok || !cur.sign) {"
    yield "    signEl.textContent = 'Biodynamics unavailable';"
    yield "    elementEl.textContent = 'Automatic ephemeris install is enabled.';"
    yield "    openBtn.style.background = '#fff7d6';"
    yield "    openBtn.style.borderColor = '#c6bb8f';"
    yield "    openBtn.style.color = '#27313a';"
    yield "    boxEl.style.background = '#ffffe0';"
    yield "    panelEl.style.background = 'transparent';"
    yield "    clearBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, upcomingEl]);"
    yield "    dateEl.textContent = '';"
    yield "    windowEl.textContent = buildUnavailableMessage(data);"
    yield "    upcomingEl.textContent = '';"
    yield "    return;"
    yield "  }"
    yield "  const color = String(cur.color || '#7f8a93');"
    yield "  try { if (cur.timestamp && !document.getElementById('biodynamicCalendarModal')) window.__bioModalState.month = bioMonthKeyFromDate(new Date(cur.timestamp)); } catch (_) {}"
    yield "  signEl.textContent = `${cur.sign || '--'} Moon`;"
    yield "  elementEl.textContent = `${cur.element || '--'} / ${cur.plant_part || '--'}`;"
    yield "  openBtn.style.background = color;"
    yield "  openBtn.style.borderColor = '#27313a';"
    yield "  openBtn.style.color = textOnHex(color);"
    yield "  boxEl.style.background = buildRollingGradient(data, cur.timestamp);"
    yield "  panelEl.style.background = 'transparent';"
    yield "  panelEl.style.borderColor = 'transparent';"
    yield "  const mainTextColor = biodynamicTextColor(cur);"
    yield "  const mainTextGradient = buildRollingTextGradient(data, cur.timestamp);"
    yield "  applyBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, upcomingEl], mainTextColor, mainTextGradient);"
    yield "  dateEl.textContent = fmtIsoDate(cur.timestamp);"
    yield "  windowEl.textContent = buildBiodynamicWindowText(data, cur.timestamp, cur);"
    yield "  upcomingEl.textContent = '';"
    yield "}"
    yield ""
    yield "window.__bioModalState = window.__bioModalState || { month: '', data: null, selectedDate: '' };"
    yield "window.__bioNoteDraft = '';"
    yield "function bioEsc(s){ return String(s == null ? '' : s).replace(/[&<>\"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch] || ch)); }"
    yield "function bioTodayIso(){ const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }"
    yield "function bioMonthKeyFromDate(d){ return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; }"
    yield "function bioShiftMonth(monthKey, delta){ const m = String(monthKey || '').match(/^(\\d{4})-(\\d{2})$/); if (!m) return bioMonthKeyFromDate(new Date()); const d = new Date(parseInt(m[1],10), parseInt(m[2],10)-1 + delta, 1); return bioMonthKeyFromDate(d); }"
    yield "function bioFmtDateLabel(iso){ const d = new Date(`${iso}T00:00:00`); if (!Number.isFinite(d.getTime())) return iso || '--'; return d.toLocaleDateString([], { weekday:'short', month:'short', day:'numeric', year:'numeric' }); }"
    yield "function bioTextToHtml(s){ return bioEsc(s).replace(/\\n/g, '<br>'); }"
    yield "function bioToMinutes(hhmm){"
    yield "  const m = String(hhmm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "  if (!m) return null;"
    yield "  const out = (parseInt(m[1], 10) * 60) + parseInt(m[2], 10);"
    yield "  return Math.max(0, Math.min(1440, out));"
    yield "}"
    yield "function bioDayBackground(day){"
    yield "  const fallback = String(day?.dominant_accent || '#fff');"
    yield "  const segments = Array.isArray(day?.segments) ? day.segments : [];"
    yield "  if (!segments.length) return fallback;"
    yield "  const stops = [];"
    yield "  const dividerStops = [];"
    yield "  for (const seg of segments) {"
    yield "    let startMin = bioToMinutes(seg?.start);"
    yield "    let endMin = bioToMinutes(seg?.end);"
    yield "    if (!Number.isFinite(startMin)) startMin = 0;"
    yield "    if (!Number.isFinite(endMin)) endMin = 1440;"
    yield "    if (endMin <= startMin) endMin = 1440;"
    yield "    const color = String(seg?.accent || fallback);"
    yield "    const startPct = Math.max(0, Math.min(100, (startMin / 1440) * 100));"
    yield "    const endPct = Math.max(0, Math.min(100, (endMin / 1440) * 100));"
    yield "    stops.push(`${color} ${startPct.toFixed(2)}%`, `${color} ${endPct.toFixed(2)}%`);"
    yield "    if (startPct > 0 && startPct < 100) {"
    yield "      const lineEnd = Math.min(100, startPct + 0.75);"
    yield "      dividerStops.push(`transparent ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${lineEnd.toFixed(2)}%`, `transparent ${lineEnd.toFixed(2)}%`);"
    yield "    }"
    yield "  }"
    yield "  if (!stops.length) return fallback;"
    yield "  const base = `linear-gradient(90deg, ${stops.join(', ')})`;"
    yield "  if (!dividerStops.length) return base;"
    yield "  const overlay = `linear-gradient(90deg, ${dividerStops.join(', ')})`;"
    yield "  return `${overlay}, ${base}`;"
    yield "}"
    yield "function bioGetWorkingNotes(){"
    yield "  const st = window.__bioModalState || {};"
    yield "  const data = st.data || {};"
    yield "  const notes = Object.assign({}, (data && data.notes && typeof data.notes === 'object') ? data.notes : {});"
    yield "  const inputEl = document.getElementById('bioNoteInput');"
    yield "  if (st.selectedDate && inputEl) notes[st.selectedDate] = String(inputEl.value || '');"
    yield "  return notes;"
    yield "}"
    yield "function renderBiodynamicPrintView(){"
    yield "  const st = window.__bioModalState || {};"
    yield "  const data = st.data || {};"
    yield "  const calendarEl = document.getElementById('bioPrintCalendarSheet');"
    yield "  const notesEl = document.getElementById('bioPrintNotesSheet');"
    yield "  if (!calendarEl || !notesEl) return;"
    yield "  const weekdays = Array.isArray(data.weekday_labels) ? data.weekday_labels : ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];"
    yield "  const days = Array.isArray(data.calendar) ? data.calendar : [];"
    yield "  const notes = bioGetWorkingNotes();"
    yield "  const summaries = (data && data.daily_summaries && typeof data.daily_summaries === 'object') ? data.daily_summaries : {};"
    yield "  if (!days.length) {"
    yield "    const emptyHtml = `<div class='bio-print-title'>Biodynamic Calendar</div><div class='bio-print-subtitle'>No printable calendar data available.</div>`;"
    yield "    calendarEl.innerHTML = emptyHtml;"
    yield "    notesEl.innerHTML = emptyHtml;"
    yield "    return;"
    yield "  }"
    yield "  const grid = weekdays.map((label) => `<div class='bio-print-weekday'>${bioEsc(label)}</div>`).join('') + days.map((day) => {"
    yield "    const classes = ['bio-print-day'];"
    yield "    if (!day.in_month) classes.push('out');"
    yield "    if (day.is_today) classes.push('today');"
    yield "    const meta = `${bioEsc(day.dominant_sign || '--')} / ${bioEsc(day.dominant_plant_part || '--')}`;"
    yield "    const dayBg = bioDayBackground(day);"
    yield "    return `<div class='${classes.join(' ')}' style='background:${dayBg};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};'><div class='bio-print-day-head'><div class='bio-print-day-num'>${bioEsc(day.day)}</div><div class='bio-print-day-part'>${bioEsc(day.dominant_plant_part || '')}</div></div><div class='bio-print-day-meta'>${meta}</div></div>`;"
    yield "  }).join('');"
    yield "  const entries = days.filter((day) => day && day.in_month).map((day) => {"
    yield "    const isSelected = st.selectedDate && st.selectedDate === day.date;"
    yield "    const entryClasses = ['bio-print-entry'];"
    yield "    if (isSelected) entryClasses.push('selected');"
    yield "    const noteText = String(notes[day.date] || '');"
    yield "    const summaryText = String(summaries[day.date] || '');"
    yield "    const noteHtml = noteText.trim() ? bioTextToHtml(noteText) : '<span>None</span>';"
    yield "    const summaryHtml = summaryText.trim() ? bioTextToHtml(summaryText) : '<span>None</span>';"
    yield "    return `<section class='${entryClasses.join(' ')}'><div class='bio-print-entry-head'><div class='bio-print-entry-date'>${bioEsc(bioFmtDateLabel(day.date))}</div><div class='bio-print-entry-meta'>${bioEsc(day.dominant_sign || '--')} / ${bioEsc(day.dominant_plant_part || '--')}</div></div><div class='bio-print-label'>Notes</div><div class='bio-print-block'>${noteHtml}</div><div class='bio-print-label'>Daily Summary</div><div class='bio-print-block'>${summaryHtml}</div></section>`;"
    yield "  }).join('');"
    yield "  const selectedMsg = st.selectedDate ? `Selected day: ${bioEsc(bioFmtDateLabel(st.selectedDate))}. Unsaved text for that note is included.` : 'No day selected.';"
    yield "  calendarEl.innerHTML = `<div class='bio-print-title'>Biodynamic Calendar</div><div class='bio-print-subtitle'>${bioEsc(data.month_label || '--')} | Landscape calendar view</div><div class='bio-print-calendar'>${grid}</div>`;"
    yield "  notesEl.innerHTML = `<div class='bio-print-title'>Biodynamic Notes and Daily Summaries</div><div class='bio-print-subtitle'>${bioEsc(data.month_label || '--')} | ${selectedMsg}</div><div class='bio-print-sections'>${entries}</div>`;"
    yield "}"
    yield "function bioRunPrint(mode){"
    yield "  renderBiodynamicPrintView();"
    yield "  document.body.classList.remove('bio-print-calendar-mode', 'bio-print-notes-mode');"
    yield "  document.body.classList.add('bio-printing');"
    yield "  document.body.classList.add(mode === 'calendar' ? 'bio-print-calendar-mode' : 'bio-print-notes-mode');"
    yield "  window.print();"
    yield "};"
    yield "window.printBiodynamicCalendar = function(){ bioRunPrint('calendar'); };"
    yield "window.printBiodynamicNotes = function(){ bioRunPrint('notes'); };"
    yield "if (!window.__bioPrintCleanupBound) {"
    yield "  window.addEventListener('afterprint', () => document.body.classList.remove('bio-printing', 'bio-print-calendar-mode', 'bio-print-notes-mode'));"
    yield "  window.__bioPrintCleanupBound = true;"
    yield "}"
    yield "function bioSelectDate(dateIso){"
    yield "  const st = window.__bioModalState || {};"
    yield "  st.selectedDate = dateIso || '';"
    yield "  const data = st.data || {};"
    yield "  const notes = (data && data.notes && typeof data.notes === 'object') ? data.notes : {};"
    yield "  const summaries = (data && data.daily_summaries && typeof data.daily_summaries === 'object') ? data.daily_summaries : {};"
    yield "  const day = Array.isArray(data.calendar) ? data.calendar.find((d) => d && d.date === dateIso) : null;"
    yield "  document.querySelectorAll('#biodynamicCalendarModal .bio-day').forEach((el) => el.classList.toggle('selected', el.getAttribute('data-date') === dateIso));"
    yield "  const dateEl = document.getElementById('bioNoteDate');"
    yield "  const summaryDateEl = document.getElementById('bioSummaryDate');"
    yield "  const metaEl = document.getElementById('bioNoteMeta');"
    yield "  const inputEl = document.getElementById('bioNoteInput');"
    yield "  const summaryEl = document.getElementById('bioDailySummary');"
    yield "  const statusEl = document.getElementById('bioNoteStatus');"
    yield "  if (dateEl) dateEl.textContent = dateIso ? bioFmtDateLabel(dateIso) : 'Select a day';"
    yield "  if (summaryDateEl) summaryDateEl.textContent = dateIso ? bioFmtDateLabel(dateIso) : 'Select a day';"
    yield "  if (metaEl) metaEl.textContent = day ? `${day.dominant_sign || '--'} / ${day.dominant_plant_part || '--'}` : '';"
    yield "  if (inputEl) inputEl.value = dateIso ? String(notes[dateIso] || '') : '';"
    yield "  if (summaryEl) {"
    yield "    const summaryText = dateIso ? String(summaries[dateIso] || '').trim() : '';"
    yield "    summaryEl.innerHTML = summaryText ? bioTextToHtml(summaryText) : '<span>No daily summary available for this date.</span>';"
    yield "  }"
    yield "  if (statusEl) statusEl.textContent = '';"
    yield "  renderBiodynamicModalSummary();"
    yield "  renderBiodynamicPrintView();"
    yield "}"
    yield "function renderBiodynamicModalSummary(){"
    yield "  const st = window.__bioModalState || {};"
    yield "  const data = st.data || {};"
    yield "  const sumEl = document.getElementById('bioModalSummary');"
    yield "  if (!sumEl) return;"
    yield "  const days = Array.isArray(data.calendar) ? data.calendar : [];"
    yield "  const selected = days.find((d) => d && d.date === st.selectedDate) || null;"
    yield "  if (!selected) {"
    yield "    const cur = data.current || {};"
    yield "    const upcoming = Array.isArray(data.upcoming) ? data.upcoming.slice(0, 2) : [];"
    yield "    sumEl.textContent = cur.sign ? `${cur.sign} Moon | ${cur.element || '--'} / ${cur.plant_part || '--'} | ${upcoming.map((item) => `${item.start_hm || '--'} ${item.sign || '--'}`).join('  |  ')}` : (data.reason || 'Unavailable');"
    yield "    return;"
    yield "  }"
    yield "  const segs = Array.isArray(selected.segments) ? selected.segments : [];"
    yield "  const first = segs[0] || null;"
    yield "  const next = segs[1] || null;"
    yield "  const sign = selected.dominant_sign || (first && first.sign) || '--';"
    yield "  const element = selected.dominant_element || (first && first.element) || '--';"
    yield "  const part = selected.dominant_plant_part || (first && first.plant_part) || '--';"
    yield "  const currentWindow = first ? `${first.start || '--'} to ${first.end || '--'}` : '--';"
    yield "  const transitions = [];"
    yield "  if (next && next.start) transitions.push(`${next.start} ${next.sign || '--'}`);"
    yield "  if (segs[2] && segs[2].start) transitions.push(`${segs[2].start} ${segs[2].sign || '--'}`);"
    yield "  sumEl.innerHTML = `<div><strong>${bioEsc(sign)} Moon</strong> | ${bioEsc(element)} / ${bioEsc(part)}</div><div>Current window: ${bioEsc(currentWindow)}</div><div>${bioEsc(transitions.join(' | ') || 'No later transitions for this day.')}</div>`;"
    yield "}"
    yield "function renderBiodynamicModal(){"
    yield "  const modal = document.getElementById('biodynamicCalendarModal');"
    yield "  if (!modal) return;"
    yield "  const st = window.__bioModalState || {};"
    yield "  const data = st.data || {};"
    yield "  const monthEl = document.getElementById('bioModalMonthLabel');"
    yield "  const calEl = document.getElementById('bioModalCalendar');"
    yield "  if (monthEl) monthEl.textContent = data.month_label || '--';"
    yield "  if (!calEl) return;"
    yield "  const weekdays = Array.isArray(data.weekday_labels) ? data.weekday_labels : ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];"
    yield "  const days = Array.isArray(data.calendar) ? data.calendar : [];"
    yield "  const notes = (data && data.notes && typeof data.notes === 'object') ? data.notes : {};"
    yield "  let html = weekdays.map((label) => `<div class='bio-weekday'>${bioEsc(label)}</div>`).join('');"
    yield "  html += days.map((day) => {"
    yield "    const classes = ['bio-day'];"
    yield "    if (!day.in_month) classes.push('out');"
    yield "    if (day.is_today) classes.push('today');"
    yield "    if (notes[day.date]) classes.push('noted');"
    yield "    if ((st.selectedDate || '') === day.date) classes.push('selected');"
    yield "    const style = `background:${bioDayBackground(day)};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};`;"
    yield "    const title = `${bioEsc(day.date || '')} ${bioEsc(day.dominant_sign || '')} ${bioEsc(day.dominant_plant_part || '')}`.trim();"
    yield "    const signLabel = String(day.dominant_sign_abbr || day.dominant_sign || '').trim() || '--';"
    yield "    const partLabel = String(day.dominant_plant_part || '').trim() || '--';"
    yield "    return `<button type='button' class='${classes.join(' ')}' data-date='${bioEsc(day.date)}' style='${style}' title='${title}'><span class='bio-day-num'>${bioEsc(day.day)}</span><span class='bio-day-meta'>${bioEsc(signLabel)} ${bioEsc(partLabel)}</span></button>`;"
    yield "  }).join('');"
    yield "  calEl.innerHTML = html;"
    yield "  calEl.querySelectorAll('.bio-day').forEach((btn) => btn.addEventListener('click', () => bioSelectDate(btn.getAttribute('data-date') || '')));"
    yield "  const hasSelectedDay = !!(st.selectedDate && days.some((d) => d && d.date === st.selectedDate));"
    yield "  if (!hasSelectedDay && days.length) {"
    yield "    const today = days.find((d) => d && d.in_month && d.is_today) || null;"
    yield "    const firstInMonth = days.find((d) => d && d.in_month) || days[0];"
    yield "    const defaultDay = today || firstInMonth || null;"
    yield "    st.selectedDate = defaultDay ? defaultDay.date : '';"
    yield "  }"
    yield "  bioSelectDate(st.selectedDate || '');"
    yield "}"
    yield "async function loadBiodynamicMonth(monthKey, preferredDate){"
    yield "  const st = window.__bioModalState || {};"
    yield "  st.month = monthKey;"
    yield "  const resp = await fetch(`/api/biodynamic-calendar?month=${encodeURIComponent(monthKey)}`, { cache:'no-store' });"
    yield "  if (!resp.ok) throw new Error(`biodynamic_month_${resp.status}`);"
    yield "  st.data = await resp.json();"
    yield "  st.selectedDate = preferredDate || st.selectedDate || '';"
    yield "  renderBiodynamicModal();"
    yield "}"
    yield "function setBioOpenButtonLoading(isLoading){"
    yield "  const btn = document.getElementById('bioOpenBtn');"
    yield "  if (!btn) return;"
    yield "  btn.disabled = !!isLoading;"
    yield "  btn.classList.toggle('loading', !!isLoading);"
    yield "  btn.setAttribute('aria-busy', isLoading ? 'true' : 'false');"
    yield "}"
    yield "window.openBiodynamicCalendarModal = async function(){"
    yield "  try {"
    yield "    if (!window.BackdropModal) return;"
    yield "    setBioOpenButtonLoading(true);"
    yield "    const html = `"
    yield "<div class='modal-backdrop' style='display:none'>"
    yield "  <div class='modal bio-modal' id='biodynamicCalendarModal'>"
    yield "    <div class='modal-header'>"
    yield "      <h3 class='modal-title'>Biodynamic Calendar</h3>"
    yield "      <button type='button' onclick='window.BackdropModal && window.BackdropModal.close(\"biodynamicCalendarModal\")' style='border:none;background:transparent;font-size:1.2rem;cursor:pointer;' aria-label='Close'>&times;</button>"
    yield "    </div>"
    yield "    <div class='modal-body'>"
    yield "      <div class='bio-modal-main'>"
    yield "        <div class='bio-nav'>"
    yield "          <button type='button' class='bio-nav-btn' id='bioPrevMonthBtn' aria-label='Previous month' title='Previous month'>&lt;</button>"
    yield "          <div class='bio-month' id='bioModalMonthLabel'>--</div>"
    yield "          <button type='button' class='bio-nav-btn' id='bioNextMonthBtn' aria-label='Next month' title='Next month'>&gt;</button>"
    yield "        </div>"
    yield "        <div class='bio-modal-summary' id='bioModalSummary'></div>"
    yield "        <div class='bio-calendar' id='bioModalCalendar'></div>"
    yield "        <div class='bio-modal-actions'>"
    yield "          <button type='button' class='bio-print-btn' id='bioPrintCalendarBtn'>Print Calendar</button>"
    yield "          <button type='button' class='bio-print-btn' id='bioPrintNotesBtn'>Print Notes</button>"
    yield "        </div>"
    yield "      </div>"
    yield "      <div class='bio-modal-side'>"
    yield "        <div class='bio-note-card bio-summary-card'>"
    yield "          <div class='bio-note-title'>Daily Summary</div>"
    yield "          <div class='bio-note-date' id='bioSummaryDate'>Select a day</div>"
    yield "          <div id='bioDailySummary' class='bio-summary-output'></div>"
    yield "        </div>"
    yield "        <div class='bio-note-card'>"
    yield "          <div class='bio-note-title'>Your Notes</div>"
    yield "          <div class='bio-note-date' id='bioNoteDate'>Select a day</div>"
    yield "          <div class='bio-note-meta' id='bioNoteMeta'></div>"
    yield "          <textarea id='bioNoteInput' class='bio-note-input' placeholder='Add a note for the selected biodynamic day...'></textarea>"
    yield "          <div class='bio-note-actions'>"
    yield "            <button type='button' class='bio-save-btn' id='bioSaveNoteBtn'>Save Note</button>"
    yield "            <div class='bio-note-status' id='bioNoteStatus'></div>"
    yield "          </div>"
    yield "        </div>"
    yield "      </div>"
    yield "    </div>"
    yield "    <div class='bio-print-sheet' id='bioPrintCalendarSheet' aria-hidden='true'></div>"
    yield "    <div class='bio-print-sheet' id='bioPrintNotesSheet' aria-hidden='true'></div>"
    yield "  </div>"
    yield "</div>`;"
    yield "    window.BackdropModal.close('biodynamicCalendarModal');"
    yield "    const modal = window.BackdropModal.openFromHtml(html, 'biodynamicCalendarModal');"
    yield "    if (!modal) return;"
    yield "    const monthKey = (window.__bioModalState && window.__bioModalState.month) || bioMonthKeyFromDate(new Date());"
    yield "    const todayIso = bioTodayIso();"
    yield "    const preferredDate = monthKey === bioMonthKeyFromDate(new Date(`${todayIso}T00:00:00`)) ? todayIso : '';"
    yield "    document.getElementById('bioPrevMonthBtn').addEventListener('click', () => loadBiodynamicMonth(bioShiftMonth(window.__bioModalState.month, -1), ''));"
    yield "    document.getElementById('bioNextMonthBtn').addEventListener('click', () => loadBiodynamicMonth(bioShiftMonth(window.__bioModalState.month, 1), ''));"
    yield "    document.getElementById('bioPrintCalendarBtn').addEventListener('click', () => { if (window.printBiodynamicCalendar) window.printBiodynamicCalendar(); });"
    yield "    document.getElementById('bioPrintNotesBtn').addEventListener('click', () => { if (window.printBiodynamicNotes) window.printBiodynamicNotes(); });"
    yield "    document.getElementById('bioSaveNoteBtn').addEventListener('click', async () => {"
    yield "      const st = window.__bioModalState || {};"
    yield "      const dateIso = st.selectedDate || '';"
    yield "      const input = document.getElementById('bioNoteInput');"
    yield "      const status = document.getElementById('bioNoteStatus');"
    yield "      const btn = document.getElementById('bioSaveNoteBtn');"
    yield "      if (!dateIso || !input || !status) { if (status) status.textContent = 'Select a day first.'; return; }"
    yield "      if (btn) { if (!btn.dataset.baseLabel) btn.dataset.baseLabel = btn.textContent || 'Save Note'; btn.disabled = true; btn.textContent = 'Saving...'; }"
    yield "      status.textContent = 'Saving...';"
    yield "      const noteText = input.value || '';"
    yield "      const resp = await fetch('/api/biodynamic-note', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ date: dateIso, note: noteText }) });"
    yield "      const payload = await resp.json().catch(() => ({}));"
    yield "      if (!resp.ok) { status.textContent = (payload && payload.error) ? payload.error : 'Save failed'; if (typeof window.showToast === 'function') window.showToast('Failed to save biodynamic note', 'error'); if (btn) { btn.disabled = false; btn.textContent = btn.dataset.baseLabel || 'Save Note'; } return; }"
    yield "      st.data = st.data || {};"
    yield "      st.data.notes = st.data.notes || {};"
    yield "      if ((noteText || '').trim()) st.data.notes[dateIso] = String(noteText).trim(); else delete st.data.notes[dateIso];"
    yield "      renderBiodynamicModal();"
    yield "      status.textContent = 'Saved';"
    yield "      if (typeof window.showToast === 'function') window.showToast('Biodynamic note saved', 'ok');"
    yield "      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.baseLabel || 'Save Note'; }"
    yield "    });"
    yield "    await loadBiodynamicMonth(monthKey, preferredDate);"
    yield "  } catch (e) {"
    yield "    console.error('Failed to open biodynamic modal', e);"
    yield "  } finally {"
    yield "    setBioOpenButtonLoading(false);"
    yield "  }"
    yield "};"

    # Dynamic sensor UI helpers 
    yield "document.addEventListener('DOMContentLoaded',()=>{"
    yield "  if (typeof drawSunPath === 'function') drawSunPath(astroData);"
    yield "  if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData);"
    yield "  if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);"
    yield "  if (typeof loadWeatherForecast === 'function') loadWeatherForecast();"
    yield "  const form = document.querySelector('form');"
    yield "  const forecastFiveDayBtn = document.getElementById('forecastFiveDayBtn');"
    yield "  const bioOpenBtn = document.getElementById('bioOpenBtn');"
    yield "  const btn = document.getElementById('saveBtn');"
    yield "  const spinner = document.getElementById('saveSpinner');"
    yield "  if (forecastFiveDayBtn) {"
    yield "    forecastFiveDayBtn.addEventListener('click', function(){ if (window.openWeatherForecastModal) window.openWeatherForecastModal(); });"
    yield "  }"
    yield "  if (bioOpenBtn) {"
    yield "    bioOpenBtn.addEventListener('click', function(){ if (window.openBiodynamicCalendarModal) window.openBiodynamicCalendarModal(); });"
    yield "  }"
    yield "  if(form && btn && spinner){"
    yield "    form.addEventListener('submit',()=>{"
    yield "      spinner.style.display='inline-block';"
    yield "      btn.disabled=true;"
    yield "    });"
    yield "  }"
    yield "});"
    
    yield "let knownSensors = new Set();"
    yield "let pendingLayoutRefresh = false;"
    yield "let deferredLayoutRefresh = false;"
    yield "let __lastSuppressedSwitchLayoutAt = 0;"

    yield "function ensureSensorInSelector(sid) {"
    yield "  const sel = document.getElementById('sensorSelect');"
    yield "  if (!sel) return;"
    yield "  if (![...sel.options].some(o => o.value === sid)) {"
    yield "    const opt = document.createElement('option');"
    yield "    opt.value = sid;"
    yield "    opt.textContent = sid;"
    yield "    sel.appendChild(opt);"
    yield "  }"
    yield "}"

    yield "function ensureSensorUI(sid, metricList, locationText) {" 
    yield " if (!sid) return;" 
    yield " const pageSensor = document.getElementById('sensor_id')?.value || 'All';" 
    yield " if (pageSensor !== 'All' && pageSensor !== sid) return;" 
    yield "" 
    yield " const safeId = (x) => x.replace(/[^a-zA-Z0-9_\\-]/g,'_');" 
    yield " const toSafeMetric = (m) => (typeof toSafe === 'function' ? toSafe(m) : safeId(m));" 
    yield ""  
    yield " const existingRow = document.querySelector('.sensor-row');" 
    yield " const byGraphModal = document.getElementById('graphModal')?.parentElement || null;" 
    yield " const dashboard = document.querySelector('.dashboard');"
    yield " const parent = dashboard || (existingRow && existingRow.parentElement) || byGraphModal || document.body;" 
    yield " const switchGroup = document.getElementById('group_switches');"
    yield ""

    yield "  const groupId = `group_${sid}`;"
    yield "  let group = document.getElementById(groupId);"
    yield "  if (!group) {"
    yield "    group = document.createElement('div');"
    yield "    group.id = groupId;"
    yield "    group.className = 'sensor-group';"
    yield "    group.style.width = '100%';"
    yield "    group.style.flex = '0 0 100%';"
    yield "    group.style.display = 'block';"
    yield "    if (switchGroup && switchGroup.parentElement === parent) parent.insertBefore(group, switchGroup);"
    yield "    else parent.appendChild(group);"
    yield "  }"
    yield ""

    #  Create header if missing        
    yield "  const headerId = `${sid}_header`; "
    yield "  if (!document.getElementById(headerId)) {"
    yield "    const headerWrap = document.createElement('div');"
    yield "    headerWrap.style.textAlign = 'center';"
    yield "    headerWrap.style.width = '100%';"
    yield "    headerWrap.style.marginTop = '1rem';"
    yield "    const locText = (locationText || sid);"
    yield "    const sidUpper = (sid || '').toUpperCase();"
    yield "    const sidLower = (sid || '').toLowerCase();"
    yield "    const pendingColor = '#ffc107';"  
    yield "    const header = document.createElement('h3');"
    yield "    header.id = `${sid}_header`;"
    yield "    const dot = document.createElement('span');"
    yield "    dot.className = 'sensor-status-dot';"
    yield "    dot.id = `${sid}_statusdot`;"
    yield "    dot.dataset.sid = sid;"
    yield "    dot.setAttribute('title', 'Connection status: unknown');"
    yield "    dot.setAttribute('aria-label', 'Connection status: unknown');"
    yield "    dot.setAttribute('style', `display:inline-block;width:15px;height:15px;border-radius:50%;vertical-align:middle;margin-right:6px;margin-bottom:4px;background:${pendingColor};border:1px solid #666;`);"
    yield "    header.appendChild(dot);"
    yield "    header.appendChild(document.createTextNode(` ${sidUpper} `));"
    yield "    const settingsLink = document.createElement('a');"
    yield "    settingsLink.href = '#';"
    yield "    settingsLink.title = 'Open settings';"
    yield "    settingsLink.setAttribute('aria-label', `Open ${sidUpper} Settings`);"
    yield "    settingsLink.setAttribute('style', 'margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;');"
    yield "    settingsLink.addEventListener('click', function(ev) {"
    yield "      ev.preventDefault();"
    yield "      if (window.editSensorSettings) window.editSensorSettings(sidLower);"
    yield "    });"
    yield "    settingsLink.innerHTML = `"
    yield from _settings_gear_svg_lines(indent="      ", aria_hidden=True)
    yield "    `;"
    yield "    header.appendChild(settingsLink);"
    yield "    header.appendChild(document.createTextNode(locText));"
    yield "    headerWrap.appendChild(header);"
    yield "    group.appendChild(headerWrap);"
    yield "  } else {"
    yield "    const hdr = document.getElementById(headerId);"
    yield "    if (hdr && !document.getElementById(`${sid}_statusdot`)) {"
    yield "      const dot = document.createElement('span');"
    yield "      dot.className = 'sensor-status-dot';"
    yield "      dot.id = `${sid}_statusdot`;"
    yield "      dot.setAttribute('data-sid', sid);"
    yield "      dot.setAttribute('title', 'Connection status: unknown');"
    yield "      dot.setAttribute('aria-label', 'Connection status: unknown');"
    yield "      dot.setAttribute('style', 'display:inline-block;width:15px;height:15px;border-radius:50%;"
    yield "                                   vertical-align:middle;margin-right:6px;margin-bottom:4px;"
    yield "                                   background:#ffc107;border:1px solid #666;');"
    yield "      hdr.insertBefore(dot, hdr.firstChild);"
    yield "    }"
    yield "  }"
    yield ""
    
    #  Create row wrapper if missing
    yield "  const rowId = `row_${sid}`;"
    yield "  let row = document.getElementById(rowId);"
    yield "  if (!row) {"
    yield "    row = document.createElement('div');"
    yield "    row.id = rowId;"
    yield "    row.className = 'sensor-row';"
    yield "    group.appendChild(row);"
    yield "  }"
    yield ""
    #  Ensure metric containers exist
    yield "  const styleMap = (expectedDisplayStyleMap && expectedDisplayStyleMap[sid]) || {};"
    yield "  (metricList || []).forEach((metric, idx) => {"
    yield "    const safeMetric = toSafeMetric(metric);"
    yield "    const containerId = `${sid}_${safeMetric}_container`;"
    yield "    if (document.getElementById(containerId)) return;"
    yield ""
    yield "    const container = document.createElement('div');"
    yield "    container.className = 'metric-container';"
    yield "    container.id = containerId;"
    yield "    container.dataset.sensor = sid;"
    yield "    container.dataset.metric = metric;"
    yield "    const styleKey = `METRIC_${idx + 1}`;"
    yield "    container.dataset.displayStyle = window.normalizeDisplayStyle(styleMap[styleKey] || window.displayStyle || 'Gauge');"
    yield ""
    yield "    const safe = `${sid}_${safeMetric}`;"
    yield "    container.innerHTML = "
    yield "      `<div class='metric-title'>${metric}</div>` +"
    yield "      `<div class='gauge-container'><div class='gauge-view' id='${safe}GaugeContainer'>` +"
    yield "        `<canvas id='${safe}Gauge'></canvas>` +"
    yield "      `</div></div>` +"
    yield "      `<div class='metric-current-value' id='${safe}_val'>--</div>` +"
    yield "      `<div class='metric-stats' id='${safe}_stats'>` +"
    yield "        `<div>Min<br><small>--</small></div>` +"
    yield "        `<div>Avg<br>--</div>` +"
    yield "        `<div>Max<br><small>--</small></div>` +"
    yield "      `</div>`;"
    yield ""
    yield "    row.appendChild(container);"
    yield "    if (window.ensureContainerDisplayStyle) {"
    yield "      window.ensureContainerDisplayStyle(container);"
    yield "    }"
    yield "    if (window.bindMetricContainer) {"
    yield "      window.bindMetricContainer(container);"
    yield "    }"
    yield "  });"
    yield "}"

    # helpers for what type of metric container is being displayed
    yield "window.DISPLAY_STYLES = ['Gauge', 'Graph6hr', 'Graph24hr'];"
    yield "window.metricDisplayStyles = window.metricDisplayStyles || {};"

    yield "window.normalizeDisplayStyle = function(raw) {"
    yield "  const s = (raw || '').toString().toLowerCase();"
    yield "  if (s === 'gauge') return 'Gauge';"
    yield "  if (s === 'graph' || s === 'graph24' || s === 'graph24hr' || s === '24h' || s === '24hr') return 'Graph24hr';"
    yield "  if (s === 'graph6' || s === 'graph6hr' || s === '6h' || s === '6hr') return 'Graph6hr';"
    yield "  return 'Gauge';"
    yield "};"

    yield "window.detectDisplayStyleFromDom = function(container) {"
    yield "  if (!container) return 'Gauge';"
    yield "  const ds = container.dataset.displayStyle;"
    yield "  if (ds) return window.normalizeDisplayStyle(ds);"
    yield "  const hasMicro = !!container.querySelector('.micrograph-canvas');"
    yield "  if (hasMicro) {"
    yield "    const r1 = container.dataset.range || container.dataset.graphRange || '';"
    yield "    const r = r1.toString().toLowerCase();"
    yield "    if (r.indexOf('6') >= 0) return 'Graph6hr';"
    yield "    return 'Graph24hr';"
    yield "  }"
    yield "  const hasGauge = !!container.querySelector('canvas[id$=\"Gauge\"]');"
    yield "  if (hasGauge) return 'Gauge';"
    yield "  return 'Gauge';"
    yield "};"

    yield "window.registerContainerStyle = function(container, style) {"
    yield "  if (!container || !container.id) return;"
    yield "  const norm = window.normalizeDisplayStyle(style);"
    yield "  window.metricDisplayStyles[container.id] = norm;"
    yield "  container.dataset.displayStyle = norm;"
    yield "};"

    yield "window.getContainerStyle = function(container) {"
    yield "  if (!container || !container.id) return 'Gauge';"
    yield "  const fromMap = window.metricDisplayStyles[container.id];"
    yield "  if (fromMap) return fromMap;"
    yield "  const inferred = window.detectDisplayStyleFromDom(container);"
    yield "  window.metricDisplayStyles[container.id] = inferred;"
    yield "  container.dataset.displayStyle = inferred;"
    yield "  return inferred;"
    yield "};"

    yield "window.getContainerStyleById = function(containerId) {"
    yield "  if (!containerId) return 'Gauge';"
    yield "  const el = document.getElementById(containerId);"
    yield "  if (!el) return 'Gauge';"
    yield "  return window.getContainerStyle(el);"
    yield "};"

    yield "document.addEventListener('DOMContentLoaded', function() {"
    yield "  try {"
    yield "    const all = document.querySelectorAll('.metric-container');"
    yield "    all.forEach(function(c) {"
    yield "      if (!c.id) return;"
    yield "      const cur = window.metricDisplayStyles[c.id];"
    yield "      if (cur) {"
    yield "        c.dataset.displayStyle = cur;"
    yield "      } else {"
    yield "        const inferred = window.detectDisplayStyleFromDom(c);"
    yield "        window.metricDisplayStyles[c.id] = inferred;"
    yield "        c.dataset.displayStyle = inferred;"
    yield "      }"
    yield "    });"
    yield "  } catch (e) {"
    yield "    console.warn('init metricDisplayStyles error', e);"
    yield "  }"
    yield "});"

    # ---- gauge init ----
    yield "function normalizeDegrees(value) {"
    yield "  const n = Number(value);"
    yield "  if (!Number.isFinite(n)) return null;"
    yield "  return ((n % 360) + 360) % 360;"
    yield "}"
    yield ""
    yield "function drawCompassGauge(canvas, rawValue) {"
    yield "  if (!canvas) return;"
    yield "  const ctx = canvas.getContext('2d');"
    yield "  if (!ctx) return;"
    yield "  const dpr = window.devicePixelRatio || 1;"
    yield "  const cssSize = 210;"
    yield "  const scale = cssSize / 170;"
    yield "  canvas.style.width = `${cssSize}px`;"
    yield "  canvas.style.height = `${cssSize}px`;"
    yield "  canvas.width = Math.round(cssSize * dpr);"
    yield "  canvas.height = Math.round(cssSize * dpr);"
    yield "  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);"
    yield "  ctx.clearRect(0, 0, cssSize, cssSize);"
    yield "  const cx = cssSize / 2;"
    yield "  const cy = cssSize / 2;"
    yield "  const r = 66 * scale;"
    yield "  ctx.save();"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(cx, cy, r, 0, Math.PI * 2);"
    yield "  ctx.fillStyle = '#dff7fb';"
    yield "  ctx.fill();"
    yield "  ctx.lineWidth = 10 * scale;"
    yield "  ctx.strokeStyle = '#a9d7e3';"
    yield "  ctx.stroke();"
    yield "  ctx.lineWidth = 1;"
    yield "  ctx.strokeStyle = 'rgba(32,45,52,.35)';"
    yield "  ctx.stroke();"
    yield ""
    yield "  for (let deg = 0; deg < 360; deg += 15) {"
    yield "    const rad = (deg - 90) * Math.PI / 180;"
    yield "    const major = deg % 45 === 0;"
    yield "    const inner = r - (major ? 13 : 8) * scale;"
    yield "    const outer = r + scale;"
    yield "    ctx.beginPath();"
    yield "    ctx.moveTo(cx + Math.cos(rad) * inner, cy + Math.sin(rad) * inner);"
    yield "    ctx.lineTo(cx + Math.cos(rad) * outer, cy + Math.sin(rad) * outer);"
    yield "    ctx.lineWidth = major ? 2 * scale : scale;"
    yield "    ctx.strokeStyle = major ? '#1f2933' : 'rgba(31,41,51,.45)';"
    yield "    ctx.stroke();"
    yield "  }"
    yield ""
    yield "  const labels = [{t:'N',d:0},{t:'E',d:90},{t:'S',d:180},{t:'W',d:270}];"
    yield "  ctx.font = `700 ${Math.round(16 * scale)}px sans-serif`;"
    yield "  ctx.fillStyle = '#1f2933';"
    yield "  ctx.textAlign = 'center';"
    yield "  ctx.textBaseline = 'middle';"
    yield "  labels.forEach(({t,d}) => {"
    yield "    const rad = (d - 90) * Math.PI / 180;"
    yield "    ctx.fillText(t, cx + Math.cos(rad) * (r - 27 * scale), cy + Math.sin(rad) * (r - 27 * scale));"
    yield "  });"
    yield ""
    yield "  const value = normalizeDegrees(rawValue);"
    yield "  if (value !== null) {"
    yield "    const rad = (value - 90) * Math.PI / 180;"
    yield "    const tipX = cx + Math.cos(rad) * (r - 14 * scale);"
    yield "    const tipY = cy + Math.sin(rad) * (r - 14 * scale);"
    yield "    const tailX = cx - Math.cos(rad) * 16 * scale;"
    yield "    const tailY = cy - Math.sin(rad) * 16 * scale;"
    yield "    const sideRad = rad + Math.PI / 2;"
    yield "    ctx.beginPath();"
    yield "    ctx.moveTo(tipX, tipY);"
    yield "    ctx.lineTo(tailX + Math.cos(sideRad) * 6 * scale, tailY + Math.sin(sideRad) * 6 * scale);"
    yield "    ctx.lineTo(tailX - Math.cos(sideRad) * 6 * scale, tailY - Math.sin(sideRad) * 6 * scale);"
    yield "    ctx.closePath();"
    yield "    ctx.fillStyle = '#050505';"
    yield "    ctx.fill();"
    yield "    ctx.beginPath();"
    yield "    ctx.arc(cx, cy, 8 * scale, 0, Math.PI * 2);"
    yield "    ctx.fill();"
    yield "  }"
    yield "  ctx.restore();"
    yield "}"
    yield ""
    yield "function getMetricCanvasSize(canvas) {"
    yield "  const root = (canvas && canvas.closest && canvas.closest('.dashboard')) || document.documentElement;"
    yield "  let cssWidth = metricCanvasWidth;"
    yield "  let cssHeight = metricCanvasHeight;"
    yield "  try {"
    yield "    const styles = window.getComputedStyle(root);"
    yield "    const rawW = Number.parseFloat(styles.getPropertyValue('--canvas-width'));"
    yield "    const rawH = Number.parseFloat(styles.getPropertyValue('--canvas-height'));"
    yield "    if (Number.isFinite(rawW) && rawW > 0) cssWidth = rawW;"
    yield "    if (Number.isFinite(rawH) && rawH > 0) cssHeight = rawH;"
    yield "  } catch (_) {}"
    yield "  return { cssWidth, cssHeight };"
    yield "}"
    yield ""
    yield "function drawFallbackGauge(canvas, rawValue, config) {"
    yield "  if (!canvas || !config) return;"
    yield "  const ctx = canvas.getContext('2d');"
    yield "  if (!ctx) return;"
    yield "  const size = getMetricCanvasSize(canvas);"
    yield "  const cssWidth = size.cssWidth;"
    yield "  const cssHeight = size.cssHeight;"
    yield "  const dpr = window.devicePixelRatio || 1;"
    yield "  canvas.style.width = '';"
    yield "  canvas.style.height = '';"
    yield "  canvas.width = Math.round(cssWidth * dpr);"
    yield "  canvas.height = Math.round(cssHeight * dpr);"
    yield "  ctx.save();"
    yield "  ctx.scale(dpr, dpr);"
    yield "  ctx.clearRect(0, 0, cssWidth, cssHeight);"
    yield "  const cx = cssWidth / 2;"
    yield "  const cy = cssHeight * 0.56;"
    yield "  const radius = Math.min(cssWidth * 0.43, cssHeight * 0.48);"
    yield "  const min = Number(config.min);"
    yield "  const max = Number(config.max);"
    yield "  const value = Number(rawValue);"
    yield "  const start = Math.PI * 0.82;"
    yield "  const end = Math.PI * 2.18;"
    yield "  const span = end - start;"
    yield "  const zones = Array.isArray(config.zones) ? config.zones : [];"
    yield "  function angleFor(v) {"
    yield "    if (!Number.isFinite(v) || !Number.isFinite(min) || !Number.isFinite(max) || max <= min) return start;"
    yield "    const ratio = Math.max(0, Math.min(1, (v - min) / (max - min)));"
    yield "    return start + (ratio * span);"
    yield "  }"
    yield "  ctx.lineWidth = 16;"
    yield "  ctx.lineCap = 'round';"
    yield "  ctx.strokeStyle = '#d7dde2';"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(cx, cy, radius, start, end);"
    yield "  ctx.stroke();"
    yield "  zones.forEach(z => {"
    yield "    const zMin = Number(z?.min);"
    yield "    const zMax = Number(z?.max);"
    yield "    if (!Number.isFinite(zMin) || !Number.isFinite(zMax)) return;"
    yield "    ctx.strokeStyle = z?.strokeStyle || z?.color || '#6FADCF';"
    yield "    ctx.beginPath();"
    yield "    ctx.arc(cx, cy, radius, angleFor(zMin), angleFor(zMax));"
    yield "    ctx.stroke();"
    yield "  });"
    yield "  const needle = angleFor(value);"
    yield "  ctx.lineWidth = 4;"
    yield "  ctx.lineCap = 'round';"
    yield "  ctx.strokeStyle = '#111827';"
    yield "  ctx.beginPath();"
    yield "  ctx.moveTo(cx, cy);"
    yield "  ctx.lineTo(cx + Math.cos(needle) * radius * 0.72, cy + Math.sin(needle) * radius * 0.72);"
    yield "  ctx.stroke();"
    yield "  ctx.fillStyle = '#111827';"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(cx, cy, 5, 0, Math.PI * 2);"
    yield "  ctx.fill();"
    yield "  ctx.fillStyle = '#374151';"
    yield "  ctx.font = '12px sans-serif';"
    yield "  ctx.textAlign = 'center';"
    yield "  if (Number.isFinite(min)) ctx.fillText(String(min), cx - radius * 0.78, cy + 18);"
    yield "  if (Number.isFinite(max)) ctx.fillText(String(max), cx + radius * 0.78, cy + 18);"
    yield "  ctx.restore();"
    yield "}"
    yield ""
    yield "function initGauge() {"
    yield "  const metricContainers = document.querySelectorAll('.metric-container');"
    yield "  metricContainers.forEach(container => {"
    yield "    const sensor = container.dataset.sensor;"
    yield "    const metric = container.dataset.metric;"
    yield "    const safe = `${sensor}_${toSafe(metric)}`;"
    yield "    const canvasId = `${safe}Gauge`;"
    yield "    const labelId = `${safe}_val`;"
    yield "    const canvas = document.getElementById(canvasId);"
    yield "    const label = document.getElementById(labelId);"
    yield "    if (!canvas || !label) return;"  
    yield "    const config = gaugeConfig?.[metric];"
    yield "    if (!config) return;"
    yield "    let value = currentValues?.[sensor]?.[metric];"
    yield "    const isNull = (value == null);"
    yield "    if ((config.render || '').toLowerCase() === 'compass') {"
    yield "      drawCompassGauge(canvas, isNull ? null : value);"
    yield "      window[`${safe}_compass`] = canvas;"
    yield "      const valueMetric = config.value_metric || metric;"
    yield "      const valueConfig = gaugeConfig?.[valueMetric] || config;"
    yield "      const displayValue = currentValues?.[sensor]?.[valueMetric];"
    yield "      label.innerText = formatCurrentValue(displayValue, valueConfig);"
    yield "      if (window.registerContainerStyle) {"
    yield "        const initialStyle = window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');"
    yield "        window.registerContainerStyle(container, initialStyle);"
    yield "      }"
    yield "      return;"
    yield "    }"
    yield "    if (isNull) value = 0;"
    yield "    const opts = {"
    yield "      angle: -0.2, lineWidth: 0.25, radiusScale: 0.9,"
    yield "      pointer: { length: 0.5, strokeWidth: 0.035, color: '#000000' },"
    yield "      staticZones: config.zones || [],"
    yield "      staticLabels: {"
    yield "        font: '12px sans-serif',"
    yield "        labels: config.ticks, color: '#000', fractionDigits: 1"
    yield "      },"
    yield "      colorStart: '#6FADCF', colorStop: '#8FC0DA', strokeColor: '#E0E0E0',"
    yield "      generateGradient: true, highDpiSupport: true"
    yield "    };"
    yield "    const canvasSize = getMetricCanvasSize(canvas);"
    yield "    canvas.width = Math.round(canvasSize.cssWidth);"
    yield "    canvas.height = Math.round(canvasSize.cssHeight);"
    yield "    canvas.style.width = '';"
    yield "    canvas.style.height = '';"
    yield "    if (typeof Gauge === 'function') {"
    yield "      try {"
    yield "        const gauge = new Gauge(canvas).setOptions(opts);"
    yield "        gauge.maxValue = config.max;"
    yield "        gauge.setMinValue(config.min);"
    yield "        gauge.animationSpeed = 32;"
    yield "        gauge.set(value);"
    yield "        gauge.render();"
    yield "        window[`${safe}_gauge`] = gauge;"
    yield "      } catch (e) {"
    yield "        console.warn('Gauge render failed, using fallback', safe, e);"
    yield "        drawFallbackGauge(canvas, value, config);"
    yield "        window[`${safe}_gauge`] = null;"
    yield "      }"
    yield "    } else {"
    yield "      drawFallbackGauge(canvas, value, config);"
    yield "      window[`${safe}_gauge`] = null;"
    yield "    }"
    yield "    label.innerText = isNull ? '--' : value + ' ' + config.unit;"
    yield "    if (window.registerContainerStyle) {"
    yield "      const initialStyle = window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');"
    yield "      window.registerContainerStyle(container, initialStyle);"
    yield "    }"    
    yield "  });"
    yield "}"

    # ---- onload setup and periodic refreshes ----
    yield "let retryCount = 0;"
    yield "const maxRetries = 3;"
    yield "function checkAndRetryIfNoGauges() {"
    yield "  let retryNeeded = false;"
    yield "  for (const [sensorID, metrics] of Object.entries(expectedGaugeMap)) {"
    yield "    for (const metric of metrics) {"
    yield "      const sensor = sensorID;"
    yield "      const metricLabel = metric;"
    yield "      const safe = `${sensor}_${toSafe(metricLabel)}`;"
    yield "      const el = document.getElementById(`${safe}_val`);"
    yield "      if (!el) {"
    yield "        retryNeeded = true;"
    yield "        console.log(`[checkAndRetry] Missing gauge: ${safe}`);"
    yield "      }"
    yield "    }"
    yield "  }"
    yield "  if (retryNeeded) {"
    yield "    if (retryCount < maxRetries) {"
    yield "      retryCount++;"
    yield "      console.log(`[checkAndRetry] Retrying initGauge() in 1s (attempt ${retryCount})`);"
    yield "      setTimeout(initGauge, 1000);"
    yield "    } else {"
    yield "      console.log(`[checkAndRetry] Max retries reached.`);"
    yield "    }"
    yield "  }"
    yield "}"
    
    yield "document.addEventListener('DOMContentLoaded', () => {"
    #  Seed known sensors from server-rendered expectation map
    yield "  try { knownSensors = new Set(Object.keys(expectedGaugeMap || {})); } catch(_) { knownSensors = new Set(); }"
    yield "  checkAndRetryIfNoGauges();"
    yield "});"

    # ---- more helper functions ----
    yield "function formatIsoForStats(ts) {"
    yield "  if (!ts || typeof ts !== 'string') return '--';"
    # Remove fractional seconds like .123456 just before Z, an offset, or end
    yield "  const noMicros = ts.replace(/\\.\\d{1,6}(?=Z|[+-]\\d{2}:\\d{2}|$)/, '');"
    # Strip trailing timezone info (Z or ±HH:MM)
    yield "  const noTz = noMicros.replace(/(Z|[+-]\\d{2}:\\d{2})$/, '');"
    # Turn date/time separator into a line break for the stats block
    yield "  return noTz.replace('T', '<br>').replace(' ', '<br>');"
    yield "}"
    yield ""
    yield "function renderStatsHtml(minVal, avgVal, maxVal, minTs, maxTs) {"
    yield "  return `<div>Min<br><small>${minVal} at<br> ${minTs}</small></div>` +"
    yield "         `<div>Avg<br>${avgVal}</div>` +"
    yield "         `<div>Max<br><small>${maxVal} at<br> ${maxTs}</small></div>`;"
    yield "}"
    yield ""
    yield "function toFixedOrDash(v) {"
    yield "  const n = Number.parseFloat(v);"
    yield "  return Number.isFinite(n) ? n.toFixed(1) : '--';"
    yield "}"
    yield ""
    yield "function formatCurrentValue(value, config) {"
    yield "  const unit = (config && config.unit) || '';"
    yield "  if (typeof value !== 'number') return '--';"
    yield "  const precision = config && config.display_precision;"
    yield "  const text = Number.isInteger(precision) ? value.toFixed(precision) : String(value);"
    yield "  return `${text} ${unit}`.trim();"
    yield "}"

    yield "function _normSwitchId(id) {"
    yield "  return String(id || '').trim().toLowerCase();"
    yield "}"

    yield "function _asStringSet(items) {"
    yield "  const out = new Set();"
    yield "  (items || []).forEach(v => {"
    yield "    const s = String(v || '').trim();"
    yield "    if (s) out.add(s);"
    yield "  });"
    yield "  return out;"
    yield "}"

    yield "function _sameStringSet(a, b) {"
    yield "  if (a.size !== b.size) return false;"
    yield "  for (const v of a) { if (!b.has(v)) return false; }"
    yield "  return true;"
    yield "}"

    yield "function _renderedMetricsForSensor(sid) {"
    yield "  const row = document.getElementById(`row_${sid}`);"
    yield "  if (!row) return [];"
    yield "  return Array.from(row.querySelectorAll('.metric-container[data-metric]'))"
    yield "    .map(el => String(el.dataset.metric || '').trim())"
    yield "    .filter(Boolean);"
    yield "}"

    yield "function _renderedSwitchIds() {"
    yield "  return Array.from(document.querySelectorAll('.switch-metric-container')).flatMap((card) => {"
    yield "    const raw = String(card.dataset.switchIds || '').trim();"
    yield "    if (raw) {"
    yield "      return raw.split(',').map(_normSwitchId).filter(Boolean);"
    yield "    }"
    yield "    const header = card.querySelector('h3[id$=\"_header\"]');"
    yield "    if (!header) return [];"
    yield "    const fallback = _normSwitchId(String(header.id || '').replace(/_header$/, ''));"
    yield "    return fallback ? [fallback] : [];"
    yield "  });"
    yield "}"

    yield "function _layoutSignature(available, nextExpMap, renderableSwitches) {"
    yield "  const sensors = (available || []).map(sid => ({"
    yield "    sid: String(sid || ''),"
    yield "    metrics: (Array.isArray(nextExpMap?.[sid]) ? nextExpMap[sid] : []).map(m => String(m || '').trim()).filter(Boolean).sort()"
    yield "  })).sort((a,b) => a.sid.localeCompare(b.sid));"
    yield "  const switches = (renderableSwitches || []).map(_normSwitchId).filter(Boolean).sort();"
    yield "  return JSON.stringify({ sensors, switches });"
    yield "}"
    yield "function hasOpenBackdropModal() {"
    yield "  return !!document.querySelector('.modal-backdrop .modal, .modal-backdrop');"
    yield "}"
    yield "function dashboardRefreshPaused() {"
    yield "  return !!(window.ModalBusyCursor && window.ModalBusyCursor.isBusy && window.ModalBusyCursor.isBusy())"
    yield "    || document.documentElement.classList.contains('modal-busy-cursor')"
    yield "    || document.body.classList.contains('modal-busy-cursor')"
    yield "    || document.visibilityState === 'hidden'"
    yield "    || hasOpenBackdropModal();"
    yield "}"

    yield "function scheduleLayoutRefresh(reason, sig) {"
    yield "  if (pendingLayoutRefresh) return;"
    yield "  if (hasOpenBackdropModal()) {"
    yield "    if (deferredLayoutRefresh) return;"
    yield "    deferredLayoutRefresh = true;"
    yield "    console.info('[layout-refresh-deferred]', reason || 'layout changed');"
    yield "    setTimeout(() => {"
    yield "      deferredLayoutRefresh = false;"
    yield "      scheduleLayoutRefresh(reason, sig);"
    yield "    }, 1000);"
    yield "    return;"
    yield "  }"
    yield "  deferredLayoutRefresh = false;"
    yield "  try {"
    yield "    const prevSig = sessionStorage.getItem('layoutRefreshSig') || '';"
    yield "    const prevAtRaw = sessionStorage.getItem('layoutRefreshSigAt') || '0';"
    yield "    const prevAt = Number.parseInt(prevAtRaw, 10) || 0;"
    yield "    const nowMs = Date.now();"
    yield "    if (sig && prevSig === sig && (nowMs - prevAt) < 20000) return;"
    yield "    if (sig) sessionStorage.setItem('layoutRefreshSig', sig);"
    yield "    sessionStorage.setItem('layoutRefreshSigAt', String(nowMs));"
    yield "  } catch (_) {}"
    yield "  pendingLayoutRefresh = true;"
    yield "  console.info('[layout-refresh]', reason || 'layout changed');"
    yield "  setTimeout(() => window.location.reload(), 350);"
    yield "}"

    yield "function shouldRefreshForLayoutDrift(available, nextExpMap, renderableSwitches, selectedView) {"
    yield "  for (const sid of (available || [])) {"
    yield "    const expected = _asStringSet(Array.isArray(nextExpMap?.[sid]) ? nextExpMap[sid] : []);"
    yield "    if (!expected.size) continue;"
    yield "    const row = document.getElementById(`row_${sid}`);"
    yield "    if (!row) continue;"
    yield "    const rendered = _asStringSet(_renderedMetricsForSensor(sid));"
    yield "    if (!_sameStringSet(expected, rendered)) return { reason: `metrics:${sid}` };"
    yield "  }"
    yield "  const expectedSwitches = new Set((renderableSwitches || []).map(_normSwitchId).filter(Boolean));"
    yield "  if (expectedSwitches.size) {"
    yield "    const renderedSwitches = new Set(_renderedSwitchIds());"
    yield "    for (const swId of expectedSwitches) {"
    yield "      if (!renderedSwitches.has(swId)) return { reason: `switch:${swId}` };"
    yield "    }"
    yield "  }"
    yield "  return null;"
    yield "}"

    yield "async function updateGauges() {"
    yield "  if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {"
    yield "    return;"
    yield "  }"
    yield "  const sensorIdEl = document.getElementById('sensor_id');"
    yield "  const sensorId = sensorIdEl ? sensorIdEl.value : 'All';"
    yield "  let d = null;"
    #yield "  console.warn('updateGauges: step 1 - get sensor data');"
    
    yield "  try {"
    yield "    const res = await fetch(window.location.pathname + '?json_only=true&sensor_id=' + encodeURIComponent(sensorId));"
    yield "    if (!res.ok) {"
    yield "      console.warn('updateGauges: non-OK response', res.status);"
    yield "      return;"
    yield "    }"
    yield "    d = await res.json();"
    yield "    __lastJsonOnly = d;"
    yield "    __lastJsonOnlyAtMs = Date.now();"
    yield "    if (typeof refreshAndApplySwitchStatus === 'function') {"
    yield "      const nowMs = Date.now();"
    yield "      if (!window.__lastSwitchStatusFromGaugesAt || (nowMs - window.__lastSwitchStatusFromGaugesAt) >= 12000) {"
    yield "        window.__lastSwitchStatusFromGaugesAt = nowMs;"
    yield "        setTimeout(() => refreshAndApplySwitchStatus(), 0);"
    yield "      }"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.warn('updateGauges: fetch failed', e);"
    yield "    return;"
    yield "  }"
    yield ""
    #yield "  console.warn('updateGauges: step 2 - check switch events');"
    
    yield "  const available = Array.isArray(d.available) ? d.available : [];"
    yield "  const nextExpMap = d.expected_gauge_map || {};"
    yield "  const nextStyleMap = d.expected_display_style_map || {};"
    yield "  const renderableSwitches = Array.isArray(d.renderable_switches_view) ? d.renderable_switches_view : (Array.isArray(d.renderable_switches) ? d.renderable_switches : []);"
    yield "  const locations  = d.locations || {};"
    yield "  const layoutDrift = shouldRefreshForLayoutDrift(available, nextExpMap, renderableSwitches, sensorId);"
    yield "  if (layoutDrift) {"
    yield "    const sig = _layoutSignature(available, nextExpMap, renderableSwitches);"
    yield "    const reason = String(layoutDrift.reason || '');"
    yield "    if (reason.startsWith('switch:')) {"
    yield "      const nowMs = Date.now();"
    yield "      if ((nowMs - __lastSuppressedSwitchLayoutAt) > 5000) {"
    yield "        __lastSuppressedSwitchLayoutAt = nowMs;"
    yield "        console.info('[layout-refresh-switch]', reason);"
    yield "      }"
    yield "    }"
    yield "    scheduleLayoutRefresh(layoutDrift.reason, sig);"
    yield "    return;"
    yield "  }"
    yield ""
    yield ""
    #yield "  console.warn('updateGauges: step 3 - sensor available loop');"
    
    yield "  for (const sid of available) {"
    yield "    const metrics = Array.isArray(nextExpMap[sid]) ? nextExpMap[sid] : [];"
    yield "    if (!metrics.length) continue;"
    yield ""
    yield "    if (!knownSensors.has(sid)) {"
    yield "      ensureSensorInSelector(sid);"
    yield "      expectedGaugeMap[sid] = metrics;"
    yield "      expectedDisplayStyleMap[sid] = nextStyleMap[sid] || expectedDisplayStyleMap[sid] || {};"
    yield "      ensureSensorUI(sid, metrics, locations[sid]);"
    yield "      try { initGauge(); }"
    yield "      catch (e) { console.error('initGauge() failed for new sensor', sid, e); }"
    yield "      knownSensors.add(sid);"
    yield "      refreshOnceAfterSensorAdded();"
    yield "    } else {"
    yield "      const needsNewUI = metrics.some(m => {"
    yield "        const safeM = (typeof toSafe === 'function') ? toSafe(m) : m.replace(/[^a-zA-Z0-9_\\-]/g,'_');"
    yield "        return !document.getElementById(`${sid}_${safeM}_container`);"
    yield "      });"
    yield "      if (needsNewUI) {"
    yield "        expectedGaugeMap[sid] = metrics;"
    yield "        expectedDisplayStyleMap[sid] = nextStyleMap[sid] || expectedDisplayStyleMap[sid] || {};"
    yield "        ensureSensorUI(sid, metrics, locations[sid]);"
    yield "        try { initGauge(); }"
    yield "        catch (e) { console.error('initGauge() failed while extending metrics', sid, e); }"
    yield "      }"
    yield "      const styleMap = nextStyleMap[sid] || {};"
    yield "      metrics.forEach((metric, idx) => {"
    yield "        const safeM = (typeof toSafe === 'function') ? toSafe(metric) : metric.replace(/[^a-zA-Z0-9_\\-]/g,'_');"
    yield "        const container = document.getElementById(`${sid}_${safeM}_container`);"
    yield "        if (!container) return;"
    yield "        if (container.dataset.userDisplayStyle === '1') return;"
    yield "        const nextStyle = window.normalizeDisplayStyle(styleMap[`METRIC_${idx + 1}`] || container.dataset.displayStyle || window.displayStyle || 'Gauge');"
    yield "        container.dataset.displayStyle = nextStyle;"
    yield "        if (typeof window.registerContainerStyle === 'function') {"
    yield "          window.registerContainerStyle(container, nextStyle);"
    yield "        }"
    yield "      });"
    yield "    }"
    yield "  }"  # close for sid of available
    yield ""
    yield "  const values     = d.values      || {}; "
    yield "  const stats      = d.stats       || {}; "
    yield "  const timestamps = d.timestamps  || {}; "
    yield ""
    yield "  let dataChanged = false;"
    yield "  for (const sid in timestamps) {"
    yield "    const newTs = timestamps[sid];"
    yield "    const oldTs = lastTimestamps[sid];"
    yield "    if (newTs && newTs !== oldTs) {"
    yield "      dataChanged = true;"
    yield "      lastTimestamps[sid] = newTs;"
    yield "    }"
    yield "  }"
    yield "  if (dataChanged) lastSensorTimestampChangeMs = Date.now();"
    yield ""
    #yield "  console.warn('updateGauges: step 4 - check sensor stats');"
    
    yield "  for (const sid in values) {"
    yield "    const vset = values[sid] || {};"
    yield "    const sset = stats[sid]  || {};"
    yield "    for (const metric in vset) {"
    yield "      const safe = `${sid}_${toSafe(metric)}`;"
    yield "      const val  = vset[metric];"
    yield "      const labelEl = document.getElementById(`${safe}_val`);"
    yield "      const g       = window[`${safe}_gauge`];"
    yield "      const metricConfig = gaugeConfig?.[metric] || {};"
    yield "      const renderMode = ((metricConfig.render) || '').toLowerCase();"
    yield "      const valueMetric = metricConfig.value_metric || metric;"
    yield "      const statsMetric = metricConfig.stats_metric || metric;"
    yield "      const valueConfig = gaugeConfig?.[valueMetric] || metricConfig;"
    yield "      if (labelEl) {"
    yield "        if (renderMode === 'compass') {"
    yield "          const displayValue = vset[valueMetric];"
    yield "          labelEl.textContent = formatCurrentValue(displayValue, valueConfig);"
    yield "        } else {"
    yield "          labelEl.textContent = formatCurrentValue(val, metricConfig);"
    yield "        }"
    yield "      }"
    yield "      if (renderMode === 'compass') {"
    yield "        const compassCanvas = window[`${safe}_compass`] || document.getElementById(`${safe}Gauge`);"
    yield "        drawCompassGauge(compassCanvas, val);"
    yield "      } else if (typeof val === 'number') {"
    yield "        if (g && typeof g.set === 'function') {"
    yield "          try { g.set(val); } catch (e) { console.warn('Gauge set() failed', safe, e); }"
    yield "        } else {"
    yield "          drawFallbackGauge(document.getElementById(`${safe}Gauge`), val, metricConfig);"
    yield "        }"
    yield "      }"
    yield ""
    yield "      const stat = sset[statsMetric] || {};"
    yield "      const min = toFixedOrDash(stat.min);"
    yield "      const avg = toFixedOrDash(stat.avg);"
    yield "      const max = toFixedOrDash(stat.max);"
    yield ""
    yield "      let min_ts = (stat.min_ts !== undefined && stat.min_ts !== null) ? String(stat.min_ts) : '--';"
    yield "      let max_ts = (stat.max_ts !== undefined && stat.max_ts !== null) ? String(stat.max_ts) : '--';"
    yield "      if (min_ts !== '--') min_ts = formatIsoForStats(min_ts);"
    yield "      if (max_ts !== '--') max_ts = formatIsoForStats(max_ts);"
    yield ""
    yield "      const blk = document.getElementById(`${safe}_stats`);"
    yield "      if (blk) blk.innerHTML = renderStatsHtml(min, avg, max, min_ts, max_ts);"
    yield "    }"
    yield "  }"
    yield ""
    yield "  const ts = document.getElementById('update_time');"
    yield "  if (ts) {"
    yield "    ts.classList.remove('flash-green', 'flash-red');"
    yield "    const sensorTimestampStale = (Date.now() - lastSensorTimestampChangeMs) >= sensorTimestampStaleMs;"
    yield "    const flashClass = dataChanged ? 'flash-green' : (isPiPlatform && sensorTimestampStale ? 'flash-red' : '');"
    yield "    if (flashClass) {"
    yield "      void ts.offsetWidth;"
    yield "      ts.classList.add(flashClass);"
    yield "      setTimeout(() => ts.classList.remove(flashClass), 1000);"
    yield "    }"
    yield "  }"
    yield "  if (dataChanged && typeof window.refreshAllMicrographs === 'function') {"
    yield "    window.refreshAllMicrographs(false);"
    yield "  }"
    yield ""
    yield "}"
          
    # Call this right after a sensor is fully onboarded and all metrics were rendered:
    yield "function refreshOnceAfterSensorAdded() {"
    yield "  try {"
    # avoid refresh loops if user-triggered refresh already occurred
    yield "    if (!sessionStorage.getItem('didAutoRefreshAfterAdd')) {"
    yield "        sessionStorage.setItem('didAutoRefreshAfterAdd', '1');"
    # small delay so UI updates + logs flush, then reload
    yield "        setTimeout(() => window.location.reload(), 400);"
    yield "    }"
    yield "  } catch (e) {"
    # If storage is disabled, just reload
    yield "    setTimeout(() => window.location.reload(), 400);"
    yield "  }"
    yield "}"
    
    # on load, clear the flag so next add can refresh again.
    yield "window.addEventListener('load', () => {"
    # If the page made it back up, we can safely clear this.
    yield "  sessionStorage.removeItem('didAutoRefreshAfterAdd');"
    yield "});"

    yield "const gaugeZonesBackgroundMicro = {"
    yield "  id: 'gaugeZonesBackgroundMicro',"
    yield "  beforeDraw(chart){"
    yield "    const { ctx, chartArea, scales, options } = chart;"
    yield "    const zones = options?.plugins?.micrographZones;"
    yield "    if (!Array.isArray(zones) || !zones.length) return;"
    yield "    const y = scales?.y || Object.values(scales).find(s => s.type==='linear');"
    yield "    if (!y || !chartArea) return;"
    yield "    const topBound = Math.min(chartArea.top, chartArea.bottom);"
    yield "    const bottomBound = Math.max(chartArea.top, chartArea.bottom);"
    yield "    ctx.save();"
    yield "    zones.forEach(z => {"
    yield "      if (!z) return;"
    yield "      const zMin = Number(z.min);"
    yield "      const zMax = Number(z.max);"
    yield "      const color = z.color || z.strokeStyle;"
    yield "      if (!Number.isFinite(zMin) || !Number.isFinite(zMax) || !color) return;"
    yield "      const rawTop = y.getPixelForValue(zMax);"
    yield "      const rawBottom = y.getPixelForValue(zMin);"
    yield "      const yTop = Math.max(topBound, Math.min(bottomBound, Math.min(rawTop, rawBottom)));"
    yield "      const yBottom = Math.max(topBound, Math.min(bottomBound, Math.max(rawTop, rawBottom)));"
    yield "      if (yBottom <= yTop) return;"
    yield "      ctx.fillStyle = color;"
    yield "      ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBottom - yTop);"
    yield "    });"
    yield "    ctx.restore();"
    yield "  }"
    yield "};"
    yield "if (window.Chart) { try { window.Chart.register(gaugeZonesBackgroundMicro); } catch(e){} }"
  
    yield "window.__micrographDataCache = window.__micrographDataCache || new Map();"
    yield "window.__micrographDataInflight = window.__micrographDataInflight || new Map();"
    yield "window.__lastMicrographRefreshAt = window.__lastMicrographRefreshAt || 0;"
    yield "async function getMicrographJson(requestKey, url, force) {"
    yield "  const now = Date.now();"
    yield "  const cacheTtlMs = 60000;"
    yield "  const cached = window.__micrographDataCache.get(requestKey);"
    yield "  if (!force && cached && (now - cached.at) < cacheTtlMs) {"
    yield "    return cached.data;"
    yield "  }"
    yield "  const existing = window.__micrographDataInflight.get(requestKey);"
    yield "  if (existing) {"
    yield "    return existing;"
    yield "  }"
    yield "  const promise = fetch(url, { cache: 'no-store' }).then(async (resp) => {"
    yield "    if (!resp.ok) {"
    yield "      throw new Error('micrograph_http_' + resp.status);"
    yield "    }"
    yield "    const data = await resp.json();"
    yield "    window.__micrographDataCache.set(requestKey, { at: Date.now(), data });"
    yield "    return data;"
    yield "  }).finally(() => {"
    yield "    window.__micrographDataInflight.delete(requestKey);"
    yield "  });"
    yield "  window.__micrographDataInflight.set(requestKey, promise);"
    yield "  return promise;"
    yield "}"
    yield ""
    yield "async function showMicrographForContainer(container, options = {}) {"
    yield "  if (!container) return;"
    yield "  const forceFetch = !!(options && options.force);"
    yield "  if (!forceFetch && typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {"
    yield "    return;"
    yield "  }"
    yield "  if (typeof updateContainerDisplayStyle === 'function') {"
    yield "    updateContainerDisplayStyle(container);"
    yield "  }"
    yield "  const gauge = container.querySelector('.gauge-container');"
    yield "  const graph = container.querySelector('.graph-container');"
    yield "  const canvas = container.querySelector('.micrograph-canvas');"
    yield "  if (!canvas || !gauge || !graph) { return; }"
    yield ""
    yield "  let token = '';"
    yield "  try {"
    yield "    const sensor = (container.dataset.sensor || '').trim();"
    yield "    const metric = (container.dataset.metric || '').trim();"
    yield "    if (!sensor || !metric) {"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    let style = 'Graph24hr';"
    yield "    if (typeof window.getContainerStyle === 'function') {"
    yield "      style = window.getContainerStyle(container);"
    yield "    }"
    yield ""
    yield "    let range = '24h';"
    yield "    let xTitleText = '24 Hours';"
    yield "    if (style === 'Graph6hr') {"
    yield "      range = '6h';"
    yield "      xTitleText = '6 Hours';"
    yield "    } else if (style === 'Graph24hr') {"
    yield "      range = '24h';"
    yield "      xTitleText = '24 Hours';"
    yield "    } else if (style === 'Gauge') {"
    yield "      graph.style.display = 'none';"
    yield "      gauge.style.display = 'block';"
    yield "      canvas.style.cursor = 'default';"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    graph.style.display = 'block';"
    yield "    gauge.style.display = 'none';"
    yield ""
    yield "    const requestKey = sensor + '::' + metric + '::' + range;"
    yield "    if (canvas.dataset.micrographInflight === requestKey) {"
    yield "      canvas.style.cursor = 'default';"
    yield "      return;"
    yield "    }"
    yield "    token = String(Date.now()) + ':' + String(Math.random());"
    yield "    canvas.dataset.micrographToken = token;"
    yield "    canvas.dataset.micrographInflight = requestKey;"
    yield "    container.dataset.micrographLoading = '1';"
    yield "    canvas.style.cursor = 'wait';"
    yield ""
    yield "    const url = '/graph-data?sensor_id=' + encodeURIComponent(sensor)"
    yield "              + '&metric1=' + encodeURIComponent(metric)"
    yield "              + '&range=' + encodeURIComponent(range);"
    yield "    const jsonData = await getMicrographJson(requestKey, url, forceFetch);"
    yield "    if (canvas.dataset.micrographToken !== token) {"
    yield "      return;"
    yield "    }"
    yield "    const liveStyle = (typeof window.getContainerStyle === 'function') ? window.getContainerStyle(container) : style;"
    yield "    if (liveStyle !== style || liveStyle === 'Gauge') {"
    yield "      if (liveStyle === 'Gauge') {"
    yield "        graph.style.display = 'none';"
    yield "        gauge.style.display = 'block';"
    yield "      }"
    yield "      return;"
    yield "    }"
    yield "    const allSeries = jsonData.series || {};"
    yield "    const entries = Object.entries(allSeries);"
    yield "    if (!entries.length) {"
    yield "      if (typeof window.showToast === 'function' && jsonData && jsonData.no_data) {"
    yield "        window.showToast('No data in selected graph window', 'warn');"
    yield "      }"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    const exactKey = sensor + '::' + metric;"
    yield "    const suffix = '::' + metric;"
    yield "    let chosen = null;"
    yield ""
    yield "    for (const kv of entries) {"
    yield "      const k = kv[0];"
    yield "      const v = kv[1];"
    yield "      if (k === exactKey) { chosen = [k, v]; break; }"
    yield "    }"
    yield ""
    yield "    if (!chosen) {"
    yield "      for (const kv of entries) {"
    yield "        const k = kv[0];"
    yield "        const v = kv[1];"
    yield "        if (k.endsWith(suffix)) { chosen = [k, v]; break; }"
    yield "      }"
    yield "    }"
    yield ""
    yield "    if (!chosen) {"
    yield "      chosen = entries[0];"
    yield "    }"
    yield ""
    yield "    const seriesObj = chosen[1] || {};"
    yield "    const labels = seriesObj.ts || [];"
    yield "    const values = seriesObj.vals || [];"
    yield "    const avgAll = (jsonData && (jsonData.simple_avg || jsonData.rolling_ema)) || {};"
    yield "    const avgObj = (avgAll && avgAll[chosen[0]]) || {};"
    yield "    const avgTs = avgObj.ts || [];"
    yield "    const avgVals = avgObj.vals || [];"
    yield ""
    yield "    if (!values.length) {"
    yield "      return;"
    yield "    }"
    yield "    if (canvas.dataset.micrographToken !== token) {"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    const metricName = container.dataset.metric || metric;"
    yield "    const metricNorm = String(metricName || '').toLowerCase().replace(/[_-]+/g, ' ');"
    yield "    const isSoilFertilityIndex = metricNorm === 'soil fertility index' || metricNorm.endsWith(' soil fertility index');"
    yield "    const metricConfig = gaugeConfig?.[metricName] || gaugeConfig?.[metric] || (isSoilFertilityIndex ? gaugeConfig?.['Soil Fertility Index'] : null);"
    yield "    const metricUnit = String(metricConfig?.unit || '').trim();"
    yield "    const metricZones = Array.isArray(metricConfig?.zones) ? metricConfig.zones.map(z => ({"
    yield "      min: Number(z?.min),"
    yield "      max: Number(z?.max),"
    yield "      color: z?.strokeStyle || z?.color || ''"
    yield "    })).filter(z => Number.isFinite(z.min) && Number.isFinite(z.max) && z.color) : [];"
    yield "    const isLightMetric = ['Light Intensity', 'Auto Light', 'Visible Light Intensity', 'Estimated PPFD'].includes(metricName);"
    yield "    const graphLineColor = isLightMetric ? '#000000' : '#ffffff';"
    yield ""
    yield "    const formatYAxisTick = (val) => {"
    yield "      const num = Number(val);"
    yield "      if (!Number.isFinite(num)) return val;"
    yield "      return Number(num.toFixed(2)).toString();"
    yield "    };"
    yield ""
    yield "    const yScaleOptions = { title: { display: false }, ticks: { callback: formatYAxisTick } };"
    yield "    if (isSoilFertilityIndex) {"
    yield "      const cfgMin = Number(metricConfig?.min);"
    yield "      const cfgMax = Number(metricConfig?.max);"
    yield "      if (Number.isFinite(cfgMin) && Number.isFinite(cfgMax) && cfgMax > cfgMin) {"
    yield "        yScaleOptions.min = cfgMin;"
    yield "        yScaleOptions.max = cfgMax;"
    yield "      }"
    yield "    }"
    yield ""
    yield "    const chartOptions = {"
    yield "      responsive: false,"
    yield "      animation: false,"
    yield "      plugins: {"
    yield "        legend: { display: false },"
    yield "        tooltip: { enabled: true },"
    yield "        micrographZones: metricZones"
    yield "      },"
    yield "      scales: {"
    yield "        x: {"
    yield "          type: 'time',"
    yield "          time: {"
    yield "            tooltipFormat: 'yyyy/MM/dd HH:mm',"
    yield "            displayFormats: { hour: 'HH:mm' }"
    yield "          },"
    yield "          title: { display: true, text: xTitleText },"
    yield "          ticks: {"
    yield "            display: false,"
    yield "            autoSkip: true,"
    yield "            maxTicksLimit: 6"
    yield "          },"
    yield "          grid: { display: true }"
    yield "        },"
    yield "        y: yScaleOptions"
    yield "      }"
    yield "    };"
    yield ""
    yield "    function _alignAvg(labels, avgTs, avgVals){"
    yield "      if (!labels.length || !avgTs.length || !avgVals.length) return [];"
    yield "      if (labels.length === avgVals.length) return avgVals;"
    yield "      const map = new Map();"
    yield "      for (let i = 0; i < avgTs.length; i++){"
    yield "        map.set(avgTs[i], avgVals[i]);"
    yield "      }"
    yield "      return labels.map(ts => map.has(ts) ? map.get(ts) : null);"
    yield "    }"
    yield ""
    yield "    const alignedAvg = _alignAvg(labels, avgTs, avgVals);"
    yield "    const hasAvg = alignedAvg.length === labels.length && alignedAvg.some(v => v !== null && v !== undefined);"
    yield ""
    yield "    const sparseRadius = (labels.length <= 1) ? 2 : 0;"
    yield "    const datasets = [{"
    yield "      data: values,"
    yield "      borderColor: graphLineColor,"
    yield "      backgroundColor: 'rgba(255,255,255,1)',"
    yield "      order: 1,"
    yield "      pointRadius: sparseRadius,"
    yield "      pointHoverRadius: Math.max(3, sparseRadius),"
    yield "      tension: 0.3"
    yield "    }];"
    yield ""
    yield "    if (hasAvg) {"
    yield "      datasets.push({"
    yield "        data: alignedAvg,"
    yield "        borderColor: graphLineColor,"
    yield "        borderDash: [6, 3],"
    yield "        order: 2,"
    yield "        pointRadius: sparseRadius,"
    yield "        pointHoverRadius: Math.max(3, sparseRadius),"
    yield "        tension: 0.3"
    yield "      });"
    yield "    }"
    yield ""
    yield "    let chart = chartMap.get(canvas);"
    yield "    if (!chart) {"
    yield "      const ctx = canvas.getContext('2d');"
    yield "      chart = new Chart(ctx, {"
    yield "        type: 'line',"
    yield "        data: {"
    yield "          labels: labels,"
    yield "          datasets: datasets"
    yield "        },"
    yield "        options: chartOptions"
    yield "      });"
    yield "      chartMap.set(canvas, chart);"
    yield "    } else {"
    yield "      chart.data.labels = labels;"
    yield "      chart.data.datasets = datasets;"
    yield "      chart.options = chartOptions;"
    yield "      chart.update('none');"
    yield "    }"
    yield ""
    yield "    graph.style.display = 'block';"
    yield "    gauge.style.display = 'none';"
    yield "  } catch (e) {"
    yield "    console.warn('showMicrographForContainer error', e);"
    yield "  } finally {"
    yield "    if (canvas.dataset.micrographToken === token) {"
    yield "      canvas.dataset.micrographInflight = '';"
    yield "      container.dataset.micrographLoading = '0';"
    yield "      canvas.style.cursor = 'default';"
    yield "    }"
    yield "  }"
    yield "}"
               
    yield "const chartMap = new WeakMap();"

    gauge_config = get_gauge_config()
    yield "  const sensorUnits = {"
    for metric, cfg in gauge_config.items():
        unit = cfg.get("unit", "")
        yield f"    '{metric}': '{unit}',"
    yield "  };"

    yield "window.handleMetricContainerClick = function(container) {"
    yield "    const gauge = container.querySelector('.gauge-container');"
    yield "    const graph = container.querySelector('.graph-container');"
    yield "    const canvas = container.querySelector('.micrograph-canvas');"
    yield "    if (!canvas || !gauge || !graph) { return; }"

    yield "    let style = 'Gauge';"
    yield "    if (typeof window.getContainerStyle === 'function') {"
    yield "      style = window.getContainerStyle(container);"
    yield "    }"

    yield "    let nextStyle = 'Graph24hr';"
    yield "    if (style === 'Graph24hr') {"
    yield "      nextStyle = 'Graph6hr';"
    yield "    } else if (style === 'Graph6hr') {"
    yield "      nextStyle = 'Gauge';"
    yield "    } else if (style === 'Gauge') {"
    yield "      nextStyle = 'Graph24hr';"
    yield "    } else {"
    yield "      nextStyle = 'Graph24hr';"
    yield "    }"

    yield "    if (typeof window.registerContainerStyle === 'function') {"
    yield "      window.registerContainerStyle(container, nextStyle);"
    yield "    }"
    yield "    container.dataset.userDisplayStyle = '1';"

    yield "    if (nextStyle === 'Gauge') {"
    yield "      canvas.dataset.micrographToken = 'gauge:' + String(Date.now());"
    yield "      canvas.dataset.micrographInflight = '';"
    yield "      container.dataset.micrographLoading = '0';"
    yield "      canvas.style.cursor = 'default';"
    yield "      graph.style.display = 'none';"
    yield "      gauge.style.display = 'block';"
    yield "      if (typeof initGauge === 'function') {"
    yield "        try { initGauge(); } catch (e) { console.warn('initGauge on view switch failed', e); }"
    yield "      }"
    yield "      return;"
    yield "    }"

    yield "    graph.style.display = 'block';"
    yield "    gauge.style.display = 'none';"
    yield "    showMicrographForContainer(container);"
    yield "};"
    yield ""
    yield "window.bindMetricContainer = function(container) {"
    yield "  if (!container || container.dataset.metricClickBound === '1') return;"
    yield "  container.dataset.metricClickBound = '1';"
    yield "  container.addEventListener('click', () => {"
    yield "    if (window.handleMetricContainerClick) window.handleMetricContainerClick(container);"
    yield "  });"
    yield "};"
    yield ""
    yield "document.querySelectorAll('.metric-container').forEach(container => {"
    yield "  if (window.bindMetricContainer) window.bindMetricContainer(container);"
    yield "});"

    yield "(function() {"
    yield "  const all = document.querySelectorAll('.metric-container');"
    yield "  all.forEach(container => {"
    yield "    const style = (typeof window.getContainerStyle === 'function')"
    yield "      ? window.getContainerStyle(container)"
    yield "      : window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');"
    yield "    if (style === 'Graph6hr' || style === 'Graph24hr') {"
    yield "      if (typeof window.registerContainerStyle === 'function') {"
    yield "        window.registerContainerStyle(container, style);"
    yield "      }"
    yield "      window.__needsInitialMicrographRefresh = true;"
    yield "    }"
    yield "  });"
    yield "})();"
       
    yield "(function() {"
    yield "  let lastRun = 0;"
    yield "  const MIN_INTERVAL_MS = 60000;"
    yield "  const MIN_FORCE_INTERVAL_MS = 5000;"
    yield ""
    yield "  async function refreshAllMicrographs(force = false) {"
    yield "    const now = Date.now();"
    yield "    if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {"
    yield "      return;"
    yield "    }"
    yield "    if (force && window.__lastMicrographRefreshAt && (now - window.__lastMicrographRefreshAt) < MIN_FORCE_INTERVAL_MS) {"
    yield "      return;"
    yield "    }"
    yield "    if (!force && (now - lastRun) < MIN_INTERVAL_MS) {"
    yield "      return;"
    yield "    }"
    yield "    lastRun = now;"
    yield "    window.__lastMicrographRefreshAt = now;"
    yield ""
    yield "    const containers = document.querySelectorAll('.metric-container');"
    yield "    for (const container of containers) {"
    yield "      try {"
    yield "        const gauge = container.querySelector('.gauge-container');"
    yield "        const graph = container.querySelector('.graph-container');"
    yield "        if (!gauge || !graph) continue;"
    yield ""
    yield "        const desiredStyle = (typeof window.getContainerStyle === 'function')"
    yield "          ? window.getContainerStyle(container)"
    yield "          : window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');"
    yield ""
    yield "        if (desiredStyle === 'Graph6hr' || desiredStyle === 'Graph24hr') {"
    yield "          await showMicrographForContainer(container, { force });"
    yield "        }"
    yield "      } catch (e) {"
    yield "        console.warn('[micrograph] refresh error', e);"
    yield "      }"
    yield "    }"
    yield "  }"
    yield ""
    yield "  window.refreshAllMicrographs = refreshAllMicrographs;"
    yield ""
    yield "  if (window.__needsInitialMicrographRefresh) {"
    yield "    window.__needsInitialMicrographRefresh = false;"
    yield "    setTimeout(() => refreshAllMicrographs(true), 250);"
    yield "  }"
    yield ""
    yield "  document.addEventListener('DOMContentLoaded', function() {"
    yield "    try {"
    yield "      const hasGraphContainer = Array.from(document.querySelectorAll('.metric-container')).some((container) => {"
    yield "        const style = (typeof window.getContainerStyle === 'function') ? window.getContainerStyle(container) : 'Gauge';"
    yield "        return style === 'Graph6hr' || style === 'Graph24hr';"
    yield "      });"
    yield "      if (hasGraphContainer) {"
    yield "        setTimeout(() => refreshAllMicrographs(true), 500);"
    yield "      }"
    yield "    } catch (e) {"
    yield "      console.warn('[micrograph] DOMContentLoaded init error', e);"
    yield "    }"
    yield "  });"
    yield "})();"

    yield "function updateContainerDisplayStyle(container) {"
    yield "  if (!container) return;"
    yield ""
    yield "  const sensorId = (container.dataset.sensor || '').trim();"
    yield "  const metric   = (container.dataset.metric || '').trim();"
    yield "  if (!sensorId || !metric) return;"
    yield ""
    yield "  const safeMetric = (typeof toSafe === 'function') ? toSafe(metric) : metric.replace(/[^a-zA-Z0-9_\\-]/g,'_');"
    yield "  const safeBase   = `${sensorId}_${safeMetric}`;"
    yield ""
    yield "  let graph = container.querySelector('.graph-container');"
    yield "  if (!graph) {"
    yield "    const gauge = container.querySelector('.gauge-container');"
    yield "    if (!gauge) return;"
    yield "    graph = document.createElement('div');"
    yield "    graph.className = 'graph-container';"
    yield "    graph.style.display = 'none';"
    yield "    graph.innerHTML = "
    yield "      `<div class='graph-view' id='${safeBase}GraphContainer'>` +"
    yield "        `<canvas class='micrograph-canvas' id='${safeBase}Micrograph' width='220' height='60'></canvas>` +"
    yield "      `</div>`;"
    yield "    gauge.insertAdjacentElement('afterend', graph);"
    yield "  }"
    yield "}"
    yield ""
    yield "window.ensureContainerDisplayStyle = function(container) {"
    yield "  if (!container) return;"
    yield "  const raw = String(container.dataset.displayStyle || window.displayStyle || 'Gauge').toLowerCase();"
    yield "  if (raw === 'graph' || raw === 'graph6hr' || raw === 'graph24hr') {"
    yield "    updateContainerDisplayStyle(container);"
    yield "    if (typeof window.registerContainerStyle === 'function') {"
    yield "      const mapped = (raw === 'graph6hr') ? 'Graph6hr' : (raw === 'graph24hr' ? 'Graph24hr' : 'Graph24hr');"
    yield "      window.registerContainerStyle(container, mapped);"
    yield "    }"
    yield "  }"
    yield "};"
    yield ""
    yield "document.addEventListener('DOMContentLoaded', function() {"
    yield "  try {"
    yield "    const all = document.querySelectorAll('.metric-container');"
    yield "    for (const c of all) {"
    yield "      window.ensureContainerDisplayStyle(c);"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.warn('ensureContainerDisplayStyle DOMContentLoaded error', e);"
    yield "  }"
    yield "});"
    
    yield "window.ensureButtonTooltips = window.ensureButtonTooltips || function(root){"
    yield "  const scope = root && root.querySelectorAll ? root : document;"
    yield "  const nodes = scope.querySelectorAll('button, a.button, [role=\"button\"]');"
    yield "  function inferTitle(el){"
    yield "    const aria = String(el.getAttribute('aria-label') || '').trim();"
    yield "    if (aria) return aria;"
    yield "    const paneTarget = String(el.getAttribute('data-target') || el.getAttribute('data-pane-target') || '').trim();"
    yield "    const text = String(el.textContent || '').replace(/\\s+/g, ' ').trim();"
    yield "    const id = String(el.id || '').trim();"
    yield "    if (paneTarget && text) return `Open ${text}`;"
    yield "    if (id === 'bioPrevMonthBtn') return 'Previous month';"
    yield "    if (id === 'bioNextMonthBtn') return 'Next month';"
    yield "    if (id && /toggle/i.test(id)) return text ? `${text} hidden value` : 'Toggle hidden value';"
    yield "    if (text && /^(?:&times;|×)$/u.test(text)) return 'Close';"
    yield "    if (text && /^(?:←|↖|<)$/u.test(text)) return 'Previous';"
    yield "    if (text && /^(?:→|↘|>)$/u.test(text)) return 'Next';"
    yield "    return text;"
    yield "  }"
    yield "  nodes.forEach(function(el){"
    yield "    if (el.hasAttribute('title')) return;"
    yield "    const title = inferTitle(el);"
    yield "    if (title) el.setAttribute('title', title);"
    yield "  });"
    yield "};"
    yield ""
    yield "window.ModalBusyCursor = window.ModalBusyCursor || (function(){"
    yield "  let depth = 0;"
    yield "  function apply(){"
    yield "    document.documentElement.classList.toggle('modal-busy-cursor', depth > 0);"
    yield "    document.body.classList.toggle('modal-busy-cursor', depth > 0);"
    yield "  }"
    yield "  function begin(){"
    yield "    depth += 1;"
    yield "    apply();"
    yield "  }"
    yield "  function end(){"
    yield "    depth = Math.max(0, depth - 1);"
    yield "    apply();"
    yield "  }"
    yield "  function isBusy(){"
    yield "    return depth > 0;"
    yield "  }"
    yield "  async function untilPaint(){"
    yield "    await new Promise(resolve => requestAnimationFrame(() => resolve()));"
    yield "    await new Promise(resolve => requestAnimationFrame(() => resolve()));"
    yield "  }"
    yield "  return { begin, end, isBusy, untilPaint };"
    yield "})();"
    yield ""

    # --- Sensor Settings Modal opener (uses BackdropModal) ---
    yield "window.editSensorSettings = async function(id) {"
    yield "  window.ModalBusyCursor.begin();"
    yield "  try {"
    yield "    const url = `/edit-sensor?sensor_id=${encodeURIComponent(id)}&embed=1&t=${Date.now()}`;"
    yield "    const res = await fetch(url, { cache: 'no-store' });"
    yield "    const html = await res.text();"
    yield "    if (!res.ok) {"
    yield "      console.error('[editSensorSettings] non-OK', res.status, html.slice(0,200));"
    yield "      alert('Failed to load Sensor Settings');"
    yield "      return;"
    yield "    }"
    yield "    if (!window.BackdropModal) {"
    yield "      console.error('BackdropModal is not defined');"
    yield "      return;"
    yield "    }"
    yield "    if (!html || !html.trim()) {"
    yield "      console.error('[editSensorSettings] empty HTML');"
    yield "      return;"
    yield "    }"
    # remove any existing sensor settings modal/backdrop
    yield "    window.BackdropModal.close('sensorSettingsModal');"
    # mount the new one
    yield "    const modal = window.BackdropModal.openFromHtml(html, 'sensorSettingsModal');"
    yield "    if (modal) {"
    yield "      modal.dataset.sensorId = id;"
    yield "      if (window.initSensorSettingsModal) window.initSensorSettingsModal(modal);"
    yield "      const hasCalibrationPane = !!modal.querySelector('#sensor-calibration-pane, #system-calibration-pane');"
    yield "      if (hasCalibrationPane) {"
    yield "        const TAG_ID = 'system-calibration-js';"
    yield "        let needLoadSystemCalJs = true;"
    yield "        if (window.initSystemCalibrationModal) needLoadSystemCalJs = false;"
    yield "        if (needLoadSystemCalJs) {"
    yield "          const existing = document.getElementById(TAG_ID);"
    yield "          if (existing && existing.parentNode) existing.parentNode.removeChild(existing);"
    yield "          await new Promise((resolve, reject) => {"
    yield "            const s = document.createElement('script');"
    yield "            s.id = TAG_ID;"
    yield "            s.src = '/ui_static/js/system_calibration.js?v=' + Date.now();"
    yield "            s.onload = resolve;"
    yield "            s.onerror = reject;"
    yield "            document.head.appendChild(s);"
    yield "          });"
    yield "        }"
    yield "        if (window.initSystemCalibrationModal) await window.initSystemCalibrationModal(modal);"
    yield "      }"
    yield "      window.ensureButtonTooltips(modal.closest('.modal-backdrop') || modal);"
    yield "      await window.ModalBusyCursor.untilPaint();"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load sensor modal', e);"
    yield "    if (typeof window.showToast === 'function') window.showToast('Failed to load Sensor Settings', 'error');"
    yield "    else alert('Failed to load Sensor Settings');"
    yield "  } finally {"
    yield "    window.ModalBusyCursor.end();"
    yield "  }"
    yield "};"

    # --- System Settings modal opener (embed into dashboard, avoid full-page nav) ---
    yield "window.editSystemSettings = async function() {"
    yield "  window.ModalBusyCursor.begin();"
    yield "  try {"
    yield "    const host = document.querySelector('#modal-host') || document.body;"
    yield "    const old = document.getElementById('system-settings-root');"
    yield "    if (old) {"
    yield "      if (typeof window.openSetupModal === 'function') {"
    yield "        window.openSetupModal();"
    yield "      } else {"
    yield "        const modal = document.getElementById('setupPiModal');"
    yield "        if (modal) modal.style.display = 'block';"
    yield "      }"
    yield "      await window.ModalBusyCursor.untilPaint();"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    const url = `/edit-system?embed=1&t=${Date.now()}`;"
    yield "    const res = await fetch(url, { cache: 'no-store' });"
    yield "    const html = await res.text();"
    yield "    if (!res.ok) {"
    yield "      console.error('[editSystemSettings] non-OK', res.status, html.slice(0,200));"
    yield "      alert('Failed to load System Settings');"
    yield "      return;"
    yield "    }"
    yield "    if (!html || !html.trim()) {"
    yield "      console.error('[editSystemSettings] empty HTML');"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    const root = document.createElement('div');"
    yield "    root.id = 'system-settings-root';"
    yield "    root.innerHTML = html;"
    yield "    host.appendChild(root);"
    yield ""
    # Ensure inline scripts in injected templates execute.
    yield "    const scripts = root.querySelectorAll('script');"
    yield "    scripts.forEach(oldScript => {"
    yield "      const s = document.createElement('script');"
    yield "      for (const a of oldScript.attributes) s.setAttribute(a.name, a.value);"
    yield "      s.textContent = oldScript.textContent || '';"
    yield "      oldScript.parentNode.replaceChild(s, oldScript);"
    yield "    });"
    yield "    window.ensureButtonTooltips(root);"
    yield ""
    yield "    if (typeof window.openSetupModal === 'function') {"
    yield "      window.openSetupModal();"
    yield "    } else {"
    yield "      const modal = document.getElementById('setupPiModal');"
    yield "      if (modal) modal.style.display = 'block';"
    yield "    }"
    yield "    await window.ModalBusyCursor.untilPaint();"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load system modal', e);"
    yield "  } finally {"
    yield "    window.ModalBusyCursor.end();"
    yield "  }"
    yield "};"
    yield ""
    yield "window.closeSystemSettingsModal = function(){"
    yield "  const ids = ["
    yield "    'setupPiModal',"
    yield "    'ha-settings-overlay',"
    yield "    'device-locations-overlay',"
    yield "    'remove-device-overlay',"
    yield "    'onboard-progress-overlay'"
    yield "  ];"
    yield "  ids.forEach(id => {"
    yield "    const el = document.getElementById(id);"
    yield "    if (el) el.style.display = 'none';"
    yield "  });"
    yield "};"
      
    # --- SWITCH HELPERS  ---
    yield "function _safeName(name) { return (name || '').toLowerCase().replaceAll(' ', '_'); }"
    yield "function _realName(s) {"
    yield "  if (typeof s !== 'string') return '';"
    yield "  return s.includes(' ') ? s : s.replaceAll('_',' ');"
    yield "}"

    yield "function _cssEsc(s){try{return CSS&&CSS.escape?CSS.escape(String(s)):String(s);}catch(_){return String(s);}}"
    
    yield "function _splitKey(key){"
    yield "  const k=String(key||'');"
    yield "  if(k.includes('::')){const [sid,...rest]=k.split('::');return {switchId:sid,label:rest.join('::'),channel:rest.join('::')};}"
    yield "  if(k.includes(':')){const [sid,...rest]=k.split(':');return {switchId:sid,label:rest.join(':'),channel:rest.join(':')};}"
    yield "  return {switchId:'',label:k,channel:k};"
    yield "}"
    
    yield "function _stripIsoExtras(ts){"
    yield "  const s = String(ts||'');"
    yield "  const noMicros = s.replace(/\\.\\d{1,6}(?=Z|[+-]\\d{2}:\\d{2}|$)/,'');"
    yield "  const noTz = noMicros.replace(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})(?:Z|[+-]\\d{2}:\\d{2})\\b/, '$1');"
    yield "  return noTz.replace('T',' ');"
    yield "}"

    yield "const _switchEventsCache = new Map();  "
    yield "const _switchRefreshBlockUntil = new Map();"
    yield "const _switchTimerState = new Map();"

    yield "function _timerStateKey(key, fallbackName){"
    yield "  const k = String(key || '').trim();"
    yield "  if (k) return k;"
    yield "  return String(fallbackName || '').trim();"
    yield "}"

    yield "function _findTimerPanel(key){"
    yield "  const k = String(key || '').trim();"
    yield "  if (k) {"
    yield "    const exact = document.querySelector(`.switch-timer-panel[data-switch-ui-key=\"${_cssEsc(k)}\"]`);"
    yield "    if (exact) return exact;"
    yield "  }"
    yield "  return null;"
    yield "}"

    yield "function _setTimerEditorOpen(panel, open){"
    yield "  if (!panel) return;"
    yield "  const editor = panel.querySelector('.switch-timer-editor');"
    yield "  const btn = panel.querySelector('.switch-timer-edit-btn');"
    yield "  const container = panel.closest('.switch-metric-container');"
    yield "  if (editor) editor.style.display = open ? 'flex' : 'none';"
    yield "  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');"
    yield "  panel.classList.toggle('timer-editor-open', !!open);"
    yield "  if (container) {"
    yield "    const anyOpen = !!container.querySelector('.switch-timer-panel.timer-editor-open');"
    yield "    container.classList.toggle('timer-editor-open', anyOpen);"
    yield "  }"
    yield "}"

    yield "function _closeAllTimerEditors(){"
    yield "  document.querySelectorAll('.switch-timer-panel').forEach((panel) => _setTimerEditorOpen(panel, false));"
    yield "}"

    yield "function _renderSwitchTimer(key, fallbackName){"
    yield "  const stateKey = _timerStateKey(key, fallbackName);"
    yield "  const panel = _findTimerPanel(stateKey);"
    yield "  if (!panel) return;"
    yield "  const input = panel.querySelector('.switch-timer-input');"
    yield "  const status = panel.querySelector('.switch-timer-status');"
    yield "  const model = _switchTimerState.get(stateKey) || {};"
    yield "  const seconds = Number(model.timer_seconds || 0);"
    yield "  const deadline = Number(model.timer_deadline_epoch || 0);"
    yield "  const isOn = !!model.state;"
    yield "  if (input && document.activeElement !== input) {"
    yield "    input.value = String(seconds);"
    yield "    input.dataset.lastGoodValue = String(seconds);"
    yield "  }"
    yield "  if (!status) return;"
    yield "  if (seconds <= 0) {"
    yield "    status.textContent = 'Timer disabled';"
    yield "    return;"
    yield "  }"
    yield "  if (!isOn || !deadline) {"
    yield "    status.textContent = `Timer set: ${seconds}s`;"
    yield "    return;"
    yield "  }"
    yield "  const remaining = Math.max(0, Math.ceil(deadline - (Date.now() / 1000)));"
    yield "  status.textContent = remaining > 0 ? `Countdown: ${remaining}s` : 'Turning off...';"
    yield "}"

    yield "function updateSwitchTimerUi(key, stateData, fallbackName){"
    yield "  const stateKey = _timerStateKey((stateData && stateData.ui_key) || key, fallbackName);"
    yield "  const prev = _switchTimerState.get(stateKey) || {};"
    yield "  const next = Object.assign({}, prev);"
    yield "  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'state')) next.state = !!stateData.state;"
    yield "  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_seconds')) next.timer_seconds = Number(stateData.timer_seconds || 0);"
    yield "  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_deadline_epoch')) next.timer_deadline_epoch = stateData.timer_deadline_epoch ? Number(stateData.timer_deadline_epoch) : 0;"
    yield "  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_remaining_s') && !Object.prototype.hasOwnProperty.call(stateData, 'timer_deadline_epoch')) {"
    yield "    const rem = Number(stateData.timer_remaining_s || 0);"
    yield "    next.timer_deadline_epoch = rem > 0 ? ((Date.now() / 1000) + rem) : 0;"
    yield "  }"
    yield "  if (!next.state) next.timer_deadline_epoch = 0;"
    yield "  _switchTimerState.set(stateKey, next);"
    yield "  _renderSwitchTimer(stateKey, fallbackName);"
    yield "}"

    yield "function tickSwitchCountdowns(){"
    yield "  for (const key of _switchTimerState.keys()) {"
    yield "    _renderSwitchTimer(key, '');"
    yield "  }"
    yield "}"

    yield "function initSwitchTimersFromDom(){"
    yield "  document.querySelectorAll('.switch-timer-panel').forEach((panel) => {"
    yield "    const key = panel.dataset.switchUiKey || '';"
    yield "    const input = panel.querySelector('.switch-timer-input');"
    yield "    const initial = Number((input && input.value) || 0);"
    yield "    _switchTimerState.set(key, { timer_seconds: initial, timer_deadline_epoch: 0, timer_remaining_s: 0, state: false });"
    yield "    if (input) input.dataset.lastGoodValue = String(initial);"
    yield "    _setTimerEditorOpen(panel, false);"
    yield "    _renderSwitchTimer(key, panel.dataset.label || '');"
    yield "  });"
    yield "}"
    
    yield "function _findEventsListElem(key){"
    yield "  const {switchId,label}=_splitKey(key);"
    yield "  const norm=(s)=>String(s||'').trim().toLowerCase().replaceAll('_',' ').replace(/\\s+/g,' ').replace(/s$/,'');"
    # 1) exact match on the *UL*
    yield "  let el=document.querySelector(`ul.switch-events-list[data-switch-key=\"${_cssEsc(key)}\"]`);"
    yield "  if(el) return el;"
    # 2) id by switchId+label
    yield "  const id1=_safeName((switchId?switchId+\"_\":\"\")+label)+\"_events_list\";"
    yield "  el=document.getElementById(id1);"
    yield "  if(el && el.tagName==='UL') return el;"
    # 3) legacy id by label only
    yield "  const id2=_safeName(label)+\"_events_list\";"
    yield "  el=document.getElementById(id2);"
    yield "  if(el && el.tagName==='UL') return el;"
    # 4) defensive row walk
    yield "  if(el && el.tagName!=='UL'){"
    yield "    const row=el.closest('tr');"
    yield "    const ul=row?row.querySelector('ul.switch-events-list'):null;"
    yield "    if(ul) return ul;"
    yield "  }"
    # 5) FINAL fallback: scan all ULs; match label loosely only when unique
    yield "  const want=norm(label);"
    yield "  const matches=[];"
    yield "  for(const ul of document.querySelectorAll('ul.switch-events-list')){"
    yield "    const k=ul.getAttribute('data-switch-key')||'';"
    yield "    const {label:lab2}=_splitKey(k);"
    yield "    if(norm(lab2)===want) matches.push(ul);"
    yield "  }"
    yield "  if(matches.length===1) return matches[0];"
    yield "  return null;"
    yield "}"

    yield "window.showSwitchSettingsModal = function(html){"
    # Parse incoming HTML and mount a fresh #switchSettingsModal
    yield "  const wrapper = document.createElement('div');"
    yield "  wrapper.innerHTML = html;"
    yield "  const modal = wrapper.querySelector('#switchSettingsModal') || wrapper.firstElementChild;"
    yield "  if (!modal) return;"
    yield "  const existing = document.getElementById('switchSettingsModal');"
    yield "  if (existing) existing.remove();"
    yield "  document.body.appendChild(modal);"
    yield "  if (window.SwitchModal && typeof window.SwitchModal.mount === 'function') {"
    yield "    window.SwitchModal.mount(modal);"
    yield "  }"
    yield "  window.SwitchModal && window.SwitchModal.open();"
    yield "};"

    yield "if (typeof window.showToast !== 'function') {"
    yield "  window.showToast = function(text, type){"
    yield "    let c = document.querySelector('.toast-container');"
    yield "    if (!c){ c = document.createElement('div'); c.className='toast-container'; document.body.appendChild(c); }"
    yield "    const t = document.createElement('div'); t.className = `toast ${type||''}`; t.textContent = text||'';"
    yield "    c.appendChild(t); setTimeout(()=>{ t.remove(); if(!c.children.length) c.remove(); }, 2500);"
    yield "  };"
    yield "}"
    yield "window.closeSensorOrderMenus = function(){"
    yield "  document.querySelectorAll('.sensor-order-wrap.open').forEach((el) => {"
    yield "    el.classList.remove('open');"
    yield "    const btn = el.querySelector('.sensor-order-btn');"
    yield "    if (btn) btn.setAttribute('aria-expanded', 'false');"
    yield "  });"
    yield "};"
    yield "window.applySensorGroupOrder = function(order){"
    yield "  if (!Array.isArray(order) || !order.length) return;"
    yield "  const dashboard = document.querySelector('.dashboard');"
    yield "  if (!dashboard) return;"
    yield "  const groups = new Map();"
    yield "  dashboard.querySelectorAll('.sensor-group[data-sensor-id]').forEach((el) => {"
    yield "    groups.set(el.getAttribute('data-sensor-id') || '', el);"
    yield "  });"
    yield "  order.forEach((sid) => {"
    yield "    const el = groups.get(String(sid || ''));"
    yield "    if (el) dashboard.appendChild(el);"
    yield "  });"
    yield "  const switchGroup = document.getElementById('group_switches');"
    yield "  if (switchGroup) dashboard.appendChild(switchGroup);"
    yield "};"
    yield "window.reorderSensorGroup = async function(sensorId, direction){"
    yield "  const sid = String(sensorId || '').trim();"
    yield "  const move = String(direction || '').trim().toLowerCase();"
    yield "  if (!sid || !['up','down'].includes(move)) return;"
    yield "  const group = document.querySelector(`.sensor-group[data-sensor-id=\"${CSS.escape(sid)}\"]`);"
    yield "  if (group) group.classList.add('moving');"
    yield "  try {"
    yield "    const res = await fetch('/dashboard/metric-position', {"
    yield "      method: 'POST',"
    yield "      headers: { 'Content-Type': 'application/json' },"
    yield "      body: JSON.stringify({ sensor_id: sid, direction: move })"
    yield "    });"
    yield "    const js = await res.json().catch(() => ({}));"
    yield "    if (!res.ok) throw new Error(String(js.error || js.detail || ('HTTP ' + res.status)));"
    yield "    if (Array.isArray(js.order)) window.applySensorGroupOrder(js.order);"
    yield "    if (js.moved) {"
    yield "      if (group) group.scrollIntoView({ block: 'nearest', behavior: 'smooth' });"
    yield "    } else if (typeof window.showToast === 'function') {"
    yield "      window.showToast(move === 'up' ? 'Row is already at the top' : 'Row is already at the bottom');"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to reorder sensor group', e);"
    yield "    if (typeof window.showToast === 'function') window.showToast('Failed to save dashboard order', 'error');"
    yield "  } finally {"
    yield "    if (group) group.classList.remove('moving');"
    yield "    window.closeSensorOrderMenus();"
    yield "  }"
    yield "};"
    yield "if (!window.__sensorOrderInit) {"
    yield "  window.__sensorOrderInit = true;"
    yield "  document.addEventListener('click', function(ev){"
    yield "    const menuBtn = ev.target.closest('.sensor-order-btn');"
    yield "    if (menuBtn) {"
    yield "      ev.preventDefault();"
    yield "      ev.stopPropagation();"
    yield "      const wrap = menuBtn.closest('.sensor-order-wrap');"
    yield "      const willOpen = !!wrap && !wrap.classList.contains('open');"
    yield "      window.closeSensorOrderMenus();"
    yield "      if (wrap && willOpen) {"
    yield "        wrap.classList.add('open');"
    yield "        menuBtn.setAttribute('aria-expanded', 'true');"
    yield "      }"
    yield "      return;"
    yield "    }"
    yield "    const moveBtn = ev.target.closest('.sensor-order-item');"
    yield "    if (moveBtn) {"
    yield "      ev.preventDefault();"
    yield "      ev.stopPropagation();"
    yield "      window.reorderSensorGroup(moveBtn.getAttribute('data-sensor-id'), moveBtn.getAttribute('data-move'));"
    yield "      return;"
    yield "    }"
    yield "    if (!ev.target.closest('.sensor-order-wrap')) window.closeSensorOrderMenus();"
    yield "  });"
    yield "}"
    

    # --- Switch Settings modal section switching + lazy automation init ---
    yield "window.initSwitchSettingsModal = function(modalEl){"
    yield "  const modal = modalEl || document.getElementById('switchSettingsModal');"
    yield "  if (!modal) return;"
    yield "  const btnSettings = modal.querySelector('#switchMenuSettings');"
    yield "  const btnAutos = modal.querySelector('#switchMenuAutomations');"
    yield "  const btnBacks = modal.querySelectorAll('[data-switch-pane-target=\"settings\"]');"
    yield "  const paneSettings = modal.querySelector('#switchSettingsPane');"
    yield "  const paneAutos = modal.querySelector('#switchAutomationsPane');"
    yield "  const form = modal.querySelector('form[action=\"/submit-switch-settings\"]');"
    yield "  const saveBtn = modal.querySelector('#switchSettingsSaveBtn');"
    yield "  const statusEl = modal.querySelector('#switchSettingsStatus');"
    yield "  const restartBtn = modal.querySelector('#switchRestartBtn');"
    yield "  if (!btnSettings || !btnAutos || !paneSettings || !paneAutos) return;"
    yield ""
    yield "  function activate(which){"
    yield "    const showSettings = (which === 'settings');"
    yield "    paneSettings.hidden = !showSettings;"
    yield "    paneAutos.hidden = showSettings;"
    yield "    btnSettings.classList.toggle('active', showSettings);"
    yield "    btnAutos.classList.toggle('active', !showSettings);"
    yield "    btnSettings.setAttribute('aria-selected', showSettings ? 'true' : 'false');"
    yield "    btnAutos.setAttribute('aria-selected', showSettings ? 'false' : 'true');"
    yield "  }"
    yield ""
    yield "  btnSettings.onclick = function(){"
    yield "    activate('settings');"
    yield "  };"
    yield ""
    yield "  btnBacks.forEach(function(btn){"
    yield "    btn.onclick = function(){"
    yield "      activate('settings');"
    yield "    };"
    yield "  });"
    yield ""
    yield "  btnAutos.onclick = async function(){"
    yield "    activate('automations');"
    yield "    if (typeof window.initAdvancedAutomationModal !== 'function') {"
    yield "      console.error('initAdvancedAutomationModal is not available');"
    yield "      return;"
    yield "    }"
    yield "    if (modal.dataset.automationInit !== '1') {"
    yield "      const ok = await window.initAdvancedAutomationModal(modal);"
    yield "      if (ok) {"
    yield "        modal.dataset.automationInit = '1';"
    yield "        if (typeof window.refreshAdvancedAutomationModal === 'function') {"
    yield "          setTimeout(function(){"
    yield "            window.refreshAdvancedAutomationModal(modal).catch(function(){});"
    yield "          }, 250);"
    yield "        }"
    yield "      }"
    yield "      return;"
    yield "    }"
    yield "    if (typeof window.refreshAdvancedAutomationModal === 'function') {"
    yield "      await window.refreshAdvancedAutomationModal(modal);"
    yield "    }"
    yield "  };"
    yield ""
    yield "  if (form && saveBtn && statusEl && form.dataset.ajaxBound !== '1') {"
    yield "    form.dataset.ajaxBound = '1';"
    yield "    function setBusy(isBusy){"
    yield "      saveBtn.disabled = !!isBusy;"
    yield "      if (restartBtn && restartBtn.dataset.pending !== '1') restartBtn.disabled = !!isBusy;"
    yield "    }"
    yield "    function setStatus(text){"
    yield "      statusEl.textContent = text || '';"
    yield "    }"
    yield "    function setRestartPending(isPending){"
    yield "      if (!restartBtn) return;"
    yield "      if (!restartBtn.dataset.baseLabel) restartBtn.dataset.baseLabel = (restartBtn.textContent || 'Restart Device').trim();"
    yield "      restartBtn.dataset.pending = isPending ? '1' : '0';"
    yield "      restartBtn.disabled = !!isPending;"
    yield "      restartBtn.textContent = isPending ? 'Device Restarting...' : (restartBtn.dataset.baseLabel || 'Restart Device');"
    yield "    }"
    yield "    async function parseResponseError(resp){"
    yield "      const contentType = String(resp.headers.get('content-type') || '').toLowerCase();"
    yield "      if (contentType.includes('application/json')) {"
    yield "        const js = await resp.json().catch(() => ({}));"
    yield "        return String(js.error || js.message || ('HTTP ' + resp.status));"
    yield "      }"
    yield "      return String((await resp.text().catch(() => '')) || ('HTTP ' + resp.status)).trim();"
    yield "    }"
    yield "    form.addEventListener('submit', async function(ev){"
    yield "      ev.preventDefault();"
    yield "      setBusy(true);"
    yield "      setStatus('Saving...');"
    yield "      try {"
    yield "        const body = new URLSearchParams(new FormData(form));"
    yield "        const resp = await fetch(form.action, {"
    yield "          method: 'POST',"
    yield "          headers: {"
    yield "            'Accept': 'application/json',"
    yield "            'X-Requested-With': 'XMLHttpRequest',"
    yield "            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',"
    yield "          },"
    yield "          body: body.toString(),"
    yield "        });"
    yield "        const js = await resp.json().catch(() => ({}));"
    yield "        if (!resp.ok || js.ok === false) throw new Error(String(js.error || js.message || ('HTTP ' + resp.status)));"
    yield "        setStatus('Saved.');"
    yield "        if (typeof window.showToast === 'function') window.showToast('Switch settings saved', 'ok');"
    yield "      } catch (err) {"
    yield "        const msg = err && err.message ? err.message : 'Failed to save switch settings.';"
    yield "        setStatus(msg);"
    yield "        if (typeof window.showToast === 'function') window.showToast('Failed to save switch settings', 'error');"
    yield "      } finally {"
    yield "        setBusy(false);"
    yield "      }"
    yield "    });"
    yield "    if (restartBtn) {"
    yield "      setRestartPending(false);"
    yield "      restartBtn.addEventListener('click', async function(){"
    yield "        const switchIdInput = form.querySelector('input[name=\"switch_id\"]');"
    yield "        const switchId = (switchIdInput && switchIdInput.value) ? String(switchIdInput.value) : String(modal.dataset.switchId || '');"
    yield "        if (!switchId) {"
    yield "          setStatus('Missing switch id.');"
    yield "          return;"
    yield "        }"
    yield "        if (!window.confirm('Restart this device now? Unsaved changes in this modal will not be applied.')) return;"
    yield "        setBusy(true);"
    yield "        setRestartPending(true);"
    yield "        setStatus('Device Restarting...');"
    yield "        try {"
    yield "          const body = new URLSearchParams();"
    yield "          body.set('switch_id', switchId);"
    yield "          const resp = await fetch(restartBtn.dataset.restartUrl || '/switch-settings/restart-device', {"
    yield "            method: 'POST',"
    yield "            headers: {"
    yield "              'Accept': 'application/json',"
    yield "              'X-Requested-With': 'XMLHttpRequest',"
    yield "              'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',"
    yield "            },"
    yield "            body: body.toString(),"
    yield "          });"
    yield "          if (!resp.ok) throw new Error(await parseResponseError(resp));"
    yield "          const js = await resp.json().catch(() => ({}));"
    yield "          const message = String(js.message || 'Device restarting...');"
    yield "          setStatus(message);"
    yield "          if (typeof window.showToast === 'function') window.showToast(message, 'ok');"
    yield "        } catch (err) {"
    yield "          const msg = err && err.message ? err.message : 'Failed to restart device.';"
    yield "          setRestartPending(false);"
    yield "          setStatus(msg);"
    yield "          if (typeof window.showToast === 'function') window.showToast('Failed to restart device', 'error');"
    yield "        } finally {"
    yield "          setBusy(false);"
    yield "        }"
    yield "      });"
    yield "    }"
    yield "  }"
    yield ""
    yield "  activate('settings');"
    yield "};"

    # --- Switch Settings Modal opener (uses BackdropModal, preserves old semantics) ---
    yield "window.editSwitchSettings = async function(id) {"
    yield "  window.ModalBusyCursor.begin();"
    yield "  try {"
    yield "    const url = `/edit-switch?switch_id=${encodeURIComponent(id)}&embed=1&t=${Date.now()}`;"
    yield "    const res = await fetch(url, { cache: 'no-store' });"
    yield "    const html = await res.text();"
    yield "    if (!res.ok) {"
    yield "      console.error('[editSwitchSettings] non-OK', res.status, html.slice(0,200));"
    yield "      alert('Failed to load Switch Settings');"
    yield "      return;"
    yield "    }"
    yield "    if (!window.BackdropModal) {"
    yield "      console.error('BackdropModal is not defined');"
    yield "      return;"
    yield "    }"
    yield "    if (!html || !html.trim()) {"
    yield "      console.error('[editSwitchSettings] empty HTML');"
    yield "      return;"
    yield "    }"
    # --- remove any existing switch modal/backdrop (old behavior preserved) ---
    yield "    window.BackdropModal.close('switchSettingsModal');"
    # --- mount the new one ---
    yield "    const modal = window.BackdropModal.openFromHtml(html, 'switchSettingsModal');"
    yield "    if (modal) {"
    yield "      modal.dataset.switchId = id;"
    yield "      modal.dataset.automationInit = '0';"
    yield "      if (typeof window.initSwitchSettingsModal === 'function') window.initSwitchSettingsModal(modal);"
    yield "      window.ensureButtonTooltips(modal.closest('.modal-backdrop') || modal);"
    yield "      await window.ModalBusyCursor.untilPaint();"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load switch modal', e);"
    yield "  } finally {"
    yield "    window.ModalBusyCursor.end();"
    yield "  }"
    yield "};"
    
    # --- Global helper to close Switch Settings modal (removes backdrop) ---
    yield "window.closeSwitchSettingsModal = function() {"
    yield "  const modal = document.getElementById('switchSettingsModal');"
    yield "  if (!modal) return;"
    yield "  const backdrop = modal.closest('.modal-backdrop');"
    yield "  if (backdrop && backdrop.parentNode) {"
    yield "    backdrop.parentNode.removeChild(backdrop);"
    yield "  } else if (modal.parentNode) {"
    yield "    modal.parentNode.removeChild(modal);"
    yield "  }"
    yield "};"

    # --- Unified BackdropModal helper (all modals use this) ---
    yield "window.BackdropModal = window.BackdropModal || (function(){"
    yield "  function ensureHost(){"
    yield "    let host = document.querySelector('#modal-host');"
    yield "    if (!host) {"
    yield "      host = document.createElement('div');"
    yield "      host.id = 'modal-host';"
    yield "      document.body.appendChild(host);"
    yield "    }"
    yield "    return host;"
    yield "  }"
    yield "  function close(modalId){"
    yield "    if (!modalId) return;"
    yield "    const modal = document.getElementById(modalId);"
    yield "    if (!modal) return;"
    yield "    const backdrop = modal.closest('.modal-backdrop');"
    yield "    if (backdrop && backdrop.parentNode) {"
    yield "      backdrop.parentNode.removeChild(backdrop);"
    yield "    } else if (modal.parentNode) {"
    yield "      modal.parentNode.removeChild(modal);"
    yield "    }"
    yield "  }"
    yield "  function openFromHtml(html, modalId){"
    yield "    if (!html || !html.trim()) {"
    yield "      console.error('BackdropModal.openFromHtml: empty HTML');"
    yield "      return null;"
    yield "    }"
    yield "    const tmp = document.createElement('div');"
    yield "    tmp.innerHTML = html.trim();"
    yield "    const backdrop = tmp.querySelector('.modal-backdrop');"
    yield "    const modal = tmp.querySelector('#' + modalId);"
    yield "    if (!modal) {"
    yield "      console.error('BackdropModal.openFromHtml: modalId not found in HTML', modalId);"
    yield "      return null;"
    yield "    }"
    yield "    const mount = ensureHost();"
    yield "    if (backdrop) {"
    yield "      mount.appendChild(backdrop);"
    yield "      backdrop.style.display = 'flex';"
    yield "      if (window.ensureButtonTooltips) window.ensureButtonTooltips(backdrop);"
    yield "    } else {"
    yield "      mount.appendChild(modal);"
    yield "      modal.style.display = 'block';"
    yield "      if (window.ensureButtonTooltips) window.ensureButtonTooltips(modal);"
    yield "    }"
    yield "    return modal;"
    yield "  }"
    yield "  return { openFromHtml, close };"
    yield "})();"
    
    # --- Global ESC + backdrop click close behavior ---
    yield "document.addEventListener('keydown', function(ev){"
    yield "  if (ev.key !== 'Escape') return;"
    yield "  const backdrops = document.querySelectorAll('.modal-backdrop');"
    yield "  if (!backdrops.length) return;"
    yield "  const last = backdrops[backdrops.length - 1];"
    yield "  const modal = last.querySelector('.modal');"
    yield "  const id = modal ? modal.id : null;"
    yield "  if (id && window.BackdropModal) window.BackdropModal.close(id);"
    yield "});"
    yield "document.addEventListener('click', function(ev){"
    yield "  const backdrop = ev.target.closest('.modal-backdrop');"
    yield "  if (!backdrop) return;"
    yield "  if (ev.target !== backdrop) return;"  # only click on the dim background
    yield "  const modal = backdrop.querySelector('.modal');"
    yield "  const id = modal ? modal.id : null;"
    yield "  if (id && window.BackdropModal) window.BackdropModal.close(id);"
    yield "});"    
    
    # --- Named close helpers (used by buttons in templates) ---
    yield "window.closeSwitchSettingsModal = function(){"
    yield "  if (window.BackdropModal) window.BackdropModal.close('switchSettingsModal');"
    yield "};"
    yield "window.closeSensorSettingsModal = function(){"
    yield "  if (window.BackdropModal) window.BackdropModal.close('sensorSettingsModal');"
    yield "};"
    yield "window.closeSystemCalibrationModal = function(){"
    yield "  if (window.BackdropModal) window.BackdropModal.close('systemCalibrationModal');"
    yield "};"

    # --- System Calibration modal opener (shared) ---
    yield "window.openSystemCalibrationModal = async function(sensorId) {"
    yield "  try {"
    yield "    const params = new URLSearchParams();"
    yield "    const sid = (sensorId && String(sensorId).trim()) || '';"
    yield "    if (sid) params.set('sensor_id', sid);"
    yield "    const url = `/ui/modal/system-calibration?${params.toString()}`;"
    yield "    const resp = await fetch(url, { cache: 'no-store' });"
    yield "    const html = await resp.text();"
    yield "    if (!resp.ok) {"
    yield "      console.error('[SystemCal] non-OK', resp.status, html.slice(0,200));"
    yield "      alert('Failed to load System Calibration');"
    yield "      return;"
    yield "    }"
    yield "    if (!window.BackdropModal) {"
    yield "      console.error('BackdropModal not defined');"
    yield "      return;"
    yield "    }"
    yield "    const modal = window.BackdropModal.openFromHtml(html, 'systemCalibrationModal');"
    yield "    if (!modal) {"
    yield "      alert('Unable to open System Calibration modal');"
    yield "      return;"
    yield "    }"
    # reload system_calibration.js every time
    yield "    const TAG_ID = 'system-calibration-js';"
    yield "    const existing = document.getElementById(TAG_ID);"
    yield "    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);"
    yield "    await new Promise((resolve, reject) => {"
    yield "      const s = document.createElement('script');"
    yield "      s.id = TAG_ID;"
    yield "      s.src = '/ui_static/js/system_calibration.js?v=' + Date.now();"
    yield "      s.onload = resolve;"
    yield "      s.onerror = reject;"
    yield "      document.head.appendChild(s);"
    yield "    });"
    yield "    const modalInDom = document.querySelector('#systemCalibrationModal');"
    yield "    if (!modalInDom) {"
    yield "      console.error('[SystemCal] modal not found after mount');"
    yield "      alert('Unable to open System Calibration modal');"
    yield "      return;"
    yield "    }"
    yield "    if (window.initSystemCalibrationModal) {"
    yield "      const ok = await window.initSystemCalibrationModal(modalInDom);"
    yield "      if (!ok) {"
    yield "        console.error('[SystemCal] initSystemCalibrationModal returned false');"
    yield "        alert('Unable to open System Calibration modal');"
    yield "      }"
    yield "    } else {"
    yield "      console.error('[SystemCal] initSystemCalibrationModal is not defined');"
    yield "      alert('Unable to open System Calibration modal');"
    yield "    }"
    yield "  } catch (err) {"
    yield "    console.error('[SystemCal] error', err);"
    yield "    alert('Error opening System Calibration modal');"
    yield "  }"
    yield "};"

    # when a keyed update is requested, never fall back to name-only.
    yield "function _selectSwitchElements(name, key) {"
    yield "  const real = _realName(name);"
    yield "  const safe = _safeName(real);"
    yield "  let box = null, labelEl = null, timeEl = null;"
    yield "  if (key) {"
    yield "    box     = document.querySelector(`.switch-box[data-switch-key='${key}'], button.button[data-switch-key='${key}']`);"
    yield "    labelEl = document.querySelector(`.switch-state[data-switch-key='${key}']`)"
    yield "          || document.querySelector(`.switch-state[data-switch-name='${real}'][data-switch-key='${key}']`);"
    yield "    timeEl  = document.querySelector(`.switch-time[data-switch-key='${key}']`)"
    yield "          || document.querySelector(`.switch-time[data-switch-name='${real}'][data-switch-key='${key}']`);"
    # if a key was provided but no keyed element exists, return nulls to avoid cross-device overwrite
    yield "    if (!box) { return { real, safe, box:null, labelEl:null, timeEl:null }; }"
    yield "  }"
    yield "  if (!box) {"
    # No key path → fall back by name or id (legacy single-device pages)
    yield "    box     = document.querySelector(`.switch-box[data-switch-name='${real}'], button.button[data-switch-name='${real}']`) "
    yield "           || document.getElementById(`${safe}_btn`) "
    yield "           || document.getElementById(`${safe}_box`);"
    yield "    labelEl = document.querySelector(`.switch-state[data-switch-name='${real}']`) || document.getElementById(`${safe}_state`);"
    yield "    timeEl  = document.querySelector(`.switch-time[data-switch-name='${real}']`)  || document.getElementById(`${safe}_time`);"
    yield "  }"
    yield "  if (!labelEl) {"
    yield "    const pref = (key ? key.replaceAll('::','_') + '_' : '') + safe;"
    yield "    labelEl = document.getElementById(`${pref}_state`) || document.getElementById(`${safe}_state`);"
    yield "  }"
    yield "  if (!timeEl) {"
    yield "    const pref = (key ? key.replaceAll('::','_') + '_' : '') + safe;"
    yield "    timeEl  = document.getElementById(`${pref}_time`)  || document.getElementById(`${safe}_time`);"
    yield "  }"
    yield "  return { real, safe, box, labelEl, timeEl };"
    yield "}"

    yield "function updateSwitchVisuals(name, stateData, key) {"
    yield "  const { safe, box, labelEl, timeEl } = _selectSwitchElements(name, key);"
    yield "  try {"
    yield "    console.debug('[switch-ui] updateSwitchVisuals', {"
    yield "      name,"
    yield "      key,"
    yield "      safe,"
    yield "      incomingState: stateData && stateData.state,"
    yield "      boxFound: !!box,"
    yield "      boxKey: box && box.dataset ? box.dataset.switchKey : '',"
    yield "      boxName: box && box.dataset ? box.dataset.switchName : '',"
    yield "      boxText: box ? (box.textContent || '').trim() : '',"
    yield "      labelFound: !!labelEl,"
    yield "      labelText: labelEl ? (labelEl.textContent || '').trim() : '',"
    yield "    });"
    yield "  } catch (_) {}"
    yield "  if (!box) { return; }"
    yield "  const isOn = !!(stateData && (stateData.state===true || String(stateData.state).toLowerCase()==='on'));"
    yield "  const lastTime = stateData && stateData.time ? stateData.time : '';"
    yield "  setSwitchBoxState(box, isOn);"
    yield "  try { console.debug('[switch-ui] setSwitchBoxState', { key, name, isOn, boxKey: box.dataset ? box.dataset.switchKey : '', boxText: (box.textContent || '').trim() }); } catch (_) {}"
    yield "  updateSwitchTimerUi(key, Object.assign({}, stateData || {}, { state: isOn }), name);"
    yield "  if (labelEl) {"
    yield "    labelEl.textContent = isOn ? ' ON' : 'OFF';"
    yield "    labelEl.style.color = isOn ? '#080' : '#666';"
    yield "    labelEl.style.fontWeight = 'bold';"
    yield "  }"
    yield "  try {"
    yield "    console.debug('[switch-ui] updated', {"
    yield "      key,"
    yield "      name,"
    yield "      isOn,"
    yield "      boxText: box ? (box.textContent || '').trim() : '',"
    yield "      labelText: labelEl ? (labelEl.textContent || '').trim() : '',"
    yield "    });"
    yield "  } catch (_) {}"
    yield "}"

    # --- WebSocket live switch updates ---
    yield "let switchWS;"
    yield "let __switchInventoryRefreshAt = 0;"
    yield "function startSwitchWS(){"
    yield "  const proto = (location.protocol === 'https:') ? 'wss' : 'ws';"
    yield "  const url   = `${proto}://${location.host}/ws/switch-updates`;"
    yield "  try{"
    yield "    switchWS = new WebSocket(url);"
    yield "    switchWS.onopen = () => console.debug('Switch WS connected');"
    yield "    switchWS.onmessage = (ev) => {"
    yield "      try{"
    yield "        const msg = JSON.parse(ev.data);"
    yield "        if (msg.type === 'switch_event'){"
    yield "          const key = msg.key || '';"
    yield "          const uiKey = msg.ui_key || key;"
    yield "          const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key], button[data-switch-key]');"
    yield "          const label = uiKey.includes('::') ? uiKey.split('::')[1] : (key.includes('::') ? key.split('::')[1] : key);"
    yield "          const data  = { state: !!msg.state, time: [], timer_seconds: msg.timer_seconds, timer_deadline_epoch: msg.timer_deadline_epoch, timer_remaining_s: msg.timer_remaining_s, ui_key: uiKey };"
    yield "          updateSwitchVisuals(label, data, uiKey);"
    yield "          if (typeof appendSwitchEventLine === 'function'){"
    yield "            const srcRaw = String(msg.source || '').trim();"
    yield "            const src = srcRaw.toLowerCase();"
    yield "            let origin = '';"
    yield "            if (src.startsWith('mqtt-auto:')) {"
    yield "              const detail = (srcRaw.split(':', 2)[1] || '').trim();"
    yield "              origin = detail ? `auto - ${detail}` : 'auto';"
    yield "            } else if (src.startsWith('mqtt-manual')) {"
    yield "              origin = 'manual';"
    yield "            } else if (src.startsWith('auto/rule:')) {"
    yield "              let detail = srcRaw.split(':', 2)[1] || '';"
    yield "              if (detail.toLowerCase().endsWith('/mqtt')) detail = detail.slice(0, -5).trim();"
    yield "              origin = detail ? `auto - ${detail}` : 'auto';"
    yield "            } else if (src.includes('manual') || src.includes('/ui') || src.includes('ui/')) {"
    yield "              origin = 'manual';"
    yield "            } else if (src.includes('auto') || src.includes('rule') || src.includes('timer') || src.includes('automation') || src.includes('schedule')) {"
    yield "              origin = 'auto';"
    yield "            }"
    yield "            const line = `${msg.state ? 'On' : 'Off'} ${msg.timestamp || ''}${origin ? ` (${origin})` : ''}`;"
    yield "            appendSwitchEventLine(uiKey, line);"
    yield "          }"
    yield "        } else if (msg.type === 'automation_toggle'){"
    yield "          const swId  = msg.switch_id || '';"
    yield "          const label = msg.label || '';"
    yield "          applyAutomationStateToSwitch(swId, label, !!msg.enabled);"
    yield "        } else if (msg.type === 'switch_inventory_changed'){"
    yield "          const now = Date.now();"
    yield "          if ((now - __switchInventoryRefreshAt) > 1500) {"
    yield "            __switchInventoryRefreshAt = now;"
    yield "            if (typeof updateGauges === 'function') {"
    yield "              setTimeout(() => updateGauges(), 120);"
    yield "            }"
    yield "          }"
    yield "        }"
    yield "      } catch(e){ console.warn('Bad WS payload', e); }"
    yield "    };"
    yield "    switchWS.onclose = () => {"
    yield "      console.warn('Switch WS closed; retrying...');"
    yield "      setTimeout(startSwitchWS, 2000);"
    yield "    };"
    yield "  } catch(e){"
    yield "    console.warn('WS not available; polling fallback active');"
    yield "  }"
    yield "}"
    yield "document.addEventListener('DOMContentLoaded', startSwitchWS);"
    yield "const _switchStatusRefreshTimers = new Set();"
    yield "function scheduleSwitchStatusRefreshes(delays){"
    yield "  const msList = Array.isArray(delays) ? delays : [0];"
    yield "  for (const raw of msList) {"
    yield "    const delay = Math.max(0, Number(raw) || 0);"
    yield "    const timer = window.setTimeout(async () => {"
    yield "      _switchStatusRefreshTimers.delete(timer);"
    yield "      try { await refreshAndApplySwitchStatus(); } catch (_) { /* silent */ }"
    yield "    }, delay);"
    yield "    _switchStatusRefreshTimers.add(timer);"
    yield "  }"
    yield "}"

    # keep for backup
    yield "async function refreshAndApplySwitchStatus() {"
    yield "  try {"
    yield "    const resp = await fetch('/switch-status-update');"
    yield "    if (!resp.ok) return;"
    yield "    const statusMap = await resp.json();"
    yield "    if (!statusMap) return;"
    yield "    const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key], button[data-switch-key]');"
    yield "    const now = Date.now();"
    yield "    Object.entries(statusMap).forEach(([key, data]) => {"
    yield "      const hasKey = key.includes('::');"
    yield "      if (!hasKey && hasKeyedBoxes) return;"
    yield "      if (hasKey && (_switchRefreshBlockUntil.get(key) || 0) > now) return;"
    yield "      const label = hasKey ? key.split('::')[1] : key;"
    yield "      updateSwitchVisuals(label, data, hasKey ? key : '');"
    yield "    });"
    yield "    updateSwitchEventsFromStatus(statusMap);"
    yield "  } catch (err) { console.error('Error refreshing switch status', err); }"
    yield "}"

    yield "function setSwitchBoxState(box, isOn) {"
    yield "  if (!box) return;"
    yield "  const state = isOn ? 'on' : 'off';"
    yield "  box.dataset.state = state;"
    # normalize label for the new button-style toggles
    yield "  if (box.tagName === 'BUTTON') {"
    yield "    box.textContent = isOn ? 'On' : 'Off';"
    yield "    box.classList.toggle('green', isOn);"
    yield "    box.classList.toggle('black', !isOn);"
    yield "  } else {"
    # legacy fallback (old .switch-box DIVs)
    yield "    box.classList.toggle('on',  isOn);"
    yield "    box.classList.toggle('off', !isOn);"
    yield "    box.style.background = isOn ? 'green' : '#aaa';"
    yield "    box.style.border     = isOn ? '2px solid #080' : '1px solid #666';"
    yield "  }"
    yield "}"

    yield "function applyAutomationStateToSwitch(switchId, label, isEnabled) {"
    yield "  const selector = label"
    yield "    ? `[data-automation-switch-id=\"${switchId}\"][data-automation-label=\"${label}\"]`"
    yield "    : `[data-automation-switch-id=\"${switchId}\"]`;"
    yield "  const nodes = document.querySelectorAll(selector);"
    yield "  nodes.forEach(node => {"
    yield "    node.dataset.automationEnabled = isEnabled ? '1' : '0';"
    yield "    node.classList.toggle('automation-enabled', !!isEnabled);"
    yield "    if (node.tagName === 'BUTTON') {"
    yield "      node.title = isEnabled"
    yield "        ? 'Automation enabled. Disable automation to toggle manually.'"
    yield "        : `Toggle state for ${node.dataset.switchName || label || 'switch'}`;"
    yield "      node.setAttribute('aria-disabled', isEnabled ? 'true' : 'false');"
    yield "    }"
    yield "  });"
    yield "}"

    # --- JS: Update switch events listbox ---
    yield "function updateSwitchEventsFromStatus(statusData){"
    yield "  if(!statusData || typeof statusData!=='object') return;"
    yield "  const _eventIdentityKey = (line) => {"
    yield "    const text = String(line || '').trim();"
    yield "    const stateMatch = text.match(/^(On|Off)\\b/i);"
    yield "    const tsMatch = text.match(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/);"
    yield "    if (!stateMatch || !tsMatch) return text;"
    yield "    return `${stateMatch[1].toLowerCase()}|${tsMatch[1]}`;"
    yield "  };"
    yield "  const _eventLineRank = (line) => {"
    yield "    const text = String(line || '').toLowerCase();"
    yield "    if (/\\((manual|auto)(\\s*-\\s*[^)]*)?\\)/.test(text)) return 2;"
    yield "    return 1;"
    yield "  };"
    yield "  const _mergeEventLines = (existingLines, newLines, maxEvents) => {"
    yield "    const bestByEvent = new Map();"
    yield "    for (const line of [...existingLines, ...newLines]) {"
    yield "      const text = String(line || '').trim();"
    yield "      if (!text) continue;"
    yield "      const identity = _eventIdentityKey(text);"
    yield "      const prior = bestByEvent.get(identity);"
    yield "      if (!prior || _eventLineRank(text) >= _eventLineRank(prior)) bestByEvent.set(identity, text);"
    yield "    }"
    yield "    const extractTs = (line) => {"
    yield "      const m = String(line || '').match(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/);"
    yield "      return m ? m[1] : '';"
    yield "    };"
    yield "    return Array.from(bestByEvent.values()).sort((a,b) => {"
    yield "      const ta = extractTs(a);"
    yield "      const tb = extractTs(b);"
    yield "      if (ta && tb) return tb.localeCompare(ta);"
    yield "      if (tb) return 1;"
    yield "      if (ta) return -1;"
    yield "      return 0;"
    yield "    }).slice(0, maxEvents);"
    yield "  };"
    yield "  for(const [key,data] of Object.entries(statusData)){"
    yield "    const listElem=_findEventsListElem(key);"
    yield "    if(!listElem){"
    yield "      continue;"
    yield "    }"
    yield ""
    yield "    const hasTime = Object.prototype.hasOwnProperty.call(data,'time');"
    yield "    let events=[];"
    yield "    if(Array.isArray(data?.time)){"
    yield "      events = data.time.slice();"
    yield "    } else if(typeof data?.time==='string'){"
    yield "      events = [data.time];"
    yield "    } else if(Array.isArray(data?.events)){"
    yield "      events = data.events.slice();"
    yield "    } else if(data && (data.state!==undefined || data.time!==undefined)){"
    yield "      const isOn = (data.state===true) || (String(data.state).toLowerCase()==='on');"
    yield "      const ts   = (typeof data.time==='string') ? data.time : '';"
    yield "      events = [ ts ? ((isOn?' ON ':'OFF ')+ts) : (isOn?' ON':'OFF') ];"
    yield "    }"
    yield ""
    yield "    if(!hasTime || events.length===0){"
    yield "      continue;"
    yield "    }"
    yield ""
    yield "    const _originFromSource = (source) => {"
    yield "      const srcRaw = String(source || '').trim();"
    yield "      const src = srcRaw.toLowerCase();"
    yield "      if (!src) return '';"
    yield "      if (src.startsWith('mqtt-auto:')) {"
    yield "        const detail = (srcRaw.split(':', 2)[1] || '').trim();"
    yield "        return detail ? `auto - ${detail}` : 'auto';"
    yield "      }"
    yield "      if (src.startsWith('mqtt-manual')) return 'manual';"
    yield "      if (src.startsWith('auto/rule:')) {"
    yield "        let detail = srcRaw.split(':', 2)[1] || '';"
    yield "        if (detail.toLowerCase().endsWith('/mqtt')) detail = detail.slice(0, -5).trim();"
    yield "        return detail ? `auto - ${detail}` : 'auto';"
    yield "      }"
    yield "      if (src === 'ui' || src.includes('manual') || src.includes('/ui') || src.includes('ui/') || src.includes('user')) return 'manual';"
    yield "      if (src.includes('auto') || src.includes('rule') || src.includes('timer') || src.includes('automation') || src.includes('schedule')) return 'auto';"
    yield "      return '';"
    yield "    };"
    yield ""
    yield "    const normLine=(evt)=>{"
    yield "      if(evt && typeof evt==='object'){"
    yield "        const isOn=(String(evt.state).toLowerCase()==='on')||(evt.state===true);"
    yield "        const tsRaw=evt.ts||evt.time||'';"
    yield "        const ts=_stripIsoExtras(tsRaw);"
    yield "        const origin = _originFromSource(evt.source);"
    yield "        const suffix = origin ? ` (${origin})` : '';"
    yield "        const line = ts ? ((isOn?' On ':' Off ')+ts+suffix) : ((isOn?' On':' Off')+suffix);"
    yield "        return line.trim();"
    yield "      }"
    yield "      let s=String(evt||'').replace(/^'+|'+$/g,'');"
    yield "      s=s.replace(/(\\d{4}-\\d{2}-\\d{2}[T\\s]\\d{2}:\\d{2}:\\d{2})(?:\\.\\d{1,6}(?=Z|[+-]\\d{2}:\\d{2}|$))?/g,(_,a)=>a);"
    yield "      s=s.replace(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})(?:Z|[+-]\\d{2}:\\d{2})\\b/g,'$1');"
    yield "      s=s.replace('T',' ');"
    yield "      return s;"
    yield "    };"
    yield ""
    yield "    const hasTimestamp = (line) => /(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/.test(String(line || ''));"
    yield ""
    yield "    const newLines = events.map(normLine).filter(Boolean).filter(hasTimestamp);"
    yield "    const existingLines = Array.from(listElem.querySelectorAll('li'))"
    yield "          .map(li => (li.textContent || '').trim())"
    yield "          .filter(Boolean)"
    yield "          .filter(hasTimestamp);"
    yield ""
    yield "    const MAX_EVENTS = 5;"
    yield "    const trimmedLines = _mergeEventLines(existingLines, newLines, MAX_EVENTS);"
    yield ""
    yield "    const sig   = trimmedLines.join('\\n');"
    yield "    const prev  = _switchEventsCache.get(key) || listElem.dataset.eventsSig || '';"
    yield "    if(sig===prev){"
    yield "      continue;"
    yield "    }"
    yield ""
    yield "    listElem.textContent='';"
    yield "    for(const textLine of trimmedLines){"
    yield "      const li=document.createElement('li');"
    yield "      li.textContent=textLine;"
    yield "      if(/^on\\b/i.test(textLine)) li.classList.add('switch-event-on');"
    yield "      else                          li.classList.add('switch-event-off');"
    yield "      listElem.appendChild(li);"
    yield "    }"
    yield "    _switchEventsCache.set(key,sig);"
    yield "    listElem.dataset.eventsSig=sig;"
    yield "  }"
    yield "}"
    yield ""
    yield "function appendSwitchEventLine(key, line){"
    yield "  const listElem = _findEventsListElem(key);"
    yield "  if(!listElem) return;"
    yield "  const text = String(line || '').trim();"
    yield "  if(!text) return;"
    yield "  const hasTimestamp = /(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/.test(text);"
    yield "  if(!hasTimestamp) return;"
    yield "  const existingLines = Array.from(listElem.querySelectorAll('li'))"
    yield "        .map(li => (li.textContent || '').trim())"
    yield "        .filter(Boolean)"
    yield "        .filter(value => /(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/.test(String(value || '')));"
    yield "  const sortedLines = _mergeEventLines(existingLines, [text], 5);"
    yield "  listElem.textContent = '';"
    yield "  for(const textLine of sortedLines){"
    yield "    const li = document.createElement('li');"
    yield "    li.textContent = textLine;"
    yield "    if(/^on\\b/i.test(textLine)) li.classList.add('switch-event-on');"
    yield "    else                         li.classList.add('switch-event-off');"
    yield "    listElem.appendChild(li);"
    yield "  }"
    yield "  const sig = sortedLines.join('\\n');"
    yield "  _switchEventsCache.set(key, sig);"
    yield "  listElem.dataset.eventsSig = sig;"
    yield "}"

    yield "window.toggleSwitchInline = function(elOrLabel) {"
    yield "  let el = null;"
    yield "  let real = '';"
    yield ""
    # If called as onclick='toggleSwitchInline(this)', elOrLabel is the div.switch-box
    yield "  if (elOrLabel && typeof elOrLabel === 'object' && elOrLabel.nodeType === 1) {"
    yield "    el = elOrLabel;"
    yield "    real = el.dataset.switchName || '';"
    yield "  } else {"
    # Back-compat: called with a label string
    yield "    real = _realName(elOrLabel);"
    yield "    const safe = _safeName(real);"
    yield "    el = document.getElementById(`${safe}_box`) || document.querySelector(`.switch-box[data-switch-name='${real}']`);"
    yield "  }"
    yield ""
    yield "  if (!el) { console.warn('[toggleSwitchInline] no element for', elOrLabel); return; }"
    yield "  if ((el.dataset.automationEnabled || '') === '1') {"
    yield "    alert('Automation is enabled for this switch. Disable automation before toggling manually.');"
    yield "    return;"
    yield "  }"
    yield "  el.classList.add('switch-pending');"
    yield ""
    yield "  const key = el.dataset.switchKey || '';"
    yield "  const switchId = el.dataset.switchId || '';"
    yield "  const params = new URLSearchParams();"
    yield "  params.set('switch_name', real);"
    yield "  if (key) params.set('switch_key', key);"
    yield "  if (switchId) params.set('switch_id', switchId);"
    yield ""
    yield "  fetch(`/switch/toggle?${params.toString()}`, { method: 'POST' })"
    yield "    .then(async r => {"
    yield "      if (r.status === 409) {"
    yield "        const info = await r.json().catch(() => null);"
    yield "        const msg = (info && info.message) || 'Multiple devices have this label. Please specify a device.';"
    yield "        console.warn('Ambiguous switch label', info && info.options);"
    yield "        alert(msg);"
    yield "        throw new Error('Ambiguous');"
    yield "      }"
    yield "      if (r.status === 423) {"
    yield "        const info = await r.json().catch(() => null);"
    yield "        const msg = (info && info.message) || 'Automation is enabled for this switch. Disable automation before toggling manually.';"
    yield "        alert(msg);"
    yield "        throw new Error('Automation enabled');"
    yield "      }"
    yield "      if (key) _switchRefreshBlockUntil.set(key, Date.now() + 1200);"
    yield "      if (!r.ok) {"
    yield "        const txt = await r.text().catch(() => '');"
    yield "        throw new Error(`HTTP ${r.status} ${txt}`);"
    yield "      }"
    yield "      return r.json();"
    yield "    })"
    yield "    .then(data => {"
    # pass key through so updateSwitchVisuals/_selectSwitchElements can disambiguate
    yield "      updateSwitchVisuals(real, data, key);"
    # Keep the status map keyed by 'switch_id::label' when possible
    yield "      updateSwitchEventsFromStatus({ [key || `::${real}`]: data });"
    yield "    })"
    yield "    .catch(e => console.warn('toggleSwitchInline failed', e))"
    yield "    .finally(() => {"
    yield "      el.classList.remove('switch-pending');"
    yield "      scheduleSwitchStatusRefreshes([1500, 6000, 12000]);"
    yield "    });"
    yield "};"

    yield "window.toggleAutomation = async function(btn, switchId, label) {"
    yield "  try {"
    yield "    const key = `${switchId}::${label}`;"
    yield "    const isEnabled = ((btn.textContent || '').trim().toLowerCase() === 'enabled');"
    yield "    const nextEnabled = !isEnabled;"  # we now toggle the RULE state
    yield "    btn.disabled = true;"
    yield "    const res = await fetch(`/switch/override?switch_key=${encodeURIComponent(key)}`, {"
    yield "      method: 'POST',"
    yield "      headers: { 'Content-Type': 'application/json' },"
    yield "      body: JSON.stringify({ enabled: nextEnabled })"  # enabled = RULE enabled
    yield "    });"
    yield "    if (!res.ok) {"
    yield "      console.error('Override update failed', await res.text());"
    yield "      btn.disabled = false;"
    yield "      return;"
    yield "    }"
    yield "    const json = await res.json();"
    yield "    const ruleEnabled = !!json.enabled;"  # server returns the authoritative rule state"
    yield "    btn.textContent = ruleEnabled ? 'Enabled' : 'Disabled';"
    yield "    btn.classList.toggle('green', ruleEnabled);"
    yield "    btn.classList.toggle('black', !ruleEnabled);"
    yield "  } catch (e) {"
    yield "    console.error('toggleAutomation error', e);"
    yield "  } finally {"
    yield "    btn.disabled = false;"
    yield "  }"
    yield "};"

    yield "async function saveSwitchTimer(inputEl){"
    yield "  if (!inputEl) return;"
    yield "  const switchId = inputEl.dataset.switchId || '';"
    yield "  const label = inputEl.dataset.label || '';"
    yield "  const uiKey = inputEl.dataset.switchUiKey || `${switchId}::${label}`;"
    yield "  const raw = String(inputEl.value || '').trim();"
    yield "  let parsed = Number(raw);"
    yield "  const prior = String(inputEl.dataset.lastGoodValue || '0');"
    yield "  if (Number.isInteger(parsed) && parsed > 0 && parsed < 30) parsed = 30;"
    yield "  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 9999) {"
    yield "    inputEl.value = prior;"
    yield "    alert('Timer must be 0 or between 30 and 9999 seconds.');"
    yield "    return;"
    yield "  }"
    yield "  if (parsed !== 0 && parsed % 30 !== 0) {"
    yield "    parsed = Math.min(9990, Math.max(30, Math.round(parsed / 30) * 30));"
    yield "  }"
    yield "  inputEl.value = String(parsed);"
    yield "  inputEl.disabled = true;"
    yield "  try {"
    yield "    const res = await fetch(`/switch/timer?switch_id=${encodeURIComponent(switchId)}&switch_name=${encodeURIComponent(label)}`, {"
    yield "      method: 'POST',"
    yield "      headers: { 'Content-Type': 'application/json' },"
    yield "      body: JSON.stringify({ seconds: parsed })"
    yield "    });"
    yield "    const data = await res.json().catch(() => ({}));"
    yield "    if (!res.ok) {"
    yield "      inputEl.value = prior;"
    yield "      throw new Error(data.error || `HTTP ${res.status}`);"
    yield "    }"
    yield "    inputEl.dataset.lastGoodValue = String(data.timer_seconds || 0);"
    yield "    updateSwitchTimerUi((data && data.ui_key) || uiKey, data || {}, label);"
    yield "    const panel = _findTimerPanel((data && data.ui_key) || uiKey);"
    yield "    _setTimerEditorOpen(panel, false);"
    yield "  } catch (err) {"
    yield "    console.error('saveSwitchTimer failed', err);"
    yield "    alert('Failed to update timer.');"
    yield "  } finally {"
    yield "    inputEl.disabled = false;"
    yield "  }"
    yield "}"

    yield "document.addEventListener('click', async (ev) => {"
    yield "  const btn = ev.target.closest('.switch-toggle');"
    yield "  if (!btn) return;"
    yield "  if ((btn.dataset.automationEnabled || '') === '1') {"
    yield "    alert('Automation is enabled for this switch. Disable automation before toggling manually.');"
    yield "    return;"
    yield "  }"
    yield "  const key        = btn.dataset.switchKey || '';"
    yield "  const _switch_id = btn.dataset.switchId  || '';"
    yield "  const label      = btn.dataset.switchLabel || '';"
    yield "  try {"
    yield "    const params = new URLSearchParams();"
    yield "    if (label)      params.set('switch_name', label);"
    yield "    if (key)        params.set('switch_key', key);"
    yield "    if (_switch_id) params.set('switch_id', _switch_id);"
    yield "    const res = await fetch(`/switch/toggle?${params.toString()}`, { method: 'POST' });"
    yield "    if (res.status === 409) {"
    yield "      const info = await res.json();"
    yield "      console.warn('Ambiguous switch name. Options:', info.options);"
    yield "      return;"
    yield "    }"
    yield "    if (res.status === 423) {"
    yield "      const info = await res.json().catch(() => null);"
    yield "      alert((info && info.message) || 'Automation is enabled for this switch. Disable automation before toggling manually.');"
    yield "      return;"
    yield "    }"
    yield "    const data = await res.json();"
    yield "    if (data && data.state !== undefined) {"
    # Build an effective key consistent with status/WS events
    yield "      const keyEff = key || (_switch_id && label ? `${_switch_id}::${label}` : '');"
    # Mirror the inline path’s short refresh block to avoid flicker
    yield "      if (keyEff && typeof _switchRefreshBlockUntil !== 'undefined' && _switchRefreshBlockUntil) {"
    yield "        try {"
    yield "          _switchRefreshBlockUntil.set(keyEff, Date.now() + 1200);"
    yield "        } catch (_) { /* non-fatal */ }"
    yield "      }"
    # Update this specific switch’s indicator
    yield "      if (label) {"
    yield "        updateSwitchVisuals(label, data, keyEff);"
    yield "      }"
    # Update this switch’s events list
    yield "      if (keyEff) {"
    yield "        updateSwitchEventsFromStatus({ [keyEff]: data });"
    yield "      }"
    yield "      scheduleSwitchStatusRefreshes([1500, 6000, 12000]);"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Toggle failed', e);"
    yield "  }"
    yield "});"

    yield "document.addEventListener('keydown', function(ev){"
    yield "  const input = ev.target.closest('.switch-timer-input');"
    yield "  if (!input) return;"
    yield "  if (ev.key !== 'Enter') return;"
    yield "  ev.preventDefault();"
    yield "  saveSwitchTimer(input);"
    yield "});"

    yield "document.addEventListener('click', function(ev){"
    yield "  const editBtn = ev.target.closest('.switch-timer-edit-btn');"
    yield "  if (editBtn) {"
    yield "    ev.preventDefault();"
    yield "    ev.stopPropagation();"
    yield "    const panel = editBtn.closest('.switch-timer-panel');"
    yield "    if (!panel) return;"
    yield "    const editor = panel.querySelector('.switch-timer-editor');"
    yield "    const willOpen = !editor || editor.style.display === 'none';"
    yield "    _closeAllTimerEditors();"
    yield "    _setTimerEditorOpen(panel, willOpen);"
    yield "    if (willOpen) {"
    yield "      const input = panel.querySelector('.switch-timer-input');"
    yield "      if (input) { input.focus(); input.select(); }"
    yield "    }"
    yield "    return;"
    yield "  }"
    yield "  const confirmBtn = ev.target.closest('.switch-timer-confirm-btn');"
    yield "  if (confirmBtn) {"
    yield "    ev.preventDefault();"
    yield "    ev.stopPropagation();"
    yield "    const inputId = confirmBtn.dataset.inputId || '';"
    yield "    const input = inputId ? document.getElementById(inputId) : null;"
    yield "    if (input) saveSwitchTimer(input);"
    yield "    return;"
    yield "  }"
    yield "  const cancelBtn = ev.target.closest('.switch-timer-cancel-btn');"
    yield "  if (cancelBtn) {"
    yield "    ev.preventDefault();"
    yield "    ev.stopPropagation();"
    yield "    const inputId = cancelBtn.dataset.inputId || '';"
    yield "    const input = inputId ? document.getElementById(inputId) : null;"
    yield "    if (input) input.value = String(input.dataset.lastGoodValue || '0');"
    yield "    const editorId = cancelBtn.dataset.editorId || '';"
    yield "    const editor = editorId ? document.getElementById(editorId) : null;"
    yield "    const panel = editor ? editor.closest('.switch-timer-panel') : null;"
    yield "    _setTimerEditorOpen(panel, false);"
    yield "    return;"
    yield "  }"
    yield "  if (!ev.target.closest('.switch-timer-panel')) _closeAllTimerEditors();"
    yield "});"
    
    yield "function _currentSwitchIdFromSettings(){"
    yield "  const mm = document.getElementById('switchSettingsModal');"
    yield "  if (!mm) return '';"
    yield "  const ds = (mm.dataset && mm.dataset.switchId) ? mm.dataset.switchId.trim() : '';"
    yield "  if (ds) return ds;"
    yield "  const form = mm.querySelector('form');"
    yield "  if (!form) return '';"
    yield "  const a = form.querySelector('input[name=\"switch_id_field\"]');"
    yield "  if (a && a.value && a.value.trim()) return a.value.trim();"
    yield "  const b = form.querySelector('input[name=\"switch_id\"]');"
    yield "  if (b && b.value && b.value.trim()) return b.value.trim();"
    yield "  return '';"
    yield "}"
    
    yield "function _currentChannelIndicesFromSettings(){"
    yield "  const mm = document.getElementById('switchSettingsModal');"
    yield "  const raw = (mm && mm.dataset && mm.dataset.channelIndices ? mm.dataset.channelIndices : '').trim();"
    yield "  if (!raw) return [1];"
    yield "  return raw.split(',').map(function(s){ return parseInt(s.trim(),10); }).filter(function(n){ return Number.isFinite(n) && n>0; });"
    yield "}"

    yield "window.openAdvancedSwitchModal = async function(switchId){"
    yield "  try {"
    yield "    const switchIdVal = (switchId && String(switchId).trim()) || (window._currentSwitchIdFromSettings && _currentSwitchIdFromSettings()) || '';"
    yield "    if (!switchIdVal) { alert('No switch selected.'); return; }"
    yield ""
    # --- Remove any existing advanced automation modal/backdrop entirely ---
    yield "    (function(){"
    yield "      const oldModal = document.querySelector('#automationManagerModal');"
    yield "      if (oldModal) {"
    yield "        const oldBackdrop = oldModal.closest('.modal-backdrop');"
    yield "        if (oldBackdrop && oldBackdrop.parentNode) oldBackdrop.parentNode.removeChild(oldBackdrop);"
    yield "      }"
    yield "    })();"
    yield ""
    # --- Fetch fresh template HTML ---
    yield "    const r = await fetch(`/ui/modal/advanced-automation?switch_id=${encodeURIComponent(switchIdVal)}`, { cache:'no-store' });"
    yield "    if (!r.ok) { console.error('modal fetch failed', r.status); alert('Failed to load Automations modal'); return; }"
    yield "    const html = await r.text();"
    yield "    if (!html || !html.trim()) { console.error('modal html empty'); alert('Unable to open Automations modal'); return; }"
    yield ""
    # --- Parse & mount ---
    yield "    const host = document.createElement('div');"
    yield "    host.innerHTML = html.trim();"
    yield "    const backdrop = host.querySelector('.modal-backdrop');"
    yield "    const modal    = host.querySelector('#automationManagerModal');"
    yield "    if (!backdrop || !modal) {"
    yield "      console.error('Malformed template. Expected .modal-backdrop and #automationManagerModal. Got:', html.slice(0,200));"
    yield "      alert('Unable to open Automations modal'); return;"
    yield "    }"
    # Stamp IDs for init
    yield "    modal.dataset.switchId = switchIdVal;"
    yield "    backdrop.dataset.switchId = switchIdVal;"
    yield ""
    yield "    const mount = document.querySelector('#modal-host') || document.body;"
    yield "    mount.appendChild(backdrop);"
    yield ""
    # === Ensure the latest automation script is loaded (fixed) ===
    yield "    async function ensureAutomationJs(){"
    yield "      const TAG_ID = 'advanced-automation-js';"
    yield "      const ver = Date.now();"
    yield "      const existing = document.getElementById(TAG_ID);"
    yield "      if (existing && existing.parentNode) existing.parentNode.removeChild(existing);"
    yield "      await new Promise((resolve, reject) => {"
    yield "        const s = document.createElement('script');"
    yield "        s.id = TAG_ID;"
    yield "        s.src = `/ui_static/js/advanced_automation.js?v=${ver}`;"
    yield "        s.onload = resolve;"
    yield "        s.onerror = reject;"
    yield "        document.head.appendChild(s);"
    yield "      });"
    yield "    }"
    yield "    await ensureAutomationJs();"
    yield ""
    # --- Validate the required nodes are actually in the DOM now ---
    yield "    const modalInDom = document.querySelector('#automationManagerModal');"
    yield "    const hasList = modalInDom && modalInDom.querySelector('#automationList');"
    yield "    const hasActions = modalInDom && modalInDom.querySelector('#actionsContainer');"
    yield "    if (!modalInDom || !hasList || !hasActions) {"
    yield "      console.error('Required nodes missing after mount', { modalInDom, hasList, hasActions });"
    yield "      alert('Unable to open Automations modal'); return;"
    yield "    }"
    yield ""
    yield "    if (window.initAdvancedAutomationModal) {"
    yield "      const ok = await window.initAdvancedAutomationModal(modalInDom);"
    yield "      if (!ok) { console.error('initAdvancedAutomationModal returned false'); alert('Unable to open Automations modal'); return; }"
    yield "    } else {"
    yield "      console.error('initAdvancedAutomationModal not found after script load');"
    yield "      alert('Automations script not available.');"
    yield "      return;"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('openAdvancedSwitchModal failed', e);"
    yield "    alert('Unable to open Automations modal');"
    yield "  }"
    yield "};"

    yield "window.closeAdvancedSwitchModal = function(){"
    yield "  const adv = document.getElementById('advancedSwitchModal');"
    yield "  if (!adv) return;"
    yield "  adv.style.display = 'none';"
    yield "  const cond = document.getElementById('conditionsContainer');"
    yield "  const acts = document.getElementById('actionsContainer');"
    yield "  if (cond) cond.innerHTML = '';"
    yield "  if (acts) acts.innerHTML = '';"
    yield "};"

    # ---------- builders ----------
    yield "window.fetchSensorIds = async function(){"
    yield "  try { const r = await fetch('/sensor-ids'); return await r.json(); }"
    yield "  catch(e){ console.error('Failed to load sensors', e); return []; }"
    yield "};"
    
    yield "window.SwitchModal = window.SwitchModal || (function(){"
    yield "  function mount(modal){"
    yield "    if (!modal) return;"
    yield "    if (modal.id !== 'switchSettingsModal') modal.id = 'switchSettingsModal';"
    yield "    modal.dataset.switchMounted = '1';"
    yield "    modal.style.display = 'none';"
    yield "  }"
    yield "  function open(){"
    yield "    const m = document.getElementById('switchSettingsModal');"
    yield "    if (m) m.style.display = 'block';"
    yield "  }"
    yield "  function close(){"
    yield "    const m = document.getElementById('switchSettingsModal');"
    yield "    if (m) m.remove();"
    yield "  }"
    yield "  return { mount, open, close };"
    yield "})();"
    yield ""
  
    yield "const SENSOR_STATUS_COLORS = { online:'#28a745', degraded:'#fd7e14', offline:'#dc3545', unknown:'#ffc107', migration_required:'#6c757d' };"
    yield "let __lastJsonOnly = null;"
    yield "let __lastJsonOnlyAtMs = 0;"
    yield "async function refreshAndApplySensorStatus(){"
    yield "  if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {"
    yield "    return;"
    yield "  }"
    yield "  try {"
    yield "    const now = Date.now();"
    yield "    let data = null;"
    yield "    const wantExtras = !window.__lastExtrasRefreshAtMs || ((now - window.__lastExtrasRefreshAtMs) >= 60000);"
    yield "    if (__lastJsonOnly && (now - __lastJsonOnlyAtMs) < 20000 && !wantExtras) {"
    yield "      data = __lastJsonOnly;"
    yield "    } else {"
    yield "      const url = new URL(window.location.href);"
    yield "      url.searchParams.set('json_only','true');"
    yield "      if (wantExtras) url.searchParams.set('include_extras','true');"
    yield "      const resp = await fetch(url.toString(), { cache:'no-store' });"
    yield "      if (!resp.ok) return;"
    yield "      data = await resp.json();"
    yield "      __lastJsonOnly = data;"
    yield "      __lastJsonOnlyAtMs = now;"
    yield "      if (wantExtras) window.__lastExtrasRefreshAtMs = now;"
    yield "    }"
    yield "    const statuses = data && data.statuses ? data.statuses : {};"
    yield "    Object.entries(statuses).forEach(([sid,st]) => {"
    yield "      const dot = document.getElementById(`${sid}_statusdot`);"
    yield "      if (!dot) return;"
    yield "      const s = (st||'unknown').toLowerCase();"
    yield "      const color = SENSOR_STATUS_COLORS[s] || SENSOR_STATUS_COLORS.unknown;"
    yield "      dot.style.background = color;"
    yield "      dot.title = `Measurement status: ${s}`;"
    yield "      dot.setAttribute('aria-label', `Measurement status: ${s}`);"
    yield "    });"
    yield "    if (data && data.astro && typeof data.astro === 'object') {"
    yield "      Object.assign(astroData, data.astro);"
    yield "      if (typeof drawSunPath === 'function') drawSunPath(astroData);"
    yield "      if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData);"
    yield "    }"
    yield "    if (data && data.biodynamic && typeof data.biodynamic === 'object') {"
    yield "      Object.keys(biodynamicData).forEach((k) => { delete biodynamicData[k]; });"
    yield "      Object.assign(biodynamicData, data.biodynamic);"
    yield "      if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);"
    yield "    }"
    yield "  } catch (_) { /* silent */ }"
    yield "}"
    yield "window.addEventListener('load', ()=>{"
    yield "  setTimeout(refreshAndApplySensorStatus, 400);"
    yield "  setInterval(refreshAndApplySensorStatus, 15000);"
    yield "});"
      
    yield "window.toggleScriptEnable = async function(btn, channel, scriptName, enabled){"
    yield "  try {"
    yield "    btn.disabled = true;"
    yield "    const next = !enabled;"
    yield "    btn.textContent = next ? 'On':'Off';"
    yield "    btn.classList.toggle('green', next);"
    yield "    btn.classList.toggle('black', !next);"
    yield "    btn.setAttribute('onclick', `toggleScriptEnable(this, \"${channel}\", \"${scriptName}\", ${next})`);"
    yield "  } finally { btn.disabled = false; }"
    yield "};"

    yield "window.fetchMetrics = async function(sensorId){"
    yield "  try { const r = await fetch(`/sensor-metrics?sensor_id=${encodeURIComponent(sensorId)}`); return await r.json(); }"
    yield "  catch(e){ console.error('Failed metrics for', sensorId, e); return {}; }"
    yield "};"

    # ---- single onload ----
    yield "window.onload = function() {"
    yield "  setTimeout(checkAndRetryIfNoGauges, 1000);"

    yield "  initGauge();"
    yield "  initSwitchTimersFromDom();"
    yield "  refreshAndApplySwitchStatus();"
    yield "  setTimeout(updateGauges, 600);"

    yield "  const sel = document.getElementById('sensorSelect');"
    yield "  if (sel) {"
    yield "    sel.addEventListener('change', function() {"
    yield "      const selected = this.value;"
    yield "      const path = window.location.pathname;"
    yield "      window.location.href = `${path}?sensor_id=${encodeURIComponent(selected)}`;"
    yield "    });"
    yield "  }"

    yield "  setInterval(updateLocalTime, 1000);"
    yield "  setInterval(tickSwitchCountdowns, 1000);"
    yield "  setInterval(function(){ if (typeof drawSunPath === 'function') drawSunPath(astroData); }, 60000);"
    yield "  setInterval(function(){ if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData); }, 3600000);"
    yield "  setInterval(function(){ if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData); }, 60000);"
    yield "  setInterval(checkAndRetryIfNoGauges, 60000);"
    yield "  setInterval(updateGauges, 15000);"
    yield "  setInterval(refreshAndApplySwitchStatus, 60000);"
    yield "  setInterval(function() {"
    yield "    if (typeof window.refreshAllMicrographs === 'function') {"
    yield "      window.refreshAllMicrographs(false);"
    yield "    }"
    yield "  }, 60000);"
    yield "}"
    
    yield "</script>"
    
    yield "<div id='modal-host'></div>"
    yield "</div>"
    yield "</body></html>"

def core_helpers_html() -> str:
    # styles sit OUTSIDE <script> to avoid parser issues
    return (
        "<style>"
        ".toast-container{position:fixed; top:16px; left:50%; transform:translateX(-50%); z-index:99999}"
        ".toast{padding:10px 14px; border-radius:8px; background:#222; color:#fff; box-shadow:0 2px 10px rgba(0,0,0,.25)}"
        ".toast.ok{background:#1f693a}"
        ".toast.error{background:#8b0000}"
        ".onboard-overlay{z-index:99990}"
        ".onboard-modal{position:relative; z-index:99991}"
        "</style>"
        "<script>"
        "(function(){"
        "  if (typeof window.showToast !== 'function') {"
        "    window.showToast = function(text, type){"
        "      var c = document.querySelector('.toast-container');"
        "      if (!c){ c = document.createElement('div'); c.className='toast-container'; document.body.appendChild(c); }"
        "      var t = document.createElement('div'); t.className = 'toast ' + (type||''); t.textContent = text||'';"
        "      c.appendChild(t); setTimeout(function(){ t.remove(); if(!c.children.length) c.remove(); }, 2500);"
        "    };"
        "  }"
        "})();"
        "</script>"
    )

def render_graph_modal(switch_installed=None):
    # Read tz offset/name from system settings for client display/normalization
    try:
        from saiSettings import saiSettings
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
    yield """
    <style>
    .button {
      display: inline-block;
      padding: 10px 20px;
      margin: 10px;
      font-size: 16px; font-weight: bold;
      border-radius: 6px; border: none; cursor: pointer;
      transition: background 0.3s, transform 0.2s;
    }
    .button:hover { transform: translateY(0px); }
    .green{background:#28a745;color:#fff}.green:hover{background:#218838}
    .red{background:#dc3545;color:#fff}.red:hover{background:#c82333}
    .neutral{background:#f7f1c1;color:#212529}.neutral:hover{background:#f7efb0}
    .black{background:#000;color:#fff}.black:hover{background:#333}
    .yellow{background:#ffc107;color:#212529}.yellow:hover{background:#e0a800}
    .blue{background:#2259f2;color:#fff}.blue:hover{background:blue}

    #fullscreen_graph_container {
      display: none; position: fixed; top:0; left:0; width:100%; height:100vh;
      background:white; z-index:1001; flex-direction:column; justify-content:flex-start;
      padding-top:3rem; padding-bottom:4rem; overflow:hidden; box-sizing:border-box;
    }
    #fullscreen_graph { flex:1; width:100%; }
    #graphModal {
      display:none; position:fixed; top:0; left:0; width:100%; height:100%;
      background:rgba(0,0,0,0.5); z-index:1000; justify-content:center; align-items:center;
    }
    #graphModal .modal-content {
      background:#F5FFFA; padding:1rem; border-radius:14px;
      box-shadow:0 10px 30px rgba(0,0,0,.18);
      width:fit-content; max-width:96vw; max-height:90%; overflow:hidden;
      display:grid; grid-template-columns: minmax(200px, 240px) minmax(400px, 520px); gap:1rem;
      box-sizing:border-box;
    }
    #graphModal .graph-left-pane,
    #graphModal .graph-right-pane{
      border:1px solid #d6dfd8;
      border-radius:10px;
      background:#ffffff;
      min-height:560px;
      display:flex;
      flex-direction:column;
      overflow:hidden;
    }
    #graphModal .graph-left-pane{ background:#ecf5ee; }
    #graphModal .graph-pane-title{
      margin:0; padding:0.85rem 1rem; font-size:1rem; font-weight:700;
      border-bottom:1px solid #e6ece8;
    }
    #graphSetupList{
      flex:1;
      overflow:auto;
      padding:0.65rem;
      display:flex;
      flex-direction:column;
      gap:0.45rem;
    }
    #graphSetupList .setup-item{
      border:1px solid #d8e5df;
      border-radius:8px;
      padding:0.55rem 0.65rem;
      background:#fff;
      text-align:left;
      cursor:pointer;
      font-size:0.92rem;
      line-height:1.25;
    }
    #graphSetupList .setup-item:hover{ background:#fff; border-color:#d6dfd8; }
    #graphSetupList .setup-item.active{ background:#dce9ff; border-color:#afc8f7; font-weight:700; }
    #graphSetupList .setup-empty{
      font-size:0.9rem;
      color:#5f7469;
      padding:0.4rem;
    }
    #graphModal .graph-left-footer{
      border-top:1px solid #e6ece8;
      padding:0.65rem;
      display:flex;
      justify-content:center;
    }
    #graphModal .graph-left-footer .button{
      margin:0;
      width:100%;
      max-width:180px;
    }
    #graphModal .graph-right-body{
      flex:1;
      overflow:auto;
      padding:0.95rem 1rem 0.6rem 1rem;
    }
    #graphModal .graph-actions{
      border-top:1px solid #e6ece8;
      padding:0.75rem 1rem;
      display:flex;
      justify-content:space-between;
      gap:0.6rem;
      align-items:center;
    }
    #graphModal .graph-actions .button{ margin:0; }
    .spinner{
      width:16px;height:16px;border:2px solid #ccc;border-top:2px solid #333;border-radius:50%;
      animation:spin 1s linear infinite; display:inline-block;vertical-align:middle
    }
    @keyframes spin { to{ transform:rotate(360deg) } }

    .axis-grid{
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:.5rem 1rem;
      margin-bottom:.75rem
    }
    .axis-grid h3{grid-column:1/3;margin:.25rem 0 .25rem 0}
    .axis-grid label{font-size:.9rem}
    @media (max-width: 980px){
      #graphModal .modal-content{
        grid-template-columns: 1fr;
        max-width: 96%;
      }
      #graphModal .graph-left-pane,
      #graphModal .graph-right-pane{
        min-height: auto;
      }
      #graphSetupList{ max-height:220px; }
    }
    </style>
    """

    # ---------- Modal shell ----------
    yield "<div id='graphModal' class='modal'>"
    yield "  <div class='modal-content'>"
    yield "    <div class='graph-left-pane'>"
    yield "      <h3 class='graph-pane-title'>Saved Graph Setups</h3>"
    yield "      <div id='graphSetupList'></div>"
    yield "      <div class='graph-left-footer'>"
    yield "        <button id='graphSetupRemoveBtn' class='button red' title='Remove graph setup' onclick='removeGraphSetup()' disabled>Remove</button>"
    yield "      </div>"
    yield "    </div>"
    yield "    <div class='graph-right-pane'>"
    yield "      <h2 class='graph-pane-title' style='text-align:center;'>Graph Sensor Metrics</h2>"
    yield "      <div class='graph-right-body'>"

    # Axis pickers
    yield "    <div class='axis-grid'>"
    yield "      <h3>Left Y-Axis</h3>"
    yield "      <select id='sensor1_select' style='width:100%'></select>"
    yield "      <select id='metric1_select' style='width:100%'></select>"
    yield "      <h3>Right Y-Axis A</h3>"
    yield "      <select id='sensor2_select' style='width:100%'></select>"
    yield "      <select id='metric2_select' style='width:100%'></select>"
    yield "      <h3>Right Y-Axis B</h3>"
    yield "      <select id='sensor3_select' style='width:100%'></select>"
    yield "      <select id='metric3_select' style='width:100%'></select>"
    yield "    </div>"

    # Time ranges
    yield "    <label>Time Range:</label><br>"
    yield "    <div style='display:flex; flex-wrap:wrap; gap:0.5rem; margin-bottom:0.5rem;'>"
    range_options = [
        ("1Hr", "1h"),
        ("3Hr", "3h"),
        ("6Hr", "6h"),
        ("12Hr", "12h"),
        ("24Hr", "24h"),
    ]
    if max_days >= 3:
        range_options.append(("3Day", "3d"))
    if max_days >= 7:
        range_options.append(("7Day", "7d"))
    if max_days > 1 and max_days not in (3, 7):
        range_options.append((f"{max_days}Day", f"{max_days}d"))
    for label, val in range_options:
        checked_attr = " checked" if val == "24h" else ""
        yield (
            f"      <label><input type='radio' name='range' value='{val}'"
            f"{checked_attr} onchange='toggleCustomTime(false)'>{label}</label>"
        )
    yield "    </div>"
    yield f"    <div style='font-size:0.85rem; opacity:0.8; margin-bottom:0.5rem;'>Max range: {max_days} days</div>"

    yield "    <div style='display:flex; flex-wrap:wrap; gap:0.5rem; margin-bottom:0.5rem;'>"
    yield "      <label><input type='radio' name='range' value='custom' onchange='toggleCustomTime(true)'>Custom</label>"
    yield "    </div>"
    yield "    <div id='custom_time_inputs' style='display:none;'>"
    yield "      <label>Start:</label><input type='datetime-local' id='start_time'><br>"
    yield "      <label>End:</label><input type='datetime-local' id='end_time'>"
    yield "    </div>"

    # ---------- Switch section ----------
    switch_map: dict[str, list[str]] = {}
    if switch_installed:
        try:
            from saiSwitchSettingsManager import SwitchSettingsManager

            sw_mgr = SwitchSettingsManager("switch_settings")
            sw_ids = sw_mgr.list_switches() or []
            for sid in sw_ids:
                try:
                    doc = sw_mgr.load(sid) or {}
                    if hasattr(sw_mgr, "get_switch_channel_names"):
                        labels = list(sw_mgr.get_switch_channel_names(doc) or [])
                    else:
                        swblk = (doc or {}).get("Switch", {}) if isinstance(doc, dict) else {}
                        labels: list[str] = []
                        for i in range(1, 7):
                            lab = str(swblk.get(f"SWITCH_{i}_LABEL", "") or "").strip()
                            en = str(
                                swblk.get(f"SWITCH_{i}_ENABLE_PIN", swblk.get(f"SWITCH_{i}_EN", ""))
                                or ""
                            ).strip()
                            pin = str(swblk.get(f"SWITCH_{i}_PIN", "") or "").strip()
                            if lab and (en or pin):
                                labels.append(lab)
                    if labels:
                        switch_map[sid] = labels
                except Exception:
                    pass
        except Exception:
            pass

        yield "    <div id='switch_lines_section' style='margin:1rem 0;'>"
        yield "      <div style='margin-bottom:0.4rem;'>Switch Transitions:</div>"
        if switch_map:
            yield "      <label for='switch_select'>Switch:</label>"
            yield "      <select id='switch_select' style='width:100%; margin-bottom:0.5rem;'></select>"
            yield "      <div id='channel_checkboxes' style='display:flex; flex-wrap:wrap; gap:0.75rem;'></div>"
        else:
            yield "      <div style='opacity:0.8;'>No switches found.</div>"
        yield "    </div>"

    # Footer bar
    yield "      </div>"
    yield "    <div class='graph-actions'>"
    yield (
        "      <button class='button black' title='Close graph setup' "
        "onclick=\"document.getElementById('graphModal').style.display='none'\">Home</button>"
    )
    yield "      <button id='graphSaveButton' class='button green' title='Save graph setup' onclick='saveGraphSetup(event)'>Save</button>"
    yield "      <button id='graphButton' class='button blue' title='Load graph' onclick='loadGraph(event)'>"
    yield "        <span class='spinner' style='display:none;margin-right:6px;'></span>"
    yield "        <span class='button-text'>Graph It</span>"
    yield "      </button>"
    yield "    </div>"
    yield "    </div>"
    yield "  </div>"
    yield "</div>"

    # ---------- Fullscreen canvas ----------
    yield """
    <div id="fullscreen_graph_container">
        <button class='button black'
                title="Close full screen graph"
                onclick="closeFullscreenGraph()"
                style="position:absolute;bottom:1rem;left:50%;transform:translateX(-50%);z-index:1002;">
            Close
        </button>
        <canvas id="fullscreen_graph" style="width:100%; height:90vh;"></canvas>
        <div id="switch_legend" style="display:flex; justify-content:center; gap:1rem; margin-bottom:0.5rem;"></div>
    </div>
    """

    # ---------- Scripts ----------
    yield "<script>"
    yield "function toggleCustomTime(enabled){ document.getElementById('custom_time_inputs').style.display = enabled ? 'block' : 'none'; }"

    # Timezone injection
    yield f"const TZ_OFFSET_S = {tz_offset};"
    yield f"const TZ_NAME = {_json.dumps(tz_name)};"
    yield f"const GRAPH_GAUGE_CONFIG = {_json.dumps(get_gauge_config())};"

    # Switch map injection
    if switch_map:
        yield f"const SWITCH_MAP = {_json.dumps(switch_map)};"
    else:
        yield "const SWITCH_MAP = {};"

    # Core JS (no // comments)
    yield r"""
    function toLocalMs(rawTs){
      if (rawTs == null) return NaN;
      if (typeof rawTs === 'number' && Number.isFinite(rawTs)) return rawTs * 1000;
      if (typeof rawTs === 'string' && /^\d+(\.\d+)?$/.test(rawTs.trim())) return Number(rawTs) * 1000;
      const t = new Date(rawTs).getTime();
      return Number.isFinite(t) ? t : NaN;
    }
    function localOrUndef(v){
      if (v === undefined || v === null || v === '') return undefined;
      const ms = toLocalMs(v);
      return Number.isFinite(ms) ? ms : undefined;
    }

    function graphMetricNameFromKey(seriesKey){
      const parts = String(seriesKey || '').split('::');
      return (parts.length ? parts[parts.length - 1] : seriesKey || '').trim();
    }

    function normalizeGraphGaugeMetricName(value){
      return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    }

    function graphGaugeMetricEntry(metricName){
      const raw = String(metricName || '').trim();
      if (!raw) return null;
      if (GRAPH_GAUGE_CONFIG && GRAPH_GAUGE_CONFIG[raw]) {
        return { key: raw, config: GRAPH_GAUGE_CONFIG[raw] };
      }
      const withoutChannel = raw.replace(/^CH\d+\s+/i, '').trim();
      if (withoutChannel && GRAPH_GAUGE_CONFIG && GRAPH_GAUGE_CONFIG[withoutChannel]) {
        return { key: withoutChannel, config: GRAPH_GAUGE_CONFIG[withoutChannel] };
      }
      const rawNorm = normalizeGraphGaugeMetricName(raw);
      const channelNorm = normalizeGraphGaugeMetricName(withoutChannel);
      for (const key of Object.keys(GRAPH_GAUGE_CONFIG || {})) {
        const keyNorm = normalizeGraphGaugeMetricName(key);
        if (keyNorm === rawNorm || (channelNorm && keyNorm === channelNorm)) {
          return { key: key, config: GRAPH_GAUGE_CONFIG[key] };
        }
      }
      return null;
    }

    function soilFertilityGaugeZones(metricName){
      const entry = graphGaugeMetricEntry(metricName);
      if (!entry || entry.key !== 'Soil Fertility Index') return null;
      const cfg = entry.config || {};
      const zones = Array.isArray(cfg.zones) ? cfg.zones.map(function(z){
        return {
          min: Number(z && z.min),
          max: Number(z && z.max),
          color: (z && (z.strokeStyle || z.color)) || ''
        };
      }).filter(function(z){
        return Number.isFinite(z.min) && Number.isFinite(z.max) && !!z.color;
      }) : [];
      if (!zones.length) return null;
      return {
        min: Number(cfg.min),
        max: Number(cfg.max),
        zones: zones
      };
    }

    let GRAPH_SETUPS = [];
    let GRAPH_LAST_USED = '';
    let GRAPH_ACTIVE_SETUP = '';

    async function fetchJSON(url){
      const r = await fetch(url, {cache: 'no-store'});
      try { return await r.json(); } catch { return null; }
    }

    async function postJSON(url, payload){
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      let data = null;
      try { data = await r.json(); } catch {}
      if(!r.ok){
        const msg = (data && (data.error || data.detail)) ? String(data.error || data.detail) : ('HTTP ' + r.status);
        throw new Error(msg);
      }
      return data || {};
    }

    async function populateSensors(selectIds){
      const sensors = await fetchJSON('/sensor-ids') || [];
      for(const id of selectIds){
        const sel = document.getElementById(id);
        if(!sel) continue;
        sel.innerHTML = "<option value=''>-- Select Sensor --</option>";
        sensors.forEach(s => {
          const o = document.createElement('option');
          o.value = s;
          o.textContent = s;
          sel.appendChild(o);
        });
      }
    }

    async function populateMetricsFor(sensorSelId, metricSelId){
      const sidEl = document.getElementById(sensorSelId);
      const sid = (sidEl && sidEl.value ? sidEl.value : "").trim();
      const msel = document.getElementById(metricSelId);
      if(!msel) return;
      msel.innerHTML = "<option value=''>-- Select Metric --</option>";
      if(!sid) return;

      const payload = await fetchJSON(`/sensor-metrics?sensor_id=${encodeURIComponent(sid)}`);
      let metricNames = [];
      if(Array.isArray(payload)) metricNames = payload;
      else if(payload && typeof payload === 'object'){
        if(Array.isArray(payload.metrics)) metricNames = payload.metrics;
        else metricNames = Object.keys(payload);
      }
      metricNames.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        msel.appendChild(opt);
      });
    }

    function renderSwitchChannels(){
      const swSel = document.getElementById('switch_select');
      const chBox = document.getElementById('channel_checkboxes');
      if(!swSel || !chBox) return;
      chBox.innerHTML = '';
      const sid = (swSel.value || '').trim();
      if(!sid) return;
      (SWITCH_MAP[sid] || []).forEach(label => {
        const encoded = btoa(unescape(encodeURIComponent(sid + '::' + label))).replace(/=/g,'');
        const id = 'ch_' + encoded;
        const wrap = document.createElement('label');
        wrap.innerHTML = "<input type='checkbox' id='" + id + "' data-label='" + label + "'> " + label;
        chBox.appendChild(wrap);
      });
    }

    function setRangeSelection(rangeVal){
      const normalized = (rangeVal || '24h').trim().toLowerCase();
      const target = document.querySelector("input[name='range'][value='" + normalized + "']");
      const custom = normalized === 'custom';
      if(target){
        target.checked = true;
      }else{
        const fallback = document.querySelector("input[name='range'][value='24h']");
        if (fallback) fallback.checked = true;
      }
      toggleCustomTime(custom);
    }

    function getCurrentGraphConfig(){
      const rangeEl = document.querySelector("input[name='range']:checked");
      const range = rangeEl ? String(rangeEl.value || '24h') : '24h';
      const cfg = {
        sensor1_select: (document.getElementById('sensor1_select')?.value || '').trim(),
        sensor2_select: (document.getElementById('sensor2_select')?.value || '').trim(),
        sensor3_select: (document.getElementById('sensor3_select')?.value || '').trim(),
        metric1_select: (document.getElementById('metric1_select')?.value || '').trim(),
        metric2_select: (document.getElementById('metric2_select')?.value || '').trim(),
        metric3_select: (document.getElementById('metric3_select')?.value || '').trim(),
        range: range,
        start_time: (document.getElementById('start_time')?.value || '').trim(),
        end_time: (document.getElementById('end_time')?.value || '').trim(),
        switch_select: (document.getElementById('switch_select')?.value || '').trim(),
        channels: []
      };
      const cbs = document.querySelectorAll('#channel_checkboxes input[type="checkbox"]');
      (cbs || []).forEach(cb => {
        if(cb.checked){
          const label = (cb.getAttribute('data-label') || '').trim();
          if(label) cfg.channels.push(label);
        }
      });
      return cfg;
    }

    async function applyGraphConfig(cfg){
      const c = (cfg && typeof cfg === 'object') ? cfg : {};
      const s1 = document.getElementById('sensor1_select');
      const s2 = document.getElementById('sensor2_select');
      const s3 = document.getElementById('sensor3_select');
      if(s1) s1.value = String(c.sensor1_select || '');
      if(s2) s2.value = String(c.sensor2_select || '');
      if(s3) s3.value = String(c.sensor3_select || '');

      await populateMetricsFor('sensor1_select','metric1_select');
      await populateMetricsFor('sensor2_select','metric2_select');
      await populateMetricsFor('sensor3_select','metric3_select');

      const m1 = document.getElementById('metric1_select');
      const m2 = document.getElementById('metric2_select');
      const m3 = document.getElementById('metric3_select');
      if(m1) m1.value = String(c.metric1_select || '');
      if(m2) m2.value = String(c.metric2_select || '');
      if(m3) m3.value = String(c.metric3_select || '');

      setRangeSelection(String(c.range || '24h'));
      const startEl = document.getElementById('start_time');
      const endEl = document.getElementById('end_time');
      if(startEl) startEl.value = String(c.start_time || '');
      if(endEl) endEl.value = String(c.end_time || '');

      const swSel = document.getElementById('switch_select');
      if(swSel){
        swSel.value = String(c.switch_select || '');
        renderSwitchChannels();
        const selected = new Set(Array.isArray(c.channels) ? c.channels.map(v => String(v)) : []);
        const cbs = document.querySelectorAll('#channel_checkboxes input[type="checkbox"]');
        (cbs || []).forEach(cb => {
          const label = String(cb.getAttribute('data-label') || '');
          cb.checked = selected.has(label);
        });
      }
    }

    function markActiveSetup(name){
      GRAPH_ACTIVE_SETUP = String(name || '');
      const removeBtn = document.getElementById('graphSetupRemoveBtn');
      if(removeBtn) removeBtn.disabled = !GRAPH_ACTIVE_SETUP;
      const nodes = document.querySelectorAll('#graphSetupList .setup-item');
      (nodes || []).forEach(n => {
        if((n.getAttribute('data-name') || '') === GRAPH_ACTIVE_SETUP) n.classList.add('active');
        else n.classList.remove('active');
      });
    }

    function renderGraphSetupList(){
      const list = document.getElementById('graphSetupList');
      if(!list) return;
      list.innerHTML = '';
      if(!Array.isArray(GRAPH_SETUPS) || !GRAPH_SETUPS.length){
        const empty = document.createElement('div');
        empty.className = 'setup-empty';
        empty.textContent = 'No saved graph setups.';
        list.appendChild(empty);
        markActiveSetup('');
        return;
      }
      GRAPH_SETUPS.forEach(item => {
        const btn = document.createElement('button');
        const setupName = String(item.name || '');
        btn.type = 'button';
        btn.className = 'setup-item';
        btn.setAttribute('data-name', setupName);
        btn.title = setupName ? `Load graph setup ${setupName}` : 'Load graph setup';
        btn.textContent = setupName;
        btn.onclick = () => loadSavedGraphSetup(setupName);
        list.appendChild(btn);
      });
      markActiveSetup(GRAPH_ACTIVE_SETUP || GRAPH_LAST_USED || '');
    }

    async function refreshGraphSetupsFromServer(){
      const payload = await fetchJSON('/graph-setups');
      const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
      GRAPH_SETUPS = items.map(it => ({
        name: String((it && it.name) || ''),
        config: (it && typeof it.config === 'object') ? it.config : {}
      })).filter(it => it.name);
      GRAPH_LAST_USED = String((payload && payload.last_used) || '');
      renderGraphSetupList();
    }

    async function loadSavedGraphSetup(name){
      const setupName = String(name || '').trim();
      if(!setupName) return;
      const hit = (GRAPH_SETUPS || []).find(it => String(it.name || '') === setupName);
      if(!hit) return;
      await applyGraphConfig(hit.config || {});
      markActiveSetup(setupName);
      try{
        await postJSON('/graph-setups/use', { name: setupName });
      }catch(e){
        console.warn('Failed to set last-used graph setup', e);
      }
    }

    async function saveGraphSetup(event){
      const btn = event && event.target ? event.target.closest('button') : document.getElementById('graphSaveButton');
      if(btn){
        if(!btn.dataset.baseLabel) btn.dataset.baseLabel = btn.textContent || 'Save';
        btn.disabled = true;
        btn.textContent = 'Saving...';
      }
      try{
        const suggested = GRAPH_ACTIVE_SETUP || '';
        const rawName = window.prompt('Save graph setup as:', suggested);
        if(rawName === null) return;
        const name = String(rawName || '').trim();
        if(!name){
          if(typeof window.showToast === 'function') window.showToast('Setup name is required.', 'error');
          return;
        }
        const config = getCurrentGraphConfig();
        const payload = await postJSON('/graph-setups/save', { name: name, config: config });
        const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
        GRAPH_SETUPS = items.map(it => ({
          name: String((it && it.name) || ''),
          config: (it && typeof it.config === 'object') ? it.config : {}
        })).filter(it => it.name);
        GRAPH_LAST_USED = String((payload && payload.last_used) || name);
        renderGraphSetupList();
        markActiveSetup(name);
        if(typeof window.showToast === 'function') window.showToast('Graph setup saved', 'ok');
      }catch(e){
        console.error('Save graph setup failed', e);
        if(typeof window.showToast === 'function') window.showToast('Failed to save graph setup: ' + (e && e.message ? e.message : 'unknown error'), 'error');
      }finally{
        if(btn){
          btn.disabled = false;
          btn.textContent = btn.dataset.baseLabel || 'Save';
        }
      }
    }

    async function removeGraphSetup(){
      const name = String(GRAPH_ACTIVE_SETUP || '').trim();
      if(!name) return;
      if(!window.confirm("Remove saved graph setup '" + name + "'?")) return;
      const btn = document.getElementById('graphSetupRemoveBtn');
      if(btn) btn.disabled = true;
      try{
        const payload = await postJSON('/graph-setups/remove', { name: name });
        const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
        GRAPH_SETUPS = items.map(it => ({
          name: String((it && it.name) || ''),
          config: (it && typeof it.config === 'object') ? it.config : {}
        })).filter(it => it.name);
        GRAPH_LAST_USED = String((payload && payload.last_used) || '');
        renderGraphSetupList();
        if(GRAPH_LAST_USED){
          await loadSavedGraphSetup(GRAPH_LAST_USED);
        } else {
          markActiveSetup('');
        }
        if(typeof window.showToast === 'function') window.showToast('Graph setup removed', 'ok');
      }catch(e){
        console.error('Remove graph setup failed', e);
        alert('Failed to remove graph setup: ' + (e && e.message ? e.message : 'unknown error'));
      }finally{
        if(btn) btn.disabled = !GRAPH_ACTIVE_SETUP;
      }
    }

    window.saveGraphSetup = saveGraphSetup;
    window.removeGraphSetup = removeGraphSetup;

    async function initGraphBuilder(){
      try{
        await populateSensors(['sensor1_select','sensor2_select','sensor3_select']);
        const s1 = document.getElementById('sensor1_select');
        const s2 = document.getElementById('sensor2_select');
        const s3 = document.getElementById('sensor3_select');

        if(s1) s1.onchange = () => populateMetricsFor('sensor1_select','metric1_select');
        if(s2) s2.onchange = () => populateMetricsFor('sensor2_select','metric2_select');
        if(s3) s3.onchange = () => populateMetricsFor('sensor3_select','metric3_select');

        await populateMetricsFor('sensor1_select','metric1_select');
        await populateMetricsFor('sensor2_select','metric2_select');
        await populateMetricsFor('sensor3_select','metric3_select');

        const swSel = document.getElementById('switch_select');
        if(swSel){
          swSel.innerHTML = "<option value=''>-- Select Switch --</option>";
          Object.keys(SWITCH_MAP).forEach(sid => {
            const o = document.createElement('option');
            o.value = sid;
            o.textContent = sid;
            swSel.appendChild(o);
          });
          swSel.onchange = () => renderSwitchChannels();
        }
        await refreshGraphSetupsFromServer();
        if(GRAPH_LAST_USED){
          await loadSavedGraphSetup(GRAPH_LAST_USED);
        }else{
          markActiveSetup('');
        }
      }catch(e){
        console.error('Graph builder init failed', e);
      }
    }

    function loadGraph(event){
      const button = event.target.closest('button');
      const spinner = button.querySelector('.spinner');
      const text = button.querySelector('.button-text');
      button.disabled = true;
      spinner.style.display='inline-block';
      text.textContent='Preparing Graph...';

      const s1 = document.getElementById('sensor1_select').value;
      const s2 = document.getElementById('sensor2_select').value;
      const s3 = document.getElementById('sensor3_select').value;
      const m1 = document.getElementById('metric1_select').value;
      const m2 = document.getElementById('metric2_select').value;
      const m3 = document.getElementById('metric3_select').value;

      const rangeEl  = document.querySelector('input[name="range"]:checked');
      const rangeVal = rangeEl ? rangeEl.value : '24h';
      const start = document.getElementById('start_time') ? document.getElementById('start_time').value : "";
      const end   = document.getElementById('end_time')   ? document.getElementById('end_time').value   : "";

      const params = new URLSearchParams({
        sensor_id: s1 || '',
        metric1: m1 || '',
        metric2: m2 || '',
        metric3: m3 || '',
        sensor_id1: s1 || '',
        sensor_id2: s2 || '',
        sensor_id3: s3 || '',
        range: rangeVal
      });

      if(rangeVal === 'custom'){
        if(!start || !end){
          alert('Enter start and end times.');
          button.disabled=false;
          spinner.style.display='none';
          text.textContent='Graph It';
          return;
        }
        params.set('start', start);
        params.set('end', end);
      }

      const swSel = document.getElementById('switch_select');
      if(swSel && swSel.value){
        params.set('switch_id', swSel.value);
        const chBox = document.getElementById('channel_checkboxes');
        const cbs = chBox ? chBox.querySelectorAll('input[type="checkbox"]') : [];
        (cbs || []).forEach(cb => {
          if(cb.checked){
            const label = cb.getAttribute('data-label') || '';
            if(label) params.append('channels', label);
          }
        });
      }

      fetch('/graph-data?' + params.toString())
        .then(r => r.json())
        .then(data => {
          if (data && data.no_data) {
            alert(data.detail || 'No data in selected graph window');
            return;
          }
          if (GRAPH_ACTIVE_SETUP){
            postJSON('/graph-setups/use', { name: GRAPH_ACTIVE_SETUP }).catch(() => {});
          }
          document.getElementById('graphModal').style.display='none';
          renderGraphFullscreen_V2(data);
        })
        .catch(e => {
          console.error('Graph error:', e);
          alert('Graph load failed');
        })
        .finally(() => {
          button.disabled=false;
          spinner.style.display='none';
          text.textContent='Graph It';
        });
    }

    const vpdBackgroundPlugin = {
      id: 'vpdBackground',
      beforeDraw(chart) {
        const enabled = !!(chart && chart.options && chart.options.plugins &&
                           chart.options.plugins.vpdZones &&
                           chart.options.plugins.vpdZones.enabled);
        if (!enabled) return;

        const ctx = chart.ctx;
        const chartArea = chart.chartArea;
        const scales = chart.scales;
        if (!chartArea) return;

        const zones = [
          { color: '#800080', min: 0.0, max: 0.4 },
          { color: '#3399ff', min: 0.4, max: 0.8 },
          { color: '#add8e6', min: 0.8, max: 1.2 },
          { color: '#66cc66', min: 1.2, max: 1.6 },
          { color: '#f00',    min: 1.6, max: 5.0 }
        ];

        const allScales = scales || {};
        const yScale =
          allScales.y1 ||
          Object.values(allScales).find(s => s && s.axis === 'y');
        if (!yScale) return;

        ctx.save();
        ctx.globalAlpha = 0.3;
        zones.forEach(z => {
          const y1 = yScale.getPixelForValue(z.min);
          const y2 = yScale.getPixelForValue(z.max);
          const top = Math.min(y1, y2);
          const height = Math.abs(y2 - y1);
          ctx.fillStyle = z.color;
          ctx.fillRect(chartArea.left, top, chartArea.right - chartArea.left, height);
        });
        ctx.globalAlpha = 1.0;
        ctx.restore();
      }
    };

    const gaugeZonesBackgroundGraph = {
      id: 'gaugeZonesBackgroundGraph',
      beforeDraw(chart) {
        const zonesByAxis = chart && chart.options && chart.options.plugins &&
          chart.options.plugins.gaugeZonesBackgroundGraph &&
          chart.options.plugins.gaugeZonesBackgroundGraph.zonesByAxis;
        if (!zonesByAxis || typeof zonesByAxis !== 'object') return;
        const ctx = chart.ctx;
        const chartArea = chart.chartArea;
        const scales = chart.scales || {};
        if (!ctx || !chartArea) return;
        ctx.save();
        ctx.globalAlpha = 0.3;
        Object.entries(zonesByAxis).forEach(function(pair){
          const axisId = pair[0];
          const zones = pair[1];
          const yScale = scales[axisId];
          if (!yScale || !Array.isArray(zones) || !zones.length) return;
          zones.forEach(function(z){
            const zMin = Number(z && z.min);
            const zMax = Number(z && z.max);
            const color = (z && z.color) || '';
            if (!Number.isFinite(zMin) || !Number.isFinite(zMax) || !color) return;
            const y1 = yScale.getPixelForValue(zMin);
            const y2 = yScale.getPixelForValue(zMax);
            const top = Math.min(y1, y2);
            const bottom = Math.max(y1, y2);
            const clippedTop = Math.max(chartArea.top, Math.min(chartArea.bottom, top));
            const clippedBottom = Math.max(chartArea.top, Math.min(chartArea.bottom, bottom));
            if (clippedBottom <= clippedTop) return;
            ctx.fillStyle = color;
            ctx.fillRect(chartArea.left, clippedTop, chartArea.right - chartArea.left, clippedBottom - clippedTop);
          });
        });
        ctx.globalAlpha = 1.0;
        ctx.restore();
      }
    };

    function renderGraphFullscreen_V2(data){
      const canvas = document.getElementById('fullscreen_graph');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      document.getElementById('fullscreen_graph_container').style.display = 'block';

      const win = (data && data.window) || {};
      const xMin = localOrUndef(
        win.since_iso !== undefined ? win.since_iso : win.since_epoch_s
      );
      const xMax = localOrUndef(
        win.until_iso !== undefined ? win.until_iso : win.until_epoch_s
      );

      const datasets = [];
      const series = (data && data.series) || {};
      const avgAll = (data && (data.simple_avg || data.rolling_ema)) || {};
      const keys = Object.keys(series || {});
      let leftAssigned = false;
      const baseColors = ['#1f77b4', '#2ca02c', '#7f3fbf'];
      const gaugeZonesByAxis = {};
      const gaugeAxisBounds = {};

      keys.forEach(function(k, idx){
        const entry = series[k] || {};
        const ts   = entry.ts   || [];
        const vals = entry.vals || [];
        const points = [];
        for (let i = 0; i < ts.length; i++){
          const x = toLocalMs(ts[i]);
          const y = Number(vals[i]);
          if (Number.isFinite(x) && Number.isFinite(y)){
            points.push({ x: x, y: y });
          }
        }
        const yAxisID = leftAssigned ? 'y2' : 'y1';
        if (!leftAssigned) leftAssigned = true;

        const metricZones = soilFertilityGaugeZones(graphMetricNameFromKey(k));
        if (metricZones && !gaugeZonesByAxis[yAxisID]) {
          gaugeZonesByAxis[yAxisID] = metricZones.zones;
          if (Number.isFinite(metricZones.min) && Number.isFinite(metricZones.max)) {
            gaugeAxisBounds[yAxisID] = { min: metricZones.min, max: metricZones.max };
          }
        }

        const baseColor = baseColors[idx % baseColors.length];
        datasets.push({
          label: (data.display_names && data.display_names[k]) || k,
          data: points,
          borderColor: baseColor,
          yAxisID: yAxisID,
          order: 1,
          tension: 0.2,
          pointRadius: (points.length <= 1 ? 3 : 0),
          pointHoverRadius: (points.length <= 1 ? 4 : 3)
        });

        const roll = (avgAll && avgAll[k]) || {};
        const rollTs = roll.ts || [];
        const rollVals = roll.vals || [];
        if (rollTs.length && rollVals.length){
          const rollPoints = [];
          for (let i = 0; i < rollTs.length; i++){
            const x = toLocalMs(rollTs[i]);
            const y = Number(rollVals[i]);
            if (Number.isFinite(x) && Number.isFinite(y)){
              rollPoints.push({ x: x, y: y });
            }
          }
          if (rollPoints.length){
            datasets.push({
              label: ((data.display_names && data.display_names[k]) || k) + " (Average)",
              data: rollPoints,
              borderColor: 'purple',
              borderDash: [6, 3],
              yAxisID: yAxisID,
              order: 2,
              tension: 0.2,
              pointRadius: (rollPoints.length <= 1 ? 3 : 0),
              pointHoverRadius: (rollPoints.length <= 1 ? 4 : 3)
            });
          }
        }
      });

      function isVPDLabel(s){
        return /(^|\b)(ambient\s+)?vpd(\b|$)/i.test(String(s));
      }
      const leftName   = datasets[0] ? (datasets[0].label || '') : '';
      const rightNames = datasets.filter(function(d){ return d.yAxisID === 'y2'; })
                                 .map(function(d){ return d.label; });

      const leftIsVPD  = isVPDLabel(leftName);
      const rightIsVPD = rightNames.some(isVPDLabel);
      const anyVPD     = leftIsVPD || rightIsVPD;

      const legendContainer = document.getElementById('switch_legend');
      if (legendContainer) legendContainer.innerHTML = '';
      const allAnnotations = {};
      if (data && data.switch_lines){
        let colorIdx = 0;
        const pal = [
          { on:'#006400', off:'#8B0000' },
          { on:'#228B22', off:'#B22222' },
          { on:'#2F4F4F', off:'#A0522D' },
          { on:'#008080', off:'#8B4513' }
        ];
        Object.entries(data.switch_lines).forEach(function(pair){
          const label  = pair[0];
          const events = pair[1] || [];
          const colors = pal[colorIdx % pal.length];
          colorIdx += 1;

          if (legendContainer){
            const el = document.createElement('div');
            el.innerHTML =
              "<span style='color:" + colors.on  + "'>&#9632;</span> " +
              label + " (ON), " +
              "<span style='color:" + colors.off + "'>&#9632;</span> OFF";
            legendContainer.appendChild(el);
          }

          events.forEach(function(ev, i){
            const stamp = ev[0];
            const state = ev[1];
            const tMs = toLocalMs(stamp);
            if (!Number.isFinite(tMs)) return;
            const id = label + "_" + (state ? "on" : "off") + "_" + String(i);
            allAnnotations[id] = {
              type: 'line',
              xMin: tMs,
              xMax: tMs,
              borderColor: state ? colors.on : colors.off,
              borderWidth: 2
            };
          });
        });
      }

      function isMidnight(ms){
        const d = new Date(ms);
        return d.getHours() === 0 && d.getMinutes() === 0;
      }
      function fmtDate(ms){
        return new Date(ms).toLocaleDateString(undefined, { month:'short', day:'numeric' });
      }
      function fmtTime(ms){
        return new Date(ms).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
      }

      if (window.graphChart){
        window.graphChart.destroy();
      }

      const axisTitles = {
        y1: (data.axis_titles && data.axis_titles.y1) || 'Left',
        y2: (data.axis_titles && data.axis_titles.y2) ||
            (rightNames.join(' / ') || '')
      };

      const formatYAxisTick = function(val){
        const num = Number(val);
        if (!Number.isFinite(num)) return val;
        return Number(num.toFixed(2)).toString();
      };

      const y1Opts = {
        position: 'left',
        beginAtZero: false,
        title: { display: true, text: axisTitles.y1 },
        ticks: { callback: formatYAxisTick }
      };
      const y2Opts = {
        position: 'right',
        beginAtZero: false,
        title: { display: (keys.length > 1), text: axisTitles.y2 },
        grid: { drawOnChartArea: false },
        display: (keys.length > 1),
        ticks: { callback: formatYAxisTick }
      };

      if (leftIsVPD){
        y1Opts.min = 0;
        y1Opts.max = 5;
      }
      if (rightIsVPD){
        y2Opts.min = 0;
        y2Opts.max = 5;
      }
      if (gaugeAxisBounds.y1){
        y1Opts.min = gaugeAxisBounds.y1.min;
        y1Opts.max = gaugeAxisBounds.y1.max;
      }
      if (gaugeAxisBounds.y2){
        y2Opts.min = gaugeAxisBounds.y2.min;
        y2Opts.max = gaugeAxisBounds.y2.max;
      }

      const annotationPlugin = Chart.registry.getPlugin('annotation');
      const pluginsArr = [vpdBackgroundPlugin, gaugeZonesBackgroundGraph];
      if (annotationPlugin){
        pluginsArr.push(annotationPlugin);
      }

      window.graphChart = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
          parsing: false,
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: {
              type: 'time',
              min: xMin,
              max: xMax,
              time: { unit: 'hour', tooltipFormat: 'PP p' },
              title: {
                display: true,
                text: (typeof TZ_NAME === 'string' && TZ_NAME)
                  ? ('Time (' + TZ_NAME + ')')
                  : 'Time'
              },
              ticks: {
                source: 'auto',
                autoSkip: true,
                maxRotation: 0,
                callback: function(val, idx, ticks){
                  const tval = (ticks[idx] && ('value' in ticks[idx]))
                    ? ticks[idx].value
                    : val;
                  return isMidnight(tval) ? fmtDate(tval) : fmtTime(tval);
                }
              },
              grid: {
                color: function(c){
                  const v = c && c.tick ? c.tick.value : undefined;
                  return isMidnight(v) ? 'rgba(0,0,0,0.25)' : 'rgba(0,0,0,0.1)';
                },
                lineWidth: function(c){
                  const v = c && c.tick ? c.tick.value : undefined;
                  return isMidnight(v) ? 1.5 : 1;
                }
              }
            },
            y1: y1Opts,
            y2: y2Opts
          },
          plugins: {
            annotation: { annotations: allAnnotations },
            vpdZones: { enabled: anyVPD },
            gaugeZonesBackgroundGraph: { zonesByAxis: gaugeZonesByAxis }
          }
        },
        plugins: pluginsArr
      });
    }

    function closeFullscreenGraph(){
      const cont = document.getElementById('fullscreen_graph_container');
      if (cont) cont.style.display = 'none';
      if (window.graphChart){
        window.graphChart.destroy();
        window.graphChart = null;
      }
    }

    window.openGraphModal = async function(){
      window.ModalBusyCursor.begin();
      try {
        const gm = document.getElementById('graphModal');
        if (gm){
          gm.style.display = 'flex';
          if (window.ensureButtonTooltips) window.ensureButtonTooltips(gm);
          await window.ModalBusyCursor.untilPaint();
          await initGraphBuilder();
        }
      } finally {
        window.ModalBusyCursor.end();
      }
    };
    """
    yield "</script>"
