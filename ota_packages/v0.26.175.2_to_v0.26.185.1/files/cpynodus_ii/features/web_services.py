"""Support constrained onboarding and setup web-service behavior.

The web-service helpers manage onboarding persistence, setup payload shaping,
and lightweight service decisions used during AP provisioning and normal web
configuration flows.
"""

import os
import time

from cpynodus_ii.core.obfuscation import encode_password
from cpynodus_ii.features import onboarding_state as _onboarding_state

ITAOT_META_SCHEMA = "itaot-meta/v1"

_ITAOT_ALLOWED_KEYS = (
    ("Network", "SSID"),
    ("Network", "PASSWORD"),
    ("Network", "HOSTNAME"),
    ("MQTT", "BROKER"),
    ("MQTT", "BROKER_IP"),
    ("MQTT", "PORT"),
    ("MQTT", "USERNAME"),
    ("MQTT", "PASSWORD"),
    ("MQTT", "BASE_TOPIC"),
    ("Profile", "ACTIVE_PROFILE"),
    ("Time", "TZ"),
    ("Time", "TZ_OFFSET"),
    ("Time", "TZ_NAME"),
    ("Time", "NTP_SERVER"),
    ("Time", "NTP_SERVER_IP"),
)

_DISPLAY_METRICS_BY_DEVICE = {
    "aqi": ("Air Quality", "Temperature"),
    "co2": ("CO2", "Temperature"),
    "avpd": ("Temperature", "Ambient VPD"),
    "apvpd": ("Temperature", "Ambient VPD"),
    "lux": ("Light Intensity", "Estimated PPFD"),
    "soil": ("Soil Moisture", "Soil Temp_C"),
}


def _clean_str(value):
    return str(value or "").strip()


def _coerce_port(value):
    try:
        port = int(value)
    except Exception:
        return 0
    if 0 < port <= 65535:
        return port
    return 0


def _bool_capabilities(runtime_config):
    return {
        "sensor": bool(getattr(runtime_config.sensor, "present", False)),
        "switch": bool(getattr(runtime_config.switch, "present", False)),
    }


def _emit_event(event_logger, message):
    if not callable(event_logger):
        return
    try:
        event_logger(str(message or ""))
    except Exception:
        pass


def _error_text(exc):
    return str(exc or "").replace(" ", "_") or type(exc).__name__


def _update_section_names(updates):
    sections = []
    for update in tuple(updates or ()):
        section = _clean_str(update.get("section", ""))
        if section and section not in sections:
            sections.append(section)
    return ",".join(sections) if sections else "none"


def _time_update_keys(updates):
    keys = []
    for update in tuple(updates or ()):
        if _clean_str(update.get("section", "")) != "Time":
            continue
        key = _clean_str(update.get("key", ""))
        if key:
            keys.append(key)
    return tuple(keys)


def bootstrap_routes_enabled(runtime_config):
    """Return whether the bootstrap route pair should be exposed."""
    return bool(getattr(runtime_config, "ap_mode", False))


def load_onboarding_state(root="."):
    """Load any persisted onboarding runtime state."""
    return _onboarding_state.load_onboarding_state(root)


def save_onboarding_state(root, state):
    """Persist onboarding runtime state outside TOML config files."""
    return _onboarding_state.save_onboarding_state(root, state)


def clear_onboarding_state(root="."):
    """Delete any persisted onboarding runtime state."""
    return _onboarding_state.clear_onboarding_state(root)


