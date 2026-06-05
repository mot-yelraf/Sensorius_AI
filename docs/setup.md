# Setup Guide

Sensorius setup scripts install the runtime under the user's home directory,
for example `/home/<user>/Sensorius` on Linux or
`/Users/<user>/Sensorius` on macOS, and configure the web UI, Python
environment, optional GUI support, Mosquitto, and service or autostart behavior
for the target platform.

Review setup scripts before running them on production devices. They are
designed to be idempotent, but platform package managers, service managers, and
Wi-Fi tooling differ across OS releases.

## Install Versus Deploy

Use `install.sh` or the platform `setup_*.sh` and `setup_*.ps1` scripts for a
first install, repair install, or intentional reconfiguration of system
packages, Python environment, Mosquitto, service files, and autostart behavior.
These scripts prepare a runtime directory and may rewrite installer-managed
configuration for the target platform.

Use `deploy_scripts/deploy_sai.sh` for routine updates to systems that already
have Sensorius installed. The deploy script syncs application source into each
configured runtime directory while preserving installed runtime state,
including `sensorius_data.db*`, `system_settings/`, `sensor_settings/`, and
`switch_settings/`. It still updates factory templates under those settings
trees so new defaults can ship without replacing device-specific files.

When requirements files change, deploy the source update first, then install
the changed Python dependencies in the target runtime environment before
restarting Sensorius.

## Deployment Modes

Raspberry Pi:

- Full hub mode.
- Supports direct sensors and GPIO relay hardware when the required hardware
  libraries are available.
- Supports MQTT-backed Nodus sensors and switches.

macOS, Windows, and non-Pi Linux:

- Hub plus MQTT plus web UI mode.
- Does not support local GPIO/direct sensor runtime.
- Supports Nodus onboarding, MQTT discovery, dashboards, automations, Home
  Assistant, farmOS, and WeeWX ingest where dependencies are installed.

## Recommended Entry Point

Use the root dispatcher where possible:

```bash
chmod +x install.sh
sudo ./install.sh
```

The dispatcher asks whether to use `uv` or `pip`, detects the platform, and
calls a matching script under `deploy_scripts/`.

## Raspberry Pi OS Bookworm

Pip path:

```bash
chmod +x deploy_scripts/setup_bookworm.sh
sudo ./deploy_scripts/setup_bookworm.sh
```

uv path:

```bash
chmod +x deploy_scripts/setup_bookwork_uv.sh
sudo ./deploy_scripts/setup_bookwork_uv.sh
```

The uv script name is currently `setup_bookwork_uv.sh` in the repository.

Bookworm setup performs the normal Pi install work:

- Installs system and Python dependencies.
- Enables I2C.
- Sets regional Wi-Fi settings where the script supports it.
- Deploys application files to the platform runtime directory, such as
  `/home/<user>/Sensorius` or `/Users/<user>/Sensorius`.
- Configures and enables `sensorius.service`.
- Configures hostname/timezone prompts where supported.

## Raspberry Pi OS Trixie

Pip path:

```bash
chmod +x deploy_scripts/setup_trixie.sh
sudo ./deploy_scripts/setup_trixie.sh
```

uv path:

```bash
chmod +x deploy_scripts/setup_trixie_uv.sh
sudo ./deploy_scripts/setup_trixie_uv.sh
```

The Trixie setup scripts use `libopenblas-dev` for BLAS support because
`libatlas-base-dev` is no longer available in Debian Trixie. Raspberry Pi OS
Trixie also uses `pinctrl` for command-line GPIO inspection instead of the
retired `raspi-gpio` package; Sensorius runtime GPIO support uses the Python
`lgpio`/`rpi-lgpio` stack from the Trixie requirements file.

To validate the Trixie uv path before rewriting the real application venv or
service configuration, run:

```bash
./deploy_scripts/setup_trixie_uv.sh --preflight
```

Or through the root dispatcher:

```bash
SETUP_PY_MANAGER=uv ./install.sh --preflight
```

Preflight installs/checks the Trixie APT dependency set, creates a temporary
venv, installs the full Python requirements there, verifies key imports, and
then removes the temporary venv.

On Raspberry Pi desktop installs, the backend `sensorius.service` runs
headless. The setup scripts also install
`/home/<user>/.config/autostart/sensorius-gui.desktop`, which opens the
pywebview shell from the user's graphical desktop session. This requires a
Raspberry Pi OS desktop session or desktop auto-login; headless/Lite images can
still use the browser UI at `http://127.0.0.1:8000`.

## macOS

macOS runs Sensorius as an MQTT hub and web UI. Direct sensors and GPIO relay
hardware are not supported on macOS.

Pip path:

```bash
chmod +x deploy_scripts/setup_mac.sh
./deploy_scripts/setup_mac.sh
```

uv path:

```bash
chmod +x deploy_scripts/setup_mac_uv.sh
./deploy_scripts/setup_mac_uv.sh
```

Notes:

