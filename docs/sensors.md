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

All timestamps are in UTC. `tz`, `tzOffset`, and `tzName` are pushed to the device and used in the UI to localize time.
