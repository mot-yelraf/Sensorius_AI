# AGENTS.md

Operational instructions for coding agents working in this repository. This
file is the repo-local source of truth for agent behavior and extension
requirements.

## Scope

- Applies to the entire repository unless a deeper `AGENTS.md` overrides it.
- Prefer these repo-specific instructions over generic coding-agent defaults.
- When switching repositories, confirm the target repository before editing.
- Do not overwrite or revert user changes you did not make unless explicitly
  asked.

## Project Context

- Sensorius is a cross-platform environmental sensing and automation hub.
- Supported runtime targets are Raspberry Pi, macOS, Windows, and Linux.
- Raspberry Pi full-hub mode supports directly connected sensors and relay
  hardware.
- macOS, Windows, and non-Pi Linux run hub plus MQTT plus web UI mode and do
  not support direct GPIO or local sensor buses.
- All platforms support MQTT-backed Nodus sensors and switches.
- Prefer changes that remain safe on low-power and low-RAM devices.
- Use absolute paths when referring to on-device files in docs, comments, and
  user guidance.

## Canonical Documentation

- User guide: `docs/user_guide.md`
- Setup: `docs/setup.md`
- Operations: `docs/operations.md`
- Architecture: `docs/architecture.md`
- Configuration: `docs/configuration.md`
- MQTT overview: `docs/mqtt.md`
- Sensorius/Nodus contract: `docs/sensorius_contract.md`
- Calibration MQTT contract: `docs/calibration_mqtt_contract.md`
- Home Assistant: `docs/homeassistant.md`
- farmOS: `docs/farmos.md`
- Sensors: `docs/sensors.md`
- Hardware: `docs/hardware.md`
- Automations: `docs/automations.md`

If implementation behavior and docs disagree, inspect the code and update the
canonical docs as part of the change.

## Runtime Architecture

Primary modules:

- `Sensorius.py`: stable repository-root launcher and compatibility import
  alias.
- `sensorius/app.py`: process entrypoint and runtime wiring.
- `sensorius/saiTaskSupervisor.py`: background task supervisor and restart loop.
- `sensorius/saiWatchdog.py`: task heartbeat monitor and forced-exit safety behavior.
- `sensorius/saiGarbageCollection.py`: watchdog-friendly GC loop.
- `sensorius/saiWebServer.py`: FastAPI app creation, static/template mounting, uvicorn,
  and optional pywebview launch.
- `sensorius/saiWebRoutes.py`: UI, API, settings, onboarding, calibration, switch,
  graph, and diagnostics routes.
- `sensorius/saiSettings.py`: system settings loading, seeding, caching, atomic writes,
  backup creation, secret obfuscation, and Astral location resolution.
- `sensorius/saiSensorSettingsManager.py`: per-sensor TOML manager.
- `sensorius/saiSwitchSettingsManager.py`: per-switch TOML manager.
- `sensorius/saiDataLogger.py`: SQLite persistence, schema migration, query helpers,
  latest-value caches, and listener callbacks.
- `sensorius/saiSensor.py`, `sensorius/saiSensorFactory.py`, and
  `sensorius/sensor_modules/`: local sensor detection and readout.
- `sensorius/saiSwitch.py`, `sensorius/saiSwitchFactory.py`: local and remote
  switch controller behavior.
- `sensorius/saiAutomationManager.py`: Advanced automation TOML schema and runtime rule
  cache.
- `sensorius/saiMQTTClient.py`: outbound local-sensor MQTT publisher.
- `sensorius/saiMQTTIngest.py`: MQTT discovery, topic registration, remote state cache,
  Nodus settings shadowing, onboarding events, calibration events, and optional
  Nodus mirroring.
- `sensorius/saiHomeAssistantMqtt.py`: Home Assistant MQTT discovery, state,
  availability, and command bridge.
