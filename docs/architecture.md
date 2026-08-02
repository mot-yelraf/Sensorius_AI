# Architecture

Sensorius is a cross-platform environmental sensing and automation hub. It can
run as a full Raspberry Pi controller with directly connected sensors and GPIO
relays, or as a macOS, Windows, or Linux hub that discovers and controls Nodus
devices over MQTT.

The runtime is intentionally conservative: most work is done with standard
library components, SQLite, FastAPI, Paho MQTT, and small supervised background
tasks so the same code can run on low-power Raspberry Pi deployments.

## Deployment Modes

Raspberry Pi full hub:

- Detects local I2C/UART sensors when the Pi sensor runtime is available.
- Materializes local `sensor_settings/<sensor_id>/sensor.toml` files from
  factory templates.
- Detects relay hardware and materializes host switch settings when a relay
  board is present.
- Runs the same MQTT ingest, web UI, database, Home Assistant, farmOS, and
  automation services as other platforms.

macOS, Windows, and non-Pi Linux hub:

- Skips local GPIO and direct sensor discovery.
- Runs the web UI, database, MQTT ingest, Nodus onboarding, Home Assistant
  bridge, farmOS export, WeeWX ingest, and automations for remote devices.
- Uses native Wi-Fi tooling during Nodus onboarding where supported by the
  setup scripts.

## Primary Runtime Modules

- `Sensorius.py`: stable repository-root launcher and compatibility import
  alias.
- `sensorius/app.py`: application entrypoint and runtime wiring.
- `sensorius/saiTaskSupervisor.py`: supervised background task runner.
- `sensorius/saiWatchdog.py`: task heartbeat monitor and process-exit safety
  net.
- `sensorius/saiGarbageCollection.py`: lightweight GC loop for long-running
  deployments.
- `sensorius/saiWebServer.py`: FastAPI app construction,
  static/template mounting, and
  uvicorn launch.
- `sensorius/saiWebRoutes.py`: UI, API, onboarding, calibration, settings,
  and switch routes.
- `sensorius/saiSettings.py`: system settings in
  `system_settings/<device_id>/settings.toml`.
- `sensorius/saiSensorSettingsManager.py`: per-sensor settings in
  `sensor_settings/<sensor_id>/sensor.toml`.
- `sensorius/saiSwitchSettingsManager.py`: per-switch settings in
  `switch_settings/<switch_id>/switch.toml`.
- `sensorius/saiDataLogger.py`: SQLite persistence for readings, sensor
  liveness events,
  switch identities, switch events, biodynamic notes, plantings, calendar
  cache entries, and daily summaries.
- `sensorius/saiBiodynamicCalendarApp.py`: integrated full-screen calendar
  routes, shared cache access, and paced background warming.
- `sensorius/biodynamic_calendar/`: BD Calendar calculations, Astral payloads,
  and planting guidance.
- `sensorius/saiSensor.py`, `sensorius/saiSensorFactory.py`, and
  `sensorius/sensor_modules/`: local sensor runtime.
- `sensorius/saiSwitch.py`, `sensorius/saiSwitchFactory.py`, and
  `sensorius/saiAutomationManager.py`: local and remote switch control plus
  Advanced automations.
- `sensorius/saiMQTTClient.py`: outbound publisher for local sensors when
  publishing to a non-local broker.
- `sensorius/saiMQTTIngest.py`: MQTT discovery, Nodus topic registration,
  retained
  metadata processing, liveness, remote switch state cache, onboarding events,
  calibration events, and optional Nodus mirroring.
- `sensorius/saiHomeAssistantMqtt.py`: Home Assistant discovery, state
  publishing,
  availability, and command routing.
- `sensorius/saiFarmOSBridge.py`: farmOS JSON:API export queue and flush
  worker.
- `sensorius/saiWeeWX.py`: optional WeeWX SQLite archive ingest.
- `sensorius/saiTimeSync.py`: timezone/DST synchronization for hub settings
  and known Nodus devices.
- `sensorius/saiWeatherForecast.py`: dashboard weather forecast provider
  and SQLite cache helpers.
- `sensorius/saiNodusOTA.py`: Nodus OTA package and job support used by web
  routes.

## Startup Flow

