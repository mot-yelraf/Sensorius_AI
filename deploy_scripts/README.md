# deploy_scripts

This folder contains:

- Setup/install scripts (`setup*.sh`, `setup*.ps1`, `setup_reqs*.txt`)
- Uninstall scripts (`uninstall*.sh`, `uninstall*.ps1`)
- Host deploy sync script (`deploy_sai.sh`)

Requirements files in this folder include `astral` for sunrise/sunset automation scheduling.

## Setup Entry Points

- Root `../setup.sh`: interactive dispatcher (asks `uv` vs `pip`, detects OS, runs the right script in this folder)
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

## Examples

```bash
chmod +x setup.sh
sudo ./setup.sh
```

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy_scripts\setup_win.ps1
```

## Deploy Sync Script

- Script: `deploy_scripts/deploy_sai.sh`
- Inventory: `deploy_scripts/sai_hosts.txt` (local, untracked) or fallback `deploy_scripts/sai_hosts.def` (tracked template)

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
