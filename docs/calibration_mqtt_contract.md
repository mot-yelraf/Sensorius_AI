# Nodus Calibration MQTT Contract

This document describes the calibration MQTT interface currently implemented on Nodus for Sensorius integration.

Scope:

- MQTT calibration writes
- MQTT calibration status queries
- MQTT-triggered local calibration start for sensors that support it
- MQTT-triggered soil pH sampling sessions for offset calculation in Sensorius
- MQTT progress/result events published by Nodus

Non-goals:

- AP/bootstrap onboarding (`/itaot-init` and `/itaot-meta` remain HTTP/AP-only)
- General onboarding/config topics unrelated to calibration

## Availability

This contract is available only when:

- Nodus is running an MQTT-enabled profile
- the MQTT client is connected
- the webserver is not required for the operation

Notes:

- `homeassistant` and `weewx` profiles do not start the normal-mode webserver, but MQTT calibration still works there if MQTT is connected.
- `nodusweb` keeps the HTTP calibration routes for local/manual use.

## Topics

Given:

- `base_topic = nodus`
- `device_id = aqi-x943fm`
- `sensor_id = aqi-x943fm`

Sensorius publishes commands to:

- `nodus/aqi-x943fm/calibration/set`

Nodus publishes command acknowledgements to:

- `nodus/aqi-x943fm/calibration/ack`

Nodus publishes command results to:

- `nodus/aqi-x943fm/calibration/result`

Nodus publishes retained runtime calibration status to:

- `nodus/aqi-x943fm/event/calibration_status`

Nodus publishes progress events to:

- `nodus/aqi-x943fm/event/calibration_progress`

Nodus publishes soil calibration samples to:

- `nodus/aqi-x943fm/event/calibration_sample`

Nodus publishes final calibration outcome events to:

- `nodus/aqi-x943fm/event/calibration_result`

## Command Envelope

All calibration commands use the same top-level envelope:

```json
{
  "message_id": "cal-20260308-001",
  "action": "apply",
  "payload": {}
}
```

Required fields:

- `message_id`: client-generated unique id for correlation
- `action`: one of `apply`, `set`, `update`, `status`, `start`, `soil_ph_session_start`, `soil_ph_session_cancel`

`payload` is required for calibration writes and optional for `status` or `start`.

## Action: Apply Calibration Values

Use this to write calibration values into the active sensor TOML and hot-reload them.

Accepted payload shape A:

```json
{
  "message_id": "cal-20260308-apply-1",
  "action": "apply",
  "payload": {
    "calibration": {
      "system": {
        "RH_OFFSET": -0.5
      },
      "device": {
        "TEMP_OFFSET": 1.5
      }
    }
  }
}
```

Accepted payload shape B:

```json
{
  "message_id": "cal-20260308-apply-2",
  "action": "apply",
  "payload": {
    "offsets": [
      {
        "key": "Calibration.System.RH_OFFSET",
        "value": -0.5
      },
      {
        "key": "Calibration.Device.TEMP_OFFSET",
        "value": 1.5
      }
    ]
  }
}
```

Section mapping currently implemented by Nodus:

- `calibration.system.*` -> `Calibration.System.*`
- `calibration.device.*` -> `Calibration.Device.*`
- `calibration.soil.*` -> `Calibration.Soil.*`
- `calibration.apvpd.*` -> `Calibration.*`

Behavior:

- writes are applied to the active sensor file
- Nodus attempts hot reload via `sensor.reload_calibration_from_settings(settings)`
- if hot reload is unavailable/fails, Nodus falls back to `sensor.try_reinit()`

## Action: Status

Use this to request an immediate status/result response.

Example:

```json
{
  "message_id": "cal-20260308-status-1",
  "action": "status"
}
```

Behavior:

- Nodus republishes retained `event/calibration_status`
- Nodus also emits a correlated `calibration/result` response

## Action: Start

Use this to request a local on-device calibration routine.

Example:

```json
{
  "message_id": "cal-20260308-start-1",
  "action": "start"
}
```

Behavior:

- Nodus starts the local calibration coroutine only if the active sensor supports it
- currently this is intended for sensor implementations like APVPD that implement `calibrate_plant_sensor()`

Error cases:

- `calibration_not_supported`
- `calibration_already_running`
- `calibration_start_failed`

Important:

