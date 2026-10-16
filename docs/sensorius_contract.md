# Sensorius Contract

This document is the canonical forward-only contract between current
`cPyNodus_II` / `cPyNodus_III` firmware and current Sensorius.

Sensorius and the supported Nodus projects move together. Old topic names and
compatibility shims do not define the contract. When other docs drift, this
document wins.

## Cross-project contract maintenance

The `docs/sensorius_contract.md` files in Sensorius_AI, cPyNodus_II, and
cPyNodus_III describe the same app/device interfaces. Changes to shared topic
names, field names, accepted values, correlation, retention, state ownership,
or user-visible command annotations must be reflected in all three copies.
Keep board/profile-specific behavior explicit rather than copying one
firmware's implementation claims into another project's contract.

Required behavior and current implementation support are distinct. An
implementation gap must remain documented until the corresponding code and
verification exist; updating a contract does not establish deployed support.

## Principles

- AP bootstrap uses only `/itaot-meta` and `/itaot-init`.
- Normal runtime sync uses only MQTT.
- OTA uses MQTT only for the prepare/result control path; package bytes move
  over HTTP while Nodus is in temporary OTA mode.
- Nodus publishes retained compact `meta` on connect/reconnect.
- Nodus publishes retained `meta/switch` with detailed switch channel topics in
  the startup identity publish batch when switch channels are present.
- Nodus II publishes retained `meta/config` with saved display and configuration
  settings alongside compact startup metadata. Sensorius consumes both snapshots.
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
display names from `script_json.name`, falling back to the rule ID only when
the name is missing or blank. Nodus displays these strings directly; renaming
a rule updates the retained status on the next publisher scan.
An empty list explicitly clears prior ownership. The retained
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
Current Nodus firmware publishes one canonical SI value for each convertible
environmental metric, such as `Temperature` and `Dew Point` in degrees C. It
does not publish duplicate display-unit metrics such as `Temperature_F` or
`Dew Point_F`; Sensorius applies the user's Metric or Imperial selection at
presentation time. Sensorius continues to accept imperial-suffixed keys from
older Nodus firmware and preserves those legacy metric identities when storing
their readings.
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
  `sensor.event_topic`, `sensor.availability_topic`
- `sensor.display_metrics`, `sensor.display_styles` in full metadata (including
  Nodus III); compact Nodus II metadata advertises `config_topic` and supplies
  these fields in retained `meta/config` instead
- optional `sensor.calibration.Device.ALTITUDE_METERS`, reflecting the device
  TOML calibration altitude in metres (number or numeric string); Sensorius
  mirrors explicit values, including zero, and preserves existing shadow
  calibration when the field is omitted by older firmware
- optional `sensors` array for a multi-sensor Nodus; every entry carries its
  own `sensor_id`, logical device, hardware, `config_file`, location, display
  settings, calibration altitude, data topic, event topic, and availability topic
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

## Switch state ownership and synchronization between UIs

This section defines the required behavior for Sensorius and Nodus implementations.
Firmware agents should review their implementation against the checklist below;
this clarification does not claim that every existing firmware version already
implements the complete flow.

### Device-owned source of truth

- Nodus owns the logical channel state and its `SWITCH_<n>_LAST_STATE` values in
  the device's `/switch.toml`. Sensorius settings files are local shadows, not
  an alternative owner of the Nodus configuration.
- Both MQTT commands and local Nodus web controls must use the same switch
  application/persistence rules. After a durable successful change, the device
  file, in-memory runtime configuration, logical output state, and subsequent
  MQTT reports must agree. Hardware pin readback alone is not independent proof
  of the attached relay/contact state.
- Preserve the existing volatile-operation fallback when the filesystem cannot
  be written, but report `persistence_mode = "volatile"` and the persistence
  failure in the command/HTTP result. A volatile applied change must not be
  described as saved to `/switch.toml`. Missing persistence metadata on older
  firmware means durability is unknown, not proven.
- In-memory configuration must be updated after an accepted MQTT switch change;
  writing the file without updating the runtime copy is insufficient. Retained
  metadata refreshes must use a current device snapshot, not a stale cached
  `last_state` value.

### Manual change from the Sensorius UI

1. Sensorius resolves the stable channel ID and sends one non-retained,
   correlated channel `config/set` command using the existing envelope.
2. Publication or `config/ack` means requested/accepted, not a confirmed state
   change. Sensorius keeps the last device-confirmed state visible and exposes
   the requested state separately as pending. It does not synthesize a switch
   transition, update its shadow as though the request succeeded, or start the
   manual auto-off countdown merely because MQTT publication succeeded.
3. Nodus applies the logical output, synchronizes runtime configuration, attempts
   the device-file write, and emits the existing correlated `config/result`,
   JSON `event`, retained `state`, and non-retained device `meta/patch` reports.
   Their values must describe the same applied state. Report persistence status
   separately from output application success.
