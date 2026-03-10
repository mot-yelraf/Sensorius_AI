from __future__ import annotations

import os
import sys
import time
import subprocess

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiWebRoutes
from saiOnboardingStore import OnboardingSessionStore
from saiOnboardingToken import OnboardingTokenManager


class _DummyFastStats:
    def __init__(self, *_args, **_kwargs):
        pass

    async def start(self):
        return

    def stop(self):
        return


class _FakeSettings:
    def __init__(self):
        self._vals = {
            ("SensorNetwork", "BROKER"): "sensorius-broker.local",
            ("MQTT", "PORT"): 1883,
            ("MQTT", "USERNAME"): "",
            ("MQTT", "PASSWORD"): "",
            ("MQTT", "USE_TLS"): False,
            ("Onboarding", "HELLO_TIMEOUT_SEC"): 30,
            ("Onboarding", "ACK_TIMEOUT_SEC"): 5,
            ("Onboarding", "RESULT_TIMEOUT_SEC"): 20,
            ("Onboarding", "CONFIG_SET_MAX_ATTEMPTS"): 2,
            ("Onboarding", "CONFIG_SET_BACKOFF_MS"): 50,
        }

    def get_all_sensor_ids(self):
        return []

    def get_setting(self, section, key, default=None):
        return self._vals.get((section, key), default)


class _FakeIngest:
    def __init__(self):
        self.handler = None
        self.published: list[tuple[str, dict]] = []

    def set_onboarding_event_handler(self, handler):
        self.handler = handler

    def publish_json(self, topic: str, obj: dict, qos: int = 0, retain: bool = False, use_ha_client: bool = True):
        self.published.append((topic, dict(obj)))
        return True


class _FakeNetMgr:
    pass


class _FakeGcMgr:
    pass


@pytest.fixture(autouse=True)
def _default_platform_linux(monkeypatch):
    monkeypatch.setattr(saiWebRoutes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        "saiAddDevice.get_itaot_meta",
        lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-123"}, "error": ""},
    )
    monkeypatch.setattr(saiWebRoutes.subprocess, "run", lambda *a, **k: _cp(stdout="10.0.0.246"))


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_scan_nodus_setup_marks_macos_miss_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("saiAddDevice.PICOW_AP_SSID", "Nodus_Setup")
    monkeypatch.setattr("saiAddDevice.PICOW_AP_PASSWORD", "password")
    monkeypatch.setattr("saiAddDevice._get_current_ssid", lambda: "ExampleWiFi")

    async def _fake_to_thread(func, *args, **kwargs):
        if not args and not kwargs:
            return func(*args, **kwargs)
        return False, "ok"

    monkeypatch.setattr(saiWebRoutes.asyncio, "to_thread", _fake_to_thread)

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/scan-nodus-setup")
        assert res.status_code == 200
        body = res.json()
        assert body.get("found") is False
        assert body.get("platform") == "Darwin"
        assert body.get("password") == "password"
        assert body.get("current_ssid") == "ExampleWiFi"
        assert body.get("manual_join_required") is True
        assert "Other Networks" in body.get("message", "")


@pytest.mark.asyncio
async def test_v2_start_and_session_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    orig_issue = OnboardingTokenManager.issue_token

    def _issue_known(self, *, session_id: str, expected_device_id: str = "", ttl_sec=None):
        out = orig_issue(self, session_id=session_id, expected_device_id=expected_device_id, ttl_sec=ttl_sec)
        out["token"] = "token-known"
        return out

    monkeypatch.setattr(OnboardingTokenManager, "issue_token", _issue_known)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-test-1"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (True, ssid))
    monkeypatch.setattr(saiWebRoutes.subprocess, "run", lambda *a, **k: _cp(stdout="10.0.0.246 192.168.4.1"))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-test-1"})
        assert res.status_code == 200
        body = res.json()
        assert body.get("ok") is True
        sid = body.get("session_id")
        assert sid

        sess = await client.get(f"/onboard-device/v2/session/{sid}")
        assert sess.status_code == 200
        sess_doc = sess.json()
        assert sess_doc.get("state") == "WAITING_MQTT_HELLO"
        assert sess_doc.get("local_ssid") == "MyWiFi"

        retry = await client.post(f"/onboard-device/v2/retry/{sid}")
        assert retry.status_code == 200
        assert retry.json().get("ok") is True


