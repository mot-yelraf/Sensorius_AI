"""Device onboarding bootstrap for Sensorius hubs and Pico2 W/Nodus nodes.

This module handles the end-to-end "Add Device" workflow:
- connects to a Pico2 W AP, pushes Wi-Fi credentials, and initializes Nodus settings
- uses POST /itaot-init for AP bootstrap
- provides helpers for hostname/ID normalization, file decoding, and Wi-Fi band info

It is used by the web UI and discovery flows to register new devices and
start MQTT-driven onboarding.
"""
from __future__ import annotations

# ---------- user-defined constants ----------
PICOW_AP_SSID      = ""
PICOW_AP_PASSWORD  = ""
PICOW_IFNAME       = "wlan0"
PICOW_ADDR         = "192.168.4.1"
HTTPPORT           = 8000
ITAOT_INIT_URL     = f"http://{PICOW_ADDR}:{HTTPPORT}/itaot-init"
DEFAULT_ENCODING   = "base64"

import os
import re
import time
import asyncio
import socket
import base64
import logging
import platform
import subprocess
import tempfile
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from zoneinfo import ZoneInfo
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape

from saiUtils import get_pi_network_info, printDM, debug_enabled, mdns_hostname

MODULE = "saiAddDevice"
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
    from saiSettings import saiSettings
    _SYS_BASE_DIR     = getattr(saiSettings, "DEFAULT_BASE_DIR", r"system_settings")
    _SYS_STD_FILENAME = getattr(saiSettings, "STANDARD_FILENAME", "settings.toml")
    PICOW_AP_SSID, PICOW_AP_PASSWORD = saiSettings.get_factory_nodus_ap_credentials(base_dir=_SYS_BASE_DIR)
except Exception:
    _SYS_BASE_DIR     = r"system_settings"
    _SYS_STD_FILENAME = "settings.toml"

HUB_SETTINGS_PATH = str(Path(_SYS_BASE_DIR) / PI_HOSTNAME / _SYS_STD_FILENAME)

try:
    from saiSensorSettingsManager import SensorSettingsManager
    _SENSOR_BASE_DIR     = getattr(SensorSettingsManager, "_default_base_dir", r"sensor_settings")
    _SENSOR_STD_FILENAME = getattr(SensorSettingsManager, "STANDARD_FILENAME", "sensor.toml")
except Exception:
    _SENSOR_BASE_DIR     = r"sensor_settings"
    _SENSOR_STD_FILENAME = "sensor.toml"

try:
    from saiSwitchSettingsManager import SwitchSettingsManager
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

def _current_platform() -> str:
    return platform.system().lower()

def _mac_wifi_interface() -> str:
    try:
        out = subprocess.check_output(["networksetup", "-listallhardwareports"], text=True, timeout=2.0)
        blocks = [blk.strip() for blk in out.split("\n\n") if blk.strip()]
        for blk in blocks:
            if "hardware port: wi-fi" not in blk.lower() and "hardware port: airport" not in blk.lower():
                continue
            m = re.search(r"^\s*Device:\s*(\S+)\s*$", blk, flags=re.MULTILINE)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return "en0"

