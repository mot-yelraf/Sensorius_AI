# MQTT

This file is a short overview. The canonical forward-only contract between
Sensorius and `cPyNodus_II` now lives in [docs/nodus_contract.md](./nodus_contract.md).

If this page and the contract page ever disagree, `docs/nodus_contract.md`
wins.

## Current Contract Summary

- AP bootstrap uses `/itaot-meta` and `/itaot-init`.
- Runtime device config uses `nodus/<device_id>/config/set`.
- Runtime switch config uses `nodus/<channel_id>/config/set`.
- Calibration uses `nodus/<device_id>/calibration/set`.
- Nodus publishes retained `nodus/<device_id>/meta` on connect/reconnect.
- Nodus publishes non-retained `nodus/<device_id>/meta/patch` after accepted
  runtime changes.
- Sensorius paces ordinary runtime config writes one key at a time per
  physical Nodus host and waits for `ack` plus successful `result`.

## Current Topic Families

- `nodus/<device_id>/status/heartbeat`
- `nodus/<device_id>/meta`
- `nodus/<device_id>/meta/patch`
- `nodus/<device_id>/onboard/hello`
- `nodus/<device_id>/config/set`
- `nodus/<device_id>/config/ack`
- `nodus/<device_id>/config/result`
- `nodus/<device_id>/calibration/set`
- `nodus/<device_id>/calibration/ack`
- `nodus/<device_id>/calibration/result`
- `nodus/<sensor_id>/data`
- `nodus/<sensor_id>/availability`
- `nodus/<channel_id>/state`
- `nodus/<channel_id>/availability`
- `nodus/<channel_id>/config/set`
- `nodus/<channel_id>/config/ack`
- `nodus/<channel_id>/config/result`

## Deprecated Topic Docs

The following older doc shapes are deprecated and should not be treated as the
current contract:

- `nodus/<channel_id>/set`
- switch-control docs centered on plain `ON` and `OFF`
- docs that imply ordinary runtime config writes cause a full retained `meta`
  republish

## Notes

- Valid Nodus `ACTIVE_PROFILE` values remain `nodusweb`, `sensorius`,
  `homeassistant`, and `weewx`.
- `nodusweb` is the AP/web-enabled profile used before reboot into MQTT-only
  runtime profiles.
- Calibration details remain documented in `docs/calibration_mqtt_contract.md`.
