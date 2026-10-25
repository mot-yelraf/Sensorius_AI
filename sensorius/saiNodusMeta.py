"""Normalize Nodus discovery metadata without transport or settings access.

Keep field coercion, legacy conversion, and companion snapshot merging separate
from MQTT ingest. These helpers preserve existing payload conventions and perform
no subscriptions, network requests, runtime writes, or command processing.
"""

import copy
import re
from collections import OrderedDict


def extract_board_type(payload: dict | None) -> str:
    """Read advertised MCU identity, preserving the legacy Pico2 W fallback."""
    if not isinstance(payload, dict):
        return ""
    for key in ("mcu", "MCU", "board_type", "BOARD_TYPE", "boardtype", "BOARDTYPE", "board", "BOARD"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    legacy_type = str(payload.get("type") or payload.get("TYPE") or "").strip()
    if legacy_type and legacy_type.lower() not in {"nodus", "remote", "mqtt"}:
        return legacy_type
    return "pico2w"


def extract_sensor_hardware(payload: dict | None) -> str:
    """Prefer sensor-level hardware identity over top-level hints."""
    if not isinstance(payload, dict):
        return ""
    containers: list[dict] = []
    sensor_blob = payload.get("sensor")
    if isinstance(sensor_blob, dict):
        containers.append(sensor_blob)
    containers.append(payload)
    for container in containers:
        for key in ("hardware", "HARDWARE", "sensor_hardware", "SENSOR_HARDWARE"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


def normalize_display_metrics(raw_metrics, *, preserve_slots=False) -> list[str]:
    """
    Normalize metric hints; preserve positional slots when writing settings.
    """
    if isinstance(raw_metrics, dict):
        values = [raw_metrics.get(f"METRIC_{idx}", "") for idx in range(1, 7)]
    elif isinstance(raw_metrics, (list, tuple)):
        values = list(raw_metrics)
    else:
        return []

    if preserve_slots:
        return [str(value or "").strip() for value in values[:6]]

    ordered: list[str] = []
    seen: set[str] = set()
    for raw in values:
        metric = str(raw or "").strip()
        if not metric or metric in seen:
            continue
        seen.add(metric)
        ordered.append(metric)
        if len(ordered) >= 6:
            break
    return ordered


def normalize_display_styles(raw_styles, default_style: str = "Graph24hr") -> list[str]:
    """
    Normalize per-metric display style hints into an ordered six-slot list.
    Returns [] when the payload does not contain any usable style hints.
    """
    if isinstance(raw_styles, dict):
        values = [raw_styles.get(f"METRIC_{idx}", "") for idx in range(1, 7)]
    elif isinstance(raw_styles, (list, tuple)):
        values = list(raw_styles)
    else:
        return []

    def _canonical_style(raw_value) -> str:
        style = str(raw_value or "").strip().lower()
        if not style:
            return default_style
        if style == "gauge":
            return "Gauge"
        if style in {"graph", "graph24", "graph24hr", "24h", "24hr"}:
            return "Graph24hr"
        if style in {"graph6", "graph6hr", "6h", "6hr"}:
            return "Graph6hr"
        return default_style

    if not any(str(item or "").strip() for item in values):
        return []

    ordered: list[str] = []
    for idx in range(6):
        raw_value = values[idx] if idx < len(values) else default_style
        ordered.append(_canonical_style(raw_value))
    return ordered


def metric_slot_map(raw_values) -> OrderedDict[str, str]:
    """Copy six display slots without compacting blanks or duplicate names."""
    slots: OrderedDict[str, str] = OrderedDict(
        (f"METRIC_{idx}", "") for idx in range(1, 7)
    )
    if isinstance(raw_values, dict):
        for idx in range(1, 7):
            key = f"METRIC_{idx}"
            value = raw_values.get(key)
            if value is not None:
                slots[key] = str(value)
        return slots
    if isinstance(raw_values, (list, tuple)):
        for idx, value in enumerate(list(raw_values)[:6], start=1):
            slots[f"METRIC_{idx}"] = "" if value is None else str(value)
    return slots


def infer_sensor_device_name(raw_device, sensor_id: str | None = None) -> str:
    """
    Prefer an explicit device name; otherwise infer from <device>-<serial> sensor IDs.
    """
    device = str(raw_device or "").strip()
    if device:
        return device
    sid = str(sensor_id or "").strip().lower()
    if "-" in sid:
        prefix = sid.split("-", 1)[0].strip()
        if prefix and prefix not in {"sensor", "nodus", "remote", "mqtt"}:
            return prefix
    return ""


def extract_sensor_serial(sensor_blob: dict | None, payload: dict | None) -> str:
    """
    Accept serial from sensor-level metadata first, then top-level metadata.
    """
    if isinstance(sensor_blob, dict):
        serial = str(
            sensor_blob.get("serial")
            or sensor_blob.get("SERIAL_NUM")
            or sensor_blob.get("device_serial_num")
            or ""
        ).strip()
        if serial:
            return serial
    if isinstance(payload, dict):
        return str(
            payload.get("serial")
            or payload.get("SERIAL_NUM")
            or payload.get("device_serial_num")
            or ""
        ).strip()
    return ""


def switch_settings_block(info: dict) -> dict:
    """
    Return the Switch block from the /itaot 'info' structure.
    Accepts several shapes defensively: top-level 'Switch' dict or nested 'switch_settings' -> 'Switch'.
    """
    if not isinstance(info, dict):
        return {}

    # common shapes
    if "Switch" in info and isinstance(info["Switch"], dict):
        return info["Switch"]

    # some /itaot payloads may nest this
    maybe = info.get("switch_settings") or info.get("nodus") or {}
    if isinstance(maybe, dict) and isinstance(maybe.get("Switch"), dict):
        return maybe["Switch"]

    return {}


def extract_switch_channels(info: dict) -> list[tuple[str, str]]:
    """
    Return list of (label, channel_id) from either a Switch block or top-level SWITCH_n_LABEL keys.
    """
    channels: list[tuple[str, str]] = []

    def _scan_block(block: dict) -> None:
        for key, val in block.items():
            m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
            if not m:
                continue
            label = (val or "").strip()
            if not label:
                continue
            idx = int(m.group(1))
            ch_id = str(block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
            if ch_id:
                channels.append((label, ch_id))

    switch_blk = switch_settings_block(info)
    if isinstance(switch_blk, dict) and switch_blk:
        _scan_block(switch_blk)
    elif isinstance(info, dict):
        _scan_block(info)

    return channels


def normalize_itaot_meta(payload: dict, *, topic_device_id: str | None = None) -> dict:
    """
    Convert /itaot-meta payload (schema: itaot-meta/v1) to nodus-meta/v1 shape.
    """
    if not isinstance(payload, dict):
        return {}

    sensor_blob = payload.get("sensor") if isinstance(payload.get("sensor"), dict) else {}
    switch_blob = payload.get("switch") if isinstance(payload.get("switch"), dict) else {}
    group_blob = payload.get("location_group") if isinstance(payload.get("location_group"), dict) else {}

    device_id = str(payload.get("device_id") or topic_device_id or "").strip()
    sensor_id = str(sensor_blob.get("sensor_id") or device_id).strip()
    switch_id = str(
        switch_blob.get("switch_device_id")
        or switch_blob.get("device_id")
        or ""
    ).strip()
    location = str(
        group_blob.get("location")
        or switch_blob.get("location")
        or sensor_blob.get("location")
        or ""
    ).strip()

    channels_out = []
    raw_channels = switch_blob.get("channels")
    if isinstance(raw_channels, list):
        for idx, row in enumerate(raw_channels, start=1):
            if not isinstance(row, dict):
                continue
            channel_id = str(row.get("channel_id") or "").strip()
            if not channel_id:
                continue
            try:
                channel_idx = int(row.get("index"))
            except Exception:
                channel_idx = idx
            channels_out.append({
                "index": channel_idx,
                "label": str(row.get("label") or channel_id).strip() or channel_id,
                "channel_id": channel_id,
                "state": row.get("state"),
                "event_topic": f"nodus/{channel_id}/event",
                "state_topic": f"nodus/{channel_id}/state",
                "set_topic": f"nodus/{channel_id}/config/set",
                "availability_topic": f"nodus/{channel_id}/availability",
            })

    return {
        "schema": "nodus-meta/v1",
        "device_id": device_id,
        "mcu": extract_board_type(payload),
        "sensor": {
            "sensor_id": sensor_id,
            "device": infer_sensor_device_name(sensor_blob.get("device"), sensor_id),
            "serial": extract_sensor_serial(sensor_blob, payload),
            "hardware": extract_sensor_hardware(payload),
            "location": str(sensor_blob.get("location") or location).strip(),
            "data_topic": f"nodus/{sensor_id}/data" if sensor_id else "",
            "event_topic": f"nodus/{sensor_id}/event" if sensor_id else "",
            "availability_topic": f"nodus/{sensor_id}/availability" if sensor_id else "",
            "display_metrics": normalize_display_metrics(
                sensor_blob.get("display_metrics") or sensor_blob.get("metrics")
            ),
            "display_styles": normalize_display_styles(
                sensor_blob.get("display_styles") or sensor_blob.get("styles")
            ),
        },
        "switch": {
            "switch_device_id": switch_id,
            "location": str(switch_blob.get("location") or location).strip(),
            "channels": channels_out,
        },
        "location_group": {
            "location": location,
            "members": group_blob.get("members") if isinstance(group_blob.get("members"), list) else [],
        },
    }


def prepare_nodus_meta(meta: dict) -> dict | None:
    """Validate the compact schema and copy/redact legacy Wi-Fi credentials."""
    if not isinstance(meta, dict):
        return None

    # Wi-Fi credentials are operational inputs, not discovery metadata.
    # Keep received values out of the long-lived discovery cache and
    # Nodus shadow settings even when older firmware includes it in meta.
    meta = copy.deepcopy(meta)
    network_for_redaction = meta.get("network")
    if isinstance(network_for_redaction, dict):
        network_for_redaction.pop("ssid", None)
        network_for_redaction.pop("SSID", None)
        network_for_redaction.pop("password", None)
        network_for_redaction.pop("PASSWORD", None)

    schema = str(meta.get("schema") or "").strip().lower()
    if schema and schema != "nodus-meta/v1":
        return None
    return meta


def metadata_topic(raw_topic: str | None, base_topic: str) -> str:
    """Accept native Nodus topics and the configured mirrored prefix."""
    t = str(raw_topic or "").strip()
    if not t:
        return ""
    if t.startswith("nodus/"):
        return t
    if base_topic and t.startswith(f"{base_topic}/nodus/"):
        return t
    return ""


def is_unknown_location(val: str | None) -> bool:
    """Recognize the established placeholder locations."""
    v = (val or "").strip().lower()
    return v in ("", "unknown", "n/a", "na", "none", "-")


def pick_location(*vals: str) -> str:
    """Choose the first meaningful location in priority order."""
    for raw in vals:
        loc = str(raw or "").strip()
        if loc and not is_unknown_location(loc):
            return loc
    return "Unknown"


def coerce_switch_state(raw_state) -> bool | None:
    """Interpret advertised switch state using existing metadata aliases."""
    if isinstance(raw_state, bool):
        return raw_state
    txt = str(raw_state or "").strip().lower()
    if txt in ("on", "1", "true", "t", "yes", "y"):
        return True
    if txt in ("off", "0", "false", "f", "no", "n"):
        return False
    return None


def sensor_metadata_blocks(meta: dict, device_id: str) -> tuple[dict, list[dict]]:
    """Select child sensor views, falling back to primary or legacy top-level fields."""
    primary_sensor_blob = dict(meta.get("sensor")) if isinstance(meta.get("sensor"), dict) else {}
    if not primary_sensor_blob:
        top_sensor_id = str(meta.get("sensor_id") or meta.get("SENSOR_ID") or "").strip()
        top_data_topic = str(meta.get("data_topic") or meta.get("mqtt_sensor_topic") or "").strip()
        top_has_sensor = bool(
            top_sensor_id
            or top_data_topic
            or meta.get("display_metrics")
            or meta.get("metrics")
        )
        if top_has_sensor:
            primary_sensor_blob = {
                "sensor_id": top_sensor_id or device_id,
                "device": meta.get("device") or meta.get("DEVICE") or meta.get("SENSOR_DEVICE") or "",
                "type": meta.get("type") or meta.get("TYPE") or "nodus",
                "location": meta.get("location") or meta.get("LOCATION") or "",
                "serial": meta.get("serial") or meta.get("SERIAL_NUM") or "",
                "data_topic": top_data_topic,
                "availability_topic": meta.get("availability_topic") or meta.get("mqtt_availability_topic") or "",
                "event_topic": meta.get("event_topic") or meta.get("mqtt_event_topic") or "",
                "hardware": (
                    meta.get("hardware")
                    or meta.get("HARDWARE")
                    or meta.get("sensor_hardware")
                    or meta.get("SENSOR_HARDWARE")
                    or ""
                ),
                "display_metrics": meta.get("display_metrics") or meta.get("metrics") or meta.get("Display") or [],
                "display_styles": meta.get("display_styles") or meta.get("styles") or [],
            }
    sensor_blobs: list[dict] = []
    raw_sensors = meta.get("sensors")
    if isinstance(raw_sensors, list):
        for item in raw_sensors:
            if isinstance(item, dict):
                sensor_blobs.append(dict(item))
    if not sensor_blobs and primary_sensor_blob:
        sensor_blobs.append(primary_sensor_blob)
    return primary_sensor_blob, sensor_blobs


def merge_config_meta(payload: dict, previous_meta, *, device_id: str) -> dict:
    """Merge a validated saved-settings companion into a copied discovery snapshot."""
    meta = copy.deepcopy(previous_meta or {})
    meta.update(schema="nodus-meta/v1", device_id=device_id)
    sensor = payload.get("sensor")
    if isinstance(sensor, dict):
        existing = meta.get("sensor") or {}
        if existing.get("sensor_id") != sensor.get("sensor_id"):
            existing = {}
        meta["sensor"] = {**existing, **copy.deepcopy(sensor)}
        for child in meta.get("sensors", []):
            if isinstance(child, dict) and child.get("sensor_id") == sensor.get("sensor_id"):
                child.update(copy.deepcopy(sensor))
    for field in ("time", "homeassistant"):
        if isinstance(payload.get(field), dict):
            meta[field] = copy.deepcopy(payload[field])
    return meta


def merge_switch_meta(payload: dict, previous_meta, *, device_id: str) -> dict:
    """Merge a validated switch companion, retaining existing identity/location rules."""
    channels = payload.get("channels")
    compact = copy.deepcopy(previous_meta or {})
    if not isinstance(compact, dict):
        compact = {}

    compact["schema"] = "nodus-meta/v1"
    compact["device_id"] = str(compact.get("device_id") or device_id).strip() or device_id
    compact.setdefault("location_group", {})
    if not isinstance(compact.get("location_group"), dict):
        compact["location_group"] = {}

    old_switch = compact.get("switch") if isinstance(compact.get("switch"), dict) else {}
    switch_id = str(
        payload.get("switch_device_id")
        or old_switch.get("switch_device_id")
        or old_switch.get("device_id")
        or ""
    ).strip()
    location = str(payload.get("location") or old_switch.get("location") or "").strip()

    switch_blob = dict(old_switch)
    if switch_id:
        switch_blob["switch_device_id"] = switch_id
        switch_blob.setdefault("device_id", switch_id)
    if location:
        switch_blob["location"] = location
    switch_blob["channel_count"] = payload.get("channel_count", len(channels))
    switch_blob["channels"] = channels
    compact["switch"] = switch_blob

    members = compact["location_group"].get("members")
    if not isinstance(members, list):
        members = []
    for member in (device_id, switch_id):
        if member and member not in members:
            members.append(member)
    compact["location_group"]["members"] = members
    if location and not str(compact["location_group"].get("location") or "").strip():
        compact["location_group"]["location"] = location
    return compact
