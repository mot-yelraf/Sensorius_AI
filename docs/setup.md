# Setup Guide

This guide captures the setup and startup material originally documented in `README.md`.

## Setup Script Note

Most setup scripts in this repository have not been fully verified across all target OS/version combinations. Use them at your own risk, review them before running, and prefer a test machine first.

All shell setup scripts in `deploy_scripts/` deploy the application files into `~/Sensorius` and configure services to run from that path.

## Raspberry Pi Setup (Direct Sensor + Hub)

### Raspberry Pi OS Bookworm

Use the root dispatcher (recommended):

```bash
chmod +x setup.sh
sudo ./setup.sh
```

It asks whether to use `uv` or `pip`, detects Bookworm/Trixie/macOS/Linux, and dispatches to the matching script in `deploy_scripts/`.

Or run Bookworm directly:

```bash
chmod +x deploy_scripts/setup_bookwork_uv.sh
sudo ./deploy_scripts/setup_bookwork_uv.sh
```

Bookworm scripts:

- Installs system and Python dependencies
- Enables I2C and sets regional Wi-Fi settings
- Installs and enables a systemd service (`sensorius.service`)
- Configures the hostname and timezone

Raspberry Pi Wi-Fi guidance for Nodus:

- Best practice: configure the Raspberry Pi to use a 2.4 GHz network path before onboarding Nodus devices with Sensorius.
- If your router provides separate 2.4 GHz and 5 GHz SSIDs, connect the Raspberry Pi to the 2.4 GHz SSID.
- If the Raspberry Pi is not headless, configure Wi-Fi locally on the Pi before running Sensorius setup or onboarding.
- If your router uses a single SSID for both 2.4 GHz and 5 GHz, the preferred setup is to connect the Raspberry Pi to the router by ethernet.
- With ethernet connected, the Raspberry Pi can still route to Nodus devices that are on the router's 2.4 GHz Wi-Fi network.
- If ethernet is not available on a single-SSID router, configure the Raspberry Pi locally so its Wi-Fi connection is using the router's 2.4 GHz radio before running the Pi headless.
- Router-specific band steering, AP isolation, and roaming behavior are outside the scope of Sensorius setup.

### Raspberry Pi OS Trixie

Use one of these scripts:

```bash
chmod +x deploy_scripts/setup_trixie.sh
sudo ./deploy_scripts/setup_trixie.sh
```

Or with `uv`:

```bash
chmod +x deploy_scripts/setup_trixie_uv.sh
sudo ./deploy_scripts/setup_trixie_uv.sh
```

## macOS Setup (Hub + MQTT Only)

macOS runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on macOS.

Use one of the macOS setup scripts:

```bash
chmod +x deploy_scripts/setup_mac.sh
./deploy_scripts/setup_mac.sh
```

Or with `uv`:

```bash
chmod +x deploy_scripts/setup_mac_uv.sh
./deploy_scripts/setup_mac_uv.sh
```

Notes:

- These scripts install Python 3.13.5 and create a local `.venv`.
- Mosquitto is installed and configured with anonymous access on port 1883.
- Mosquitto runs in user scope (LaunchAgent + user-owned config/data paths), avoiding `/usr/local/var/*` permission issues.
- Add Device onboarding uses native macOS Wi-Fi tools (`networksetup`/`airport`); no `nmcli` install is required.
- GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
- If `pywebview` is not installed, Sensorius will continue headless.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

## Windows Setup (Hub + MQTT Only)

Windows runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on Windows.

Use one of the Windows setup scripts (run in an elevated PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win.ps1
```

Or with `uv`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win_uv.ps1
```

Notes:

- These scripts use `winget`.
- By default (`BROKER_SCOPE=system`), run in elevated PowerShell (Administrator).
- For user-level setup, set `BROKER_SCOPE=user` and run in normal PowerShell.
- Python 3.13.5 is installed via `pyenv-win` (pip script) or `uv` (uv script).
- Mosquitto is installed and configured with anonymous access on port 1883.
- Add Device onboarding uses native Windows Wi-Fi tooling (`netsh`); no `nmcli` equivalent install is required.
- GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
- If `pywebview` is not installed, Sensorius will continue headless.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

Windows scope example:

```powershell
$env:BROKER_SCOPE = 'user'
.\deploy_scripts\setup_win_uv.ps1
```

## Linux Setup (Debian/Ubuntu, Hub + MQTT Only)

Linux non-Pi hosts run Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported in this setup path.

Use the Linux setup script:

```bash
chmod +x deploy_scripts/setup_linux.sh
./deploy_scripts/setup_linux.sh
```

Notes:

- Uses `apt` to install precompiled system packages (`python3`, `mosquitto`, etc.).
- Installs Python dependencies from `deploy_scripts/setup_reqs_linux.txt`.
- Requirements now include `astral` for sunrise/sunset automation conditions.
- Defaults to wheel-only Python installs (`PIP_ONLY_BINARY=1`) to avoid source builds.
- Set `INSTALL_PYWEBVIEW=0` to skip pywebview and force headless mode.
- Broker scope defaults to `system`; set `BROKER_SCOPE=user` to use user-owned mosquitto config/data and user service startup.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

Linux scope example:

```bash
BROKER_SCOPE=user ./deploy_scripts/setup_linux.sh
```

## FarmOS Integration Prerequisites

FarmOS export is optional and can be enabled after install from the web UI System Settings.

- FarmOS export uses the built-in `httpx` backend only.
- Python requirements do not include `farmOS.py`.
- You only need API endpoint/auth access to your farmOS instance.
- Configure FarmOS URL/auth and run the built-in `Test` action in System Settings before turning `FarmOS.ENABLED` on.
- See `docs/farmos.md` for key settings and troubleshooting details.

## Astral Automation Note

If you use Astral automation conditions, Sensorius can auto-resolve location from IP (internet required) when `[Astral].AUTO_IP = true` and manual coordinates are not set.

## Web UI Astral Data

When Astral location and timezone are available, the web UI dashboard shows:

- Sun position path with sunrise, noon, and sunset times
- Moon phase visualization and illumination

Location resolution order is:

- Manual `[Astral].LATITUDE` / `[Astral].LONGITUDE` / `[Astral].TIMEZONE`
- IP geolocation when `[Astral].AUTO_IP = true`

If manual lat/lon are blank and IP lookup succeeds, Sensorius now persists the discovered coordinates into `[Astral]` for later boots.

## Application Startup

### Manual Start

```bash
python3 Sensorius.py
```

### Enable and Start as Service

```bash
sudo systemctl enable sensorius.service
sudo systemctl start sensorius.service
```

## Uninstall Scripts

Optional uninstall helpers are included for local cleanup:

- Linux: `./deploy_scripts/uninstall_linux.sh`
- macOS: `./deploy_scripts/uninstall_mac.sh`
- Windows (PowerShell): `.\deploy_scripts\uninstall_win.ps1`

These scripts are interactive and attempt to remove the local venv and optional service/broker setup.
