"""Define weather observations in Sensorius's existing storage units.

Adapters own bounded I/O, vendor parsing and freshness. Consumers persist these
values through saiDataLogger without knowing the console or gateway protocol.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass(frozen=True)
class RainPolicy:
    """Describe the source counter separately from interval rainfall in inches."""

    kind: str = "none"
    channel_id: str = "primary"
    timezone: str = "UTC"
    reset_hour: int = 0
    modulus: float | None = None

    def __post_init__(self):
        if self.kind not in {"none", "daily", "rolling", "interval", "lifetime"}:
            raise ValueError("Invalid rain policy")
        if not self.channel_id or not 0 <= self.reset_hour <= 23:
            raise ValueError("Invalid rain channel/reset hour")
        if self.kind == "rolling" and (self.modulus is None or not math.isfinite(self.modulus) or self.modulus <= 0):
            raise ValueError("Rolling rain requires a positive finite modulus")


@dataclass(frozen=True)
class ChannelCapability:
    """Describe parser-supported fields, independently of current availability."""

    channel_id: str
    metrics: tuple[str, ...]
    primary: bool = True


@dataclass
class Observation:
    """Carry a source, UTC timestamps and finite measurements; zero is valid.

    Metric labels and units are the existing Sensorius persistence contract:
    Temperature is Celsius, Temperature_F Fahrenheit, pressure hPa, wind mph,
    rain inches, relative humidity percent and direction clockwise degrees.
    Channel-specific labels remain intact. Missing/None means unavailable.
    """

    source_id: str
    observed_at: datetime
    received_at: datetime
    measurements: dict[str, Any]
    rain: RainPolicy = field(default_factory=RainPolicy)
    rain_counter: float | None = None
    rain_counter_at: datetime | None = None

    def __post_init__(self):
        if not self.source_id.strip():
            raise ValueError("Observation requires a source identity")
        for name in ("observed_at", "received_at", "rain_counter_at"):
            value = getattr(self, name)
            if value is None and name == "rain_counter_at":
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Observation timestamps must include a timezone")
            setattr(self, name, value.astimezone(timezone.utc))
        values = {}
        for key, value in self.measurements.items():
            if not isinstance(key, str) or not key.strip() or value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (ValueError, TypeError, OverflowError):
                continue
            if math.isfinite(number):
                values[key] = number
        self.measurements = values
        if self.rain_counter is not None:
            if isinstance(self.rain_counter, bool) or not math.isfinite(self.rain_counter) or self.rain_counter < 0 or self.rain_counter_at is None:
                raise ValueError("Rain counter requires a finite nonnegative value and sample time")


class WeatherAdapter(Protocol):
    """Acquire without blocking the event loop; local status/capabilities do no I/O.

    Setup/discovery and task supervision are separate from acquisition. read()
    returns None when unavailable or unchanged; consumers never fabricate zeros.
    """

    async def read(self) -> Observation | None: ...
    def capabilities(self) -> tuple[ChannelCapability, ...]: ...
    def status(self) -> dict[str, Any]: ...


async def persist_observation(logger, sensor_id: str, observation: Observation, *, unavailable_metrics=()) -> bool:
    """Write a normalized observation through the shared persistence/listener path."""
    import asyncio

    if not observation.measurements:
        return False
    values = dict.fromkeys(unavailable_metrics)
    values.update(observation.measurements)
    return await asyncio.to_thread(
        logger.log_readings, observation.observed_at.isoformat(), sensor_id, values
    )
