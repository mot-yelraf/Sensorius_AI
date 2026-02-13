# Setup Guide

This guide captures the setup and startup material originally documented in `README.md`.

## Setup Script Note

Most setup scripts in this repository have not been fully verified across all target OS/version combinations. Use them at your own risk, review them before running, and prefer a test machine first.

## Raspberry Pi Setup (Direct Sensor + Hub)

### Raspberry Pi OS Bookworm

Use one of these scripts:

```bash
chmod +x setup.sh
sudo ./setup.sh
```

Or with `uv`:

```bash
chmod +x setup_uv.sh
sudo ./setup_uv.sh
```

Bookworm scripts:

- Installs system and Python dependencies
- Enables I2C and sets regional Wi-Fi settings
- Installs and enables a systemd service (`sensorius.service`)
- Configures the hostname and timezone

### Raspberry Pi OS Trixie

Use one of these scripts:

```bash
chmod +x setup_trixie.sh
sudo ./setup_trixie.sh
```

Or with `uv`:

```bash
chmod +x setup_trixie_uv.sh
sudo ./setup_trixie_uv.sh
```

## macOS Setup (Hub + MQTT Only)

macOS runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on macOS.

Use one of the macOS setup scripts:

```bash
chmod +x setup_mac.sh
./setup_mac.sh
```

Or with `uv`:

```bash
chmod +x setup_mac_uv.sh
./setup_mac_uv.sh
```

Notes:

- These scripts install Python 3.13.5 and create a local `.venv`.
- Mosquitto is installed and configured with anonymous access on port 1883.
- GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
- If `pywebview` is not installed, Sensorius will continue headless.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

## Windows Setup (Hub + MQTT Only)

Windows runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on Windows.

Use one of the Windows setup scripts (run in an elevated PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_win.ps1
```

Or with `uv`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_win_uv.ps1
```

Notes:

- These scripts use `winget` and require running PowerShell as Administrator.
- Python 3.13.5 is installed via `pyenv-win` (pip script) or `uv` (uv script).
- Mosquitto is installed and configured with anonymous access on port 1883.
- GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
- If `pywebview` is not installed, Sensorius will continue headless.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

## Linux Setup (Debian/Ubuntu, Hub + MQTT Only)

Linux non-Pi hosts run Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported in this setup path.

Use the Linux setup script:

```bash
chmod +x setup_linux.sh
./setup_linux.sh
```

Notes:

- Uses `apt` to install precompiled system packages (`python3`, `mosquitto`, etc.).
- Installs Python dependencies from `setup_reqs_linux.txt`.
- Defaults to wheel-only Python installs (`PIP_ONLY_BINARY=1`) to avoid source builds.
- Set `INSTALL_PYWEBVIEW=0` to skip pywebview and force headless mode.
- Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

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

- Linux: `./uninstall_linux.sh`
- macOS: `./uninstall_mac.sh`
- Windows (PowerShell): `.\uninstall_win.ps1`

These scripts are interactive and attempt to remove the local venv and optional service/broker setup.