def _windows_wifi_interface() -> str:
    try:
        out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, timeout=3.0)
        m = re.search(r"^\s*Name\s*:\s*(.+)\s*$", out, flags=re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return "Wi-Fi"

def _wifi_interface_name() -> str:
    sys_name = _current_platform()
    if sys_name == "darwin":
        return _mac_wifi_interface()
    if sys_name == "windows":
        return _windows_wifi_interface()
    return PICOW_IFNAME

def _windows_profile_xml(ssid: str, password: str) -> str:
    esc_ssid = xml_escape(ssid)
    hex_ssid = ssid.encode("utf-8").hex().upper()
    if password:
        return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{esc_ssid}</name>
    <SSIDConfig>
        <SSID>
            <hex>{hex_ssid}</hex>
            <name>{esc_ssid}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>WPA2PSK</authentication>
                <encryption>AES</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
            <sharedKey>
                <keyType>passPhrase</keyType>
                <protected>false</protected>
                <keyMaterial>{xml_escape(password)}</keyMaterial>
            </sharedKey>
        </security>
    </MSM>
</WLANProfile>"""
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{esc_ssid}</name>
    <SSIDConfig>
        <SSID>
            <hex>{hex_ssid}</hex>
            <name>{esc_ssid}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>open</authentication>
                <encryption>none</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
        </security>
    </MSM>
</WLANProfile>"""

def _windows_add_profile_and_connect(ssid: str, password: str, iface: str) -> bool:
    xml_path = None
    try:
        xml_text = _windows_profile_xml(ssid, password or "")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as tf:
            tf.write(xml_text)
            xml_path = tf.name
        subprocess.run(
            ["netsh", "wlan", "add", "profile", f"filename={xml_path}", "user=current"],
            check=True, timeout=5
        )
        subprocess.run(
            ["netsh", "wlan", "connect", f"name={ssid}", f"interface={iface}"],
            check=True, timeout=12
        )
        return True
    except Exception as e:
        if DEBUG:
            printDM(f"Windows WLAN connect failed: {e}", location=f"{MODULE}._windows_add_profile_and_connect")
        return False
    finally:
        if xml_path:
            try:
                os.remove(xml_path)
            except Exception:
                pass

def _ssid_visible_linux(ssid: str, iface: str) -> bool:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface],
            text=True
        )
        return any(line.strip() == ssid for line in out.splitlines())
    except Exception as e:
        if DEBUG:
            printDM(f"SSID scan failed: {e}", location=f"{MODULE}._ssid_visible_linux")
        return False

def _connect_wifi(ssid: str, password: str, iface: str) -> bool:
    sys_name = _current_platform()
    if sys_name == "windows":
        return _windows_add_profile_and_connect(ssid, password, iface)
    if sys_name == "darwin":
        cmd = ["networksetup", "-setairportnetwork", iface, ssid]
        if password:
            cmd.append(password)
        try:
            subprocess.run(cmd, check=True, timeout=12)
            return True
        except Exception as e:
            if DEBUG:
                printDM(f"macOS Wi-Fi connect failed: {e}", location=f"{MODULE}._connect_wifi")
            return False
    try:
        try:
            subprocess.run(["nmcli", "dev", "disconnect", iface], check=True, timeout=4)
        except subprocess.CalledProcessError:
            pass
        cmd = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", iface]
        if password:
            cmd.extend(["password", password])
        subprocess.run(cmd, check=True, timeout=12)
        return True
    except Exception as e:
        if DEBUG:
            printDM(f"Linux Wi-Fi connect failed: {e}", location=f"{MODULE}._connect_wifi")
        return False

