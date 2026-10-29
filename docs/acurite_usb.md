# AcuRite USB consoles

In **General Settings > Add Device > AcuRite USB Console**, choose the configured
console family, optionally enter its USB serial, select a retrieval interval
(60–3600 seconds), and choose **Save Console**. Saving enables acquisition even
if the console is currently unplugged. Status reports connection and RF errors;
initial fields can take up to two minutes to arrive. Ecowitt and Nodus devices
continue operating independently. **Disable** retains station settings/history.

Profiles cover 01536-RX/06056, 01025, 01035, 01036/01092/06006, 01525 and
02032/02033/01093/06037 families. Shared USB IDs do not identify the model; select
the actual console. The 01536 profile uses mode 4 automatically. For the other
listed families select **USB MODE / PC CONNECT MODE 4** on the console and recheck
after power loss. Sensorius never changes console modes or reads the archive.
Close PC Connect, WeeWX and any other process holding this console's USB handle.

These devices aggregate outdoor sensors over RF. Live fields include outdoor
temperature/RH, wind speed/direction, indoor temperature and uncorrected Station Pressure.
Alternating reports are sampled every 18 seconds and expire individually after
120 seconds. No live indoor humidity or measured gust is exposed by this adapter.
Unknown pressure calibration is rejected without discarding valid outdoor data.
See the [adapter contract](weather_adapters.md) for rain semantics and extension
instructions. All profiles need physical model/platform validation.

## Sensor calibration and pressure

Open the AcuRite station's settings gear, then **Sensor Calibration**. Set the
outdoor Temperature and Rel-Humidity offsets and choose **Apply Device
Calibration**. Temperature offsets are shown in the selected °C/°F units and
stored as Celsius differences; RH offsets are percentage points, clamped to
0–100% after correction. Indoor console temperature is a separate measurement
and is not adjusted by the outdoor offset.

**System Altitude** is read-only here. Set it in General Settings > Astral.
The ingestion service reads the current altitude and sensor offsets before each
stored snapshot, so edits apply without a service restart. It mirrors valid
system altitude into `[Calibration.Device].ALTITUDE_METERS`; local offsets live
in `TEMP_OFFSET` and `RH_OFFSET` in the same sensor TOML section.

**Station Pressure** is the uncorrected pressure measured at the console's
altitude. Its historical storage key remains `Console Absolute Baro-Pressure`.
**Baro-Pressure** is the altitude-corrected sea-level value, calculated using the
same standard-atmosphere formula as the local pressure sensors. Without a valid
system altitude, Baro-Pressure stays unavailable; Station Pressure remains usable.
New station defaults display Baro-Pressure. Existing untouched default layouts
migrate that pressure slot; custom metric selections remain as configured.

Corrections apply before SQLite writes and listener delivery, so dashboard,
graph, automation, Home Assistant and farmOS consumers receive corrected values.
Fahrenheit, absolute humidity and VPD are recomputed from the corrected outdoor
readings. Previously stored history is retained unchanged; the console itself
receives no calibration commands.

Dashboard station headers shorten Ecowitt and AcuRite identities to the final
six characters, such as `ECOWITT-51804C` and `ACURITE-882C4F`. The full identity
remains the key for TOML settings, database history, API requests and controls.

## Platform setup

Raspberry Pi and other Linux hosts use PyUSB/libusb with USB ID `24c0:0003`.
Updated Debian/Raspberry Pi installers install `libusb-1.0-0` and create
`/etc/udev/rules.d/70-sensorius-acurite.rules`, granting `0660` access to the
installing user's primary group. Native Debian packages use the `sensorius`
service group. The runtime remains unprivileged. Unplug/reconnect the console
after rule installation. Never make all USB devices world-writable.

For an existing Linux source installation, the narrow helper can be run manually
(adjust the absolute installation path):

```sh
sudo sh /home/<user>/Sensorius/deploy_scripts/configure_acurite_usb.sh "$(id -gn)" 1
```

The last argument requests installation of libusb through apt. On a distribution
without apt, install libusb using its package manager and use argument `0`.
The helper requires udev; hosts without it need equivalent device permissions
managed by the operator. Service users must belong to the group in the rule.

macOS and Windows use the platform HID driver through `hidapi`, included in their
updated requirements. Reads run in a worker with a five-second timeout. No driver
replacement, Zadig or elevated runtime is required. Reinstall/update dependencies
with the platform installer before enabling USB on an existing installation.
Native desktop builds include the HID worker and its dedicated entrypoint.

Source installs normally store device configuration in
`/home/<user>/Sensorius/system_settings/<hub_id>/settings.toml` under `[AcuRite]`
on Linux, or `/Users/<user>/Sensorius/system_settings/<hub_id>/settings.toml` on
macOS. Keys are `ENABLED`, `MODEL`, `USB_SERIAL` and `POLL_INTERVAL_SEC`.
Missing sections default to disabled and the 01536 profile. A station ID is
`acurite-<console_identity_hash>`; its sensor TOML lives at
`/home/<user>/Sensorius/sensor_settings/<station_id>/sensor.toml`. USB serial is
preferred for selection; devices without serials use USB-port/HID-path identity,
so moving ports can create a new station. Existing station history is retained.
