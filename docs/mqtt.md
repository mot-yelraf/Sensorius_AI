# MQTT

The device can publish sensor metrics and receive switch commands over MQTT to a single MQTT Broker. Sensorius (Automatio Instrumentorum) is the primary broker Nodus was developed for and supports device discovery. Home Assistant is an option for the MQTT Broker.

## Behavior

- Valid Nodus `ACTIVE_PROFILE` values are `nodusweb`, `sensorius`, `homeassistant`, and `weewx`.
- By default MQTT uses port 1883 on both Sensorius AI and Home Assistant. 
- Sensorius AI uses anonymous access; Home Assistant requires a username and password.
- TLS is enabled when configured or when broker port is 8883.
- Home Assistant discovery can be enabled with configurable prefixes.
- Switch control is handled via `/set` topics.
- Events and state are published to `/event` and `/state` topics.
- Runtime identity metadata is published as a retained payload on `nodus/<device_id>/meta` (`schema = "nodus-meta/v1"`).
- `nodusweb` is the web-enabled Nodus runtime profile and replaces the older `standalone` profile name.
- When `ACTIVE_PROFILE = "homeassistant"` or `ACTIVE_PROFILE = "weewx"`, Nodus does not start the normal-mode webserver.
- Provisioning for these MQTT-only profiles is expected to happen in `nodusweb`/AP mode before rebooting into the target profile.

## Core Topic Contract (Sensorius)

- Device heartbeat:
  - `nodus/<device_id>/status/heartbeat` (retained online/offline status envelope)
- Sensor data:
  - `nodus/<sensor_id>/data`
- Sensor availability:
  - `nodus/<sensor_id>/availability` (retained online/offline)
- Switch channels:
  - `nodus/<channel_id>/event`
  - `nodus/<channel_id>/state` (retained `ON`/`OFF`)
  - `nodus/<channel_id>/set` (command topic consumed by Nodus)
  - `nodus/<channel_id>/availability` (retained online/offline)
- Runtime metadata:
  - `nodus/<device_id>/meta` (retained; includes location, channel IDs, labels, and per-channel topics)

## `nodus-meta/v1` Payload

Published retained at `nodus/<device_id>/meta` after successful MQTT connect/reconnect.

```json
{
  "schema": "nodus-meta/v1",
  "device_id": "aqi-x943fm",
  "hostname": "aqi-x943fm",
  "serial": "x943fm",
  "type": "nodus",
  "version": "vX.Y.Z",
  "capabilities": {
    "sensor": true,
    "switch": true
  },
  "sensor": {
    "sensor_id": "aqi-x943fm",
    "location": "TestLab",
    "display_metrics": ["Air Quality", "Temperature", "Rel-Humidity"],
    "data_topic": "nodus/aqi-x943fm/data",
    "event_topic": "nodus/aqi-x943fm/event",
    "availability_topic": "nodus/aqi-x943fm/availability"
  },
  "status": {
    "heartbeat_topic": "nodus/aqi-x943fm/status/heartbeat"
  },
  "switch": {
    "device_id": "switch-x943fm",
    "location": "TestLab",
    "channels": [
      {
        "index": 1,
        "label": "Fan",
        "channel_id": "S1-x943fm",
        "state": false,
        "event_topic": "nodus/S1-x943fm/event",
        "state_topic": "nodus/S1-x943fm/state",
        "set_topic": "nodus/S1-x943fm/set",
        "availability_topic": "nodus/S1-x943fm/availability"
      },
      {
        "index": 2,
        "label": "Light",
        "channel_id": "S2-x943fm",
        "state": false,
        "event_topic": "nodus/S2-x943fm/event",
        "state_topic": "nodus/S2-x943fm/state",
        "set_topic": "nodus/S2-x943fm/set",
        "availability_topic": "nodus/S2-x943fm/availability"
      }
    ]
  },
  "location_group": {
    "location": "TestLab",
    "members": ["aqi-x943fm", "S1-x943fm", "S2-x943fm"]
  },
  "timestamp": 1763859546
}
```

### Sensorius Consumption Notes

- Prefer MQTT metadata (`nodus/<device_id>/meta`) over `/itaot-meta` for steady-state discovery/materialization.
- Use `switch.channels[*].channel_id` + topic fields as source of truth for switch tile creation.
- Group sensor and switch tiles by `location_group.location` (fallback: `switch.location`, then `sensor.location`).
- `/itaot-meta` remains optional diagnostic enrichment and should not be required for online device rendering.

## Notes

- Keep publish intervals conservative to reduce power usage.
- If MQTT is disabled, the device still runs locally under the `nodusweb` profile.
- Calibration MQTT contract for Sensorius integration: see `docs/calibration_mqtt_contract.md`.
