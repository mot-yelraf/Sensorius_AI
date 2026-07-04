"""Low-stack calibration apply handling for sensor offset updates."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic


def process_calibration_apply_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Handle simple calibration apply payloads without the full command stack."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and runtime_config.sensor.present
        and topic == mqtt_topic(runtime_config, device_id, "calibration", "set")
    ):
        return None

    parsed = _parse_calibration_apply(payload_text)
    if parsed is None:
        return None
    message_id, updates = parsed
    if not updates:
        return None

    duplicate = bool(message_id and message_id in tuple(handled_message_ids or ()))
    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")

    if duplicate:
        _publish_calibration_ack(transport, ack_topic, message_id)
        _publish_calibration_result(
            transport,
            result_topic,
            message_id,
            applied=True,
            updated=0,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=message_id,
            duplicate=True,
        )

    try:
        updated_runtime_config = _calibration_runtime_config(runtime_config, updates)
        meta_patch_payload = _build_meta_patch_payload(
            updated_runtime_config,
            source="calibration_set",
            message_id=message_id,
            updates=updates,
        )
    except RuntimeError as exc:
        if "pystack exhausted" not in str(exc).lower():
            raise
        _publish_calibration_ack(transport, ack_topic, message_id)
        _publish_calibration_result(
            transport,
            result_topic,
            message_id,
            applied=False,
            updated=0,
            error="pystack_exhausted",
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="calibration",
            published_count=2,
            errors=("pystack_exhausted",),
            runtime_config=runtime_config,
            message_id=message_id,
            persistence_mode="volatile",
        )

    _publish_calibration_ack(transport, ack_topic, message_id)
    _publish_calibration_result(
        transport,
        result_topic,
        message_id,
        applied=True,
        updated=len(updates),
    )
    transport.publish(
        mqtt_topic(updated_runtime_config, device_id, "meta", "patch"),
        meta_patch_payload,
        retain=False,
    )
    persistence_errors = _persist_calibration_updates_fast(
        updated_runtime_config,
        updates,
        settings_root=settings_root,
    )
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="calibration",
        published_count=3,
        errors=persistence_errors,
        runtime_config=updated_runtime_config,
        message_id=message_id,
        persistence_mode=_persistence_mode(settings_root, persistence_errors),
    )


def _parse_calibration_apply(payload_text):
    try:
        import json

        payload = json.loads(str(payload_text or "").strip())
    except (ImportError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    action = str(payload.get("action", "") or "").strip().lower()
    if not message_id or action not in {"apply", "set", "update"}:
        return None
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        return None
    updates = _calibration_updates_from_body(body)
    if updates is None:
        return None
    return message_id, tuple(updates)


def _calibration_updates_from_body(body):
    if isinstance(body.get("offsets"), list):
        updates = []
        for item in body["offsets"]:
            if not isinstance(item, dict):
                continue
            section, key = _normalize_calibration_key(item.get("key"))
            if not _supported_calibration_key(section, key):
                return None
            updates.append(
                {
                    "section": section,
                    "key": key,
                    "value": item.get("value"),
                }
            )
        return updates

    calibration = body.get("calibration")
    if isinstance(calibration, dict):
        updates = []
        for branch, values in calibration.items():
            section = _calibration_section_for_branch(branch)
            if not section or not isinstance(values, dict):
                return None
            for key, value in values.items():
                key = str(key or "").strip().upper()
                target_section = section
                if target_section == "Calibration.System" and key == "ALTITUDE_METERS":
                    target_section = "Calibration.Device"
                if (
                    target_section == "Calibration.Device"
                    and key == "SOIL_TEMP_MOIST_VAL"
                ):
                    key = "SOIL_MOIST_CAL_VAL"
                if not _supported_calibration_key(target_section, key):
                    return None
                updates.append(
                    {"section": target_section, "key": key, "value": value}
                )
        return updates
    return None


def _persist_calibration_updates_fast(runtime_config, updates, *, settings_root=None):
    if settings_root is None:
        return ()
    try:
        from cpynodus_ii.features.calibration_persistence import (
            persist_calibration_updates,
        )
    except MemoryError:
        return ("calibration_persist_import_memory",)
    try:
        return persist_calibration_updates(
            runtime_config,
            updates,
            settings_root=settings_root,
        )
    except MemoryError:
        return ("calibration_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("calibration_persist_pystack",)
        raise


def _persistence_mode(settings_root, errors):
    if settings_root is None:
        return ""
    return "volatile" if errors else "persisted"


def _calibration_runtime_config(runtime_config, updates):
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        if section not in {"Calibration.System", "Calibration.Device"}:
            continue
        key = str(update.get("key", "") or "").strip().upper()
        try:
            value = float(update.get("value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if section == "Calibration.System":
            _set_calibration_value(runtime_config.sensor.calibration_system, key, value)
        else:
            _set_calibration_value(runtime_config.sensor.calibration_device, key, value)
    return runtime_config


def _set_calibration_value(calibration, key, value):
    if key == "TEMP_OFFSET":
        calibration.temp_offset = value
    elif key == "RH_OFFSET":
        calibration.rh_offset = value
    elif key == "CO2_OFFSET":
        calibration.co2_offset = value
    elif key == "AQI_OFFSET":
        calibration.aqi_offset = value
    elif key == "GAS_OFFSET":
        calibration.gas_offset = value
    elif key == "LUX_OFFSET":
        calibration.lux_offset = value
    elif key == "PPFD_OFFSET":
        calibration.ppfd_offset = value
    elif key == "APVPD_TEMP_CAL_VAL":
        calibration.apvpd_temp_cal_val = value
    elif key == "APVPD_RH_CAL_VAL":
        calibration.apvpd_rh_cal_val = value
    elif key == "ALTITUDE_METERS":
        calibration.altitude_meters = value
    elif key == "SOIL_TEMP_CAL_VAL":
        calibration.soil_temp_cal_val = value
    elif key in {"SOIL_MOIST_CAL_VAL", "SOIL_TEMP_MOIST_VAL"}:
        calibration.soil_moist_cal_val = value
    elif key == "SOIL_PH_CAL_VAL":
        calibration.soil_ph_cal_val = value
    elif key == "SOIL_EC_CAL_VAL":
        calibration.soil_ec_cal_val = value


def _normalize_calibration_key(key):
    text = str(key or "").strip()
    if text == "soil_ph_offset":
        return "Calibration.Device", "SOIL_PH_CAL_VAL"
    if text == "soil_moisture_offset":
        return "Calibration.Device", "SOIL_MOIST_CAL_VAL"
    parts = text.split(".")
    if len(parts) >= 3:
        section = ".".join(parts[:-1])
        key = parts[-1].upper()
        if section == "Calibration.System" and key == "ALTITUDE_METERS":
            return "Calibration.Device", key
        if section == "Calibration.Device" and key == "SOIL_TEMP_MOIST_VAL":
            return section, "SOIL_MOIST_CAL_VAL"
        return section, key
    return "", ""


def _calibration_section_for_branch(branch):
    normalized = str(branch or "").strip().lower()
    if normalized == "system":
        return "Calibration.System"
    if normalized == "device":
        return "Calibration.Device"
    if normalized == "soil":
        return "Calibration.Device"
    if normalized == "apvpd":
        return "Calibration"
    return ""


def _supported_calibration_key(section, key):
    section = str(section or "").strip()
    key = str(key or "").strip()
    return bool(key and section in {
        "Calibration.System",
        "Calibration.Device",
        "Calibration",
    })


def _publish_calibration_ack(transport, topic, message_id, *, accepted=True):
    transport.publish(
        topic,
        {
            "message_id": str(message_id or ""),
            "accepted": bool(accepted),
        },
        retain=False,
    )


def _publish_calibration_result(
    transport,
    topic,
    message_id,
    *,
    applied,
    updated=0,
    error="",
    duplicate=False,
):
    transport.publish(
        topic,
        {
            "message_id": str(message_id or ""),
            "applied": bool(applied),
            "updated": int(updated or 0),
            "duplicate": bool(duplicate),
            "error": str(error or ""),
        },
        retain=False,
    )


def _build_meta_patch_payload(runtime_config, *, source, message_id, updates):
    normalized_updates = []
    sections = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        if not (section and key):
            continue
        if section not in sections:
            sections.append(section)
        normalized_updates.append(
            {
                "section": section,
                "key": key,
                "value": update.get("value"),
            }
        )
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    return {
        "schema": "nodus-meta-patch/v1",
        "device_id": _device_id(runtime_config),
        "timestamp": timestamp,
        "source": str(source or ""),
        "message_id": str(message_id or ""),
        "sections": sections,
        "updates": normalized_updates,
    }


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )
