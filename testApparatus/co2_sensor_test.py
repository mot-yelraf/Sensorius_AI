"""Manual CO2 sensor smoke test for Raspberry Pi I2C bus 1.

Probes both supported CO2 sensors on GPIO2/GPIO3 (SDA/SCL):
- SCD4x at address 0x62
- SCD30 at address 0x61
"""

from __future__ import annotations

import time

import board
import busio
from smbus2 import SMBus

SCD4X_ADDR = 0x62
SCD30_ADDR = 0x61
CO2_ADDRS = (SCD4X_ADDR, SCD30_ADDR)
adafruit_scd30_import_error = None
adafruit_scd4x_import_error = None

try:
    import adafruit_scd30
except Exception as exc:
    adafruit_scd30 = None
    adafruit_scd30_import_error = exc

try:
    import adafruit_scd4x
except Exception as exc:
    adafruit_scd4x = None
    adafruit_scd4x_import_error = exc


def _wait_for_data_ready(sensor, timeout_s: float = 20.0, interval_s: float = 0.5) -> bool:
    """Wait until the sensor reports data-ready or timeout expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if hasattr(sensor, "data_ready") and sensor.data_ready:
                return True
            if hasattr(sensor, "data_available") and sensor.data_available:
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def _print_readings(sensor_name: str, sensor) -> None:
    """Print CO2/temperature/humidity readings for whichever sensor was detected."""
    co2 = getattr(sensor, "CO2", None)
    temp_c = getattr(sensor, "temperature", None)
    rh = getattr(sensor, "relative_humidity", None)
    print(f"[{sensor_name}] CO2: {co2} ppm")
    print(f"[{sensor_name}] Temperature: {temp_c} C")
    print(f"[{sensor_name}] Relative Humidity: {rh} %")


def _scan_i2c_bus(bus_num: int = 1) -> set[int]:
    """Return 7-bit I2C addresses that respond to SMBus read_byte."""
    found: set[int] = set()
    try:
        with SMBus(bus_num) as bus:
            for addr in range(0x03, 0x78):
                try:
                    bus.read_byte(addr)
                    found.add(addr)
                except OSError:
                    continue
    except Exception as exc:
        print(f"Could not scan /dev/i2c-{bus_num}: {exc}")
    return found


def _probe_co2_addrs(bus_num: int = 1) -> dict[int, list[str]]:
    """Probe CO2 addresses with read_byte and write_quick for i2cdetect parity."""
    results: dict[int, list[str]] = {addr: [] for addr in CO2_ADDRS}
    try:
        with SMBus(bus_num) as bus:
            for addr in CO2_ADDRS:
                for op_name in ("read_byte", "write_quick"):
                    op = getattr(bus, op_name, None)
                    if not callable(op):
                        continue
                    try:
                        op(addr)
                        results[addr].append(op_name)
                    except OSError:
                        continue
                    except Exception as exc:
                        print(f"Probe {op_name} at 0x{addr:02X} failed unexpectedly: {exc}")
    except Exception as exc:
        print(f"Could not run targeted CO2 probe on /dev/i2c-{bus_num}: {exc}")
    return results


def main() -> int:
    print("Using Raspberry Pi I2C bus 1 on GPIO2 (SDA) / GPIO3 (SCL).")
    found_addrs = _scan_i2c_bus(1)
    if found_addrs:
        formatted = ", ".join(f"0x{addr:02X}" for addr in sorted(found_addrs))
        print(f"SMBus read_byte scan on /dev/i2c-1: {formatted}")
    else:
        print("No I2C devices responded to SMBus read_byte on /dev/i2c-1.")
        print("Check wiring, sensor power, and whether Raspberry Pi I2C is enabled.")

    co2_probe = _probe_co2_addrs(1)
    for addr in CO2_ADDRS:
        ops = co2_probe.get(addr) or []
        detail = ", ".join(ops) if ops else "no response"
        print(f"CO2 targeted probe 0x{addr:02X}: {detail}")

    i2c = busio.I2C(scl=board.SCL, sda=board.SDA)

    # Probe order matches runtime preference: SCD4x first, then SCD30.
    if adafruit_scd4x is not None:
        try:
            scd4x = adafruit_scd4x.SCD4X(i2c)
            scd4x.start_periodic_measurement()
            print("Detected SCD4x at 0x62. Waiting for first sample...")
            if _wait_for_data_ready(scd4x, timeout_s=20.0):
                _print_readings("SCD4x", scd4x)
                return 0
            print("SCD4x detected but no data-ready sample arrived before timeout.")
        except Exception as exc:
            print(f"SCD4x probe failed: {exc}")
    else:
        print(f"SCD4x driver import failed: {adafruit_scd4x_import_error}")

    if adafruit_scd30 is not None:
        try:
            scd30 = adafruit_scd30.SCD30(i2c)
            print("Detected SCD30 at 0x61. Waiting for first sample...")
            if _wait_for_data_ready(scd30, timeout_s=20.0):
                _print_readings("SCD30", scd30)
                return 0
            print("SCD30 detected but no data-ready sample arrived before timeout.")
        except Exception as exc:
            print(f"SCD30 probe failed: {exc}")
    else:
        print(f"SCD30 driver import failed: {adafruit_scd30_import_error}")

    print("No supported CO2 sensor found. Checked SCD4x (0x62) and SCD30 (0x61) on I2C(1).")
    if not any(co2_probe.get(addr) for addr in CO2_ADDRS):
        print("Neither 0x61 nor 0x62 responded during targeted CO2 probing.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
