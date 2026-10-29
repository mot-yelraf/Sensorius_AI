#!/bin/sh
# Elevated Linux-only helper. Application/venv installation stays unprivileged.
set -eu
usb_group=${1:-}
install_libusb=${2:-}
case "$usb_group" in
  ''|*[!a-zA-Z0-9_.-]*) printf 'Invalid USB access group.\n' >&2; exit 2 ;;
esac
case "$install_libusb" in 0|1) ;; *) exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { printf 'Administrator authorization is required.\n' >&2; exit 1; }
getent group "$usb_group" >/dev/null || { printf 'USB access group does not exist.\n' >&2; exit 1; }
command -v udevadm >/dev/null || { printf 'udev is required for automatic USB access setup.\n' >&2; exit 1; }
if [ "$install_libusb" -eq 1 ]; then
  command -v apt-get >/dev/null || {
    printf 'Install libusb using your distribution package manager, then rerun the installer.\n' >&2
    exit 1
  }
  apt-get install -y --no-install-recommends libusb-1.0-0
fi
usb_rule_temp=$(mktemp)
trap 'unlink "$usb_rule_temp"' EXIT
printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="24c0", ATTR{idProduct}=="0003", GROUP="%s", MODE="0660"\n' "$usb_group" > "$usb_rule_temp"
printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="0fde", ATTR{idProduct}=="ca01", GROUP="%s", MODE="0660"\n' "$usb_group" >> "$usb_rule_temp"
mkdir -p /etc/udev/rules.d
if ! cmp -s "$usb_rule_temp" /etc/udev/rules.d/70-sensorius-acurite.rules; then
  install -o root -g root -m 0644 "$usb_rule_temp" /etc/udev/rules.d/70-sensorius-acurite.rules
fi
udevadm control --reload-rules
printf 'AcuRite and Oregon Scientific USB access configured for group %s. Unplug/reconnect the console to apply it.\n' "$usb_group"
