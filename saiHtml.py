"""HTML rendering helpers and shared UI constants."""
import os
import re
from saiUtils import printDM, debug_enabled, html_escape, normalize_hostname_base, mdns_hostname
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

def get_gauge_config():
    gauge_config = {
        "Air Quality": {"unit": "AQI", "min": 0, "max": 500, "ticks": [0, 50, 100, 150, 200, 300, 400, 500], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 50}, {"strokeStyle": "#ffcc00", "min": 50, "max": 100}, {"strokeStyle": "#ffa500", "min": 100, "max": 150}, {"strokeStyle": "#ff0000", "min": 150, "max": 200}, {"strokeStyle": "#800080", "min": 200, "max": 300}, {"strokeStyle": "#800000", "min": 300, "max": 500}]},
        "Gas": {"unit": "Ω", "min": 500, "max": 2000500, "ticks": [500, 500500, 1000500, 1500500, 2000500], "zones": [{"strokeStyle": "#f3d2fc", "min": 500, "max": 2000500}]},
        "CO2": {"unit": "ppm", "min": 0, "max": 3000, "ticks": [0, 200, 400, 800, 1200, 1600, 2000, 3000], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 200}, {"strokeStyle": "#ffcc00", "min": 200, "max": 400}, {"strokeStyle": "#66cc66", "min": 400, "max": 1600}, {"strokeStyle": "#ffcc00", "min": 1600, "max": 2000}, {"strokeStyle": "#f00", "min": 2000, "max": 3000}]},
        "Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Dew-Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Dew-Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Dewpoint Depression": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Ambient VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Baro-Pressure": {"unit": "hPa", "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
        "Temperature_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Temperature": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Rel-Humidity": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 30}, {"strokeStyle": "#add8e6", "min": 30, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Plant Humidity": {"unit": "g/m³", "min": 0, "max": 130, "ticks": [0, 26, 52, 78, 104, 130], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 26}, {"strokeStyle": "#ffcc00", "min": 26, "max": 52}, {"strokeStyle": "#add8e6", "min": 52, "max": 78}, {"strokeStyle": "#66b2ff", "min": 78, "max": 104}, {"strokeStyle": "#0033cc", "min": 104, "max": 130}]},
        "Plant Dew-Point": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Plant Dew-Point_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Plant Dewpoint Depression": {"unit": "°C", "min": 0, "max": 30, "ticks": [0, 5, 10, 15, 20, 25, 30], "zones": [{"strokeStyle": "#0033cc", "min": 0, "max": 2}, {"strokeStyle": "#66cc66", "min": 2, "max": 8}, {"strokeStyle": "#ffcc00", "min": 8, "max": 15}, {"strokeStyle": "#f00", "min": 15, "max": 30}]},
        "Plant DewVPD Risk": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#66cc66", "min": 0, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 60}, {"strokeStyle": "#bf9000", "min": 60, "max": 100}]},
        "Plant VPD": {"unit": "kPa", "min": 0.0, "max": 5.0, "ticks": [0, 0.4, 0.8, 1.2, 1.6, 2, 3, 4, 5], "zones": [{"strokeStyle": "#0033cc", "min": 0.0, "max": 0.4}, {"strokeStyle": "#66cc66", "min": 0.4, "max": 0.8}, {"strokeStyle": "#03a603", "min": 0.8, "max": 1.2}, {"strokeStyle": "#3e803e", "min": 1.2, "max": 1.6}, {"strokeStyle": "#bf9000", "min": 1.6, "max": 5.0}]},
        "Plant Baro-Pressure": {"unit": "hPa", "min": 700, "max": 1100, "ticks": [700, 750, 800, 850, 900, 950, 1000, 1050, 1100], "zones": [{"strokeStyle": "#add8e6", "min": 700, "max": 1100}]},
        "Plant Temperature_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Soil-Moisture": {"unit": "%", "min": 0, "max": 100, "ticks": [0, 20, 40, 60, 80, 100], "zones": [{"strokeStyle": "#bf9000", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 50}, {"strokeStyle": "#add8e6", "min": 50, "max": 70}, {"strokeStyle": "#66b2ff", "min": 70, "max": 80}, {"strokeStyle": "#0033cc", "min": 80, "max": 100}]},
        "Soil-Temp": {"unit": "°C", "min": -20, "max": 60, "ticks": [-20, 0, 10, 20, 30, 40, 60], "zones": [{"strokeStyle": "#00f", "min": -20, "max": 0}, {"strokeStyle": "#3399ff", "min": 0, "max": 10}, {"strokeStyle": "#66cc66", "min": 10, "max": 30}, {"strokeStyle": "#ffcc00", "min": 30, "max": 40}, {"strokeStyle": "#f00", "min": 40, "max": 60}]},
        "Soil-Temp_F": {"unit": "°F", "min": 0, "max": 140, "ticks": [0, 32, 50, 70, 90, 110, 140], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 32}, {"strokeStyle": "#3399ff", "min": 32, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 86}, {"strokeStyle": "#ffcc00", "min": 86, "max": 104}, {"strokeStyle": "#f00", "min": 104, "max": 140}]},
        "Soil-pH": {"unit": "pH", "min": 0, "max": 10, "ticks": [1, 3, 5, 7, 9], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 4.5}, {"strokeStyle": "#3399ff", "min": 4.5, "max": 5.5}, {"strokeStyle": "#66cc66", "min": 5.5, "max": 6.5}, {"strokeStyle": "#ffcc00", "min": 6.5, "max": 7.5}, {"strokeStyle": "#f00", "min": 7.5, "max": 8.5}, {"strokeStyle": "#800000", "min": 8.5, "max": 10}]},
        "Soil-EC": {"unit": "mS/cm", "min": 0, "max": 10, "ticks": [0, 2, 4, 6, 8, 10], "zones": [{"strokeStyle": "#00f", "min": 0, "max": 0.8}, {"strokeStyle": "#3399ff", "min": 0.8, "max": 1.8}, {"strokeStyle": "#66cc66", "min": 1.8, "max": 2.5}, {"strokeStyle": "#ffcc00", "min": 2.5, "max": 4.0}, {"strokeStyle": "#800000", "min": 4.0, "max": 10}]},
        "Soil-N": {"unit": "mg/kg", "min": 0, "max": 150, "ticks": [0, 25, 50, 75, 100, 125, 150], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 25}, {"strokeStyle": "#ffcc00", "min": 25, "max": 50}, {"strokeStyle": "#66cc66", "min": 50, "max": 125}, {"strokeStyle": "#3399ff", "min": 125, "max": 150}]},
        "Soil-P": {"unit": "mg/kg", "min": 0, "max": 60, "ticks": [0, 20, 30, 40, 50, 60], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 20}, {"strokeStyle": "#ffcc00", "min": 20, "max": 36}, {"strokeStyle": "#66cc66", "min": 36, "max": 50}, {"strokeStyle": "#3399ff", "min": 50, "max": 60}]},
        "Soil-K": {"unit": "mg/kg", "min": 0, "max": 200, "ticks": [0, 60, 100, 130, 150, 175, 200], "zones": [{"strokeStyle": "#f00", "min": 0, "max": 60}, {"strokeStyle": "#ffcc00", "min": 60, "max": 131}, {"strokeStyle": "#66cc66", "min": 131, "max": 175}, {"strokeStyle": "#3399ff", "min": 175, "max": 200}]},
        "Light Intensity": {"unit": "lux", "min": 0,  "max": 120000, "ticks": [0, 20000, 40000, 60000, 80000, 100000, 120000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 120000}]},
        "Auto Light": {"unit": "lux", "min": 0,  "max": 120000, "ticks": [0, 20000, 40000, 60000, 80000, 100000, 120000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 120000}]},
        "PPFD": {"unit": "µmol·m⁻²·s⁻¹", "min": 0, "max": 2000, "ticks": [0, 400, 800, 1200, 1600, 2000], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 2000}]},
        "DLI": {"unit": "mol·m⁻²·day⁻¹", "min": 0, "max": 70, "ticks": [0, 10, 20, 30, 40, 50, 60, 70], "zones": [{"strokeStyle": "#ffff00", "min": 0, "max": 70}]},
    }
    return gauge_config

