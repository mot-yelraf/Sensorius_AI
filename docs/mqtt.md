# MQTT

This page is the operational overview for Sensorius MQTT behavior. The detailed
forward contract between Nodus firmware and Sensorius is
`docs/sensorius_contract.md`. If this overview conflicts with the contract,
the contract wins.

## MQTT Roles

Sensorius uses MQTT in three ways:

- Local sensor publishing through `saiMQTTClient.py` when local Pi sensors need
  to publish to a non-local broker.
- Remote discovery and ingest through `saiMQTTIngest.py`.
- Home Assistant discovery, state, availability, and command routing through
  `saiHomeAssistantMqtt.py`.

The normal Nodus runtime path is MQTT-first. AP-mode HTTP is only for bootstrap
and diagnostics.

## Broker Settings

Primary Nodus broker settings live in `[SensorNetwork]`:

```toml
[SensorNetwork]
BROKER = "localhost"
MQTTPORT = 1883
USE_TLS = false
NODUS_DEBUG_DATA_ONLY = false
LEGACY_FIRMWARE_HOSTS = []
LEGACY_POLLER_SUNSET_DATE = "2026-06-30"
```

Home Assistant can use the same broker or a separate broker through
`[HomeAssistant].HA_BROKER` and `[HomeAssistant].HA_MQTTPORT`.

## Startup Behavior

- `saiMQTTIngest` starts when `SensorNetwork.BROKER` is set.
- Local sensor MQTT publishers are skipped when the broker is unset, local, or
  treated as this host.
- Ingest subscribes to Nodus topic families, optional base-topic mirrored
  families, calibration topics, onboarding topics, and optional WeeWX MQTT
  topics.
- If Home Assistant uses a separate broker, ingest opens a second Paho client.

## Current Topic Families

Nodus device topics:

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<device_id>/meta/switch`
- `nodus/<device_id>/meta/patch`
- `nodus/<device_id>/onboard/hello`
- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`
- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<device_id>/event/calibration_status`
- `nodus/<device_id>/event/calibration_progress`
- `nodus/<device_id>/event/calibration_sample`
- `nodus/<device_id>/event/calibration_result`
- `nodus/<device_id>/fwupdate`
- `nodus/<device_id>/fwupdate/ack`
- `nodus/<device_id>/fwupdate/result`

Nodus sensor and switch topics:

- `nodus/<sensor_id>/data`
- `nodus/<sensor_id>/availability`
- `nodus/<channel_id>/state`
- `nodus/<channel_id>/event`
- `nodus/<channel_id>/availability`
- `nodus/<channel_id>/config/set`
- `nodus/<channel_id>/config/ack`
- `nodus/<channel_id>/config/result`

Sensorius/Home Assistant topic families use the configured
`[HomeAssistant].BASE_TOPIC`, default `sensorius`.

## Discovery And Shadow State

Retained `nodus/<device_id>/meta` is the compact authoritative startup
snapshot. Retained `nodus/<device_id>/meta/switch` is the authoritative switch
channel topic map when switch channels are present. Sensorius uses them to:

- Register sensor data topics.
- Register switch event, state, command, availability, ack, and result topics.
- Seed or update local shadow settings under `sensor_settings/`,
  `switch_settings/`, and `system_settings/`.
- Register switch identities in the database.
- Publish Home Assistant discovery when enabled.

Accepted runtime changes should produce non-retained `meta/patch` messages.
Sensorius applies correlated patches to local shadow settings after config or
calibration commands.

## Commands

Command topics are not state topics.

- Ordinary device config uses `nodus/<device_id>/config/set`.
- Switch config/control uses `nodus/<channel_id>/config/set`.
- Calibration uses `nodus/<device_id>/calibration/set`.
- Sensorius keeps at most one live switch `config/set` in flight per channel.
  Duplicate requests for the same desired state are coalesced until the
  correlated result, state, event, or `meta/patch` is observed; conflicting
  requests are rejected until the in-flight command clears or expires.
- Publish `/set` commands non-retained by default.
- If Sensorius intentionally publishes a retained `/set` command, Sensorius
  owns clearing it with an empty retained payload to the same topic after a
  successful result.

`saiMQTTIngest.publish_text` refuses retained command publishes to `/set`
topics unless the payload is an empty retained cleanup payload.

For diagnostics, `GET /debug/mqtt-retained-commands` performs a short-lived
read-only scan for retained non-empty Nodus `/set` payloads. The response
redacts values and reports only topic names, payload size, message IDs, and
section/key names so stale command clues can be inspected without exposing
credentials.

## Switch State Ownership

Remote switch commands should use the shared controller path:

1. UI, Home Assistant, or automation calls `set_state(...)`.
2. `RemoteSwitchController` resolves the label and channel ID.
3. `saiMQTTIngest.set_switch(...)` publishes one channel-scoped Nodus command.
4. Authoritative Nodus state or event topics update ingest caches.
5. `saiDataLogger.log_switch_event` records transitions in `sw_events`.

Do not write switch state directly to the database or publish ad hoc switch
topics from route handlers.

## Liveness

Use MQTT liveness for onboarded devices:

- Heartbeats: `nodus/<device_id>/status/heartbeat`
- Availability: `nodus/<device_id>/availability` and channel availability
- Retained metadata replay on reconnect

For non-retained live heartbeats, Sensorius uses broker receipt time for
liveness if the payload `timestamp` is skewed far enough to look stale, such as
when a Nodus publishes an epoch shifted by the current GMT offset. Retained
heartbeats still use the payload timestamp so stale broker replays do not mark a
device online.

Periodic `/hayd` and `/itaot` polling is deprecated for steady-state health.
`/itaot-meta` may still be used for AP-mode/bootstrap or user-initiated
diagnostics.

## Mirroring And Passthrough

Home Assistant settings include:

- `NODUS_PASSTHROUGH`: keep direct Nodus topic subscription behavior enabled in
  the ingest layer.
- `MIRROR_NODUS`: mirror discovered Nodus traffic to the HA broker when
  configured.

Mirroring avoids echoing command topics back to HA.

## Deprecated Shapes

Do not introduce new behavior based on these old shapes:

- `nodus/<channel_id>/set`
- Switch-control docs centered on plain `ON` and `OFF` command topics.
- Runtime config writes that require a full retained `meta` republish.
- Ongoing health polling through `/hayd` or `/itaot`.

## Troubleshooting

No Nodus device appears:

- Check broker host/port and credentials.
- Check retained `nodus/<device_id>/meta`.
- For switches, check retained `nodus/<device_id>/meta/switch`.
- Check heartbeat recency.
- Check AP isolation or guest-network isolation.
- Use the UI retry-discovery action after confirming metadata exists.

Remote switch command does not work:

- Confirm `SWITCH_N_CHANNEL_ID` is present and stable.
- Confirm command, state, and event topics are present in retained `meta/switch`.
- Confirm no enabled Advanced automation is overriding manual control.
- Confirm state/event topic feedback arrives after a command.

MQTT publish stall on Pico 2 W:

- If Nodus logs local publish success but broker-observed traffic stops after
  startup, save TOML files, flash `flash_nuke.uf2`, flash fresh CircuitPython,
  deploy a clean Nodus build, and restore TOML files. A plain CircuitPython
  reflash may not clear this failure mode.