def normalize_itaot_init_payload(payload):
    """Validate and normalize the onboarding bootstrap payload."""
    document = payload if isinstance(payload, dict) else {}
    mqtt_doc = document.get("mqtt", {})
    if not isinstance(mqtt_doc, dict):
        mqtt_doc = {}
    time_doc = document.get("time", {})
    if not isinstance(time_doc, dict):
        time_doc = {}

    onboard_token = _clean_str(document.get("onboard_token", ""))
    ssid = _clean_str(document.get("ssid", ""))
    password = _clean_str(document.get("password", ""))
    hostname = _clean_str(document.get("hostname", ""))
    broker_host = _clean_str(mqtt_doc.get("broker_host", ""))
    broker_ip = _clean_str(mqtt_doc.get("broker_ip", ""))
    broker_port = _coerce_port(mqtt_doc.get("broker_port", 0))
    mqtt_username = _clean_str(mqtt_doc.get("username", ""))
    mqtt_password = _clean_str(mqtt_doc.get("password", ""))
    mqtt_base_topic = _clean_str(mqtt_doc.get("base_topic", "")) or "nodus"
    active_profile = _clean_str(mqtt_doc.get("active_profile", "")).lower()
    if not active_profile and broker_host:
        active_profile = "sensorius"

    errors = []
    time_config = {}
    for key in ("TZ", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP"):
        if key in time_doc:
            time_config[key] = _clean_str(time_doc.get(key, ""))
    if "TZ_OFFSET" in time_doc:
        try:
            time_config["TZ_OFFSET"] = int(time_doc.get("TZ_OFFSET", 0) or 0)
        except Exception:
            errors.append("time_tz_offset_invalid")

    if not onboard_token:
        errors.append("onboard_token_required")
    if not ssid:
        errors.append("ssid_required")
    if not password:
        errors.append("password_required")
    if not hostname:
        errors.append("hostname_required")
    if not broker_host:
        errors.append("mqtt_broker_host_required")
    if not broker_port:
        errors.append("mqtt_broker_port_required")
    if active_profile and active_profile not in {
        "sensorius",
        "weewx",
        "homeassistant",
        "nodusweb",
    }:
        errors.append("mqtt_active_profile_invalid")

    return {
        "onboard_token": onboard_token,
        "ssid": ssid,
        "password": password,
        "hostname": hostname,
        "mqtt": {
            "broker_host": broker_host,
            "broker_ip": broker_ip,
            "broker_port": broker_port,
            "username": mqtt_username,
            "password": mqtt_password,
            "base_topic": mqtt_base_topic,
            "active_profile": active_profile or "sensorius",
        },
        "time": time_config,
        "errors": tuple(errors),
    }


def build_itaot_init_updates(normalized_payload):
    """Translate normalized bootstrap input into TOML updates."""
    mqtt_doc = normalized_payload.get("mqtt", {})
    updates = [
        {
            "section": "Network",
            "key": "SSID",
            "value": normalized_payload.get("ssid", ""),
        },
        {
            "section": "Network",
            "key": "PASSWORD",
            "value": normalized_payload.get("password", ""),
        },
        {
            "section": "Network",
            "key": "HOSTNAME",
            "value": normalized_payload.get("hostname", ""),
        },
        {"section": "MQTT", "key": "BROKER", "value": mqtt_doc.get("broker_host", "")},
        {
            "section": "MQTT",
            "key": "PORT",
            "value": int(mqtt_doc.get("broker_port", 0) or 0),
        },
        {
            "section": "MQTT",
            "key": "BASE_TOPIC",
            "value": mqtt_doc.get("base_topic", "nodus"),
        },
        {
            "section": "Profile",
            "key": "ACTIVE_PROFILE",
            "value": mqtt_doc.get("active_profile", "sensorius"),
        },
    ]
    if mqtt_doc.get("broker_ip"):
        updates.append(
            {"section": "MQTT", "key": "BROKER_IP", "value": mqtt_doc.get("broker_ip")}
        )
    if mqtt_doc.get("username"):
        updates.append(
            {"section": "MQTT", "key": "USERNAME", "value": mqtt_doc.get("username")}
        )
    if mqtt_doc.get("password"):
        updates.append(
            {"section": "MQTT", "key": "PASSWORD", "value": mqtt_doc.get("password")}
        )
    for key, value in (normalized_payload.get("time", {}) or {}).items():
        updates.append({"section": "Time", "key": key, "value": value})
    return tuple(updates)


class ItaotInitResult:
    """Describe the result of bootstrap payload handling."""

    def __init__(
        self,
        *,
        accepted,
        rebooting,
        status_code,
        body,
        runtime_config,
        applied_updates=(),
        errors=(),
    ):
        self.accepted = bool(accepted)
        self.rebooting = bool(rebooting)
        self.status_code = int(status_code)
        self.body = body
        self.runtime_config = runtime_config
        self.applied_updates = tuple(applied_updates or ())
        self.errors = tuple(errors or ())


def apply_itaot_init_payload(
    payload, runtime_config, *, settings_root=".", event_logger=None
):
    """Apply a validated onboarding bootstrap payload to local config."""
    _emit_event(event_logger, "apply phase=normalize_begin")
    normalized = normalize_itaot_init_payload(payload)
    errors = normalized.get("errors", ())
    time_keys = tuple((normalized.get("time", {}) or {}).keys())
    _emit_event(
        event_logger,
        "apply phase=normalize_done errors={} ssid_present={} password_present={} "
        "hostname_present={} broker_host_present={} time_keys={}".format(
            ",".join(errors) if errors else "none",
            1 if normalized.get("ssid", "") else 0,
            1 if normalized.get("password", "") else 0,
            1 if normalized.get("hostname", "") else 0,
            1 if normalized.get("mqtt", {}).get("broker_host", "") else 0,
            ",".join(time_keys) if time_keys else "none",
        ),
    )
    if errors:
        _emit_event(event_logger, "apply phase=response_ready status=400")
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=400,
            body={"success": False, "accepted": False, "errors": list(errors)},
            runtime_config=runtime_config,
            errors=tuple(errors),
        )

    updates = build_itaot_init_updates(normalized)
    update_time_keys = _time_update_keys(updates)
    _emit_event(
        event_logger,
        "apply phase=updates_built updates={} sections={} time_updates={} "
        "time_keys={}".format(
            len(updates),
            _update_section_names(updates),
            len(update_time_keys),
            ",".join(update_time_keys) if update_time_keys else "none",
        ),
    )
    _emit_event(
        event_logger,
        "apply phase=persist_begin writer=direct reload_runtime=0",
    )
    if settings_root is None:
        _emit_event(
            event_logger,
            "apply phase=persist_error code=settings_root_missing",
        )
        applied_updates = ()
        persistence_errors = ("settings_root_missing",)
    else:
        try:
            applied_updates, persistence_errors = _write_itaot_settings_file(
                _join_settings_path(settings_root, "settings.toml"),
                updates,
                event_logger=event_logger,
            )
        except MemoryError:
            _emit_event(
                event_logger,
                "apply phase=persist_error code=persistence_memory",
            )
            applied_updates = ()
            persistence_errors = ("persistence_memory",)
        except RuntimeError as exc:
            if "pystack exhausted" not in str(exc).lower():
                raise
            _emit_event(
                event_logger,
                "apply phase=persist_error code=pystack_exhausted",
            )
            applied_updates = ()
            persistence_errors = ("pystack_exhausted",)
    applied_time_keys = _time_update_keys(applied_updates)
    _emit_event(
        event_logger,
        "apply phase=persist_done applied={} time_updates={} time_keys={} "
        "errors={}".format(
            len(applied_updates),
            len(applied_time_keys),
            ",".join(applied_time_keys) if applied_time_keys else "none",
            ",".join(persistence_errors) if persistence_errors else "none",
        ),
    )
    if persistence_errors:
        _emit_event(event_logger, "apply phase=response_ready status=503")
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=503,
            body={
                "success": False,
                "accepted": False,
                "errors": list(persistence_errors),
            },
            runtime_config=runtime_config,
            applied_updates=tuple(applied_updates),
            errors=tuple(persistence_errors),
        )

    state = {
        "schema": "nodus-onboard-state/v1",
        "onboard_token": normalized.get("onboard_token", ""),
        "hostname": normalized.get("hostname", ""),
        "base_topic": normalized.get("mqtt", {}).get("base_topic", "nodus"),
        "active_profile": normalized.get("mqtt", {}).get("active_profile", "sensorius"),
        "created_at": int(time.time()),
    }
    try:
        _emit_event(event_logger, "apply phase=state_persist_begin")
        save_onboarding_state(settings_root, state)
        _emit_event(event_logger, "apply phase=state_persist_done")
    except OSError as exc:
        _emit_event(
            event_logger,
            "apply phase=state_persist_error type={} detail={}".format(
                type(exc).__name__,
                _error_text(exc),
            ),
        )
        _emit_event(event_logger, "apply phase=response_ready status=503")
        return ItaotInitResult(
            accepted=False,
            rebooting=False,
            status_code=503,
            body={
                "success": False,
                "accepted": False,
                "errors": ["onboarding_state_persist_failed", str(exc)],
            },
            runtime_config=runtime_config,
            applied_updates=tuple(applied_updates),
            errors=("onboarding_state_persist_failed", str(exc)),
        )

    body = {
        "success": True,
        "accepted": True,
        "rebooting": True,
        "restart_mode": "hard",
        "hostname": normalized.get("hostname", ""),
        "mqtt_profile": normalized.get("mqtt", {}).get("active_profile", "sensorius"),
        "base_topic": normalized.get("mqtt", {}).get("base_topic", "nodus"),
    }
    _emit_event(event_logger, "apply phase=response_ready status=200")
    return ItaotInitResult(
        accepted=True,
        rebooting=True,
        status_code=200,
        body=body,
        runtime_config=runtime_config,
        applied_updates=tuple(applied_updates),
        errors=(),
    )


