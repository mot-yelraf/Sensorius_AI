"""Supervise Davis Vue and Oregon Scientific USB weather consoles.

Vendor adapters retain bounded acquisition and per-sensor freshness. This service
owns settings, canonical metric conversion and persistence through the hub logger.
"""

import asyncio
import hashlib
import time
from datetime import timezone
from types import SimpleNamespace

from .saiUtils import printDM
from .saiWeatherStation import ensure_station_settings
from .weather_adapters.davis import BAUD_RATES, DavisVueAdapter, validate_port
from .weather_adapters.wmr100 import Wmr100UsbAdapter, normalize_model
from .weather_adapters.observations import ChannelCapability, persist_observation
from .weather_adapters.metrics import derive_outdoor_air_metrics
from .weather_adapters.acurite_calibration import calibrate_acurite_measurements, system_altitude_meters

FAMILIES = {
    "davis": ("DavisVue", "Davis Vue USB Console", "davis_vue", DavisVueAdapter),
    "oregon": ("OregonScientific", "Oregon Scientific USB Console", "wmr100_usb", Wmr100UsbAdapter),
}
FIELD_NAMES = {
    "temperature": "Temperature_F", "humidity": "Rel-Humidity",
    "indoor_temperature": "Console Temperature_F", "indoor_humidity": "Gateway Rel-Humidity",
    "pressure": "Baro-Pressure", "absolute_pressure": "Console Absolute Baro-Pressure",
    "wind_speed": "Wind Speed", "wind_gust": "Wind Gust", "wind_dir": "Wind Direction",
    "uv": "UV Index", "rain_rate": "Rain Rate", "rain_total": "Rain Day",
    "rain_month": "Rain Month", "rain_year": "Rain Year", "rain_event": "Rain Event",
    "rain_increment": "Rain",
}
DISPLAY_METRICS = ("Temperature_F", "Rel-Humidity", "Wind Speed", "Wind Direction", "Rain", "Baro-Pressure")


def normalize_measurements(values):
    """Convert decoder Fahrenheit/inHg values into established Sensorius metrics."""
    result = {FIELD_NAMES[key]: value for key, value in values.items() if key in FIELD_NAMES}
    for source, target in (("temperature", "Temperature"), ("indoor_temperature", "Console Temperature")):
        if source in values:
            result[target] = round((values[source] - 32) / 1.8, 2)
    for source in ("pressure", "absolute_pressure"):
        if source in values:
            result[FIELD_NAMES[source]] = round(values[source] * 33.8638866667, 2)
    derive_outdoor_air_metrics(result)
    return result


