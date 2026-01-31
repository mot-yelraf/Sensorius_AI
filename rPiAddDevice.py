"""Device onboarding + settings sync for Sensorius hubs and Pico2 W/Nodus nodes.

This module handles the end-to-end "Add Device" workflow:
- connects to a Pico2 W AP, pushes Wi-Fi credentials, and initializes Nodus settings
- fetches /itaot metadata and applies sensor/switch/system settings on the hub
- writes TOML payloads into the correct settings directories and updates hub clients
- provides helpers for hostname/ID normalization, file decoding, and Wi-Fi band info

It is used by the web UI and discovery flows to register new devices and
keep hub-side settings in sync with remote sensors/switches.
"""
from __future__ import annotations

# ---------- user-defined constants ----------
PICOW_AP_SSID      = "Sensor_Setup"
PICOW_AP_PASSWORD  = "llihecaep442"
PICOW_IFNAME       = "wlan0"
PICOW_ADDR         = "192.168.4.1"
HTTPPORT           = 8000
ITAOT_URL          = f"http://{PICOW_ADDR}:{HTTPPORT}/itaot"
INIT_NODUS_URL     = f"http://{PICOW_ADDR}:{HTTPPORT}/init-nodus-settings"
DEFAULT_ENCODING   = "base64"

import os
import re
import json
import time
import asyncio
import socket
import base64
import logging
import subprocess
import requests
from pathlib import Path
import tomllib
from typing import Dict, Any, Optional, List, Tuple
from zoneinfo import ZoneInfo

from rPiUtils import get_pi_network_info, get_time_settings, printDM, debug_enabled

MODULE = "rPiAddDevice"
DEBUG = debug_enabled(MODULE)

# ---------- logger ----------
logger = logging.getLogger(MODULE)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# ---------- resolve Pi info at import (only stable items) ----------
PI_HOSTNAME = socket.gethostname()
PI_TIMEZONE = ZoneInfo("America/Denver")

# ---------- manager-aware base dirs & filenames ----------
try:
    from rPiSettings import rPiSettings
    _SYS_BASE_DIR     = getattr(rPiSettings, "DEFAULT_BASE_DIR", r"system_settings")
    _SYS_STD_FILENAME = getattr(rPiSettings, "STANDARD_FILENAME", "settings.toml")
except Exception:
    _SYS_BASE_DIR     = r"system_settings"
    _SYS_STD_FILENAME = "settings.toml"

HUB_SETTINGS_PATH = str(Path(_SYS_BASE_DIR) / PI_HOSTNAME / _SYS_STD_FILENAME)

try:
    from rPiSensorSettingsManager import SensorSettingsManager
    _SENSOR_BASE_DIR     = getattr(SensorSettingsManager, "_default_base_dir", r"sensor_settings")
    _SENSOR_STD_FILENAME = getattr(SensorSettingsManager, "STANDARD_FILENAME", "sensor.toml")
except Exception:
    _SENSOR_BASE_DIR     = r"sensor_settings"
    _SENSOR_STD_FILENAME = "sensor.toml"

try:
    from rPiSwitchSettingsManager import SwitchSettingsManager
    _SWITCH_BASE_DIR     = getattr(SwitchSettingsManager, "_default_base_dir", r"switch_settings")
    _SWITCH_STD_FILENAME = getattr(SwitchSettingsManager, "STANDARD_FILENAME", "switch.toml")
except Exception:
    _SWITCH_BASE_DIR     = r"switch_settings"
    _SWITCH_STD_FILENAME = "switch.toml"

# ---------- small helpers ----------
def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def _sanitize_for_fs(name: str) -> str:
    name = (name or "").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)

def _decode_bytes(data_str: str, encoding: str) -> bytes:
    enc = (encoding or DEFAULT_ENCODING).lower()
    if enc == "base64":
        return base64.b64decode(data_str)
    raise ValueError(f"Unsupported content encoding: {encoding}")

def _canonical_sensor_filename(incoming_name: str) -> str:
    name = (incoming_name or "").strip().lower()
    if name in ("sensor.toml", "sensor_i2c.toml", "sensor_soil.toml"):
        return "sensor.toml" if name == "sensor.toml" else _SENSOR_STD_FILENAME
    return _SENSOR_STD_FILENAME

def run_nmcli(cmd_list: list[str]) -> bool:
    try:
        subprocess.run(["nmcli"] + cmd_list, check=True)
        return True
    except subprocess.CalledProcessError as e:
        if DEBUG:
            printDM(f"nmcli command failed: {e}", location=f"{MODULE}.run_nmcli")
        return False

# ---------- Wi-Fi band detection (sync; use via asyncio.to_thread) ----------
def _band_from_freq(freq_mhz: int | None) -> str:
    try:
        f = int(freq_mhz or 0)
    except Exception:
        return "unknown"
    if 2400 <= f < 2500:
        return "2.4"
    if 4900 <= f < 5925:
        return "5"
    if 5925 <= f < 7125:
        return "6"
    return "unknown"

