#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SOURCE_REPO_DIR="${SOURCE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"

_log_command_first_line() {
  local label="$1"
  shift
  if command -v "$1" >/dev/null 2>&1; then
    local output
    output="$("$@" 2>&1 | sed -n '1p' || true)"
    if [[ -n "${output}" ]]; then
      echo "${label}: ${output}"
    else
      echo "${label}: available"
    fi
  else
    echo "${label}: not found"
  fi
}

_log_optional_setting() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "${value}" ]]; then
    echo "${name}: ${value}"
  fi
}

_log_host_system_config() {
  local os_pretty cpu_model hardware_model mac_cpu mem_total mem_bytes mem_gib

  echo "--- Host system ---"
  echo "Hostname: $(hostname 2>/dev/null || printf 'unknown')"
  echo "User: $(id -un 2>/dev/null || whoami 2>/dev/null || printf 'unknown')"
  echo "Effective UID: $(id -u 2>/dev/null || printf 'unknown')"
  echo "Working directory: $(pwd 2>/dev/null || printf 'unknown')"
  echo "Platform: $(uname -srm 2>/dev/null || printf 'unknown')"
  echo "Kernel: $(uname -a 2>/dev/null || printf 'unknown')"

  if [[ -r /etc/os-release ]]; then
    os_pretty="$(awk -F= '$1 == "PRETTY_NAME" {gsub(/^"|"$/, "", $2); print $2}' /etc/os-release 2>/dev/null || true)"
    if [[ -n "${os_pretty}" ]]; then
      echo "OS: ${os_pretty}"
    fi
  elif command -v sw_vers >/dev/null 2>&1; then
    echo "OS: $(sw_vers -productName 2>/dev/null || true) $(sw_vers -productVersion 2>/dev/null || true) ($(sw_vers -buildVersion 2>/dev/null || true))"
  else
    echo "OS: unavailable"
  fi

  if [[ -r /proc/device-tree/model ]]; then
    hardware_model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
  elif [[ -r /sys/firmware/devicetree/base/model ]]; then
    hardware_model="$(tr -d '\0' < /sys/firmware/devicetree/base/model 2>/dev/null || true)"
  elif command -v sysctl >/dev/null 2>&1; then
    hardware_model="$(sysctl -n hw.model 2>/dev/null || true)"
    if [[ -z "${hardware_model}" ]]; then
      hardware_model="$(sysctl -n hw.machine 2>/dev/null || true)"
    fi
  fi
  echo "Hardware model: ${hardware_model:-unavailable}"

  if [[ -r /proc/cpuinfo ]]; then
    cpu_model="$(awk -F: '/model name|Hardware|Processor/ {gsub(/^[ \t]+/, "", $2); print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
  elif command -v sysctl >/dev/null 2>&1; then
    mac_cpu="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)"
    cpu_model="${mac_cpu}"
  fi
  echo "CPU: ${cpu_model:-unavailable}"

  if command -v getconf >/dev/null 2>&1; then
    echo "CPU cores online: $(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 'unknown')"
  fi

  if [[ -r /proc/meminfo ]]; then
    mem_total="$(awk '/^MemTotal:/ {printf "%.1f GiB (%s kB)", $2 / 1048576, $2}' /proc/meminfo 2>/dev/null || true)"
    [[ -n "${mem_total}" ]] && echo "Memory: ${mem_total}"
  elif command -v sysctl >/dev/null 2>&1; then
    mem_bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    if [[ -n "${mem_bytes}" ]]; then
      mem_gib="$(awk -v bytes="${mem_bytes}" 'BEGIN {printf "%.1f GiB", bytes / 1073741824}' 2>/dev/null || true)"
      echo "Memory: ${mem_gib} (${mem_bytes} bytes)"
    else
      echo "Memory: unavailable"
    fi
  else
    echo "Memory: unavailable"
  fi

  echo "Disk space:"
  df -h "${PROJECT_DIR}" "${HOME}" / 2>/dev/null | sed 's/^/  /' || echo "  unavailable"
}

