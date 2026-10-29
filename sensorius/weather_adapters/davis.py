"""Read-only Vantage Vue datalogger acquisition using Davis's serial protocol.

Independent implementation of the Vantage Serial Communication Reference 2.6.1:
https://www.davis.cz/download/VantageSerialProtocolDocs_v261.pdf
Only wakeup, station identification, configuration reads, RXCHECK and live LPS
requests are sent. Console archives, clocks, calibration and pairing are untouched.
"""

import binascii
from .diagnostics import report_error
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from sensorius.weather_adapters.observations import ChannelCapability, Observation, RainPolicy


BAUD_RATES = (1200, 2400, 4800, 9600, 14400, 19200)
SAMPLE_SECONDS = 10
MAX_AGE_SECONDS = 120
OUTDOOR_FIELDS = (
    "temperature",
    "humidity",
    "wind_speed",
    "wind_gust",
    "wind_dir",
    "rain_rate",
    "rain_total",
    "rain_month",
    "rain_year",
    "rain_event",
)
METRICS = (
    *OUTDOOR_FIELDS,
    "indoor_temperature",
    "indoor_humidity",
    "pressure",
    "absolute_pressure",
    "rain_increment",
)


class DavisError(RuntimeError):
    """An operator-visible datalogger or protocol failure."""