def _write_itaot_settings_file(path, updates, *, event_logger=None):
    _emit_event(event_logger, "apply phase=persist_targets_begin")
    targets = _itaot_patch_targets(updates)
    if not targets:
        _emit_event(event_logger, "apply phase=persist_targets_error code=invalid")
        return (), ("settings_key_missing",)
    _emit_event(
        event_logger,
        (
            "apply phase=persist_targets_done targets={} sections={} "
            "password_updates={}"
        ).format(
            len(targets),
            _target_section_names(targets),
            _target_password_count(targets),
        ),
    )
    return _rewrite_itaot_settings_file(str(path or ""), updates, targets, event_logger)


def _rewrite_itaot_settings_file(path_text, updates, targets, event_logger):
    tmp_path = "{}.tmp".format(path_text)
    backup_path = "{}.bak".format(path_text)
    seen_sections = []
    found = [False] * len(targets)
    source = None
    target = None
    try:
        _emit_event(event_logger, "apply phase=persist_open_begin file=settings.toml")
        source = open(path_text, "r")
        target = open(tmp_path, "w")
        _emit_event(event_logger, "apply phase=persist_open_done")
        _copy_itaot_settings_file(
            source,
            target,
            targets,
            found,
            seen_sections,
            event_logger,
        )
        try:
            target.flush()
        except AttributeError:
            pass
        _emit_event(event_logger, "apply phase=persist_flush_done")
        try:
            source.close()
        except AttributeError:
            pass
        source = None
        try:
            target.close()
        except AttributeError:
            pass
        target = None
        _emit_event(event_logger, "apply phase=persist_close_done")
        if not _all_found(found):
            _remove_tmp(tmp_path)
            _emit_event(event_logger, "apply phase=persist_validate_error code=missing")
            return (), ("settings_key_missing",)
        if _path_size(tmp_path) <= 0:
            _remove_tmp(tmp_path)
            _emit_event(
                event_logger,
                "apply phase=persist_validate_error code=empty_tmp",
            )
            return (), ("toml_write_empty_tmp",)
        _emit_event(event_logger, "apply phase=persist_validate_done")
        _emit_event(event_logger, "apply phase=persist_rotate_begin")
        try:
            os.stat(backup_path)
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.stat(path_text)
            os.rename(path_text, backup_path)
        except OSError:
            pass
        _emit_event(event_logger, "apply phase=persist_backup_done")
        os.rename(tmp_path, path_text)
        _emit_event(event_logger, "apply phase=persist_rename_done")
    except OSError as exc:
        if source is not None:
            try:
                source.close()
            except AttributeError:
                pass
        if target is not None:
            try:
                target.close()
            except AttributeError:
                pass
        _remove_tmp(tmp_path)
        error = _persistence_error(exc)
        _emit_event(
            event_logger,
            "apply phase=persist_os_error code={} detail={}".format(
                error,
                _error_text(exc),
            ),
        )
        return (), (error,)
    return tuple(updates), ()