4. Sensorius confirms the state from a matching channel state report, a live
   canonical switch event, or an explicit state patch; a successful result with
   no state report does not by itself replace the displayed state. Matching
   confirmation clears pending and records the actual transition once through
   `log_switch_event`. Rejects and confirmation timeouts remain visible without
   changing the last confirmed state. Repeated same-state requests must not
   restart the confirmation deadline.

### Manual change from the Nodus UI

1. Nodus applies and persists through the same device-owned path and synchronizes
   the in-memory runtime configuration. The local HTTP result reports application
   and persistence outcomes.
2. When MQTT is available, Nodus publishes a JSON channel `event`, retained
   channel `state`, and non-retained device `meta/patch` for the change. Use a
   fresh action `message_id` shared by these correlated JSON reports and
   `source = "manual/web"` on the event and patch. No Sensorius-issued command
   or MQTT command acknowledgment is required for a local web action.
3. Sensorius accepts these unsolicited live reports, updates its confirmed-state
   cache and shadow, records the transition once, and updates the dashboard.
   It must not echo the received state back as another switch command.
4. If MQTT was unavailable, a later reconnect must publish a current retained
   channel state and current metadata. Missed intermediate local events must not
   be invented as historical transitions.

### Snapshot and live-report precedence

- Retained `meta` and `meta/switch` remain authoritative for discovery, identity,
  channel labels, and topic wiring. Their state fields bootstrap an unknown
  channel but must not overwrite a channel state already learned from device
  runtime reports during the ingest session. This protects against older
  firmware publishing stale cached configuration snapshots; it does not transfer
  ownership of state to Sensorius.
- Channel `state` reports accept existing plain `ON`/`OFF` and JSON shapes. Live,
  non-retained `nodus-switch-event/v1` events may also confirm a state change.
  Retained/replayed events and legacy event-only history payloads may update
  history but must not roll the current display backward.
- An explicit non-retained `meta/patch` update to
  `Switch.SWITCH_<n>_LAST_STATE` is a device runtime confirmation. Replaying a
  discovery snapshot is not an explicit state patch.
- A report of the previous state while a different state is requested must not
  silently mark that request confirmed. Reports with distinct channel IDs must
  never affect each other's pending state, labels, or history.
- Refreshing metadata, receiving events, and rendering either UI must not send
  a new switch command. Startup metadata replay must not create synthetic switch
  events. Keep the existing topic and payload compatibility paths.

### Firmware implementation review checklist

For each supported Nodus firmware, check the local web state handler, MQTT switch
command handler, runtime configuration update, persistence result, and deferred
metadata publisher together. In particular:

Implementation review entry points (under `cpynodus_ii/` or `cpynodus_iii/`):
`features/command_handlers.py::process_switch_command_message`,
`features/web_handlers.py::handle_switch_state_request`,
`features/web_config.py::apply_web_switch_override`, the local
`/set-switch-state` handler in `features/web_runtime.py`,
`features/publish_cycle.py::publish_metadata_refresh_cycle`, and its call site
in `app.py`. Reuse the existing state/event publication helpers while preserving
lazy imports and the shallow command stacks. The cPyNodus III v0.26.288.2 review
found the local web action requesting only metadata refresh, the deferred
refresh call omitting a live switch snapshot, and the MQTT switch handler not
propagating updated runtime configuration. Review cPyNodus II for the same gaps
rather than assuming identical behavior.

- Return/propagate updated runtime configuration after MQTT channel commands.
- Supply a fresh switch snapshot when deferred metadata is constructed.
- Queue state/event/patch publication after a local web change; a full metadata
  refresh alone is not the runtime synchronization flow.
- Preserve constrained heap/pystack behavior, lazy command imports, non-retained
  commands, and existing filesystem fallback semantics.
- Add host tests for both UI origins, stale metadata after a live state change,
  repeated metadata refresh, failed persistence, disconnected/reconnect behavior,
  two channels with opposite states, and duplicate state/event/patch delivery.
- Verify on hardware with broker-visible captures. Check the device file,
  runtime/device UI, physical output, and Sensorius UI separately; host tests
  alone do not establish GPIO correctness or broker delivery.

## Switch `config/set`

Canonical topic:

- `nodus/<channel_id>/config/set`

Canonical switch-control envelope:

