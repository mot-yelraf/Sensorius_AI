# Nodus Calibration MQTT Contract

This document describes the calibration MQTT interface currently implemented on Nodus for Sensorius integration.

Scope:

- MQTT calibration writes
- MQTT calibration status queries
- MQTT-triggered soil pH sampling sessions for automatic offset calculation on Nodus
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

- `sensorius`, `homeassistant`, and `weewx` profiles do not start the normal-mode webserver, but MQTT calibration still works there if MQTT is connected.
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
- `action`: one of `apply`, `set`, `update`, `status`, `soil_ph_session_start`, `soil_ph_session_cancel`

`payload` is required for calibration writes and optional for `status`.

## Action: Apply Calibration Values

Use this to write calibration values into the active sensor TOML and hot-reload them.
Nodus accepts the shapes below for compatibility. Sensorius paces runtime
calibration writes by sending one `offsets[]` item per `calibration/set` command
and waiting for the correlated `ack` plus successful `result` before sending the
next value.

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
- `offsets[].key` also accepts the short aliases exposed in calibration status, including `soil_ph_offset`

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

## Action: Soil pH Session Start

Use this to request a short burst of soil measurements so Nodus can calculate and apply a pH offset automatically.

Example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "action": "soil_ph_session_start",
  "payload": {
    "reference_ph": 7.0,
    "sample_interval_s": 10,
    "sample_count": 12
  }
}
```

Behavior:

- Nodus marks calibration status as in progress for the soil session
- Nodus publishes a sample payload to the normal sensor data topic on each sample
- Nodus also publishes the same sample payload to `event/calibration_sample`
- Nodus emits lightweight progress notifications on `event/calibration_progress`
- when sampling completes, Nodus computes the average raw pH and writes the resulting `soil_ph_offset` automatically

Contract note:

- `event/calibration_sample` is the authoritative ingestion path for soil calibration samples
- the mirrored publish on the normal sensor data topic is a compatibility side effect and should not be used by Sensorius as the discriminator for a calibration session
- Sensorius should not assume the normal sensor topic has a calibration-specific schema marker in this implementation
- Sensorius may still use manual `action = "apply"` writes for `soil_ph_offset` when the operator enters an offset directly

Limits:

- `sample_count` defaults to `12`
- `sample_count` is clamped to `6..18`
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
  "error": ""
}
```

Notes:

- manual offset-apply responses use a compact envelope only: `message_id`, `applied`, `updated`, `error`
- accepted calibration deltas are mirrored on the correlated `nodus/<device_id>/meta/patch` payload with `source = "calibration_set"`
- clients should refresh mirrored calibration state from that `meta/patch` delta or from retained `event/calibration_status`

Successful soil pH session start example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "applied": true,
  "started": true,
  "sample_interval_s": 10.0,
  "sample_count": 12,
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
    "soil_calibration_sample_count": 12,
    "soil_calibration_samples_collected": 0
  },
  "error": ""
}
```

Failure example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "applied": false,
  "error": "missing_reference_ph"
}
```

## Meta Patch Topic

Topic:

- `nodus/<device_id>/meta/patch`

For successful manual offset applies, Nodus publishes a correlated non-retained
runtime meta patch after:

1. writing the accepted calibration values into the local TOML file
2. publishing the compact `calibration/result` response

This patch is the authoritative delta Sensorius should use to update its
mirrored calibration state. It carries the accepted TOML write, not a second
copy of the richer runtime calibration status payload.

Current implementation note:

- Empty payloads on `nodus/<device_id>/calibration/set` are ignored.
- Nodus does not publish empty retained cleanup payloads to
  `nodus/<device_id>/calibration/set`.
- If Sensorius publishes a calibration command retained, Sensorius must clear
  it after successful `calibration/result` by publishing an empty retained
  payload to the same `calibration/set` topic.

Example:

```json
{
  "schema": "nodus-meta-patch/v1",
  "device_id": "soil-bd1234",
  "timestamp": 1776266491,
  "source": "calibration_set",
  "message_id": "cal-1776266496-apply-soil-bd1234",
  "sections": ["Calibration.Device"],
  "updates": [
    {
      "section": "Calibration.Device",
      "key": "SOIL_PH_CAL_VAL",
      "value": 0.5
    }
  ]
}
```

