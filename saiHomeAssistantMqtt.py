"""saiHomeAssistantMqtt.py
    HomeAssistantTopMap and rPiHomeAssistantBridge classes for integrating Sensorius AI 
    with the Home Assistant (HA) dot io (open source project).

    Sensorius's mqtt client sends each individual sensor/switch metrics data as a single topic per sensor to the mqtt broker.
    HA requires the each sensor metric be 'discovered' using a single topic per metric; thus the HA bridge.
"""
    
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from saiUtils import printDM, debug_enabled
import re
from dataclasses import dataclass

MODULE = "saiHomeAssistantMqtt"
DEBUG = debug_enabled(MODULE)

# module helpers
def slugify(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "_")

@dataclass(frozen=True)
class HomeAssistantTopicMap:
    node_id: str
    base_topic: str = "sensorius"
    discovery_prefix: str = "homeassistant"

    # ---------- sensors ----------
    def sensor_state_topic(self, sensor_id: str) -> str:
        return f"{self.base_topic}/sensor/{sensor_id}/state"

    def sensor_command_topic(self, sensor_id: str) -> str:
        return f"{self.base_topic}/sensor/{sensor_id}/set"

    def sensor_availability_topic(self, sensor_id: str) -> str:
        return f"{self.base_topic}/sensor/{sensor_id}/availability"

    def sensor_discovery_topic(self, object_id: str) -> str:
        return f"{self.discovery_prefix}/sensor/{self.node_id}/{object_id}/config"

    # ---------- switches ----------
    def switch_state_topic(self, switch_id: str, channel_id: str) -> str:
        return f"{self.base_topic}/switch/{switch_id}/{channel_id}/state"

    def switch_command_topic(self, switch_id: str, channel_id: str) -> str:
        return f"{self.base_topic}/switch/{switch_id}/{channel_id}/set"

    def switch_availability_topic(self, switch_id: str) -> str:
        return f"{self.base_topic}/switch/{switch_id}/availability"

    def switch_discovery_topic(self, object_id: str) -> str:
        return f"{self.discovery_prefix}/switch/{self.node_id}/{object_id}/config"

