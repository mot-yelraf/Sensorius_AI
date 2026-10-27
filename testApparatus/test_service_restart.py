"""Verify restart eligibility and manager commands without restarting services.

Probes use controlled process identities and platform outputs so manual launches,
disabled entries, and unrelated service processes cannot enable a restart.
"""

import os
import plistlib
from types import SimpleNamespace

import pytest

from sensorius import saiServiceRestart as restart


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    monkeypatch.setattr(restart, "_output", lambda command: "")


@pytest.mark.parametrize("scope", ["system", "user"])
def test_linux_finds_actual_scope_without_saved_settings(monkeypatch, scope):
    monkeypatch.setattr(restart.platform, "system", lambda: "Linux")
    def output(command):
        if ("--user" in command) == (scope == "user"):
            return f"MainPID={os.getpid()}\nActiveState=active\nUnitFileState=enabled"
        return "MainPID=999999\nActiveState=active\nUnitFileState=enabled"
    monkeypatch.setattr(restart, "_output", output)
    target = restart.find_restart_service()
    assert target is not None
    assert ("--user" in target.command) == (scope == "user")
    assert target.command[-2:] == ("restart", "sensorius.service")
    assert "--no-ask-password" in target.command


@pytest.mark.parametrize("state,pid,active", [
    ("disabled", None, "active"), ("static", None, "active"),
    ("enabled-runtime", None, "active"), ("enabled", 999999, "active"),
    ("enabled", None, "inactive"),
])
def test_linux_rejects_disabled_or_unrelated_processes(monkeypatch, state, pid, active):
    monkeypatch.setattr(restart.platform, "system", lambda: "Linux")
    monkeypatch.setattr(restart, "_output", lambda command:
                        f"MainPID={pid or os.getpid()}\nActiveState={active}\nUnitFileState={state}")
    assert restart.find_restart_service() is None


@pytest.mark.parametrize("label", ["com.sensorius.app", "com.sensorius.sensorius"])
def test_mac_supports_settings_and_installer_labels(monkeypatch, tmp_path, label):
    monkeypatch.setattr(restart.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(restart.Path, "home", lambda: tmp_path)
    directory = tmp_path / "Library/LaunchAgents"
    directory.mkdir(parents=True)
    (directory / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "RunAtLoad": True}))
    monkeypatch.setattr(restart, "_output", lambda command:
                        '{}' if command[1] == "print-disabled" else f"\tpid = {os.getpid()}\n")
    target = restart.find_restart_service()
    assert target.command == ("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
    monkeypatch.setattr(restart, "_output", lambda command: f'"{label}" => true')
    assert restart.find_restart_service() is None
    monkeypatch.setattr(restart, "_output", lambda command:
                        '{}' if command[1] == "print-disabled" else "\tpid = 999999\n")
    assert restart.find_restart_service() is None


def test_missing_manager_disables_restart(monkeypatch):
    monkeypatch.setattr(restart.platform, "system", lambda: "Linux")
    def missing(command):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(restart, "_output", missing)
    assert restart.find_restart_service() is None


def test_windows_only_accepts_known_verified_task_output(monkeypatch):
    monkeypatch.setattr(restart.platform, "system", lambda: "Windows")
    def task_probe(command):
        assert "$engine -le 0 -or $engine -notin $ancestors" in command[-1]
        assert "$registered.Enabled" in command[-1]
        assert "$actions.Count -gt 0" in command[-1]
        return "SensoriusStartupUser"
    monkeypatch.setattr(restart, "_output", task_probe)
    target = restart.find_restart_service()
    assert target.detached
    assert "GetTask('SensoriusStartupUser')" in target.command[-1]
    assert "$task.Stop(0)" in target.command[-1]
    assert "$task.Run($null)" in target.command[-1]
    monkeypatch.setattr(restart, "_output", lambda command: "unrelated-task")
    assert restart.find_restart_service() is None


def test_manager_failure_is_reported(monkeypatch):
    monkeypatch.setattr(restart.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stderr="Permission denied", stdout=""))
    with pytest.raises(RuntimeError, match="Permission denied"):
        restart.restart_service(restart.RestartService("launchd", ("launchctl", "kickstart", "-k", "target")))


def test_system_service_can_use_existing_passwordless_sudo(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0 if command[0] == "sudo" else 1, stderr="denied", stdout="")
    monkeypatch.setattr(restart.subprocess, "run", run)
    target = restart.RestartService("systemd", ("systemctl", "--no-ask-password", "--no-block", "restart", "sensorius.service"))
    restart.restart_service(target)
    assert calls == [target.command, ("sudo", "-n", *target.command)]
