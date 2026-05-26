# Home Assistant Integration

Sensorius publishes MQTT discovery, state, availability, and switch command
topics for Home Assistant through `saiHomeAssistantMqtt.py`.

## Runtime Flow

1. Configure `[SensorNetwork]` so Sensorius can connect to the Nodus broker.
2. Enable `[HomeAssistant]`.
3. `saiMQTTIngest` connects to the MQTT broker.
4. `rPiHomeAssistantBridge` waits for the HA MQTT connection.
5. The bridge installs command handlers and publishes retained discovery.
6. New database readings and switch events publish retained HA state updates.
7. HA switch commands route back through the shared switch controller path.

Home Assistant discovery is built from:

- Sensor settings plus metrics found in `readings`.
- Switch identities found in `switch_ids`.
- Runtime switch controllers when available.

## Settings

```toml
[HomeAssistant]
ENABLED = false
HA_USERNAME = ""
HA_PASSWORD = ""
HA_BROKER = ""
HA_MQTTPORT = 1883
USE_TLS = false
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "sensorius"
PUBLISH_DISCOVERY_RETAIN = true
PUBLISH_STATE_RETAIN = true
PUBLISH_LEGACY_SENSOR_TOPIC = true
NODUS_PASSTHROUGH = false
MIRROR_NODUS = true
```

Key behavior:

- `ENABLED`: turns the bridge on.
- `HA_BROKER` and `HA_MQTTPORT`: broker used for HA publish and command
  traffic. If blank, Sensorius uses the primary broker.
- `HA_USERNAME` and `HA_PASSWORD`: optional broker auth. Password is obfuscated
  at rest.
- `DISCOVERY_PREFIX`: usually `homeassistant`.
- `BASE_TOPIC`: root for Sensorius state and command topics, usually
  `sensorius`.
- `PUBLISH_DISCOVERY_RETAIN`: retain discovery payloads so HA can restore
  entities after restart.
- `PUBLISH_STATE_RETAIN`: retain state payloads.
- `PUBLISH_LEGACY_SENSOR_TOPIC`: keeps legacy sensor topic behavior enabled.
- `NODUS_PASSTHROUGH`: keeps direct Nodus topic subscription behavior enabled.
- `MIRROR_NODUS`: mirrors Nodus traffic to the HA broker when configured.

## Topic Shape

Sensor state:

```text
<base_topic>/sensor/<sensor_id>/state
<base_topic>/sensor/<sensor_id>/availability
```

Switch state and commands:

```text
<base_topic>/switch/<switch_id>/<channel_id>/state
<base_topic>/switch/<switch_id>/<channel_id>/set
<base_topic>/switch/<switch_id>/availability
```

Discovery:

```text
<discovery_prefix>/sensor/<node_id>/<object_id>/config
<discovery_prefix>/switch/<node_id>/<object_id>/config
```

## Operations

After enabling HA:

1. Restart Sensorius if the broker connection changed.
2. Verify Sensorius MQTT ingest is connected.
3. Confirm retained discovery exists under `homeassistant/.../config`.
4. Restart Home Assistant or reload MQTT entities if discovery was missed.

Use retained discovery and retained state unless there is a specific broker
policy reason not to.

## Troubleshooting

No entities:

- Confirm `HomeAssistant.ENABLED = true`.
- Confirm `DISCOVERY_PREFIX` matches Home Assistant's MQTT integration.
- Confirm broker credentials.
- Confirm retained discovery publish is enabled.
- Confirm there is at least one sensor reading or switch identity to publish.

Switch command does not work:

- Confirm HA is publishing to `<base>/switch/<switch_id>/<channel_id>/set`.
- Confirm Sensorius has a live switch controller or remote ingest mapping.
- Confirm the channel ID matches `SWITCH_N_CHANNEL_ID`.
- Confirm no enabled Advanced automation blocks manual toggles.

Duplicate entities:

- Review `BASE_TOPIC`, `DISCOVERY_PREFIX`, `PUBLISH_LEGACY_SENSOR_TOPIC`,
  `NODUS_PASSTHROUGH`, and `MIRROR_NODUS`.
- Avoid changing `BASE_TOPIC`, node ID, switch IDs, or channel IDs after HA has
  already discovered entities unless you intentionally want new entity IDs.
