# Operations

This guide covers day-to-day operation, health checks, backup, restart, and
troubleshooting for a deployed Sensorius system.

## Start And Stop

Manual start from the installed runtime directory:

```bash
cd /home/<user>/Sensorius
python3 Sensorius.py
```

On macOS, use `/Users/<user>/Sensorius`. On Windows, use the setup script's
deployed runtime path, normally `C:\Users\<user>\Sensorius`.

Default web UI:

```text
http://127.0.0.1:8000
```

Health check:

```text
GET http://127.0.0.1:8000/healthz
```

For systemd service installs:

```bash
sudo systemctl status sensorius.service
sudo systemctl restart sensorius.service
```

For user systemd installs:

```bash
systemctl --user status sensorius.service
systemctl --user restart sensorius.service
```

On macOS setup-script installs, Sensorius and Mosquitto may be configured as
user LaunchAgents. Use the service path created by the setup script or the
Advanced Settings autostart panel to inspect or change autostart behavior.

## Runtime Health

Check these in order:

1. Web server responds at `/healthz`.
2. Dashboard loads at `/`.
3. MQTT broker is reachable from the Sensorius host.
4. Nodus devices publish retained `nodus/<device_id>/meta`.
5. `nodus/<device_id>/status/heartbeat` updates within the expected interval.
6. Sensor readings appear in the dashboard and graph API.
7. Switch state changes write to `sw_events` and update the dashboard.
8. Home Assistant or farmOS status endpoints are healthy when those integrations
   are enabled.

Useful web routes:

- `/healthz`: minimal service readiness.
- `/network-status`: network status payload.
- `/debug`: diagnostic HTML page.
- `/debug/switch-controllers`: switch controller snapshot.
- `/debug/automation-state`: Advanced automation state.
- `/farmos/status`: farmOS bridge state.
- `/weewx/status`: WeeWX ingest state.
- `/advanced/status`: Advanced Settings status.

## Logs

Console logging is enabled by default. File logging is controlled by:

```env
SENSORIUS_FILE_LOG=true
SENSORIUS_LOG_FILE=logs/sensorius.log
SENSORIUS_LOG_LEVEL=DEBUG
```

Debug module filtering is controlled by:

```env
SENSORIUS_DEBUG_MODULES=Sensorius,saiMQTTIngest,saiSwitch
```

Use `ALL` only for short diagnostic sessions. MQTT ingest and switch monitors
can be noisy on active systems.

## Backups

Before upgrades, broker migrations, storage work, or large settings changes,
back up:

```text
/home/<user>/Sensorius/system_settings/
/home/<user>/Sensorius/sensor_settings/
/home/<user>/Sensorius/switch_settings/
/home/<user>/Sensorius/sensorius_data.db
```

On macOS, use `/Users/<user>/Sensorius/...`. On Windows, use
`C:\Users\<user>\Sensorius\...`. If the database is in a different working
directory, back up that `sensorius_data.db` file instead. SQLite WAL files may
also exist during active runtime; stop the service before copying the database
for the cleanest backup.

## Restart Requirements

Restart Sensorius after changes to:

- HTTP host or port.
- MQTT broker host, port, TLS, or auth that affects startup wiring.
- Service/autostart scope.
- Local GPIO relay hardware settings.
- Python dependencies or deployed source files.
- Environment variables in `.env`.

A restart is usually not required for:

- Sensor display names and locations.
- Switch labels and locations after the web route completes.
- Advanced automation edits.
- farmOS or Home Assistant enablement after the bridge has a live MQTT
  connection, although a restart is a useful diagnostic if discovery was missed.
- Calibration values after the UI applies the update and reloads the runtime
  sensor where supported.

## MQTT Operations

Steady-state Nodus operation should be MQTT-first:

