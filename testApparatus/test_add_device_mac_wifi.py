"""Pytest coverage for Add Device Wi-Fi helpers on macOS and Linux.

These tests validate the platform-specific join and reconnect flows used during
onboarding without requiring real network changes.
"""

from __future__ import annotations

import os
import plistlib
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiAddDevice as saiAddDevice


def test_nodus_setup_ssid_recognizes_serial_and_legacy_names():
    assert saiAddDevice.is_nodus_setup_ssid("Nodus-123456") is True
    assert saiAddDevice.is_nodus_setup_ssid("Nodus-AB12.cd_3") is True
    assert saiAddDevice.is_nodus_setup_ssid("Nodus_Setup") is True
    assert saiAddDevice.is_nodus_setup_ssid("Nodus-Setup") is True
    assert saiAddDevice.is_nodus_setup_ssid("ExampleWiFi") is False
    assert saiAddDevice.is_nodus_setup_ssid("Nodus-") is False


def test_itaot_init_read_timeout_is_indeterminate(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise saiAddDevice.requests.exceptions.ReadTimeout("response lost")

    monkeypatch.setattr(saiAddDevice.requests, "post", _timeout)

    result = saiAddDevice.post_itaot_init({"ssid": "ExampleWiFi"}, timeout_sec=1)

    assert result["ok"] is False
    assert result["indeterminate"] is True
    assert result["status_code"] == 0


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_mac_connect_wifi_does_not_mutate_preferred_networks(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(
        saiAddDevice.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("macOS Wi-Fi must not be changed")),
    )

    ok = saiAddDevice._connect_wifi("Nodus_Setup", "password", "en0")

    assert ok is False


def test_mac_reconnect_returns_false_without_network_mutation(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(saiAddDevice, "_wifi_interface_name", lambda: "en1")
    monkeypatch.setattr(saiAddDevice.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        saiAddDevice.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("macOS Wi-Fi must not be changed")),
    )

    ok, ssid = saiAddDevice.reconnect_to_network("ExampleWiFi", "password", max_attempts=1, delay_sec=0)

    assert ok is False
    assert ssid == "ExampleWiFi"


def test_mac_wifi_interface_prefers_system_preferences(tmp_path, monkeypatch):
    preferences = tmp_path / "NetworkInterfaces.plist"
    preferences.write_bytes(plistlib.dumps({"Interfaces": [
        {"BSD Name": "en0", "SCNetworkInterfaceType": "Ethernet"},
        {"BSD Name": "en1", "SCNetworkInterfaceType": "IEEE80211"},
    ]}))

    assert saiAddDevice._mac_wifi_interface_from_preferences(preferences) == "en1"

    monkeypatch.setattr(saiAddDevice, "_mac_wifi_interface_from_preferences", lambda: "en1")
    monkeypatch.setattr(
        saiAddDevice.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("networksetup must not run")),
    )
    assert saiAddDevice._mac_wifi_interface() == "en1"


def test_mac_wifi_nodus_subnet_uses_ipconfig_without_networksetup(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(saiAddDevice, "_mac_wifi_interface", lambda: "en1")
    monkeypatch.setattr(saiAddDevice.subprocess, "check_output", lambda cmd, **_kwargs: "192.168.4.2\n")

    assert saiAddDevice.mac_wifi_ipv4() == "192.168.4.2"
    assert saiAddDevice.mac_wifi_is_on_nodus_subnet() is True


def test_linux_reconnect_returns_success_after_connect(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")
    monkeypatch.setattr(saiAddDevice, "_wifi_interface_name", lambda: "wlan0")
    monkeypatch.setattr(saiAddDevice, "_ssid_visible_linux", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(saiAddDevice, "_connect_wifi", lambda *_args, **_kwargs: True)

    ok, ssid = saiAddDevice.reconnect_to_network("ExampleWiFi", "pw", max_attempts=1, delay_sec=0)

    assert ok is True
    assert ssid == "ExampleWiFi"


def test_linux_reconnect_returns_failure_when_reconnect_fails(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")
    monkeypatch.setattr(saiAddDevice, "_wifi_interface_name", lambda: "wlan0")
    monkeypatch.setattr(saiAddDevice, "_ssid_visible_linux", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(saiAddDevice, "_connect_wifi", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saiAddDevice.time, "sleep", lambda *_args, **_kwargs: None)

    ok, ssid = saiAddDevice.reconnect_to_network("ExampleWiFi", "pw", max_attempts=1, delay_sec=0)

    assert ok is False
    assert ssid == "ExampleWiFi"


def test_linux_network_control_permission_status_allows_yes(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        assert cmd == ["nmcli", "-t", "-f", "PERMISSION,VALUE", "general", "permissions"]
        return _cp(stdout="\n".join([
            "org.freedesktop.NetworkManager.network-control:yes",
            "org.freedesktop.NetworkManager.wifi.scan:yes",
        ]))

    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)

    ok, detail = saiAddDevice.linux_network_control_permission_status()

    assert ok is True
    assert detail == "network_control=yes,wifi_scan=yes"


def test_linux_network_control_permission_status_blocks_auth(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        return _cp(stdout="org.freedesktop.NetworkManager.network-control:auth\n")

    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)

    ok, detail = saiAddDevice.linux_network_control_permission_status()

    assert ok is False
    assert detail == "network_control=auth"


def test_linux_network_control_permission_status_treats_scan_denial_as_advisory(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        return _cp(stdout="\n".join([
            "org.freedesktop.NetworkManager.network-control:yes",
            "org.freedesktop.NetworkManager.wifi.scan:auth",
            "org.freedesktop.NetworkManager.settings.modify.system:yes",
        ]))

    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)

    ok, detail = saiAddDevice.linux_network_control_permission_status()

    assert ok is True
    assert detail == "network_control=yes,wifi_scan=auth,settings_modify_system=yes,settings_modify_own=unknown"


def test_linux_network_control_permission_status_blocks_profile_modify_denial(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "linux")

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        return _cp(stdout="\n".join([
            "org.freedesktop.NetworkManager.network-control:yes",
            "org.freedesktop.NetworkManager.settings.modify.system:auth",
            "org.freedesktop.NetworkManager.settings.modify.own:no",
        ]))

    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)

    ok, detail = saiAddDevice.linux_network_control_permission_status()

    assert ok is False
    assert detail == "settings_modify_system=auth,settings_modify_own=no"