1. `Sensorius.py` loads the installed `sensorius` package and delegates to
   `sensorius/app.py`, which configures logging, starts the async runtime in
   a thread, and optionally opens a pywebview shell when GUI mode is available.
2. `sensorius.saiSettings` loads or seeds `system_settings/<device_id>/settings.toml`,
   applies live hostname/time values, and may persist Astral IP geolocation
   when configured.
3. `TaskSupervisor`, `GCManager`, network helpers, and `sensorius.saiDataLogger` are
   created.
4. Local sensor configs are materialized only if the `board` runtime is
   available. Remote sensor settings are skipped by the local sensor builder.
5. Switch settings are enumerated. Local relay settings are materialized only
   when relay hardware is detected. Each switch is built through
   `build_switch_controller(...)`, which returns either `SwitchController` or
   `RemoteSwitchController`.
6. Runtime objects are attached to `sensorius.saiWebRoutes` globals and `app.state` so the
   UI and APIs share the same controller, database, and ingest instances.
7. Local MQTT publisher tasks are created only for local sensors and only when
   `SensorNetwork.BROKER` is configured as a non-local broker. If the broker is
   unset, `localhost`, or this host, local publisher tasks are skipped.
8. `sensorius.saiMQTTIngest` starts when `SensorNetwork.BROKER` is set. It subscribes to
   Nodus, optional mirrored base-topic, calibration, onboarding, and optional
   WeeWX topics.
9. If Home Assistant is enabled, the HA bridge waits for MQTT connection,
   installs command handlers, and publishes retained discovery.
10. Always-on services are registered: WeeWX archive ingest, farmOS bridge,
    Time Sync Manager, watchdog, GC, local sensor data collection, and switch
    monitor loops. A lightweight loop-lag monitor is also started.
11. `WebServerController` registers FastAPI routes and runs uvicorn. The web
    server can run with zero local sensors. After startup settles, the
    integrated calendar begins its delayed single-worker cache warm without
    holding up monitoring or control readiness.

## Data Flow

Local sensors:

- `SensorController.data_collection` reads a concrete sensor module.
- `sensorius.saiDataLogger.log_readings` writes metric rows to `readings`.
- In-memory latest-value caches are updated for dashboard and fast stats.
- `sensorius.saiStats` adds cached 24-hour min/avg/max values and least-squares
  trends to dashboard payloads. Ordinary trends use 19 minutes of readings;
  barometric pressure uses up to three hours.
- Readings listeners notify Home Assistant and farmOS when those integrations
  are enabled.
- System automation monitors evaluate Notify actors. SMTP delivery runs on a
  daemon worker thread so it does not block switch monitoring.

Remote Nodus sensors:

- Nodus publishes retained compact `nodus/<device_id>/meta`, retained
  `nodus/<device_id>/meta/switch` when switch channels are present, runtime
  data, heartbeat, availability, calibration, and patch topics.
- `sensorius.saiMQTTIngest` uses retained metadata to register sensor and switch topics
  and seed or update local shadow settings.
- Remote switch commands are paced per channel and publish to a single
  channel-scoped `config/set` target until the correlated Nodus response clears
  the in-flight command.
- Sensor payloads are normalized and written through `sensorius.saiDataLogger`, the same
  path as local readings.

WeeWX:

- Archive polling and MQTT ingest are optional.
- `sensorius/saiWeeWX.py` polls a configured SQLite archive database.
- `sensorius/saiMQTTIngest.py` handles configured WeeWX MQTT publications.
- Both paths use `sensorius/sensor_modules/station_weewx.py` helpers and normalize
  station data into the same sensor settings and database paths as other
  sensors.

Weather forecast:

- `sensorius/saiWeatherForecast.py` resolves the dashboard forecast location from Astral
  settings or Astral auto-detection.
- The selected `[WeatherForecast].PROVIDER` controls the forecast source:
  MET Norway Location Forecast, Open-Meteo, US National Weather Service, or
  no forecast.
- Forecast payloads are cached in SQLite in `weather_forecast` and reused for
  up to six hours. If the selected provider fails, the dashboard can continue
  to show the latest cached forecast for that provider marked as stale.

Switches:

- `SwitchController` handles local GPIO relays.
- `RemoteSwitchController` uses the same public interface while resolving
  state and commands through `sensorius.saiMQTTIngest`.
