# FarmOS Integration

Sensorius can export sensor readings to farmOS log entities through `saiFarmOSBridge`.

## What It Does

- Listens for new sensor readings as they are written to the local data logger
- Queues readings in memory
- Writes each reading to farmOS as a log record (default bundle: `observation`)
- Exposes runtime status and connection test endpoints

## Configure in Web UI

Open System Settings and navigate to the FarmOS pane.

Set:

- Enabled
- Backend (`httpx` or `farmospy`)
- Base URL (`https://your-farmos-host`)
- TLS verification toggle
- Auth fields (token or username/password flow)
- Log bundle

Then use the built-in test action before enabling continuous export.

## `[FarmOS]` Keys

```toml
[FarmOS]
ENABLED = false
BACKEND = "httpx"
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
SCOPE = "farm_manager"
```

Notes:

- `ACCESS_TOKEN` is preferred when present.
- If no static token is present, runtime auth can attempt OAuth password flow with `CLIENT_ID`, `CLIENT_SECRET`, `USERNAME`, `PASSWORD`.
- `SCOPE` is optional and used by the `farmospy` path.
- Queue overflow drops oldest entries first.

## Backends

### `httpx` Backend

- Uses direct HTTP calls to farmOS JSON:API
- Connectivity test targets `<BASE_URL>/api`
- Writes logs with `POST <BASE_URL>/api/log/<LOG_BUNDLE>`

### `farmospy` Backend

- Uses `farmOS.py` client methods
- Requires `farmOS` Python package available at runtime
- Uses client log API (`send` or `create`, based on available method)

## Runtime Status and Test APIs

- `GET /farmos/status`: returns enabled flag, backend, queue depth, token state, package availability, and last error
- `POST /farmos/test`: performs best-effort connectivity/auth test and returns result payload

## Troubleshooting

- `FarmOS.BASE_URL is empty`: set Base URL in settings.
- `Unauthorized (401)`: token/credentials invalid, or OAuth client mismatch.
- `farmOS.py not installed`: use `httpx` backend or install the `farmOS` package into the runtime environment.
- Repeated write failures: check status endpoint `last_error`, then verify TLS, auth, and target bundle permissions in farmOS.