- `sensorius/saiFarmOSBridge.py`: farmOS JSON:API export queue and worker.
- `sensorius/saiWeeWX.py`: optional WeeWX archive and MQTT ingest.
- `sensorius/saiNodusOTA.py`: Nodus OTA package and job support.

Startup sequence:

1. Configure logging and start the async runtime.
2. Load or seed `system_settings/<device_id>/settings.toml`.
3. Create supervisor, GC manager, network manager, and data logger.
4. Build local sensor controllers only when the Pi sensor runtime is available.
5. Build switch controllers through `build_switch_controller(...)`.
6. Attach runtime objects to `sensorius.saiWebRoutes` and `app.state`.
7. Start local MQTT publishers only for local sensors and only when publishing
   to a non-local broker.
8. Start MQTT ingest when `SensorNetwork.BROKER` is configured.
9. Start Home Assistant bridge when enabled and MQTT is connected.
10. Register WeeWX, farmOS, daily summaries, watchdog, GC, sensor collection,
    and switch monitor tasks.
11. Register FastAPI routes and run the web server.

## Runtime Paths And State

`sensorius.saiRuntimePaths.resolve_runtime_base_dir(...)` controls writable settings
roots.

- Outside pytest, bare `system_settings`, `sensor_settings`, and
  `switch_settings` resolve under the user's runtime directory, for example
  `/home/<user>/Sensorius/` on Linux or `/Users/<user>/Sensorius/` on macOS.
- Inside pytest, relative roots remain relative for test isolation.
- Absolute paths are used unchanged.

Canonical runtime state:

- `/home/<user>/Sensorius/system_settings/<device_id>/settings.toml`
- `/home/<user>/Sensorius/sensor_settings/<sensor_id>/sensor.toml`
- `/home/<user>/Sensorius/switch_settings/<switch_id>/switch.toml`
- `/home/<user>/Sensorius/switch_settings/automations/automations.toml`
- `sensorius_data.db` in the process working directory unless a caller passes
  another DB path.

Factory templates:

- `system_settings/factory/`
- `system_settings/factory_nodus/`
- `sensor_settings/factory/`
- `sensor_settings/factory_nodus/`
- `switch_settings/factory/`
- `switch_settings/factory_nodus/`

Do not commit secrets, Wi-Fi credentials, MQTT credentials, API keys, or private
host-specific runtime configuration.

## Configuration Requirements

- Environment and `.env` control logging, HTTP binding, GUI mode, watchdog, GC,
  DB retention, autostart, and API keys.
- TOML settings control system, sensor, switch, integration, display,
  automation, calibration, and Nodus shadow state.
- Use settings managers for writes instead of ad hoc file edits when code paths
  exist.
- Keep setup and runtime behavior idempotent.
- Guard against missing files and missing sections.
- Preserve factory defaults and add migrations or compatibility defaults for
  existing installations.
- Secrets saved through `sensorius.saiSettings` are obfuscated, not encrypted. Do not
  treat obfuscation as a security boundary.

Important sections:

- `[Network]`: hostname and HTTP port.
- `[SensorNetwork]`: primary MQTT broker, port, TLS, Nodus debug, and legacy
  poller controls.
- `[HomeAssistant]`: HA broker, discovery, retain, passthrough, and mirroring.
- `[FarmOS]`: farmOS URL, TLS, auth, queue, timeout, and bundle.
- `[WeeWX]`: optional archive/MQTT station ingest.
- `[Time]` and `[Astral]`: timezone, offsets, coordinates, and IP geolocation.
- `[Display]`: dashboard gauge size and display style.
- `[Sensor]`, `[Calibration]`, `[Display]`: per-sensor state.
- `[Switch]`: per-switch type, identity, channel labels, channel IDs, and
  state.

## Persistence Requirements

Sensorius persists runtime data in SQLite through `sensorius/saiDataLogger.py`.

Key tables:

- `readings`: sensor metric values.
- `switch_ids`: switch/channel identity metadata.
- `sw_events`: switch state transitions.
- `biodynamic_notes`: calendar notes.
- `biodynamic_daily_summaries`: generated summaries.

