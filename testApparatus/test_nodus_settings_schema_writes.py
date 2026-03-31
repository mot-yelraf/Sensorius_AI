"""Pytest coverage for onboarding and settings-schema materialization.

These tests exercise route-level settings writes, broker rewriting, Astral value
handling, and Nodus-facing config payload generation.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from datetime import datetime, timedelta
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiAddDevice
import saiMQTTIngest
import saiSensorSettingsManager
import saiSettings as saiSettingsModule
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
        self.published_json: list[dict] = []
        self.switch_commands: list[dict] = []
        self.mqtt_clients: list[str] = []
        self.calibration_commands: list[dict] = []
        self.calibration_state: dict[str, dict] = {}
        self.sample_events_by_message: dict[str, list[dict]] = {}
        self.next_calibration_ack: dict | None = {"accepted": True}
        self.next_calibration_result: dict | None = {"applied": True, "status": {"status": "calibrated", "calibrated": True}}
        self.next_calibration_result_by_action: dict[str, dict | None] = {}
        self.device_location: dict[str, str] = {}
        self.expected_gauge_map: dict[str, list[str]] = {}
        self.device_status: dict[str, str] = {}
        self.nodus_switch_topic_map: dict[str, dict] = {}
        self.nodus_firmware_versions: dict[str, str] = {}
        self._switch_state_cache: dict[str, dict] = {}
        self._host_ip_cache: dict[str, str] = {}
        self._host_ipv4addr: dict[str, str] = {}
        self.next_config_ack: dict | None = {"accepted": True}
        self.next_config_result: dict | None = {"applied": True, "updated": 1, "error": ""}
        self.next_switch_command_ok: bool = True

    def set_onboarding_event_handler(self, handler):
        self.handler = handler

    def resolve_nodus_hostname(self, *_args, **_kwargs):
        return None

    def _normalize_host_key(self, hostname: str | None) -> str | None:
        host = str(hostname or "").strip()
        if not host:
            return None
        return host[:-6] if host.endswith(".local") else host

    def _host_candidates(self, hostname: str) -> list[str]:
        base = self._normalize_host_key(hostname)
        if not base:
            return []
        cached_ip = self._host_ip_cache.get(base)
        mdns = f"{base}.local"
        return [cached_ip, mdns] if cached_ip else [mdns]

    def get_known_devices(self):
        return list(self.mqtt_clients)

    def get_known_switch_devices(self):
        return []

    def get_measure_status(self, _sid: str):
        return "online"

    def add_client(self, host: str):
        self.added.append(host)

    async def force_refresh_device_metadata(self, device_id: str):
        self.refreshed.append(device_id)

    def publish_json(self, topic: str, obj: dict, *, qos: int = 0, retain: bool = False, use_ha_client: bool = True):
        self.published_json.append(
            {
                "topic": topic,
                "payload": dict(obj or {}),
                "qos": qos,
                "retain": retain,
                "use_ha_client": use_ha_client,
            }
        )
        return True

    def publish_nodus_config(self, device_id: str, *, payload: dict, message_id: str | None = None, qos: int = 1, restart: bool = False, onboard_token: str = ""):
        mid = message_id or f"cfg-{len(self.published_json) + 1}"
        envelope = {
            "message_id": mid,
            "payload": dict(payload or {}),
            "restart": bool(restart),
        }
        if onboard_token:
            envelope["onboard_token"] = onboard_token
        topic = f"nodus/{device_id}/config/set"
        ok = self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False)
        return {"ok": ok, "message_id": mid, "topic": topic, "payload": envelope}

    def publish_nodus_restart(self, device_id: str, *, restart_mode: str = "soft", message_id: str | None = None, qos: int = 1):
        mid = message_id or f"rst-{len(self.published_json) + 1}"
        envelope = {
            "message_id": mid,
            "restart": True,
            "restart_mode": str(restart_mode or "soft"),
        }
        topic = f"nodus/{device_id}/config/set"
        ok = self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False)
        return {"ok": ok, "message_id": mid, "topic": topic, "payload": envelope}

    async def wait_for_config_ack(self, message_id: str, timeout: float = 0):
        if self.next_config_ack is None:
            return None
        out = dict(self.next_config_ack)
        out.setdefault("message_id", message_id)
        return out

    async def wait_for_config_result(self, message_id: str, timeout: float = 0):
        if self.next_config_result is None:
            return None
        out = dict(self.next_config_result)
        out.setdefault("message_id", message_id)
        return out

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

    def set_switch_by_channel_id(self, switch_id: str, channel_id: str, new_state: bool, qos: int = 0, retain: bool = False):
        self.switch_commands.append(
            {
                "switch_id": switch_id,
                "channel_id": channel_id,
                "new_state": bool(new_state),
                "qos": qos,
                "retain": retain,
            }
        )
        return bool(self.next_switch_command_ok)

    async def wait_for_calibration_ack(self, message_id: str, timeout: float = 0):
        if self.next_calibration_ack is None:
            return None
        out = dict(self.next_calibration_ack)
        out.setdefault("message_id", message_id)
        return out

    async def wait_for_calibration_result(self, message_id: str, timeout: float = 0):
        action = ""
        for row in self.calibration_commands:
            if row.get("message_id") == message_id:
                action = str(row.get("action") or "")
                break
        if action in self.next_calibration_result_by_action:
            payload = self.next_calibration_result_by_action.get(action)
        else:
            payload = self.next_calibration_result
        if payload is None:
            return None
        out = dict(payload)
        out.setdefault("message_id", message_id)
        return out

    async def wait_for_calibration_samples(self, message_id: str, expected_count: int | None = None, timeout: float = 0):
        rows = [dict(item) for item in self.sample_events_by_message.get(message_id, [])]
        if expected_count is None or len(rows) >= int(expected_count):
            return rows
        return rows

    def get_nodus_calibration_state(self, sensor_id: str):
        state = self.calibration_state.get(sensor_id)
        return dict(state) if isinstance(state, dict) else state

    def get_nodus_firmware_version(self, device_id: str | None, device_type: str | None = None):
        dev = str(device_id or "").strip()
        if not dev:
            return ""
        return str(self.nodus_firmware_versions.get(dev) or "")


class _FakeSaiSettings:
    DEFAULT_BASE_DIR = ""
    STANDARD_FILENAME = "settings.toml"

    def __init__(self, *args, **_kwargs):
        self.settings_root = self.DEFAULT_BASE_DIR
        self.system_dir = self.DEFAULT_BASE_DIR
        self.settings = {}
        self._dirty = False

    def get_setting(self, _section, _key, default=None):
        return default

    def get_section(self, _section, reload_if_changed=False):
        return {}

    def save_settings(self):
        return

    @staticmethod
    def obfuscate_secret(value):
        return value

    @staticmethod
    def deobfuscate_secret(value):
        return value


class _PersistentFakeSaiSettings(_FakeSaiSettings):
    STORED_SETTINGS: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings = {
            section: dict(values or {})
            for section, values in self.__class__.STORED_SETTINGS.items()
        }

    def get_section(self, section, reload_if_changed=False):
        data = self.settings.get(section, {})
        if isinstance(data, dict):
            return dict(data)
        return data

    def save_settings(self):
        self.__class__.STORED_SETTINGS = {
            section: dict(values or {})
            for section, values in self.settings.items()
        }
        self._dirty = False


class _BaseDirOnlyFakeSaiSettings:
    DEFAULT_BASE_DIR = ""

    def __init__(self, *args, **_kwargs):
        self.base_dir = self.DEFAULT_BASE_DIR
        self.settings = {}
        self._dirty = False

    def get_setting(self, _section, _key, default=None):
        return default


class _RouteFakeSaiSettings(_PersistentFakeSaiSettings):
    def get_setting(self, section, key, default=None):
        return self.settings.get(section, {}).get(key, default)

    def replace_setting(self, section, key, value):
        bucket = self.settings.setdefault(section, {})
        bucket[key] = value
        self.save_settings()

    @staticmethod
    def timezone_info(tz_name: str):
        if str(tz_name).strip() == "America/Denver":
            return -21600, "MDT"
        return 0, "UTC"


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
    app = FastAPI()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), ingest)
    return app, ingest, system_root, sensor_root, switch_root


async def _build_app_base_dir_only(tmp_path, monkeypatch):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root.mkdir()
    sensor_root.mkdir()
    switch_root.mkdir()

    _BaseDirOnlyFakeSaiSettings.DEFAULT_BASE_DIR = str(system_root)

    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _BaseDirOnlyFakeSaiSettings)
    monkeypatch.setattr(saiWebRoutes, "SystemSettingsManager", _FakeSystemSettingsManager, raising=False)
    monkeypatch.setattr(saiWebRoutes, "SensorSettingsManager", lambda *_a, **_k: _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)))
    monkeypatch.setattr(saiWebRoutes, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
    app = FastAPI()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), ingest)
    return app, ingest, system_root, sensor_root, switch_root


async def _build_route_app_with_settings(tmp_path, monkeypatch, stored_settings: dict):
    _RouteFakeSaiSettings.STORED_SETTINGS = {
        section: dict(values or {})
        for section, values in (stored_settings or {}).items()
    }
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _RouteFakeSaiSettings)
    app = FastAPI()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), ingest)
    return app


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
async def test_submit_pi_setup_reset_clears_saved_astral_location(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Astral": {
                "LATITUDE": "40.015000",
                "LONGITUDE": "-105.270500",
                "TIMEZONE": "America/Denver",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "broker": "",
                "tz": "America/Denver",
                "httpport": "8000",
                "astral_lat": "reset",
                "astral_lon": "",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == ""
    assert stored["Astral"]["LONGITUDE"] == ""
    assert stored["Astral"]["TIMEZONE"] == ""


@pytest.mark.asyncio
async def test_submit_pi_setup_blank_astral_fields_leave_saved_location_unchanged(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Astral": {
                "LATITUDE": "40.015000",
                "LONGITUDE": "-105.270500",
                "TIMEZONE": "America/Denver",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "broker": "",
                "tz": "America/Denver",
                "httpport": "8000",
                "astral_lat": "",
                "astral_lon": "",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == "40.015000"
    assert stored["Astral"]["LONGITUDE"] == "-105.270500"
    assert stored["Astral"]["TIMEZONE"] == "America/Denver"


@pytest.mark.asyncio
async def test_submit_pi_setup_ajax_returns_json_success(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "broker": "",
                "tz": "America/Denver",
                "httpport": "8000",
                "astral_lat": "",
                "astral_lon": "",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )

    assert res.status_code == 200
    assert res.json()["ok"] is True


@pytest.mark.asyncio
async def test_submit_sensor_settings_ajax_returns_json_success(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "local",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Old Room",
            },
            "Display": {"METRIC_1": "Temperature"},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={"sensor_id": "apvpd-test123", "location": "Grow Tent"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["sensor_id"] == "apvpd-test123"


@pytest.mark.asyncio
async def test_submit_switch_settings_ajax_returns_json_success(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-1",
        {
            "Switch": {
                "TYPE": "local",
                "SWITCH_LOCATION": "Old Room",
                "SWITCH_1_LABEL": "Fan",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={"switch_id": "switch-1", "location": "Grow Tent", "SWITCH_1_LABEL": "Fan"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["switch_id"] == "switch-1"


@pytest.mark.asyncio
async def test_submit_sensor_settings_pushes_sensor_and_display_updates_for_nodus(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Old Room",
            },
            "Display": {"METRIC_1": "Old"},
        },
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "apvpd-test123",
                "sensor_id_field": "apvpd-test123",
                "device": "aqi",
                "location": "Grow Tent",
                "metric_1": "Temperature",
                "metric_2": "Humidity",
                "metric_3": "PM2.5",
                "metric_4": "",
                "metric_5": "",
                "metric_6": "",
                "display_style_1": "Graph24hr",
                "display_style_2": "Gauge",
                "display_style_3": "Graph6hr",
                "display_style_4": "Gauge",
                "display_style_5": "Gauge",
                "display_style_6": "Gauge",
            },
        )

    assert res.status_code == 303
    assert len(ingest.published_json) == 10
    assert all(len((((row.get("payload") or {}).get("payload") or {}).get("updates") or [])) == 1 for row in ingest.published_json)
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert any(p["section"] == "Sensor" and p["key"] == "LOCATION" and p["value"] == "Grow Tent" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_1" and p["value"] == "Temperature" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_2" and p["value"] == "Humidity" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_3" and p["value"] == "PM2.5" for p in posted)
    assert any(p["section"] == "Display.Style" and p["key"] == "METRIC_1" and p["value"] == "Graph24hr" for p in posted)
    assert any(p["section"] == "Display.Style" and p["key"] == "METRIC_3" and p["value"] == "Graph6hr" for p in posted)
    assert not any(p["section"] == "Sensor" and p["key"] == "DEVICE" for p in posted)
    assert not any(p["section"] == "Sensor" and p["key"] == "SENSOR_ID" for p in posted)
    assert any(p.get("name") == "sensor_i2c.toml" for p in posted)
    assert ingest.refreshed == []
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Sensor"]["DEVICE"] == "aqi"
    assert saved["Sensor"]["SENSOR_ID"] == "apvpd-test123"
    assert saved["Display"]["Style"]["METRIC_1"] == "Graph24hr"
    assert saved["Display"]["Style"]["METRIC_3"] == "Graph6hr"


@pytest.mark.asyncio
async def test_submit_sensor_settings_prefers_cached_ip_for_nodus_push(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Lab",
            },
            "Display": {"METRIC_1": "CO2"},
        },
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    ingest._host_ip_cache["apvpd-test123"] = "192.168.4.23"
    ingest._host_ipv4addr["apvpd-test123"] = "192.168.4.23"
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "apvpd-test123",
                "sensor_id_field": "apvpd-test123",
                "device": "co2",
                "location": "Lab",
                "metric_1": "CO2",
                "metric_2": "Temperature",
                "metric_3": "",
                "metric_4": "",
                "metric_5": "",
                "metric_6": "",
                "display_style_1": "Graph6hr",
                "display_style_2": "Gauge",
                "display_style_3": "Gauge",
                "display_style_4": "Gauge",
                "display_style_5": "Gauge",
                "display_style_6": "Gauge",
            },
        )

    assert res.status_code == 303
    assert ingest.published_json
    assert ingest.published_json[0]["topic"] == "nodus/apvpd-test123/config/set"
    assert all(len((((row.get("payload") or {}).get("payload") or {}).get("updates") or [])) == 1 for row in ingest.published_json)


@pytest.mark.asyncio
async def test_sensor_settings_modal_shows_nodus_firmware_version_in_settings_pane_title(tmp_path, monkeypatch):
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    template = env.get_template("modals/sensor_settings.html")

    html = template.render(
        sensor_id="apvpd-test123",
        settings={"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Veg Tent"}},
        metric_options=["", "Temperature", "Rel-Humidity", "Ambient VPD"],
        current_metrics=["Temperature", "Rel-Humidity", "Ambient VPD", "", "", ""],
        display_style_options=["Gauge", "Graph6hr", "Graph24hr"],
        current_metric_styles=["Gauge", "Graph6hr", "Graph24hr", "Gauge", "Gauge", "Gauge"],
        location="Veg Tent",
        device_kind="aqi",
        device_label="aqi",
        is_apvpd=False,
        is_soil=False,
        ambient_temp_offset=0.0,
        ambient_rh_offset=0.0,
        nodus_firmware_version="v1.2.3",
        soil_ph_offset=0.0,
        device_offsets=[],
        candidate_sensors=[],
        default_range_hours=24,
        can_restart_device=True,
    )

    assert "Sensor Settings v1.2.3" in html
    assert html.index("Home") < html.index("Restart Device") < html.index("Save")
    assert 'name="display_style_1"' in html
    assert 'name="display_style_3"' in html


def test_switch_settings_modal_shows_nodus_firmware_version_in_settings_pane_title():
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    template = env.get_template("modals/switch_settings.html")

    html = template.render(
        switch_id="switch-test123",
        settings={"Switch": {"TYPE": "nodus", "SWITCH_LOCATION": "Veg Tent"}},
        channel_indices=[1],
        channels=[{"index": 1, "label": "Fan"}],
        nodus_firmware_version="v1.2.3",
        can_restart_device=True,
    )

    assert "Switch Settings v1.2.3" in html
    assert html.index("Home") < html.index("Restart Device") < html.index("Save")
    assert "Device Restarting..." in html


def test_sensor_settings_modal_restart_button_uses_restarting_label():
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    template = env.get_template("modals/sensor_settings.html")

    html = template.render(
        sensor_id="apvpd-test123",
        settings={"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Veg Tent"}},
        metric_options=["", "Temperature", "Rel-Humidity", "Ambient VPD"],
        current_metrics=["Temperature", "Rel-Humidity", "Ambient VPD", "", "", ""],
        display_style_options=["Gauge", "Graph6hr", "Graph24hr"],
        current_metric_styles=["Gauge", "Graph6hr", "Graph24hr", "Gauge", "Gauge", "Gauge"],
        location="Veg Tent",
        device_kind="aqi",
        device_label="aqi",
        is_apvpd=False,
        is_soil=False,
        ambient_temp_offset=0.0,
        ambient_rh_offset=0.0,
        nodus_firmware_version="v1.2.3",
        soil_ph_offset=0.0,
        device_offsets=[],
        candidate_sensors=[],
        default_range_hours=24,
        can_restart_device=True,
    )

    assert "Device Restarting..." in html


@pytest.mark.asyncio
async def test_restart_sensor_device_publishes_soft_restart_to_nodus_config_set(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    restart_logs: list[tuple[str, str, str]] = []
    monkeypatch.setattr(saiWebRoutes, "printDM", lambda msg, location="", level="debug": restart_logs.append((str(msg), str(location), str(level))))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            }
        },
    )
    _write_system_settings(system_root, "apvpd-test123", "aqi-nz6g89")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/sensor-settings/restart-device",
            data={"sensor_id": "apvpd-test123"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message"] == "Device restarting..."
    assert ingest.published_json[-1]["topic"] == f"nodus/{body['target_device']}/config/set"
    assert ingest.published_json[-1]["payload"]["restart"] is True
    assert ingest.published_json[-1]["payload"]["restart_mode"] == "soft"
    assert "payload" not in ingest.published_json[-1]["payload"]
    assert any("[restart-request]" in msg for msg, _location, _level in restart_logs)
    assert any("[restart-result]" in msg and "ok=True" in msg for msg, _location, _level in restart_logs)


@pytest.mark.asyncio
async def test_restart_switch_device_publishes_soft_restart_to_nodus_config_set(tmp_path, monkeypatch):
    app, ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    restart_logs: list[tuple[str, str, str]] = []
    monkeypatch.setattr(saiWebRoutes, "printDM", lambda msg, location="", level="debug": restart_logs.append((str(msg), str(location), str(level))))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_LOCATION": "Veg Tent",
                "SWITCH_1_LABEL": "Fan",
            }
        },
    )
    _write_system_settings(system_root, "switch-test123", "switch-nz6g89")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/switch-settings/restart-device",
            data={"switch_id": "switch-test123"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message"] == "Device restarting..."
    assert ingest.published_json[-1]["topic"] == "nodus/switch-nz6g89/config/set"
    assert ingest.published_json[-1]["payload"]["restart"] is True
    assert ingest.published_json[-1]["payload"]["restart_mode"] == "soft"
    assert any("[restart-request]" in msg for msg, _location, _level in restart_logs)
    assert any("[restart-result]" in msg and "ok=True" in msg for msg, _location, _level in restart_logs)


@pytest.mark.asyncio
async def test_restart_sensor_device_requires_config_result_before_reporting_success(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            }
        },
    )
    ingest.next_config_result = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/sensor-settings/restart-device",
            data={"sensor_id": "apvpd-test123"},
        )

    assert res.status_code == 502
    body = res.json()
    assert body["ok"] is False
    assert "timed out waiting for device result" in body["error"]


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_uses_mqtt_and_updates_shadow_on_success(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}],
            },
        )

    assert res.status_code == 200
    assert ingest.calibration_commands[-1]["action"] == "apply"
    assert ingest.calibration_commands[-1]["payload"]["offsets"][0]["key"] == "Calibration.Device.TEMP_OFFSET"
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["TEMP_OFFSET"] == 1.5


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_can_update_same_offset_multiple_times(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "apvpd-test123",
            },
            "Calibration": {
                "Device": {
                    "CO2_OFFSET": 0.0,
                }
            },
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "co2",
                "offsets": [{"key": "Calibration.Device.CO2_OFFSET", "value": -750.0}],
            },
        )
        second = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "co2",
                "offsets": [{"key": "Calibration.Device.CO2_OFFSET", "value": -250.0}],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert sensor_mgr.load("apvpd-test123")["Calibration"]["Device"]["CO2_OFFSET"] == -250.0
    assert [cmd["payload"]["offsets"][0]["value"] for cmd in ingest.calibration_commands[-2:]] == [-750.0, -250.0]


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_filters_unchanged_offsets_before_mqtt(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            },
            "Calibration": {
                "Device": {
                    "TEMP_OFFSET": 0.0,
                    "RH_OFFSET": 0.0,
                    "AQI_OFFSET": 0.0,
                    "GAS_OFFSET": 0.0,
                }
            },
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [
                    {"key": "Calibration.Device.TEMP_OFFSET", "value": 0},
                    {"key": "Calibration.Device.RH_OFFSET", "value": 0.4},
                    {"key": "Calibration.Device.AQI_OFFSET", "value": 0},
                    {"key": "Calibration.Device.GAS_OFFSET", "value": 0},
                ],
            },
        )

    assert res.status_code == 200
    sent_offsets = ingest.calibration_commands[-1]["payload"]["offsets"]
    assert sent_offsets == [{"key": "Calibration.Device.RH_OFFSET", "value": 0.4}]
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["RH_OFFSET"] == 0.4


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_does_not_update_shadow_on_failure(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_result = {"applied": False, "error": "bad_payload", "status": {"status": "idle", "calibrated": False}}
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.5}],
            },
        )

    assert res.status_code == 400
    saved = sensor_mgr.load("apvpd-test123")
    assert "Calibration" not in saved


@pytest.mark.asyncio
async def test_soil_ph_buffer_calibration_for_remote_nodus_uses_sample_session_and_updates_shadow(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "soil-123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "soil",
                "SENSOR_ID": "soil-123",
            }
        },
    )
    ingest.next_calibration_result_by_action["soil_ph_session_start"] = {
        "applied": True,
        "started": True,
        "sample_interval_s": 1.0,
        "sample_count": 3,
        "reference_ph": 7.0,
        "status": {
            "status": "in_progress",
            "calibrated": False,
            "sensor_id": "soil-123",
        },
    }
    ingest.next_calibration_result_by_action["apply"] = {
        "applied": True,
        "status": {
            "status": "calibrated",
            "calibrated": True,
            "sensor_id": "soil-123",
        },
    }
    ingest.sample_events_by_message["test-1"] = [
        {
            "message_id": "test-1",
            "sensor_id": "soil-123",
            "sample_index": 1,
            "sample_count": 3,
            "raw_ph": 6.40,
            "corrected_ph": 6.40,
            "soil_ph_offset": 0.0,
        },
        {
            "message_id": "test-1",
            "sensor_id": "soil-123",
            "sample_index": 2,
            "sample_count": 3,
            "raw_ph": 6.42,
            "corrected_ph": 6.42,
            "soil_ph_offset": 0.0,
        },
        {
            "message_id": "test-1",
            "sensor_id": "soil-123",
            "sample_index": 3,
            "sample_count": 3,
            "raw_ph": 6.44,
            "corrected_ph": 6.44,
            "soil_ph_offset": 0.0,
        },
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/soil/ph-buffer",
            json={
                "sensor_id": "soil-123",
                "buffer_ph": 7.0,
            },
        )

    body = res.json()
    assert res.status_code == 200
    assert body["soil_ph_offset"] == pytest.approx(0.58)
    assert ingest.calibration_commands[0]["action"] == "soil_ph_session_start"
    assert ingest.calibration_commands[-1]["action"] == "apply"
    sent_offset = ingest.calibration_commands[-1]["payload"]["offsets"][0]
    assert sent_offset["key"] == "soil_ph_offset"
    assert sent_offset["value"] == pytest.approx(0.58)
    saved = sensor_mgr.load("soil-123")
    assert saved["Calibration"]["Device"]["SOIL_PH_CAL_VAL"] == pytest.approx(0.58)


@pytest.mark.asyncio
async def test_local_soil_ph_buffer_calibration_requires_recent_soil_ph_reading(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "soil-123",
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "soil",
                "SENSOR_ID": "soil-123",
            }
        },
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda _sensor_id: {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/soil/ph-buffer",
            json={
                "sensor_id": "soil-123",
                "buffer_ph": 4.0,
            },
        )

    assert res.status_code == 409
    assert "Soil-pH reading" in res.json()["message"]


@pytest.mark.asyncio
async def test_calibration_status_prefers_mqtt_state_for_remote_nodus(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
            },
        },
    )
    ingest.calibration_state["apvpd-test123"] = {
        "status": {
            "sensor_id": "apvpd-test123",
            "status": "in_progress",
            "calibrated": False,
            "sample_index": 2,
            "sample_total": 5,
            "temp_offset": 1.25,
            "rh_offset": -2.5,
        }
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/calibration-status", params={"sensor_id": "apvpd-test123"})

    body = res.json()
    assert res.status_code == 200
    assert body["calibrated"] == "Calibrating"
    assert body["sample_index"] == 2


@pytest.mark.asyncio
async def test_calibrate_remote_nodus_uses_mqtt_start(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "apvpd",
                "SENSOR_ID": "apvpd-test123",
            }
        },
    )
    ingest.next_calibration_result = {"applied": True, "started": True, "status": {"status": "in_progress", "calibrated": False}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/calibrate", params={"sensor_id": "apvpd-test123"})

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert ingest.calibration_commands[-1]["action"] == "start"


@pytest.mark.asyncio
async def test_submit_switch_settings_pushes_remote_updates_for_nodus(tmp_path, monkeypatch):
    app, ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
            }
        },
    )
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={
                "switch_id": "switch-test123",
                "location": "Veg Rack",
                "SWITCH_1_LABEL": "Lights",
            },
        )

    assert res.status_code == 303
    assert len(ingest.published_json) == 2
    assert all(len((((row.get("payload") or {}).get("payload") or {}).get("updates") or [])) == 1 for row in ingest.published_json)
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_LOCATION" and p["value"] == "Veg Rack" for p in posted)
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_1_LABEL" and p["value"] == "Lights" for p in posted)
    assert not any(p["section"] == "Switch" and p["key"] == "DEVICE" for p in posted)
    assert not any(p["section"] == "Switch" and p["key"] == "SWITCH_DEVICE_ID" for p in posted)
    assert all(p.get("name") == "switch.toml" for p in posted)
    saved = switch_mgr.load("switch-test123")
    assert saved["Switch"]["DEVICE"] == "switch"
    assert saved["Switch"]["SWITCH_DEVICE_ID"] == "switch-test123"


@pytest.mark.asyncio
async def test_submit_switch_settings_waits_for_config_result_before_next_remote_update(tmp_path, monkeypatch):
    app, ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
            }
        },
    )
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()

    first_result_released = asyncio.Event()
    second_result_released = asyncio.Event()

    async def _wait_for_config_result(message_id: str, timeout: float = 0):
        if message_id == "cfg-1":
            await first_result_released.wait()
        elif message_id == "cfg-2":
            await second_result_released.wait()
        return {"message_id": message_id, "applied": True, "updated": 1, "error": ""}

    ingest.wait_for_config_result = _wait_for_config_result

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        submit_task = asyncio.create_task(
            client.post(
                "/submit-switch-settings",
                data={
                    "switch_id": "switch-test123",
                    "location": "Veg Rack",
                    "SWITCH_1_LABEL": "Lights",
                },
            )
        )

        await asyncio.sleep(0.05)
        assert len(ingest.published_json) == 1

        first_result_released.set()
        await asyncio.sleep(0.05)
        assert len(ingest.published_json) == 2

        second_result_released.set()
        res = await submit_task

    assert res.status_code == 303


@pytest.mark.asyncio
async def test_device_locations_pushes_for_nodus_sensor_and_switch(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Old"}},
    )
    switch_mgr.save(
        "switch-test123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-test123", "SWITCH_LOCATION": "Old"}},
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[
                {"id": "apvpd-test123", "type": "sensor", "location": "Room A"},
                {"id": "switch-test123", "type": "switch", "location": "Room B"},
            ],
        )

    assert res.status_code == 200
    assert len(ingest.published_json) == 2
    assert all(len((((row.get("payload") or {}).get("payload") or {}).get("updates") or [])) == 1 for row in ingest.published_json)
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert any(p["section"] == "Sensor" and p["key"] == "LOCATION" and p["value"] == "Room A" for p in posted)
    assert any(p["section"] == "Switch" and p["key"] == "SWITCH_LOCATION" and p["value"] == "Room B" for p in posted)


@pytest.mark.asyncio
async def test_device_locations_serializes_shared_host_nodus_updates(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Old"}},
    )
    switch_mgr.save(
        "switch-test123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-test123", "SWITCH_LOCATION": "Old"}},
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    _write_system_settings(system_root, "switch-test123", "apvpd-test123")
    ingest.published_json.clear()

    first_result_released = asyncio.Event()
    second_result_released = asyncio.Event()

    async def _wait_for_config_result(message_id: str, timeout: float = 0):
        if message_id == "cfg-1":
            await first_result_released.wait()
        elif message_id == "cfg-2":
            await second_result_released.wait()
        return {"message_id": message_id, "applied": True, "updated": 1, "error": ""}

    ingest.wait_for_config_result = _wait_for_config_result

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        save_task = asyncio.create_task(
            client.post(
                "/device-locations",
                json=[
                    {"id": "apvpd-test123", "type": "sensor", "location": "Room A"},
                    {"id": "switch-test123", "type": "switch", "location": "Room B"},
                ],
            )
        )

        await asyncio.sleep(0.05)
        assert len(ingest.published_json) == 1
        assert ingest.published_json[0]["topic"] == "nodus/apvpd-test123/config/set"

        first_result_released.set()
        await asyncio.sleep(0.05)
        assert len(ingest.published_json) == 2
        assert all(row["topic"] == "nodus/apvpd-test123/config/set" for row in ingest.published_json)

        second_result_released.set()
        res = await save_task

    assert res.status_code == 200


@pytest.mark.asyncio
async def test_device_locations_prefers_paired_sensor_host_when_switch_system_host_is_stale(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Old"}},
    )
    switch_mgr.save(
        "switch-test123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-test123", "SWITCH_LOCATION": "Old"}},
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[{"id": "switch-test123", "type": "switch", "location": "Room B"}],
        )

    assert res.status_code == 200
    assert len(ingest.published_json) == 1
    assert ingest.published_json[0]["topic"] == "nodus/apvpd-test123/config/set"


@pytest.mark.asyncio
async def test_device_locations_skips_unchanged_rows_and_only_pushes_modified_nodus_devices(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Room A"}},
    )
    sensor_mgr.save(
        "co2-123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "co2", "SENSOR_ID": "co2-123", "LOCATION": "Room B"}},
    )
    switch_mgr.save(
        "switch-test123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-test123", "SWITCH_LOCATION": "Room C"}},
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    _write_system_settings(system_root, "co2-123", "co2-123")
    _write_system_settings(system_root, "switch-test123", "apvpd-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[
                {"id": "apvpd-test123", "type": "sensor", "location": "Room A"},
                {"id": "co2-123", "type": "sensor", "location": "Veg Tent"},
                {"id": "switch-test123", "type": "switch", "location": "Room C"},
            ],
        )

    assert res.status_code == 200
    body = res.json()
    assert body["updated"] == {"sensor": 1, "switch": 0, "nodus_pushed": 1}
    assert len(ingest.published_json) == 1
    assert ingest.published_json[0]["topic"] == "nodus/co2-123/config/set"


@pytest.mark.asyncio
async def test_device_locations_returns_502_when_nodus_config_apply_fails(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Old"}},
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    ingest.next_config_result = {"applied": False, "error": "apply_failed"}
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[{"id": "apvpd-test123", "type": "sensor", "location": "Room A"}],
        )

    assert res.status_code == 502
    body = res.json()
    assert body["ok"] is False
    assert body["error"] == "nodus_remote_apply_failed"
    assert body["results"][0]["id"] == "apvpd-test123"
    assert body["results"][0]["type"] == "sensor"
    assert body["results"][0]["ok"] is False
    assert body["results"][0]["target_host"] == "apvpd-test123"


@pytest.mark.asyncio
async def test_device_locations_resolves_system_root_from_base_dir_only_settings(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app_base_dir_only(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "co2-123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "co2", "SENSOR_ID": "co2-123", "LOCATION": "Lab"}},
    )
    switch_mgr.save(
        "switch-test123",
        {"Switch": {"TYPE": "nodus", "DEVICE": "switch", "SWITCH_DEVICE_ID": "switch-test123", "SWITCH_LOCATION": "Lab"}},
    )
    _write_system_settings(system_root, "co2-123", "co2-123")
    _write_system_settings(system_root, "switch-test123", "co2-123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/device-locations",
            json=[
                {"id": "co2-123", "type": "sensor", "location": "Office"},
                {"id": "switch-test123", "type": "switch", "location": "Office"},
            ],
        )

    assert res.status_code == 200
    assert len(ingest.published_json) == 2
    assert all(row["topic"] == "nodus/co2-123/config/set" for row in ingest.published_json)


@pytest.mark.asyncio
async def test_submit_switch_settings_prefers_paired_sensor_host_when_switch_system_host_is_stale(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    sensor_mgr.save(
        "apvpd-test123",
        {"Sensor": {"TYPE": "nodus", "DEVICE": "apvpd", "SENSOR_ID": "apvpd-test123", "LOCATION": "Old Rack"}},
    )
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
            }
        },
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={
                "switch_id": "switch-test123",
                "location": "Veg Rack",
                "SWITCH_1_LABEL": "Lights",
            },
        )

    assert res.status_code == 303
    assert len(ingest.published_json) == 2
    assert all(row["topic"] == "nodus/apvpd-test123/config/set" for row in ingest.published_json)


@pytest.mark.asyncio
async def test_submit_switch_settings_remote_last_state_uses_config_set(tmp_path, monkeypatch):
    app, ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
                "SWITCH_1_CHANNEL_ID": "S1-sernum",
                "SWITCH_1_LAST_STATE": False,
            }
        },
    )
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()
    ingest.switch_commands.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={
                "switch_id": "switch-test123",
                "location": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
                "SWITCH_1_LAST_STATE": "true",
            },
        )

    assert res.status_code == 303
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert posted == [{"section": "Switch", "key": "SWITCH_1_LAST_STATE", "value": True, "name": "switch.toml"}]
    assert all(row["topic"] == "nodus/switch-test123/config/set" for row in ingest.published_json)
    assert ingest.switch_commands == []
    saved = switch_mgr.load("switch-test123")
    assert saved["Switch"]["SWITCH_1_LAST_STATE"] is True


@pytest.mark.asyncio
async def test_submit_switch_settings_remote_last_state_uses_previous_label_mapping_when_label_changes(tmp_path, monkeypatch):
    app, ingest, system_root, _sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Old Rack",
                "SWITCH_1_LABEL": "Relay 1",
                "SWITCH_1_CHANNEL_ID": "S1-sernum",
                "SWITCH_1_LAST_STATE": False,
            }
        },
    )
    _write_system_settings(system_root, "switch-test123", "switch-test123")
    ingest.published_json.clear()
    ingest.switch_commands.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-switch-settings",
            data={
                "switch_id": "switch-test123",
                "location": "Old Rack",
                "SWITCH_1_LABEL": "Lights",
                "SWITCH_1_LAST_STATE": "true",
            },
        )

    assert res.status_code == 303
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert posted == [
        {"section": "Switch", "key": "SWITCH_1_LABEL", "value": "Lights", "name": "switch.toml"},
        {"section": "Switch", "key": "SWITCH_1_LAST_STATE", "value": True, "name": "switch.toml"},
    ]
    assert all(row["topic"] == "nodus/switch-test123/config/set" for row in ingest.published_json)
    assert ingest.switch_commands == []
    saved = switch_mgr.load("switch-test123")
    assert saved["Switch"]["SWITCH_1_LABEL"] == "Lights"
    assert saved["Switch"]["SWITCH_1_LAST_STATE"] is True


@pytest.mark.asyncio
async def test_dashboard_sensor_locations_ignore_unknown_live_cache_and_use_toml(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._SENSOR_LOCATION_CACHE.clear()
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    app.state.sensor_map = [SimpleNamespace(sensor_id="apvpd-test123", location="Grow Tent")]
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Grow Tent",
            },
            "Display": {"METRIC_1": "Temperature"},
        },
    )

    ingest.mqtt_clients = ["apvpd-test123"]
    ingest.device_location["sensor/apvpd-test123/data"] = "Unknown"

    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["apvpd-test123"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid == "apvpd-test123" else "")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {"apvpd-test123": {"Temperature": 72.0}},
            {"apvpd-test123": now_iso},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"apvpd-test123": {}})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["locations"]["apvpd-test123"] == "Grow Tent"


@pytest.mark.asyncio
async def test_dashboard_read_does_not_rewrite_metric_positions_for_offline_sensors(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._SENSOR_LOCATION_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    app.state.sensor_map = []
    saiWebRoutes.sensor_map = []
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    for sid in ("aqi-a", "aqi-b", "aqi-c"):
        sensor_mgr.save(
            sid,
            {
                "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": sid, "LOCATION": sid},
                "Display": {"METRIC_1": "Temperature"},
            },
        )

    _PersistentFakeSaiSettings.STORED_SETTINGS = {
        "MetricPosition": {"aqi-a": 1, "aqi-b": 2, "aqi-c": 3},
    }
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _PersistentFakeSaiSettings)
    monkeypatch.setattr(saiSettingsModule, "saiSettings", _PersistentFakeSaiSettings)

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-a", "aqi-c"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid in {"aqi-a", "aqi-c"} else "")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {sid: {"Temperature": 72.0} for sid in ids},
            {sid: now_iso for sid in ids},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Temperature"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"aqi-a": {}, "aqi-c": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["available"] == ["aqi-a", "aqi-c"]
    assert _PersistentFakeSaiSettings.STORED_SETTINGS["MetricPosition"] == {
        "aqi-a": 1,
        "aqi-b": 2,
        "aqi-c": 3,
    }


@pytest.mark.asyncio
async def test_dashboard_metric_position_reorder_preserves_hidden_sensor_slots(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._SENSOR_LOCATION_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    app.state.sensor_map = []
    saiWebRoutes.sensor_map = []
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    for sid in ("aqi-a", "aqi-b", "aqi-c"):
        sensor_mgr.save(
            sid,
            {
                "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": sid, "LOCATION": sid},
                "Display": {"METRIC_1": "Temperature"},
            },
        )

    _PersistentFakeSaiSettings.STORED_SETTINGS = {
        "MetricPosition": {"aqi-a": 1, "aqi-b": 2, "aqi-c": 3},
    }
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _PersistentFakeSaiSettings)
    monkeypatch.setattr(saiSettingsModule, "saiSettings", _PersistentFakeSaiSettings)

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-a", "aqi-c"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid in {"aqi-a", "aqi-c"} else "")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {sid: {"Temperature": 72.0} for sid in ids},
            {sid: now_iso for sid in ids},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Temperature"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"aqi-a": {}, "aqi-c": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/dashboard/metric-position", json={"sensor_id": "aqi-c", "direction": "up"})

    assert res.status_code == 200
    body = res.json()
    assert body["order"] == ["aqi-c", "aqi-a"]
    assert _PersistentFakeSaiSettings.STORED_SETTINGS["MetricPosition"] == {
        "aqi-c": 1,
        "aqi-b": 2,
        "aqi-a": 3,
    }


@pytest.mark.asyncio
async def test_dashboard_display_style_prefers_sensor_settings_over_global_default(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "apvpd-test123", "LOCATION": "Grow Tent"},
            "Display": {
                "METRIC_1": "Temperature",
                "Style": {
                    "METRIC_1": "Graph6hr",
                },
            },
        },
    )

    _PersistentFakeSaiSettings.STORED_SETTINGS = {
        "Display": {"display_style": "Graph24hr"},
    }
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _PersistentFakeSaiSettings)
    monkeypatch.setattr(saiSettingsModule, "saiSettings", _PersistentFakeSaiSettings)

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["apvpd-test123"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {sid: {"Temperature": 72.0} for sid in ids},
            {sid: now_iso for sid in ids},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Temperature"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"apvpd-test123": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    style_map = body["expected_display_style_map"]["apvpd-test123"]
    assert style_map["METRIC_1"] == "Graph6hr"
    assert style_map["METRIC_2"] == "Gauge"


@pytest.mark.asyncio
async def test_remove_device_list_merges_settings_db_and_ingest_ids(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)

    sensor_mgr.save(
        "aqi-settings",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "aqi-settings", "LOCATION": "Room A"},
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    switch_mgr.save(
        "switch-settings",
        {
            "Switch": {"DEVICE": "nodus", "SWITCH_DEVICE_ID": "switch-settings", "SWITCH_LOCATION": "Room B"},
        },
    )

    ingest.mqtt_clients = ["aqi-settings", "aqi-live.local"]
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-db", "aqi-settings"])
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_switch_identities",
        lambda: [{"switch_id": "switch-db", "switch_key": "switch-db::Fan", "label": "Fan", "location": "Room C"}],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.get("/remove-device-list")

    assert res.status_code == 200
    body = res.json()
    assert body["devices"] == [
        "aqi-db",
        "aqi-live",
        "aqi-settings",
        "switch-db",
        "switch-settings",
    ]


@pytest.mark.asyncio
async def test_remove_device_list_allows_same_origin_browser_request_without_api_key_header(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)

    sensor_mgr.save(
        "aqi-settings",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "aqi-settings", "LOCATION": "Room A"},
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"referer": "http://test/"},
    ) as client:
        res = await client.get("/remove-device-list")

    assert res.status_code == 200
    body = res.json()
    assert "aqi-settings" in body["devices"]


@pytest.mark.asyncio
async def test_switch_status_update_prefers_pending_remote_state_over_stale_cache(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._switch_status_cache_payload = None
    saiWebRoutes._switch_status_cache_until = 0.0
    saiWebRoutes.switch_controllers = {}

    ingest._switch_state_cache = {"switch-ykdvea": {"S1-ykdvea": "off"}}
    ingest._pending_set = {
        ("switch-ykdvea", "Fan"): {
            "ts": time.time(),
            "state": True,
            "channel_id": "S1-ykdvea",
        }
    }

    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_switch_identities",
        lambda: [
            {
                "switch_id": "switch-ykdvea",
                "switch_key": "S1-ykdvea::Fan",
                "channel_id": "S1-ykdvea",
                "label": "Fan",
                "location": "OfficeDesk",
            }
        ],
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_switch_state", lambda _switch_key: "Off")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_last_switch_events", lambda *_a, **_k: [])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/switch-status-update")

    assert res.status_code == 200
    body = res.json()
    assert body["switch-ykdvea::Fan"]["state"] is True


@pytest.mark.asyncio
async def test_switch_toggle_returns_recent_events_for_immediate_ui_refresh(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._switch_status_cache_payload = None
    saiWebRoutes._switch_status_cache_until = 0.0

    class _Ctrl:
        switch_id = "desk-hub"
        location = "OfficeDesk"

        def __init__(self):
            self.last_state = {"Fan": False}
            self.last_set_time = {"Fan": 0.0}

        def get_switch_names(self):
            return ["Fan"]

        def get_state(self, label):
            return bool(self.last_state[label])

        def set_state(self, label, on, force=False):
            self.last_state[label] = bool(on)
            return True

        def _switch_key(self, label):
            return f"S1-ykdvea::{label}"

        def get_auto_off_status(self, label):
            return {
                "timer_seconds": 0,
                "timer_enabled": False,
                "timer_deadline_epoch": None,
                "timer_remaining_s": 0,
            }

    saiWebRoutes.switch_controllers = {"desk-hub": _Ctrl()}
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_switch_state", lambda _switch_key: "Off")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_last_switch_events",
        lambda *_a, **_k: [("On", "2026-03-31 11:30:03", "ui")],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"referer": "http://test/"},
    ) as client:
        res = await client.post("/switch/toggle?switch_name=Fan&switch_id=desk-hub")

    assert res.status_code == 200
    body = res.json()
    assert body["state"] is True
    assert body["events"] == ["On 2026-03-31 11:30:03 (manual)"]
    assert body["time"] == ""


@pytest.mark.asyncio
async def test_advanced_automations_list_filters_to_requested_switch_id(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)

    class _FakeAutomationManager:
        def __init__(self, _base_dir):
            pass

        def load(self, _switch_id):
            return {
                "Advanced": {
                    "desk-fan-rule": {
                        "enabled": True,
                        "script_json": (
                            '{"name":"Desk Fan","actions":[{"switch_key":"desk-hub::S1-desk"}]}'
                        ),
                    },
                    "grow-rule": {
                        "enabled": True,
                        "script_json": (
                            '{"name":"Grow Rack","actions":[{"switch_key":"grow-hub::S1-grow"}]}'
                        ),
                    },
                    "legacy-local-rule": {
                        "enabled": False,
                        "script_json": (
                            '{"name":"Legacy Local","actions":[{"switch_key":"Fan"}]}'
                        ),
                    },
                }
            }

    monkeypatch.setattr("saiAutomationManager.AutomationManager", _FakeAutomationManager)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/advanced/automations?switch_id=desk-hub")

    assert res.status_code == 200
    body = res.json()
    assert body["switch_id"] == "desk-hub"
    assert [item["rule_id"] for item in body["items"]] == ["desk-fan-rule", "legacy-local-rule"]
