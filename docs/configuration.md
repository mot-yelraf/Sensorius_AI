# Configuration Guide

This guide captures the runtime environment configuration material originally documented in `README.md`.

## Logging and Debug Environment Variables

Use a project `.env` file as the primary configuration method.
This is the recommended approach for both manual runs and service deployments.

Create or edit `.env` in the project root:

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