Rules:

- Preserve backward compatibility for schema and query behavior where practical.
- If schema or persistence logic changes, include additive migration logic or a
  compatibility path.
- Confirm historical query behavior still works after DB changes.
- Switch events must be written through `sensorius.saiDataLogger.log_switch_event`.
- Sensor readings must be written through `sensorius.saiDataLogger.log_readings` unless a
  narrow test bypass is explicitly justified.
- Do not reintroduce switch-state storage as synthetic `readings` metrics.
- Preserve canonical switch keys in the form `<channel_id>::<label>`.
- Retention is controlled by `SENSORIUS_DB_RETENTION_DAYS`; avoid expensive
  cleanup in hot paths.

## MQTT And Nodus Requirements

Treat MQTT discovery, remote switch identity, and Nodus settings shadowing as
compatibility-sensitive.

Rules:

- Preserve legacy MQTT topics and payload shapes unless the user explicitly
  requests a breaking change.
- Retained `nodus/<device_id>/meta` is the full remote-device discovery source.
- Accepted runtime changes should flow through correlated `meta/patch` updates.
- AP-mode HTTP is for onboarding/bootstrap and diagnostics, not steady-state
  health polling.
- Do not reintroduce periodic `/hayd` or `/itaot` polling for onboarded Nodus
  health.
- Use MQTT heartbeat and availability topics for liveness.
- Publish `/set` commands non-retained by default.
- If Sensorius intentionally publishes a retained `/set` command, Sensorius
  owns clearing it with an empty retained payload after success.
- Keep runtime config writes paced and correlated by message ID.
- Calibration commands must follow `docs/calibration_mqtt_contract.md`.
- Nodus shadow writes under `sensor_settings/`, `switch_settings/`, and
  `system_settings/` must remain idempotent and tolerant of partial metadata.

## Switch Controller Contract

Local and remote switch behavior is unified behind:

- `SwitchController`
- `RemoteSwitchController`
- `build_switch_controller(...)`

Shared public behavior used by UI, HA, MQTT, and automation:

- `get_switch_names()`
- `get_state(label)`
- `set_state(label, on, force=False, event_source=...)`
- `override_script`
- `last_state`
- `channel_id_for_label`
- `run_controladora_monitor(...)`

Rules:

- Keep this contract stable.
- Do not break dynamic controller creation for newly discovered switches.
- Remote automations must continue to work with live data, cache, and DB
  fallback behavior.
- Use UI/action switch keys in the form `<channel_id>::<label>`.
- Keep channel IDs stable for physical channels.
- Manual UI toggles are blocked when an enabled Advanced automation owns the
  same switch key.

## Automation Requirements

- Advanced automation state lives in
  `switch_settings/automations/automations.toml`.
- Use `sensorius/saiAutomationManager.py` for reads/writes.
- Preserve `[Meta]`, `[Advanced]`, and `[Scripts]` schema compatibility.
- Keep `script_json` compact and valid JSON when writing rules.
- New rule fields require UI, runtime evaluator, and tests.
- Avoid blocking operations in switch monitor loops.
- Test critical automation changes with focused automation and switch tests.

## Home Assistant Requirements

Expected flow:

1. Configure broker and HA settings.
2. Start MQTT ingest.
3. Let the HA bridge advertise retained discovery.
4. Let HA observe and control through MQTT topics.

Rules:

- Keep discovery payload shapes and entity IDs stable.
- Keep `DISCOVERY_PREFIX`, `BASE_TOPIC`, retain behavior, and legacy topic
  flags backward compatible unless a breaking change is explicitly requested.
- HA commands must route through the shared switch controller path.
- Discovery should be based on known sensor metrics and switch identities.
- Publish retained discovery and state by default.

## farmOS Requirements

