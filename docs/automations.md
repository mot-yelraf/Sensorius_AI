# Switch Automations

This guide captures the switch automation overview originally documented in `README.md`.

Switch automations support:

- Rule-level enable/disable (Basic and Advanced rules)
- Sensor + metric threshold conditions (for example: `Temperature_F > 82`)
- Threshold hysteresis and minimum interval timing to reduce relay chatter
- Time-of-day windows (`start` / `end`) and day-based scheduling (`days` in Advanced rules)
- Astral conditions (`astral_event` + `offset_min`) for sunrise/sunset schedules
- Timer-based schedules (`duration_min`, `freq_hours`) for periodic active windows
- Action-level revert behavior via `revert_action` (`previous_state` or `do_nothing`) plus `delay_s`

Time window notes:

- `00:00` to `00:00` is treated as all day.
- Other time windows are inclusive of the start and exclusive of the end.
- Wraparound windows such as `22:00` to `06:00` are supported.

Action revert notes:

- `delay_s` is a delay before the action is applied after the rule becomes true.
- While a rule remains true, the evaluator keeps the target switch at the configured action state.
- For timer conditions, `duration_min` defines how long that timer window stays active within each period.
- If a rule later becomes false and `revert_action = "previous_state"`, the evaluator restores the switch to the state it had before the rule first applied.
- If a rule later becomes false and `revert_action = "do_nothing"`, the evaluator leaves the switch in its current state.

Astral conditions require `astral` and use location from:

- Manual settings in `[Astral]` (`LATITUDE`, `LONGITUDE`, `TIMEZONE`), or
- IP geolocation fallback when `[Astral].AUTO_IP = true` (internet required).

## Controller Model

Switch automation evaluation runs through a common controller interface:

- `SwitchController` for directly connected GPIO relays.
- `RemoteSwitchController` for MQTT-backed Nodus/Pico switches.

Both controllers expose the same runtime behavior to the automation engine
(`get_switch_names`, `get_state`, `set_state`, override flags, and monitor loop),
so rules execute consistently for local and remote switches.
