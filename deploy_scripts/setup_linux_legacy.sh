#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/setup_reqs_linux.txt}"
INSTALL_PYWEBVIEW="${INSTALL_PYWEBVIEW:-1}"
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-1}"
BROKER_SCOPE="${BROKER_SCOPE:-}"
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

choose_broker_scope() {
  if [[ "${BROKER_SCOPE}" == "user" || "${BROKER_SCOPE}" == "system" ]]; then
    return
  fi
  local ans
  read -r -p "Mosquitto scope [system/user] (default: system): " ans
  ans="$(printf '%s' "${ans:-}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "${ans}" ]]; then
    ans="system"
  fi
  if [[ "${ans}" != "system" && "${ans}" != "user" ]]; then
    echo "Invalid Mosquitto scope '${ans}', defaulting to system."
    ans="system"
  fi
  BROKER_SCOPE="${ans}"
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

verify_runtime_imports() {
  run_with_heartbeat "Verify Python runtime imports" \
    "${VENV_PATH}/bin/python" -c "import fastapi; import requests; import paho.mqtt.client as mqtt; from zoneinfo import ZoneInfo; ZoneInfo('America/Denver'); print('Python dependency check passed')"
}

install_mosquitto_config() {
  if [[ "${BROKER_SCOPE}" == "user" ]]; then
    local mosq_bin user_root user_run user_lib user_log user_conf user_svc_dir user_svc_file
    mosq_bin="$(command -v mosquitto || true)"
    if [[ -z "${mosq_bin}" ]]; then
      echo "ERROR: mosquitto binary not found in PATH."
      cleanup
      exit 1
    fi

    user_root="${HOME}/.local/share/sensorius/mosquitto"
    user_run="${user_root}/run"
    user_lib="${user_root}/lib"
    user_log="${user_root}/log"
    user_conf="${HOME}/.config/sensorius/mosquitto/mosquitto.conf"
    user_svc_dir="${HOME}/.config/systemd/user"
    user_svc_file="${user_svc_dir}/sensorius-mosquitto.service"

    mkdir -p "${user_run}" "${user_lib}" "${user_log}" "$(dirname "${user_conf}")" "${user_svc_dir}"
    touch "${user_log}/mosquitto.log"

    cat > "${user_conf}" <<EOF
pid_file ${user_run}/mosquitto.pid
persistence true
persistence_location ${user_lib}/
log_dest file ${user_log}/mosquitto.log
listener 1883
allow_anonymous true
EOF

    if command -v systemctl >/dev/null 2>&1; then
      cat > "${user_svc_file}" <<EOF
[Unit]
Description=Sensorius Mosquitto (User)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${mosq_bin} -c ${user_conf}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
      run_with_heartbeat "Reload user systemd daemon" systemctl --user daemon-reload
      run_with_heartbeat "Enable user mosquitto service" systemctl --user enable sensorius-mosquitto.service
      run_with_heartbeat "Restart user mosquitto service" systemctl --user restart sensorius-mosquitto.service
    else
      echo "WARNING: systemctl --user not available; starting mosquitto in background for this session."
      nohup "${mosq_bin}" -c "${user_conf}" >/dev/null 2>&1 &
    fi

    # Avoid port conflicts if a system service is active.
    if command -v systemctl >/dev/null 2>&1; then
      sudo systemctl stop mosquitto >/dev/null 2>&1 || true
    fi
    return
  fi

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

configure_boot_start() {
  read -r -p "Start Sensorius automatically at system boot? [y/N]: " setup_boot
  if [[ ! "${setup_boot}" =~ ^[Yy]$ ]]; then
    return
  fi

  local service_path="/etc/systemd/system/sensorius.service"
  local run_user run_group
  run_user="${SUDO_USER:-$(whoami)}"
  run_group="$(id -gn "${run_user}" 2>/dev/null || printf '%s' "${run_user}")"

  # Ensure runtime files are writable by the non-root service account.
  run_with_heartbeat "Set project ownership for ${run_user}" \
    sudo chown -R "${run_user}:${run_group}" "${PROJECT_DIR}"

  echo "Installing systemd service at ${service_path}..."
  sudo tee "${service_path}" >/dev/null <<EOF
[Unit]
Description=Sensorius Python Startup Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_PATH}/bin/python ${PROJECT_DIR}/Sensorius.py
User=${run_user}
Group=${run_group}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  run_with_heartbeat "Enable sensorius.service" sudo systemctl enable sensorius.service
  run_with_heartbeat "Restart sensorius.service" sudo systemctl restart sensorius.service
}

main() {
  echo "Linux MQTT-only setup (no directly connected GPIO/I2C sensors)."
  echo "Using precompiled system packages and binary Python wheels when possible."

  ensure_apt
  choose_broker_scope
  install_system_packages
  install_requirements
  verify_runtime_imports
  install_mosquitto_config
  configure_boot_start

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