class rPiHomeAssistantBridge:
    def __init__(
        self,
        *,
        mqtt_clients,
        settings,
        topic_map: HomeAssistantTopicMap,
        switch_controllers,
        data_logger,   # <-- use DB-backed truth
    ):
        self.mqtt_clients = mqtt_clients
        self.settings = settings
        self.topic_map = topic_map
        self.switch_controllers = switch_controllers
        self.data_logger = data_logger

        self.enabled = bool(self.settings.get_setting("HomeAssistant", "ENABLED", False))
        self.discovery_retain = bool(self.settings.get_setting("HomeAssistant", "PUBLISH_DISCOVERY_RETAIN", True))
        self.state_retain = bool(self.settings.get_setting("HomeAssistant", "PUBLISH_STATE_RETAIN", True))
        self.qos = 0
        self._channel_index = self._build_channel_index()
        self._db_listeners_installed = False

        self._asyncio_loop: asyncio.AbstractEventLoop | None = None
        self._ha_discovered_sensor_metrics: set[str] = set()


    # ------- init helpers --------
    def _build_channel_index(self) -> dict[str, tuple[Any, str, str]]:
        """
        channel_id -> (controller, label, switch_id)
        channel_id is SWITCH_#_ID (truth).
        """
        index: dict[str, tuple[Any, str, str]] = {}
        for ctrl in self._iter_all_switch_controllers():
            switch_id = (getattr(ctrl, "switch_id", "") or "").strip()
            mapping = getattr(ctrl, "channel_id_for_label", {}) or {}
            for label, channel_id in mapping.items():
                channel_id = (channel_id or "").strip()
                if not channel_id:
                    continue
                # if collisions ever occur, last one wins; you can log/debug later
                index[channel_id] = (ctrl, label, switch_id)
        return index
    
    # ---------------------------------------------------------------------
    # Discovery publishing
    # ---------------------------------------------------------------------

    async def publish_all_discovery(self) -> None:
        """
        Publishes retained HA discovery configs for:
          - all sensors/metrics found in readings table
          - all switch channels found in switch_ids table
        """
        if not self.enabled:
            return
        from collections import defaultdict
        from saiSensorSettingsManager import SensorSettingsManager

        if DEBUG:
            printDM("Publishing HA discovery from DataLogger", location=MODULE)

        sensor_mgr = SensorSettingsManager("sensor_settings")

        # ---- sensors from readings ----
        sensor_ids = set(self.data_logger.get_available_sensors() or [])
        try:
            sensor_ids.update(sensor_mgr.list_ids() or [])
        except Exception:
            pass
        for sensor_id in sensor_ids:
            display_metrics = []
            try:
                display_metrics = sensor_mgr.get_display_metrics(sensor_id)
            except Exception:
                display_metrics = []
            metrics = display_metrics or (self.data_logger.get_available_metrics(sensor_id) or [])
            if not metrics:
                continue

            # These can be enhanced later by joining sensor settings/location tables;
            # for now, name defaults to sensor_id and location left blank.
            sensor_name = sensor_id
            location = None
            try:
                sensor_name = sensor_mgr.get_setting(sensor_id, "Sensor.SENSOR_ID", sensor_id) or sensor_id
                location = sensor_mgr.get_setting(sensor_id, "Sensor.LOCATION", None)
            except Exception:
                pass

            for metric_name in metrics:
                metric_key = f"{sensor_id}::{slugify(metric_name)}"
                if metric_key in self._ha_discovered_sensor_metrics:
                    continue
                discovery_topic, payload = build_sensor_metric_discovery_payload(
                    topic_map=self.topic_map,
                    sensor_id=sensor_id,
                    sensor_name=sensor_name,
                    metric_name=metric_name,
                    location=location,
                )
                ok = self.mqtt_clients.publish_json(discovery_topic, payload, qos=self.qos, retain=self.discovery_retain)
                if DEBUG and not ok:
                    printDM(f"HA sensor discovery publish failed: {discovery_topic}", location=MODULE)
                if ok:
                    self._ha_discovered_sensor_metrics.add(metric_key)

            # availability online (retained)
            avail_topic = self.topic_map.sensor_availability_topic(sensor_id)
            self.mqtt_clients.publish_text(avail_topic, "online", qos=self.qos, retain=True)
            # publish an initial retained state snapshot so HA doesn't sit at "unknown"
            try:
                latest = self.data_logger.get_latest_readings(sensor_id)  # you may need to implement/adjust this
                # expected: {"ts": <epoch or iso>, "values": {metric: value, ...}} OR similar
                payload = latest.get("values", {}) if isinstance(latest, dict) else {}
                ts_epoch = (latest.get("ts") if isinstance(latest, dict) else None) or time.time()
                ha_payload = build_sensor_state_payload(ts_epoch=float(ts_epoch), values_by_metric_name=payload)
                await self.publish_sensor_state(sensor_id, ha_payload)
            except Exception as e:
                if DEBUG:
                    printDM(f"HA initial state publish skipped for {sensor_id}: {e}", location=MODULE)

        # ---- switches from switch_ids ----
        identities = self.data_logger.get_switch_identities() or []

        # group by switch_id for device-level config cleanliness
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in identities:
            grouped[row.get("switch_id", "")].append(row)

        for switch_id, rows in grouped.items():
            if not switch_id:
                continue
            # pick a stable device_name/location; labels vary per channel
            device_name = switch_id
            location = rows[0].get("location") if rows else None

            for r in rows:
                channel_id = (r.get("channel_id") or "").strip()
                label = r.get("label") or channel_id
                if not channel_id:
                    continue

                discovery_topic, payload = build_switch_channel_discovery_payload(
                    topic_map=self.topic_map,
                    switch_id=switch_id,
                    channel_id=channel_id,
                    channel_label=label,
                    device_name=device_name,
                    location=location,
                )
                ok = self.mqtt_clients.publish_json(discovery_topic, payload, qos=self.qos, retain=self.discovery_retain)
                if DEBUG and not ok:
                    printDM(f"HA switch discovery publish failed: {discovery_topic}", location=MODULE)

            # availability online (retained)
            avail_topic = self.topic_map.switch_availability_topic(switch_id)
            self.mqtt_clients.publish_text(avail_topic, "online", qos=self.qos, retain=True)
            # publish retained initial switch states so HA doesn't remain 'unknown'
            await self.publish_initial_switch_states()

        if DEBUG:
            printDM("HA discovery publish complete", location=MODULE)

    # ---------------------------------------------------------------------
    # State publishing
    # ---------------------------------------------------------------------

    async def publish_sensor_state(self, sensor_id: str, payload_dict: dict) -> None:
        if not self.enabled:
            return
        topic = self.topic_map.sensor_state_topic(sensor_id)
        ok = self.mqtt_clients.publish_json(topic, payload_dict, qos=self.qos, retain=self.state_retain)
        if DEBUG and not ok:
            printDM(f"HA sensor state publish failed: {topic}", location=MODULE)

    def publish_sensor_availability(self, sensor_id: str, status: str) -> None:
        if not self.enabled:
            return
        topic = self.topic_map.sensor_availability_topic(sensor_id)
        self.mqtt_clients.publish_text(topic, status, qos=self.qos, retain=True)

    async def publish_switch_state(self, switch_id: str, channel_id: str, is_on: bool) -> None:
        if not self.enabled:
            return
        topic = self.topic_map.switch_state_topic(switch_id, channel_id)
        payload = "ON" if is_on else "OFF"
        ok = self.mqtt_clients.publish_text(topic, payload, qos=self.qos, retain=True)
        if DEBUG and not ok:
            printDM(f"HA switch state publish failed: {topic}", location=MODULE)

    def publish_switch_availability(self, switch_id: str, status: str) -> None:
        if not self.enabled:
            return
        topic = self.topic_map.switch_availability_topic(switch_id)
        self.mqtt_clients.publish_text(topic, status, qos=self.qos, retain=True)

    # ---------------------------------------------------------------------
    # Command handlers (HA -> Sensorius)
    # ---------------------------------------------------------------------

    def install_command_handlers(self) -> None:
        if not self.enabled:
            return

        try:
            self._asyncio_loop = asyncio.get_running_loop()
        except Exception:
            self._asyncio_loop = None

        self._install_db_listeners()

        cmd_filter = self.topic_map.switch_command_topic("+", "+")  # "<base>/switch/+/+/set"
        self.mqtt_clients.subscribe(cmd_filter, self._on_switch_command_message, qos=self.qos)
        self.mqtt_clients.subscribe("homeassistant/status", self._on_ha_status_message, qos=self.qos)

        if DEBUG:
            printDM(f"Installed HA handlers: {cmd_filter}, homeassistant/status", location=MODULE)

    def _install_db_listeners(self) -> None:
        if self._db_listeners_installed:
            return
        self._db_listeners_installed = True
        try:
            self.data_logger.add_readings_listener(self._on_db_readings_written)
        except Exception:
            pass
        try:
            self.data_logger.add_switch_event_listener(self._on_db_switch_event)
        except Exception:
            pass

    def _on_db_readings_written(self, sensor_id: str, timestamp_iso: str, values: dict) -> None:
        if not self.enabled:
            return
        values = values or {}
        metrics = self._filter_metrics_for_sensor(sensor_id, values)
        if not metrics:
            return
        try:
            self._ensure_sensor_discovery(sensor_id, list(metrics.keys()))
        except Exception:
            pass

        ts_epoch = _epoch_from_iso(timestamp_iso)
        payload = build_sensor_state_payload(ts_epoch=ts_epoch, values_by_metric_name=metrics)
        self.publish_sensor_availability(sensor_id, "online")
        self._schedule_async(self.publish_sensor_state(sensor_id, payload))

    def _on_db_switch_event(
        self,
        switch_key: str,
        is_on: bool,
        timestamp_iso: str,
        source: str | None,
        sensor_id: str | None,
    ) -> None:
        if not self.enabled:
            return
        if "::" not in (switch_key or ""):
            return
        switch_id, channel_id = switch_key.split("::", 1)
        switch_id = (switch_id or "").strip()
        channel_id = (channel_id or "").strip()
        if not switch_id or not channel_id:
            return
        self.publish_switch_availability(switch_id, "online")
        self._schedule_async(self.publish_switch_state(switch_id, channel_id, bool(is_on)))

    def _filter_metrics_for_sensor(self, sensor_id: str, values: dict) -> dict:
        try:
            from saiSensorSettingsManager import SensorSettingsManager
            sensor_mgr = SensorSettingsManager("sensor_settings")
            display_metrics = sensor_mgr.get_display_metrics(sensor_id) or []
        except Exception:
            display_metrics = []
        if display_metrics:
            return {k: values[k] for k in display_metrics if k in values}
        return dict(values)

    def _ensure_sensor_discovery(self, sensor_id: str, metrics: list[str]) -> None:
        if not self.enabled or not metrics:
            return
        try:
            from saiSensorSettingsManager import SensorSettingsManager
            sensor_mgr = SensorSettingsManager("sensor_settings")
            sensor_name = sensor_mgr.get_setting(sensor_id, "Sensor.SENSOR_ID", sensor_id) or sensor_id
            location = sensor_mgr.get_setting(sensor_id, "Sensor.LOCATION", None)
        except Exception:
            sensor_name = sensor_id
            location = None

        for metric_name in metrics:
            metric_key = f"{sensor_id}::{slugify(metric_name)}"
            if metric_key in self._ha_discovered_sensor_metrics:
                continue
            discovery_topic, payload = build_sensor_metric_discovery_payload(
                topic_map=self.topic_map,
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                metric_name=metric_name,
                location=location,
            )
            ok = self.mqtt_clients.publish_json(discovery_topic, payload, qos=self.qos, retain=self.discovery_retain)
            if DEBUG and not ok:
                printDM(f"HA sensor discovery publish failed: {discovery_topic}", location=MODULE)
            if ok:
                self._ha_discovered_sensor_metrics.add(metric_key)

    def _schedule_async(self, coro) -> None:
        loop = self._asyncio_loop
        if loop:
            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(coro))
            except Exception:
                pass

    async def publish_initial_switch_states(self) -> None:
        """
        Publish a retained initial state snapshot for all switch channels
        so HA entities do not remain 'unknown'.

        Source of truth preference:
        1) DB last-known state (sw_events or a switch_state table)
        2) Live controller state (if exposed)
        """
        if not self.enabled:
            return

        identities = self.data_logger.get_switch_identities() or []
        if not identities:
            return

        # If your DataLogger can return last-known states in bulk, use it.
        # Otherwise we query per-channel below.
        for row in identities:
            switch_id = (row.get("switch_id") or "").strip()
            channel_id = (row.get("channel_id") or "").strip()
            if not switch_id or not channel_id:
                continue

            try:
                # ---- Option A: DB-backed last state (recommended) ----
                # Implement/adjust this call to your DataLogger. Expected to return
                # something like {"is_on": True, "ts": 1234567890} or just True/False.
                last_state = None
                try:
                    last_state = self.data_logger.get_last_switch_state(switch_id, channel_id)
                except Exception:
                    last_state = None

                is_on = None
                if isinstance(last_state, dict):
                    is_on = normalize_switch_state(last_state.get("is_on"))
                else:
                    is_on = normalize_switch_state(last_state)

                # ---- Option B: Live controller fallback ----
                if is_on is None:
                    item = self._channel_index.get(channel_id)
                    if item:
                        ctrl, label, _resolved_switch_id = item
                        # if your controller exposes a state getter, use it.
                        # Common patterns: ctrl.get_state(label), ctrl.get_channel_state(label), etc.
                        if hasattr(ctrl, "get_state"):
                            is_on = normalize_switch_state(ctrl.get_state(label))

                # If still unknown, skip publishing (or choose a default).
                if is_on is None:
                    if DEBUG:
                        printDM(
                            f"Initial HA switch state unknown for {switch_id}::{channel_id}; not publishing",
                            location=MODULE,
                        )
                    continue

                await self.publish_switch_state(switch_id, channel_id, bool(is_on))

                if DEBUG:
                    printDM(
                        f"Published initial HA switch state {switch_id}::{channel_id} -> {'ON' if is_on else 'OFF'}",
                        location=MODULE,
                    )

            except Exception as e:
                if DEBUG:
                    printDM(
                        f"Initial HA switch state publish failed for {switch_id}::{channel_id}: {e}",
                        location=MODULE,
                    )

    def _on_ha_status_message(self, client, userdata, msg) -> None:
        try:
            payload = (msg.payload or b"").decode(errors="ignore").strip().lower()
            if payload != "online":
                return
            loop = self._asyncio_loop
            if loop:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self.publish_all_discovery()))
        except Exception:
            return

    def _on_switch_command_message(self, client, userdata, msg) -> None:
        """
        Topic: <base>/switch/<switch_id>/<channel_id>/set
        Payload: "ON"/"OFF" or truthy variants
        """
        try:
            topic = (msg.topic or "").strip()
            payload = (msg.payload or b"").decode(errors="ignore").strip()
            parts = topic.split("/")
            if len(parts) < 5:
                return

            if parts[0] != self.topic_map.base_topic or parts[1] != "switch":
                return

            switch_id = parts[2]
            channel_id = parts[3]
            desired_on = payload.lower() in ("on", "1", "true", "yes")

            loop = self._asyncio_loop
            if loop:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_switch_command_async(switch_id, channel_id, desired_on))
                )

        except Exception as e:
            printDM(f"HA switch command parse error: {e}", location=MODULE)

    async def _handle_switch_command_async(self, switch_id: str, channel_id: str, desired_on: bool) -> None:
        item = self._channel_index.get((channel_id or "").strip())
        if not item:
            printDM(f"Unknown HA channel_id={channel_id} for switch_id={switch_id}", location=MODULE)
            return

        ctrl, label, resolved_switch_id = item

        # optional sanity: if HA used a switch_id that doesn't match, just log
        if switch_id and resolved_switch_id and switch_id != resolved_switch_id and DEBUG:
            printDM(f"HA switch_id mismatch: got={switch_id}, resolved={resolved_switch_id} for channel_id={channel_id}", location=MODULE)

        ctrl.set_state(label, desired_on, force=True)
        await self.publish_switch_state(resolved_switch_id or switch_id, channel_id, desired_on)

    # ---------------------------------------------------------------------
    # Inventory adapters (update if your registries differ)
    # ---------------------------------------------------------------------
    """
    def _iter_sensors(self):

        if isinstance(self.sensor_registry, dict):
            for sensor_id, info in self.sensor_registry.items():
                yield sensor_id, info
            return

        # If your registry is a list of objects/dicts, handle that here
        try:
            for item in self.sensor_registry:
                if isinstance(item, dict):
                    sensor_id = item.get("sensor_id") or item.get("id") or ""
                    if sensor_id:
                        yield sensor_id, item
        except Exception:
            return
    """

    def _iter_all_switch_controllers(self):
        sc = self.switch_controllers
        if not sc:
            return
        if isinstance(sc, dict):
            for _, ctrl in sc.items():
                if ctrl:
                    yield ctrl
        else:
            for ctrl in sc:
                if ctrl:
                    yield ctrl
                
