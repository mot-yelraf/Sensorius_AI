#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/setup_reqs_linux.txt}"
INSTALL_PYWEBVIEW="${INSTALL_PYWEBVIEW:-1}"
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-1}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-60}"
START_TS="$(date +%s)"

CREATED_VENV=0

cleanup() {
  if [[ "${CREATED_VENV}" -eq 1 && -d "${VENV_PATH}" ]]; then
    echo "Cleaning up virtual environment at ${VENV_PATH}..."
    rm -rf "${VENV_PATH}"
  fi
}

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
    echo "ERROR: This script currently supports Debian/Ubuntu hosts with apt-get."
    echo "Use setup_mac.sh/setup_mac_uv.sh or setup_win.ps1 on other platforms."
    cleanup
    exit 1
  fi
}

install_system_packages() {
  run_with_heartbeat "APT update" sudo apt-get update

  local apt_pkgs=(
    ca-certificates
    curl
    python3
    python3-venv
    python3-pip
    mosquitto
    mosquitto-clients
  )

  if [[ "${INSTALL_PYWEBVIEW}" == "1" ]]; then
    apt_pkgs+=(
      python3-gi
      gir1.2-webkit2-4.1
      libgtk-3-0
      libwebkit2gtk-4.1-0
    )
  fi

  if [[ "${PIP_ONLY_BINARY}" != "1" ]]; then
    apt_pkgs+=(
      build-essential
      pkg-config
      libgirepository1.0-dev
      libgtk-3-dev
      libwebkit2gtk-4.1-dev
      python3-dev
    )
  fi

  run_with_heartbeat "APT install system packages" \
    sudo apt-get install -y --no-install-recommends "${apt_pkgs[@]}"
}

install_requirements() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    echo "ERROR: requirements file not found at ${REQ_FILE}"
    cleanup
    exit 1
  fi

  python3 -m venv "${VENV_PATH}"
  CREATED_VENV=1
  # shellcheck disable=SC1091
  source "${VENV_PATH}/bin/activate"

  run_with_heartbeat "pip upgrade" python -m pip install --upgrade pip

  if [[ "${INSTALL_PYWEBVIEW}" == "0" ]]; then
    echo "INSTALL_PYWEBVIEW=0 set - installing without pywebview."
    local tmp_reqs
    tmp_reqs="$(mktemp)"
    grep -v '^pywebview==' "${REQ_FILE}" > "${tmp_reqs}"
    if [[ "${PIP_ONLY_BINARY}" == "1" ]]; then
      echo "PIP_ONLY_BINARY=1 set - requiring wheel/binary packages only."
      run_with_heartbeat "pip install requirements (without pywebview, binary-only)" \
        python -m pip install --only-binary=:all: -r "${tmp_reqs}"
    else
      run_with_heartbeat "pip install requirements (without pywebview)" \
        python -m pip install -r "${tmp_reqs}"
    fi
    rm -f "${tmp_reqs}"
  else
    if [[ "${PIP_ONLY_BINARY}" == "1" ]]; then
      echo "PIP_ONLY_BINARY=1 set - requiring wheel/binary packages only."
      run_with_heartbeat "pip install requirements (binary-only)" \
        python -m pip install --only-binary=:all: -r "${REQ_FILE}"
    else
      run_with_heartbeat "pip install requirements" python -m pip install -r "${REQ_FILE}"
    fi
  fi
}

install_mosquitto_config() {
  local conf_tmp
  conf_tmp="$(mktemp)"
  cat > "${conf_tmp}" <<'EOF'
listener 1883
allow_anonymous true
EOF

  run_with_heartbeat "Configure mosquitto anonymous listener" \
    sudo install -m 0644 "${conf_tmp}" /etc/mosquitto/conf.d/anon.conf
  rm -f "${conf_tmp}"

  if command -v systemctl >/dev/null 2>&1; then
    run_with_heartbeat "Restart mosquitto service" \
      sudo systemctl restart mosquitto
  elif command -v service >/dev/null 2>&1; then
    run_with_heartbeat "Restart mosquitto service" \
      sudo service mosquitto restart
  else
    echo "WARNING: No service manager found. Restart mosquitto manually."
  fi
}

main() {
  echo "Linux MQTT-only setup (no directly connected GPIO/I2C sensors)."
  echo "Using precompiled system packages and binary Python wheels when possible."

  ensure_apt
  install_system_packages
  install_requirements
  install_mosquitto_config

  echo ""
  echo "Setup complete."
  echo "Activate your environment: source ${VENV_PATH}/bin/activate"
  echo "Start Sensorius: python ${PROJECT_DIR}/Sensorius.py"
  echo "Web UI: open http://127.0.0.1:8000 (or http://<host-ip>:8000 from another device)"
  local end_ts elapsed
  end_ts="$(date +%s)"
  elapsed="$((end_ts - START_TS))"
  echo "Sensorius setup took ${elapsed} seconds."
}

main "$@"
