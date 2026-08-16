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
files, and autostart behavior. `deploy_sai.sh` syncs application files,
reconciles Python requirements in the existing runtime venv, and optionally
runs a per-host post-deploy command. It does not install or reconfigure system
packages, services, kernel modules, or boot settings.

Dependency reconciliation inspects the remote platform and uses the matching
requirements file from the source checkout. An optional inventory
`runtime_python` path takes precedence. Without one, Linux selects the Python
environment used by an active Sensorius process first, then the Python in the
configured `sensorius.service` `ExecStart`, and finally
`<deployment-target>/.venv/bin/python`. This supports manually started installs
whose virtual environment is outside the deployment directory as well as
systemd services with an external virtual environment. macOS uses the target
`.venv` unless the inventory supplies another path.

Raspberry Pi hosts use the direct sensor requirements
(`setup_reqs_trixie.txt` on Trixie and `setup_reqs.txt` on other supported Pi
releases). Non-Pi Linux and macOS use their MQTT-only requirements. Dry-run
reports the selected Python, missing or incompatible distributions, and failed
runtime imports without installing anything. Apply mode installs only when the
check fails, verifies the environment again, and runs the inventory
post-deploy command only after verification succeeds. The deploy script does
not infer restart policy: manually started systems remain manual, while a
service is restarted only when its inventory entry includes that command.
Raspberry Pi I2C device access is reported separately; use `install.sh` for
missing system-level I2C setup.

The canonical project version is stored once in `sensorius/__init__.py`. The
deploy script verifies that marker exists before syncing; the repository-root
`__init__.py` re-exports it for compatibility. The runtime UI and versioned
static-asset URLs both use the canonical package value.

The deploy script is intended for existing runtime directories such as
`/home/<user>/Sensorius` or `/Users/<user>/Sensorius`. It excludes installed
runtime state, including `sensorius_data.db*`, `system_settings/`,
`sensor_settings/`, `switch_settings/`, `automation_settings/`, and generated
runtime data under `cache/`. Runtime-generated `.lgd-*` named pipes are also
preserved. It explicitly allows factory templates under those settings trees
to update.

Inventory format:

```text
host_alias|target_path|post_deploy_command|runtime_python
```

The last two fields are optional. Keep the empty post-deploy field when only a
runtime Python override is needed:

```text
sensoria-hub-0|/home/twfarley/Sensorius||/home/twfarley/py311/bin/python
sensorius-hub-3|/home/twfarley/Sensorius|sudo systemctl restart sensorius.service|/home/twfarley/py313/bin/python
```

Usage:

```bash
deploy_scripts/deploy_sai.sh
```

```bash
deploy_scripts/deploy_sai.sh --apply
```

Use `--skip-deps` only when dependency management is intentionally handled by
another system:

```bash
deploy_scripts/deploy_sai.sh --apply --skip-deps
```

To update one configured host without touching the rest of the inventory:

```bash
deploy_scripts/deploy_sai.sh --apply --host sensorius-hub-1
```
