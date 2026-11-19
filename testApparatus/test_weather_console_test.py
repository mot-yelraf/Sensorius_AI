"""Verify one-shot USB console tests without saving settings or live readings.

Protocol fixtures exercise the normal adapters, while API and cancellation
checks cover enable gating, sanitized failures and exclusive acquisition locks.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from sensorius.saiAcuRite import AcuRiteUsbIngest
from sensorius.saiUsbWeather import UsbWeatherIngest
from sensorius.saiWebWeatherDevices import register_weather_device_routes
from test_weather_devices import Settings
from test_acurite_adapter import FakeTransport as AcuRiteTransport
from test_davis_adapter import FakeTransport as DavisTransport
from test_wmr100_adapter import FakeTransport as OregonTransport


def make_service(kind):
    settings, logger = Settings(), SimpleNamespace()
    if kind == "acurite":
        transport = AcuRiteTransport()
        current = AcuRiteUsbIngest(settings=settings, data_logger=logger, transport=transport)
        payload = {"enabled": True, "model": "02032", "usb_serial": "test-console", "poll_interval_sec": "300"}
        method = "read_reports"
    else:
        transport = DavisTransport() if kind == "davis" else OregonTransport()
        current = UsbWeatherIngest(kind, settings=settings, data_logger=logger, transport=transport)
        payload = {"enabled": True, "poll_interval_sec": "300"}
        payload.update({"port": "/dev/test-vue", "baud_rate": "9600"} if kind == "davis" else {"model": "WMR88A", "usb_serial": "test-console"})
        method = "read_snapshot" if kind == "davis" else "read_batch"
    return current, settings, transport, payload, method


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis", "oregon"])
async def test_retrieval_uses_draft_config_and_real_protocol_without_persistence(kind, monkeypatch):
    current, settings, transport, payload, _method = make_service(kind)
    captured = []
    if kind == "acurite":
        import sensorius.saiAcuRite as module
        original = module.AcuRiteUsbReader
        def create(config, transport):
            captured.append(vars(config))
            return original(config, transport)
        monkeypatch.setattr(module, "AcuRiteUsbReader", create)
    else:
        import sensorius.saiUsbWeather as module
        family = module.FAMILIES[kind]
        def create(config, transport):
            captured.append(vars(config))
            return family[3](config, transport)
        monkeypatch.setitem(module.FAMILIES, kind, (*family[:3], create))
    baseline = object()
    current._baseline = baseline
    assert await current.test_retrieval(payload) is True
    assert len(captured) == 1
    config = captured[0]
    if kind == "davis":
        assert config["davis_port"] == payload["port"] and config["davis_baud_rate"] == 9600
        assert transport.calls == 1
    else:
        prefix = "acurite" if kind == "acurite" else "wmr100"
        assert config[f"{prefix}_model"] == payload["model"]
        assert config[f"{prefix}_usb_serial"] == "test-console"
        if kind == "acurite":
            assert transport.requests == [True]
        else:
            assert transport.calls == 1
    assert settings.values == {} and settings.saves == 0
    assert current._baseline is baseline and current._last_write == float("-inf")
    assert current._sensor_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis", "oregon"])
async def test_disabled_or_invalid_configuration_never_requests_data(kind, monkeypatch):
    current, settings, transport, payload, method = make_service(kind)
    def no_hardware(*args):
        pytest.fail("Disabled/invalid test must not acquire USB")
    monkeypatch.setattr(transport, method, no_hardware)
    for candidate in ({**payload, "enabled": False}, {**payload, "poll_interval_sec": 0}, {**payload, "enabled": "true"}):
        with pytest.raises(ValueError):
            await current.test_retrieval(candidate)
    assert settings.values == {} and settings.saves == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis", "oregon"])
async def test_probe_requires_new_decoded_data_instead_of_old_status(kind, monkeypatch):
    current, settings, transport, payload, method = make_service(kind)
    assert await current.test_retrieval(payload)
    if kind == "acurite":
        transport.reports = {}
    elif kind == "davis":
        from sensorius.weather_adapters.davis import DavisError
        def fail():
            raise DavisError("No valid serial reply")
        monkeypatch.setattr(transport, method, fail)
    else:
        monkeypatch.setattr(transport, method, lambda: ("console-1", []))
    assert await current.test_retrieval(payload) is False
    assert settings.values == {} and settings.saves == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis", "oregon"])
async def test_cancellation_holds_acquisition_lock_until_worker_finishes(kind, monkeypatch):
    current, _settings, transport, payload, method = make_service(kind)
    entered, release = threading.Event(), threading.Event()
    original = getattr(transport, method)
    def blocked(*args):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("Test worker was not released")
        return original(*args)
    monkeypatch.setattr(transport, method, blocked)
    task = asyncio.create_task(current.test_retrieval(payload))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        lock = current._configuration_lock if kind == "acurite" else current._lock
        assert lock.locked() and not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lock.locked()


@pytest.mark.asyncio
async def test_oregon_probe_releases_owned_usb_before_testing_and_closes_probe(monkeypatch):
    current, settings, transport, payload, method = make_service("oregon")
    released = []
    current.adapter._owned_transport = SimpleNamespace(close=lambda: released.append("old"))
    original = getattr(transport, method)
    def read():
        assert released == ["old"]
        return original()
    monkeypatch.setattr(transport, method, read)
    import sensorius.weather_adapters.wmr100_usb as usb
    current.adapter.transport = None
    transport.close = lambda: released.append("probe")
    monkeypatch.setattr(usb, "console_transport", lambda serial: transport)
    assert await current.test_retrieval(payload)
    assert released == ["old", "probe"]
    assert current.adapter._owned_transport is None
    assert settings.values == {} and settings.saves == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis", "oregon"])
async def test_http_retrieval_gates_enable_protects_access_and_sanitizes_errors(kind):
    calls = []
    result = [True]
    class Service:
        async def test_retrieval(self, payload):
            calls.append(payload)
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]
    services = SimpleNamespace(**{f"{key}_service": Service() for key in ("acurite", "davis", "oregon")})
    def require_access(request, **kwargs):
        assert kwargs["require_csrf"] is True
        if request.headers.get("x-test-key") != "authorized":
            raise HTTPException(401)
    app = FastAPI()
    register_weather_device_routes(app, services=services, settings_factory=Settings, require_protected_access=require_access, invalidate_dashboard_caches=lambda: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/{kind}/test"
        assert (await client.post(url, json={"enabled": True})).status_code == 401
        client.headers["x-test-key"] = "authorized"
        for payload in ({}, {"enabled": False}, {"enabled": "true"}, []):
            assert (await client.post(url, json=payload)).status_code == 400
        assert calls == []
        payload = make_service(kind)[3]
        reply = await client.post(url, json=payload)
        assert reply.status_code == 200 and reply.json()["ok"] is True
        assert "succeeded" in reply.json()["message"] and calls == [payload]
        result[0] = False
        reply = await client.post(url, json=payload)
        assert reply.status_code == 502 and "no valid live data" in reply.json()["error"]
        for error in (ValueError("/private/device/config"), RuntimeError("/private/device/driver")):
            result[0] = error
            reply = await client.post(url, json=payload)
            assert reply.status_code in {400, 502} and "/private/" not in reply.text
        setattr(services, f"{kind}_service", None)
        if kind != "acurite":
            assert (await client.post(url, json=payload)).status_code == 503
