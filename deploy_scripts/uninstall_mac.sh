#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-${HOME}/Sensorius}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
PLIST_PATH="/Library/LaunchDaemons/com.sensorius.sensorius.plist"

ask_yes_no() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [y/N]: " answer
  [[ "${answer}" =~ ^[Yy]$ ]]
}

echo "Sensorius uninstall (macOS)."
echo "Project: ${PROJECT_DIR}"
echo "Venv: ${VENV_PATH}"
echo ""

if [[ -f "${PLIST_PATH}" ]]; then
  if ask_yes_no "Unload and remove launchd service (${PLIST_PATH})?"; then
    sudo launchctl bootout system "${PLIST_PATH}" >/dev/null 2>&1 || true
    sudo rm -f "${PLIST_PATH}"
    echo "Removed launchd service."
  fi
fi

if [[ -d "${VENV_PATH}" ]]; then
  if ask_yes_no "Remove virtual environment at ${VENV_PATH}?"; then
    rm -rf "${VENV_PATH}"
    echo "Removed ${VENV_PATH}."
  fi
fi

if command -v brew >/dev/null 2>&1; then
  if ask_yes_no "Stop mosquitto brew service and remove anon config?"; then
    brew services stop mosquitto || true
    MOSQ_CONF_D="$(brew --prefix)/etc/mosquitto/conf.d"
    rm -f "${MOSQ_CONF_D}/anon.conf" || true
    echo "Mosquitto service stopped and anon config removed."
  fi

  if ask_yes_no "Uninstall mosquitto formula from Homebrew?"; then
    brew uninstall mosquitto || true
  fi
fi

echo "Uninstall script completed."
