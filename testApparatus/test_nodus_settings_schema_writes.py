from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiAddDevice
import saiSensorSettingsManager
import saiSwitchSettingsManager
import saiWebRoutes

_REAL_SENSOR_SETTINGS_MANAGER = saiSensorSettingsManager.SensorSettingsManager
_REAL_SWITCH_SETTINGS_MANAGER = saiSwitchSettingsManager.SwitchSettingsManager


class _DummyFastStats:
    def __init__(self, *_args, **_kwargs):
        pass

    async def start(self):
        return

    def stop(self):
        return


class _HubSettings:
    def get_all_sensor_ids(self):
        return []

    def get_setting(self, _section, _key, default=None):
        return default


class _FakeNetMgr:
    pass


class _FakeGcMgr:
    pass


class _FakeIngest:
    def __init__(self):
        self.added: list[str] = []
        self.refreshed: list[str] = []
        self.mqtt_clients: list[str] = []

    def set_onboarding_event_handler(self, handler):
        self.handler = handler

    def resolve_nodus_hostname(self, *_args, **_kwargs):
        return None

    def add_client(self, host: str):
        self.added.append(host)

    async def force_refresh_device_metadata(self, device_id: str):
        self.refreshed.append(device_id)

    def publish_json(self, *_args, **_kwargs):
        return True


class _FakeSaiSettings:
    DEFAULT_BASE_DIR = ""
    STANDARD_FILENAME = "settings.toml"

    def __init__(self, *args, **_kwargs):
        self.settings_root = self.DEFAULT_BASE_DIR
        self.system_dir = self.DEFAULT_BASE_DIR

    def get_setting(self, _section, _key, default=None):
        return default

    @staticmethod
    def obfuscate_secret(value):
        return value

    @staticmethod
    def deobfuscate_secret(value):
        return value


class _FakeSystemSettingsManager:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def get_setting(self, device_id: str, dotted_key: str, default=None):
        section, key = dotted_key.split(".", 1)
        path = self.base_dir / device_id / "settings.toml"
        if not path.exists():
            return default
        data = path.read_text(encoding="utf-8")
        block = {}
        current = None
        for line in data.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1]
                block.setdefault(current, {})
                continue
            if "=" not in stripped or current is None:
                continue
            k, v = [part.strip() for part in stripped.split("=", 1)]
            block[current][k] = v.strip('"')
        return block.get(section, {}).get(key, default)


class _RecordedResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {"success": True}
        self.text = ""

    def json(self):
        return dict(self._payload)


class _RecordingAsyncClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict | None = None):
        self.__class__.calls.append({"url": url, "json": dict(json or {})})
        return _RecordedResponse()


def _write_system_settings(root: Path, device_id: str, hostname: str) -> None:
    target = root / device_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "settings.toml").write_text(
        f'[Network]\nHOSTNAME = "{hostname}"\n',
        encoding="utf-8",
    )


async def _build_app(tmp_path, monkeypatch):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root.mkdir()
    sensor_root.mkdir()
    switch_root.mkdir()

    _FakeSaiSettings.DEFAULT_BASE_DIR = str(system_root)

    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _FakeSaiSettings)
    monkeypatch.setattr(saiWebRoutes, "SystemSettingsManager", _FakeSystemSettingsManager, raising=False)
    monkeypatch.setattr(saiWebRoutes, "SensorSettingsManager", lambda *_a, **_k: _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)))
    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
    monkeypatch.setattr(saiWebRoutes.httpx, "AsyncClient", _RecordingAsyncClient)

    app = FastAPI()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), ingest)
    return app, ingest, system_root, sensor_root, switch_root


