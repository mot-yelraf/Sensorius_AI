#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SOURCE_REPO_DIR="${SOURCE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

sensorius_install_user_home() {
  local account="${SUDO_USER:-}" resolved=""
  if [[ -n "${account}" && "${account}" != "root" ]]; then
    if command -v getent >/dev/null 2>&1; then
      resolved="$(getent passwd "${account}" 2>/dev/null | awk -F: 'NR == 1 {print $6}')"
    elif command -v dscl >/dev/null 2>&1; then
      resolved="$(dscl . -read "/Users/${account}" NFSHomeDirectory 2>/dev/null | awk 'NR == 1 {print $2}')"
    fi
  fi
  printf '%s\n' "${resolved:-$HOME}"
}

choose_sensorius_install_parent() {
  local initial_parent="$1" python_bin=""
  case "$(uname -s)" in
    Darwin)
      osascript - "${initial_parent}" <<'APPLESCRIPT'
on run argv
  set initialFolder to POSIX file (item 1 of argv)
  set chosenFolder to choose folder with prompt "Choose where Sensorius should be installed. A Sensorius folder will be created here." default location initialFolder
  return POSIX path of chosenFolder
end run
APPLESCRIPT
      ;;
    Linux)
      if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v zenity >/dev/null 2>&1; then
        zenity --file-selection --directory --title="Choose where Sensorius should be installed" --filename="${initial_parent}/"
        return
      fi
      if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v kdialog >/dev/null 2>&1; then
        kdialog --getexistingdirectory "${initial_parent}" --title "Choose where Sensorius should be installed"
        return
      fi
      if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        return 2
      fi
      if command -v python3 >/dev/null 2>&1; then
        python_bin="$(command -v python3)"
      elif command -v python >/dev/null 2>&1; then
        python_bin="$(command -v python)"
      else
        return 2
      fi
      "${python_bin}" - "${initial_parent}" <<'PYTHON'
import sys

try:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    selected = filedialog.askdirectory(
        title="Choose where Sensorius should be installed",
        initialdir=sys.argv[1],
        mustexist=True,
    )
    root.destroy()
except Exception:
    raise SystemExit(2)

if not selected:
    raise SystemExit(1)
print(selected)
PYTHON
      ;;
    *) return 2 ;;
  esac
}

remember_sensorius_install_location() {
  local state_dir="$1" state_file="$2" install_dir="$3" state_temp
  mkdir -p "${state_dir}"
  state_temp="${state_file}.tmp.$$"
  printf '%s\n' "${install_dir}" > "${state_temp}"
  mv "${state_temp}" "${state_file}"
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    chown -R "${SUDO_USER}" "${state_dir}" 2>/dev/null || true
  fi
}

