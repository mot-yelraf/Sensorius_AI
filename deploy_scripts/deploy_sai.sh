#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage:
  deploy_scripts/deploy_sai.sh [--apply] [--dry-run] [--skip-deps] [--host HOST_ALIAS] [--hosts FILE] [--source DIR] [--rsync-bin PATH]

Options:
  --apply            Perform deploy (default is dry-run)
  --dry-run          Force dry-run mode
  --skip-deps        Skip remote Python dependency checks and installation
  --host HOST_ALIAS  Deploy only the matching inventory host_alias
  --hosts FILE       Inventory file (default: deploy_scripts/sai_hosts.txt, fallback: deploy_scripts/sai_hosts.def)
  --source DIR       Source directory to sync (default: repo root)
  --rsync-bin PATH   rsync binary path (default: /opt/homebrew/bin/rsync, fallback: rsync)
  -h, --help         Show this help text

Inventory format (pipe-delimited):
  host_alias|target_path|post_deploy_command|runtime_python

Examples:
  deploy_scripts/deploy_sai.sh
  deploy_scripts/deploy_sai.sh --apply
HELP
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=1
HOSTS_FILE="${REPO_ROOT}/deploy_scripts/sai_hosts.txt"
if [[ ! -f "${HOSTS_FILE}" ]]; then
  HOSTS_FILE="${REPO_ROOT}/deploy_scripts/sai_hosts.def"
fi
SOURCE_DIR="${REPO_ROOT}"
ONLY_HOST=""
SKIP_DEPENDENCIES=0

if [[ -x "/opt/homebrew/bin/rsync" ]]; then
  RSYNC_BIN="/opt/homebrew/bin/rsync"
else
  RSYNC_BIN="$(command -v rsync)"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      DRY_RUN=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-deps)
      SKIP_DEPENDENCIES=1
      shift
      ;;
    --host|--only)
      ONLY_HOST="$2"
      shift 2
      ;;
    --hosts)
      HOSTS_FILE="$2"
      shift 2
      ;;
    --source)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --rsync-bin)
      RSYNC_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

[[ -f "${HOSTS_FILE}" ]] || { echo "Hosts file not found: ${HOSTS_FILE}" >&2; exit 1; }
[[ -d "${SOURCE_DIR}" ]] || { echo "Source directory not found: ${SOURCE_DIR}" >&2; exit 1; }
[[ -f "${SOURCE_DIR}/Sensorius.py" && -f "${SOURCE_DIR}/sensorius/__init__.py" ]] || {
  echo "Source does not contain the root sensorius package and launcher: ${SOURCE_DIR}" >&2
  exit 1
}
PACKAGE_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${SOURCE_DIR}/sensorius/__init__.py" | head -n 1)"
if [[ -z "${PACKAGE_VERSION}" ]]; then
  echo "Canonical version marker missing from ${SOURCE_DIR}/sensorius/__init__.py" >&2
  exit 1
fi
[[ -x "${RSYNC_BIN}" ]] || { echo "rsync not executable: ${RSYNC_BIN}" >&2; exit 1; }

cleanup_remote_legacy_layout() {
  local host="$1"
  local target="$2"

  ssh "${host}" sh -s -- "${target}" <<'REMOTE_CLEANUP'
set -eu
target_dir=$1
if [ ! -f "${target_dir}/Sensorius.py" ] || [ ! -f "${target_dir}/sensorius/__init__.py" ]; then
  echo "Refusing legacy Python cleanup: replacement package is incomplete in ${target_dir}." >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  python3 - "${target_dir}" <<'PYTHON_MIGRATION'
from pathlib import Path
import sys

target = sys.argv[1]
old_launcher = f"{target}/saiGuiLauncher.py"
new_launcher = "-m sensorius.saiGuiLauncher"
old_env = "env WEBKIT_DISABLE_COMPOSITING_MODE=1"
new_env = f"env PYTHONPATH={target} WEBKIT_DISABLE_COMPOSITING_MODE=1"
paths = (
    Path.home() / ".config" / "labwc" / "autostart",
    Path.home() / ".config" / "autostart" / "sensorius-gui.desktop",
)
for path in paths:
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8")
    if old_launcher not in text:
        continue
    updated = text.replace(old_launcher, new_launcher).replace(old_env, new_env)
    path.write_text(updated, encoding="utf-8")
    print(f"Updated legacy GUI launcher command in {path}.")
PYTHON_MIGRATION
fi

find "${target_dir}" -maxdepth 1 -type f \( -name 'sai*.py' -o -name 'sai*.pyc' \) -exec rm -f -- {} +
if [ -d "${target_dir}/__pycache__" ]; then
  if [ -w "${target_dir}/__pycache__" ]; then
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
rm -rf -- "${target_dir}/sensor_modules"
rm -rf -- "${target_dir}/platform_installers"
rm -rf -- "${target_dir}/src/sensorius" "${target_dir}/src/sensorius.egg-info"
rm -rf -- "${target_dir}/src/__pycache__"
rm -f -- "${target_dir}/src/.DS_Store"
rmdir "${target_dir}/src" 2>/dev/null || true

# Remove repository-only artifacts copied by older blacklist-based deploys.
# Runtime settings, databases, caches, custom themes, and local host state are
# deliberately absent from this narrow cleanup list.
rm -rf -- \
  "${target_dir}/.github" \
  "${target_dir}/assets" \
  "${target_dir}/docs" \
  "${target_dir}/playwright-report" \
  "${target_dir}/test-results" \
  "${target_dir}/testApparatus" \
  "${target_dir}/tests" \
  "${target_dir}/sensorius.egg-info" \
  "${target_dir}/utils"
rm -f -- \
  "${target_dir}/install.sh" \
  "${target_dir}/package-lock.json" \
  "${target_dir}/package.json" \
  "${target_dir}/playwright.config.js" \
  "${target_dir}/pyproject.toml" \
  "${target_dir}/pytest.ini" \
  "${target_dir}/scripts/build_user_guide_pdf.mjs" \
  "${target_dir}/scripts/pep257_audit.py"
REMOTE_CLEANUP
}

