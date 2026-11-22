# Nodus Calibration MQTT Contract

This document describes the calibration MQTT interface currently implemented on Nodus for Sensorius integration.

Scope:

- MQTT calibration writes
- MQTT calibration status queries
- Per-probe derived soil settings for firmware advertising the capability below
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
- `action`: one of `apply`, `set`, `update`, `status`, `start`, `soil_ph_session_start`, `soil_ph_session_cancel`, `soil_derived_apply`

`payload` is required for calibration writes and optional for `status` and
`start`.

## Sensorius input units

Sensorius may display and accept temperature corrections in °F and altitude in
feet. It converts these to canonical values before publishing: temperature
corrections are Celsius differences (°F × 5/9, with no 32-degree shift), and
`Calibration.Device.ALTITUDE_METERS` is meters (feet × 0.3048). Nodus firmware,
retained metadata, settings shadows, and the MQTT payload shapes below are
unchanged. General Settings is the source for the calibration altitude.

The Sensorius HTTP `/calibration/device/apply` endpoint accepts optional
`input_unit` on recognized temperature-offset and altitude items. Missing units
retain the historical interpretation (Celsius for temperature offsets and meters
for altitude). Unsupported unit/key combinations and non-finite values are
rejected before any writes. `input_unit` is consumed at the HTTP boundary and
never included in `calibration/set`. Automatic system and plant calibration
continue computing and applying canonical values.

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
- `calibration.soil.*` -> `Calibration.Device.*`
- `calibration.apvpd.*` -> `Calibration.*`

Behavior:

- writes are applied to the active sensor file
- Nodus attempts hot reload via `sensor.reload_calibration_from_settings(settings)`
- if hot reload is unavailable/fails, Nodus falls back to `sensor.try_reinit()`
- `offsets[].key` also accepts the short aliases exposed in calibration status,
  including `soil_ph_offset`
- `soil_moisture_offset` is materialized as
  `Calibration.Device.SOIL_MOIST_CAL_VAL`; Sensorius still reads legacy
  `SOIL_TEMP_MOIST_VAL` shadows for compatibility

## Action: Derived Soil Calibration

`soil_derived_apply` is the firmware handoff for Nodus II and III. Sensorius
displays and edits the settings advertised by `soil_derived.version = 1`.
Each command writes a single key. Sensor offsets and pH sessions use their existing commands.
Nodus owns the calculations and the authoritative settings.

### Discovery and telemetry

Publish the effective settings as a top-level `soil_derived` block in the
existing retained `nodus/<physical_device_id>/meta/config` companion
(`schema = "nodus-meta-config/v1"`). Publish it with MQTT retain enabled on
connect and reconnect so a Sensorius restart can recover the snapshot.
Keep compact `/meta` focused on identity
and advertise `config_topic` there as usual. In compact `/meta`, identify the
soil sensor in `sensor` or `sensors[]` with its matching `sensor_id` and
`config_file = "sensor_soil.toml"`. The companion's normal primary `sensor`
block may describe a different sensor on the same host. The `soil_derived`
block targets the soil sensor by its own `sensor_id`. The two retained
packets can arrive in either order.

```json
{
  "schema": "nodus-meta-config/v1",
  "device_id": "nodus-lab01",
  "soil_derived": {
    "sensor_id": "soil-lab01",
    "version": 1,
    "channels": {
      "CH1": {
        "metric_prefix": "CH1 ",
        "groups": {
          "SoilDeficit": [38.0, 18.0],
          "SoilStress": [15.0, 18.0, 24.0, 30.0, 70.0, 30.0],
          "NPK": [20.0, 30.0, 70.0, 50.0]
        }
      },
      "CH2": {
        "metric_prefix": "CH2 ",
        "groups": {
          "SoilDeficit": [40.0, 20.0],
          "SoilStress": [15.0, 18.0, 24.0, 30.0, 70.0, 30.0]
        }
      }
    }
  }
}
```

List only active physical channels. Report a single probe under its physical
channel even when it uses unprefixed telemetry. For a legacy unchannelized
`[Modbus]` probe, use `CH1`. With one active probe, set `metric_prefix` to
the empty string. With both probes, set it to `"CH1 "` and `"CH2 "`
respectively, including the trailing space. The prefix describes existing
telemetry only. The channel key remains the write target if probe count changes.

The retained group arrays use the field order below. The arrays keep two
fully configured probes within the metadata size budget for typical settings.
Use the named keys in commands, patches and TOML.

