"""Prepare switch baselines before enabling Advanced automation rules.

Actions that restore their previous state need an opposite baseline. Keep
evaluation paused during preparation and require device confirmation before
persisting the enabled rule, using the shared switch controller command path.
"""

import asyncio
import json
import time


class AutomationActivationError(ValueError):
    """Report a switch baseline that could not be prepared."""


def _script(rule):
    raw = rule.get("script_json", {})
    return raw if isinstance(raw, dict) else json.loads(raw)


def _enabled(value):
    return str(value).strip().lower() in {"true", "1", "on", "yes"}


async def activate_with_baselines(manager, hostname, rules, controllers, commit, *,
                                  lock, mqtt_ingest=None, timeout_s=15.0):
    """Confirm opposite baselines, then commit a save or enable operation.

    ``rules`` contains only rules being saved enabled or transitioned to enabled.
    Existing selected rules are disabled before commands are sent. A failure may
    leave some channels prepared; those rules stay disabled for an explicit retry.
    Disabled saves and actions using ``do_nothing`` require no preparation.
    """
    async with lock:
        targets = {}
        selected_ids = set(rules)
        for rule_id, rule in rules.items():
            script = _script(rule)
            actions = script.get("actions") or []
            if isinstance(actions, dict):
                actions = [actions]
            for action in actions:
                if action.get("type", "switch") != "switch":
                    continue
                if action.get("revert_action", "do_nothing") != "previous_state":
                    continue
                key = str(action.get("switch_key", "") or "").strip()
                if "::" not in key:
                    raise AutomationActivationError("Switch action requires a valid switch key.")
                sid, suffix = key.split("::", 1)
                ctrl = next((c for c in controllers.values()
                             if str(c.switch_id).lower() == sid.lower()), None)
                if ctrl is None:
                    raise AutomationActivationError(f"Switch {sid} is unavailable; automation was not enabled.")
                labels = list(ctrl.get_switch_names())
                resolver = getattr(ctrl, "label_for_channel_id", None)
                label = resolver(suffix) if callable(resolver) else ""
                if not label:
                    label = next((name for name in labels if name.lower() == suffix.lower()), "")
                if not label or label not in labels:
                    raise AutomationActivationError(f"Unknown switch channel {key}.")
                baseline = not _enabled(action.get("set", True))
                identity = (str(ctrl.switch_id).lower(), label)
                if identity in targets and targets[identity][2] != baseline:
                    raise AutomationActivationError(f"Conflicting action states for {key}.")
                # Query aliases through the manager so label-style keys are included.
                label_key = f"{ctrl.switch_id}::{label}"
                owners = set(manager.get_advanced_state_for_switch_key(sid, label_key).get("rule_ids", []))
                owners.update(manager.get_advanced_state_for_switch_key(sid, key).get("rule_ids", []))
                existing = manager.load(hostname).get("Advanced", {})
                if any(rid not in selected_ids and _enabled(existing[rid].get("enabled", True))
                       for rid in owners if rid in existing):
                    raise AutomationActivationError(
                        f"Another enabled automation controls {key}; disable it before preparing this baseline.")
                ingest = getattr(ctrl, "mqtt_ingest", None) or mqtt_ingest
                if getattr(ctrl, "is_ecowitt", False) and not ctrl.available:
                    raise AutomationActivationError(f"Switch {sid} is unavailable; automation was not enabled.")
                if getattr(ctrl, "is_remote", False):
                    if ingest is None:
                        raise AutomationActivationError(f"MQTT is unavailable for {key}.")
                    if ingest.get_nodus_liveness(ctrl.switch_id, device_type="switch").get("state") != "online":
                        raise AutomationActivationError(f"Switch {sid} is offline; automation was not enabled.")
                targets[identity] = (ctrl, label, baseline, ingest)

        # Include do_nothing rows when checking conflicts with prepared channels.
        for rule in rules.values():
            actions = _script(rule).get("actions") or []
            for action in ([actions] if isinstance(actions, dict) else actions):
                if action.get("type", "switch") != "switch":
                    continue
                sid, _, suffix = str(action.get("switch_key", "")).partition("::")
                for (target_sid, label), (ctrl, _, baseline, _) in targets.items():
                    channel_id = (getattr(ctrl, "channel_id_for_label", {}) or {}).get(label, "")
                    if sid.lower() == target_sid and suffix.lower() in {label.lower(), channel_id.lower()}:
                        if _enabled(action.get("set", True)) == baseline:
                            raise AutomationActivationError(f"Conflicting action states for {sid}::{suffix}.")

        paused = {id(item[0]): item[0] for item in targets.values()}
        for ctrl in paused.values():
            ctrl._automation_preparing = True
        try:
            if targets:
                existing = await asyncio.to_thread(manager.load, hostname)
                for rule_id in selected_ids:
                    if rule_id in (existing.get("Advanced") or {}):
                        await asyncio.to_thread(manager.set_rule_enabled, hostname,
                                                section="Advanced", rule_id=rule_id, enabled=False)
                for ctrl in paused.values():
                    cache = getattr(ctrl, "_rules_cache", None)
                    if isinstance(cache, dict):
                        cache["mtime"] = None
                    for attr in ("_advanced_active_actions", "_advanced_delay_due"):
                        entries = getattr(ctrl, attr, {})
                        for key in list(entries):
                            if key[0] in selected_ids:
                                entries.pop(key, None)
                    cooldown = getattr(ctrl, "_advanced_revert_cooldown", set())
                    cooldown.difference_update([key for key in cooldown if key[0] in selected_ids])
                    persist = getattr(ctrl, "_persist_advanced_runtime_state", None)
                    if callable(persist):
                        await asyncio.to_thread(persist)

            for ctrl, label, baseline, ingest in targets.values():
                remote = bool(getattr(ctrl, "is_remote", False))
                channel_id = (getattr(ctrl, "channel_id_for_label", {}) or {}).get(label, "")

                def status():
                    if getattr(ctrl, "is_ecowitt", False):
                        task = ctrl.command_task
                        if task and not task.done():
                            return {"pending": True}
                        if task and (task.cancelled() or task.exception() or task.result() is not True):
                            return {"command_error": ctrl.last_error or "Plug command failed."}
                        return {}
                    return ingest.get_switch_command_status(ctrl.switch_id, label, channel_id) if remote else {}

                pending = status()
                if pending.get("pending"):
                    raise AutomationActivationError(f"A switch command is still pending for {label}; retry after it finishes.")
                if not remote and not getattr(ctrl, "is_ecowitt", False) and bool(ctrl.get_state(label)) == baseline:
                    continue
                if getattr(ctrl, "is_ecowitt", False):
                    # This controller queues asynchronous HTTP work on the runtime loop.
                    accepted = ctrl.set_state(label, baseline, force=True, event_source="auto/baseline")
                else:
                    accepted = await asyncio.to_thread(ctrl.set_state, label, baseline, force=True,
                                                       event_source="auto/baseline")
                if not accepted:
                    raise AutomationActivationError(f"Could not prepare {label}; automation remains disabled.")
                deadline = time.monotonic() + timeout_s
                while True:
                    command = status()
                    if command.get("command_error"):
                        raise AutomationActivationError(f"Could not prepare {label}: {command['command_error']} Automation remains disabled.")
                    if not command.get("pending") and bool(ctrl.get_state(label)) == baseline:
                        break
                    if time.monotonic() >= deadline:
                        raise AutomationActivationError(f"Timed out confirming the baseline for {label}; automation remains disabled.")
                    await asyncio.sleep(0.1)
            for ctrl, label, baseline, _ingest in targets.values():
                if bool(ctrl.get_state(label)) != baseline:
                    raise AutomationActivationError(f"The baseline changed for {label}; retry before enabling automation.")
                deadlines = getattr(ctrl, "auto_off_deadline", None)
                if isinstance(deadlines, dict):
                    deadlines[label] = None
            return await asyncio.to_thread(commit)
        finally:
            for ctrl in paused.values():
                ctrl._automation_preparing = False
                cache = getattr(ctrl, "_rules_cache", None)
                if isinstance(cache, dict):
                    cache["mtime"] = None