detect_remote_runtime() {
  local host="$1"
  local target="$2"
  local configured_python="${3:-}"

  ssh "${host}" sh -s -- "${target}" "${configured_python}" <<'REMOTE_RUNTIME'
set -eu
target_dir=$1
configured_python=${2-}
os_name=$(uname -s)
target_real=$(cd "${target_dir}" 2>/dev/null && pwd -P || printf '%s' "${target_dir}")
python_path=""
python_source=""

if [ -n "${configured_python}" ]; then
  if [ ! -x "${configured_python}" ]; then
    echo "Configured runtime Python is not executable: ${configured_python}" >&2
    exit 12
  fi
  python_path="${configured_python}"
  python_source="inventory"
fi

if [ -z "${python_path}" ] && [ "${os_name}" = "Linux" ] && [ -d /proc ]; then
  for proc_dir in /proc/[0-9]*; do
    [ -r "${proc_dir}/cmdline" ] || continue
    command_line=$(tr '\000' ' ' < "${proc_dir}/cmdline" 2>/dev/null || true)
    case "${command_line}" in
      *Sensorius.py*|*sensorius.saiGuiLauncher*|*sensorius.app*)
        ;;
      *)
        continue
        ;;
    esac
    process_cwd=$(readlink "${proc_dir}/cwd" 2>/dev/null || true)
    case "${command_line}" in
      *"${target_real}/"*)
        ;;
      *)
        [ "${process_cwd}" = "${target_real}" ] || continue
        ;;
    esac
    virtual_env=$(tr '\000' '\n' < "${proc_dir}/environ" 2>/dev/null \
      | sed -n 's/^VIRTUAL_ENV=//p' | head -n 1 || true)
    if [ -n "${virtual_env}" ] && [ -x "${virtual_env}/bin/python" ]; then
      python_path="${virtual_env}/bin/python"
      python_source="active-process"
      break
    fi
    process_exe=$(tr '\000' '\n' < "${proc_dir}/cmdline" 2>/dev/null \
      | head -n 1 || true)
    case "${process_exe}" in
      *python*)
        if [ -x "${process_exe}" ]; then
          python_path="${process_exe}"
          python_source="active-process-executable"
          break
        fi
        ;;
    esac
  done
fi

if [ -z "${python_path}" ] && command -v systemctl >/dev/null 2>&1; then
  service_exec=$(systemctl show sensorius.service -p ExecStart --value 2>/dev/null \
    | sed -n 's/.*path=\([^ ;}]*\).*/\1/p' | head -n 1 || true)
  if [ -n "${service_exec}" ] && [ -x "${service_exec}" ]; then
    python_path="${service_exec}"
    python_source="systemd-execstart"
  fi
fi

if [ -z "${python_path}" ] && [ -x "${target_dir}/.venv/bin/python" ]; then
  python_path="${target_dir}/.venv/bin/python"
  python_source="target-venv"
fi

if [ -z "${python_path}" ]; then
  echo "Could not resolve the Sensorius runtime Python for ${target_dir}." >&2
  echo "Set runtime_python in the inventory, start the manual runtime once, configure sensorius.service, or create ${target_dir}/.venv." >&2
  exit 12
