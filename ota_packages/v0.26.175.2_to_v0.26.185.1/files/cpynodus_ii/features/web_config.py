"""Apply configuration changes requested through the web interface.

These helpers bridge web forms into validated runtime and persisted updates,
including switch overrides and settings writes that may require a reboot.
"""

from dataclasses import dataclass

from cpynodus_ii.core.settings import Settings


@dataclass(frozen=True)
class WebConfigDecision:
    """Describe how one web configuration update should be handled."""

    section: str
    key: str
    value: object
    accepted: bool
    applies_live: bool
    requires_restart: bool
    reason: str = ""


@dataclass(frozen=True)
class WebConfigResult:
    """Describe the outcome of one or more web configuration updates."""

    runtime_config: object
    applied_updates: tuple
    live_updates: tuple
    restart_required_updates: tuple
    ignored_updates: tuple
    errors: tuple = ()
    persistence_mode: str = ""


def classify_web_update(update):
    """Classify a web update as live, restart-required, or rejected."""
    section = str(update.get("section", "") or "").strip()
    key = str(update.get("key", "") or "").strip()
    value = update.get("value")
    key_upper = key.upper()

    if not (section and key):
        return WebConfigDecision(
            section, key, value, False, False, False, "missing_section_or_key"
        )

    if section == "Sensor" and key_upper == "LOCATION":
        return WebConfigDecision(section, key, value, True, True, False)
    if section == "Switch" and key_upper in {
        "SWITCH_LOCATION",
        "SWITCH_1_LABEL",
        "SWITCH_2_LABEL",
    }:
        return WebConfigDecision(section, key, value, True, True, False)
    if section in {"Display", "Display.Style"} and _display_metric_index(key_upper):
        return WebConfigDecision(section, key, value, True, True, False)
    if section == "NPK" and key_upper in {"N_TARGET", "P_TARGET", "K_TARGET"}:
        return WebConfigDecision(section, key, value, True, True, False)
    if section in {
        "Calibration.System",
        "Calibration.Device",
    } and _calibration_attr_name(section, key_upper):
        return WebConfigDecision(section, key, value, True, True, False)
    if section == "Switch" and key_upper in {
        "SWITCH_1_LAST_STATE",
        "SWITCH_2_LAST_STATE",
    }:
        return WebConfigDecision(section, key, value, True, True, False)
    if section == "Time" and key_upper in {
        "TZ",
        "TZ_OFFSET",
        "TZ_NAME",
        "NTP_SERVER",
        "NTP_SERVER_IP",
    }:
        return WebConfigDecision(section, key, value, True, True, False)

    if section == "Network" and key_upper in {
        "SSID",
        "PASSWORD",
        "HOSTNAME",
        "HTTPPORT",
        "AP_CHANNEL",
    }:
        return WebConfigDecision(
            section, key, value, True, False, True, "network_restart_required"
        )
    if section == "MQTT" and key_upper in {
        "BROKER",
        "BROKER_IP",
        "PORT",
        "USE_TLS",
        "BASE_TOPIC",
        "USERNAME",
        "PASSWORD",
    }:
        return WebConfigDecision(
            section, key, value, True, False, True, "mqtt_restart_required"
        )
    if section == "Profile" and key_upper == "ACTIVE_PROFILE":
        return WebConfigDecision(
            section, key, value, True, False, True, "profile_restart_required"
        )
    if section == "HomeAssistant" and key_upper in {
        "DISCOVERY_PREFIX",
        "BASE_TOPIC",
        "PUBLISH_DISCOVERY_RETAIN",
        "PUBLISH_STATE_RETAIN",
        "PUBLISH_LEGACY_SENSOR_TOPIC",
    }:
        return WebConfigDecision(
            section, key, value, True, False, True, "homeassistant_restart_required"
        )

    return WebConfigDecision(
        section, key, value, False, False, False, "unsupported_update"
    )


