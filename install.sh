#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${ROOT_DIR}/deploy_scripts"

choose_manager() {
  if [[ "${SETUP_PY_MANAGER:-}" =~ ^(uv|pip)$ ]]; then
    echo "${SETUP_PY_MANAGER}"
    return
  fi

  read -r -p "Use Python manager 'uv' or 'pip'? [uv/pip] (default: uv): " ans
  # macOS ships Bash 3.2, which does not support ${var,,} lowercase expansion.
  ans="$(printf '%s' "${ans}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "${ans}" ]]; then
    ans="uv"
  fi
  if [[ "${ans}" != "uv" && "${ans}" != "pip" ]]; then
    echo "Invalid choice: ${ans}" >&2
    exit 1
  fi
  echo "${ans}"
}

is_rpi() {
  if [[ -f /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model; then
    return 0
  fi
  if [[ -f /etc/os-release ]] && grep -qiE 'raspbian|raspberry' /etc/os-release; then
    return 0
  fi
  return 1
}

linux_codename() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    echo "${VERSION_CODENAME:-}"
  fi
}

main() {
  if [[ ! -d "${DEPLOY_DIR}" ]]; then
    echo "Missing deploy_scripts directory at: ${DEPLOY_DIR}" >&2
    exit 1
  fi

  local manager target uname_s codename
  manager="$(choose_manager)"
  uname_s="$(uname -s)"

  case "${uname_s}" in
    Darwin)
      if [[ "${manager}" == "uv" ]]; then
        target="setup_mac_uv.sh"
      else
        target="setup_mac.sh"
      fi
      ;;
    Linux)
      if is_rpi; then
        codename="$(linux_codename)"
        if [[ "${codename}" == "trixie" ]]; then
          if [[ "${manager}" == "uv" ]]; then
            target="setup_trixie_uv.sh"
          else
            target="setup_trixie.sh"
          fi
        else
          if [[ "${manager}" == "uv" ]]; then
            target="setup_bookwork_uv.sh"
          else
            target="setup_bookworm.sh"
          fi
        fi
      else
        target="setup_linux.sh"
        if [[ "${manager}" == "uv" ]]; then
          echo "No separate Linux uv setup script exists; using setup_linux.sh."
        fi
      fi
      ;;
    CYGWIN*|MINGW*|MSYS*)
      echo "Windows uses PowerShell setup scripts in deploy_scripts/." >&2
      echo "Run .\\deploy_scripts\\setup_win.ps1 or .\\deploy_scripts\\setup_win_uv.ps1" >&2
      exit 1
      ;;
    *)
      echo "Unsupported OS for setup dispatcher: ${uname_s}" >&2
      echo "Run platform-specific setup scripts from deploy_scripts/." >&2
      exit 1
      ;;
  esac

  echo "Selected setup script: deploy_scripts/${target}"
  exec "${DEPLOY_DIR}/${target}" "$@"
}

main "$@"
