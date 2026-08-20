# System Automations

Automations are configured from **General Settings > Automations** and evaluated
by the switch controller monitors.
Local GPIO relays and remote Nodus switches share the same controller contract,
so automation rules use the same behavior for both.

## Storage

System automations are stored in:

```text
automation_settings/automations.toml
```

At runtime this resolves under the Sensorius runtime directory, for example
`/Users/<user>/Sensorius/automation_settings/automations.toml` on
macOS or `/home/<user>/Sensorius/automation_settings/automations.toml`
on Linux.

During the path transition, rules that exist only in the former
`switch_settings/automations/automations.toml` file remain active and appear
with a **Legacy — open and Save** marker. Saving one rule writes only that rule
to the new path; rules already present at the new path take precedence by rule
ID. Deleting a legacy rule records its suppression in the new file so it does
not reappear, while leaving the former file untouched as a backup.
Before replacing an existing canonical file, the manager also preserves its
previous contents as `automations.toml.bak`. A parse failure blocks mutation
instead of being treated as an empty rule set.
Deployment and repair synchronization treats `automation_settings/` as
installed runtime state and preserves it across application updates and
restarts.

`sensorius/saiAutomationManager.py` owns this file. The current schema is:

- `[Meta]`: version and notes.
- `[Advanced]`: named rules with `enabled` and compact JSON `script_json`.
- `[Scripts]`: optional coarse global toggles.

## Switch Keys

Automation actions target switch keys in the form:

```text
<switch_id>::<channel_id>
```

Example:

```text
switch-sernum::S1-sernum
```

The manager keeps some alias tolerance for older `<switch_id>::<label>` shapes,
but new rules should use stable channel IDs.

## Rule Capabilities

Advanced rules can express:

- Rule-level enable/disable.
- Sensor metric thresholds, such as `Temperature_F > 82`.
- Hysteresis and minimum interval timing to reduce relay chatter.
- Time-of-day windows.
- Day-of-week schedules.
- Sunrise and sunset schedules through Astral settings.
- Timer windows through `duration_min`, `period_min`, and legacy `freq_hours`.
- Multi-action rules.
- Email Notify actors with a per-action recipient when email is enabled.
- `Alert` actors for Web UI-only notifications without relay or email actions.
  These retain the internal `none` action type for compatibility with existing
  rules. The dashboard toast uses three lines: `<automation name> @ <date time>`,
  `<trigger condition(s)>`, then `<trigger value>`. AND/OR condition groups are
  retained in the condition line. The configured sensor location is used as
  the device name when available; otherwise its sensor ID is shown.
- Revert behavior through `revert_action`.
- Optional delayed action application through `delay_s`.

Time window behavior:

- `00:00` to `00:00` is all day.
- Other windows are inclusive of start and exclusive of end.
- Wraparound windows such as `22:00` to `06:00` are supported.

Timer behavior:

- `duration_min` controls how long a timer window stays active.
- `duration_min` must be less than the repeat interval.
- Hour-based intervals keep on-the-hour alignment.
- Minute-based intervals can use `anchor_epoch` so a newly saved rule starts
  from save time.

BD Transitions behavior:

- `BD Transitions` has no configuration options.
- The condition becomes true for one automation evaluation when the
  biodynamic calendar enters a new segment.
- A transition publishes a dashboard toast containing the local transition
  date and time plus the outgoing and incoming zodiac sign, element, and plant
  part. The toast uses two lines: `BD Transition @ <date time>`, then the
  `From ... → To ...` transition description.
- The toast background uses the incoming element's established biodynamic
  color, with automatically contrasting text.
- The actor list always includes `Alert`.
  Selecting it evaluates the transition and publishes the automatic toast
  without sending email or changing a relay.
- Rules saved with the initial `None` implementation are accepted through a
  compatibility path that treats an empty switch action as `None` for BD
  transition rules.
- The toast remains visible until it is clicked.
- Toast events are delivered to connected dashboard clients through the live
  dashboard WebSocket.
- The first evaluation after service startup establishes the current segment
  without producing a stale transition toast.
- A Notify actor sends one transition email rather than a subsequent
  transition-cleared email. Its subject summarizes the transition time and
  From/To segments; its body includes the full condition details.
- Operators can exercise the live dashboard delivery path without changing
  calendar or automation state by sending a POST request to
  `/advanced/automations/test-bd-transition`. The resulting persistent toast
  uses the standard BD Transition format and is broadcast to every connected
  dashboard.

Notify behavior:

- A false-to-true rule transition sends a **TRIGGERED** message.
- A true-to-false rule transition sends a **CLEARED** message.
- Each message identifies the hub and automation, reports every evaluated
  condition grouped by AND/OR logic, includes current sensor values when
  applicable, and lists all configured switch and Notify actions.
