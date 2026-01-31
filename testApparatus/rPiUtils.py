import os
import time
import logging
from datetime import datetime

# Setup basic logger configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S"
)

# Optional: adjust specific module log levels
logger = logging.getLogger("rPiUtils")
logger.setLevel(logging.DEBUG)  # or INFO

# Define which modules have debug enabled
DEBUG_MODULES = set()  # e.g., {"ALL"} or {"rPiSensor", "rPiMQTTClient"}

def debug_enabled(module_name: str) -> bool:
    return "ALL" in DEBUG_MODULES or module_name in DEBUG_MODULES

def printDM(msg, location=""):
    timestamped = f"{get_timestamp()} [{location}] {msg}" if location else f"{get_timestamp()} {msg}"
    logger.debug(timestamped)

def get_timestamp():
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")

from datetime import datetime
from zoneinfo import ZoneInfo
import os
def get_time_settings():
    # Attempt to get the system TZ via /etc/localtime symlink
    try:
        tz_path = os.readlink("/etc/localtime")
        tz_parts = tz_path.split("zoneinfo/")
        timezone_id = tz_parts[1] if len(tz_parts) > 1 else "UTC"
    except Exception:
        timezone_id = "UTC"

    try:
        tz = ZoneInfo(timezone_id)
    except Exception:
        tz = ZoneInfo("UTC")
        timezone_id = "UTC"

    now = datetime.now(tz)
    tz_name = now.tzname()
    tz_offset = int(now.utcoffset().total_seconds())

    return {
        "tz": timezone_id,
        "tzOffset": tz_offset,
        "tzName": tz_name
    }
    
import subprocess
import socket
def get_pi_network_info(interface="wlan0"):
    try:
        # Get SSID
        ssid_cmd = ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]
        output = subprocess.check_output(ssid_cmd).decode()
        ssid = next((line.split(":")[1] for line in output.splitlines() if line.startswith("yes:")), "")

        # Password may be stored in connection config
        passwd_cmd = ["nmcli", "-s", "-g", "802-11-wireless-security.psk", "connection", "show", ssid]
        password = subprocess.check_output(passwd_cmd).decode().strip()

        hostname = socket.gethostname()
        broker = f"{hostname}.local"

        return {
            "ssid": ssid,
            "password": password,
            "hostname": hostname,
            "broker": broker,
        }

    except Exception as e:
        print(f"[ERROR] Failed to get Pi network info: {e}")
        return {}



