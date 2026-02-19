# Sensorius

**Environmental Sensing + Automation Hub**

Sensorius is a Raspberry Pi-first sensor and automation hub with a full web UI, MQTT ingestion, and optional Home Assistant and farmOS integrations. It auto-detects local sensors, discovers Nodus devices over MQTT, and turns those signals into live dashboards, historical data, switch control, and optional farmOS telemetry export.

The goal is simple: get visibility, storage, and control with minimal manual setup.

## What It Does

- Auto-detects locally attached sensors (Raspberry Pi deployments)
- Discovers Nodus sensors and switches via MQTT and `/itaot` metadata
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

All shell setup scripts deploy the runtime app to `~/Sensorius`.

- Raspberry Pi, macOS, Linux: `./setup.sh` (auto-select) 

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
- Configuration and `.env`: `docs/configuration.md`
- Home Assistant integration: `docs/homeassistant.md`
- FarmOS integration: `docs/farmos.md`
- Architecture: `docs/architecture.md`
- Sensors and metrics: `docs/sensors.md`
- Hardware and GPIO mapping: `docs/hardware.md`
- Switch automations: `docs/automations.md`

## Product Overview

For a fuller narrative overview of system behavior and design goals, see `ABOUT.md`.

## Attribution

- System Architecture: TW Farley
- Implementation and Coding: TW Farley and ChatGPT/Codex
