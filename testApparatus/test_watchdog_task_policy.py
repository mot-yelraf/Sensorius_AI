"""Focused coverage for watchdog task criticality and sensor timeout handling."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sensorius.saiSensor as saiSensor
import sensorius.saiWatchdog as saiWatchdog
from sensorius.saiTaskSupervisor import TaskSupervisor


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