```json
{
  "message_id": "cfg-123",
  "reason": "manual",
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
- Switch reason/name annotations follow the shared requirements below.
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

### Switch reasons and rule names (shared app/device requirements)

These requirements apply to both Nodus generations and Sensorius. The support
matrix below records current source support, not a claim about deployed devices.

- `reason` is a top-level command-envelope string, outside `payload.updates`.
  Sensorius user toggles send `"manual"`; automation actions send `"auto"`.
  Local Nodus web controls and local automations must supply the same distinction
  to device history. Transport (`mqtt`) and operation (`switch_set`) are separate
  from the action reason; automation ownership alone does not establish cause.
- Named automation commands also carry top-level `rule_name`, containing the
  rule's display name. Preserve the complete name, including colons. Omit this
  field for manual commands or when no rule name is known. It is annotation,
  not a configuration update or an instruction to evaluate a rule on Nodus.
- Keep `reason` exactly `"auto"`; never send `"auto - Cooling"` in that field.
  Both UIs should render a known name as `auto - Cooling`, falling back to
  `auto` when only the reason is known, and `manual` for a manual action.
- Missing or unsupported reasons remain unknown. Do not infer a reason from
  the current automation owner, a matching On/Off value, or the transport.
  Existing unknown historical transitions cannot be classified retroactively.
  A missing rule name must not invalidate a valid automatic action.
- Required receiver behavior is to preserve the reason and available rule name
  with the actual applied transition. A queued publish or ACK is not a
  transition. Match action metadata by channel and `message_id` when available;
  do not transfer annotations between channels or unrelated command IDs.
- Repeated reports of the same recorded transition must preserve its known
  reason, name, and last-change timestamp. Generic state refreshes must not
  create new unknown-origin events. A later opposite transition without a known
  cause remains unknown; it must not inherit the previous action's reason.
- Existing plain `ON`/`OFF` state reports remain valid and carry no reason or
  action ID. Do not require a JSON state conversion or extra publications for
  this annotation. JSON reports that carry action annotations use the same
  top-level `reason` and `rule_name` names alongside existing identity and
  correlation fields. Their absence is not a protocol error or proof of cause.
- The annotation does not change command topics, QoS, retain rules, ACK/result
  semantics, physical channel identity, or persistence-success reporting.

Named automation command example on `nodus/<channel_id>/config/set`:

```json
{
  "message_id": "cfg-auto-123",
  "reason": "auto",
  "rule_name": "Cooling",
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

Current source support reviewed 2026-10-16:

| Project | Implemented | Remaining work |
| --- | --- | --- |
| Sensorius | Sends `reason` and known `rule_name`; accepts these fields in JSON channel state/event reports; preserves its recorded transition annotation and timestamp on repeated reports; displays `auto - <rule name>`. | Verify the app/device flow on deployed firmware. |
| cPyNodus II | Existing state command handling accepts the envelope but does not read `reason` or `rule_name`. | Implement reason/name handling and corresponding history/display support where available; verify on-device. |
| cPyNodus III | Reads `reason: "manual"` / `"auto"` from channel commands and records it in local history. Local web controls and automations supply their reasons. | Read and preserve `rule_name` for named history/display. Current MQTT reports do not echo these annotations; reason/name report support must be implemented before relying on it. Verify on-device. |

Shared acceptance checks: manual toggles from either UI, named automation
commands, unnamed automatic commands, omitted/invalid reasons, rule names with
colons, duplicate state/event/patch delivery after temporary command metadata
expires, mismatched command IDs/channels, and a later unannotated opposite
transition. Validate stored history and both UI labels as well as switch state.
Host tests do not establish physical output behavior or broker delivery.


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

## Saved configuration companion (`meta/config`)

Nodus II publishes retained `nodus/<device_id>/meta/config`, advertised by
`config_topic` in compact `meta`, with schema `nodus-meta-config/v1`. Its
`sensor` block identifies `sensor_id` and `config_file` and includes ordered
`display_metrics`, matching `display_styles`, and saved calibration. The
companion also carries `time` and `homeassistant` settings. Saved device TOML
values are authoritative, including after device-side edits and a restart.

Sensorius subscribes to both snapshots (including base-prefixed topics),
accepts either arrival order, and merges configuration into its discovery
cache and `/home/<user>/Sensorius/sensor_settings/<sensor_id>/sensor.toml`.
Missing display fields must preserve existing shadow settings; factory defaults
are only used to seed missing settings. Full metadata remains supported.
Metric slots must keep their positions, including blank slots and temporary
duplicates during a reorder. Styles belong to the corresponding slot.

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


## Standalone device halt

Both cPyNodus II and III advertise halt support in retained
`nodus/<device_id>/meta` with `capabilities.halt = true`. Sensorius lists only
explicit boolean true capabilities in **Halt Device(s)** and rechecks support
before publishing. A physical device is listed once, independently of its
sensor and switch children.

After user acknowledgement, Sensorius publishes non-retained, QoS 1 to
`nodus/<device_id>/config/set`:

```json
{"message_id":"halt-001","payload":{},"halt":true}
```

`halt-001` is an example correlation ID, not an operation name. Sensorius uses
a fresh `halt-<uuid>` for each request to preserve Nodus message deduplication.
Halt carries no settings updates or restart fields. Existing device token
validation still applies. `config/ack` confirms acceptance only; Sensorius
requires a correlated `config/result` from the target device with
`applied = true`, `halt = true`, `phase = "halted"`, and no error before
reporting ready to unplug. `relays = "held"` describes unchanged relay outputs.

A missing result leaves completion unconfirmed even if the device goes offline.
`filesystem_sync_failed` is reported as a failure. Commands are not retried
automatically. Successful halt stops application writes and syncs the device
filesystem; host USB writes are outside this contract. Power cycle or reset
resumes the device. Halting does not remove Sensorius settings, history,
retained discovery, or Home Assistant entities.
