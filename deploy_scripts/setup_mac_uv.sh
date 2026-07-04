#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${SCRIPT_DIR}/setup_reqs_mac.txt}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/setup_common.sh"
start_install_log "$0" "$@"

deploy_project_files

export SCRIPT_DIR SOURCE_REPO_DIR PROJECT_DIR VENV_PATH REQ_FILE
exec "${SCRIPT_DIR}/setup_mac_uv_legacy.sh" "$@"
