"""Protocol-derived WMR100 fixtures; no hardware, network, or WeeWX dependency."""

from types import SimpleNamespace

import pytest

def AppSettings(**kwargs):
    defaults = dict(davis_enabled=False, davis_port="", davis_baud_rate=19200,
                    wmr100_enabled=False, wmr100_usb_serial="", wmr100_model="WMR100", timezone="UTC")
    return SimpleNamespace(**(defaults | kwargs))

from sensorius.weather_adapters.wmr100 import PacketStream, Wmr100Error, Wmr100UsbAdapter, decode_packet
from sensorius.weather_adapters.wmr100_usb import PyUsbConsole, NativeHidConsole, read_native_batch


def packet(payload):
    raw = bytes.fromhex(payload)
    return raw + sum(raw).to_bytes(2, "little")


# Literal protocol examples: 14.5 C, 72% RH; 2.2 m/s gust, 4.6 m/s mean;
# raw 1005 hPa, UV 5. Rain here is a synthetic 1.00-inch reset counter.
TEMP = bytes.fromhex("20 42 d1 91 00 48 64 00 00 20 90 02")
PRESSURE = bytes.fromhex("00 46 ed 03 ed 33 56 02")
WIND = bytes.fromhex("00 48 0a 0c 16 e0 02 00 20 76 01")
UV = bytes.fromhex("00 47 00 05 4c 00")
RAIN = packet("00 41 04 00 00 00 00 00 64 00 00 0c 01 01 1a")


def reports(*packets):
    stream = b"\xff\xff" + b"\xff\xff".join(packets) + b"\xff\xff"
    return [
        bytes([len(chunk)]) + chunk + bytes(7 - len(chunk))
        for offset in range(0, len(stream), 7)
        if (chunk := stream[offset : offset + 7])
    ]


class FakeTransport:
    continuous = True

    def __init__(self, *packets):
        self.packets = packets or (TEMP, PRESSURE, WIND, UV, RAIN)
        self.identity = "console-1"
        self.calls = 0

    def read_batch(self):
        self.calls += 1
        return self.identity, reports(*self.packets)


def adapter(*packets):
    return Wmr100UsbAdapter(AppSettings(wmr100_enabled=True), FakeTransport(*packets))


def test_literal_protocol_packets_and_units():
    assert decode_packet(TEMP).values == {"temperature": 58.1, "humidity": 72.0}
    assert decode_packet(PRESSURE).values["absolute_pressure"] == pytest.approx(
        1005 / 33.8638866667
    )
    assert decode_packet(UV).values == {"uv": 5}
    wind = decode_packet(WIND).values
    assert wind["wind_speed"] == pytest.approx(4.6 / 0.44704)
    assert wind["wind_dir"] == 225
    assert wind["wind_gust"] is None  # Console gust below mean is unusable.
    rain = decode_packet(RAIN)
    assert rain.counter == 1
    assert rain.values == {"rain_rate": 0.04}
    assert rain.reset_id == "000c01011a"
    negative = decode_packet(packet("40 42 01 7b 80 00 00 00 00 00"))
    assert negative.values["temperature"] == pytest.approx(9.86)
    assert negative.values["humidity"] == 0
    assert negative.battery == "LOW"
    assert decode_packet(packet("00 42 02 00 00 32 00 00 00 00")) is None
    assert decode_packet(packet("00 60 00 00 00 00 01 01 1a 00")) is None


def test_zero_missing_and_invalid_fields():
    wind = decode_packet(packet("00 48 00 00 00 00 00 00 00"))
    assert wind.values == {"wind_speed": 0, "wind_gust": 0, "wind_dir": 0}
    temp = decode_packet(packet("00 42 00 ff 7f ff 00 00 00 00"))
    assert temp.values == {"indoor_temperature": None, "indoor_humidity": None}
    assert decode_packet(packet("00 47 00 ff")).values["uv"] is None
    for bad in (b"", TEMP[:-1], TEMP[:-1] + b"\x00", packet("00 48 00")):
        with pytest.raises(Wmr100Error):
            decode_packet(bad)


def test_stream_handles_fragmentation_padding_noise_and_bounded_recovery():
    parser = PacketStream()
    result = []
    wire = b"noise\xff\xff" + TEMP + b"\xff\xff" + UV + b"\xff\xff"
    for byte in wire:
        result.extend(parser.feed(bytes([1, byte, 99, 99, 99, 99, 99, 99])))
    assert result == [TEMP, UV]
    with pytest.raises(Wmr100Error):
        parser.feed(bytes([8]) + bytes(7))
    with pytest.raises(Wmr100Error):
        for _ in range(5):
            parser.feed(bytes([7]) + bytes(7))
    assert len(parser.buffer) <= 1
    recovered = []
    for report in reports(UV):
        recovered.extend(parser.feed(report))
    assert recovered == [UV]


