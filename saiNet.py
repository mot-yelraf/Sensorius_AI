"""Network connectivity and Wi-Fi mode helpers for Sensorius host nodes.

This module wraps common network-management actions used during startup and
onboarding flows:
- scan available Wi-Fi SSIDs
- detect active Wi-Fi association
- test practical network/DNS reachability
- connect to a configured infrastructure network
- fall back to local Access Point mode when needed

Implementation notes:
- Uses Linux tools such as `nmcli`, `ip`, and `rfkill`.
- Designed for single-interface management (default: `wlan0`).
- Returns simple success/failure values and logs operational context via
  `printDM` for diagnostics.
"""

import subprocess
import socket
import asyncio
from saiUtils import printDM

MODULE = "saiNet"

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
        except Exception as e:
            printDM(f"is_connected() check failed: {e}", location=MODULE)
        return False

    async def test_dns_connectivity(self, host="example.com", timeout=2.0):
        """
        Verify connectivity via DNS resolution (not ICMP ping).
        Falls back to a quick TCP:53 probe to detect networks where DNS
        lookup is blocked/misconfigured but routing is otherwise up.
        """
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.getaddrinfo(host, 53, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
                timeout=timeout,
            )
            return True
        except Exception as dns_err:
            try:
                conn = await asyncio.wait_for(asyncio.open_connection("1.1.1.1", 53), timeout=timeout)
                reader, writer = conn
                writer.close()
                await writer.wait_closed()
                printDM(
                    f"DNS resolution failed for '{host}' ({dns_err}); network path still reachable via TCP/53.",
                    location=MODULE,
                )
                return True
            except Exception as net_err:
                printDM(
                    f"DNS connectivity test failed for '{host}': dns={dns_err}; tcp53={net_err}",
                    location=MODULE,
                )
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
