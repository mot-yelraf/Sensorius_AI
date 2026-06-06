#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage:
  deploy_scripts/clean_sensorius_install.sh [--apply] [--dry-run] [--hosts FILE] [--host HOST]...
                                        [--ssh-bin PATH] [--mqtt-host HOST] [--mqtt-port PORT]
                                        [--mqtt-user USER] [--mqtt-pass PASS]

Options:
  --apply            Perform cleanup (default: dry-run)
  --dry-run          Show actions without changing target host
  --hosts FILE       Inventory file (default: deploy_scripts/sai_hosts.txt, fallback: deploy_scripts/sai_hosts.def)
  --host HOST        Only run for this host alias (repeatable)
  --ssh-bin PATH     ssh binary path (default: ssh)
  --mqtt-host HOST   Broker host as seen from target host (default: 127.0.0.1)
  --mqtt-port PORT   Broker port (default: 1883)
  --mqtt-user USER   Optional MQTT username
  --mqtt-pass PASS   Optional MQTT password
  -h, --help         Show this help text

Inventory format (pipe-delimited):
  host_alias|target_path|post_clean_command

Notes:
  - This script runs cleanup remotely over SSH.
  - For each host, it performs:
      1) Remove sensorius_data.* files
      2) Remove non-factory entries under system_settings/, sensor_settings/, switch_settings/
      3) Purge retained MQTT topics associated with Nodus/device IDs
  - post_clean_command is only executed in --apply mode.

Examples:
  deploy_scripts/clean_sensorius_install.sh
  deploy_scripts/clean_sensorius_install.sh --apply
  deploy_scripts/clean_sensorius_install.sh --apply --host pi-lab-a
HELP
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=1
HOSTS_FILE="${SCRIPT_DIR}/sai_hosts.txt"
if [[ ! -f "${HOSTS_FILE}" ]]; then
  HOSTS_FILE="${SCRIPT_DIR}/sai_hosts.def"
fi

SSH_BIN="ssh"
MQTT_HOST="127.0.0.1"
MQTT_PORT="1883"
MQTT_USER=""
MQTT_PASS=""

SELECTED_HOSTS=()

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
    --hosts)
      HOSTS_FILE="$2"
      shift 2
      ;;
    --host)
      SELECTED_HOSTS+=("$2")
      shift 2
      ;;
    --ssh-bin)
      SSH_BIN="$2"
      shift 2
      ;;
    --mqtt-host)
      MQTT_HOST="$2"
      shift 2
      ;;
    --mqtt-port)
      MQTT_PORT="$2"
      shift 2
      ;;
    --mqtt-user)
      MQTT_USER="$2"
      shift 2
      ;;
    --mqtt-pass)
      MQTT_PASS="$2"
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
if ! command -v "${SSH_BIN}" >/dev/null 2>&1; then
  echo "ssh not executable: ${SSH_BIN}" >&2
  exit 1
fi

