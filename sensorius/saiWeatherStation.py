"""Materialize weather devices without replacing operator display choices.

Each console or gateway owns one station ID. Its channels can reuse the existing
settings, SQLite, automation, Home Assistant and dashboard paths.
"""

from collections import OrderedDict
import asyncio
import threading
import time
from .saiSensorSettingsManager import SensorSettingsManager


class StationReportingHealth:
    """Track observed reporting sessions and confirm sustained station outages.

    Reader freshness remains authoritative for measurements. A short retry window
    changes only the displayed health and liveness-event history, never readings.
    Sessions begin with a healthy observation and restart after a confirmed outage
    or a hub process restart; they do not claim to measure console power-on time.
    """

    def __init__(self, data_logger, *, retry_seconds=60):
        """Bind the existing event logger without opening another database."""
        self.data_logger = data_logger
        self.retry_seconds = retry_seconds
        self._lock = threading.RLock()
        self._sensor_id = ""
        self._last_good = None
        self._started = None
        self._online = False

    async def update(self, sensor_id, snapshot):
        """Record confirmed transitions once, outside the acquisition hot path."""
        if not sensor_id:
            return
        event = None
        now = time.monotonic()
        with self._lock:
            if sensor_id != self._sensor_id:
                self._sensor_id = sensor_id
                self._last_good = self._started = None
                self._online = False
            enabled = bool(snapshot.get("enabled"))
            healthy = enabled and bool(snapshot.get("station", {}).get("reporting"))
            if healthy:
                if not self._online:
                    self._started = now
                    event = "online"
                self._last_good = now
                self._online = True
            elif self._online and (not enabled or now - self._last_good >= self.retry_seconds):
                self._online = False
                self._started = None
                event = "offline"
        logger = getattr(self.data_logger, "log_sensor_event", None)
        if event and callable(logger):
            await asyncio.to_thread(logger, sensor_id, "liveness", state=event, source="weather_station")

    def decorate(self, snapshot):
        """Add display health and reporting uptime to a reader's current snapshot."""
        now = time.monotonic()
        with self._lock:
            enabled = bool(snapshot.get("enabled"))
            healthy = enabled and bool(snapshot.get("station", {}).get("reporting"))
            if not enabled:
                state = "disabled"
            elif self._online and self._last_good is not None and (
                healthy or now - self._last_good < self.retry_seconds
            ):
                state = "online" if healthy else "degraded"
            else:
                state = "offline"
            started = self._started if state in {"online", "degraded"} else None
            uptime_start = time.time() - (now - started) if started is not None else None
            empty_label = "Disabled" if not enabled else "Offline" if self._last_good is not None else "Waiting for station data"
        return {
            **snapshot, "state": state, "uptime_started_epoch": uptime_start,
            "uptime_empty_label": empty_label,
        }


def ensure_station_settings(sensor_id, *, device, model, metrics, manager=None):
    """Create station defaults and refresh identity while retaining user settings."""
    manager = manager or SensorSettingsManager("sensor_settings")
    try:
        doc = OrderedDict(manager.load(sensor_id) or {})
    except FileNotFoundError:
        doc = OrderedDict()
    sensor = doc.setdefault("Sensor", OrderedDict())
    sensor.update(TYPE="station", DEVICE=device, SENSOR_ID=sensor_id, STATION_MODEL=model)
    sensor.setdefault("LOCATION", "Weather Station")
    display = doc.setdefault("Display", OrderedDict())
    for index, metric in enumerate(metrics, 1):
        display.setdefault(f"METRIC_{index}", metric)
    display.setdefault(manager.DISPLAY_METRIC_MODE_KEY, manager.DISPLAY_METRIC_MODE_PICK6)
    styles = display.setdefault("Style", OrderedDict())
    for index in range(1, len(metrics) + 1):
        styles.setdefault(f"METRIC_{index}", "Gauge")
    manager.save(sensor_id, doc)


def station_health(data_logger, sensor_id):
    """Resolve adapter health locally without probing a station from a web request."""
    for name in ("acurite_service", "ecowitt_service", "davis_service", "oregon_service"):
        service = getattr(data_logger, name, None)
        if service is not None:
            snapshot = service.status()
            if snapshot.get("sensor_id") == sensor_id:
                health = dict(snapshot.get("station", {"reporting": False, "battery_status": "UNKNOWN"}))
                if name != "ecowitt_service":
                    health.update(
                        usb_connection=snapshot.get("usb_connection", "Console not detected"),
                        usb_connected=bool(snapshot.get("connected")),
                        usb_status="Connected" if snapshot.get("connected") else "Disconnected",
                        station_reception=("Receiving" if health.get("reporting") else "Retrying"
                                           if snapshot.get("state") == "degraded" else "No recent data"),
                        uptime_started_epoch=snapshot.get("uptime_started_epoch"),
                        uptime_empty_label=snapshot.get("uptime_empty_label", "Waiting for station data"),
                    )
                if name == "acurite_service":
                    signal = health.get("signal")
                    health["signal_strength_label"] = f"{signal}/3 (outdoor RF)" if signal is not None else "Unavailable"
                return health
    return {"reporting": False, "battery_status": "UNKNOWN"}
