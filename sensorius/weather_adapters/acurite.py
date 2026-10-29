"""AcuRite PC Connect live USB acquisition; no console archive access.

Protocol facts: https://weewx.com/docs/5.5/hardware/acurite/ and the
WeeWX AcuRite driver's R1/R2 message maps. Decoding is implemented locally.
"""

import math
import json
import subprocess
import sys
from pathlib import Path
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from .acurite_models import console_profile
from .observations import ChannelCapability, Observation, RainPolicy

from .metrics import FIELD_NAMES, normalize_acurite

from .diagnostics import report_error
USB_VENDOR = 0x24C0
USB_PRODUCT = 0x0003
SAMPLE_SECONDS = 18
CONSOLE_SECONDS = 60
MAX_AGE_SECONDS = 120
# N, NNE, NE, ... in the console's encoded order.
COMPASS_CODES = (6, 14, 12, 8, 10, 11, 9, 13, 15, 7, 5, 1, 3, 2, 0, 4)


class AcuRiteError(RuntimeError):
    """A safe operator-facing acquisition or decoding failure."""


@dataclass(frozen=True)
class OutdoorReport:
    """One live outdoor report; rain is an unsigned rolling tip counter in inches."""

    sensor_id: str
    signal: int
    battery: str
    measurements: dict[str, float]
    rain_counter: float | None = None


def decode_outdoor(raw: bytes) -> OutdoorReport:
    """Decode the ten-byte R1 packet without treating lost RF as fresh weather."""
    if len(raw) != 10 or raw[0] != 1:
        raise AcuRiteError("Invalid AcuRite outdoor report length or ID.")
    flavor, signal = raw[3] & 15, raw[8] & 15
    channel = {0xC0: "A", 0x80: "B", 0x00: "C"}.get(raw[1] & 0xF0)
    if flavor not in {1, 8} or signal > 3 or raw[9] not in {0, 255} or channel is None:
        raise AcuRiteError("Invalid AcuRite outdoor report framing.")
    sensor_id = f"{channel}-{((raw[1] & 15) << 8) | raw[2]:03x}"
    status = raw[3] >> 4
    battery = {7: "OK", 11: "LOW"}.get(status, "UNKNOWN") if signal else "UNKNOWN"
    values = {}
    rain = None
    if signal:
        wind_code = (raw[4] & 31) * 8 + ((raw[5] >> 4) & 7)
        values["wind_speed"] = (0.8278 * wind_code + 1) / 1.609344 if wind_code else 0.0
        if flavor == 1:
            values["wind_dir"] = COMPASS_CODES.index(raw[5] & 15) * 22.5
            rain = (((raw[6] & 63) << 7) + (raw[7] & 127)) / 100
        else:
            # Encoded Celsius is offset by 40 C and scaled by 18.
            celsius = (((raw[5] & 15) << 7) + (raw[6] & 127)) / 18 - 40
            humidity = raw[7] & 127
            if not -40 <= celsius <= 70 or not 0 <= humidity <= 100:
                raise AcuRiteError("AcuRite outdoor values are outside sensor bounds.")
            values.update(temperature=celsius * 1.8 + 32, humidity=float(humidity))
    return OutdoorReport(sensor_id, signal, battery, values, rain)