class UsbWeatherIngest:
    """Run one independently configured console family under the hub supervisor."""

    def __init__(self, kind, *, settings, data_logger, supervisor=None, transport=None, sensor_manager=None):
        self.kind = kind
        self.section, self.label, self.device, adapter_class = FAMILIES[kind]
        self.settings, self.data_logger, self.supervisor = settings, data_logger, supervisor
        self.sensor_manager = sensor_manager
        setattr(data_logger, f"{kind}_service", self)
        self.adapter = adapter_class(self._configuration(), transport)
        self._lock = asyncio.Lock()
        self._baseline = None
        self._last_write = float("-inf")
        self._sensor_id = self._get("SENSOR_ID", "")
        self._identity = None
        self._error = ""
        self.dashboard_broadcast = None

    def _get(self, key, default):
        return self.settings.get_setting(self.section, key, default, reload_if_changed=True)

    def _configuration(self):
        return SimpleNamespace(
            davis_enabled=bool(self._get("ENABLED", False)),
            davis_port=self._get("PORT", ""), davis_baud_rate=self._get("BAUD_RATE", 19200),
            wmr100_enabled=bool(self._get("ENABLED", False)),
            wmr100_usb_serial=self._get("USB_SERIAL", ""), wmr100_model=self._get("MODEL", "WMR100"),
            timezone=self.settings.get_setting("Time", "TIMEZONE", "UTC"),
        )

    def configure(self, payload):
        """Validate the complete family configuration before writing through settings."""
        if not isinstance(payload, dict):
            raise ValueError("Expected a console configuration object.")
        interval = payload.get("poll_interval_sec", 60)
        if isinstance(interval, bool) or not str(interval).isdigit() or not 60 <= int(interval) <= 3600:
            raise ValueError("Data retrieval interval must be 60–3600 seconds.")
        updates = [(self.section, "POLL_INTERVAL_SEC", int(interval))]
        if self.kind == "davis":
            port = validate_port(payload.get("port", ""))
            if not port:
                raise ValueError("A Davis USB serial port is required.")
            baud = payload.get("baud_rate", 19200)
            if isinstance(baud, bool) or str(baud) not in {str(b) for b in BAUD_RATES}:
                raise ValueError("Unsupported Davis baud rate")
            updates.extend(((self.section, "PORT", port), (self.section, "BAUD_RATE", int(baud))))
        else:
            model = normalize_model(payload.get("model", "WMR100"))
            serial = payload.get("usb_serial", "")
            if not isinstance(serial, str) or len(serial) > 128 or any(ord(c) < 32 for c in serial):
                raise ValueError("Invalid USB serial number.")
            updates.extend(((self.section, "MODEL", model), (self.section, "USB_SERIAL", serial.strip())))
        updates.append((self.section, "ENABLED", True))
        self.settings.set_many_in_memory(updates)
        self.settings.save_settings()
        self.adapter.reset()
        self._baseline, self._last_write, self._identity = None, float("-inf"), None
        self._error = ""

    async def save_configuration(self, payload):
        """Serialize configuration changes with bounded acquisition off the event loop."""
        async with self._lock:
            await asyncio.to_thread(self.configure, payload)

    async def disable_device(self):
        """Disable acquisition and release USB ownership without removing history."""
        async with self._lock:
            await asyncio.to_thread(self.settings.replace_setting, self.section, "ENABLED", False)
            await asyncio.to_thread(self.adapter.reset)
            self._baseline = None

    async def read(self):
        """Acquire live vendor reports in a worker and normalize their storage units."""
        self.adapter.settings = self._configuration()
        await asyncio.to_thread(self.adapter.sample_once)
        observation = self.adapter.read()
        if observation:
            raw = observation.measurements
            observation.measurements = normalize_measurements(raw)
            if self.kind == "davis" and "rain_total" in raw and self.adapter.status()["station"]["reporting"]:
                observation.rain_counter = raw["rain_total"]
                observation.rain_counter_at = observation.observed_at
            if self.kind == "oregon":
                observation.measurements.pop("Rain Day", None)
        return observation

    def capabilities(self):
        """Expose canonical metric names independently of current hardware readings."""
        fields = {FIELD_NAMES[key] for channel in self.adapter.capabilities() for key in channel.metrics if key in FIELD_NAMES}
        if self.kind == "oregon":
            fields.discard("Rain Day")
        fields.update(("Temperature", "Console Temperature", "Humidity", "Ambient VPD"))
        return (ChannelCapability("primary", tuple(sorted(fields))),)

    def status(self):
        """Report safe local health; settings reads never trigger hardware probes."""
        config = self._configuration()
        snapshot = self.adapter.status()
        if vars(config) != vars(self.adapter.settings):
            snapshot.update(state="offline", connected=False, observation_fresh=False, sensors=[])
            snapshot["station"] = {"reporting": False, "battery_status": "UNKNOWN"}
        enabled = bool(self._get("ENABLED", False))
        if not enabled:
            snapshot.update(state="disabled", connected=False, observation_fresh=False, sensors=[])
            snapshot["station"] = {"reporting": False, "battery_status": "UNKNOWN"}
        return {**snapshot, "enabled": enabled, "label": self.label, "sensor_id": self._sensor_id,
                "port": config.davis_port, "baud_rate": config.davis_baud_rate,
                "model": config.wmr100_model, "usb_serial": config.wmr100_usb_serial,
                "poll_interval_sec": self._get("POLL_INTERVAL_SEC", 60),
                "last_error": self._error or snapshot.get("last_error", "")}

    async def poll_once(self):
        """Persist independently identified stations and conservative interval rainfall."""
        async with self._lock:
            observation = await self.read()
            if not self.adapter.status()["station"]["reporting"]:
                self._baseline = None
            if observation is None:
                return False
            interval = max(60, min(3600, float(self._get("POLL_INTERVAL_SEC", 60))))
            if time.monotonic() - self._last_write < interval:
                return False
            sensor_id = self.kind + "-" + hashlib.sha256(observation.source_id.encode()).hexdigest()[:16]
            if self._identity != sensor_id:
                await asyncio.to_thread(ensure_station_settings, sensor_id, device=self.device,
                                        model=self.label, metrics=DISPLAY_METRICS, manager=self.sensor_manager)
                await asyncio.to_thread(self.settings.replace_setting, self.section, "SENSOR_ID", sensor_id)
                self._sensor_id = self._identity = sensor_id
            candidate = None
            if observation.rain_counter is not None:
                counter, stamp = observation.rain_counter, observation.rain_counter_at
                source = (observation.source_id, observation.rain.channel_id)
                if self.kind == "davis":
                    local_tz = getattr(self.data_logger, "local_tz", None) or timezone.utc
                    source += (stamp.astimezone(local_tz).date(),)
                candidate = (source, counter, stamp)
                baseline = self._baseline
                if baseline and baseline[0] == source and stamp > baseline[2]:
                    elapsed = (stamp - baseline[2]).total_seconds()
                    delta = counter - baseline[1]
                    if 0 < elapsed <= interval * 2 and delta >= 0:
                        observation.measurements["Rain"] = round(delta, 3)
                elif baseline and stamp <= baseline[2]:
                    candidate = baseline
            if self.kind == "oregon":
                altitude = system_altitude_meters(self.settings.get_setting("Astral", "ALTITUDE", None))
                observation.measurements = calibrate_acurite_measurements(observation.measurements, {}, altitude)
            metrics = self.capabilities()[0].metrics
            if not await persist_observation(self.data_logger, sensor_id, observation, unavailable_metrics=metrics):
                raise RuntimeError("Weather console readings could not be saved.")
            self._baseline, self._last_write, self._error = candidate, time.monotonic(), ""
            return True

    async def run(self):
        """Retry acquisition under the existing supervisor and release handles on exit."""
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception as exc:
                    if str(exc) != self._error:
                        printDM(f"{self.label}: {exc}", location=__name__)
                    self._error = str(exc)
                if self.supervisor:
                    self.supervisor.feedthedogs(self.label)
                delay = 10 if self.kind == "davis" else 0.05 if self._get("ENABLED", False) and not self.adapter.status().get("last_error") else 2
                await asyncio.sleep(delay)
        finally:
            await asyncio.to_thread(self.adapter.reset)
