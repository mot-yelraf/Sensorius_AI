# Nodus II Contract

This document is the canonical forward-only contract between current
`cPyNodus_II` firmware and current Sensorius.

The two projects are developed together. Older MQTT topic shapes and fallback
parsers may still exist in code for transitional reasons, but they are not the
contract. When docs disagree, this document wins.

## Principles

- AP bootstrap is handled only by `/itaot-meta` and `/itaot-init`.
- Normal runtime sync is handled only by MQTT.
- Nodus publishes a retained full `meta` snapshot on connect/reconnect.
- After accepted runtime changes, Nodus publishes only `meta/patch`.
- Sensorius sends ordinary runtime config writes one key at a time per
  physical Nodus host and waits for `ack` and successful `result` before
  sending the next queued update.

## AP Bootstrap

Nodus in AP mode exposes:

- `GET /itaot-meta`
- `POST /itaot-init`

`/itaot-meta` is used by Sensorius before bootstrap so the device can provide
its own identity.

Canonical `/itaot-init` request:

```json
{
  "onboard_token": "token-123",
  "ssid": "MyWiFi",
  "password": "my-password",
  "hostname": "co2-ykdvea",
  "mqtt": {
    "broker_host": "samhain.local",
    "broker_port": 1883
  }
}
```

Canonical `/itaot-init` success response:

```json
{
  "accepted": true,
  "rebooting": true
}
```

Bootstrap payload rules:

- `onboard_token`, `ssid`, `password`, `hostname`, `mqtt.broker_host`, and
  `mqtt.broker_port` are required.
- If `mqtt.active_profile` is omitted but `mqtt.broker_host` is present,
  Nodus should infer `ACTIVE_PROFILE = "sensorius"`.
- Onboarding protocol state must stay out of persistent TOML schema.

## MQTT Topics

### Startup and steady state

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<sensor_id>/availability`
- `nodus/<sensor_id>/data`
- `nodus/<channel_id>/state`
- `nodus/<channel_id>/availability`

### Onboarding

- `nodus/<device_id>/onboard/hello`
- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`

### Ordinary runtime config

- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`
- `nodus/<device_id>/meta/patch`

### Switch runtime config

- `nodus/<channel_id>/config/set`
- `nodus/<channel_id>/config/ack`
- `nodus/<channel_id>/config/result`
- retained `nodus/<channel_id>/state`
- `nodus/<device_id>/meta/patch`

### Calibration

- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/meta/patch`

## Onboarding MQTT Flow

1. Nodus joins Wi-Fi and MQTT.
2. Nodus publishes `nodus/<device_id>/onboard/hello`.
3. Sensorius validates the token and publishes one full onboarding
   `nodus/<device_id>/config/set` envelope.
4. Nodus publishes `config/ack`.
5. Nodus publishes `config/result`.
6. Nodus publishes retained `nodus/<device_id>/meta`.

Canonical `onboard/hello` payload:

```json
{
  "onboard_token": "token-123",
  "device_id": "co2-ykdvea",
  "hostname": "co2-ykdvea",
  "serial": "ykdvea",
  "type": "pico2w",
  "version": "v0.26.111.15",
  "capabilities": {
    "sensor": true,
    "switch": true
  }
}
```

Canonical onboarding `config/set` envelope from Sensorius:

```json
{
  "message_id": "cfg-123",
  "onboard_token": "token-123",
  "config_version": 1,
  "checksum": "sha256:...",
  "payload": {
    "settings": {
      "Network": {},
      "MQTT": {},
      "Time": {
        "TZ": "America/Denver",
        "TZ_OFFSET": -21600,
        "TZ_NAME": "MDT"
      },
      "Sensor": {},
      "Switch": {},
      "Display": {},
      "Calibration": {}
    }
  }
}
```

During V2 onboarding, Sensorius includes the hub's active `[Time]` values in
`payload.settings.Time`. Nodus must apply `TZ`, `TZ_OFFSET`, and `TZ_NAME` to
its `settings.toml` when present.

## Runtime Payloads

Canonical `/data` payload:

```json
{
  "schema": "nodus-sensor-data/v1",
  "sensor_id": "co2-ykdvea",
  "device": "co2",
  "location": "OfficeDesk",
  "values": {
    "CO2": 792,
    "Temperature": 22.8
  },
  "timestamp": 946709424
}
```

Canonical heartbeat payload:

```json
{
  "schema": "nodus-heartbeat/v1",
  "device_id": "co2-ykdvea",
  "status": "online",
  "timestamp": 946709424
}
```

