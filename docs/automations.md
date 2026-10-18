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
- Calendar events for filtered biodynamic transitions and the eight lunar phases.
- **Severe Weather**: active NWS weather warnings and watches at the Astral location.
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

Temperature conditions display thresholds and hysteresis in the selected
Metric/Imperial units. A threshold is an absolute temperature (77°F = 25°C), while
hysteresis is a difference (1.8°F = 1°C). Saving converts edited fields to the
selected metric's native units; `Temperature` stays Celsius and legacy
`Temperature_F` stays Fahrenheit. Unedited numeric fields retain their original
precision. Changing the display preference never rewrites rules or changes their
trigger points. Notification temperature values and thresholds use the preferred
units as well.

Existing rules and API requests without unit annotations retain their native
metric interpretation. The editor submits optional `value_unit` and `hyst_unit`
annotations, which the server validates and removes before storing `script_json`.
Unknown metrics are labelled as native units; no temperature conversion is guessed.
Non-temperature conditions continue to use native units. Sensorius evaluates its
own rules, including those controlling remote switches; Nodus device-native rules
and their metric storage are unchanged.

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

Calendar behavior:

- The Type menu lists Sensor, Time of Day, Timer, Astral, Calendar, Severe Weather,
  and Or, in that order. Calendar adds a category selector and event selector (or date/time field) to the right.
- **Calendar → BD Transitions** offers **All**, **Earth/Root**, **Water/Leaf**,
  **Air/Flower**, and **Fire/Fruit**. The filter matches the incoming element.
  A new sign/element/plant-part segment produces one evaluation pulse; a daily
  rendering boundary at midnight does not itself create an event.
- **Calendar → Lunar Events** offers **All** and **New Moon**, **Waxing Crescent**,
  **First Quarter**, **Waxing Gibbous**, **Full Moon**, **Waning Gibbous**,
  **Last Quarter**, and **Waning Crescent**. These are timed apparent geocentric
  Sun–Moon elongation crossings at 0°, 45°, 90°, 135°, 180°, 225°, 270°, and 315°.
  Crescent/gibbous entries represent the phase milestones, rather than an
  entire multiday phase window or the approximate dates shown in the dashboard.
- **Calendar → Date/Time** presents a date/time field to the right. It is a
  one-time Sensorius Calendar event using the displayed IANA timezone, initially
  the hub's Astral timezone (or Time.TZ, default UTC). Save stores the local
  selection, timezone, and resolved offset-aware instant as `calendar_local`,
  `calendar_timezone`, and `calendar_at`; later timezone changes do not shift it.
  Nonexistent or ambiguous daylight-saving wall times are rejected with a clear
  error. It needs no location or ephemeris data. A past event is not replayed on
  startup, and a clock rollback cannot fire an already observed event twice.
