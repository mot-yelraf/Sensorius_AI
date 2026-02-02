"""Shared utility helpers for logging, time, and formatting."""
import os
import time
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

# Setup basic logger configuration
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT
)

# Toggle file logging. Set True to write sensorius.log.
DEBUGLOG = False
LOG_FILE = "sensorius.log"

if DEBUGLOG:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers:
        handler.setLevel(logging.INFO)
    if not any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", "").endswith(LOG_FILE)
        for handler in root_logger.handlers
    ):
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root_logger.addHandler(file_handler)

# Optional: adjust specific module log levels
logger = logging.getLogger("saiUtils")
logger.setLevel(logging.DEBUG)  # or INFO

# Define which modules have debug enabled
DEBUG_MODULES = set()  # e.g., {"ALL"} or {"saiSensor", "saiWebRoutes", "saiHtml"}
DEBUG_MODULES = {"Sensorius", "saiSensor", "saiMQTTIngest", "saiHtml", "saiSwitch", "saiWebRoutes"}

async def supervised_task(name, coro_func, supervisor):
    try:
        await coro_func()
    except asyncio.CancelledError:
        printDM(f"[{name}] Task was cancelled", location="saiSupervisor")
        raise  # important: re-raise to allow proper shutdown
    except Exception as e:
        printDM(f"[{name}] Task crashed: {e}", location="saiSupervisor")
    finally:
        if supervisor:
            printDM(f"[{name}] Marking watchdog as fed with error", location="saiSupervisor")
            supervisor.feedthedogs(name, error=True)

def debug_enabled(module_name: str) -> bool:
    return "ALL" in DEBUG_MODULES or module_name in DEBUG_MODULES

def printDM(msg, location=""):
    log_info = f"[{location}] {msg}" if location else f"{get_timestamp()} {msg}"
    logger.debug(log_info)

def html_escape(text):
    # Be tolerant: accept int/float/None/etc.
    if text is None:
        text = ""
    else:
        text = str(text)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))
    
def normalize_sensor_id(sensor_id):
    return sensor_id.lower().replace("_", "-")

def normalize_hostname_base(name: str | None) -> str:
    """
    Return a canonical host base (no trailing .local), preserving case.
    Accepts inputs like "host", "host.local", or "host.local.local".
    """
    s = (name or "").strip()
    if not s:
        return ""
    while s.endswith(".local"):
        s = s[:-6]
    return s

def mdns_hostname(name: str | None) -> str:
    """
    Return an mDNS-friendly hostname.
    - If name already includes .local, keep a single .local.
    - If name has no dot, append .local.
    - If name is an IP or FQDN (contains a dot), return as-is.
    """
    base = normalize_hostname_base(name)
    if not base:
        return ""
    if "." in base:
        return base
    return f"{base}.local"

def get_timestamp(include_microseconds: bool = True) -> str:
    """
    Return an ISO8601 local timestamp with timezone offset.
    Defaults to microsecond precision to match saiDataLogger defaults.
    """
    # Prefer the app setting (same source the logger uses)
    tzname = None
    try:
        from saiSettings import saiSettings
        _settings = saiSettings(apply_live=False)
        tzname = (_settings.get_setting("Time", "TZ")
                  or _settings.get_setting("Time", "tz"))
    except Exception:
        tzname = None

    # Fallback to system /etc/localtime symlink, then UTC
    if not tzname:
        try:
            tz_path = os.readlink("/etc/localtime")
            parts = tz_path.split("zoneinfo/")
            tzname = parts[1] if len(parts) > 1 else "UTC"
        except Exception:
            tzname = "UTC"

    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("UTC")

    timespec = "microseconds" if include_microseconds else "seconds"
    return datetime.now(tz).isoformat(timespec=timespec)


def get_time_settings():
    """
    Try to determine the system TZ.
    If we can’t, return None values so callers can keep defaults.
    Returns keys in UPPERCASE to match your factory TOML.
    """
    timezone_id = None

    # Debian/Raspbian: /etc/timezone (e.g., "America/Denver")
    try:
        if os.path.exists("/etc/timezone"):
            with open("/etc/timezone", "r", encoding="utf-8") as f:
                val = (f.read() or "").strip()
                if val:
                    timezone_id = val
    except Exception:
        pass

    # /etc/localtime symlink target .../zoneinfo/Area/City
    if not timezone_id:
        try:
            target = os.path.realpath("/etc/localtime")
            marker = "zoneinfo/"
            if marker in target:
                timezone_id = target.split(marker, 1)[1]
        except Exception:
            pass

    # Couldn’t determine ― signal “unknown” with None values
    if not timezone_id:
        return {"TZ": None, "TZ_OFFSET": None, "TZ_NAME": None}

    # Compute offset/name if the zone can be loaded; otherwise return TZ only
    try:
        tzinfo = ZoneInfo(timezone_id)
        now = datetime.now(tzinfo)
        offset = now.utcoffset() or timedelta(0)
        return {
            "TZ": timezone_id,
            "TZ_OFFSET": int(offset.total_seconds()),  # seconds (matches your factory)
            "TZ_NAME": now.tzname() or timezone_id,
        }
    except Exception:
        return {"TZ": timezone_id, "TZ_OFFSET": None, "TZ_NAME": None}


# tools/loop_lag_monitor.py
async def loop_lag_monitor(name="loop_lag", period=0.5, warn_over=1.25):
    last = time.perf_counter()
    while True:
        await asyncio.sleep(period)
        now = time.perf_counter()
        drift = (now - last) - period   # <-- per-interval drift (what you want)
        last = now
        if drift > warn_over:
            printDM(f"[{name}] drift={drift:.3f}s (period={period:.2f}s)", location="loop_lag_monitor")

import subprocess
import socket
import shutil
import time

def get_pi_network_info(interface: str = "wlan0", force_refresh: bool = False) -> dict:
    """
    Return {'ssid','password','hostname','broker'}.
    - On success: real SSID/password from NetworkManager.
    - On failure/timeout/missing nmcli: still returns {'ssid': '', 'password': '', 'hostname', 'broker'}.

    This version avoids 'nmcli dev wifi' (which triggers a scan and can block),
    and adds caching + exponential backoff to prevent log spam under flakiness.
    """
    # Always be able to return these, even if nmcli fails
    hostname = socket.gethostname()
    broker = mdns_hostname(hostname)
    soft = {"ssid": "", "password": "", "hostname": hostname, "broker": broker}

    # --- lightweight cache / backoff (function attributes, no globals) ---
    now = time.monotonic()
    if not hasattr(get_pi_network_info, "_cache"):
        get_pi_network_info._cache = {"data": None, "ok_until": 0.0, "backoff_until": 0.0, "backoff": 0.0}
    cache = get_pi_network_info._cache

    if not force_refresh:
        if now < cache["backoff_until"]:
            return cache["data"] or soft
        if now < cache["ok_until"] and cache["data"]:
            return cache["data"]

    try:
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli not found")

        # 1) Ask NM for this interface's *current* connection (no scan)
        #    Example lines:
        #      GENERAL.TYPE:wifi
        #      GENERAL.STATE:100 (connected)
        #      GENERAL.CONNECTION:My Home WiFi
        show_lines = subprocess.check_output(
            ["nmcli", "-t", "-f", "GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION", "device", "show", interface],
            text=True, timeout=2.0
        ).splitlines()

        # parse fields
        def _val_after(prefix: str, line: str) -> str:
            # strip only that exact prefix, not any colon in the value
            return line.split(":", 1)[1].strip() if line.startswith(prefix) else ""

        gen_type = ""
        gen_state = ""
        conn_name = ""
        for ln in show_lines:
            if ln.startswith("GENERAL.TYPE:"):
                gen_type = _val_after("GENERAL.TYPE", ln)
            elif ln.startswith("GENERAL.STATE:"):
                gen_state = _val_after("GENERAL.STATE", ln)
            elif ln.startswith("GENERAL.CONNECTION:"):
                conn_name = _val_after("GENERAL.CONNECTION", ln)

        is_wifi = (gen_type.strip() == "wifi")
        is_connected = gen_state.strip().startswith("100")
        if not (is_wifi and is_connected and conn_name):
            result = soft
        else:
            # SSID
            def _read_nm_field(field: str) -> str:
                try:
                    out = subprocess.check_output(
                        ["nmcli", "-t", "-g", field, "connection", "show", conn_name],
                        text=True, timeout=1.5
                    ).strip()
                    # remove a *leading* field label if a quirky nmcli build adds it
                    if out.startswith(field + ":"):
                        out = out.split(":", 1)[1].strip()
                    return out
                except subprocess.CalledProcessError:
                    return ""

            ssid = _read_nm_field("802-11-wireless.ssid") or conn_name

            # Passwords (best-effort)
            password = (
                _read_nm_field("802-11-wireless-security.psk")
                or _read_nm_field("wifi-sec.psk")
                or _read_nm_field("802-11-wireless-security.psk-file")  # some profiles use a file
            )

            result = {"ssid": ssid or "", "password": password or "", "hostname": hostname, "broker": broker}

        cache["data"] = result
        cache["ok_until"] = now + 300.0
        cache["backoff"] = 0.0
        cache["backoff_until"] = 0.0
        return result

    except Exception as e:
        try:
            # use your logger here if desired
            printDM(f"[ERROR] Failed to get Pi network info: {e}", location="get_pi_network_info")  # noqa
        except Exception:
            pass
        new_backoff = 5.0 if cache["backoff"] <= 0.0 else min(cache["backoff"] * 2.0, 300.0)
        cache["backoff"] = new_backoff
        cache["backoff_until"] = now + new_backoff
        return cache["data"] or soft

class SettingsWrapper:
    def __init__(self, data):
        self.settings = data  # this is an OrderedDict
        
    def get(self, section, default=None):
        return self.settings.get(section, default)

    def get_setting(self, section, key, default=None):
        return self.settings.get(section, {}).get(key, default)

    def replace_setting(self, section, key, value):
        if section not in self.settings:
            self.settings[section] = {}
        self.settings[section][key] = value

    def __getitem__(self, section):
        return self.settings[section]

    def __contains__(self, section):
        return section in self.settings
