"""Supply lightweight system status conditions for Advanced automations.

Disk probes run outside the event loop. Device conditions use existing MQTT
liveness without network requests; elapsed durations require continuous runtime
observations and restart with Sensorius. No system metrics are written as readings.
"""

import asyncio
import math
from pathlib import Path
import re
import shutil
import time


def normalize_system_status_condition(condition: dict) -> dict:
    """Validate and retain only the fields belonging to a system condition."""
    status = str(condition.get("status", "disk_free"))
    if status not in {"disk_free", "db_write_errors", "sensor_offline", "mqtt_disconnected"}:
        raise ValueError("Select a valid System Status condition.")
    result = {"type": "system_status", "status": status}

    def number(key, default, maximum, *, integer=False):
        try:
            value = float(condition.get(key, default))
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"Invalid System Status {key}.") from None
        if not math.isfinite(value) or value <= 0 or value > maximum or (integer and not value.is_integer()):
            raise ValueError(f"Invalid System Status {key}.")
        return int(value) if integer else value

    if status == "disk_free":
        result["threshold_gib"] = number("threshold_gib", 1, 1048576)
    elif status == "db_write_errors":
        result["failures"] = number("failures", 3, 1000000, integer=True)
    else:
        device = str(condition.get("device_id", "")).strip()
        kind = str(condition.get("device_type", "sensor"))
        if device != "__all__" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", device):
            raise ValueError("Select a device for System Status.")
        allowed_kinds = {"sensor", "switch", "all"} if device == "__all__" else {"sensor", "switch"}
        if kind not in allowed_kinds or (status == "sensor_offline" and kind != "sensor"):
            raise ValueError("Sensor offline requires a sensor device.")
        result.update(device_id=device, device_type=kind,
                      duration_min=number("duration_min", 60 if status == "sensor_offline" else 15,
                                          525600, integer=True))
    return result


def system_status_devices(ingest) -> list[dict]:
    """List known MQTT sensors and switches without reading settings or storage."""
    sensors = set(getattr(ingest, "nodus_sensor_hosts", {}) or {})
    switches = set(getattr(ingest, "_known_switch_ids", set()) or set())
    for host, peers in (getattr(ingest, "host_to_peer_ids", {}) or {}).items():
        for peer in peers or []:
            if str(peer).startswith("switch-"):
                switches.add(peer)
            elif peer != host and not str(peer).endswith(".local"):
                sensors.add(peer)
    return [{"id": str(device), "type": kind, "label": str(device)}
            for kind, devices in (("sensor", sensors), ("switch", switches))
            for device in sorted(devices)]


def system_status_groups_unknown(groups: list[dict]) -> bool:
    """Return whether unknown telemetry prevents deciding an AND/OR rule."""
    if any(group["result"] for group in groups):
        return False
    return any(
        any(cond.get("_system_status_unknown") for cond, _ in group["conditions"])
        and not any(not result and not cond.get("_system_status_unknown")
                    for cond, result in group["conditions"])
        for group in groups
    )


class SystemStatusService:
    """Share cached storage probes and observed device outage durations."""

    def __init__(self, *, data_logger, mqtt_ingest=None, supervisor=None, clock=time.monotonic):
        """Bind the active logger and MQTT ingest without opening resources."""
        self.data_logger = data_logger
        self.mqtt_ingest = mqtt_ingest
        self.supervisor = supervisor
        self.clock = clock
        self.disk_free = None
        self.disk_sampled_at = None
        self._outages = {}

    async def refresh_disk(self):
        """Sample free bytes on the active database filesystem in a worker."""
        def probe():
            path = str(getattr(self.data_logger, "db_path", "") or "")
            if not path or path == ":memory:" or path.startswith("file:"):
                return None
            try:
                return shutil.disk_usage(Path(path).expanduser().resolve().parent).free
            except (OSError, ValueError, RuntimeError):
                return None

        self.disk_free = await asyncio.to_thread(probe)
        self.disk_sampled_at = self.clock()

    def _device_fault(self, condition):
        ingest = self.mqtt_ingest
        if ingest is None:
            return None
        device = condition["device_id"]
        snapshot = ingest.get_nodus_liveness(device, device_type=condition["device_type"])
        host = snapshot.get("host")
        if not host:
            return None
        parent = ingest.get_nodus_liveness(host)
        state = parent.get("state")
        ages = [parent.get(name) for name in ("last_report_s", "last_heartbeat_s", "last_seen_s")
                if isinstance(parent.get(name), (int, float))]
        communicating = bool(ages and min(ages) < 3 * float(parent.get("heartbeat_interval_s") or 60))
        if condition["status"] == "mqtt_disconnected":
            return False if communicating else True if state == "offline" else None
        if not communicating:
            return None
        if snapshot.get("availability") == "offline":
            return True
        return False if snapshot.get("state") in {"online", "degraded"} else None

    def evaluate(self, raw_condition: dict) -> tuple[bool | None, str]:
        """Return condition truth and a human-readable report using cached data."""
        try:
            condition = normalize_system_status_condition(raw_condition)
        except ValueError as exc:
            return None, str(exc)
        status = condition["status"]
        now = self.clock()
        if status == "disk_free":
            limit = condition["threshold_gib"]
            if self.disk_free is None or self.disk_sampled_at is None or now - self.disk_sampled_at > 90:
                return None, f"Free disk space unavailable (threshold < {limit:g} GiB)"
            return self.disk_free < limit * 1024**3, f"Free disk space {self.disk_free / 1024**3:.2f} GiB < {limit:g} GiB"
        if status == "db_write_errors":
            health = self.data_logger.database_write_health_snapshot()
            count = health["consecutive_failures"]
            return count >= condition["failures"], (
                f"Database write failures: {count} consecutive; threshold {condition['failures']}"
            )
        if condition["device_id"] == "__all__":
            devices = [device for device in system_status_devices(self.mqtt_ingest)
                       if condition["device_type"] == "all" or device["type"] == condition["device_type"]]
            if not devices:
                return None, "All: no MQTT devices discovered"
            results = [self.evaluate({**condition, "device_id": device["id"], "device_type": device["type"]})
                       for device in devices]
            failures = [report for matched, report in results if matched is True]
            if failures:
                return True, "All (any matching device): " + "; ".join(failures)
            if any(matched is None for matched, _ in results):
                return None, "All: device status pending or unavailable"
            return False, f"All: {len(devices)} MQTT devices currently online"
        key = (status, condition["device_type"], condition["device_id"])
        try:
            fault = self._device_fault(condition)
        except (AttributeError, KeyError, TypeError, ValueError):
            fault = None
        previous = self._outages.pop(key, None)
        label = "Sensor offline" if status == "sensor_offline" else "MQTT disconnected"
        description = f"{label}: {condition['device_id']} > {condition['duration_min']} minutes"
        if fault is None:
            return None, description + "; status unavailable"
        if not fault:
            return False, description + "; currently online"
        started = previous[0] if previous and 0 <= now - previous[1] <= 120 else now
        self._outages[key] = (started, now)
        elapsed = max(0, now - started)
        result = True if elapsed > condition["duration_min"] * 60 else None
        return result, description + f"; observed {elapsed / 60:.1f} minutes"

    async def run(self):
        """Refresh disk metadata every 30 seconds while feeding the supervisor."""
        while True:
            await self.refresh_disk()
            for _ in range(6):
                if self.supervisor:
                    self.supervisor.feedthedogs("System Status")
                await asyncio.sleep(5)