- farmOS export is optional and uses the built-in `httpx` backend.
- Do not add `farmOS.py` unless explicitly justified and discussed.
- Export listens to newly written readings through `sensorius.saiDataLogger` listeners.
- Queue behavior is in-memory and bounded by `FarmOS.QUEUE_MAX`.
- Expose meaningful status through `/farmos/status`.
- Preserve `/farmos/test` behavior when changing auth or payload construction.

## WeeWX Requirements

- WeeWX ingest is optional.
- Keep archive and MQTT ingest paths normalized through sensor settings and
  `sensorius.saiDataLogger`.
- Preserve derived rain behavior unless explicitly changing station semantics.
- Use `testApparatus/test_weewx_ingest.py` and
  `testApparatus/test_weewx_sensor_settings.py` for focused checks.

## Web UI And Route Requirements

- Keep FastAPI route handlers thin where practical.
- Move substantial behavior into supporting modules when adding new features.
- Avoid blocking operations inside async handlers. Use `asyncio.to_thread` or a
  background task for blocking I/O.
- Preserve existing background task structure unless the user requests a larger
  refactor.
- Keep template, static asset, and route state wiring consistent with
  `sensorius/saiWebServer.py` and `sensorius/saiWebRoutes.py`.
- Use existing UI templates under `ui_templates/` and assets under
  `ui_static/`.
- In Python-rendered JS/HTML, especially `yield "..."` builders in
  `sensorius/saiHtml.py`, do not emit JavaScript `//` comments inside generated strings.
  Use Python comments outside emitted strings.

## Sensor Extension Requirements

When adding a local sensor:

1. Add or update a module under `sensorius/sensor_modules/`.
2. Preserve the `measurements` list and metric names carefully.
3. Wire detection through `sensorius/saiSensorFactory.py`.
4. Add factory/default settings if needed.
5. Add calibration support only when the runtime module can reload safely.
6. Add focused tests for detection, metric names, and calibration behavior.

When adding a remote/Nodus sensor feature:

1. Update the Nodus MQTT contract docs if the payload changes.
2. Update `sensorius/saiMQTTIngest.py` normalization and shadow settings logic.
3. Preserve retained `meta` and `meta/patch` compatibility.
4. Verify local dashboard, DB logging, Home Assistant discovery, and
   calibration implications.

Metric names are stored in the database, display settings, automations, and HA
entities. Do not rename metrics casually.

## Code Generation Rules

- Keep edits minimal, targeted, and easy to review.
- Do not reformat unrelated code.
- Prefer clear, explicit naming over abstraction for its own sake.
- Keep modules cohesive and avoid deep or circular import chains.
- Prefer the Python standard library unless a dependency is clearly justified.
- Avoid heavy dependencies unless necessary.
- Do not upgrade major dependencies without explicit discussion.
- Reuse existing utilities before adding helpers, especially in `sensorius/saiUtils.py`,
  `sensorius/saiDataLogger.py`, and `sensorius/saiMQTTIngest.py`.
- Prefer explicit error handling and clear operator-visible failures over
  layered silent fallbacks.
- Add concise docstrings to module level with explanatory paragraph after the concise description.
- Add concise docstrings to public classes and functions when touching public
  interfaces.
- Keep logging lightweight. Use `printDM(...)` and existing debug flags instead
  of introducing a new logging pattern.
- Do not add noisy logging in hot paths.

## Testing And Verification

Prefer the smallest relevant verification first.

### Web UI Verification Environment

- Playwright and Google Chrome are installed on this system and should be used
  to exercise web UI changes when running inside VS Code.
- The in-app Browser (`@Browser`) capabilities are not available from the VS
  Code agent surface. An "IAB failed" or "No browser is available" result in
  VS Code is an expected surface limitation, not evidence that the opened tab,
  browser profile, VS Code extension, or browser data is broken.
- Do not attempt to fix this VS Code limitation by reinstalling extensions,
  resetting browser data, or repeatedly retrying in-app-browser discovery.