@pytest.mark.asyncio
async def test_v2_start_resolves_local_wifi_before_ap_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    calls: list[str] = []

    def _resolve():
        calls.append("resolve")
        return "MyWiFi", "my-password"

    def _connect(*_args, **_kwargs):
        calls.append("connect")
        return True

    def _reconnect(ssid: str, password: str = "", **_kwargs):
        calls.append(f"reconnect:{ssid}")
        return True, ssid

    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", _resolve)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", _connect)
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", _reconnect)
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-test-order"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})
    monkeypatch.setattr(saiWebRoutes.subprocess, "run", lambda *a, **k: _cp(stdout="10.0.0.246"))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-test-order"})
        assert res.status_code == 200

    assert calls[:3] == ["resolve", "connect", "reconnect:MyWiFi"]


@pytest.mark.asyncio
async def test_v2_start_prefers_itaot_meta_device_id_and_hub_mdns_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.socket, "gethostname", lambda: "sensoria-hub-0")
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-meta-123"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (True, ssid))
    init_payloads: list[dict] = []

    def _post(payload, **_kwargs):
        init_payloads.append(dict(payload))
        return {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""}

    monkeypatch.setattr("saiAddDevice.post_itaot_init", _post)

    app = FastAPI()
    settings = _FakeSettings()
    settings._vals[("SensorNetwork", "BROKER")] = "localhost"
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "sensoria-hub-0"})
        assert res.status_code == 200

    assert init_payloads
    assert init_payloads[0]["hostname"] == "aqi-meta-123"
    assert init_payloads[0]["mqtt"]["broker_host"] == "sensoria-hub-0.local"


@pytest.mark.asyncio
async def test_v2_start_rewrites_ip_broker_to_hub_mdns(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.socket, "gethostname", lambda: "sensoria-hub-0")
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-meta-123"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (True, ssid))

    init_payloads: list[dict] = []

    def _post(payload, **_kwargs):
        init_payloads.append(dict(payload))
        return {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""}

    monkeypatch.setattr("saiAddDevice.post_itaot_init", _post)

    app = FastAPI()
    settings = _FakeSettings()
    settings._vals[("SensorNetwork", "BROKER")] = "192.168.4.17"
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "sensoria-hub-0"})
        assert res.status_code == 200

    assert init_payloads
    assert init_payloads[0]["mqtt"]["broker_host"] == "sensoria-hub-0.local"


@pytest.mark.asyncio
async def test_v2_start_on_macos_requires_manual_join_when_not_on_target_ap(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "pw"))
    monkeypatch.setattr("saiAddDevice._get_current_ssid", lambda: "ExampleWiFi")
    monkeypatch.setattr("saiAddDevice.PICOW_AP_SSID", "Nodus_Setup")
    monkeypatch.setattr("saiAddDevice.PICOW_AP_PASSWORD", "password")

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-manual-join"})
        assert res.status_code == 400
        body = res.json()
        assert body.get("error") == "manual_join_required"
        assert "Nodus_Setup" in body.get("detail", "")


@pytest.mark.asyncio
async def test_v2_start_on_macos_uses_existing_manual_join(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "pw"))
    monkeypatch.setattr("saiAddDevice._get_current_ssid", lambda: "Nodus_Setup")
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not connect on macOS")))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-manual-join-ok"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (True, ssid))
    monkeypatch.setattr(saiWebRoutes.subprocess, "run", lambda *a, **k: _cp(stdout="10.0.0.246"))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-manual-join-ok"})
        assert res.status_code == 200
        assert res.json().get("ok") is True


@pytest.mark.asyncio
async def test_v2_restart_creates_new_session(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-123"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (True, ssid))
    monkeypatch.setattr(saiWebRoutes.subprocess, "run", lambda *a, **k: _cp(stdout="10.0.0.246"))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        assert start.status_code == 200
        sid = start.json().get("session_id")
        assert sid

        restart = await client.post(f"/onboard-device/v2/restart/{sid}")
        assert restart.status_code == 200
        rb = restart.json()
        assert rb.get("ok") is True
        assert rb.get("session_id")
        assert rb.get("session_id") != sid
        assert rb.get("state") == "AP_DISCOVERED"


@pytest.mark.asyncio
async def test_v2_start_init_failed_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": False, "status_code": 500, "body": None, "error": "boom"})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-test-fail"})
        assert res.status_code == 502
        body = res.json()
        assert body.get("error") == "INIT_FAILED"
        sid = body.get("session_id")
        assert sid

        sess = await client.get(f"/onboard-device/v2/session/{sid}")
        assert sess.status_code == 200
        assert sess.json().get("state") == "FAILED"