def apply_web_config_updates(runtime_config, updates, *, settings_root=None):
    """Persist updates and apply live-safe changes to runtime config."""
    normalized_updates = []
    decisions = []
    for update in updates or ():
        decision = classify_web_update(update)
        decisions.append(decision)
        if decision.accepted:
            normalized_updates.append(
                {
                    "section": decision.section,
                    "key": decision.key,
                    "value": decision.value,
                }
            )

    if not normalized_updates:
        return WebConfigResult(
            runtime_config=runtime_config,
            applied_updates=(),
            live_updates=(),
            restart_required_updates=(),
            ignored_updates=tuple(_decision_payloads(decisions, accepted=False)),
            errors=("no_supported_updates",),
            persistence_mode="",
        )

    current = runtime_config
    applied_updates = tuple(normalized_updates)
    persistence_errors = ()
    if settings_root is not None:
        current, applied_updates, persistence_errors = _persist_web_updates(
            runtime_config,
            normalized_updates,
            settings_root=settings_root,
        )
    live_updates = []
    restart_required_updates = []
    ignored_updates = []

    for update in applied_updates:
        decision = classify_web_update(update)
        if not decision.accepted:
            ignored_updates.append(_decision_payload(decision))
            continue
        if decision.applies_live:
            from cpynodus_ii.features.runtime_config_update import (
                apply_runtime_config_update,
            )

            updated_runtime = apply_runtime_config_update(
                current,
                decision.section,
                decision.key,
                decision.value,
            )
            if updated_runtime is not None:
                current = updated_runtime
            live_updates.append(_decision_payload(decision))
            continue
        if decision.requires_restart:
            restart_required_updates.append(_decision_payload(decision))
            continue
        ignored_updates.append(_decision_payload(decision))

    for decision in decisions:
        if decision.accepted:
            continue
        ignored_updates.append(_decision_payload(decision))

    return WebConfigResult(
        runtime_config=current,
        applied_updates=tuple(applied_updates),
        live_updates=tuple(live_updates),
        restart_required_updates=tuple(restart_required_updates),
        ignored_updates=tuple(ignored_updates),
        errors=tuple(persistence_errors),
        persistence_mode="volatile"
        if persistence_errors
        else "persisted"
        if settings_root is not None
        else "",
    )


def apply_web_switch_override(
    runtime_config, switch_service, *, channel_id=None, channel_key=None, state
):
    """Apply a live switch override and reflect it into runtime config."""
    from cpynodus_ii.features.runtime_config_update import apply_runtime_config_update
    from cpynodus_ii.features.switch_service import apply_switch_state

    apply_result = apply_switch_state(
        switch_service,
        channel_id=channel_id,
        channel_key=channel_key,
        state=state,
    )
    if apply_result.phase != "ready":
        return runtime_config, apply_result
    channel_key = apply_result.key or channel_key or ""
    if channel_key:
        key = "{}_LAST_STATE".format(channel_key)
        updated_runtime = apply_runtime_config_update(
            runtime_config,
            "Switch",
            key,
            bool(apply_result.applied_state),
        )
        if updated_runtime is not None:
            runtime_config = updated_runtime
    return runtime_config, apply_result


def _persist_web_updates(runtime_config, updates, *, settings_root):
    persisted_runtime_config, persisted_updates, persistence_errors = (
        Settings.apply_updates_to_directory(
            settings_root,
            runtime_config,
            updates,
            reload_runtime=False,
        )
    )
    _ = persisted_runtime_config
    return runtime_config, tuple(persisted_updates), tuple(persistence_errors)


def _decision_payload(decision):
    payload = {
        "section": decision.section,
        "key": decision.key,
        "value": decision.value,
        "applies_live": decision.applies_live,
        "requires_restart": decision.requires_restart,
    }
    if decision.reason:
        payload["reason"] = decision.reason
    return payload


def _decision_payloads(decisions, *, accepted):
    for decision in decisions:
        if bool(decision.accepted) is bool(accepted):
            yield _decision_payload(decision)


def _calibration_attr_name(section, key_upper):
    if section not in {"Calibration.System", "Calibration.Device"}:
        return ""
    mapping = {
        "TEMP_OFFSET": "temp_offset",
        "RH_OFFSET": "rh_offset",
        "CO2_OFFSET": "co2_offset",
        "AQI_OFFSET": "aqi_offset",
        "GAS_OFFSET": "gas_offset",
        "LUX_OFFSET": "lux_offset",
        "PPFD_OFFSET": "ppfd_offset",
        "SOIL_TEMP_CAL_VAL": "soil_temp_cal_val",
        "SOIL_MOIST_CAL_VAL": "soil_moist_cal_val",
        "SOIL_TEMP_MOIST_VAL": "soil_moist_cal_val",
        "SOIL_PH_CAL_VAL": "soil_ph_cal_val",
        "SOIL_EC_CAL_VAL": "soil_ec_cal_val",
        "ALTITUDE_METERS": "altitude_meters",
    }
    return mapping.get(key_upper, "")


def _display_metric_index(key_upper):
    if not key_upper.startswith("METRIC_"):
        return 0
    try:
        index = int(key_upper.split("_", 1)[1])
    except Exception:
        return 0
    if 1 <= index <= 6:
        return index
    return 0
