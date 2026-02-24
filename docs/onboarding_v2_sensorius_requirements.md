# Sensorius Requirements: Nodus Onboarding V2 (HTTP Init + MQTT Config)

## Purpose
Define the Sensorius-side implementation required to support Nodus Onboarding V2:
1. Minimal bootstrap via `POST /itaot-init` while Nodus is in AP mode.
2. Full configuration via MQTT after Nodus reboots and joins Wi-Fi.

This document is intended as a handoff specification for the Sensorius project.

## Scope
Sensorius changes covered here:
1. Add-device orchestration and state machine.
2. HTTP bootstrap call to Nodus `/itaot-init`.
3. MQTT onboarding/config protocol handling.
4. Enrollment token issuance/validation.
5. Device identity and persistence model updates.
6. UI, observability, rollout, and compatibility requirements.

Nodus implementation details are out of scope except where they define interface contracts.

## High-Level Flow
1. User starts Add Device in Sensorius.
2. Sensorius connects to Nodus AP and calls `POST /itaot-init` with minimum bootstrap payload.
3. Nodus stores bootstrap payload and reboots.
4. Nodus joins target Wi-Fi, connects MQTT, and publishes onboarding hello.
5. Sensorius validates hello and token, then publishes full config.
6. Nodus applies config and returns config result.
7. Sensorius marks device onboarded and online.

## Sequence Diagram (Text)
```text
User -> Sensorius UI: Add Device
Sensorius -> Nodus(AP): POST /itaot-init {ssid,password,hostname,mqtt,onboard_token}
Nodus -> Sensorius: 200 ACK {accepted:true,rebooting:true}
Nodus -> Nodus: reboot
Nodus -> MQTT Broker: CONNECT
Nodus -> MQTT Broker: PUBLISH nodus/<device_id>/onboard/hello
Sensorius -> MQTT Broker: SUB nodus/+/onboard/hello
Sensorius -> MQTT Broker: PUBLISH nodus/<device_id>/config/set
Nodus -> MQTT Broker: PUBLISH nodus/<device_id>/config/ack
Nodus -> MQTT Broker: PUBLISH nodus/<device_id>/config/result
Sensorius -> User: Device onboarded
```

## API Contract: `POST /itaot-init`
### Endpoint
- `POST http://192.168.4.1:8000/itaot-init` (Nodus AP mode address shown as example)

### Request JSON (minimum bootstrap)
```json
{
  "onboard_token": "token-123",
  "ssid": "MyWiFi",
  "password": "my-password",
  "hostname": "aqi-x943fm",
  "mqtt": {
    "broker_host": "sensorius-broker.local",
    "broker_port": 1883,
    "username": "",
    "password": "",
    "use_tls": false,
    "active_profile": "sensorius"
  },
  "sensorius": {
    "instance_id": "sensorius-main",
    "base_topic": "nodus",
    "reply_topic": "sensorius/onboard/reply"
  }
}
```

### Required Fields
1. `onboard_token`
2. `ssid`
3. `password`
4. `hostname`
5. `mqtt.broker_host`
6. `mqtt.broker_port`

### Expected Success Response (example)
```json
{
  "success": true,
  "accepted": true,
  "rebooting": true,
  "restart_mode": "hard",
  "hostname": "aqi-x943fm",
  "mqtt_profile": "sensorius",
  "base_topic": "nodus"
}
```

### Operational Guardrails
Nodus may reject bootstrap early under protection rules:

1. `413 Payload Too Large` when request body exceeds bootstrap limit.
2. `503 Service Unavailable` during low-memory protection windows.
3. `400 Bad Request` for malformed JSON or missing required fields.

### Error Handling
Sensorius must treat non-200 or malformed response as `INIT_FAILED` and stop progression to MQTT phase.

## MQTT Contract
## Topic Conventions
1. Nodus hello:
   - `nodus/<device_id>/onboard/hello`
2. Full config set (Sensorius -> Nodus):
   - `nodus/<device_id>/config/set`
3. Config ack (Nodus -> Sensorius):
   - `nodus/<device_id>/config/ack`
4. Config apply result (Nodus -> Sensorius):
   - `nodus/<device_id>/config/result`
5. Runtime metadata (retained):
   - `nodus/<device_id>/meta`

### `onboard/hello` Payload (example)
```json
{
  "onboard_token": "token-123",
  "device_id": "aqi-x943fm",
  "hostname": "aqi-x943fm",
  "serial": "x943fm",
  "type": "pico2w",
  "version": "v0.26.055.0",
  "capabilities": {
    "sensor": true,
    "switch": true
  }
}
```

### `config/set` Payload (accepted shape A: updates list)
```json
{
  "message_id": "cfg-20260223-001",
  "onboard_token": "token-123",
  "payload": {
    "updates": [
      {"section": "Network", "key": "HOSTNAME", "value": "aqi-x943fm"},
      {"section": "Display", "key": "METRIC_1", "value": "Air Quality"}
    ]
  },
  "restart": false
}
```

### `config/set` Payload (accepted shape B: settings map)
```json
{
  "message_id": "cfg-20260223-002",
  "onboard_token": "token-123",
  "payload": {
    "settings": {
      "Display": {
        "METRIC_1": "Air Quality",
        "METRIC_2": "Temperature"
      }
    }
  }
}
```

Important:
1. Nodus currently expects `payload.updates[]` or `payload.settings{}`.
2. A nested `payload.network/sensor/switch/...` document is not consumed unless transformed into one of the accepted shapes above.

