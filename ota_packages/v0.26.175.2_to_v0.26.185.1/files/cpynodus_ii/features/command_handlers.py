"""Parse and apply inbound runtime commands from MQTT and web flows.

This module translates command payloads into settings updates, calibration
actions, switch operations, and other runtime side effects while keeping
command handling testable outside the main loop.
"""

import json
from dataclasses import replace

from cpynodus_ii.core.settings import Settings
from cpynodus_ii.features.command_models import (
    CalibrationCommand,
    CommandResult,
    DeviceConfigCommand,
    FwUpdateCommand,
    SoilPhCalibrationSession,
    SwitchCommand,
)
from cpynodus_ii.features.onboarding_state import (
    clear_onboarding_state,
    load_onboarding_state,
)
from cpynodus_ii.features.payloads import (
    build_calibration_ack_payload,
    build_calibration_result_payload,
    build_calibration_status_payload,
    build_config_ack_payload,
    build_config_result_payload,
    build_meta_patch_payload,
    build_sensor_data_payload,
)
from cpynodus_ii.features.runtime_config_update import apply_runtime_config_updates
from cpynodus_ii.features.topics import mqtt_topic

_TIME_KEYS_TRIGGER_NTP_RESYNC = (
    "TZ",
    "TZ_OFFSET",
    "TZ_NAME",
    "NTP_SERVER",
    "NTP_SERVER_IP",
)


def subscribe_runtime_topics(transport, runtime_config):
    """Subscribe the transport to current runtime command topics."""
    topics = []
    device_id = _device_id(runtime_config)
    if device_id:
        topics.append(
            transport.subscribe(mqtt_topic(runtime_config, device_id, "config", "set"))
        )
        topics.append(
            transport.subscribe(
                mqtt_topic(runtime_config, device_id, "calibration", "set")
            )
        )
        topics.append(
            transport.subscribe(mqtt_topic(runtime_config, device_id, "fwupdate"))
        )
        topics.extend(_subscribe_log_transfer_topics(transport, runtime_config))
    topics.extend(subscribe_switch_runtime_topics(transport, runtime_config))
    return tuple(topics)


def subscribe_device_runtime_topics(transport, runtime_config):
    """Subscribe the transport to device-level command topics."""
    topics = []
    device_id = _device_id(runtime_config)
    if not device_id:
        return tuple(topics)
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "config", "set"))
    )
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "calibration", "set"))
    )
    topics.append(
        transport.subscribe(mqtt_topic(runtime_config, device_id, "fwupdate"))
    )
    topics.extend(_subscribe_log_transfer_topics(transport, runtime_config))
    return tuple(topics)


def subscribe_switch_runtime_topics(transport, runtime_config):
    """Subscribe the transport to switch channel command topics."""
    topics = []
    for channel in runtime_config.switch.channels:
        topics.append(
            transport.subscribe(
                mqtt_topic(runtime_config, channel.channel_id, "config", "set")
            )
        )
    return tuple(topics)


def process_inbound_messages(
    transport,
    runtime_config,
    switch_service,
    *,
    handled_message_ids=(),
    settings_root=None,
):
    """Process all queued inbound messages and publish resulting outputs."""
    results = []
    current_runtime_config = runtime_config
    seen_message_ids = set(handled_message_ids or ())
    device_id = _device_id(runtime_config)

    for message in transport.drain_received():
        if message.topic == mqtt_topic(
            current_runtime_config, device_id, "config", "set"
        ):
            result = process_device_config_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.runtime_config is not None:
                current_runtime_config = result.runtime_config
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic == mqtt_topic(
            current_runtime_config, device_id, "calibration", "set"
        ):
            result = process_calibration_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic == mqtt_topic(current_runtime_config, device_id, "fwupdate"):
            result = process_fwupdate_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic == mqtt_topic(
            current_runtime_config, device_id, "logs", "get"
        ):
            result = _process_log_transfer_message(
                transport,
                current_runtime_config,
                topic=message.topic,
                payload_text=message.payload_text,
                duplicate_message_ids=seen_message_ids,
                settings_root=settings_root,
            )
            if result.message_id:
                seen_message_ids.add(result.message_id)
            results.append(result)
            continue

        if message.topic.endswith("/config/set"):
            results.append(
                process_switch_command_message(
                    transport,
                    current_runtime_config,
                    switch_service,
                    topic=message.topic,
                    payload_text=message.payload_text,
                    settings_root=settings_root,
                )
            )

    return tuple(results)


