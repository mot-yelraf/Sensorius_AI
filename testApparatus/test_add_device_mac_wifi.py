"""Pytest coverage for Add Device Wi-Fi helpers on macOS and Linux.

These tests validate the platform-specific join and reconnect flows used during
onboarding without requiring real network changes.
"""

from __future__ import annotations

import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiAddDevice as saiAddDevice

def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_mac_connect_wifi_falls_back_to_preferred_network_retry(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(saiAddDevice.time, "sleep", lambda *_args, **_kwargs: None)

    seen_cmds: list[list[str]] = []
    join_calls = {"count": 0}

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        seen_cmds.append(list(cmd))
        if cmd[:2] == ["networksetup", "-setairportnetwork"]:
            join_calls["count"] += 1
            if join_calls["count"] == 1:
                return _cp(returncode=1, stderr="Error: -3900 tmpErr")
            return _cp(returncode=0)
        if cmd[:2] == ["networksetup", "-addpreferredwirelessnetworkatindex"]:
            return _cp(returncode=0)
        if cmd[:2] == ["networksetup", "-removepreferredwirelessnetwork"]:
            return _cp(returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    ssids = iter(["ExampleWiFi", "ExampleWiFi", "Nodus_Setup"])
    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)
    monkeypatch.setattr(saiAddDevice, "_get_current_ssid", lambda: next(ssids))

    ok = saiAddDevice._connect_wifi("Nodus_Setup", "password", "en0")

    assert ok is True
    assert any(cmd[:2] == ["networksetup", "-addpreferredwirelessnetworkatindex"] for cmd in seen_cmds)
    assert join_calls["count"] == 2


def test_mac_connect_wifi_returns_false_when_join_never_lands(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "_current_platform", lambda: "darwin")

    def _fake_run(cmd, capture_output=False, text=False, timeout=None):
        if cmd[:2] == ["networksetup", "-setairportnetwork"]:
            return _cp(returncode=0)
        if cmd[:2] == ["networksetup", "-addpreferredwirelessnetworkatindex"]:
            return _cp(returncode=0)
        if cmd[:2] == ["networksetup", "-removepreferredwirelessnetwork"]:
            return _cp(returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(saiAddDevice.subprocess, "run", _fake_run)
    monkeypatch.setattr(saiAddDevice, "_get_current_ssid", lambda: "ExampleWiFi")
    monkeypatch.setattr(saiAddDevice.time, "sleep", lambda *_args, **_kwargs: None)

    ok = saiAddDevice._connect_wifi("Nodus_Setup", "password", "en0")

    assert ok is False


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