### `config/ack` Payload (example)
```json
{
  "message_id": "cfg-20260223-001",
  "accepted": true
}
```

### `config/result` Payload (example)
```json
{
  "message_id": "cfg-20260223-001",
  "applied": true,
  "updated": 2,
  "error": ""
}
```

Duplicate replay behavior (idempotent):
```json
{
  "message_id": "cfg-20260223-001",
  "accepted": true,
  "duplicate": true
}
```

### Nodus Enforcement Checklist (Required)
1. Persist `onboard_token` from `/itaot-init` and require the same token in:
   - `onboard/hello`
   - `config/set`
2. Keep token valid until `config/result.applied == true`, then invalidate it.
3. Reject `config/set` when token is missing/mismatch/expired/already used.
4. Process `payload.settings` as the canonical full-config shape.
5. Ignore unknown top-level fields in `config/set` (forward compatibility), including:
   - `checksum`
   - `config_version`
6. Publish `config/ack` for every received `config/set`, including duplicates.
7. On duplicate `message_id`, return idempotent ack:
   - `accepted: true`
   - `duplicate: true` (recommended)
8. Publish `config/result` once per unique `message_id` with stable replay semantics.
9. Correlate and deduplicate by `device_id + message_id`, not timestamp.
10. Do not require NTP-synchronized timestamps for onboarding correctness.

### Standard Error Values (Recommended)
Use stable short error strings to keep UI and recovery behavior predictable:
1. `token_invalid`
2. `token_expired`
3. `token_already_used`
4. `config_rejected`
5. `schema_invalid`
6. `apply_failed`
7. `not_ready`

### `meta` Payload Field Notes
`nodus/<device_id>/meta` (`schema = "nodus-meta/v1"`) includes:
1. `sensor.display_metrics` for Sensorius TOML `[Display]` materialization.
2. `switch.channels[*]` with `channel_id`, `event_topic`, `state_topic`, `set_topic`, and `availability_topic`.
3. `location_group` grouping metadata.

Sensorius should treat `meta` as eventual (it may be deferred briefly on low memory) and should not block onboarding success on immediate meta arrival.

## Sensorius Add-Device State Machine
States:
1. `AP_DISCOVERED`
2. `INIT_SENDING`
3. `INIT_SENT`
4. `WAITING_REBOOT`
5. `WAITING_MQTT_HELLO`
6. `CONFIG_SENDING`
7. `WAITING_CONFIG_ACK`
8. `WAITING_CONFIG_RESULT`
9. `ONLINE`
10. `FAILED`

Transition requirements:
1. Persist state transitions to durable storage.
2. Resume safely after Sensorius process restart.
3. Ignore stale/duplicate transitions using `device_id + token + message_id`.

## Enrollment Token Requirements
1. Short TTL (recommended: 5-10 minutes).
2. Single-use.
3. Bound to onboarding session and expected device identity where possible.
4. Replay must be rejected.
5. Token invalidated after successful config apply.

## Device Identity Model Requirements
1. Primary identity should be stable (`device_id`/serial/hostname), not IP.
2. IP address should be runtime metadata only.
3. Store hostname and identifiers from hello payload.
4. Maintain `last_seen`, `last_hello`, and onboarding state.

## Reliability Requirements
1. Retries:
   - `config/set` retry with bounded attempts and backoff.
2. Idempotency:
   - Resending same `message_id` must be safe.
3. Correlation:
   - Accept `ack/result` only for active onboarding session and current `message_id`.
4. Timeout policy:
   - Explicit deadlines for hello, ack, and result phases.

## Security Requirements
1. Do not log plaintext credentials/tokens.
2. Protect secrets in storage.
3. Apply topic-level ACLs for onboarding/config topics.
4. Validate payload schema before use.

## UI/UX Requirements
Add Device workflow should display:
1. Current stage.
2. Stage-specific timeout/retry status.
3. Actionable failure messages:
   - init failed
   - hello timeout
   - token invalid/expired
   - config rejected
   - config apply failure
4. User controls:
   - retry current stage
   - restart onboarding

## Observability Requirements
Emit structured events:
1. `onboarding_init_sent`
2. `onboarding_init_ack`
3. `onboarding_hello_received`
4. `onboarding_config_sent`
5. `onboarding_config_ack`
6. `onboarding_config_result`
7. `onboarding_failed`
8. `onboarding_online`

Key metrics:
1. Success rate by device type/version.
2. Time to first hello.
3. Time to config applied.
4. Failure reasons distribution.

## Compatibility and Rollout
1. Add feature flag: `onboarding_v2_mqtt`.
2. Keep legacy onboarding available during migration.
3. Fallback behavior:
   - if `/itaot-init` unavailable, optionally route to legacy flow.
4. Rollout strategy:
   - dev -> staging -> limited production cohort -> full rollout.

## Testing Requirements
Minimum test coverage:
1. Unit tests:
   - token lifecycle
   - state transitions
   - payload validation
2. Integration tests:
   - full happy path AP init -> MQTT hello -> config applied
3. Fault injection:
   - duplicate hello
   - missing ack/result
   - Sensorius restart mid-onboarding
   - token expiry/replay
   - broker transient disconnect

## Acceptance Criteria
1. Nodus onboarding completes without requiring device IP after reboot.
2. Sensorius can resume onboarding after restart.
3. Duplicate MQTT messages do not corrupt state.
4. Replay/expired tokens are rejected.
5. UI provides clear progress and errors.
6. Legacy onboarding remains functional while feature flag is disabled.
