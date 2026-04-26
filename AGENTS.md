# AGENTS.md

Operational instructions for coding agents working in this repository. This file is the repo-local source of truth for agent behavior. Existing guidance from `copilot-instructions.md` is incorporated here so agents follow one consistent contract.

## Scope

- Applies to the entire repository unless a deeper `AGENTS.md` overrides it for a subdirectory.
- Prefer repo-specific instructions here over generic coding-agent defaults.

## Project context

- Sensorius is a cross-platform environmental sensing and automation hub.
- Supported runtime targets include Raspberry Pi, macOS, Windows, and Linux.
- Raspberry Pi supports directly connected sensors and relay hardware.
- All platforms support Nodus MQTT-backed sensors and switches.
- Prefer changes that remain safe on low-power and low-RAM devices.
- Use absolute paths when referring to on-device files in docs, comments, and user guidance.

## Primary runtime modules

- `Sensorius.py`: main process entrypoint and runtime wiring.
- `saiWebServer.py` and `saiWebRoutes.py`: FastAPI web UI and API routes.
- `saiMQTTIngest.py`: MQTT discovery, ingest, topic registration, and remote state cache.
- `saiHomeAssistantMqtt.py`: Home Assistant MQTT discovery and state bridge.
- `saiDataLogger.py`: SQLite persistence for readings, switch events, and identities.
- `saiSensor.py` and `sensor_modules/`: local directly connected sensor runtime.
- `saiSwitch.py`: local and remote switch controller behavior.
- `saiFarmOSBridge.py`: optional farmOS telemetry export.
- `saiSettings.py`, `saiSensorSettingsManager.py`, `saiSwitchSettingsManager.py`: configuration loading and materialization.

## Workflow preferences

- If the user asks for a code change, make the edit directly rather than asking for per-edit confirmation.
- Keep edits minimal, targeted, and easy to review.
- Do not reformat unrelated code.
- When switching repositories, confirm the target repository before editing.
- Do not overwrite or revert user changes you did not make unless explicitly asked.

## Code generation rules

- Prefer clear, explicit naming over abstraction for its own sake.
- Keep modules cohesive and avoid deep or circular import chains.
- Prefer the Python standard library unless a dependency is clearly justified.
- Avoid heavy dependencies unless necessary.
- Do not upgrade major dependencies without explicit discussion.
- Prefer explicit error handling and user prompts over layered fallback behavior.
- Reuse existing utilities before adding new helpers, especially in `saiUtils.py`, `saiDataLogger.py`, and `saiMQTTIngest.py`.
- Keep FastAPI route handlers thin; move substantial behavior into supporting modules where appropriate.
- Avoid blocking operations inside async request handlers.
- Preserve existing background task structure unless the user explicitly requests a larger refactor.
- Add concise docstrings to public classes and functions when touching public interfaces.

## Logging and diagnostics

- Keep logging lightweight.
- Use `printDM(...)` and existing debug flags instead of introducing new logging patterns unless there is a strong reason.
- Do not add noisy logging in hot paths.

## Python-rendered HTML and JavaScript

- In Python-rendered JS/HTML, especially `yield "..."` builders in `saiHtml.py`, do not emit JavaScript `//` comments inside generated strings.
- Use Python comments outside emitted strings instead.
- This is a known regression area; malformed inline JS comments have previously broken front-end rendering.

## MQTT, switch, and DB conventions

- Preserve legacy MQTT topics and payload shapes unless the user explicitly requests a breaking change.
- Switch events must be written through `saiDataLogger.log_switch_event`.
- Use UI/action switch keys in the form `<channel_id>::<label>`, for example `S1-sernum::Eastside_Pump`.
- Keep existing entity IDs and discovery payload shapes stable when changing Home Assistant integration.
- Treat MQTT discovery, remote switch identity, and DB key compatibility as high-risk areas.

## Home Assistant guidance

- Expected flow is: configure broker and HA settings, start MQTT ingest, let the HA bridge advertise entities, then let HA observe/control through MQTT topics.
- Prefer non-breaking changes to discovery payloads, entity IDs, and topic structure.

## Configuration hygiene

- Runtime configuration lives under `sensor_settings/`, `switch_settings/`, and `system_settings/`.
- Use `factory/` and `factory_nodus/` defaults for templates and examples.
- Do not commit secrets, Wi-Fi credentials, MQTT credentials, or private host-specific configuration.
- Guard against missing config files and favor idempotent setup behavior.

## Database and migration rules

- Sensorius persists runtime data in SQLite.
- Preserve backward compatibility for schema and query behavior when practical.
- If schema or persistence logic changes, include migration logic or a compatibility path.
- Confirm historical query behavior still works after DB changes.
- Document behavior changes that affect stored data or deployment expectations.

## Architecture boundaries

- Keep the shared switch controller contract stable across local and remote controllers.
- Do not break dynamic controller creation for newly discovered switches.
- Remote automation must continue to work with live data, cache, and DB fallback behavior.
- Major concurrency, architecture, or storage refactors should be surfaced clearly to the user before implementation.

## Testing and verification

- Prefer the smallest relevant verification command first.
- Use `pytest` against `testApparatus/` for focused coverage when possible.
- Example targeted commands:
  - `pytest testApparatus/test_homeassistant_mqtt.py`
  - `pytest testApparatus/test_mqtt_ingest_auth.py`
  - `pytest testApparatus/test_datalogger_migration.py`
  - `pytest testApparatus/test_compile_python.py`
- If automation coverage is missing, run the most relevant existing targeted tests and say what was not verified.
- When changes affect runtime behavior, include concise notes about environment assumptions such as platform, broker type, and whether web UI or onboarding paths were exercised.
- Suggest service restart steps only when they are actually needed.

## Setup and local commands

- Main entrypoint: `python3 Sensorius.py`
- Default UI URL: `http://127.0.0.1:8000`
- Auto-select setup path: `./install.sh`
- Platform-specific setup scripts live under `deploy_scripts/`
- Pytest configuration is in `pytest.ini` and test discovery points at `testApparatus/`

## Safety rules

- Do not run destructive commands without explicit user request.
- Prefer idempotent operations.
- Avoid broad search-and-replace edits unless the task specifically requires them.
- Treat changes to MQTT contracts, settings materialization, onboarding, and persistence as compatibility-sensitive.

## Versioning Rule

When you make a code change, update `__version__` in `__init__.py` using:

`v0.<year>.<doy>.<x>`

- `<year>`: 2-digit year
- `<doy>`: 3-digit day of year
- `<x>`: per-day incrementing patch counter

Rule:

1. Read the current version from `__init__.py`.
2. If `<year>` and `<doy>` match today, increment `<x>` by 1.
3. If the day changed, reset `<x>` to `1`.
4. Preserve zero padding.
5. Only update the version string, not unrelated lines.

Example:

- `v0.26.057.2` -> `v0.26.057.3` on the same day
- `v0.26.057.2` -> `v0.26.058.1` on the next day

## Agent output expectations

- When making changes, summarize:
  - what changed
  - why it changed
  - what was verified
  - any residual risk or unverified 
  - update the version in `__init__.py`
- If a requested change conflicts with these rules, call out the tradeoff and proceed only with explicit user direction.