- The scripts create a local Python environment.
- Mosquitto is configured in user scope by default.
- Nodus onboarding uses native macOS Wi-Fi tools.
- GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
- If `pywebview` is unavailable, Sensorius continues headless.

## Windows

Windows runs Sensorius as an MQTT hub and web UI. Direct sensors and GPIO relay
hardware are not supported on Windows.

Run in elevated PowerShell for the default system broker scope:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win.ps1
```

uv path:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win_uv.ps1
```

User broker scope:

```powershell
$env:BROKER_SCOPE = 'user'
.\deploy_scripts\setup_win_uv.ps1
```

Notes:

- The scripts use `winget`.
- `BROKER_SCOPE=system` requires Administrator.
- `BROKER_SCOPE=user` uses user-owned broker config and startup behavior.
- Nodus onboarding uses native Windows Wi-Fi tooling.
- Set `INSTALL_PYWEBVIEW=0` to skip pywebview.

## Debian Or Ubuntu Linux

Non-Pi Linux runs Sensorius as an MQTT hub and web UI. Direct sensor/GPIO
runtime is not supported by this setup path.

```bash
chmod +x deploy_scripts/setup_linux.sh
./deploy_scripts/setup_linux.sh
```

User broker scope:

```bash
BROKER_SCOPE=user ./deploy_scripts/setup_linux.sh
```

Notes:

- Uses `apt` for system packages.
- Installs Python dependencies from `deploy_scripts/setup_reqs_linux.txt`.
- Defaults to wheel-only Python installs where the script supports it.
- Set `INSTALL_PYWEBVIEW=0` to skip pywebview.

## Nodus Wi-Fi Guidance

Nodus devices use 2.4 GHz Wi-Fi. For Raspberry Pi onboarding:

- Prefer connecting the Pi to a 2.4 GHz SSID before onboarding.
- If the router combines 2.4 GHz and 5 GHz under one SSID, ethernet is the most
  reliable Pi connection during onboarding.
- Disable AP isolation or guest-network isolation for the Sensorius host and
  Nodus devices.
- Router band steering and roaming behavior are outside Sensorius setup.

## Manual Runtime Start

From the installed runtime directory:

```bash
cd /home/<user>/Sensorius
python3 Sensorius.py
```

On macOS, use `/Users/<user>/Sensorius`. On Windows, use the setup script's
deployed runtime path, normally `C:\Users\<user>\Sensorius`.

Default UI URLs:

```text
http://127.0.0.1:8000
http://<sensorius-host-ip>:8000
http://<hostname>.local:8000
```

Health check:

```text
http://127.0.0.1:8000/healthz
```

## Service Start

Systemd system service:

```bash
sudo systemctl enable sensorius.service
sudo systemctl start sensorius.service
```

Systemd user service:

```bash
systemctl --user enable sensorius.service
systemctl --user start sensorius.service
```

Service files should use the deployed absolute runtime path as their working
directory so `sensorius_data.db` and logs land in the expected place.

## Optional Integrations

Home Assistant:

- Requires an MQTT broker reachable by Sensorius and Home Assistant.
- Enable and configure from System Settings.
- Discovery topics are retained by default.

farmOS:

- Uses the built-in `httpx` JSON:API backend.
- Does not require `farmOS.py`.
- Configure URL/auth in System Settings and run the built-in test before
  enabling continuous export.

Astral automations and dashboard data:

- Use manual `[Astral]` latitude/longitude/timezone when possible.
- If `[Astral].AUTO_IP = true`, Sensorius may use IP geolocation and persist
  coordinates when manual values are empty.
- Coordinates persisted from IP geolocation are marked with `[Astral].SOURCE =
  "ip"` and `[Astral].PROVIDER`, so Sensorius can refresh them later without
  overwriting hand-entered coordinates.
- Automatic detection tries `ipapi.co` first, then `ip-api.com`, then
  `ipwho.is`; this keeps the IPv4-based result ahead of providers that may
  resolve an IPv6 address to a different city.
- In System Settings, clearing both latitude and longitude then saving asks
  Sensorius to re-detect via IP geolocation. If that lookup cannot resolve,
  the fields remain blank and manual coordinates are required.
- On startup, Sensorius retries automatic Astral location resolution in the
  background so a fresh install can recover when network service is slow.

WeeWX:

- Configure archive DB path or MQTT topic in System Settings.
- WeeWX readings are normalized into the same database and dashboard path as
  other sensors.

## Uninstall Helpers

Interactive cleanup helpers are included:

- Linux: `./deploy_scripts/uninstall_linux.sh`
- macOS: `./deploy_scripts/uninstall_mac.sh`
- Windows: `.\deploy_scripts\uninstall_win.ps1`

Review prompts carefully. Back up `/home/<user>/Sensorius/system_settings`,
`/home/<user>/Sensorius/sensor_settings`,
`/home/<user>/Sensorius/switch_settings`, and the database before removing a
production install. Use `/Users/<user>/Sensorius/...` on macOS and
`C:\Users\<user>\Sensorius\...` on Windows.
