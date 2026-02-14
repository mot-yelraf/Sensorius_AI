#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage:
  deploy_scripts/deploy_sai.sh [--apply] [--dry-run] [--hosts FILE] [--source DIR] [--rsync-bin PATH]

Options:
  --apply            Perform deploy (default is dry-run)
  --dry-run          Force dry-run mode
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
[[ -x "${RSYNC_BIN}" ]] || { echo "rsync not executable: ${RSYNC_BIN}" >&2; exit 1; }

EXCLUDES=(
  ".git/"
  ".venv/"
  ".env"
  "__pycache__/"
  "*.pyc"
  "*.pyo"
  ".pytest_cache/"
  ".mypy_cache/"
  ".ruff_cache/"
  ".DS_Store"
  "deploy_scripts/"
  "docs/"
  "testApparatus/"
  "*.md"
  "sensor_data.db"
  "sensorius_data.db*"
  "system_settings/"
  "sensor_settings/"
  "switch_settings/"
  "*.log"
)

RSYNC_OPTS=(-az --delete --itemize-changes --human-readable)
if [[ "${DRY_RUN}" -eq 1 ]]; then
  RSYNC_OPTS+=(--dry-run)
  echo "Mode: DRY RUN"
else
  echo "Mode: APPLY"
fi
for x in "${EXCLUDES[@]}"; do
  RSYNC_OPTS+=(--exclude "${x}")
done

FAILURES=0
LINE_NO=0
while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
  LINE_NO=$((LINE_NO + 1))
  line="$(printf '%s' "${raw_line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "${line}" || "${line}" == \#* ]] && continue

  IFS='|' read -r host target post_cmd <<< "${line}"
  host="$(printf '%s' "${host:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
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

  if [[ "${DRY_RUN}" -eq 0 && -n "${post_cmd}" ]]; then
    echo "Post-deploy -> ${host}: ${post_cmd}"
    if ! ssh "${host}" "${post_cmd}"; then
      echo "Post-deploy command failed for ${host}" >&2
      FAILURES=$((FAILURES + 1))
    fi
  fi
done < "${HOSTS_FILE}"

if [[ "${FAILURES}" -gt 0 ]]; then
  echo
  echo "Completed with ${FAILURES} failure(s)." >&2
  exit 1
fi

echo
echo "Deploy completed successfully."
