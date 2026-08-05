#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/setup_common.sh"
start_install_log "$0" "$@"

deploy_project_files

cp -f "${SCRIPT_DIR}/setup_reqs_trixie.txt" "${PROJECT_DIR}/setup_reqs.txt"

export SCRIPT_DIR SOURCE_REPO_DIR PROJECT_DIR VENV_PATH
"${SCRIPT_DIR}/setup_trixie_legacy.sh" "$@"
configure_rpi_printer
