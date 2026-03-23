"""Helpers for persisted local host identity and channel-id normalization."""

from __future__ import annotations

import secrets
import string
from pathlib import Path

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None


_SERIAL_ALPHABET = string.ascii_lowercase + string.digits


def generate_host_serial(length: int = 6) -> str:
    return "".join(secrets.choice(_SERIAL_ALPHABET) for _ in range(max(1, int(length))))


def is_placeholder_channel_id(value: str | None, *, channel_index: int | None = None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if channel_index is None:
        return text.startswith("S") and text.endswith("-") and text.count("-") == 1
    return text == f"S{int(channel_index)}-"


def make_channel_id(channel_index: int, suffix: str) -> str:
    return f"S{int(channel_index)}-{str(suffix or '').strip()}"


def extract_local_host_id_from_sensor_id(sensor_id: str | None) -> str | None:
    text = str(sensor_id or "").strip()
    if not text:
        return None
    prefixes = ("i2c-", "spi-", "uart-")
    for prefix in prefixes:
        marker = f"-{prefix}"
        idx = text.find(marker)
        if idx < 0:
            continue
        tail = text[idx + 1 :]
        pieces = tail.split("-", 2)
        if len(pieces) < 3:
            continue
        return pieces[2].strip() or None
    return None


def resolve_persisted_host_serial(
    host_id: str,
    *,
    switch_base_dir: str | Path = "switch_settings",
    sensor_base_dir: str | Path = "sensor_settings",
) -> str:
    """Return an existing persisted host serial if present, else generate one."""
    host = str(host_id or "").strip()
    if not host:
        return generate_host_serial()

    candidates: list[Path] = []
    switch_base = Path(switch_base_dir).expanduser().resolve()
    sensor_base = Path(sensor_base_dir).expanduser().resolve()

    candidates.append(switch_base / host / "switch.toml")
    candidates.append(switch_base / f"{host}.toml")

    try:
        for child in sensor_base.iterdir():
            if not child.is_dir():
                continue
            sensor_file = child / "sensor.toml"
            if sensor_file.exists():
                candidates.append(sensor_file)
    except Exception:
        pass

    for path in candidates:
        try:
            if not path.exists() or tomllib is None:
                continue
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            sw = data.get("Switch")
            if isinstance(sw, dict):
                sid = str(sw.get("SWITCH_DEVICE_ID", "") or "").strip()
                serial = str(sw.get("DEVICE_SERIAL_NUM", "") or "").strip()
                if sid == host and serial:
                    return serial
            sensor = data.get("Sensor")
            if isinstance(sensor, dict):
                sid = str(sensor.get("SENSOR_ID", "") or "").strip()
                serial = str(sensor.get("SERIAL_NUM", "") or "").strip()
                if serial and extract_local_host_id_from_sensor_id(sid) == host:
                    return serial
        except Exception:
            continue

    return generate_host_serial()
