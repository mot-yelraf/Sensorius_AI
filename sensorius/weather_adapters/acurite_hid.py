"""Native HID report access for AcuRite on macOS/Windows, isolated by a worker.

HIDAPI uses the operating system's HID driver. Its input-report API does not
expose a caller timeout, so the adapter runs this worker with a bounded lifetime.
"""

import hashlib
import json
import sys
from typing import Any

from sensorius.weather_adapters.acurite import AcuRiteError, USB_PRODUCT, USB_VENDOR

from .diagnostics import report_error
MAX_REPORT_BYTES = 33


class NativeHidConsole:
    """Read numbered HID input reports without replacing or detaching OS drivers."""

    def __init__(self, serial: str = "", *, hid_module: Any = None) -> None:
        self.serial, self.hid = serial, hid_module

    def read_reports(
        self, include_console: bool
    ) -> tuple[str, dict[int, bytes], list[str]]:
        """Open the selected console, read R1/R2, and always close its handle."""
        if self.hid is None:
            try:
                import hid
            except ImportError as exc:
                raise AcuRiteError(
                    "HIDAPI is missing; reinstall the standard Sensorius dependencies."
                ) from exc
            self.hid = hid
        handle = None
        try:
            matches = [
                item
                for item in self.hid.enumerate(USB_VENDOR, USB_PRODUCT)
                if item.get("interface_number") in {None, -1, 0}
                and (not self.serial or item.get("serial_number") == self.serial)
            ]
            if len(matches) != 1:
                raise AcuRiteError(
                    "No matching AcuRite HID console found."
                    if not matches
                    else "Multiple AcuRite HID consoles found; select a USB serial number."
                )
            device = matches[0]
            path = device["path"]
            identity = (
                device.get("serial_number")
                or hashlib.sha256(
                    path if isinstance(path, bytes) else path.encode()
                ).hexdigest()[:24]
            )
            handle = self.hid.device()
            handle.open_path(path)
            reports, errors = {}, []
            for number, length in ((1, 10), (2, 25)):
                if number == 2 and not include_console:
                    continue
                try:
                    # Windows may return a buffer padded to the largest HID report.
                    raw = bytes(handle.get_input_report(number, MAX_REPORT_BYTES))
                    if len(raw) < length or raw[0] != number:
                        raise ValueError("Unexpected HID report ID or length")
                    reports[number] = raw[:length]
                except (OSError, ValueError):
                    report_error(
                        "AcuRite HID report %s unavailable", number, exc_info=True
                    )
                    errors.append(
                        f"HID report {number} unavailable; check console access and close other USB readers."
                    )
            if not reports:
                raise AcuRiteError(
                    "AcuRite HID reports unavailable; check console access and close other USB readers."
                )
            return f"hid-{identity}", reports, errors
        except AcuRiteError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            report_error("AcuRite native HID access failed", exc_info=True)
            raise AcuRiteError(
                "AcuRite HID access failed; reconnect the console and close other USB readers."
            ) from exc
        finally:
            if handle is not None:
                handle.close()


def main() -> None:
    """Serve one bounded-size JSON request; emit only report data or safe errors."""
    try:
        request = json.loads(sys.stdin.read(4096))
        if (
            not isinstance(request, dict)
            or not isinstance(request.get("serial", ""), str)
            or not isinstance(request.get("include_console"), bool)
        ):
            raise ValueError("Invalid HID worker request")
        identity, reports, errors = NativeHidConsole(
            request.get("serial", "")
        ).read_reports(request["include_console"])
        reply = {
            "identity": identity,
            "reports": {str(key): value.hex() for key, value in reports.items()},
            "errors": errors,
        }
    except (AcuRiteError, ValueError) as exc:
        reply = {"error": str(exc)}
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
