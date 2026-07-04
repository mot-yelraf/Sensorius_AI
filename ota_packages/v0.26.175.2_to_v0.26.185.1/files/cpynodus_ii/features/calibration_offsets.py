"""Small single-offset calibration apply handler."""

from cpynodus_ii.features.command_models import CommandResult
from cpynodus_ii.features.topics import mqtt_topic

_KEY_ATTRS = {
    "TEMP_OFFSET": "temp_offset",
    "RH_OFFSET": "rh_offset",
    "CO2_OFFSET": "co2_offset",
    "AQI_OFFSET": "aqi_offset",
    "GAS_OFFSET": "gas_offset",
    "LUX_OFFSET": "lux_offset",
    "PPFD_OFFSET": "ppfd_offset",
    "APVPD_TEMP_CAL_VAL": "apvpd_temp_cal_val",
    "APVPD_RH_CAL_VAL": "apvpd_rh_cal_val",
    "ALTITUDE_METERS": "altitude_meters",
    "SOIL_TEMP_CAL_VAL": "soil_temp_cal_val",
    "SOIL_MOIST_CAL_VAL": "soil_moist_cal_val",
    "SOIL_TEMP_MOIST_VAL": "soil_moist_cal_val",
    "SOIL_PH_CAL_VAL": "soil_ph_cal_val",
    "SOIL_EC_CAL_VAL": "soil_ec_cal_val",
}


def process_calibration_offsets_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    handled_message_ids=(),
    settings_root=None,
):
    """Apply one ``payload.offsets`` calibration command."""
    device_id = _device_id(runtime_config)
    if not (
        device_id
        and runtime_config.sensor.present
        and topic == mqtt_topic(runtime_config, device_id, "calibration", "set")
    ):
        return None

    from cpynodus_ii.features.calibration_offset_parse import (
        parse_single_offset_payload,
    )

    parsed = parse_single_offset_payload(payload_text)
    if parsed is None:
        return None
    message_id, update = parsed
    if not (message_id and update):
        return None

    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")
    if _contains_message_id(handled_message_ids, message_id):
        _publish_ack(transport, ack_topic, message_id)
        _publish_result(
            transport,
            result_topic,
            message_id,
            applied=True,
            duplicate=True,
        )
        return _command_result(
            topic,
            runtime_config,
            message_id,
            published_count=2,
            duplicate=True,
            requested_state="offset_fast:duplicate",
        )

    try:
        updated_runtime_config = _apply_runtime_offset(runtime_config, update)
        meta_payload = _build_meta_patch_payload(
            updated_runtime_config,
            message_id,
            update,
        )
    except MemoryError:
        _publish_ack(transport, ack_topic, message_id)
        _publish_result(
            transport,
            result_topic,
            message_id,
            applied=False,
            error="calibration_runtime_memory",
        )
        return _command_result(
            topic,
            runtime_config,
            message_id,
            phase="error",
            published_count=2,
            errors=("calibration_runtime_memory",),
            persistence_mode="volatile",
            requested_state="offset_fast:runtime",
        )

    _publish_ack(transport, ack_topic, message_id)
    _publish_result(transport, result_topic, message_id, applied=True, updated=1)
    transport.publish(
        mqtt_topic(updated_runtime_config, device_id, "meta", "patch"),
        meta_payload,
        retain=False,
    )
    persistence_errors = _persist_single_offset(
        updated_runtime_config,
        update,
        settings_root,
    )
    return _command_result(
        topic,
        updated_runtime_config,
        message_id,
        published_count=3,
        errors=persistence_errors,
        persistence_mode=_persistence_mode(settings_root, persistence_errors),
        requested_state="offset_fast:applied",
    )


def _persist_single_offset(runtime_config, update, settings_root):
    if settings_root is None:
        return ()
    try:
        from cpynodus_ii.features.calibration_offset_persistence import (
            persist_single_calibration_offset,
        )
    except MemoryError:
        return ("calibration_offset_persist_import_memory",)
    try:
        return persist_single_calibration_offset(
            runtime_config,
            update,
            settings_root=settings_root,
        )
    except MemoryError:
        return ("calibration_offset_persist_memory",)
    except RuntimeError as exc:
        if "pystack exhausted" in str(exc).lower():
            return ("calibration_offset_persist_pystack",)
        raise


def _persistence_mode(settings_root, errors):
    if settings_root is None:
        return ""
    return "volatile" if errors else "persisted"


def _apply_runtime_offset(runtime_config, update):
    key = str(update.get("key", "") or "").strip().upper()
    attr = _KEY_ATTRS.get(key)
    if not attr:
        return runtime_config
    value = _float_value(update.get("value"))
    if update.get("section") == "Calibration.System":
        setattr(runtime_config.sensor.calibration_system, attr, value)
    else:
        setattr(runtime_config.sensor.calibration_device, attr, value)
    return runtime_config


def _publish_ack(transport, topic, message_id):
    transport.publish(
        topic,
        {"message_id": str(message_id or ""), "accepted": True},
        retain=False,
    )


def _publish_result(
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


def _build_meta_patch_payload(runtime_config, message_id, update):
    section = str(update.get("section", "") or "").strip()
    key = str(update.get("key", "") or "").strip()
    try:
        from time import time

        timestamp = int(time())
    except Exception:
        timestamp = 0
    return {
        "schema": "nodus-meta-patch/v1",
        "device_id": _device_id(runtime_config),
        "timestamp": timestamp,
        "source": "calibration_set",
        "message_id": str(message_id or ""),
        "sections": [section],
        "updates": [{"section": section, "key": key, "value": update.get("value")}],
    }


def _command_result(
    topic,
    runtime_config,
    message_id,
    *,
    phase="published",
    published_count=0,
    errors=(),
    duplicate=False,
    persistence_mode="",
    requested_state="",
):
    return CommandResult(
        phase=phase,
        topic=topic,
        command_type="calibration",
        published_count=published_count,
        errors=tuple(errors),
        runtime_config=runtime_config,
        message_id=message_id,
        duplicate=duplicate,
        persistence_mode=persistence_mode,
        requested_state=requested_state,
    )


def _float_value(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _contains_message_id(message_ids, message_id):
    for item in message_ids or ():
        if item == message_id:
            return True
    return False


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )
