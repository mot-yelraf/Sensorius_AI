# deploy_scripts

This folder contains:

- Setup/install scripts (`setup*.sh`, `setup*.ps1`, `setup_reqs*.txt`)
- Uninstall scripts (`uninstall*.sh`, `uninstall*.ps1`)
- Host deploy sync script (`deploy_sai.sh`)

Requirements files in this folder include:

- `astral` for sunrise/sunset automation scheduling
- `skyfield` for biodynamic calendar ephemeris calculations
- `httpx`-based FarmOS integration support (no `farmOS.py` dependency)

## Setup Entry Points

- Root `../install.sh`: interactive dispatcher (asks `uv` vs `pip`, detects OS, runs the right script in this folder)
- `setup_bookworm.sh`: Raspberry Pi Bookworm + pip path
- `setup_bookwork_uv.sh`: Raspberry Pi Bookworm + uv path
- `setup_trixie.sh`: Raspberry Pi Trixie + pip path
- `setup_trixie_uv.sh`: Raspberry Pi Trixie + uv path
- `setup_linux.sh`: Linux (non-Pi) path
- `setup_mac.sh`: macOS + pip path
- `setup_mac_uv.sh`: macOS + uv path
- `setup_win.ps1`: Windows + pip path
- `setup_win_uv.ps1`: Windows + uv path

Shell and PowerShell setup scripts deploy app files from your cloned repo into:

- `~/Sensorius`

That path is used for runtime execution and service working directories.

## Mosquitto Scope

Linux and Windows setup scripts support `BROKER_SCOPE`:

- `system` (default): system service install/startup (`sudo`/Administrator required)
- `user`: user-owned mosquitto config/state and user startup (no elevated broker runtime)

Examples:

```bash
BROKER_SCOPE=user ./deploy_scripts/setup_linux.sh
```

```powershell
$env:BROKER_SCOPE = 'user'
.\deploy_scripts\setup_win_uv.ps1
```

macOS setup scripts now configure mosquitto in user scope by default (LaunchAgent + user-owned config/data paths).

## Examples

```bash
chmod +x install.sh
sudo ./install.sh
```

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win.ps1
```

## Deploy Sync Script

- Script: `deploy_scripts/deploy_sai.sh`
- Inventory: `deploy_scripts/sai_hosts.txt` (local, untracked) or fallback `deploy_scripts/sai_hosts.def` (tracked template)

Use this script for routine updates to systems that already have Sensorius
installed. It is different from `install.sh` and the platform setup scripts:
setup scripts prepare or repair the Python environment, Mosquitto, service
files, and autostart behavior, while `deploy_sai.sh` only syncs application
files and optionally runs a per-host post-deploy command.

The deploy script is intended for existing runtime directories such as
`/home/<user>/Sensorius` or `/Users/<user>/Sensorius`. It excludes installed
runtime state, including `sensorius_data.db*`, `system_settings/`,
`sensor_settings/`, and `switch_settings/`, and explicitly allows factory
templates under those settings trees to update.

Inventory format:

```text
host_alias|target_path|post_deploy_command
```

Usage:

```bash
deploy_scripts/deploy_sai.sh
```

```bash
deploy_scripts/deploy_sai.sh --apply
```
