#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SOURCE_REPO_DIR="${SOURCE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/Sensorius}"

deploy_project_files() {
  if [[ "${SOURCE_REPO_DIR}" == "${PROJECT_DIR}" ]]; then
    echo "Source and target are the same (${PROJECT_DIR}); skipping file sync."
    return
  fi

  mkdir -p "${PROJECT_DIR}"

  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git/' \
      --exclude '.env' \
      --exclude '.venv/' \
      --exclude '__pycache__/' \
      --exclude '.pytest_cache/' \
      --exclude '.mypy_cache/' \
      --exclude '.ruff_cache/' \
      --exclude '*.pyc' \
      --exclude '*.pyo' \
      --exclude 'sensor_data.db' \
      --exclude '*.log' \
      --exclude '*.md' \
      --exclude 'docs/' \
      --exclude 'testApparatus/' \
      --exclude 'deploy_scripts/' \
      "${SOURCE_REPO_DIR}/" "${PROJECT_DIR}/"
  else
    echo "rsync not found, using cp fallback."
    rm -rf "${PROJECT_DIR:?}"/*
    cp -a "${SOURCE_REPO_DIR}/." "${PROJECT_DIR}/"
    rm -rf "${PROJECT_DIR}/.git" "${PROJECT_DIR}/deploy_scripts"
    rm -f "${PROJECT_DIR}/.env"
    rm -rf "${PROJECT_DIR}/docs" "${PROJECT_DIR}/testApparatus"
    find "${PROJECT_DIR}" -type f -name '*.md' -delete
  fi

  echo "Application files deployed to ${PROJECT_DIR}"
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