- **Calendar → Season** offers **All**, **Imbolc**, **Spring Equinox / Ostara**,
  **Spring Equilux**, **Beltane**, **Summer Solstice / Litha**, **Lughnasadh /
  Lammas**, **Autumn Equilux**, **Autumn Equinox / Mabon**, **Samhain**, and
  **Winter Solstice / Yule**. The four solar turning points use calculated
  instants. Traditional northern-hemisphere cross-quarter observances start at
  local midnight on February 1, May 1, August 1, and October 31, respectively.
  Southern-hemisphere seasonal names follow the local seasons; Imbolc is
  August 1, Beltane November 1, Lughnasadh February 1, and Samhain April 30.
  These conventions reflect the [eightfold Wheel of the Year](https://druidry.org/druid-way/teaching-and-practice/druid-festivals);
  individual traditions may observe different dates.
- Equilux is separate from equinox: it depends on latitude and measured
  sunrise-to-sunset daylight. Sensorius finds each annual twelve-hour crossing,
  selects the neighboring local date closest to twelve hours, and triggers at
  that date's sunrise. The message states the convention, daylight duration,
  and local sunrise/sunset times. If there is no crossing (for example, near
  the equator), no equilux event is invented. See the
  [equilux definition](https://www.timeanddate.com/astronomy/equilux.html).
  Fixed observances and equilux calculations remain available without local
  ephemeris data; solstices and equinoxes require it.
- Date/Time and Season use the standard generic persistent Alert style and
  guarded Notify queue, including one event email with no CLEARED follow-up.
  Seasonal reports include the celebration name, meaning, timezone and local
  event time. Lunar imagery and phase details belong to Lunar Events.
- There are no condition time offsets. The condition pulses at the first
  automation evaluation at or after the calculated event (normally within the
  switch monitor interval). A switch actor's own Delay Action and Restore Action
  settings continue to apply. A one-pass event is not a sustained time window.
- The first valid evaluation after startup establishes a baseline, without
  replaying an old event. Local ephemeris data and resolved Astral coordinates
  and timezone are required for lunar events. Calculations warm in a worker;
  unavailable data does not trigger an event.
- An **Alert** actor shows a persistent, click-to-dismiss dashboard toast through
  the live WebSocket. BD alerts retain the incoming element's established color
  and From/To sign, element, and plant-part description. Lunar alerts retain the
  standard automation alert style. BD alerts and Notify condition reports show
  the transition time and From/To descriptions, without lunar sky details. Lunar
  alerts and Notify bodies include the Moon phase description, illumination,
  local date/time and location, moonrise/moonset, and sunrise/sunset. Missing rise/set events are identified
  explicitly, including polar conditions.
- A **Notify** actor sends one event email, without a subsequent CLEARED email.
  Each event rearms delivery independently. The guarded queue, retries, and
  rolling delivery limits still apply. The subject identifies the event and time.
- Lunar event email includes an inline PNG **Lunar Calendar** snapshot using the
  dashboard Moon texture, the event phase, four previous/upcoming phase dates,
  and local Sun/Moon times. It uses **Local View**, with the illuminated edge and
  lunar surface oriented for the configured observer at each phase's event time.
  The email also contains the full plain-text report.
  If image rendering fails, the text report is sent and an operator warning is
  logged. Image creation runs in the SMTP worker and needs no browser.
- Existing `type: "bd_transitions"` rules remain valid. The editor loads them as
  Calendar → BD Transitions → All; saving writes `type: "calendar"` with
  `calendar_kind`, `calendar_event`, and `executor_switch_id`. Legacy unsaved
  rules retain their automatic BD toast behavior, including compatibility for
  the initial empty switch/None actor representation.
- Operators can exercise the existing BD toast delivery path by sending a POST
  request to `/advanced/automations/test-bd-transition`, without changing state.

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
- Editing a Notify rule's conditions (including its sensor, metric, threshold,
  or hysteresis) starts a new notification state. A currently true condition
  can send a fresh ACTIVATED email; a currently false condition starts inactive
  without a CLEARED email for the old conditions. Renaming or saving unchanged
  conditions preserves the existing state. The manager stores an internal
  `_notification_revision` in `script_json` so this behavior survives restarts;
  existing rules without it retain their state until their conditions are edited.
  Queued messages from superseded conditions are discarded when the evaluator
  observes the edit. An SMTP send already in progress may still complete, but
  cannot mark the edited conditions as delivered.

Revert behavior:

- `previous_state` restores the state that existed before the rule applied.
- `do_nothing` leaves the switch in its current state when the rule becomes
  false.
- Runtime ownership needed for `previous_state` is persisted in the switch
  settings runtime block.
- Actions set absolute states. They are not toggle or invert commands.
- Saving an enabled rule or enabling a disabled rule prepares every
  `previous_state` switch action with the opposite baseline: an On action starts
  from Off, and an Off action starts from On. Disabled saves, notifications, and
  `do_nothing` actions do not change switch state during save.
- Baseline preparation uses the shared controller path and waits up to 15
  seconds per channel for device confirmation. Existing rules being prepared
  are disabled first; a rejected or unconfirmed command leaves them disabled.
  Some channels may already have changed when another channel fails. Resolve
  the device error and retry save or enable. Missing/offline channels and
  channels owned by another enabled rule are rejected before preparation.
- Once enabled, conditions are evaluated normally. If they already match, the
  action may run immediately after baseline preparation; saving does not defer
  an active timer window until the next hour. Runtime startup does not reset
  baselines.
- `previous_state` is captured when an action actually changes a channel. If
  the channel is already at the requested state, the action is skipped and
  there is no new previous-state value for that action to restore.

## Paired Timer Example

Use one timer automation for paired outputs, such as alternating two LEDs or
two relay channels, by separating the baseline state from the active-window
state.

To run Green normally on and Yellow normally off, then flip them for 8 minutes
every 15 minutes:

1. Set the automation condition to `timer`, Every `15 minutes`, Duration `8`.
2. Set the action rows to the active-window state: Green off, Yellow on.
3. Set **Restore Action** to `To previous state` for both action rows.
4. Save with Enable set to Yes. Sensorius prepares Green on and Yellow off
   before enabling the rule.

During the timer window Sensorius applies the action states. When the window
ends, `To previous state` restores the baseline state that existed before the
actions changed the channels.

Do not encode the baseline as the action state. If the action rows are Green on
and Yellow off, saving the enabled rule prepares Green off and Yellow on, then
the timer window enforces Green on and Yellow off. Also note
that all action rows in a rule share the same condition groups; adding a second
condition row does not bind one condition to one action and another condition
to another action.

## Severe Weather

Select **Severe Weather** as a condition. It has no additional parameters and
requires **US · National Weather Service** as the forecast provider. Sensorius
validates the Astral coordinates with the NWS point service and a US state or
territory before polling the location-filtered active-alert feed once per minute.
MET Norway, Open-Meteo, Disabled, and unsupported locations cannot trigger it.
Actual weather/geophysical warnings and watches qualify; test messages,
advisories, statements, future-effective and expired messages do not.

The condition can be combined with other conditions using the existing AND/OR
groups and can target any existing actor. Each automation/actor runs at most
once for a given event. NWS CAP references and VTEC identities correlate updates
with the original event. Trigger receipts live in SQLite `weather_alert_state`
and survive restarts and condition edits. Separate events can each trigger,
even when their periods overlap. Receipts are retained for 90 days after the
last sighting of an event. Explicitly disabling and saving an existing weather
automation, then enabling and saving it, rearms its actors for one new trigger
per active event. Saving an already-enabled rule, dismissing an Alert, restarting
the application, or receiving an NWS update does not rearm it.

- **Alert** displays the event type, headline, onset/end times in the Astral
  timezone, expected duration, affected area, description, and instructions.
  Dismissing this toast does not cause it to return on subsequent polls or a
  restart. Pending weather Alerts persist in SQLite and are delivered when a
  dashboard connects (within its 15-second refresh interval). Dismissal is saved
  separately and applies to every dashboard. Expired or cancelled events are
  no longer replayed. Older trigger receipts without a saved message cannot be
  replayed; this delivery protection applies to newly triggered events.
- **Notify** uses the same details in an email and the event type in its subject.
  The existing email delivery queue handles delivery attempts. No separate
  recovery email is generated for this event condition. Once accepted into the
  queue, the event receipt prevents re-enqueueing after restart; a process crash
  before the queued email is sent can therefore lose that delivery.
- **Switch** respects the configured delay and reversion behavior. After the
  event action succeeds, it is not repeatedly reasserted. A configured return
  to previous state runs when the rule becomes false.

The dashboard forecast content and Caelus Forecast button remain orange while
qualifying warnings are valid, independently of whether an automation exists
or its toast has been dismissed. Caelus replaces its daily synopsis with all
active event details. Expiry/cancellation restores the normal forecast. On a
feed failure, previously received warnings remain visible only until their
expiry/end; Caelus marks their details as potentially stale.

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

## Manual Auto-Off Clock Handling

Manual auto-off countdowns use monotonic elapsed time. Host clock corrections
cannot shorten or extend a running countdown. The UI still receives an epoch
deadline and remaining seconds, recalculated from that elapsed-time clock.
Timers remain runtime-only and send one Off attempt on expiry; the existing
manual/Advanced automation ownership rules are unchanged.
