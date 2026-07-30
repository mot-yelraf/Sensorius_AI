# Configuration Guide

Sensorius uses two configuration layers:

- Process environment and `.env` values for runtime controls such as logging,
  HTTP binding, retention, watchdog, GC, and API keys.
- TOML settings files for system, sensor, switch, Nodus shadow, integration,
  display, automation, and calibration state.

Most user-facing settings are edited through the web UI. File-level edits are
still supported, but keep the path rules below in mind.

## Runtime Path Resolution

Bare settings roots are resolved by `sensorius.saiRuntimePaths.resolve_runtime_base_dir`.

- Outside pytest, `system_settings`, `sensor_settings`, and `switch_settings`
  resolve under the installed runtime directory, for example
  `/home/<user>/Sensorius/` on Linux or `/Users/<user>/Sensorius/` on macOS.
- Inside pytest, relative roots stay relative so tests can use temporary
  directories.
- Absolute paths are used unchanged.

For a normal macOS or Linux install, expect runtime state here:

```text
/Users/<user>/Sensorius/system_settings/<device_id>/settings.toml
/Users/<user>/Sensorius/sensor_settings/<sensor_id>/sensor.toml
/Users/<user>/Sensorius/switch_settings/<switch_id>/switch.toml
/Users/<user>/Sensorius/switch_settings/automations/automations.toml
```

On Linux service installs the same layout is normally:

```text
/home/<user>/Sensorius/system_settings/<device_id>/settings.toml
/home/<user>/Sensorius/sensor_settings/<sensor_id>/sensor.toml
/home/<user>/Sensorius/switch_settings/<switch_id>/switch.toml
```

On Windows installs, the equivalent runtime root is normally:

```text
C:\Users\<user>\Sensorius
```

The SQLite database defaults to `sensorius_data.db` in the process working
directory.

## Environment And `.env`

`sensorius/saiUtils.py` loads a project-root `.env` early at process startup. Environment
variables supplied by the service manager or shell still work. Some runtime
panels in Advanced Settings write selected values back to `.env`.

API keys:

- `SAI_WEB_API_KEY` protects selected web and OTA endpoints when configured.
  It is not full-site authentication and does not protect every mutating route.
- `SAI_PEER_API_KEY` is reserved for Sensorius-to-Sensorius peer flows.
- If keys are missing, Sensorius generates process values at startup. When
  running from a git checkout, it does not write generated keys back into the
  repository `.env` unless `SENSORIUS_ALLOW_REPO_ENV_WRITE=true`.

Common environment keys:

```env
SENSORIUS_LOG_LEVEL=DEBUG
SENSORIUS_FILE_LOG=false
SENSORIUS_LOG_FILE=logs/sensorius.log
SENSORIUS_HTTP_DEBUG=false
SENSORIUS_DEBUG_MODULES=Sensorius,saiSensor,saiMQTTIngest,saiHtml,saiSwitch,saiWebRoutes

SENSORIUS_HTTP_HOST=0.0.0.0
SENSORIUS_HTTP_PORT=8000
SENSORIUS_GUI=
SENSORIUS_OTA_TRUST_DIR=/home/<user>/Sensorius/ota_trust_keys

SENSORIUS_DB_RETENTION_DAYS=90

SENSORIUS_WATCHDOG_TIMEOUT_SEC=71
SENSORIUS_WATCHDOG_LOOP_INTERVAL_SEC=10.0
SENSORIUS_WATCHDOG_JITTER_SEC=0.8

SENSORIUS_GC_ENABLED=true
SENSORIUS_GC_INTERVAL_SEC=29
SENSORIUS_GC_JITTER_SEC=0.7
SENSORIUS_GC_MIN_SLEEP_SEC=1.0
SENSORIUS_GC_FULL_EVERY_N=10

SENSORIUS_TIME_SYNC_ENABLED=true
SENSORIUS_TIME_SYNC_INTERVAL_SEC=3600
SENSORIUS_TIME_SYNC_ACK_TIMEOUT_SEC=5
SENSORIUS_TIME_SYNC_RESULT_TIMEOUT_SEC=20

SENSORIUS_EMAIL_ENABLED=false
SENSORIUS_EMAIL_SMTP_HOST=smtp.gmail.com
SENSORIUS_EMAIL_SMTP_PORT=465
SENSORIUS_EMAIL_SECURITY=ssl
SENSORIUS_EMAIL_USERNAME=
SENSORIUS_EMAIL_APP_PASSWORD=
SENSORIUS_EMAIL_FROM=
SENSORIUS_EMAIL_TO=
SENSORIUS_EMAIL_REARM_COOLDOWN_SEC=600
SENSORIUS_EMAIL_FAILURE_COOLDOWN_SEC=600
SENSORIUS_EMAIL_MAX_PER_HOUR=10
SENSORIUS_EMAIL_MAX_PER_DAY=40

SENSORIUS_AUTOSTART_SCOPE=user
SENSORIUS_AUTOSTART_ENABLED=false

SAI_WEB_API_KEY=
SAI_PEER_API_KEY=
```

