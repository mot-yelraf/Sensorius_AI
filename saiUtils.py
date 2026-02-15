"""Core shared utilities for Sensorius runtime behavior.

This module centralizes:
- logging setup and debug-module controls
- timestamp/timezone helpers
- light async watchdog helpers
- network identity helpers (hostname/mDNS)
- small compatibility wrappers used across services
"""
import os
import time
import socket
import shutil
import platform
import logging
import asyncio
import inspect
import subprocess
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
try:
    import pwd  # POSIX only
except Exception:  # pragma: no cover - windows-safe guard
    pwd = None

try:
    from dotenv import load_dotenv, dotenv_values
except Exception:  # pragma: no cover - optional dependency guard
    load_dotenv = None
    dotenv_values = None

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FILE = "sensorius.log"
DEFAULT_DEBUG_MODULES = {
    "Sensorius",
    "saiSensor",
    "saiMQTTIngest",
    "saiHtml",
    "saiSwitch",
    "saiWebRoutes",
}

logger = logging.getLogger("saiUtils")
logger.addHandler(logging.NullHandler())
logger.setLevel(logging.NOTSET)
_DOTENV_FILE_VALUES: dict[str, str] = {}


def _load_startup_dotenv() -> None:
    """
    Load a project-root .env (if present) before any env-driven config is read.
    """
    global _DOTENV_FILE_VALUES
    _DOTENV_FILE_VALUES = {}
    if load_dotenv is None:
        return

    base_dir = Path(__file__).resolve().parent
    dotenv_path = base_dir / ".env"
    if dotenv_path.exists():
        if dotenv_values is not None:
            try:
                parsed = dotenv_values(dotenv_path=dotenv_path)
                _DOTENV_FILE_VALUES = {
                    str(k): str(v)
                    for k, v in (parsed or {}).items()
                    if k and v is not None
                }
            except Exception:
                _DOTENV_FILE_VALUES = {}
        load_dotenv(dotenv_path=dotenv_path, override=False)
    else:
        # Fall back to default dotenv discovery behavior.
        load_dotenv(override=False)


_load_startup_dotenv()