fi

case "${os_name}" in
  Darwin)
    profile=mac
    ;;
  Linux)
    model=""
    is_pi=0
    if [ -r /proc/device-tree/model ]; then
      model=$(tr -d '\000' < /proc/device-tree/model 2>/dev/null || true)
    fi
    if printf '%s' "${model}" | grep -qi 'raspberry pi'; then
      is_pi=1
    elif [ -r /etc/os-release ] \
      && grep -qiE 'raspbian|raspberry' /etc/os-release; then
      is_pi=1
    fi
    if [ "${is_pi}" -eq 1 ]; then
      codename=""
      if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        codename=${VERSION_CODENAME:-}
      fi
      if [ "${codename}" = "trixie" ]; then
        profile=pi-trixie
      else
        profile=pi
      fi
    else
      profile=linux
    fi
    ;;
  *)
    echo "Unsupported remote platform for dependency reconciliation: ${os_name}" >&2
    exit 13
    ;;
esac

printf '%s|%s|%s\n' "${profile}" "${python_path}" "${python_source}"
REMOTE_RUNTIME
}

requirements_for_profile() {
  local profile="$1"
  case "${profile}" in
    pi-trixie)
      printf '%s\n' "${SOURCE_DIR}/deploy_scripts/setup_reqs_trixie.txt"
      ;;
    pi)
      printf '%s\n' "${SOURCE_DIR}/deploy_scripts/setup_reqs.txt"
      ;;
    linux)
      printf '%s\n' "${SOURCE_DIR}/deploy_scripts/setup_reqs_linux.txt"
      ;;
    mac)
      printf '%s\n' "${SOURCE_DIR}/deploy_scripts/setup_reqs_mac.txt"
      ;;
    *)
      return 1
      ;;
  esac
}

requirements_payload() {
  local requirements_file="$1"
  base64 < "${requirements_file}" | tr -d '\r\n'
}

check_remote_dependencies() {
  local host="$1"
  local target="$2"
  local python_path="$3"
  local profile="$4"
  local payload="$5"

  ssh "${host}" sh -s -- "${target}" "${python_path}" "${profile}" "${payload}" <<'REMOTE_CHECK'
set -eu
target_dir=$1
python_path=$2
profile=$3
payload=$4
cd "${target_dir}"
PYTHONPATH="${target_dir}" "${python_path}" - "${profile}" "${payload}" <<'PYTHON_CHECK'
import base64
import importlib
from importlib import metadata
import sys

profile = sys.argv[1]
requirements_text = base64.b64decode(sys.argv[2]).decode("utf-8")
problems = []

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:
    problems.append("packaging: missing dependency checker")
else:
    for raw_line in requirements_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            problems.append("invalid requirement: {}".format(line))
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            problems.append("{}: missing".format(requirement.name))
            continue
        if requirement.specifier and installed not in requirement.specifier:
            problems.append(
                "{}: installed {}, requires {}".format(
                    requirement.name,
                    installed,
                    requirement.specifier,
                )
            )

imports = ["fastapi", "requests", "paho.mqtt.client", "sensorius"]
if profile.startswith("pi"):
    imports.extend(
        [
            "board",
            "busio",
            "lgpio",
            "adafruit_extended_bus",
            "adafruit_sgp30",
            "adafruit_sgp40",
            "adafruit_sgp41.sgp41",
        ]
    )
for module_name in imports:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        problems.append(
            "{}: import failed ({})".format(module_name, type(exc).__name__)
        )

if problems:
    print("Dependencies need reconciliation:")
    for problem in problems:
        print("  - {}".format(problem))
    raise SystemExit(10)

print("Dependencies satisfy the {} runtime profile.".format(profile))
PYTHON_CHECK
REMOTE_CHECK
}