`SENSORIUS_OTA_TRUST_DIR` contains cPyNodus III OTA public-key documents named
`<key_id>.json`. Sensorius uses them to verify signed `nodus-ota/v2`
manifests. Legacy cPyNodus II `nodus-ota/v1` packages do not require a key.

### HTTP Trust Boundary

`SENSORIUS_HTTP_HOST=0.0.0.0` exposes the UI through every network interface
permitted by the host firewall. This supports phones and workstations on the
local network, but Sensorius assumes that network is trusted. Do not expose the
service with public port forwarding or an Internet-facing reverse proxy.

For host-only access, set:

```env
SENSORIUS_HTTP_HOST=127.0.0.1
```

Use a firewall or isolated VLAN for LAN deployments and an authenticated VPN
for remote access. Protect MQTT independently with broker ACLs, credentials,
and TLS when it crosses a trusted boundary. Settings-manager secret
obfuscation is reversible and is not encryption; protect `.env`, runtime TOML
files, backups, and diagnostic exports as sensitive data.

The System Settings **System Settings > Notifications** form writes the email
keys to the project-root `.env`, which is restricted to the owning user. The
password field is never populated in HTML; leaving it blank preserves the
current app password. `ssl` uses implicit TLS (normally port 465), while
`starttls` upgrades a plain SMTP connection (normally port 587).

The form's **To** value is only for test delivery. Runtime recipients are
stored on Notify actions in `switch_settings/automations/automations.toml`.
Notify actions send on the false-to-true edge of their automation conditions.

GUI behavior:

- Empty `SENSORIUS_GUI` means auto-detect.
- `1`, `true`, `yes`, or `on` forces GUI.
- `0`, `false`, `no`, or `off` forces headless mode.

Linux GUI hints used by setup scripts and GUI launch:

```env
WEBKIT_DISABLE_COMPOSITING_MODE=1
GDK_BACKEND=wayland,x11
SENSORIUS_GUI_Y=48
```

On Raspberry Pi OS Trixie, leave `DISPLAY` and `WAYLAND_DISPLAY` inherited from
the graphical desktop session. Do not force `DISPLAY=:0` or clear
`WAYLAND_DISPLAY` in `sensorius.service`; the backend service should remain
headless and the labwc autostart entry should launch the pywebview shell from
`/home/<user>/.config/labwc/autostart`.

## System Settings

System settings are stored in:

```text
system_settings/<device_id>/settings.toml
```

`sensorius.saiSettings` seeds the file from `system_settings/factory/settings.toml` when
missing, creates a `.bak` backup once per startup when possible, and writes
changes atomically.

Core factory sections:

```toml
[Network]
HOSTNAME = ""
HTTPPORT = 8000

[SensorNetwork]
BROKER = "localhost"
MQTTPORT = 1883
USE_TLS = false
NODUS_DEBUG_DATA_ONLY = false
REMOVED_NODUS_IDS = []
LEGACY_FIRMWARE_HOSTS = []
LEGACY_POLLER_SUNSET_DATE = "2026-06-30"

[HomeAssistant]
ENABLED = false
HA_USERNAME = ""
HA_PASSWORD = ""
HA_BROKER = ""
HA_MQTTPORT = 1883
USE_TLS = false
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "sensorius"
PUBLISH_DISCOVERY_RETAIN = true
PUBLISH_STATE_RETAIN = true
PUBLISH_LEGACY_SENSOR_TOPIC = true
NODUS_PASSTHROUGH = false
MIRROR_NODUS = true

[FarmOS]
ENABLED = false
BASE_URL = ""
VERIFY_TLS = true
ACCESS_TOKEN = ""
CLIENT_ID = "farm"
CLIENT_SECRET = ""
USERNAME = ""
PASSWORD = ""
LOG_BUNDLE = "observation"
QUEUE_MAX = 1000
FLUSH_INTERVAL_SEC = 3.0
REQUEST_TIMEOUT_SEC = 10.0

[Notifications]
RULES_JSON = "[]"

[Time]
TZ = "America/Denver"
TZ_OFFSET = -25200
TZ_NAME = "MST"

[Astral]
AUTO_IP = true
LATITUDE = ""
LONGITUDE = ""
ALTITUDE = ""
TIMEZONE = ""
SOURCE = ""
PROVIDER = ""

[WeatherForecast]
PROVIDER = "met_no"

[WeeWX]
ENABLED = false
AUTO_DISCOVER = false
DB_PATH = "/var/lib/weewx/weewx.sdb"
SENSOR_ID = "weewx-station"
POLL_INTERVAL_SEC = 60
MQTT_ENABLED = false
MQTT_TOPIC = "weewx/#"
UPDATE_PERIOD_SEC = 300

[Display]
gauge_size = "Small"
display_style = "Gauge"
```

`SensorNetwork.REMOVED_NODUS_IDS` is maintained by the Remove Device workflow.
It prevents retained or newly arriving MQTT packets from recreating a removed
Nodus sensor/switch family. Do not edit it during normal operation; successfully
onboarding that device again removes its identity family from the list.

Runtime notes:

- `Network.HOSTNAME` and `[Time]` values may be refreshed from live host state.
- The Time Sync Manager uses Python `zoneinfo`/IANA timezone rules and the
  configured `Time.TZ` value to detect Standard Time and Daylight Saving Time
  offset/name changes. When `Time.TZ_OFFSET` or `Time.TZ_NAME` changes, it
  updates the hub settings and sends one-key-at-a-time `Time.*` config updates
  to known Nodus hosts.
- `SENSORIUS_HTTP_HOST` and `SENSORIUS_HTTP_PORT` override web binding at
  process startup.
- `Network.HTTPPORT` is the persisted UI setting for the web port.
- Changing HTTP binding, MQTT broker wiring, or service/autostart settings may
  require a service restart.
- Home Assistant bridge creation is part of startup; restart Sensorius after
  enabling Home Assistant or changing HA broker/topic settings.
- WeeWX MQTT settings are applied live through the running MQTT ingest client
  when available. If MQTT ingest is not running, the settings apply when MQTT
  ingest starts.
- Home Assistant and farmOS secrets are obfuscated at rest by `sensorius.saiSettings`.
  This is reversible obfuscation, not encryption.
- `[WeatherForecast].PROVIDER` accepts `met_no`, `open_meteo`, `us`, or `none`.
  `none` disables the dashboard forecast card.

## Sensor Settings

Sensor settings are stored in:

```text
sensor_settings/<sensor_id>/sensor.toml
```

Local Raspberry Pi sensors are detected and seeded from
`sensor_settings/factory/sensor.toml` when the Pi sensor runtime is available.
Remote Nodus sensors are seeded or updated from retained MQTT metadata and
patches. WeeWX can also materialize a station-style sensor config.

Nodus shadows also contain Sensorius routing metadata:

```toml
[Nodus]
DEVICE_ID = "aht-lux-ykdvea"
CONFIG_FILE = "sensor_i2c_2.toml"
```

`DEVICE_ID` is the physical MQTT command target. `CONFIG_FILE` selects the
child sensor TOML on that Nodus. On a dual-sensor device, each child has its own
`sensor_settings/<sensor_id>/sensor.toml`, dashboard settings, history, and
calibration state while sharing device-level heartbeat, restart, and OTA
operations.

Important sections and keys:

- `[Sensor]`: type, device, serial number, sensor ID, location.
- `[Sensor].HARDWARE`: concrete remote Nodus sensor hardware family when
  advertised by retained metadata.
- `[Sensor].STATION_MODEL`, `[Sensor].STATION_TYPE`, and
  `[Sensor].STATION_DRIVER`: WeeWX station identity copied from the local
  WeeWX config when the station sensor is materialized or refreshed.
