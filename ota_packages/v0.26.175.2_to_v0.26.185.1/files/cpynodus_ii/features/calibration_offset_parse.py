"""Parse single-offset calibration apply payloads."""

_SUPPORTED_KEYS = (
    "TEMP_OFFSET",
    "RH_OFFSET",
    "CO2_OFFSET",
    "AQI_OFFSET",
    "GAS_OFFSET",
    "LUX_OFFSET",
    "PPFD_OFFSET",
    "APVPD_TEMP_CAL_VAL",
    "APVPD_RH_CAL_VAL",
    "ALTITUDE_METERS",
    "SOIL_TEMP_CAL_VAL",
    "SOIL_MOIST_CAL_VAL",
    "SOIL_TEMP_MOIST_VAL",
    "SOIL_PH_CAL_VAL",
    "SOIL_EC_CAL_VAL",
)


def parse_single_offset_payload(payload_text):
    """Return ``(message_id, update)`` for one-offset apply payloads."""
    text = str(payload_text or "").strip()
    if not text:
        return None
    action = _extract_string_field(text, "action").lower()
    if action not in {"apply", "set", "update"}:
        return None
    message_id = _extract_string_field(text, "message_id")
    offsets_index = text.find('"offsets"')
    if offsets_index < 0:
        return None
    array_start = text.find("[", offsets_index)
    item_start = text.find("{", array_start)
    if array_start < 0 or item_start < 0:
        return None
    item_end = _find_object_end(text, item_start)
    array_end = text.find("]", item_end + 1)
    if item_end < 0 or array_end < 0:
        return None
    if text.find("{", item_end + 1, array_end) >= 0:
        return None

    item_text = text[item_start : item_end + 1]
    section, key = _normalize_calibration_key(_extract_string_field(item_text, "key"))
    if not _supported_offset(section, key):
        return None
    return message_id, {
        "section": section,
        "key": key,
        "value": _extract_value_field(item_text, "value"),
    }


def _extract_string_field(text, field):
    marker = '"{}"'.format(str(field or ""))
    index = text.find(marker)
    if index < 0:
        return ""
    colon_index = text.find(":", index + len(marker))
    if colon_index < 0:
        return ""
    start = text.find('"', colon_index + 1)
    if start < 0:
        return ""
    end = start + 1
    escaped = False
    while end < len(text):
        char = text[end]
        if char == '"' and not escaped:
            return text[start + 1 : end]
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
        end += 1
    return ""


def _extract_value_field(text, field):
    marker = '"{}"'.format(str(field or ""))
    index = text.find(marker)
    if index < 0:
        return 0.0
    colon_index = text.find(":", index + len(marker))
    if colon_index < 0:
        return 0.0
    start = colon_index + 1
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    if start < len(text) and text[start] == '"':
        end = text.find('"', start + 1)
        return text[start + 1 : end] if end > start else ""
    end = start
    while end < len(text) and text[end] not in ",}]":
        end += 1
    token = text[start:end].strip()
    lower = token.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return float(token or 0.0)
    except ValueError:
        return 0.0


def _find_object_end(text, start):
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if char == '"' and not escaped:
                in_string = False
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


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


def _supported_offset(section, key):
    return bool(
        section in {"Calibration.System", "Calibration.Device"}
        and str(key or "").strip().upper() in _SUPPORTED_KEYS
    )