def _copy_itaot_settings_file(
    source, target, targets, found, seen_sections, event_logger
):
    _emit_event(event_logger, "apply phase=persist_copy_begin")
    current_section = ""
    copied = 0
    appended = 0
    last_had_ending = True
    while True:
        raw_line = source.readline()
        if raw_line == "":
            break
        section = _section_from_toml_line(raw_line)
        if section is not None:
            appended += _finish_itaot_section(
                target,
                current_section,
                targets,
                found,
                last_had_ending,
            )
            current_section = section
            if section not in seen_sections:
                seen_sections.append(section)
            target.write(raw_line)
            copied += 1
            last_had_ending = _has_line_ending(raw_line)
            continue
        last_had_ending = _write_itaot_setting_line(
            target,
            current_section,
            raw_line,
            targets,
            found,
        )
        copied += 1

    appended += _finish_itaot_section(
        target,
        current_section,
        targets,
        found,
        last_had_ending,
    )
    appended += _append_missing_itaot_sections(
        target, targets, found, seen_sections, True
    )
    _emit_event(
        event_logger,
        (
            "apply phase=persist_copy_done copied={} appended={} found={} "
            "missing={}"
        ).format(
            copied,
            appended,
            _found_count(found),
            _missing_target_names(targets, found),
        ),
    )


