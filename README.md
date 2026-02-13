# Sensorius

**Environmental Sensing + Automation Hub**

Sensorius is a Raspberry Pi-first sensor and automation hub with a full web UI, MQTT ingestion, and optional Home Assistant integration. It auto-detects local sensors, discovers Nodus devices over MQTT, and turns those signals into live dashboards, historical data, and switch control.

The goal is simple: get visibility, storage, and control with minimal manual setup.

## What It Does

- Auto-detects locally attached sensors (Raspberry Pi deployments)
- Discovers Nodus sensors and switches via MQTT and `/itaot` metadata
- Stores sensor readings and switch events in a local SQLite database
- Provides live dashboards, historical graphing, and location-based views
- Supports manual and automated switch control
- Supports calibration workflows for sensors
- Can publish discovery/state for Home Assistant

## Deployment Modes

- Raspberry Pi: full hub mode with local sensors, GPIO relays, Nodus discovery, and web UI
- macOS/Windows/Linux (non-Pi): hub + MQTT + web UI mode (no local GPIO/direct sensor wiring)

## Quick Start

Choose one setup path:

- Raspberry Pi Bookworm: `setup.sh` or `setup_uv.sh`
- Raspberry Pi Trixie: `setup_trixie.sh` or `setup_trixie_uv.sh`
- macOS: `setup_mac.sh` or `setup_mac_uv.sh`
- Windows: `setup_win.ps1` or `setup_win_uv.ps1`
- Linux (Debian/Ubuntu): `setup_linux.sh`

Then run:

```bash
python3 Sensorius.py
```

Default UI URL: `http://127.0.0.1:8000`

## Architecture (High-Level)

```text
                     +------------------------+        +------------------+
                     |      Sensorius Hub     |<------>| Home Assistant   |
                     |  (FastAPI + MQTT + DB) |        |     (optional)   |
                     +------------------------+        +------------------+
                         ^            ^
                         |            |
                 +-------+            +-------+
                 |                            |
                 v                            v
         +---------------+            +----------------+
         | Nodus Sensor  |            | Nodus Switch   |
         +---------------+            +----------------+
```

## Documentation

- Setup: `docs/setup.md`
- Configuration and `.env`: `docs/configuration.md`
- Sensors and metrics: `docs/sensors.md`
- Hardware and GPIO mapping: `docs/hardware.md`
- Switch automations: `docs/automations.md`

## Product Overview

For a fuller narrative overview of system behavior and design goals, see `ABOUT.md`.

## Attribution

- System Architecture: TW Farley
- Implementation and Coding: TW Farley and ChatGPT/Codex
