"""Oregon Scientific WMR100-family live USB packets, independently decoded.

Wire references (protocol facts, not imported driver code):
https://github.com/ejeklint/WLoggerDaemon/blob/master/Station_protocol.md
https://weewx.com/docs/5.5/hardware/drivers/ (compatible console families).
Rain uses hundredths of an inch, as corrected by WeeWX's WMR100 field notes.
"""

from .diagnostics import report_error
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from sensorius.weather_adapters.observations import ChannelCapability, Observation, RainPolicy


USB_VENDOR, USB_PRODUCT = 0x0FDE, 0xCA01
MODELS = ("WMR100", "WMR100N", "WMR88", "WMR88A", "WMR180", "WMR180A", "WMRS200")
ACTIVATE = bytes.fromhex("20 00 08 01 00 00 00 00")
MAX_AGE_SECONDS = 180
METRICS = (
    "temperature",
    "humidity",
    "indoor_temperature",
    "indoor_humidity",
    "absolute_pressure",
    "pressure",
    "wind_speed",
    "wind_gust",
    "wind_dir",
    "uv",
    "rain_rate",
    "rain_increment",
    "rain_total",
)


class Wmr100Error(RuntimeError):
    """An operator-visible protocol or transport error."""


def normalize_model(value: str) -> str:
    """Normalize supported console labels without inferring a model from USB IDs."""
    if not isinstance(value, str) or value.strip().upper() not in MODELS:
        raise ValueError("Unsupported WMR100-family console model")
    return value.strip().upper()


@dataclass(frozen=True)
class SensorPacket:
    """One sensor update; the rain counter is separate from daily rainfall."""

    channel: str
    values: dict[str, float | None]
    battery: str = "UNKNOWN"
    counter: float | None = None
    reset_id: str = ""


