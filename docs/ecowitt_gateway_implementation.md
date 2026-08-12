# Ecowitt GW1100 Integration Implementation Brief

This document is a handoff to the VS Code Codex agent that will implement
local Ecowitt gateway support in Sensorius. Read the repository-root
`AGENTS.md` before making changes and follow all versioning, testing,
documentation, runtime-path, and compatibility requirements in that file.

## User Goal And Expected Hardware Setup

The initial target is an Ecowitt GW1100 gateway paired with an Ecowitt 7-in-1
outdoor weather array (commonly sold as WS69, but the gateway API may identify
the sensor family internally as WH65 or another Ecowitt type).

The user will:

1. Use the Ecowitt application or the gateway web interface to join the GW1100
   to the local 2.4 GHz Wi-Fi network.
2. Confirm that the gateway and outdoor array work in Ecowitt before adding
   them to Sensorius.
3. Prefer metric display units in the gateway, when available. Sensorius must
   nevertheless accept either metric or imperial gateway output.
4. Make the gateway reachable from the Sensorius host. A DHCP reservation is
   recommended so the gateway address remains stable.

The user originally considered configuring a 300-second HTTP push interval.
That is not required for the recommended implementation. The GW1100 has a
local HTTP API that Sensorius can poll. If push support is implemented later,
the gateway would need the Sensorius host/address, listening port, and request
path; setting an upload interval alone is insufficient.

## Confirmed Ecowitt LAN API

Ecowitt's current generic HTTP API explicitly includes the GW1100. The local
API uses ordinary HTTP GET requests and returns JSON.

Primary references:

- Ecowitt product support page:
  <https://www.ecowitt.com/support/download/106>
- Ecowitt HTTP API Interface Protocol V1.0.6:
  <https://oss.ecowitt.net/uploads/20260114/HTTP%20API%20interface%20Protocol%20(Generic)-(V1.0.6-2026-1-14)%20.pdf>

Relevant read-only endpoints are:

```text
GET http://<gateway>/get_version?
GET http://<gateway>/get_network_info?
GET http://<gateway>/get_device_info?
GET http://<gateway>/get_units_info?
GET http://<gateway>/get_sensors_info?page=1
GET http://<gateway>/get_sensors_info?page=2
GET http://<gateway>/get_livedata_info?
GET http://<gateway>/get_rain_totals?
```

Do not use any `set_*`, reboot, restore, calibration-write, sensor-registration,
or firmware-upgrade endpoint in the initial integration. Sensorius should be a
read-only LAN client.

`get_sensors_info` returns the gateway's sensor inventory. Entries can contain
the internal image/family name, numeric type, display name, sensor ID, battery,
RF signal, registration status, and sometimes firmware version. The current
API is paginated across pages 1 and 2.

The inventory can include unused, re-register, or disabled slots. Do not treat
IDs `FFFFFFFF` or `FFFFFFFE` as active sensor identities. ID `0` is ambiguous
in the official examples and is retained only with evidence such as a nonzero
RF signal. Do not
delete a previously discovered sensor merely because its current RF signal is
zero; retain it and mark it unavailable. Confirm exact behavior against the
real GW1100 response before finalizing the filter.

The protocol documents no query option for selecting only particular live-data
sections; each request returns the complete live-data object. Sensorius may
select sections after receipt for profiling or parsing, but that does not
reduce gateway response size. The profiler supports direct sampling with
`--ecowitt-url`, `--ecowitt-only`, and client-side `--ecowitt-sections`.

`get_livedata_info` reports current observations in sections such as
`common_list`, `rain`, `piezoRain`, and `wh25`, with additional channel arrays
for optional Ecowitt sensors. Common weather item IDs include:

| ID | Observation |
| --- | --- |
| `0x02` | Outdoor temperature |
| `0x03` | Dew point |
| `0x04` | Wind chill |
| `0x07` | Outdoor relative humidity |
| `0x08` | Absolute barometric pressure |
| `0x09` | Relative barometric pressure |
| `0x0A` | Wind direction |
| `0x0B` | Wind speed |
| `0x0C` | Wind gust |
| `0x0D` | Rain event total |
| `0x0E` | Rain rate |
| `0x10` | Rain day total |
| `0x11` | Rain week total |
| `0x12` | Rain month total |
| `0x13` | Rain year total |
| `0x15` | Light or solar-radiation value; interpret its returned unit |
| `0x17` | UV index |
| `0x19` | Daily maximum wind |

