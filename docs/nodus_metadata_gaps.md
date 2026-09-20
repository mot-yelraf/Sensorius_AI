# Nodus metadata gaps and firmware handoff

Audit date: 2026-09-20. Compared the local cPyNodus_II and cPyNodus_III
payload builders with Sensorius MQTT ingest and settings materialization.
This is a source-code audit, not a complete hardware validation.

The device TOML files are authoritative. Sensorius shadows are caches of
explicitly advertised device values, not replacement configuration sources.

## Confirmed gaps

| Area | Current firmware behavior | Consequence / proposed addition |
| --- | --- | --- |
| Pressure calibration | Both versions omit calibration altitude from retained startup `meta`. | Publish `sensor.calibration.Device.ALTITUDE_METERS`; repeat in each `sensors[]` child on III. This fixes the pressure range mismatch. |
| Other calibration values | Both startup snapshots omit `Calibration.System` and `Calibration.Device`; calibration-status builders contain status/identity/time and optional extra fields, not a complete calibration snapshot. | Temperature, RH, CO2, AQI/gas, light/PPFD, APVPD and soil offsets can remain factory defaults in a new shadow, or stale in an existing shadow. Publish supported TOML calibration sections and persisted calibration status. |
| Single-sensor identity | II's sensor block lacks logical `device` and `config_file`. III supplies those fields for multi-sensor entries but omits them from its primary compatibility block. | Always include both fields in `sensor` and every `sensors[]` entry. Avoid inferring device family from the sensor ID or guessing a configuration filename. |
| Time and Home Assistant configuration | Both startup builders omit these sections, although Sensorius accepts their `meta/patch` deltas and has shadow sections for them. | A new subscriber cannot reconstruct the device's time/HA configuration from retained startup metadata. Add explicit snapshots if these sections are intended to be mirrored. |
| Switch configuration completeness | Both retained `meta/switch` builders publish channel identities, labels, state and topics, but omit `enable_pin`, physical `pin`, and `override_script`. | Remote switching has enough information to work, but the resulting shadow is not an exact physical configuration mirror. Publish these fields if the shadow must expose device configuration; keep physical pins separate from remote channel identity. |

For calibration, include the keys actually supported by each sensor, preserving
explicit zeros and false values. Known keys include:

- `Calibration.Device`: `ALTITUDE_METERS`, `TEMP_OFFSET`, `RH_OFFSET`,
  `CO2_OFFSET`, `AQI_OFFSET`, `GAS_OFFSET`, `LUX_OFFSET`, `PPFD_OFFSET`,
  `APVPD_TEMP_CAL_VAL`, `APVPD_RH_CAL_VAL`, `SOIL_TEMP_CAL_VAL`,
  `SOIL_MOIST_CAL_VAL`, `SOIL_PH_CAL_VAL`, `SOIL_EC_CAL_VAL`.
- `Calibration.System`: applicable offsets and persisted reference fields
  such as `REF_SENSOR_ID`, `REF_RANGE_HOURS`, `REF_START_TS`, `REF_END_TS`,
  and `REF_NOTE`, where supported by the firmware's TOML schema.
- `Calibration`: persisted `CALIBRATED` and `CALIB_STATUS`, where supported.
  Keep persisted calibration state distinct from transient session status.

Suggested Time fields: `AUTO_TIMEZONE`, `TZ`, `TZ_OFFSET`, `TZ_NAME`,
`NTP_SERVER`, `NTP_SERVER_IP`. Suggested HomeAssistant fields:
`DISCOVERY_PREFIX`, `BASE_TOPIC`, `PUBLISH_DISCOVERY_RETAIN`,
`PUBLISH_STATE_RETAIN`, `PUBLISH_LEGACY_SENSOR_TOPIC`.
These broader additions require agreed casing and Sensorius full-snapshot
materialization; the pressure branch does not implement all of them.

## Pressure payload supported by this branch

Add this to the existing sensor object; preserve all existing fields:

```json
{
  "sensor_id": "avpd-1jm5s1",
  "device": "avpd",
  "config_file": "sensor_i2c.toml",
  "calibration": {
    "Device": {
      "ALTITUDE_METERS": 1719.0
    }
  }
}
```

Use the value loaded from that sensor's device TOML. Numeric strings such as
`"1719.0"` are accepted by Sensorius as well. Zero explicitly means zero;
omission means no information, and must not reset the hub's existing shadow.
For multi-sensor devices, use each child's own altitude and keep the primary
`sensor` compatibility view consistent with its corresponding child.

Sensorius now mirrors this field into the sensor shadow through its settings
manager. The existing dashboard then uses the mirrored device calibration to
select 960–1070 hPa / 28.4–31.4 inHg for corrected pressure. No reading-based
inference or device calibration writes are required. Regular dashboard JSON
refreshes pick up new calibration context and update existing gauge ranges.

## Delivery and replay requirements

Keep correlated `meta/patch` updates for accepted changes. Those patches are
non-retained, so also ensure the corresponding retained snapshot becomes
current after a persisted configuration change. Otherwise, a hub that was
offline during a patch can later receive the old startup value. Current
command handlers publish non-retained deltas; startup/recovery builders are
separate paths. Verify snapshot freshness for MQTT edits, local web edits,
and device restarts.

For larger calibration/configuration snapshots, a separately advertised,
retained topic can keep the main discovery packet small. Such a split is a
proposal and is not yet implemented in this branch; it requires coordinated
subscriber support. The pressure field above can be added to existing `meta`
without a new topic.

Verify firmware changes with:

1. Startup from TOML with nonzero altitude and explicit zero altitude.
2. Different calibration values for two children on one device.
3. An accepted patch followed by a newly connected hub reading retained data.
4. Offline hub during a device configuration edit, then hub reconnect.
5. Repeated snapshots: no redundant shadow writes.
6. Partial/older metadata: preserve previously mirrored fields.

## Other differences and limits

III uses `password_configured` flags in discovery; II still publishes
obfuscated password fields. This is a compatibility difference, not a reason
to add credentials to metadata. Align on configured flags when reconciling the
contracts. Current Sensorius contract prose still describes older credential
fields and needs a separate reconciliation with the firmware.

Sensor bus wiring and III-specific web display, weather, notification and
Ecowitt configuration are also absent from the compact discovery snapshot.
They are outside the current Sensorius remote-sensor discovery contract;
include them only if a complete device-configuration mirror is intended.

Source locations inspected:

- `/Users/twfarley/Projects/cPyNodus_II/cpynodus_ii/features/payloads.py`
- `/Users/twfarley/Projects/cPyNodus_III/cpynodus_iii/features/payloads.py`
- `/Users/twfarley/Projects/cPyNodus_III/cpynodus_iii/features/command_handlers.py`
- `/Users/twfarley/Projects/cPyNodus_III/cpynodus_iii/features/publish_cycle.py`
- `/Users/twfarley/Projects/cPyNodus_III/cpynodus_iii/features/web_handlers.py`
- `/Users/twfarley/Projects/Sensorius_AI/sensorius/saiMQTTIngest.py`

No firmware or live device configuration was modified during this audit.