def _chan_from_freq(freq_mhz: int | None) -> int | None:
    try:
        f = int(freq_mhz or 0)
    except Exception:
        return None
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5000 < f < 6000:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None

def get_wifi_band_info(interface: str = PICOW_IFNAME) -> dict:
    """
    Returns live Wi-Fi band info for the active link on `interface`.
    {
      "connected": bool, "ssid": str, "freq_mhz": int|None,
      "band": "2.4"|"5"|"6"|"unknown", "channel": int|None, "source": "iw"|"nmcli"|"none"
    }
    """
    # 1) Try `iw` (best for the current link)
    try:
        out = subprocess.check_output(["iw", "dev", interface, "link"], text=True, timeout=2.0)
        if "Connected to" in out:
            ssid = ""
            freq = None
            m_ssid = re.search(r"SSID:\s*(.+)", out)
            if m_ssid:
                ssid = m_ssid.group(1).strip()
            m_freq = re.search(r"freq:\s*(\d+)", out)
            if m_freq:
                freq = int(m_freq.group(1))
            return {
                "connected": True,
                "ssid": ssid,
                "freq_mhz": freq,
                "band": _band_from_freq(freq),
                "channel": _chan_from_freq(freq),
                "source": "iw",
            }
    except Exception as e:
        if DEBUG:
            printDM(f"'iw dev {interface} link' failed: {e}", location=f"{MODULE}.get_wifi_band_info")

    # 2) Fallback to nmcli (use ONLY the active row)
    try:
        # find the active connection name for this interface
        dev_lines = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"],
            text=True, timeout=2.0
        ).splitlines()
        con_name = ""
        for ln in dev_lines:
            parts = ln.split(":")
            if len(parts) >= 4 and parts[0] == interface and parts[2] == "connected":
                con_name = parts[3].strip()
                break

        # read ONLY the active Wi-Fi row; do NOT match by SSID string
        wifi_lines = subprocess.check_output(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,FREQ", "dev", "wifi"],
            text=True, timeout=3.0
        ).splitlines()

        ssid = ""
        freq = None
        for ln in wifi_lines:
            parts = ln.split(":")
            if len(parts) < 3:
                continue
            active, s, f = parts[0], parts[1], parts[2]
            if active != "yes":
                continue
            # this is the single active row — trust it
            ssid = (s or "").strip()
            m = re.search(r"(\d+)", f or "")
            if m:
                freq = int(m.group(1))
            break

        return {
            "connected": bool(ssid),
            "ssid": ssid,
            "freq_mhz": freq,
            "band": _band_from_freq(freq),
            "channel": _chan_from_freq(freq),
            "source": "nmcli",
        }
    except Exception as e:
        if DEBUG:
            printDM(f"'nmcli' fallback failed: {e}", location=f"{MODULE}.get_wifi_band_info")

def pi_on_24ghz(interface: str = PICOW_IFNAME) -> tuple[bool, str]:
    """
    Returns (ok, reason). ok=True iff we are connected on 2.4 GHz.
    """
    info = get_wifi_band_info(interface)
    if not info.get("connected"):
        return False, "Wi-Fi not connected"
    if info.get("band") != "2.4":
        ssid = info.get("ssid", "")
        band = info.get("band", "unknown")
        freq = info.get("freq_mhz")
        return False, f"Active SSID '{ssid}' is on {band} GHz (freq={freq} MHz)"
    ch = info.get("channel")
    return True, f"2.4 GHz OK on SSID '{info.get('ssid','')}' (ch={ch})"

def _require_24ghz_or_abort() -> tuple[bool, str]:
    """
    Returns (ok, message). ok=True iff connected on 2.4 GHz.
    """
    info = get_wifi_band_info(PICOW_IFNAME)
    if not info.get("connected"):
        msg = "Wi-Fi not connected on the Pi"
        logger.error(f"[Add Device] {msg}")
        return False, msg
    if info.get("band") != "2.4":
        ssid = info.get("ssid", "")
        band = info.get("band", "unknown")
        freq = info.get("freq_mhz")
        ch   = info.get("channel")
        msg = f"Active SSID '{ssid}' is {band} GHz (freq={freq} MHz, ch={ch}); Pico2 W requires 2.4 GHz"
        logger.error(f"[Add Device] {msg}")
        return False, msg
    ssid = info.get("ssid", "")
    ch   = info.get("channel")
    ok_msg = f"2.4 GHz OK on '{ssid}' (ch={ch})"
    logger.info(f"[Add Device] {ok_msg}")
    return True, ok_msg
    
