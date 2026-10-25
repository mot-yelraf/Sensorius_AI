"""Exercise condition decisions without controllers, GPIO, MQTT, or delivery.

Fixed inputs check evaluation order, hysteresis, timing boundaries, shared
calendar edges, and deferred notification requests independently of actuation.
"""

from datetime import datetime, timezone

import pytest

from sensorius.saiAutomationEvaluation import (
    AutomationConditionEvaluator, CalendarEvaluationState,
    compare_with_hysteresis, evaluate_condition_groups, evaluate_switch_groups,
    split_condition_groups, time_in_window,
)


def make_evaluator(**overrides):
    args = dict(
        switch_id="hub", now_str="12:00", now_wday=0, seconds_since_midnight=43200,
        epoch=lambda: 1000.0, values_for_sensor=lambda sensor: {"Soil-Temp": 25.0},
        state_for_label=lambda label: False, time_in_window=time_in_window,
        compare_with_hysteresis=compare_with_hysteresis,
        astral_condition=lambda cond: True, calendar_snapshot=lambda: {},
        biodynamic_transition=lambda: {}, calendar_state=CalendarEvaluationState(),
        weather_events=[],
    )
    args.update(overrides)
    return AutomationConditionEvaluator(**args)


def test_group_modes_preserve_short_circuit_and_complete_reports():
    conditions = [{"type": "or"}, {"id": "a", "result": False},
                  {"id": "b", "result": True}, {"type": "OR"},
                  {"id": "c", "result": True}, {"type": "or"}, {"type": "or"}]
    groups = split_condition_groups(conditions)
    calls = []

    def evaluate(cond, label, **kwargs):
        calls.append((cond["id"], label, kwargs))
        return cond["result"]

    assert evaluate_switch_groups(groups, evaluate, target_label="Fan", rule_id="r") == [False, True]
    assert [row[0] for row in calls] == ["a", "c"]
    calls.clear()
    reports = evaluate_condition_groups(groups, evaluate, target_label="", current_action_state=True, rule_id="r")
    assert [row[0] for row in calls] == ["a", "b", "c"]
    assert all(row[2] == {"current_action_state": True, "rule_id": "r"} for row in calls)
    assert [group["result"] for group in reports] == [False, True]
    assert reports[0]["conditions"] == [(conditions[1], False), (conditions[2], True)]


@pytest.mark.parametrize("op,value,state,expected", [
    (">", 27, False, False), (">", 27.01, False, True),
    (">", 24, True, True), (">", 23, True, False),
    ("<", 23, False, False), ("<", 22.99, False, True),
    ("<", 26, True, True), ("<", 27, True, False),
    ("==", 25, False, True), ("!=", 25, True, False),
    ("unsupported", 100, True, True), ("unsupported", 100, False, False),
])
def test_sensor_hysteresis_boundaries_and_metric_aliases(op, value, state, expected):
    evaluator = make_evaluator(values_for_sensor=lambda sensor: {"soil_temp": value},
                               state_for_label=lambda label: state)
    cond = {"type": "sensor", "sensor": "soil", "metric": "Soil-Temp", "value": 25, "hyst": 2, "op": op}
    assert evaluator.evaluate(cond, "Fan") is expected


def test_notification_hysteresis_uses_actor_state_without_reading_a_switch():
    def no_switch(label):
        raise AssertionError("Notification condition must use the supplied actor state")
    evaluator = make_evaluator(state_for_label=no_switch)
    cond = {"type": "sensor", "sensor": "soil", "metric": "Soil-Temp", "value": 25, "hyst": 2}
    assert evaluator.evaluate(cond, "", current_action_state=True)
    assert not evaluator.evaluate(cond, "", current_action_state=False)


@pytest.mark.parametrize("values,condition", [
    ({}, {"sensor": "s", "metric": "Temperature", "value": 1}),
    ({"Temperature": None}, {"sensor": "s", "metric": "Temperature", "value": 1}),
    ({"Temperature": "invalid"}, {"sensor": "s", "metric": "Temperature", "value": 1}),
    ({"Temperature": 2}, {"sensor": "s", "metric": "Temperature", "value": "bad"}),
    ({"Temperature": 2}, {"sensor": "", "metric": "Temperature", "value": 1}),
])
def test_missing_or_invalid_readings_do_not_match(values, condition):
    evaluator = make_evaluator(values_for_sensor=lambda sensor: values)
    assert not evaluator.evaluate({"type": "sensor", **condition}, "Fan")


@pytest.mark.parametrize("now,days,expected", [
    ("23:00", [0], True), ("05:59", [0], True), ("06:00", [0], False),
    ("12:00", [0], False), ("23:00", [1], False), ("23:00", ["bad", 7], True),
])
def test_time_windows_keep_day_filters_and_midnight_wrap(now, days, expected):
    evaluator = make_evaluator(now_str=now)
    assert evaluator.evaluate({"type": "time", "start": "22:00", "end": "06:00", "days": days}, "Fan") is expected
    assert time_in_window("00:00", "00:00", now)


@pytest.mark.parametrize("epoch,expected", [(1059, True), (1060, False), (1299, False), (1300, True)])
def test_anchored_timer_boundaries(epoch, expected):
    evaluator = make_evaluator(epoch=lambda: epoch)
    assert evaluator.evaluate({"type": "timer", "duration_min": 1, "period_min": 5, "anchor_epoch": 1000}, "Fan") is expected


def test_timer_reads_clock_at_each_condition_and_keeps_legacy_hour_alignment():
    times = iter([1059, 1060])
    evaluator = make_evaluator(epoch=lambda: next(times), seconds_since_midnight=3600)
    cond = {"type": "timer", "duration_min": 1, "period_min": 5, "anchor_epoch": 1000}
    assert evaluator.evaluate(cond, "Fan")
    assert not evaluator.evaluate(cond, "Fan")
    assert evaluator.evaluate({"type": "timer", "duration_min": 2, "freq_hours": 1}, "Fan")
    assert not evaluator.evaluate({"type": "timer", "duration_min": 5, "period_min": 5}, "Fan")


def test_datetime_event_triggers_only_on_a_new_due_edge():
    target = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    state = CalendarEvaluationState()
    cond = {"type": "calendar", "calendar_kind": "date_time", "calendar_at": target.isoformat()}
    before = make_evaluator(calendar_state=state, epoch=lambda: target.timestamp() - 1)
    assert not before.evaluate(dict(cond), "Fan", rule_id="r")
    due = make_evaluator(calendar_state=state, epoch=lambda: target.timestamp())
    assert due.evaluate(cond, "Fan", rule_id="r")
    assert cond["_calendar_event"]["transition_at"] == target.isoformat()
    # The same pass can evaluate multiple actors without consuming the edge twice.
    assert due.evaluate(dict(cond), "Pump", rule_id="r")
    after = make_evaluator(calendar_state=state, epoch=lambda: target.timestamp() + 1)
    assert not after.evaluate(dict(cond), "Fan", rule_id="r")
    startup_due = make_evaluator(epoch=lambda: target.timestamp() + 1)
    assert not startup_due.evaluate(dict(cond), "Fan", rule_id="r")


def test_legacy_bd_event_is_requested_once_and_retains_cross_pass_state():
    state = CalendarEvaluationState()
    old = {"transition_at": "old", "sign": "Aries", "element": "Fire", "plant_part": "Fruit"}
    new = {"transition_at": "new", "sign": "Taurus", "element": "Earth", "plant_part": "Root"}
    cond = {"type": "bd_transitions", "executor_switch_id": "hub"}
    first = make_evaluator(calendar_state=state, biodynamic_transition=lambda: old)
    assert not first.evaluate(dict(cond), "Fan", rule_id="r")
    second = make_evaluator(calendar_state=state, biodynamic_transition=lambda: new)
    assert second.evaluate(cond, "Fan", rule_id="r")
    assert second.evaluate(dict(cond), "Pump", rule_id="r")
    assert len(second.biodynamic_notifications) == 1
    event = second.biodynamic_notifications[0]
    assert event["from"]["element"] == "Fire" and event["to"]["element"] == "Earth"
    assert cond["_bd_transition"] == event
    third = make_evaluator(calendar_state=state, biodynamic_transition=lambda: new)
    assert not third.evaluate(dict(cond), "Fan", rule_id="r")


def test_calendar_bd_does_not_fire_for_midnight_split_of_same_segment():
    state = CalendarEvaluationState()
    segment = {"sign": "Taurus", "element": "Earth", "plant_part": "Root"}
    cond = {"type": "calendar", "calendar_kind": "bd_transitions", "calendar_event": "earth"}
    for key in ["before-midnight", "after-midnight"]:
        evaluator = make_evaluator(calendar_state=state, biodynamic_transition=lambda: {**segment, "transition_at": key})
        assert not evaluator.evaluate(dict(cond), "Fan", rule_id="r")
        assert not evaluator.biodynamic_notifications


def test_lunar_identity_uses_stable_event_key_not_fractional_search_time():
    state = CalendarEvaluationState()
    cond = {"type": "calendar", "calendar_kind": "lunar_events", "calendar_event": "all"}
    for stamp in ["2026-10-25T12:00:00.1+00:00", "2026-10-25T12:00:00.2+00:00"]:
        event = {"transition_at": stamp, "event_key": "same-phase", "phase": "full_moon"}
        evaluator = make_evaluator(calendar_state=state, calendar_snapshot=lambda: {"lunar_event": event})
        assert not evaluator.evaluate(dict(cond), "Fan", rule_id="r")


def test_weather_annotations_retain_snapshot_without_delivery_effects():
    events = [{"event_key": "warning"}]
    evaluator = make_evaluator(weather_events=events)
    cond = {"type": "severe_weather"}
    assert evaluator.evaluate(cond, "Fan")
    assert cond["_weather_events"] is events
    assert not evaluator.biodynamic_notifications


def test_construction_does_not_call_runtime_providers():
    def unexpected(*args):
        raise AssertionError("Provider was called during construction")
    evaluator = make_evaluator(epoch=unexpected, values_for_sensor=unexpected,
                               state_for_label=unexpected, calendar_snapshot=unexpected,
                               biodynamic_transition=unexpected, astral_condition=unexpected)
    assert not evaluator.evaluate({"type": "unknown"}, "Fan")
