# Configuration Guide

This guide captures the runtime environment configuration material originally documented in `README.md`.

## Logging and Debug Environment Variables

Use a project `.env` file as the primary configuration method.
This is the recommended approach for both manual runs and service deployments.

Create `.env` from `.env.def` (or edit `.env`) in the project root:

```env
# -----------------------------
# Sensorius runtime .env file
# -----------------------------

# Log verbosity: DEBUG, INFO, WARNING, ERROR
SENSORIUS_LOG_LEVEL=DEBUG

# Enable rotating file logging (true/false)
SENSORIUS_FILE_LOG=false

# Log file path/name used when file logging is enabled
SENSORIUS_LOG_FILE=sensorius.log

# Show low-level HTTP client/server debug logs (true/false)
SENSORIUS_HTTP_DEBUG=false

# Debug module filter:
# - comma-separated module names, or
# - ALL
SENSORIUS_DEBUG_MODULES=Sensorius,saiSensor,saiMQTTIngest,saiHtml,saiSwitch,saiWebRoutes

# HTTP bind host/port for the web app
SENSORIUS_HTTP_HOST=0.0.0.0
SENSORIUS_HTTP_PORT=8000

# GUI behavior:
# - empty => auto detect
# - 1/true/yes/on => force GUI
# - 0/false/no/off => force headless
SENSORIUS_GUI=

# Watchdog timing controls
SENSORIUS_WATCHDOG_TIMEOUT_SEC=71
SENSORIUS_WATCHDOG_LOOP_INTERVAL_SEC=10.0
SENSORIUS_WATCHDOG_JITTER_SEC=0.8

# Garbage collector scheduling controls
SENSORIUS_GC_ENABLED=true
SENSORIUS_GC_INTERVAL_SEC=29
SENSORIUS_GC_JITTER_SEC=0.7
SENSORIUS_GC_MIN_SLEEP_SEC=1.0
SENSORIUS_GC_FULL_EVERY_N=10

# Database retention (days)
# 0 disables pruning; default is 90 days.
SENSORIUS_DB_RETENTION_DAYS=90

# Optional API key for protected web endpoints
SAI_WEB_API_KEY=
# Optional API key for Sensorius-to-Sensorius federation
SAI_PEER_API_KEY=

# Linux display/backend hints used by GUI launch/service setup
DISPLAY=
WAYLAND_DISPLAY=
GDK_BACKEND=x11
WEBKIT_DISABLE_COMPOSITING_MODE=1
```

Temporary shell overrides (session-only) are still supported, for example:

```bash
export SENSORIUS_LOG_LEVEL=DEBUG
export SENSORIUS_GUI=0
```

## Web UI System Settings

System Settings in the web UI now allow saving:

- HTTP Port
- Sensorius Hub (MQTT broker)
- Home Assistant integration settings
- FarmOS integration settings (`httpx` backend)
- Time Zone (`Time.TZ`)
- Astral Latitude/Longitude/Altitude (`Astral.LATITUDE`, `Astral.LONGITUDE`, `Astral.ALTITUDE`)
- Gauge Size and Display Style

Time Zone entry supports suggestion options from available IANA zones (`zoneinfo`), prioritized using Astral location when available.

When `Astral.AUTO_IP = true` and manual lat/lon are empty, Sensorius can auto-discover coordinates from IP geolocation and persist them into `[Astral]`.

## FarmOS Settings (`[FarmOS]`)

FarmOS integration can be configured from the System Settings modal in the web UI.
It is stored under the `[FarmOS]` section in `system_settings/*/settings.toml`.

```toml
[FarmOS]
ENABLED = false
BASE_URL = ""
VERIFY_TLS = true
ACCESS_TOKEN = ""
CLIENT_ID = "farm"
CLIENT_SECRET = ""
USERNAME = ""
PASSWORD = ""
LOG_BUNDLE = "observation"
QUEUE_MAX = 1000
FLUSH_INTERVAL_SEC = 3.0
REQUEST_TIMEOUT_SEC = 10.0
```

Key behavior:

- `ENABLED`: if `false`, the bridge stays idle and does not flush queued telemetry.
- `BASE_URL`: farmOS server root URL, for example `https://farm.example.com`.
- `VERIFY_TLS`: HTTPS certificate verification for outbound calls.
- `ACCESS_TOKEN`: static bearer token; when set, it is preferred over runtime OAuth refresh.
- `CLIENT_ID`, `CLIENT_SECRET`, `USERNAME`, `PASSWORD`: used for password-grant token acquisition when no static token is provided.
- `LOG_BUNDLE`: farmOS log bundle to write, default `observation`.
- `QUEUE_MAX`: in-memory queue cap. Oldest events are dropped when full.
- `FLUSH_INTERVAL_SEC`: polling interval used when queue is empty.
- `REQUEST_TIMEOUT_SEC`: outbound request timeout.

Operational notes:

- Use `POST /farmos/test` in the UI to validate connectivity/auth before enabling export.
- Bridge status is available from `GET /farmos/status` (queue depth, token state, last error).
- Secrets are obfuscated in saved settings and deobfuscated at runtime for auth flows.

See `docs/farmos.md` for end-to-end setup and troubleshooting.

For Home Assistant integration details, see `docs/homeassistant.md`.
