# Sensors And Metrics

Sensorius supports local Raspberry Pi sensors, MQTT-discovered Nodus sensors,
optional WeeWX station ingest, and read-only Ecowitt LAN gateway polling. All readings are normalized into the same
database and dashboard model.

Each sensor defines a `measurements` list that determines the metric names
written to `readings` in `sensorius_data.db`. Timestamps are stored with an
epoch column for comparisons and localized in the UI using `[Time].TZ`.

## Sensor Sources

Local Raspberry Pi sensors:

- Detected by `sensorius.saiSensorFactory.find_sensors(...)` when the `board` runtime is
  available.
- Materialized under `sensor_settings/<sensor_id>/sensor.toml`.
- Read by `SensorController.data_collection`.

Remote Nodus sensors:

- Discovered from retained `nodus/<device_id>/meta`.
- Runtime data arrives on Nodus data topics.
- Local settings are shadow copies of device metadata and patches.

WeeWX station ingest:

- Optional archive or MQTT ingest.
- Materializes a station sensor config when enabled.
- Copies the WeeWX station model, station type, and driver into the station
  sensor config when a readable WeeWX config exists on the host.
- Adds station metrics to the same dashboard and DB paths.

Ecowitt gateway ingest:

- Discovers GW1100 and GW1200 gateways, plus other compatible Ecowitt gateways,
  and their registered sensors through the local generic HTTP API. GW1200
  support includes WH65/WS69-class traditional-rain arrays; the Ambient Weather
  WS-2000 outdoor array is sold as a WH65B and must use the same regional RF
  frequency as the gateway (915 MHz in North America).
- Uses `ecowitt-<gateway_mac>` as one stable station identity and marks the
  sensor settings as `TYPE = "station"`, `DEVICE = "ecowitt"`.
- Normalizes temperature, humidity, pressure, wind, rain, solar/light, UV,
  lightning, gateway indoor, air-quality, and supported additional channel
  arrays.
- Uses the units declared by each gateway response. Gateway-local units can
  differ from Ecowitt app display preferences; wind observations are normalized
  into Sensorius's canonical mph metrics. `Wind Direction` remains a separate
  degree metric used by the compass and wind-rose views; the combined card's
  current reading and statistics show wind speed, matching WeeWX behavior.
- Derives `Humidity` (absolute humidity in g/m³) and `Ambient VPD` (kPa) from
  the outdoor `Temperature` and `Rel-Humidity` observations before the complete
  reading set is written to SQLite.
- Uses distinct channel metric names such as `WH31 CH1 Temperature_F`,
  `Soil Moisture CH3`, `PM2.5 CH2`, and `Leaf Wetness CH1`.
- Stores Ecowitt rain day/week/month/year values as cumulative metrics. Only a
  restart-safe day-total delta is written as interval `Rain`, allowing the
  logger to derive `Rain Last 24h` correctly.
- Makes observed Ecowitt metrics available behind the dashboard sensor-row
  expander. **Pick 6** initially shows the configured six-card summary, while
  **All** initially expands both standard weather metrics and supported
  channel-numbered metrics without requiring a Nodus metric schema.
- Reports dashboard connection state from the supervised Ecowitt poller, with
  recent stored readings as a fallback when service state is unavailable.
- Treats the GW1200/WH65B metric mapping as provisional until verified against
  physical Ambient Weather hardware. Expected fields are outdoor temperature
  and relative humidity, wind speed/direction/gust, traditional rain, solar
  radiation or light, and UV index. Unrecognized fields are ignored safely and
  can be added after a real gateway response is inspected.

## Common Metrics

Metric names are compatibility-sensitive because they are stored in SQLite,
used by dashboards, and referenced by automations.

Common environmental metrics:

- `Temperature` - degrees C.
- `Temperature_F` - degrees F.
- `Rel-Humidity` - relative humidity percent.
- `Humidity` - absolute humidity, g/m3.
- `CO2` - ppm.
- `Ambient VPD` - kPa.
- `Plant VPD` - kPa.
- `Dew-Point` or `Dew Point` - degrees C, depending on sensor module.
- `Dew-Point_F` or `Dew Point_F` - degrees F.
- `Dewpoint Depression` or `Dew Point Deficit` - degrees C.
- `DewVPD Risk` - percent.
- `Baro-Pressure`, `Plant Baro-Pressure`, or legacy `Bar-Pressure` - hPa,
  normalized and displayed at `0.1 hPa` resolution.
- `Air Quality` - derived AQI.
- `Equivalent CO2` - SGP30 equivalent-CO2 estimate in ppm.
- `TVOC` - SGP30 total volatile organic compounds in ppb.
- `VOC Index` - SGP40 or SGP41 VOC gas index, 0 through 500.
- `NOx Index` - SGP41 NOx gas index, 0 through 500. SGP40 does not measure
  or derive this metric.

Soil metrics:

- `Soil-Moisture` - corrected volumetric moisture percent.
- `SMD` - soil moisture deficit percent.
- `SSI` - soil stress index percent.
- `Soil-Temp` - degrees C.
- `Soil-Temp_F` - degrees F.
- `Soil-pH` - pH.
- `Soil-EC` - mS/cm.
- `Soil Nitrogen`, `Soil Phosphorus`, and `Soil Potassium` - mg/kg readings
  from 7-in-1 soil probes.
- `Soil Fertility Index` - derived NPK sufficiency score, percent.

WeeWX metrics are defined by `sensorius/sensor_modules/station_weewx.py`. The logger can
derive rolling `Rain Last 24h` from interval `Rain` readings. WeeWX MQTT
single-field replays are treated as incremental updates so repeated station
fields do not multiply interval rainfall totals.

## Supported Local Sensor Modules

`AQISensor`:

- Hardware: BME680.
- Bus: I2C_1, GPIO2 SDA and GPIO3 SCL.
- Typical metrics: temperature, humidity, air quality, VPD, dew point,
  dew-risk, and pressure.

`CO2Sensor`:

- Hardware: SCD30 or SCD4x.
- Bus: I2C_1.
- Typical metrics: CO2, temperature, humidity, VPD, dew point, and dew-risk.

`SGPSensor`:

- Hardware: SGP30 at `0x58`, or SGP40/SGP41 at `0x59`.
- Bus: I2C_1 or I2C_0.
- Metrics: SGP30 publishes `Equivalent CO2` and `TVOC`; SGP40 publishes only
  `VOC Index`; SGP41 publishes `VOC Index` and `NOx Index`.
- The gas algorithms are serviced on a fixed one-second period, including the
  time spent inside the hardware driver. Sensorius emits the latest values to
  normal persistence at the one-minute sensor cadence.
- When another directly connected sensor provides valid `Temperature` and
  `Rel-Humidity` readings, Sensorius uses a same-location sensor first and
  supplies those values to the SGP humidity-compensation interface.

`VPDSensor`:

- Hardware: BME280.
- Bus: I2C_1.
- Typical metrics: temperature, humidity, ambient VPD, dew point, dew-risk,
  and pressure.

`AHTSensor`:

- Hardware: AHT10/AHTx0.
- Bus: I2C_1.
- Typical metrics: temperature, humidity, ambient VPD, dew point, and
  dew-risk.

`VPDPlantSensor`:

- Hardware: dual BME280.
- Ambient bus: I2C_1.
- Plant probe bus: I2C_0, GPIO0 SDA1 and GPIO1 SCL1.
- Typical metrics: ambient readings plus plant temperature, plant humidity,
  plant VPD, plant dew point, and plant pressure.

Soil sensor:

- Hardware: UART/Modbus soil sensor.
- Typical metrics: soil moisture, SMD, SSI, soil temperature, pH, EC, and
  optional 7-in-1 NPK fertility metrics.

## Calibration

Calibration data is stored in the sensor settings file:

- `[Calibration.Device]`: per-device offsets, including soil-specific offsets.
- `[Calibration.System]`: reference-sensor/system calibration metadata.

Local calibration updates are persisted and hot-reloaded into the running
sensor when supported. Remote Nodus calibration uses MQTT calibration commands,
acks, results, and correlated metadata patches.

## Display Settings

The `[Display]` section stores the selected dashboard metrics. The UI supports:

- Pick-six display mode.
- All-metric display mode.
- Per-metric display style overrides under `[Display.Style]`.

Gas gauges use the same scale bands and color palette as the Nodus web UI.
After direct hardware identification, Sensorius removes display metrics that
the installed SGP model cannot provide.

Do not rename stored metric keys casually. Existing database history,
automation rules, Home Assistant entities, and display settings can depend on
the exact strings.