# This is intentionally conservative; extend as you like.
# Keys MUST match your existing metric names exactly.
METRIC_META = {
    "Temperature": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "precision": 2,
    },
    "Temperature_F": {
        "unit": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "precision": 1,
    },
    "Rel-Humidity": {
        "unit": "%",
        "device_class": "humidity",
        "state_class": "measurement",
        "precision": 1,
    },
    "Ambient VPD": {
        "unit": "kPa",
        "state_class": "measurement",
        "precision": 2,
    },
    "Plant VPD": {
        "unit": "kPa",
        "state_class": "measurement",
        "precision": 2,
    },
    "Baro-Pressure": {
        "unit": "hPa",   # if you store hPa; change to "Pa" if you store Pa
        "device_class": "pressure",
        "state_class": "measurement",
        "precision": 1,
    },
    "CO2": {
        "unit": "ppm",
        "device_class": "carbon_dioxide",
        "state_class": "measurement",
        "precision": 0,
    },
    "Air Quality": {
        # HA has no universal "AQI" device_class across all versions;
        # keep it generic and just set unit.
        "unit": "AQI",
        "state_class": "measurement",
        "precision": 0,
    },
    "Gas": {
        "unit": "Ω",
        "state_class": "measurement",
        "precision": 0,
    },
    "Humidity": {
        # You appear to compute absolute humidity (g/m³) in some flows.
        # If instead this is something else, adjust unit.
        "unit": "g/m³",
        "state_class": "measurement",
        "precision": 2,
    },
    # --- VEML7700 / light metrics ---
    "Light Intensity": {
        "unit": "lx",                 # HA commonly uses "lx" for lux
        "device_class": "illuminance",
        "state_class": "measurement",
        "precision": 1,
    },
    "Auto Light": {
        "unit": "lx",
        "device_class": "illuminance",
        "state_class": "measurement",
        "precision": 1,
    },
    "PPFD": {
        "unit": "µmol/m²/s",
        "state_class": "measurement",
        "precision": 0,
    },
    "DLI": {
        "unit": "mol/m²/day",
        "state_class": "measurement",
        "precision": 2,
    },}