def decode_console(raw: bytes) -> dict[str, float]:
    """Decode recognized R2 calibration layouts as indoor temperature and absolute pressure."""
    if len(raw) != 25 or raw[:3] != b"\x02\x00\x00":
        raise AcuRiteError("Invalid AcuRite console report length or ID.")
    coefficients = [
        int.from_bytes(raw[index : index + 2], "big") for index in range(3, 17, 2)
    ]
    sensitivity, offset, thermal_sens, thermal_offset, reference, thermal_temp, fine = (
        coefficients
    )
    a, b, c, d = raw[17:21]
    temperature_raw = int.from_bytes(raw[21:23], "big")
    pressure_raw = int.from_bytes(raw[23:25], "big")
    if coefficients == [0x8000, 0, 0, 0x400, 0x1000, 0, 0x960] and (a, b, c, d) == (
        1,
        1,
        1,
        1,
    ):
        signed_temperature = temperature_raw & 0xFFF
        if signed_temperature >= 0x800:
            signed_temperature -= 0x1000
        celsius = 25 + signed_temperature / 20
        hpa = pressure_raw / 16 - 208
    elif (
        0x100 <= sensitivity <= 0xFFFF
        and 0 <= offset <= 0x1FFF
        and 0 <= thermal_sens <= 0x400
        and 0 <= thermal_offset <= 0x1000
        and 0x1000 <= reference <= 0xFFFF
        and 0 <= thermal_temp <= 0x4000
        and 0x960 <= fine <= 0xA28
        and 1 <= a <= 63
        and 1 <= b <= 63
        and 1 <= c <= 15
        and 1 <= d <= 15
    ):
        # HP03S compensation equations; use only validated calibration parameters.
        delta = temperature_raw - reference
        corrected = delta - (delta / 128) ** 2 * (a if delta >= 0 else b) / 2**c
        pressure_offset = 4 * (offset + (thermal_offset - 1024) * corrected / 16384)
        pressure_sensitivity = sensitivity + thermal_sens * corrected / 1024
        hpa = (
            pressure_sensitivity * (pressure_raw - 7168) / 16384 - pressure_offset
        ) / 32 + fine / 10
        celsius = (250 + corrected * thermal_temp / 65536 - corrected / 2**d) / 10
    else:
        raise AcuRiteError(
            "Unrecognized AcuRite pressure calibration; console validation is needed."
        )
    if not math.isfinite(hpa) or not 300 <= hpa <= 1100 or not -40 <= celsius <= 85:
        raise AcuRiteError("AcuRite console values are outside sensor bounds.")
    return {
        "indoor_temperature": celsius * 1.8 + 32,
        "indoor_absolute_pressure": hpa / 33.8638866667,
    }


class PyUsbConsole:
    """Claim one console for bounded HID GET_REPORT requests, then release it."""

    def __init__(self, serial: str = "", *, core: Any = None, util: Any = None) -> None:
        self.serial = serial
        self.core, self.util = core, util

    def read_reports(
        self, include_console: bool
    ) -> tuple[str, dict[int, bytes], list[str]]:
        """Read R1 and optionally R2; never issue logger, reset, or mode commands."""
        if self.core is None:
            try:
                import usb.core
                import usb.util
            except ImportError as exc:
                raise AcuRiteError(
                    "PyUSB is missing; reinstall the standard Sensorius dependencies."
                ) from exc
            self.core, self.util = usb.core, usb.util
        devices = []
        device = None
        detached = claimed = False
        try:
            devices = list(
                self.core.find(
                    find_all=True, idVendor=USB_VENDOR, idProduct=USB_PRODUCT
                )
                or []
            )
            matches = devices
            if self.serial:
                matches = [
                    candidate
                    for candidate in devices
                    if candidate.serial_number == self.serial
                ]
            if len(matches) != 1:
                raise AcuRiteError(
                    "No matching AcuRite USB console found."
                    if not matches
                    else "Multiple AcuRite consoles found; select a USB serial number."
                )
            device = matches[0]
            try:
                if device.is_kernel_driver_active(0):
                    device.detach_kernel_driver(0)
                    detached = True
            except NotImplementedError:
                pass
            self.util.claim_interface(device, 0)
            claimed = True
            # USB port topology is stable across reconnects at the same Pi port.
            ports = ".".join(str(port) for port in (device.port_numbers or ()))
            identity = self.serial or f"bus-{device.bus}-ports-{ports or 'single'}"
            reports, errors = {}, []
            for report_id, length in ((1, 10), (2, 25)):
                if report_id == 2 and not include_console:
                    continue
                try:
                    reports[report_id] = bytes(
                        device.ctrl_transfer(
                            0xA1, 0x01, 0x0100 | report_id, 0, length, timeout=1000
                        )
                    )
                except self.core.USBError as exc:
                    report_error("AcuRite USB report %s failed: %s", report_id, exc)
                    errors.append(
                        f"USB report {report_id} unavailable; check connection and USB permissions."
                    )
            if not reports:
                raise AcuRiteError(
                    "AcuRite USB reports unavailable; check connection and USB permissions."
                )
            return identity, reports, errors
        except AcuRiteError:
            raise
        except Exception as exc:
            report_error("AcuRite USB access failed", exc_info=True)
            raise AcuRiteError(
                "AcuRite USB access failed; check libusb, permissions, and exclusive device access."
            ) from exc
        finally:
            if device is not None:
                if claimed:
                    try:
                        self.util.release_interface(device, 0)
                    except Exception:
                        report_error(
                            "Could not release AcuRite USB interface", exc_info=True
                        )
                if detached:
                    try:
                        device.attach_kernel_driver(0)
                    except Exception:
                        report_error(
                            "Could not reattach AcuRite kernel driver", exc_info=True
                        )
            for candidate in devices:
                try:
                    self.util.dispose_resources(candidate)
                except Exception:
                    report_error(
                        "Could not dispose AcuRite USB resources", exc_info=True
                    )


