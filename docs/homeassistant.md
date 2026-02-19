# Home Assistant Integration

Sensorius can publish MQTT discovery and state topics for Home Assistant.

## What It Does

- Connects to MQTT for Home Assistant integration
- Publishes discovery payloads for supported entities
- Publishes sensor/switch state updates
- Optionally mirrors Nodus traffic into the configured base topic

## Configure in Web UI

Open System Settings and configure Home Assistant:

- Enabled
- MQTT broker host
- MQTT port
- Username/password (optional, based on broker setup)

Advanced Home Assistant topic/retain controls are in `[HomeAssistant]` settings.

## `[HomeAssistant]` Keys

```toml
[HomeAssistant]
ENABLED = false
HA_USERNAME = ""
HA_PASSWORD = ""
HA_BROKER = ""
HA_MQTTPORT = 1883
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "sensorius"
PUBLISH_DISCOVERY_RETAIN = true
PUBLISH_STATE_RETAIN = true
PUBLISH_LEGACY_SENSOR_TOPIC = true
NODUS_PASSTHROUGH = false
MIRROR_NODUS = true
```

Notes:

- `ENABLED`: turns Home Assistant publish behavior on/off.
- `HA_BROKER` / `HA_MQTTPORT`: MQTT target used for HA publish path.
- `HA_USERNAME` / `HA_PASSWORD`: optional MQTT auth; password is obfuscated at rest.
- `DISCOVERY_PREFIX`: usually `homeassistant`.
- `BASE_TOPIC`: root topic for Sensorius-published state.
- `PUBLISH_DISCOVERY_RETAIN`: retain discovery payloads so HA can restore entities after restart.
- `PUBLISH_STATE_RETAIN`: retain state payloads on broker.
- `PUBLISH_LEGACY_SENSOR_TOPIC`: also publish legacy sensor topic format when enabled.
- `NODUS_PASSTHROUGH`: pass Nodus topics through directly.
- `MIRROR_NODUS`: mirror discovered Nodus data into Sensorius/HA topic space.

## Topic Behavior

- Discovery topics are published under `DISCOVERY_PREFIX`.
- State topics are published under `BASE_TOPIC`.
- Retain behavior is controlled independently for discovery vs state.

## Runtime Route

- `POST /submit-homeassistant-settings`: saves Home Assistant settings from the web UI.

## Troubleshooting

- No entities in Home Assistant: verify `ENABLED`, broker host/port, and credentials.
- Entities appear then disappear: ensure `PUBLISH_DISCOVERY_RETAIN = true`.
- State not restoring after restart: ensure `PUBLISH_STATE_RETAIN = true`.
- Duplicate or unexpected topics: review `BASE_TOPIC`, `NODUS_PASSTHROUGH`, and `MIRROR_NODUS`.
