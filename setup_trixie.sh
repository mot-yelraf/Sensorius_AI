#!/bin/bash
set -euo pipefail

# -------- user-tunable versions (top of file) --------
PY_VERSION="${PY_VERSION:-3.11.9}"         # change to 3.13.5 to match Trixie system Python
VENV_NAME="${VENV_NAME:-sensorius}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/saiSensorius}"

# Where pyenv will install Pythons
PYENV_ROOT="${HOME}/.pyenv"
VENV_PATH="${HOME}/.virtualenvs/${VENV_NAME}"   # we’ll create this with the selected pyenv Python

cd ~

echo "Updating APT and installing system dependencies..."
sudo apt update
sudo apt upgrade -y

# Base packages (unchanged from your script, plus Python build deps for pyenv)
sudo apt install -y \
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
  # ---- Python-from-source build deps for pyenv ----
  make libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev \
  xz-utils tk-dev libxml2-dev libxmlsec1-dev lzma

# -------- pyenv install / init --------
if [ ! -d "${PYENV_ROOT}" ]; then
  echo "Installing pyenv to ${PYENV_ROOT}..."
  git clone https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"
fi

# Add pyenv init to ~/.bashrc (idempotent)
if ! grep -q 'PYENV_ROOT' "${HOME}/.bashrc"; then
  {
    echo ''
    echo '# --- pyenv init (Sensorius) ---'
    echo 'export PYENV_ROOT="$HOME/.pyenv"'
    echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"'
    echo 'eval "$(pyenv init - bash)"'
  } >> "${HOME}/.bashrc"
fi

# Make pyenv available in this shell
export PYENV_ROOT
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init - bash)"

# Optional: build shared libpython to help native wheels like matplotlib
export PYTHON_CONFIGURE_OPTS="--enable-shared"

# Install the requested Python if missing
if ! pyenv versions --bare | grep -qx "${PY_VERSION}"; then
  echo "pyenv: installing Python ${PY_VERSION} (this can take a while on Pi)…"
  CFLAGS="-O3" pyenv install "${PY_VERSION}"
fi

# Use this Python for the rest of the script
pyenv shell "${PY_VERSION}"

# -------- virtualenv (with system site packages for GTK/GI) --------
echo "Creating virtual environment at ${VENV_PATH} using Python $(python -V)…"
python -m venv --system-site-packages "${VENV_PATH}"
source "${VENV_PATH}/bin/activate"
python -V
echo "Virtual environment activated."

# -------- user / groups (unchanged) --------
username=$(whoami)
sudo usermod -aG i2c,gpio,dialout "${username}"
echo "Added ${username} to groups: i2c,gpio,dialout (log out/in to take effect)"

# -------- pip / piwheels hint --------
python -m pip install --upgrade pip
# (Trixie usually has /etc/pip.conf -> piwheels mirror; if not, consider adding it for much faster ARM installs.)

# -------- Python deps from requirements --------
REQ_FILE="${PROJECT_DIR}/setup_reqs.txt"
if [[ -f "${REQ_FILE}" ]]; then
  echo "Installing Python packages from ${REQ_FILE} …"
  python -m pip install -r "${REQ_FILE}"
else
  echo "ERROR: setup_reqs.txt not found at ${REQ_FILE}"
  exit 1
fi

# -------- Mosquitto config (unchanged) --------
echo "Creating Mosquitto anonymous config at /etc/mosquitto/conf.d/anon.conf..."
sudo bash -c 'echo -e "listener 1883\nallow_anonymous true" > /etc/mosquitto/conf.d/anon.conf'
sudo systemctl restart mosquitto

# -------- Kernel/I2C config (unchanged) --------
echo "Ensure i2c-dev kernel module loads at boot"
if ! grep -q "^i2c-dev" /etc/modules; then
  echo "i2c-dev" | sudo tee -a /etc/modules
  echo "Added i2c-dev to /etc/modules"
fi

CONFIG_FILE="/boot/firmware/config.txt"
echo "Ensuring I2C overlays in ${CONFIG_FILE}…"
if ! grep -q "^dtoverlay=i2c0,pins_0_1" "$CONFIG_FILE"; then
  echo "dtoverlay=i2c0,pins_0_1" | sudo tee -a "$CONFIG_FILE"
  echo "Added I2C0 overlay"
else
  echo "I2C0 overlay already present"
fi
if ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE"; then
  echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_FILE"
  echo "Enabled i2c_arm"
else
  echo "i2c_arm already enabled"
fi

echo "Ensuring GPU memory allocation (for WebKit rendering)…"
if ! grep -q "^gpu_mem=" "$CONFIG_FILE"; then
  echo "gpu_mem=128" | sudo tee -a "$CONFIG_FILE"
else
  echo "gpu_mem already set"
fi

echo "Adding WEBKIT_DISABLE_COMPOSITING_MODE=1 to ~/.bashrc if needed…"
if ! grep -q "WEBKIT_DISABLE_COMPOSITING_MODE=1" "$HOME/.bashrc"; then
  echo 'export WEBKIT_DISABLE_COMPOSITING_MODE=1' >> "$HOME/.bashrc"
fi

# -------- Optional systemd service (updated to use pyenv venv) --------
echo ""
read -p "Would you like to install and enable sensorius.service? [y/N]: " setup_service
if [[ "$setup_service" =~ ^[Yy]$ ]]; then
  workdir="${PROJECT_DIR}"
  pyexec="${VENV_PATH}/bin/python"

  echo "Creating systemd service file..."
  sudo bash -c "cat > /etc/systemd/system/sensorius.service <<EOF
[Unit]
Description=Sensorius Python Startup Service
Wants=network-online.target
After=network.target

[Service]
ExecStart=${pyexec} ${workdir}/Sensorius.py
WorkingDirectory=${workdir}
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
  sudo systemctl start sensorius.service
  echo "Service installed and will automatically start at system startup"
else
  echo "Skipping service installation."
fi

echo ""
echo "Setup complete."
echo "To activate your Python environment now:  source ${VENV_PATH}/bin/activate"
echo "Some changes require a reboot (I2C enablement, GPU mem). Recommended: sudo reboot"