def ha_value_template_for_metric(metric_name: str, *, precision: int | None = None) -> str:
    """
    HA Jinja value_template for:
      - Nested JSON: {"values": {"Temperature": 21.1, ...}, ...}
      - Flat JSON:   {"Temperature": 21.1, ...}
    """
    m = (metric_name or "").replace('"', '\\"')

    # Important: set v to the raw JSON value (not a rendered {{ ... }} string)
    # Use .get() to avoid undefined exceptions.
    tmpl = (
        '{% set v = none %}'
        '{% if value_json is defined %}'
        '{% if value_json.values is defined and value_json.values is mapping %}'
        '{% set v = value_json.values.get("' + m + '") %}'
        '{% endif %}'
        '{% if v is none and value_json is mapping %}'
        '{% set v = value_json.get("' + m + '") %}'
        '{% endif %}'
        '{% endif %}'
    )

    if isinstance(precision, int) and precision >= 0:
        # Convert to float when possible so rounding works even if broker sends numeric strings.
        # If conversion fails, fall back to original v.
        tmpl += (
            '{% set n = v | float(default=none) %}'
            '{% if n is not none %}{{ n | round(' + str(int(precision)) + ') }}'
            '{% else %}{{ v if v is not none else "" }}{% endif %}'
        )
    else:
        tmpl += '{{ v if v is not none else "" }}'

    return tmpl