- `[Calibration]`: high-level calibration status.
- `[Calibration.System]`: reference-sensor calibration fields.
- `[Calibration.Device]`: per-device offsets, altitude, and soil probe
  calibration values.
- `[Display]`: selected metrics and metric display mode.
- `[Display.Style]`: per-metric display style overrides.

Every dashboard display style places a trend arrow beside the current value.
The arrow is derived automatically from database history and has no separate
setting. Ordinary metrics use a 19-minute least-squares trend. Barometric
pressure uses up to three hours and remains marked provisional until that full
span is available. The initial calculation uses qualifying readings already
stored in `sensorius_data.db`. Hovering over or focusing the arrow shows the
calculated rate per hour and actual history window.

Remote sensor settings are local shadows of device metadata and should not be
treated as independent truth when a Nodus device publishes a newer retained
`meta` or correlated `meta/patch`.

## Switch Settings

Switch settings are stored in:

```text
switch_settings/<switch_id>/switch.toml
```

Local relay settings are created only when relay hardware is detected. Remote
Nodus switch settings are created from retained MQTT metadata.

Important `[Switch]` keys:

- `TYPE`: `pi`, `nodus`, `picow`, `pico2w`, `remote`, or `mqtt`.
- `DEVICE` and `DEVICE_SERIAL_NUM`: device classification and serial metadata.
- `SWITCH_DEVICE_ID`: stable switch controller ID.
- `SWITCH_LOCATION`: dashboard and automation grouping.
- `SWITCH_N_LABEL`: user-visible channel label.
- `SWITCH_N_CHANNEL_ID`: stable channel ID used for DB identity and MQTT.
- `SWITCH_N_LAST_STATE`: persisted startup/default state.
- `SWITCH_N_OVERRIDE_SCRIPT`: per-channel override flag.

The canonical UI/action key is:

```text
<switch_id>::<channel_id>
```

For example:

```text
switch-sernum::S1-sernum
```

## Automations

Advanced switch automations are stored in:

```text
switch_settings/automations/automations.toml
```

At runtime this is under the Sensorius runtime directory, such as
`/Users/<user>/Sensorius/switch_settings/automations/automations.toml` on
macOS or `/home/<user>/Sensorius/switch_settings/automations/automations.toml`
on Linux.

`sensorius/saiAutomationManager.py` owns this file. The current schema uses:

- `[Meta]` for schema notes and version.
- `[Advanced]` for named rules with `enabled` and compact JSON `script_json`.
- `[Scripts]` for optional coarse global toggles.

Switch controller monitors evaluate enabled Advanced rules. Do not bypass the
manager with broad text replacement because rule IDs, switch keys, and compact
JSON payloads are compatibility-sensitive.

Automation action rows are absolute target states, not toggles. For paired
timer behavior, disable the rule, set the normal baseline switch states
manually, then configure action rows as the active timer-window states with
`revert_action` set to `previous_state`.

## Nodus Factory Defaults

Nodus provisioning templates live under:

- `system_settings/factory_nodus/settings.toml.def`
- `sensor_settings/factory_nodus/*.toml.def`
- `switch_settings/factory_nodus/switch.toml.def`

AP-mode onboarding reads the factory Nodus AP credentials from
`system_settings/factory_nodus/settings.toml.def`. After onboarding, Sensorius
sends runtime config over MQTT and expects Nodus to publish retained metadata.

## Database Retention

`sensorius.saiDataLogger` defaults to a 90-day retention window controlled by:

```env
SENSORIUS_DB_RETENTION_DAYS=90
```

The web UI accepts 30 to 365 days. Set the environment value to `0` to disable
pruning. Retention applies to `readings`, `sw_events`, and `sensor_events` and
is throttled during normal writes.

## Configuration Change Checklist

Use this sequence for changes with runtime impact:

1. Back up `system_settings/`, `sensor_settings/`, `switch_settings/`, and
   `sensorius_data.db`.
2. Change settings through the web UI or the appropriate settings manager.
3. Restart the service only when startup wiring changes, such as HTTP binding,
   broker host/port, service/autostart mode, or local hardware config.
4. Verify `GET /healthz`.
5. Confirm MQTT ingest, Nodus metadata, Home Assistant discovery, or farmOS
   status as appropriate for the change.
