# Hardware and GPIO

This guide captures hardware mapping and switch support details originally documented in `README.md`.

## GPIO Pin Assignments

### Supported Relay Configurations (from `switch_settings/factory/`)

| Configuration         | Enable GPIO (Physical) | Switch   | GPIO (Physical) |
| --------------------- | ---------------------- | -------- | --------------- |
| `switch_1_relay.toml` | GPIO23 (Pin 16)        | Switch 1 | GPIO26 (Pin 37) |
| `switch_2_relay.toml` | GPIO27 (Pin 13)        | Switch 1 | GPIO26 (Pin 37) |
| `switch_2_relay.toml` |                        | Switch 2 | GPIO20 (Pin 38) |
| `switch_3_relay.toml` | GPIO5 (Pin 29)         | Switch 1 | GPIO26 (Pin 37) |
| `switch_3_relay.toml` |                        | Switch 2 | GPIO20 (Pin 38) |
| `switch_3_relay.toml` |                        | Switch 3 | GPIO21 (Pin 40) |

### Sensor I2C Pins

| Purpose             | GPIO Pin | Physical Pin | Notes                         |
| ------------------- | -------- | ------------ | ----------------------------- |
| I2C_1 SDA (Sensor)  | GPIO2    | Pin 3        | Default I2C bus               |
| I2C_1 SCL (Sensor)  | GPIO3    | Pin 5        |                               |
| I2C_0 SDA1 (Plant)  | GPIO0    | Pin 27       | Dedicated for VPDPlant sensor |
| I2C_0 SCL1 (Plant)  | GPIO1    | Pin 28       |                               |

## Supported Switches

Sensorius supports:

- Directly connected switches (up to 3 relays)
- Nodus devices with switches enabled

Relay-capable configurations include:

- Single relay (individual relay control)
- 1-relay hat configuration
- 2-relay hat configuration
- 3-relay hat configuration

Switch channels are exposed in the UI and can be controlled manually, by automation rules, or by MQTT-connected workflows.
