# Contributing to Sensorius (saiSensorius)

Thanks for your interest in Sensorius. This repo is the hub/runtime for
Sensorius Automatio Instrumentorum (Sensorius AI). It runs on Raspberry Pi
with local sensors and GPIO, and can also run on macOS/Windows/Linux as a
MQTT hub + web UI for Nodus devices.

## Ways to help

- Improve docs, onboarding, or troubleshooting notes
- Fix bugs or edge cases in MQTT ingest, UI, or automation logic
- Add sensor drivers or switch backends
- Expand tests in `testApparatus/`

## Development setup

Pick the environment you want to develop on.

### Raspberry Pi (full hardware support)

1. Run the setup script:

```bash
chmod +x setup.sh
sudo ./setup.sh
```

2. Start the app:

```bash
python3 Sensorius.py
```

### macOS / Windows / Linux (hub + MQTT only)

macOS:

```bash
chmod +x setup_mac.sh
./setup_mac.sh
```

Windows PowerShell (run elevated):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_win.ps1
```

Linux (Debian/Ubuntu, non-Pi):

```bash
chmod +x setup_linux.sh
./setup_linux.sh
```

## Repo layout (high-level)

- `Sensorius.py` entrypoint
- `saiWebRoutes.py` FastAPI routes and UI endpoints
- `saiMQTTIngest.py` MQTT discovery/ingest
- `saiSensorFactory.py` sensor detection/creation
- `saiSwitchFactory.py` switch/relay backend creation
- `sensor_modules/` sensor drivers
- `sensor_settings/`, `switch_settings/`, `system_settings/` runtime config

## Adding a new (directly connected) sensor

1. Create a driver in `sensor_modules/` (see `sensor_template.py`).
2. Register it in `saiSensorFactory.py`.
3. Add or update default config under `sensor_settings/factory/`
   or `sensor_settings/factory_nodus/` if needed.
4. If it introduces new metrics, document them in `README.md`.

## Adding a new (directly connected) switch backend

1. Add or extend a backend in `saiSwitchFactory.py`.
2. Ensure settings are read through `saiSwitchSettingsManager.py`.
3. Add default config under `switch_settings/factory/`
   or `switch_settings/factory_nodus/` if needed.
4. Document MQTT topics and expected behavior in `README.md`.

## Configuration hygiene

Runtime settings for real devices are stored under:

- `sensor_settings/`
- `switch_settings/`
- `system_settings/`

Do not commit personal device settings or secrets. Use the `factory/` or
`factory_nodus/` folders for defaults and sample configs.

## Code style

- Prefer clear, explicit names over clever abstractions.
- Keep modules focused and avoid deep import chains.
- Use `printDM()` from `saiUtils.py` for structured logging.
- Add short docstrings for public classes and functions.

## Testing

There is no single test runner yet. Useful scripts live in `testApparatus/`.
When changing behavior, add a small script there or extend an existing one.

Suggested checks (manual):

- Start the app and open the UI at `http://127.0.0.1:8000`
- Verify MQTT ingest populates `sensorius_data.db`
- Confirm discovery + onboarding for at least one sensor/switch
- If you touched web routes, exercise the affected endpoints

## Pull requests

- Keep PRs small and focused
- Include a short summary and testing notes
- Update docs if behavior or configuration changes