host_selected() {
  local host="$1"
  if [[ ${#SELECTED_HOSTS[@]} -eq 0 ]]; then
    return 0
  fi
  local wanted
  for wanted in "${SELECTED_HOSTS[@]}"; do
    if [[ "${host}" == "${wanted}" ]]; then
      return 0
    fi
    if is_local_host "${host}" && is_local_host "${wanted}"; then
      return 0
    fi
  done
  return 1
}

is_local_host() {
  local value
  value="$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
  [[ -n "${value}" ]] || return 1
  case "${value}" in
    localhost|localhost.local|127.0.0.1|::1)
      return 0
      ;;
  esac

  local full short
  full="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)"
  short="${full%%.*}"
  if [[ -n "${full}" && "${value}" == "${full}" ]]; then
    return 0
  fi
  if [[ -n "${short}" && ( "${value}" == "${short}" || "${value}" == "${short}.local" ) ]]; then
    return 0
  fi
  return 1
}

print_available_hosts() {
  local line host _target _post_cmd
  echo "Available host aliases in ${HOSTS_FILE}:" >&2
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="$(printf '%s' "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    IFS='|' read -r host _target _post_cmd <<< "${line}"
    host="$(printf '%s' "${host:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -n "${host}" ]] && echo "  ${host}" >&2
  done < "${HOSTS_FILE}"
}

run_remote_cleanup() {
  local host="$1"
  local target="$2"
  local post_cmd="$3"

  echo
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[DRY-RUN] Cleaning -> ${host}:${target}"
  else
    echo "[APPLY] Cleaning -> ${host}:${target}"
  fi

  local -a runner
  runner=("${SSH_BIN}" "${host}" bash -s -- "${target}" "${DRY_RUN}" "${MQTT_HOST}" "${MQTT_PORT}" "${MQTT_USER}" "${MQTT_PASS}")
  if is_local_host "${host}"; then
    runner=(bash -s -- "${target}" "${DRY_RUN}" "${MQTT_HOST}" "${MQTT_PORT}" "${MQTT_USER}" "${MQTT_PASS}")
  fi

  if ! "${runner[@]}" <<'REMOTE_CLEAN'
set -euo pipefail

TARGET_DIR="$1"
DRY_RUN="$2"
MQTT_HOST="$3"
MQTT_PORT="$4"
MQTT_USER="${5-}"
MQTT_PASS="${6-}"

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

is_factory_name() {
  local name="$1"
  [[ "$name" == "factory" || "$name" == "factory_nodus" ]]
}

trim_dot_toml() {
  local name="$1"
  if [[ "$name" == *.toml ]]; then
    printf '%s\n' "${name%.toml}"
  else
    printf '%s\n' "$name"
  fi
}

is_prunable_settings_entry() {
  local entry="$1"
  local bn="$2"
  if [[ -d "$entry" ]]; then
    is_factory_name "$bn" && return 1
    return 0
  fi
  if [[ -f "$entry" && "$bn" == *.toml ]]; then
    return 0
  fi
  return 1
}

safe_remove_path() {
  local p="$1"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "  would remove: ${p}"
  else
    rm -rf -- "$p"
    log "  removed: ${p}"
  fi
}

purge_retained_topic() {
  local topic="$1"
  local pub_cmd=(mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT")
  if [[ -n "$MQTT_USER" ]]; then
    pub_cmd+=( -u "$MQTT_USER" )
  fi
  if [[ -n "$MQTT_PASS" ]]; then
    pub_cmd+=( -P "$MQTT_PASS" )
  fi
  pub_cmd+=( -t "$topic" -n -r )

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "  would purge retained topic: ${topic}"
  else
    "${pub_cmd[@]}" >/dev/null 2>&1 || true
    log "  purged retained topic: ${topic}"
  fi
}

[[ -n "$TARGET_DIR" ]] || die "Target path is empty"
[[ "$TARGET_DIR" != "/" ]] || die "Refusing to operate on /"
[[ -d "$TARGET_DIR" ]] || die "Target directory not found: $TARGET_DIR"
[[ -f "$TARGET_DIR/Sensorius.py" ]] || die "Target does not look like a Sensorius install: $TARGET_DIR"

cd "$TARGET_DIR"

log "Target: $TARGET_DIR"

# Collect candidate device IDs from settings trees before deletion.
device_ids=()
for root in system_settings sensor_settings switch_settings; do
  [[ -d "$root" ]] || continue
  while IFS= read -r -d '' entry; do
    bn="$(basename "$entry")"
    if ! is_prunable_settings_entry "$entry" "$bn"; then
      continue
    fi
    id="$(trim_dot_toml "$bn")"
    [[ -n "$id" ]] && device_ids+=("$id")
  done < <(find "$root" -mindepth 1 -maxdepth 1 -print0)
done

# 1) remove sensorius_data.* files (root + data/)
log "Step 1: remove sensorius_data.*"
db_candidates=()
if [[ -d "$TARGET_DIR" ]]; then
  while IFS= read -r -d '' f; do db_candidates+=("$f"); done \
    < <(find "$TARGET_DIR" -maxdepth 1 -type f -name 'sensorius_data.*' -print0)
fi
if [[ -d "$TARGET_DIR/data" ]]; then
  while IFS= read -r -d '' f; do db_candidates+=("$f"); done \
    < <(find "$TARGET_DIR/data" -maxdepth 1 -type f -name 'sensorius_data.*' -print0)
fi
if [[ ${#db_candidates[@]} -eq 0 ]]; then
  log "  no sensorius_data.* files found"
else
  for f in "${db_candidates[@]}"; do
    safe_remove_path "$f"
  done
fi

# 2) remove non-factory entries from settings directories
log "Step 2: prune non-factory settings entries"
for root in system_settings sensor_settings switch_settings; do
  if [[ ! -d "$root" ]]; then
    log "  missing: $root (skipped)"
    continue
  fi
  while IFS= read -r -d '' entry; do
    bn="$(basename "$entry")"
    if ! is_prunable_settings_entry "$entry" "$bn"; then
      continue
    fi
    safe_remove_path "$entry"
  done < <(find "$root" -mindepth 1 -maxdepth 1 -print0)
done

# 3) purge retained MQTT topics tied to Nodus/devices
log "Step 3: purge retained Nodus/device MQTT topics"
if ! command -v mosquitto_sub >/dev/null 2>&1 || ! command -v mosquitto_pub >/dev/null 2>&1; then
  log "  mosquitto_sub/mosquitto_pub not found on target; MQTT cleanup skipped"
  exit 0
fi

retained_topics=()
sub_help="$(mosquitto_sub --help 2>&1 || true)"

if grep -q -- '--retained-only' <<<"$sub_help"; then
  sub_cmd=(mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" --retained-only -F '%t' -W 3 -t '#')
  if [[ -n "$MQTT_USER" ]]; then
    sub_cmd+=( -u "$MQTT_USER" )
  fi
  if [[ -n "$MQTT_PASS" ]]; then
    sub_cmd+=( -P "$MQTT_PASS" )
  fi

  while IFS= read -r topic; do
    [[ -n "$topic" ]] && retained_topics+=("$topic")
  done < <("${sub_cmd[@]}" 2>/dev/null | sed '/^[[:space:]]*$/d' | sort -u || true)
elif grep -q -- '-F' <<<"$sub_help"; then
  log "  mosquitto_sub lacks --retained-only; using -F '%r|%t' fallback"
  sub_cmd=(mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -F '%r|%t' -W 3 -t '#')
  if [[ -n "$MQTT_USER" ]]; then
    sub_cmd+=( -u "$MQTT_USER" )
  fi
  if [[ -n "$MQTT_PASS" ]]; then
    sub_cmd+=( -P "$MQTT_PASS" )
  fi

  while IFS= read -r line; do
    [[ "$line" == 1\|* ]] || continue
    topic="${line#1|}"
    [[ -n "$topic" ]] && retained_topics+=("$topic")
  done < <("${sub_cmd[@]}" 2>/dev/null | sed '/^[[:space:]]*$/d' | sort -u || true)
else
  log "  mosquitto_sub lacks --retained-only/-F; using topic scan fallback"
  sub_cmd=(mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -v -W 3 -t '#')
  if [[ -n "$MQTT_USER" ]]; then
    sub_cmd+=( -u "$MQTT_USER" )
  fi
  if [[ -n "$MQTT_PASS" ]]; then
    sub_cmd+=( -P "$MQTT_PASS" )
  fi

  while IFS= read -r line; do
    topic="${line%% *}"
    [[ -n "$topic" ]] && retained_topics+=("$topic")
  done < <("${sub_cmd[@]}" 2>/dev/null | sed '/^[[:space:]]*$/d' | sort -u || true)
fi

if [[ ${#retained_topics[@]} -eq 0 ]]; then
  log "  no retained topics discovered"
  exit 0
fi

purge_topics=()
for topic in "${retained_topics[@]}"; do
  purge=0
  if [[ "$topic" == nodus/* || "$topic" == */nodus/* ]]; then
    purge=1
  fi

  if [[ "$purge" -eq 0 ]]; then
    for id in "${device_ids[@]}"; do
      [[ -z "$id" ]] && continue
      if [[ "$topic" == *"$id"* ]]; then
        purge=1
        break
      fi
    done
  fi

  if [[ "$purge" -eq 1 ]]; then
    purge_topics+=("$topic")
  fi
done

if [[ ${#purge_topics[@]} -eq 0 ]]; then
  log "  retained topics found, but none matched nodus/device patterns"
  exit 0
fi

while IFS= read -r topic; do
  [[ -z "$topic" ]] && continue
  purge_retained_topic "$topic"
done < <(printf '%s\n' "${purge_topics[@]}" | sort -u)
REMOTE_CLEAN
  then
    echo "Cleanup failed for ${host}" >&2
    return 1
  fi

  if [[ "${DRY_RUN}" -eq 0 && -n "${post_cmd}" ]]; then
    echo "Post-clean -> ${host}: ${post_cmd}"
    if ! "${SSH_BIN}" "${host}" "${post_cmd}"; then
      echo "Post-clean command failed for ${host}" >&2
      return 1
    fi
  fi

  return 0
}

FAILURES=0
LINE_NO=0
MATCHED=0

while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
  LINE_NO=$((LINE_NO + 1))
  line="$(printf '%s' "${raw_line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "${line}" || "${line}" == \#* ]] && continue

  IFS='|' read -r host target post_cmd <<< "${line}"
  host="$(printf '%s' "${host:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  target="$(printf '%s' "${target:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  post_cmd="$(printf '%s' "${post_cmd:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [[ -z "${host}" || -z "${target}" ]]; then
    echo "[line ${LINE_NO}] Invalid entry: ${raw_line}" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  if ! host_selected "${host}"; then
    continue
  fi

  MATCHED=$((MATCHED + 1))
  if ! run_remote_cleanup "${host}" "${target}" "${post_cmd}"; then
    FAILURES=$((FAILURES + 1))
  fi
done < "${HOSTS_FILE}"

if [[ "${MATCHED}" -eq 0 ]]; then
  echo "No hosts matched selection." >&2
  print_available_hosts
  exit 1
fi

if [[ "${FAILURES}" -gt 0 ]]; then
  echo
  echo "Completed with ${FAILURES} failure(s)." >&2
  exit 1
fi

echo
echo "Cleanup completed successfully."
