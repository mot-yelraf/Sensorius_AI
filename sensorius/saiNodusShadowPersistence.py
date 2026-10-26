"""Persist Nodus metadata into local system, sensor, and switch shadows.

Transport, discovery caches, command correlation, and subscription state remain in
MQTT ingest. These writers preserve existing templates, partial-update semantics,
and idempotent settings-manager writes without owning an MQTT client.
"""

import json
import re
import tomllib
from collections import OrderedDict
from . import saiNodusMeta as nodus_meta
from .saiRuntimePaths import resolve_runtime_base_dir
from .saiUtils import printDM, debug_enabled

MODULE = "saiMQTTIngest"
DEBUG = debug_enabled(MODULE)

def _merge_nodus_pressure_calibration(doc: dict, calibration: object) -> bool:
    """Mirror an explicitly advertised device altitude without inventing defaults."""
    if not isinstance(calibration, dict):
        return False
    device = calibration.get("Device")
    if not isinstance(device, dict):
        return False
    raw = device.get("ALTITUDE_METERS")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        altitude = float(raw)
    except (TypeError, ValueError):
        return False
    if not -500 <= altitude <= 10000:
        return False
    for name in ("Calibration", "Device"):
        if not isinstance(doc.get(name), dict):
            doc[name] = OrderedDict()
        doc = doc[name]
    if doc.get("ALTITUDE_METERS") == altitude:
        return False
    doc["ALTITUDE_METERS"] = altitude
    return True


