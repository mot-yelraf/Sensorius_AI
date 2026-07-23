"""Regression coverage for keeping runtime settings out of the source checkout."""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.saiRuntimePaths import resolve_runtime_base_dir


def _non_pytest_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    home.mkdir()
    checkout.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.chdir(checkout)
    return home, checkout


def test_bare_settings_roots_resolve_to_runtime_home_outside_pytest(tmp_path, monkeypatch):
    home, checkout = _non_pytest_runtime(tmp_path, monkeypatch)

    assert resolve_runtime_base_dir("system_settings") == home / "Sensorius" / "system_settings"
    assert resolve_runtime_base_dir("sensor_settings") == home / "Sensorius" / "sensor_settings"
    assert resolve_runtime_base_dir("switch_settings") == home / "Sensorius" / "switch_settings"
    assert not (checkout / "system_settings").exists()
    assert not (checkout / "sensor_settings").exists()
    assert not (checkout / "switch_settings").exists()


def test_bare_settings_roots_use_test_runtime_root_inside_pytest(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    runtime = tmp_path / "pytest-runtime"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_runtime_path_boundaries.py::test")
    monkeypatch.setenv("SENSORIUS_TEST_RUNTIME_ROOT", str(runtime))

    assert resolve_runtime_base_dir("system_settings") == runtime / "system_settings"
    assert resolve_runtime_base_dir("sensor_settings") == runtime / "sensor_settings"
    assert resolve_runtime_base_dir("switch_settings") == runtime / "switch_settings"
    assert resolve_runtime_base_dir("relative_data") == checkout / "relative_data"


def test_onboarding_and_automation_writers_use_runtime_roots(tmp_path, monkeypatch):
    home, checkout = _non_pytest_runtime(tmp_path, monkeypatch)
    runtime = home / "Sensorius"

    import sensorius.saiAddDevice as saiAddDevice
    from sensorius.saiAutomationManager import AutomationManager
    from sensorius.saiOnboardingStore import OnboardingSessionStore
    from sensorius.saiSwitchTriggerManager import SwitchTriggerManager

    monkeypatch.setattr(saiAddDevice, "_SYS_BASE_DIR", "system_settings")
    monkeypatch.setattr(saiAddDevice, "_SENSOR_BASE_DIR", "sensor_settings")
    monkeypatch.setattr(saiAddDevice, "_SWITCH_BASE_DIR", "switch_settings")

    system_path = saiAddDevice.persist_system_settings_by_device_id(
        [{"section": "Network", "key": "HOSTNAME", "value": "apvpd-test123"}]
    )
    sensor_path = saiAddDevice.persist_sensor_toml(
        "apvpd-test123",
        "sensor.toml",
        "base64",
        base64.b64encode(b"[Sensor]\nSENSOR_ID = \"apvpd-test123\"\n").decode("ascii"),
    )
    switch_path = saiAddDevice.persist_switch_toml(
        "switch-test123",
        "base64",
        base64.b64encode(b"[Switch]\nSWITCH_DEVICE_ID = \"switch-test123\"\n").decode("ascii"),
    )
    onboarding_store = OnboardingSessionStore(base_dir="system_settings")
    automation_path = AutomationManager("switch_settings").get_storage_path()
    trigger_path = SwitchTriggerManager("switch_settings").get_storage_path()

    assert str(system_path).startswith(str(runtime / "system_settings"))
    assert str(sensor_path).startswith(str(runtime / "sensor_settings"))
    assert str(switch_path).startswith(str(runtime / "switch_settings"))
    assert str(onboarding_store._root).startswith(str(runtime / "system_settings"))
    assert str(automation_path).startswith(str(runtime / "switch_settings"))
    assert str(trigger_path).startswith(str(runtime / "switch_settings"))

    assert not (checkout / "system_settings").exists()
    assert not (checkout / "sensor_settings").exists()
    assert not (checkout / "switch_settings").exists()
