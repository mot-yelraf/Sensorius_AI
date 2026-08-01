"""Manual AQI sensor smoke test on Raspberry Pi I2C buses 1 and 0.

This helper exercises the AQI sensor backend outside pytest so a developer can
confirm live hardware readings during bench testing.
"""

from __future__ import annotations

import time

from adafruit_extended_bus import ExtendedI2C
from adafruit_bme680 import Adafruit_BME680_I2C

I2C_BUSES = (1, 0)
BME680_ADDRS = (0x77, 0x76)


def _exercise_bus(bus_num: int) -> bool:
    """Try supported BME680 addresses on one explicit Linux I2C bus."""
    try:
        i2c = ExtendedI2C(bus_num)
    except Exception as exc:
        print(f"Could not open /dev/i2c-{bus_num}: {exc}")
        return False

    try:
        for address in BME680_ADDRS:
            try:
                bme680 = Adafruit_BME680_I2C(i2c, address=address)
                bme680.sea_level_pressure = 1013.25
                time.sleep(1)
                print(f"Detected BME680 at 0x{address:02X} on /dev/i2c-{bus_num}.")
                print(f"Temperature: {bme680.temperature}")
                print(f"Gas: {bme680.gas}")
                return True
            except Exception as exc:
                print(f"BME680 probe at 0x{address:02X} on /dev/i2c-{bus_num} failed: {exc}")
    finally:
        try:
            i2c.deinit()
        except Exception:
            pass
    return False


def main() -> int:
    print("Checking BME680 addresses 0x77 and 0x76 on Raspberry Pi I2C buses 1 and 0.")
    for bus_num in I2C_BUSES:
        if _exercise_bus(bus_num):
            return 0
    print("No supported AQI sensor found on I2C-1 or I2C-0.")
    print("Check wiring, sensor power, configured address, and whether both I2C buses are enabled.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
