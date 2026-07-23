#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage:
  deploy_scripts/deploy_sai.sh [--apply] [--dry-run] [--host HOST_ALIAS] [--hosts FILE] [--source DIR] [--rsync-bin PATH]

Options:
  --apply            Perform deploy (default is dry-run)
  --dry-run          Force dry-run mode
  --host HOST_ALIAS  Deploy only the matching inventory host_alias
  --hosts FILE       Inventory file (default: deploy_scripts/sai_hosts.txt, fallback: deploy_scripts/sai_hosts.def)
  --source DIR       Source directory to sync (default: repo root)
  --rsync-bin PATH   rsync binary path (default: /opt/homebrew/bin/rsync, fallback: rsync)
  -h, --help         Show this help text

Inventory format (pipe-delimited):
  host_alias|target_path|post_deploy_command

Examples:
  deploy_scripts/deploy_sai.sh
  deploy_scripts/deploy_sai.sh --apply
HELP
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=1
HOSTS_FILE="${REPO_ROOT}/deploy_scripts/sai_hosts.txt"
if [[ ! -f "${HOSTS_FILE}" ]]; then
  HOSTS_FILE="${REPO_ROOT}/deploy_scripts/sai_hosts.def"
fi
SOURCE_DIR="${REPO_ROOT}"
ONLY_HOST=""

if [[ -x "/opt/homebrew/bin/rsync" ]]; then
  RSYNC_BIN="/opt/homebrew/bin/rsync"
else
  RSYNC_BIN="$(command -v rsync)"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      DRY_RUN=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --host|--only)
      ONLY_HOST="$2"
      shift 2
      ;;
    --hosts)
      HOSTS_FILE="$2"
      shift 2
      ;;
    --source)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --rsync-bin)
      RSYNC_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

[[ -f "${HOSTS_FILE}" ]] || { echo "Hosts file not found: ${HOSTS_FILE}" >&2; exit 1; }
[[ -d "${SOURCE_DIR}" ]] || { echo "Source directory not found: ${SOURCE_DIR}" >&2; exit 1; }
[[ -f "${SOURCE_DIR}/Sensorius.py" && -f "${SOURCE_DIR}/sensorius/__init__.py" ]] || {
  echo "Source does not contain the root sensorius package and launcher: ${SOURCE_DIR}" >&2
  exit 1
}
[[ -x "${RSYNC_BIN}" ]] || { echo "rsync not executable: ${RSYNC_BIN}" >&2; exit 1; }

cleanup_remote_legacy_layout() {
  local host="$1"
  local target="$2"

  ssh "${host}" sh -s -- "${target}" <<'REMOTE_CLEANUP'
set -eu
target_dir=$1
if [ ! -f "${target_dir}/Sensorius.py" ] || [ ! -f "${target_dir}/sensorius/__init__.py" ]; then
  echo "Refusing legacy Python cleanup: replacement package is incomplete in ${target_dir}." >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  python3 - "${target_dir}" <<'PYTHON_MIGRATION'
from pathlib import Path
import sys

target = sys.argv[1]
old_launcher = f"{target}/saiGuiLauncher.py"
new_launcher = "-m sensorius.saiGuiLauncher"
old_env = "env WEBKIT_DISABLE_COMPOSITING_MODE=1"
new_env = f"env PYTHONPATH={target} WEBKIT_DISABLE_COMPOSITING_MODE=1"
paths = (
    Path.home() / ".config" / "labwc" / "autostart",
    Path.home() / ".config" / "autostart" / "sensorius-gui.desktop",
)
for path in paths:
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8")
    if old_launcher not in text:
        continue
    updated = text.replace(old_launcher, new_launcher).replace(old_env, new_env)
    path.write_text(updated, encoding="utf-8")
    print(f"Updated legacy GUI launcher command in {path}.")
PYTHON_MIGRATION
fi

find "${target_dir}" -maxdepth 1 -type f \( -name 'sai*.py' -o -name 'sai*.pyc' \) -exec rm -f -- {} +
if [ -d "${target_dir}/__pycache__" ]; then
  find "${target_dir}/__pycache__" -maxdepth 1 -type f -name 'sai*.pyc' -delete
  rmdir "${target_dir}/__pycache__" 2>/dev/null || true
fi
rm -rf -- "${target_dir}/sensor_modules"
rm -rf -- "${target_dir}/src/sensorius" "${target_dir}/src/sensorius.egg-info"
rm -rf -- "${target_dir}/src/__pycache__"
rm -f -- "${target_dir}/src/.DS_Store"
rmdir "${target_dir}/src" 2>/dev/null || true
REMOTE_CLEANUP
}

