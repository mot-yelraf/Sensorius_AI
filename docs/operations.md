# Operations

This guide covers day-to-day operation, health checks, backup, restart, and
troubleshooting for a deployed Sensorius system.

## Start And Stop

Manual start from the installed runtime directory:

```bash
cd /home/<user>/Sensorius
.venv/bin/python Sensorius.py
```

On macOS, use `/Users/<user>/Sensorius`. On Windows, use the setup script's
deployed runtime path, normally `C:\Users\<user>\Sensorius`.

Default web UI:

```text
http://127.0.0.1:8000
```

## Trusted-LAN Operation

Sensorius assumes that clients able to reach its web UI are trusted. It does
not provide a complete login/session boundary around all settings, onboarding,
calibration, switch, and maintenance actions. `SAI_WEB_API_KEY` applies only to
selected protected routes.

Keep HTTP and MQTT behind host/network firewalls, do not configure public port
forwarding, and use a VPN for remote operation. If LAN clients do not need the
UI, set `SENSORIUS_HTTP_HOST=127.0.0.1` and restart Sensorius. Review firewall
and broker access after network, router, or service changes.

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
- `/debug/mqtt-retained-commands`: redacted scan for retained non-empty Nodus
  `/set` command payloads.
- `/farmos/status`: farmOS bridge state.
- `/weewx/status`: WeeWX ingest state.
- `/advanced/status`: Advanced Settings status.

## Add Device Wi-Fi Authorization

On Raspberry Pi, Add Device temporarily moves the Sensorius host from its normal
Wi-Fi network to `Nodus_Setup`, posts bootstrap data to the Nodus AP, and then
rejoins the normal network. If logs show:

```text
org.freedesktop.NetworkManager.network-control request failed: not authorized
```

or the UI reports `network_control_not_authorized`, the `sensorius.service` user
cannot control NetworkManager.

Supported Linux and Raspberry Pi setup scripts install the required
NetworkManager polkit rule automatically when they create `sensorius.service`.
Use the checks below for older installs, hand-created services, or repairs.

Check the service user:

```bash
systemctl show sensorius.service -p User
```

Check that user's NetworkManager permissions:

```bash
sudo -u <user> nmcli -t -f PERMISSION,VALUE general permissions | grep 'org.freedesktop.NetworkManager.network-control\|org.freedesktop.NetworkManager.wifi.scan\|org.freedesktop.NetworkManager.settings.modify'
```

The `network-control` value must be `yes`, and at least one
`settings.modify.*` value must be `yes`. If `wifi.scan` is listed as `auth`,
the passive scanner may report authorization errors even though Add can still
attempt onboarding. Rerun the current setup script or install a local polkit
rule at
`/etc/polkit-1/rules.d/50-sensorius-networkmanager.rules`, replacing `<user>`
with the service user:

```javascript
polkit.addRule(function(action, subject) {
  var allowed = [
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.wifi.scan",
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.NetworkManager.settings.modify.own",
    "org.freedesktop.NetworkManager.enable-disable-wifi"
  ];
  if (subject.user == "<user>" && allowed.indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
});
```

Then reload the policy service and restart Sensorius:

```bash
sudo systemctl restart polkit.service
sudo systemctl restart sensorius.service
```

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

The Advanced settings pane can also create an on-demand database archive. It
uses SQLite backup semantics, saves the snapshot under `database_archives/`
next to the active database, and downloads the same `.sqlite3` file.

## Restart Requirements

Restart Sensorius after changes to:

- HTTP host or port.
- MQTT broker host, port, TLS, or auth that affects startup wiring.
- Home Assistant enablement or HA broker/discovery topic settings.
- Service/autostart scope.
- Local GPIO relay hardware settings.
- Python dependencies or deployed source files.
- Environment variables in `.env`.

A restart is usually not required for:

- Sensor display names and locations.
- Switch labels and locations after the web route completes.
- Advanced automation edits.
- farmOS enablement; the bridge is always registered and reads its enabled flag
  while running.
- WeeWX MQTT setting changes; Sensorius applies them live when MQTT ingest is
  running, otherwise they apply when MQTT ingest starts.
