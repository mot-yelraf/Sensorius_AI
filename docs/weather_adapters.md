# Weather device adapters

A weather device can be a gateway or console collecting an entire sensor family.
WeeWX commonly communicates with those aggregators; direct sensor access is also
possible with suitable drivers/extensions. Sensorius can ingest WeeWX's archive
or MQTT output, or acquire directly through a supported weather device adapter.
These are separate acquisition paths. WeeWX is not required for direct Ecowitt
or AcuRite support.

## Review of Caelus and implementation in Sensorius

The Caelus review covered its observation contract, adapter selection, Ecowitt
HTTP normalizer, AcuRite report decoder/transports, console profiles, acquisition
freshness, fixtures, settings, and installer setup. The useful boundaries are:

- Keep vendor report parsing and device communication inside adapters.
- Describe capabilities independently from the sensors currently reporting.
- Separate connectivity, outdoor observation freshness, and battery health.
- Keep timestamps, source identity, units and rain-counter provenance explicit.
- Keep optional discovery/setup separate from acquisition; USB consoles need not
  implement LAN discovery.
- Put SQLite and derived weather calculations behind shared code.

Sensorius preserves its existing SQLite schema, sensor IDs and metric names
rather than importing Caelus's database layout or station-selection model.
Ecowitt and AcuRite run concurrently as distinct station devices. There is one
configured gateway and one configured USB console per hub in this implementation.
A console serial selects one device when several are attached; it does not enable
simultaneous acquisition from all attached consoles.

## Acquisition contract

`WeatherAdapter` is defined in `sensorius/weather_adapters/observations.py`:

- `async read() -> Observation | None` performs bounded acquisition without
  blocking the event loop. None means no new usable observation. Transport errors
  are recoverable by the existing supervisor; they must not stop the hub.
- `capabilities()` describes supported normalized fields/channels, not current
  availability. Newly supported channel metrics must retain unique stable labels.
- `status()` reports safe local diagnostics, including source connectivity,
  observation freshness and station battery health. It never probes hardware or
  performs network I/O. Unknown battery status remains `UNKNOWN`.

An `Observation` has a source ID, aware observation/receipt timestamps converted
to UTC, finite numeric measurements and optional explicit rain policy/counter
provenance. Zero is valid. None, booleans, malformed and nonfinite measurements
are omitted. Device timestamps should be used when available; these adapters use
acquisition time because their live reports do not supply observation timestamps.

Metric names and storage units remain Sensorius's established contract:
`Temperature` is Celsius, `Temperature_F` Fahrenheit, `Rel-Humidity` percent,
pressure hPa, wind mph, direction degrees clockwise from north, and rain inches.
Station Pressure means uncorrected absolute pressure, retained under the existing
`Console Absolute Baro-Pressure` key for AcuRite. System altitude supplies the
separate corrected `Baro-Pressure` metric; absolute pressure is never overwritten. Adding a new metric also
requires a gauge definition and review of unit conversion, HA discovery, graphs
and automation use. Display preferences do not change stored units.

`EcowittGatewayIngest.read()` implements the async contract with the existing LAN
transport and recovery path. Its `poll_once()` retains existing daily rain and
restart behavior. `AcuRiteUsbIngest.read()` wraps the bounded PC Connect reader in
`asyncio.to_thread`; its supervisor samples every 18 seconds and persists at the
configured retrieval interval (rounded up to a sample boundary). The internal
USB reader owns per-field freshness and RF-loss invalidation. Shared
`persist_observation()` calls `saiDataLogger.log_readings`, preserving listeners,
Home Assistant, farmOS and history. AcuRite sensor offsets are loaded from sensor TOML and applied to fresh snapshots
before persistence; derived air metrics are then recalculated. Shared outdoor humidity/VPD calculations live
in `weather_adapters/metrics.py`. Missing AcuRite capability fields are written as
unavailable snapshots so previously cached temperature/RH cannot appear fresh
when only wind or console reports continue arriving. Historical values remain.

AcuRite's rolling tip counter is distinct from daily/lifetime rain. The first
counter establishes a baseline. Only a newer counter from the same console and
RF sensor can produce interval `Rain`; repeated counter timestamps cannot emit
the same rain twice. A plausible rollover from above 80 inches to below 2 inches
uses the 81.92-inch modulus. Other backwards changes rebaseline. Baselines commit
only after a successful database write. RF loss, disconnects, configuration
changes, process restarts and gaps exceeding twice the retrieval interval reset
the baseline. Rain during those gaps is unavailable; it is not manufactured as
zero. Raw counters and baselines are not persisted. This conservative limitation
avoids a database schema change and should be revisited before archive recovery
or gap-spanning rainfall is offered.

## Extending support

1. Implement bounded communication and normalization behind the contract. Reuse
   an existing gateway adapter if the new family shares its protocol; gateway
   compatibility does not imply support for every attached sensor channel.
2. Add setup validation and a device entry without embedding vendor parsing in
   routes, dashboard code or the logger. Keep existing device TOML settings as the
   source of configuration and use settings managers for writes.
3. Register the adapter service in `sensorius/app.py` and `RuntimeServices`.
   Preserve the current supervisor/watchdog lifecycle. For many future families,
   replace explicit registrations with a small factory/registry once there is a
   concrete need; avoid a second independent task scheduler.
4. Materialize station defaults using `ensure_station_settings` and preserve
   operator location/display choices. Define a stable device identity, then keep
   source/channel provenance separate for counters.
5. Add protocol fixtures, malformed/zero/missing-value tests, reconnect/freshness
   tests, persistence/listener tests, setup route checks and the host browser gate.
6. Update platform dependencies, packaged worker entrypoints and canonical docs.
   Keep native HID on macOS/Windows and least-privilege USB access on Linux.

GW2000/GW3000 fixtures were ported from Caelus and are synthetic protocol-derived
examples, not physical-device captures. AcuRite R1/R2 fixtures are also synthetic.
USB models, native platform packaging and physical gateways still need field
verification. The [WeeWX AcuRite guide](https://weewx.com/docs/5.5/hardware/acurite/)
describes alternating packets, USB mode behavior and available measurements.

## Existing persistence follow-up

Python 3.13 checks with warnings treated as errors expose existing unclosed
SQLite connections in the shared logger's initialization and rolling-rain query
paths (`saiDataLogger._init_db` and `get_metric_sum_for_window`). SQLite's
connection context manager commits/rolls back but does not close the connection.
This integration keeps using the canonical logger; a separate, consistent
connection-lifecycle fix is preferable to adding adapter-specific database
access. Normal integration checks pass; USB/HID-only checks also pass with
warnings treated as errors.