class PacketStream:
    """Reassemble count-prefixed USB reports with bounded storage and delimiter sync."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.synced = False

    def feed(self, report: bytes) -> list[bytes]:
        """Return complete packets; reject malformed reports and retain split delimiters."""
        if len(report) != 8 or report[0] > 7:
            self.__init__()
            raise Wmr100Error("Malformed WMR100 USB report")
        self.buffer.extend(report[1 : 1 + report[0]])
        packets = []
        while (end := self.buffer.find(b"\xff\xff")) >= 0:
            packet = bytes(self.buffer[:end])
            del self.buffer[: end + 2]
            if self.synced and packet:
                packets.append(packet)
            self.synced = True
        if len(self.buffer) > 32:
            # A missing delimiter must not grow memory or join unrelated packets.
            trailing_ff = self.buffer[-1:] == b"\xff"
            self.buffer = bytearray(b"\xff" if trailing_ff else b"")
            self.synced = False
            raise Wmr100Error("WMR100 packet stream lost synchronization")
        return packets


def decode_packet(packet: bytes) -> SensorPacket | None:
    """Validate framing/checksum and decode supported primary-channel weather fields."""
    if (
        len(packet) < 4
        or len(packet) > 17
        or sum(packet[:-2]) != int.from_bytes(packet[-2:], "little")
    ):
        raise Wmr100Error("Invalid WMR100 packet checksum or length")
    kind = packet[1]
    lengths = {0x41: 17, 0x42: 12, 0x46: 8, 0x47: 6, 0x48: 11, 0x60: 12}
    if kind not in lengths:
        return None
    if len(packet) != lengths[kind]:
        raise Wmr100Error("Unexpected WMR100 measurement length")
    battery = {0: "OK", 4: "LOW"}.get(packet[0] >> 4, "UNKNOWN")
    if kind == 0x42:
        battery = "LOW" if packet[0] & 0x40 else "OK"
        channel = packet[2] & 15
        if channel not in {0, 1}:
            return None  # Extra channels need independent storage/UI support.
        magnitude = int.from_bytes(bytes((packet[3], packet[4] & 0x7F)), "little") / 10
        celsius = -magnitude if packet[4] & 0x80 else magnitude
        indoor = channel == 0
        prefix = "indoor_" if indoor else ""
        return SensorPacket(
            "indoor" if indoor else "outdoor",
            {
                prefix + "temperature": celsius * 1.8 + 32
                if -60 <= celsius <= 85
                else None,
                prefix + "humidity": float(packet[5]) if packet[5] <= 100 else None,
            },
            battery,
        )
    if kind == 0x48:
        speed = ((packet[6] << 4) | (packet[5] >> 4)) / 10
        gust = (((packet[5] & 15) << 8) | packet[4]) / 10
        return SensorPacket(
            "wind",
            {
                "wind_speed": speed / 0.44704 if speed <= 100 else None,
                "wind_gust": gust / 0.44704 if speed <= gust <= 100 else None,
                "wind_dir": (packet[2] & 15) * 22.5,
            },
            battery,
        )
    if kind == 0x46:
        hpa = ((packet[3] & 15) << 8) | packet[2]
        # Use raw station pressure. Console relative-pressure calibration varies.
        return SensorPacket(
            "pressure",
            {"absolute_pressure": hpa / 33.8638866667 if 300 <= hpa <= 1100 else None},
        )
    if kind == 0x47:
        return SensorPacket(
            "uv", {"uv": float(packet[3]) if packet[3] <= 25 else None}, battery
        )
    if kind == 0x41:
        # Reset date is part of counter provenance. Do not call rolling 24h rain "today".
        return SensorPacket(
            "rain",
            {"rain_rate": int.from_bytes(packet[2:4], "little") / 100},
            battery,
            int.from_bytes(packet[8:10], "little") / 100,
            packet[10:15].hex(),
        )
    return None  # Console clock is not a trustworthy timezone-aware sample timestamp.


class Wmr100UsbAdapter:
    """Continuously collect independent sensors; persistence reads only the fresh cache."""

    def __init__(self, settings: Any, transport: Any = None) -> None:
        self.settings, self.transport = settings, transport
        self._owned_transport = None
        self._io_lock = threading.Lock()
        self._lock = threading.RLock()
        self._stream = PacketStream()
        self._samples = {}
        self._sensors = {}
        self._counter = None
        self._identity = ""
        self._serial = settings.wmr100_usb_serial
        self._connected = False
        self._error = ""
        self._last_emitted = None

    def _clear(self) -> None:
        self._samples.clear()
        self._sensors.clear()
        self._counter = None
        self._connected = False
        self._stream = PacketStream()

    def reset(self) -> None:
        """Release USB ownership and discard cached fields after connection changes."""
        with self._io_lock:
            try:
                if self._owned_transport is not None:
                    self._owned_transport.close()
            finally:
                self._owned_transport = None
                with self._lock:
                    self._clear()
                    self._error = ""

    def sample_once(self) -> None:
        """Consume one bounded batch off the event loop; never hold the cache lock for I/O."""
        with self._io_lock:
            serial = self.settings.wmr100_usb_serial
            if not self.settings.wmr100_enabled:
                if self._owned_transport is not None:
                    self._owned_transport.close()
                    self._owned_transport = None
                with self._lock:
                    self._clear()
                return
            try:
                if self.transport is None:
                    from sensorius.weather_adapters.wmr100_usb import console_transport

                    if self._owned_transport is None or serial != self._serial:
                        if self._owned_transport is not None:
                            self._owned_transport.close()
                        self._owned_transport = console_transport(serial)
                    transport = self._owned_transport
                else:
                    transport = self.transport
                identity, reports = transport.read_batch()
                now, clock = datetime.now(timezone.utc), monotonic()
                with self._lock:
                    if (
                        not self.settings.wmr100_enabled
                        or serial != self.settings.wmr100_usb_serial
                    ):
                        self._clear()
                        return
                    if identity != self._identity or serial != self._serial:
                        self._clear()
                        self._identity, self._serial = identity, serial
                    if not getattr(transport, "continuous", True):
                        self._stream = PacketStream()
                    self._connected, self._error = True, ""
                    for report in reports:
                        try:
                            packets = self._stream.feed(report)
                        except Wmr100Error as exc:
                            self._error = str(exc)
                            continue
                        for packet in packets:
                            try:
                                self._accept(decode_packet(packet), now, clock)
                            except Wmr100Error as exc:
                                self._error = str(exc)
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, Wmr100Error)
                    else "WMR100 USB acquisition failed; check device access and server logs."
                )
                if not isinstance(exc, Wmr100Error):
                    report_error("WMR100 USB acquisition failed")
                with self._lock:
                    if message != self._error:
                        report_error("WMR100: %s", message)
                    self._clear()
                    self._error = message
                if self._owned_transport is not None:
                    self._owned_transport.close()
                    self._owned_transport = None

    def _accept(self, packet, now, clock) -> None:
        if packet is None:
            return
        self._sensors[packet.channel] = (
            clock,
            packet.battery,
            any(v is not None for v in packet.values.values()),
        )
        for key, value in packet.values.items():
            self._samples[key] = (value, now, clock)
        if packet.channel == "rain":
            self._counter = (packet.counter, now, clock, packet.reset_id)

    def read(self) -> Observation | None:
        """Return each fresh snapshot once, with an independently timestamped rain counter."""
        with self._lock:
            if (
                not self.settings.wmr100_enabled
                or not self._connected
                or self._serial != self.settings.wmr100_usb_serial
            ):
                return None
            clock = monotonic()
            samples = {
                key: item
                for key, item in self._samples.items()
                if clock - item[2] <= MAX_AGE_SECONDS
            }
            if not any(item[0] is not None for item in samples.values()):
                return None
            at = max(item[1] for item in samples.values())
            if self._last_emitted is not None and at <= self._last_emitted:
                return None
            self._last_emitted = at
            values = {key: item[0] for key, item in samples.items()}
            counter = (
                self._counter
                if self._counter and clock - self._counter[2] <= MAX_AGE_SECONDS
                else None
            )
            return Observation(
                source_id=f"wmr100:{self._identity}:outdoor-1",
                observed_at=at,
                received_at=at,
                measurements=values,
                rain=RainPolicy(
                    kind="rolling",
                    channel_id=f"rain:{counter[3] if counter else 'unavailable'}",
                    timezone=self.settings.timezone,
                    modulus=655.36,
                ),
                rain_counter=counter[0] if counter else None,
                rain_counter_at=counter[1] if counter else None,
            )

    def capabilities(self) -> tuple[ChannelCapability, ...]:
        """Declare the primary fields supported by this decoder, independent of hardware."""
        return (ChannelCapability("primary", METRICS),)

    def status(self) -> dict[str, Any]:
        """Report console connection and individual sensor freshness without USB access."""
        with self._lock:
            enabled = self.settings.wmr100_enabled
            connected = (
                enabled
                and self._connected
                and self._serial == self.settings.wmr100_usb_serial
            )
            clock = monotonic()
            sensors = [
                {
                    "sensor_id": key,
                    "name": f"Oregon Scientific {key}",
                    "reporting": bool(
                        connected and valid and clock - at <= MAX_AGE_SECONDS
                    ),
                    "battery_status": battery
                    if connected and clock - at <= MAX_AGE_SECONDS
                    else "UNKNOWN",
                    "signal": None,
                }
                for key, (at, battery, valid) in self._sensors.items()
            ]
            outdoor = [
                s
                for s in sensors
                if s["sensor_id"] in {"outdoor", "wind", "rain", "uv"}
                and s["reporting"]
            ]
            fresh = bool(outdoor)
            batteries = [s["battery_status"] for s in outdoor]
            battery = (
                "LOW"
                if "LOW" in batteries
                else "OK"
                if batteries and all(b == "OK" for b in batteries)
                else "UNKNOWN"
            )
            return {
                "source_id": f"wmr100:{self._identity}:outdoor-1",
                "enabled": enabled,
                "connected": bool(connected),
                "observation_fresh": fresh,
                "state": "online" if fresh else "offline" if enabled else "disabled",
                "label": f"Oregon Scientific {self.settings.wmr100_model} USB",
                "configured_model": self.settings.wmr100_model,
                "last_error": self._error if enabled else "",
                "station": {"reporting": fresh, "battery_status": battery},
                "sensors": sensors,
            }
