#!/bin/bash
set -e

cd ~

echo "Updating APT and installing system dependencies..."
sudo apt update
sudo apt upgrade -y


echo "Preparing Python 3.11 virtual environment..."
source "$HOME/py311/bin/activate"
echo "Virtual environment activated."


echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing Python packages from requirements.txt..."
REQ_FILE="$HOME/saiSensorius/setup_reqs.txt"
if [[ -f "$REQ_FILE" ]]; then
  pip install -r "$REQ_FILE"
else
  echo "ERROR: requirements.txt not found at $REQ_FILE"
  exit 1
fi
