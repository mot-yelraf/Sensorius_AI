#!/usr/bin/env bash
set -euo pipefail

PATH="${PATH}:/usr/sbin:/sbin"
export PATH

TARGET_USER="${SUDO_USER:-$(id -un)}"
SELECTED_URI=""
QUEUE_NAME=""
ASSUME_YES=0
LIST_ONLY=0
KEEP_CUPS_BROWSED=0

usage() {
  cat <<'EOF'
Usage: setup_rpi_printer.sh [options]

Discover and configure a permanent IPP Everywhere printer for local Sensorius
and Biodynamic Calendar reports on Raspberry Pi OS.

Options:
  --yes                    Configure automatically when exactly one printer is found.
  --list                   List discovered driverless printers without changing CUPS.
  --uri URI                Configure this explicit ipp:// or ipps:// printer URI.
  --queue NAME             Override the generated CUPS queue name.
  --user USER              Set this user's CUPS default as well as the system default.
  --keep-cups-browsed      Do not disable cups-browsed or remove matching proxies.
  -h, --help               Show this help.

When multiple printers are discovered, interactive selection or --uri is
required. The helper never prints a test page or cancels queued jobs.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    --uri)
      [[ $# -ge 2 ]] || { echo "ERROR: --uri requires a value." >&2; exit 2; }
      SELECTED_URI="$2"
      shift 2
      ;;
    --queue)
      [[ $# -ge 2 ]] || { echo "ERROR: --queue requires a value." >&2; exit 2; }
      QUEUE_NAME="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { echo "ERROR: --user requires a value." >&2; exit 2; }
      TARGET_USER="$2"
      shift 2
      ;;
    --keep-cups-browsed)
      KEEP_CUPS_BROWSED=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for required_command in driverless ipptool lp lpadmin lpoptions lpstat systemctl; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Printer setup unavailable: ${required_command} is not installed." >&2
    echo "Install cups, cups-client, cups-ipp-utils, and cups-filters-core-drivers." >&2
    exit 1
  fi
done

run_admin() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

run_as_target_user() {
  if [[ "$(id -un)" == "${TARGET_USER}" ]]; then
    "$@"
  elif [[ "$(id -u)" -eq 0 ]]; then
    sudo -H -u "${TARGET_USER}" "$@"
  else
    sudo -H -u "${TARGET_USER}" "$@"
  fi
}

printer_info_for_uri() {
  local uri="$1"
  ipptool -tv "${uri}" /usr/share/cups/ipptool/get-printer-attributes.test 2>/dev/null \
    | awk '/printer-info .* = / {sub(/^.* = /, ""); print; exit}'
}

queue_name_for_info() {
  local info="$1"
  printf '%s' "${info}" \
    | sed -e 's/[^A-Za-z0-9]/_/g' -e 's/__*/_/g' -e 's/^_//' -e 's/_$//'
}

discover_printers() {
  driverless --std-ipp-uris 2>/dev/null \
    | awk '/^ipps?:\/\// && !seen[$0]++ {print}'
}

declare -a DISCOVERED_URIS=()
while IFS= read -r discovered_uri; do
  [[ -n "${discovered_uri}" ]] && DISCOVERED_URIS+=("${discovered_uri}")
done < <(discover_printers)

echo "Driverless IPP printers discovered: ${#DISCOVERED_URIS[@]}"
for index in "${!DISCOVERED_URIS[@]}"; do
  info="$(printer_info_for_uri "${DISCOVERED_URIS[$index]}" || true)"
  printf '  %d) %s\n     %s\n' "$((index + 1))" "${info:-Unknown IPP printer}" "${DISCOVERED_URIS[$index]}"
done

if [[ "${LIST_ONLY}" -eq 1 ]]; then
  exit 0
fi

if [[ -z "${SELECTED_URI}" ]]; then
  if [[ "${#DISCOVERED_URIS[@]}" -eq 0 ]]; then
    echo "No reachable driverless IPP printer was found."
    echo "Connect the printer to the same network, then rerun this helper."
    exit 0
  fi

  if [[ "${#DISCOVERED_URIS[@]}" -eq 1 ]]; then
    SELECTED_URI="${DISCOVERED_URIS[0]}"
    current_default="$(lpstat -d 2>/dev/null | sed -n 's/^system default destination: //p')"
    current_device=""
    current_state=""
    if [[ -n "${current_default}" ]]; then
      current_device="$(lpstat -v "${current_default}" 2>/dev/null || true)"
      current_state="$(lpstat -p "${current_default}" 2>/dev/null || true)"
    fi
    if [[ "${current_device}" == *"${SELECTED_URI}"* && "${current_state}" != *" disabled "* ]]; then
      echo "Printer setup already complete: ${current_default} is the enabled direct IPP default."
      exit 0
    fi
    if [[ "${ASSUME_YES}" -ne 1 ]]; then
      if [[ ! -t 0 ]]; then
        echo "Printer setup skipped because no interactive terminal is available."
        echo "Rerun with --yes to configure the single discovered printer."
        exit 0
      fi
      read -r -p "Configure this printer as the local Sensorius default? [y/N]: " answer
      [[ "${answer}" =~ ^[Yy]$ ]] || { echo "Printer setup skipped."; exit 0; }
    fi
  else
    if [[ "${ASSUME_YES}" -eq 1 || ! -t 0 ]]; then
      echo "ERROR: multiple printers were found; refusing to choose automatically." >&2
      echo "Rerun interactively or pass --uri with the intended printer." >&2
      exit 2
    fi
    read -r -p "Select a printer [1-${#DISCOVERED_URIS[@]}] or press Enter to skip: " selection
    [[ -n "${selection}" ]] || { echo "Printer setup skipped."; exit 0; }
    [[ "${selection}" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid selection." >&2; exit 2; }
    (( selection >= 1 && selection <= ${#DISCOVERED_URIS[@]} )) \
      || { echo "ERROR: selection out of range." >&2; exit 2; }
    SELECTED_URI="${DISCOVERED_URIS[$((selection - 1))]}"
  fi
fi

if [[ ! "${SELECTED_URI}" =~ ^ipps?:// ]]; then
  echo "ERROR: printer URI must begin with ipp:// or ipps://" >&2
  exit 2
fi

PRINTER_INFO="$(printer_info_for_uri "${SELECTED_URI}" || true)"
if [[ -z "${PRINTER_INFO}" ]]; then
  echo "ERROR: the selected printer did not answer an IPP capability query." >&2
  exit 1
fi

if [[ -z "${QUEUE_NAME}" ]]; then
  QUEUE_NAME="$(queue_name_for_info "${PRINTER_INFO}")"
  if [[ "${#DISCOVERED_URIS[@]}" -gt 1 ]]; then
    uri_host="${SELECTED_URI#*://}"
    uri_host="${uri_host%%/*}"
    uri_host="${uri_host%%:*}"
    QUEUE_NAME="${QUEUE_NAME}_$(queue_name_for_info "${uri_host}")"
  fi
fi
if [[ -z "${QUEUE_NAME}" || "${QUEUE_NAME}" =~ [^A-Za-z0-9_-] ]]; then
  echo "ERROR: invalid generated or supplied CUPS queue name: ${QUEUE_NAME}" >&2
  exit 2
fi

echo "Configuring ${PRINTER_INFO} as ${QUEUE_NAME} for ${TARGET_USER}."

pending_jobs="$(lpstat -W not-completed -o "${QUEUE_NAME}" 2>/dev/null || true)"
if [[ -n "${pending_jobs}" ]]; then
  echo "Holding existing jobs on ${QUEUE_NAME}; no queued document will print automatically."
  while IFS= read -r job_line; do
    job_id="${job_line%%[[:space:]]*}"
    [[ -n "${job_id}" ]] && run_admin lp -i "${job_id}" -H hold
  done <<< "${pending_jobs}"
fi

manage_cups_browsed=0
if [[ "${KEEP_CUPS_BROWSED}" -ne 1 && "${#DISCOVERED_URIS[@]}" -eq 1 ]]; then
  unrelated_proxy_count="$({
    lpstat -v 2>/dev/null \
      | awk '/^device for / && /implicitclass:\/\// {name=$3; sub(/:$/, "", name); print name}'
  } | awk -v queue="${QUEUE_NAME}" '$0 != queue && index($0, queue "@") != 1 {count++} END {print count + 0}')"
  if [[ "${unrelated_proxy_count}" -gt 0 ]]; then
    echo "Leaving cups-browsed enabled because unrelated proxy queues exist."
  else
    manage_cups_browsed=1
    if systemctl list-unit-files cups-browsed.service >/dev/null 2>&1; then
      run_admin systemctl disable --now cups-browsed.service
    fi
    run_admin systemctl restart cups.service
  fi
fi

run_admin lpadmin -p "${QUEUE_NAME}" -E -v "${SELECTED_URI}" -m everywhere
run_as_target_user lpoptions -d "${QUEUE_NAME}"
run_admin lpadmin -d "${QUEUE_NAME}"

configured_device="$(lpstat -v "${QUEUE_NAME}" 2>/dev/null || true)"
if [[ "${configured_device}" != *"${SELECTED_URI}"* ]]; then
  echo "ERROR: CUPS did not retain the selected direct IPP destination." >&2
  exit 1
fi

if [[ "${manage_cups_browsed}" -eq 1 ]]; then
  while IFS= read -r proxy_name; do
    [[ -n "${proxy_name}" ]] || continue
    [[ "${proxy_name}" == "${QUEUE_NAME}" || "${proxy_name}" == "${QUEUE_NAME}@"* ]] || continue
    proxy_jobs="$(lpstat -W not-completed -o "${proxy_name}" 2>/dev/null || true)"
    if [[ -n "${proxy_jobs}" ]]; then
      echo "Leaving proxy ${proxy_name} because it still owns queued jobs."
      continue
    fi
    run_admin lpadmin -x "${proxy_name}" || true
  done < <(
    lpstat -v 2>/dev/null \
      | awk '/^device for / && /implicitclass:\/\// {name=$3; sub(/:$/, "", name); print name}'
  )
  run_admin systemctl restart cups.service
fi

echo "Printer setup complete."
lpstat -p "${QUEUE_NAME}" -d
lpstat -v "${QUEUE_NAME}"
echo "No test page was printed. Use a BD Calendar report or an existing PDF to verify output."
