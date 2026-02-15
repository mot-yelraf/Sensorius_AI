#!/usr/bin/env bash
set -euo pipefail

PY_VERSION="${PY_VERSION:-3.13.5}"
PY_MM="${PY_VERSION%.*}"
BREW_PY_FORMULA="${BREW_PY_FORMULA:-python@${PY_MM}}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/setup_reqs_mac.txt}"
INSTALL_PYWEBVIEW="${INSTALL_PYWEBVIEW:-1}"
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-1}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-60}"
START_TS="$(date +%s)"

CREATED_VENV=0
PYTHON_BIN=""
MIN_MACOS_MAJOR=13
MIN_MACOS_MINOR=0

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
      printf "… still working on: %s\r\n" "${label}"
    done
  ) &
  local hb_pid=$!

  set +e
  "$@"
  local rc=$?
  set -e

  if [[ -n "${hb_pid}" ]]; then
    kill "${hb_pid}" >/dev/null 2>&1 || true
    wait "${hb_pid}" 2>/dev/null || true
  fi

  if [[ ${rc} -ne 0 ]]; then
    echo "ERROR: step failed: ${label} (exit ${rc})"
    exit "${rc}"
  fi

  echo "==> Done: ${label}"
}

version_ge() {
  # Compare dotted version strings: version_ge 13.1 13.0
  local a="$1" b="$2"
  [[ "$(printf '%s\n%s\n' "$b" "$a" | sort -V | head -n1)" == "$b" ]]
}

check_macos_compat() {
  if ! command -v sw_vers >/dev/null 2>&1; then
    return 0
  fi

  local mac_ver mac_major mac_minor
  mac_ver="$(sw_vers -productVersion)"
  mac_major="${mac_ver%%.*}"
  mac_minor="${mac_ver#*.}"
  mac_minor="${mac_minor%%.*}"

  local min_ver="${MIN_MACOS_MAJOR}.${MIN_MACOS_MINOR}"
  if ! version_ge "${mac_ver}" "${min_ver}"; then
    echo "ERROR: macOS ${mac_ver} detected. This installer supports macOS ${min_ver}+ on Intel."
    echo "Homebrew and uv are not supported on macOS 12 and may fail during builds."
    echo ""
    echo "If you want to attempt anyway, re-run with:"
    echo "  ALLOW_UNSUPPORTED_MACOS=1 ./setup_mac.sh"
    cleanup
    exit 1
  fi

  if [[ "${mac_major}" -lt 13 ]]; then
    if ! command -v realpath >/dev/null 2>&1; then
      echo "ERROR: macOS ${mac_ver} requires 'realpath' for uv."
      echo "Install coreutils (brew install coreutils) or use a supported macOS version."
      cleanup
      exit 1
    fi
  fi
}

ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi

  echo "Homebrew not found. It is required to install dependencies."
  read -p "Install Homebrew now? [y/N]: " install_brew
  if [[ ! "${install_brew}" =~ ^[Yy]$ ]]; then
    echo "Homebrew install declined. Aborting."
    cleanup
    exit 1
  fi

  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [[ -x "/opt/homebrew/bin/brew" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x "/usr/local/bin/brew" ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi

  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew install did not complete successfully. Aborting."
    cleanup
    exit 1
  fi
}

ensure_xcode_clt() {
  if xcode-select -p >/dev/null 2>&1; then
    return 0
  fi

  echo "Xcode Command Line Tools not found. They are required to build Python ${PY_VERSION}."
  read -p "Install Xcode Command Line Tools now? [y/N]: " install_clt
  if [[ ! "${install_clt}" =~ ^[Yy]$ ]]; then
    echo "Xcode CLT install declined. Aborting."
    cleanup
    exit 1
  fi

  xcode-select --install || true
  echo "Please complete the Xcode CLT installation and re-run this script."
  cleanup
  exit 1
}

install_python_with_brew() {
  run_with_heartbeat "Homebrew update" brew update
  run_with_heartbeat "Homebrew install ${BREW_PY_FORMULA}" brew install "${BREW_PY_FORMULA}"

  PYTHON_BIN="$(brew --prefix)/opt/${BREW_PY_FORMULA}/bin/python${PY_MM}"
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    if command -v "python${PY_MM}" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "python${PY_MM}")"
    elif command -v python3 >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v python3)"
    else
      echo "ERROR: Could not find a usable Python binary after installing ${BREW_PY_FORMULA}."
      cleanup
      exit 1
    fi
  fi

  run_with_heartbeat "Verify Python runtime (${PYTHON_BIN})" "${PYTHON_BIN}" --version
}