- Calibration values after the UI applies the update and reloads the runtime
  sensor where supported.

## MQTT Operations

Steady-state Nodus operation should be MQTT-first:

- Do not use periodic `/hayd` or `/itaot` polling for onboarded devices.
- Use retained compact `nodus/<device_id>/meta` for device and sensor
  discovery.
- Use retained `nodus/<device_id>/meta/switch` for switch channel topic maps
  when switch channels are present.
- Use `nodus/<device_id>/meta/patch` for accepted runtime changes.
- Use heartbeat and availability topics for liveness.
- Use non-retained `/set` commands unless a cleanup flow explicitly owns the
  retained command cleanup.
- Timezone/DST changes are handled by the Time Sync Manager. It updates the
  hub's `[Time]` settings from `Time.TZ` and sends `Time.*` updates to known
  Nodus hosts over MQTT.

When a Nodus device does not appear:

1. Confirm the broker configured in `[SensorNetwork]` is reachable.
2. Confirm the device is on the same network path as the broker.
3. Confirm retained `nodus/<device_id>/meta` exists.
4. For switch-capable devices, confirm retained `nodus/<device_id>/meta/switch`
   exists.
5. Check `/debug/mqtt-retained-commands` for stale retained `/set` commands
   that could replay after reconnect.
6. Confirm heartbeat updates are recent.
7. Use the web UI retry-discovery action if metadata arrived before Sensorius
   subscribed.

## Nodus OTA Operations

Nodus OTA uses MQTT only to prepare the device and report results. Package
bytes move over the temporary OTA HTTP service after the device reboots into
OTA mode.

Operational rules:

- Verified package targets are `pico2w` and `xesp32s3`.
- Keep package `target.platform` aligned with retained Nodus `mcu` metadata.
  Sensorius rejects known target mismatches before transfer.
- Update only when device power, Wi-Fi, and broker connectivity are stable.
- Use low concurrency for constrained networks or mixed device groups.
- Nodus may take up to 150 seconds to reboot and expose OTA HTTP mode. During
  that interval the UI reports `Nodus OTA mode booting...`; routine HTTP probe
  failures are intentionally hidden. If the interval expires, Sensorius asks
  the device to abort OTA and return to normal operation when reachable.
- Each file has three total transfer attempts. A device update is stopped and
  aborted after the third file failure or after the 30-minute device limit.
- A job is complete only after a fresh, package-matched completion result or
  fresh metadata confirms the exact target firmware version.
- For Pico 2 W, avoid large single-file compiled `.mpy` updates. Command-line
  OTA testing showed a Nodus-side memory allocation failure when transferring
  `app.mpy` larger than about 50 KB. Split the change into smaller files or use
  a smaller/uncompiled app file until the firmware OTA apply path supports
  larger compiled modules.

## Switch Operations

Manual toggles, HA commands, MQTT commands, and automations should all travel
through the shared switch controller path.

Operational rules:

- Use stable channel labels once automations or HA entities depend on them.
- Keep `SWITCH_N_CHANNEL_ID` stable for a physical channel.
- Do not write switch events directly to `readings`; use `sw_events` through
  `sensorius.saiDataLogger.log_switch_event`.
- Manual UI toggles are blocked when an enabled Advanced automation owns the
  same switch key.
- For timer rules that should return to a normal state, disable the automation,
  set the normal switch states manually, then save action rows as the active
  timer-window states with `Previous State` revert behavior.
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

The database uses SQLite WAL mode and additive migrations. `sensorius.saiDataLogger`
creates core telemetry tables and indexes at startup; `sensorius.saiWeatherForecast`
creates the forecast cache table on first forecast use.

Key tables:

- `readings`: sensor metric samples.
- `sensor_events`: sensor liveness and related event rows.
- `switch_ids`: switch/channel identity records.
- `sw_events`: switch state transitions.
- `biodynamic_notes`: calendar note text.
- `biodynamic_daily_summaries`: generated daily summaries.
- `weather_forecast`: cached dashboard forecast payloads, created by the
  weather forecast helper when forecasts are used.

