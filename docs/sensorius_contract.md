# Sensorius Contract

This document is the canonical forward-only contract between current
`cPyNodus_II` firmware and current Sensorius.

The two projects move together. Old topic names and compatibility shims do not
define the contract. When other docs drift, this document wins.

## Principles

- AP bootstrap uses only `/itaot-meta` and `/itaot-init`.
- Normal runtime sync uses only MQTT.
- OTA uses MQTT only for the prepare/result control path; package bytes move
  over HTTP while Nodus is in temporary OTA mode.
- Nodus publishes retained compact `meta` on connect/reconnect.
- Nodus publishes retained `meta/switch` with detailed switch channel topics in
  the startup identity publish batch when switch channels are present.
- After accepted runtime changes, Nodus publishes only `meta/patch`.
- Sensorius paces ordinary runtime config writes one key at a time per
  physical Nodus host and waits for `ack` plus successful `result`.
- Sensorius restarts a Nodus through device `config/set` with `restart = true`
  and waits for `ack`, successful `result`, then the device's reconnect.

## AP Bootstrap

Routes exposed in AP mode:

- `GET /itaot-meta`
- `POST /itaot-init`

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
  },
  "time": {
    "TZ": "America/Denver",
    "TZ_OFFSET": -21600,
    "TZ_NAME": "MDT",
    "NTP_SERVER": "us.pool.ntp.org",
    "NTP_SERVER_IP": "132.163.96.6"
  }
}
```

Canonical success response:

```json
{
  "accepted": true,
  "rebooting": true
}
```

Bootstrap rules:

- Validate required fields and types.
- Persist only supported Network, MQTT, Profile, and Time settings.
- Keep onboarding protocol state out of normal TOML config schema.
- If `mqtt.active_profile` is omitted and `mqtt.broker_host` is present,
  infer `ACTIVE_PROFILE = "sensorius"`.
- Sensorius sends available hub `[Time]` values in `time`; Nodus persists
  supported keys when present.
- Current firmware stores the bootstrap token in `onboarding_state.json`,
  validates `config/set` against it when present, and deletes the file after a
  successful config apply. On-device TTL enforcement is not currently
  implemented.

## MQTT Topics

### External automation ownership

Sensorius publishes presentation-only automation ownership for each physical
Nodus device with switch channels:

- `nodus/<device_id>/automation/sensorius/status`
- `nodus/<device_id>/automation/sensorius/availability`

The retained status payload is:

```json
{"schema":"nodus-automation-status/v1","controller":"sensorius","controller_id":"sensorius-main","updated_at":1784469600,"channels":[{"channel_id":"switch-1jm5s1-2","automations":["Night Lights"],"enabled":true}]}
```

`channels` lists canonical Nodus channel IDs and their enabled Advanced-rule
names. An empty list explicitly clears prior ownership. The retained
availability payload uses schema `nodus-automation-availability/v1`, the same
controller fields, `updated_at`, and `status` set to `online` or `offline`.

Sensorius refreshes online availability every 60 seconds. Because its shared
MQTT connection can represent multiple Nodus devices but has only one Last
Will, Nodus treats the online record as a lease and expires it after 180
seconds. Sensorius publishes offline for known devices during orderly task
cancellation. Ownership does not grant exclusive control and does not move
rule evaluation onto Nodus.

### Startup and steady state

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<device_id>/meta/switch`
- `nodus/<sensor_id>/availability`
- `nodus/<sensor_id>/data`
- `nodus/<channel_id>/event`
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
- `nodus/<channel_id>/event`
- retained `nodus/<channel_id>/state`
- `nodus/<device_id>/meta/patch`
- optional Sensorius-owned retained empty `nodus/<channel_id>/config/set`
  cleanup when Sensorius used a retained command

### Calibration

- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/meta/patch`

### Log Transfer

- `nodus/<device_id>/logs/get`
- `nodus/<device_id>/logs/ack`
- `nodus/<device_id>/logs/chunk`
- `nodus/<device_id>/logs/result`

### Firmware Update

- `nodus/<device_id>/fwupdate`
- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`

## Onboarding MQTT Flow

1. Nodus joins Wi-Fi and MQTT.
2. Nodus publishes `nodus/<device_id>/onboard/hello`.
3. Sensorius validates the token, clears any prior removal suppression, and
   renews its metadata subscriptions so retained `meta` and `meta/switch`
   records suppressed before the hello are replayed.
