# Architecture

This document describes the runtime architecture of Sensorius and how local and remote devices are handled.

## Runtime Components

- `Sensorius.py`: process entrypoint and runtime wiring.
- `saiWebServer.py` + `saiWebRoutes.py`: FastAPI UI/API layer.
- `saiMQTTIngest.py`: MQTT discovery, ingest, topic registration, and remote state cache.
- `saiSensor.py`: local directly connected sensor controllers.
- `saiSwitch.py`: switch controllers and automation monitor loop.
- `saiDataLogger.py`: SQLite persistence for sensor data, switch events, and switch identities.
- `saiFarmOSBridge.py`: optional farmOS telemetry export bridge (queue + flush worker).
- Settings managers: `saiSensorSettingsManager.py`, `saiSwitchSettingsManager.py`, `saiSettings.py`

## Switch Controller Model

Sensorius now uses two switch controller classes behind one interface:

- `SwitchController`: local GPIO relay switches.
- `RemoteSwitchController`: MQTT-backed Nodus/Pico switches.

Both are created through `build_switch_controller(...)` in `saiSwitch.py`.

Shared contract used by UI/routes/automation:

- `get_switch_names()`
- `get_state(label)`
- `set_state(label, on, force=False)`
- `override_script` map
- `last_state` map
- `run_controladora_monitor(...)`

This keeps local and remote switch behavior consistent for automation and dashboard rendering.

## Startup Flow

1. `Sensorius.py` initializes settings, supervisor, GC, and network helpers.
2. Local sensors are detected and instantiated (if present on host hardware).
3. Switch settings are enumerated and a controller is created per switch via `build_switch_controller(...)`.
4. MQTT ingest is started and begins remote discovery (`/itaot` + MQTT topics).
5. FarmOS bridge task is started and subscribes to new readings from `saiDataLogger`.
6. Web server starts; dashboard and API routes read from controllers + ingest + DB.
7. Each switch controller monitor (`run_controladora_monitor`) is scheduled to evaluate automations.

## FarmOS Telemetry Path

When FarmOS integration is enabled:

- `saiFarmOSBridge` listens for newly written sensor readings from `saiDataLogger`.
- Readings are queued in memory (bounded by `FarmOS.QUEUE_MAX`).
- A worker loop flushes queued items to farmOS using the selected backend (`httpx` direct JSON:API calls, or `farmospy` client log API `send`/`create`).
- Failed writes are re-queued for retry, and status/error details are exposed through `/farmos/status`.

## Discovery and Identity

Remote Nodus/Pico switches are discovered through MQTT ingest:

- `/itaot` metadata provides switch id, labels, channel ids, and topics.
- Topic maps are cached in ingest.
- Switch identity records are persisted in DB (`switch_id`, `label`, `channel_id` via switch key).

Identity strategy:

- Human-facing key: `switch_id::label` (UI/automation semantics).
- Canonical DB key: `channel_id::label` when a channel id exists.

## Automation Evaluation

Switch automation is evaluated inside each switch controller monitor:

- TriggerScript rules from switch settings.
- Advanced rules from `switch_settings/<switch_id>/automations.toml`.

Evaluation uses:

- Live sensor data when available.
- Cached values and DB fallback when live data is unavailable.

So remote switch automation does not require a dedicated remote sensor controller.

## Dynamic Controller Creation

When an automation is saved for a switch discovered after startup, routes can create the controller dynamically:

- Load switch settings.
- Build controller via `build_switch_controller(...)`.
- Register it in `switch_controllers`.
- Start a monitor task for that switch.

This prevents “automation saved but no monitor running” failures.