install_remote_dependencies() {
  local host="$1"
  local python_path="$2"
  local payload="$3"

  ssh "${host}" sh -s -- "${python_path}" "${payload}" <<'REMOTE_INSTALL'
set -eu
python_path=$1
payload=$2
requirements_file=$(mktemp "${TMPDIR:-/tmp}/sensorius-requirements.XXXXXX")
cleanup_requirements() {
  rm -f "${requirements_file}"
}
trap cleanup_requirements EXIT HUP INT TERM

"${python_path}" - "${payload}" "${requirements_file}" <<'PYTHON_DECODE'
import base64
from pathlib import Path
import sys

Path(sys.argv[2]).write_bytes(base64.b64decode(sys.argv[1]))
PYTHON_DECODE

uv_path=""
if command -v uv >/dev/null 2>&1; then
  uv_path=$(command -v uv)
elif [ -x "/opt/homebrew/bin/uv" ]; then
  uv_path="/opt/homebrew/bin/uv"
elif [ -x "/usr/local/bin/uv" ]; then
  uv_path="/usr/local/bin/uv"
elif [ -x "${HOME}/.local/bin/uv" ]; then
  uv_path="${HOME}/.local/bin/uv"
fi

if "${python_path}" -m pip --version >/dev/null 2>&1; then
  "${python_path}" -m pip install -r "${requirements_file}"
elif [ -n "${uv_path}" ]; then
  "${uv_path}" pip install -r "${requirements_file}" --python "${python_path}"
else
  echo "Neither pip nor uv is available for ${python_path}. Run install.sh to repair the environment." >&2
  exit 14
fi
REMOTE_INSTALL
}

check_remote_i2c_prerequisites() {
  local host="$1"

  ssh "${host}" sh -s <<'REMOTE_I2C'
set -eu
set -- /dev/i2c-*
if [ ! -e "$1" ]; then
  echo "NOTICE: no /dev/i2c-* device is available; direct sensors require the Pi I2C setup/repair path." >&2
  exit 0
fi

accessible=0
for device in "$@"; do
  if [ -r "${device}" ] && [ -w "${device}" ]; then
    accessible=1
    break
  fi
done
if [ "${accessible}" -ne 1 ]; then
  echo "NOTICE: the deployment user cannot read/write any /dev/i2c-* device; check i2c group membership." >&2
fi
REMOTE_I2C
}

reconcile_remote_dependencies() {
  local host="$1"
  local target="$2"
  local configured_python="${3:-}"
  local runtime_info profile python_path python_source requirements_file payload
  local check_output check_rc

  if [[ "${SKIP_DEPENDENCIES}" -eq 1 ]]; then
    echo "Dependency reconciliation skipped -> ${host}"
    return 0
  fi

  if ! runtime_info="$(detect_remote_runtime \
    "${host}" "${target}" "${configured_python}")"; then
    return 1
  fi
  IFS='|' read -r profile python_path python_source <<< "${runtime_info}"
  if ! requirements_file="$(requirements_for_profile "${profile}")"; then
    echo "No requirements mapping for remote profile ${profile}" >&2
    return 1
  fi
  if [[ ! -f "${requirements_file}" ]]; then
    echo "Requirements file not found: ${requirements_file}" >&2
    return 1
  fi
  payload="$(requirements_payload "${requirements_file}")"

  echo "Dependencies -> ${host}: profile=${profile} python=${python_path} source=${python_source}"
  set +e
  check_output="$(check_remote_dependencies \
    "${host}" "${target}" "${python_path}" "${profile}" "${payload}" 2>&1)"
  check_rc=$?
  set -e
  if [[ -n "${check_output}" ]]; then
    printf '%s\n' "${check_output}"
  fi

  if [[ "${check_rc}" -eq 10 && "${DRY_RUN}" -eq 1 ]]; then
    echo "DRY RUN: dependency installation would run for ${host}."
  elif [[ "${check_rc}" -eq 10 ]]; then
    echo "Installing missing or outdated dependencies -> ${host}"
    if ! install_remote_dependencies "${host}" "${python_path}" "${payload}"; then
      return 1
    fi
    if ! check_remote_dependencies \
      "${host}" "${target}" "${python_path}" "${profile}" "${payload}"; then
      echo "Dependency verification still fails after installation on ${host}." >&2
      return 1
    fi
  elif [[ "${check_rc}" -ne 0 ]]; then
    echo "Dependency check failed unexpectedly for ${host} (exit ${check_rc})." >&2
    return 1
  fi

  if [[ "${profile}" == pi* ]]; then
    check_remote_i2c_prerequisites "${host}"
  fi
}