- not all sensor types support local calibration start
- system/device offset writes are broader than local calibration start support

## Action: Soil pH Session Start

Use this to request a short burst of soil measurements so Sensorius can calculate a pH offset.

Example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "action": "soil_ph_session_start",
  "payload": {
    "reference_ph": 7.0,
    "sample_interval_s": 10,
    "sample_count": 6
  }
}
```

Behavior:

- Nodus marks calibration status as in progress for the soil session
- Nodus publishes a sample payload to the normal sensor data topic on each sample
- Nodus also publishes the same sample payload to `event/calibration_sample`
- Nodus emits lightweight progress notifications on `event/calibration_progress`
- this is intended for Sensorius-driven offset calculation, not on-device automatic offset persistence

Contract note:

- `event/calibration_sample` is the authoritative ingestion path for soil calibration samples
- the mirrored publish on the normal sensor data topic is a compatibility side effect and should not be used by Sensorius as the discriminator for a calibration session
- Sensorius should not assume the normal sensor topic has a calibration-specific schema marker in this implementation
- after collecting the requested samples, Sensorius is responsible for calculating the final offset and issuing a separate `action = "apply"` write for `soil_ph_offset`

Limits:

- `sample_count` is clamped to `1..12`
- `sample_interval_s` defaults to `10`

Error cases:

- `missing_reference_ph`
- `soil_calibration_already_running`

## Action: Soil pH Session Cancel

Use this to stop an active soil pH sampling session.

Example:

```json
{
  "message_id": "soil-ph-20260313-cancel-1",
  "action": "soil_ph_session_cancel"
}
```

Error cases:

- `soil_calibration_not_running`

## Acknowledgement Topic

On accepted command parsing, Nodus publishes:

Topic:

- `nodus/<device_id>/calibration/ack`

Payload:

```json
{
  "message_id": "cal-20260308-apply-1",
  "accepted": true
}
```

This only means the command envelope was accepted for handling. Final success/failure is reported on `calibration/result`.

## Result Topic

Topic:

- `nodus/<device_id>/calibration/result`

Successful apply example:

```json
{
  "message_id": "cal-20260308-apply-1",
  "applied": true,
  "updated": 2,
  "status": {
    "status": "calibrated",
    "calibrated": true,
    "sensor_id": "aqi-x943fm",
    "timestamp": 1772956800,
    "temp_offset": 1.25,
    "rh_offset": -2.5
  },
  "error": ""
}
```

Successful start example:

```json
{
  "message_id": "cal-20260308-start-1",
  "applied": true,
  "started": true,
  "status": {
    "status": "in_progress",
    "calibrated": false,
    "sensor_id": "aqi-x943fm",
    "timestamp": 1772956800
  },
  "error": ""
}
```

Successful soil pH session start example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "applied": true,
  "started": true,
  "sample_interval_s": 10.0,
  "sample_count": 6,
  "reference_ph": 7.0,
  "status": {
    "status": "in_progress",
    "calibrated": false,
    "sensor_id": "soil-x943fm",
    "timestamp": 1773380000,
    "soil_ph_offset": 0.0,
    "soil_calibration_active": true,
    "soil_calibration_message_id": "soil-ph-20260313-1",
    "soil_calibration_reference_ph": 7.0,
    "soil_calibration_sample_count": 6,
    "soil_calibration_samples_collected": 0
  },
  "error": ""
}
```

Failure example:

```json
{
  "message_id": "cal-20260308-start-1",
  "applied": false,
  "error": "calibration_not_supported",
  "status": {
    "status": "idle",
    "calibrated": false,
    "sensor_id": "aqi-x943fm",
    "timestamp": 1772956800
  }
}
```

## Soil Sample Event Payload

Topic:

- `nodus/<sensor_id>/event/calibration_sample`