| Group | Array fields in wire order |
| --- | --- |
| `SoilDeficit` | `SPD_WET_THRESHOLD_PCT`, `SPD_DRY_THRESHOLD_PCT` |
| `SoilStress` | `SSI_TEMP_LOW_CRIT_C`, `SSI_TEMP_LOW_OK_C`, `SSI_TEMP_HIGH_OK_C`, `SSI_TEMP_HIGH_CRIT_C`, `SSI_MOISTURE_WEIGHT_PCT`, `SSI_TEMP_WEIGHT_PCT` |
| `NPK` | `N_TARGET`, `P_TARGET`, `K_TARGET`, `SFI_MIN_WEIGHT_PCT` |

Include all effective fields for each supported group, including inherited
values. Include `NPK` only for a `soil_7in1` channel. A missing live nutrient
reading can suppress the index for an individual sample without changing
the advertised capability. Omit `soil_derived` from the configuration companion
when the firmware cannot handle this action. Replace the retained companion
on reconnect, including after downgrade, so old capability data expires.

Keep existing `/data` metric names, units and channel prefixes. Sensorius uses
corrected moisture and temperature, nutrient readings and derived results from
`/data`. Settings travel in metadata and patches. Continue to omit derived
values for a sample when their required readings are unavailable. Publish an
up-to-date retained configuration companion on boot and after successful
changes, after queued command replies drain. This lets Sensorius recover
missed patches and see saves made from the local Nodus UI.

### Write command

Publish non-retained with QoS 1 to
`nodus/<physical_device_id>/calibration/set`. Capability version 1 uses
one scalar setting per command. Keep the payload flat so Nodus II and III
can handle writes within their Python stack limits. Only advertise version 1
when this single-key handler is available.

```json
{
  "message_id": "soil-5ac3f87f657a423090423820e8c40aa0",
  "action": "soil_derived_apply",
  "sensor_id": "soil-lab01",
  "name": "sensor_soil.toml",
  "payload": {
    "section": "SoilDeficit.CH1",
    "key": "SPD_WET_THRESHOLD_PCT",
    "value": 43.0
  }
}
```

- Require a unique `message_id`, the exact soil `sensor_id` and `name`,
  and one advertised section in the form `<group>.<channel>`. Require one
  known setting key and a finite JSON number. Reject mismatched targets,
  additional keys, grouped values, strings, booleans, NaN and infinities.
- Validate the candidate probe settings after substituting the single value.
  Moisture thresholds and weights are percentages in `[0, 100]`. Require
  wet > dry. Temperature thresholds are absolute °C, with low critical <
  low acceptable ≤ high acceptable < high critical. Require a positive sum
  for the two stress weights. N/P/K targets in mg/kg must be positive. The
  minimum-score weight must be in `[0, 100]`.
- Persist the changed key and keep other keys, probes and groups intact. If a
  probe still inherits its settings, derive its effective values from the shared
  defaults and store only the requested override. Treat a valid no-op as a
  successful single-key command and echo its effective value.
- Sensorius skips unchanged values and chooses an order that keeps the
  calculation valid at each intermediate step. Hold the physical host's
  command lock and wait for the matching ACK, persisted result and patch
  before sending the next key. On failure or missing confirmation, stop the
  sequence and report progress. Earlier successful writes remain in effect.

### Device storage and calculation

Use the active `/sensor_soil.toml` file. Keep existing shared sections
`[SoilDeficit]`, `[SoilStress]` and `[NPK]` as the compatibility baseline.
Store probe overrides in nested sections such as `[SoilDeficit.CH1]`,
`[SoilStress.CH1]` and `[NPK.CH1]`. Resolve keys without a probe override
from the shared settings and existing firmware defaults. In particular,
default `SFI_MIN_WEIGHT_PCT` to 50. Apply the same validation and persistence
to the local Nodus calibration UI, and show the effective values and latest
readings.

- **Soil Moisture Deficit** uses corrected moisture. Compute
  `100 × (wet − moisture) / (wet − dry)` and clamp to `[0, 100]`.
- **Soil Stress Index** uses the channel's deficit and corrected temperature.
  Temperature stress is zero inside the acceptable band, 100 at or beyond
  the critical limits and linear in between. Average deficit and temperature
  stress using the two weights divided by their sum. Require both readings
  even when one weight is zero, as with existing firmware.
- **Soil Fertility Index** uses the channel's N, P and K. Divide each reading by
  its target and clamp each ratio to `[0, 1]`. Let `w = SFI_MIN_WEIGHT_PCT / 100`.
  The index is `100 × (w × lowest_ratio + (1 − w) × average_ratio)`.
  Round deficit and stress to one decimal place and fertility to a whole
  percentage, matching the existing firmware.

