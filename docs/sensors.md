# Sensors and Metrics

This guide captures the supported sensor and measurement details originally documented in `README.md`.

Each sensor defines its own `self.measurements` list, which determines the exact metrics written to the database. Each metric is timestamped and stored in `sensor_data.db`.

## AQISensor (based on BME680)

- I2C Bus: I2C_1 (GPIO2/SDA, GPIO3/SCL)
- Metrics Stored:
- `Temperature` - C
- `Temperature_F` - F
- `Rel-Humidity` - % (relative)
- `Humidity` - g/m3 (absolute)
- `Air Quality` - AQI (derived from gas resistance)
- `Ambient VPD` - kPa
- `Dew-Point` - C
- `Dew-Point_F` - F
- `Dewpoint Depression` - C
- `DewVPD Risk` - %
- `Baro-Pressure` - hPa

## CO2Sensor (based on SCD30 or SCD4x)

- I2C Bus: I2C_1 (GPIO2/SDA, GPIO3/SCL)
- Metrics Stored:
- `CO2` - ppm
- `Temperature` - C
- `Temperature_F` - F
- `Rel-Humidity` - % (relative)
- `Humidity` - g/m3 (absolute)
- `Ambient VPD` - kPa
- `Dew-Point` - C
- `Dew-Point_F` - F
- `Dewpoint Depression` - C
- `DewVPD Risk` - %

## VPDSensor (based on BME280)

- I2C Bus: I2C_1 (GPIO2/SDA, GPIO3/SCL)
- Metrics Stored:
- `Temperature` - C
- `Temperature_F` - F
- `Rel-Humidity` - % (relative)
- `Humidity` - g/m3 (absolute)
- `Ambient VPD` - kPa
- `Dew-Point` - C
- `Dew-Point_F` - F
- `Dewpoint Depression` - C
- `DewVPD Risk` - %
- `Bar-Pressure` - hPa

## VPDPlantSensor (dual BME280 on I2C_1 and I2C_0)

- I2C Buses:
- Ambient: I2C_1 (GPIO2/SDA, GPIO3/SCL)
- Plant Probe: I2C_0 (GPIO0/SDA1, GPIO1/SCL1)
- Metrics Stored:
- `Temperature` - C (ambient)
- `Temperature_F` - F
- `Rel-Humidity` - %
- `Humidity` - g/m3
- `Ambient VPD` - kPa
- `Dew-Point` - C
- `Dew-Point_F` - F
- `Dewpoint Depression` - C
- `DewVPD Risk` - %
- `Baro-Pressure` - hPa

Plant probe additions (I2C_0):

- `Temperature Plant` - C
- `Temperature_F Plant` - F
- `Rel-Humidity Plant` - %
- `Humidity Plant` - g/m3
- `Plant VPD` - kPa
- `Plant Dew-Point` - C
- `Plant Dew-Point_F` - F
- `Plant Dewpoint Depression` - C
- `Plant DewVPD Risk` - %
- `Baro-Pressure Plant` - hPa

## SoilSensor (UART/Modbus soil sensor)

- Bus: UART / Modbus RTU
- Metrics Stored:
- `Soil-Moisture` - % volumetric water content after calibration/correction
- `SMD` - % soil moisture deficit, derived from corrected `Soil-Moisture`
- `SSI` - % soil stress index, derived from `SMD` and corrected `Soil-Temp`
- `Soil-Temp` - C
- `Soil-Temp_F` - F
- `Soil-pH` - pH
- `Soil-EC` - mS/cm

Derived soil metrics:

- `SMD` is a normalized dryness percentage, not an independent raw sensor register.
- `SMD` maps moisture at or above the wet threshold to `0%` and moisture at or below the dry threshold to `100%`.
- Values between the wet and dry thresholds scale linearly and are clamped to `0-100%`.
- If the wet threshold is less than or equal to the dry threshold, `SMD` is not reported.
- `SSI` is a normalized soil concern percentage that combines `SMD` with temperature-based stress from corrected `Soil-Temp`.
- The temperature contribution is `0%` inside the configured OK band and scales toward `100%` at the configured low/high critical edges.
- `SSI` uses configurable moisture and temperature weights to blend the two contributions into a single `0-100%` index.
- If the configured temperature band is invalid or the total weight is zero, `SSI` is not reported.

All timestamps are in UTC. `tz`, `tzOffset`, and `tzName` are pushed to the device and used in the UI to localize time.