4. Sensorius sends one full onboarding
   `nodus/<device_id>/config/set` envelope.
5. Nodus publishes `config/ack`.
6. Nodus publishes `config/result`.
7. Nodus publishes retained `nodus/<device_id>/meta`.
8. If switch channels are present, Nodus publishes retained
   `nodus/<device_id>/meta/switch` in the startup identity publish batch.

The AP bootstrap and full onboarding config both carry hub `[Time]` values.
The bootstrap uses top-level `time`. The full onboarding config uses
`payload.settings.Time`. Nodus persists supported keys when present.

Canonical `onboard/hello` payload:

```json
{
  "onboard_token": "token-123",
  "device_id": "co2-ykdvea",
  "hostname": "co2-ykdvea",
  "serial": "ykdvea",
  "type": "nodus",
  "mcu": "pico2w",
  "version": "v0.26.xxx.x",
  "capabilities": {
    "sensor": true,
    "switch": true
  },
  "sensor": {
    "present": true,
    "device": "co2",
    "hardware": "SCD4x"
  }
}
```

In `onboard/hello`, `type` is the device class and should be `nodus`. `mcu` is
the board target identifier for the running firmware. Verified `mcu` values are
`pico2w` and `xesp32s3`. Sensorius treats a missing `mcu` as `pico2w` for
legacy Nodus firmware compatibility. The `sensor` block is a compact
onboarding hint. `sensor.device` is the logical Nodus sensor ID, and
`sensor.hardware` is the concrete sensor hardware family when known.

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

`values` is a dynamic metric map. Soil 7-in-1 devices may include raw N/P/K
nutrient readings and derived metrics such as `Soil Fertility Index` when the
relevant sensor registers and `[NPK]` targets are available.
SGP gas sensors use `Equivalent CO2` and `TVOC` for SGP30, `VOC Index` for
SGP40, and `VOC Index` plus `NOx Index` for SGP41. Sensorius ingests these keys
unchanged from Nodus `/data` payloads and honors their advertised
`display_metrics`.
`Baro-Pressure` and `Plant Baro-Pressure` values are normalized to one decimal
place in hPa before publication and storage.

Canonical heartbeat payload:

```json
{
  "schema": "nodus-heartbeat/v1",
  "device_id": "co2-ykdvea",
  "status": "online",
  "timestamp": 946709424
}
```

Canonical switch event payload:

```json
{
  "schema": "nodus-switch-event/v1",
  "device_id": "switch-ykdvea",
  "channel_id": "S1-ykdvea",
  "label": "Fan",
  "state": "ON",
  "message_id": "cfg-123",
  "timestamp": 946709424
}
```

Retained switch `state` topic implementation note:

- Startup refresh currently publishes a JSON `nodus-switch-state/v1` snapshot.
- Accepted runtime switch commands publish raw retained `ON` or `OFF`.
- Consumers should tolerate both shapes on `nodus/<channel_id>/state` and use
  `event` plus `config/result` for correlated command handling.

## Retained `meta`

Retained `nodus/<device_id>/meta` is the compact authoritative startup
snapshot. Sensorius uses it to materialize the device, sensor, core MQTT
topics, and switch presence quickly after connect/reconnect.

`device_id` identifies the physical Nodus. A factory dual-sensor Nodus uses
`<sensor1>-<sensor2>-<serial>`, while its child sensor IDs remain
`<sensor1>-<serial>` and `<sensor2>-<serial>`.

The payload must include:

- top-level `schema`, `device_id`, `hostname`, `serial`, `version`, `type`,
  `mcu`
- `capabilities`
- `status.heartbeat_topic`
- `network.ssid`, `network.password`, `network.hostname`, `network.ipv4addr`
- `profile.active_profile`
- `mqtt.broker`, `mqtt.broker_ip`, `mqtt.active_broker`, `mqtt.port`,
  `mqtt.use_tls`, `mqtt.base_topic`, and configured `mqtt.username` /
  `mqtt.password`
- `fwupdate.schema`, `fwupdate.transport`, `fwupdate.prepare_topic`,
  `fwupdate.ack_topic`, `fwupdate.result_topic`
- `location_group.location`, `location_group.members`
- `sensor.sensor_id`, `sensor.location`, `sensor.hardware`,
  `sensor.data_topic`,
  `sensor.event_topic`, `sensor.availability_topic`,
  `sensor.display_metrics`, `sensor.display_styles`