Example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "sample_index": 3,
  "sample_count": 6,
  "reference_ph": 7.0,
  "timestamp": 1773380020,
  "status": "ok",
  "metrics": ["Soil-pH", "Soil-Temp"],
  "display_metrics": ["Soil-pH", "Soil-Temp"],
  "values": {
    "Soil-pH": 6.8,
    "Soil-Temp": 21.2
  },
  "units": {
    "Soil-pH": "pH",
    "Soil-Temp": "C"
  },
  "soil_ph_offset": 0.0,
  "corrected_ph": 6.8,
  "raw_ph": 6.8
}
```

Notes:

- the same payload is also published to the normal sensor data topic during the sampling session
- this payload does not currently include an explicit event discriminator field such as `event = "calibration_sample"`
- Sensorius should therefore treat the topic `nodus/<sensor_id>/event/calibration_sample` as the session discriminator and treat the mirrored normal-topic publish as non-authoritative
- `raw_ph` is derived from `corrected_ph - soil_ph_offset` when the active soil offset is available

## Runtime Status Event

Topic:

- `nodus/<sensor_id>/event/calibration_status`

This topic is published retained.

Example:

```json
{
  "status": "in_progress",
  "calibrated": false,
  "sensor_id": "aqi-x943fm",
  "timestamp": 1772956800,
  "temp_offset": 1.25,
  "rh_offset": -2.5
}
```

Observed status values from current implementation:

- `unavailable`
- `idle`
- `in_progress`
- normalized sensor state values such as `calibrated` or `not_calibrated`

Notes:

- fields like `temp_offset`, `rh_offset`, `device_temp_offset`, `device_rh_offset`, `system_temp_offset`, and `system_rh_offset` are present only when the active sensor exposes them

## Progress Event

Topic:

- `nodus/<sensor_id>/event/calibration_progress`

Currently emitted by APVPD local calibration.

Example:

```json
{
  "status": "in_progress",
  "sensor_id": "aqi-x943fm",
  "timestamp": 1772956800,
  "sample_index": 2,
  "sample_total": 5
}
```

Notes:

- this is not retained
- not all sensor drivers emit progress events

## Final Result Event

Topic:

- `nodus/<sensor_id>/event/calibration_result`

Success example:

```json
{
  "status": "success",
  "sensor_id": "aqi-x943fm",
  "timestamp": 1772956800,
  "calibrated": true,
  "temp_offset": 1.25,
  "rh_offset": -2.5
}
```

Failure example:

```json
{
  "status": "failed",
  "sensor_id": "aqi-x943fm",
  "timestamp": 1772956800,
  "calibrated": false,
  "temp_offset": 0.0,
  "rh_offset": 0.0,
  "error": "no valid samples"
}
```

Notes:

- this event is published retained by the local calibration-capable sensor implementations
- Sensorius can use it for final UI state even if it missed intermediate progress

## Sensorius Integration Guidance

For offset updates:

1. Publish `calibration/set` with `action = "apply"`.
2. Wait for `calibration/ack`.
3. Wait for `calibration/result`.
4. Refresh local UI state from `status` inside the result or from retained `event/calibration_status`.

For local calibration start:

1. Publish `calibration/set` with `action = "start"`.
2. Wait for `calibration/ack`.
3. If `calibration/result.started == true`, transition UI to active state.
4. Consume `event/calibration_progress`.
5. Consume retained `event/calibration_result` and retained `event/calibration_status` for completion.

For soil pH session flow:

1. Publish `calibration/set` with `action = "soil_ph_session_start"`.
2. Wait for `calibration/ack`.
3. Consume `event/calibration_sample` for each sample in the session.
4. Optionally ignore mirrored sample payloads on the normal sensor topic, since they are not explicitly tagged as calibration traffic.
5. When `sample_index == sample_count`, compute the desired `soil_ph_offset` in Sensorius from the collected samples and the requested `reference_ph`.
6. Publish a follow-up `calibration/set` with `action = "apply"` and `payload.calibration.soil.SOIL_PH_OFFSET = <computed_offset>` or the equivalent `offsets[]` form.
7. Wait for `calibration/ack`.
8. Wait for `calibration/result`.
9. Refresh retained `event/calibration_status` and confirm the new `soil_ph_offset`.

Recommended behavior:

- treat `calibration/result` as the command-scoped response
- treat `event/calibration_status` as the latest device state
- treat `event/calibration_progress` as optional live progress telemetry
- treat `event/calibration_result` as the latest completion outcome
- treat `event/calibration_sample` as the only authoritative soil calibration sample stream

## Current Limitations

- No cancel/pause command is implemented.
- No QoS-specific behavior is negotiated; current implementation uses the client defaults.
- Not all sensor drivers support local calibration start.
- AP/bootstrap mode does not expose calibration over MQTT; MQTT requires a connected MQTT-enabled runtime profile.
