#!/bin/bash
set -e

install_pi_gui_autostart() {
  local username="$1"
  local project_dir="$2"
  local venv_python="$3"
  local user_group user_home autostart_dir desktop_file tmp_file

  user_group="$(id -gn "${username}" 2>/dev/null || printf '%s' "${username}")"
  user_home="$(getent passwd "${username}" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -z "${user_home}" ]]; then
    user_home="${HOME}"
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
Exec=${venv_python} ${project_dir}/saiGuiLauncher.py
Path=${project_dir}
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  sudo install -m 0644 -o "${username}" -g "${user_group}" "${tmp_file}" "${desktop_file}"
  rm -f "${tmp_file}"
  echo "Sensorius GUI will open at the next graphical desktop login."
}

configure_mosquitto_anon_only() {
  local conf_dir="/etc/mosquitto/conf.d"
  local backup_suffix
  backup_suffix="disabled-by-sensorius-$(date +%Y%m%d%H%M%S)"

  echo "Configuring Mosquitto with only /etc/mosquitto/conf.d/anon.conf active..."
  sudo install -d -m 0755 "${conf_dir}"
  shopt -s nullglob
  local conf_file
  for conf_file in "${conf_dir}"/*.conf; do
    if [[ "${conf_file}" != "${conf_dir}/anon.conf" ]]; then
      echo "Disabling existing Mosquitto drop-in ${conf_file}"
      sudo mv "${conf_file}" "${conf_file}.${backup_suffix}"
    fi
  done
  shopt -u nullglob

  sudo install -m 0644 /dev/null "${conf_dir}/anon.conf"
  printf 'listener 1883\nallow_anonymous true\n' | sudo tee "${conf_dir}/anon.conf" >/dev/null
  sudo systemctl restart mosquitto
}

cd ~

echo "Updating APT and installing system dependencies..."
sudo apt update
sudo apt upgrade -y
sudo apt install -y \
  python3 python3-pip python3-venv python3-dev \
  sqlite3 libatlas-base-dev \
  build-essential git chrony locate cmake swig liblgpio-dev \
  raspi-gpio logrotate mosquitto mosquitto-clients \
  libgirepository1.0-dev \
  libgtk-3-dev libwebkit2gtk-4.1-dev \
  python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  i2c-tools \
  libffi-dev libssl-dev \
  libjpeg-dev zlib1g-dev libopenjp2-7

echo "Preparing Python 3.11 virtual environment..."
# Create new venv with access to system packages (e.g., gi, GTK)
python3 -m venv --system-site-packages "$HOME/py311"
source "$HOME/py311/bin/activate"
echo "Virtual environment created and activated."

username=$(whoami)
sudo usermod -aG i2c,gpio,dialout ${username}
echo "Added ${username} to groups: i2c,gpio,dialout (log out/in to take effect)"

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing Python packages from requirements.txt..."
REQ_FILE="$HOME/saiSensorius/setup_reqs.txt"
if [[ -f "$REQ_FILE" ]]; then
  pip install -r "$REQ_FILE"
  python -c "import webview; import gi; gi.require_version('Gtk','3.0'); gi.require_version('WebKit2','4.1'); print('pywebview GTK import check passed')"
else
  echo "ERROR: setup_reqs.txt not found at $REQ_FILE"
  exit 1
fi

echo "Configuring Mosquitto anonymous listener..."
configure_mosquitto_anon_only

echo "Ensure i2c-dev kernel module loads at boot"
if ! grep -q "^i2c-dev" /etc/modules; then
  echo "i2c-dev" | sudo tee -a /etc/modules
  echo "Added i2c-dev to /etc/modules"
fi

echo "Ensuring I2C0 overlay is set in /boot/firmware/config.txt..."
CONFIG_FILE="/boot/firmware/config.txt"
if ! grep -q "^dtoverlay=i2c0,pins_0_1" "$CONFIG_FILE"; then
  echo "dtoverlay=i2c0,pins_0_1" | sudo tee -a "$CONFIG_FILE"
  echo "Added I2C0 overlay"
else
  echo "I2C0 overlay already present"
fi

echo "Ensure I2C1 (i2c_arm) is enabled as well (default primary bus)"
if ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE"; then
  echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_FILE"
  echo "Enabled i2c_arm"
else
  echo "i2c_arm already enabled"
fi


echo "Ensuring GPU memory allocation (for WebKit rendering)..."
if ! grep -q "^gpu_mem=" "$CONFIG_FILE"; then
  echo "gpu_mem=128" | sudo tee -a "$CONFIG_FILE"
  echo "Added gpu_mem=128"
else
  echo "gpu_mem already set"
fi

echo "Adding WEBKIT_DISABLE_COMPOSITING_MODE=1 to ~/.bashrc if needed..."
if ! grep -q "WEBKIT_DISABLE_COMPOSITING_MODE=1" "$HOME/.bashrc"; then
  echo 'export WEBKIT_DISABLE_COMPOSITING_MODE=1' >> "$HOME/.bashrc"
  echo "Added WEBKIT_DISABLE_COMPOSITING_MODE=1 to ~/.bashrc"
else
  echo "WEBKIT_DISABLE_COMPOSITING_MODE=1 already in ~/.bashrc"
fi

echo ""
read -p "Would you like to install and enable sensorius.service? [y/N]: " setup_service
if [[ "$setup_service" =~ ^[Yy]$ ]]; then
  username="${SUDO_USER:-$(whoami)}"
  user_group="$(id -gn "${username}" 2>/dev/null || printf '%s' "${username}")"
  workdir="/home/$username/saiSensorius"
  pyexec="/home/$username/py311/bin/python"

  echo "Ensuring project ownership for ${username}:${user_group}..."
  sudo chown -R "${username}:${user_group}" "${workdir}"

  echo "Creating systemd service file..."
  sudo bash -c "cat > /etc/systemd/system/sensorius.service <<EOF
[Unit]
Description=Sensorius Python Startup Service
Wants=network-online.target
After=network.target

[Service]
ExecStart=$pyexec $workdir/Sensorius.py
WorkingDirectory=$workdir
User=$username
Group=$user_group
Restart=always
RestartSec=3
Environment=WEBKIT_DISABLE_COMPOSITING_MODE=1
Environment=SENSORIUS_GUI=0

[Install]
WantedBy=multi-user.target
EOF"

  install_pi_gui_autostart "${username}" "${workdir}" "${pyexec}"

  echo "Enabling and starting sensorius.service..."
  sudo systemctl daemon-reexec
  sudo systemctl daemon-reload
  sudo systemctl enable sensorius.service
  sudo systemctl start sensorius.service
  echo "Service installed and will automatically start at system startup"
else
  echo "Skipping service installation."
fi

echo ""
echo "Setup complete. To activate your Python environment now:"
echo "  source ~/py311/bin/activate"
echo ""
echo "Some changes require a reboot (I2C enablement, GPU mem)."
echo "Recommended: sudo reboot"
