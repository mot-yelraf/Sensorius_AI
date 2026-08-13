# Integrated Biodynamic Calendar

The former standalone BD Calendar companion is now integrated into Sensorius.
This document records the migration for operators who previously ran the
companion service.

## Current Behavior

The dashboard **Calendar** button opens the full-screen calendar at:

```text
http://<sensorius-host>:8000/calendar
```

The calendar runs in the Sensorius FastAPI process and uses the same HTTP port,
Astral settings, SQLite database, lifecycle, and version as Sensorius. Select
**Dashboard** in the top-left to return to the dashboard.

The integrated application includes the current month, Sun and Moon graphics,
Moon Phase Local and Reference views, a Next 12 Months planning view, planting
records, daily guidance and notes, and printable reports.

Location is controlled by the Sensorius **General Settings** Astral and Time
sections. The calendar does not maintain a second location configuration.

## Storage

All calendar state is stored in the active Sensorius SQLite database:

- `biodynamic_notes`
- `biodynamic_daily_summaries`
- `biodynamic_plantings`
- `biodynamic_calendar_cache`

The schema changes are additive. Existing notes and summaries remain
available. The cache is disposable and is rebuilt when missing or when the
location or calculation version changes.

On first integrated use, former `~/.biodynamic_calendar/notes.json` and
`~/.biodynamic_calendar/plantings.json` data is imported when the matching
Sensorius table is empty.

## Background Loading

Calendar calculation starts only after the primary Sensorius runtime has had
time to settle. A single background worker warms the current month first,
followed by nearby and future months with pauses between builds. Current-day
Astral information and biodynamic guidance are warmed with the current month.

The calendar shell opens immediately. If the system has only just started, the
12-month planning section can report that it is warming and refresh as cached
months become available. Sensor collection, MQTT, switch control, and
automation startup do not wait for calendar warming.

Operators can tune the worker with:

- `SENSORIUS_BIODYNAMIC_PREWARM_DELAY_SEC`
- `SENSORIUS_BIODYNAMIC_PREWARM_PAUSE_SEC`
- `SENSORIUS_BIODYNAMIC_PREWARM_PAST_MONTHS`
- `SENSORIUS_BIODYNAMIC_PREWARM_FUTURE_MONTHS`

## Removing The Former Companion Service

Sensorius no longer probes port `8765`, launches an iframe, or falls back to
the previous dashboard calendar modal. A separately running BD Calendar
service is not used and can be removed from systemd, launchd, or Windows
startup if it was previously installed.

The former `SENSORIUS_DB_PATH`, `BD_CALENDAR_SENSORIUS_DB_PATH`, and
`BIODYNAMIC_CALENDAR_SENSORIUS_DB_PATH` environment variables are not required
for the integrated application.

## Health And Troubleshooting

Use the normal Sensorius health endpoint:

```text
http://<sensorius-host>:8000/healthz
```

If the calendar has no data, confirm the Astral latitude, longitude, and
timezone; confirm Skyfield was installed by the Sensorius setup process; allow
the initial background warm cycle to complete; and confirm Sensorius can write
to its active SQLite database.
