# Sensorius

**Environmental Sensing + Automation Hub**

Sensorius is a Raspberry Pi-first sensor and automation hub with a full web UI, MQTT ingestion, and optional Home Assistant and farmOS integrations. It auto-detects local sensors, discovers Nodus devices over MQTT, and turns those signals into live dashboards, historical data, switch control, and optional farmOS telemetry export.

The goal is simple: get visibility, storage, and control with minimal manual setup.

## What It Does

- Auto-detects locally attached sensors (Raspberry Pi deployments)
- Discovers Nodus sensors and switches via MQTT and retained `nodus/<device_id>/meta` metadata (`/itaot-meta` fallback for AP-mode/diagnostics)
- Stores sensor readings and switch events in a local SQLite database
- Provides live dashboards, historical graphing, and location-based views
- Shows Astral-based sun position and moon phase cards in the dashboard when location/timezone are available
- Supports manual and automated switch control
- Supports calibration workflows for sensors
- Can publish discovery/state for Home Assistant
- Can export sensor telemetry to farmOS log entries

## Deployment Modes

- Raspberry Pi: full hub mode with local sensors, GPIO relays, Nodus discovery, and web UI
- macOS/Windows/Linux (non-Pi): hub + MQTT + web UI mode (no local GPIO/direct sensor wiring)

## Quick Start

Choose one setup path:

All shell setup scripts deploy the runtime app under the user's home directory,
for example `/home/<user>/Sensorius` on Linux or `/Users/<user>/Sensorius` on
macOS.

- Raspberry Pi, macOS, Linux: `./install.sh` (auto-select)

Manual Setup
- Raspberry Pi Bookworm: `deploy_scripts/setup_bookworm.sh` / `deploy_scripts/setup_bookwork_uv.sh`
- Raspberry Pi Trixie: `deploy_scripts/setup_trixie.sh` or `deploy_scripts/setup_trixie_uv.sh`
- macOS: `deploy_scripts/setup_mac.sh` or `deploy_scripts/setup_mac_uv.sh`
- Linux (Debian/Ubuntu): `deploy_scripts/setup_linux.sh`
- Windows: `deploy_scripts/setup_win.ps1` or `deploy_scripts/setup_win_uv.ps1`

Then run:

```bash
python3 Sensorius.py
```

Default UI URL: `http://127.0.0.1:8000`

## Security Boundary

Sensorius is intended for a trusted private LAN. The default HTTP bind is
`0.0.0.0`, so permitted devices on that LAN can reach the UI, and the
application does not provide complete login/session protection for every
state-changing route. Do not expose Sensorius HTTP or MQTT ports directly to
the Internet. Use firewall or VLAN controls and a VPN for remote access; set
`SENSORIUS_HTTP_HOST=127.0.0.1` when access should remain on the host. See
`SECURITY.md` for the complete deployment boundary and secret-storage limits.

## Raspberry Pi Wi-Fi Notes

For Raspberry Pi deployments that will onboard Nodus devices over Wi-Fi, best practice is to have the Raspberry Pi use a 2.4 GHz network path.

- Best practice: configure the Raspberry Pi to use a 2.4 GHz Wi-Fi network before running Sensorius setup or onboarding Nodus devices.
- If your router provides separate SSIDs for 2.4 GHz and 5 GHz, connect the Raspberry Pi to the 2.4 GHz SSID.
- If the Raspberry Pi is not headless, you can configure Wi-Fi locally on the Pi before starting Sensorius setup.

Some routers use one SSID for both 2.4 GHz and 5 GHz bands. In that case, a headless Raspberry Pi may join the 5 GHz band, while Nodus devices remain limited to 2.4 GHz.

- On single-SSID multi-frequency routers, the recommended Sensorius setup is to connect the Raspberry Pi to the router by ethernet.
- With ethernet connected, the Raspberry Pi can still communicate with Nodus devices on the router's 2.4 GHz Wi-Fi network.
- If ethernet is not available, configure the Raspberry Pi locally so its Wi-Fi connection uses the router's 2.4 GHz radio before running headless.
- Router-specific band steering, isolation, or roaming behavior is outside the scope of Sensorius setup.

## Architecture (High-Level)

Home Assistant option:

```text
                  +------------------------+      +------------------+
                  |      Sensorius Hub     |<---->| Home Assistant   |
                  |  (FastAPI + MQTT + DB) |      |    (optional)    |
                  +------------------------+      +------------------+
                      ^                 ^
                      |                 |
              +-------+                 +-------+
              |                                 |
              v                                 v
      +---------------+                 +----------------+
      | Nodus Sensor  |                 | Nodus Switch   |
      +---------------+                 +----------------+
```

farmOS export option:

```text
                  +------------------------+      +------------------+
                  |      Sensorius Hub     |----->|      farmOS      |
                  |  (FastAPI + MQTT + DB) |      |    (optional)    |
                  +------------------------+      +------------------+
                      ^                 ^
                      |                 |
              +-------+                 +-------+
              |                                 |
              v                                 v
      +---------------+                 +----------------+
      | Nodus Sensor  |                 | Nodus Switch   |
      +---------------+                 +----------------+
```

## Documentation

- Setup: `docs/setup.md`
- Operations: `docs/operations.md`
- Architecture: `docs/architecture.md`
- Configuration and `.env`: `docs/configuration.md`
- MQTT and Nodus runtime: `docs/mqtt.md`
- Home Assistant integration: `docs/homeassistant.md`
- FarmOS integration: `docs/farmos.md`
- Sensors and metrics: `docs/sensors.md`
- Hardware and GPIO mapping: `docs/hardware.md`
- Switch automations: `docs/automations.md`
- Third-party and binary notices: `THIRD_PARTY_NOTICES.md`

## Product Overview

For a fuller narrative overview of system behavior and design goals, see `ABOUT.md`.

## Attribution

- System Architecture: TW Farley
- Implementation and Coding: TW Farley and ChatGPT/Codex
