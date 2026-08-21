# Contributing to Sensorius (saiSensorius)

Thank you for your interest in improving Sensorius.

Sensorius is the hub/runtime component of Sensorius Automatio Instrumentorum (Sensorius AI). It is a Python-based system that combines:

- FastAPI web application
- MQTT ingest and device discovery
- SQLite-based data logging
- Direct sensor and relay support (Raspberry Pi)
- Cross-platform runtime (Raspberry Pi, macOS, Windows, Linux)

This project is educational and approachable, but stability and architectural consistency are primary goals.

## Workflow

1. Fork the repository.
2. Create a feature branch from `trunk`.
3. Make focused, well-documented changes.
4. Submit a pull request against `trunk`.

Direct pushes to `trunk` are not accepted.

Keep pull requests small and focused on a single logical change.

Pull requests run read-only remote compile and focused regression checks. The
workflow does not receive repository secrets and is safe to run for forked pull
requests. Maintainers must review workflow-file changes before approving a run.
The rendered dashboard browser gate remains a trusted-host check and is not run
by GitHub Actions.

## Areas Where Contributions Are Welcome

- Documentation improvements
- UI clarity and usability enhancements
- Bug fixes in MQTT ingest or onboarding flows
- Additional directly-connected sensor drivers
- Additional switch/relay backends
- Test improvements under `testApparatus/`
- Performance or stability improvements

## Dependency Policy

Sensorius intentionally avoids unnecessary dependencies.

When contributing:

- Prefer the Python standard library where practical.
- Avoid introducing new third-party dependencies unless clearly justified.
- Do not upgrade major dependencies (FastAPI, Pydantic, Uvicorn, etc.) without discussion.
- If adding a dependency, explain why it is required.

Dependency upgrades can impact stability across platforms and must be considered carefully.

## Architecture and Async Discipline

Sensorius includes:

- FastAPI async routes
- Background tasks
- MQTT ingest loop
- Database persistence
- Sensor/switch abstraction layers

Current switch abstractions include:

- `SwitchController` (local GPIO relay controllers)
- `RemoteSwitchController` (MQTT-backed Nodus/Pico controllers)

When contributing:

- Avoid blocking operations inside async request handlers.
- Avoid long-running synchronous DB operations in request paths.
- Preserve existing background task structure.
- Discuss major concurrency or architectural refactors before submitting a PR.

Large structural rewrites should be proposed via an issue before implementation.

## Database Changes

Sensorius stores runtime data in `sensorius_data.db`.

If modifying database schema or storage logic:

- Preserve backward compatibility where possible.
- Provide migration logic if required.
- Confirm historical data queries still function.
- Document schema changes in `README.md` or docs.

Unannounced schema changes can break deployed hubs.

## Configuration Hygiene

Runtime configuration is stored under:

- `sensor_settings/`
- `switch_settings/`
- `system_settings/`

Do not commit:

- Personal Wi-Fi credentials
- MQTT credentials
- Device-specific configuration files
- Private IP addresses

Use the `factory/` or `factory_nodus/` folders for defaults and sample configs.

## Code Style

- Prefer clear, explicit naming over clever abstractions.
- Keep modules cohesive and readable.
- Avoid deep or circular import chains.
- Use `printDM()` from `saiUtils.py` for structured logging.
- Add concise docstrings to public classes and functions.
- Keep FastAPI route handlers thin and readable.

Clarity and maintainability take precedence over stylistic cleverness.

## Testing Expectations

The automated test suite uses pytest, with tests and focused diagnostic scripts
under `testApparatus/`. Run the smallest relevant test module first, then the
broader suite when the change warrants it:

```bash
pytest testApparatus/test_compile_python.py
pytest
```

When submitting changes, describe:

- Environment used (Raspberry Pi, macOS, Windows, Linux)
- Python version
- Broker type (mosquitto, HA broker, etc.)
- Whether web UI loads successfully
- Whether MQTT ingest functions correctly
- Whether onboarding still works (if touched)
- Whether database persistence remains stable (if touched)

Place new automated tests under `testApparatus/` and keep hardware-dependent
assumptions explicit.

Before committing work intended for a pull request, run the host-side browser
validation:

```bash
npm ci
npm run validate:pr
```

This starts an isolated local Sensorius fixture host and runs Chromium through
the project Playwright checks. It does not start sensor, MQTT, service, or
production runtime tasks, and it does not rely on GitHub Actions. Install the
local Chromium bundle once with `npx playwright install chromium` if Playwright
reports that the browser executable is missing.

The browser gate is required when a change can affect rendered UI behavior.
Record its result in the pull request. Maintainers should run it from a trusted
checkout before merging an untrusted contribution; do not expose local runtime
settings, broker credentials, or other secrets to contributor-controlled code.

## Pull Request Guidelines

Pull requests should:

- Be small and focused
- Include a clear summary of what changed and why
- Include testing notes
- Update documentation if behavior changes
- Avoid unrelated formatting-only churn

PRs may be declined if they introduce architectural instability, unnecessary complexity, or undocumented behavior changes.

## Project Maturity

Sensorius is a pre-1.0 project under active development.

APIs, configuration formats, and internal structure may evolve.

Backward compatibility is considered but not guaranteed.

Stability and clarity take precedence over rapid feature expansion.
