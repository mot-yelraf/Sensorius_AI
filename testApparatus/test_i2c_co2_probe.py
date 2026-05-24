"""Coverage for CO2 I2C detection when Blinka scans miss SCD4x."""

import os
import sys
from types import SimpleNamespace


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiSensorFactory
from sensor_modules import base as sensor_base


class _FakeScanI2C:
    def __init__(self, addrs):
        self.addrs = set(addrs)
        self.unlocked = False
        self.deinited = False

    def try_lock(self):
        return True

    def scan(self):
        return list(self.addrs)

    def unlock(self):
        self.unlocked = True

    def deinit(self):
        self.deinited = True


def _smbus_module_with_visible(visible=None, quick_visible=None):
    visible = set(visible or set())
    quick_visible = set(quick_visible or set())

    class _FakeSMBus:
        def __init__(self, bus_num):
            self.bus_num = bus_num

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read_byte(self, addr):
            if (self.bus_num, addr) not in visible:
                raise OSError("no response")
            return 0

        def write_quick(self, addr):
            if (self.bus_num, addr) not in quick_visible:
                raise OSError("no response")

    return SimpleNamespace(SMBus=_FakeSMBus)


def test_startup_scan_merges_smbus_co2_probe(monkeypatch):
    monkeypatch.setattr(saiSensorFactory, "_last_scan", None)
    monkeypatch.setattr(saiSensorFactory, "ExtI2C", None)
    monkeypatch.setitem(sys.modules, "board", SimpleNamespace(SCL=object(), SDA=object()))
    monkeypatch.setitem(
        sys.modules,
        "busio",
        SimpleNamespace(I2C=lambda _scl, _sda: _FakeScanI2C({0x76})),
    )
    monkeypatch.setitem(sys.modules, "smbus2", _smbus_module_with_visible({(1, 0x62)}))

    scan = saiSensorFactory._scan_pi_i2c_busses()

    assert scan["i2c-1"] == {0x62, 0x76}
    assert scan["i2c-0"] == set()


def test_runtime_find_sensor_bus_uses_smbus_probe_when_scan_misses_co2(monkeypatch):
    class _FakeExtendedI2C(_FakeScanI2C):
        def __init__(self, bus_num):
            super().__init__({0x76})
            self.bus_num = bus_num

    monkeypatch.setitem(
        sys.modules,
        "adafruit_extended_bus",
        SimpleNamespace(ExtendedI2C=_FakeExtendedI2C),
    )
    monkeypatch.setitem(sys.modules, "smbus2", _smbus_module_with_visible({(1, 0x62)}))

    i2c = sensor_base.find_sensor_bus(address=0x62, delay=0, buses=(1,), lock_timeout=0.01)

    assert isinstance(i2c, _FakeExtendedI2C)
    assert i2c.bus_num == 1
    assert i2c.unlocked is True
    assert i2c.deinited is False


def test_startup_scan_uses_smbus_quick_when_read_byte_misses_co2(monkeypatch):
    monkeypatch.setattr(saiSensorFactory, "_last_scan", None)
    monkeypatch.setattr(saiSensorFactory, "ExtI2C", None)
    monkeypatch.setitem(sys.modules, "board", SimpleNamespace(SCL=object(), SDA=object()))
    monkeypatch.setitem(
        sys.modules,
        "busio",
        SimpleNamespace(I2C=lambda _scl, _sda: _FakeScanI2C({0x76})),
    )
    monkeypatch.setitem(
        sys.modules,
        "smbus2",
        _smbus_module_with_visible(quick_visible={(1, 0x62)}),
    )

    scan = saiSensorFactory._scan_pi_i2c_busses()

    assert scan["i2c-1"] == {0x62, 0x76}


def test_runtime_find_sensor_bus_uses_smbus_quick_when_read_byte_misses_co2(monkeypatch):
    class _FakeExtendedI2C(_FakeScanI2C):
        def __init__(self, bus_num):
            super().__init__({0x76})
            self.bus_num = bus_num

    monkeypatch.setitem(
        sys.modules,
        "adafruit_extended_bus",
        SimpleNamespace(ExtendedI2C=_FakeExtendedI2C),
    )
    monkeypatch.setitem(
        sys.modules,
        "smbus2",
        _smbus_module_with_visible(quick_visible={(1, 0x62)}),
    )

    i2c = sensor_base.find_sensor_bus(address=0x62, delay=0, buses=(1,), lock_timeout=0.01)

    assert isinstance(i2c, _FakeExtendedI2C)
    assert i2c.bus_num == 1