Biodynamic month payloads are also cached as JSON files under the Sensorius
runtime directory, normally `/home/<user>/Sensorius/cache/biodynamic/` on
Linux or `/Users/<user>/Sensorius/cache/biodynamic/` on macOS. This cache is
keyed by month, rounded Astral location, timezone, altitude, ephemeris name,
and cache schema version. It can be removed safely; Sensorius will rebuild
missing months in the background or on calendar demand.

After startup settles, the web server also warms generated biodynamic month
payloads in the background so later browser sessions can reuse the shared disk
cache. Defaults are intentionally conservative: current month first, then nearby
months, with pauses between builds. Operators can tune this with:

- `SENSORIUS_BIODYNAMIC_PREWARM_ENABLED`
- `SENSORIUS_BIODYNAMIC_PREWARM_DELAY_SEC`
- `SENSORIUS_BIODYNAMIC_PREWARM_PAUSE_SEC`
- `SENSORIUS_BIODYNAMIC_PREWARM_PAST_MONTHS`
- `SENSORIUS_BIODYNAMIC_PREWARM_FUTURE_MONTHS`

Retention is controlled by:

```env
SENSORIUS_DB_RETENTION_DAYS=90
```

Set to `0` to disable pruning.
Pruning applies to `readings`, `sw_events`, and `sensor_events`.
The web UI retention selector accepts 30 to 365 days.

### Automatic Database Recovery

Sensorius attempts best-effort SQLite recovery when the runtime sees corruption
errors such as `database disk image is malformed`, `file is not a database`, or
`malformed database schema`.

The recovery flow preserves the damaged DB family before changing live files:

```text
/home/<user>/Sensorius/database_recovery/sensorius_data-YYYYMMDD-HHMMSS/
```

On macOS, use `/Users/<user>/Sensorius/database_recovery/...`. The recovery
workspace contains copied files such as `sensorius_data.db`,
`sensorius_data.db-wal`, and `sensorius_data.db-shm`. If Sensorius replaces the
live DB, it also moves the damaged live files into the same directory with a
`.damaged` suffix.

When the `sqlite3` command-line tool is available, Sensorius first tries
SQLite's `.recover` command and validates the recovered database with
`PRAGMA integrity_check`. If salvage fails, Sensorius rebuilds an empty database
by default so collection and switch events can resume while the damaged files
remain available for inspection.

Recovery controls:

```env
SENSORIUS_DB_AUTO_RECOVER=1
SENSORIUS_DB_AUTO_REBUILD_ON_RECOVERY_FAIL=1
SENSORIUS_DB_RECOVERY_MIN_INTERVAL_SEC=300
SENSORIUS_DB_RECOVERY_TIMEOUT_SEC=300
SENSORIUS_SQLITE3_BIN=sqlite3
```

Set `SENSORIUS_DB_AUTO_RECOVER=0` to disable automatic recovery. Set
`SENSORIUS_DB_AUTO_REBUILD_ON_RECOVERY_FAIL=0` if you prefer Sensorius to leave
a damaged live DB in place when `.recover` cannot produce a valid replacement.

## Upgrade Checklist

1. Stop Sensorius.
2. Back up settings directories and database.
3. For existing installs, prefer `deploy_scripts/deploy_sai.sh --apply` from
   the source checkout. It preserves `sensorius_data.db*`, `system_settings/`,
   `sensor_settings/`, and `switch_settings/` while updating application code
   and factory templates. Once the replacement `sensorius/` package and root
   launcher are present, deployment also removes legacy root `sai*.py`,
   `sensor_modules/`, and transitional `src/sensorius/` source.
4. Use `install.sh` or platform setup scripts only when doing a first install,
   repair install, or intentional package/broker/service reconfiguration.
5. Install changed dependencies in the target runtime environment if
   requirements changed.
6. Start Sensorius.
7. Verify `/healthz`, dashboard load, MQTT ingest, switch controls, and any
   enabled integrations.
8. Run targeted tests from the source checkout before deploying when practical.

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