- Do not use periodic `/hayd` or `/itaot` polling for onboarded devices.
- Use retained `nodus/<device_id>/meta` for full discovery.
- Use `nodus/<device_id>/meta/patch` for accepted runtime changes.
- Use heartbeat and availability topics for liveness.
- Use non-retained `/set` commands unless a cleanup flow explicitly owns the
  retained command cleanup.

When a Nodus device does not appear:

1. Confirm the broker configured in `[SensorNetwork]` is reachable.
2. Confirm the device is on the same network path as the broker.
3. Confirm retained `nodus/<device_id>/meta` exists.
4. Confirm heartbeat updates are recent.
5. Use the web UI retry-discovery action if metadata arrived before Sensorius
   subscribed.

## Switch Operations

Manual toggles, HA commands, MQTT commands, and automations should all travel
through the shared switch controller path.

Operational rules:

- Use stable channel labels once automations or HA entities depend on them.
- Keep `SWITCH_N_CHANNEL_ID` stable for a physical channel.
- Do not write switch events directly to `readings`; use `sw_events` through
  `saiDataLogger.log_switch_event`.
- Manual UI toggles are blocked when an enabled Advanced automation owns the
  same switch key.
- Test critical automations with harmless loads before connecting equipment.

## Home Assistant Operations

Expected flow:

1. Configure the MQTT broker in `[SensorNetwork]`.
2. Enable `[HomeAssistant]` and set HA broker settings.
3. Start or restart Sensorius.
4. Let MQTT ingest connect.
5. Let the HA bridge publish retained discovery.
6. Let HA observe state and send switch commands through MQTT.

If entities do not appear, verify:

- `HomeAssistant.ENABLED = true`.
- `DISCOVERY_PREFIX` matches the HA MQTT integration, usually `homeassistant`.
- Discovery retain is enabled.
- HA broker credentials are correct.
- Sensorius has readings or switch identity rows to advertise.

## farmOS Operations

Use the FarmOS settings panel to configure URL, TLS verification, and auth.
Run the built-in test before enabling continuous export.

Operational rules:

- farmOS export only sends newly written readings while enabled.
- The queue is in memory and bounded by `FarmOS.QUEUE_MAX`.
- Repeated failures should be diagnosed through `/farmos/status`.
- A full service restart clears the in-memory queue.

## Database Operations

The database uses SQLite WAL mode and additive migrations. `saiDataLogger`
creates tables and indexes at startup.

Key tables:

- `readings`: sensor metric samples.
- `switch_ids`: switch/channel identity records.
- `sw_events`: switch state transitions.
- `biodynamic_notes`: calendar note text.
- `biodynamic_daily_summaries`: generated daily summaries.

Retention is controlled by:

```env
SENSORIUS_DB_RETENTION_DAYS=90
```

Set to `0` to disable pruning.

## Upgrade Checklist

1. Stop Sensorius.
2. Back up settings directories and database.
3. Deploy updated source into the runtime directory, such as
   `/home/<user>/Sensorius` or `/Users/<user>/Sensorius`.
4. Install changed dependencies if requirements changed.
5. Start Sensorius.
6. Verify `/healthz`, dashboard load, MQTT ingest, switch controls, and any
   enabled integrations.
7. Run targeted tests from the source checkout before deploying when practical.

## Troubleshooting Quick Reference

Sensor missing:

- Check power, wiring, and Pi I2C/UART only for local sensors.
- Check retained MQTT metadata and heartbeat for Nodus sensors.
- Confirm `sensor_settings/<sensor_id>/sensor.toml` exists in the runtime root.

Switch will not toggle:

- Confirm the switch is online.
- Confirm no enabled Advanced automation owns the switch key.
- Confirm the label and channel ID match the switch settings.
- For Nodus switches, verify command, state, and event topics.

Graphs have gaps:

- Confirm the sensor was online.
- Check service restart history.
- Check database retention.
- Verify the process can write to `sensorius_data.db`.

Watchdog exits:

- Review the timeout snapshot in logs.
- Identify whether one task or many tasks stopped feeding.
- Increase watchdog values only after understanding the blocked task.
