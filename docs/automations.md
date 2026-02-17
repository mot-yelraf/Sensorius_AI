# Switch Automations

This guide captures the switch automation overview originally documented in `README.md`.

Switch automations support:

- Rule-level enable/disable (Basic and Advanced rules)
- Sensor + metric threshold conditions (for example: `Temperature_F > 82`)
- Threshold hysteresis and minimum interval timing to reduce relay chatter
- Time-of-day windows (`start` / `end`) and day-based scheduling (`days` in Advanced rules)
- Timer-based schedules (`duration_min`, `freq_hours`) for periodic ON windows

## Controller Model

Switch automation evaluation runs through a common controller interface:

- `SwitchController` for directly connected GPIO relays.
- `RemoteSwitchController` for MQTT-backed Nodus/Pico switches.

Both controllers expose the same runtime behavior to the automation engine
(`get_switch_names`, `get_state`, `set_state`, override flags, and monitor loop),
so rules execute consistently for local and remote switches.
