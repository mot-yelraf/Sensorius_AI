# cPyNodus Handoff: Onboarding V2 Without `settings.toml` Schema Changes

## Purpose
Define the cPyNodus-side contract for Onboarding V2 while preserving the current Nodus `settings.toml` schema.

This design keeps onboarding/session metadata out of `settings.toml` and treats it as protocol/runtime state.

## Non-Goal
Do **not** add new persistent keys to Nodus `settings.toml` for onboarding state, token state, message IDs, retries, or session tracking.

## High-Level Flow
1. Sensorius joins Nodus AP and sends `POST /itaot-init` with minimum bootstrap payload.
2. Nodus validates payload shape, applies only existing network/MQTT fields, stores onboarding runtime state outside `settings.toml`, responds `200` ack, then reboots.
3. After Wi-Fi + MQTT connect, Nodus publishes onboarding hello.
4. Sensorius validates token/session and publishes full config.
5. Nodus acks config receipt, applies config, publishes apply result.
6. Sensorius marks onboarding complete and invalidates token.

The full config includes `payload.settings.Time` with the Sensorius hub's
active `TZ`, `TZ_OFFSET`, and `TZ_NAME`. These are ordinary existing `[Time]`
settings, not onboarding/session metadata.

## HTTP Contract
### Endpoint
- `POST /itaot-init`

### Request (minimum bootstrap)
```json
{
  "onboard_token": "token-123",
  "ssid": "MyWiFi",
  "password": "my-password",
  "hostname": "aqi-x943fm",
  "mqtt": {
    "broker_host": "sensorius-broker.local",
    "broker_port": 1883
  }
}
```

### Required behavior on Nodus
1. Validate required fields and types.
2. Apply only existing settings fields already supported by current settings model.
3. Do not persist onboarding protocol metadata into `settings.toml`.
4. Return JSON success ack before reboot when possible:
```json
{
  "accepted": true,
  "rebooting": true
}
```

## MQTT Contract (Canonical)
1. Hello: `nodus/<device_id>/onboard/hello`
2. Config set: `nodus/<device_id>/config/set`
3. Config ack: `nodus/<device_id>/config/ack`
4. Config result: `nodus/<device_id>/config/result`

`device_id` is authoritative identity.

## Runtime State Placement on Nodus
Because `settings.toml` is unchanged, onboarding runtime state should be held in one of these:
1. In-memory state only (preferred when flow window is short and reboot timing is deterministic).
2. Ephemeral file (for example `onboarding_state.json`) with TTL, removed after success or expiry.

Allowed runtime state fields:
- `onboard_token` (or secure hash)
- `token_expires_at`
- `session_started_at`
- `last_message_id` (for idempotency guard)
- `pending_config_version`

Do not merge these into persistent config schema.

## Token Rules
1. Token TTL target: 5-10 minutes.
2. Token is single-use.
3. Replay is rejected.
4. Token invalidated after successful config apply.

## `onboard/hello` Payload
```json
{
  "onboard_token": "token-123",
  "device_id": "aqi-x943fm",
  "hostname": "aqi-x943fm",
  "serial": "x943fm",
  "type": "pico2w",
  "version": "v0.26.053.0",
  "capabilities": {
    "sensor": true,
    "switch": true
  }
}
```

## `config/set` Handling Rules
1. Parse and validate payload schema.
2. Use `message_id` for idempotency:
   - duplicate `message_id` should be safe.
3. Publish `config/ack` immediately when accepted.
4. Publish `config/result` after apply attempt.
5. Apply `payload.settings.Time.TZ`, `payload.settings.Time.TZ_OFFSET`, and
   `payload.settings.Time.TZ_NAME` when present.

## Failure Behavior
1. Invalid/expired token: reject onboarding/config flow and publish negative `config/result` when applicable.
2. Schema invalid: reject with explicit error.
3. Apply failure: publish `config/result` with `applied=false` and `error` string.

## Logging and Security
1. Never log plaintext Wi-Fi password, MQTT password, or onboarding token.
2. If token is persisted in ephemeral file, prefer hash-at-rest.
3. Keep runtime onboarding artifacts short-lived.

## Compatibility Notes
1. This flow assumes all deployed Nodus devices are updated to V2 onboarding/ingesting behavior.
2. Legacy onboarding path does not need to be used for V2-enabled cohorts.

## Acceptance Criteria (Nodus side)
1. V2 onboarding succeeds without adding keys to `settings.toml`.
2. Device publishes hello on canonical V2 topic after reboot.
3. `config/ack` and `config/result` follow `message_id` correlation.
4. Duplicate `config/set` with same `message_id` is idempotent.
5. Expired/replayed token is rejected.