- optional `sensors` array for a multi-sensor Nodus; every entry carries its
  own `sensor_id`, logical device, hardware, `config_file`, location, display
  settings, data topic, event topic, and availability topic
- `switch.device_id`, `switch.channel_count`, and `switch.meta_topic` when
  switch capability is present

`type` remains the device class (`nodus`). `mcu` is the board target identifier
for the running firmware. Verified `mcu` values are `pico2w` and `xesp32s3`.
Sensorius treats a missing `mcu` as `pico2w` for legacy Nodus firmware
compatibility.

`sensor.hardware` is the concrete sensor hardware family when known. Current
values include `BME280`, `BME680`, `VEML7700`, `AHTx0`, `SCD30`, `SCD4x`,
`SGP30`, `SGP40`, and `SGP41`.
Logical sensor device IDs remain unchanged.

`network.ipv4addr` is the current runtime station IPv4 address from the active
network stack. It is not a TOML setting and should be treated as volatile
runtime state that can change after DHCP lease changes, reconnects, or network
changes.

The retained `meta.status` block intentionally advertises only the heartbeat
topic. Consumers should use retained heartbeat and availability topics for
online/offline state.

The startup `meta` payload intentionally does not include
`switch.channels[*]`. The detailed per-channel switch topic map is published
separately on retained `nodus/<device_id>/meta/switch`. Switch location also
lives in retained `meta/switch` to keep the main retained `meta` packet small
on constrained board MQTT startup paths, especially Pico 2 W.

The startup `meta` payload also intentionally does not include the log-transfer
topic map. When `capabilities.log_transfer` is true, Sensorius should use the
deterministic `nodus/<device_id>/logs/{get,ack,chunk,result}` topic family.

Sensorius compatibility rule:

1. If `meta.sensors` is non-empty, materialize every child and treat
   `meta.sensor` as the primary compatibility view; otherwise materialize
   `meta.sensor`.
2. Persist the child-to-physical-device and child-to-`config_file` mapping in
   each local Nodus sensor shadow.
3. If retained `meta.switch.channels` exists, parse it as the legacy embedded
   switch topic map.
4. Else if retained `meta.switch.meta_topic` exists, read retained
   `meta.switch.meta_topic` and parse `nodus-meta-switch/v1`.
5. Else if `meta.switch.channel_count > 0`, derive the default topic
   `nodus/<device_id>/meta/switch` and read retained `nodus-meta-switch/v1`
   as a fallback.
6. Else treat the device as having no switch channel topic map yet and wait
   for a later retained `meta` or `meta/switch`.

Password fields in retained `meta` use the same `obf1:` obfuscation format as
persisted TOML password fields. They are not plaintext.

## Retained `meta/switch`

Retained `nodus/<device_id>/meta/switch` is the authoritative switch channel
topic map for Sensorius control. Nodus publishes it with the retained startup
identity batch. Sensorius should merge it with the latest retained `meta` for
switch control materialization.

Canonical topic:

- `nodus/<device_id>/meta/switch`

Canonical payload:

```json
{
  "schema": "nodus-meta-switch/v1",
  "device_id": "co2-ykdvea",
  "switch_device_id": "switch-ykdvea",
  "location": "OfficeDesk",
  "channel_count": 2,
  "channels": [
    {
      "index": 1,
      "label": "Fan",
      "channel_id": "S1-ykdvea",
      "state": false,
      "event_topic": "nodus/S1-ykdvea/event",
      "state_topic": "nodus/S1-ykdvea/state",
      "set_topic": "nodus/S1-ykdvea/config/set",
      "ack_topic": "nodus/S1-ykdvea/config/ack",
      "result_topic": "nodus/S1-ykdvea/config/result",
      "availability_topic": "nodus/S1-ykdvea/availability"
    },
    {
      "index": 2,
      "label": "Humidifier",
      "channel_id": "S2-ykdvea",
      "state": false,
      "event_topic": "nodus/S2-ykdvea/event",
      "state_topic": "nodus/S2-ykdvea/state",
      "set_topic": "nodus/S2-ykdvea/config/set",
      "ack_topic": "nodus/S2-ykdvea/config/ack",
      "result_topic": "nodus/S2-ykdvea/config/result",
      "availability_topic": "nodus/S2-ykdvea/availability"
    }
  ],
  "timestamp": 946709424
}
```

`meta/switch` must include:

- top-level `schema`, `device_id`, `switch_device_id`, `location`,
  `channel_count`, `channels`, and `timestamp`
