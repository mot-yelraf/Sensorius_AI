# Hardware And GPIO

Direct hardware support is Raspberry Pi focused. macOS, Windows, and non-Pi
Linux deployments run Sensorius as MQTT hubs and do not use direct GPIO or
local sensor buses.

## Local Sensor Buses

| Purpose | GPIO Pin | Physical Pin | Notes |
| --- | --- | --- | --- |
| I2C_1 SDA | GPIO2 | Pin 3 | Default local sensor bus |
| I2C_1 SCL | GPIO3 | Pin 5 | Default local sensor bus |
| I2C_0 SDA1 | GPIO0 | Pin 27 | Plant probe bus for VPDPlant |
| I2C_0 SCL1 | GPIO1 | Pin 28 | Plant probe bus for VPDPlant |

Local sensor discovery only runs when the Python `board` runtime is available.
If it is missing, Sensorius skips local sensor discovery and continues as an
MQTT hub.

## Relay Configurations

Factory relay templates live under `switch_settings/factory/`.

| Template | Enable GPIO | Enable Physical | Channel | GPIO | Physical |
| --- | --- | --- | --- | --- | --- |
| `switch_1_relay.toml` | GPIO23 | Pin 16 | Switch 1 | GPIO26 | Pin 37 |
| `switch_2_relay.toml` | GPIO27 | Pin 13 | Switch 1 | GPIO26 | Pin 37 |
| `switch_2_relay.toml` | GPIO27 | Pin 13 | Switch 2 | GPIO20 | Pin 38 |
| `switch_3_relay.toml` | GPIO5 | Pin 29 | Switch 1 | GPIO26 | Pin 37 |
| `switch_3_relay.toml` | GPIO5 | Pin 29 | Switch 2 | GPIO20 | Pin 38 |
| `switch_3_relay.toml` | GPIO5 | Pin 29 | Switch 3 | GPIO21 | Pin 40 |

`saiSwitchFactory.detect_relay_board()` controls whether host switch settings
are materialized at startup. When no local relay board is detected, local relay
settings are skipped and remote Nodus switches can still operate.

## Switch Types

Sensorius supports:

- Local Raspberry Pi GPIO relay switches.
- Remote Nodus/Pico MQTT switches.

Both are exposed through the same switch controller interface and can be used
by the dashboard, automations, Home Assistant, and MQTT-backed workflows.

## Nodus Hardware

Nodus devices are managed over Wi-Fi and MQTT after onboarding. Sensorius uses:

- AP-mode HTTP only during bootstrap.
- Retained MQTT metadata for discovery and shadow settings.
- MQTT config, state, event, availability, heartbeat, and calibration topics
  for steady-state operation.

Nodus Wi-Fi uses 2.4 GHz. For Raspberry Pi onboarding, prefer a 2.4 GHz Pi
connection or ethernet when the router uses one combined SSID for both bands.
