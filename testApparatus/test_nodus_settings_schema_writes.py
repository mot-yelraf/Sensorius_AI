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
        self.calibration_commands: list[dict] = []
        self.calibration_state: dict[str, dict] = {}
        self.next_calibration_ack: dict | None = {"accepted": True}
        self.next_calibration_result: dict | None = {"applied": True, "status": {"status": "calibrated", "calibrated": True}}

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

    def publish_nodus_calibration(self, device_id: str, *, action: str, payload=None, message_id=None, qos=1):
        message = {
            "device_id": device_id,
            "action": action,
            "payload": payload,
            "message_id": message_id or f"test-{len(self.calibration_commands) + 1}",
            "qos": qos,
        }
        self.calibration_commands.append(message)
        return {"ok": True, "message_id": message["message_id"], "topic": f"nodus/{device_id}/calibration/set"}

    async def wait_for_calibration_ack(self, message_id: str, timeout: float = 0):
        if self.next_calibration_ack is None:
            return None
        out = dict(self.next_calibration_ack)
        out.setdefault("message_id", message_id)
        return out

    async def wait_for_calibration_result(self, message_id: str, timeout: float = 0):
        if self.next_calibration_result is None:
            return None
        out = dict(self.next_calibration_result)
        out.setdefault("message_id", message_id)
        return out

    def get_nodus_calibration_state(self, sensor_id: str):
        state = self.calibration_state.get(sensor_id)
        return dict(state) if isinstance(state, dict) else state


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
    monkeypatch.setattr(saiWebRoutes, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
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


def test_build_picow_settings_updates_rewrites_ip_broker_to_hub_hostname(monkeypatch):
    monkeypatch.setattr(saiAddDevice, "resolve_pi_wifi_credentials", lambda: ("MyWiFi", "secret"))
    monkeypatch.setattr(saiAddDevice, "PI_HOSTNAME", "sensoria-hub-0")

    updates = saiAddDevice.build_picow_settings_updates(
        {"broker": "192.168.4.17"},
        {"TZ": "America/Denver", "TZ_OFFSET": -25200, "TZ_NAME": "MST"},
        "aqi-test",
    )

    assert {"section": "MQTT", "key": "BROKER", "value": "sensoria-hub-0.local"} in updates


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
async def test_device_calibration_apply_for_remote_nodus_uses_mqtt_and_updates_shadow_on_success(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-123",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "aqi-123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}],
            },
        )

    assert res.status_code == 200
    assert ingest.calibration_commands[-1]["action"] == "apply"
    assert ingest.calibration_commands[-1]["payload"]["offsets"][0]["key"] == "Calibration.Device.TEMP_OFFSET"
    saved = sensor_mgr.load("aqi-123")
    assert saved["Calibration"]["Device"]["TEMP_OFFSET"] == 1.5


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_does_not_update_shadow_on_failure(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_result = {"applied": False, "error": "bad_payload", "status": {"status": "idle", "calibrated": False}}
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-123",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "aqi-123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}],
            },
        )

    assert res.status_code == 400
    saved = sensor_mgr.load("aqi-123")
    assert "Calibration" not in saved


@pytest.mark.asyncio
async def test_calibration_status_prefers_mqtt_state_for_remote_nodus(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-123",
            },
        },
    )
    ingest.calibration_state["aqi-123"] = {
        "status": {
            "sensor_id": "aqi-123",
            "status": "in_progress",
            "calibrated": False,
            "sample_index": 2,
            "sample_total": 5,
            "temp_offset": 1.25,
            "rh_offset": -2.5,
        }
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/calibration-status", params={"sensor_id": "aqi-123"})

    body = res.json()
    assert res.status_code == 200
    assert body["calibrated"] == "Calibrating"
    assert body["sample_index"] == 2


@pytest.mark.asyncio
async def test_calibrate_remote_nodus_uses_mqtt_start(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "apvpd",
                "SENSOR_ID": "aqi-123",
            }
        },
    )
    ingest.next_calibration_result = {"applied": True, "started": True, "status": {"status": "in_progress", "calibrated": False}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/calibrate", params={"sensor_id": "aqi-123"})

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert ingest.calibration_commands[-1]["action"] == "start"


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