Consumption notes:

- correlate the patch with the command using `message_id`
- require `source = "calibration_set"` before treating it as a calibration delta
- apply each `updates[]` entry as a TOML-style write to Sensorius' mirrored copy
- for apply-style commands, Sensorius may treat a correlated
  `source = "calibration_set"` patch as success if `calibration/ack` or
  `calibration/result` is missed
- use `sections` only as a quick grouping hint; `updates[]` is the actual delta
- treat this patch as the accepted calibration write-set, while retained
  `event/calibration_status` and `event/calibration_result` remain the richer
  device-state views
- no calibration `meta/patch` is emitted for failed apply commands

## Soil Sample Event Payload

Topic:

- `nodus/<sensor_id>/event/calibration_sample`

Example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "sample_index": 3,
  "sample_count": 12,
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

Currently emitted by soil pH sampling sessions.

Example:

```json
{
  "message_id": "soil-ph-20260313-1",
  "status": "in_progress",
  "sensor_id": "soil-x943fm",
  "timestamp": 1773380020,
  "sample_index": 3,
  "sample_count": 12
}
```

Notes:

- this is not retained
- current progress events are specific to the soil pH session flow

## Final Result Event

Topic:

- `nodus/<sensor_id>/event/calibration_result`

This is the retained device-state event published by the soil pH calibration
session flow. It is distinct from the command-scoped
`nodus/<device_id>/calibration/result` response used for MQTT `calibration/set`
commands.

Success example:

```json
{
  "status": "success",
  "sensor_id": "soil-x943fm",
  "timestamp": 1773380120,
  "calibrated": true,
  "soil_ph_offset": 0.42,
  "computed_soil_ph_offset": 0.42,
  "reference_ph": 7.0,
  "sample_count": 12
}
```

Failure example:

```json
{
  "status": "failed",
  "sensor_id": "soil-x943fm",
  "timestamp": 1773380120,
  "calibrated": false,
  "soil_ph_offset": 0.0,
  "error": "soil_calibration_not_running"
}
```

Notes:

- this event is published retained by the soil session completion flow
- Sensorius can use it for final UI state even if it missed intermediate progress
- manual offset-apply commands do not mirror this full payload on `calibration/result`; those command responses use the compact envelope described below

## Sensorius Integration Guidance

For offset updates:

1. Publish `calibration/set` with `action = "apply"`.
2. Wait for `calibration/ack`.
3. Wait for `calibration/result`.
4. Treat `calibration/result` as a compact apply envelope: `message_id`, `applied`, `updated`, `error`.
5. If `calibration/ack` or `calibration/result` is missed, a correlated
   `nodus/<device_id>/meta/patch` delta with `source = "calibration_set"` can
   be treated as the accepted apply result.
6. Apply the correlated `meta/patch` delta to Sensorius' mirrored TOML state.
7. Refresh local UI state from the mirrored calibration values or from retained `event/calibration_status`.

For soil pH session flow:

1. Publish `calibration/set` with `action = "soil_ph_session_start"`.
2. Wait for `calibration/ack`.
3. Consume `event/calibration_sample` for each sample in the session.
4. Optionally ignore mirrored sample payloads on the normal sensor topic, since they are not explicitly tagged as calibration traffic.
5. When `sample_index == sample_count`, wait for the completion `calibration/result`.
6. Confirm the returned `computed_soil_ph_offset` and refresh retained `event/calibration_status`.
7. Use manual `action = "apply"` only when the operator wants to enter an offset directly rather than run the automated session.

Recommended behavior:

- treat `calibration/result` as the command-scoped response
- treat `event/calibration_status` as the latest device state
- treat `event/calibration_progress` as optional live progress telemetry
- treat `event/calibration_result` as the latest completion outcome
- treat `event/calibration_sample` as the only authoritative soil calibration sample stream

## Current Limitations

- Generic `action = "start"` local calibration is not implemented in the current runtime.
- No QoS-specific behavior is negotiated; current implementation uses the client defaults.
- AP/bootstrap mode does not expose calibration over MQTT; MQTT requires a connected MQTT-enabled runtime profile.