def _finish_itaot_section(handle, section, targets, found, last_had_ending):
    if not section:
        return 0
    if not last_had_ending:
        handle.write("\n")
    return _append_missing_itaot_targets(handle, section, targets, found)


def _write_itaot_setting_line(handle, section, raw_line, targets, found):
    key = _key_from_toml_line(raw_line)
    if not key:
        handle.write(raw_line)
        return _has_line_ending(raw_line)
    replacement = _replacement_itaot_line(
        section,
        key,
        raw_line,
        targets,
        found,
    )
    if replacement:
        handle.write(replacement)
        return _has_line_ending(replacement)
    handle.write(raw_line)
    return _has_line_ending(raw_line)


def _itaot_patch_targets(updates):
    hostname = ""
    for update in tuple(updates or ()):
        section = _clean_str(update.get("section", ""))
        key = _clean_str(update.get("key", "")).upper()
        if section == "Network" and key == "HOSTNAME":
            hostname = _clean_str(update.get("value", ""))

    targets = []
    seen = []
    for update in tuple(updates or ()):
        section = _clean_str(update.get("section", ""))
        key = _clean_str(update.get("key", "")).upper()
        if not _itaot_key_allowed(section, key):
            return ()
        target = (section, key)
        if target in seen:
            return ()
        seen.append(target)
        value = update.get("value")
        if _is_password_target(section, key):
            value = encode_password(value, hostname=hostname)
        targets.append((section, key, _format_toml_scalar(value)))
    return tuple(targets)


def _itaot_key_allowed(section, key):
    for allowed_section, allowed_key in _ITAOT_ALLOWED_KEYS:
        if section == allowed_section and key == allowed_key:
            return True
    return False


