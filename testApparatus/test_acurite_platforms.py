"""Exercise native desktop HID access without physical USB devices."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from sensorius.weather_adapters import acurite
from sensorius.weather_adapters.acurite import (
    AcuRiteError,
    HidWorkerConsole,
    PyUsbConsole,
    console_transport,
)
from sensorius.weather_adapters.acurite_hid import NativeHidConsole
from test_acurite_adapter import packet


class HidDevice:
    def __init__(self):
        self.calls = []
        self.fail = None

    def open_path(self, path):
        self.calls.append(("open", path))
        if self.fail == "open":
            raise OSError("device busy")

    def get_input_report(self, number, length):
        self.calls.append(("report", number, length))
        if self.fail == number:
            raise OSError("read error")
        raw = packet("temperature" if number == 1 else "console_linear")
        return list(raw.ljust(length, b"\0"))

    def close(self):
        self.calls.append(("close",))


def hid_module(device, *, entries=None):
    entries = (
        entries
        if entries is not None
        else [
            {
                "path": b"console-path",
                "interface_number": 0,
                "serial_number": "console-123",
            }
        ]
    )

    def enumerate_devices(vendor, product):
        assert (vendor, product) == (0x24C0, 3)
        return entries

    return SimpleNamespace(enumerate=enumerate_devices, device=lambda: device)


def test_native_hid_reads_numbered_reports_and_closes_handle():
    device = HidDevice()
    identity, reports, errors = NativeHidConsole(
        hid_module=hid_module(device)
    ).read_reports(True)
    assert identity == "hid-console-123"
    assert reports == {1: packet("temperature"), 2: packet("console_linear")}
    assert not errors
    assert device.calls == [
        ("open", b"console-path"),
        ("report", 1, 33),
        ("report", 2, 33),
        ("close",),
    ]


@pytest.mark.parametrize("failure", ["open", 1, 2])
def test_native_hid_failures_close_handle_and_preserve_partial_reads(failure):
    device = HidDevice()
    device.fail = failure
    transport = NativeHidConsole(hid_module=hid_module(device))
    if failure == "open":
        with pytest.raises(AcuRiteError, match="HID access failed"):
            transport.read_reports(True)
    else:
        _, reports, errors = transport.read_reports(True)
        assert set(reports) == ({2} if failure == 1 else {1})
        assert errors
    assert device.calls[-1] == ("close",)


def test_native_hid_requires_selection_when_two_consoles_are_present():
    device = HidDevice()
    entries = [
        {"path": b"one", "serial_number": "one"},
        {"path": b"two", "serial_number": "two"},
    ]
    module = hid_module(device, entries=entries)
    with pytest.raises(AcuRiteError, match="Multiple"):
        NativeHidConsole(hid_module=module).read_reports(False)
    identity, _, _ = NativeHidConsole("two", hid_module=module).read_reports(False)
    assert identity == "hid-two"
    assert device.calls[0] == ("open", b"two")


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("linux", PyUsbConsole),
        ("darwin", HidWorkerConsole),
        ("win32", HidWorkerConsole),
    ],
)
def test_platform_selects_native_transport(platform, expected, monkeypatch):
    monkeypatch.setattr(acurite.sys, "platform", platform)
    assert isinstance(console_transport("console-123"), expected)


def test_hid_worker_applies_timeout_and_decodes_reports(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "identity": "hid-console",
                    "reports": {"1": packet("temperature").hex()},
                    "errors": [],
                }
            ),
        )

    monkeypatch.setattr(acurite.subprocess, "run", run)
    identity, reports, _ = HidWorkerConsole("console-123").read_reports(False)
    assert identity == "hid-console" and reports[1] == packet("temperature")
    assert calls[0][1]["timeout"] == 5
    assert json.loads(calls[0][1]["input"]) == {
        "serial": "console-123",
        "include_console": False,
    }
    assert calls[0][0][-2:] == ["-m", "sensorius.weather_adapters.acurite_hid"]


def test_hid_worker_timeout_is_recoverable(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("test", 5)

    monkeypatch.setattr(acurite.subprocess, "run", timeout)
    with pytest.raises(AcuRiteError, match="timed out"):
        HidWorkerConsole().read_reports(True)


def test_frozen_hid_worker_uses_dedicated_entrypoint(monkeypatch):
    calls = []
    monkeypatch.setattr(acurite.sys, "frozen", True, raising=False)
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "identity": "console", "reports": {"1": packet("temperature").hex()}, "errors": [],
        }))
    monkeypatch.setattr(acurite.subprocess, "run", run)
    assert HidWorkerConsole().read_reports(False)[0] == "console"
    assert calls[0] == [acurite.sys.executable, "--acurite-hid-worker"]