EXCLUDES=(
  ".git/"
  ".venv/"
  "node_modules/"
  ".env"
  "__pycache__/"
  "*.pyc"
  "*.pyo"
  ".pytest_cache/"
  ".mypy_cache/"
  ".ruff_cache/"
  ".DS_Store"
  "*.local/"
  "*.local/***"
  "deploy_scripts/"
  "docs/"
  "testApparatus/"
  "*.md"
  "sensor_data.db"
  "sensordata.db"
  "sensorius_data.db*"
  "database_archives/"
  "database_archives/***"
  "database_recovery/"
  "database_recovery/***"
  "system_settings/***"
  "sensor_settings/***"
  "switch_settings/***"
  "*.log"
)

# Keep runtime settings directories excluded, but explicitly include factory templates.
INCLUDES=(
  "utils/"
  "utils/***"
  "system_settings/"
  "system_settings/factory/"
  "system_settings/factory/***"
  "system_settings/factory_nodus/"
  "system_settings/factory_nodus/***"
  "sensor_settings/"
  "sensor_settings/factory/"
  "sensor_settings/factory/***"
  "sensor_settings/factory_nodus/"
  "sensor_settings/factory_nodus/***"
  "switch_settings/"
  "switch_settings/factory/"
  "switch_settings/factory/***"
  "switch_settings/factory_nodus/"
  "switch_settings/factory_nodus/***"
)

RSYNC_OPTS=(-az --delete --itemize-changes --human-readable)
if [[ "${DRY_RUN}" -eq 1 ]]; then
  RSYNC_OPTS+=(--dry-run)
  echo "Mode: DRY RUN"
else
  echo "Mode: APPLY"
fi
for i in "${INCLUDES[@]}"; do
  RSYNC_OPTS+=(--include "${i}")
done
for x in "${EXCLUDES[@]}"; do
  RSYNC_OPTS+=(--exclude "${x}")
done

FAILURES=0
LINE_NO=0
MATCHED_HOSTS=0
while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
  LINE_NO=$((LINE_NO + 1))
  line="$(printf '%s' "${raw_line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "${line}" || "${line}" == \#* ]] && continue

  IFS='|' read -r host target post_cmd <<< "${line}"
  host="$(printf '%s' "${host:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -n "${ONLY_HOST}" && "${host}" != "${ONLY_HOST}" ]]; then
    continue
  fi
  MATCHED_HOSTS=$((MATCHED_HOSTS + 1))
  target="$(printf '%s' "${target:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  post_cmd="$(printf '%s' "${post_cmd:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [[ -z "${host}" || -z "${target}" ]]; then
    echo "[line ${LINE_NO}] Invalid entry: ${raw_line}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  remote="${host}:${target%/}/"
  echo
  echo "Deploying -> ${remote}"

  if ! "${RSYNC_BIN}" "${RSYNC_OPTS[@]}" "${SOURCE_DIR%/}/" "${remote}"; then
    echo "Deploy failed for ${host}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  if [[ "${DRY_RUN}" -eq 0 ]]; then
    echo "Removing legacy Python layout -> ${host}:${target}"
    if ! cleanup_remote_legacy_layout "${host}" "${target}"; then
      echo "Legacy Python layout cleanup failed for ${host}" >&2
      FAILURES=$((FAILURES + 1))
      continue
    fi
  fi

  if [[ "${DRY_RUN}" -eq 0 && -n "${post_cmd}" ]]; then
    echo "Post-deploy -> ${host}: ${post_cmd}"
    if ! ssh "${host}" "${post_cmd}"; then
      echo "Post-deploy command failed for ${host}" >&2
      FAILURES=$((FAILURES + 1))
    fi
  fi
done < "${HOSTS_FILE}"

if [[ -n "${ONLY_HOST}" && "${MATCHED_HOSTS}" -eq 0 ]]; then
  echo
  echo "No inventory entry matched --host ${ONLY_HOST}" >&2
  exit 1
fi

if [[ "${FAILURES}" -gt 0 ]]; then
  echo
  echo "Completed with ${FAILURES} failure(s)." >&2
  exit 1
fi

echo
echo "Deploy completed successfully."