Canonical switch state payload:

```json
{
  "schema": "nodus-switch-state/v1",
  "device_id": "switch-ykdvea",
  "channel_id": "S1-ykdvea",
  "label": "Fan",
  "state": "ON",
  "timestamp": 946709424
}
```

## `nodus-meta/v1`

Retained `nodus/<device_id>/meta` is the authoritative full startup snapshot.
Sensorius uses it to initialize or rebuild the local Nodus shadow state stored
under `system_settings/`, `sensor_settings/`, and `switch_settings/`.

The payload must include the routing and identity fields Sensorius needs:

- top-level `schema`, `device_id`, `hostname`, `serial`, `version`, `type`
- `capabilities`
- `status.heartbeat_topic`
- `mqtt.broker`, `mqtt.broker_ip`, `mqtt.active_broker`, `mqtt.port`
- `location_group.location`, `location_group.members`
- `sensor.sensor_id`, `sensor.location`, `sensor.data_topic`,
  `sensor.event_topic`, `sensor.availability_topic`
- `switch.device_id`, `switch.location`
- per-channel `index`, `label`, `channel_id`, `enable_pin`, `pin`,
  `state_topic`, `set_topic`, `result_topic`, `availability_topic`

## Ordinary `config/set`

Canonical topic:

- `nodus/<device_id>/config/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cfg-123",
  "payload": {
    "updates": [
      {
        "section": "Sensor",
        "key": "LOCATION",
        "value": "OfficeDesk",
        "name": "sensor_i2c.toml"
      }
    ]
  },
  "restart": false
}
```

Canonical Nodus responses:

```json
{"message_id":"cfg-123","accepted":true,"duplicate":false}
{"message_id":"cfg-123","applied":true,"updated":1,"duplicate":false,"error":""}
```

Pacing rules in Sensorius:

- resolve one physical Nodus host
- queue updates per host
- send one update item at a time
- wait for `config/ack`
- wait for successful `config/result`
- then send the next queued update
- `Time.TZ`, `Time.TZ_OFFSET`, and `Time.TZ_NAME` updates are valid ordinary
  runtime config writes. Sensorius sends them when its IANA timezone rules move
  the hub between Standard Time and Daylight Saving Time.
- Auto-generated command `message_id` timestamps use Sensorius local-naive
  epoch seconds from `[Time].TZ`, matching Nodus MQTT payload timestamps and
  local data storage.

If a queued update fails, the remaining batch for that route/action stops.

## Switch `config/set`

Canonical topic:

- `nodus/<channel_id>/config/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cfg-123",
  "payload": {
    "updates": [
      {
        "section": "Switch",
        "key": "SWITCH_1_LAST_STATE",
        "value": true,
        "name": "switch.toml"
      }
    ]
  },
  "restart": false
}
```

Forward-only rule:

- JSON `config/set` is the canonical switch-control contract.
- Plain `ON` and `OFF` payloads are not part of the forward contract even if
  firmware still tolerates them.

## `calibration/set`

Canonical topic:

- `nodus/<device_id>/calibration/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cal-123",
  "action": "apply",
  "payload": {
    "offsets": [
      {
        "key": "Calibration.Device.TEMP_OFFSET",
        "value": 1.5
      }
    ]
  }
}
```

Canonical Nodus calibration replies:

- `calibration/ack` includes `message_id` and `accepted`
- `calibration/result` includes `message_id`, `applied`, `updated`, `error`
- accepted calibration writes emit `meta/patch` with `source = "calibration_set"`

## `nodus-meta-patch/v1`

Canonical non-retained delta payload:

```json
{
  "schema": "nodus-meta-patch/v1",
  "device_id": "co2-ykdvea",
  "timestamp": 946709500,
  "source": "config_set",
  "message_id": "cfg-123",
  "sections": ["Sensor"],
  "updates": [
    {
      "section": "Sensor",
      "key": "LOCATION",
      "value": "OfficeDesk"
    }
  ]
}
```

Patch rules:

- `meta/patch` is the incremental sync stream after startup.
- `meta/patch` is not a replacement for retained startup `meta`.
- Sensorius applies the patch to cached meta, reparses the patched meta, and
  updates the corresponding shadow TOML state.

## Deprecated Doc Shapes

The following doc shapes are deprecated and should not be treated as canonical:

- `nodus/<channel_id>/set`
- switch control docs that imply plain `ON`/`OFF` is the primary contract
- docs that imply runtime config writes trigger a full retained `meta` refresh