def _strip_env_value(raw: str) -> str:
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _normalize_dotenv_ownership(dotenv_path: Path) -> None:
    """
    Ensure .env is user-writable and, when possible, owned by the invoking user.
    """
    try:
        if not dotenv_path.exists():
            return
        try:
            os.chmod(dotenv_path, 0o644)
        except Exception:
            pass

        if os.name != "posix":
            return
        if os.geteuid() != 0:
            return

        target_user = (os.environ.get("SUDO_USER") or "").strip()
        if not target_user:
            return
        if pwd is None:
            return
        pw = pwd.getpwnam(target_user)
        os.chown(dotenv_path, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass


def _ensure_startup_api_keys() -> None:
    """
    Ensure web/peer API keys exist in process env and project .env.

    If either key is missing/blank at startup, generate it once and persist it.
    """
    keys = ("SAI_WEB_API_KEY", "SAI_PEER_API_KEY")
    base_dir = Path(__file__).resolve().parent
    dotenv_path = base_dir / ".env"
    dotenv_example_path = base_dir / ".env.def"
    running_from_repo = (base_dir / ".git").exists()
    allow_repo_write = str(os.environ.get("SENSORIUS_ALLOW_REPO_ENV_WRITE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    file_lines: list[str] = []
    key_line_index: dict[str, int] = {}
    key_file_value: dict[str, str] = {}

    if dotenv_path.exists():
        try:
            file_lines = dotenv_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            file_lines = []
    elif dotenv_example_path.exists():
        try:
            file_lines = dotenv_example_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            file_lines = []
        for idx, line in enumerate(file_lines):
            if "=" not in line:
                continue
            left, right = line.split("=", 1)
            key = left.strip()
            if key in keys and key not in key_line_index:
                key_line_index[key] = idx
                key_file_value[key] = _strip_env_value(right)
    if not key_line_index and file_lines:
        for idx, line in enumerate(file_lines):
            if "=" not in line:
                continue
            left, right = line.split("=", 1)
            key = left.strip()
            if key in keys and key not in key_line_index:
                key_line_index[key] = idx
                key_file_value[key] = _strip_env_value(right)

    write_back: dict[str, str] = {}
    for key in keys:
        env_val = (os.environ.get(key) or "").strip()
        file_val = key_file_value.get(key, "")

        if env_val:
            if not file_val:
                write_back[key] = env_val
            continue

        if file_val:
            os.environ[key] = file_val
            continue

        new_value = _generate_api_key()
        os.environ[key] = new_value
        write_back[key] = new_value

    if not write_back:
        return

    if running_from_repo and not allow_repo_write:
        # Keep source repository .env clean by default.
        return

    if not file_lines:
        file_lines = ["# Auto-generated API keys"]

    for key, value in write_back.items():
        new_line = f"{key}={value}"
        idx = key_line_index.get(key)
        if idx is not None:
            file_lines[idx] = new_line
        else:
            file_lines.append(new_line)

    try:
        dotenv_path.write_text("\n".join(file_lines).rstrip() + "\n", encoding="utf-8")
        _normalize_dotenv_ownership(dotenv_path)
    except Exception:
        # Keep runtime env keys even if persistence fails.
        pass


_ensure_startup_api_keys()


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _get_env_setting(key: str, default=None, *, prefer_dotenv: bool = False):
    """
    Read env settings with optional .env-file precedence.
    """
    if prefer_dotenv:
        file_val = _DOTENV_FILE_VALUES.get(key)
        if file_val is not None and str(file_val).strip() != "":
            return str(file_val)
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return raw


def _load_debug_modules() -> set[str]:
    """
    Load debug-enabled modules from environment.

    ENV:
      - SENSORIUS_DEBUG_MODULES="ALL" or comma-delimited names
    """
    raw = _get_env_setting("SENSORIUS_DEBUG_MODULES", None, prefer_dotenv=True)
    if raw is None or raw.strip() == "":
        return set(DEFAULT_DEBUG_MODULES)
    modules = {part.strip() for part in raw.split(",") if part.strip()}
    return modules or set(DEFAULT_DEBUG_MODULES)


DEBUG_MODULES = _load_debug_modules()


def configure_logging(
    *,
    level: str | None = None,
    enable_file: bool | None = None,
    log_file: str | None = None,
    force: bool = False,
) -> logging.Logger:
    """
    Configure process-wide logging explicitly from the app entrypoint.

    ENV overrides:
      - SENSORIUS_LOG_LEVEL (default: DEBUG)
      - SENSORIUS_FILE_LOG  (true/false, default: false)
      - SENSORIUS_LOG_FILE  (default: sensorius.log)
      - SENSORIUS_HTTP_DEBUG (true/false, default: false)
    """
    effective_level = (level or _get_env_setting("SENSORIUS_LOG_LEVEL", "DEBUG", prefer_dotenv=True)).upper()
    file_logging = _parse_bool(
        _get_env_setting("SENSORIUS_FILE_LOG", None, prefer_dotenv=True),
        default=False if enable_file is None else bool(enable_file),
    )
    if enable_file is not None:
        file_logging = bool(enable_file)
    target_file = log_file or _get_env_setting("SENSORIUS_LOG_FILE", DEFAULT_LOG_FILE, prefer_dotenv=True)
    http_debug = _parse_bool(_get_env_setting("SENSORIUS_HTTP_DEBUG", None, prefer_dotenv=True), default=False)

    root_logger = logging.getLogger()
    if root_logger.handlers and not force:
        root_logger.setLevel(getattr(logging, effective_level, logging.INFO))
    else:
        logging.basicConfig(
            level=getattr(logging, effective_level, logging.INFO),
            format=LOG_FORMAT,
            datefmt=DATE_FORMAT,
            force=force,
        )

    # Always keep terminal logging active for direct Sensorius.py runs.
    have_stream_handler = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root_logger.handlers
    )
    if not have_stream_handler:
        sh = logging.StreamHandler()
        sh.setLevel(getattr(logging, effective_level, logging.INFO))
        sh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root_logger.addHandler(sh)

    if file_logging:
        have_file_handler = any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", "").endswith(target_file)
            for h in root_logger.handlers
        )
        if not have_file_handler:
            fh = RotatingFileHandler(target_file, maxBytes=5_000_000, backupCount=3)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
            root_logger.addHandler(fh)

    # Keep library transport internals quiet by default; set
    # SENSORIUS_HTTP_DEBUG=true when low-level trace is needed.
    if not http_debug:
        for noisy_logger in (
            "asyncio",
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "hpack",
            "urllib3",
        ):
            logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    return logging.getLogger("saiUtils")

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


def printDM(msg, location="", level: str = "debug"):
    if not location:
        try:
            # Auto-attach origin for callsites that omit location.
            frame = inspect.currentframe()
            caller = frame.f_back if frame else None
            module_name = (caller.f_globals.get("__name__", "") if caller else "") or "unknown"
            function_name = (caller.f_code.co_name if caller else "") or "unknown"
            location = f"{module_name}:{function_name}"
        except Exception:
            location = ""
    log_info = f"[{location}] {msg}" if location else f"{msg}"
    log_method = getattr(logger, str(level).lower(), logger.debug)
    log_method(log_info)

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
    If we cannot, return None values so callers can keep defaults.
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

    def _mac_wifi_interface() -> str:
        try:
            out = subprocess.check_output(["networksetup", "-listallhardwareports"], text=True, timeout=2.0)
            blocks = [blk.strip() for blk in out.split("\n\n") if blk.strip()]
            for blk in blocks:
                low = blk.lower()
                if "hardware port: wi-fi" not in low and "hardware port: airport" not in low:
                    continue
                for ln in blk.splitlines():
                    if ln.strip().startswith("Device:"):
                        return ln.split(":", 1)[1].strip()
        except Exception:
            pass
        return "en0"

    def _mac_info() -> dict:
        result = dict(soft)
        try:
            iface = _mac_wifi_interface()
            out = subprocess.check_output(["networksetup", "-getairportnetwork", iface], text=True, timeout=2.0)
            if ":" in out:
                ssid = out.split(":", 1)[1].strip()
                if ssid and "not associated" not in ssid.lower():
                    result["ssid"] = ssid
                    try:
                        psk = subprocess.check_output(
                            ["security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w"],
                            text=True, timeout=3.0
                        ).strip()
                        result["password"] = psk or ""
                    except Exception:
                        result["password"] = ""
        except Exception:
            pass
        return result

    def _windows_info() -> dict:
        result = dict(soft)
        try:
            out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, timeout=3.0)
            state = ""
            ssid = ""
            for ln in out.splitlines():
                if ":" not in ln:
                    continue
                key, val = ln.split(":", 1)
                key = key.strip().lower()
                val = val.strip()
                if key == "state":
                    state = val
                elif key == "ssid" and not ln.lstrip().lower().startswith("bssid"):
                    ssid = val
            if "connected" in state.lower() and ssid:
                result["ssid"] = ssid
                try:
                    prof = subprocess.check_output(
                        ["netsh", "wlan", "show", "profile", f"name={ssid}", "key=clear"],
                        text=True, timeout=4.0
                    )
                    for pl in prof.splitlines():
                        low = pl.lower()
                        if "key content" in low and ":" in pl:
                            result["password"] = pl.split(":", 1)[1].strip()
                            break
                except Exception:
                    result["password"] = ""
        except Exception:
            pass
        return result

    try:
        sys_name = platform.system().lower()
        if sys_name == "darwin":
            result = _mac_info()
            cache["data"] = result
            cache["ok_until"] = now + 120.0
            cache["backoff"] = 0.0
            cache["backoff_until"] = 0.0
            return result
        if sys_name == "windows":
            result = _windows_info()
            cache["data"] = result
            cache["ok_until"] = now + 120.0
            cache["backoff"] = 0.0
            cache["backoff_until"] = 0.0
            return result
        if sys_name != "linux":
            cache["data"] = soft
            cache["ok_until"] = now + 300.0
            cache["backoff"] = 0.0
            cache["backoff_until"] = 0.0
            return soft
        try:
            model_text = ""
            model_paths = (
                "/proc/device-tree/model",
                "/sys/firmware/devicetree/base/model",
            )
            for model_path in model_paths:
                if os.path.exists(model_path):
                    with open(model_path, "rb") as fh:
                        model_text = fh.read().decode("utf-8", errors="ignore").strip("\x00 \n\t")
                    if model_text:
                        break
            is_rpi = ("raspberry pi" in model_text.lower()) if model_text else False
            if not is_rpi and os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
                    cpuinfo = fh.read()
                if "raspberry pi" in cpuinfo.lower():
                    is_rpi = True
        except Exception:
            is_rpi = False
        if not is_rpi:
            cache["data"] = soft
            cache["ok_until"] = now + 300.0
            cache["backoff"] = 0.0
            cache["backoff_until"] = 0.0
            return soft

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