# Keep deployment source selection explicit. The final exclude makes newly
# added repository tooling opt-in instead of silently copying it to every hub.
# Excluded destination paths are also protected from rsync --delete.
RSYNC_FILTERS=(
  --exclude ".git/"
  --exclude ".env"
  --exclude ".venv/"
  --exclude "node_modules/"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude "*.pyo"
  --exclude ".pytest_cache/"
  --exclude ".mypy_cache/"
  --exclude ".ruff_cache/"
  --exclude ".DS_Store"
  --exclude "._*"
  --exclude "*.md"
  --exclude ".lgd-*"
  --exclude "*.local/"
  --exclude "*.local/***"
  --exclude "sensor_data.db"
  --exclude "sensordata.db"
  --exclude "sensorius_data.db*"
  --exclude "database_archives/"
  --exclude "database_archives/***"
  --exclude "database_recovery/"
  --exclude "database_recovery/***"
  --exclude "cache/"
  --exclude "cache/***"
  --exclude "automation_settings/"
  --exclude "theme_settings/"
  --exclude "theme_assets/"
  --exclude "*.log"

  --include "/Sensorius.py"
  --include "/__init__.py"
  --include "/.env.def"
  --include "/LICENSE"

  --include "/sensorius/"
  --include "/sensorius/***"
  --include "/ui_static/"
  --include "/ui_static/***"
  --include "/ui_templates/"
  --include "/ui_templates/***"

  --include "/data/"
  --include "/data/skyfield/"
  --include "/data/skyfield/***"
  --exclude "/data/***"

  --include "/ota_packages/"
  --include "/ota_packages/***"

  --include "/system_settings/"
  --include "/system_settings/factory/"
  --include "/system_settings/factory/***"
  --include "/system_settings/factory_nodus/"
  --include "/system_settings/factory_nodus/***"
  --exclude "/system_settings/***"

  --include "/sensor_settings/"
  --include "/sensor_settings/factory/"
  --include "/sensor_settings/factory/***"
  --include "/sensor_settings/factory_nodus/"
  --include "/sensor_settings/factory_nodus/***"
  --exclude "/sensor_settings/***"

  --include "/switch_settings/"
  --include "/switch_settings/factory/"
  --include "/switch_settings/factory/***"
  --include "/switch_settings/factory_nodus/"
  --include "/switch_settings/factory_nodus/***"
  --exclude "/switch_settings/***"

  --include "/scripts/"
  --include "/scripts/setup_rpi_printer.sh"
  --exclude "/scripts/***"

  --exclude "*"
)

RSYNC_OPTS=(-az --delete --itemize-changes --human-readable)
if [[ "${DRY_RUN}" -eq 1 ]]; then
  RSYNC_OPTS+=(--dry-run)
  echo "Mode: DRY RUN"
else
  echo "Mode: APPLY"
fi
RSYNC_OPTS+=("${RSYNC_FILTERS[@]}")

FAILURES=0
LINE_NO=0
MATCHED_HOSTS=0
while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
  LINE_NO=$((LINE_NO + 1))
  line="$(printf '%s' "${raw_line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "${line}" || "${line}" == \#* ]] && continue

  IFS='|' read -r host target post_cmd runtime_python <<< "${line}"
  host="$(printf '%s' "${host:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -n "${ONLY_HOST}" && "${host}" != "${ONLY_HOST}" ]]; then
    continue
  fi
  MATCHED_HOSTS=$((MATCHED_HOSTS + 1))
  target="$(printf '%s' "${target:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  post_cmd="$(printf '%s' "${post_cmd:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  runtime_python="$(printf '%s' "${runtime_python:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [[ -z "${host}" || -z "${target}" ]]; then
    echo "[line ${LINE_NO}] Invalid entry: ${raw_line}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  remote="${host}:${target%/}/"
  echo
  echo "Deploying -> ${remote}"

  if ! "${RSYNC_BIN}" "${RSYNC_OPTS[@]}" "${SOURCE_DIR%/}/" "${remote}"; then
    echo "Deploy failed for ${host}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  if [[ "${DRY_RUN}" -eq 0 ]]; then
    echo "Removing legacy Python layout -> ${host}:${target}"
    if ! cleanup_remote_legacy_layout "${host}" "${target}"; then
      echo "Legacy Python layout cleanup failed for ${host}" >&2
      FAILURES=$((FAILURES + 1))
      continue
    fi
  fi

  if ! reconcile_remote_dependencies "${host}" "${target}" "${runtime_python}"; then
    echo "Dependency reconciliation failed for ${host}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  if [[ "${DRY_RUN}" -eq 0 && -n "${post_cmd}" ]]; then
    echo "Post-deploy -> ${host}: ${post_cmd}"
    if ! ssh -n "${host}" "${post_cmd}"; then
      echo "Post-deploy command failed for ${host}" >&2
      FAILURES=$((FAILURES + 1))
    fi
  fi
done < "${HOSTS_FILE}"

if [[ -n "${ONLY_HOST}" && "${MATCHED_HOSTS}" -eq 0 ]]; then
  echo
  echo "No inventory entry matched --host ${ONLY_HOST}" >&2
  exit 1
fi

if [[ "${FAILURES}" -gt 0 ]]; then
  echo
  echo "Completed with ${FAILURES} failure(s)." >&2
  exit 1
fi

echo
echo "Deploy completed successfully."