def build_ha_device_block(*, identifiers: list[str], name: str, model: str = "Sensorius", manufacturer: str = "Sensorius", sw_version: str | None = None) -> dict:
    device = {
        "identifiers": identifiers,
        "name": name,
        "model": model,
        "manufacturer": manufacturer,
    }
    if sw_version:
        device["sw_version"] = sw_version
    return device

def build_sensor_metric_discovery_payload(
    *,
    topic_map: HomeAssistantTopicMap,
    sensor_id: str,
    sensor_name: str,
    metric_name: str,
    location: str | None = None,
    model: str = "Sensorius Sensor",
    sw_version: str | None = None,
    state_topic_override: str | None = None,
    availability_topic_override: str | None = None,
) -> tuple[str, dict]:
    """
    Returns (discovery_topic, payload_dict)
    """
    metric_slug = slugify(metric_name)
    object_id = f"{sensor_id}__{metric_slug}"
    unique_id = f"sensorius__{sensor_id}__{metric_slug}"

    state_topic = state_topic_override or topic_map.sensor_state_topic(sensor_id)
    avail_topic = availability_topic_override or topic_map.sensor_availability_topic(sensor_id)

    meta = METRIC_META.get(metric_name, {})
    unit = meta.get("unit")
    device_class = meta.get("device_class")
    state_class = meta.get("state_class", "measurement")
    precision = meta.get("precision")

    payload: dict = {
        "name": metric_name,  # preserve your existing metric name for HA display
        "unique_id": unique_id,
        "state_topic": state_topic,
        "value_template": ha_value_template_for_metric(metric_name, precision=precision),
        "availability_topic": avail_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "state_class": state_class,
        "device": build_ha_device_block(
            identifiers=[f"sensorius:{sensor_id}"],
            name=sensor_name if not location else f"{sensor_name} ({location})",
            model=model,
            sw_version=sw_version,
        ),
    }

    if unit:
        payload["unit_of_measurement"] = unit
    if device_class:
        payload["device_class"] = device_class
    if isinstance(precision, int):
        # This key is accepted by HA for display hints in some entity types;
        # if HA ignores it, it’s harmless.
        payload["suggested_display_precision"] = precision

    discovery_topic = topic_map.sensor_discovery_topic(object_id)
    return discovery_topic, payload