Ecowitt responses can place the unit in a separate `unit` member or append it
to the value text. The parser must handle both representations and must not
assume the gateway is configured for one unit system.

## Recommended Scope: Polling MVP

Implement local pull/polling first. Do not implement Ecowitt custom-server
push, Ecowitt Cloud API access, or gateway MQTT configuration in the same
change.

The existing Add Device stub is in
`ui_templates/modals/system_settings.html`. It currently has a gateway URL and
polling interval but no behavior. Those fields are directionally correct.

The completed panel provides four primary items in a two-column layout:

- Row 1: gateway base address, for example `http://192.168.1.100`, and a
  `Find Sensors` action.
- Row 2: the valid discovered-sensor list and data retrieval interval.
- Poll interval in seconds, defaulting to 60. Allow 300 seconds if the user
  prefers it. A minimum of 60 seconds is appropriate for the initial version
  and avoids unnecessary work on low-power hosts.
- A `Test and Discover` action.
- Clear progress and validation errors.
- Gateway identity and firmware returned by `get_version` and
  `get_network_info`.
- A discovered-sensor list built from both `get_sensors_info` pages.
- An indication of whether each registered sensor is also present in the live
  data.
- An `Add` or `Save` action that persists only after successful validation.
- A status area showing last successful poll, freshness, and the most recent
  connection or parsing error without exposing secrets.

Treat the entered value as a base URL. Normalize a trailing slash and append
known endpoint paths internally. For the first version, accept local plain
HTTP, reject embedded credentials, fragments, and unexpected path/query
components, and provide an operator-visible validation error. Hostnames should
remain usable; do not require a numeric IPv4 address.

Do not log complete `get_network_info`, `get_device_info`, or weather-service
settings responses. They can contain local network information. There is no
need to call `get_ws_settings` for discovery or ingestion, and its response can
contain custom-server or MQTT credentials.

## Sensorius Architecture

Follow the existing station-ingestion pattern rather than forcing Ecowitt into
the Raspberry Pi direct-sensor factory.

Suggested modules and responsibilities:

- `sensorius/saiEcowitt.py`
  - Async HTTP client and supervised polling task.
  - Read current settings on each loop so enable/disable and interval changes
    can take effect without a process restart where practical.
  - Bounded connection/read timeout (approximately 5 seconds).
  - No overlapping polls.
  - Watchdog heartbeats during long waits, following `saiWeeWX.py`.
  - Lightweight, rate-limited error logging through the existing logging
    conventions.
- `sensorius/sensor_modules/station_ecowitt.py`
  - Pure parsing, sensor-inventory normalization, unit conversion, and metric
    mapping helpers.
  - No network or settings I/O, so captured payloads can be unit-tested.
- `sensorius/app.py`
  - Create one always-registered Ecowitt ingestion service and add its `run`
    method to `saiTaskSupervisor`, following the WeeWX/farmOS pattern.
- `sensorius/saiWebRoutes.py`
  - Thin validation, discovery, save, status, and disable/remove handlers.
  - Put network and parsing behavior in `saiEcowitt.py`, not in route bodies.
- `sensorius/saiSensorSettingsManager.py`
  - Add an Ecowitt station default only if it is needed for idempotent sensor
    settings materialization.

Use the existing `httpx` dependency already used by Sensorius. Do not add a
new HTTP or Ecowitt library unless there is a demonstrated requirement and the
dependency tradeoff has been discussed.

Persist integration configuration through `sensorius.saiSettings`, not direct
TOML edits. A single-gateway `[Ecowitt]` system-settings section is sufficient
for this initial UI, but keep service and parser boundaries suitable for adding
multiple gateways later. Expected fields include:

```toml
[Ecowitt]
ENABLED = false
GATEWAY_URL = ""
POLL_INTERVAL_SEC = 300
SENSOR_ID = ""
INVENTORY_JSON = "[]"
RAIN_SOURCE = ""
RAIN_RESET_HOUR = 0
```

