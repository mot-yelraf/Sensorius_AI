# Sensorius User Guide

Sensorius Automatio Instrumentorum, also called Sensorius AI or Sensorius, is an environmental sensing and automation hub for live dashboards, historical readings, switch control, calibration, and optional integrations with Home Assistant and farmOS. It can run as a full Raspberry Pi hub with directly connected sensors and relays, or as a macOS, Windows, or Linux hub for MQTT-backed Nodus sensors and switches.

This guide is written for day-to-day users of the Sensorius web app. For installation scripts, hardware wiring, and deployment notes, see the setup and hardware documents in this folder.

## Opening Sensorius

During setup, Sensorius may be configured to run as a background service and start automatically when the host starts. If you selected that option during installation, wait for the host to finish booting and then open the web UI.

If you did not configure Sensorius to run as a service, start it manually from the installed Sensorius folder:

```bash
python3 Sensorius.py
```

Then open the web UI:

- On the same computer: `http://127.0.0.1:8000`
- By host name on many local networks: `http://<hostname>.local:8000`
- From another device on the same network: `http://<sensorius-host-ip>:8000`

For service installs, the web UI is normally available after the service starts. For manual runs, keep the terminal process running while you use the app.

<div class="page-break"></div>

## Dashboard Overview

The dashboard is the main operating view. It shows live sensor readings, switch state, device status, location groups, and graph controls.

![Sensorius sensor dashboard](../assets/screenshots/Dashboard%20-%20sensor.png)

Use the dashboard to:

- Check current environmental readings such as temperature, humidity, CO2, VPD, air quality, soil moisture, and pressure.
- See whether a sensor is online, offline, or pending.
- Filter readings by location.
- Open sensor settings and calibration tools.
- Switch between gauge, 6-hour micrograph, and 24-hour micrograph views.
- Open full-screen history graphs for closer review.

Each sensor card is based on the metrics reported by that device. Local Raspberry Pi sensors and discovered Nodus sensors appear together once Sensorius knows their identity and metadata.

<div class="page-break"></div>

### Metric Display Styles

Each dashboard metric can be shown as a gauge, a 6-hour micrograph, or a 24-hour micrograph. Click a metric on the dashboard to rotate through the available display styles for that metric.

Gauge view emphasizes the current reading as instrument panels. The 6-hour micrograph keeps recent movement visible without leaving the main dashboard. The 24-hour micrograph gives more daily context while still keeping the metric compact.

Display style can also be set globally in System Settings or adjusted for an individual sensor in Sensor Setup. Use the global setting when you want a consistent dashboard style, and use Sensor Setup when one sensor needs its own presentation.

<div class="page-break"></div>

## Graphs and History

Sensor cards can show recent history, and the full-screen graph view gives more room for reviewing trends.

![Full-screen graph](../assets/screenshots/Full%20Screen%20Graph.png)

Use full-screen graphs to:

- Compare changes across time.
- Look for environmental drift.
- Review the effect of switch or automation activity.
- Investigate spikes, dropouts, or slow changes.

The graph setup panel controls the graph view and overlays.

![Full-screen graph setup](../assets/screenshots/Full%20Screen%20Graph%20Setup.png)

Switch event overlays can help connect relay activity to sensor changes, such as irrigation events affecting soil moisture or ventilation events affecting temperature and humidity.

<div class="page-break"></div>

## Biodynamic Calendar

When location and timezone settings are available, Sensorius can show sun and moon context in the dashboard.

![Biodynamic calendar](../assets/screenshots/Dashboard%20-%20Biodynamic%20Calendar.png)

This view can include sunrise, solar noon, sunset, moonrise, moonset, moon phase, traditional full moon names, moon position, and illumination details. It is especially useful when automations depend on Astral timing or when environmental patterns follow daylight cycles.

The dashboard also shows a 24-hour weather forecast card next to the biodynamic calendar. The forecast uses the same Astral latitude, longitude, and timezone settings. Click **6 Day Forecast** to open a six-day outlook with daily forecast text, temperature range, wind, and relative humidity range.

<div class="page-break"></div>

## Sensors and Readings

Sensorius supports direct Raspberry Pi sensors and MQTT-discovered Nodus sensors. The available readings depend on the sensor type, but common metrics include:

- Temperature in Celsius and Fahrenheit.
- Relative humidity and absolute humidity.
- CO2 concentration.
- Ambient or plant VPD.
- Dew point and dew point risk indicators.
- Air quality and barometric pressure.
- Soil moisture, soil temperature, soil pH, soil EC, soil moisture deficit, and soil stress index.

Readings are stored in the local SQLite database so the app can show history and full-screen charts. Timestamps are stored in UTC and localized in the UI using the configured timezone.

## Sensor Settings

Open a sensor settings panel from the dashboard when you need to rename, organize, or adjust how a sensor appears.

![Sensor settings](../assets/screenshots/Sensor%20Setup%20-%20Sensor.png)

Typical sensor settings include:

- Display name or label.
- Location assignment.
- Enabled or visible metrics.
- Sensor-level display style, when a sensor should differ from the global dashboard style.
- Sensor-specific configuration provided by the device metadata or local settings.

Keep names and locations clear. Good location labels make the dashboard easier to scan and make automation rules easier to understand later.

## Calibration

Calibration tools help align readings with trusted reference measurements. In the Sensor Setup left menu, Device Calibration is listed before System Calibration.

![Device calibration](../assets/screenshots/Sensor%20Setup%20-%20Device%20Calibration.png)

Device calibration is useful when one physical sensor needs its own correction. Apply calibration changes carefully, then watch the dashboard and graphs to confirm that readings now track the expected values.

![System calibration](../assets/screenshots/Sensor%20Setup%20-%20System%20Calibration.png)

System calibration is useful when you want a shared correction strategy for a class of readings.

<div class="page-break"></div>

## Switches

Switches can be local GPIO relays on a Raspberry Pi or remote Nodus MQTT switches. Sensorius presents both through the same dashboard and automation model.

![Switch dashboard](../assets/screenshots/Dashboard%20-%20switches.png)

Use the switch dashboard to:

- See current switch state.
- Toggle channels manually.
- Review switch location and channel labels.
- Confirm that remote commands are being reflected back through MQTT state updates.

Switch state changes are written to the local database. This makes switch behavior available for history, graph overlays, and automation review.

## Switch Settings

Open switch settings when you need to edit channel labels, location, and switch behavior.

![Switch settings](../assets/screenshots/Switch%20Setup%20-%20Switch%20Settings.png)

Use stable, meaningful channel labels. Sensorius uses switch keys internally in the form `<channel_id>::<label>`, so labels should be descriptive and consistent once automations or external integrations depend on them.

## Automations

Sensorius can automate switches from sensor thresholds, time windows, day schedules, timer cycles, and sunrise or sunset conditions.

![Switch automations](../assets/screenshots/Switch%20Settings%20-%20Automations.png)

Automation rules can include:

- Sensor metric thresholds, such as turning on a fan when temperature rises above a target.
- Hysteresis and minimum interval timing to reduce rapid relay chatter.
- Time-of-day windows and selected days.
- Sunrise or sunset offsets when Astral location settings are available.
- Timer-based active windows for recurring cycles.
- Revert behavior when a rule is no longer true.

After saving an automation, confirm the expected behavior from the switch dashboard. For critical equipment, test with a harmless load first.

<div class="page-break"></div>

## System Settings

System Settings contains hub-level configuration for the Sensorius app, MQTT, display options, timezone, location, integrations, and maintenance tools. The sections below follow the top-down order of the System Settings left menu.

![Sensorius system settings](../assets/screenshots/System%20Setup%20-%20Sensorius.png)

Common settings include:

- HTTP port for the web app.
- Sensorius MQTT broker host and port.
- Timezone.
- Astral latitude and longitude.
- Gauge size and global dashboard display style.
- Integration panels for Home Assistant and farmOS.

Changing the web app port or MQTT broker settings may require a service restart or a page refresh, depending on the setting and deployment mode.

## Home Assistant

Sensorius can publish MQTT discovery and state topics for Home Assistant.

![Home Assistant settings](../assets/screenshots/System%20Setup%20-%20Home%20Asssistant.png)

Use the Home Assistant panel to configure:

- Enabled state.
- MQTT broker host and port.
- Username and password, if your broker requires them.
- Discovery and state publishing behavior from advanced settings.

Expected flow:

1. Configure broker and Home Assistant settings.
2. Start MQTT ingest.
3. Let Sensorius advertise entities.
4. Let Home Assistant observe and control through MQTT topics.