def validate_port(value: str) -> str:
    """Accept explicit local serial paths or Windows COM ports, never network URLs."""
    if (
        not isinstance(value, str)
        or len(value) > 256
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid Davis serial port")
    value = value.strip()
    if value and not (
        re.fullmatch(r"COM[1-9][0-9]*", value, re.IGNORECASE)
        or value.startswith("/dev/")
        and "/../" not in value
        and not value.endswith("/..")
    ):
        raise ValueError(
            "Use a local serial port such as /dev/ttyUSB0, /dev/cu.usbserial-123, or COM3"
        )
    return value


def checked_loop(raw: bytes, kind: int) -> bytes:
    """Require the expected live packet type, length, trailer and zero CRC-CCITT residue."""
    if (
        len(raw) != 99
        or raw[:3] != b"LOO"
        or raw[4] != kind
        or raw[95:97] != b"\n\r"
        or binascii.crc_hqx(raw, 0)
    ):
        raise DavisError("Invalid Davis LOOP packet framing or CRC")
    return raw


def rain_configuration(raw: bytes) -> tuple[int, float]:
    """Read a single Vue ISS transmitter and collector scale from EEPROM 0x17–0x2B."""
    if len(raw) != 21:
        raise DavisError("Invalid Davis station configuration length")
    mask = raw[0]
    if not mask or mask & (mask - 1):
        raise DavisError(
            "Davis support requires one enabled Vue ISS transmitter; check console setup."
        )
    transmitter = mask.bit_length()
    if raw[2 + (transmitter - 1) * 2] & 15 != 0:
        raise DavisError("The configured Davis transmitter is not a Vantage Vue ISS.")
    factor = {0: 0.01, 1: 0.2 / 25.4, 2: 0.1 / 25.4}.get((raw[20] >> 4) & 3)
    if factor is None:
        raise DavisError("Unknown Davis rain-collector configuration")
    return transmitter, factor


@dataclass(frozen=True)
class DavisSnapshot:
    """One validated console session, with radio reception progress kept separately."""

    identity: str
    transmitter: int
    rain_scale: float
    received_packets: int
    values: dict[str, float | None]
    battery: str


def decode_snapshot(
    loop: bytes,
    loop2: bytes,
    configuration: bytes,
    received_packets: int,
    identity: str,
) -> DavisSnapshot:
    """Normalize Vue live fields while preserving missing sentinels and legitimate zeroes."""
    checked_loop(loop, 0)
    checked_loop(loop2, 1)
    transmitter, scale = rain_configuration(configuration)
    if (
        isinstance(received_packets, bool)
        or not isinstance(received_packets, int)
        or received_packets < 0
    ):
        raise DavisError("Invalid Davis reception diagnostics")

    def word(raw, offset):
        return int.from_bytes(raw[offset : offset + 2], "little")

    def temperature(raw, offset):
        value = int.from_bytes(raw[offset : offset + 2], "little", signed=True)
        return value / 10 if -900 <= value <= 1800 else None

    def pressure(raw, offset):
        value = word(raw, offset)
        return value / 1000 if 10000 <= value <= 35000 else None

    def rain(raw, offset):
        value = word(raw, offset)
        return value * scale if value != 0xFFFF else None

    direction = word(loop2, 16)
    gust = word(loop2, 22)
    values = {
        "temperature": temperature(loop2, 12),
        "indoor_temperature": temperature(loop2, 9),
        "humidity": float(loop2[33]) if loop2[33] <= 100 else None,
        "indoor_humidity": float(loop2[11]) if loop2[11] <= 100 else None,
        "wind_speed": float(loop2[14]) if loop2[14] != 255 else None,
        "wind_gust": gust / 10 if gust <= 2500 else None,
        # Davis 0 means unavailable; 360 is valid north and normalizes to 0.
        "wind_dir": float(direction % 360) if 1 <= direction <= 360 else None,
        "pressure": pressure(loop2, 7),
        "absolute_pressure": pressure(loop2, 67),
        "rain_rate": rain(loop2, 41),
        "rain_total": rain(loop2, 50),
        "rain_event": rain(loop2, 46),
        "rain_month": rain(loop, 52),
        "rain_year": rain(loop, 54),
    }
    battery = "LOW" if loop[86] & (1 << (transmitter - 1)) else "OK"
    return DavisSnapshot(
        identity, transmitter, scale, received_packets, values, battery
    )


class SerialConsole:
    """Open only the configured local port; bound reads/writes and always close it."""

    def __init__(
        self, port: str, baud: int = 19200, *, serial_module: Any = None
    ) -> None:
        self.port, self.baud, self.serial = validate_port(port), baud, serial_module
        if isinstance(baud, bool) or baud not in BAUD_RATES:
            raise ValueError("Unsupported Davis baud rate")

    def read_snapshot(self) -> DavisSnapshot:
        """Identify a Vue and read configuration, radio diagnostics, LOOP and LOOP2."""
        if not self.port:
            raise DavisError(
                "Set the Davis datalogger's serial port before enabling acquisition."
            )
        if self.serial is None:
            try:
                import serial
            except ImportError as exc:
                raise DavisError(
                    "PySerial is missing; reinstall Sensorius dependencies."
                ) from exc
            self.serial = serial
        handle = None
        try:
            kwargs = {
                "port": self.port,
                "baudrate": self.baud,
                "timeout": 0.25,
                "write_timeout": 0.5,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "xonxoff": False,
                "rtscts": False,
                "dsrdtr": False,
            }
            if sys.platform != "win32":
                kwargs["exclusive"] = True
            handle = self.serial.Serial(**kwargs)
            session_end = monotonic() + 12

            def read_exact(size, seconds=1.2):
                deadline = min(session_end, monotonic() + seconds)
                result = bytearray()
                while len(result) < size:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise DavisError(
                            "Davis datalogger response timed out or was truncated."
                        )
                    handle.timeout = min(0.25, remaining)
                    chunk = handle.read(size - len(result))
                    if not chunk:
                        # Serial.read has already waited for its caller timeout.
                        continue
                    result.extend(chunk)
                return bytes(result)

            def send(command):
                if monotonic() >= session_end:
                    raise DavisError("Davis acquisition deadline exceeded")
                if handle.write(command) != len(command):
                    raise DavisError("Incomplete Davis datalogger command write")

            def ack(command):
                send(command)
                if read_exact(1) != b"\x06":
                    raise DavisError("Davis datalogger rejected a read request.")

            def eeprom(address, length):
                ack(f"EEBRD {address:02X} {length:02X}\n".encode("ascii"))
                block = read_exact(length + 2)
                if binascii.crc_hqx(block, 0):
                    raise DavisError("Invalid Davis configuration CRC")
                return block[:-2]

            handle.reset_input_buffer()
            for attempt in range(3):
                send(b"\n")
                try:
                    if read_exact(2) == b"\n\r":
                        break
                except DavisError:
                    if attempt == 2:
                        raise
                handle.reset_input_buffer()
            else:
                raise DavisError(
                    "Davis console did not wake; leave its setup screens and check baud rate."
                )
            ack(b"WRD\x12\x4d\n")
            if read_exact(1) != b"\x11":
                raise DavisError("The connected console is not a Davis Vantage Vue.")
            configuration = eeprom(0x17, 21)
            rain_configuration(configuration)
            send(b"RXCHECK\n")
            diagnostic = bytearray()
            diag_end = min(session_end, monotonic() + 1.5)
            match = None
            while len(diagnostic) < 128 and monotonic() < diag_end:
                diagnostic.extend(
                    read_exact(1, seconds=max(0.001, diag_end - monotonic()))
                )
                match = re.fullmatch(
                    rb"\s*OK\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\n\r", diagnostic
                )
                if match:
                    break
            if match is None:
                raise DavisError("Invalid Davis RXCHECK reception diagnostics")
            ack(b"LPS 3 2\n")
            packets = [read_exact(99, seconds=3.5), read_exact(99, seconds=3.5)]
            # The Vue alternates types; tolerate either starting type, require one of each.
            by_kind = {raw[4]: raw for raw in packets}
            if set(by_kind) != {0, 1}:
                raise DavisError("Davis did not return both LOOP and LOOP2 packets")
            return decode_snapshot(
                by_kind[0], by_kind[1], configuration, int(match[1]), self.port
            )
        except (DavisError, ValueError):
            raise
        except Exception as exc:
            report_error("Davis serial acquisition failed", exc_info=True)
            raise DavisError(
                "Davis serial access failed; check port, permissions, cable, baud rate, and exclusive access."
            ) from exc
        finally:
            if handle is not None:
                handle.close()


class DavisVueAdapter:
    """Sample a Vue independently of history writes and verify outdoor RF progress."""

    def __init__(self, settings: Any, transport: Any = None) -> None:
        self.settings, self.transport = settings, transport
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._snapshot = None
        self._sample_at = None
        self._sample_clock = float("-inf")
        self._last_emitted = None
        self._radio_clock = float("-inf")
        self._received = None
        self._source = ""
        self._config = None
        self._error = ""

    def _connection(self):
        return self.settings.davis_port, self.settings.davis_baud_rate

    def _clear(self):
        self._snapshot = None
        self._received = None
        self._radio_clock = float("-inf")

    def reset(self) -> None:
        """Discard old connection state after settings changes or shutdown."""
        with self._io_lock, self._lock:
            self._clear()
            self._error = ""

    def sample_once(self) -> None:
        """Acquire outside the cache lock, rejecting results from changed settings."""
        with self._io_lock:
            connection = self._connection()
            if not self.settings.davis_enabled:
                with self._lock:
                    self._clear()
                return
            try:
                snapshot = (
                    self.transport or SerialConsole(*connection)
                ).read_snapshot()
                now, clock = datetime.now(timezone.utc), monotonic()
                with self._lock:
                    if (
                        not self.settings.davis_enabled
                        or connection != self._connection()
                    ):
                        self._clear()
                        return
                    source = f"davis_vue:{snapshot.identity}:tx-{snapshot.transmitter}"
                    if source != self._source or connection != self._config:
                        self._clear()
                    if self._received is not None:
                        if snapshot.received_packets > self._received:
                            self._radio_clock = clock
                        elif snapshot.received_packets < self._received:
                            self._radio_clock = float("-inf")
                    self._received = snapshot.received_packets
                    self._snapshot, self._sample_at, self._sample_clock = (
                        snapshot,
                        now,
                        clock,
                    )
                    self._source, self._config, self._error = source, connection, ""
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, DavisError)
                    else "Davis acquisition failed; see server logs."
                )
                if not isinstance(exc, DavisError):
                    report_error("Davis acquisition failed")
                with self._lock:
                    if message != self._error:
                        report_error("Davis: %s", message)
                    self._clear()
                    self._error = message

    def _connected(self, clock):
        return bool(
            self.settings.davis_enabled
            and self._snapshot is not None
            and self._config == self._connection()
            and clock - self._sample_clock <= MAX_AGE_SECONDS
        )

    def _radio_fresh(self, clock):
        return self._connected(clock) and clock - self._radio_clock <= MAX_AGE_SECONDS

    def read(self) -> Observation | None:
        """Return each snapshot once, suppressing outdoor fields until RF progress is verified."""
        with self._lock:
            clock = monotonic()
            if not self._connected(clock) or self._sample_at == self._last_emitted:
                return None
            values = dict(self._snapshot.values)
            if not self._radio_fresh(clock):
                for key in OUTDOOR_FIELDS:
                    values.pop(key, None)
            self._last_emitted = self._sample_at
            return Observation(
                source_id=self._source,
                observed_at=self._sample_at,
                received_at=self._sample_at,
                measurements=values,
                rain=RainPolicy(
                    kind="daily",
                    channel_id=f"tx-{self._snapshot.transmitter}:bucket-{self._snapshot.rain_scale:.12g}",
                    timezone=self.settings.timezone,
                ),
            )

    def capabilities(self) -> tuple[ChannelCapability, ...]:
        """Advertise Vue measurements only; UV, solar and extra channels are unavailable."""
        return (ChannelCapability("primary", METRICS),)

    def status(self) -> dict[str, Any]:
        """Report radio freshness separately from datalogger connectivity without device I/O."""
        with self._lock:
            clock = monotonic()
            connected = self._connected(clock)
            radio = self._radio_fresh(clock)
            snapshot = self._snapshot
            fresh = bool(
                radio
                and snapshot
                and any(
                    snapshot.values.get(k) is not None
                    for k in ("temperature", "humidity", "wind_speed")
                )
            )
            battery = snapshot.battery if fresh else "UNKNOWN"
            return {
                "source_id": self._source,
                "enabled": self.settings.davis_enabled,
                "connected": connected,
                "observation_fresh": fresh,
                "state": "online"
                if fresh
                else "offline"
                if self.settings.davis_enabled
                else "disabled",
                "label": "Davis Vantage Vue",
                "last_error": self._error if self.settings.davis_enabled else "",
                "station": {"reporting": fresh, "battery_status": battery},
                "sensors": [
                    {
                        "sensor_id": f"tx-{snapshot.transmitter}",
                        "name": "Vantage Vue ISS",
                        "reporting": fresh,
                        "battery_status": battery,
                        "signal": None,
                    }
                ]
                if snapshot
                else [],
            }
