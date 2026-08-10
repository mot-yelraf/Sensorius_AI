# Documentation Index

These are the canonical operating documents for Sensorius.

- User guide: `docs/user_guide.md`
- Setup and installation: `docs/setup.md`
- Operations: `docs/operations.md`
- System architecture: `docs/architecture.md`
- Integrated Biodynamic Calendar and companion migration: `docs/biodynamic_calendar_companion.md`
- Configuration: `docs/configuration.md`
- MQTT and Nodus runtime contract overview: `docs/mqtt.md`
- Home Assistant integration: `docs/homeassistant.md`
- farmOS integration: `docs/farmos.md`
- Sensors and metrics: `docs/sensors.md`
- Hardware and GPIO: `docs/hardware.md`
- Switch automations: `docs/automations.md`
- Security policy and deployment boundary: `SECURITY.md`
- Third-party and binary notices: `THIRD_PARTY_NOTICES.md`

Protocol and migration contract documents:

- Current Sensorius/Nodus contract: `docs/sensorius_contract.md`
- Archived Nodus contract copy: `docs/nodus_contract.md` (the canonical
  Sensorius/Nodus contract always takes precedence)
- Calibration MQTT contract: `docs/calibration_mqtt_contract.md`
- Onboarding V2 requirements: `docs/onboarding_v2_sensorius_requirements.md`
- Nodus onboarding V2 handoff: `docs/onboarding_v2_nodus_handoff_no_settings_schema.md`
- HTTP health-polling migration note: `docs/sensorius_migration_off_hayd_itaot.md`

Implementation plans and handoffs:

- Ecowitt GW1100 implementation handoff: `docs/ecowitt_gateway_implementation.md`

Archived implementation notes are kept for traceability and are not the source
of truth when they conflict with the canonical docs above.

## Rebuild The User Guide PDF

Install the repository's documentation tooling, then render the canonical
Markdown guide:

```bash
npm install
npm run docs:pdf
```

The build writes `docs/user_guide.pdf` atomically from `docs/user_guide.md` and
uses the screenshots under `assets/screenshots/`.
