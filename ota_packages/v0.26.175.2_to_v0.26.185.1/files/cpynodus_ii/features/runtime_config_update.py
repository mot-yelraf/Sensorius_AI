"""Apply live-safe runtime configuration changes."""

from dataclasses import replace

from cpynodus_ii.core.config import SoilNPKConfig
from cpynodus_ii.core.settings import Settings


def apply_runtime_config_updates(runtime_config, updates, *, settings_root=None):
    """Apply supported runtime updates and return the new config plus writes."""
    normalized_updates = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        if section == "MQTT" and key.upper() == "BROKER_IP_ALT":
            continue
        if (
            section == "Sensor"
            and key.upper() == "LOCATION"
            and not runtime_config.sensor.present
            and runtime_config.switch.present
        ):
            normalized_updates.append(
                {
                    "section": "Switch",
                    "key": "SWITCH_LOCATION",
                    "value": update.get("value"),
                }
            )
            continue
        normalized_updates.append(update)
    updates = tuple(normalized_updates)

    if settings_root is not None:
        persisted_runtime_config, persisted_updates, persistence_errors = (
            Settings.apply_updates_to_directory(
                settings_root,
                runtime_config,
                updates,
                reload_runtime=False,
            )
        )
        _ = persisted_runtime_config
        current = runtime_config
        for update in persisted_updates:
            section = str(update.get("section", "") or "").strip()
            key = str(update.get("key", "") or "").strip()
            value = update.get("value")
            updated = apply_runtime_config_update(current, section, key, value)
            if updated is None:
                continue
            current = updated
        return current, tuple(persisted_updates), tuple(persistence_errors)
    current = runtime_config
    applied_updates = []
    for update in updates:
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        value = update.get("value")
        updated = apply_runtime_config_update(current, section, key, value)
        if updated is None:
            continue
        current = updated
        applied_updates.append(
            {
                "section": section,
                "key": key,
                "value": value,
            }
        )
    return current, tuple(applied_updates), ()