# ---------- Wi-Fi helpers (sync; use via asyncio.to_thread) ----------
def connect_to_ap(ssid: str, password: str, max_retries: int = 3) -> bool:
    def ssid_visible() -> bool:
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"],
                text=True
            )
            return any(line.strip() == ssid for line in out.splitlines())
        except Exception as e:
            if DEBUG:
                printDM(f"SSID scan failed: {e}", location=f"{MODULE}.connect_to_ap")
            return False

    for attempt in range(1, max_retries + 1):
        if DEBUG:
            printDM(f"Attempt {attempt} to connect to {ssid}...", location=f"{MODULE}.connect_to_ap")
        for _ in range(5):
            if ssid_visible():
                break
            if DEBUG:
                printDM(f"Waiting for {ssid} SSID to appear...", location=f"{MODULE}.connect_to_ap")
            time.sleep(1)
        else:
            if DEBUG:
                printDM(f"SSID '{ssid}' not visible after scan attempts.", location=f"{MODULE}.connect_to_ap")
            continue

        try:
            subprocess.run(["nmcli", "dev", "disconnect", PICOW_IFNAME], check=True)
        except subprocess.CalledProcessError:
            pass

        try:
            subprocess.run(
                ["nmcli", "dev", "wifi", "connect", ssid, "password", password, "ifname", PICOW_IFNAME],
                check=True, timeout=10
            )
            if DEBUG:
                printDM("Connected to Pico2 W AP.", location=f"{MODULE}.connect_to_ap")
            return True
        except subprocess.CalledProcessError as e:
            if DEBUG:
                printDM(f"nmcli connect failed: {e}", location=f"{MODULE}.connect_to_ap")
        except subprocess.TimeoutExpired:
            if DEBUG:
                printDM("Connection attempt timed out.", location=f"{MODULE}.connect_to_ap")
        time.sleep(1)

    if DEBUG:
        printDM("Failed to connect to Pico2 W AP after multiple attempts.", location=f"{MODULE}.connect_to_ap")
    return False

def _is_placeholder_psk(psk: str) -> bool:
    return isinstance(psk, str) and (
        not psk.strip()
        or "<hidden>" in psk
        or "802-11-wireless-security.psk" in psk.lower()
    )

def resolve_pi_wifi_credentials() -> Tuple[str, str]:
    def _strip_label(line: str) -> str:
        if not isinstance(line, str):
            return ""
        s = line.strip()
        if ":" in s:
            s = s.split(":")[-1].strip()
        return s

    info = get_pi_network_info()
    ssid = _strip_label(info.get("ssid", "") or "")
    psk  = _strip_label(info.get("password", "") or "")

    if ssid and _is_placeholder_psk(psk):
        try:
            out = subprocess.check_output(
                ["nmcli", "-s", "-g", "802-11-wireless-security.psk", "con", "show", "id", ssid],
                text=True, timeout=3
            ).strip()
            out = _strip_label(out)
            if out and not _is_placeholder_psk(out):
                psk = out
        except Exception as e:
            printDM(f"Could not read NM secrets via 'con show id {ssid}': {e}", location=f"{MODULE}.resolve_pi_wifi_credentials")

        if _is_placeholder_psk(psk):
            try:
                out2 = subprocess.check_output(
                    ["nmcli", "-s", "dev", "wifi", "show-password"],
                    text=True, timeout=3
                )
                lines = [ln for ln in out2.splitlines() if ln.strip()]
                for ln in lines:
                    low = ln.lower()
                    if "802-11-wireless-security.psk" in low and _is_placeholder_psk(psk):
                        candidate = _strip_label(ln)
                        if candidate and not _is_placeholder_psk(candidate):
                            psk = candidate
            except Exception as e:
                printDM(f"Fallback show-password parse failed: {e}", location=f"{MODULE}.resolve_pi_wifi_credentials")

    ssid = _strip_label(ssid)
    psk  = _strip_label(psk)
    if _is_placeholder_psk(psk):
        psk = ""
    if DEBUG:
        printDM(f"Success: ssid: {ssid}, psk: {psk}", location=f"{MODULE}.resolve_pi_wifi_credentials")

    return ssid, psk

def reconnect_to_pi(max_attempts: int = 3, delay_sec: float = 3.0) -> tuple[bool, str]:
    current_info = get_pi_network_info()
    ssid_saved   = current_info.get("ssid", "") or ""
    psk_saved    = current_info.get("password", "") or ""

    if DEBUG:
        printDM(f"Reconnecting to Pi network: {ssid_saved}...", location=f"{MODULE}.reconnect_to_pi")

    def ssid_visible() -> bool:
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"],
                text=True
            )
            return any(line.strip() == ssid_saved for line in out.splitlines())
        except Exception as e:
            if DEBUG:
                printDM(f"SSID scan failed: {e}", location=f"{MODULE}.reconnect_to_pi")
            return False

    for attempt in range(1, max_attempts + 1):
        if DEBUG:
            printDM(f"Attempt {attempt} to find {ssid_saved}...", location=f"{MODULE}.reconnect_to_pi")
        for _ in range(5):
            if ssid_visible():
                break
            if DEBUG:
                printDM(f"Waiting for {ssid_saved} SSID to appear...", location=f"{MODULE}.reconnect_to_pi")
            time.sleep(1)
        else:
            if DEBUG:
                printDM(f"SSID {ssid_saved} not visible after scan attempts.", location=f"{MODULE}.reconnect_to_pi")
            continue

        try:
            subprocess.run(["nmcli", "dev", "disconnect", PICOW_IFNAME], check=True)
        except subprocess.CalledProcessError:
            pass

        if run_nmcli(["con", "up", "id", ssid_saved, "ifname", PICOW_IFNAME]):
            if DEBUG:
                printDM(f"Reconnected to {ssid_saved} network successfully.", location=f"{MODULE}.reconnect_to_pi")
            return True, ssid_saved

        if run_nmcli(["dev", "wifi", "connect", ssid_saved, "ifname", PICOW_IFNAME]):
            if DEBUG:
                printDM(f"Reconnected to {ssid_saved} network successfully.", location=f"{MODULE}.reconnect_to_pi")
            return True, ssid_saved

        if psk_saved and not _is_placeholder_psk(psk_saved):
            if run_nmcli(["dev", "wifi", "connect", ssid_saved, "password", psk_saved, "ifname", PICOW_IFNAME]):
                if DEBUG:
                    printDM(f"Reconnected to {ssid_saved} network successfully.", location=f"{MODULE}.reconnect_to_pi")
                return True, ssid_saved

        if DEBUG:
            printDM(f"Reconnect attempt {attempt} failed.", location=f"{MODULE}.reconnect_to_pi")
        time.sleep(delay_sec)

    printDM(f"Failed to reconnect to Pi network '{ssid_saved}' after {max_attempts} attempts.", location=f"{MODULE}.reconnect_to_pi")
    return False, ssid_saved

# Keep alias so existing code doesn’t break
def connect_to_sensor_ap(ap_ssid: str, ap_password: str, *, attempts: int = 3) -> bool:
    return connect_to_ap(ap_ssid, ap_password, max_retries=attempts)

# ---------- device-id selection ----------
def _choose_device_id(payload: dict) -> str | None:
    for key in ("SENSOR_ID", "SWITCH_ID", "HOSTNAME"):
        val = (payload.get(key) or "").strip()
        if val:
            return val
    return None

# ---------- persistence: system / sensor / switch ----------
def persist_system_settings_by_device_id(updates: list[Dict[str, Any]]) -> Optional[str]:
    try:
        hostname_item = next(
            (u for u in updates
             if (u.get("section") == "Network" and u.get("key") == "HOSTNAME")),
            None
        )
        device_id = hostname_item.get("value") if hostname_item else None
        if not device_id:
            if DEBUG:
                printDM("Missing Network.HOSTNAME in updates", location=f"{MODULE}.persist_system_")
            return None

        safe_device_id = _sanitize_for_fs(str(device_id).strip())
        system_dir = Path(_SYS_BASE_DIR) / safe_device_id
        _ensure_dir(system_dir)

        network_values: Dict[str, Any] = {}
        time_values: Dict[str, Any] = {}
        for item in updates:
            section = item.get("section")
            key     = item.get("key")
            value   = item.get("value")
            if not section or not key:
                continue
            if section == "Network":
                network_values[key] = value
            elif section == "Time":
                time_values[key] = value

        def _toml_escape(v: Any) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return f"{v}"
            s = "" if v is None else str(v)
            s = s.replace("\\", "\\\\").replace('"', '\\"')
            return f"\"{s}\""

        def _emit_table(name: str, values: Dict[str, Any]) -> list[str]:
            lines: list[str] = []
            lines.append(f"[{name}]")
            for k, v in values.items():
                lines.append(f"{k} = {_toml_escape(v)}")
            lines.append("")
            return lines

        lines_out: list[str] = []
        if network_values:
            lines_out.extend(_emit_table("Network", network_values))
        if time_values:
            lines_out.extend(_emit_table("Time", time_values))

        toml_bytes = ("\n".join(lines_out) + ("\n" if lines_out else "")).encode("utf-8")

        dest_path = system_dir / _SYS_STD_FILENAME
        tmp_path = dest_path.with_suffix(".toml.tmp")
        tmp_path.write_bytes(toml_bytes)
        tmp_path.replace(dest_path)

        if DEBUG:
            printDM(f"{dest_path} persisted.", location=f"{MODULE}.persist_system_")

        return str(dest_path)

    except Exception as ex:
        printDM(f"Error persisting system settings: {ex}", location=f"{MODULE}.persist_system_")
        return None

def persist_sensor_toml(sensor_id: str, toml_name: str, encoding: str, data_b64: str) -> Optional[str]:
    try:
        if not sensor_id:
            printDM("No SENSOR_ID; skipping sensor file persist.", location=f"{MODULE}.persist_sensor")
            return None
        sensor_dir = Path(_SENSOR_BASE_DIR) / _sanitize_for_fs(sensor_id)
        _ensure_dir(sensor_dir)
        canonical = _canonical_sensor_filename(toml_name)
        raw = _decode_bytes(data_b64, encoding)
        dest = sensor_dir / canonical
        dest.write_bytes(raw)
        logger.info(f"Wrote sensor config → {dest}")
        if DEBUG:
            printDM(f"{dest} persisted.", location=f"{MODULE}.persist_sensor")
        return str(dest)
    except Exception as ex:
        printDM(f"Error persisting sensor file: {ex}", location=f"{MODULE}.persist_sensor")
        return None

def persist_switch_toml(switch_id: str, encoding: str, data_b64: str) -> Optional[str]:
    try:
        if not switch_id:
            printDM("No SWITCH_ID; skipping switch file persist.", location=f"{MODULE}.persist_switch")
            return None
        switch_dir = Path(_SWITCH_BASE_DIR) / _sanitize_for_fs(switch_id)
        _ensure_dir(switch_dir)
        raw = _decode_bytes(data_b64, encoding)
        dest = switch_dir / _SWITCH_STD_FILENAME
        dest.write_bytes(raw)
        logger.info(f"Wrote switch config → {dest}")
        return str(dest)
    except Exception as ex:
        printDM(f"Error persisting switch file: {ex}", location=f"{MODULE}.persist_switch")
        return None

