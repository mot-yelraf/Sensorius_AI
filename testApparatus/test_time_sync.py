from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiTimeSync import (
    TimeSyncService,
    discover_nodus_time_targets,
    find_next_time_transition,
    time_values_for_zone,
)


class _FakeSettings:
    device_id = "sensorius-hub"

    def __init__(self, values: dict[tuple[str, str], object] | None = None):
        self.values = {
            ("Network", "HOSTNAME"): "sensorius-hub",
            ("Time", "TZ"): "America/Denver",
            ("Time", "TZ_OFFSET"): -25200,
            ("Time", "TZ_NAME"): "MST",
        }
        self.values.update(values or {})
        self.saved = 0

    def get_setting(self, section: str, key: str, default=None):
        return self.values.get((section, key), default)

    def set_many_in_memory(self, updates):
        for section, key, value in updates:
            self.values[(section, key)] = value

    def save_settings(self):
        self.saved += 1


class _FakeIngest:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish_nodus_config(self, device_id: str, *, payload: dict, message_id: str | None = None, qos: int = 1, restart: bool = False, onboard_token: str = ""):
        mid = message_id or f"cfg-{len(self.published) + 1}"
        self.published.append((device_id, dict(payload or {})))
        return {"ok": True, "message_id": mid, "topic": f"nodus/{device_id}/config/set"}

    async def wait_for_config_ack(self, message_id: str, timeout: float = 0):
        return {"message_id": message_id, "accepted": True}

    async def wait_for_config_result(self, message_id: str, timeout: float = 0):
        return {"message_id": message_id, "applied": True}


class _LateAckIngest(_FakeIngest):
    async def wait_for_config_ack(self, message_id: str, timeout: float = 0):
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _write_nodus_sensor(root: Path, sensor_id: str) -> None:
    _write_text(
        root / sensor_id / "sensor.toml",
        f"""
        [Sensor]
        TYPE = "nodus"
        DEVICE = "aqi"
        SENSOR_ID = "{sensor_id}"
        LOCATION = "Room"
        """,
    )


def _write_nodus_switch(root: Path, switch_id: str) -> None:
    _write_text(
        root / switch_id / "switch.toml",
        f"""
        [Switch]
        TYPE = "nodus"
        DEVICE = "switch"
        SWITCH_DEVICE_ID = "{switch_id}"
        SWITCH_LOCATION = "Room"
        """,
    )


def _write_system_settings(root: Path, system_id: str, hostname: str, *, offset: int = -25200, name: str = "MST") -> None:
    _write_text(
        root / system_id / "settings.toml",
        f"""
        [Network]
        HOSTNAME = "{hostname}"

        [Profile]
        ACTIVE_PROFILE = "sensorius"

        [Time]
        TZ = "America/Denver"
        TZ_OFFSET = {offset}
        TZ_NAME = "{name}"
        """,
    )


def _write_factory_nodus_system_settings(root: Path) -> None:
    _write_text(
        root / "factory_nodus" / "settings.toml.def",
        """
        [Network]
        HOSTNAME = ""

        [Time]
        TZ = "America/Denver"
        TZ_OFFSET = -25200
        TZ_NAME = "MST"

        [MQTT]
        BROKER = ""
        """,
    )


def _read_toml(path: Path) -> dict:
    assert tomllib is not None
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_zoneinfo_reports_denver_dst_transition():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    transition = find_next_time_transition("America/Denver", start_utc=start)

    assert transition is not None
    before = time_values_for_zone("America/Denver", transition - timedelta(minutes=2))
    after = time_values_for_zone("America/Denver", transition)
    assert before is not None and after is not None
    assert before["TZ_OFFSET"] != after["TZ_OFFSET"] or before["TZ_NAME"] != after["TZ_NAME"]


def test_zoneinfo_returns_no_transition_for_utc():
    assert find_next_time_transition("UTC", start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), horizon_days=370) is None


