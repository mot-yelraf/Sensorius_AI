# Biodynamic Calendar Companion App

This note tracks the standalone Biodynamic Calendar app integration work for
Sensorius. The implementation lives in
`/Users/twfarley/Projects/Biodynamic_Calendar`; Sensorius remains the hub and
owns the runtime database.

## Implemented In Biodynamic Calendar

The standalone Biodynamic Calendar app can now run as a Sensorius companion app
on the same host as Sensorius.

Implemented behavior:

- Sensorius SQLite storage support for calendar state.
- Additive SQLite tables for plantings and calendar cache.
- Compatibility with existing Sensorius tables:
  - `biodynamic_notes`
  - `biodynamic_daily_summaries`
- New companion tables:
  - `biodynamic_plantings`
  - `biodynamic_calendar_cache`
- First-start import of existing local JSON notes and plantings when the
  corresponding Sensorius SQLite tables are empty.
- Sensorius Astral settings are preferred for calendar location when available.
- Local JSON storage under `~/.biodynamic_calendar/` remains the fallback.
- Health endpoints for Sensorius detection:
  - `/healthz`
  - `/api/health`
- Sensorius launch mode:
  - `/?source=sensorius`
  - hides the standalone app top row cards.
  - direct browser access keeps the full standalone app UI.

## How To Run The Companion App

Run the Biodynamic Calendar app on the Sensorius host and point it at the
Sensorius runtime database:

```bash
cd /Users/twfarley/Projects/Biodynamic_Calendar
SENSORIUS_DB_PATH=/Users/<user>/Sensorius/sensorius_data.db \
PYTHONPATH=src uvicorn biodynamic_calendar_app.app:app --host 0.0.0.0 --port 8765
```

For a standard macOS install for this user, the database path is:

```text
/Users/twfarley/Sensorius/sensorius_data.db
```

The app also recognizes:

- `SENSORIUS_DB_PATH`
- `BD_CALENDAR_SENSORIUS_DB_PATH`
- `BIODYNAMIC_CALENDAR_SENSORIUS_DB_PATH`
- `BD_CALENDAR_STORE=sensorius`
- `BIODYNAMIC_CALENDAR_STORE=sensorius`

If the store is set to `sensorius` and no explicit database path is supplied,
the app uses:

```text
~/Sensorius/sensorius_data.db
```

## How To Access

Direct standalone access keeps the full BD Calendar app UI:

```text
http://127.0.0.1:8765/
```

Sensorius Calendar button access should open the companion UI mode:

```text
http://127.0.0.1:8765/?source=sensorius
```

For a remote browser, replace `127.0.0.1` with the Sensorius host name or IP.
For example, if Sensorius is opened at:

```text
http://sensorius.local:8000/
```

then the companion calendar should be opened at:

```text
http://sensorius.local:8765/?source=sensorius
```

Health checks:

```text
http://127.0.0.1:8765/healthz
http://127.0.0.1:8765/api/health
```

`/healthz` returns plain `ok`. `/api/health` returns JSON including the active
store class.

## Sensorius Integration Hook

Sensorius redirects the dashboard Biodynamic Calendar card's **Calendar** button
to the standalone companion when that app is running, and otherwise keeps the
integrated calendar modal as the fallback. The behavior is:

1. When the Calendar button is clicked, Sensorius probes the same host on port
   `8765` through its local status endpoint:

   ```text
   /api/biodynamic-calendar-companion
   ```

   That endpoint checks:

   ```text
   http://127.0.0.1:8765/healthz
   ```

   If the health route is unavailable but the companion app is serving its
   `/?source=sensorius` page, Sensorius treats that as the companion app being
   available. This keeps compatibility with running BD Calendar app instances
   that serve the app UI and `/api/calendar` but do not expose `/healthz`.

2. If the probe succeeds, Sensorius opens the companion app in a full-window
   overlay inside the Sensorius dashboard with a **Back to Sensorius** button:

   ```text
   http://<sensorius-host>:8765/?source=sensorius
   ```

3. If the user clicks **Back to Sensorius** or presses Escape, Sensorius closes
   the companion overlay and returns to the dashboard view.

4. If the probe fails, Sensorius opens its integrated biodynamic calendar.

5. During Sensorius install, the user can be offered the optional BD Calendar
   companion install. The app may also be installed before or after Sensorius,
   as long as it runs on the same host and can access the Sensorius database.

The Sensorius installer or service wrapper should run the companion app with
`SENSORIUS_DB_PATH` set to the active Sensorius runtime database.

## Verification Completed In Biodynamic Calendar

The BD Calendar implementation was verified with:

```bash
pytest -q
python -m compileall -q src/biodynamic_calendar_app
```

At the time this note was written, the BD Calendar project version containing
this companion support is `v0.26.165.8`.