def _get_current_ssid() -> str:
    sys_name = _current_platform()
    try:
        if sys_name == "windows":
            out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, timeout=3.0)
            m_state = re.search(r"^\s*State\s*:\s*(.+)$", out, flags=re.MULTILINE | re.IGNORECASE)
            if not m_state or "connected" not in m_state.group(1).strip().lower():
                return ""
            for ln in out.splitlines():
                if not re.match(r"^\s*SSID\s*:", ln, flags=re.IGNORECASE):
                    continue
                if re.match(r"^\s*BSSID\s*:", ln, flags=re.IGNORECASE):
                    continue
                return ln.split(":", 1)[1].strip()
            return ""
        if sys_name == "darwin":
            iface = _wifi_interface_name()
            out = subprocess.check_output(["networksetup", "-getairportnetwork", iface], text=True, timeout=2.0)
            if ":" in out:
                return out.split(":", 1)[1].strip()
            return ""
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            text=True, timeout=2.0
        )
        for ln in out.splitlines():
            parts = ln.split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                return parts[1].strip()
    except Exception:
        pass
    return ""

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
    sys_name = _current_platform()
    iface = interface or _wifi_interface_name()

    if sys_name == "darwin":
        try:
            airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
            out = subprocess.check_output([airport, "-I"], text=True, timeout=2.0)
            ssid = ""
            chan = None
            for ln in out.splitlines():
                if " SSID:" in ln:
                    ssid = ln.split("SSID:", 1)[1].strip()
                if " channel:" in ln:
                    raw_chan = ln.split("channel:", 1)[1].split(",", 1)[0].strip()
                    if raw_chan.isdigit():
                        chan = int(raw_chan)
            band = "unknown"
            if chan is not None:
                if 1 <= chan <= 14:
                    band = "2.4"
                elif chan >= 30:
                    band = "5"
            return {
                "connected": bool(ssid),
                "ssid": ssid,
                "freq_mhz": None,
                "band": band,
                "channel": chan,
                "source": "airport",
            }
        except Exception as e:
            if DEBUG:
                printDM(f"macOS airport band query failed: {e}", location=f"{MODULE}.get_wifi_band_info")

    if sys_name == "windows":
        try:
            out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, timeout=3.0)
            m_state = re.search(r"^\s*State\s*:\s*(.+)$", out, flags=re.MULTILINE | re.IGNORECASE)
            connected = bool(m_state and "connected" in m_state.group(1).strip().lower())
            ssid = ""
            ch = None
            for ln in out.splitlines():
                if re.match(r"^\s*SSID\s*:", ln, flags=re.IGNORECASE) and not re.match(r"^\s*BSSID\s*:", ln, flags=re.IGNORECASE):
                    ssid = ln.split(":", 1)[1].strip()
                if re.match(r"^\s*Channel\s*:", ln, flags=re.IGNORECASE):
                    raw = ln.split(":", 1)[1].strip()
                    if raw.isdigit():
                        ch = int(raw)
            band = "unknown"
            if ch is not None:
                band = "2.4" if 1 <= ch <= 14 else "5"
            return {
                "connected": connected and bool(ssid),
                "ssid": ssid,
                "freq_mhz": None,
                "band": band,
                "channel": ch,
                "source": "netsh",
            }
        except Exception as e:
            if DEBUG:
                printDM(f"Windows band query failed: {e}", location=f"{MODULE}.get_wifi_band_info")

    # 1) Try `iw` (best for the current link)
    try:
        out = subprocess.check_output(["iw", "dev", iface, "link"], text=True, timeout=2.0)
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
            printDM(f"'iw dev {iface} link' failed: {e}", location=f"{MODULE}.get_wifi_band_info")

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
            if len(parts) >= 4 and parts[0] == iface and parts[2] == "connected":
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
    info = get_wifi_band_info(_wifi_interface_name())
    if not info.get("connected"):
        msg = "Wi-Fi is not connected on the host"
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
    sys_name = _current_platform()
    iface = _wifi_interface_name()

    for attempt in range(1, max_retries + 1):
        if DEBUG:
            printDM(f"Attempt {attempt} to connect to {ssid} on {iface}...", location=f"{MODULE}.connect_to_ap")

        if sys_name == "linux":
            for _ in range(5):
                if _ssid_visible_linux(ssid, iface):
                    break
                if DEBUG:
                    printDM(f"Waiting for {ssid} SSID to appear...", location=f"{MODULE}.connect_to_ap")
                time.sleep(1)
            else:
                if DEBUG:
                    printDM(f"SSID '{ssid}' not visible after scan attempts.", location=f"{MODULE}.connect_to_ap")
                continue

        if _connect_wifi(ssid, password, iface):
            if DEBUG:
                printDM("Connected to Pico2 W AP.", location=f"{MODULE}.connect_to_ap")
            return True
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
        sys_name = _current_platform()
        if sys_name == "darwin":
            try:
                out = subprocess.check_output(
                    ["security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w"],
                    text=True, timeout=3
                ).strip()
                out = _strip_label(out)
                if out and not _is_placeholder_psk(out):
                    psk = out
            except Exception as e:
                if DEBUG:
                    printDM(f"Could not read macOS Keychain Wi-Fi password: {e}", location=f"{MODULE}.resolve_pi_wifi_credentials")
        elif sys_name == "windows":
            try:
                out = subprocess.check_output(
                    ["netsh", "wlan", "show", "profile", f"name={ssid}", "key=clear"],
                    text=True, timeout=4
                )
                for ln in out.splitlines():
                    low = ln.lower()
                    if "key content" in low:
                        candidate = _strip_label(ln)
                        if candidate and not _is_placeholder_psk(candidate):
                            psk = candidate
                            break
            except Exception as e:
                if DEBUG:
                    printDM(f"Could not read Windows Wi-Fi profile key: {e}", location=f"{MODULE}.resolve_pi_wifi_credentials")

    if ssid and _is_placeholder_psk(psk) and _current_platform() == "linux":
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
    ssid_saved   = (current_info.get("ssid", "") or "").strip()
    psk_saved    = current_info.get("password", "") or ""
    iface        = _wifi_interface_name()
    sys_name     = _current_platform()

    if not ssid_saved:
        ssid_saved = _get_current_ssid()

    if DEBUG:
        printDM(f"Reconnecting to Pi network: {ssid_saved}...", location=f"{MODULE}.reconnect_to_pi")

    if not ssid_saved:
        printDM("No saved SSID to reconnect to.", location=f"{MODULE}.reconnect_to_pi")
        return False, ""

    for attempt in range(1, max_attempts + 1):
        if DEBUG:
            printDM(f"Attempt {attempt} to find {ssid_saved}...", location=f"{MODULE}.reconnect_to_pi")

        if sys_name == "linux":
            for _ in range(5):
                if _ssid_visible_linux(ssid_saved, iface):
                    break
                if DEBUG:
                    printDM(f"Waiting for {ssid_saved} SSID to appear...", location=f"{MODULE}.reconnect_to_pi")
                time.sleep(1)
            else:
                if DEBUG:
                    printDM(f"SSID {ssid_saved} not visible after scan attempts.", location=f"{MODULE}.reconnect_to_pi")
                continue

        if _connect_wifi(ssid_saved, "", iface):
            if DEBUG:
                printDM(f"Reconnected to {ssid_saved} network successfully.", location=f"{MODULE}.reconnect_to_pi")
            return True, ssid_saved

        if psk_saved and not _is_placeholder_psk(psk_saved) and _connect_wifi(ssid_saved, psk_saved, iface):
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
    for key in ("SENSOR_ID", "SWITCH_ID", "SWITCH_DEVICE_ID", "HOSTNAME"):
        val = (payload.get(key) or "").strip()
        if val:
            return val
    return None


