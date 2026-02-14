#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SOURCE_REPO_DIR="${SOURCE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${SCRIPT_DIR}/setup_reqs.txt}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-60}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/setup_common.sh"

run_with_heartbeat() {
  local label="$1"
  shift

  echo "==> ${label}"
  (
    while true; do
      sleep "${HEARTBEAT_SECONDS}"
      printf "... still working on: %s\r\n" "${label}"
    done
  ) &
  local hb_pid=$!

  set +e
  "$@"
  local rc=$?
  set -e

  kill "${hb_pid}" >/dev/null 2>&1 || true
  wait "${hb_pid}" 2>/dev/null || true

  if [[ ${rc} -ne 0 ]]; then
    echo "ERROR: step failed: ${label} (exit ${rc})"
    exit "${rc}"
  fi

  echo "==> Done: ${label}"
}

ensure_apt() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: setup_uv.sh supports Debian/Raspberry Pi OS with apt-get."
    exit 1
  fi
}

install_system_packages() {
  run_with_heartbeat "APT update" sudo apt-get update
  run_with_heartbeat "APT upgrade" sudo apt-get upgrade -y

  run_with_heartbeat "APT install system dependencies" sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    sqlite3 libatlas-base-dev \
    build-essential git chrony locate cmake \
    raspi-gpio logrotate mosquitto mosquitto-clients \
    libgirepository1.0-dev \
    libgtk-3-dev libwebkit2gtk-4.1-dev \
    python3-gi gir1.2-webkit2-4.1 \
    i2c-tools \
    libffi-dev libssl-dev \
    libjpeg-dev zlib1g-dev libopenjp2-7 \
    ca-certificates curl
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  echo "uv not found. Installing uv..."
  run_with_heartbeat "Install uv" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  export PATH="${HOME}/.local/bin:${PATH}"

  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv install did not complete successfully."
    exit 1
  fi
}

setup_python_env() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    echo "ERROR: requirements file not found at ${REQ_FILE}"
    exit 1
  fi

  mkdir -p "$(dirname "${VENV_PATH}")"
  run_with_heartbeat "Create venv with uv" uv venv "${VENV_PATH}" --python "${PYTHON_BIN}"
  local venv_python="${VENV_PATH}/bin/python"

  run_with_heartbeat "Install Python dependencies with uv" \
    uv pip install -r "${REQ_FILE}" --python "${venv_python}"

  run_with_heartbeat "Verify Python runtime imports" \
    "${venv_python}" -c "import fastapi; import requests; import paho.mqtt.client as mqtt; import adafruit_scd30; import adafruit_scd4x; from zoneinfo import ZoneInfo; ZoneInfo('America/Denver'); print('Python dependency check passed')"
}

configure_system() {
  local username
  username="$(whoami)"
  sudo usermod -aG i2c,gpio,dialout "${username}" || true
  echo "Added ${username} to groups: i2c,gpio,dialout (log out/in to take effect)"

  echo "Creating Mosquitto anonymous config at /etc/mosquitto/conf.d/anon.conf..."
  sudo bash -c 'echo -e "listener 1883\nallow_anonymous true" > /etc/mosquitto/conf.d/anon.conf'
  sudo systemctl restart mosquitto || true

  echo "Ensure i2c-dev kernel module loads at boot"
  if ! grep -q "^i2c-dev" /etc/modules; then
    echo "i2c-dev" | sudo tee -a /etc/modules
  fi

  local config_file="/boot/firmware/config.txt"
  echo "Ensuring I2C overlays in ${config_file}..."
  if ! grep -q "^dtoverlay=i2c0,pins_0_1" "${config_file}"; then
    echo "dtoverlay=i2c0,pins_0_1" | sudo tee -a "${config_file}"
  fi
  if ! grep -q "^dtparam=i2c_arm=on" "${config_file}"; then
    echo "dtparam=i2c_arm=on" | sudo tee -a "${config_file}"
  fi
  if ! grep -q "^gpu_mem=" "${config_file}"; then
    echo "gpu_mem=128" | sudo tee -a "${config_file}"
  fi

  if ! grep -q "WEBKIT_DISABLE_COMPOSITING_MODE=1" "$HOME/.bashrc"; then
    echo 'export WEBKIT_DISABLE_COMPOSITING_MODE=1' >> "$HOME/.bashrc"
  fi
}

configure_boot_start() {
  read -r -p "Start Sensorius automatically at system boot (install sensorius.service)? [y/N]: " setup_service
  if [[ ! "${setup_service}" =~ ^[Yy]$ ]]; then
    echo "Skipping service installation."
    return
  fi

  local username
  username="$(whoami)"
  local pyexec="${VENV_PATH}/bin/python"

  echo "Creating systemd service file..."
  sudo bash -c "cat > /etc/systemd/system/sensorius.service <<EOF
[Unit]
Description=Sensorius Python Startup Service
Wants=network-online.target
After=network.target

[Service]
ExecStart=${pyexec} ${PROJECT_DIR}/Sensorius.py
WorkingDirectory=${PROJECT_DIR}
User=${username}
Group=${username}
Restart=always
RestartSec=3
Environment=WEBKIT_DISABLE_COMPOSITING_MODE=1
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
EOF"

  echo "Enabling and starting sensorius.service..."
  sudo systemctl daemon-reexec
  sudo systemctl daemon-reload
  sudo systemctl enable sensorius.service
  sudo systemctl restart sensorius.service
  echo "Service installed and will automatically start at system startup."
}

main() {
  deploy_project_files
  cd ~
  ensure_apt
  install_system_packages
  ensure_uv
  setup_python_env
  configure_system
  configure_boot_start

  echo ""
  echo "Setup complete."
  echo "Activate your environment now: source ${VENV_PATH}/bin/activate"
  echo "Some changes require reboot (I2C enablement / group changes). Recommended: sudo reboot"
}

main "$@"
