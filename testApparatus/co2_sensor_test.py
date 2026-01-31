import board
import busio
from smbus2 import SMBus
import board
import busio
import adafruit_scd30

import time
try:
    # lets us open specific /dev/i2c-* numbers if we want to later
    from adafruit_extended_bus import ExtendedI2C as ExtI2C
except Exception:
    ExtI2C = None
try:
    i2c0 = ExtI2C(0)
except Exception:
    ExtI2C = None
    exit
i2c = busio.I2C(scl=board.SCL, sda=board.SDA)

scd30 = adafruit_scd30.SCD30(i2c)

time.sleep(10)
reading = scd30.temperature
print(f"Temperature: {reading}")
reading = scd30.CO2
print(f"CO2: {reading}")