class HidWorkerConsole:
    """Bound native HID reads to five seconds, including a stuck OS USB request."""

    def __init__(self, serial: str = "") -> None:
        self.serial = serial

    def read_reports(
        self, include_console: bool
    ) -> tuple[str, dict[int, bytes], list[str]]:
        """Read through a short-lived worker so shutdown never waits on unbounded HID I/O."""
        try:
            result = subprocess.run(
                ([sys.executable, "--acurite-hid-worker"] if getattr(sys, "frozen", False)
                 else [sys.executable, "-m", "sensorius.weather_adapters.acurite_hid"]),
                input=json.dumps(
                    {"serial": self.serial, "include_console": include_console}
                ),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=Path(__file__).resolve().parents[2],
            )
            if result.returncode or len(result.stdout) > 8192:
                raise AcuRiteError(
                    "AcuRite HID worker failed; see server logs for details."
                )
            reply = json.loads(result.stdout)
            if "error" in reply:
                raise AcuRiteError(reply["error"])
            return (
                reply["identity"],
                {
                    int(key): bytes.fromhex(value)
                    for key, value in reply["reports"].items()
                },
                reply["errors"],
            )
        except subprocess.TimeoutExpired as exc:
            raise AcuRiteError(
                "AcuRite HID read timed out; reconnect the console and close other USB readers."
            ) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report_error("AcuRite HID worker failed", exc_info=True)
            raise AcuRiteError(
                "AcuRite HID worker failed; see server logs for details."
            ) from exc


def console_transport(serial: str = ""):
    """Use native OS HID on macOS/Windows and bounded libusb requests on Linux."""
    return (
        HidWorkerConsole(serial)
        if sys.platform in {"darwin", "win32"}
        else PyUsbConsole(serial)
    )