def apply_runtime_config_update(runtime_config, section, key, value):
    """Apply one supported live-safe runtime config update."""
    key_upper = key.upper()
    if section == "Network" and key_upper == "HOSTNAME":
        return replace(
            runtime_config,
            network=replace(runtime_config.network, hostname=str(value or "").strip()),
        )
    if section == "Network" and key_upper == "AP_CHANNEL":
        return replace(
            runtime_config,
            network=replace(runtime_config.network, ap_channel=int(value or 6)),
        )
    if section == "Network" and key_upper == "HTTPPORT":
        return replace(
            runtime_config,
            network=replace(runtime_config.network, http_port=int(value or 8000)),
        )
    if section == "MQTT" and key_upper == "BROKER":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, broker=str(value or "").strip()),
        )
    if section == "MQTT" and key_upper == "BROKER_IP":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, broker_ip=str(value or "").strip()),
        )
    if section == "MQTT" and key_upper == "PORT":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, port=int(value or 1883)),
        )
    if section == "MQTT" and key_upper == "BASE_TOPIC":
        return replace(
            runtime_config,
            mqtt=replace(runtime_config.mqtt, base_topic=str(value or "").strip()),
        )
    if section == "HomeAssistant" and key_upper == "DISCOVERY_PREFIX":
        return replace(
            runtime_config,
            homeassistant=replace(
                runtime_config.homeassistant,
                discovery_prefix=str(value or "").strip(),
            ),
        )
    if section == "HomeAssistant" and key_upper == "BASE_TOPIC":
        return replace(
            runtime_config,
            homeassistant=replace(
                runtime_config.homeassistant,
                base_topic=str(value or "").strip(),
            ),
        )
    if section == "Time" and key_upper == "TZ":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz=str(value or "").strip()),
        )
    if section == "Time" and key_upper == "TZ_OFFSET":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz_offset=int(value or 0)),
        )
    if section == "Time" and key_upper == "TZ_NAME":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, tz_name=str(value or "").strip()),
        )
    if section == "Time" and key_upper == "NTP_SERVER":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, ntp_server=str(value or "").strip()),
        )
    if section == "Time" and key_upper == "NTP_SERVER_IP":
        return replace(
            runtime_config,
            time=replace(runtime_config.time, ntp_server_ip=str(value or "").strip()),
        )
    if section == "Profile" and key_upper == "ACTIVE_PROFILE":
        return replace(runtime_config, active_profile=str(value or "").strip())
    if section == "Sensor" and key_upper == "LOCATION":
        if not runtime_config.sensor.present and runtime_config.switch.present:
            return replace(
                runtime_config,
                switch=replace(
                    runtime_config.switch,
                    location=str(value or "").strip(),
                ),
            )
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, location=str(value or "").strip()),
        )
    if section == "Sensor" and key_upper == "SENSOR_ID":
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, sensor_id=str(value or "").strip()),
        )
    if section == "Sensor" and key_upper == "SERIAL_NUM":
        return replace(
            runtime_config,
            sensor=replace(
                runtime_config.sensor, serial_number=str(value or "").strip()
            ),
        )
    if section == "Switch" and key_upper == "SWITCH_LOCATION":
        return replace(
            runtime_config,
            switch=replace(runtime_config.switch, location=str(value or "").strip()),
        )
    channel_index = _switch_channel_index_from_key(key_upper)
    if section == "Switch" and channel_index:
        return _replace_switch_channel(runtime_config, channel_index, key_upper, value)
    display_index = _display_metric_index(key_upper)
    if section == "Display" and display_index:
        metrics = list(runtime_config.sensor.display.metrics)
        metrics[display_index - 1] = str(value or "").strip()
        return replace(
            runtime_config,
            sensor=replace(
                runtime_config.sensor,
                display=replace(runtime_config.sensor.display, metrics=tuple(metrics)),
            ),
        )
    if section == "Display.Style" and display_index:
        styles = list(runtime_config.sensor.display.styles)
        styles[display_index - 1] = str(value or "").strip()
        return replace(
            runtime_config,
            sensor=replace(
                runtime_config.sensor,
                display=replace(runtime_config.sensor.display, styles=tuple(styles)),
            ),
        )
    npk_attr = _npk_attr_name(section, key_upper)
    if npk_attr:
        if runtime_config.sensor.device != "soil":
            return None
        soil_npk = runtime_config.sensor.soil_npk or SoilNPKConfig()
        return replace(
            runtime_config,
            sensor=replace(
                runtime_config.sensor,
                soil_npk=replace(soil_npk, **{npk_attr: float(value or 0.0)}),
            ),
        )
    calibration_attr = _calibration_attr_name(section, key_upper)
    if calibration_attr and section == "Calibration.System":
        calibration = replace(
            runtime_config.sensor.calibration_system,
            **{calibration_attr: float(value or 0.0)},
        )
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, calibration_system=calibration),
        )
    if calibration_attr and section == "Calibration.Device":
        calibration = replace(
            runtime_config.sensor.calibration_device,
            **{calibration_attr: float(value or 0.0)},
        )
        return replace(
            runtime_config,
            sensor=replace(runtime_config.sensor, calibration_device=calibration),
        )
    return None


def _npk_attr_name(section, key_upper):
    if section != "NPK":
        return ""
    mapping = {
        "N_TARGET": "n_target",
        "P_TARGET": "p_target",
        "K_TARGET": "k_target",
    }
    return mapping.get(key_upper, "")


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


def _switch_channel_index_from_key(key_upper):
    if key_upper.startswith("SWITCH_1_"):
        return 1
    if key_upper.startswith("SWITCH_2_"):
        return 2
    return 0


def _replace_switch_channel(runtime_config, channel_index, key_upper, value):
    channels = list(runtime_config.switch.channels)
    position = channel_index - 1
    if position >= len(channels):
        return None
    channel = channels[position]
    if key_upper.endswith("_LABEL"):
        channels[position] = replace(channel, label=str(value or "").strip())
    elif key_upper.endswith("_LAST_STATE"):
        channels[position] = replace(channel, last_state=bool(value))
    else:
        return None
    return replace(
        runtime_config,
        switch=replace(runtime_config.switch, channels=tuple(channels)),
    )


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