Add compatibility defaults to both applicable factory settings templates.
Existing installations with no `[Ecowitt]` section must continue starting
normally.

Use a stable Sensorius sensor ID derived from the normalized gateway MAC, such
as `ecowitt-<12 lowercase hex digits>`. Do not use the gateway IP as identity.
Store the internal Ecowitt sensor IDs and types as station metadata so the UI
can show what is paired without changing the Sensorius station ID whenever a
sensor temporarily disappears.

Materialize `sensor_settings/<sensor_id>/sensor.toml` idempotently, with an
Ecowitt/station type that `sensorius/app.py` recognizes as externally ingested
and therefore does not pass to the local GPIO/I2C `SensorController` factory.
Update `is_remote_sensor_settings(...)` or use an already supported station
type deliberately; do not rely on an accidental skip after an initialization
failure.

All accepted readings must be written through
`sensorius.saiDataLogger.log_readings`. This preserves the latest-value cache,
listeners, graphs, farmOS forwarding, Home Assistant publication, and other
downstream behavior.

## Metric And Unit Contract

Gateway unit selection must not change the meaning of a Sensorius metric or
create different database identities for the same physical observation.
Normalize every value based on the unit returned with that value. Audit the
existing station contract in `sensorius/sensor_modules/station_weewx.py`,
`sensorius/saiHomeAssistantMqtt.py`, `sensorius/saiHtml.py`, and the canonical
sensor documentation before finalizing names.

Prefer existing Sensorius weather metrics when the semantics match:

- `Temperature` in degrees C and `Temperature_F` in degrees F.
- `Rel-Humidity` in percent.
- `Dew Point` in degrees C and `Dew Point_F` in degrees F.
- `Baro-Pressure` in hPa, normally using relative pressure for station display.
- `Wind Speed` in mph for compatibility with the existing WeeWX/HA contract.
- `Wind Direction` in degrees.
- `Rain` as interval rainfall in inches.
- `Rain Rate` in inches/hour.

Additional metrics include `Wind Gust`, `Wind Chill`,
`Solar Radiation`, `UV Index`, `Rain Day`, `Rain Week`, `Rain Month`, and
`Rain Year`. Finalize their exact names, units, gauge configuration, and Home
Assistant metadata together and cover them with compatibility tests. Do not
silently assign a lux value to `Solar Radiation` or a W/m2 value to a lux
metric; use the actual returned unit.

Gateway built-in indoor temperature/humidity/pressure should not overwrite the
outdoor array values. Give indoor observations explicit gateway/indoor metric
names if they are exposed in the initial UI.

Parsing requirements:

- Accept numeric values and numeric strings.
- Strip unit suffixes and percent signs safely.
- Treat `None`, empty strings, malformed numbers, and absent objects as missing
  observations rather than zero.
- Handle decimal values and case variations in units.
- Convert C/F, hPa/inHg, m/s/km/h/mph, and mm/in explicitly.
- Ignore unknown item IDs without failing the whole reading, while making them
  inspectable under debug logging without logging an entire sensitive payload.
- Preserve metric-name stability after release.

## Rainfall Correctness Requirement

This is the most important data-integrity pitfall.

Ecowitt's `Rain Event`, `Rain Day`, `Rain Week`, `Rain Month`, and `Rain Year`
values are cumulative counters. Sensorius's existing `Rain` metric is an
interval amount, and `saiDataLogger` calculates `Rain Last 24h` by summing
stored `Rain` values. Never write an Ecowitt cumulative counter directly as
`Rain`, or rainfall totals will be severely overcounted.

For the polling implementation, `get_rain_totals.rainFallPriority` selects the
single authoritative `rain` or `piezoRain` array; a no-gauge priority emits no
rain metrics. Then:

1. Normalize and store `Rain Day` as its own cumulative metric.
2. Calculate `Rain` as the non-negative change in `Rain Day` since the prior
   accepted poll.
3. If no previous sample exists, do not synthesize the current entire daily
   total as a new interval.
4. On service restart, recover the prior daily total from the latest stored
   `Rain Day` reading when possible.