class AcuRiteUsbReader:
    """Sample alternating reports independently of the per-station history interval."""

    def __init__(self, settings: Any, transport: Any = None) -> None:
        self.settings = settings
        self.transport = transport
        self._lock = threading.RLock()
        self._samples: dict[str, tuple[float, datetime, float]] = {}
        self._counter: tuple[float, datetime, float] | None = None
        self._identity = ""
        self._sensor = ""
        self._signal = 0
        self._battery = "UNKNOWN"
        self._last_outdoor = 0.0
        self._last_console = float("-inf")
        self._last_emitted: datetime | None = None
        self._connected = False
        self._error = ""
        self._console_error = ""
        self._outdoor_error = ""

    @property
    def device_identity(self) -> str:
        """Return the console identity separately from its outdoor RF sensor."""
        with self._lock:
            return self._identity

    def sample_once(self) -> None:
        """Read bounded USB reports off the event loop, then update the local cache."""
        if not self.settings.acurite_enabled:
            with self._lock:
                self._samples.clear()
                self._counter = None
                self._connected = False
                self._last_console = float("-inf")
            return
        now, clock = datetime.now(timezone.utc), monotonic()
        try:
            serial = self.settings.acurite_usb_serial
            transport = self.transport or console_transport(serial)
            identity, reports, errors = transport.read_reports(
                clock - self._last_console >= CONSOLE_SECONDS
            )
            with self._lock:
                if (
                    not self.settings.acurite_enabled
                    or serial != self.settings.acurite_usb_serial
                ):
                    self._samples.clear()
                    self._counter = None
                    self._connected = False
                    return
                self._accept_reports(identity, reports, errors, now, clock)
        except AcuRiteError as exc:
            with self._lock:
                if self._error != str(exc):
                    report_error("AcuRite: %s", exc)
                self._error = str(exc)
                self._connected = False
                self._clear_outdoor()
                self._samples.clear()
                self._last_console = float("-inf")

    def _accept_reports(self, identity, reports, errors, now, clock) -> None:
        if identity != self._identity:
            self._samples.clear()
            self._counter = None
            self._sensor = ""
            self._last_outdoor = 0
            self._identity = identity
            self._console_error = self._outdoor_error = ""
            self._last_console = float("-inf")
        self._connected = True
        if 1 in reports:
            try:
                outdoor = decode_outdoor(reports[1])
                self._outdoor_error = ""
                if outdoor.sensor_id != self._sensor:
                    self._clear_outdoor()
                    self._sensor = outdoor.sensor_id
                self._signal, self._battery = outdoor.signal, outdoor.battery
                if not outdoor.signal:
                    self._clear_outdoor()
                else:
                    self._last_outdoor = clock
                    for key, value in outdoor.measurements.items():
                        self._samples[key] = (value, now, clock)
                    if outdoor.rain_counter is not None:
                        self._counter = (outdoor.rain_counter, now, clock)
            except AcuRiteError as exc:
                self._clear_outdoor()
                self._outdoor_error = str(exc)
        if 2 in reports:
            self._last_console = clock
            try:
                values = decode_console(reports[2])
                self._console_error = ""
                for key, value in values.items():
                    self._samples[key] = (value, now, clock)
            except AcuRiteError as exc:
                for key in ("indoor_temperature", "indoor_absolute_pressure"):
                    self._samples.pop(key, None)
                self._console_error = str(exc)
        error = " ".join(
            message
            for message in [*errors, self._outdoor_error, self._console_error]
            if message
        )
        if error != self._error and error:
            report_error("AcuRite: %s", error)
        self._error = error

    def _clear_outdoor(self) -> None:
        for key in ("temperature", "humidity", "wind_speed", "wind_dir"):
            self._samples.pop(key, None)
        self._counter = None
        self._signal = 0
        self._battery = "UNKNOWN"
        self._last_outdoor = 0

    def read(self) -> Observation | None:
        """Return fresh timestamped fields once per sampled snapshot, without USB I/O."""
        with self._lock:
            if not self.settings.acurite_enabled or not self._connected:
                return None
            clock = monotonic()
            samples = {
                key: sample
                for key, sample in self._samples.items()
                if clock - sample[2] <= MAX_AGE_SECONDS
            }
            if not samples:
                return None
            timestamp = max(sample[1] for sample in samples.values())
            if self._last_emitted is not None and timestamp <= self._last_emitted:
                return None
            self._last_emitted = timestamp
            counter = (
                self._counter
                if self._counter and clock - self._counter[2] <= MAX_AGE_SECONDS
                else None
            )
            return Observation(
                source_id=f"acurite:{self._identity}:{self._sensor or 'console'}",
                observed_at=timestamp,
                received_at=timestamp,
                measurements=normalize_acurite({key: sample[0] for key, sample in samples.items()}),
                rain=RainPolicy(
                    kind="rolling",
                    channel_id=self._sensor or "console",
                    timezone=self.settings.timezone,
                    modulus=81.92,
                ),
                rain_counter=counter[0] if counter else None,
                rain_counter_at=counter[1] if counter else None,
            )

    def capabilities(self) -> tuple[ChannelCapability, ...]:
        """Advertise only the live outdoor and console fields decoded here."""
        return (
            ChannelCapability(
                "primary",
                tuple(FIELD_NAMES.values()) + ("Temperature", "Console Temperature", "Humidity", "Ambient VPD"),
            ),
        )

    def status(self) -> dict[str, Any]:
        """Report USB connectivity separately from fresh outdoor RF reception."""
        with self._lock:
            enabled = self.settings.acurite_enabled
            fresh = (
                enabled
                and self._connected
                and self._signal > 0
                and monotonic() - self._last_outdoor <= MAX_AGE_SECONDS
            )
            battery = self._battery if fresh else "UNKNOWN"
            profile = console_profile(self.settings.acurite_model)
            error = self._error if enabled else ""
            if error and profile.requires_mode4_selection:
                error += " Confirm USB mode 4 is selected on the console."
            return {
                "source_id": f"acurite:{self._identity}:{self._sensor}",
                "enabled": enabled,
                "connected": enabled and self._connected,
                "state": "online" if fresh else "offline" if enabled else "disabled",
                "label": f"AcuRite {profile.label} USB",
                "configured_model": profile.key,
                "requires_mode4_selection": profile.requires_mode4_selection,
                "usb_mode_help": profile.mode_help,
                "last_error": error,
                "observation_fresh": fresh,
                "station": {"reporting": fresh, "battery_status": battery},
                "sensors": [
                    {
                        "sensor_id": self._sensor,
                        "name": "AcuRite outdoor array",
                        "reporting": fresh,
                        "battery_status": battery,
                        "signal": self._signal,
                    }
                ]
                if self._sensor
                else [],
            }
