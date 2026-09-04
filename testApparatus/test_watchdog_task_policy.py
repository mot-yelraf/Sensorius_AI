"""Cover watchdog task criticality and sensor timeout handling.

The tests verify which supervised tasks may trigger process recovery and how
sensor heartbeat delays are classified.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sensorius.saiSensor as saiSensor
import sensorius.saiTaskSupervisor as saiTaskSupervisor
import sensorius.saiWatchdog as saiWatchdog
from sensorius.saiTaskSupervisor import TaskSupervisor
from sensorius.saiUtils import supervised_task


@pytest.mark.asyncio
async def test_supervisor_reports_crashes_and_applies_capped_backoff(monkeypatch):
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name="Recoverable Worker", fatal_on_error=False)
    calls = 0
    sleep_delays = []

    async def _crashing_worker():
        nonlocal calls
        calls += 1
        if calls >= 6:
            supervisor._shutdown = True
        raise RuntimeError("simulated failure")

    async def _capture_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(saiTaskSupervisor.asyncio, "sleep", _capture_sleep)

    await supervisor.runner(_crashing_worker, (), {}, "Recoverable Worker")

    assert sleep_delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert supervisor.task_issues["Recoverable Worker"]["issue_type"] == "task_crash"
    assert supervisor.task_issues["Recoverable Worker"]["count"] == 6
    assert "Recoverable Worker" in supervisor.failed_tasks


def test_successful_heartbeat_clears_task_crash_issue():
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name="Recovered Worker", fatal_on_error=False)
    supervisor.report_issue("Recovered Worker", "failed", issue_type="task_crash")
    supervisor.failed_tasks["Recovered Worker"] = time.monotonic()

    supervisor.feedthedogs("Recovered Worker")

    assert "Recovered Worker" not in supervisor.task_issues
    assert "Recovered Worker" not in supervisor.failed_tasks


@pytest.mark.asyncio
async def test_supervised_task_propagates_failure_to_restart_runner():
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name="Wrapped Worker", fatal_on_error=False)

    async def _fail():
        raise RuntimeError("wrapped failure")

    with pytest.raises(RuntimeError, match="wrapped failure"):
        await supervised_task("Wrapped Worker", _fail, supervisor)

    assert "Wrapped Worker" in supervisor.failed_tasks


@pytest.mark.asyncio
async def test_watchdog_nonfatal_failed_task_reports_issue(monkeypatch):
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name="Daily Summary Writer", fatal_on_timeout=False, fatal_on_error=False)
    supervisor.failed_tasks["Daily Summary Writer"] = time.monotonic()

    exit_calls = []

    async def _fake_force_process_exit(reason: str, code: int):
        exit_calls.append((reason, code))
        raise AssertionError("watchdog should not hard-exit for non-fatal task failures")

    async def _cancel_sleep(_delay: float):
        raise asyncio.CancelledError()

    monkeypatch.setattr(saiWatchdog, "_force_process_exit", _fake_force_process_exit)
    monkeypatch.setattr(saiWatchdog.asyncio, "sleep", _cancel_sleep)

    await saiWatchdog.WatchdogMonitor(supervisor, timeout=1)

    assert exit_calls == []
    assert "Daily Summary Writer" not in supervisor.failed_tasks
    assert supervisor.task_issues["Daily Summary Writer"]["issue_type"] == "failed_task"


@pytest.mark.asyncio
async def test_sensor_data_collection_timeout_marks_sensor_not_present_and_reports_issue(monkeypatch):
    controller = object.__new__(saiSensor.SensorController)
    controller.sensor = SimpleNamespace(
        present=True,
        sensor_id="co2-test123",
        meas_status="",
        meas_interval=0.01,
        publish_interval=0.01,
        read_sensor_data=lambda: time.sleep(0.05),
    )
    controller.sensor_id = "co2-test123"
    controller.supervisor = TaskSupervisor()
    controller.supervisor.add(
        lambda: None,
        name="co2-test123 Data Collection",
        fatal_on_timeout=False,
        fatal_on_error=False,
    )
    controller.data_logger = SimpleNamespace(log_readings=lambda *args, **kwargs: None)
    controller.meas_interval = 0.01
    controller._last_read_error_log = 0.0
    controller._read_error_log_interval_s = 30.0
    controller._sensor_read_timeout_s = 0.01
    controller._db_write_timeout_s = 1.0

    async def _cancel_sleep(_delay: float):
        raise asyncio.CancelledError()

    monkeypatch.setattr(saiSensor.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await saiSensor.SensorController.data_collection(controller)

    assert controller.sensor.present is False
    assert controller.sensor.meas_status == "pending"
    assert controller.supervisor.task_issues["co2-test123 Data Collection"]["issue_type"] == "sensor_timeout"


@pytest.mark.asyncio
async def test_fixed_period_sensor_sleep_accounts_for_read_duration(monkeypatch):
    controller = object.__new__(saiSensor.SensorController)

    def _read_sensor_data():
        time.sleep(0.05)
        return None, None, None

    controller.sensor = SimpleNamespace(
        present=True,
        sensor_id="voc-test123",
        meas_status="",
        meas_interval=1.0,
        publish_interval=60.0,
        fixed_period_sampling=True,
        read_sensor_data=_read_sensor_data,
    )
    controller.sensor_id = "voc-test123"
    controller.supervisor = SimpleNamespace(feedthedogs=lambda *_args: None)
    controller.data_logger = SimpleNamespace(log_readings=lambda *_args, **_kwargs: None)
    controller.meas_interval = 1.0
    controller._last_read_error_log = 0.0
    controller._read_error_log_interval_s = 30.0
    controller._sensor_read_timeout_s = 1.0
    controller._db_write_timeout_s = 1.0
    sleep_calls = []

    async def _capture_sleep(delay):
        sleep_calls.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(saiSensor.asyncio, "sleep", _capture_sleep)

    with pytest.raises(asyncio.CancelledError):
        await saiSensor.SensorController.data_collection(controller)

    assert 0.80 < sleep_calls[0] < 0.99