- Switch state changes must be written through `sensorius.saiDataLogger.log_switch_event`.
- `switch_ids` stores switch identity metadata and `sw_events` stores state
  transitions.

Automations:

- Advanced automation rules are stored in
  `switch_settings/automations/automations.toml`.
- Each switch controller monitor evaluates enabled rules every few seconds.
- Evaluation prefers live sensor data, then cached values, then DB-backed
  fallback behavior where implemented.
- Manual UI toggles are blocked while an enabled Advanced automation owns the
  target switch key.

Home Assistant:

- `sensorius.saiHomeAssistantMqtt.rPiHomeAssistantBridge` publishes retained discovery
  from known sensor metrics and switch identities.
- New readings and switch events trigger retained state and availability
  publishes.
- Commands on `<base>/switch/<switch_id>/<channel_id>/set` are routed back to
  the shared switch controller path.

farmOS:

- `sensorius.saiFarmOSBridge` listens for newly written sensor readings.
- Readings are queued in memory, bounded by `FarmOS.QUEUE_MAX`, and flushed to
  farmOS JSON:API with `httpx`.
- Failed writes are retried by pushing the item back onto the queue.

Time sync:

- `sensorius.saiTimeSync.TimeSyncService` derives current `Time.TZ_OFFSET` and
  `Time.TZ_NAME` from `Time.TZ` or `Astral.TIMEZONE` using Python `zoneinfo`.
- When local hub time values change, the service persists them and sends
  paced `Time.*` config updates to known Nodus MQTT targets.

## Runtime State And Paths

The source checkout is not always the writable runtime root. Bare settings roots
are resolved by `sensorius.saiRuntimePaths.resolve_runtime_base_dir(...)`.

- Outside pytest, bare `sensor_settings`, `switch_settings`, and
  `system_settings` resolve under `/Users/<user>/Sensorius/` on macOS and
  `/home/<user>/Sensorius/` on Linux.
- In pytest, relative settings roots remain inside the test working directory
  to preserve test isolation.
- Absolute paths are used as-is.

Canonical runtime files:

- `/Users/<user>/Sensorius/system_settings/<device_id>/settings.toml`
- `/Users/<user>/Sensorius/sensor_settings/<sensor_id>/sensor.toml`
- `/Users/<user>/Sensorius/switch_settings/<switch_id>/switch.toml`
- `/Users/<user>/Sensorius/switch_settings/automations/automations.toml`
- `sensorius_data.db` in the process working directory unless an explicit DB
  path is passed to `sensorius.saiDataLogger`.

Factory templates remain in the repository and deployed runtime tree under:

- `system_settings/factory/`
- `system_settings/factory_nodus/`
- `sensor_settings/factory/`
- `sensor_settings/factory_nodus/`
- `switch_settings/factory/`
- `switch_settings/factory_nodus/`

## Identity And Compatibility Rules

- Remote Nodus retained `meta` is the authoritative steady-state discovery
  input.
- AP/bootstrap HTTP endpoints are limited to onboarding and diagnostics.
- Ongoing Nodus health should come from MQTT heartbeat and availability topics,
  not periodic `/hayd` or `/itaot` polling.
- UI, automation, and DB switch keys use the form
  `<switch_id>::<channel_id>` when a channel ID exists.
- MQTT topic shapes, Home Assistant entity IDs, retained discovery payloads,
  and stored DB keys are compatibility-sensitive.

## Extension Boundaries

Use these ownership boundaries when extending the system:

- Add sensor hardware behavior in `sensorius/sensor_modules/` and wire detection through
  `sensorius/saiSensorFactory.py`.
- Add switch hardware behavior in `sensorius/saiSwitchFactory.py` while preserving the
  `SwitchController` public interface.
- Put substantial route behavior in supporting modules and keep
  `sensorius/saiWebRoutes.py` handlers thin where practical.
- Add settings through the appropriate manager and factory template, with
  idempotent migration or defaulting for existing installations.
- Route all sensor readings and switch events through `sensorius.saiDataLogger`.
- Treat MQTT discovery, Home Assistant discovery, switch identity, and DB
  migrations as high-risk compatibility surfaces.
