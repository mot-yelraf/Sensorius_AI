#!/usr/bin/env bash
set -euo pipefail

PY_VERSION="${PY_VERSION:-3.13.5}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_PATH="${VENV_PATH:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/setup_reqs_mac.txt}"
INSTALL_PYWEBVIEW="${INSTALL_PYWEBVIEW:-1}"

CREATED_VENV=0

cleanup() {
  if [[ "${CREATED_VENV}" -eq 1 && -d "${VENV_PATH}" ]]; then
    echo "Cleaning up virtual environment at ${VENV_PATH}..."
    rm -rf "${VENV_PATH}"
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

install_python_with_pyenv() {
  brew update
  brew install pyenv openssl@3 readline sqlite3 xz zlib tcl-tk pkg-config

  export PYENV_ROOT="${HOME}/.pyenv"
  export PATH="${PYENV_ROOT}/bin:${PATH}"
  eval "$(pyenv init - bash)"

  OPENSSL_PREFIX="$(brew --prefix openssl@3)"
  READLINE_PREFIX="$(brew --prefix readline)"
  SQLITE_PREFIX="$(brew --prefix sqlite3)"
  XZ_PREFIX="$(brew --prefix xz)"
  ZLIB_PREFIX="$(brew --prefix zlib)"
  TCLTK_PREFIX="$(brew --prefix tcl-tk)"

  export LDFLAGS="-L${OPENSSL_PREFIX}/lib -L${READLINE_PREFIX}/lib -L${SQLITE_PREFIX}/lib -L${XZ_PREFIX}/lib -L${ZLIB_PREFIX}/lib -L${TCLTK_PREFIX}/lib"
  export CPPFLAGS="-I${OPENSSL_PREFIX}/include -I${READLINE_PREFIX}/include -I${SQLITE_PREFIX}/include -I${XZ_PREFIX}/include -I${ZLIB_PREFIX}/include -I${TCLTK_PREFIX}/include"
  export PKG_CONFIG_PATH="${OPENSSL_PREFIX}/lib/pkgconfig:${READLINE_PREFIX}/lib/pkgconfig:${SQLITE_PREFIX}/lib/pkgconfig:${XZ_PREFIX}/lib/pkgconfig:${ZLIB_PREFIX}/lib/pkgconfig:${TCLTK_PREFIX}/lib/pkgconfig"
  export PYTHON_CONFIGURE_OPTS="--enable-shared"

  if ! pyenv versions --bare | grep -qx "${PY_VERSION}"; then
    echo "pyenv: installing Python ${PY_VERSION} (this may take a while)…"
    pyenv install "${PY_VERSION}"
  fi

  pyenv shell "${PY_VERSION}"
}

install_requirements() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    echo "ERROR: requirements file not found at ${REQ_FILE}"
    cleanup
    exit 1
  fi

  python -m venv "${VENV_PATH}"
  CREATED_VENV=1
  # shellcheck disable=SC1091
  source "${VENV_PATH}/bin/activate"

  python -m pip install --upgrade pip

  if [[ "${INSTALL_PYWEBVIEW}" == "0" ]]; then
    echo "INSTALL_PYWEBVIEW=0 set — installing without pywebview."
    tmp_reqs="$(mktemp)"
    grep -v '^pywebview==' "${REQ_FILE}" > "${tmp_reqs}"
    python -m pip install -r "${tmp_reqs}"
    rm -f "${tmp_reqs}"
  else
    python -m pip install -r "${REQ_FILE}"
  fi
}

install_mosquitto() {
  brew install mosquitto

  MOSQ_ETC="$(brew --prefix)/etc/mosquitto"
  MOSQ_CONF="${MOSQ_ETC}/mosquitto.conf"
  MOSQ_CONF_D="${MOSQ_ETC}/conf.d"

  mkdir -p "${MOSQ_CONF_D}"

  if [[ -f "${MOSQ_CONF}" ]]; then
    if ! grep -q "^include_dir .*conf.d" "${MOSQ_CONF}"; then
      echo "include_dir ${MOSQ_CONF_D}" >> "${MOSQ_CONF}"
    fi
  fi

  cat > "${MOSQ_CONF_D}/anon.conf" <<'EOF'
listener 1883
allow_anonymous true
EOF

  brew services restart mosquitto || brew services start mosquitto
}

main() {
  ensure_brew
  ensure_xcode_clt
  install_python_with_pyenv
  install_requirements
  install_mosquitto

  echo ""
  echo "Setup complete."
  echo "Activate your environment: source ${VENV_PATH}/bin/activate"
  echo "Start Sensorius: python ${PROJECT_DIR}/Sensorius.py"
  echo "Web UI: open http://127.0.0.1:8000 (or http://<host-ip>:8000 from another device)"
}

main "$@"
