"""Evaluate automation conditions independently of switch actuation.

A per-pass evaluator receives clock, reading, and calendar providers explicitly.
Calendar tracking maps remain shared with the controller, while legacy BD toast
requests are collected for controller delivery. Evaluation performs no switch
commands, settings writes, SMTP delivery, or dashboard broadcasts.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CalendarEvaluationState:
    """Keep calendar edge detection shared across evaluation passes."""

    transition_keys: dict = field(default_factory=dict)
    transition_segments: dict = field(default_factory=dict)
    datetime_seen: dict = field(default_factory=dict)


def split_condition_groups(conditions: list[dict]) -> list[list[dict]]:
    """
    Convert a flat list of conditions (with type:'or' rows as separators)
    into a list of groups. Each group is a list of non-OR conditions.
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    for c in conditions:
        ctype = str(c.get("type", "") or "").strip().lower()
        if ctype == "or":
            if current:
                groups.append(current)
                current = []
            continue
        current.append(c)
    if current:
        groups.append(current)
    return groups


def evaluate_condition_groups(
    groups: list[list[dict]],
    evaluate_condition: Callable,
    *,
    target_label: str,
    current_action_state: bool | None = None,
    rule_id: str = "",
) -> list[dict]:
    """AND each group, retaining group order and per-condition diagnostics.

    Notification actors evaluate every condition so their reports retain all
    results. Every OR group is run.
    """
    evaluated_groups = []
    for group in groups:
        evaluated_conditions = []
        group_ok = True
        for cond in group:
            condition_ok = bool(evaluate_condition(
                cond, target_label, current_action_state=current_action_state,
                rule_id=rule_id,
            ))
            evaluated_conditions.append((cond, condition_ok))
            if not condition_ok:
                group_ok = False
        evaluated_groups.append({"result": group_ok, "conditions": evaluated_conditions})
    return evaluated_groups


def evaluate_switch_groups(
    groups: list[list[dict]], evaluate_condition: Callable, *,
    target_label: str, rule_id: str = "",
) -> list[bool]:
    """Short-circuit each switch AND group without allocating condition reports."""
    results = []
    for group in groups:
        group_ok = True
        for cond in group:
            if not evaluate_condition(cond, target_label, rule_id=rule_id):
                group_ok = False
                break
        results.append(group_ok)
    return results


def time_in_window(start: str, end: str, now_str: str) -> bool:
    """Test an inclusive-start, exclusive-end window, including midnight wrap."""
    # Inclusive start, exclusive end: [start, end)
    if not start and not end:
        return True
    s = (start or "00:00")
    e = (end or "24:00")
    if s == "00:00" and e == "00:00":
        # Treat midnight-to-midnight as an explicit all-day window.
        return True
    # handle wrap-around (e.g., 22:00–06:00)
    return (s <= now_str < e) if s <= e else (now_str >= s) or (now_str < e)


def compare_with_hysteresis(op: str, actual: float, threshold: float, hyst: float, current_state: bool) -> bool:
    """
    Return the desired state (True/False) using hysteresis around threshold.
    For op ">", turns ON above (threshold+hyst), turns OFF below (threshold-hyst).
    For op "<", turns ON below (threshold-hyst), turns OFF above (threshold+hyst).
    """
    op = (op or ">").strip()
    hi = threshold + hyst
    lo = threshold - hyst

    if op == ">":
        return (actual > hi) if not current_state else (actual > lo)
    elif op == "<":
        return (actual < lo) if not current_state else (actual < hi)
    elif op == "==":
        return (actual == threshold)
    elif op == "!=":
        return (actual != threshold)
    # default (unknown op) → do nothing (keep current)
    return current_state


class AutomationConditionEvaluator:
    """Evaluate one pass using explicit read providers and shared calendar state."""

    def __init__(
        self, *, switch_id: str, now_str: str, now_wday: int,
        seconds_since_midnight: int, epoch: Callable[[], float],
        values_for_sensor: Callable, state_for_label: Callable,
        time_in_window: Callable, compare_with_hysteresis: Callable,
        astral_condition: Callable, calendar_snapshot: Callable,
        biodynamic_transition: Callable, calendar_state: CalendarEvaluationState,
        weather_events: list[dict], debug: bool = False, log: Callable | None = None,
        system_status: Callable | None = None,
    ):
        """Bind inputs without reading hardware, fetching data, or emitting events."""
        self.switch_id = switch_id
        self.now_str = now_str
        self.now_wday = now_wday
        self.seconds_since_midnight = seconds_since_midnight
        self.epoch = epoch
        self.values_for_sensor = values_for_sensor
        self.state_for_label = state_for_label
        self.time_in_window = time_in_window
        self.compare_with_hysteresis = compare_with_hysteresis
        self.astral_condition = astral_condition
        self.calendar_snapshot = calendar_snapshot
        self.biodynamic_transition = biodynamic_transition
        self.calendar_state = calendar_state
        self.weather_events = weather_events
        self.system_status = system_status
        self.debug = debug
        self.log = log
        self.calendar_results: dict[tuple, tuple[bool, dict]] = {}
        self.biodynamic_notifications: list[dict] = []

    def evaluate(
        self,
        cond: dict,
        target_label: str,
        current_action_state: bool | None = None,
        rule_id: str = "",
    ) -> bool:
        """Evaluate a condition using reading providers and per-actor hysteresis.

        Calendar markers and condition annotations retain their existing lifetime;
        legacy BD notification requests are collected for controller delivery.
        """
        ctype = str(cond.get("type", "") or "").strip().lower()

        if ctype == "system_status":
            result, report = self.system_status(cond) if self.system_status else (None, "System Status unavailable")
            cond["_system_status_unknown"] = result is None
            cond["_system_status_report"] = report
            return bool(result)

        if ctype == "severe_weather":
            cond["_weather_events"] = self.weather_events
            return bool(self.weather_events)

        # --- TIME-OF-DAY CONDITION ---------------------------------------
        # type == "time"
        if ctype == "time":
            start = cond.get("start") or "00:00"
            end   = cond.get("end")   or "24:00"

            # Optional: restrict to specific weekdays (0=Mon..6=Sun)
            # If 'days' is missing or empty, treat as "all days".
            raw_days = cond.get("days") or []
            allowed_days: list[int] = []
            for d in raw_days:
                try:
                    n = int(d)
                except Exception:
                    continue
                if 0 <= n <= 6:
                    allowed_days.append(n)

            if allowed_days and (self.now_wday not in allowed_days):
                return False

            return self.time_in_window(start, end, self.now_str)

        # --- ASTRAL CONDITION --------------------------------------------
        # type == "astral"
        if ctype == "astral":
            return self.astral_condition(cond)

        # --- TIMER CONDITION ----------------------------------------------
        # type == "timer"
        if ctype == "timer":
            # duration_min: active window length in minutes
            # period_min:   repeat period in minutes
            # freq_hours:   legacy hourly repeat period
            try:
                duration_min = int(cond.get("duration_min") or 0)
            except Exception:
                duration_min = 0
            try:
                freq_hours = int(cond.get("freq_hours") or 0)
            except Exception:
                freq_hours = 0
            try:
                period_min = int(cond.get("period_min") or 0)
            except Exception:
                period_min = 0
            if period_min <= 0 and freq_hours > 0:
                period_min = freq_hours * 60
            try:
                anchor_epoch = int(cond.get("anchor_epoch") or 0)
            except Exception:
                anchor_epoch = 0

            if duration_min <= 0 or period_min <= 0 or duration_min >= period_min:
                return False

            period_sec = max(period_min, 1) * 60
            duration_sec = max(1, min(duration_min * 60, period_sec - 1))

            if anchor_epoch > 0:
                phase = max(0, int(self.epoch()) - anchor_epoch) % period_sec
            else:
                # Preserve legacy on-the-hour alignment for hourly timers.
                phase = self.seconds_since_midnight % period_sec
            return phase < duration_sec

        # --- CALENDAR EVENT CONDITIONS -----------------------------------
        if ctype in {"bd_transitions", "calendar"}:
            executor_sid = str(cond.get("executor_switch_id", "") or "").strip()
            own_sid = str(self.switch_id or "").strip()
            if ctype == "bd_transitions" and executor_sid and executor_sid.lower() != own_sid.lower():
                return False
            kind = cond.get("calendar_kind", "bd_transitions") if ctype == "calendar" else "bd_transitions"
            selected = str(cond.get("calendar_event", "all"))
            state_key = (str(rule_id or "calendar"), ctype, kind, selected, str(cond.get("calendar_at", "")))
            event_field = "_lunar_event" if kind == "lunar_events" else "_calendar_event" if kind in {"season", "date_time"} else "_bd_transition"
            if state_key in self.calendar_results:
                result, event = self.calendar_results[state_key]
                if event:
                    cond[event_field] = event
                return result
            if kind == "date_time":
                try:
                    target = datetime.fromisoformat(str(cond.get("calendar_at", "")))
                    if target.tzinfo is None:
                        return False
                except (TypeError, ValueError):
                    return False
                due = self.epoch() >= target.timestamp()
                seen = self.calendar_state.datetime_seen
                was_due = seen.get(state_key, due)
                seen[state_key] = bool(was_due or due)
                triggered = due and not was_due
                event = {"transition_at": target.isoformat(), "event_key": "date_time:" + target.isoformat(),
                         "name": "Sensorius Calendar Event", "description": "Scheduled calendar event.",
                         "context": "Scheduled Sensorius calendar event; " + str(cond.get("calendar_timezone", ""))}
                self.calendar_results[state_key] = (triggered, event if triggered else {})
                if triggered:
                    cond[event_field] = event
                return triggered
            if kind not in {"lunar_events", "season", "bd_transitions"}:
                return False
            snapshot = self.calendar_snapshot() if kind in {"lunar_events", "season"} else {}
            if kind == "lunar_events":
                transition = snapshot.get("lunar_event") or {}
            elif kind == "season":
                transition = snapshot.get("season_event") or {}
            else:
                transition = self.biodynamic_transition()
            transition_key = str(transition.get("transition_at") or "").strip()
            if kind in {"lunar_events", "season"} and transition_key:
                # Searches with a new daily window can differ by fractions
                # of a second; the same phase must keep a stable identity.
                transition_key = str(transition.get("event_key") or transition_key)
            transition_keys = self.calendar_state.transition_keys
            transition_segments = self.calendar_state.transition_segments
            previous_key = transition_keys.get(state_key)
            previous_segment = transition_segments.get(state_key)
            triggered = bool(transition_key and previous_key and transition_key != previous_key)
            if transition_key:
                transition_keys[state_key] = transition_key
                transition_segments[state_key] = {
                    key: str(transition.get(key) or "")
                    for key in ("sign", "element", "plant_part", "color", "accent")
                }
            event = {}
            if kind in {"lunar_events", "season"}:
                triggered = triggered and selected in {"all", transition.get("phase" if kind == "lunar_events" else "season")}
                if triggered:
                    event = dict(transition)
                    cond[event_field] = event
            elif kind == "bd_transitions":
                if ctype == "calendar":
                    # Midnight splits a day's rendering; it is not a new BD event.
                    changed = any((previous_segment or {}).get(key) != transition.get(key)
                                  for key in ("sign", "element", "plant_part"))
                    triggered = triggered and changed and selected in {"all", str(transition.get("element", "")).lower()}
                if triggered:
                    event = {"transition_at": transition_key, "from": dict(previous_segment or {}),
                             "to": dict(transition_segments[state_key])}
                    cond["_bd_transition"] = event
                    if ctype == "bd_transitions":
                        self.biodynamic_notifications.append(event)
            else:
                triggered = False
            self.calendar_results[state_key] = (triggered, event)
            return triggered

        # --- SENSOR CONDITION ---------------------------------------------
        if ctype == "sensor":
            sensor_id = str(cond.get("sensor", "") or "").strip()
            metric    = str(cond.get("metric", "") or "").strip()
            if not sensor_id or not metric:
                return False

            # Pull latest values for that sensor (live → cache → DB)
            vals = self.values_for_sensor(sensor_id)
            if not vals:
                if self.debug and self.log is not None:
                    self.log(
                        f"[advanced] {self.switch_id}: no values for sensor '{sensor_id}'",
                        location="saiSwitch",
                    )
                return False

            # Try exact metric first, then a lenient normalized match
            actual = None
            if metric in vals:
                actual = vals[metric]
            else:
                key_norm = (
                    metric.lower()
                    .replace("-", "")
                    .replace("_", "")
                    .replace(" ", "")
                )
                for k, v in vals.items():
                    if not k:
                        continue
                    if (
                        k.lower()
                        .replace("-", "")
                        .replace("_", "")
                        .replace(" ", "")
                        == key_norm
                    ):
                        actual = v
                        break
            if actual is None:
                if self.debug and self.log is not None:
                    self.log(
                        f"[advanced] {self.switch_id}: metric '{metric}' not found; "
                        f"keys={list(vals.keys())[:8]}",
                        location="saiSwitch",
                    )
                return False

            try:
                actual_f   = float(actual)
                threshold  = float(cond.get("value"))
            except Exception:
                return False

            try:
                hyst = float(cond.get("hyst", 0) or 0)
            except Exception:
                hyst = 0.0

            op = str(cond.get("op", ">") or ">").strip()
            curr_state = (
                bool(current_action_state)
                if current_action_state is not None
                else bool(self.state_for_label(target_label))
            )

            # Interpret "condition truth" as "this condition alone wants the
            # channel to be ON" using hysteresis around the threshold.
            desired_for_cond = self.compare_with_hysteresis(
                op, actual_f, threshold, hyst, curr_state
            )
            return bool(desired_for_cond)

        # Unknown condition type → treat as False (safe default)
        return False
