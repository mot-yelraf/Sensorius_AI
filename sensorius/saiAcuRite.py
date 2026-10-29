"""Supervise independent AcuRite PC Connect devices through weather adapters.

USB acquisition runs off the event loop. Bounded live reports are normalized
before entering the existing logger; no console archive or mode commands run.
"""

import asyncio
import hashlib
import time
from types import SimpleNamespace

from .saiUtils import printDM
from .saiSensorSettingsManager import SensorSettingsManager
from .weather_adapters.acurite_calibration import calibrate_acurite_measurements, system_altitude_meters
from .saiWeatherStation import ensure_station_settings
from .weather_adapters.acurite import AcuRiteUsbReader, SAMPLE_SECONDS
from .weather_adapters.acurite_models import CONSOLE_PROFILES, console_profile
from .weather_adapters.observations import Observation, ChannelCapability, persist_observation

TASK_NAME = "AcuRite USB Ingest"
DISPLAY_METRICS = (
    "Temperature_F", "Rel-Humidity", "Wind Speed", "Wind Direction",
    "Rain", "Baro-Pressure",
)


class AcuRiteUsbIngest:
    """Own a configured console's adapter, settings and successful rain baseline."""

    def __init__(self, *, settings, data_logger, supervisor=None, transport=None, sensor_manager=None):
        self.settings, self.data_logger, self.supervisor = settings, data_logger, supervisor
        self.data_logger.acurite_service = self
        self.sensor_manager = sensor_manager
        self.adapter = AcuRiteUsbReader(self._configuration(), transport)
        self._baseline = None
        self._last_write = float("-inf")
        self._sensor_id = ""
        self._settings_identity = None
        self._error = ""
        self.dashboard_broadcast = None
        self._configuration_lock = asyncio.Lock()

    def _configuration(self):
        get = lambda key, default: self.settings.get_setting("AcuRite", key, default, reload_if_changed=True)
        return SimpleNamespace(
            acurite_enabled=bool(get("ENABLED", False)),
            acurite_usb_serial=str(get("USB_SERIAL", "") or "").strip(),
            acurite_model=console_profile(get("MODEL", "01536")).key,
            timezone="UTC",
        )

    def configure(self, payload):
        """Validate all input before saving and enable acquisition last."""
        if not isinstance(payload, dict):
            raise ValueError("Expected an AcuRite configuration object.")
        profile = console_profile(payload.get("model", "01536"))
        serial = payload.get("usb_serial", "")
        if not isinstance(serial, str) or len(serial) > 128 or any(ord(c) < 32 for c in serial):
            raise ValueError("Invalid USB serial number.")
        interval = payload.get("poll_interval_sec", 60)
        if isinstance(interval, bool):
            raise ValueError("Data retrieval interval must be 60–3600 seconds.")
        try:
            interval = int(interval)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Data retrieval interval must be 60–3600 seconds.") from exc
        if not 60 <= interval <= 3600:
            raise ValueError("Data retrieval interval must be 60–3600 seconds.")
        self.settings.set_many_in_memory([
            ("AcuRite", "MODEL", profile.key),
            ("AcuRite", "USB_SERIAL", serial.strip()),
            ("AcuRite", "POLL_INTERVAL_SEC", interval),
            ("AcuRite", "ENABLED", True),
        ])
        self.settings.save_settings()
        self._baseline = None
        self._last_write = float("-inf")

    def disable(self):
        """Stop acquisition while retaining station settings and historical readings."""
        self.settings.replace_setting("AcuRite", "ENABLED", False)
        self._baseline = None

    async def save_configuration(self, payload) -> None:
        """Serialize device setup with acquisition and keep disk writes off the loop."""
        async with self._configuration_lock:
            await asyncio.to_thread(self.configure, payload)

    async def disable_device(self) -> None:
        """Apply disable after any bounded in-flight acquisition finishes."""
        async with self._configuration_lock:
            await asyncio.to_thread(self.disable)

    async def read(self) -> Observation | None:
        """Sample bounded USB reports and return a fresh normalized observation."""
        config = self._configuration()
        previous = self.adapter.settings
        if (previous.acurite_enabled, previous.acurite_usb_serial, previous.acurite_model) != (
            config.acurite_enabled, config.acurite_usb_serial, config.acurite_model
        ):
            self.adapter = AcuRiteUsbReader(config, self.adapter.transport)
            self._baseline = None
            self._last_write = float("-inf")
        self.adapter.settings = config
        await asyncio.to_thread(self.adapter.sample_once)
        return self.adapter.read()

    def capabilities(self) -> tuple[ChannelCapability, ...]:
        """Describe supported live metrics without querying USB."""
        return tuple(ChannelCapability(channel.channel_id, channel.metrics + ("Baro-Pressure",), channel.primary)
                     for channel in self.adapter.capabilities())

    def status(self) -> dict:
        """Expose safe local connectivity and sensor freshness diagnostics."""
        config = self._configuration()
        snapshot = self.adapter.status()
        enabled = config.acurite_enabled
        profile = console_profile(config.acurite_model)
        previous = self.adapter.settings
        changed = (previous.acurite_enabled, previous.acurite_usb_serial, previous.acurite_model) != (
            enabled, config.acurite_usb_serial, config.acurite_model
        )
        if changed:
            snapshot.update(state="offline" if enabled else "disabled", connected=False, observation_fresh=False, sensors=[])
            snapshot["station"] = {"reporting": False, "battery_status": "UNKNOWN"}
        snapshot.update(label=f"AcuRite {profile.label} USB", configured_model=profile.key,
                        requires_mode4_selection=profile.requires_mode4_selection, usb_mode_help=profile.mode_help)
        if not enabled:
            snapshot.update(state="disabled", connected=False, observation_fresh=False)
            snapshot["station"] = {"reporting": False, "battery_status": "UNKNOWN"}
        snapshot.update(
            enabled=enabled, model=config.acurite_model, usb_serial=config.acurite_usb_serial,
            sensor_id=self._sensor_id, last_error=self._error or snapshot["last_error"],
            poll_interval_sec=self.settings.get_setting("AcuRite", "POLL_INTERVAL_SEC", 60),
            profiles=[{"key": p.key, "label": p.label, "help": p.mode_help} for p in CONSOLE_PROFILES],
        )
        return snapshot

    def _calibrate_observation(self, sensor_id: str, observation: Observation) -> None:
        """Reload offsets and mirror system altitude with writes only when values change."""
        if self.sensor_manager is None:
            self.sensor_manager = SensorSettingsManager("sensor_settings")
        doc = self.sensor_manager.load(sensor_id)
        device = doc.setdefault("Calibration", {}).setdefault("Device", {})
        changed = False
        for key in ("TEMP_OFFSET", "RH_OFFSET"):
            if key not in device:
                device[key] = 0.0
                changed = True
        altitude = system_altitude_meters(self.settings.get_setting(
            "Astral", "ALTITUDE", None, reload_if_changed=True))
        if altitude is not None and device.get("ALTITUDE_METERS") != altitude:
            device["ALTITUDE_METERS"] = altitude
            changed = True
        display = doc.get("Display", {})
        previous_defaults = (*DISPLAY_METRICS[:-1], "Console Absolute Baro-Pressure")
        if tuple(display.get(f"METRIC_{index}") for index in range(1, 7)) == previous_defaults:
            display["METRIC_6"] = "Baro-Pressure"
            changed = True
        values = calibrate_acurite_measurements(observation.measurements, device, altitude)
        if changed:
            self.sensor_manager.save(sensor_id, doc)
        observation.measurements = values

    async def poll_once(self) -> bool:
        """Persist fresh samples and commit rain provenance only after a successful write."""
        async with self._configuration_lock:
            observation = await self.read()
            if not self.adapter.status()["station"]["reporting"]:
                self._baseline = None
            if observation is None:
                return False
            interval = max(60, min(3600, float(self.settings.get_setting("AcuRite", "POLL_INTERVAL_SEC", 60))))
            if time.monotonic() - self._last_write < interval:
                return False
            identity = self.adapter.device_identity
            sensor_id = "acurite-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            profile = console_profile(self.adapter.settings.acurite_model)
            settings_identity = (sensor_id, profile.key)
            if self._settings_identity != settings_identity:
                await asyncio.to_thread(
                    ensure_station_settings, sensor_id, device="acurite_usb",
                    model=profile.label,
                    metrics=DISPLAY_METRICS, manager=self.sensor_manager,
                )
                self._sensor_id = sensor_id
                self._settings_identity = settings_identity
            baseline = self._baseline
            counter = observation.rain_counter
            stamp = observation.rain_counter_at
            candidate = None
            if counter is not None:
                candidate = (observation.source_id, counter, stamp)
                if baseline and baseline[0] == observation.source_id and stamp > baseline[2]:
                    elapsed = (stamp - baseline[2]).total_seconds()
                    if elapsed <= max(120, interval * 2):
                        delta = counter - baseline[1]
                        if delta < 0 and baseline[1] > 80 and counter < 2:
                            delta += observation.rain.modulus
                        if delta >= 0:
                            observation.measurements["Rain"] = round(delta, 3)
                elif baseline and stamp <= baseline[2]:
                    candidate = baseline
            await asyncio.to_thread(self._calibrate_observation, sensor_id, observation)
            metrics = tuple(metric for channel in self.capabilities() for metric in channel.metrics)
            if not await persist_observation(self.data_logger, sensor_id, observation, unavailable_metrics=metrics):
                raise RuntimeError("AcuRite readings could not be saved to the database.")
            self._baseline = candidate
            self._last_write = time.monotonic()
            self._error = ""
            return True

    async def run(self) -> None:
        """Retry USB acquisition under the existing watchdog and supervisor."""
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                message = str(exc)
                if message != self._error:
                    printDM(f"AcuRite: {message}", location=__name__)
                self._error = message
            for delay in (9, SAMPLE_SECONDS - 9):
                if self.supervisor:
                    self.supervisor.feedthedogs(TASK_NAME)
                await asyncio.sleep(delay)