def persist_metadata(
    data_logger,
    info: dict,
    hostname: str,
    sensors: list[dict],
    switches: list[dict],
) -> None:
    """Materialize system, sensor, and switch settings from normalized metadata.

    Preserve existing defaults and partial-update behavior for both bootstrap
    descriptors and retained Nodus metadata. The logger is used only to prune
    obsolete switch identities after authoritative channel updates.
    """
    from pathlib import Path
    try:
        from .saiSensorSettingsManager import SensorSettingsManager
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiSettings import saiSettings
    except Exception as exc:
        if DEBUG:
            printDM(f"[itaot-settings] import error: {exc}", location=MODULE)
        return

    def _strip_local(host: str) -> str:
        host = (host or "").strip()
        return host[:-6] if host.endswith(".local") else host

    def _is_soil_device(name: str) -> bool:
        dn = (name or "").strip().lower()
        return dn.startswith("soil") or dn in {"soil", "soil4in1", "rs485", "modbus"}

    def _parse_simple_toml(path: Path) -> OrderedDict:
        if tomllib:
            try:
                with path.open("rb") as f:
                    data = tomllib.load(f) or {}
                return SensorSettingsManager._to_ordered(data)
            except Exception:
                pass
        settings = OrderedDict()
        section = None
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1]
                        settings[section] = OrderedDict()
                    elif "=" in line and section:
                        key, value = map(str.strip, line.split("=", 1))
                        if value.startswith('[') and value.endswith(']'):
                            value = json.loads(value.replace("'", '"'))
                        elif value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        else:
                            lv = value.lower()
                            if lv == "true":
                                value = True
                            elif lv == "false":
                                value = False
                            else:
                                try:
                                    value = float(value) if "." in value else int(value)
                                except Exception:
                                    pass
                        settings[section][key] = value
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] parse error for {path}: {exc}", location=MODULE)
        return settings

    def _emit_simple_toml(path: Path, settings: OrderedDict) -> None:
        def _toml_escape(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return f"{v}"
            if isinstance(v, list):
                return json.dumps(v)
            s = "" if v is None else str(v)
            s = s.replace("\\", "\\\\").replace('"', '\\"')
            return f"\"{s}\""

        lines = []
        for section, pairs in (settings or {}).items():
            lines.append(f"[{section}]\n")
            for key, value in (pairs or {}).items():
                lines.append(f"{key} = {_toml_escape(value)}\n")
            lines.append("\n")
        text = "".join(lines)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)

    def _display_defaults_for_device(device: str, hardware: str = "") -> list[str]:
        base_device = (device or "").split("_", 1)[0].lower()
        hardware_key = str(hardware or "").strip().lower()
        if hardware_key in {"sgp30", "sgp40", "sgp41"}:
            base_device = hardware_key
        mapping: dict[str, list[str]] = {
            "apvpd": ["Ambient VPD", "Temperature", "Rel-Humidity", "Plant VPD", "Plant Temperature", "Plant Rel-Humidity"],
            "aqi":   ["Air Quality", "Temperature", "Rel-Humidity", "Ambient VPD", "Dewpoint Deficit", "dewVPD Risk"],
            "avpd":  ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure", "Dewpoint Deficit", "dewVPD Risk"],
            "aht":   ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "aht10": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "ahtx0": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
            "co2":   ["CO2", "Temperature", "Rel-Humidity", "Ambient VPD", "Dewpoint Deficit", "dewVPD Risk"],
            "lux":   ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
            "veml":  ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
            "voc":   ["VOC Index", "NOx Index", "", "", "", ""],
            "sgp30": ["Equivalent CO2", "TVOC", "", "", "", ""],
            "sgp40": ["VOC Index", "", "", "", "", ""],
            "sgp41": ["VOC Index", "NOx Index", "", "", "", ""],
            "sgp4x": ["VOC Index", "NOx Index", "", "", "", ""],
            "soil":  ["Soil Moisture", "Soil Moisture Deficit", "Soil Stress Index", "Soil Temp_C", "Soil pH", "Soil EC"],
        }
        return mapping.get(base_device, ["", "", "", "", "", ""])

    def _display_block_is_blank(display: dict | None) -> bool:
        if not isinstance(display, dict):
            return True
        return not any(str(display.get(f"METRIC_{idx}", "")).strip() for idx in range(1, 7))

    def _set_if_present(section: OrderedDict, key: str, value, *, allow_blank: bool = False) -> bool:
        if not isinstance(section, dict):
            return False
        if value is None:
            return False
        if isinstance(value, str):
            if not allow_blank and not value.strip():
                return False
            value = value.strip() if not allow_blank else value
        if section.get(key) == value:
            return False
        section[key] = value
        return True

    def _is_unknown_loc(val: str | None) -> bool:
        v = str(val or "").strip().lower()
        return v in ("", "unknown", "n/a", "na", "none", "-")

    def _canonical_location(value: str | None) -> str:
        loc = str(value or "").strip()
        return loc if loc and not _is_unknown_loc(loc) else "Unknown"

    # ---- system_settings/<HOSTNAME>/settings.toml ----
    system_id = _strip_local(str((info or {}).get("HOSTNAME") or hostname or ""))
    if system_id:
        system_base_dir = resolve_runtime_base_dir(saiSettings.DEFAULT_BASE_DIR)
        sys_path = system_base_dir / system_id / saiSettings.STANDARD_FILENAME
        existed_before = sys_path.exists()
        nodus_tpl = system_base_dir / "factory_nodus" / f"{saiSettings.STANDARD_FILENAME}.def"
        fallback_tpl = system_base_dir / "factory" / saiSettings.STANDARD_FILENAME
        tpl_path = nodus_tpl if nodus_tpl.exists() else (fallback_tpl if fallback_tpl.exists() else None)

        settings_doc = _parse_simple_toml(sys_path) if existed_before else (_parse_simple_toml(tpl_path) if tpl_path else OrderedDict())
        changed = not existed_before
        for block_name in ("Network", "Profile", "MQTT", "HomeAssistant", "Time"):
            if block_name not in settings_doc or not isinstance(settings_doc.get(block_name), dict):
                settings_doc[block_name] = OrderedDict()
                changed = True

        net_block = info.get("Network") if isinstance(info, dict) else None
        if isinstance(net_block, dict):
            for k, v in net_block.items():
                changed = _set_if_present(settings_doc["Network"], str(k), v, allow_blank=str(k).upper() in {"PASSWORD"}) or changed
        changed = _set_if_present(settings_doc["Network"], "HOSTNAME", system_id) or changed

        profile_block = info.get("Profile") if isinstance(info, dict) else None
        if isinstance(profile_block, dict):
            for k, v in profile_block.items():
                changed = _set_if_present(settings_doc["Profile"], str(k), v) or changed

        mqtt_block = info.get("MQTT") if isinstance(info, dict) else None
        if isinstance(mqtt_block, dict):
            for k, v in mqtt_block.items():
                changed = _set_if_present(settings_doc["MQTT"], str(k), v, allow_blank=str(k).upper() in {"USERNAME", "PASSWORD"}) or changed

        ha_block = info.get("HomeAssistant") if isinstance(info, dict) else None
        if isinstance(ha_block, dict):
            for k, v in ha_block.items():
                changed = _set_if_present(settings_doc["HomeAssistant"], str(k), v) or changed

        time_block = info.get("Time") if isinstance(info, dict) else None
        if isinstance(time_block, dict):
            for k, v in time_block.items():
                changed = _set_if_present(settings_doc["Time"], str(k), v) or changed

        if changed:
            _emit_simple_toml(sys_path, settings_doc)
            if DEBUG:
                verb = "updated" if existed_before else "created"
                printDM(f"[itaot-settings] {verb} system settings for {system_id}", location=MODULE)

    # ---- sensor_settings/<SENSOR_ID>/sensor.toml ----
    try:
        sensor_mgr = SensorSettingsManager()
        for s in (sensors or []):
            sensor_id = str(s.get("sensor_id") or "").strip()
            if not sensor_id:
                continue
            device_type = (s.get("device_type") or "picow")
            device_name = nodus_meta.infer_sensor_device_name(
                s.get("device") or s.get("sensor_type"),
                sensor_id,
            )
            location = (s.get("location") or "Unknown")
            serial = (s.get("serial") or "")
            board_type = str(s.get("mcu") or s.get("MCU") or "").strip()
            sensor_hardware = str(
                s.get("hardware")
                or s.get("HARDWARE")
                or s.get("sensor_hardware")
                or s.get("SENSOR_HARDWARE")
                or ""
            ).strip()
            physical_device_id = str(
                s.get("physical_device_id")
                or s.get("device_id")
                or system_id
                or ""
            ).strip()
            config_file = str(
                s.get("config_file")
                or s.get("name")
                or ""
            ).strip()
            remote_display_metrics = nodus_meta.normalize_display_metrics(
                s.get("display_metrics", s.get("metrics")),
                preserve_slots=True,
            )
            remote_display_styles = nodus_meta.normalize_display_styles(
                s.get("display_styles") or s.get("styles")
            )
            new_path, legacy_path = sensor_mgr.get_candidate_paths(sensor_id)

            existing_path = new_path if new_path.exists() else (legacy_path if legacy_path.exists() else None)
            if existing_path:
                try:
                    data = sensor_mgr.load(sensor_id)
                except Exception:
                    data = OrderedDict()
                changed = False

                if "Sensor" not in data or not isinstance(data["Sensor"], dict):
                    data["Sensor"] = OrderedDict()
                sb = data["Sensor"]
                if device_name and str(sb.get("DEVICE", "") or "").strip() != device_name:
                    sb["DEVICE"] = device_name
                    changed = True
                if serial and str(sb.get("SERIAL_NUM", "") or "").strip() != serial:
                    sb["SERIAL_NUM"] = serial
                    changed = True
                if board_type and str(sb.get("MCU", "") or "").strip() != board_type:
                    sb["MCU"] = board_type
                    changed = True
                if sensor_hardware and str(sb.get("HARDWARE", "") or "").strip() != sensor_hardware:
                    sb["HARDWARE"] = sensor_hardware
                    changed = True
                canonical_location = _canonical_location(location)
                if str(sb.get("LOCATION", "") or "").strip() != canonical_location:
                    sb["LOCATION"] = canonical_location
                    changed = True
                if sensor_id and str(sb.get("SENSOR_ID", "") or "").strip() != sensor_id:
                    sb["SENSOR_ID"] = sensor_id
                    changed = True
                if device_type and str(sb.get("TYPE", "") or "").strip() != device_type:
                    sb["TYPE"] = device_type
                    changed = True
                if "Nodus" not in data or not isinstance(data["Nodus"], dict):
                    data["Nodus"] = OrderedDict()
                    changed = True
                nodus_block = data["Nodus"]
                if (
                    physical_device_id
                    and str(nodus_block.get("DEVICE_ID", "") or "").strip()
                    != physical_device_id
                ):
                    nodus_block["DEVICE_ID"] = physical_device_id
                    changed = True
                if (
                    config_file
                    and str(nodus_block.get("CONFIG_FILE", "") or "").strip()
                    != config_file
                ):
                    nodus_block["CONFIG_FILE"] = config_file
                    changed = True

                if "Display" not in data or not isinstance(data["Display"], dict):
                    data["Display"] = OrderedDict()
                display = data["Display"]
                # Missing compact metadata is not a reset to factory defaults.
                chosen_metrics = remote_display_metrics or (
                    [str(display.get(f"METRIC_{idx}", "") or "") for idx in range(1, 7)]
                    if any(f"METRIC_{idx}" in display for idx in range(1, 7))
                    else _display_defaults_for_device(device_name or device_type, sensor_hardware)
                )
                for idx in range(6):
                    metric_key = f"METRIC_{idx + 1}"
                    metric_val = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                    if str(display.get(metric_key, "") or "") != metric_val:
                        display[metric_key] = metric_val
                        changed = True

                if remote_display_styles:
                    style_block = display.get("Style")
                    if not isinstance(style_block, dict):
                        style_block = OrderedDict()
                        display["Style"] = style_block
                        changed = True
                    for idx in range(6):
                        style_key = f"METRIC_{idx + 1}"
                        style_val = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"
                        if str(style_block.get(style_key, "") or "") != style_val:
                            style_block[style_key] = style_val
                            changed = True

                changed = _merge_nodus_pressure_calibration(data, s.get("calibration")) or changed
                if changed:
                    sensor_mgr.save(sensor_id, data)
                    if DEBUG:
                        printDM(f"[itaot-settings] updated existing sensor settings for {sensor_id}", location=MODULE)
                continue

            nodus_dir = sensor_mgr.base_dir / "factory_nodus"
            tpl_soil = nodus_dir / "sensor_soil.toml.def"
            tpl_i2c = nodus_dir / "sensor_i2c.toml.def"
            use_soil = _is_soil_device(device_name) or _is_soil_device(sensor_id) or _is_soil_device(device_type)

            tpl_path = tpl_soil if (use_soil and tpl_soil.exists()) else (tpl_i2c if tpl_i2c.exists() else None)
            if tpl_path:
                data = sensor_mgr._parse_toml_from_disk(tpl_path)
                if "Sensor" not in data or not isinstance(data["Sensor"], dict):
                    data["Sensor"] = OrderedDict()
                sb = data["Sensor"]
                if device_name:
                    sb["DEVICE"] = device_name
                if device_type:
                    sb["TYPE"] = device_type
                sb["SENSOR_ID"] = sensor_id
                sb["LOCATION"] = _canonical_location(location)
                if serial:
                    sb["SERIAL_NUM"] = serial
                if board_type:
                    sb["MCU"] = board_type
                if sensor_hardware:
                    sb["HARDWARE"] = sensor_hardware
                data["Nodus"] = OrderedDict()
                data["Nodus"]["DEVICE_ID"] = physical_device_id
                data["Nodus"]["CONFIG_FILE"] = config_file

                if "Display" not in data or not isinstance(data["Display"], dict):
                    data["Display"] = OrderedDict()
                display = data["Display"]
                chosen_metrics = remote_display_metrics or _display_defaults_for_device(
                    device_name or device_type,
                    sensor_hardware,
                )
                for idx in range(6):
                    display[f"METRIC_{idx + 1}"] = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                if remote_display_styles:
                    style_block = display.get("Style")
                    if not isinstance(style_block, dict):
                        style_block = OrderedDict()
                        display["Style"] = style_block
                    for idx in range(6):
                        style_block[f"METRIC_{idx + 1}"] = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"

                _merge_nodus_pressure_calibration(data, s.get("calibration"))
                sensor_mgr.save(sensor_id, data)
            else:
                data = OrderedDict()
                data["Sensor"] = OrderedDict()
                sb = data["Sensor"]
                if device_name:
                    sb["DEVICE"] = device_name
                if device_type:
                    sb["TYPE"] = device_type
                sb["SENSOR_ID"] = sensor_id
                sb["LOCATION"] = _canonical_location(location)
                if serial:
                    sb["SERIAL_NUM"] = serial
                if board_type:
                    sb["MCU"] = board_type
                if sensor_hardware:
                    sb["HARDWARE"] = sensor_hardware

                data["Nodus"] = OrderedDict()
                data["Nodus"]["DEVICE_ID"] = physical_device_id
                data["Nodus"]["CONFIG_FILE"] = config_file

                data["Display"] = OrderedDict()
                chosen_metrics = remote_display_metrics or _display_defaults_for_device(
                    device_name or device_type,
                    sensor_hardware,
                )
                for idx in range(6):
                    data["Display"][f"METRIC_{idx + 1}"] = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                if remote_display_styles:
                    style_block = OrderedDict()
                    data["Display"]["Style"] = style_block
                    for idx in range(6):
                        style_block[f"METRIC_{idx + 1}"] = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"

                _merge_nodus_pressure_calibration(data, s.get("calibration"))
                sensor_mgr.save(sensor_id, data)
            if DEBUG:
                printDM(f"[itaot-settings] seeded sensor settings for {sensor_id}", location=MODULE)
    except Exception as exc:
        if DEBUG:
            printDM(f"[itaot-settings] sensor seed error: {exc}", location=MODULE)

    source_hostname = _strip_local(str((info or {}).get("HOSTNAME") or hostname or ""))

    # ---- switch_settings/<SWITCH_DEVICE_ID>/switch.toml ----
    try:
        switch_mgr = SwitchSettingsManager()
        for sw in (switches or []):
            switch_id = str(sw.get("switch_id") or "").strip()
            if not switch_id:
                continue
            sw_path = switch_mgr.get_path(switch_id)
            switch_loc = sw.get("switch_location") or "Unknown"
            switch_type = (sw.get("switch_type") or "").strip()
            switch_serial = (sw.get("serial") or "").strip()
            board_type = str(sw.get("mcu") or sw.get("MCU") or "").strip()
            switch_payload = sw.get("switch_payload") if isinstance(sw, dict) else None

            nodus_dir = switch_mgr.base_dir / "factory_nodus"
            tpl_path = nodus_dir / "switch.toml.def"
            if sw_path.exists():
                doc = switch_mgr.load(switch_id)
            elif tpl_path.exists():
                doc = switch_mgr._parse_toml_from_disk(tpl_path)
            else:
                switch_mgr.ensure_host_switch(switch_id, switch_loc=switch_loc)
                doc = switch_mgr.load(switch_id)

            changed = False
            if "Switch" not in doc or not isinstance(doc["Switch"], dict):
                doc["Switch"] = OrderedDict()
                changed = True
            sb = doc["Switch"]
            if str(sb.get("DEVICE", "") or "").strip() != "switch":
                sb["DEVICE"] = "switch"
                changed = True
            if str(sb.get("SWITCH_DEVICE_ID", "") or "").strip() != switch_id:
                sb["SWITCH_DEVICE_ID"] = switch_id
                changed = True
            canonical_switch_location = _canonical_location(switch_loc)
            if str(sb.get("SWITCH_LOCATION", "") or "").strip() != canonical_switch_location:
                sb["SWITCH_LOCATION"] = canonical_switch_location
                changed = True
            if switch_type and str(sb.get("TYPE", "") or "").strip() != switch_type:
                sb["TYPE"] = switch_type
                changed = True
            if switch_serial and str(sb.get("DEVICE_SERIAL_NUM", "") or "").strip() != switch_serial:
                sb["DEVICE_SERIAL_NUM"] = switch_serial
                changed = True
            if board_type and str(sb.get("MCU", "") or "").strip() != board_type:
                sb["MCU"] = board_type
                changed = True
            # Overlay indexed switch fields from metadata so rendering tracks
            # authoritative channel IDs, labels, last state, and mqtt wiring.
            try:
                src = {}
                if isinstance(switch_payload, dict):
                    src = switch_payload.get("Switch") if isinstance(switch_payload.get("Switch"), dict) else switch_payload
                if isinstance(src, dict):
                    incoming_indices: set[int] = set()
                    existing_indices: set[int] = set()
                    for existing_key in list(sb.keys()):
                        match = re.fullmatch(r"SWITCH_(\d+)_(.+)", str(existing_key or ""))
                        if match:
                            existing_indices.add(int(match.group(1)))
                    for k, v in src.items():
                        ks = str(k or "")
                        match = re.fullmatch(r"SWITCH_(\d+)_(.+)", ks)
                        if match:
                            incoming_indices.add(int(match.group(1)))
                        if not ks.startswith("SWITCH_") or ks == "SWITCH_DEVICE_ID":
                            continue

                        existing_val = sb.get(ks)
                        if existing_val != v:
                            sb[ks] = v
                            changed = True

                    preserve_richer_existing_remote = (
                        str(switch_type or "").strip().lower() in {"nodus", "picow", "pico2w", "remote", "mqtt"}
                        and source_hostname
                        and source_hostname.lower() == switch_id.lower()
                        and incoming_indices
                        and existing_indices
                        and len(existing_indices) > len(incoming_indices)
                        and incoming_indices.issubset(existing_indices)
                    )

                    if incoming_indices:
                        if preserve_richer_existing_remote:
                            if DEBUG:
                                printDM(
                                    f"[itaot-settings] preserve richer switch definition for {switch_id}: "
                                    f"host={source_hostname} existing={sorted(existing_indices)} incoming={sorted(incoming_indices)}",
                                    location=MODULE,
                                )
                        else:
                            for existing_key in list(sb.keys()):
                                match = re.fullmatch(r"SWITCH_(\d+)_(.+)", str(existing_key or ""))
                                if not match:
                                    continue
                                existing_idx = int(match.group(1))
                                existing_suffix = match.group(2)
                                if existing_idx not in incoming_indices:
                                    sb.pop(existing_key, None)
                                    changed = True
                                    continue
                                if existing_suffix == "EN" and f"SWITCH_{existing_idx}_EN" not in src:
                                    sb.pop(existing_key, None)
                                    changed = True
            except Exception:
                pass
            try:
                switch_mgr._ensure_channel_ids(sb)
            except Exception:
                pass
            if changed:
                switch_mgr.save(switch_id, doc)
            try:
                valid_channel_ids: list[str] = []
                for idx in range(1, 33):
                    label = str(sb.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                    channel_id = str(sb.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                    if label and channel_id:
                        valid_channel_ids.append(channel_id)
                if valid_channel_ids and data_logger:
                    data_logger.prune_switch_identities(
                        switch_id=switch_id,
                        valid_channel_ids=valid_channel_ids,
                    )
            except Exception:
                pass

            if DEBUG:
                action = "updated" if sw_path.exists() else "seeded"
                printDM(f"[itaot-settings] {action} switch settings for {switch_id}", location=MODULE)
    except Exception as exc:
        if DEBUG:
            printDM(f"[itaot-settings] switch seed error: {exc}", location=MODULE)


def persist_calibration_patch(sensor_patch_info: dict) -> None:
    """Apply correlated calibration blocks without rewriting unchanged shadows."""
    try:
        from collections import OrderedDict
        from .saiSensorSettingsManager import SensorSettingsManager

        def _ensure_block(parent: dict, name: str) -> dict:
            block = parent.get(name)
            if not isinstance(block, dict):
                block = OrderedDict()
                parent[name] = block
            return block

        sensor_mgr = SensorSettingsManager()
        for target_sensor_id, target_blocks in sensor_patch_info.items():
            try:
                sensor_doc = sensor_mgr.load(target_sensor_id) or OrderedDict()
            except Exception:
                sensor_doc = OrderedDict()

            changed = False
            for block_name, items in target_blocks.items():
                current = sensor_doc
                for segment in [
                    seg for seg in str(block_name or "").split(".") if seg
                ]:
                    current = _ensure_block(current, segment)
                for key, value in (items or {}).items():
                    if current.get(key) != value:
                        current[key] = value
                        changed = True
            if changed:
                sensor_mgr.save(target_sensor_id, sensor_doc)
    except Exception as e:
        if DEBUG:
            printDM(f"[nodus-meta-patch] sensor settings apply failed: {e}", location=MODULE)