- After recognizing the VS Code surface limitation, use terminal-based
  verification appropriate to the change: Playwright, headless Chrome, Chrome
  DevTools Protocol, `curl`, and screenshot tooling. These tools are independent
  of the in-app Browser and may require their normal execution permissions.
- For headless screenshots, use Playwright's bundled Chromium/headless shell.
  Do not launch `/Applications/Google Chrome.app` directly with `--headless`
  because it triggers macOS GUI-registration crashes and user-facing crash
  dialogs.
- Prefer Playwright for DOM assertions and interactive behavior, and use Chrome
  screenshots when visual layout verification is important. Use `curl` for
  endpoint, rendered-markup, and health checks that do not require a browser.
- When the task explicitly requires `@Browser`, browser annotations, in-app
  screenshots, DOM inspection, or interactive page control through the Browser
  plugin, run it in a Codex chat inside the desktop app with the Browser plugin
  installed and enabled.
- Report which verification surface was used and any UI behavior that remains
  unverified.

Focused test examples:

- `pytest testApparatus/test_homeassistant_mqtt.py`
- `pytest testApparatus/test_mqtt_ingest_auth.py`
- `pytest testApparatus/test_mqtt_ingest_liveness.py`
- `pytest testApparatus/test_datalogger_migration.py`
- `pytest testApparatus/test_automation_contract.py`
- `pytest testApparatus/test_sai_switch_controller.py`
- `pytest testApparatus/test_sai_switch_factory.py`
- `pytest testApparatus/test_sai_switch_settings_manager.py`
- `pytest testApparatus/test_sai_switch_trigger_manager_compat.py`
- `pytest testApparatus/test_onboarding_v2_core.py`
- `pytest testApparatus/test_onboarding_v2_routes.py`
- `pytest testApparatus/test_nodus_settings_schema_writes.py`
- `pytest testApparatus/test_weewx_ingest.py`
- `pytest testApparatus/test_compile_python.py`

If automation coverage is missing, run the most relevant existing targeted
tests and state what was not verified.

When changes affect runtime behavior, include concise environment assumptions
such as platform, broker type, direct hardware availability, and whether the web
UI or onboarding paths were exercised.

Suggest service restart steps only when they are actually needed.

## Setup And Local Commands

- Main entrypoint: `python3 Sensorius.py`
- Default UI URL: `http://127.0.0.1:8000`
- Health check: `http://127.0.0.1:8000/healthz`
- Auto-select setup path: `./install.sh`
- Platform setup scripts: `deploy_scripts/`
- Pytest configuration: `pytest.ini`
- Test discovery: `testApparatus/`

## Safety Rules

- Do not run destructive commands without explicit user request.
- Prefer idempotent operations.
- Avoid broad search-and-replace edits unless the task specifically requires
  them.
- Treat MQTT contracts, settings materialization, onboarding, Home Assistant,
  switch identity, and persistence as compatibility-sensitive.
- Major concurrency, architecture, or storage refactors should be surfaced
  clearly to the user before implementation.

## Versioning Rule

When you make a code content change, update the canonical `__version__` in
`sensorius/__init__.py` using:

```text
v0.<year>.<doy>.<x>
```

- `<year>`: 2-digit year.
- `<doy>`: 3-digit day of year.
- `<x>`: per-day incrementing patch counter.

Rule:

1. Read the current version from `sensorius/__init__.py`.
2. If `<year>` and `<doy>` match today, increment `<x>` by 1.
3. If the day changed, reset `<x>` to `1`.
4. Preserve zero padding.
5. Only update the version string, not unrelated lines.

Example:

- `v0.26.057.2` becomes `v0.26.057.3` on the same day.
- `v0.26.057.2` becomes `v0.26.058.1` on the next day.

## Agent Output Expectations

When making changes, summarize:

- What changed.
- Why it changed.
- What was verified.
- Any residual risk or unverified area.
- The version update in `__init__.py`.

If a requested change conflicts with these rules, call out the tradeoff and
proceed only with explicit user direction.