_log_installer_context() {
  echo "--- Installer context ---"
  echo "Source repo: ${SOURCE_REPO_DIR}"
  echo "Project dir: ${PROJECT_DIR}"
  _log_optional_setting SCRIPT_DIR
  _log_optional_setting VENV_PATH
  _log_optional_setting REQ_FILE
  _log_optional_setting PY_VERSION
  _log_optional_setting PYTHON_BIN
  _log_optional_setting SETUP_PY_MANAGER
  _log_optional_setting BROKER_SCOPE
  _log_optional_setting INSTALL_PYWEBVIEW
  _log_optional_setting PIP_ONLY_BINARY
  _log_optional_setting SENSORIUS_PREFLIGHT_ONLY
  echo "Shell: ${SHELL:-unknown}"
  echo "Bash: ${BASH_VERSION:-unknown}"

  if command -v git >/dev/null 2>&1 && [[ -d "${SOURCE_REPO_DIR}/.git" ]]; then
    echo "Git branch: $(git -C "${SOURCE_REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'unknown')"
    echo "Git revision: $(git -C "${SOURCE_REPO_DIR}" rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
    if ! git -C "${SOURCE_REPO_DIR}" diff --quiet --ignore-submodules -- 2>/dev/null; then
      echo "Git worktree: modified"
    else
      echo "Git worktree: clean"
    fi
  fi

  echo "--- Tool versions ---"
  _log_command_first_line "python3" python3 --version
  _log_command_first_line "python" python --version
  _log_command_first_line "pip3" pip3 --version
  _log_command_first_line "uv" uv --version
  _log_command_first_line "git" git --version
  _log_command_first_line "rsync" rsync --version
  _log_command_first_line "apt-get" apt-get --version
  _log_command_first_line "brew" brew --version
  _log_command_first_line "systemctl" systemctl --version
  _log_command_first_line "mosquitto" mosquitto -h
}

