"""Read and write versioned, readable automation exchange documents.

Transfers contain Advanced rules and their presentation groups, using native
stored quantities. Validation finishes before persistence, and imported copies
always start disabled. Runtime annotations are excluded from portable scripts.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import uuid
from zoneinfo import ZoneInfo

from . import __version__

FORMAT = "sensorius-automations"
FORMAT_VERSION = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_RULES = 1000
ERROR_MESSAGES = {
    "size": "Automation file is too large (maximum 4 MiB).",
    "json": "This file is not valid UTF-8 JSON, or contains duplicate fields.",
    "format": "This is not a Sensorius automation export.",
    "version": "This automation file version is not supported.",
    "structure": "Invalid automation file structure or rule fields. Nothing was imported.",
}


class TransferFormatError(ValueError):
    """Identify a transfer validation failure without exposing parser details."""

    def __init__(self, code: str = "structure"):
        self.code = code
        super().__init__(ERROR_MESSAGES.get(code, ERROR_MESSAGES["structure"]))


def _require(ok: bool) -> None:
    if not ok:
        raise TransferFormatError()


def _text(value, limit=256, *, empty=False) -> bool:
    return isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e15


def _portable(value, depth=0):
    _require(depth <= 16)
    if isinstance(value, dict):
        _require(len(value) <= 100 and all(_text(key, 128) for key in value))
        return {key: _portable(item, depth + 1) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        _require(len(value) <= MAX_RULES)
        return [_portable(item, depth + 1) for item in value]
    _require(value is None or type(value) is bool or _number(value) or isinstance(value, str) and len(value) <= 8192)
    return value


def _validate_script(script: dict) -> None:
    from .saiAutomationLibrary import normalize_stage_window
    from .saiCalendarAutomation import LUNAR_PHASES
    from .saiSeasonCalendar import SEASON_EVENTS
    from .saiSystemStatus import normalize_system_status_condition

    _require(isinstance(script, dict) and not set(script) - {"name", "enabled", "conditions", "actions", "logic"})
    _require(len(json.dumps(script, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) <= 65536)
    _require(_text(script.get("name"), 512) and type(script.get("enabled")) is bool)
    conditions, actions = script.get("conditions"), script.get("actions")
    _require(isinstance(conditions, list) and len(conditions) <= 100)
    _require(isinstance(actions, list) and len(actions) <= 100)
    for condition in conditions:
        _require(isinstance(condition, dict))
        for key in ("value", "hyst", "offset_min", "offset_minutes", "duration_min", "freq_hours", "period_min", "anchor_epoch", "threshold_gib", "failures"):
            _require(condition.get(key) is None or _number(condition[key]))
        kind = condition.get("type")
        _require(kind in {"sensor", "time", "timer", "astral", "calendar", "bd_transitions", "or", "severe_weather", "system_status"})
        if kind == "sensor":
            _require(_text(condition.get("sensor", condition.get("sensor_id"))) and _text(condition.get("metric")))
            _require(condition.get("op") in {">", "<", "==", "!="} and _number(condition.get("value")))
            _require(condition.get("hyst") is None or _number(condition["hyst"]) and condition["hyst"] >= 0)
        elif kind == "time":
            for key in ("start", "end"):
                value = condition.get(key) or "00:00"
                _require(isinstance(value, str) and len(value) == 5 and value[2] == ":")
                hour, minute = int(value[:2]), int(value[3:])
                _require(0 <= hour <= 23 and 0 <= minute <= 59 or key == "end" and value == "24:00")
            days = condition.get("days")
            _require(days is None or isinstance(days, list) and all(type(day) is int and 0 <= day <= 6 for day in days))
        elif kind == "timer":
            duration = condition.get("duration_min")
            period = condition.get("period_min") or (condition.get("freq_hours") or 0) * 60
            _require(_number(duration) and _number(period) and 0 < duration < period)
        elif kind == "astral":
            _require(condition.get("astral_event", condition.get("event", "sunrise")) in {"sunrise", "sunset", "sunrise_to_sunset", "sunset_to_sunrise", "sunrise-sunset", "sunset-sunrise"})
        elif kind == "system_status":
            normalize_system_status_condition(condition)
        elif kind in {"calendar", "bd_transitions"}:
            calendar_kind = condition.get("calendar_kind", "bd_transitions") if kind == "calendar" else "bd_transitions"
            events = {"bd_transitions": {"all", "earth", "water", "air", "fire"}, "lunar_events": {"all", *LUNAR_PHASES},
                      "season": {"all", *SEASON_EVENTS}, "date_time": {"all"}, "date_range": {"all"}}
            _require(calendar_kind in events and condition.get("calendar_event", "all") in events[calendar_kind])
            if calendar_kind in {"date_time", "date_range"}:
                ZoneInfo(str(condition.get("calendar_timezone") or "UTC"))
                if calendar_kind == "date_range":
                    first, _ = normalize_stage_window(condition.get("calendar_start"), condition.get("calendar_end"))
                    _require(bool(first))
                else:
                    _require(bool(condition.get("calendar_local") or condition.get("calendar_at")))
                    datetime.fromisoformat(str(condition.get("calendar_local") or condition["calendar_at"]))
    for action in actions:
        _require(isinstance(action, dict))
        kind = action.get("type", "switch")
        _require(kind in {"none", "switch", "notify"})
        _require(action.get("executor_switch_id") is None or _text(action["executor_switch_id"]))
        if kind == "switch":
            _require(_text(action.get("switch_key")) and "::" in action["switch_key"] and type(action.get("set")) is bool)
            delay = action.get("delay_s", 0)
            _require(_number(delay) and delay >= 0)
            _require(action.get("revert_action", "previous_state") in {"previous_state", "do_nothing"})
        elif kind == "notify":
            _require(_text(action.get("to"), 1024) and "@" in action["to"])


def validate_document(document: object) -> dict:
    """Validate the complete exchange schema and return a detached safe document."""
    try:
        _require(isinstance(document, dict))
        if document.get("format") != FORMAT:
            raise TransferFormatError("format")
        if type(document.get("format_version")) is not int or document["format_version"] != FORMAT_VERSION:
            raise TransferFormatError("version")
        _require(set(document) == {"format", "format_version", "application_version", "exported_at", "value_units", "groups", "automations"})
        _require(_text(document["application_version"], 80) and document["value_units"] == "native")
        _require(_text(document["exported_at"], 80))
        _require(datetime.fromisoformat(document["exported_at"]).utcoffset() is not None)
        groups, rules = document["groups"], document["automations"]
        _require(isinstance(groups, list) and 1 <= len(groups) <= 1000)
        _require(all(_text(group, 80) and group.casefold() not in {"up", "down"} for group in groups))
        _require(len({group.casefold() for group in groups}) == len(groups))
        _require(isinstance(rules, list) and len(rules) <= MAX_RULES)
        seen = set()
        for rule in rules:
            _require(isinstance(rule, dict) and set(rule) in ({"id", "group", "position", "enabled", "script"}, {"id", "group", "position", "enabled", "script", "library"}))
            _require(_text(rule["id"]) and rule["id"] not in seen)
            seen.add(rule["id"])
            _require(rule["group"] in groups and type(rule["position"]) is int and 0 <= rule["position"] <= 9007199254740991)
            _require(type(rule["enabled"]) is bool and rule["script"].get("enabled") == rule["enabled"])
            _validate_script(rule["script"])
            _require("library" not in rule or isinstance(rule["library"], dict))
        safe = _portable(document)
        _require(len(json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= MAX_BYTES)
        return safe
    except TransferFormatError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        raise TransferFormatError() from exc


def parse_document(content: bytes | str) -> dict:
    """Decode bounded UTF-8 JSON and reject ambiguous keys and non-finite numbers."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_BYTES:
        raise TransferFormatError("size")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TransferFormatError("json")
            result[key] = value
        return result

    def invalid_number(_value):
        raise TransferFormatError("json")

    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs, parse_constant=invalid_number)
    except TransferFormatError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TransferFormatError("json") from exc
    return validate_document(value)


def export_document(data: dict, *, now: datetime | None = None) -> tuple[str, str]:
    """Return a portable filename and readable JSON for all saved Advanced rules."""
    from .saiAutomationManager import automation_groups, automation_group, automation_position, _as_enabled

    now = now or datetime.now().astimezone()
    groups = automation_groups(data)
    rules = []
    for key, rule in (data.get("Advanced") or {}).items():
        script = _portable(json.loads(rule["script_json"]))
        enabled = _as_enabled(rule.get("enabled")) and _as_enabled(script.get("enabled", True))
        script.update(name=script.get("name") or key, enabled=enabled)
        item = {"id": key, "group": automation_group(rule), "position": automation_position(rule), "enabled": enabled, "script": script}
        if rule.get("library_json"):
            item["library"] = _portable(json.loads(rule["library_json"]))
        rules.append(item)
    rules.sort(key=lambda item: (groups.index(item["group"]), item["position"], item["id"]))
    document = validate_document({"format": FORMAT, "format_version": FORMAT_VERSION, "application_version": __version__,
                                  "exported_at": now.isoformat(timespec="seconds"), "value_units": "native", "groups": groups, "automations": rules})
    content = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise TransferFormatError("size")
    patch = __version__.rsplit(".", 1)[-1]
    return f"automations.{now:%y.%j}.{patch}-{now:%H-%M}.json", content


def transfer_rule_ids(batch_id: str, document: dict) -> dict[str, str]:
    """Derive retry-safe copy IDs while hashing the validated document once."""
    namespace = uuid.UUID(batch_id)
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {rule["id"]: "auto-import-" + uuid.uuid5(namespace, digest + ":" + rule["id"]).hex for rule in document["automations"]}
