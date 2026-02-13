# Switch Automations

This guide captures the switch automation overview originally documented in `README.md`.

Switch automations support:

- Rule-level enable/disable (Basic and Advanced rules)
- Sensor + metric threshold conditions (for example: `Temperature_F > 82`)
- Threshold hysteresis and minimum interval timing to reduce relay chatter
- Time-of-day windows (`start` / `end`) and day-based scheduling (`days` in Advanced rules)
- Timer-based schedules (`duration_min`, `freq_hours`) for periodic ON windows