- per-channel `index`, `label`, `channel_id`, `state`, `event_topic`,
  `state_topic`, `set_topic`, `ack_topic`, `result_topic`, and
  `availability_topic`

Hardware pin fields are not part of the MQTT control contract. If Sensorius
needs pin diagnostics, use `/itaot-meta` or a later diagnostic contract rather
than startup MQTT metadata.

## Retained Command Cleanup

`/set` topics are command topics, not state topics. Sensorius should publish
commands non-retained unless a specific command flow requires retained delivery.

When Sensorius intentionally publishes any `/set` command retained, Sensorius
owns removing that retained command after successful handling by publishing an
empty retained payload to the same topic. Nodus ignores empty `/set` payloads.

Sensorius retained-command sequence:

1. Publish the command to the relevant `/set` topic with `retain = true`.
2. Wait for the correlated `ack`.
3. Wait for the correlated successful `result`.
4. Consume the correlated retained state, event, or `meta/patch` update as
   applicable.
5. Publish an empty retained payload to the same `/set` topic.

Current implementation:

- Nodus ignores empty payloads on switch `nodus/<channel_id>/config/set`.
- Nodus ignores empty payloads on ordinary device
  `nodus/<device_id>/config/set`.
- Nodus ignores empty payloads on `nodus/<device_id>/calibration/set`.
- Nodus does not publish empty retained cleanup payloads to `/set` topics.

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
        "sensor_id": "lux-ykdvea",
        "section": "Sensor",
        "key": "LOCATION",
        "value": "Canopy",
        "name": "sensor_i2c_2.toml"
      }
    ]
  },
  "restart": false
}
```

Canonical replies:

```json
{"message_id":"cfg-123","accepted":true,"duplicate":false}
{"message_id":"cfg-123","applied":true,"updated":1,"duplicate":false,"error":""}
```

Canonical standalone restart request:

```json
{
  "message_id": "rst-123",
  "payload": {},
  "restart": true,
  "restart_mode": "soft"
}
```

Canonical restart replies:

```json
{"message_id":"rst-123","accepted":true,"duplicate":false}
{"message_id":"rst-123","applied":true,"updated":0,"duplicate":false,"error":"","restart":true,"restart_mode":"soft"}
```

Implemented behavior:

- Nodus publishes `config/ack` after a valid envelope is accepted for
  handling.
- Duplicate `message_id` values produce `config/ack` with
  `duplicate = true` and `config/result` with `applied = true`,
  `updated = 0`, and `duplicate = true`. Duplicate restart requests do not
  reboot the device again.
- Accepted non-duplicate writes publish `config/result` and a non-retained
  `meta/patch` with `source = "config_set"`.
- Sensor-specific writes use the physical device topic and identify the child
  with `sensor_id`, `name`, or both. Sensorius publishes both when retained
  metadata provides them.
- Accepted non-duplicate standalone restart requests publish `config/result`
  and then reboot after queued MQTT publishes drain.
- Accepted non-duplicate config writes with `restart = true` publish
  `config/result`, publish `meta/patch`, then reboot after queued MQTT
  publishes drain.
- `restart_mode = "hard"` requests a hard reset. Other values, including
  omitted `restart_mode`, are treated as `"soft"`. In MQTT profiles, runtime
  soft restarts are promoted by firmware policy to a hard reset.
- Accepted non-duplicate `Time.*` writes request a fresh NTP sync after command
  responses and queued MQTT publishes drain.
- Accepted `Time.*` writes are live-first. Nodus publishes successful
  `config/result` and `meta/patch` before best-effort TOML persistence; if
  persistence fails from constrained-memory or Python-stack pressure, the
  command remains MQTT-visible as applied and serial logging reports
  `persistence_mode = "volatile"`.
- Switch-only devices accept `Sensor.LOCATION` as a device-location alias and
  persist it as `Switch.SWITCH_LOCATION`.
- Failed validation or rejected writes publish `config/result` with
  `applied = false` and an error string.
- Empty payloads on `nodus/<device_id>/config/set` are ignored. This allows
  Sensorius retained command cleanup publishes to be received safely after
  reconnect.
- Nodus does not clear `nodus/<device_id>/config/set`; Sensorius owns retained
  command cleanup for commands it publishes retained.

### Coordinated Wi-Fi credential rotation

Sensorius stages fleet Wi-Fi changes while the existing network is still
available:

1. Build a deduplicated list of physical Nodus hosts whose MQTT liveness state
   is `online`.
2. Publish `Network.SSID` and `Network.PASSWORD` as two non-retained
   `config/set` envelopes per host, with one update and `restart = false` in
   each envelope.
3. Wait for the correlated accepted `config/ack` and successful
   `config/result` after each setting before publishing the next setting.
4. After the staging pass is complete, issue a separate correlated restart
   request only to hosts whose credential write succeeded.

The network updates use the same one-key-at-a-time pacing as ordinary runtime
configuration. SSID and password still form one logical credential change:
both must succeed before the host is eligible for restart. Fleet commands
remain bounded to four concurrent physical hosts.

Wi-Fi SSIDs and passwords used by this flow are write-only protocol values.
Nodus must not include them in retained `meta`, `meta/patch`, acknowledgments,
results, errors, or diagnostic logging. Sensorius discards `network.ssid`,
`network.password`, `Network.SSID`, and `Network.PASSWORD` if older firmware
echoes them in metadata.

## Switch `config/set`

Canonical topic:

- `nodus/<channel_id>/config/set`

Canonical switch-control envelope:

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
- Plain `ON` and `OFF` payloads may still be tolerated by firmware, but they
  are not the documented forward contract.

Implemented behavior:

- Sensorius publishes switch commands to one channel-scoped `config/set` topic
  and does not fan out the same state change to both channel and host/device
  topics.
- Sensorius keeps at most one switch `config/set` in flight per channel.
  Duplicate requests for the same desired state are coalesced; conflicting
  requests are rejected until the correlated result, state, event, or
  `meta/patch` clears the in-flight command, or until the command expires.
- Empty payloads on `nodus/<channel_id>/config/set` are ignored. This allows
  Sensorius retained command cleanup publishes to be received safely after
  reconnect.
- Nodus publishes channel-scoped `config/ack` after a valid switch command is
  accepted for handling.
- Nodus applies the switch state, publishes channel-scoped `config/result`,
  publishes a JSON `event`, publishes retained `state`, and publishes a
  non-retained device `meta/patch` with `source = "switch_set"`.
- Runtime command handling currently writes retained `state` as raw `ON` or
  `OFF`; startup refresh may write a JSON state snapshot.
- If the filesystem is writable, Nodus persists the channel
  `SWITCH_<n>_LAST_STATE` update into `switch.toml`. If persistence fails, the
  command may still be applied locally and the command result carries
  `persistence_mode = "volatile"` in serial logging.
- Nodus does not clear `nodus/<channel_id>/config/set`; Sensorius owns retained
  command cleanup for commands it publishes retained.

## `calibration/set`

Canonical topic:

- `nodus/<device_id>/calibration/set`

Canonical Sensorius envelope:

```json
{
  "message_id": "cal-123",
  "action": "apply",
  "sensor_id": "lux-ykdvea",
  "name": "sensor_i2c_2.toml",
  "payload": {
    "offsets": [
      {
        "key": "Calibration.Device.LUX_OFFSET",
        "value": 12.0
      }
    ]
  }
}
```

Canonical replies:

- `calibration/ack`
- `calibration/result`
- `meta/patch` with `source = "calibration_set"` for accepted writes

Implemented behavior:

- Nodus publishes `calibration/ack` after a valid calibration envelope is
  accepted for handling.
- Duplicate `message_id` values produce `calibration/ack` and
  `calibration/result` with `applied = true`, `updated = 0`, and no
  `meta/patch`.
- `action = "apply"`, `"set"`, or `"update"` writes accepted calibration
  values, publishes `calibration/result`, and publishes non-retained
  `meta/patch` with `source = "calibration_set"`.
- `Calibration.Device.ALTITUDE_METERS` accepts meters for BME280 published
  barometric-pressure normalization, BME680 altitude calibration, and
  SCD30/SCD4x CO2 altitude compensation. BME680 and SCD30/SCD4x apply the value
  at driver startup.
- `action = "status"` republishes retained
  `nodus/<sensor_id>/event/calibration_status` and publishes a correlated
  `calibration/result`.
- Soil pH session actions publish command-scoped `calibration/result` plus the
  soil calibration event topics described in
  `docs/calibration_mqtt_contract.md`.
- Empty payloads on `nodus/<device_id>/calibration/set` are ignored. This
  allows Sensorius retained command cleanup publishes to be received safely
  after reconnect.
- Nodus does not clear `nodus/<device_id>/calibration/set`; Sensorius owns
  retained command cleanup for commands it publishes retained.

## `fwupdate`

Canonical prepare topic:

- `nodus/<device_id>/fwupdate`

Signed cPyNodus III prepare payload:

```json
{
  "schema": "nodus-fwupdate/v2",
  "message_id": "fw-20260504T193222Z",
  "command": "prepare",
  "package_id": "ota-tagA-to-tagB",
  "session_id": "32-or-more-random-hex-characters",
  "manifest_sha256": "64-lowercase-hex-characters",
  "key_id": "trusted-key-id"
}
```

Unchanged cPyNodus II devices continue to use the legacy
`nodus-fwupdate/v1` payload containing `message_id`, `command`, and
`package_id` only.

Canonical replies:

- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`