def _is_password_target(section, key):
    return (section == "Network" and key in ("PASSWORD", "AP_PASSWORD")) or (
        section == "MQTT" and key == "PASSWORD"
    )


def _target_section_names(targets):
    sections = []
    for section, _key, _formatted in tuple(targets or ()):
        if section and section not in sections:
            sections.append(section)
    return ",".join(sections) if sections else "none"


def _target_password_count(targets):
    count = 0
    for section, key, _formatted in tuple(targets or ()):
        if _is_password_target(section, key):
            count += 1
    return count


def _append_missing_itaot_targets(handle, section, targets, found):
    wrote = 0
    for index, target in enumerate(targets):
        if found[index] or section != target[0]:
            continue
        handle.write("{} = {}\n".format(target[1], target[2]))
        found[index] = True
        wrote += 1
    return wrote


def _append_missing_itaot_sections(
    handle, targets, found, seen_sections, last_had_ending
):
    wrote = 0
    for section in _target_sections(targets):
        if section in tuple(seen_sections or ()):
            continue
        if not _section_has_missing_targets(section, targets, found):
            continue
        if not last_had_ending:
            handle.write("\n")
        handle.write("\n[{}]\n".format(section))
        wrote += _append_missing_itaot_targets(handle, section, targets, found)
        last_had_ending = True
    return wrote


def _target_sections(targets):
    sections = []
    for section, _key, _formatted in tuple(targets or ()):
        if section and section not in sections:
            sections.append(section)
    return tuple(sections)


def _section_has_missing_targets(section, targets, found):
    for index, target in enumerate(targets):
        if not found[index] and section == target[0]:
            return True
    return False


def _replacement_itaot_line(section, key, raw_line, targets, found):
    key_upper = _clean_str(key).upper()
    for index, target in enumerate(targets):
        if found[index]:
            continue
        if section == target[0] and key_upper == target[1]:
            found[index] = True
            return "{} = {}{}".format(target[1], target[2], _line_ending(raw_line))
    return ""


def _section_from_toml_line(raw_line):
    body = str(raw_line or "").split("#", 1)[0].strip()
    if body.startswith("[") and body.endswith("]"):
        return body[1:-1].strip()
    return None


def _key_from_toml_line(raw_line):
    body = str(raw_line or "").split("#", 1)[0]
    if "=" not in body:
        return ""
    return body.split("=", 1)[0].strip()


def _format_toml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        text = repr(float(value))
        if "." not in text and "e" not in text.lower():
            text += ".0"
        return text
    text = str(value or "")
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def _line_ending(raw_line):
    text = str(raw_line or "")
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\n"):
        return "\n"
    return ""


def _has_line_ending(raw_line):
    text = str(raw_line or "")
    return text.endswith("\n")


def _found_count(found):
    count = 0
    for item in tuple(found or ()):
        if item:
            count += 1
    return count


def _missing_target_names(targets, found):
    missing = []
    for index, target in enumerate(tuple(targets or ())):
        if not found[index]:
            missing.append("{}.{}".format(target[0], target[1]))
    return ",".join(missing) if missing else "none"


def _all_found(found):
    for item in tuple(found or ()):
        if not item:
            return False
    return True


def _join_settings_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


def _path_size(path):
    try:
        return os.stat(str(path or ""))[6]
    except OSError:
        return 0


def _remove_tmp(path):
    try:
        os.remove(str(path or ""))
    except OSError:
        pass


def _persistence_error(exc):
    text = str(exc or "").lower()
    if "read-only" in text or "errno 30" in text:
        return "read_only_filesystem"
    if "no space" in text or "errno 28" in text:
        return "no_space_left"
    return "persistence_failed"