def test_build_picow_settings_updates_uses_profile_and_mqtt(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "resolve_pi_wifi_credentials", lambda: ("MyWiFi", "secret"))
    monkeypatch.setattr(saiAddDevice, "PI_HOSTNAME", "sensorius-main")

    updates = saiAddDevice.build_picow_settings_updates(
        {"broker": "broker.local"},
        {"TZ": "America/Denver", "TZ_OFFSET": -25200, "TZ_NAME": "MST"},
        "aqi-test",
    )

    assert {"section": "Network", "key": "SSID", "value": "MyWiFi"} in updates
    assert {"section": "Network", "key": "PASSWORD", "value": "secret"} in updates
    assert {"section": "Profile", "key": "ACTIVE_PROFILE", "value": "sensorius"} in updates
    assert {"section": "MQTT", "key": "BROKER", "value": "broker.local"} in updates
    assert not any(item["section"] == "Network" and item["key"] == "BROKER" for item in updates)


@pytest.mark.asyncio
async def test_submit_sensor_settings_pushes_sensor_and_display_updates_for_nodus(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-123",
                "LOCATION": "Old Room",
            },
            "Display": {"METRIC_1": "Old"},
        },
    )
    _write_system_settings(system_root, "aqi-123", "aqi-123")
    _RecordingAsyncClient.calls.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "aqi-123",
                "sensor_id_field": "aqi-123",
                "device": "aqi",
                "location": "Grow Tent",
                "metric_1": "Temperature",
                "metric_2": "Humidity",
                "metric_3": "PM2.5",
                "metric_4": "",
                "metric_5": "",
                "metric_6": "",
            },
        )

    assert res.status_code == 303
    posted = [call["json"] for call in _RecordingAsyncClient.calls]
    assert any(p["section"] == "Sensor" and p["key"] == "DEVICE" and p["value"] == "aqi" for p in posted)
    assert any(p["section"] == "Sensor" and p["key"] == "SENSOR_ID" and p["value"] == "aqi-123" for p in posted)
    assert any(p["section"] == "Sensor" and p["key"] == "LOCATION" and p["value"] == "Grow Tent" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_1" and p["value"] == "Temperature" for p in posted)
    assert any(p.get("name") == "sensor_i2c.toml" for p in posted)
    assert ingest.refreshed == ["aqi-123"]


@pytest.mark.asyncio
async def test_submit_switch_settings_pushes_remote_updates_for_nodus(tmp_path, monkeypatch):
    app, _ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
            }
        },
    )
    _write_system_settings(system_root, "switch-123", "switch-123")
    _RecordingAsyncClient.calls.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={
                "switch_id": "switch-123",
                "switch_id_field": "switch-123",
                "device": "switch",
                "location": "Veg Rack",
                "SWITCH_1_LABEL": "Lights",
                "SWITCH_1_Trigger": "auto",
            },
        )

    assert res.status_code == 303
    posted = [call["json"] for call in _RecordingAsyncClient.calls]
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_LOCATION" and p["value"] == "Veg Rack" for p in posted)
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_1_LABEL" and p["value"] == "Lights" for p in posted)
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_1_Trigger" and p["value"] == "auto" for p in posted)
    assert all(p.get("name") == "switch.toml" for p in posted)


@pytest.mark.asyncio
async def test_device_locations_pushes_for_nodus_sensor_and_switch(tmp_path, monkeypatch):
    app, _ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "aqi-123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "aqi-123", "LOCATION": "Old"}},
    )
    switch_mgr.save(
        "switch-123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-123", "SWITCH_LOCATION": "Old"}},
    )
    _write_system_settings(system_root, "aqi-123", "aqi-123")
    _write_system_settings(system_root, "switch-123", "switch-123")
    _RecordingAsyncClient.calls.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[
                {"id": "aqi-123", "type": "sensor", "location": "Room A"},
                {"id": "switch-123", "type": "switch", "location": "Room B"},
            ],
        )

    assert res.status_code == 200
    posted = [call["json"] for call in _RecordingAsyncClient.calls]
    assert any(p["section"] == "Sensor" and p["key"] == "LOCATION" and p["value"] == "Room A" for p in posted)
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_LOCATION" and p["value"] == "Room B" for p in posted)
