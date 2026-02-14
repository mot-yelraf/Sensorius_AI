#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/setup_common.sh"

deploy_project_files

TMP_SCRIPT="$(mktemp)"
trap 'rm -f "${TMP_SCRIPT}"' EXIT

sed \
  -e "s|REQ_FILE=\"\$HOME/saiSensorius/setup_reqs.txt\"|REQ_FILE=\"${SCRIPT_DIR}/setup_reqs.txt\"|" \
  -e "s|workdir=\"/home/\$username/saiSensorius\"|workdir=\"${PROJECT_DIR}\"|" \
  -e 's|pyexec="/home/\$username/py311/bin/python"|pyexec="\$HOME/py311/bin/python"|' \
  "${SCRIPT_DIR}/setup_legacy.sh" > "${TMP_SCRIPT}"

bash "${TMP_SCRIPT}" "$@"