def _display_metrics_for_sensor(sensor):
    if not getattr(sensor, "present", False):
        return ()
    configured = tuple(
        value
        for value in tuple(
            getattr(getattr(sensor, "display", None), "metrics", ()) or ()
        )
        if _clean_str(value)
    )
    if configured:
        return configured
    return _DISPLAY_METRICS_BY_DEVICE.get(
        _clean_str(getattr(sensor, "device", "")).lower(), ()
    )


def _calibration_status(sensor):
    offsets = []
    for calibration in (
        getattr(sensor, "calibration_system", None),
        getattr(sensor, "calibration_device", None),
    ):
        if calibration is None:
            continue
        for name in (
            "temp_offset",
            "rh_offset",
            "co2_offset",
            "aqi_offset",
            "gas_offset",
            "lux_offset",
            "ppfd_offset",
            "apvpd_temp_cal_val",
            "apvpd_rh_cal_val",
            "soil_temp_cal_val",
            "soil_moist_cal_val",
            "soil_ph_cal_val",
            "soil_ec_cal_val",
        ):
            try:
                offsets.append(float(getattr(calibration, name, 0.0) or 0.0))
            except Exception:
                offsets.append(0.0)
    calibrated = any(value != 0.0 for value in offsets)
    return {
        "calibrated": calibrated,
        "status": "Calibrated" if calibrated else "Not calibrated",
    }


def _switch_channel_state(channel, switch_states):
    snapshot = {}
    if isinstance(switch_states, dict):
        snapshot = (
            switch_states.get(channel.key)
            or switch_states.get(channel.channel_id)
            or {}
        )
    if isinstance(snapshot, dict) and "state" in snapshot:
        return bool(snapshot.get("state"))
    return bool(getattr(channel, "last_state", False))


def build_itaot_meta_payload(
    runtime_config, *, version, ip_address="", switch_states=None
):
    """Build the compact onboarding metadata payload."""
    sensor = runtime_config.sensor
    switch = runtime_config.switch
    device_id = sensor.sensor_id or switch.device_id or runtime_config.network.hostname
    members = []
    if sensor.sensor_id:
        members.append(sensor.sensor_id)
    for channel in switch.channels:
        if channel.channel_id:
            members.append(channel.channel_id)

    sensor_block = {
        "present": bool(sensor.present),
        "device": sensor.device,
        "sensor_id": sensor.sensor_id,
        "serial": sensor.serial_number,
        "location": sensor.location,
        "active_sensor_file": sensor.active_config_file,
        "display_metrics": list(_display_metrics_for_sensor(sensor)),
        "calibration": _calibration_status(sensor),
    }
    hardware = str(getattr(sensor, "hardware", "") or "").strip()
    if sensor.present and hardware:
        sensor_block["hardware"] = hardware
    if not sensor.present:
        sensor_block["display_metrics"] = []

    channels = []
    for index, channel in enumerate(switch.channels, start=1):
        channels.append(
            {
                "index": index,
                "label": channel.label,
                "channel_id": channel.channel_id,
                "state": _switch_channel_state(channel, switch_states),
                "enabled": True,
            }
        )

    switch_block = {
        "present": bool(switch.present),
        "device_id": switch.device_id,
        "serial": switch.serial_number,
        "location": switch.location,
        "channels": channels,
    }

    location_group = {
        "id": sensor.serial_number or switch.serial_number or device_id,
        "members": members,
        "label": " ".join(member for member in members if member),
    }

    return {
        "schema": ITAOT_META_SCHEMA,
        "version": _clean_str(version),
        "origin": "nodus",
        "device_id": device_id,
        "network": {
            "hostname": runtime_config.network.hostname,
            "ssid": runtime_config.network.ssid,
            "ipv4addr": _clean_str(ip_address),
        },
        "device": {
            "type": "nodus",
            "capabilities": _bool_capabilities(runtime_config),
        },
        "endpoints": {},
        "sensor": sensor_block,
        "switch": switch_block,
        "location_group": location_group,
    }