5. When the gateway counter resets at its configured day boundary, treat the
   new current total as rainfall since reset.
6. Treat unexplained large negative changes or manual calibration/reset events
   conservatively and cover them in tests.
7. Keep units consistent before calculating the delta.

Review the minute-deduplication behavior in `saiDataLogger` when choosing the
minimum poll interval. A 60-second minimum avoids creating multiple independent
rain increments inside one deduplication minute in the initial implementation.

## Discovery Versus Availability

Keep these concepts separate:

- Registered/discovered: returned by `get_sensors_info` with a usable sensor
  identity.
- Reporting: the corresponding observation is present in
  `get_livedata_info`.
- Gateway reachable: the last HTTP validation or poll succeeded.
- Fresh: the most recent accepted reading is within approximately three poll
  intervals.

A sensor can remain registered while temporarily not reporting. The UI and
stored metadata should preserve it and display an unavailable/stale state.
Refresh the inventory at Add time, explicit user refresh, service start, and a
low-frequency interval such as daily—not on every live-data poll.

## Routes And Runtime Behavior

Choose route names consistent with current System Settings APIs. At minimum,
the UI needs operations equivalent to:

- Test/discover a submitted gateway URL without persisting it.
- Save/enable a successfully validated gateway.
- Read current configuration and runtime status.
- Disable or remove the integration without deleting historical readings.
- Refresh the registered sensor inventory.

Network calls inside async routes must use async I/O or be moved off the event
loop. Return concise structured errors for DNS failure, timeout, connection
refusal, non-JSON response, HTTP authorization failure, unsupported gateway,
and payload-schema mismatch.

The supervised ingestion task should stay alive while disabled and sleep with
watchdog heartbeats, like other always-registered optional integrations. An
individual timeout or malformed response must not terminate Sensorius.

Historical readings belong to the user. Disabling or removing the configured
gateway must stop polling and remove active configuration/settings references
only; do not delete SQLite history unless a separate explicit destructive
operation is designed and confirmed.

## Home Assistant And Downstream Consumers

Confirm that the first Ecowitt database write triggers dynamic Home Assistant
sensor discovery through the existing data-logger listener. Add metadata for
new metric names to `sensorius/saiHomeAssistantMqtt.py`. Keep entity IDs stable
across IP changes, unit changes, restart, and temporary sensor loss.

Exercise the integrated weather-forecast code with the Ecowitt metric set,
especially temperature, pressure, wind, rain rate, UV index, and solar
radiation. Confirm dashboard metric selection, graphs, statistics, automation
metric lists, farmOS listeners, and diagnostics can consume the station without
special-case breakage.

## Tests Required Before Handoff

Add focused tests with sanitized captured fixtures for both metric and imperial
responses. Suggested files:

- `testApparatus/test_ecowitt_parser.py`
- `testApparatus/test_ecowitt_ingest.py`
- `testApparatus/test_ecowitt_routes.py`
- Extend `testApparatus/test_onboarding_v2_routes.py` for the completed panel.
- Extend Home Assistant and integrated-weather tests for new metrics.

Cover at least:

- Both sensor-inventory pages and active/sentinel sensor IDs.
- A registered sensor with zero RF signal.
- Metric and imperial live payloads producing identical normalized readings.
- Unit suffixes embedded in values versus separate unit fields.
- Missing arrays, unknown IDs, `None`, malformed numeric values, and partial
  payloads.
- Timeout, refused connection, HTTP error, invalid JSON, and unexpected schema.
- Stable ID generation from differently formatted MAC addresses.
- Idempotent system and sensor settings materialization.
- Existing installation with no Ecowitt settings.
- Rain delta, no prior sample, service restart, midnight reset, manual reset,
  and dry periods.
- Polling enable/disable and interval changes.
- Task recovery after a failed poll.
- Home Assistant units and discovery for every new weather metric.
- No accidental local `SensorController` construction for the Ecowitt station.

Run the smallest relevant tests first, followed by the repository's Python
compile test. Run existing WeeWX, data-logger, Home Assistant, onboarding-route,
and integrated-weather tests because Ecowitt reuses those contracts.