Implemented behavior:

- Nodus subscribes to `fwupdate` in normal MQTT runtime.
- For accepted `prepare`, Nodus persists private OTA state, publishes `ack`
  and a prepared `result`, publishes offline availability/heartbeat, then
  soft-reboots into temporary OTA mode.
- OTA mode does not run MQTT. Sensorius or the CLI transfers package files
  over HTTP using the Nodus OTA endpoints.
- Sensorius verifies signed `nodus-ota/v2` packages locally, sends the exact
  signed manifest bytes, and includes the one-use session plus signature
  headers required by cPyNodus III. Unsigned `nodus-ota/v1` remains available
  for cPyNodus II compatibility.
- Sensorius allows the existing 60-second reboot settle window followed by a
  90-second OTA HTTP readiness window. Throughout both windows the operator UI
  reports `Nodus OTA mode booting...`; HTTP probe details remain diagnostic.
  If readiness still times out, Sensorius makes a best-effort `/ota/abort`
  request even though HTTP readiness was not confirmed.
- Sensorius attempts each chunked file at most three times in total and bounds
  one device update to 30 minutes. Exhausting either limit is terminal and
  triggers best-effort abort after OTA HTTP readiness.
- Once Sensorius has observed OTA HTTP readiness, failed manifest validation,
  failed file transfer, failed commit, and failed or rollback
  `fwupdate/result` notifications are terminal for that OTA attempt.
  Sensorius must make a best-effort `POST /ota/abort` request to that Nodus
  device and keep the original failure as the job error if abort also fails.