def process_fwupdate_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse and persist one firmware-update prepare command."""
    command = parse_fwupdate_command(payload_text)
    ack_topic = mqtt_topic(
        runtime_config, _device_id(runtime_config), "fwupdate", "ack"
    )
    result_topic = mqtt_topic(
        runtime_config, _device_id(runtime_config), "fwupdate", "result"
    )
    if command is None:
        transport.publish(
            ack_topic,
            _build_fwupdate_ack_payload("", accepted=False, duplicate=False),
            retain=False,
        )
        transport.publish(
            result_topic,
            _build_fwupdate_result_payload("", prepared=False, error="schema_invalid"),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="fwupdate",
            published_count=2,
            errors=("schema_invalid",),
            runtime_config=runtime_config,
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())
    transport.publish(
        ack_topic,
        _build_fwupdate_ack_payload(
            command.message_id, accepted=True, duplicate=duplicate
        ),
        retain=False,
    )
    if duplicate:
        transport.publish(
            result_topic,
            _build_fwupdate_result_payload(
                command.message_id,
                prepared=True,
                package_id=command.package_id,
                duplicate=True,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="fwupdate",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
            requested_state=command.command,
        )

    error = _fwupdate_prepare_error(command, settings_root)
    if error:
        transport.publish(
            result_topic,
            _build_fwupdate_result_payload(
                command.message_id,
                prepared=False,
                package_id=command.package_id,
                error=error,
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="fwupdate",
            published_count=2,
            errors=(error,),
            runtime_config=runtime_config,
            message_id=command.message_id,
            requested_state=command.command,
        )

    try:
        from cpynodus_ii.ota.state import FwUpdateState, save_ota_state

        save_ota_state(
            FwUpdateState(
                prior_profile=runtime_config.active_profile,
                package_id=command.package_id,
                phase="requested",
            ),
            _ota_state_path(settings_root),
        )
        persistence_error = ""
    except OSError:
        persistence_error = "ota_state_persist_failed"

    if persistence_error:
        transport.publish(
            result_topic,
            _build_fwupdate_result_payload(
                command.message_id,
                prepared=False,
                package_id=command.package_id,
                error=persistence_error,
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="fwupdate",
            published_count=2,
            errors=(persistence_error,),
            runtime_config=runtime_config,
            message_id=command.message_id,
            requested_state=command.command,
        )

    transport.publish(
        result_topic,
        _build_fwupdate_result_payload(
            command.message_id,
            prepared=True,
            package_id=command.package_id,
        ),
        retain=False,
    )
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="fwupdate",
        published_count=2,
        errors=(),
        runtime_config=runtime_config,
        message_id=command.message_id,
        persistence_mode="persisted",
        requested_state=command.command,
        reboot_requested=True,
    )


def process_soil_calibration_session(
    transport,
    runtime_config,
    sensor_service,
    *,
    now_monotonic,
    settings_root=None,
):
    """Advance one active soil pH calibration session when due."""
    session = _current_soil_session(transport)
    if session is None:
        return CommandResult(
            phase="ignored",
            topic="",
            command_type="calibration_session",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
        )
    if not transport.connected or sensor_service is None:
        return CommandResult(
            phase="ignored",
            topic="",
            command_type="calibration_session",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
            message_id=session.message_id,
        )
    if float(now_monotonic) < float(session.next_sample_at):
        return CommandResult(
            phase="ignored",
            topic="",
            command_type="calibration_session",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    from cpynodus_ii.features.sensor_service import read_sensor_snapshot

    snapshot = read_sensor_snapshot(sensor_service, runtime_config)
    ph_value = (
        (snapshot.metrics or {}).get("Soil pH") if snapshot.phase == "ready" else None
    )
    if ph_value is None:
        return CommandResult(
            phase="error",
            topic="",
            command_type="calibration_session",
            published_count=0,
            errors=("soil_ph_sample_unavailable",),
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    samples = tuple(session.samples) + (float(ph_value),)
    sample_index = len(samples)
    sensor_id = runtime_config.sensor.sensor_id
    published = 0

    transport.publish(
        mqtt_topic(runtime_config, sensor_id, "data"),
        build_sensor_data_payload(runtime_config, snapshot),
        retain=False,
    )
    published += 1
    transport.publish(
        mqtt_topic(runtime_config, sensor_id, "event", "calibration_sample"),
        {
            "schema": "nodus-calibration-sample/v1",
            "sensor_id": sensor_id,
            "message_id": session.message_id,
            "sample_index": sample_index,
            "sample_count": int(session.sample_count),
            "reference_ph": float(session.reference_ph),
            "soil_ph": float(ph_value),
            "timestamp": int(now_monotonic),
        },
        retain=False,
    )
    published += 1
    transport.publish(
        mqtt_topic(runtime_config, sensor_id, "event", "calibration_progress"),
        {
            "schema": "nodus-calibration-progress/v1",
            "sensor_id": sensor_id,
            "message_id": session.message_id,
            "sample_index": sample_index,
            "sample_count": int(session.sample_count),
            "remaining": max(0, int(session.sample_count) - sample_index),
            "timestamp": int(now_monotonic),
        },
        retain=False,
    )
    published += 1

    if sample_index < int(session.sample_count):
        _set_soil_session(
            transport,
            replace(
                session,
                samples=samples,
                next_sample_at=float(now_monotonic) + float(session.sample_interval_s),
            ),
        )
        return CommandResult(
            phase="published",
            topic=mqtt_topic(runtime_config, sensor_id, "event", "calibration_sample"),
            command_type="calibration_session",
            published_count=published,
            errors=(),
            runtime_config=runtime_config,
            message_id=session.message_id,
        )

    current_offset = float(
        getattr(runtime_config.sensor.calibration_device, "soil_ph_cal_val", 0.0) or 0.0
    )
    average_ph = sum(samples) / float(len(samples) or 1)
    computed_offset = current_offset + (float(session.reference_ph) - float(average_ph))
    updates = (
        {
            "section": "Calibration.Device",
            "key": "SOIL_PH_CAL_VAL",
            "value": computed_offset,
        },
    )
    updated_runtime_config, applied_updates, persistence_errors = (
        apply_runtime_config_updates(
            runtime_config,
            updates,
            settings_root=settings_root,
        )
    )
    transport.publish(
        mqtt_topic(updated_runtime_config, sensor_id, "event", "calibration_status"),
        build_calibration_status_payload(
            updated_runtime_config,
            status="idle",
            calibrated=True,
            extra={
                "soil_calibration_active": False,
                "soil_calibration_message_id": session.message_id,
                "soil_calibration_reference_ph": float(session.reference_ph),
                "soil_calibration_sample_count": int(session.sample_count),
                "soil_calibration_samples_collected": len(samples),
                "computed_soil_ph_offset": float(computed_offset),
            },
        ),
        retain=True,
    )
    published += 1
    transport.publish(
        mqtt_topic(updated_runtime_config, sensor_id, "event", "calibration_result"),
        {
            "schema": "nodus-calibration-event-result/v1",
            "sensor_id": sensor_id,
            "message_id": session.message_id,
            "reference_ph": float(session.reference_ph),
            "sample_count": int(session.sample_count),
            "samples_collected": len(samples),
            "average_soil_ph": float(average_ph),
            "computed_soil_ph_offset": float(computed_offset),
            "timestamp": int(now_monotonic),
        },
        retain=True,
    )
    published += 1
    calibration_result_payload = build_calibration_result_payload(
        session.message_id,
        applied=True,
        updated=len(applied_updates),
        error="",
        reference_ph=session.reference_ph,
    )
    calibration_result_payload["computed_soil_ph_offset"] = float(computed_offset)
    calibration_result_payload["samples_collected"] = len(samples)
    transport.publish(
        mqtt_topic(
            updated_runtime_config,
            _device_id(updated_runtime_config),
            "calibration",
            "result",
        ),
        calibration_result_payload,
        retain=False,
    )
    published += 1
    transport.publish(
        mqtt_topic(
            updated_runtime_config, _device_id(updated_runtime_config), "meta", "patch"
        ),
        build_meta_patch_payload(
            updated_runtime_config,
            source="calibration_set",
            message_id=session.message_id,
            updates=applied_updates,
        ),
        retain=False,
    )
    published += 1
    _set_soil_session(transport, None)
    return CommandResult(
        phase="published",
        topic=mqtt_topic(
            updated_runtime_config, sensor_id, "event", "calibration_result"
        ),
        command_type="calibration_session",
        published_count=published,
        errors=tuple(persistence_errors),
        runtime_config=updated_runtime_config,
        message_id=session.message_id,
        persistence_mode="volatile"
        if persistence_errors
        else "persisted"
        if settings_root is not None
        else "",
    )


def process_switch_command_message(
    transport,
    runtime_config,
    switch_service,
    *,
    topic,
    payload_text,
    settings_root=None,
):
    """Parse, apply, and publish one switch command message."""
    if not str(payload_text or "").strip():
        return CommandResult(
            phase="ignored",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=(),
            requested_state="",
        )
    command = parse_switch_command(topic, payload_text, runtime_config=runtime_config)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="switch",
            published_count=0,
            errors=("invalid_switch_command",),
            requested_state="",
        )
    ack_topic = mqtt_topic(runtime_config, command.channel_id, "config", "ack")
    transport.publish(
        ack_topic,
        build_config_ack_payload(command.message_id, accepted=True, duplicate=False),
        retain=False,
    )
    from cpynodus_ii.features.publish_cycle import publish_switch_result
    from cpynodus_ii.features.switch_service import apply_switch_state

    apply_result = apply_switch_state(
        switch_service,
        channel_id=command.channel_id,
        state=command.desired_state,
    )
    publish_result = publish_switch_result(
        transport,
        runtime_config,
        apply_result,
        message_id=command.message_id,
    )
    meta_result = _publish_switch_meta_patch(
        transport,
        runtime_config,
        apply_result,
        message_id=command.message_id,
    )
    persistence_errors = ()
    if settings_root is not None:
        switch_channel = _find_runtime_channel(runtime_config, command.channel_id)
        if switch_channel is not None:
            _, _, persistence_errors = Settings.apply_updates_to_directory(
                settings_root,
                runtime_config,
                (
                    {
                        "section": "Switch",
                        "key": "{}_LAST_STATE".format(switch_channel.key),
                        "value": bool(command.desired_state),
                    },
                ),
                reload_runtime=False,
            )
    return CommandResult(
        phase=publish_result.phase
        if publish_result.phase != "skipped"
        else meta_result.phase,
        topic=topic,
        command_type="switch",
        published_count=1
        + publish_result.published_count
        + meta_result.published_count,
        errors=publish_result.errors + meta_result.errors + tuple(persistence_errors),
        message_id=command.message_id,
        persistence_mode="volatile"
        if persistence_errors
        else "persisted"
        if settings_root is not None
        else "",
        requested_state="ON" if command.desired_state else "OFF",
    )


def process_device_config_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse, apply, and publish one device config command."""
    if not str(payload_text or "").strip():
        return CommandResult(
            phase="ignored",
            topic=topic,
            command_type="config",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
        )

    command = parse_device_config_command(payload_text)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=0,
            errors=("schema_invalid",),
        )

    ack_topic = mqtt_topic(runtime_config, _device_id(runtime_config), "config", "ack")
    result_topic = mqtt_topic(
        runtime_config, _device_id(runtime_config), "config", "result"
    )
    token_error = _validate_onboarding_token(command, settings_root=settings_root)
    if token_error:
        transport.publish(
            ack_topic,
            build_config_ack_payload(
                command.message_id, accepted=False, duplicate=False
            ),
            retain=False,
        )
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=False,
                updated=0,
                error=token_error,
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(token_error,),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())

    transport.publish(
        ack_topic,
        build_config_ack_payload(
            command.message_id, accepted=True, duplicate=duplicate
        ),
        retain=False,
    )

    if duplicate:
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                duplicate=True,
                restart=command.restart_requested,
                restart_mode=command.restart_mode,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
            reboot_mode=command.restart_mode if command.restart_requested else "",
        )

    if command.restart_requested and not command.updates:
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                restart=True,
                restart_mode=command.restart_mode,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            requested_state="restart:{}".format(command.restart_mode),
            reboot_requested=True,
            reboot_mode=command.restart_mode,
        )

    try:
        updated_runtime_config, applied_updates, persistence_errors = (
            apply_runtime_config_updates(
                runtime_config,
                command.updates,
                settings_root=settings_root,
            )
        )
    except RuntimeError as exc:
        if "pystack exhausted" not in str(exc).lower():
            raise
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=False,
                updated=0,
                error="pystack_exhausted",
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=("pystack_exhausted",),
            runtime_config=runtime_config,
            message_id=command.message_id,
            persistence_mode="volatile",
        )
    if not applied_updates:
        transport.publish(
            result_topic,
            build_config_result_payload(
                command.message_id,
                applied=False,
                updated=0,
                error="config_rejected",
            ),
            retain=False,
        )
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="config",
            published_count=2,
            errors=tuple(persistence_errors) or ("config_rejected",),
            runtime_config=runtime_config,
            message_id=command.message_id,
            persistence_mode="volatile" if persistence_errors else "persisted",
        )

    transport.publish(
        result_topic,
        build_config_result_payload(
            command.message_id,
            applied=True,
            updated=len(applied_updates),
            error="",
            restart=command.restart_requested,
            restart_mode=command.restart_mode,
        ),
        retain=False,
    )
    transport.publish(
        mqtt_topic(
            updated_runtime_config, _device_id(updated_runtime_config), "meta", "patch"
        ),
        build_meta_patch_payload(
            updated_runtime_config,
            source="config_set",
            message_id=command.message_id,
            updates=applied_updates,
        ),
        retain=False,
    )
    if settings_root is not None:
        clear_onboarding_state(settings_root)
    return CommandResult(
        phase="published",
        topic=topic,
        command_type="config",
        published_count=3,
        errors=tuple(persistence_errors),
        runtime_config=updated_runtime_config,
        message_id=command.message_id,
        persistence_mode="volatile" if persistence_errors else "persisted",
        requested_state=(
            "restart:{}".format(command.restart_mode)
            if command.restart_requested
            else ""
        ),
        reboot_requested=command.restart_requested,
        reboot_mode=command.restart_mode if command.restart_requested else "",
        ntp_resync_requested=_updates_request_ntp_resync(applied_updates),
    )