# ---------- HTTP helpers (sync) ----------
def _http_get_json(url: str, timeout: float = 8.0) -> dict:
    headers = {"Accept": "application/json", "Connection": "close"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _get_itaot_once(timeout_sec: float = 8.0) -> Optional[Dict[str, Any]]:
    headers = {"Accept": "application/json", "Connection": "close"}
    resp = requests.get(ITAOT_URL, headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    raw = resp.text or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        try:
            Path("/tmp").mkdir(parents=True, exist_ok=True)
            Path("/tmp/itaot_raw.txt").write_text(raw, encoding="utf-8")
        except Exception:
            pass
        pos = getattr(e, "pos", 0)
        start = max(0, pos - 120)
        end   = min(len(raw), pos + 120)
        snippet = raw[start:end]
        caret_offset = pos - start
        caret_line = " " * max(0, caret_offset) + "^"
        logger.warning(
            "JSON decode failed at pos=%s (line=%s col=%s): %s\n"
            "…snippet…\n%s\n%s\n"
            "Saved full body to /tmp/itaot_raw.txt",
            pos, getattr(e, "lineno", "?"), getattr(e, "colno", "?"), e.msg,
            snippet, caret_line
        )
        raise

def _post_init_nodus_settings(settings_list: List[Dict[str, Any]], timeout_sec: float = 8.0) -> Dict[str, Any]:
    serialized = json.dumps(settings_list, separators=(",", ":"))
    if DEBUG:
        printDM(f"JSON→/init_nodus length={len(serialized)}", location=f"{MODULE}._post_init")

    try:
        resp = requests.post(INIT_NODUS_URL, json=settings_list, timeout=timeout_sec)
        if resp.status_code >= 400:
            body = None
            try:
                body = resp.text
            except Exception:
                pass
            raise requests.HTTPError(f"{resp.status_code} {resp.reason}; body={body!r}", response=resp)

        try:
            return resp.json()
        except json.JSONDecodeError:
            printDM("Non-JSON response; assuming success due to Pico reboot.", location=f"{MODULE}._post_init")
            return {"success": True, "updated": None, "hostname": None}

    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        if "104" in msg or "Connection reset by peer" in msg:
            printDM("ECONNRESET after POST; assuming Pico applied settings and rebooted.", location=f"{MODULE}._post_init")
            return {"success": True, "updated": None, "hostname": None}
        raise

# ---------- TOML fetchers (new endpoints) ----------
def _resolve_endpoint(base: str, path_or_abs: str) -> str:
    p = (path_or_abs or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    return f"http://{PICOW_ADDR}:{HTTPPORT}{p if p.startswith('/') else '/'+p}"

def fetch_settings_toml(endpoints: dict) -> Optional[tuple[str, str]]:
    try:
        url = _resolve_endpoint(ITAOT_URL, endpoints.get("settings") or "/getSettingsToml")
        obj = _http_get_json(url, timeout=8.0)
        name = obj.get("name") or "settings.toml"
        enc  = obj.get("encoding") or DEFAULT_ENCODING
        data = obj.get("data")
        if not data:
            return None
        return name, enc, data  # returning 3-tuple for uniformity; sensor/switch need id to persist
    except Exception as e:
        if DEBUG:
            printDM(f"fetch_settings_toml: {e}", location=MODULE)
        return None

def fetch_sensor_toml(endpoints: dict, active_name: str | None) -> Optional[tuple[str, str, str]]:
    try:
        base_path = endpoints.get("sensor") or "/getSensorToml"
        if active_name and active_name.strip() and active_name.strip() != "sensor.toml":
            sep = "&" if "?" in base_path else "?"
            base_path = f"{base_path}{sep}name={active_name.strip()}"
        url = _resolve_endpoint(ITAOT_URL, base_path)
        obj = _http_get_json(url, timeout=8.0)
        return obj.get("name"), obj.get("encoding") or DEFAULT_ENCODING, obj.get("data")
    except Exception as e:
        if DEBUG:
            printDM(f"fetch_sensor_toml: {e}", location=MODULE)
        return None

def fetch_switch_toml(endpoints: dict) -> Optional[tuple[str, str]]:
    try:
        url = _resolve_endpoint(ITAOT_URL, endpoints.get("switch") or "/getSwitchToml")
        obj = _http_get_json(url, timeout=8.0)
        return obj.get("encoding") or DEFAULT_ENCODING, obj.get("data")
    except Exception as e:
        if DEBUG:
            printDM(f"fetch_switch_toml: {e}", location=MODULE)
        return None

# ---------- TOML edit utilities for hub settings ----------
def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _toml_join_list(str_list: List[str]) -> str:
    inner = ", ".join(f"\"{_toml_escape(s)}\"" for s in str_list)
    return f"[{inner}]"

def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def update_hub_clients(settings_path: str, new_sensor_id: str) -> bool:
    def _ensure_local_suffix(name: str) -> str:
        name = (name or "").strip()
        return name if name.endswith(".local") else f"{name}.local"

    def _normalize_clients(names: List[str]) -> List[str]:
        seen = {}
        for n in names:
            n = (n or "").strip()
            if not n:
                continue
            base = n[:-6] if n.endswith(".local") else n
            seen.setdefault(base, _ensure_local_suffix(n))
            if n.endswith(".local"):
                seen[base] = n
        return list(seen.values())

    settings_file = Path(settings_path)
    settings_dir = settings_file.parent
    settings_dir.mkdir(parents=True, exist_ok=True)

    broker_desired = f"{PI_HOSTNAME}.local"
    new_client_norm = _ensure_local_suffix(new_sensor_id)

    if not settings_file.exists():
        minimal = (
            "[Network]\n"
            f'HOSTNAME = "{PI_HOSTNAME}"\n\n'
            "[SensorNetwork]\n"
            f'BROKER = "{broker_desired}"\n'
            f'CLIENTS = ["{_toml_escape(new_client_norm)}"]\n\n'
            "[Time]\n"
            'TZ = "America/Denver"\nTZ_OFFSET = -21600\nTZ_NAME = "MDT"\n'
        )
        _atomic_write_text(settings_file, minimal)
        if DEBUG:
            printDM(f"Created hub settings and added CLIENTS → {settings_file}", location=f"{MODULE}.update_hub_clients")
        return True

    try:
        raw = settings_file.read_text(encoding="utf-8")
    except Exception as e:
        printDM(f"Failed to read hub settings: {e}", location=f"{MODULE}.update_hub_clients")
        return False

    used_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    try:
        data = tomllib.loads(text)
    except Exception as e:
        printDM(f"Failed to parse hub settings TOML: {e}", location=f"{MODULE}.update_hub_clients")
        return False

    sn = data.get("SensorNetwork", {})
    clients_existing: List[str] = list(sn.get("CLIENTS", []))

    clients_norm = _normalize_clients(clients_existing)
    if new_client_norm not in clients_norm:
        clients_norm.append(new_client_norm)

    m = re.search(r"(?ms)^\[SensorNetwork\]\s*(.*?)(?=^\[|\Z)", text)
    if not m:
        block = (
            "\n[SensorNetwork]\n"
            f'BROKER = "{_toml_escape(broker_desired)}"\n'
            f"CLIENTS = {_toml_join_list(clients_norm)}\n"
        )
        new_text = text.rstrip("\n") + "\n" + block
    else:
        block_txt = m.group(0)
        if re.search(r"(?m)^\s*BROKER\s*=", block_txt):
            block_txt = re.sub(
                r'(?m)^\s*BROKER\s*=\s*".*?"\s*$',
                f'BROKER = "{_toml_escape(broker_desired)}"',
                block_txt
            )
        else:
            if not block_txt.endswith("\n"):
                block_txt += "\n"
            block_txt += f'BROKER = "{_toml_escape(broker_desired)}"\n'

        if re.search(r"(?m)^\s*CLIENTS\s*=\s*\[", block_txt):
            block_txt = re.sub(
                r"(?ms)^\s*CLIENTS\s*=\s*\[.*?\]",
                f"CLIENTS = {_toml_join_list(clients_norm)}",
                block_txt,
            )
        else:
            if not block_txt.endswith("\n"):
                block_txt += "\n"
            block_txt += f"CLIENTS = {_toml_join_list(clients_norm)}\n"

        start, end = m.span()
        new_text = text[:start] + block_txt + text[end:]

    if used_crlf:
        new_text = new_text.replace("\n", "\r\n")

    if new_text == raw:
        if DEBUG:
            printDM("SensorNetwork unchanged; no write needed.", location=f"{MODULE}.update_hub_clients")
        return True

    try:
        _atomic_write_text(settings_file, new_text)
        if DEBUG:
            printDM(f"Updated SensorNetwork → {settings_file}", location=f"{MODULE}.update_hub_clients")
        return True
    except Exception as e:
        printDM(f"Failed to write hub settings: {e}", location=f"{MODULE}.update_hub_clients")
        return False

# ---------- update payload build ----------
def build_picow_settings_updates(
    pi_info: Dict[str, Any],
    time_settings: Dict[str, Any],
    host: str,
) -> list[Dict[str, Any]]:
    _hostname   = host
    ssid_resolved, psk_resolved = resolve_pi_wifi_credentials()
    broker_val = pi_info.get("broker", "") or f"{PI_HOSTNAME}.local"

    return [
        {"section": "Network", "key": "SSID",      "value": ssid_resolved},
        {"section": "Network", "key": "PASSWORD",  "value": psk_resolved},
        {"section": "Network", "key": "HOSTNAME",  "value": _hostname},
        {"section": "Network", "key": "BROKER",    "value": broker_val},

        {"section": "Time", "key": "TZ",        "value": time_settings.get("TZ", "")},
        {"section": "Time", "key": "TZ_OFFSET", "value": time_settings.get("TZ_OFFSET", 0)},
        {"section": "Time", "key": "TZ_NAME",   "value": time_settings.get("TZ_NAME", "")},
    ]

# ---------- core configure+reboot (sync; call via to_thread) ----------
def perform_picow_configure_and_reboot() -> tuple[bool, Optional[str]]:
    """
    Assumes we are already connected to the Pico2 W AP.
    Flow:
      - GET /itaot (metadata-only)
      - fetch TOMLs via /getSettingsToml, /getSensorToml, /getSwitchToml
      - persist locally
      - POST /set-nodus-setting (device likely reboots)
    Returns: (success, sensor_id or None)
    """
    pi_info       = get_pi_network_info()
    time_settings = get_time_settings()

    device_info: Optional[Dict[str, Any]] = None
    for attempt in range(1, 4):
        if DEBUG:
            printDM(f"Attempting to fetch /itaot (Attempt {attempt}/3)...", location=f"{MODULE}.ppcar")
        try:
            device_info = _get_itaot_once(timeout_sec=8.0)
            if device_info:
                break
        except Exception as e:
            printDM(f"Attempt {attempt} failed: {e}", location=f"{MODULE}.ppcar")
            if attempt < 3:
                time.sleep(2)

    if not device_info:
        printDM("Failed to get /itaot after multiple attempts.", location=f"{MODULE}.ppcar")
        return (False, None)

    hostname   = device_info.get("HOSTNAME")
    sensor_id  = device_info.get("SENSOR_ID", "")
    mqtt_topic = device_info.get("mqtt_sensor_topic", "")
    endpoints  = device_info.get("endpoints") or {}

    if not hostname:
        printDM("Missing HOSTNAME in /itaot response", location=f"{MODULE}.ppcar")
        return (False, sensor_id or None)

    # Build updates (what we want the Pico to adopt)
    updates = build_picow_settings_updates(pi_info, time_settings, hostname)

    # Persist hub-side system settings immediately
    persist_system_settings_by_device_id(updates)

    # --- fetch and persist TOMLs from new endpoints ---
    try:
        st = fetch_settings_toml(endpoints)
        if st:
            name, enc, data_b64 = st
            # system settings are saved under system_settings/<HOSTNAME>/settings.toml by persist_system_settings_by_device_id (authoritative)
            # We still keep a copy from device if ever needed for audit; store as .device.snapshot
            try:
                safe_host = _sanitize_for_fs(hostname)
                audit_dir = Path(_SYS_BASE_DIR) / safe_host
                _ensure_dir(audit_dir)
                snap_path = audit_dir / f"{Path(name).stem}.device.snapshot.toml"
                snap_path.write_bytes(_decode_bytes(data_b64, enc))
            except Exception as e:
                if DEBUG:
                    printDM(f"settings snapshot skipped: {e}", location=f"{MODULE}.ppcar")
    except Exception:
        pass

    if sensor_id:
        active_file = device_info.get("active_sensor_file") or None
        st2 = fetch_sensor_toml(endpoints, active_file)
        if st2:
            s_name, s_enc, s_b64 = st2
            persist_sensor_toml(sensor_id, s_name or "sensor.toml", s_enc, s_b64)

    if device_info.get("SWITCH_ID"):
        sw = fetch_switch_toml(endpoints)
        if sw:
            sw_enc, sw_b64 = sw
            persist_switch_toml(device_info.get("SWITCH_ID"), sw_enc, sw_b64)

    # --- send updates to Pico2 W (will reboot) ---
    try:
        result = _post_init_nodus_settings(updates, timeout_sec=11.0)
        if not result.get("success", False):
            printDM(f"{INIT_NODUS_URL} failed: {result}", location=f"{MODULE}.ppcar")
            return (False, sensor_id or None)
        if DEBUG:
            printDM(f"{INIT_NODUS_URL} success: {result}", location=f"{MODULE}.ppcar")
        return (True, sensor_id or None)
    except Exception as e:
        printDM(f"Failed posting {INIT_NODUS_URL}: {e}", location=f"{MODULE}.ppcar")
        return (False, sensor_id or None)

# ---------- Public entrypoints (async) ----------
async def begin_onboarding_preview() -> Dict[str, Any]:
    """
    Preview for UI:
      - connect to Pico AP
      - GET /itaot (metadata-only)
      - return summary + switch meta (no TOML blobs)
    """
    ok24, reason24 = await asyncio.to_thread(_require_24ghz_or_abort)
    if not ok24:
        return {"error": f"Cannot start onboarding: {reason24}"}
        
    ok = await asyncio.to_thread(connect_to_ap, PICOW_AP_SSID, PICOW_AP_PASSWORD, 3)
    if not ok:
        return {"error": "Could not connect to Sensor_Setup AP"}

    try:
        if DEBUG:
            printDM(f"requesting {ITAOT_URL}", location=f"{MODULE}.bop")
        info = await asyncio.to_thread(_get_itaot_once, 5.0)
        if not info:
            return {"error": "Empty /itaot response"}

        hostname   = info.get("HOSTNAME", "")
        mqtt_topic = info.get("mqtt_sensor_topic", "")
        sensor_id  = info.get("SENSOR_ID", "")
        if not hostname:
            printDM("Incomplete /itaot (missing HOSTNAME)", location=f"{MODULE}.bop")
            return {"error": "Incomplete /itaot: no HOSTNAME"}

        now_info = get_pi_network_info()

        switch_preview = {
            "switch_id":          (info.get("SWITCH_ID") or ""),
            "switch_location":    (info.get("SWITCH_LOCATION") or ""),
            "mqtt_switch_topics": info.get("mqtt_switch_topics") or {},
        }

        return {
            "ssid":       now_info.get("ssid", ""),
            "password":   now_info.get("password", ""),
            "hostname":   hostname,
            "mqtt_topic": mqtt_topic,
            "device":     info.get("DEVICE", ""),
            "sensor_id":  sensor_id,
            "broker":     now_info.get("broker", ""),
            "location":   info.get("LOCATION", ""),
            "active_sensor_file": info.get("active_sensor_file", ""),
            "switch":     switch_preview,
        }

    except Exception as e:
        printDM(f"Failed to get /itaot: {e}", location=f"{MODULE}.bop")
        return {"error": f"Pico2 W not responding at {ITAOT_URL}"}

async def onboard_picow() -> bool:
    """
    Legacy one-shot for callers already using this function:
      connect_to_sensor_ap → perform_picow_configure_and_reboot → reconnect_to_pi → update hub CLIENTS
    """
    ok24, reason24 = await asyncio.to_thread(_require_24ghz_or_abort)
    if not ok24:
        if DEBUG:
            printDM(f"Onboarding aborted: {reason24}", location=f"{MODULE}.onboard_picow")
        return False
        
    ok = await asyncio.to_thread(connect_to_ap, PICOW_AP_SSID, PICOW_AP_PASSWORD, 3)
    if not ok:
        return False

    await asyncio.sleep(1.0)

    ok2, sensor_id = await asyncio.to_thread(perform_picow_configure_and_reboot)
    ok3, _ = await asyncio.to_thread(reconnect_to_pi)

    if not ok3:
        printDM("Failed to reconnect to Pi network.", location=f"{MODULE}.onboard_picow")
        return False

    if ok2 and sensor_id:
        try:
            update_hub_clients(HUB_SETTINGS_PATH, sensor_id)
        except Exception as e:
            printDM(f"Failed to update hub CLIENTS: {e}", location=f"{MODULE}.onboard_picow")

    if DEBUG:
        printDM(f"Pico2 W should have onboarded {ok2}", location=f"{MODULE}.onboard_picow")
    return bool(ok2)

# ---------- CLI ----------
if __name__ == "__main__":
    import asyncio as _asyncio
    _asyncio.run(onboard_picow())