def build_switch_channel_discovery_payload(
    *,
    topic_map: HomeAssistantTopicMap,
    switch_id: str,
    channel_id: str,   # <-- SWITCH_#_ID (truth)
    channel_label: str,
    device_name: str,
    location: str | None = None,
    model: str = "Sensorius Switch",
    sw_version: str | None = None,
    state_topic_override: str | None = None,
    command_topic_override: str | None = None,
    availability_topic_override: str | None = None,
) -> tuple[str, dict]:
    """
    Returns (discovery_topic, payload_dict)
    """
    object_id = f"{switch_id}__{channel_id}"
    unique_id = f"sensorius__{switch_id}__{channel_id}"

    cmd_topic = command_topic_override or topic_map.switch_command_topic(switch_id, channel_id)
    st_topic = state_topic_override or topic_map.switch_state_topic(switch_id, channel_id)
    avail_topic = availability_topic_override or topic_map.switch_availability_topic(switch_id)

    payload: dict = {
        "name": channel_label,
        "unique_id": unique_id,
        "command_topic": cmd_topic,
        "state_topic": st_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "availability_topic": avail_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": build_ha_device_block(
            identifiers=[f"sensorius:{switch_id}"],
            name=device_name if not location else f"{device_name} ({location})",
            model=model,
            sw_version=sw_version,
        ),
    }

    discovery_topic = topic_map.switch_discovery_topic(object_id)
    return discovery_topic, payload

def build_sensor_state_payload(*, ts_epoch: float, values_by_metric_name: dict) -> dict:
    # Keep your current metric names as keys; HA templates depend on exact match.
    payload = {"ts": ts_epoch}
    payload.update(values_by_metric_name)
    return payload

def _epoch_from_iso(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return time.time()
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()

def normalize_switch_state(value) -> bool | None:
    """
    Best-effort normalization of a switch state to bool.
    Returns True/False, or None if unknown.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None

    text = str(value).strip().lower()
    if text in ("on", "1", "true", "yes", "closed"):
        return True
    if text in ("off", "0", "false", "no", "open"):
        return False
    return None