If entities do not appear in Home Assistant, verify that integration is enabled, broker settings are correct, credentials match the broker, and retained discovery publishing is enabled.

## FarmOS

Sensorius can export sensor readings to farmOS as log records.

![FarmOS settings](../assets/screenshots/System%20Setup%20-%20FarmOS.png)

Use the FarmOS panel to configure:

- Enabled state.
- Base farmOS URL.
- TLS verification.
- Access token or username/password authentication.
- Log bundle.

Use the built-in test action before enabling continuous export. If writes fail, check the FarmOS status response for queue depth, token state, and the last error.

## Locations

Locations help organize sensors and switches by room, cabinet, zone, greenhouse, rack, or field area.

![Edit locations](../assets/screenshots/System%20Setup%20-%20Edit%20Locations.png)

Use locations to keep the dashboard readable. A clear location model also makes it easier to build automations and interpret historical graphs.

## Adding Nodus Devices

Use `System Settings > Add Device` to onboard a factory-bootstrapped Nodus device.

![Add device](../assets/screenshots/System%20Setup%20-%20Add%20Device.png)

During onboarding, the Nodus device is operating in AP mode. Sensorius already knows the AP credentials needed to join the Nodus access point, connect to the device, and provision it for the same network that Sensorius is using. Sensorius also sends the broker hostname and tells Nodus to use the `sensorius` profile.

After provisioning, Nodus reboots, joins the configured network, connects back to Sensorius AI through MQTT, and publishes its metadata. Sensorius then uses that metadata to identify the device, subscribe to its topics, place it in the dashboard, and store its readings or switch events.

For Raspberry Pi deployments that onboard Nodus devices over Wi-Fi, use a 2.4 GHz network path. If your router combines 2.4 GHz and 5 GHz under one SSID, connecting the Raspberry Pi by ethernet is usually the most reliable approach because Nodus devices remain on 2.4 GHz Wi-Fi.

## Removing Devices

Use the remove-device workflow when a sensor or switch should no longer appear in the app.

![Remove device](../assets/screenshots/System%20Setup%20-%20Remove%20Device.png)

Before removing a device, confirm that any automations, Home Assistant entities, or farmOS expectations that depend on it have been updated.

## Advanced Settings

Advanced settings are intended for users who understand their deployment and MQTT environment.

![Advanced settings](../assets/screenshots/System%20Setup%20-%20Advance.png)

Use this panel cautiously. Changes to broker behavior, topic publishing, discovery, or low-level runtime settings can affect dashboards, Home Assistant discovery, and device control.

## Good Operating Habits

- Keep sensor, switch, and location names consistent.
- Test switch automations with low-risk equipment before controlling critical loads.
- Watch graphs after calibration changes to confirm the adjustment behaved as expected.
- Keep MQTT broker details stable once Home Assistant or remote devices depend on them.
- Restart the Sensorius service after changes that affect startup configuration, host binding, or broker wiring.
- Back up `sensor_settings/`, `switch_settings/`, `system_settings/`, and the SQLite database before major deployment changes.

## Troubleshooting

If a sensor does not appear:

- Confirm that Sensorius is running and the web UI is reachable.
- For Nodus devices, confirm that the MQTT broker is reachable and the device has published metadata.
- Check that the device is powered and on the expected network.
- Refresh the dashboard after the device publishes metadata.

If switch control does not work:

- Confirm that the switch is online.
- Check that channel labels match the configured switch.
- For Nodus switches, confirm that command and state topics are flowing through MQTT.
- Review automations for rules that may immediately restore or override manual changes.

If Home Assistant does not show entities:

- Confirm Home Assistant integration is enabled.
- Verify broker host, port, username, and password.
- Confirm retained discovery publishing is enabled.
- Restart Home Assistant or reload MQTT entities if needed.

If graphs have gaps:

- Confirm the sensor was online during the missing period.
- Check whether the host or service restarted.
- Verify the local database is writable and retention settings have not pruned older data.

## Related Documentation

- Setup: `docs/setup.md`
- Operations: `docs/operations.md`
- Architecture: `docs/architecture.md`
- Configuration: `docs/configuration.md`
- Sensors and metrics: `docs/sensors.md`
- Switch automations: `docs/automations.md`
- MQTT: `docs/mqtt.md`
- Home Assistant: `docs/homeassistant.md`
- FarmOS: `docs/farmos.md`
- Hardware: `docs/hardware.md`
