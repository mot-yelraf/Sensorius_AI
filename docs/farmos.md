# farmOS Integration

Sensorius can export newly written sensor readings to farmOS log entities
through `sensorius/saiFarmOSBridge.py`.

## Runtime Flow

1. `sensorius.saiDataLogger.log_readings` writes sensor metrics.
2. `sensorius.saiFarmOSBridge` receives the readings listener callback.
3. The bridge queues the reading in memory.
4. The worker loop posts JSON:API log payloads to farmOS with `httpx`.
5. Failed writes are pushed back to the front of the queue for retry.

The queue is in memory only. A service restart clears queued-but-unsent items.

## Settings

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

- `ENABLED`: controls whether the bridge flushes telemetry.
- `BASE_URL`: farmOS server root URL, for example
  `https://farm.example.com`.
- `VERIFY_TLS`: controls HTTPS certificate verification.
- `ACCESS_TOKEN`: static bearer token. Preferred when present.
- `CLIENT_ID`, `CLIENT_SECRET`, `USERNAME`, `PASSWORD`: OAuth password-flow
  inputs used when no static token is configured.
- `LOG_BUNDLE`: farmOS log bundle, default `observation`.
- `QUEUE_MAX`: maximum queued readings. Oldest items are dropped when full.
- `FLUSH_INTERVAL_SEC`: idle polling interval.
- `REQUEST_TIMEOUT_SEC`: outbound HTTP timeout.

Secrets are obfuscated at rest by `sensorius.saiSettings`. This is reversible
obfuscation, not encryption.

## APIs

- `GET /farmos/status`: enabled flag, base URL, TLS verification state, log
  bundle, queue depth, static/runtime token state, and last error.
- `POST /farmos/test`: best-effort connectivity and auth test.

Use the test endpoint before enabling continuous export.

## Operations

Recommended enablement flow:

1. Configure `BASE_URL`, TLS verification, and auth.
2. Run the FarmOS test action from System Settings.
3. Confirm the target log bundle and permissions in farmOS.
4. Enable export.
5. Watch `/farmos/status` after new readings are written.

farmOS export sends new readings while enabled. It does not backfill historical
database rows unless a separate backfill tool is implemented.

## Troubleshooting

`FarmOS.BASE_URL is empty`:

- Set the base URL in System Settings.

Unauthorized:

- Check access token or OAuth credentials.
- Confirm the farmOS OAuth client and user permissions.

TLS failures:

- Keep `VERIFY_TLS=true` for normal HTTPS deployments.
- Temporarily disable only for controlled local tests with known certificates.

Repeated write failures:

- Check `/farmos/status`.
- Confirm network reachability from the Sensorius host.
- Confirm the configured `LOG_BUNDLE` exists and accepts writes.
- Restarting Sensorius clears the in-memory queue, so inspect status before
  restarting if you need failure details.