def test_cache_freshness_status_identity_and_no_duplicate_snapshot(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("sensorius.weather_adapters.wmr100.monotonic", lambda: now[0])
    device = adapter()
    assert device.read() is None
    device.sample_once()
    observation = device.read()
    assert observation.source_id == "wmr100:console-1:outdoor-1"
    assert observation.rain_counter == 1
    assert observation.rain.modulus == 655.36
    assert "pressure" not in observation.measurements
    assert device.read() is None
    assert device.status()["station"]["reporting"]
    now[0] += 181
    device.transport.packets = (PRESSURE,)
    device.sample_once()
    observation = device.read()
    assert "temperature" not in observation.measurements
    assert observation.rain_counter is None
    assert not device.status()["station"]["reporting"]
    assert device.status()["connected"]
    device.transport.identity = "replacement"
    device.transport.packets = (UV,)
    device.sample_once()
    observation = device.read()
    assert observation.source_id.startswith("wmr100:replacement:")
    assert "absolute_pressure" not in observation.measurements
    device.settings.wmr100_enabled = False
    assert device.read() is None
    assert not device.status()["connected"]
    device.sample_once()
    assert device.transport.calls == 3


def test_failure_clears_cache_and_bad_packet_does_not_hide_later_good_packet():
    device = adapter(TEMP[:-1] + b"\x00", UV)
    device.sample_once()
    assert "checksum" in device.status()["last_error"]
    # A damaged packet must not discard a later packet in the same batch.
    assert device.read().measurements["uv"] == 5

    def fail():
        raise Wmr100Error("disconnected")

    device.transport.read_batch = fail
    device.sample_once()
    assert device.read() is None
    assert not device.status()["connected"]
    assert device.status()["last_error"] == "disconnected"






def test_native_worker_deadline(monkeypatch):
    import subprocess

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired("worker", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(Wmr100Error, match="timed out"):
        NativeHidConsole().read_batch()


class UsbError(Exception):
    pass


class Timeout(UsbError):
    pass


class FakeUsb:
    serial_number, bus, port_numbers = "usb-1", 1, (2, 3)

    def __init__(self):
        self.calls = []

    def is_kernel_driver_active(self, interface):
        return True

    def detach_kernel_driver(self, interface):
        self.calls.append("detach")

    def attach_kernel_driver(self, interface):
        self.calls.append("attach")

    def ctrl_transfer(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def read(self, *args, **kwargs):
        assert args == (0x81, 8) and kwargs["timeout"] == 250
        return reports(UV)[0]


def test_linux_claim_activation_persistent_session_and_cleanup():
    usb = FakeUsb()
    util = SimpleNamespace(
        claim_interface=lambda d, i: usb.calls.append("claim"),
        release_interface=lambda d, i: usb.calls.append("release"),
        dispose_resources=lambda d: usb.calls.append("dispose"),
    )
    core = SimpleNamespace(find=lambda **kw: [usb], USBTimeoutError=Timeout)
    transport = PyUsbConsole(core=core, util=util)
    assert transport.read_batch()[0] == "usb-1"
    assert len(transport.read_batch()[1]) == 32
    assert usb.calls.count("claim") == 1
    command = next(item for item in usb.calls if isinstance(item, tuple))
    assert command[0][:4] == (0x21, 9, 0x200, 0)
    transport.close()
    assert usb.calls[-3:] == ["release", "attach", "dispose"]
    transport.close()


def test_native_hid_activation_and_always_close(monkeypatch):
    handle = SimpleNamespace(
        open_path=lambda p: None,
        write=lambda raw: len(raw),
        read=lambda *a, **kw: list(reports(UV)[0]),
        close=lambda: closed.append(True),
    )
    closed = []
    hid = SimpleNamespace(
        enumerate=lambda *a: [{"path": b"path", "interface_number": 0}],
        device=lambda: handle,
    )
    identity, batch = read_native_batch("", hid)
    assert identity.startswith("hid-") and len(batch) == 128
    assert closed == [True]
    handle.write = lambda raw: 0
    with pytest.raises(Wmr100Error):
        read_native_batch("", hid)
    assert closed == [True, True]






def test_linux_activation_failure_restores_driver_and_releases_every_handle_once():
    usb, other = FakeUsb(), FakeUsb()
    other.serial_number = "other"
    disposed = []

    def fail(*args, **kwargs):
        raise UsbError("activation failed")

    usb.ctrl_transfer = fail
    util = SimpleNamespace(
        claim_interface=lambda d, i: usb.calls.append("claim"),
        release_interface=lambda d, i: usb.calls.append("release"),
        dispose_resources=disposed.append,
    )
    core = SimpleNamespace(find=lambda **kw: [usb, other], USBTimeoutError=Timeout)
    transport = PyUsbConsole("usb-1", core=core, util=util)
    with pytest.raises(Wmr100Error):
        transport.read_batch()
    assert disposed.count(usb) == disposed.count(other) == 1
    assert usb.calls == ["detach", "claim", "release", "attach"]
    assert transport.device is None