def _choose_switch_id(payload: dict) -> str:
    """
    Resolve switch id from /itaot payload across legacy and current schemas.
    """
    if not isinstance(payload, dict):
        return ""

    for key in ("SWITCH_ID", "SWITCH_DEVICE_ID"):
        val = str(payload.get(key) or "").strip()
        if val:
            return val

    # Modern schema often uses a switches[] array.
    switches = payload.get("switches")
    if isinstance(switches, list):
        for sw in switches:
            if not isinstance(sw, dict):
                continue
            val = str(sw.get("SWITCH_DEVICE_ID") or sw.get("switch_id") or sw.get("SWITCH_ID") or "").strip()
            if val:
                return val
    return ""

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
def _build_itaot_init_payload(
    *,
    ssid: str,
    password: str,
    hostname: str,
    onboard_token: str,
) -> Dict[str, Any]:
    pi_info = get_pi_network_info()
    broker_host = str(pi_info.get("broker", "") or "").strip() or mdns_hostname(PI_HOSTNAME)
    try:
        broker_port = int(pi_info.get("port", 1883) or 1883)
    except Exception:
        broker_port = 1883
    if broker_port <= 0:
        broker_port = 1883

    return {
        "onboard_token": str(onboard_token or "").strip(),
        "ssid": str(ssid or "").strip(),
        "password": str(password or ""),
        "hostname": str(hostname or "").strip(),
        "mqtt": {
            "broker_host": broker_host,
            "broker_port": broker_port,
            "username": "",
            "password": "",
            "use_tls": False,
            "active_profile": "sensorius",
        },
        "sensorius": {
            "instance_id": PI_HOSTNAME or "sensorius",
            "base_topic": "nodus",
            "reply_topic": f"sensorius/{PI_HOSTNAME or 'sensorius'}/onboard/reply",
        },
    }

def _extract_device_id_from_init_result(result: Dict[str, Any], fallback_hostname: str) -> str:
    body = result.get("body") if isinstance(result, dict) else None
    if not isinstance(body, dict):
        return str(fallback_hostname or "").strip()
    for key in ("device_id", "sensor_id", "hostname"):
        val = str(body.get(key) or "").strip()
        if val:
            return val
    return str(fallback_hostname or "").strip()

