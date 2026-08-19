"""Pytest coverage for onboarding and settings-schema materialization.

These tests exercise route-level settings writes, broker rewriting, Astral value
handling, and Nodus-facing config payload generation.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from datetime import datetime, timedelta
from jinja2 import Environment, FileSystemLoader
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiAddDevice as saiAddDevice
import sensorius.saiDataLogger as saiDataLoggerModule
import sensorius.saiMQTTIngest as saiMQTTIngest
import sensorius.saiSensorSettingsManager as saiSensorSettingsManager
import sensorius.saiSettings as saiSettingsModule
import sensorius.saiSwitchSettingsManager as saiSwitchSettingsManager
import sensorius.saiWebRoutes as saiWebRoutes
import sensorius.saiCalibration as saiCalibration
from sensorius.sensor_modules.station_weewx import WEEWX_RAIN_24H_METRIC

_REAL_SENSOR_SETTINGS_MANAGER = saiSensorSettingsManager.SensorSettingsManager
_REAL_SWITCH_SETTINGS_MANAGER = saiSwitchSettingsManager.SwitchSettingsManager


def test_nodus_sgp_display_defaults_use_concrete_hardware():
    assert saiWebRoutes._nodus_display_defaults_for_device("voc", "SGP30")[:2] == [
        "Equivalent CO2",
        "TVOC",
    ]
    assert saiWebRoutes._nodus_display_defaults_for_device("voc", "SGP40")[:2] == [
        "VOC Index",
        "",
    ]
    assert saiWebRoutes._nodus_display_defaults_for_device("voc", "SGP41")[:2] == [
        "VOC Index",
        "NOx Index",
    ]
    assert saiWebRoutes._infer_nodus_sensor_device(
        "sensor-test123",
        ["Equivalent CO2", "TVOC"],
    ) == "voc"


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


class _HubSettingsWithAltitude(_HubSettings):
    def __init__(self, altitude: str = "1624.00"):
        self.altitude = altitude

    def get_setting(self, section, key, default=None):
        if section == "Astral" and key == "ALTITUDE":
            return self.altitude
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
        self.meta_patches_by_message: dict[str, dict] = {}
        self.next_meta_patch_by_action: dict[str, dict | None] = {}
        self.device_location: dict[str, str] = {}
        self.expected_gauge_map: dict[str, list[str]] = {}
        self.device_status: dict[str, str] = {}
        self.nodus_liveness: dict[str, dict] = {}
        self.nodus_switch_topic_map: dict[str, dict] = {}
        self.nodus_firmware_versions: dict[str, str] = {}
        self.nodus_board_types: dict[str, str] = {}
        self._switch_state_cache: dict[str, dict] = {}
        self._host_ip_cache: dict[str, str] = {}
        self._host_ipv4addr: dict[str, str] = {}
        self._removed_nodus_ids: set[str] = set()
        self.next_config_ack: dict | None = {"accepted": True}
        self.next_config_result: dict | None = {"applied": True, "updated": 1, "error": ""}
        self.next_switch_command_ok: bool = True
        self.ha_client = None
        self.client = SimpleNamespace(published=[])
        self.client.publish = self._publish_raw

    def set_onboarding_event_handler(self, handler):
        self.handler = handler

    def _publish_raw(self, topic: str, payload="", qos: int = 0, retain: bool = False):
        self.client.published.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain,
            }
        )
        return SimpleNamespace(rc=0)

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

    def suppress_nodus_devices(self, device_ids, *, persist=True):
        added = []
        for device_id in device_ids or []:
            value = str(device_id or "").strip().lower()
            if value and value not in self._removed_nodus_ids:
                self._removed_nodus_ids.add(value)
                added.append(value)
        return {
            "added": added,
            "persisted": bool(persist),
            "persistence_supported": True,
            "active": bool(self._removed_nodus_ids),
        }

    def allow_nodus_devices(self, device_ids, *, persist=True):
        removed = []
        for device_id in device_ids or []:
            value = str(device_id or "").strip().lower()
            if value in self._removed_nodus_ids:
                self._removed_nodus_ids.remove(value)
                removed.append(value)
        return {"removed": removed, "persisted": bool(persist)}

    def get_measure_status(self, _sid: str):
        return "online"

    def get_nodus_liveness(self, device_id: str, **_kwargs):
        return dict(self.nodus_liveness.get(str(device_id or "").strip(), {"state": "unknown"}))

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
            "payload": {},
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

    def publish_nodus_calibration(
        self,
        device_id: str,
        *,
        action: str,
        payload=None,
        message_id=None,
        qos=1,
        sensor_id="",
        name="",
    ):
        message = {
            "device_id": device_id,
            "sensor_id": sensor_id,
            "name": name,
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

    def set_switch(self, switch_id: str, channel_label: str, new_state: bool, qos: int = 0, retain: bool = False, *, event_origin: str | None = None, event_label: str | None = None):
        self.switch_commands.append(
            {
                "switch_id": switch_id,
                "channel_label": channel_label,
                "new_state": bool(new_state),
                "qos": qos,
                "retain": retain,
                "event_origin": event_origin,
                "event_label": event_label,
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

    async def wait_for_nodus_meta_patch(self, message_id: str, *, source: str | None = None, timeout: float = 0):
        if message_id in self.meta_patches_by_message:
            payload = self.meta_patches_by_message.get(message_id)
        else:
            action = ""
            for row in self.calibration_commands:
                if row.get("message_id") == message_id:
                    action = str(row.get("action") or "")
                    break
            payload = self.next_meta_patch_by_action.get(action)
        if payload is None:
            return None
        out = dict(payload)
        out.setdefault("message_id", message_id)
        if source:
            out_source = str(out.get("source") or "").strip().lower()
            if out_source != str(source or "").strip().lower():
                return None
        return out

    def get_nodus_calibration_state(self, sensor_id: str):
        state = self.calibration_state.get(sensor_id)
        return dict(state) if isinstance(state, dict) else state

    def get_nodus_firmware_version(self, device_id: str | None, device_type: str | None = None):
        dev = str(device_id or "").strip()
        if not dev:
            return ""
        return str(self.nodus_firmware_versions.get(dev) or "")

    def get_nodus_board_type(self, device_id: str | None, device_type: str | None = None):
        dev = str(device_id or "").strip()
        if not dev:
            return ""
        return str(self.nodus_board_types.get(dev) or "")


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


class _AllMetricFakeSaiSettings(_FakeSaiSettings):
    def get_setting(self, section, key, default=None):
        if section == "Display" and key == "metric_set":
            return "All"
        if section == "Display" and key == "display_style":
            return "Graph6hr"
        return default


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
    RESOLVED_ASTRAL: dict | None = None

    def get_setting(self, section, key, default=None):
        return self.settings.get(section, {}).get(key, default)

    def replace_setting(self, section, key, value):
        bucket = self.settings.setdefault(section, {})
        bucket[key] = value
        self.save_settings()

    def resolve_astral_location(self, persist_if_auto=False, timeout_sec=0):
        response = self.__class__.RESOLVED_ASTRAL
        if response is None:
            lat = self.get_setting("Astral", "LATITUDE", "")
            lon = self.get_setting("Astral", "LONGITUDE", "")
            tz_name = self.get_setting("Astral", "TIMEZONE", "") or self.get_setting("Time", "TZ", "")
            source = self.get_setting("Astral", "SOURCE", "")
            provider = self.get_setting("Astral", "PROVIDER", "")
            return {
                "lat": float(lat) if str(lat).strip() else None,
                "lon": float(lon) if str(lon).strip() else None,
                "tz": str(tz_name or "").strip(),
                "source": str(source or "").strip() or ("manual" if str(lat).strip() and str(lon).strip() else "none"),
                "provider": str(provider or "").strip(),
                "error": "",
            }
        out = dict(response)
        if persist_if_auto and out.get("source") == "ip" and out.get("lat") is not None and out.get("lon") is not None:
            self.replace_setting("Astral", "LATITUDE", f"{float(out['lat']):.6f}")
            self.replace_setting("Astral", "LONGITUDE", f"{float(out['lon']):.6f}")
            self.replace_setting("Astral", "SOURCE", "ip")
            self.replace_setting("Astral", "PROVIDER", str(out.get("provider") or ""))
            if out.get("tz"):
                self.replace_setting("Astral", "TIMEZONE", str(out["tz"]))
        return out

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


def _write_system_settings(root: Path, device_id: str, hostname: str, *, broker: str = "") -> None:
    target = root / device_id
    target.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Network]",
        f'HOSTNAME = "{hostname}"',
    ]
    if broker:
        lines.extend([
            "",
            "[MQTT]",
            f'BROKER = "{broker}"',
        ])
    (target / "settings.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _build_app(tmp_path, monkeypatch, hub_settings=None, ota_service=None):
    system_root = tmp_path / "system_settings"
    sensor_root = tmp_path / "sensor_settings"
    switch_root = tmp_path / "switch_settings"
    system_root.mkdir()
    sensor_root.mkdir()
    switch_root.mkdir()

    saiWebRoutes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None

    _FakeSaiSettings.DEFAULT_BASE_DIR = str(system_root)

    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _FakeSaiSettings)
    monkeypatch.setattr(saiWebRoutes, "SystemSettingsManager", _FakeSystemSettingsManager, raising=False)
    monkeypatch.setattr(saiWebRoutes, "SensorSettingsManager", lambda *_a, **_k: _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)))
    monkeypatch.setattr(saiSensorSettingsManager, "SensorSettingsManager", lambda *_a, **_k: _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)))
    monkeypatch.setattr(saiWebRoutes, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
    monkeypatch.setattr(saiSwitchSettingsManager, "SwitchSettingsManager", lambda *_a, **_k: _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root)))
    app = FastAPI()
    if ota_service is not None:
        app.state.nodus_ota_service = ota_service
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, hub_settings or _HubSettings(), _FakeNetMgr(), _FakeGcMgr(), ingest)
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


@pytest.mark.asyncio
async def test_custom_theme_upload_and_list_routes(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    image_buffer = io.BytesIO()
    Image.new("RGB", (640, 360), "#8bbf8b").save(image_buffer, "PNG")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/themes",
            data={
                "section": "biodynamic",
                "name": "Moon Garden",
                "image_names": "Moonlit Beds",
                "palettes": "pale_water",
            },
            files={"images": ("moon-garden.png", image_buffer.getvalue(), "image/png")},
        )
        listed = await client.get("/api/themes", params={"section": "biodynamic"})

    assert created.status_code == 200
    payload = created.json()
    assert payload["theme"]["section"] == "biodynamic"
    assert payload["theme"]["images"][0]["name"] == "Moonlit Beds"
    assert payload["theme"]["images"][0]["selection"].startswith("custom:")
    assert listed.status_code == 200
    assert listed.json()["themes"][0]["name"] == "Moon Garden"
    assert list((tmp_path / "theme_assets").glob("*/*.webp"))


@pytest.mark.asyncio
async def test_bd_transition_test_endpoint_broadcasts_to_live_dashboards(
    tmp_path,
    monkeypatch,
):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
    )

    class _CalendarService:
        @staticmethod
        def current_transition_sync():
            return {
                "transition_at": "2026-08-16T00:00:00-06:00",
                "sign": "Virgo",
                "element": "Earth",
                "plant_part": "Root",
                "color": "#e5b172",
                "accent": "#644817",
            }

    broadcasts = []

    async def _broadcast(payload):
        broadcasts.append(payload)

    app.state.biodynamic_calendar_service = _CalendarService()
    app.state.switch_broadcast = _broadcast

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/advanced/automations/test-bd-transition")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(broadcasts) == 1
    assert broadcasts[0]["type"] == "bd_transition"
    assert broadcasts[0]["test"] is True
    assert broadcasts[0]["from"]["sign"] == "Virgo"
    assert broadcasts[0]["to"]["plant_part"] == "Root"


@pytest.mark.asyncio
async def test_ota_package_browse_defaults_to_service_package_root(tmp_path, monkeypatch):
    package_root = tmp_path / "ota_packages"
    (package_root / "pkg-one").mkdir(parents=True)
    ota_service = saiWebRoutes.NodusOTAService(package_root=package_root)
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        ota_service=ota_service,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"sec-fetch-site": "same-origin"},
    ) as client:
        res = await client.get("/api/nodus-ota/package/browse")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    folder = body["folder"]
    assert folder["path"] == str(package_root.resolve())
    assert {row["name"] for row in folder["directories"]} == {"pkg-one"}


async def _build_route_app_with_settings(tmp_path, monkeypatch, stored_settings: dict):
    _RouteFakeSaiSettings.STORED_SETTINGS = {
        section: dict(values or {})
        for section, values in (stored_settings or {}).items()
    }
    _RouteFakeSaiSettings.RESOLVED_ASTRAL = None
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


def test_system_settings_template_has_astral_altitude_field():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert 'class="field-grid-astral"' in text
    assert '<label for="astral_altitude">Altitude (m)</label>' in text
    assert 'id="astral_altitude" name="astral_altitude"' in text
    assert text.index('id="astral_lat"') < text.index('id="astral_lon"') < text.index('id="astral_altitude"')
    for removed_id in ("astral_sunrise", "astral_sunset", "astral_daylight", "astral_noon"):
        assert f'id="{removed_id}"' not in text


def test_system_settings_template_has_caelus_location_name_beside_timezone():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")
    general_section = text[
        text.index('data-runtime-section="system-general"'):
        text.index('data-runtime-section="system-wifi"')
    ]

    assert '<label for="astral_location_name">Community/Location Name</label>' in general_section
    assert 'id="astral_location_name" name="astral_location_name"' in general_section
    assert general_section.index('id="tz"') < general_section.index('id="astral_location_name"')


def test_system_settings_template_has_weather_forecast_controls():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert 'class="field-grid-network"' in text
    assert 'data-runtime-section="system-weather-forecast"' in text
    assert '<label for="weather_forecast_provider">Forecast Provider</label>' in text
    assert 'name="weather_forecast_provider"' in text
    assert '<option value="met_no"' in text
    assert '<option value="us"' in text
    assert '<option value="open_meteo"' in text
    assert '<option value="none"' in text
    assert 'name="weather_forecast_theme"' in text
    assert 'name="weather_forecast_sensor_id"' in text
    assert 'class="weather-forecast-controls"' in text
    assert 'id="weather_forecast_theme" style="--thumbnail-count:5"' in text
    for theme in ("pollinator", "garden", "island", "river", "desert"):
        assert f'name="weather_forecast_theme" value="{theme}"' in text
    assert "The forecast uses the Sensorius Astral location." not in text


def test_system_settings_display_has_conditional_gauge_size_and_theme_thumbnails():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    factory_settings = Path(__file__).resolve().parents[1] / "system_settings" / "factory" / "settings.toml"
    text = source.read_text(encoding="utf-8")
    factory_text = factory_settings.read_text(encoding="utf-8")
    display_section = text[
        text.index('data-runtime-section="system-display"'):
        text.index('<div class="status-text sai-live-status" id="system-status"')
    ]

    assert display_section.index('id="unit_system"') < display_section.index('id="metric_set"')
    assert display_section.index('id="metric_set"') < display_section.index('id="display_style"')
    assert display_section.index('id="display_style"') < display_section.index('id="gauge_size"')
    assert 'class="field-grid-stack display-settings-top-row"' in display_section
    assert 'id="gauge_size_field"' in display_section
    assert '<label for="unit_system">Units</label>' in display_section
    assert '<option value="Imperial"' in display_section
    assert '<option value="Metric"' in display_section
    assert '<label for="metric_set">Metric Set</label>' in display_section
    assert '<option value="Pick 6"' in display_section
    assert '<option value="All"' in display_section
    assert 'id="dashboard_background_theme"' in display_section
    assert '<summary>Sensorius Dashboard Theme</summary>' in display_section
    assert '<legend class="theme-selector-legend">Sensorius Dashboard Theme</legend>' in display_section
    assert 'id="weather_forecast_theme"' in display_section
    assert '<summary>Caelus Theme</summary>' in display_section
    assert '<legend class="theme-selector-legend">Caelus Theme</legend>' in display_section
    assert 'id="biodynamic_calendar_theme"' in display_section
    assert '<summary>Biodynamic Calendar Theme</summary>' in display_section
    assert '<legend class="theme-selector-legend">Biodynamic Calendar Theme</legend>' in display_section
    assert display_section.count('<details class="theme-section">') == 3
    assert display_section.count('<div class="theme-section-content">') == 3
    assert '#setupPiModal .theme-section > summary {' in text
    assert '#setupPiModal .theme-section > summary::before {' in text
    assert '#setupPiModal .theme-section[open] > summary::before' in text
    assert 'text-align: left;' in text[
        text.index('#setupPiModal .theme-section > summary {'):
        text.index('#setupPiModal .theme-section > summary::-webkit-details-marker')
    ]
    assert display_section.count('style="--thumbnail-count:5"') == 3
    for theme in ("leaf", "root", "leaf_crop", "flower", "fruit"):
        assert f'name="dashboard_background_theme" value="{theme}"' in display_section
    for theme in ("auto", "garden_tools", "spring", "summer", "autumn", "winter"):
        assert f'name="biodynamic_calendar_theme" value="{theme}"' in display_section
    biodynamic_builtin = display_section[
        display_section.index('id="biodynamic_calendar_theme"'):
        display_section.index('{% for theme in custom_themes.biodynamic')
    ]
    assert biodynamic_builtin.count('class="thumbnail-option"><input type="radio" name="biodynamic_calendar_theme"') == 5
    assert display_section.count('open-custom-theme') == 3
    assert 'data-theme-section="sensorius"' in display_section
    assert 'data-theme-section="caelus"' in display_section
    assert 'data-theme-section="biodynamic"' in display_section
    assert "Automatic Season Rotation uses only the built-in seasonal themes." in text
    assert 'customThemeField("Image Selector", file)' in text
    assert 'customThemeField("Image", preview)' in text
    assert 'customThemeField("Palette Selector", palette)' in text
    assert 'customThemeField("Image Name", name)' in text
    assert 'remove.textContent = "Remove Image"' in text
    assert 'customThemeAddImage.textContent = "Add Image"' in text
    assert 'main.append(fileWrap, previewWrap, paletteWrap)' in text
    assert 'row.append(main, nameWrap, actions)' in text
    assert 'justify-content:space-between' in text
    assert '#setupPiModal .sai-system-dialog.custom-theme-dialog { width:min(54rem' in text
    assert '#setupPiModal .custom-theme-image-actions .custom-theme-remove-image { margin-left:auto; }' in text
    assert '#setupPiModal .custom-theme-dialog .sai-system-dialog-actions .button {' in text
    assert '#setupPiModal .custom-theme-dialog .sai-system-dialog-actions #custom-theme-create {' in text
    assert 'background:var(--dashboard-card-bg' in text
    assert 'background:var(--dashboard-dialog-panel' in text
    assert 'border:1px solid var(--dashboard-card-border' in text
    assert "Automatic Season Rotation" in display_section
    assert "Changes between Spring, Summer, Autumn, and Winter." not in display_section
    assert display_section.index('class="thumbnail-options"', display_section.index('id="biodynamic_calendar_theme"')) < display_section.index('class="biodynamic-auto-option"')
    assert display_section.index('id="dashboard_background_theme"') < display_section.index('id="weather_forecast_theme"')
    assert display_section.index('id="weather_forecast_theme"') < display_section.index('id="biodynamic_calendar_theme"')
    assert 'background_theme = "leaf"' in factory_text
    assert 'metric_set = "Pick 6"' in factory_text
    assert 'unit_system = "Imperial"' in factory_text
    assert 'biodynamic_calendar_theme = "garden_tools"' in factory_text
    assert 'THEME = "pollinator"' in factory_text
    assert 'if (ev?.target?.id === "display_style")' in text
    assert "updateGaugeSizeVisibility();" in text


def test_system_settings_sections_match_integration_accordions():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    section_markup = 'class="integration-block system-section-block"'
    assert text.count(section_markup) == 6
    assert 'data-target="pane-system">General Settings</button>' in text
    assert '<h3 class="pane-title">General Settings</h3>' in text
    assert f'<details {section_markup} data-runtime-section="system-general">' in text
    assert f'<details {section_markup} id="nodus-wifi-section" data-runtime-section="system-wifi">' in text
    assert '<summary>Network Settings</summary>' in text
    expected_order = (
        'data-runtime-section="system-astral"',
        'data-runtime-section="system-display"',
        'data-runtime-section="system-general"',
        'data-runtime-section="system-wifi"',
        'data-runtime-section="system-notifications"',
        'data-runtime-section="system-weather-forecast"',
    )
    assert [text.index(item) for item in expected_order] == sorted(
        text.index(item) for item in expected_order
    )
    assert text.count('class="button blue btn-system-save"') == 5
    system_pane = text[text.index('<div class="settings-pane" id="pane-system">'):text.index('<div class="settings-pane" id="pane-automations"')]
    assert 'class="button black btn-back-system">Dashboard</button>' not in system_pane
    assert system_pane.count('class="pane-footer section-action-footer"') == 6
    assert 'class="pane-footer pane-global-footer"' not in system_pane
    assert 'id="btn-system-save"' not in text


def test_notification_subsection_titles_are_left_aligned():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert "#setupPiModal .notification-subsection > summary {" in text
    assert "  text-align: left;" in text
    assert "#setupPiModal .notification-subsection > summary::before {" in text


def test_notifications_has_one_email_action_row():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    email_section = text[
        text.index("<summary>Notifications</summary>"):
        text.index("<summary>Weather Forecast</summary>")
    ]
    assert "<summary>Notification Rules</summary>" not in email_section
    assert 'class="pane-footer section-action-footer"' in email_section
    assert 'class="button black btn-back-system">Dashboard</button>' not in email_section
    assert 'class="button blue btn-system-save">Save</button>' in email_section
    assert 'class="email-grid-two email-server-grid"' in email_section
    assert 'class="email-port-security-grid"' in email_section
    assert email_section.index('for="email_smtp_host"') < email_section.index('class="email-port-security-grid"')
    assert email_section.index('for="email_smtp_port"') < email_section.index('for="email_security"')
    assert email_section.index('for="email_security"') < email_section.index('for="email_username"')


def test_system_settings_template_uses_compact_field_spacing():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert "#setupPiModal .field-stack {\n  display: flex;\n  flex-direction: column;\n  gap: .16rem;" in text
    assert "#setupPiModal .field-stack label {\n  font-weight: 600;\n  margin: 0;\n  line-height: 1.1;" in text
    assert "min-height: 2.72rem;" in text
    assert "gap: .5rem .9rem;" in text


def test_system_settings_weewx_pane_omits_inline_mqtt_instructions():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert "id=\"pane-integrations\"" in text
    integrations_pane = text[text.index('id="pane-integrations"'):text.index('id="pane-locations"')]
    assert integrations_pane.count('class="integration-block"') == 3
    assert "id=\"weewx-conf-example\"" not in text
    assert "Configure the WeeWX MQTT extension" not in text
    assert "[StdRESTful]" not in text


def test_system_settings_titlebar_close_button_closes_modal():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert '>Dashboard</button>' not in text
    assert 'class="settings-title-close btn-back-system"' in text
    assert 'classList.contains("btn-back-system")) {\n      if (document.getElementById("pane-add")' in text
    assert "      goHomeFromSettings();\n    }" in text


def test_system_settings_save_applies_dashboard_weather_forecast_change():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert "function applyDashboardWeatherForecastChange" in text
    assert "body.get(\"weather_forecast_provider\")" in text
    assert "window.loadWeatherForecast(true)" in text
    assert "window.location.reload()" in text


def test_system_settings_save_serializes_only_clicked_section():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert 'saveButton?.closest(".system-section-block")' in text
    assert "const body = sectionFormBody(section);" in text
    assert 'section.querySelectorAll("input[name], select[name], textarea[name]")' in text
    assert "new URLSearchParams(new FormData(form))" not in text
    assert 'classList.contains("btn-system-save")' in text
    assert "saveSystemSettings(ev.target);" in text


def test_system_settings_save_tolerates_absent_notification_rule_editor():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    declaration = 'const notificationRulesInput = document.getElementById("notification_rules_json");'
    assert declaration in text
    assert text.index(declaration) < text.index("function syncNotificationRulesInput()")
    assert "if (!notificationRulesInput || !notificationRuleList) return;" in text


def test_remove_device_success_reloads_dashboard():
    root = Path(__file__).resolve().parents[1] / "ui_templates" / "modals"
    system_settings = (root / "system_settings.html").read_text(encoding="utf-8")
    standalone_remove = (root / "system_remove_device.html").read_text(encoding="utf-8")

    for text in (system_settings, standalone_remove):
        assert "Reloading dashboard" in text
        assert "window.location.reload()" in text
        assert "await loadRemovableDevices();" not in text
        assert "showRemoveDeviceNotice" in text
        assert "Removing ${targetLabel}..." in text
        assert "Removed ${targetLabel}. Reloading dashboard..." in text
        assert "Failed to remove ${targetLabel}:" in text

    assert 'class="status-text sai-live-status" id="remove-device-status" aria-live="polite"' in system_settings
    assert 'class="sai-live-status"\n         aria-live="polite"' in standalone_remove


@pytest.mark.asyncio
async def test_submit_pi_setup_blank_astral_fields_clear_saved_astral_location(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Astral": {
                "LATITUDE": "40.015000",
                "LONGITUDE": "-105.270500",
                "ALTITUDE": "1624.00",
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
                "astral_altitude": "",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == ""
    assert stored["Astral"]["LONGITUDE"] == ""
    assert stored["Astral"]["ALTITUDE"] == ""
    assert stored["Astral"]["TIMEZONE"] == ""
    assert stored["Astral"]["SOURCE"] == ""
    assert stored["Astral"]["PROVIDER"] == ""


@pytest.mark.asyncio
async def test_submit_pi_setup_blank_astral_fields_auto_detects_when_available(tmp_path, monkeypatch):
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
    _RouteFakeSaiSettings.RESOLVED_ASTRAL = {
        "lat": 39.7392,
        "lon": -104.9903,
        "tz": "America/Denver",
        "source": "ip",
        "provider": "ipapi.co",
    }

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
    body = res.json()
    assert body["ok"] is True
    assert body["astral"] == {
        "ok": True,
        "source": "ip",
        "provider": "ipapi.co",
        "error": "",
        "lat": 39.7392,
        "lon": -104.9903,
        "tz": "America/Denver",
    }
    assert "re-detected" in body["message"]
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == "39.739200"
    assert stored["Astral"]["LONGITUDE"] == "-104.990300"
    assert stored["Astral"]["TIMEZONE"] == "America/Denver"
    assert stored["Astral"]["SOURCE"] == "ip"
    assert stored["Astral"]["PROVIDER"] == "ipapi.co"


@pytest.mark.asyncio
async def test_submit_pi_setup_partial_blank_astral_fields_return_error(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Astral": {
                "LATITUDE": "40.015000",
                "LONGITUDE": "-105.270500",
                "ALTITUDE": "1624.00",
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
                "astral_lon": "-105.270500",
                "astral_altitude": "1600",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 400
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == "40.015000"
    assert stored["Astral"]["LONGITUDE"] == "-105.270500"
    assert stored["Astral"]["ALTITUDE"] == "1624.00"
    assert stored["Astral"]["TIMEZONE"] == "America/Denver"


@pytest.mark.asyncio
async def test_submit_pi_setup_persists_astral_altitude(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "broker": "",
                "tz": "America/Denver",
                "httpport": "8000",
                "astral_lat": "40.015",
                "astral_lon": "-105.2705",
                "astral_altitude": "1624",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == "40.015000"
    assert stored["Astral"]["LONGITUDE"] == "-105.270500"
    assert stored["Astral"]["ALTITUDE"] == "1624.00"
    assert stored["Astral"]["TIMEZONE"] == "America/Denver"
    assert stored["Astral"]["SOURCE"] == "manual"
    assert stored["Astral"]["PROVIDER"] == ""


@pytest.mark.asyncio
async def test_submit_pi_setup_persists_optional_caelus_location_name(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Time": {"TZ": "America/Denver", "TZ_OFFSET": -21600, "TZ_NAME": "MDT"},
            "Astral": {"LOCATION_NAME": "Old Location"},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={"tz": "America/Denver", "astral_location_name": "Silver City"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 200
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LOCATION_NAME"] == "Silver City"
    assert stored["Time"]["TZ"] == "America/Denver"


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
async def test_submit_pi_setup_persists_sensornetwork_tls_and_mqtt_port(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "broker": "hub.local",
                "tz": "America/Denver",
                "httpport": "8000",
                "mqttport": "8883",
                "sensornetwork_use_tls": "on",
                "astral_lat": "",
                "astral_lon": "",
                "gauge_size": "medium",
                "display_style": "grid",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["SensorNetwork"]["BROKER"] == "hub.local"
    assert stored["SensorNetwork"]["MQTTPORT"] == 8883
    assert stored["SensorNetwork"]["USE_TLS"] is True


@pytest.mark.asyncio
async def test_submit_pi_setup_persists_weather_forecast_provider(tmp_path, monkeypatch):
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
                "weather_forecast_provider": "open_meteo",
                "weather_forecast_theme": "desert",
                "weather_forecast_sensor_id": "nodus-weather",
            },
            follow_redirects=False,
        )

    assert res.status_code == 303
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["WeatherForecast"]["PROVIDER"] == "open_meteo"
    assert stored["WeatherForecast"]["THEME"] == "desert"
    assert stored["WeatherForecast"]["CURRENT_SENSOR_ID"] == "nodus-weather"


@pytest.mark.asyncio
async def test_submit_pi_setup_display_section_does_not_write_other_sections(tmp_path, monkeypatch):
    initial = {
        "Network": {"HTTPPORT": 8123},
        "SensorNetwork": {"BROKER": "hub.local", "MQTTPORT": 8883, "USE_TLS": True},
        "Time": {"TZ": "America/Denver", "TZ_OFFSET": -21600, "TZ_NAME": "MDT"},
        "Astral": {
            "LATITUDE": "40.015000",
            "LONGITUDE": "-105.270500",
            "ALTITUDE": "1624.00",
            "TIMEZONE": "America/Denver",
            "SOURCE": "manual",
            "PROVIDER": "",
        },
        "Display": {"gauge_size": "Small", "display_style": "Gauge"},
        "WeatherForecast": {"PROVIDER": "open_meteo", "THEME": "desert", "CURRENT_SENSOR_ID": "weather-one"},
    }
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, initial)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={"gauge_size": "Large", "display_style": "Graph24hr"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 200
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Display"] == {"gauge_size": "Large", "display_style": "Graph24hr"}
    for section in ("Network", "SensorNetwork", "Time", "Astral", "WeatherForecast"):
        assert stored[section] == initial[section]


@pytest.mark.asyncio
async def test_submit_pi_setup_persists_metric_set_and_independent_background_themes(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {"Display": {"gauge_size": "Small", "display_style": "Gauge", "background_theme": "leaf"}},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={
                "gauge_size": "Large",
                "display_style": "Gauge",
                "metric_set": "All",
                "unit_system": "Metric",
                "dashboard_background_theme": "flower",
                "biodynamic_calendar_theme": "autumn",
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 200
    assert _RouteFakeSaiSettings.STORED_SETTINGS["Display"] == {
        "gauge_size": "Large",
        "display_style": "Gauge",
        "metric_set": "All",
        "unit_system": "Metric",
        "background_theme": "flower",
        "biodynamic_calendar_theme": "autumn",
    }


@pytest.mark.asyncio
async def test_submit_pi_setup_rejects_unsupported_metric_set(tmp_path, monkeypatch):
    initial = {"Display": {"metric_set": "Pick 6"}}
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, initial)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={"metric_set": "Everything"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 400
    assert _RouteFakeSaiSettings.STORED_SETTINGS == initial


@pytest.mark.asyncio
async def test_submit_pi_setup_rejects_unsupported_unit_system(tmp_path, monkeypatch):
    initial = {"Display": {"unit_system": "Imperial"}}
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, initial)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={"unit_system": "Native"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 400
    assert _RouteFakeSaiSettings.STORED_SETTINGS == initial


@pytest.mark.asyncio
async def test_submit_pi_setup_astral_section_uses_saved_timezone_without_writing_other_sections(tmp_path, monkeypatch):
    initial = {
        "Network": {"HTTPPORT": 8123},
        "SensorNetwork": {"BROKER": "hub.local"},
        "Time": {"TZ": "America/Denver", "TZ_OFFSET": -21600, "TZ_NAME": "MDT"},
        "Astral": {"LATITUDE": "", "LONGITUDE": "", "ALTITUDE": ""},
        "Display": {"gauge_size": "Small", "display_style": "Gauge"},
    }
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, initial)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-pi-setup",
            data={"astral_lat": "39.7392", "astral_lon": "-104.9903", "astral_altitude": "1609"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    assert res.status_code == 200
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["Astral"]["LATITUDE"] == "39.739200"
    assert stored["Astral"]["LONGITUDE"] == "-104.990300"
    assert stored["Astral"]["ALTITUDE"] == "1609.00"
    assert stored["Astral"]["TIMEZONE"] == "America/Denver"
    for section in ("Network", "SensorNetwork", "Time", "Display"):
        assert stored[section] == initial[section]


@pytest.mark.asyncio
async def test_advanced_database_save_writes_only_retention_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SENSORIUS_DB_RETENTION_DAYS", "90")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SENSORIUS_LOG_LEVEL=WARNING\n"
        "SENSORIUS_FILE_LOG=true\n"
        "SENSORIUS_DEBUG_MODULES=saiDataLogger\n"
        "SENSORIUS_DB_RETENTION_DAYS=90\n"
        "SENSORIUS_AUTOSTART_SCOPE=user\n"
        "SENSORIUS_AUTOSTART_ENABLED=false\n",
        encoding="utf-8",
    )
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/advanced/save", json={"db_retention_days": 120})

    assert res.status_code == 200
    saved_env = env_path.read_text(encoding="utf-8")
    assert "SENSORIUS_DB_RETENTION_DAYS=120" in saved_env
    assert "SENSORIUS_LOG_LEVEL=WARNING" in saved_env
    assert "SENSORIUS_FILE_LOG=true" in saved_env
    assert "SENSORIUS_DEBUG_MODULES=saiDataLogger" in saved_env
    assert "SENSORIUS_AUTOSTART_SCOPE=user" in saved_env
    assert "SENSORIUS_AUTOSTART_ENABLED=false" in saved_env


def test_advanced_save_builds_payload_from_clicked_section_only():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    assert 'saveButton?.closest(".advanced-section")' in text
    assert 'section.id === "adv-section-startup"' in text
    assert 'section.id === "adv-section-database"' in text
    assert 'section.id === "adv-section-debug"' in text
    assert "saveAdvancedSettings(ev.target);" in text


def test_sensor_and_switch_save_forms_are_scoped_to_settings_panes():
    modal_root = Path(__file__).resolve().parents[1] / "ui_templates" / "modals"
    sensor_text = (modal_root / "sensor_settings.html").read_text(encoding="utf-8")
    switch_text = (modal_root / "switch_settings.html").read_text(encoding="utf-8")

    sensor_settings_pane = sensor_text[
        sensor_text.index('id="sensor-settings-pane"'):sensor_text.index('id="sensor-calibration-pane"')
    ]
    assert 'action="/submit-sensor-settings"' in sensor_settings_pane
    assert 'new URLSearchParams(new FormData(form))' in sensor_text
    assert sensor_text.index("</form>") < sensor_text.index('id="sensor-calibration-pane"')

    switch_settings_pane = switch_text[
        switch_text.index('id="switchSettingsPane"'):switch_text.index('id="switchStatisticsPane"')
    ]
    assert 'action="/submit-switch-settings"' in switch_settings_pane
    assert switch_text.index("</form>") < switch_text.index('id="switchStatisticsPane"')


@pytest.mark.asyncio
async def test_submit_homeassistant_settings_persists_enabled_tls_and_port(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-homeassistant-settings",
            json={
                "enabled": True,
                "use_tls": True,
                "broker": "homeassistant.local",
                "port": 8883,
                "username": "ha-user",
                "password": "secret",
            },
            follow_redirects=False,
        )

    assert res.status_code == 200
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["HomeAssistant"]["ENABLED"] is True
    assert stored["HomeAssistant"]["USE_TLS"] is True
    assert stored["HomeAssistant"]["HA_BROKER"] == "homeassistant.local"
    assert stored["HomeAssistant"]["HA_MQTTPORT"] == 8883
    assert stored["HomeAssistant"]["HA_USERNAME"] == "ha-user"
    assert stored["HomeAssistant"]["HA_PASSWORD"] == "secret"


@pytest.mark.asyncio
async def test_submit_farmos_settings_persists_enabled_and_verify_tls(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-farmos-settings",
            json={
                "enabled": True,
                "verify_tls": False,
                "base_url": "https://farm.example.com",
                "access_token": "token",
                "client_id": "farm",
                "client_secret": "client-secret",
                "username": "farm-user",
                "password": "farm-pass",
                "log_bundle": "observation",
            },
            follow_redirects=False,
        )

    assert res.status_code == 200
    stored = _RouteFakeSaiSettings.STORED_SETTINGS
    assert stored["FarmOS"]["ENABLED"] is True
    assert stored["FarmOS"]["VERIFY_TLS"] is False
    assert stored["FarmOS"]["BASE_URL"] == "https://farm.example.com"
    assert stored["FarmOS"]["ACCESS_TOKEN"] == "token"
    assert stored["FarmOS"]["CLIENT_ID"] == "farm"
    assert stored["FarmOS"]["CLIENT_SECRET"] == "client-secret"
    assert stored["FarmOS"]["USERNAME"] == "farm-user"
    assert stored["FarmOS"]["PASSWORD"] == "farm-pass"
    assert stored["FarmOS"]["LOG_BUNDLE"] == "observation"


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
    saiWebRoutes._SENSOR_LOCATION_CACHE["apvpd-test123"] = (float("inf"), "Old Room")

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
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Sensor"]["LOCATION"] == "Grow Tent"
    assert saved["Display"]["METRIC_1"] == "Temperature"
    assert "apvpd-test123" not in saiWebRoutes._SENSOR_LOCATION_CACHE


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
    controller = SimpleNamespace(switch_id="switch-1", location="Old Room")
    app.state.switch_controllers = {"switch-1": controller}

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
    saved = switch_mgr.load("switch-1")
    assert saved["Switch"]["SWITCH_LOCATION"] == "Grow Tent"
    assert saved["Switch"]["SWITCH_1_LABEL"] == "Fan"
    assert controller.location == "Grow Tent"


def test_location_save_surfaces_reload_existing_dashboard_layout():
    root = Path(__file__).resolve().parents[1]
    sensor_script = (root / "ui_static" / "js" / "sensor_settings_modal.js").read_text(encoding="utf-8")
    switch_template = (root / "ui_templates" / "modals" / "switch_settings.html").read_text(encoding="utf-8")
    system_template = (root / "ui_templates" / "modals" / "system_settings.html").read_text(encoding="utf-8")
    legacy_locations = (root / "ui_templates" / "modals" / "system_device_locations.html").read_text(encoding="utf-8")

    assert "savedLocation !== initialLocation" in sensor_script
    assert "savedLocation !== initialLocation" in switch_template
    assert 'input.dataset.originalLocation = (dev.location || "").trim();' in system_template
    assert "window.setTimeout(() => window.location.reload(), 400);" in system_template
    assert "window.setTimeout(() => window.location.reload(), 400);" in legacy_locations


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
    assert len(ingest.published_json) == 13
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
    assert any(p["section"] == "Display" and p["key"] == "METRIC_4" and p["value"] == "" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_5" and p["value"] == "" for p in posted)
    assert any(p["section"] == "Display" and p["key"] == "METRIC_6" and p["value"] == "" for p in posted)
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
async def test_submit_second_sensor_settings_targets_physical_host_and_second_file(
    tmp_path,
    monkeypatch,
):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "lux-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "lux",
                "SENSOR_ID": "lux-test123",
                "LOCATION": "Bench",
            },
            "Nodus": {
                "DEVICE_ID": "aht-lux-test123",
                "CONFIG_FILE": "sensor_i2c_2.toml",
            },
            "Display": {"METRIC_1": "Lux"},
        },
    )
    ingest.published_json.clear()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "lux-test123",
                "location": "Canopy",
            },
        )

    assert res.status_code == 303
    assert ingest.published_json
    assert all(
        row["topic"] == "nodus/aht-lux-test123/config/set"
        for row in ingest.published_json
    )
    updates = [
        update
        for row in ingest.published_json
        for update in row["payload"]["payload"]["updates"]
    ]
    assert updates == [
        {
            "sensor_id": "lux-test123",
            "name": "sensor_i2c_2.toml",
            "section": "Sensor",
            "key": "LOCATION",
            "value": "Canopy",
        }
    ]


@pytest.mark.asyncio
async def test_submit_sensor_settings_ignores_legacy_metric_display_mode_for_nodus(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
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
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "apvpd-test123",
                "location": "Grow Tent",
                "metric_display_mode": "All",
            },
        )

    assert res.status_code == 303
    assert ingest.published_json == []
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Display"] == {"METRIC_1": "Temperature"}


@pytest.mark.asyncio
async def test_submit_sensor_settings_pushes_explicit_blank_metric_clears_for_nodus(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-ykdvea",
                "LOCATION": "Lab",
            },
            "Display": {
                "METRIC_1": "CO2",
                "METRIC_2": "Temperature",
                "METRIC_3": "Rel-Humidity",
                "METRIC_4": "Ambient VPD",
            },
        },
    )
    _write_system_settings(system_root, "co2-ykdvea", "co2-ykdvea")
    ingest.published_json.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "co2-ykdvea",
                "sensor_id_field": "co2-ykdvea",
                "device": "co2",
                "location": "Lab",
                "metric_1": "CO2",
                "metric_2": "Temperature",
                "metric_3": "Rel-Humidity",
                "metric_4": "Ambient VPD",
                "metric_5": "",
                "metric_6": "",
            },
        )

    assert res.status_code == 303
    posted = [
        update
        for row in ingest.published_json
        for update in (((row.get("payload") or {}).get("payload") or {}).get("updates") or [])
    ]
    assert {
        "sensor_id": "co2-ykdvea",
        "section": "Display",
        "key": "METRIC_5",
        "value": "",
        "name": "sensor_i2c.toml",
    } in posted
    assert {
        "sensor_id": "co2-ykdvea",
        "section": "Display",
        "key": "METRIC_6",
        "value": "",
        "name": "sensor_i2c.toml",
    } in posted


@pytest.mark.asyncio
async def test_submit_sensor_settings_uses_extended_nodus_config_timeouts(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-ykdvea",
                "LOCATION": "Old Lab",
            },
        },
    )
    _write_system_settings(system_root, "co2-ykdvea", "co2-ykdvea")
    ingest.published_json.clear()

    ack_timeouts: list[float] = []
    result_timeouts: list[float] = []

    async def _wait_for_config_ack(message_id: str, timeout: float = 0):
        ack_timeouts.append(float(timeout))
        return {"message_id": message_id, "accepted": True, "duplicate": False, "error": ""}

    async def _wait_for_config_result(message_id: str, timeout: float = 0):
        result_timeouts.append(float(timeout))
        if float(timeout) < 20.0:
            return None
        return {"message_id": message_id, "applied": True, "updated": 1, "error": ""}

    ingest.wait_for_config_ack = _wait_for_config_ack
    ingest.wait_for_config_result = _wait_for_config_result

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/submit-sensor-settings",
            data={
                "sensor_id": "co2-ykdvea",
                "sensor_id_field": "co2-ykdvea",
                "device": "co2",
                "location": "New Lab",
            },
        )

    assert res.status_code == 303
    assert ack_timeouts == [5.0]
    assert result_timeouts == [20.0]


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
        nodus_board_type="pico2w",
        nodus_sensor_hardware="BME280",
        offline_events_24h=7,
        last_offline_epoch=1780783200.0,
        uptime_since_last_offline_label="12m 4s",
        last_offline_event_label="2026-06-06 12:00:00",
        data_packets_received=42,
        last_packet_epoch=1780783260.0,
        last_packet_received_label="11m 4s ago",
        network_info={
            "ip_address": "10.0.0.23",
            "broker": "broker.local",
            "broker_status": "Connected",
        },
        soil_ph_offset=0.0,
        device_offsets=[],
        candidate_sensors=[],
        default_range_hours=24,
        can_restart_device=True,
    )

    assert "Sensor Settings v1.2.3" in html
    assert "apvpd-test123 (pico2w)" not in html
    assert "Sensor Info: Board Type: pico2w Sensor:BME280" in html
    assert html.index('<h4 class="section-title">Network</h4>') < html.index('<h4 class="section-title">Statistics</h4>')
    assert "IP Address:" in html
    assert "10.0.0.23" in html
    assert "Broker:" in html
    assert "broker.local" in html
    assert "Broker Status:" in html
    assert "Connected" in html
    assert "Host Name:" not in html
    assert "Broker IP:" not in html
    assert "24hr Offline Events:" not in html
    assert "Last 24hr offline events:" in html
    assert "Last offline event time:" in html
    assert "Last packet received:" in html
    assert "Data packets received:" in html
    assert "Data packets last 24hr:" not in html
    assert 'data-stat-value="offline-events">7</strong>' in html
    assert 'data-stat-value="packets">42</strong>' in html
    assert 'class="sensor-location-input"' in html
    assert 'class="sensor-settings-form"' in html
    assert html.index("Restart Device") < html.index("Save")
    assert 'aria-label="Close Sensor Settings"' in html
    assert 'name="metric_display_mode"' not in html
    assert 'name="display_style_1"' in html
    assert 'name="display_style_3"' in html


@pytest.mark.asyncio
async def test_switch_settings_modal_shows_statistics_pane(tmp_path, monkeypatch):
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    template = env.get_template("modals/switch_settings.html")

    html = template.render(
        switch_id="switch-test123",
        settings={"Switch": {"SWITCH_LOCATION": "Veg Tent"}},
        channel_indices=[1],
        channels=[{"index": 1, "label": "Fan", "channel_id": "S1-test123"}],
        nodus_firmware_version="v1.2.3",
        nodus_board_type="xesp32s3",
        can_restart_device=True,
        offline_events_24h=3,
        last_offline_epoch=1780783200.0,
        uptime_since_last_offline_label="12m 4s",
        last_offline_event_label="2026-06-06 12:00:00",
        switch_last_packet_epoch=1780783260.0,
        switch_last_packet_received_label="11m 4s ago",
        switch_channel_state_stats=[
            {
                "channel_id": "S1-test123",
                "state": "Off",
                "state_epoch": 1780783260.0,
                "row_label": "S1 current state, age:",
                "state_age_label": "Off, 11m 4s",
            }
        ],
        network_info={
            "ip_address": "10.0.0.24",
            "broker": "broker.local",
            "broker_status": "Connected",
        },
    )

    assert "Switch Settings v1.2.3" in html
    assert "switch-test123 (xesp32s3)" not in html
    assert "Switch Info: Board Type: xesp32s3" in html
    assert "Statistics" in html
    assert html.index('<h4 class="section-title">Network</h4>') < html.index('<h4 class="section-title">Statistics</h4>')
    assert "IP Address:" in html
    assert "10.0.0.24" in html
    assert "Broker:" in html
    assert "broker.local" in html
    assert "Broker Status:" in html
    assert "Connected" in html
    assert "Host Name:" not in html
    assert "Broker IP:" not in html
    assert "Last 24hr offline events:" in html
    assert "Last offline event time:" in html
    assert "Last packet received:" in html
    assert "S1 current state, age:" in html
    assert "Switch packets received:" not in html
    assert "Switch packets last 24hr:" not in html
    assert "Last state change:" not in html
    assert "State changes last 24hr:" not in html
    assert "Current state age:" not in html
    assert "Data packets received:" not in html
    assert 'data-stat-value="offline-events">3</strong>' in html


@pytest.mark.asyncio
async def test_sensor_settings_modal_shows_network_info_and_recorded_statistics(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "apvpd",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Veg Tent",
                "MCU": "xesp32s3",
                "HARDWARE": "SCD4x",
            },
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    _write_system_settings(system_root, "apvpd-test123", "apvpd-test123", broker="broker.local")
    ingest._host_ipv4addr["apvpd-test123"] = "10.0.0.25"
    ingest.client.is_connected = lambda: True
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_sensor_offline_event_count",
        lambda sid, **_kwargs: 4 if sid == "apvpd-test123" else 0,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-sensor", params={"sensor_id": "apvpd-test123", "embed": "1"})

    assert res.status_code == 200
    assert "apvpd-test123 (xesp32s3)" not in res.text
    assert "Sensor Info: Board Type: xesp32s3 Sensor:SCD4x" in res.text
    assert res.text.index('<h4 class="section-title">Network</h4>') < res.text.index('<h4 class="section-title">Statistics</h4>')
    assert "IP Address:" in res.text
    assert "10.0.0.25" in res.text
    assert "Broker:" in res.text
    assert "broker.local" in res.text
    assert "Broker Status:" in res.text
    assert "Connected" in res.text
    assert "Host Name:" not in res.text
    assert "Broker IP:" not in res.text
    assert "24hr Offline Events:" not in res.text
    assert "Last 24hr offline events:" in res.text
    assert 'data-stat-value="offline-events">4</strong>' in res.text


@pytest.mark.asyncio
async def test_sensor_settings_modal_shows_local_pi_board_and_sensor_type(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_id = "co2-i2c-1-sensorius-hub-3"
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        sensor_id,
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "co2",
                "SENSOR_ID": sensor_id,
                "LOCATION": "Veg Tent",
            },
            "Display": {"METRIC_1": "CO2"},
        },
    )
    app.state.sensor_map = [
        SimpleNamespace(
            sensor_id=sensor_id,
            sensor=SimpleNamespace(sensor_id=sensor_id, _co2_model="SCD4x"),
        )
    ]
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_sensor_offline_event_count",
        lambda sid, **_kwargs: 0,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-sensor", params={"sensor_id": sensor_id, "embed": "1"})

    assert res.status_code == 200
    assert "Sensor Info: Board Type: rPi Sensor:SCD4x" in res.text
    assert f"{sensor_id} (rPi)" not in res.text


@pytest.mark.asyncio
async def test_sensor_directory_keeps_local_pi_sensor_before_first_logged_reading(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_id = "co2-i2c-1-sensorius-hub-2"
    app.state.sensor_map = [SimpleNamespace(sensor_id=sensor_id)]
    saiWebRoutes._sensor_ids_cache_payload = None
    saiWebRoutes._sensor_ids_cache_until = 0.0
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda _sid: "")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/sensor-directory")

    assert res.status_code == 200
    assert sensor_id in [row["id"] for row in res.json()]


@pytest.mark.asyncio
async def test_direct_i2c_sensor_shadow_seeded_as_nodus_is_repaired_for_calibration(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_id = "aqi-i2c-0-sensorius-0"
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        sensor_id,
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": sensor_id,
                "LOCATION": "GH Desk",
            },
            "Calibration": {
                "Device": {
                    "TEMP_OFFSET": 0.0,
                    "RH_OFFSET": 0.0,
                    "AQI_OFFSET": 0.0,
                }
            },
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": sensor_id,
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 1.25}],
            },
        )

    assert res.status_code == 200
    assert ingest.calibration_commands == []
    saved = sensor_mgr.load(sensor_id)
    assert saved["Sensor"]["TYPE"] == "pi"
    assert saved["Calibration"]["Device"]["TEMP_OFFSET"] == 1.25


@pytest.mark.asyncio
async def test_sensor_settings_modal_shows_weewx_station_model(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "weewx-station",
        {
            "Sensor": {
                "TYPE": "weewx",
                "DEVICE": "weewx",
                "SENSOR_ID": "weewx-station",
                "LOCATION": "Weather Station",
                "STATION_MODEL": "AcuRite 01536",
                "STATION_TYPE": "AcuRite",
                "STATION_DRIVER": "weewx.drivers.acurite",
            },
            "Display": {"METRIC_1": "Temperature_F"},
        },
    )
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_sensor_offline_event_count",
        lambda sid, **_kwargs: 0,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-sensor", params={"sensor_id": "weewx-station", "embed": "1"})

    assert res.status_code == 200
    assert "Sensor Info: Station: AcuRite 01536" in res.text
    assert "Station: AcuRite</span>" not in res.text


@pytest.mark.asyncio
async def test_switch_settings_modal_uses_paired_sensor_broker_info(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-ykdvea",
                "LOCATION": "Veg Tent",
            },
            "Display": {"METRIC_1": "CO2"},
        },
    )
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-ykdvea",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_LOCATION": "Veg Tent",
                "MCU": "pico2w",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-ykdvea",
                "SWITCH_1_PIN": "GP15",
            },
        },
    )
    _write_system_settings(system_root, "co2-ykdvea", "co2-ykdvea", broker="broker.local")
    ingest._host_ipv4addr["co2-ykdvea"] = "10.0.0.26"
    ingest.client.is_connected = lambda: True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-switch", params={"switch_id": "switch-ykdvea", "embed": "1"})

    assert res.status_code == 200
    assert "switch-ykdvea (pico2w)" not in res.text
    assert "Switch Info: Board Type: pico2w" in res.text
    assert res.text.index('<h4 class="section-title">Network</h4>') < res.text.index('<h4 class="section-title">Statistics</h4>')
    assert "IP Address:" in res.text
    assert "10.0.0.26" in res.text
    assert "Broker:" in res.text
    assert "broker.local" in res.text
    assert "Broker Status:" in res.text
    assert "Connected" in res.text
    assert "Host Name:" not in res.text
    assert "Broker IP:" not in res.text


@pytest.mark.asyncio
async def test_sensor_settings_modal_seeds_live_nodus_sensor_without_shadow(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    ingest.device_location["sensor/aht-yuk0nv/data"] = "Propagation Tent"

    observed_metrics = [
        "DewVPD Risk",
        "Ambient VPD",
        "Temperature_F",
        "Humidity",
        "Rel-Humidity",
        "Dew Point_F",
        "Dew Point",
        "Dew Point Deficit",
        "Temperature",
    ]
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_available_metrics",
        lambda sid: list(observed_metrics) if sid == "aht-yuk0nv" else [],
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda _sid: {})
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_sensor_offline_event_count", lambda *_a, **_k: 0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-sensor", params={"sensor_id": "aht-yuk0nv", "embed": "1"})

    assert res.status_code == 200
    assert "aht-yuk0nv (pico2w)" not in res.text
    assert "Sensor Info: Board Type: pico2w" in res.text
    assert "Sensor Info: Board Type: pico2w Sensor:" not in res.text

    saved = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)).load("aht-yuk0nv")
    assert saved["Sensor"]["TYPE"] == "nodus"
    assert saved["Sensor"]["DEVICE"] == "aht"
    assert saved["Sensor"]["LOCATION"] == "Propagation Tent"
    assert saved["Display"]["METRIC_1"] == "Ambient VPD"
    assert saved["Display"]["METRIC_2"] == "Temperature"
    assert saved["Display"]["METRIC_3"] == "Rel-Humidity"
    assert saved["Display"]["METRIC_4"] == "Humidity"


@pytest.mark.asyncio
async def test_sensor_settings_modal_shows_read_only_system_altitude_for_supported_sensor(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        _HubSettingsWithAltitude("1624.00"),
    )
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "aqi-test123",
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-test123",
                "LOCATION": "Veg Tent",
            },
            "Calibration": {
                "Device": {
                    "TEMP_OFFSET": 0.0,
                    "RH_OFFSET": 0.0,
                }
            },
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/edit-sensor", params={"sensor_id": "aqi-test123", "embed": "1"})

    assert res.status_code == 200
    html = res.text
    assert "System Altitude" in html
    assert 'data-key="Calibration.Device.ALTITUDE_METERS"' in html
    assert 'data-force-send="1"' in html
    assert 'readonly aria-readonly="true"' in html
    assert 'value="1624.0"' in html


@pytest.mark.asyncio
async def test_sensor_settings_modal_uses_fresh_system_altitude_for_local_avpd_sensor(tmp_path, monkeypatch):
    app, _ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        _HubSettings(),
    )
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "avpd-i2c-1-sensorius-hub-3",
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "avpd",
                "SENSOR_ID": "avpd-i2c-1-sensorius-hub-3",
                "LOCATION": "Sun Room",
            },
            "Calibration": {
                "Device": {
                    "TEMP_OFFSET": 0.0,
                    "RH_OFFSET": 0.0,
                }
            },
        },
    )

    class _FreshAltitudeSettings(_FakeSaiSettings):
        def get_setting(self, section, key, default=None):
            if section == "Astral" and key == "ALTITUDE":
                return "1783"
            return default

    monkeypatch.setattr(saiWebRoutes, "saiSettings", _FreshAltitudeSettings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            "/edit-sensor",
            params={"sensor_id": "avpd-i2c-1-sensorius-hub-3", "embed": "1"},
        )

    assert res.status_code == 200
    html = res.text
    assert "System Altitude" in html
    assert 'data-key="Calibration.Device.ALTITUDE_METERS"' in html
    assert 'data-force-send="1"' in html
    assert 'readonly aria-readonly="true"' in html
    assert 'value="1783.0"' in html


def test_switch_settings_modal_shows_nodus_firmware_version_in_settings_pane_title():
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    template = env.get_template("modals/switch_settings.html")

    html = template.render(
        switch_id="switch-test123",
        settings={"Switch": {"TYPE": "nodus", "SWITCH_LOCATION": "Veg Tent"}},
        channel_indices=[1],
        channels=[{"index": 1, "label": "Fan"}],
        nodus_firmware_version="v1.2.3",
        nodus_board_type="pico2w",
        can_restart_device=True,
    )

    assert "Switch Settings v1.2.3" in html
    assert "switch-test123 (pico2w)" not in html
    assert "Switch Info: Board Type: pico2w" in html
    assert html.index("Restart Device") < html.index("Save")
    assert 'aria-label="Close Switch Settings"' in html
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


def test_local_calibration_nested_soil_updates_write_device_section(tmp_path, monkeypatch):
    sensor_root = tmp_path / "sensor_settings"
    sensor_root.mkdir()
    monkeypatch.setattr(saiCalibration, "SensorSettingsManager", lambda *_a, **_k: _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root)))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "soil-123",
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "soil",
                "SENSOR_ID": "soil-123",
            },
            "Calibration": {
                "Device": {
                    "SOIL_TEMP_CAL_VAL": 0.0,
                    "SOIL_MOIST_CAL_VAL": 0.0,
                }
            },
        },
    )

    assert saiCalibration.apply_calibration_updates_local(
        "soil-123",
        {
            "soil": {
                "SOIL_PH_CAL_VAL": 0.75,
                "SOIL_MOIST_CAL_VAL": 10.0,
            }
        },
    ) is True

    saved = sensor_mgr.load("soil-123")
    assert saved["Calibration"]["Device"]["SOIL_PH_CAL_VAL"] == 0.75
    assert saved["Calibration"]["Device"]["SOIL_MOIST_CAL_VAL"] == 10.0
    assert "Soil" not in saved["Calibration"]


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
    assert ingest.published_json[-1]["payload"]["payload"] == {}
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
async def test_restart_sensor_device_accepts_legacy_restart_ack_and_result_shapes(tmp_path, monkeypatch):
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
    ingest.next_config_ack = {"message_id": "ignored-by-fake", "duplicate": False}
    ingest.next_config_result = {"message_id": "ignored-by-fake", "rebooting": True, "error": ""}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/sensor-settings/restart-device",
            data={"sensor_id": "apvpd-test123"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message"] == "Device restarting..."


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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "TEMP_OFFSET", "value": 1.5},
        ],
    }

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
    assert res.json()["shadow_synced"] is True
    assert ingest.calibration_commands[-1]["action"] == "apply"
    assert ingest.calibration_commands[-1]["payload"]["offsets"][0]["key"] == "Calibration.Device.TEMP_OFFSET"
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["TEMP_OFFSET"] == 1.5


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_uses_extended_calibration_timeouts(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-ykdvea",
            }
        },
    )
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "co2-ykdvea",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "RH_OFFSET", "value": 3.0},
        ],
    }

    ack_timeouts: list[float] = []
    result_timeouts: list[float] = []

    async def _wait_for_calibration_ack(message_id: str, timeout: float = 0):
        ack_timeouts.append(float(timeout))
        if float(timeout) < 8.0:
            return None
        return {"message_id": message_id, "accepted": True}

    async def _wait_for_calibration_result(message_id: str, timeout: float = 0):
        result_timeouts.append(float(timeout))
        if float(timeout) < 20.0:
            return None
        return {"message_id": message_id, "applied": True, "updated": 1, "error": ""}

    ingest.wait_for_calibration_ack = _wait_for_calibration_ack
    ingest.wait_for_calibration_result = _wait_for_calibration_result

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "co2-ykdvea",
                "device_kind": "co2",
                "offsets": [{"key": "Calibration.Device.RH_OFFSET", "value": 3.0}],
            },
        )

    assert res.status_code == 200
    assert ack_timeouts == [8.0]
    assert result_timeouts == [20.0]
    assert sensor_mgr.load("co2-ykdvea")["Calibration"]["Device"]["RH_OFFSET"] == 3.0


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_uses_meta_patch_fallback_when_ack_missing(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_ack = None
    ingest.next_calibration_result = None
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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "TEMP_OFFSET", "value": 2.25},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 2.25}],
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["shadow_synced"] is True
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["TEMP_OFFSET"] == 2.25


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_uses_meta_patch_fallback_when_result_missing(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_ack = {"accepted": True}
    ingest.next_calibration_result = None
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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "RH_OFFSET", "value": -1.75},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.RH_OFFSET", "value": -1.75}],
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["shadow_synced"] is True
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["RH_OFFSET"] == -1.75


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ack_payload", "expected_message"),
    [
        (None, "Calibration command was not acknowledged"),
        ({"accepted": True}, "Timed out waiting for calibration result"),
    ],
)
async def test_device_calibration_apply_for_remote_nodus_without_ack_result_or_patch_still_fails(
    tmp_path,
    monkeypatch,
    ack_payload,
    expected_message,
):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_ack = ack_payload
    ingest.next_calibration_result = None
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

    assert res.status_code == 502
    assert res.json()["message"] == expected_message
    saved = sensor_mgr.load("apvpd-test123")
    assert "Calibration" not in saved


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_ignores_meta_patch_fallback_with_wrong_source(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.next_calibration_ack = None
    ingest.next_calibration_result = None
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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "config_set",
        "updates": [
            {"section": "Calibration.Device", "key": "TEMP_OFFSET", "value": 2.25},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "apvpd-test123",
                "device_kind": "aqi",
                "offsets": [{"key": "Calibration.Device.TEMP_OFFSET", "value": 2.25}],
            },
        )

    assert res.status_code == 502
    assert res.json()["message"] == "Calibration command was not acknowledged"
    saved = sensor_mgr.load("apvpd-test123")
    assert "Calibration" not in saved


@pytest.mark.asyncio
async def test_device_calibration_apply_persists_altitude_for_local_sensor(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        _HubSettingsWithAltitude("1624.00"),
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "local-aqi",
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "aqi",
                "SENSOR_ID": "local-aqi",
            }
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "local-aqi",
                "device_kind": "aqi",
                "offsets": [
                    {"key": "Calibration.Device.ALTITUDE_METERS", "value": 9999.0, "force": True}
                ],
            },
        )

    assert res.status_code == 200
    assert ingest.calibration_commands == []
    saved = sensor_mgr.load("local-aqi")
    assert saved["Calibration"]["Device"]["ALTITUDE_METERS"] == 1624.0
    assert res.json()["applied"] == ["Calibration.Device.ALTITUDE_METERS"]


@pytest.mark.asyncio
async def test_device_calibration_apply_sends_altitude_even_when_shadow_value_matches(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        _HubSettingsWithAltitude("1624.00"),
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-test123",
            },
            "Calibration": {
                "Device": {
                    "ALTITUDE_METERS": 1624.0,
                }
            },
        },
    )
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "co2-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "ALTITUDE_METERS", "value": 1624.0},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "co2-test123",
                "device_kind": "co2",
                "offsets": [
                    {"key": "Calibration.Device.ALTITUDE_METERS", "value": 9999.0, "force": True}
                ],
            },
        )

    assert res.status_code == 200
    assert ingest.calibration_commands[-1]["payload"]["offsets"] == [
        {"key": "Calibration.Device.ALTITUDE_METERS", "value": 1624.0}
    ]
    assert sensor_mgr.load("co2-test123")["Calibration"]["Device"]["ALTITUDE_METERS"] == 1624.0


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_sends_each_offset_as_own_command(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
        _HubSettingsWithAltitude("1783.00"),
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-ykdvea",
            },
            "Calibration": {
                "Device": {
                    "CO2_OFFSET": 0.0,
                    "ALTITUDE_METERS": 1783.0,
                }
            },
        },
    )
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "co2-ykdvea",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "CO2_OFFSET", "value": -100.0},
        ],
    }
    ingest.meta_patches_by_message["test-2"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "co2-ykdvea",
        "message_id": "test-2",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "ALTITUDE_METERS", "value": 1783.0},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "co2-ykdvea",
                "device_kind": "co2",
                "offsets": [
                    {"key": "Calibration.Device.CO2_OFFSET", "value": -100.0},
                    {"key": "Calibration.Device.ALTITUDE_METERS", "value": 9999.0, "force": True},
                ],
            },
        )

    assert res.status_code == 200
    assert len(ingest.calibration_commands) == 2
    assert all(len((cmd["payload"] or {}).get("offsets") or []) == 1 for cmd in ingest.calibration_commands)
    assert ingest.calibration_commands[0]["payload"]["offsets"] == [
        {"key": "Calibration.Device.CO2_OFFSET", "value": -100.0}
    ]
    assert ingest.calibration_commands[1]["payload"]["offsets"] == [
        {"key": "Calibration.Device.ALTITUDE_METERS", "value": 1783.0}
    ]
    body = res.json()
    assert body["shadow_synced"] is True
    assert body["applied"] == [
        "Calibration.Device.CO2_OFFSET",
        "Calibration.Device.ALTITUDE_METERS",
    ]
    saved = sensor_mgr.load("co2-ykdvea")
    assert saved["Calibration"]["Device"]["CO2_OFFSET"] == -100.0
    assert saved["Calibration"]["Device"]["ALTITUDE_METERS"] == 1783.0


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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "CO2_OFFSET", "value": -750.0},
        ],
    }
    ingest.meta_patches_by_message["test-2"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-2",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "CO2_OFFSET", "value": -250.0},
        ],
    }

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
    assert first.json()["shadow_synced"] is True
    assert second.json()["shadow_synced"] is True
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
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "apvpd-test123",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "RH_OFFSET", "value": 0.4},
        ],
    }

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
    assert res.json()["shadow_synced"] is True
    sent_offsets = ingest.calibration_commands[-1]["payload"]["offsets"]
    assert sent_offsets == [{"key": "Calibration.Device.RH_OFFSET", "value": 0.4}]
    saved = sensor_mgr.load("apvpd-test123")
    assert saved["Calibration"]["Device"]["RH_OFFSET"] == 0.4


@pytest.mark.asyncio
async def test_device_calibration_apply_for_remote_nodus_waits_for_meta_patch_before_shadow_update(tmp_path, monkeypatch):
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
    body = res.json()
    assert body["shadow_synced"] is False
    saved = sensor_mgr.load("apvpd-test123")
    assert "Calibration" not in saved


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
async def test_remote_soil_moisture_calibration_reopens_with_canonical_nodus_key(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    app.state.templates = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "ui_templates")))
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "soil-0xc4wu",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "soil",
                "SENSOR_ID": "soil-0xc4wu",
                "LOCATION": "Greenhouse",
            },
            "Calibration": {
                "Device": {
                    "SOIL_TEMP_CAL_VAL": 0.0,
                    "SOIL_TEMP_MOIST_VAL": 0.0,
                    "SOIL_PH_CAL_VAL": 3.0,
                    "SOIL_EC_CAL_VAL": 0.0,
                }
            },
        },
    )
    ingest.meta_patches_by_message["test-1"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "soil-0xc4wu",
        "message_id": "test-1",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "SOIL_MOIST_CAL_VAL", "value": 20.0},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        apply_res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "soil-0xc4wu",
                "device_kind": "soil",
                "offsets": [{"key": "soil_moisture_offset", "value": 20.0}],
            },
        )
        modal_res = await client.get(
            "/edit-sensor",
            params={"sensor_id": "soil-0xc4wu", "embed": "1"},
        )

    assert apply_res.status_code == 200
    assert apply_res.json()["shadow_synced"] is True
    saved = sensor_mgr.load("soil-0xc4wu")
    assert saved["Calibration"]["Device"]["SOIL_MOIST_CAL_VAL"] == 20.0
    assert saved["Calibration"]["Device"]["SOIL_TEMP_MOIST_VAL"] == 0.0
    assert modal_res.status_code == 200
    html = modal_res.text
    assert 'data-key="soil_moisture_offset"' in html
    assert 'value="20.0"' in html
    assert 'data-key="soil_ph_offset"' in html
    assert 'value="3.0"' in html


@pytest.mark.asyncio
async def test_remote_soil_moisture_calibration_filter_uses_canonical_nodus_key(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "soil-0xc4wu",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "soil",
                "SENSOR_ID": "soil-0xc4wu",
            },
            "Calibration": {
                "Device": {
                    "SOIL_TEMP_MOIST_VAL": 0.0,
                    "SOIL_MOIST_CAL_VAL": 20.0,
                }
            },
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/calibration/device/apply",
            json={
                "sensor_id": "soil-0xc4wu",
                "device_kind": "soil",
                "offsets": [{"key": "soil_moisture_offset", "value": 20.0}],
            },
        )

    assert res.status_code == 400
    assert res.json()["message"] == "No calibration changes detected."
    assert ingest.calibration_commands == []


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
    ingest.meta_patches_by_message["test-2"] = {
        "schema": "nodus-meta-patch/v1",
        "device_id": "soil-123",
        "message_id": "test-2",
        "source": "calibration_set",
        "updates": [
            {"section": "Calibration.Device", "key": "SOIL_PH_CAL_VAL", "value": 0.58},
        ],
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
    assert body["shadow_synced"] is True
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
async def test_calibrate_second_sensor_targets_physical_host_and_second_file(
    tmp_path,
    monkeypatch,
):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "lux-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "lux",
                "SENSOR_ID": "lux-test123",
            },
            "Nodus": {
                "DEVICE_ID": "aht-lux-test123",
                "CONFIG_FILE": "sensor_i2c_2.toml",
            },
        },
    )
    ingest.next_calibration_result = {
        "applied": True,
        "started": True,
        "status": {"status": "in_progress", "calibrated": False},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res = await client.post("/calibrate", params={"sensor_id": "lux-test123"})

    assert res.status_code == 200
    command = ingest.calibration_commands[-1]
    assert command["device_id"] == "aht-lux-test123"
    assert command["sensor_id"] == "lux-test123"
    assert command["name"] == "sensor_i2c_2.toml"


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
async def test_dashboard_include_extras_astro_payload_is_single_flight_and_nonblocking(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(
        tmp_path,
        monkeypatch,
        {
            "Astral": {
                "LATITUDE": "32.7900",
                "LONGITUDE": "-108.2749",
                "TIMEZONE": "America/Denver",
            },
            "Time": {"TZ": "America/Denver"},
        },
    )
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._ASTRO_PAYLOAD_CACHE = None

    build_calls = []

    class _FakeLocationInfo:
        def __init__(self, *args, **kwargs):
            self.observer = SimpleNamespace(elevation=0.0)

    class _FakeMoon:
        @staticmethod
        def phase(_day):
            return 5.0

        @staticmethod
        def azimuth(*_args, **_kwargs):
            return 180.0

        @staticmethod
        def elevation(*_args, **_kwargs):
            return 20.0

        @staticmethod
        def julianday_2000(_dt):
            return 0.0

        @staticmethod
        def moon_position(_jd):
            return SimpleNamespace(right_ascension=0.1, declination=0.2)

        @staticmethod
        def moonrise(_obs, date=None, tzinfo=None):
            return datetime.combine(date, datetime.min.time(), tzinfo=tzinfo) + timedelta(hours=7)

        @staticmethod
        def moonset(_obs, date=None, tzinfo=None):
            return datetime.combine(date, datetime.min.time(), tzinfo=tzinfo) + timedelta(hours=19)

    def _slow_sun(_obs, date=None, tzinfo=None):
        build_calls.append(time.monotonic())
        time.sleep(0.2)
        return {
            "sunrise": datetime.combine(date, datetime.min.time(), tzinfo=tzinfo) + timedelta(hours=6),
            "sunset": datetime.combine(date, datetime.min.time(), tzinfo=tzinfo) + timedelta(hours=18),
            "noon": datetime.combine(date, datetime.min.time(), tzinfo=tzinfo) + timedelta(hours=12),
        }

    monkeypatch.setattr(saiWebRoutes, "LocationInfo", _FakeLocationInfo)
    monkeypatch.setattr(saiWebRoutes, "_astral_sun", _slow_sun)
    monkeypatch.setattr(saiWebRoutes, "_astral_elevation", lambda *_a, **_k: 15.0)
    monkeypatch.setattr(saiWebRoutes, "_astral_azimuth", lambda *_a, **_k: 180.0)
    monkeypatch.setattr(saiWebRoutes, "_astral_lmst", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(saiWebRoutes, "_astral_moon", _FakeMoon())
    monkeypatch.setattr(saiWebRoutes, "get_skyfield_runtime_if_installed", lambda: None)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({}, {}))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {})

    started = time.monotonic()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res_one, res_two = await asyncio.gather(
            client.get("/", params={"json_only": "true", "include_extras": "true"}),
            client.get("/", params={"json_only": "true", "include_extras": "true"}),
        )
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.25)

    assert res_one.status_code == 200
    assert res_two.status_code == 200
    assert res_one.json()["astro"]["reason"] == "warming"
    assert res_two.json()["astro"]["reason"] == "warming"
    assert len(build_calls) == 1
    assert elapsed < 0.18


@pytest.mark.asyncio
async def test_dashboard_weewx_status_uses_recent_station_data_when_ingest_unknown(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    ingest.get_measure_status = lambda _sid: "unknown"

    now_iso = datetime.now().isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["weewx-station"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid == "weewx-station" else "")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda sid: {"Temperature_F": 72.1} if sid == "weewx-station" else {})
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {"weewx-station": {"Temperature_F": 72.1, "Wind Speed": 3.0}},
            {"weewx-station": now_iso},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"weewx-station": {}})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    assert res.json()["statuses"]["weewx-station"] == "online"


@pytest.mark.asyncio
async def test_dashboard_ecowitt_status_uses_ingest_service_state(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    ingest.get_measure_status = lambda _sid: "unknown"

    sensor_id = "ecowitt-e8db840f1543"
    now_iso = datetime.now().isoformat()
    app.state.ecowitt_service = SimpleNamespace(status=lambda: {
        "sensor_id": sensor_id,
        "state": "online",
    })
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [sensor_id])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid == sensor_id else "")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda sid: {"Temperature_F": 72.1} if sid == sensor_id else {})
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: ({sensor_id: {"Temperature_F": 72.1}}, {sensor_id: now_iso}),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {sensor_id: {}})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    assert res.json()["statuses"][sensor_id] == "online"


@pytest.mark.asyncio
async def test_dashboard_weewx_all_metric_mode_is_not_limited_to_station_defaults(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _AllMetricFakeSaiSettings)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "weewx-station",
        {
            "Sensor": {
                "TYPE": "weewx",
                "DEVICE": "weewx",
                "SENSOR_ID": "weewx-station",
                "LOCATION": "Weather Station",
            },
            "Display": {
                "METRIC_1": "Temperature_F",
                "METRIC_2": "Rel-Humidity",
                "METRIC_3": "Baro-Pressure",
                "METRIC_4": "Rain",
                "METRIC_5": "Wind Speed",
                "METRIC_6": "Wind Direction",
            },
        },
    )

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None
    now_iso = datetime.now().isoformat()
    station_values = {
        "Temperature_F": 72.1,
        "Rel-Humidity": 44.0,
        "Baro-Pressure": 1012.4,
        "Rain": 0.02,
        WEEWX_RAIN_24H_METRIC: 0.17,
        "Wind Speed": 3.0,
        "Wind Direction": 180.0,
        "Dew Point_F": 52.0,
        "Rain Rate": 0.01,
    }
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["weewx-station"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda sid: dict(station_values))
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: ({sid: dict(station_values) for sid in ids}, {sid: now_iso for sid in ids}),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: list(station_values.keys()))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"weewx-station": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["expected_gauge_map"]["weewx-station"] == [
        "Temperature_F",
        "Rel-Humidity",
        "Baro-Pressure",
        "Rain",
        "Wind Speed",
        "Wind Direction",
        "Dew Point_F",
        WEEWX_RAIN_24H_METRIC,
        "Rain Rate",
    ]
    style_map = body["expected_display_style_map"]["weewx-station"]
    assert style_map["METRIC_7"] == "Graph6hr"
    assert style_map["METRIC_8"] == "Graph6hr"
    assert style_map["METRIC_9"] == "Graph6hr"


@pytest.mark.asyncio
async def test_dashboard_global_pick6_keeps_summary_then_appends_known_metrics(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "weewx-station",
        {
            "Sensor": {
                "TYPE": "weewx",
                "DEVICE": "weewx",
                "SENSOR_ID": "weewx-station",
                "LOCATION": "Weather Station",
            },
            "Display": {
                "METRIC_1": "Temperature_F",
                "METRIC_2": "Rel-Humidity",
                "METRIC_3": "Rain",
                "METRIC_4": WEEWX_RAIN_24H_METRIC,
                "METRIC_5": "Wind Direction",
                "METRIC_6": "Baro-Pressure",
                "METRIC_DISPLAY_MODE": "All",
                "Style": {
                    "METRIC_1": "Graph24hr",
                    "METRIC_2": "Graph24hr",
                    "METRIC_3": "Gauge",
                    "METRIC_4": "Gauge",
                    "METRIC_5": "Gauge",
                    "METRIC_6": "Gauge",
                },
            },
        },
    )

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None
    now_iso = datetime.now().isoformat()
    station_values = {
        "Temperature_F": 72.1,
        "Rel-Humidity": 44.0,
        "Rain": 0.02,
        WEEWX_RAIN_24H_METRIC: 0.17,
        "Baro-Pressure": 1012.4,
        "Wind Speed": 3.0,
    }
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["weewx-station"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", lambda sid: dict(station_values))
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: ({sid: dict(station_values) for sid in ids}, {sid: now_iso for sid in ids}),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: list(station_values.keys()))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"weewx-station": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["expected_gauge_map"]["weewx-station"] == [
        "Temperature_F",
        "Rel-Humidity",
        "Rain",
        WEEWX_RAIN_24H_METRIC,
        "Wind Direction",
        "Baro-Pressure",
        "Wind Speed",
    ]
    assert body["expected_display_style_map"]["weewx-station"]["METRIC_5"] == "Gauge"


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
async def test_dashboard_json_sanitizes_sideways_sensor_and_keeps_other_values(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
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

    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-a", "aqi-b", "aqi-c"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {
                "aqi-a": {"Temperature": 72.0},
                "aqi-b": {"Temperature": math.nan},
                "aqi-c": {"Temperature": 74.0},
            },
            {sid: now_iso for sid in ids},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Temperature"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: (_ for _ in ()).throw(RuntimeError("stats down")))
    monkeypatch.setattr(saiWebRoutes.statter, "get_24hr_stats", lambda sid: {})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["values"]["aqi-a"]["Temperature"] == 72.0
    assert body["values"]["aqi-b"]["Temperature"] is None
    assert body["values"]["aqi-c"]["Temperature"] == 74.0
    assert body["stats"] == {"aqi-a": {}, "aqi-b": {}, "aqi-c": {}}


@pytest.mark.asyncio
async def test_dashboard_json_falls_back_per_sensor_when_bulk_latest_values_fail(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
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

    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    values_by_sid = {
        "aqi-a": {"Temperature": 72.0},
        "aqi-b": RuntimeError("sensor read failed"),
        "aqi-c": {"Temperature": 74.0},
    }

    def _latest_values(sid):
        item = values_by_sid[sid]
        if isinstance(item, Exception):
            raise item
        return dict(item)

    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-a", "aqi-b", "aqi-c"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (_ for _ in ()).throw(RuntimeError("bulk latest failed")),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values", _latest_values)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Temperature"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"aqi-a": {}, "aqi-b": {}, "aqi-c": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["values"]["aqi-a"]["Temperature"] == 72.0
    assert body["values"]["aqi-b"] == {}
    assert body["values"]["aqi-c"]["Temperature"] == 74.0


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
async def test_dashboard_merges_switch_cards_for_same_location(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._SENSOR_LOCATION_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    app.state.sensor_map = []
    saiWebRoutes.sensor_map = []
    saiWebRoutes.switch_controllers = {}

    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))

    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "aqi",
                "SENSOR_ID": "co2-ykdvea",
                "LOCATION": "OfficeDesk",
            },
            "Display": {"METRIC_1": "CO2"},
        },
    )
    switch_mgr.save(
        "switch-ykdvea",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": "switch-ykdvea",
                "SWITCH_LOCATION": "OfficeDesk",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-ykdvea",
                "SWITCH_1_ENABLE_PIN": "installed",
            },
        },
    )
    switch_mgr.save(
        "switch-zbcalz",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": "switch-zbcalz",
                "SWITCH_LOCATION": "OfficeDesk",
                "SWITCH_1_LABEL": "Pump",
                "SWITCH_1_CHANNEL_ID": "S1-zbcalz",
                "SWITCH_1_ENABLE_PIN": "installed",
            },
        },
    )

    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["co2-ykdvea"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid == "co2-ykdvea" else "")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {"co2-ykdvea": {"CO2": 1271.0}},
            {"co2-ykdvea": now_iso},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["CO2"])
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
            },
            {
                "switch_id": "switch-zbcalz",
                "switch_key": "S1-zbcalz::Pump",
                "channel_id": "S1-zbcalz",
                "label": "Pump",
                "location": "OfficeDesk",
            },
        ],
    )
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"co2-ykdvea": {}})

    ingest.mqtt_clients = ["co2-ykdvea"]
    ingest._switch_state_cache = {
        "switch-ykdvea": {"S1-ykdvea": "on"},
        "switch-zbcalz": {"S1-zbcalz": "off"},
    }
    ingest.nodus_switch_topic_map = {
        "nodus/S1-ykdvea/state": {
            "switch_id": "switch-ykdvea",
            "channel_id": "S1-ykdvea",
            "label": "Fan",
        },
        "nodus/S1-zbcalz/state": {
            "switch_id": "switch-zbcalz",
            "channel_id": "S1-zbcalz",
            "label": "Pump",
        },
    }
    ingest.device_location = {
        "nodus/S1-ykdvea/state": "OfficeDesk",
        "nodus/S1-zbcalz/state": "OfficeDesk",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/")

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store, max-age=0"
    assert res.headers["pragma"] == "no-cache"
    assert res.text.count("class='switch-metric-container'") == 1
    assert "data-switch-ids='switch-ykdvea,switch-zbcalz'" in res.text
    assert "<td>Fan " in res.text or "<td>Fan</td>" in res.text
    assert "<td>Pump " in res.text or "<td>Pump</td>" in res.text


@pytest.mark.asyncio
async def test_dashboard_json_ignores_stale_switch_settings_without_channels(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))

    sensor_mgr.save(
        "aqi-i2c-1-ff-sensorius",
        {
            "Sensor": {
                "TYPE": "local",
                "DEVICE": "aqi",
                "SENSOR_ID": "aqi-i2c-1-ff-sensorius",
                "LOCATION": "Unknown",
            },
            "Display": {"METRIC_1": "Air Quality"},
        },
    )
    switch_mgr.save(
        "switch-test123",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": "switch-test123",
                "SWITCH_LOCATION": "Unknown",
            },
        },
    )

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["aqi-i2c-1-ff-sensorius"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso if sid == "aqi-i2c-1-ff-sensorius" else "")
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {"aqi-i2c-1-ff-sensorius": {"Air Quality": 95.0}},
            {"aqi-i2c-1-ff-sensorius": now_iso},
        ),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: ["Air Quality"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(
        saiWebRoutes.statter,
        "get_all_stats_fast",
        lambda: {"aqi-i2c-1-ff-sensorius": {"Air Quality": {"min": 90.0, "avg": 95.0, "max": 100.0}}},
    )
    ingest.mqtt_clients = ["aqi-i2c-1-ff-sensorius"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        html_res = await client.get("/")
        json_res = await client.get("/?json_only=true")

    assert html_res.status_code == 200
    assert json_res.status_code == 200
    assert "class='switch-metric-container'" not in html_res.text
    payload = json_res.json()
    assert payload["available_switches"] == []
    assert payload["renderable_switches"] == []
    assert payload["renderable_switches_view"] == []


@pytest.mark.asyncio
async def test_dashboard_json_reports_switch_only_nodus_discovery_for_layout_refresh(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes.switch_controllers = {}
    app.state.switch_controllers = {}

    switch_id = "relay-standalone"
    channel_id = "S1-standalone"
    ingest.get_known_switch_devices = lambda: [switch_id]
    ingest.nodus_switch_topic_map = {
        f"nodus/{channel_id}/state": {
            "switch_id": switch_id,
            "channel_id": channel_id,
            "label": "Pump",
            "kind": "state",
        }
    }
    ingest._switch_state_cache = {switch_id: {channel_id: "off", "Pump": "off"}}
    ingest.device_location = {f"nodus/{channel_id}/state": "Shed"}

    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({}, {}))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/?json_only=true")

    assert res.status_code == 200
    body = res.json()
    assert channel_id in body["available_switches"]
    assert switch_id in body["renderable_switches"]
    assert switch_id in body["renderable_switches_view"]


@pytest.mark.asyncio
async def test_dashboard_json_reports_live_nodus_shadow_without_db_rows(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-pmoopn",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-pmoopn",
                "LOCATION": "Propagation Tent",
            },
            "Display": {"METRIC_1": "CO2", "METRIC_2": "Temperature"},
        },
    )

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._sensor_ids_cache_payload = None
    saiWebRoutes._sensor_ids_cache_until = 0.0
    ingest.nodus_liveness["co2-pmoopn"] = {
        "state": "degraded",
        "last_seen_s": 1.0,
        "last_report_s": None,
    }

    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda _sid: "")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({}, {}))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda _sid: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/?json_only=true")
        ids_res = await client.get("/sensor-ids")

    assert res.status_code == 200
    body = res.json()
    assert "co2-pmoopn" in body["available"]
    assert body["expected_gauge_map"]["co2-pmoopn"][:2] == ["CO2", "Temperature"]
    assert ids_res.status_code == 200
    assert "co2-pmoopn" in ids_res.json()


@pytest.mark.asyncio
async def test_dashboard_json_hides_retained_only_nodus_shadow_without_db_rows(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-stale",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "co2",
                "SENSOR_ID": "co2-stale",
                "LOCATION": "Propagation Tent",
            },
            "Display": {"METRIC_1": "CO2"},
        },
    )

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._sensor_ids_cache_payload = None
    saiWebRoutes._sensor_ids_cache_until = 0.0
    ingest.nodus_liveness["co2-stale"] = {
        "state": "unknown",
        "retained_seen_s": 1.0,
        "last_seen_s": None,
        "last_report_s": None,
    }

    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda _sid: "")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({}, {}))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda _sid: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/?json_only=true")

    assert res.status_code == 200
    assert "co2-stale" not in res.json()["available"]


@pytest.mark.asyncio
async def test_dashboard_json_ignores_incomplete_switch_identity_without_channel_id(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes.switch_controllers = {}
    app.state.switch_controllers = {}

    ingest.get_known_switch_devices = lambda: ["switch-test123"]
    ingest.nodus_switch_topic_map = {}
    ingest._switch_state_cache = {}

    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({}, {}))
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_switch_identities",
        lambda: [
            {
                "switch_id": "switch-test123",
                "switch_key": "switch-test123::Relay 1",
                "channel_id": "",
                "label": "Relay 1",
                "location": "Unknown",
            }
        ],
    )
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/?json_only=true")

    assert res.status_code == 200
    body = res.json()
    assert body["available_switches"] == []
    assert body["renderable_switches"] == []
    assert body["renderable_switches_view"] == []


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
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
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


def test_dashboard_gauge_init_preserves_configured_metric_display_style():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "data-display-style='Graph24hr'" in html
    assert "registerContainerStyle(container, 'Gauge')" not in html
    assert "const initialStyle = window.normalizeDisplayStyle(container.dataset.displayStyle" in html


def test_dashboard_dynamic_sensor_settings_gear_uses_bound_sensor_id():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "settingsLink.addEventListener('click'" in html
    assert 'onclick="window.editSensorSettings && window.editSensorSettings(sidLower)' not in html


def test_dashboard_biodynamic_calendar_card_has_calendar_button():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
        )
    )

    assert "Biodynamic Calendar</div>" in html
    assert "class='bio-open-btn' id='bioOpenBtn'" in html
    assert "aria-label='Open biodynamic calendar'" in html
    assert "title='View Calendar'" in html
    assert "<span class='bio-open-btn-label'>Calendar</span>" in html
    assert "<div class='bio-daylight' id='bioDaylightLine'>Hours of Daylight: --</div>" in html
    assert "daylightEl.textContent = buildDaylightText(data, cur.timestamp);" in html
    assert ".bio-open-btn{display:inline-flex;" in html
    assert "#bioBox{width:230px;box-sizing:border-box;overflow:hidden;align-items:stretch;}" in html
    assert "#bioBox .astro-card{width:100%;min-width:0;align-items:stretch;box-sizing:border-box;height:100%;}" in html
    assert "text-transform:uppercase" in html
    assert "window.openBiodynamicCalendar = function(){" in html
    assert "setBioOpenButtonLoading(true);" in html
    assert "window.requestAnimationFrame(function(){" in html
    assert "window.requestAnimationFrame(function(){ window.location.assign('/calendar'); });" in html
    assert "window.addEventListener('pageshow', function(){ setBioOpenButtonLoading(false); });" in html
    assert "fetch('/api/biodynamic-calendar-companion'" not in html
    assert "url.port = '8765'" not in html
    assert "bioOpenBtn.addEventListener('click'" in html
    assert "window.openBiodynamicCalendar) window.openBiodynamicCalendar();" in html
    assert "class='dashboard-content'" in html
    assert "class='dash-theme-trigger' id='dashboardThemeBtn'" in html
    assert "id='dashboardThemeView'" in html
    assert "data-dashboard-preview-theme='leaf-crop'" in html
    assert "function openDashboardThemeView(){" in html
    assert "function closeDashboardThemeView(){" in html


def test_dashboard_weather_forecast_card_has_caelus_button():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
            weather_forecast_theme="desert",
            dashboard_background_theme="flower",
        )
    )

    assert "<body class='dashboard-page dashboard-theme-flower'>" in html
    assert "24 Hour Forecast</div>" in html
    assert "<dt id='forecastPrecipLabel'>Rain</dt><dd id='forecastPrecipChance'>--</dd>" in html
    assert "class='astro-box forecast-scene-pending' id='weatherForecastBox'" in html
    assert "class='astro-box weather-theme-desert' id='weatherForecastBox'" not in html
    assert "background:var(--forecast-scene-bg)" in html
    assert "background:var(--forecast-button)" in html
    assert "forecast-scene-clear-day" in html
    assert "forecast-scene-clear-night" in html
    assert "forecast-scene-cloudy-day" in html
    assert "forecast-scene-cloudy-night" in html
    assert "forecast-scene-rain-day" in html
    assert "forecast-scene-rain-night" in html
    assert "--forecast-scene-bg:radial-gradient(circle at 82% 17%" in html
    assert "linear-gradient(165deg,#236b96 0%,#3a82a6 58%,#72aec2 100%)" in html
    assert "--forecast-panel:rgba(9,48,69,.82)" in html
    assert "--forecast-ink:#fff;--forecast-muted:#d9eff8" in html
    assert "--forecast-ink:#14252d;--forecast-muted:#31444d" in html
    assert "color:var(--forecast-button-ink)" in html
    assert "Array.isArray(data && data.hourly) ? data.hourly.slice(0, 3)" in html
    assert "precipitation > 0.05" in html
    assert "averageCloud >= 45" in html
    assert "function forecastStationMinutes(data){" in html
    assert "const daybreak = (sunrise - 30 + 1440) % 1440;" in html
    assert "const dusk = (sunset + 30) % 1440;" in html
    assert "isNight = !forecastMinuteInWindow(minutes, daybreak, dusk);" in html
    assert "applyForecastScene(window.__weatherForecastPayload); }, 60000);" in html
    assert "applyForecastScene(data);" in html
    assert "function forecastPrecipitationKind(data, cur){" in html
    assert "return /snow|sleet/.test(text) ? 'Snow' : 'Rain';" in html
    assert "setForecastPrecipitation(data, cur);" in html
    assert "`${Math.round(Math.max(0, Math.min(100, chance)))}% chance`" in html
    assert "Loading forecast..." not in html
    assert "class='forecast-open-btn' id='forecastFiveDayBtn'" in html
    assert "aria-label='Open Caelus weather forecast' title='Caelus Forecast'" in html
    assert "<span class='forecast-open-btn-label'>Caelus Forecast</span>" in html
    assert "<span class='spinner dashboard-card-spinner' aria-hidden='true'></span>" in html
    assert "/api/weather-forecast?days=1" in html
    assert "/api/weather-forecast?days=1&force_refresh=true" in html
    assert "window.location.assign('/weather-forecast')" in html
    assert "window.__weatherForecastProvider = weatherForecastProvider;" in html
    assert "function setWeatherForecastCardLoading(isLoading){" in html
    assert "setDashboardCardLoading('weatherForecastBox', isLoading);" in html
    assert "setWeatherForecastCardLoading(true);" in html
    assert "setWeatherForecastCardLoading(false);" in html
    assert "window.loadWeatherForecast = loadWeatherForecast;" in html
    assert "window.setInterval(function(){ loadWeatherForecast(false, true); }, 300000);" in html
    assert "if (!isBackgroundRefresh) renderWeatherForecast({ ok:false, reason:'forecast_failed' });" in html
    assert "forecastFiveDayBtn.addEventListener('click'" in html
    assert "window.location.assign('/weather-forecast')" in html


def test_dashboard_weather_forecast_none_hides_forecast_card():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
            weather_forecast_provider="none",
        )
    )

    assert "24 Hour Forecast</div>" not in html
    assert "id='weatherForecastBox'" not in html
    assert "const weatherForecastEnabled = false;" in html
    assert "if (weatherForecastEnabled && typeof loadWeatherForecast === 'function') {" in html
    assert "window.setInterval(function(){ loadWeatherForecast(false, true); }, 300000);" in html


def test_dashboard_refresh_pauses_during_modal_and_hidden_tab():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "function dashboardRefreshPaused() {" in html
    assert "window.ModalBusyCursor.isBusy && window.ModalBusyCursor.isBusy()" in html
    assert "const ignoreVisibility = !!(options && options.ignoreVisibility);" in html
    assert "const ignoreModal = !!(options && options.ignoreModal);" in html
    assert "document.visibilityState === 'hidden'" in html
    assert "return { begin, end, isBusy, untilPaint };" in html
    assert "dashboardRefreshPaused({ ignoreVisibility, ignoreModal })" in html
    assert "if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {" in html


def test_dashboard_micrograph_fetches_are_cached_and_throttled():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0, "Temperature": 24.1}},
            {
                "co2-ykdvea": {
                    "CO2": {"min": 700.0, "avg": 718.0, "max": 730.0},
                    "Temperature": {"min": 23.0, "avg": 24.1, "max": 25.0},
                }
            },
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2", "Temperature"]},
            expected_display_style_map={
                "co2-ykdvea": {"METRIC_1": "Graph24hr", "METRIC_2": "Graph6hr"}
            },
            display_style="Gauge",
        )
    )

    assert "window.__micrographDataCache = window.__micrographDataCache || new Map();" in html
    assert "window.__micrographDataInflight = window.__micrographDataInflight || new Map();" in html
    assert "window.__lastMicrographForceRefreshAt = window.__lastMicrographForceRefreshAt || 0;" in html
    assert "async function getMicrographJson(requestKey, url, force)" in html
    assert "const cacheTtlMs = 60000;" in html
    assert "const micrographCacheMaxEntries = 24;" in html
    assert "const micrographCacheMaxBytes = 8 * 1024 * 1024;" in html
    assert "function trimMicrographDataCache(now = Date.now())" in html
    assert "while (cache.size > micrographCacheMaxEntries || totalBytes > micrographCacheMaxBytes)" in html
    assert "const existing = window.__micrographDataInflight.get(requestKey);" in html
    assert "if (!force && (!existingAt || (now - existingAt) < micrographInflightStaleMs)) {" in html
    assert "const MIN_FORCE_INTERVAL_MS = 5000;" in html
    assert "container.dataset.metricClickAt = String(now);" in html
    assert "showMicrographForContainer(container, { force: true });" in html
    assert "showMicrographForContainer(container, { force, ignoreModal });" in html
    assert "window.__needsInitialMicrographRefresh = true;" in html
    assert "window.refreshAllMicrographs(true, { ignoreModal });" in html


def test_dashboard_initial_graph_styles_are_refreshed_by_configured_style():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"CO2": 718.0}},
            {"co2-ykdvea": {"CO2": {"min": 700.0, "avg": 718.0, "max": 730.0}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["CO2"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "data-display-style='Graph24hr'" in html
    assert "const desiredStyle = (typeof window.getContainerStyle === 'function')" in html
    assert "desiredStyle === 'Graph6hr' || desiredStyle === 'Graph24hr'" in html
    assert "if (graphVisible && !gaugeVisible)" not in html


def test_dashboard_metric_card_click_cycles_24hr_graph_to_6hr_graph():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"Ambient VPD": 1.95}},
            {"co2-ykdvea": {"Ambient VPD": {"min": 1.4, "avg": 2.1, "max": 2.8}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["Ambient VPD"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "if (style === 'Graph24hr')" in html
    assert "nextStyle = 'Graph6hr'" in html
    assert "else if (style === 'Graph6hr')" in html
    assert "nextStyle = 'Gauge'" in html


def test_dashboard_metric_card_reuses_chart_with_updated_graph_options():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"Ambient VPD": 1.95}},
            {"co2-ykdvea": {"Ambient VPD": {"min": 1.4, "avg": 2.1, "max": 2.8}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["Ambient VPD"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "xTitleText = '6 Hours'" in html
    assert "chart.options = chartOptions;" in html
    assert "chart.update('none');" in html


def test_dashboard_metric_card_gauge_view_has_canvas_fallback():
    from sensorius.saiHtml import get_gauge_config, render_dashboard

    ingest = SimpleNamespace(expected_gauge_map={})
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["co2-ykdvea"],
            {"co2-ykdvea": {"Ambient VPD": 1.95}},
            {"co2-ykdvea": {"Ambient VPD": {"min": 1.4, "avg": 2.1, "max": 2.8}}},
            ingest,
            gauge_config=gauge_config,
            expected_gauge_map={"co2-ykdvea": ["Ambient VPD"]},
            expected_display_style_map={"co2-ykdvea": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
        )
    )

    assert "function drawFallbackGauge(canvas, rawValue, config)" in html
    assert "if (typeof Gauge === 'function')" in html
    assert "drawFallbackGauge(canvas, gaugeValue ?? 0, config);" in html
    assert "const metricCanvasWidth = 260;" in html
    assert "canvas.width = Math.round(canvasSize.cssWidth);" in html
    assert "canvas.style.width = '160px';" not in html
    assert "radiusScale: 0.9" in html
    assert "radiusScale: 0.72" not in html
    assert "initGauge on view switch failed" in html


@pytest.mark.asyncio
async def test_dashboard_global_all_mode_uses_system_style_for_extra_metrics(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    monkeypatch.setattr(saiWebRoutes, "saiSettings", _AllMetricFakeSaiSettings)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "co2-ykdvea",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "co2", "SENSOR_ID": "co2-ykdvea", "LOCATION": "Office"},
            "Display": {
                "METRIC_1": "CO2",
                "METRIC_2": "Temperature",
                "METRIC_3": "Rel-Humidity",
                "METRIC_4": "Ambient VPD",
                "METRIC_5": "Dew Point Deficit",
                "METRIC_6": "DewVPD Risk",
                "Style": {
                    "METRIC_1": "Graph6hr",
                    "METRIC_2": "Gauge",
                    "METRIC_3": "Graph24hr",
                    "METRIC_4": "Gauge",
                    "METRIC_5": "Graph6hr",
                    "METRIC_6": "Gauge",
                },
            },
        },
    )
    saved_sensor_settings = sensor_mgr.load("co2-ykdvea")

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    saiWebRoutes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    all_values = {
        "CO2": 718.0,
        "Temperature": 25.1,
        "Rel-Humidity": 55.0,
        "Ambient VPD": 1.42,
        "Dew Point Deficit": 6.1,
        "DewVPD Risk": 21.0,
        "Gas": 1234.0,
        "Baro-Pressure": 1007.0,
    }
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["co2-ykdvea"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: ({sid: dict(all_values) for sid in ids}, {sid: now_iso for sid in ids}),
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_metrics", lambda sid: list(all_values.keys()))
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"co2-ykdvea": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["expected_gauge_map"]["co2-ykdvea"] == [
        "CO2",
        "Temperature",
        "Rel-Humidity",
        "Ambient VPD",
        "Dew Point Deficit",
        "DewVPD Risk",
        "Gas",
        "Baro-Pressure",
    ]
    style_map = body["expected_display_style_map"]["co2-ykdvea"]
    assert style_map["METRIC_1"] == "Graph6hr"
    assert style_map["METRIC_6"] == "Gauge"
    assert style_map["METRIC_7"] == "Graph6hr"
    assert style_map["METRIC_8"] == "Graph6hr"
    assert sensor_mgr.load("co2-ykdvea") == saved_sensor_settings
    assert ingest.published_json == []


@pytest.mark.asyncio
async def test_dashboard_display_metrics_prefer_sensor_settings_over_ingest_expected_map(tmp_path, monkeypatch):
    app, ingest, _system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_mgr.save(
        "apvpd-test123",
        {
            "Sensor": {
                "TYPE": "nodus",
                "DEVICE": "apvpd",
                "SENSOR_ID": "apvpd-test123",
                "LOCATION": "Grow Tent",
            },
            "Display": {
                "METRIC_1": "Ambient VPD",
                "METRIC_2": "Temperature",
                "METRIC_3": "Rel-Humidity",
                "METRIC_4": "Plant VPD",
                "METRIC_5": "Plant Temperature",
                "METRIC_6": "Plant Rel-Humidity",
            },
        },
    )

    ingest.expected_gauge_map["apvpd-test123"] = [
        "Temperature",
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Plant Temperature",
        "Plant Temperature_F",
    ]

    saiWebRoutes._DASHBOARD_JSON_CACHE.clear()
    saiWebRoutes._DASHBOARD_INVENTORY_CACHE = None
    now_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: ["apvpd-test123"])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_timestamp", lambda sid: now_iso)
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_latest_values_and_timestamps",
        lambda ids: (
            {
                sid: {
                    "Temperature": 25.45,
                    "Temperature_F": 77.8,
                    "Rel-Humidity": 30.49,
                    "Ambient VPD": 2.262,
                    "Plant Temperature": 25.21,
                    "Plant Temperature_F": 77.4,
                    "Plant Rel-Humidity": 24.75,
                    "Plant VPD": 2.414,
                }
                for sid in ids
            },
            {sid: now_iso for sid in ids},
        ),
    )
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_available_metrics",
        lambda sid: [
            "Temperature",
            "Temperature_F",
            "Rel-Humidity",
            "Ambient VPD",
            "Plant Temperature",
            "Plant Temperature_F",
            "Plant Rel-Humidity",
            "Plant VPD",
        ],
    )
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.statter, "get_all_stats_fast", lambda: {"apvpd-test123": {}})
    ingest.mqtt_clients = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/", params={"json_only": "true"})

    assert res.status_code == 200
    body = res.json()
    assert body["expected_gauge_map"]["apvpd-test123"] == [
        "Ambient VPD",
        "Temperature",
        "Rel-Humidity",
        "Plant VPD",
        "Plant Temperature",
        "Plant Rel-Humidity",
        "Temperature_F",
        "Plant Temperature_F",
    ]

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
    ingest.last_mqtt_seen = {"aqi-live": 100.0}
    ingest._host_ipv4addr = {"aqi-live": "10.0.0.55"}
    monkeypatch.setattr(saiWebRoutes.time, "time", lambda: 125.0)
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
    assert body["device_details"]["aqi-live"] == {
        "url": "http://10.0.0.55:8000",
        "last_seen_s": 25.0,
    }
    assert body["device_details"]["switch-db"]["url"] == "http://switch-db.local:8000"
    assert body["device_details"]["switch-db"]["last_seen_s"] is None


@pytest.mark.asyncio
async def test_remove_device_list_groups_dual_sensor_children_under_physical_nodus(
    tmp_path,
    monkeypatch,
):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(
        tmp_path,
        monkeypatch,
    )
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])

    for sensor_id, config_file in (
        ("aht-test123", "sensor_i2c.toml"),
        ("lux-test123", "sensor_i2c_2.toml"),
    ):
        sensor_mgr.save(
            sensor_id,
            {
                "Sensor": {
                    "TYPE": "nodus",
                    "DEVICE": sensor_id.split("-", 1)[0],
                    "SENSOR_ID": sensor_id,
                },
                "Nodus": {
                    "DEVICE_ID": "aht-lux-test123",
                    "CONFIG_FILE": config_file,
                },
            },
        )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.get("/remove-device-list")

    assert res.status_code == 200
    body = res.json()
    assert "aht-lux-test123" in body["devices"]
    assert "aht-test123" not in body["devices"]
    assert "lux-test123" not in body["devices"]
    assert body["device_details"]["aht-lux-test123"]["sensors"] == [
        "aht-test123",
        "lux-test123",
    ]


@pytest.mark.asyncio
async def test_remove_device_list_uses_last_packet_age_for_direct_i2c_sensor(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_id = "aqi-i2c-0-sensorius-0"
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)

    sensor_mgr.save(
        sensor_id,
        {
            "Sensor": {"TYPE": "pi", "DEVICE": "aqi", "SENSOR_ID": sensor_id, "LOCATION": "GH Desk"},
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    monkeypatch.setattr(saiWebRoutes.time, "time", lambda: 200.0)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(
        saiWebRoutes.data_logger,
        "get_sensor_last_packet_epoch",
        lambda sid, **_kwargs: 125.5 if sid == sensor_id else None,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.get("/remove-device-list")

    assert res.status_code == 200
    body = res.json()
    assert sensor_id in body["devices"]
    assert body["device_details"][sensor_id]["last_seen_s"] == 74.5


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
async def test_remove_device_expands_nodus_switch_suffix_cleanup(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiWebRoutes, "_SENSOR_BASE_DIR", str(sensor_root))
    monkeypatch.setattr(saiWebRoutes, "_SWITCH_BASE_DIR", str(switch_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])

    sensor_mgr.save(
        "avpd-zbcalz",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "avpd", "SENSOR_ID": "avpd-zbcalz", "LOCATION": "Room A"},
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    switch_mgr.save(
        "switch-zbcalz",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-zbcalz",
                "SWITCH_LOCATION": "Room A",
                "SWITCH_1_LABEL": "Pump",
                "SWITCH_1_CHANNEL_ID": "S1-zbcalz",
            },
        },
    )
    ingest._switch_state_cache["switch-zbcalz"] = {"S1-zbcalz": "off", "Pump": "off"}
    saiWebRoutes.switch_controllers = {
        "switch-zbcalz": SimpleNamespace(
            switch_id="switch-zbcalz",
            channel_id_for_label={"Pump": "S1-zbcalz"},
        )
    }
    app.state.switch_controllers = dict(saiWebRoutes.switch_controllers)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.post("/remove-device", json={"device_ids": ["switch-zbcalz"]})

    assert res.status_code == 200
    assert not (sensor_root / "avpd-zbcalz").exists()
    assert not (switch_root / "switch-zbcalz").exists()
    cleared_topics = {row["topic"] for row in ingest.client.published if row["payload"] == "" and row["retain"] is True}
    assert "nodus/avpd-zbcalz/meta" in cleared_topics
    assert "nodus/switch-zbcalz/meta" in cleared_topics
    assert "nodus/S1-zbcalz/state" in cleared_topics
    assert "avpd-zbcalz" in ingest._removed_nodus_ids
    assert "switch-zbcalz" in ingest._removed_nodus_ids
    assert "s1-zbcalz" in ingest._removed_nodus_ids
    assert "switch-zbcalz" not in ingest._switch_state_cache
    assert saiWebRoutes.switch_controllers == {}
    assert app.state.switch_controllers == {}


@pytest.mark.asyncio
async def test_remove_device_expands_nodus_sensor_suffix_cleanup(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiWebRoutes, "_SENSOR_BASE_DIR", str(sensor_root))
    monkeypatch.setattr(saiWebRoutes, "_SWITCH_BASE_DIR", str(switch_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])

    sensor_mgr.save(
        "aqi-x943fm",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "aqi", "SENSOR_ID": "aqi-x943fm", "LOCATION": "TestLab"},
            "Display": {"METRIC_1": "Air Quality"},
        },
    )
    switch_mgr.save(
        "switch-x943fm",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-x943fm",
                "SWITCH_LOCATION": "TestLab",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-x943fm",
                "SWITCH_2_LABEL": "Pump",
                "SWITCH_2_CHANNEL_ID": "S2-x943fm",
            },
        },
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.post("/remove-device", json={"device_ids": ["aqi-x943fm"]})

    assert res.status_code == 200
    assert not (sensor_root / "aqi-x943fm").exists()
    assert not (switch_root / "switch-x943fm").exists()
    cleared_topics = {row["topic"] for row in ingest.client.published if row["payload"] == "" and row["retain"] is True}
    assert "nodus/aqi-x943fm/meta" in cleared_topics
    assert "nodus/aqi-x943fm/meta/switch" in cleared_topics
    assert "nodus/aqi-x943fm/status/heartbeat" in cleared_topics
    assert "nodus/aqi-x943fm/event/calibration_status" in cleared_topics
    assert "nodus/switch-x943fm/meta" in cleared_topics
    assert "nodus/S1-x943fm/state" in cleared_topics
    assert "nodus/S1-x943fm/availability" in cleared_topics
    assert "nodus/S1-x943fm/config/ack" in cleared_topics
    assert "aqi-x943fm" in ingest._removed_nodus_ids
    assert "switch-x943fm" in ingest._removed_nodus_ids
    assert "s1-x943fm" in ingest._removed_nodus_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_ids", "cleanup_target"),
    [
        (["aht-1jm5s1", "switch-1jm5s1"], "aht-1jm5s1"),
        (["switch-1jm5s1", "aht-1jm5s1"], "switch-1jm5s1"),
    ],
)
async def test_remove_device_groups_selected_sensor_and_switch_and_suppresses_replay(
    tmp_path,
    monkeypatch,
    selected_ids,
    cleanup_target,
):
    app, ingest, system_root, sensor_root, switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiWebRoutes, "_SENSOR_BASE_DIR", str(sensor_root))
    monkeypatch.setattr(saiWebRoutes, "_SWITCH_BASE_DIR", str(switch_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_available_sensors", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])

    sensor_mgr.save(
        "aht-1jm5s1",
        {
            "Sensor": {"TYPE": "nodus", "DEVICE": "aht", "SENSOR_ID": "aht-1jm5s1", "LOCATION": "Desklab"},
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    switch_mgr.save(
        "switch-1jm5s1",
        {
            "Switch": {
                "TYPE": "nodus",
                "DEVICE": "switch",
                "SWITCH_DEVICE_ID": "switch-1jm5s1",
                "SWITCH_LOCATION": "Desklab",
                "SWITCH_1_LABEL": "Fan",
                "SWITCH_1_CHANNEL_ID": "S1-1jm5s1",
            },
        },
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.post(
            "/remove-device",
            json={"device_ids": selected_ids},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert not (sensor_root / "aht-1jm5s1").exists()
    assert not (switch_root / "switch-1jm5s1").exists()
    assert "aht-1jm5s1" in ingest._removed_nodus_ids
    assert "switch-1jm5s1" in ingest._removed_nodus_ids
    assert body["results"]["aht-1jm5s1"] == body["results"]["switch-1jm5s1"]
    assert body["results"]["aht-1jm5s1"]["cleanup_targets"] == [cleanup_target]
    assert body["results"]["aht-1jm5s1"]["remaining_ids"] == []


@pytest.mark.asyncio
async def test_remove_direct_i2c_sensor_purges_active_database_rows(tmp_path, monkeypatch):
    app, ingest, system_root, sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    sensor_mgr = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sensor_id = "avpd-i2c-0-sensorius-0"
    monkeypatch.setenv("SAI_WEB_API_KEY", "test-key")
    monkeypatch.setattr(saiWebRoutes, "_SYS_BASE_DIR", str(system_root))
    monkeypatch.setattr(saiWebRoutes, "_SENSOR_BASE_DIR", str(sensor_root))
    monkeypatch.setattr(saiMQTTIngest, "get_current_ingest", lambda: ingest)

    sensor_mgr.save(
        sensor_id,
        {
            "Sensor": {
                "TYPE": "pi",
                "DEVICE": "avpd",
                "SENSOR_ID": sensor_id,
                "LOCATION": "Bench",
            },
            "Display": {"METRIC_1": "Temperature"},
        },
    )
    monkeypatch.setattr(saiDataLoggerModule.saiDataLogger, "_schema_ready", False)
    logger = saiDataLoggerModule.saiDataLogger(str(tmp_path / "active-sensorius.db"))
    monkeypatch.setattr(saiWebRoutes, "data_logger", logger)
    app.state.data_logger = logger
    logger.log_readings("2026-06-27T16:00:00-06:00", sensor_id, {"Temperature": 24.0})
    logger.log_sensor_event(
        sensor_id,
        saiDataLoggerModule.SENSOR_EVENT_TYPE_LIVENESS,
        state="offline",
        timestamp="2026-06-27T16:01:00-06:00",
        source="test",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "test-key"},
    ) as client:
        res = await client.post("/remove-device", json={"device_ids": [sensor_id]})

    assert res.status_code == 200
    body = res.json()
    assert body["results"][sensor_id]["db"]["rows_deleted"] == 2
    assert not (sensor_root / sensor_id).exists()
    assert logger.get_available_sensors() == []
    with sqlite3.connect(logger.db_path) as conn:
        readings = conn.execute("SELECT COUNT(*) FROM readings WHERE sensor_id = ?", (sensor_id,)).fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM sensor_events WHERE sensor_id = ?", (sensor_id,)).fetchone()[0]
    assert readings == 0
    assert events == 0
    logger.close()


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
async def test_switch_toggle_channel_fallback_returns_requested_state(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._switch_status_cache_payload = None
    saiWebRoutes._switch_status_cache_until = 0.0
    saiWebRoutes.switch_controllers = {}
    app.state.switch_controllers = {}

    ingest._switch_state_cache = {"switch-ykdvea": {"S1-ykdvea": "on", "Fan": "on"}}
    ingest.nodus_liveness["switch-ykdvea"] = {"state": "online"}
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
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_switch_state", lambda _switch_key: None)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"referer": "http://test/"},
    ) as client:
        res = await client.post("/switch/toggle?switch_name=Fan&switch_id=S1-ykdvea")

    assert res.status_code == 200
    body = res.json()
    assert ingest.switch_commands[-1]["switch_id"] == "switch-ykdvea"
    assert ingest.switch_commands[-1]["channel_id"] == "S1-ykdvea"
    assert ingest.switch_commands[-1]["new_state"] is False
    assert body["state"] is False


@pytest.mark.asyncio
async def test_switch_toggle_remote_controller_prefers_live_state_over_stale_db(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    saiWebRoutes._switch_status_cache_payload = None
    saiWebRoutes._switch_status_cache_until = 0.0

    class _RemoteCtrl:
        switch_id = "switch-ykdvea"
        location = "OfficeDesk"
        switch_topics = {"Fan": "nodus/S1-ykdvea/config/set"}
        channel_id_for_label = {"Fan": "S1-ykdvea"}

        def __init__(self):
            self.last_state = {"Fan": True}
            self.last_set_time = {"Fan": 0.0}

        def get_switch_names(self):
            return ["Fan"]

        def get_state(self, label):
            return bool(self.last_state[label])

        def sync_manual_toggle_result(self, label, is_on, *, previous_state):
            self.last_state[label] = bool(is_on)

        def _switch_key(self, label):
            return f"S1-ykdvea::{label}"

        def get_auto_off_status(self, label):
            return {
                "timer_seconds": 0,
                "timer_enabled": False,
                "timer_deadline_epoch": None,
                "timer_remaining_s": 0,
            }

    ctrl = _RemoteCtrl()
    saiWebRoutes.switch_controllers = {"switch-ykdvea": ctrl}
    app.state.switch_controllers = dict(saiWebRoutes.switch_controllers)
    ingest.nodus_liveness["switch-ykdvea"] = {"state": "online"}
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_latest_switch_state", lambda _switch_key: "Off")
    monkeypatch.setattr(saiWebRoutes.data_logger, "get_last_switch_events", lambda *_a, **_k: [])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"referer": "http://test/"},
    ) as client:
        res = await client.post("/switch/toggle?switch_name=Fan&switch_id=switch-ykdvea")

    assert res.status_code == 200
    body = res.json()
    assert ingest.switch_commands[-1]["switch_id"] == "switch-ykdvea"
    assert ingest.switch_commands[-1]["channel_label"] == "Fan"
    assert ingest.switch_commands[-1]["new_state"] is False
    assert body["state"] is False
    assert ctrl.last_state["Fan"] is False


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

        def get_legacy_rule_ids(self, _switch_id):
            return {"legacy-local-rule"}

    monkeypatch.setattr("sensorius.saiAutomationManager.AutomationManager", _FakeAutomationManager)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/advanced/automations?switch_id=desk-hub")

    assert res.status_code == 200
    body = res.json()
    assert body["switch_id"] == "desk-hub"
    assert [item["rule_id"] for item in body["items"]] == ["desk-fan-rule", "legacy-local-rule"]
    assert [item["legacy"] for item in body["items"]] == [False, True]


@pytest.mark.asyncio
async def test_system_automation_save_reloads_action_target_controller(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, switch_root = await _build_app(
        tmp_path, monkeypatch
    )

    import sensorius.saiAutomationManager as automation_module

    real_automation_manager = automation_module.AutomationManager

    class _TmpAutomationManager(real_automation_manager):
        def __init__(self, _base_dir="switch_settings"):
            super().__init__(str(switch_root))

    monkeypatch.setattr(
        automation_module, "AutomationManager", _TmpAutomationManager
    )

    switch_mgr = _REAL_SWITCH_SETTINGS_MANAGER(str(switch_root))
    switch_mgr.save(
        "switch-8y47n1",
        {
            "Switch": {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": "switch-8y47n1",
                "SWITCH_LOCATION": "Greenhouse",
                "SWITCH_1_LABEL": "Water",
                "SWITCH_1_CHANNEL_ID": "S1-8y47n1",
            },
        },
    )

    class _TargetController:
        switch_id = "switch-8y47n1"
        sensor = None

        def __init__(self):
            self.values = {"soil-8y47n1": {"Soil Moisture": 17.0}}
            self.override_script = {"Water": True}
            self._rules_cache = {"mtime": 123.0, "enabled": False}
            self.evaluations = []

        def _evaluate_and_apply_advanced(self, current_values_map):
            self.evaluations.append(current_values_map)

    target_ctrl = _TargetController()
    controllers = {"switch-8y47n1": target_ctrl}
    saiWebRoutes.switch_controllers = controllers
    app.state.switch_controllers = controllers
    app.state.supervisor = SimpleNamespace(
        _task_names={"switch-8y47n1 Controladora Monitor"}
    )

    payload = {
        "switch_id": "__system__",
        "rule_id": "water-greenhouse-on",
        "enabled": "true",
        "script_json": (
            '{"name":"Water Greenhouse On","enabled":true,'
            '"conditions":[{"type":"sensor","sensor":"soil-8y47n1",'
            '"metric":"Soil Moisture","op":"<","value":20,"hyst":1}],'
            '"actions":[{"type":"switch","switch_key":"switch-8y47n1::Water",'
            '"set":true,"revert_action":"previous_state","delay_s":0}]}'
        ),
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/submit-advanced-trigger", json=payload)

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert target_ctrl._rules_cache["mtime"] is None
    assert target_ctrl.evaluations == [
        {"soil-8y47n1": {"Soil Moisture": 17.0}}
    ]

    saved = _TmpAutomationManager().load("__system__")
    script = saved["Advanced"]["water-greenhouse-on"]["script_json"]
    assert '"switch_key":"switch-8y47n1::Water"' in script


@pytest.mark.asyncio
async def test_bd_none_automation_round_trip_preserves_executor(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, switch_root = await _build_app(
        tmp_path, monkeypatch
    )

    import sensorius.saiAutomationManager as automation_module

    real_automation_manager = automation_module.AutomationManager

    class _TmpAutomationManager(real_automation_manager):
        def __init__(self, _base_dir="switch_settings"):
            super().__init__(str(switch_root))

    monkeypatch.setattr(
        automation_module, "AutomationManager", _TmpAutomationManager
    )

    payload = {
        "switch_id": "sensoria-hub-0",
        "rule_id": "bd-none",
        "enabled": "true",
        "script_json": (
            '{"name":"BD Transitions","enabled":true,'
            '"conditions":[{"type":"bd_transitions",'
            '"executor_switch_id":"sensoria-hub-0"}],'
            '"actions":[{"type":"none",'
            '"executor_switch_id":"sensoria-hub-0"}]}'
        ),
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/submit-advanced-trigger", json=payload)

    assert res.status_code == 200
    assert res.json()["ok"] is True
    saved = _TmpAutomationManager().load("sensoria-hub-0")
    script = json.loads(saved["Advanced"]["bd-none"]["script_json"])
    assert script["conditions"] == [{
        "type": "bd_transitions",
        "sensor": "",
        "metric": "",
        "op": ">",
        "value": None,
        "hyst": None,
        "start": "",
        "end": "",
        "astral_event": "sunrise",
        "offset_min": 0,
        "days": None,
        "duration_min": None,
        "freq_hours": None,
        "period_min": None,
        "anchor_epoch": None,
        "executor_switch_id": "sensoria-hub-0",
    }]
    assert script["actions"] == [{
        "type": "none",
        "executor_switch_id": "sensoria-hub-0",
    }]


@pytest.mark.asyncio
async def test_nodus_wifi_inventory_reports_only_live_devices_as_eligible(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.mqtt_clients = ["apvpd-live123", "co2-offline123"]
    ingest.nodus_liveness = {
        "apvpd-live123": {"state": "online", "last_seen_s": 1.5},
        "co2-offline123": {"state": "offline", "last_seen_s": 95.0},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.get("/api/nodus-wifi/devices")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    payload = response.json()
    assert payload["eligible_count"] == 1
    by_id = {row["device_id"]: row for row in payload["devices"]}
    assert by_id["apvpd-live123"]["eligible"] is True
    assert by_id["co2-offline123"]["eligible"] is False
    assert by_id["co2-offline123"]["reason"] == "device is offline"


@pytest.mark.asyncio
async def test_nodus_wifi_inventory_excludes_retained_only_removed_switch(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.mqtt_clients = ["co2-live123"]
    ingest.nodus_liveness = {"co2-live123": {"state": "online", "last_seen_s": 1.5}}
    ingest.device_status["switch-stale1"] = "offline"
    ingest.nodus_firmware_versions["switch-stale1"] = "v0.26.180.1"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.get("/api/nodus-wifi/devices")

    assert response.status_code == 200
    assert [row["device_id"] for row in response.json()["devices"]] == ["co2-live123"]


@pytest.mark.asyncio
async def test_nodus_wifi_current_credentials_are_transient_and_not_cacheable(tmp_path, monkeypatch):
    app, _ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        saiAddDevice,
        "resolve_pi_wifi_credentials",
        lambda: ("Current Network", "current-secret"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.get("/api/nodus-wifi/current")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == {
        "ok": True,
        "ssid": "Current Network",
        "password": "current-secret",
        "password_available": True,
    }


@pytest.mark.asyncio
async def test_nodus_wifi_update_stages_all_devices_before_restart(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.mqtt_clients = ["apvpd-live123", "co2-live456"]
    ingest.nodus_liveness = {
        "apvpd-live123": {"state": "online"},
        "co2-live456": {"state": "online"},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.post(
            "/api/nodus-wifi/update",
            json={
                "ssid": "Replacement Network",
                "password": "replacement-secret",
                "device_ids": ["apvpd-live123", "co2-live456"],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["staged"] == 2
    assert payload["restarting"] == 2
    assert "replacement-secret" not in response.text
    assert len(ingest.published_json) == 6
    assert [row["payload"]["restart"] for row in ingest.published_json] == [False, False, False, False, True, True]
    for row in ingest.published_json[:4]:
        assert row["retain"] is False
        assert len(row["payload"]["payload"]["updates"]) == 1
    updates_by_device = {}
    for row in ingest.published_json[:4]:
        updates_by_device.setdefault(row["topic"], []).append(row["payload"]["payload"]["updates"][0])
    assert updates_by_device == {
        "nodus/apvpd-live123/config/set": [
            {"section": "Network", "key": "SSID", "value": "Replacement Network"},
            {"section": "Network", "key": "PASSWORD", "value": "replacement-secret"},
        ],
        "nodus/co2-live456/config/set": [
            {"section": "Network", "key": "SSID", "value": "Replacement Network"},
            {"section": "Network", "key": "PASSWORD", "value": "replacement-secret"},
        ],
    }


@pytest.mark.asyncio
async def test_nodus_wifi_update_restarts_only_successfully_staged_devices(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.mqtt_clients = ["apvpd-live123", "co2-live456"]
    ingest.nodus_liveness = {
        "apvpd-live123": {"state": "online"},
        "co2-live456": {"state": "online"},
    }

    async def _result_for_message(message_id: str, timeout: float = 0):
        if message_id == "cfg-3":
            return {"message_id": message_id, "applied": False, "error": "write_failed"}
        return {"message_id": message_id, "applied": True, "updated": 2, "error": ""}

    ingest.wait_for_config_result = _result_for_message

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.post(
            "/api/nodus-wifi/update",
            json={
                "ssid": "Replacement Network",
                "password": "replacement-secret",
                "device_ids": ["apvpd-live123", "co2-live456"],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["staged"] == 1
    assert payload["restarting"] == 1
    assert [row["payload"]["restart"] for row in ingest.published_json] == [False, False, False, True]
    config_topics = [row["topic"] for row in ingest.published_json if row["payload"]["restart"] is False]
    assert config_topics.count("nodus/apvpd-live123/config/set") == 2
    assert config_topics.count("nodus/co2-live456/config/set") == 1
    by_id = {row["device_id"]: row for row in payload["results"]}
    assert by_id["apvpd-live123"]["status"] == "restarting"
    assert by_id["co2-live456"]["status"] == "failed"


@pytest.mark.asyncio
async def test_nodus_wifi_update_rejects_invalid_credentials_without_publish(tmp_path, monkeypatch):
    app, ingest, _system_root, _sensor_root, _switch_root = await _build_app(tmp_path, monkeypatch)
    ingest.mqtt_clients = ["apvpd-live123"]
    ingest.nodus_liveness = {"apvpd-live123": {"state": "online"}}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}
    ) as client:
        response = await client.post(
            "/api/nodus-wifi/update",
            json={"ssid": "Replacement Network", "password": "short", "device_ids": ["apvpd-live123"]},
        )

    assert response.status_code == 400
    assert ingest.published_json == []
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_nodus_wifi_update_ui_is_transient_and_revealable():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    template = source.read_text(encoding="utf-8")

    assert 'data-target="pane-wifi">Wi-Fi Settings</button>' not in template
    assert '<summary>Nodus Wifi Update</summary>' in template
    assert '<summary>Network Settings</summary>' in template
    expected_order = (
        'data-runtime-section="system-astral"',
        'data-runtime-section="system-display"',
        'data-runtime-section="system-general"',
        'data-runtime-section="system-wifi"',
    )
    assert [template.index(item) for item in expected_order] == sorted(
        template.index(item) for item in expected_order
    )
    assert 'id="nodus-wifi-ssid" value="" autocomplete="off"' in template
    assert 'type="password" id="nodus-wifi-password" value="" autocomplete="new-password"' in template
    assert 'id="nodus-wifi-password-toggle">Show</button>' in template
    assert "Stage replacement Wi-Fi credentials on every currently connected Nodus." not in template
    assert '<div class="integration-state-title nodus-wifi-device-title">' in template
    assert '<span>Nodus Devices</span>' in template
    assert "Physical Nodus devices" not in template
    assert 'updateButton.disabled = nodusWifiRequestActive || !hasEligibleDevice || !ssid || !password' in template
    assert 'ev.target.id === "nodus-wifi-ssid" || ev.target.id === "nodus-wifi-password"' in template
    assert 'clearNodusWifiCredentials();' in template
    assert 'fetch("/api/nodus-wifi/current"' in template
    assert 'fetch("/api/nodus-wifi/update"' in template
    assert 'statePanel.classList.remove("ok", "fail")' in template
    assert 'if (loadFailed || !rows.length || !healthy.length) statePanel.classList.add("fail")' in template
    assert 'else if (healthy.length === rows.length) statePanel.classList.add("ok")' in template
    assert 'renderNodusWifiDevices([], true)' in template
    assert 'loadNodusWifiDevices({silent: true})' in template
    assert 'if (!nodusWifiRequestActive) await loadNodusWifiDevices({silent: true})' in template
    assert '}, 5000);' in template
    assert 'if (silent) return nodusWifiDevices' in template
    assert 'stopNodusWifiAutoRefresh();' in template
    assert 'id="nodus-wifi-confirm-dialog"' in template
    assert 'id="nodus-wifi-confirm-accept">Update Devices</button>' in template
    assert 'await confirmNodusWifiUpdate(ssid, targets)' in template
    assert 'Stage Wi-Fi network “${ssid}” on ${targets.length}' in template
    assert 'const confirmed = window.confirm(' not in template[template.index('async function updateNodusWifiCredentials()'):]


def test_nodus_config_debug_summary_redacts_password_values():
    ingest = object.__new__(saiMQTTIngest.saiMQTTIngest)
    summary = ingest._summarize_nodus_config_payload(
        {
            "payload": {
                "updates": [
                    {"section": "Network", "key": "SSID", "value": "Replacement Network"},
                    {"section": "Network", "key": "PASSWORD", "value": "replacement-secret"},
                ]
            }
        }
    )

    assert "Replacement Network" in summary
    assert "replacement-secret" not in summary
    assert "<redacted>" in summary


def test_wifi_credential_helpers_do_not_log_plaintext_passwords():
    source = Path(__file__).resolve().parents[1] / "sensorius" / "saiAddDevice.py"
    module_text = source.read_text(encoding="utf-8")

    assert 'psk: {psk}' not in module_text
    assert '/itaot-init payload={raw_payload' not in module_text
