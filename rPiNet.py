"""Network helpers for hostname, IP discovery, and interface info."""

import subprocess
import socket
import asyncio
from rPiUtils import printDM

MODULE = "rPiNet"

class rPiNetManager:
    def __init__(self, iface="wlan0"):
        self.iface = iface
        self.current_ssid = None

    def scan_wifi_networks(self):
        try:
            output = subprocess.check_output([
                "nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"
            ]).decode()
            ssids = [line.strip() for line in output.splitlines() if line.strip()]
            printDM(f"Available SSIDs: {ssids}", location=MODULE)
            return ssids
        except Exception as e:
            printDM(f"Wi-Fi scan failed: {e}", location=MODULE)
            return []

    def is_connected(self):
        try:
            output = subprocess.check_output([
                "nmcli", "-t", "-f", "active,ssid", "dev", "wifi"
            ]).decode()
            for line in output.splitlines():
                if line.startswith("yes:"):
                    self.current_ssid = line.split(":")[1]
                    return True
        except Exception:
            pass
        return False

    async def test_dns_connectivity(self, host="8.8.8.8"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "2", host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    def connect_to_network(self, ssid, password, timeout=10):
        try:
            subprocess.run(["nmcli", "dev", "disconnect", self.iface], check=False)
            subprocess.run([
                "nmcli", "dev", "wifi", "connect", ssid,
                "password", password, "ifname", self.iface
            ], check=True, timeout=timeout)
            printDM(f"Connected to {ssid}", location=MODULE)
            return True
        except Exception as e:
            printDM(f"Failed to connect to {ssid}: {e}", location=MODULE)
            return False

    def start_access_point(self, ssid="rPiHub_AP", password="raspberry", static_ip="192.168.4.100"):
        try:
            subprocess.run(["nmcli", "radio", "wifi", "off"], check=True)
            subprocess.run(["rfkill", "unblock", "wifi"], check=True)
            subprocess.run(["ip", "link", "set", self.iface, "up"], check=True)

            subprocess.run(["ip", "addr", "flush", "dev", self.iface], check=True)
            subprocess.run(["ip", "addr", "add", f"{static_ip}/24", "dev", self.iface], check=True)

            subprocess.run([
                "nmcli", "device", "wifi", "hotspot",
                "ifname", self.iface,
                "con-name", "rPiHubHotspot",
                "ssid", ssid,
                "band", "bg",
                "password", password
            ], check=True)

            printDM(f"Started AP mode on {self.iface} \u2014 SSID: {ssid}, IP: {static_ip}, Port: 8000", location=MODULE)
            return True
        except Exception as e:
            printDM(f"Failed to start AP mode: {e}", location=MODULE)
            return False

    def stop_access_point(self):
        try:
            subprocess.run(["nmcli", "connection", "down", "rPiHubHotspot"], check=False)
            subprocess.run(["nmcli", "radio", "wifi", "on"], check=True)
            printDM("Access Point stopped", location=MODULE)
        except Exception as e:
            printDM(f"Failed to stop AP: {e}", location=MODULE)

    def get_current_ssid(self):
        return self.current_ssid or "Unknown"

    def connect_or_ap_mode(self, ssid, password, fallback_ap_ssid="rPiHub_AP", fallback_ap_password="raspberry"):
        if ssid in self.scan_wifi_networks():
            if self.connect_to_network(ssid, password):
                printDM("Successfully connected to known Wi-Fi.", location=MODULE)
                return "connected"
            else:
                printDM("Known SSID found, but connection failed.", location=MODULE)
        else:
            printDM("SSID not found during scan.", location=MODULE)

        printDM("Falling back to AP mode...", location=MODULE)
        success = self.start_access_point(ssid=fallback_ap_ssid, password=fallback_ap_password)
        return "ap" if success else "offline"
