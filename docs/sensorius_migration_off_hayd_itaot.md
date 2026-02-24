# Sensorius Migration: Move Off `/hayd` and `/itaot` for Nodus Health

## Purpose
Define how `saiSensorius` should monitor Nodus health after onboarding without periodic HTTP polling.

This migration is driven by observed instability correlated with repeated `/hayd` and `/itaot` requests on constrained Nodus devices.

## Scope
This applies to **post-onboarded** devices (already on Wi-Fi and connected to MQTT).

## Summary
1. `/hayd` is deprecated for ongoing health checks.
2. `/itaot` is deprecated for ongoing health/status polling.
3. MQTT is the authoritative health/liveness channel after onboarding.
4. `/itaot-meta` is optional and should be used sparingly (on-demand UI metadata), not as a poll target.

## New Health Model (Authoritative)
Sensorius should use MQTT for liveness:

1. Heartbeat topic:
   - `nodus/<device_id>/status/heartbeat`
   - Retained JSON payload:
   ```json
   {
     "device_id": "aqi-x943fm",
     "status": "online",
     "timestamp": 1760000000
   }
   ```
2. Availability topics (existing):
   - Sensor: `nodus/<sensor_id>/availability`
   - Switch channels: `nodus/<channel_id>/availability`
   - Retained payload: `online` / `offline`
3. Sensor data stream (if sensor-capable):
   - `nodus/<sensor_id>/data`

## Recommended Sensorius Liveness Policy
1. Subscribe to:
   - `nodus/+/status/heartbeat`
   - `nodus/+/availability` (and switch availability patterns already in use)
2. Update `last_seen` on any heartbeat/data/availability message.
3. Mark device degraded/offline if no heartbeat for `>= 90s` (3x 30s interval), unless a stricter policy is needed.
4. Prefer heartbeat as canonical liveness; use availability/data as supplemental signals.

## Route Usage by Lifecycle

### After onboarding (steady state)
1. Do **not** poll `/hayd`.
2. Do **not** poll `/itaot`.
3. Use MQTT only for health.
4. Use `/itaot-meta` only for user-initiated enrichment (for example: device details panel open), not background polling.

### During onboarding
1. `POST /itaot-init` is still required for AP bootstrap flow.
   - It seeds Wi-Fi + MQTT settings and triggers reboot to join target network.
2. After reboot, use MQTT onboarding flow:
   - `nodus/<device_id>/onboard/hello`
   - `nodus/<device_id>/config/set`
   - `nodus/<device_id>/config/ack`
   - `nodus/<device_id>/config/result`
3. `/itaot` is **not required** for onboarding success in V2.
4. `/itaot-meta` is optional for UI enrichment only.

## Can Sensorius Avoid HTTP for Onboarding Too?
1. For AP-mode bootstrap: **No** (not today).
   - Sensorius still needs `POST /itaot-init` while Nodus is on setup AP.
2. For post-reboot provisioning/config: **Yes**.
   - MQTT onboarding/config exchange fully covers this path.

## Data Sensorius Should Persist Per Device
1. `device_id`
2. `last_seen_ts`
3. `last_heartbeat_ts`
4. `online_state` (online/degraded/offline)
5. `last_heartbeat_payload`
6. onboarding session fields (`token`, `message_id`, state machine step) while onboarding is active

## Rollout Plan
1. Add feature flag in Sensorius:
   - `nodus_health_via_mqtt = true`
2. When enabled:
   - disable `/hayd` poller
   - disable periodic `/itaot` fetch
   - enable heartbeat-based liveness
3. Keep `/itaot-meta` as on-demand diagnostics only.
4. Remove legacy poll paths after stable bake period.

## Acceptance Criteria
1. Sensorius no longer polls `/hayd` for onboarded devices.
2. Sensorius no longer polls `/itaot` for onboarded devices.
3. Device liveness state is derived from MQTT heartbeat/availability/data.
4. Onboarding V2 succeeds using `/itaot-init` + MQTT exchange without `/itaot`.

