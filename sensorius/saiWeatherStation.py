"""Materialize weather devices without replacing operator display choices.

Each console or gateway owns one station ID. Its channels can reuse the existing
settings, SQLite, automation, Home Assistant and dashboard paths.
"""

from collections import OrderedDict
from .saiSensorSettingsManager import SensorSettingsManager


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
    for name in ("acurite_service", "ecowitt_service"):
        service = getattr(data_logger, name, None)
        if service is not None:
            snapshot = service.status()
            if snapshot.get("sensor_id") == sensor_id:
                return snapshot.get("station", {"reporting": False, "battery_status": "UNKNOWN"})
    return {"reporting": False, "battery_status": "UNKNOWN"}