@pytest.mark.asyncio
async def test_time_sync_updates_hub_pushes_nodus_and_updates_shadow(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-test123"
    _write_nodus_sensor(sensor_root, sensor_id)
    _write_system_settings(system_root, sensor_id, sensor_id, offset=-25200, name="MST")

    settings = _FakeSettings()
    ingest = _FakeIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["ok"] is True
    assert settings.get_setting("Time", "TZ_OFFSET") == -21600
    assert settings.get_setting("Time", "TZ_NAME") == "MDT"
    assert settings.saved == 1
    assert [row[0] for row in ingest.published] == [sensor_id, sensor_id]
    updates = [row[1]["updates"][0] for row in ingest.published]
    assert [(item["section"], item["key"], item["value"]) for item in updates] == [
        ("Time", "TZ_OFFSET", -21600),
        ("Time", "TZ_NAME", "MDT"),
    ]

    shadow = _read_toml(system_root / sensor_id / "settings.toml")
    assert shadow["Time"]["TZ_OFFSET"] == -21600
    assert shadow["Time"]["TZ_NAME"] == "MDT"


@pytest.mark.asyncio
async def test_time_sync_result_allows_late_ack_and_updates_shadow(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-test123"
    _write_nodus_sensor(sensor_root, sensor_id)
    _write_system_settings(system_root, sensor_id, sensor_id, offset=-25200, name="MST")

    settings = _FakeSettings({("Time", "TZ_OFFSET"): -21600, ("Time", "TZ_NAME"): "MDT"})
    ingest = _LateAckIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["ok"] is True
    assert result["pushed"] == [sensor_id]
    updates = [row[1]["updates"][0] for row in ingest.published]
    assert [(item["section"], item["key"], item["value"]) for item in updates] == [
        ("Time", "TZ_OFFSET", -21600),
        ("Time", "TZ_NAME", "MDT"),
    ]
    shadow = _read_toml(system_root / sensor_id / "settings.toml")
    assert shadow["Time"]["TZ_OFFSET"] == -21600
    assert shadow["Time"]["TZ_NAME"] == "MDT"


@pytest.mark.asyncio
async def test_time_sync_resumes_from_partial_shadow_without_repeating_keys(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-test123"
    _write_nodus_sensor(sensor_root, sensor_id)
    _write_system_settings(system_root, sensor_id, sensor_id, offset=-21600, name="MST")

    settings = _FakeSettings({("Time", "TZ_OFFSET"): -21600, ("Time", "TZ_NAME"): "MDT"})
    ingest = _FakeIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["ok"] is True
    updates = [row[1]["updates"][0] for row in ingest.published]
    assert [(item["section"], item["key"], item["value"]) for item in updates] == [
        ("Time", "TZ_NAME", "MDT"),
    ]
    shadow = _read_toml(system_root / sensor_id / "settings.toml")
    assert shadow["Time"]["TZ"] == "America/Denver"
    assert shadow["Time"]["TZ_OFFSET"] == -21600
    assert shadow["Time"]["TZ_NAME"] == "MDT"


@pytest.mark.asyncio
async def test_time_sync_creates_missing_nodus_shadow_from_factory(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-test123"
    _write_factory_nodus_system_settings(system_root)
    _write_nodus_sensor(sensor_root, sensor_id)

    settings = _FakeSettings({("Time", "TZ_OFFSET"): -21600, ("Time", "TZ_NAME"): "MDT"})
    ingest = _FakeIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["ok"] is True
    assert [row[0] for row in ingest.published] == [sensor_id, sensor_id, sensor_id]
    shadow = _read_toml(system_root / sensor_id / "settings.toml")
    assert shadow["Network"]["HOSTNAME"] == sensor_id
    assert "MQTT" in shadow
    assert shadow["Time"]["TZ"] == "America/Denver"
    assert shadow["Time"]["TZ_OFFSET"] == -21600
    assert shadow["Time"]["TZ_NAME"] == "MDT"


@pytest.mark.asyncio
async def test_time_sync_skips_nodus_when_shadow_already_matches(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-test123"
    _write_nodus_sensor(sensor_root, sensor_id)
    _write_system_settings(system_root, sensor_id, sensor_id, offset=-21600, name="MDT")

    settings = _FakeSettings({("Time", "TZ_OFFSET"): -21600, ("Time", "TZ_NAME"): "MDT"})
    ingest = _FakeIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["skipped"] == [sensor_id]
    assert ingest.published == []
    assert settings.saved == 0


@pytest.mark.asyncio
async def test_time_sync_skips_combined_sensor_switch_when_host_shadow_matches(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    sensor_id = "apvpd-abc123"
    switch_id = "switch-abc123"
    _write_nodus_sensor(sensor_root, sensor_id)
    _write_nodus_switch(switch_root, switch_id)
    _write_system_settings(system_root, sensor_id, sensor_id, offset=-21600, name="MDT")

    settings = _FakeSettings({("Time", "TZ_OFFSET"): -21600, ("Time", "TZ_NAME"): "MDT"})
    ingest = _FakeIngest()
    service = TimeSyncService(
        settings=settings,
        mqtt_ingest=ingest,
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
        interval_sec=60,
    )

    result = await service.sync_once(when_utc=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert result["skipped"] == [sensor_id]
    assert ingest.published == []
    assert not (system_root / switch_id / "settings.toml").exists()


def test_time_sync_discovers_shared_sensor_switch_host_once(tmp_path):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    _write_nodus_sensor(sensor_root, "apvpd-abc123")
    _write_nodus_switch(switch_root, "switch-abc123")
    _write_system_settings(system_root, "apvpd-abc123", "apvpd-abc123")
    _write_system_settings(system_root, "switch-abc123", "switch-abc123")

    targets = discover_nodus_time_targets(
        settings=_FakeSettings(),
        system_base_dir=system_root,
        sensor_base_dir=sensor_root,
        switch_base_dir=switch_root,
    )

    assert len(targets) == 1
    assert targets[0].hostname == "apvpd-abc123"
    assert targets[0].system_ids == {"apvpd-abc123"}