def post_itaot_init(payload: Dict[str, Any], timeout_sec: float = 8.0) -> Dict[str, Any]:
    """
    V2 onboarding bootstrap call.
    Posts minimal bootstrap payload to /itaot-init while Nodus is in AP mode.

    Returns normalized shape:
      {"ok": bool, "status_code": int, "body": dict|None, "error": str}
    """
    try:
        if not isinstance(payload, dict):
            return {"ok": False, "status_code": 0, "body": None, "error": "invalid_payload_type"}

        resp = requests.post(ITAOT_INIT_URL, json=payload, timeout=timeout_sec)
        status = int(getattr(resp, "status_code", 0) or 0)
        body: Optional[Dict[str, Any]] = None
        try:
            body_obj = resp.json()
            if isinstance(body_obj, dict):
                body = body_obj
        except Exception:
            body = None

        if status != 200:
            return {
                "ok": False,
                "status_code": status,
                "body": body,
                "error": "non_200_response",
            }
        if not body:
            return {
                "ok": False,
                "status_code": status,
                "body": None,
                "error": "malformed_response",
            }
        accepted = bool(body.get("accepted", False))
        rebooting = bool(body.get("rebooting", False))
        if not accepted:
            return {
                "ok": False,
                "status_code": status,
                "body": body,
                "error": "init_not_accepted",
            }
        if DEBUG:
            printDM(f"/itaot-init accepted={accepted} rebooting={rebooting}", location=f"{MODULE}.post_itaot_init")
        return {"ok": True, "status_code": status, "body": body, "error": ""}
    except Exception as e:
        return {"ok": False, "status_code": 0, "body": None, "error": str(e)}

# ---------- TOML edit utilities for hub settings ----------
def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def update_hub_clients(settings_path: str, new_sensor_id: str) -> bool:
    # CLIENTS is deprecated; discovery is automatic.
    return True

# ---------- update payload build ----------
def build_picow_settings_updates(
    pi_info: Dict[str, Any],
    time_settings: Dict[str, Any],
    host: str,
) -> list[Dict[str, Any]]:
    _hostname   = host
    ssid_resolved, psk_resolved = resolve_pi_wifi_credentials()
    broker_val = pi_info.get("broker", "") or mdns_hostname(PI_HOSTNAME)

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
      - POST /itaot-init with minimal bootstrap payload
      - Nodus reboots and publishes identity/metadata over MQTT after reconnect
    Returns: (success, device_id_or_hostname or None)
    """
    ssid_resolved, psk_resolved = resolve_pi_wifi_credentials()
    if not ssid_resolved:
        printDM("Cannot bootstrap Nodus: local SSID unknown", location=f"{MODULE}.ppcar")
        return (False, None)

    host_suffix = f"{int(time.time()) % 1000000:06d}"
    hostname = f"nodus-{host_suffix}"
    init_payload = _build_itaot_init_payload(
        ssid=ssid_resolved,
        password=psk_resolved,
        hostname=hostname,
        onboard_token=f"legacy-{uuid4().hex}",
    )

    try:
        result = post_itaot_init(init_payload, timeout_sec=11.0)
    except Exception as e:
        printDM(f"Failed posting {ITAOT_INIT_URL}: {e}", location=f"{MODULE}.ppcar")
        return (False, None)

    if not bool(result.get("ok", False)):
        printDM(f"{ITAOT_INIT_URL} failed: {result}", location=f"{MODULE}.ppcar")
        return (False, None)

    device_id = _extract_device_id_from_init_result(result, hostname)
    if DEBUG:
        printDM(f"{ITAOT_INIT_URL} success for device={device_id}", location=f"{MODULE}.ppcar")
    return (True, device_id or hostname)

# ---------- Public entrypoints (async) ----------
async def begin_onboarding_preview() -> Dict[str, Any]:
    """
    Preview for UI:
      - connect to Pico AP
      - no metadata HTTP fetch; identity/config arrives via MQTT after reboot
      - return only local bootstrap summary
    """
    ok24, reason24 = await asyncio.to_thread(_require_24ghz_or_abort)
    if not ok24:
        return {"error": f"Cannot start onboarding: {reason24}"}
        
    target_ap = (PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
    ok = await asyncio.to_thread(connect_to_ap, target_ap, PICOW_AP_PASSWORD, 3)
    if not ok:
        return {"error": f"Could not connect to {target_ap} AP"}

    now_info = get_pi_network_info()
    ssid_resolved, psk_resolved = await asyncio.to_thread(resolve_pi_wifi_credentials)
    return {
        "ssid": ssid_resolved or now_info.get("ssid", ""),
        "password": psk_resolved or now_info.get("password", ""),
        "broker": now_info.get("broker", ""),
        "note": "Device identity and metadata will be learned from MQTT after /itaot-init and reboot.",
    }

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
        
    target_ap = (PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
    ok = await asyncio.to_thread(connect_to_ap, target_ap, PICOW_AP_PASSWORD, 3)
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
