#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
SERVICE_NAME="${SERVICE_NAME:-sensorius.service}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
MOSQ_CONF="/etc/mosquitto/conf.d/anon.conf"

ask_yes_no() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [y/N]: " answer
  [[ "${answer}" =~ ^[Yy]$ ]]
}

echo "Sensorius uninstall (Linux)."
echo "Project: ${PROJECT_DIR}"
echo "Venv: ${VENV_PATH}"
echo ""

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
    if ask_yes_no "Disable and remove ${SERVICE_NAME}?"; then
      sudo systemctl stop "${SERVICE_NAME}" || true
      sudo systemctl disable "${SERVICE_NAME}" || true
      sudo rm -f "${SERVICE_PATH}"
      sudo systemctl daemon-reload || true
      echo "Removed ${SERVICE_NAME}."
    fi
  fi
fi

if [[ -d "${VENV_PATH}" ]]; then
  if ask_yes_no "Remove virtual environment at ${VENV_PATH}?"; then
    rm -rf "${VENV_PATH}"
    echo "Removed ${VENV_PATH}."
  fi
fi

if [[ -f "${MOSQ_CONF}" ]]; then
  if ask_yes_no "Remove mosquitto anonymous config (${MOSQ_CONF}) and restart mosquitto?"; then
    sudo rm -f "${MOSQ_CONF}"
    if command -v systemctl >/dev/null 2>&1; then
      sudo systemctl restart mosquitto || true
    elif command -v service >/dev/null 2>&1; then
      sudo service mosquitto restart || true
    fi
    echo "Removed ${MOSQ_CONF}."
  fi
fi

if ask_yes_no "Uninstall mosquitto and mosquitto-clients packages via apt?"; then
  sudo apt-get remove -y mosquitto mosquitto-clients || true
fi

echo "Uninstall script completed."