def process_calibration_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    """Parse and respond to one device calibration command."""
    if not str(payload_text or "").strip():
        return CommandResult(
            phase="ignored",
            topic=topic,
            command_type="calibration",
            published_count=0,
            errors=(),
            runtime_config=runtime_config,
        )

    command = parse_calibration_command(payload_text)
    if command is None:
        return CommandResult(
            phase="error",
            topic=topic,
            command_type="calibration",
            published_count=0,
            errors=("schema_invalid",),
        )

    duplicate = command.message_id in set(duplicate_message_ids or ())
    device_id = _device_id(runtime_config)
    ack_topic = mqtt_topic(runtime_config, device_id, "calibration", "ack")
    result_topic = mqtt_topic(runtime_config, device_id, "calibration", "result")
    published_count = 0

    transport.publish(
        ack_topic,
        build_calibration_ack_payload(command.message_id, accepted=True),
        retain=False,
    )
    published_count += 1

    if duplicate:
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 1,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
            duplicate=True,
        )

    if command.action in {"apply", "set", "update"}:
        if not command.updates:
            transport.publish(
                result_topic,
                build_calibration_result_payload(
                    command.message_id,
                    applied=False,
                    updated=0,
                    error="schema_invalid",
                ),
                retain=False,
            )
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="calibration",
                published_count=published_count + 1,
                errors=("schema_invalid",),
                runtime_config=runtime_config,
                message_id=command.message_id,
            )
        applied_updates = tuple(command.updates)
        persistence_errors = ()
        updated_runtime_config, applied_updates, _ = apply_runtime_config_updates(
            runtime_config,
            applied_updates,
            settings_root=None,
        )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=len(applied_updates),
                error="",
            ),
            retain=False,
        )
        transport.publish(
            mqtt_topic(updated_runtime_config, device_id, "meta", "patch"),
            build_meta_patch_payload(
                updated_runtime_config,
                source="calibration_set",
                message_id=command.message_id,
                updates=applied_updates,
            ),
            retain=False,
        )
        if settings_root is not None:
            try:
                _, _, persistence_errors = Settings.apply_updates_to_directory(
                    settings_root,
                    runtime_config,
                    applied_updates,
                    reload_runtime=False,
                )
            except RuntimeError as exc:
                if "pystack exhausted" not in str(exc).lower():
                    raise
                persistence_errors = ("pystack_exhausted",)
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=tuple(persistence_errors),
            runtime_config=updated_runtime_config,
            message_id=command.message_id,
            persistence_mode=_persistence_mode(settings_root, persistence_errors),
        )

    if command.action == "status":
        status_payload = _calibration_status_payload(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status_payload,
            retain=True,
        )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                status=status_payload,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    if command.action == "soil_ph_session_start":
        if runtime_config.sensor.device != "soil":
            return _unsupported_calibration_command(
                transport,
                topic=topic,
                result_topic=result_topic,
                runtime_config=runtime_config,
                command=command,
            )
        if command.reference_ph is None:
            transport.publish(
                result_topic,
                build_calibration_result_payload(
                    command.message_id,
                    applied=False,
                    updated=0,
                    error="missing_reference_ph",
                ),
                retain=False,
            )
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="calibration",
                published_count=published_count + 1,
                errors=("missing_reference_ph",),
                runtime_config=runtime_config,
                message_id=command.message_id,
            )
        if _current_soil_session(transport) is not None:
            transport.publish(
                result_topic,
                build_calibration_result_payload(
                    command.message_id,
                    applied=False,
                    updated=0,
                    error="soil_calibration_already_running",
                ),
                retain=False,
            )
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="calibration",
                published_count=published_count + 1,
                errors=("soil_calibration_already_running",),
                runtime_config=runtime_config,
                message_id=command.message_id,
            )
        session = SoilPhCalibrationSession(
            message_id=command.message_id,
            reference_ph=float(command.reference_ph),
            sample_interval_s=float(command.sample_interval_s),
            sample_count=int(command.sample_count),
            started_at=0.0,
            next_sample_at=0.0,
            samples=(),
        )
        _set_soil_session(transport, session)
        status_payload = _calibration_status_payload(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status_payload,
            retain=True,
        )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                started=True,
                status=status_payload,
                sample_interval_s=command.sample_interval_s,
                sample_count=command.sample_count,
                reference_ph=command.reference_ph,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    if command.action == "soil_ph_session_cancel":
        session = _current_soil_session(transport)
        if session is None:
            transport.publish(
                result_topic,
                build_calibration_result_payload(
                    command.message_id,
                    applied=False,
                    updated=0,
                    error="soil_calibration_not_running",
                ),
                retain=False,
            )
            return CommandResult(
                phase="error",
                topic=topic,
                command_type="calibration",
                published_count=published_count + 1,
                errors=("soil_calibration_not_running",),
                runtime_config=runtime_config,
                message_id=command.message_id,
            )
        _set_soil_session(transport, None)
        status_payload = _calibration_status_payload(runtime_config, transport)
        transport.publish(
            mqtt_topic(runtime_config, device_id, "event", "calibration_status"),
            status_payload,
            retain=True,
        )
        transport.publish(
            result_topic,
            build_calibration_result_payload(
                command.message_id,
                applied=True,
                updated=0,
                error="",
                status=status_payload,
            ),
            retain=False,
        )
        return CommandResult(
            phase="published",
            topic=topic,
            command_type="calibration",
            published_count=published_count + 2,
            errors=(),
            runtime_config=runtime_config,
            message_id=command.message_id,
        )

    return _unsupported_calibration_command(
        transport,
        topic=topic,
        result_topic=result_topic,
        runtime_config=runtime_config,
        command=command,
    )