- A rule that remains in the same state does not repeatedly send email.
- Notify delivery uses the guarded email queue with persisted delivery state,
  including the global rolling hourly and 24-hour limits and up to three
  retries after a failed SMTP attempt.
- When all delivery attempts fail, connected dashboards show a persistent
  error toast with the SMTP failure. Click the toast to dismiss it.
- Sensorius persists the last successfully emailed state for each automation
  recipient. Restarting while that state is unchanged does not resend it. If
  the current state changed while Sensorius was stopped, the corresponding
  ACTIVATED or CLEARED message remains eligible until delivery succeeds.

Revert behavior:

- `previous_state` restores the state that existed before the rule applied.
- `do_nothing` leaves the switch in its current state when the rule becomes
  false.
- Runtime ownership needed for `previous_state` is persisted in the switch
  settings runtime block.
- Actions set absolute states. They are not toggle or invert commands.
- `previous_state` is captured when an action actually changes a channel. If
  the channel is already at the requested state, the action is skipped and
  there is no new previous-state value for that action to restore.

## Paired Timer Example

Use one timer automation for paired outputs, such as alternating two LEDs or
two relay channels, by separating the baseline state from the active-window
state.

To run Green normally on and Yellow normally off, then flip them for 8 minutes
every 15 minutes:

1. Disable the automation so manual control is allowed.
2. Set the baseline state manually: Green on, Yellow off.
3. Edit the automation condition to `timer`, Every `15 minutes`, Duration `8`.
4. Set the action rows to the active-window state: Green off, Yellow on.
5. Set **Restore Action** to `To previous state` for both action rows.
6. Save and enable the automation.

During the timer window Sensorius applies the action states. When the window
ends, `To previous state` restores the baseline state that existed before the
actions changed the channels.

Do not encode the baseline as the action state. If the action rows are Green on
and Yellow off, the timer window enforces Green on and Yellow off. Also note
that all action rows in a rule share the same condition groups; adding a second
condition row does not bind one condition to one action and another condition
to another action.

## Astral Conditions

Astral conditions require location and timezone settings from:

- Manual `[Astral].LATITUDE`, `[Astral].LONGITUDE`, and
  `[Astral].TIMEZONE`, or
- IP geolocation when `[Astral].AUTO_IP = true`.

Supported Astral events include:

- `sunrise`
- `sunset`
- `sunrise_to_sunset`
- `sunset_to_sunrise`

Selecting the `Alert` actor publishes a persistent, click-to-dismiss Web UI
notification when an Astral rule changes from false to true. This supports sunrise,
sunset, and Astral window notifications without changing a relay or sending
email. The same actor is available for other condition types and emits one
toast per false-to-true transition.

Window modes can let one automation turn a channel on at the beginning of a
window and revert it at the end.

## Runtime Evaluation

Each switch controller runs `run_controladora_monitor(...)` every few seconds.

Evaluation order:

1. Check whether any enabled rule applies to the switch.
2. Read live bound sensor values when a local sensor is available.
3. Fall back to cached values or DB-backed data paths where implemented.
4. Evaluate Advanced rules.
5. Call `set_state(...)` for actions that should change a switch.
6. Record state changes through `sensorius.saiDataLogger.log_switch_event`.

Manual UI toggles are blocked when an enabled Advanced automation owns the same
switch key. Disable the automation before manual operation.

## Nodus Ownership Status

Sensorius reports enabled Advanced rules that target Nodus channels back to the
physical device. This is presentation metadata only: Nodus continues to accept
normal channel commands and does not evaluate Sensorius rules locally.

- Status: `nodus/<device_id>/automation/sensorius/status`
- Availability: `nodus/<device_id>/automation/sensorius/availability`
- Both payloads are retained. Availability is refreshed every 60 seconds.
- Nodus treats the controller as unavailable after 180 seconds without a fresh
  online lease and removes the automation highlight.
- Removing or disabling a rule publishes a status snapshot without that channel.

The Nodus web UI uses a green State-cell background and lists the controlling
Sensorius rule names while the corresponding lease is fresh.

## Extension Notes

- Add new rule fields through the Advanced JSON schema and
  `sensorius/saiAutomationManager.py`, then update the UI and tests.
- Keep the shared switch controller interface stable:
  `get_switch_names`, `get_state`, `set_state`, `override_script`,
  `last_state`, and `run_controladora_monitor`.
- Use existing tests in `testApparatus/test_automation_contract.py` and
  `testApparatus/test_sai_switch_trigger_manager_compat.py` as starting points
  for automation changes.