Prepare the validated runtime values before writing. Use a bounded settings
transaction that preserves the previous file on write or rename failure and
keeps the active settings aligned with the successful file commit. Make the
values available to the next sample without reboot. A read-only filesystem,
storage failure or inability to install the validated settings must produce a
failed result. This action requires persistence before success. Keep the
shallow command path and existing one-offset handlers for legacy calibration.

### Replies and reconciliation

Publish the normal non-retained `calibration/ack` on the physical device topic.
`accepted: true` confirms receipt of the command envelope. Then publish the
correlated non-retained `calibration/result` on the same physical device's
topic. A success looks like this.

```json
{
  "message_id": "soil-5ac3f87f657a423090423820e8c40aa0",
  "sensor_id": "soil-lab01",
  "name": "sensor_soil.toml",
  "section": "SoilDeficit.CH1",
  "key": "SPD_WET_THRESHOLD_PCT",
  "applied": true,
  "persistence_mode": "persisted",
  "updated": 1,
  "error": ""
}
```

On failure, send `applied: false`, `updated: 0` and a short `error`, such as
`invalid_soil_derived_values`, `soil_channel_not_found`,
`unsupported_soil_group` or `settings_not_writable`. Do not emit a patch
for a failed command. Duplicate message IDs should replay the original outcome
without writing again or turning a rejected command into a success.

After a successful result, publish a single non-retained
`nodus/<physical_device_id>/meta/patch` containing the one saved key. The
value should reflect the effective device value, including any firmware
numeric representation differences.

```json
{
  "schema": "nodus-meta-patch/v1",
  "device_id": "nodus-lab01",
  "message_id": "soil-5ac3f87f657a423090423820e8c40aa0",
  "timestamp": 1795276800,
  "source": "calibration_set",
  "sections": ["SoilDeficit.CH1"],
  "updates": [
    {"sensor_id": "soil-lab01", "name": "sensor_soil.toml", "section": "SoilDeficit.CH1", "key": "SPD_WET_THRESHOLD_PCT", "value": 43.0}
  ]
}
```

For a save from the Nodus UI while MQTT is connected, publish one patch per
changed key with `source = "calibration_local"` and refresh retained `meta/config`.
When the local UI is running without MQTT, the next MQTT startup advertises
the saved settings. Sensorius mirrors complete finite settings from retained
metadata so older values can be reviewed and corrected. Sensorius checks each
patch's scalar value and merges it into the cached settings and mirrored TOML.
If a persisted result arrives without a matching patch, Sensorius reports how
many writes the device reported saving and stops sending further writes.

### Firmware acceptance checks

- Upgrade a device with shared settings, including legacy `[Modbus]`, and
  confirm the effective values and outputs match its pre-upgrade behavior.
- Capture the request, ACK, persisted result, patch and refreshed retained
  `meta/config` on the broker. Confirm that every command and patch contains
  one key and each next command follows the prior result. Compare the next sample to
  the formula using the advertised inputs, then reboot and confirm persistence.
- Exercise both physical channels, including a 7-in-1 paired with a non-NPK
  probe. Save a group on one probe and confirm the other probe's settings
  and calculation are unaffected. Test CH2 as the only active probe.
- Reject grouped commands, invalid keys or values, wrong targets, repeats of
  rejected commands and writes to a read-only or failing filesystem. Fail a
  later write in a sequence and confirm the earlier persisted keys remain in effect.
- Exercise repeated saves and local calibration saves on the supported
  boards. Check command latency, heap and Python stack headroom, and the
  broker-visible ordering of responses, metadata and telemetry. Validate actual
  encoded MQTT packet sizes with two 7-in-1 probes against the Pico2 W
  1,460-byte packet guard, including the topic and protocol overhead.

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

For APVPD plant-sensor calibration:

1. Publish `calibration/set` with `action = "start"`.
2. Wait for `calibration/ack`.
3. Wait for the correlated `calibration/result`.
4. Treat a result with `started = true` or `applied = true` as a started
   calibration routine.

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

- `action = "start"` is reserved for the APVPD plant-sensor calibration
  routine. Other calibration flows should use their explicit actions.
- No QoS-specific behavior is negotiated; current implementation uses the client defaults.
- AP/bootstrap mode does not expose calibration over MQTT; MQTT requires a connected MQTT-enabled runtime profile.