@pytest.mark.asyncio
async def test_v2_start_meta_fetch_failed_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": False, "status_code": 500, "body": None, "error": "boom"})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-test-fail"})
        assert res.status_code == 502
        body = res.json()
        assert body.get("error") == "meta_fetch_failed"
        sid = body.get("session_id")
        assert sid

        sess = await client.get(f"/onboard-device/v2/session/{sid}")
        assert sess.status_code == 200
        assert sess.json().get("state") == "FAILED"


@pytest.mark.asyncio
async def test_v2_start_init_failed_restores_previous_ssid(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": False, "status_code": 500, "body": None, "error": "boom"})

    reconnect_calls: list[tuple[str, str]] = []

    def _reconnect(ssid: str, password: str = "", **_kwargs):
        reconnect_calls.append((ssid, password))
        return True, ssid

    monkeypatch.setattr("saiAddDevice.reconnect_to_network", _reconnect)

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-test-fail"})
        assert res.status_code == 502

    assert reconnect_calls == [("ExampleWiFi", "my-password")]


@pytest.mark.asyncio
async def test_v2_start_on_macos_init_failed_restores_previous_ssid(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr(saiWebRoutes.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "pw"))
    monkeypatch.setattr("saiAddDevice._get_current_ssid", lambda: "Nodus_Setup")
    monkeypatch.setattr("saiAddDevice.PICOW_AP_SSID", "Nodus_Setup")
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": False, "status_code": 500, "body": None, "error": "boom"})

    reconnect_calls: list[tuple[str, str]] = []

    def _reconnect(ssid: str, password: str = "", **_kwargs):
        reconnect_calls.append((ssid, password))
        return True, ssid

    monkeypatch.setattr("saiAddDevice.reconnect_to_network", _reconnect)

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-manual-join-fail"})
        assert res.status_code == 502

    assert reconnect_calls == [("ExampleWiFi", "pw")]


@pytest.mark.asyncio
async def test_v2_start_success_restores_previous_ssid_before_waiting_for_hello(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "pw"))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-waiting"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})

    reconnect_calls: list[tuple[str, str]] = []

    def _reconnect(ssid: str, password: str = "", **_kwargs):
        reconnect_calls.append((ssid, password))
        return True, ssid

    monkeypatch.setattr("saiAddDevice.reconnect_to_network", _reconnect)

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-waiting"})
        assert res.status_code == 200
        body = res.json()
        assert body.get("state") == "WAITING_MQTT_HELLO"
        assert body.get("local_ssid") == "ExampleWiFi"

    assert reconnect_calls == [("ExampleWiFi", "pw")]


@pytest.mark.asyncio
async def test_v2_start_success_fails_when_local_wifi_restore_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("ExampleWiFi", "pw"))
    monkeypatch.setattr("saiAddDevice.get_itaot_meta", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"device_id": "aqi-restore-fail"}, "error": ""})
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})
    monkeypatch.setattr("saiAddDevice.reconnect_to_network", lambda ssid, password="", **_kwargs: (False, ssid))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-restore-fail"})
        assert res.status_code == 502
        body = res.json()
        assert body.get("error") == "local_wifi_restore_failed"


@pytest.mark.asyncio
async def test_v2_start_ap_connect_failed_marks_failed_session(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: False)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"hostname": "aqi-ap-fail"})
        assert res.status_code == 502
        body = res.json()
        assert body.get("error") == "ap_connect_failed"
        sid = body.get("session_id")
        assert sid

        sess = await client.get(f"/onboard-device/v2/session/{sid}")
        assert sess.status_code == 200
        assert sess.json().get("state") == "FAILED"


