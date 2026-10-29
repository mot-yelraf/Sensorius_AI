"""USB protocol, transport lifecycle and isolated station regression coverage."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sensorius.weather_adapters import acurite
from sensorius.weather_adapters.acurite import (
    AcuRiteError,
    AcuRiteUsbReader,
    PyUsbConsole,
    decode_console,
    decode_outdoor,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/acurite-usb.json").read_text())


def packet(key):
    return bytes.fromhex(FIXTURE[key])


class Clock:
    def __init__(self):
        self.ticks = 1000

    def monotonic(self):
        return self.ticks

    def now(self, zone):
        return datetime(2026, 10, 28, 12, tzinfo=timezone.utc) + timedelta(
            seconds=self.ticks
        )

    def advance(self, seconds=18):
        self.ticks += seconds


class FakeTransport:
    def __init__(self):
        self.reports = {1: packet("temperature"), 2: packet("console_linear")}
        self.identity = "pi-usb-port-1"
        self.error = None
        self.requests = []

    def read_reports(self, include_console):
        self.requests.append(include_console)
        if self.error:
            raise self.error
        return (
            self.identity,
            {
                key: value
                for key, value in self.reports.items()
                if key == 1 or include_console
            },
            [],
        )


@pytest.fixture
def acquisition(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(acurite, "monotonic", clock.monotonic)
    monkeypatch.setattr(acurite, "datetime", clock)
    settings = SimpleNamespace(acurite_enabled=True, acurite_usb_serial="", acurite_model="01536", timezone="UTC")
    transport = FakeTransport()
    return AcuRiteUsbReader(settings, transport), transport, clock


def test_outdoor_reports_preserve_zero_and_decode_sensor_identity():
    report = decode_outdoor(packet("temperature"))
    assert report.sensor_id == "A-123"
    assert report.measurements == {"temperature": 68, "humidity": 65, "wind_speed": 0}
    assert report.signal == 3 and report.battery == "OK"
    report = decode_outdoor(packet("north_dry"))
    assert report.measurements == {"wind_speed": 0, "wind_dir": 0}
    assert report.rain_counter == 0
    report = decode_outdoor(packet("wind_east"))
    assert report.measurements["wind_dir"] == 90
    assert report.measurements["wind_speed"] == pytest.approx(4.73634, abs=0.0001)
    assert report.rain_counter == 1


@pytest.mark.parametrize("key", ["console_linear", "console_hp03"])
def test_console_calibration_maps_absolute_pressure_only(key):
    values = decode_console(packet(key))
    assert values["indoor_temperature"] == 77
    hpa = 1013.25 if key == "console_linear" else 763.77734375
    assert values["indoor_absolute_pressure"] * 33.8638866667 == pytest.approx(hpa)
    assert "pressure" not in values and "indoor_pressure" not in values


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x01" * 9,
        b"\x02" * 10,
        packet("sensor_absent"),
        bytes.fromhex("01 c1 23 78 00 08 38 41 04 ff"),
    ],
)
def test_bad_outdoor_reports_are_rejected(raw):
    with pytest.raises(AcuRiteError):
        decode_outdoor(raw)


@pytest.mark.parametrize("raw", [b"", b"\x02" * 25, bytes(25)])
def test_unknown_console_calibration_is_not_guessed(raw):
    with pytest.raises(AcuRiteError):
        decode_console(raw)


def test_rf_loss_does_not_republish_cached_outdoor_values(acquisition):
    adapter, transport, clock = acquisition
    adapter.sample_once()
    clock.advance()
    transport.reports[1] = packet("rf_lost")
    adapter.sample_once()
    values = adapter.read().measurements
    assert "Temperature_F" not in values and "Wind Speed" not in values
    assert values["Console Temperature_F"] == 77
    assert adapter.status()["connected"] is True
    assert adapter.status()["station"]["reporting"] is False


def test_alternating_packets_merge_only_fresh_fields(acquisition):
    adapter, transport, clock = acquisition
    adapter.sample_once()
    clock.advance()
    transport.reports[1] = packet("north_rain_one_inch")
    adapter.sample_once()
    observation = adapter.read()
    assert observation.measurements["Temperature_F"] == 68
    assert observation.measurements["Wind Direction"] == 0
    assert observation.rain_counter == 1
    assert adapter.read() is None  # Never stamp the same cache as a new observation.
    clock.advance(121)
    assert adapter.read() is None
    assert adapter.status()["observation_fresh"] is False


def test_source_change_and_usb_disconnect_clear_caches(acquisition):
    adapter, transport, clock = acquisition
    adapter.sample_once()
    old = adapter.read().source_id
    clock.advance()
    transport.identity = "pi-usb-port-2"
    transport.reports = {1: packet("north_dry")}
    adapter.sample_once()
    observation = adapter.read()
    assert observation.source_id != old
    assert "Temperature_F" not in observation.measurements
    clock.advance()
    transport.error = AcuRiteError("USB unavailable")
    adapter.sample_once()
    assert adapter.read() is None
    assert adapter.status()["connected"] is False
    transport.error = None
    clock.advance()
    adapter.sample_once()
    assert adapter.read() is not None


def test_bad_console_data_keeps_valid_outdoor_report(acquisition):
    adapter, transport, _ = acquisition
    transport.reports[2] = bytes(25)
    adapter.sample_once()
    values = adapter.read().measurements
    assert {key: values[key] for key in ("Temperature_F", "Rel-Humidity", "Wind Speed", "Temperature")} == {
        "Temperature_F": 68,
        "Rel-Humidity": 65,
        "Wind Speed": 0,
        "Temperature": 20,
    }
    assert (
        "calibration" in adapter.status()["last_error"]
        or "console report" in adapter.status()["last_error"]
    )


def test_sampling_console_cadence_and_disabled_behavior(acquisition):
    adapter, transport, clock = acquisition
    adapter.sample_once()
    clock.advance(18)
    adapter.sample_once()
    clock.advance(60)
    adapter.sample_once()
    assert transport.requests == [True, False, True]
    adapter.settings.acurite_enabled = False
    adapter.sample_once()
    assert len(transport.requests) == 3
    assert adapter.read() is None
    assert adapter.status()["state"] == "disabled"






class UsbError(Exception):
    pass


class Device:
    bus = 1
    port_numbers = (2, 3)
    serial_number = "test-console"

    def __init__(self):
        self.calls = []
        self.fail_claim = False
        self.fail_report = None

    def is_kernel_driver_active(self, interface):
        return True

    def detach_kernel_driver(self, interface):
        self.calls.append("detach")

    def attach_kernel_driver(self, interface):
        self.calls.append("attach")

    def ctrl_transfer(self, kind, request, value, interface, length, timeout):
        self.calls.append((kind, request, value, interface, length, timeout))
        if value == self.fail_report:
            raise UsbError("test failure")
        return packet("temperature" if value == 0x101 else "console_linear")


class Util:
    def claim_interface(self, device, interface):
        device.calls.append("claim")
        if device.fail_claim:
            raise UsbError("busy")

    def release_interface(self, device, interface):
        device.calls.append("release")

    def dispose_resources(self, device):
        device.calls.append("dispose")


def usb_transport(devices, serial=""):
    core = SimpleNamespace(find=lambda **kwargs: devices, USBError=UsbError)
    return PyUsbConsole(serial, core=core, util=Util())


def test_usb_requests_are_bounded_and_resources_reattached():
    device = Device()
    identity, reports, errors = usb_transport([device]).read_reports(True)
    assert identity == "bus-1-ports-2.3"
    assert set(reports) == {1, 2} and not errors
    assert device.calls == [
        "detach",
        "claim",
        (0xA1, 1, 0x101, 0, 10, 1000),
        (0xA1, 1, 0x102, 0, 25, 1000),
        "release",
        "attach",
        "dispose",
    ]


def test_usb_partial_failure_keeps_valid_report_and_cleanup():
    device = Device()
    device.fail_report = 0x102
    _, reports, errors = usb_transport([device]).read_reports(True)
    assert set(reports) == {1} and errors
    assert device.calls[-3:] == ["release", "attach", "dispose"]


def test_usb_failed_claim_reattaches_driver_and_disposes():
    device = Device()
    device.fail_claim = True
    with pytest.raises(AcuRiteError, match="exclusive"):
        usb_transport([device]).read_reports(False)
    assert device.calls == ["detach", "claim", "attach", "dispose"]


def test_usb_device_ambiguity_requires_explicit_selection():
    first, second = Device(), Device()
    with pytest.raises(AcuRiteError, match="Multiple"):
        usb_transport([first, second]).read_reports(False)
    second.serial_number = "second"
    identity, _, _ = usb_transport([first, second], "second").read_reports(False)
    assert identity == "second"
    assert "claim" not in first.calls


def test_missing_pyusb_reports_actionable_error(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "usb", None)
    with pytest.raises(AcuRiteError, match="PyUSB is missing"):
        PyUsbConsole().read_reports(False)






def test_console_calibration_error_stays_visible_between_console_reports(acquisition):
    adapter, transport, clock = acquisition
    transport.reports[2] = bytes(25)
    adapter.sample_once()
    error = adapter.status()["last_error"]
    assert error
    clock.advance(18)
    adapter.sample_once()
    assert adapter.status()["last_error"] == error
    clock.advance(60)
    transport.reports[2] = packet("console_linear")
    adapter.sample_once()
    assert adapter.status()["last_error"] == ""