For UI verification, follow the `AGENTS.md` instructions: use Playwright and
its bundled browser/headless shell for DOM behavior and screenshots. Do not
launch the installed Google Chrome application directly in headless mode.

## Real-Hardware Verification

Do not claim complete GW1100 support solely from protocol fixtures. When the
hardware is available, run read-only checks from the same host/network as
Sensorius:

```bash
curl --max-time 5 'http://<gateway-ip>/get_version?'
curl --max-time 5 'http://<gateway-ip>/get_units_info?'
curl --max-time 5 'http://<gateway-ip>/get_sensors_info?page=1'
curl --max-time 5 'http://<gateway-ip>/get_sensors_info?page=2'
curl --max-time 5 'http://<gateway-ip>/get_livedata_info?'
```

Sanitize the MAC address, SSID, IPs, and any other host-specific information
before committing fixture files. Do not capture or commit Wi-Fi passwords,
custom weather-service settings, MQTT credentials, API keys, or Ecowitt Cloud
credentials.

Verify these hardware-specific questions:

- Exact internal type/name returned for the user's 7-in-1 array.
- Whether the GW1100 firmware requires pagination or supports an older
  unpaged inventory response.
- Exact active, unused, disabled, and temporarily unavailable ID/status values.
- Whether a configured gateway web password changes LAN API authentication.
- Exact light/solar unit and rain-array shape.
- Timestamp behavior and whether live values can remain unchanged/stale while
  HTTP requests still succeed.
- Counter behavior at the configured rain-day reset boundary.

Run Sensorius for several poll intervals, confirm SQLite rows and dashboard
freshness, disconnect the outdoor sensor temporarily, reboot the gateway, and
confirm recovery without duplicate device creation.

## Documentation And Versioning

Update the canonical documentation as part of implementation:

- `docs/user_guide.md`: gateway preparation, Add Device workflow, polling,
  status, units, and troubleshooting.
- `docs/configuration.md`: `[Ecowitt]` settings and compatibility defaults.
- `docs/architecture.md`: supervised Ecowitt ingestion and data flow.
- `docs/sensors.md`: supported observations, canonical metric names, and units.
- `docs/hardware.md`: GW1100/7-in-1 support and LAN assumptions.
- `docs/homeassistant.md`: new weather entities and normalized units.
- `docs/operations.md`: reachability, DHCP reservation, stale/offline behavior,
  and safe diagnostics.

When code content changes, update `sensorius/__init__.py` exactly as required
by the versioning rule in `AGENTS.md`. At handoff, report what changed, why,
tests and UI/hardware verification performed, unverified hardware behavior,
residual risks, and the new version.

## Acceptance Criteria

The polling MVP is complete when:

1. A user can enter a GW1100 base URL, test it, see its registered sensors, and
   save the integration.
2. Sensorius uses the gateway MAC for stable identity and does not duplicate
   the station after an IP change.
3. Metric and imperial gateway configurations produce the same canonical
   Sensorius metrics and units.
4. Live 7-in-1 weather observations are persisted through `log_readings` and
   appear in dashboard, graph, statistics, forecast, and Home Assistant paths.
5. Cumulative Ecowitt rainfall is never incorrectly summed as interval rain.
6. Missing sensors, gateway timeouts, malformed payloads, restart, and gateway
   reboot do not crash or stall Sensorius.
7. Disabling/removing the integration stops polling without deleting history.
8. Focused regression tests pass, documentation is current, UI behavior is
   verified, and real-hardware limitations are explicitly reported.

## Suggested VS Code Codex Prompt

```text
Read /Users/twfarley/Projects/Sensorius_AI/AGENTS.md and then read
/Users/twfarley/Projects/Sensorius_AI/docs/ecowitt_gateway_implementation.md in
full. Implement the Ecowitt GW1100 polling MVP described there. Inspect the
existing WeeWX, data-logger, settings, Add Device, Home Assistant, forecast,
and supervisor implementations before editing. Keep the change minimal and
backward compatible, update canonical docs and the required version, run the
focused tests and Playwright UI verification, and clearly separate fixture
verification from behavior that still requires the physical gateway. Do not
implement custom-server push, cloud API access, MQTT reconfiguration, or any
gateway write endpoint in this change.
```