resolve_sensorius_install_location() {
  if [[ "${SENSORIUS_INSTALL_LOCATION_RESOLVED:-0}" == "1" ]]; then
    return
  fi

  local install_home default_dir state_root state_dir state_file remembered_dir
  local initial_parent selected_parent selection_status=0
  install_home="$(sensorius_install_user_home)"
  default_dir="${install_home}/Sensorius"
  if [[ -n "${XDG_CONFIG_HOME:-}" && -z "${SUDO_USER:-}" ]]; then
    state_root="${XDG_CONFIG_HOME}"
  else
    state_root="${install_home}/.config"
  fi
  state_dir="${state_root}/sensorius"
  state_file="${state_dir}/install-location"
  remembered_dir=""
  if [[ -f "${state_file}" ]]; then
    IFS= read -r remembered_dir < "${state_file}" || true
  fi
  case "${remembered_dir}" in
    ""|/) remembered_dir="${default_dir}" ;;
  esac

  if [[ -n "${SENSORIUS_INSTALL_DIR:-}" ]]; then
    PROJECT_DIR="${SENSORIUS_INSTALL_DIR}"
  elif [[ -n "${PROJECT_DIR:-}" ]]; then
    PROJECT_DIR="${PROJECT_DIR}"
  else
    initial_parent="$(dirname -- "${remembered_dir}")"
    if [[ ! -d "${initial_parent}" ]]; then
      initial_parent="${install_home}"
    fi
    selected_parent="$(choose_sensorius_install_parent "${initial_parent}")" || selection_status=$?
    if [[ "${selection_status}" -eq 1 ]]; then
      echo "Sensorius installation was cancelled." >&2
      return 1
    elif [[ "${selection_status}" -eq 0 && -n "${selected_parent}" ]]; then
      PROJECT_DIR="${selected_parent%/}/Sensorius"
    elif [[ -t 0 ]]; then
      printf 'Install Sensorius under which directory? [%s] ' "${initial_parent}"
      IFS= read -r selected_parent
      selected_parent="${selected_parent:-$initial_parent}"
      PROJECT_DIR="${selected_parent%/}/Sensorius"
    else
      PROJECT_DIR="${remembered_dir}"
      echo "No graphical folder chooser is available; using ${PROJECT_DIR}"
    fi
  fi

  case "${PROJECT_DIR}" in
    ""|/|"${install_home}")
      echo "Install location must name a dedicated Sensorius directory." >&2
      return 1
      ;;
    /*) ;;
    *)
      echo "Install location must be an absolute path: ${PROJECT_DIR}" >&2
      return 1
      ;;
  esac
  mkdir -p "${PROJECT_DIR}"
  PROJECT_DIR="$(cd "${PROJECT_DIR}" && pwd -P)"
  remember_sensorius_install_location "${state_dir}" "${state_file}" "${PROJECT_DIR}"
  export PROJECT_DIR
  export SENSORIUS_INSTALL_LOCATION_RESOLVED=1
}

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

remove_legacy_python_layout() {
  local target_dir="$1"
  local legacy_file removed_count=0

  if [[ ! -f "${target_dir}/Sensorius.py" || ! -f "${target_dir}/sensorius/__init__.py" ]]; then
    echo "ERROR: refusing legacy Python cleanup because the replacement sensorius package is incomplete in ${target_dir}." >&2
    return 1
  fi

  while IFS= read -r -d '' legacy_file; do
    rm -f -- "${legacy_file}"
    removed_count=$((removed_count + 1))
  done < <(find "${target_dir}" -maxdepth 1 -type f \( -name 'sai*.py' -o -name 'sai*.pyc' \) -print0)

  if [[ -d "${target_dir}/__pycache__" ]]; then
    if [[ -w "${target_dir}/__pycache__" ]]; then
      find "${target_dir}/__pycache__" -maxdepth 1 -type f -name 'sai*.pyc' \
        -exec rm -f -- {} + 2>/dev/null || true
    fi
    if find "${target_dir}/__pycache__" -maxdepth 1 -type f -name 'sai*.pyc' \
        -print -quit | grep -q .; then
      echo "NOTICE: owner-protected legacy bytecode remains in ${target_dir}/__pycache__; it is safe to leave in place." >&2
    else
      rmdir "${target_dir}/__pycache__" 2>/dev/null || true
    fi
  fi

  if [[ -d "${target_dir}/sensor_modules" ]]; then
    rm -rf -- "${target_dir}/sensor_modules"
    removed_count=$((removed_count + 1))
  fi

  if [[ -d "${target_dir}/src/sensorius" ]]; then
    rm -rf -- "${target_dir}/src/sensorius"
    removed_count=$((removed_count + 1))
  fi
  rm -rf -- "${target_dir}/src/sensorius.egg-info"
  rm -rf -- "${target_dir}/src/__pycache__"
  rm -f -- "${target_dir}/src/.DS_Store"
  rmdir "${target_dir}/src" 2>/dev/null || true

  if [[ "${removed_count}" -gt 0 ]]; then
    echo "Removed legacy flat/transitional Python layout from ${target_dir}."
  else
    echo "No legacy Python layout found in ${target_dir}."
  fi
}

deploy_project_files() {
  if [[ "${SOURCE_REPO_DIR}" == "${PROJECT_DIR}" ]]; then
    echo "Source and target are the same (${PROJECT_DIR}); skipping file sync."
    remove_legacy_python_layout "${PROJECT_DIR}"
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
      --include 'automation_settings/' \
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
      --exclude 'automation_settings/***' \
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

  remove_legacy_python_layout "${PROJECT_DIR}"
  echo "Application files deployed to ${PROJECT_DIR}"
}

configure_rpi_printer() {
  local helper_path target_user setup_mode
  helper_path="${PROJECT_DIR}/scripts/setup_rpi_printer.sh"
  target_user="${SUDO_USER:-$(id -un)}"
  setup_mode="${SENSORIUS_PRINTER_SETUP:-prompt}"

  case "${setup_mode}" in
    0|false|no|off|skip)
      echo "Skipping Raspberry Pi printer setup (SENSORIUS_PRINTER_SETUP=${setup_mode})."
      return
      ;;
  esac

  if [[ ! -f "${helper_path}" ]]; then
    echo "WARNING: Raspberry Pi printer helper not found at ${helper_path}." >&2
    return
  fi

  echo ""
  echo "Checking for a local driverless printer for Sensorius reports..."
  if [[ "${setup_mode}" =~ ^(1|true|yes|on|auto)$ ]]; then
    if ! bash "${helper_path}" --user "${target_user}" --yes; then
      echo "WARNING: printer setup did not complete; Sensorius installation will continue." >&2
    fi
  else
    if ! bash "${helper_path}" --user "${target_user}"; then
      echo "WARNING: printer setup did not complete; Sensorius installation will continue." >&2
    fi
  fi
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

install_pi_gui_desktop_entry() {
  local username="$1"
  local project_dir="$2"
  local gui_exec="$3"
  local user_group user_home applications_dir desktop_file icons_dir icon_file tmp_file

  user_group="$(id -gn "${username}" 2>/dev/null || printf '%s' "${username}")"
  user_home="$(getent passwd "${username}" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -z "${user_home}" ]]; then
    user_home="${HOME}"
  fi

  applications_dir="${user_home}/.local/share/applications"
  desktop_file="${applications_dir}/ai.sensorius.Sensorius.desktop"
  icons_dir="${user_home}/.local/share/icons/hicolor/512x512/apps"
  icon_file="${icons_dir}/ai.sensorius.Sensorius.png"
  sudo -u "${username}" mkdir -p "${applications_dir}"
  sudo -u "${username}" mkdir -p "${icons_dir}"
  sudo install -m 0644 -o "${username}" -g "${user_group}" \
    "${project_dir}/ui_static/sensorius-icon.png" "${icon_file}"
  tmp_file="$(mktemp)"
  cat > "${tmp_file}" <<EOF
[Desktop Entry]
Type=Application
Name=Sensorius
Comment=Open the Sensorius local dashboard
Exec=${gui_exec}
Path=${project_dir}
Icon=${project_dir}/ui_static/sensorius-icon.png
Terminal=false
StartupNotify=true
StartupWMClass=ai.sensorius.Sensorius
EOF
  sudo install -m 0644 -o "${username}" -g "${user_group}" "${tmp_file}" "${desktop_file}"
  rm -f "${tmp_file}"
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

  gui_exec="env PYTHONPATH=${project_dir} WEBKIT_DISABLE_COMPOSITING_MODE=1 GDK_BACKEND=wayland,x11 SENSORIUS_GUI_Y=48 ${venv_path}/bin/python -m sensorius.saiGuiLauncher"
  install_pi_gui_desktop_entry "${username}" "${project_dir}" "${gui_exec}"
  labwc_dir="${user_home}/.config/labwc"
  labwc_file="${labwc_dir}/autostart"

  if [[ -d "${labwc_dir}" ]] || command -v labwc >/dev/null 2>&1; then
    echo "Installing Sensorius labwc autostart at ${labwc_file}..."
    sudo -u "${username}" mkdir -p "${labwc_dir}"

    tmp_file="$(mktemp)"
    if [[ -f "${labwc_file}" ]]; then
      grep -Ev 'saiGuiLauncher.py|sensorius\.saiGuiLauncher' "${labwc_file}" > "${tmp_file}" || true
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
Icon=${project_dir}/ui_static/sensorius-icon.png
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  sudo install -m 0644 -o "${username}" -g "${user_group}" "${tmp_file}" "${desktop_file}"
  rm -f "${tmp_file}"
  echo "Sensorius GUI will open at the next graphical desktop login."
}