def render_dashboard(sensor_id, sensor, available, all_values, all_stats, mqtt_ingest, switch_controllers=None, sensor_locations=None, gauge_config=None, gauge_size="Small", expected_gauge_map=None, display_style=None):

    import json
    import os
    import re
    import sys
    import math
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from collections import defaultdict
    from saiUtils import get_timestamp
    from saiSettings import saiSettings
    import saiAddDevice
    from saiSensorSettingsManager import SensorSettingsManager
    from saiHtml import render_graph_modal
    try:
        import httpx
    except Exception:
        httpx = None
    try:
        from astral import LocationInfo
        from astral.sun import sun as _astral_sun, elevation as _astral_elevation
        from astral import moon as _astral_moon
    except Exception:
        LocationInfo = None
        _astral_sun = None
        _astral_elevation = None
        _astral_moon = None
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

    def _safe_float(v):
        try:
            return float(v)
        except Exception:
            return None

    def _moon_phase_name(phase_val: float) -> str:
        p = phase_val % 28.0

        def _circular_dist(a: float, b: float, cycle: float = 28.0) -> float:
            d = abs(a - b) % cycle
            return min(d, cycle - d)

        if _circular_dist(p, 0.0) <= 1.0:
            return "New Moon"
        if _circular_dist(p, 7.0) <= 1.0:
            return "1st Quarter"
        if _circular_dist(p, 14.0) <= 1.0:
            return "Full Moon"
        if _circular_dist(p, 21.0) <= 1.0:
            return "3rd Quarter"
        if 1.0 < p < 6.0:
            return "Waxing Crescent"
        if 8.0 < p < 13.0:
            return "Waxing Gibbous"
        if 15.0 < p < 20.0:
            return "Waning Gibbous"
        return "Waning Crescent"

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
            "moon_phase_value": None,
            "moon_phase_label": "",
        }
        if LocationInfo is None or _astral_sun is None or _astral_elevation is None or _astral_moon is None:
            return out

        resolved_lat = None
        resolved_lon = None
        resolved_tz = ""
        try:
            s = saiSettings(apply_live=False)
            resolved_tz = str(s.get_setting("Astral", "TIMEZONE", "") or "").strip() or str(s.get_setting("Time", "TZ", "") or "").strip()
            cfg_lat = _safe_float(s.get_setting("Astral", "LATITUDE", ""))
            cfg_lon = _safe_float(s.get_setting("Astral", "LONGITUDE", ""))
            if cfg_lat is not None and cfg_lon is not None and -90.0 <= cfg_lat <= 90.0 and -180.0 <= cfg_lon <= 180.0:
                resolved_lat = cfg_lat
                resolved_lon = cfg_lon
            else:
                auto_ip_raw = s.get_setting("Astral", "AUTO_IP", True)
                auto_ip = str(auto_ip_raw).strip().lower() in {"1", "true", "yes", "on"} if isinstance(auto_ip_raw, str) else bool(auto_ip_raw)
                if auto_ip and httpx is not None:
                    try:
                        with httpx.Client(timeout=2.5) as client:
                            resp = client.get("https://ipapi.co/json/")
                        if resp.status_code == 200:
                            payload = resp.json() or {}
                            ip_lat = _safe_float(payload.get("latitude"))
                            ip_lon = _safe_float(payload.get("longitude"))
                            ip_tz = str(payload.get("timezone", "") or "").strip()
                            if ip_lat is not None and ip_lon is not None:
                                if -90.0 <= ip_lat <= 90.0 and -180.0 <= ip_lon <= 180.0:
                                    resolved_lat = ip_lat
                                    resolved_lon = ip_lon
                                    if ip_tz:
                                        resolved_tz = ip_tz
                    except Exception:
                        pass
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
            sun_map = _astral_sun(obs, date=now_local.date(), tzinfo=tzinfo)
            sunrise = sun_map.get("sunrise")
            sunset = sun_map.get("sunset")
            noon = sun_map.get("noon")
            if not isinstance(sunrise, datetime) or not isinstance(sunset, datetime):
                return out

            pts = []
            cur = sunrise
            while cur <= sunset:
                try:
                    elev = float(_astral_elevation(obs, cur))
                except Exception:
                    elev = float("nan")
                if math.isfinite(elev):
                    pts.append({"t": cur.strftime("%H:%M"), "e": round(elev, 2)})
                cur = cur + timedelta(minutes=5)
            if pts and pts[-1]["t"] != sunset.strftime("%H:%M"):
                try:
                    elev_sunset = float(_astral_elevation(obs, sunset))
                except Exception:
                    elev_sunset = 0.0
                pts.append({"t": sunset.strftime("%H:%M"), "e": round(elev_sunset, 2)})

            moon_val = float(_astral_moon.phase(now_local.date()))
            out.update({
                "ok": True,
                "lat": round(resolved_lat, 6),
                "lon": round(resolved_lon, 6),
                "tz": resolved_tz,
                "sunrise": sunrise.strftime("%H:%M"),
                "sunset": sunset.strftime("%H:%M"),
                "sun_noon": noon.strftime("%H:%M") if isinstance(noon, datetime) else "",
                "sun_points": pts,
                "moon_phase_value": round(moon_val, 2),
                "moon_phase_label": _moon_phase_name(moon_val),
            })
            return out
        except Exception:
            return out

    astro_payload = _build_astro_payload()
    
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
    if switch_controllers:
        local_iter = switch_controllers.values() if isinstance(switch_controllers, dict) else [switch_controllers]
        for ctrl in local_iter:
            try:
                if not getattr(ctrl, "is_present", False):
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
    yield "<script type='module' src='/ui_static/js/advanced_automation.js'></script>"
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
    mgr = SensorSettingsManager("sensor_settings")
    sensor_lookup = {s.lower(): s for s in mgr.list_ids()}
    for sid in available:
        normalized = sid.lower()
        if normalized not in sensor_lookup:
            sensor_lookup[normalized] = sid  # include MQTT-only sensors

    if sensor_id:
        normalized_sensor_id = sensor_id.lower()
        actual_sensor_id = sensor_lookup.get(normalized_sensor_id)
        has_sensor_toml = actual_sensor_id is not None
    else:
        actual_sensor_id = None
        has_sensor_toml = False

    sensor_display_map = {}
    for sid in all_values:
        try:
            normalized_id = sid.lower()
            actual_id = sensor_lookup.get(normalized_id)
            if actual_id:
                try:
                    metrics = mgr.get_display_metrics(actual_id)
                except Exception:
                    metrics = list(gauge_config.keys())  # fallback to default gauges
                sensor_display_map[sid] = metrics
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
          3) Fallback: 'pending'
        Returns one of: 'online' | 'offline' | 'pending'
        """
        # 1) direct/local sensor object
        try:
            sensor_obj = _active_sensor_for(sid)
            st = getattr(getattr(sensor_obj, "sensor", sensor_obj), "meas_status", None)
            if isinstance(st, str) and st.strip().lower() in {"online", "offline", "pending"}:
                return st.strip().lower()
        except Exception:
            pass

        # 2) mqtt-ingested remote
        try:
            for host in _hostname_variants_from_sid(sid):
                st = (getattr(mqtt_ingest, "device_status", {}) or {}).get(host)
                if isinstance(st, str) and st.strip().lower() in {"online", "offline", "pending"}:
                    return st.strip().lower()
        except Exception:
            pass

        # 3) fallback
        return "pending"

    def _status_color_hex(status: str) -> str:
        """
        Map status -> color (matches your CSS palette):
          pending = yellow, online = green, offline = red
        """
        s = (status or "").strip().lower()
        if s == "online":
            return "#28a745"  # green
        if s == "offline":
            return "#dc3545"  # red
        return "#ffc107"      # yellow (pending)

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
    yield "    <svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' role='img' aria-label='Settings' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    yield "      <!-- outer ring -->"
    yield "      <circle cx='12' cy='12' r='7'/>"
    yield "      <!-- teeth (flat) -->"
    yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
    yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
    yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
    yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
    yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "      <!-- hub -->"
    yield "      <circle cx='12' cy='12' r='2.25'/>"
    yield "    </svg>"
    yield "</a>"
    yield "</h2>"
  
    yield "<p id='update_time'>--</p>"

    yield "<style>"
    yield ".dash-top-row{display:flex;justify-content:center;align-items:stretch;gap:.75rem;flex-wrap:wrap;margin-top:1rem;}"
    yield ".dash-loc-form{display:flex;flex-direction:column;align-items:stretch;justify-content:flex-start;gap:.45rem;background:#e6faff;border:1px solid #c9ddff;border-radius:10px;padding:.45rem .65rem .55rem;min-height:102px;min-width:172px;width:172px;}"
    yield ".dash-loc-head{display:flex;align-items:center;justify-content:space-between;gap:.1rem;}"
    yield ".dash-loc-label{font-size:.78rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;opacity:.85;}"
    yield ".astro-box{display:flex;align-items:flex-start;justify-content:flex-start;background:#ffffe0;border:1px solid #ccc;border-radius:10px;padding:.45rem .55rem;min-height:102px;}"
    yield ".dash-loc-form select{background:#ffffe0;border:1px solid #ccc;}"
    yield ".astro-card{display:flex;flex-direction:column;align-items:center;gap:.2rem;min-width:132px;}"
    yield ".astro-title{font-size:.78rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;opacity:.8;}"
    yield ".astro-meta{font-size:.74rem;line-height:1.25;text-align:center;color:#27313a;min-height:1.9em;white-space:normal;}"
    yield "#sunBox .astro-card{min-width:230px;}"
    yield "#moonBox .astro-card{min-width:230px;}"
    yield "#moonMeta{white-space:nowrap;padding:0 10px;}"
    yield ".astro-times{width:210px;display:flex;justify-content:space-between;gap:.35rem;font-variant-numeric:tabular-nums;}"
    yield ".astro-times span{display:inline-block;min-width:0;}"
    yield "#sunPathCanvas{width:210px;height:96px;border:1px solid #d5c7a8;border-radius:8px;background:#dff1ff;}"
    yield "#moonPhaseCanvas{width:96px;height:96px;border:1px solid #d5c7a8;border-radius:50%;background:#081322;}"
    yield "@media (max-width: 760px){#sunPathCanvas{width:184px;height:86px}.astro-times{width:184px}.astro-card{min-width:120px}.dash-loc-form,.astro-box{min-height:unset}}"
    yield "</style>"

    yield "<div class='dash-top-row'>"
    yield "<div class='astro-box' id='sunBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Sun Position</div>"
    yield "    <canvas id='sunPathCanvas' width='156' height='96'></canvas>"
    yield "    <div class='astro-meta astro-times' id='sunMeta'><span id='sunTimeRise'>--</span><span id='sunTimeNoon'>--</span><span id='sunTimeSet'>--</span></div>"
    yield "  </div>"
    yield "</div>"
    yield "<form method='get' class='dash-loc-form'>"
    yield "<div class='dash-loc-head'>"
    yield "  <div class='dash-loc-label'>Device Locations</div>"
    yield "  <a id='refresh_link' class='refresh-link' href='/' title='Refresh dashboard' aria-label='Refresh dashboard'>⟳</a>"
    yield "</div>"
    yield "<select name='sensor_id' id='sensor_id' onchange='this.form.submit()' style='background-color:#ffffe0;'>"
    # treat any non 'loc:*' as All (back-compat: direct sensor ids will land here)
    is_loc_filter = isinstance(sensor_id, str) and sensor_id.startswith("loc:")
    yield f"<option value='All' {'selected' if (not is_loc_filter or sensor_id == 'All') else ''}>All Locations</option>"
    for norm, disp in known_items:
        val = f"loc:{disp}"
        sel = "selected" if sensor_id == val else ""
        yield f"<option value='{val}' {sel}>{disp}</option>"
    yield "</select>"
    yield "</form>"
    yield "<div class='astro-box' id='moonBox' aria-live='polite'>"
    yield "  <div class='astro-card'>"
    yield "    <div class='astro-title'>Moon Phase</div>"
    yield "    <canvas id='moonPhaseCanvas' width='96' height='96'></canvas>"
    yield "    <div class='astro-meta' id='moonMeta'>Loading moon data...</div>"
    yield "  </div>"
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

        yield f"<div style='text-align:center; width:100%; margin-top:1rem;'>"
  
        yield f"<h3 id='{sid}_header'>"      
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
        yield "    <svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' role='img' aria-label='Settings' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        yield "      <!-- outer ring -->"
        yield "      <circle cx='12' cy='12' r='7'/>"
        yield "      <!-- teeth (flat) -->"
        yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
        yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
        yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
        yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
        yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
        yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
        yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
        yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
        yield "      <!-- hub -->"
        yield "      <circle cx='12' cy='12' r='2.25'/>"
        yield "    </svg>"
        yield "  </a>"
        yield (f"{location}")

        yield "</h3>"
        yield "</div>"
        yield f"<div class='sensor-row' id='row_{sid}'>"

        # build out the gauges for this sensor based on its configured display metrics; if none, show all available gauges
        for metric in sensor_metrics:
            if metric not in gauge_config:
                continue
            config = gauge_config[metric]
            val = values.get(metric)
            if val is None:
                for k in values.keys():
                    if k.lower().replace("-", "").replace("_", "") == metric.lower().replace("-", "").replace("_", ""):
                        val = values[k]
                        break

            stat = stats.get(metric, {})
            display_val = val if val is not None else "--"

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

            yield f"<div class='metric-container' id='{safe_id}_container' data-sensor='{sid}' data-metric='{metric}'>"
            yield f"<div class='metric-title'>{metric} ({config['unit']})</div>"

            yield "<div class='gauge-container'>"
            yield f"<div class='gauge-view'><canvas id='{safe_id}Gauge'></canvas></div>"
            yield "</div>"

            yield "<div class='graph-container'>"
            yield f"<div class='graph-view'>"
            yield f"<canvas class='micrograph-canvas' width='{layout['canvas_width']}' height='{layout['canvas_height']}'></canvas>"
            yield "</div>"
            yield "</div>"

            yield f"<div class='metric-current-value' id='{safe_id}_val'>{display_val}</div>"

            yield f"<div class='metric-stats' id='{safe_id}_stats'>"

            yield f"<div>Min<br><small>{min_val} at<br>{min_ts}</small></div>"
            yield f"<div>Avg<br>{avg_val}</div>"
            yield f"<div>Max<br><small>{max_val} at<br>{max_ts}</small></div>"

            yield "</div>"  # metric-stats
            yield "</div>"  # metric-container

        matched_switches = switches_by_loc.get(_norm_loc(location), [])

        if DEBUG and matched_switches:
            printDM(f"[render_dashboard] {sid} @ '{location}' matched {len(matched_switches)} switch controller(s)", location="saiHtml")

        # ── render-time dedupe: ensure each switch_id renders at most once per location ──
        _rendered_swids_here: set[str] = set()

        for switch_ctrl in matched_switches:
            # Normalize id for dedupe (case-insensitive); also keep raw for lookups/UI
            sw_id: str = (getattr(switch_ctrl, "switch_id", "") or "").strip()
            sw_id_key: str = sw_id.lower() if sw_id else "__no_switch_id__"

            if sw_id_key in _rendered_swids_here:
                if DEBUG:
                    printDM(f"[render_dashboard] skip duplicate render of switch '{sw_id}' in location '{location}'", location="saiHtml")
                continue
            _rendered_swids_here.add(sw_id_key)

            # ------ decide which labels to render (prefer on-disk truth) ------
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
                    # Pico2 W: *_ENABLE_PIN indicates channel installed
                    pairs = [("SWITCH_1_LABEL", 1), ("SWITCH_2_LABEL", 2)]
                    tmp = []
                    for lbl_key, idx in pairs:
                        label = (sw_blk.get(lbl_key) or "").strip()
                        en_val = _enable_field_value(sw_blk, idx)
                        enabled = _has_install_marker(en_val)
                        if label and enabled:
                            tmp.append(label)
                    if tmp:
                        render_labels = tmp
                else:
                    # Pi: require BOTH a label and an integer PIN to render the channel
                    tmp = []
                    for n in range(1, 33):
                        label = (str(sw_blk.get(f"SWITCH_{n}_LABEL", "") or "").strip())
                        pin   = sw_blk.get(f"SWITCH_{n}_PIN", None)
                        if not label:
                            continue
                        if isinstance(pin, (int, float)):
                            tmp.append(label)
                    if tmp:
                        render_labels = tmp

            except Exception:
                # fall back below
                pass

            # Final fallback: whatever the controller reported (may be stale)
            if not render_labels:
                render_labels = list(getattr(switch_ctrl, "switches", []))
            # If the controller only reports generic relay placeholders, prefer
            # discovery/DB-derived labels for this switch_id.
            try:
                is_generic_only = bool(render_labels) and all(
                    re.match(r"(?i)^relay\s+\d+$", str(lbl or "").strip()) for lbl in render_labels
                )
            except Exception:
                is_generic_only = False
            if (not render_labels) or is_generic_only:
                try:
                    discovered_map = dict(discovered_switches.get(sw_id, {}) or {})
                    db_map = _db_label_map_for_switch(sw_id)
                    enabled_map = _channel_map_from_switch_settings(sw_id)
                    candidate_map: dict[str, str] = {}
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
                    if candidate_map:
                        render_labels = list(candidate_map.keys())
                except Exception:
                    pass

            # ----------------------------------------------------------------------
            yield "<div class='switch-metric-container'>"
            yield f"<div style='text-align:center; width:100%; margin-top:-1.5rem; margin-bottom:-1.0rem;'>"
            yield f"<h3 id='{sw_id}_header'>{sw_id.upper()} "
            yield f"  <a href='javascript:void(0)' onclick='editSwitchSettings(\"{sw_id}\")' title='Open {sw_id} Settings' style='margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
            yield "    <svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' role='img' aria-label='Settings' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
            yield "      <circle cx='12' cy='12' r='7'/>"
            yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
            yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
            yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
            yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
            yield "      <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
            yield "      <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
            yield "      <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
            yield "      <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
            yield "      <circle cx='12' cy='12' r='2.25'/>"
            yield "    </svg>"
            yield "  </a>"
            yield f"{switch_ctrl.location}</h3>"
            yield "</div>"

            yield "<div class='switch-container'>"
            yield "<div class='switch-view'>"

            yield "<table class='switch-table'>"
            yield "<thead><tr>"
            yield "<th>Switch</th><th>State</th><th>Automation</th><th>Events</th>"
            yield "</tr></thead>"
            yield "<tbody>"

            # if nothing is enabled for a Pico2 W, show a friendly row
            if not render_labels:
                yield "<tr><td colspan='4' style='opacity:0.7;'>No enabled switch channels</td></tr>"

            for label in render_labels:
                safe_label = label.lower().replace(" ", "_")
                is_on = bool(getattr(switch_ctrl, "last_state", {}).get(label, False))
                state_str = "on" if is_on else "off"
                current_state_text = " ON" if is_on else "OFF"
                override_enabled = bool(getattr(switch_ctrl, "override_script", {}).get(label, False))
                checked_attr = "checked" if override_enabled else ""
                last_time_str = getattr(switch_ctrl, "last_set_time", {}).get(label, "")

                # Prefer channel_id for action payload when available; otherwise use switch_id.
                channel_id = ""
                try:
                    channel_id = str((getattr(switch_ctrl, "channel_id_for_label", {}) or {}).get(label, "") or "").strip()
                except Exception:
                    channel_id = ""
                action_sid = channel_id or sw_id
                switch_key = f"{action_sid}::{label}" if action_sid else f"::{label}"
                box_id  = f"{sw_id}-{safe_label}_box" if sw_id else f"{safe_label}_box"
                state_id= f"{sw_id}-{safe_label}_state" if sw_id else f"{safe_label}_state"
                time_id = f"{sw_id}-{safe_label}_time"  if sw_id else f"{safe_label}_time"
                
                yield "<tr>"
                yield f"<td>{label}</td>"
                # Switch cell
                yield "<td>"
                yield (
                    f"<button "
                    f"  id='{box_id}_btn' "
                    f"  class='button {'green' if is_on else 'black'}' "
                    f"  title='Toggle state for {label}' "
                    f"  data-switch-name='{label}' "
                    f"  data-switch-key='{switch_key}' "
                    f"  data-switch-id='{action_sid}' "
                    f"  data-state='{state_str}' "
                    f"  onclick='toggleSwitchInline(this)'>"
                    f"{'On' if is_on else 'Off'}"
                    f"</button>"
                )
                yield "</td>"

                # Override checkbox cell
                # --- Automation button cell (replaces previous checkbox) ---
                label_norm = (label or "").strip()
                safe_key   = _safe(f"{getattr(switch_ctrl,'switch_id','')}_{label_norm}_automation")

                try:
                    from saiAutomationManager import AutomationManager
                    am  = AutomationManager()
                    sid = getattr(switch_ctrl, "switch_id", "") or ""
                    switch_key_full = f"{sid}::{label_norm}" if sid else f"::{label_norm}"
                    rule_enabled = am.get_advanced_enabled_for_switch_key(sid, switch_key_full)
                except Exception:
                    rule_enabled = False

                enabled = bool(rule_enabled)


                # Button shows Enabled/Disabled and uses our existing .button .green/.black styles
                yield "<td>"
                yield (
                    f'<button '
                    f'  id="{safe_key}_btn" '
                    f'  class="button automation-enabled-btn {"green" if enabled else "black"}" '
                    f'  data-switch-id="{getattr(switch_ctrl, "switch_id", "")}" '
                    f'  data-label="{label_norm}" '
                    f'  title="Enable/Disable automation for {label_norm}" '
                    f'  onclick="toggleAutomation(this, {json.dumps(getattr(switch_ctrl, "switch_id", ""))!s}, {json.dumps(label_norm)!s}); return false;">'
                    f'{"Enabled" if enabled else "Disabled"}'
                    f'</button>'
                )
                yield "</td>"

                yield "<td>"
                yield f"  <div id='{safe_label}_events' class='switch-events' role='listbox' aria-label='Recent switch events'>"
                yield f"    <ul id='{safe_label}_events_list' class='switch-events-list' data-switch-key='{switch_key}'></ul>"
                yield f"  </div>"
                yield "</td>"
                yield "</tr>"                
                
            yield "</tbody>"
            yield "</table>"
            yield "</div>"  # .switch-view
            yield "</div>"  # .switch-container
            yield "</div>"  # .switch-metric-container

        yield "</div>"  # sensor-row

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
    yield f"const astroData = {json.dumps(astro_payload)};"
    yield f"const isPiPlatform = {str(is_pi_platform).lower()};"
    yield "const lastTimestamps = {};"
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

    yield "function updateLocalTime() {"
    yield "  const ts = document.getElementById('update_time');"
    yield "  if (ts) {"
    yield "    const now = new Date();"
    yield "    const formatted = now.toLocaleString('en-CA', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' }).replace(',', '');"
    yield "    ts.textContent = formatted;"
    yield "  }"
    yield "}"

    yield "function drawSunPath(data){"
    yield "  const c = document.getElementById('sunPathCanvas');"
    yield "  const meta = document.getElementById('sunMeta');"
    yield "  const riseEl = document.getElementById('sunTimeRise');"
    yield "  const noonEl = document.getElementById('sunTimeNoon');"
    yield "  const setEl = document.getElementById('sunTimeSet');"
    yield "  if (!c || !meta || !riseEl || !noonEl || !setEl) return;"
    yield "  const ctx = c.getContext('2d');"
    yield "  ctx.clearRect(0,0,c.width,c.height);"
    yield "  if (!data || !data.ok || !Array.isArray(data.sun_points) || data.sun_points.length < 2){"
    yield "    riseEl.textContent = '--'; noonEl.textContent = '--'; setEl.textContent = '--';"
    yield "    return;"
    yield "  }"
    yield "  const fmtSun = (hhmm) => {"
    yield "    const m = String(hhmm || '').match(/^(\\d{1,2}):(\\d{2})$/);"
    yield "    if (!m) return '--';"
    yield "    const hh = parseInt(m[1], 10);"
    yield "    const mm = m[2];"
    yield "    const ap = hh < 12 ? 'A' : 'P';"
    yield "    const h12 = (hh % 12) || 12;"
    yield "    return `${h12}:${mm}${ap}`;"
    yield "  };"
    yield "  const pts = data.sun_points;"
    yield "  let minE = Infinity, maxE = -Infinity;"
    yield "  for (const p of pts){ if (typeof p.e === 'number'){ minE=Math.min(minE,p.e); maxE=Math.max(maxE,p.e);} }"
    yield "  if (!Number.isFinite(minE) || !Number.isFinite(maxE)){ riseEl.textContent='--'; noonEl.textContent='--'; setEl.textContent='--'; return; }"
    yield "  if (Math.abs(maxE-minE) < 0.001){ maxE = minE + 1; }"
    yield "  const padX = 8, padY = 8;"
    yield "  const w = c.width - padX*2, h = c.height - padY*2;"
    yield "  ctx.strokeStyle = '#8fa4b3'; ctx.lineWidth = 1;"
    yield "  ctx.beginPath(); ctx.moveTo(padX, c.height-padY); ctx.lineTo(c.width-padX, c.height-padY); ctx.stroke();"
    yield "  ctx.strokeStyle = '#7ec8ff'; ctx.lineWidth = 2;"
    yield "  ctx.beginPath();"
    yield "  pts.forEach((p, i) => {"
    yield "    const x = padX + (i/(pts.length-1))*w;"
    yield "    const y = padY + (1-((p.e-minE)/(maxE-minE)))*h;"
    yield "    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);"
    yield "  });"
    yield "  ctx.stroke();"
    yield "  const now = new Date();"
    yield "  const hh = String(now.getHours()).padStart(2,'0');"
    yield "  const mm = String(now.getMinutes()).padStart(2,'0');"
    yield "  const cur = `${hh}:${mm}`;"
    yield "  let idx = 0;"
    yield "  for (let i=0;i<pts.length;i++){ if (pts[i].t <= cur) idx = i; }"
    yield "  const xNow = padX + (idx/(pts.length-1))*w;"
    yield "  const yNow = padY + (1-((pts[idx].e-minE)/(maxE-minE)))*h;"
    yield "  ctx.fillStyle = '#ffff00'; ctx.beginPath(); ctx.arc(xNow, yNow, 3.84, 0, Math.PI*2); ctx.fill(); ctx.strokeStyle = '#ff8c00'; ctx.lineWidth = 1; ctx.stroke();"
    yield "  riseEl.textContent = fmtSun(data.sunrise);"
    yield "  noonEl.textContent = fmtSun(data.sun_noon);"
    yield "  setEl.textContent = fmtSun(data.sunset);"
    yield "}"

    yield "function drawMoonPhase(data){"
    yield "  const c = document.getElementById('moonPhaseCanvas');"
    yield "  const meta = document.getElementById('moonMeta');"
    yield "  if (!c || !meta) return;"
    yield "  const ctx = c.getContext('2d');"
    yield "  ctx.clearRect(0,0,c.width,c.height);"
    yield "  if (!data || !data.ok || typeof data.moon_phase_value !== 'number'){"
    yield "    meta.textContent = 'Moon data unavailable';"
    yield "    return;"
    yield "  }"
    yield "  const w = c.width, h = c.height;"
    yield "  const r = Math.min(w, h) / 2 - 1;"
    yield "  const x = w / 2, y = h / 2;"
    yield "  const phase = ((data.moon_phase_value % 28) + 28) % 28;"
    yield "  const illum = 0.5 * (1 - Math.cos((2*Math.PI*phase)/28));"
    yield "  const lat = Number(data.lat || 0);"
    yield "  const hemisphereFlip = lat < 0 ? -1 : 1;"
    yield "  const image = ctx.createImageData(w, h);"
    yield "  const pix = image.data;"
    yield "  const phaseAngle = (2 * Math.PI * phase) / 28;"
    yield "  const sx = Math.sin(phaseAngle) * hemisphereFlip;"
    yield "  const sz = -Math.cos(phaseAngle);"
    yield "  for (let py = 0; py < h; py++) {"
    yield "    for (let px = 0; px < w; px++) {"
    yield "      const dx = (px + 0.5 - x) / r;"
    yield "      const dy = (py + 0.5 - y) / r;"
    yield "      const rr = dx*dx + dy*dy;"
    yield "      const off = (py * w + px) * 4;"
    yield "      if (rr > 1) { pix[off+3] = 0; continue; }"
    yield "      const dz = Math.sqrt(Math.max(0, 1 - rr));"
    yield "      const dot = dx * sx + dz * sz;"
    yield "      const lit = Math.max(0, dot);"
    yield "      const earthshine = 0.08;"
    yield "      const shade = Math.pow(Math.min(1, lit + earthshine), 0.72);"
    yield "      const baseR = 9, baseG = 18, baseB = 34;"
    yield "      const litR = 248, litG = 244, litB = 218;"
    yield "      pix[off+0] = Math.round(baseR + (litR - baseR) * shade);"
    yield "      pix[off+1] = Math.round(baseG + (litG - baseG) * shade);"
    yield "      pix[off+2] = Math.round(baseB + (litB - baseB) * shade);"
    yield "      pix[off+3] = 255;"
    yield "    }"
    yield "  }"
    yield "  ctx.putImageData(image, 0, 0);"
    yield "  ctx.strokeStyle = '#c7ba9b';"
    yield "  ctx.lineWidth = 1;"
    yield "  ctx.beginPath();"
    yield "  ctx.arc(x, y, r, 0, Math.PI * 2);"
    yield "  ctx.stroke();"
    yield "  meta.textContent = `${data.moon_phase_label || 'Moon'} (${(illum*100).toFixed(0)}% lit)`;"
    yield "}"

    # Dynamic sensor UI helpers 
    yield "document.addEventListener('DOMContentLoaded',()=>{"
    yield "  if (typeof drawSunPath === 'function') drawSunPath(astroData);"
    yield "  if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData);"
    yield "  const form = document.querySelector('form');"
    yield "  const btn = document.getElementById('saveBtn');"
    yield "  const spinner = document.getElementById('saveSpinner');"
    yield "  if(form && btn && spinner){"
    yield "    form.addEventListener('submit',()=>{"
    yield "      spinner.style.display='inline-block';"
    yield "      btn.disabled=true;"
    yield "    });"
    yield "  }"
    yield "});"
    
    yield "let knownSensors = new Set();"
    yield "let pendingLayoutRefresh = false;"

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
    yield " const parent = (existingRow && existingRow.parentElement) || byGraphModal || document.body;" 
    yield ""

    #  Create header if missing        
    yield "  const headerId = `${sid}_header`; "
    yield "  if (!document.getElementById(headerId)) {"
    yield "    const headerWrap = document.createElement('div');"
    yield "    headerWrap.style.textAlign = 'center';"
    yield "    headerWrap.style.width = '100%';"
    yield "    headerWrap.style.marginTop = '-1.5rem';"
    yield "    headerWrap.style.marginBottom = '-1.0rem';"
    yield "    const locText = (locationText || sid);"
    yield "    const sidUpper = (sid || '').toUpperCase();"
    yield "    const sidLower = (sid || '').toLowerCase();"
    yield "    const pendingColor = '#ffc107';"  # default; poller will repaint
    yield "    headerWrap.innerHTML = `"
    yield "      <h3 id='${sid}_header'>"
    yield "        <span class='sensor-status-dot' id='${sid}_statusdot' data-sid='${sid}'"
    yield "              title='Connection status: pending' aria-label='Connections status: pending'"
    yield "              style='display:inline-block;width:15px;height:15px;border-radius:50%;"
    yield "                     vertical-align:middle;margin-right:6px;margin-bottom:4px;"
    yield "                     background:${pendingColor};border:1px solid #666;'></span>"
    yield "        ${sidUpper}"
    yield "       <a href='#' onclick=\"window.editSensorSettings && window.editSensorSettings(sidLower); return false;\" title='Open settings' style='margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;'>"
    yield "          <svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' role='img' aria-label='Settings' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    yield "            <circle cx='12' cy='12' r='7'/>"
    yield "            <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
    yield "            <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none'/>"
    yield "            <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
    yield "            <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none'/>"
    yield "            <rect x='11' y='1'  width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "            <rect x='11' y='19' width='2' height='4' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "            <rect x='1'  y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "            <rect x='19' y='11' width='4' height='2' rx='1' fill='currentColor' stroke='none' transform='rotate(45 12 12)'/>"
    yield "            <circle cx='12' cy='12' r='2.25'/>"
    yield "          </svg>"
    yield "        </a>"
    yield "        ${locText}"
    yield "      </h3>`;"
    yield "    parent.appendChild(headerWrap);"
    yield "  } else {"
    yield "    const hdr = document.getElementById(headerId);"
    yield "    if (hdr && !document.getElementById(`${sid}_statusdot`)) {"
    yield "      const dot = document.createElement('span');"
    yield "      dot.className = 'sensor-status-dot';"
    yield "      dot.id = `${sid}_statusdot`;"
    yield "      dot.setAttribute('data-sid', sid);"
    yield "      dot.setAttribute('title', 'Connection status: pending');"
    yield "      dot.setAttribute('aria-label', 'Connection status: pending');"
    yield "      dot.setAttribute('style', 'display:inline-block;width:15px;height:15px;border-radius:50%;"
    yield "                                   vertical-align:middle;margin-right:6px;margin-bottom:4px;"
    yield "                                   background:#ffc107;border:1px solid #666;');"
    yield "      hdr.insertBefore(dot, hdr.firstChild);"
    yield "    }"
    yield "  }"
    
    #  Create row wrapper if missing
    yield "  const rowId = `row_${sid}`;"
    yield "  let row = document.getElementById(rowId);"
    yield "  if (!row) {"
    yield "    row = document.createElement('div');"
    yield "    row.id = rowId;"
    yield "    row.className = 'sensor-row';"
    yield "    parent.appendChild(row);"
    yield "  }"
    yield ""
    #  Ensure metric containers exist
    yield "  (metricList || []).forEach(metric => {"
    yield "    const safeMetric = toSafeMetric(metric);"
    yield "    const containerId = `${sid}_${safeMetric}_container`;"
    yield "    if (document.getElementById(containerId)) return;"
    yield ""
    yield "    const container = document.createElement('div');"
    yield "    container.className = 'metric-container';"
    yield "    container.id = containerId;"
    yield "    container.dataset.sensor = sid;"
    yield "    container.dataset.metric = metric;"
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
    yield "    if (!canvas || !label) return;"  # only skip if structure truly broken
    yield "    const config = gaugeConfig?.[metric];"
    yield "    if (!config) return;"
    yield "    let value = currentValues?.[sensor]?.[metric];"
    yield "    const isNull = (value == null);"
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
    yield "    canvas.width = 160;"
    yield "    canvas.height = 160;"
    yield "    const gauge = new Gauge(canvas).setOptions(opts);"
    yield "    gauge.maxValue = config.max;"
    yield "    gauge.setMinValue(config.min);"
    yield "    gauge.animationSpeed = 32;"
    yield "    gauge.set(value);"
    yield "    gauge.render();"
    yield "    window[`${safe}_gauge`] = gauge;"
    yield "    label.innerText = isNull ? '--' : value + ' ' + config.unit;"
    yield "    if (window.registerContainerStyle) {"
    yield "      window.registerContainerStyle(container, 'Gauge');"
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
    yield "  return Array.from(document.querySelectorAll('.switch-metric-container h3[id$=\"_header\"]'))"
    yield "    .map(h => String(h.id || '').replace(/_header$/, ''))"
    yield "    .map(_normSwitchId)"
    yield "    .filter(Boolean);"
    yield "}"

    yield "function _layoutSignature(available, nextExpMap, renderableSwitches) {"
    yield "  const sensors = (available || []).map(sid => ({"
    yield "    sid: String(sid || ''),"
    yield "    metrics: (Array.isArray(nextExpMap?.[sid]) ? nextExpMap[sid] : []).map(m => String(m || '').trim()).filter(Boolean).sort()"
    yield "  })).sort((a,b) => a.sid.localeCompare(b.sid));"
    yield "  const switches = (renderableSwitches || []).map(_normSwitchId).filter(Boolean).sort();"
    yield "  return JSON.stringify({ sensors, switches });"
    yield "}"

    yield "function scheduleLayoutRefresh(reason, sig) {"
    yield "  if (pendingLayoutRefresh) return;"
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
    yield "  } catch (e) {"
    yield "    console.warn('updateGauges: fetch failed', e);"
    yield "    return;"
    yield "  }"
    yield ""
    #yield "  console.warn('updateGauges: step 2 - check switch events');"
    
    yield "  const available = Array.isArray(d.available) ? d.available : [];"
    yield "  const nextExpMap = d.expected_gauge_map || {};"
    yield "  const renderableSwitches = Array.isArray(d.renderable_switches_view) ? d.renderable_switches_view : (Array.isArray(d.renderable_switches) ? d.renderable_switches : []);"
    yield "  const locations  = d.locations || {};"
    yield "  const layoutDrift = shouldRefreshForLayoutDrift(available, nextExpMap, renderableSwitches, sensorId);"
    yield "  if (layoutDrift) {"
    yield "    const sig = _layoutSignature(available, nextExpMap, renderableSwitches);"
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
    yield "        ensureSensorUI(sid, metrics, locations[sid]);"
    yield "        try { initGauge(); }"
    yield "        catch (e) { console.error('initGauge() failed while extending metrics', sid, e); }"
    yield "      }"
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
    yield "      if (labelEl) {"
    yield "        const unit = (gaugeConfig?.[metric]?.unit) || '';"
    yield "        labelEl.textContent = (typeof val === 'number') ? `${val} ${unit}` : '--';"
    yield "      }"
    yield "      if (g && typeof val === 'number') {"
    yield "        try { g.set(val); } catch (e) { console.warn('Gauge set() failed', safe, e); }"
    yield "      }"
    yield ""
    yield "      const stat = sset[metric] || {};"
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
    yield "    const flashClass = dataChanged ? 'flash-green' : (isPiPlatform ? 'flash-red' : '');"
    yield "    if (flashClass) {"
    yield "      void ts.offsetWidth;"
    yield "      ts.classList.add(flashClass);"
    yield "      setTimeout(() => ts.classList.remove(flashClass), 1000);"
    yield "    }"
    yield "  }"
    yield "  if (dataChanged && typeof window.refreshAllMicrographs === 'function') {"
    yield "    window.refreshAllMicrographs(true);"
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

    yield "const vpdBackgroundMicro = {"
    yield "  id: 'vpdBackgroundMicro',"
    yield "  beforeDraw(chart){"
    yield "    const { ctx, chartArea, scales, options } = chart;"
    yield "    if (!options?.plugins?.vpdMicro) return;"
    yield "    const y = scales?.y || Object.values(scales).find(s => s.type==='linear');"
    yield "    if (!y) return;"
    yield "    const zones = ["
    yield "      { color: '#0033cc', min: 0.0, max: 0.4 },"
    yield "      { color: '#66cc66', min: 0.4, max: 0.8 },"
    yield "      { color: '#03a603', min: 0.8, max: 1.2 },"
    yield "      { color: '#3e803e', min: 1.2, max: 1.6 },"
    yield "      { color: '#bf9000', min: 1.6, max: 5.0 },"
    yield "    ];"
    yield "    ctx.save();"
    yield "    zones.forEach(z => {"
    yield "      const yTop = y.getPixelForValue(z.max);"
    yield "      const yBot = y.getPixelForValue(z.min);"
    yield "      ctx.fillStyle = z.color;"
    yield "      ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBot - yTop);"
    yield "    });"
    yield "    ctx.restore();"
    yield "  }"
    yield "};"
    yield "if (window.Chart) { try { window.Chart.register(vpdBackgroundMicro); } catch(e){} }"
    yield ""
    yield "const dewVpdRiskBackgroundMicro = {"
    yield "  id: 'dewVpdRiskBackgroundMicro',"
    yield "  beforeDraw(chart){"
    yield "    const { ctx, chartArea, scales, options } = chart;"
    yield "    if (!options?.plugins?.dewVpdRiskMicro) return;"
    yield "    const y = scales?.y || Object.values(scales).find(s => s.type==='linear');"
    yield "    if (!y) return;"
    yield "    const zones = ["
    yield "      { color: '#66cc66', min: 0, max: 30 },"
    yield "      { color: '#ffcc00', min: 30, max: 60 },"
    yield "      { color: '#bf9000', min: 60, max: 100 },"
    yield "    ];"
    yield "    ctx.save();"
    yield "    zones.forEach(z => {"
    yield "      const yTop = y.getPixelForValue(z.max);"
    yield "      const yBot = y.getPixelForValue(z.min);"
    yield "      ctx.fillStyle = z.color;"
    yield "      ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBot - yTop);"
    yield "    });"
    yield "    ctx.restore();"
    yield "  }"
    yield "};"
    yield "if (window.Chart) { try { window.Chart.register(dewVpdRiskBackgroundMicro); } catch(e){} }"
  
    yield "async function showMicrographForContainer(container) {"
    yield "  if (!container) return;"
    yield "  if (typeof updateContainerDisplayStyle === 'function') {"
    yield "    updateContainerDisplayStyle(container);"
    yield "  }"
    yield "  const gauge = container.querySelector('.gauge-container');"
    yield "  const graph = container.querySelector('.graph-container');"
    yield "  const canvas = container.querySelector('.micrograph-canvas');"
    yield "  if (!canvas || !gauge || !graph) { return; }"
    yield ""
    yield "  canvas.style.cursor = 'wait';"
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
    yield "      range = '24h';"
    yield "      xTitleText = '24 Hours';"
    yield "    }"
    yield ""
    yield "    await new Promise(function(r) { setTimeout(r, 10); });"
    yield ""
    yield "    const url = '/graph-data?sensor_id=' + encodeURIComponent(sensor)"
    yield "              + '&metric1=' + encodeURIComponent(metric)"
    yield "              + '&range=' + encodeURIComponent(range);"
    yield "    const resp = await fetch(url, { cache: 'no-store' });"
    yield "    if (!resp.ok) {"
    yield "      return;"
    yield "    }"
    yield ""
    yield "    const jsonData = await resp.json();"
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
    yield ""
    yield "    const metricName = container.dataset.metric || metric;"
    yield "    const metricNorm = String(metricName || '').toLowerCase().replace(/[_-]+/g, ' ');"
    yield "    const isDewVpdRisk = metricNorm.includes('dewvpd risk');"
    yield "    const isVPD = /vpd/i.test(metricName) && !isDewVpdRisk;"
    yield ""
    yield "    const chartOptions = {"
    yield "      responsive: false,"
    yield "      animation: false,"
    yield "      plugins: {"
    yield "        legend: { display: false },"
    yield "        tooltip: { enabled: true },"
    yield "        vpdMicro: !!isVPD,"
    yield "        dewVpdRiskMicro: !!isDewVpdRisk"
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
    yield "        y: { title: { display: false }, ticks: { precision: 1 } }"
    yield "      }"
    yield "    };"
    yield ""
    yield "    if (isDewVpdRisk) {"
    yield "      chartOptions.scales.y.min = 0;"
    yield "      chartOptions.scales.y.max = 100;"
    yield "      chartOptions.scales.y.ticks.precision = 0;"
    yield "    } else if (isVPD) {"
    yield "      chartOptions.scales.y.min = 0;"
    yield "      chartOptions.scales.y.max = 5.0;"
    yield "    }"
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
    yield "      borderColor: '#00bfff',"
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
    yield "        borderColor: 'purple',"
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
    yield "      chart.update();"
    yield "    }"
    yield ""
    yield "    graph.style.display = 'block';"
    yield "    gauge.style.display = 'none';"
    yield "  } catch (e) {"
    yield "    console.warn('showMicrographForContainer error', e);"
    yield "  } finally {"
    yield "    canvas.style.cursor = 'default';"
    yield "  }"
    yield "}"
               
    yield "const chartMap = new WeakMap();"
    yield "document.querySelectorAll('.metric-container').forEach(container => {"
    yield "  const gauge = container.querySelector('.gauge-container');"
    yield "  const graph = container.querySelector('.graph-container');"
    yield "  const canvas = container.querySelector('.micrograph-canvas');"

    gauge_config = get_gauge_config()
    yield "  const sensorUnits = {"
    for metric, cfg in gauge_config.items():
        unit = cfg.get("unit", "")
        yield f"    '{metric}': '{unit}',"
    yield "  };"

    yield "  container.addEventListener('click', async () => {"
    yield "    const gauge = container.querySelector('.gauge-container');"
    yield "    const graph = container.querySelector('.graph-container');"
    yield "    const canvas = container.querySelector('.micrograph-canvas');"
    yield "    if (!canvas || !gauge || !graph) { return; }"

    yield "    let style = 'Gauge';"
    yield "    if (typeof window.getContainerStyle === 'function') {"
    yield "      style = window.getContainerStyle(container);"
    yield "    }"

    yield "    let nextStyle = 'Gauge';"
    yield "    if (style === 'Gauge') {"
    yield "      nextStyle = 'Graph6hr';"
    yield "    } else if (style === 'Graph6hr') {"
    yield "      nextStyle = 'Graph24hr';"
    yield "    } else if (style === 'Graph24hr') {"
    yield "      nextStyle = 'Gauge';"
    yield "    } else {"
    yield "      nextStyle = 'Gauge';"
    yield "    }"

    yield "    if (typeof window.registerContainerStyle === 'function') {"
    yield "      window.registerContainerStyle(container, nextStyle);"
    yield "    }"

    yield "    if (nextStyle !== 'Gauge') {"
    yield "      const oldChart = chartMap.get(canvas);"
    yield "      if (oldChart) {"
    yield "        try { oldChart.destroy(); } catch (e) { console.warn('chart destroy failed', e); }"
    yield "        chartMap.delete(canvas);"
    yield "      }"
    yield "    }"

    yield "    if (nextStyle === 'Gauge') {"
    yield "      const oldChart = chartMap.get(canvas);"
    yield "      if (oldChart) {"
    yield "        try { oldChart.destroy(); } catch (e) { console.warn('chart destroy failed', e); }"
    yield "        chartMap.delete(canvas);"
    yield "      }"
    yield "      graph.style.display = 'none';"
    yield "      gauge.style.display = 'block';"
    yield "      return;"
    yield "    }"

    yield "    await showMicrographForContainer(container);"
    yield "  });"
    yield "});"

    yield "(function() {"
    yield "  const ds = (typeof window.displayStyle !== 'undefined' && window.displayStyle != null)"
    yield "    ? String(window.displayStyle)"
    yield "    : '';"
    yield "  const normalized = ds.toLowerCase();"

    yield "  if (normalized === 'graph6hr' || normalized === 'graph24hr') {"
    yield "    const all = document.querySelectorAll('.metric-container');"
    yield "    all.forEach(container => {"
    yield "      const targetStyle = (normalized === 'graph6hr') ? 'Graph6hr' : 'Graph24hr';"
    yield "      if (typeof window.registerContainerStyle === 'function') {"
    yield "        window.registerContainerStyle(container, targetStyle);"
    yield "      }"
    yield "      showMicrographForContainer(container);"
    yield "    });"
    yield "  }"
    yield "})();"
       
    yield "(function() {"
    yield "  let lastRun = 0;"
    yield "  const MIN_INTERVAL_MS = 60000;"
    yield ""
    yield "  async function refreshAllMicrographs(force = false) {"
    yield "  console.warn('refreshAllMicrographs called');"

    yield "    const now = Date.now();"
    yield "    if (!force && (now - lastRun) < MIN_INTERVAL_MS) {"
    yield "      return;"
    yield "    }"
    yield "    lastRun = now;"
    yield ""
    yield "    const containers = document.querySelectorAll('.metric-container');"
    yield "    for (const container of containers) {"
    yield "      try {"
    yield "        const gauge = container.querySelector('.gauge-container');"
    yield "        const graph = container.querySelector('.graph-container');"
    yield "        if (!gauge || !graph) continue;"
    yield ""
    yield "        const graphVisible = graph.style.display !== 'none';"
    yield "        const gaugeVisible = gauge.style.display !== 'none';"
    yield ""
    yield "        if (graphVisible && !gaugeVisible) {"
    yield "          await showMicrographForContainer(container);"
    yield "        }"
    yield "      } catch (e) {"
    yield "        console.warn('[micrograph] refresh error', e);"
    yield "      }"
    yield "    }"
    yield "  }"
    yield ""
    yield "  window.refreshAllMicrographs = refreshAllMicrographs;"
    yield ""
    yield "  document.addEventListener('DOMContentLoaded', function() {"
    yield "    try {"
    yield "      const style = (window.displayStyle || 'Gauge').toString().toLowerCase();"
    yield "      if (style === 'graph6hr' || style === 'graph24hr') {"
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
    yield "  const raw = (window.displayStyle || 'Gauge').toString().toLowerCase();"
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
    yield "    const style = (window.displayStyle || 'Gauge').toString().toLowerCase();"
    yield "    if (style === 'graph' || style === 'graph6hr' || style === 'graph24hr') {"
    yield "      const all = document.querySelectorAll('.metric-container');"
    yield "      for (const c of all) {"
    yield "        window.ensureContainerDisplayStyle(c);"
    yield "      }"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.warn('ensureContainerDisplayStyle DOMContentLoaded error', e);"
    yield "  }"
    yield "});"
    
    # --- Sensor Settings Modal opener (uses BackdropModal) ---
    yield "window.editSensorSettings = async function(id) {"
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
    yield "      const TAG_ID = 'system-calibration-js';"
    yield "      let needLoadSystemCalJs = true;"
    yield "      if (window.initSystemCalibrationModal) needLoadSystemCalJs = false;"
    yield "      if (needLoadSystemCalJs) {"
    yield "        const existing = document.getElementById(TAG_ID);"
    yield "        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);"
    yield "        await new Promise((resolve, reject) => {"
    yield "          const s = document.createElement('script');"
    yield "          s.id = TAG_ID;"
    yield "          s.src = '/ui_static/js/system_calibration.js?v=' + Date.now();"
    yield "          s.onload = resolve;"
    yield "          s.onerror = reject;"
    yield "          document.head.appendChild(s);"
    yield "        });"
    yield "      }"
    yield "      if (window.initSensorSettingsModal) window.initSensorSettingsModal(modal);"
    yield "      if (window.initSystemCalibrationModal) await window.initSystemCalibrationModal(modal);"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load sensor modal', e);"
    yield "  }"
    yield "};"

    # --- System Settings modal opener (embed into dashboard, avoid full-page nav) ---
    yield "window.editSystemSettings = async function() {"
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
    yield ""
    yield "    if (typeof window.openSetupModal === 'function') {"
    yield "      window.openSetupModal();"
    yield "    } else {"
    yield "      const modal = document.getElementById('setupPiModal');"
    yield "      if (modal) modal.style.display = 'block';"
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load system modal', e);"
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
    
    yield "function _findEventsListElem(key){"
    yield "  const {switchId,label}=_splitKey(key);"
    yield "  const norm=(s)=>String(s||'').trim().toLowerCase().replaceAll('_',' ').replace(/\\s+/g,' ').replace(/s$/,'');"
    # 1) exact match on the *UL*
    yield "  let el=document.querySelector(`ul.switch-events-list[data-switch-key=\"${_cssEsc(key)}\"]`);"
    yield "  if(el) return el;"
    # 2) suffix match ::Label
    yield "  el=document.querySelector(`ul.switch-events-list[data-switch-key$=\"::${_cssEsc(label)}\"]`);"
    yield "  if(el) return el;"
    # 3) id by label
    yield "  const id1=_safeName(label)+\"_events_list\";"
    yield "  el=document.getElementById(id1);"
    yield "  if(el && el.tagName==='UL') return el;"
    # 4) id by switchId+label
    yield "  const id2=_safeName((switchId?switchId+\"_\":\"\")+label)+\"_events_list\";"
    yield "  el=document.getElementById(id2);"
    yield "  if(el && el.tagName==='UL') return el;"
    # 5) defensive row walk
    yield "  if(el && el.tagName!=='UL'){"
    yield "    const row=el.closest('tr');"
    yield "    const ul=row?row.querySelector('ul.switch-events-list'):null;"
    yield "    if(ul) return ul;"
    yield "  }"
    # 6) FINAL fallback: scan all ULs; match label loosely (case-insensitive, ignore trailing 's')
    yield "  const want=norm(label);"
    yield "  for(const ul of document.querySelectorAll('ul.switch-events-list')){"
    yield "    const k=ul.getAttribute('data-switch-key')||'';"
    yield "    const {label:lab2}=_splitKey(k);"
    yield "    if(norm(lab2)===want) return ul;"
    yield "  }"
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
    
    yield "if (typeof window.postNodusSetting !== 'function') {"
    yield "  window.postNodusSetting = async function(host, filename, section, key, value){"
    yield "    const url = `http://${host}:8000/set-nodus-setting`;"
    yield "    const res = await fetch(url, {"
    yield "      method: 'POST',"
    yield "      headers: {'Content-Type':'application/json'},"
    yield "      body: JSON.stringify({ filename, section, key, value })"
    yield "    });"
    yield "    if (!res.ok) throw new Error(await res.text());"
    yield "    return true;"
    yield "  };"
    yield "}"

    # --- Switch Settings modal section switching + lazy automation init ---
    yield "window.initSwitchSettingsModal = function(modalEl){"
    yield "  const modal = modalEl || document.getElementById('switchSettingsModal');"
    yield "  if (!modal) return;"
    yield "  const btnSettings = modal.querySelector('#switchMenuSettings');"
    yield "  const btnAutos = modal.querySelector('#switchMenuAutomations');"
    yield "  const paneSettings = modal.querySelector('#switchSettingsPane');"
    yield "  const paneAutos = modal.querySelector('#switchAutomationsPane');"
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
    yield "  activate('settings');"
    yield "};"

    # --- Switch Settings Modal opener (uses BackdropModal, preserves old semantics) ---
    yield "window.editSwitchSettings = async function(id) {"
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
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Failed to load switch modal', e);"
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
    yield "    } else {"
    yield "      mount.appendChild(modal);"
    yield "      modal.style.display = 'block';"
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
    yield "  if (!box) { return; }"
    yield "  const isOn = !!(stateData && (stateData.state===true || String(stateData.state).toLowerCase()==='on'));"
    yield "  const lastTime = stateData && stateData.time ? stateData.time : '';"
    yield "  setSwitchBoxState(box, isOn);"
    yield "  if (labelEl) {"
    yield "    labelEl.textContent = isOn ? ' ON' : 'OFF';"
    yield "    labelEl.style.color = isOn ? '#080' : '#666';"
    yield "    labelEl.style.fontWeight = 'bold';"
    yield "  }"
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
    yield "          const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key]');"
    yield "          const label = key.includes('::') ? key.split('::')[1] : key;"
    yield "          const data  = { state: !!msg.state, time: [] };"
    yield "          updateSwitchVisuals(label, data, key);"
    yield "          if (typeof appendSwitchEventLine === 'function'){"
    yield "            const line = `${msg.state ? 'On' : 'Off'} ${msg.timestamp || ''}`;"
    yield "            appendSwitchEventLine(key, line);"
    yield "          }"
    yield "        } else if (msg.type === 'automation_toggle'){"
    yield "          const swId  = msg.switch_id || '';"
    yield "          const label = msg.label || '';"
    yield "          const q = label"
    yield "            ? `.automation-enabled-btn[data-switch-id=\"${swId}\"][data-label=\"${label}\"]`"
    yield "            : `.automation-enabled-btn[data-switch-id=\"${swId}\"]`;"
    yield "          const btn = document.querySelector(q);"
    yield "          if (btn) {"
    yield "            const ruleEnabled = !!msg.enabled;"
    yield "            btn.textContent = ruleEnabled ? 'Enabled' : 'Disabled';"
    yield "            btn.classList.toggle('green', ruleEnabled);"
    yield "            btn.classList.toggle('black', !ruleEnabled);"
    yield "          }"
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

    # keep for backup
    yield "async function refreshAndApplySwitchStatus() {"
    yield "  try {"
    yield "    const resp = await fetch('/switch-status-update');"
    yield "    if (!resp.ok) return;"
    yield "    const statusMap = await resp.json();"
    yield "    if (!statusMap) return;"
    yield "    const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key]');"
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

    # --- JS: Update switch events listbox ---
    yield "function updateSwitchEventsFromStatus(statusData){"
    yield "  if(!statusData || typeof statusData!=='object') return;"
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
    yield "    const normLine=(evt)=>{"
    yield "      if(evt && typeof evt==='object'){"
    yield "        const isOn=(String(evt.state).toLowerCase()==='on')||(evt.state===true);"
    yield "        const tsRaw=evt.ts||evt.time||'';"
    yield "        const ts=_stripIsoExtras(tsRaw);"
    yield "        const line = ts ? ((isOn?' On ':' Off ')+ts) : (isOn?' On':' Off');"
    yield "        return line.trim();"
    yield "      }"
    yield "      let s=String(evt||'').replace(/^'+|'+$/g,'');"
    yield "      s=s.replace(/(\\d{4}-\\d{2}-\\d{2}[T\\s]\\d{2}:\\d{2}:\\d{2})(?:\\.\\d{1,6}(?=Z|[+-]\\d{2}:\\d{2}|$))?/g,(_,a)=>a);"
    yield "      s=s.replace(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})(?:Z|[+-]\\d{2}:\\d{2})\\b/g,'$1');"
    yield "      s=s.replace('T',' ');"
    yield "      return s;"
    yield "    };"
    yield ""
    yield "    const newLines = events.map(normLine).filter(Boolean);"
    yield "    const existingLines = Array.from(listElem.querySelectorAll('li'))"
    yield "          .map(li => (li.textContent || '').trim())"
    yield "          .filter(Boolean);"
    yield ""
    yield "    const mergedLines = existingLines.slice();"
    yield "    for(const line of newLines){"
    yield "      if(!mergedLines.includes(line)){"
    yield "        mergedLines.push(line);"
    yield "      }"
    yield "    }"
    yield ""
    yield "    const extractTs = (line) => {"
    yield "      const m = String(line || '').match(/(\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})/);"
    yield "      return m ? m[1] : '';"
    yield "    };"
    yield ""
    yield "    const sortedLines = mergedLines.slice().sort((a,b) => {"
    yield "      const ta = extractTs(a);"
    yield "      const tb = extractTs(b);"
    yield "      if (ta && tb) {"
    yield "        return tb.localeCompare(ta);"
    yield "      }"
    yield "      if (tb) return 1;"
    yield "      if (ta) return -1;"
    yield "      return 0;"
    yield "    });"
    yield ""
    yield "    const MAX_EVENTS = 5;"
    yield "    const trimmedLines = sortedLines.slice(0, MAX_EVENTS);"
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
    yield "      setTimeout(() => refreshAndApplySwitchStatus(), 30000);"
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

    yield "document.addEventListener('click', async (ev) => {"
    yield "  const btn = ev.target.closest('.switch-toggle');"
    yield "  if (!btn) return;"
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
    yield "    }"
    yield "  } catch (e) {"
    yield "    console.error('Toggle failed', e);"
    yield "  }"
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
    yield "    const hasSwitch = modalInDom && modalInDom.querySelector('#actionSwitch');"
    yield "    if (!modalInDom || !hasList || !hasSwitch) {"
    yield "      console.error('Required nodes missing after mount', { modalInDom, hasList, hasSwitch });"
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
  
    yield "const SENSOR_STATUS_COLORS = { online:'#28a745', offline:'#dc3545', pending:'#ffc107' };"
    yield "let __lastJsonOnly = null;"
    yield "let __lastJsonOnlyAtMs = 0;"
    yield "async function refreshAndApplySensorStatus(){"
    yield "  try {"
    yield "    const now = Date.now();"
    yield "    let data = null;"
    yield "    if (__lastJsonOnly && (now - __lastJsonOnlyAtMs) < 20000) {"
    yield "      data = __lastJsonOnly;"
    yield "    } else {"
    yield "      const url = new URL(window.location.href);"
    yield "      url.searchParams.set('json_only','true');"
    yield "      const resp = await fetch(url.toString(), { cache:'no-store' });"
    yield "      if (!resp.ok) return;"
    yield "      data = await resp.json();"
    yield "      __lastJsonOnly = data;"
    yield "      __lastJsonOnlyAtMs = now;"
    yield "    }"
    yield "    const statuses = data && data.statuses ? data.statuses : {};"
    yield "    Object.entries(statuses).forEach(([sid,st]) => {"
    yield "      const dot = document.getElementById(`${sid}_statusdot`);"
    yield "      if (!dot) return;"
    yield "      const s = (st||'pending').toLowerCase();"
    yield "      const color = SENSOR_STATUS_COLORS[s] || SENSOR_STATUS_COLORS.pending;"
    yield "      dot.style.background = color;"
    yield "      dot.title = `Measurement status: ${s}`;"
    yield "      dot.setAttribute('aria-label', `Measurement status: ${s}`);"
    yield "    });"
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
    yield "  setInterval(function(){ if (typeof drawSunPath === 'function') drawSunPath(astroData); }, 60000);"
    yield "  setInterval(function(){ if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData); }, 3600000);"
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
        "  if (typeof window.postNodusSetting !== 'function') {"
        "    window.postNodusSetting = async function(host, filename, section, key, value){"
        "      var url = 'http://' + host + ':8000/set-nodus-setting';"
        "      var res = await fetch(url, {"
        "        method: 'POST',"
        "        headers: {'Content-Type':'application/json'},"
        "        body: JSON.stringify({ filename: filename, section: section, key: key, value: value })"
        "      });"
        "      if (!res.ok) throw new Error(await res.text());"
        "      return true;"
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
    yield "        <button id='graphSetupRemoveBtn' class='button red' onclick='removeGraphSetup()' disabled>Remove</button>"
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
        "      <button class='button black' "
        "onclick=\"document.getElementById('graphModal').style.display='none'\">Home</button>"
    )
    yield "      <button id='graphSaveButton' class='button green' onclick='saveGraphSetup(event)'>Save</button>"
    yield "      <button id='graphButton' class='button blue' onclick='loadGraph(event)'>"
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
        btn.type = 'button';
        btn.className = 'setup-item';
        btn.setAttribute('data-name', String(item.name || ''));
        btn.textContent = String(item.name || '');
        btn.onclick = () => loadSavedGraphSetup(String(item.name || ''));
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
      if(btn) btn.disabled = true;
      try{
        const suggested = GRAPH_ACTIVE_SETUP || '';
        const rawName = window.prompt('Save graph setup as:', suggested);
        if(rawName === null) return;
        const name = String(rawName || '').trim();
        if(!name){
          alert('Setup name is required.');
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
        alert('Failed to save graph setup: ' + (e && e.message ? e.message : 'unknown error'));
      }finally{
        if(btn) btn.disabled = false;
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

      const y1Opts = {
        position: 'left',
        beginAtZero: false,
        title: { display: true, text: axisTitles.y1 }
      };
      const y2Opts = {
        position: 'right',
        beginAtZero: false,
        title: { display: (keys.length > 1), text: axisTitles.y2 },
        grid: { drawOnChartArea: false },
        display: (keys.length > 1)
      };

      if (leftIsVPD){
        y1Opts.min = 0;
        y1Opts.max = 5;
      }
      if (rightIsVPD){
        y2Opts.min = 0;
        y2Opts.max = 5;
      }

      const annotationPlugin = Chart.registry.getPlugin('annotation');
      const pluginsArr = [vpdBackgroundPlugin];
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
            vpdZones: { enabled: anyVPD }
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
      const gm = document.getElementById('graphModal');
      if (gm){
        gm.style.display = 'flex';
        await initGraphBuilder();
      }
    };
    """
    yield "</script>"
