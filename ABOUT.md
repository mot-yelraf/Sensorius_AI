# Sensorius

Sensorius is a Raspberry Pi-based sensor and automation hub with a full web UI, MQTT ingestion, and Home Assistant support. It auto-detects local sensors, discovers Nodus devices over MQTT, and turns those signals into a live dashboard, data history, and switch control system. The goal is simple: plug in sensors, power up Nodus devices, and get immediate visibility, storage, and control with minimal manual setup.

## What Sensorius Does

Sensorius combines data collection, discovery, configuration, visualization, and automation in one app:

- Auto-detects locally attached sensors.
- Used for Nodus device bootstrapping via `System Settings > Add Device`
- Discovers Nodus sensors and switches via MQTT and retained `nodus/<device_id>/meta` metadata.
- Stores readings and switch events in the built-in database.
- Provides a live dashboard for all devices and locations.
- Handles sensor calibration workflows (device and system).
- Supports manual and automated switch control.
- Mirrors and/or integrates with Home Assistant.

## Discovery and Metadata

Sensorius discovers Nodus devices by listening to MQTT traffic and consuming retained `nodus/<device_id>/meta` metadata. (`/itaot` remains a legacy/diagnostic fallback.) That metadata drives:

- Device identity (sensor id, type, serial, hostname).
- Sensor location, display metrics, and dashboard grouping.
- Switch definitions and channel labels.
- MQTT topics for state, event, and command channels.

This keeps the dashboard clean and intentional: a device appears once its MQTT metadata is known.

## Dashboard and Views

The web dashboard is the center of operations:

- Three metric container modes per sensor:
  - Gauge view
  - 6-hour graph view
  - 24-hour graph view
- Location filter to focus on a room, zone, or cabinet.
- Online/offline/pending status indicator per sensor.
- Full-screen graphs with optional switch state-change overlays.
- Quick navigation between sensors and locations.

## Sensor Configuration and Calibration

Sensorius supports both local and Nodus sensor configuration:

- Sensor settings can be edited from the dashboard.
- Calibration workflows are available for device and system calibration.
- Display metrics are based on sensor settings and metadata hints.

Calibration state can be surfaced in the UI, and settings are stored in the sensor configuration files so they persist across restarts.

## Switches and Automation

Sensorius supports both local GPIO switches and Nodus remote switches.

Switch configuration includes:

- Switch name and label definitions.
- Per-channel state tracking and history.
- Manual toggle control from the dashboard.
- Location grouping alongside sensors.

Automation controls include:

- Advanced rules with conditions and actions.
- Logic selection (AND/OR).
- Event-based actions tied to sensor measurements or switch states.
- Remote commands routed over MQTT for Nodus switches.

This allows Sensorius to act as a lightweight automation engine even without Home Assistant.

## Data Storage and History

All sensor readings and switch events are stored in the local database.

- Sensor readings are timestamped and queryable.
- Switch events are deduplicated and recorded with source metadata.
- Historical graphs can be viewed in the dashboard or full screen.

## Home Assistant Integration

Sensorius can publish discovery and state to Home Assistant:

- Optional discovery topics for sensors and switches.
- Availability topic publishing for online status.
- Optional passthrough of raw Nodus MQTT topics.

This lets Sensorius work as a standalone hub or as a data/automation source for HA.

## Nodus Devices

Nodus devices are MQTT-native sensor and switch nodes.
Sensorius uses MQTT metadata to learn their layout and topics, then:

- Subscribes to their data and switch topics.
- Mirrors state into the local dashboard and database.
- Enables direct control and automation.

Nodus devices are first-class citizens in the Sensorius UI, appearing and behaving like local hardware once discovered.

## Design Goals

- Minimal setup and maximum automation.
- Local-first operation with optional HA integration.
- Unified dashboard for local and remote devices.
- Clear device identity and location-centric organization.

If you are running both Sensorius and Nodus, this stack is meant to feel like a single cohesive system rather than a collection of scripts.
