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
      --exclude '.venv/' \
      --exclude '__pycache__/' \
      --exclude '.pytest_cache/' \
      --exclude '.mypy_cache/' \
      --exclude '.ruff_cache/' \
      --exclude '*.pyc' \
      --exclude '*.pyo' \
      --exclude 'sensor_data.db' \
      --exclude '*.log' \
      --exclude 'deploy_scripts/' \
      "${SOURCE_REPO_DIR}/" "${PROJECT_DIR}/"
  else
    echo "rsync not found, using cp fallback."
    rm -rf "${PROJECT_DIR:?}"/*
    cp -a "${SOURCE_REPO_DIR}/." "${PROJECT_DIR}/"
    rm -rf "${PROJECT_DIR}/.git" "${PROJECT_DIR}/deploy_scripts"
  fi

  echo "Application files deployed to ${PROJECT_DIR}"
}