start_install_log() {
  if [[ "${SENSORIUS_INSTALL_LOG_ACTIVE:-0}" == "1" ]]; then
    return
  fi

  local log_path log_dir start_ts fifo_dir fifo_path tee_pid
  log_path="${SENSORIUS_INSTALL_LOG:-${PROJECT_DIR}/install.log}"
  log_dir="$(dirname "${log_path}")"

  if ! mkdir -p "${log_dir}" 2>/dev/null || ! touch "${log_path}" 2>/dev/null; then
    log_path="${TMPDIR:-/tmp}/sensorius-install.log"
    log_dir="$(dirname "${log_path}")"
    if ! mkdir -p "${log_dir}" 2>/dev/null || ! touch "${log_path}" 2>/dev/null; then
      echo "WARNING: unable to create install log; continuing without transcript." >&2
      return
    fi
  fi

  if ! command -v tee >/dev/null 2>&1; then
    echo "WARNING: tee not found; continuing without install transcript." >&2
    return
  fi

  fifo_dir="$(mktemp -d "${TMPDIR:-/tmp}/sensorius-install-log.XXXXXX" 2>/dev/null || true)"
  if [[ -z "${fifo_dir}" ]]; then
    echo "WARNING: unable to create install log pipe; continuing without transcript." >&2
    return
  fi
  fifo_path="${fifo_dir}/output"
  if ! mkfifo "${fifo_path}" 2>/dev/null; then
    rm -rf "${fifo_dir}"
    echo "WARNING: unable to create install log pipe; continuing without transcript." >&2
    return
  fi

  tee -a "${log_path}" < "${fifo_path}" &
  tee_pid="$!"
  if ! exec > "${fifo_path}" 2>&1; then
    kill "${tee_pid}" >/dev/null 2>&1 || true
    rm -rf "${fifo_dir}"
    echo "WARNING: unable to start install transcript; continuing without it." >&2
    return
  fi
  export SENSORIUS_INSTALL_LOG="${log_path}"
  export SENSORIUS_INSTALL_LOG_ACTIVE=1
  export SENSORIUS_INSTALL_LOG_TEE_PID="${tee_pid}"
  rm -f "${fifo_path}"
  rmdir "${fifo_dir}" 2>/dev/null || true

  start_ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo ""
  echo "=== Sensorius install started ${start_ts} ==="
  echo "Logging install output to ${log_path}"
  if [[ $# -gt 0 ]]; then
    printf 'Command:'
    printf ' %q' "$@"
    printf '\n'
  fi
  _log_host_system_config
  _log_installer_context
}

deploy_project_files() {
  if [[ "${SOURCE_REPO_DIR}" == "${PROJECT_DIR}" ]]; then
    echo "Source and target are the same (${PROJECT_DIR}); skipping file sync."
    return
  fi

  mkdir -p "${PROJECT_DIR}"

  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --include 'system_settings/' \
      --include 'system_settings/factory/' \
      --include 'system_settings/factory/***' \
      --include 'system_settings/factory_nodus/' \
      --include 'system_settings/factory_nodus/***' \
      --include 'sensor_settings/' \
      --include 'sensor_settings/factory/' \
      --include 'sensor_settings/factory/***' \
      --include 'sensor_settings/factory_nodus/' \
      --include 'sensor_settings/factory_nodus/***' \
      --include 'switch_settings/' \
      --include 'switch_settings/factory/' \
      --include 'switch_settings/factory/***' \
      --include 'switch_settings/factory_nodus/' \
      --include 'switch_settings/factory_nodus/***' \
      --exclude '.git/' \
      --exclude '.env' \
      --exclude '.venv/' \
      --exclude 'node_modules/' \
      --exclude '__pycache__/' \
      --exclude '.pytest_cache/' \
      --exclude '.mypy_cache/' \
      --exclude '.ruff_cache/' \
      --exclude '*.pyc' \
      --exclude '*.pyo' \
      --exclude 'sensor_data.db' \
      --exclude 'sensordata.db' \
      --exclude 'sensorius_data.db*' \
      --exclude 'database_archives/' \
      --exclude 'database_archives/***' \
      --exclude 'database_recovery/' \
      --exclude 'database_recovery/***' \
      --exclude '*.local/' \
      --exclude '*.local/***' \
      --exclude 'system_settings/***' \
      --exclude 'sensor_settings/***' \
      --exclude 'switch_settings/***' \
      --exclude '*.log' \
      --exclude 'assets/screenshots/' \
      --exclude '*.md' \
      --exclude 'docs/' \
      --exclude 'testApparatus/' \
      --exclude 'deploy_scripts/' \
      "${SOURCE_REPO_DIR}/" "${PROJECT_DIR}/"
  else
    echo "rsync not found, using cp fallback."
    find "${PROJECT_DIR}" -mindepth 1 -maxdepth 1 ! -name 'install.log' -exec rm -rf {} + 2>/dev/null || true
    cp -a "${SOURCE_REPO_DIR}/." "${PROJECT_DIR}/"
    rm -rf "${PROJECT_DIR}/.git" "${PROJECT_DIR}/deploy_scripts"
    rm -rf "${PROJECT_DIR}/node_modules"
    rm -f "${PROJECT_DIR}/.env"
    rm -rf "${PROJECT_DIR}/assets/screenshots"
    rm -rf "${PROJECT_DIR}/database_archives" "${PROJECT_DIR}/database_recovery"
    rm -rf "${PROJECT_DIR}/docs" "${PROJECT_DIR}/testApparatus"
    rm -rf "${PROJECT_DIR}"/*.local
    rm -f "${PROJECT_DIR}/sensor_data.db" "${PROJECT_DIR}/sensordata.db" "${PROJECT_DIR}"/sensorius_data.db*
    find "${PROJECT_DIR}/system_settings" -mindepth 1 -maxdepth 1 ! -name 'factory' ! -name 'factory_nodus' ! -name '__init__.py' -exec rm -rf {} + 2>/dev/null || true
    find "${PROJECT_DIR}/sensor_settings" -mindepth 1 -maxdepth 1 ! -name 'factory' ! -name 'factory_nodus' ! -name '__init__.py' -exec rm -rf {} + 2>/dev/null || true
    find "${PROJECT_DIR}/switch_settings" -mindepth 1 -maxdepth 1 ! -name 'factory' ! -name 'factory_nodus' ! -name '__init__.py' -exec rm -rf {} + 2>/dev/null || true
    find "${PROJECT_DIR}" -type f -name '*.md' -delete
  fi

  echo "Application files deployed to ${PROJECT_DIR}"
}

install_networkmanager_polkit_rule() {
  local username="$1"
  local rules_dir="/etc/polkit-1/rules.d"
  local rule_path="${rules_dir}/50-sensorius-networkmanager.rules"
  local tmp_file js_user

  if [[ -z "${username}" ]]; then
    echo "WARNING: service user is empty; skipping NetworkManager polkit rule."
    return
  fi

  js_user="${username//\\/\\\\}"
  js_user="${js_user//\"/\\\"}"

  tmp_file="$(mktemp)"
  cat > "${tmp_file}" <<EOF
polkit.addRule(function(action, subject) {
  var allowed = [
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.wifi.scan",
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.NetworkManager.settings.modify.own",
    "org.freedesktop.NetworkManager.enable-disable-wifi"
  ];
  if (subject.user == "${js_user}" && allowed.indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
});
EOF

  sudo install -d -m 0755 "${rules_dir}"
  sudo install -m 0644 "${tmp_file}" "${rule_path}"
  rm -f "${tmp_file}"
  echo "Installed NetworkManager polkit rule at ${rule_path} for ${username}."

  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart polkit.service >/dev/null 2>&1 || true
  fi
}

install_pi_gui_autostart() {
  local username="$1"
  local project_dir="$2"
  local venv_path="$3"
  local user_group user_home labwc_dir labwc_file autostart_dir desktop_file tmp_file gui_exec

  user_group="$(id -gn "${username}" 2>/dev/null || printf '%s' "${username}")"
  user_home="$(getent passwd "${username}" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -z "${user_home}" ]]; then
    user_home="${HOME}"
  fi

  gui_exec="env WEBKIT_DISABLE_COMPOSITING_MODE=1 GDK_BACKEND=wayland,x11 SENSORIUS_GUI_Y=48 ${venv_path}/bin/python ${project_dir}/saiGuiLauncher.py"
  labwc_dir="${user_home}/.config/labwc"
  labwc_file="${labwc_dir}/autostart"

  if [[ -d "${labwc_dir}" ]] || command -v labwc >/dev/null 2>&1; then
    echo "Installing Sensorius labwc autostart at ${labwc_file}..."
    sudo -u "${username}" mkdir -p "${labwc_dir}"

    tmp_file="$(mktemp)"
    if [[ -f "${labwc_file}" ]]; then
      grep -v 'saiGuiLauncher.py' "${labwc_file}" > "${tmp_file}" || true
    fi
    {
      printf '\n# Sensorius GUI\n'
      printf '( sleep 8; %s ) >/tmp/sensorius-gui.log 2>&1 &\n' "${gui_exec}"
    } >> "${tmp_file}"

    sudo install -m 0755 -o "${username}" -g "${user_group}" "${tmp_file}" "${labwc_file}"
    rm -f "${tmp_file}"
    echo "Sensorius GUI will open at the next labwc desktop login."
    return
  fi

  autostart_dir="${user_home}/.config/autostart"
  desktop_file="${autostart_dir}/sensorius-gui.desktop"

  echo "Installing Sensorius desktop autostart at ${desktop_file}..."
  sudo -u "${username}" mkdir -p "${autostart_dir}"

  tmp_file="$(mktemp)"
  cat > "${tmp_file}" <<EOF
[Desktop Entry]
Type=Application
Name=Sensorius
Comment=Open the Sensorius local dashboard
Exec=${gui_exec}
Path=${project_dir}
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  sudo install -m 0644 -o "${username}" -g "${user_group}" "${tmp_file}" "${desktop_file}"
  rm -f "${tmp_file}"
  echo "Sensorius GUI will open at the next graphical desktop login."
}