install_requirements() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    echo "ERROR: requirements file not found at ${REQ_FILE}"
    cleanup
    exit 1
  fi

  if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: Python runtime not initialized."
    cleanup
    exit 1
  fi

  "${PYTHON_BIN}" -m venv "${VENV_PATH}"
  CREATED_VENV=1
  # shellcheck disable=SC1091
  source "${VENV_PATH}/bin/activate"

  run_with_heartbeat "pip upgrade" python -m pip install --upgrade pip

  if [[ "${INSTALL_PYWEBVIEW}" == "0" ]]; then
    echo "INSTALL_PYWEBVIEW=0 set — installing without pywebview."
    tmp_reqs="$(mktemp)"
    grep -v '^pywebview==' "${REQ_FILE}" > "${tmp_reqs}"
    if [[ "${PIP_ONLY_BINARY}" == "1" ]]; then
      echo "PIP_ONLY_BINARY=1 set — requiring wheel/binary packages only."
      run_with_heartbeat "pip install requirements (without pywebview, binary-only)" \
        python -m pip install --only-binary=:all: -r "${tmp_reqs}"
    else
      run_with_heartbeat "pip install requirements (without pywebview)" \
        python -m pip install -r "${tmp_reqs}"
    fi
    rm -f "${tmp_reqs}"
  else
    if [[ "${PIP_ONLY_BINARY}" == "1" ]]; then
      echo "PIP_ONLY_BINARY=1 set — requiring wheel/binary packages only."
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

install_mosquitto() {
  run_with_heartbeat "Homebrew install mosquitto" brew install mosquitto

  local brew_prefix
  brew_prefix="$(brew --prefix)"
  MOSQ_ETC="${brew_prefix}/etc/mosquitto"
  MOSQ_CONF="${MOSQ_ETC}/mosquitto.conf"
  MOSQ_CONF_D="${MOSQ_ETC}/conf.d"
  local mosq_var_run="${brew_prefix}/var/run/mosquitto"
  local mosq_var_lib="${brew_prefix}/var/lib/mosquitto"
  local mosq_var_log="${brew_prefix}/var/log"

  mkdir -p "${mosq_var_run}" "${mosq_var_lib}" "${mosq_var_log}"
  touch "${mosq_var_log}/mosquitto.log"
  mkdir -p "${MOSQ_CONF_D}"

  if [[ ! -f "${MOSQ_CONF}" ]]; then
    cat > "${MOSQ_CONF}" <<EOF
pid_file ${mosq_var_run}/mosquitto.pid
persistence true
persistence_location ${mosq_var_lib}/
log_dest file ${mosq_var_log}/mosquitto.log
include_dir ${MOSQ_CONF_D}
EOF
  elif ! grep -q "^include_dir .*conf.d" "${MOSQ_CONF}"; then
    echo "include_dir ${MOSQ_CONF_D}" >> "${MOSQ_CONF}"
  fi

  cat > "${MOSQ_CONF_D}/anon.conf" <<'EOF'
listener 1883
allow_anonymous true
EOF

  brew services restart mosquitto || brew services start mosquitto
}

configure_boot_start() {
  read -r -p "Start Sensorius automatically at system boot? [y/N]: " setup_boot
  if [[ ! "${setup_boot}" =~ ^[Yy]$ ]]; then
    return
  fi

  local service_user service_group service_uid
  service_user="$(id -un)"
  service_group="$(id -gn)"
  service_uid="$(id -u)"
  local plist_dir="${HOME}/Library/LaunchAgents"
  local plist_path="${plist_dir}/com.sensorius.sensorius.plist"
  local launchd_domain="gui/${service_uid}"
  local launchd_label="${launchd_domain}/com.sensorius.sensorius"
  mkdir -p "${plist_dir}"
  echo "Installing user LaunchAgent at ${plist_path} (UserName=${service_user})..."

  cat > "${plist_path}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sensorius.sensorius</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_PATH}/bin/python</string>
    <string>${PROJECT_DIR}/Sensorius.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/sensorius.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/sensorius.err.log</string>
</dict>
</plist>
EOF

  # Ensure runtime files remain writable by the service user.
  chown -R "${service_user}:${service_group}" "${PROJECT_DIR}" 2>/dev/null || true
  chmod 644 "${plist_path}"
  launchctl bootout "${launchd_label}" >/dev/null 2>&1 || true
  launchctl bootstrap "${launchd_domain}" "${plist_path}"
  launchctl enable "${launchd_label}"
  launchctl kickstart -k "${launchd_label}"
}

main() {
  echo "Note: This setup may take a while depending on Homebrew and package downloads."
  if [[ "${ALLOW_UNSUPPORTED_MACOS:-0}" != "1" ]]; then
    check_macos_compat
  fi
  ensure_brew
  if [[ "${PIP_ONLY_BINARY}" != "1" ]]; then
    ensure_xcode_clt
  fi
  install_python_with_brew
  install_requirements
  verify_runtime_imports
  install_mosquitto
  configure_boot_start

  echo ""
  echo "Setup complete."
  echo "Activate your environment: source ${VENV_PATH}/bin/activate"
  echo "Start Sensorius: python ${PROJECT_DIR}/Sensorius.py"
  echo "Web UI: open http://127.0.0.1:8000 (or http://<host-ip>:8000 from another device)"
  end_ts="$(date +%s)"
  elapsed="$((end_ts - START_TS))"
  echo "Sensorius setup took ${elapsed} seconds."
}

main "$@"