def parse_switch_command(topic, payload_text, runtime_config=None):
    """Parse a compact ON/OFF or JSON switch command payload."""
    channel_id = _channel_id_from_topic(topic)
    if not channel_id:
        return None

    text = str(payload_text or "").strip()
    if not text:
        return None

    normalized = text.upper()
    if normalized in {"ON", "OFF"}:
        return SwitchCommand(
            channel_id=channel_id,
            desired_state=(normalized == "ON"),
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    desired_state = _extract_switch_state(payload)
    if desired_state is None and runtime_config is not None:
        desired_state = _extract_switch_state_from_config_updates(
            payload,
            runtime_config,
            channel_id=channel_id,
        )
    if desired_state is None:
        return None
    return SwitchCommand(
        channel_id=channel_id,
        desired_state=desired_state,
        message_id=str(payload.get("message_id", "") or "").strip(),
    )


def parse_device_config_command(payload_text):
    """Parse device-level config/set payloads."""
    payload = _parse_json_object(payload_text)
    if payload is None:
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    if not message_id:
        return None
    body = payload.get("payload") or {}
    body_restart_mode = (
        body.get("restart_mode", "soft") if isinstance(body, dict) else "soft"
    )
    restart_requested = _truthy_config_restart(payload.get("restart", False))
    restart_mode = _normalize_restart_mode(
        payload.get("restart_mode", body_restart_mode)
    )
    updates = _extract_config_updates(body)
    if updates is None and not restart_requested:
        return None
    return DeviceConfigCommand(
        message_id=message_id,
        updates=tuple(updates or ()),
        onboard_token=str(payload.get("onboard_token", "") or "").strip(),
        restart_requested=restart_requested,
        restart_mode=restart_mode,
    )


def _truthy_config_restart(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _normalize_restart_mode(value):
    mode = str(value or "soft").strip().lower()
    if mode == "hard":
        return "hard"
    return "soft"


def parse_calibration_command(payload_text):
    """Parse calibration/set payloads."""
    payload = _parse_json_object(payload_text)
    if payload is None:
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    action = str(payload.get("action", "") or "").strip().lower()
    if not (message_id and action):
        return None
    body = payload.get("payload") or {}
    updates = _extract_calibration_updates(body)
    reference_ph = body.get("reference_ph")
    sample_interval_s = float(body.get("sample_interval_s", 10) or 10)
    sample_count = int(body.get("sample_count", 12) or 12)
    return CalibrationCommand(
        message_id=message_id,
        action=action,
        updates=tuple(updates),
        reference_ph=float(reference_ph) if reference_ph is not None else None,
        sample_interval_s=sample_interval_s,
        sample_count=max(6, min(18, sample_count)),
    )


def parse_fwupdate_command(payload_text):
    """Parse firmware-update control payloads."""
    payload = _parse_json_object(payload_text)
    if payload is None:
        return None
    message_id = str(payload.get("message_id", "") or "").strip()
    body = payload.get("payload") or {}
    command = str(payload.get("command", body.get("command", "")) or "").strip()
    package_id = str(
        payload.get("package_id", body.get("package_id", "")) or ""
    ).strip()
    if not (message_id and command):
        return None
    return FwUpdateCommand(
        message_id=message_id,
        command=command.lower(),
        package_id=package_id,
    )


def _publish_switch_meta_patch(transport, runtime_config, apply_result, *, message_id):
    from cpynodus_ii.features.publish_cycle import PublishCycleResult

    if apply_result.phase != "ready":
        return PublishCycleResult(
            phase="skipped",
            published_count=0,
            topics=(),
            errors=apply_result.errors,
        )

    channel = _find_runtime_channel(runtime_config, apply_result.channel_id)
    if channel is None:
        return PublishCycleResult(
            phase="error",
            published_count=0,
            topics=(),
            errors=("switch_channel_not_found",),
        )

    topic = mqtt_topic(runtime_config, _device_id(runtime_config), "meta", "patch")
    transport.publish(
        topic,
        build_meta_patch_payload(
            runtime_config,
            source="switch_set",
            message_id=message_id,
            updates=(
                {
                    "section": "Switch",
                    "key": "{}_LAST_STATE".format(channel.key),
                    "value": bool(apply_result.applied_state),
                },
            ),
        ),
        retain=False,
    )
    return PublishCycleResult(
        phase="published",
        published_count=1,
        topics=(topic,),
        errors=(),
    )


def _device_id(runtime_config):
    return (
        runtime_config.sensor.sensor_id
        or runtime_config.switch.device_id
        or runtime_config.network.hostname
    )


def _subscribe_log_transfer_topics(transport, runtime_config):
    device_id = _device_id(runtime_config)
    if not device_id:
        return ()
    return (transport.subscribe(mqtt_topic(runtime_config, device_id, "logs", "get")),)


def _process_log_transfer_message(
    transport,
    runtime_config,
    *,
    topic,
    payload_text,
    duplicate_message_ids=(),
    settings_root=None,
):
    from cpynodus_ii.features.log_transfer import process_log_transfer_message

    return process_log_transfer_message(
        transport,
        runtime_config,
        topic=topic,
        payload_text=payload_text,
        duplicate_message_ids=duplicate_message_ids,
        settings_root=settings_root,
    )


def _fwupdate_prepare_error(command, settings_root):
    if command.command != "prepare":
        return "unsupported_fwupdate_command"
    if not command.package_id:
        return "package_id_missing"
    if not settings_root:
        return "read_only_filesystem"
    if _filesystem_writable(settings_root) is False:
        return "read_only_filesystem"
    return ""


def _filesystem_writable(root):
    writable = Settings.filesystem_writable(root)
    if writable is not None:
        return bool(writable)
    probe_path = _join_root(root, ".ota_write_probe.tmp")
    try:
        with open(probe_path, "w") as handle:
            handle.write("1")
        try:
            import os

            os.remove(probe_path)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _ota_state_path(root):
    return _join_root(root, "_ota/state.json")


def _join_root(root, path):
    root_text = str(root or ".")
    path_text = str(path or "")
    if root_text == "/":
        return "/{}".format(path_text.lstrip("/"))
    if root_text.endswith("/"):
        return "{}{}".format(root_text, path_text.lstrip("/"))
    return "{}/{}".format(root_text, path_text.lstrip("/"))


def _build_fwupdate_ack_payload(message_id, *, accepted, duplicate):
    return {
        "schema": "nodus-fwupdate-ack/v1",
        "message_id": str(message_id or ""),
        "accepted": bool(accepted),
        "duplicate": bool(duplicate),
    }


def _build_fwupdate_result_payload(
    message_id,
    *,
    prepared,
    package_id="",
    error="",
    duplicate=False,
):
    return {
        "schema": "nodus-fwupdate-result/v1",
        "message_id": str(message_id or ""),
        "prepared": bool(prepared),
        "package_id": str(package_id or ""),
        "duplicate": bool(duplicate),
        "error": str(error or ""),
    }


def _find_runtime_channel(runtime_config, channel_id):
    for channel in getattr(runtime_config.switch, "channels", ()):
        if getattr(channel, "channel_id", "") == channel_id:
            return channel
    return None


def _current_soil_session(transport):
    return getattr(transport, "_soil_ph_session", None)


def _set_soil_session(transport, session):
    setattr(transport, "_soil_ph_session", session)


def _calibration_status_payload(runtime_config, transport):
    session = _current_soil_session(transport)
    extra = None
    if session is not None:
        extra = {
            "soil_calibration_active": True,
            "soil_calibration_message_id": session.message_id,
            "soil_calibration_reference_ph": float(session.reference_ph),
            "soil_calibration_sample_count": int(session.sample_count),
            "soil_calibration_samples_collected": len(tuple(session.samples or ())),
        }
        return build_calibration_status_payload(
            runtime_config,
            status="active",
            calibrated=False,
            extra=extra,
        )
    return build_calibration_status_payload(
        runtime_config, status="idle", calibrated=False
    )


def _validate_onboarding_token(command, *, settings_root=None):
    if settings_root is None:
        return ""
    state = load_onboarding_state(settings_root)
    expected = str(state.get("onboard_token", "") or "").strip()
    if not expected:
        return ""
    if str(command.onboard_token or "").strip() == expected:
        return ""
    return "onboard_token_invalid"


def _unsupported_calibration_command(
    transport, *, topic, result_topic, runtime_config, command
):
    transport.publish(
        result_topic,
        build_calibration_result_payload(
            command.message_id,
            applied=False,
            updated=0,
            error="calibration_not_supported",
        ),
        retain=False,
    )
    return CommandResult(
        phase="error",
        topic=topic,
        command_type="calibration",
        published_count=2,
        errors=("calibration_not_supported",),
        runtime_config=runtime_config,
        message_id=command.message_id,
    )


def _channel_id_from_topic(topic):
    text = str(topic or "").strip()
    parts = text.split("/")
    if len(parts) >= 3:
        return parts[1]
    return ""


def _extract_switch_state(payload):
    candidates = [
        payload.get("state"),
        payload.get("set"),
        (payload.get("payload") or {}).get("state")
        if isinstance(payload.get("payload"), dict)
        else None,
        (payload.get("payload") or {}).get("set")
        if isinstance(payload.get("payload"), dict)
        else None,
    ]
    for value in candidates:
        normalized = _normalize_switch_state(value)
        if normalized is not None:
            return normalized
    return None


def _extract_switch_state_from_config_updates(payload, runtime_config, *, channel_id):
    if not isinstance(payload, dict):
        return None

    channel = _find_runtime_channel(runtime_config, channel_id)
    if channel is None:
        return None

    expected_keys = {
        "{}_LAST_STATE".format(channel.key).upper(),
        "{}_STATE".format(channel.key).upper(),
    }
    body = payload.get("payload")
    if not isinstance(body, dict):
        return None

    updates = body.get("updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section", "") or "").strip()
            key = str(item.get("key", "") or "").strip().upper()
            if section == "Switch" and key in expected_keys:
                normalized = _normalize_switch_state(item.get("value"))
                if normalized is not None:
                    return normalized

    settings = body.get("settings")
    if isinstance(settings, dict):
        switch_settings = settings.get("Switch")
        if isinstance(switch_settings, dict):
            for key in expected_keys:
                if key in switch_settings:
                    normalized = _normalize_switch_state(switch_settings.get(key))
                    if normalized is not None:
                        return normalized
    return None


def _normalize_switch_state(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"ON", "TRUE", "1"}:
        return True
    if text in {"OFF", "FALSE", "0"}:
        return False
    return None


def _parse_json_object(payload_text):
    try:
        payload = json.loads(str(payload_text or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _extract_config_updates(body):
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("updates"), list):
        updates = []
        for item in body["updates"]:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section", "") or "").strip()
            key = str(item.get("key", "") or "").strip()
            if not (section and key):
                continue
            updates.append(
                {
                    "section": section,
                    "key": key,
                    "value": item.get("value"),
                }
            )
        return updates
    if isinstance(body.get("settings"), dict):
        updates = []
        for section, values in body["settings"].items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                updates.append(
                    {
                        "section": str(section or "").strip(),
                        "key": str(key or "").strip(),
                        "value": value,
                    }
                )
        return updates
    return None


def _persistence_mode(settings_root, errors):
    if settings_root is None:
        return ""
    return "volatile" if errors else "persisted"


def _updates_request_ntp_resync(updates):
    """Return True when applied config updates should force an NTP attempt."""
    for update in updates or ():
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip().upper()
        if section == "Time" and key in _TIME_KEYS_TRIGGER_NTP_RESYNC:
            return True
    return False


def _extract_calibration_updates(body):
    if not isinstance(body, dict):
        return ()
    if isinstance(body.get("offsets"), list):
        updates = []
        for item in body["offsets"]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "") or "").strip()
            value = item.get("value")
            section, normalized_key = _normalize_calibration_key(key)
            if section and normalized_key:
                updates.append(
                    {
                        "section": section,
                        "key": normalized_key,
                        "value": value,
                    }
                )
        return tuple(updates)
    calibration = body.get("calibration")
    if isinstance(calibration, dict):
        updates = []
        for branch, values in calibration.items():
            if not isinstance(values, dict):
                continue
            section = _calibration_section_for_branch(branch)
            if not section:
                continue
            for key, value in values.items():
                key_text = str(key or "").strip()
                if (
                    section == "Calibration.Device"
                    and key_text.upper() == "SOIL_TEMP_MOIST_VAL"
                ):
                    key_text = "SOIL_MOIST_CAL_VAL"
                updates.append(
                    {
                        "section": section,
                        "key": key_text,
                        "value": value,
                    }
                )
        return tuple(updates)
    return ()


def _calibration_section_for_branch(branch):
    normalized = str(branch or "").strip().lower()
    if normalized == "system":
        return "Calibration.System"
    if normalized == "device":
        return "Calibration.Device"
    if normalized == "soil":
        return "Calibration.Soil"
    if normalized == "apvpd":
        return "Calibration"
    return ""


def _normalize_calibration_key(key):
    text = str(key or "").strip()
    if not text:
        return "", ""
    if "." in text:
        parts = text.split(".")
        if len(parts) >= 3:
            section = ".".join(parts[:-1])
            key = parts[-1]
            if (
                section == "Calibration.Device"
                and key.upper() == "SOIL_TEMP_MOIST_VAL"
            ):
                key = "SOIL_MOIST_CAL_VAL"
            return section, key
    if text == "soil_ph_offset":
        return "Calibration.Device", "SOIL_PH_CAL_VAL"
    if text == "soil_moisture_offset":
        return "Calibration.Device", "SOIL_MOIST_CAL_VAL"
    return "", ""
