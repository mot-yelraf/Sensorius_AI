"""Manual AQI sensor smoke test script for local hardware validation.

This helper exercises the AQI sensor backend outside pytest so a developer can
confirm live hardware readings during bench testing.
"""

import board
import busio
from smbus2 import SMBus
from adafruit_bme680 import Adafruit_BME680_I2C
import time

i2c = busio.I2C(scl=board.SCL, sda=board.SDA)
bme680 = Adafruit_BME680_I2C(i2c, address=0x77)

bme680.sea_level_pressure = 1013.25  # Optional, for altitude compensation


time.sleep(1)
reading = bme680.temperature
print(f"Temperature: {reading}")
reading = bme680.gas
print(f"Gas: {reading}")

"""
i2c_0 = SMBus(0)
try:
    aqi = adafruit_sgp40.SGP40(i2c_0)
except Exception as e:
    print(f"AQI Exception: {e}")

time.sleep(1)
reading = aqi.raw
print(f"spg40 raw: {reading}")

"""
