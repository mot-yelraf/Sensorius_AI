"""Verify weather acquisition cadence with controlled clocks and fake transports.

Request counts exercise the supervised services, including retries and live
settings changes, without USB hardware or waiting for real polling intervals.
"""

import asyncio
from types import SimpleNamespace

import pytest

import sensorius.saiAcuRite as acurite_service
import sensorius.saiUsbWeather as usb_service
from sensorius.weather_adapters import acurite, davis
from test_acurite_adapter import acquisition, packet
from test_davis_adapter import FakeTransport as DavisTransport
from test_weather_devices import service


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis"])
@pytest.mark.parametrize("interval", [60, 300, 3600])
@pytest.mark.parametrize("fail", [False, True])
async def test_device_requests_and_retries_follow_configured_interval(service, monkeypatch, kind, interval, fail):
    current, settings, logger, manager = service
    clock = SimpleNamespace(value=1000.0)
    attempts, sleeps, feeds = [], [], []
    if kind == "davis":
        current = usb_service.UsbWeatherIngest(
            kind, settings=settings, data_logger=logger, sensor_manager=manager, transport=DavisTransport())
        current.configure({"port": "/dev/test-vue", "poll_interval_sec": interval})
        module, adapter_module, method = usb_service, davis, "read_snapshot"
    else:
        current.configure({"poll_interval_sec": interval})
        module, adapter_module, method = acurite_service, acurite, "read_reports"
    original = getattr(current.adapter.transport, method)

    def request(*args):
        attempts.append(clock.value)
        if fail:
            raise RuntimeError("Transport unavailable")
        return original(*args)

    async def sleep(delay):
        sleeps.append(delay)
        if len(attempts) == 3:
            raise asyncio.CancelledError
        clock.value += delay

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock.value)
    monkeypatch.setattr(current.adapter.transport, method, request)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    current.supervisor = SimpleNamespace(feedthedogs=lambda name: feeds.append(name))
    with pytest.raises(asyncio.CancelledError):
        await current.run()
    assert attempts == [1000, 1000 + interval, 1000 + interval * 2]
    assert max(sleeps) <= 9
    assert len(feeds) >= len(sleeps)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis"])
async def test_live_interval_change_updates_next_device_request(service, monkeypatch, kind):
    current, settings, logger, manager = service
    if kind == "davis":
        current = usb_service.UsbWeatherIngest(
            kind, settings=settings, data_logger=logger, sensor_manager=manager, transport=DavisTransport())
        current.configure({"port": "/dev/test-vue", "poll_interval_sec": 300})
        module, section = usb_service, "DavisVue"
    else:
        current.configure({"poll_interval_sec": 300})
        module, section = acurite_service, "AcuRite"
    clock = SimpleNamespace(value=1000.0)
    attempts = []

    async def poll():
        attempts.append(clock.value)

    async def sleep(delay):
        if len(attempts) == 2:
            raise asyncio.CancelledError
        clock.value += delay
        settings.replace_setting(section, "POLL_INTERVAL_SEC", 60)

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(current, "poll_once", poll)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await current.run()
    assert attempts == [1000, 1060]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acurite", "davis"])
async def test_longer_live_interval_updates_cached_health_without_device_requests(service, monkeypatch, kind):
    from dataclasses import replace
    current, settings, logger, manager = service
    clock = SimpleNamespace(value=1000.0)
    if kind == "davis":
        current = usb_service.UsbWeatherIngest(
            kind, settings=settings, data_logger=logger, sensor_manager=manager, transport=DavisTransport())
        current.configure({"port": "/dev/test-vue"})
        adapter_module, section = davis, "DavisVue"
    else:
        adapter_module, section = acurite, "AcuRite"
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock.value)
    await current.poll_once()
    if kind == "davis":
        current.adapter.transport.current = replace(current.adapter.transport.current, received_packets=102)
        await current.poll_once()
    transport = current.adapter.transport
    requests = transport.calls if kind == "davis" else len(transport.requests)
    settings.replace_setting(section, "POLL_INTERVAL_SEC", 300)
    clock.value += 200
    assert current.status()["state"] == "online"
    assert (transport.calls if kind == "davis" else len(transport.requests)) == requests


def test_acurite_long_interval_retains_alternating_fields_but_expires_them(acquisition):
    reader, transport, clock = acquisition
    reader.settings.acurite_poll_interval_sec = 300
    reader.sample_once()
    clock.advance(300)
    assert reader.status()["station"]["reporting"]
    transport.reports[1] = packet("wind_east")
    reader.sample_once()
    observation = reader.read()
    assert observation.measurements["Temperature_F"] == 68
    assert observation.measurements["Wind Direction"] == 90
    clock.advance(301)
    reader.sample_once()
    assert "Temperature_F" not in reader.read().measurements
    clock.advance(601)
    assert not reader.status()["station"]["reporting"]
    assert reader.read() is None


def test_davis_long_interval_does_not_expire_before_next_acquisition(monkeypatch):
    from dataclasses import replace
    from test_davis_adapter import adapter
    reader = adapter()
    reader.settings.davis_poll_interval_sec = 300
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(davis, "monotonic", lambda: clock.value)
    reader.sample_once()
    clock.value += 300
    reader.transport.current = replace(reader.transport.current, received_packets=102)
    reader.sample_once()
    clock.value += 300
    assert reader.status()["station"]["reporting"]
    clock.value += 301
    assert not reader.status()["station"]["reporting"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ecowitt", "weewx"])
@pytest.mark.parametrize("interval", [60, 300, 3600])
async def test_gateway_and_archive_acquisition_use_configured_interval(monkeypatch, kind, interval):
    import sensorius.saiEcowitt as gateway_module
    import sensorius.saiWeeWX as archive_module
    from test_weather_devices import Settings
    settings = Settings()
    section = "Ecowitt" if kind == "ecowitt" else "WeeWX"
    settings.set_many_in_memory([
        (section, "ENABLED", True), (section, "POLL_INTERVAL_SEC", interval),
        (section, "GATEWAY_URL", "http://192.0.2.1"), (section, "SENSOR_ID", "weather-test"),
    ])
    if kind == "ecowitt":
        current = gateway_module.EcowittGatewayIngest(settings=settings, data_logger=SimpleNamespace())
        current._migrated_sensor_id = "weather-test"
    else:
        current = archive_module.WeeWXArchiveIngest(settings=settings, data_logger=SimpleNamespace())
    clock = SimpleNamespace(value=0)
    attempts = []

    def acquire():
        attempts.append(clock.value)

    async def poll():
        acquire()

    async def sleep(delay):
        if len(attempts) == 3:
            raise asyncio.CancelledError
        clock.value += delay

    if kind == "ecowitt":
        monkeypatch.setattr(current, "poll_once", poll)
    else:
        monkeypatch.setattr(current, "import_latest_once", acquire)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await current.run()
    assert attempts == [0, interval, interval * 2]
