"""Provide bounded USB transports for Oregon Scientific WMR100 consoles.

Linux keeps one claimed interface; macOS/Windows use a native HID worker with
a hard deadline while retaining the operating system driver.
"""

import hashlib
import json
from .diagnostics import report_error
import subprocess
import sys
from pathlib import Path
from time import monotonic
from typing import Any

from sensorius.weather_adapters.wmr100 import ACTIVATE, USB_PRODUCT, USB_VENDOR, Wmr100Error




class PyUsbConsole:
    """Keep one Linux USB interface claimed between bounded interrupt-read batches."""

    continuous = True

    def __init__(self, serial: str = "", *, core: Any = None, util: Any = None) -> None:
        self.serial, self.core, self.util = serial, core, util
        self.device = None
        self.detached = self.claimed = False
        self.identity = ""

    def _open(self) -> None:
        if self.core is None:
            try:
                import usb.core
                import usb.util
            except ImportError as exc:
                raise Wmr100Error(
                    "PyUSB is missing; reinstall Sensorius dependencies."
                ) from exc
            self.core, self.util = usb.core, usb.util
        devices = list(
            self.core.find(find_all=True, idVendor=USB_VENDOR, idProduct=USB_PRODUCT)
            or []
        )
        selected = None
        try:
            matches = [
                d for d in devices if not self.serial or d.serial_number == self.serial
            ]
            if len(matches) != 1:
                raise Wmr100Error(
                    "No matching WMR100 USB console found."
                    if not matches
                    else "Multiple WMR100 consoles found; select a USB serial number."
                )
            selected = self.device = matches[0]
            try:
                if self.device.is_kernel_driver_active(0):
                    self.device.detach_kernel_driver(0)
                    self.detached = True
            except NotImplementedError:
                pass
            self.util.claim_interface(self.device, 0)
            self.claimed = True
            ports = ".".join(str(p) for p in (self.device.port_numbers or ()))
            self.identity = (
                self.serial
                or self.device.serial_number
                or f"bus-{self.device.bus}-ports-{ports or 'single'}"
            )
            # HID SET_REPORT enables live input. It is not a reset/mode/archive command.
            self.device.ctrl_transfer(0x21, 0x09, 0x0200, 0, ACTIVATE, timeout=500)
        except Exception:
            self.close()
            raise
        finally:
            for device in devices:
                if device is not selected:
                    self.util.dispose_resources(device)

    def read_batch(self) -> tuple[str, list[bytes]]:
        """Read at most 32 reports in one second; an idle console is not an I/O failure."""
        try:
            if self.device is None:
                self._open()
            reports = []
            deadline = monotonic() + 1
            while len(reports) < 32 and monotonic() < deadline:
                try:
                    reports.append(bytes(self.device.read(0x81, 8, timeout=250)))
                except self.core.USBTimeoutError:
                    continue
            return self.identity, reports
        except Wmr100Error:
            raise
        except Exception as exc:
            raise Wmr100Error(
                "WMR100 USB access failed; check libusb, permissions, cable, and exclusive console access."
            ) from exc

    def close(self) -> None:
        """Release the interface, restore any detached kernel driver, and dispose resources."""
        device, self.device = self.device, None
        if device is None:
            return
        try:
            if self.claimed:
                self.util.release_interface(device, 0)
        except Exception:
            report_error("Could not release WMR100 USB interface", exc_info=True)
        finally:
            self.claimed = False
            try:
                if self.detached:
                    device.attach_kernel_driver(0)
            except Exception:
                report_error(
                    "Could not restore WMR100 USB kernel driver", exc_info=True
                )
            finally:
                self.detached = False
                self.util.dispose_resources(device)


class NativeHidConsole:
    """Read a short native-HID stream session inside a process with a hard deadline."""

    continuous = False

    def __init__(self, serial: str = "") -> None:
        self.serial = serial

    def read_batch(self) -> tuple[str, list[bytes]]:
        """Collect one session; framing is resynchronized at each session boundary."""
        try:
            result = subprocess.run(
                ([sys.executable, "--wmr100-hid-worker"] if getattr(sys, "frozen", False)
                 else [sys.executable, "-m", "sensorius.weather_adapters.wmr100_usb"]),
                input=json.dumps({"serial": self.serial}),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                cwd=Path(__file__).resolve().parents[2],
            )
            if result.returncode or len(result.stdout) > 16384:
                raise Wmr100Error("WMR100 HID worker failed; check server logs.")
            reply = json.loads(result.stdout)
            if "error" in reply:
                raise Wmr100Error(reply["error"])
            return reply["identity"], [bytes.fromhex(item) for item in reply["reports"]]
        except subprocess.TimeoutExpired as exc:
            raise Wmr100Error(
                "WMR100 HID session timed out; reconnect the console and close other USB readers."
            ) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Wmr100Error(
                "WMR100 HID worker failed; check native HID access."
            ) from exc

    def close(self) -> None:
        """Each bounded worker closes its own handle before returning."""


def console_transport(serial: str = ""):
    """Select native HID on macOS/Windows and libusb on Linux, including Raspberry Pi."""
    return (
        NativeHidConsole(serial)
        if sys.platform in {"darwin", "win32"}
        else PyUsbConsole(serial)
    )


def read_native_batch(serial: str, hid_module=None) -> tuple[str, list[bytes]]:
    """Collect bounded interrupt input using the OS HID driver; always close the handle."""
    if hid_module is None:
        try:
            import hid as hid_module
        except ImportError as exc:
            raise Wmr100Error(
                "HIDAPI is missing; reinstall Sensorius dependencies."
            ) from exc
    matches = [
        item
        for item in hid_module.enumerate(USB_VENDOR, USB_PRODUCT)
        if item.get("interface_number") in {None, -1, 0}
        and (not serial or item.get("serial_number") == serial)
    ]
    if len(matches) != 1:
        raise Wmr100Error(
            "No matching WMR100 HID console found."
            if not matches
            else "Multiple WMR100 HID consoles found; select a USB serial number."
        )
    info = matches[0]
    path = info["path"]
    identity = (
        info.get("serial_number")
        or hashlib.sha256(
            path if isinstance(path, bytes) else path.encode()
        ).hexdigest()[:24]
    )
    handle = hid_module.device()
    try:
        handle.open_path(path)
        # HIDAPI includes a report-ID byte for unnumbered output reports.
        output = b"\x00" + ACTIVATE
        if handle.write(output) != len(output):
            raise Wmr100Error("WMR100 HID live-stream activation failed.")
        reports = []
        deadline = monotonic() + 3
        while len(reports) < 128 and monotonic() < deadline:
            raw = bytes(handle.read(8, timeout_ms=250))
            if raw:
                reports.append(raw)
        return f"hid-{identity}", reports
    finally:
        handle.close()


def main() -> None:
    """Serve one small worker request without modifying device settings or archives."""
    try:
        request = json.loads(sys.stdin.read(4096))
        serial = request.get("serial", "")
        if not isinstance(serial, str) or len(serial) > 128:
            raise ValueError("Invalid WMR100 serial number")
        identity, reports = read_native_batch(serial)
        reply = {"identity": identity, "reports": [item.hex() for item in reports]}
    except Exception as exc:
        reply = {
            "error": str(exc)
            if isinstance(exc, Wmr100Error)
            else "WMR100 HID access failed; close other readers and check device permissions."
        }
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