- `POST /ota/abort` is an idempotent cleanup request. Nodus should remove
  `/_ota/state.json` and staged or temporary OTA files; it should not preserve
  private OTA state by writing `phase = "aborted"`.
- Failed `POST /ota/begin` JSON parsing or manifest validation is also
  terminal on the Nodus side and should clear `/_ota/state.json` plus staged
  or temporary files before returning the error.
- If Nodus aborts OTA during startup and reboots into normal runtime, it should
  clear `/_ota/state.json` before rebooting.
- OTA package `target.platform` values verified end-to-end are `pico2w` and
  `xesp32s3`. Sensorius compares the package target with retained `mcu`
  metadata and rejects known mismatches before transfer.
- Pico 2 W has a constrained OTA memory budget. Command-line OTA testing showed
  a Nodus-side memory allocation failure when transferring a compiled
  `app.mpy` larger than about 50 KB. Treat large single-file `.mpy` updates on
  `pico2w` as unsupported until the firmware transfer/apply path is changed;
  split changes into smaller files or deploy an uncompiled/smaller app file
  when possible.
- After successful apply and reboot back into the prior profile, Nodus
  publishes a non-retained `fwupdate/result` with `phase = "applied"`,
  `applied = true`, `package_id`, and `prior_profile`.
- Sensorius accepts completion only from a result received after commit with
  the current `package_id`, or from fresh online metadata reporting the exact
  target version when the target differs from the prior version. A final
  confirmation timeout is a failed update, not a successful update.

## `meta/patch`

Canonical non-retained patch payload:

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
- It does not replace retained startup `meta`.
- Accepted config, switch, and calibration writes should emit `meta/patch`.
- Sensor-specific patch entries carry `sensor_id` and `name`; Sensorius updates
  only the matching `meta.sensors` entry and child shadow. It also updates
  `meta.sensor` when the target is the primary sensor.

## Deprecated Doc Shapes

These shapes are deprecated and should not be treated as canonical:

- `nodus/<channel_id>/set`
- switch-control docs centered on plain `ON` and `OFF`
- docs that imply ordinary runtime config writes trigger a full retained `meta`
  refresh
