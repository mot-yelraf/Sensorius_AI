"""Validate weather-console setup and run bounded, temporary retrieval probes.

Test uses the same adapter commands as supervised ingestion without saving
configuration, readings, or rain baselines. Callers hold their acquisition lock
until the worker has closed its handles, including when a request is cancelled.
"""

import asyncio
from types import SimpleNamespace

from .weather_adapters.acurite_models import console_profile
from .weather_adapters.davis import BAUD_RATES, validate_port
from .weather_adapters.wmr100 import normalize_model


def console_configuration(kind: str, payload: dict, *, timezone: str = "UTC") -> tuple[list, SimpleNamespace]:
    """Validate form values once for both Save and a temporary Test adapter."""
    if not isinstance(payload, dict):
        raise ValueError("Expected an AcuRite configuration object." if kind == "acurite" else "Expected a console configuration object.")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("Enabled must be a boolean.")
    interval = payload.get("poll_interval_sec", 60)
    if isinstance(interval, bool) or kind != "acurite" and not str(interval).isdigit():
        raise ValueError("Data retrieval interval must be 60–3600 seconds.")
    try:
        interval = int(interval)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Data retrieval interval must be 60–3600 seconds.") from exc
    if not 60 <= interval <= 3600:
        raise ValueError("Data retrieval interval must be 60–3600 seconds.")
    if kind == "davis":
        port = validate_port(payload.get("port", ""))
        if not port:
            raise ValueError("A Davis USB serial port is required.")
        baud = payload.get("baud_rate", 19200)
        if isinstance(baud, bool) or str(baud) not in {str(value) for value in BAUD_RATES}:
            raise ValueError("Unsupported Davis baud rate")
        return [("DavisVue", "PORT", port), ("DavisVue", "BAUD_RATE", int(baud)),
                ("DavisVue", "POLL_INTERVAL_SEC", interval), ("DavisVue", "ENABLED", enabled)], SimpleNamespace(
                    davis_enabled=enabled, davis_port=port, davis_baud_rate=int(baud),
                    davis_poll_interval_sec=interval, timezone=timezone)
    if kind not in {"acurite", "oregon"}:
        raise ValueError("Unsupported USB console family.")
    model = console_profile(payload.get("model", "01536")).key if kind == "acurite" else normalize_model(payload.get("model", "WMR100"))
    serial = payload.get("usb_serial", "")
    if not isinstance(serial, str) or len(serial) > 128 or any(ord(char) < 32 for char in serial):
        raise ValueError("Invalid USB serial number.")
    section = "AcuRite" if kind == "acurite" else "OregonScientific"
    prefix = "acurite" if kind == "acurite" else "wmr100"
    config = SimpleNamespace(timezone=timezone, **{
        f"{prefix}_enabled": enabled, f"{prefix}_model": model, f"{prefix}_usb_serial": serial.strip(),
        f"{prefix}_poll_interval_sec": interval,
    })
    return [(section, "MODEL", model), (section, "USB_SERIAL", serial.strip()),
            (section, "POLL_INTERVAL_SEC", interval), (section, "ENABLED", enabled)], config


def probe_adapter(adapter) -> bool:
    """Require a decoded observation from one new adapter acquisition session."""
    try:
        adapter.sample_once()
        observation = adapter.read()
        return bool(observation is not None and observation.measurements)
    finally:
        reset = getattr(adapter, "reset", None)
        if callable(reset):
            reset()


async def run_console_probe(probe) -> bool:
    """Keep a bounded worker alive until cleanup before releasing its caller's lock."""
    task = asyncio.create_task(asyncio.to_thread(probe))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