@pytest.mark.asyncio
async def test_v2_hello_matches_token_valid_session_when_multiple_active(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    def _issue_deterministic(self, *, session_id: str, expected_device_id: str = "", ttl_sec=None):
        ttl = max(60, int(ttl_sec if ttl_sec is not None else self.default_ttl_sec))
        token = f"tok-{session_id[:10]}"
        token_hash = self.hash_token(token)
        exp = time.time() + ttl
        session = self.store.create_session(
            session_id=session_id,
            onboard_token_hash=token_hash,
            onboard_token_secret=saiWebRoutes.saiSettings.obfuscate_secret(token),
            token_expires_at=exp,
            expected_device_id=expected_device_id,
        )
        return {
            "token": token,
            "token_hash": token_hash,
            "expires_at": exp,
            "session": session,
        }

    monkeypatch.setattr(OnboardingTokenManager, "issue_token", _issue_deterministic)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        s1 = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        s2 = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        assert s1.status_code == 200
        assert s2.status_code == 200
        sid1 = str(s1.json().get("session_id") or "")
        sid2 = str(s2.json().get("session_id") or "")
        assert sid1 and sid2 and sid1 != sid2

        token_for_sid2 = f"tok-{sid2[:10]}"
        assert callable(ingest.handler)
        ingest.handler(
            {
                "event_type": "onboarding_hello",
                "device_id": "aqi-123",
                "payload": {"onboard_token": token_for_sid2},
            }
        )

        sess1 = await client.get(f"/onboard-device/v2/session/{sid1}")
        sess2 = await client.get(f"/onboard-device/v2/session/{sid2}")
        assert sess1.status_code == 200
        assert sess2.status_code == 200
        assert sess1.json().get("state") != "FAILED"
        assert sess2.json().get("state") == "WAITING_CONFIG_ACK"


@pytest.mark.asyncio
async def test_v2_config_set_includes_onboard_token_and_settings_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    def _issue_deterministic(self, *, session_id: str, expected_device_id: str = "", ttl_sec=None):
        ttl = max(60, int(ttl_sec if ttl_sec is not None else self.default_ttl_sec))
        token = f"tok-{session_id[:10]}"
        token_hash = self.hash_token(token)
        exp = time.time() + ttl
        session = self.store.create_session(
            session_id=session_id,
            onboard_token_hash=token_hash,
            onboard_token_secret=saiWebRoutes.saiSettings.obfuscate_secret(token),
            token_expires_at=exp,
            expected_device_id=expected_device_id,
        )
        return {
            "token": token,
            "token_hash": token_hash,
            "expires_at": exp,
            "session": session,
        }

    monkeypatch.setattr(OnboardingTokenManager, "issue_token", _issue_deterministic)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        assert start.status_code == 200
        sid = str(start.json().get("session_id") or "")
        assert sid
        token = f"tok-{sid[:10]}"

        ingest.handler(
            {
                "event_type": "onboarding_hello",
                "device_id": "aqi-123",
                "payload": {"onboard_token": token},
            }
        )

        assert ingest.published
        topic, envelope = ingest.published[-1]
        assert topic == "nodus/aqi-123/config/set"
        assert envelope.get("onboard_token") == token
        payload = envelope.get("payload")
        assert isinstance(payload, dict)
        assert "settings" in payload
        assert isinstance(payload.get("settings"), dict)


@pytest.mark.asyncio
async def test_v2_ack_rejected_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    def _issue_deterministic(self, *, session_id: str, expected_device_id: str = "", ttl_sec=None):
        ttl = max(60, int(ttl_sec if ttl_sec is not None else self.default_ttl_sec))
        token = f"tok-{session_id[:10]}"
        token_hash = self.hash_token(token)
        exp = time.time() + ttl
        session = self.store.create_session(
            session_id=session_id,
            onboard_token_hash=token_hash,
            onboard_token_secret=saiWebRoutes.saiSettings.obfuscate_secret(token),
            token_expires_at=exp,
            expected_device_id=expected_device_id,
        )
        return {
            "token": token,
            "token_hash": token_hash,
            "expires_at": exp,
            "session": session,
        }

    monkeypatch.setattr(OnboardingTokenManager, "issue_token", _issue_deterministic)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        sid = str(start.json().get("session_id") or "")
        token = f"tok-{sid[:10]}"
        ingest.handler({"event_type": "onboarding_hello", "device_id": "aqi-123", "payload": {"onboard_token": token}})

        _topic, envelope = ingest.published[-1]
        msg_id = str(envelope.get("message_id") or "")
        assert msg_id
        ingest.handler(
            {
                "event_type": "onboarding_config_ack",
                "device_id": "aqi-123",
                "payload": {"message_id": msg_id, "accepted": False, "error": "config_rejected"},
            }
        )

        sess = await client.get(f"/onboard-device/v2/session/{sid}")
        assert sess.status_code == 200
        body = sess.json()
        assert body.get("state") == "FAILED"
        assert body.get("failure_reason") == "config_rejected"


@pytest.mark.asyncio
async def test_v2_ack_correlates_by_message_id_with_same_device(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)

    def _issue_deterministic(self, *, session_id: str, expected_device_id: str = "", ttl_sec=None):
        ttl = max(60, int(ttl_sec if ttl_sec is not None else self.default_ttl_sec))
        token = f"tok-{session_id[:10]}"
        token_hash = self.hash_token(token)
        exp = time.time() + ttl
        session = self.store.create_session(
            session_id=session_id,
            onboard_token_hash=token_hash,
            onboard_token_secret=saiWebRoutes.saiSettings.obfuscate_secret(token),
            token_expires_at=exp,
            expected_device_id=expected_device_id,
        )
        return {
            "token": token,
            "token_hash": token_hash,
            "expires_at": exp,
            "session": session,
        }

    monkeypatch.setattr(OnboardingTokenManager, "issue_token", _issue_deterministic)
    monkeypatch.setattr("saiAddDevice.connect_to_sensor_ap", lambda *a, **k: True)
    monkeypatch.setattr("saiAddDevice.resolve_pi_wifi_credentials", lambda: ("MyWiFi", "my-password"))
    monkeypatch.setattr("saiAddDevice.post_itaot_init", lambda *a, **k: {"ok": True, "status_code": 200, "body": {"accepted": True, "rebooting": True}, "error": ""})

    app = FastAPI()
    settings = _FakeSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        s1 = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        s2 = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        sid1 = str(s1.json().get("session_id") or "")
        sid2 = str(s2.json().get("session_id") or "")
        tok1 = f"tok-{sid1[:10]}"
        tok2 = f"tok-{sid2[:10]}"

        ingest.handler({"event_type": "onboarding_hello", "device_id": "aqi-123", "payload": {"onboard_token": tok1}})
        _topic1, env1 = ingest.published[-1]
        msg1 = str(env1.get("message_id") or "")
        ingest.handler({"event_type": "onboarding_hello", "device_id": "aqi-123", "payload": {"onboard_token": tok2}})
        _topic2, env2 = ingest.published[-1]
        msg2 = str(env2.get("message_id") or "")
        assert msg1 and msg2 and msg1 != msg2

        ingest.handler(
            {
                "event_type": "onboarding_config_ack",
                "device_id": "aqi-123",
                "payload": {"message_id": msg2, "accepted": True},
            }
        )

        sess1 = await client.get(f"/onboard-device/v2/session/{sid1}")
        sess2 = await client.get(f"/onboard-device/v2/session/{sid2}")
        assert sess1.status_code == 200
        assert sess2.status_code == 200
        assert sess1.json().get("state") == "WAITING_CONFIG_ACK"
        assert sess2.json().get("state") == "WAITING_CONFIG_RESULT"


@pytest.mark.asyncio
async def test_v2_start_disabled_by_feature_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _DummyFastStats)

    class _TmpStore(OnboardingSessionStore):
        def __init__(self, base_dir: str = "system_settings"):
            super().__init__(base_dir=str(tmp_path))

    class _FlagOffSettings(_FakeSettings):
        def __init__(self):
            super().__init__()
            self._vals[("Onboarding", "ONBOARDING_V2_MQTT")] = False

    monkeypatch.setattr(saiWebRoutes, "OnboardingSessionStore", _TmpStore)
    app = FastAPI()
    settings = _FlagOffSettings()
    ingest = _FakeIngest()
    await saiWebRoutes.register_routes(app, settings, _FakeNetMgr(), _FakeGcMgr(), ingest)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/onboard-device/v2/start", data={"device_id": "aqi-123"})
        assert res.status_code == 409
        assert res.json().get("error") == "onboarding_v2_disabled"
