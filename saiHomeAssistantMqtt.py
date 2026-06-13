"""Home Assistant MQTT discovery and state publishing for Sensorius.

This module defines the topic map and bridge used to publish Home Assistant
discovery payloads, sensor state, switch state, availability, and command
routing for Sensorius-managed local and remote entities.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from saiUtils import printDM, debug_enabled
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

    def _remote_nodus_state(self, device_id: str, *, device_type: str | None = None) -> str | None:
        try:
            ing = self.mqtt_clients
            getter = getattr(ing, "get_nodus_liveness", None)
            if not callable(getter):
                return None
            dev_map = getattr(ing, "device_type", {}) or {}
            dev_id = str(device_id or "").strip()
            if not dev_id:
                return None
            host_to_peer_ids = getattr(ing, "host_to_peer_ids", {}) or {}
            looks_remote = (
                str(dev_map.get(dev_id) or "").strip().lower() == "nodus"
                or dev_id.startswith("switch-")
                or any(dev_id in (peers or []) for peers in host_to_peer_ids.values())
            )
            if not looks_remote:
                return None
            snapshot = getter(dev_id, device_type=device_type)
            return str(snapshot.get("state") or "").strip().lower() or None
        except Exception:
            return None

    @staticmethod
    def _ha_availability_from_state(state: str | None) -> str:
        return "online" if str(state or "").strip().lower() == "online" else "offline"

    def _sensor_availability_for_discovery(self, sensor_id: str) -> str:
        state = self._remote_nodus_state(sensor_id, device_type="sensor")
        if state is None:
            return "online"
        return self._ha_availability_from_state(state)

    def _switch_availability_for_discovery(self, switch_id: str) -> str:
        state = self._remote_nodus_state(switch_id, device_type="switch")
        if state is None:
            return "online"
        return self._ha_availability_from_state(state)

    def handle_nodus_liveness_change(self, host: str, status: str, snapshot: dict | None = None) -> None:
        """Publish retained HA availability when MQTT ingest marks a Nodus host online/offline."""
        if not self.enabled:
            return
        state = str((snapshot or {}).get("state") or status or "").strip().lower()
        if state not in {"online", "degraded", "offline", "unknown", "migration_required"}:
            return
        availability = self._ha_availability_from_state(state)
        peers = []
        try:
            peers = list((snapshot or {}).get("peer_ids") or [])
        except Exception:
            peers = []
        host_text = str(host or "").strip()
        if host_text and host_text not in peers:
            peers.append(host_text)

        switch_ids: set[str] = set()
        try:
            for row in self.data_logger.get_switch_identities() or []:
                switch_id = str(row.get("switch_id") or "").strip()
                if switch_id:
                    switch_ids.add(switch_id)
        except Exception:
            switch_ids = set()

        for peer in peers:
            peer_id = str(peer or "").strip()
            if not peer_id or (len(peer_id) > 1 and peer_id[0] == "S" and peer_id[1].isdigit()):
                continue
            try:
                if peer_id in switch_ids or peer_id.startswith("switch-"):
                    self.publish_switch_availability(peer_id, availability)
                else:
                    self.publish_sensor_availability(peer_id, availability)
            except Exception:
                continue


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

    def _refresh_channel_index(self, *, reason: str | None = None) -> None:
        self._channel_index = self._build_channel_index()
        if DEBUG:
            msg = "HA channel index refreshed"
            if reason:
                msg += f" ({reason})"
            printDM(msg, location=MODULE)
    
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

            # availability retained; Nodus uses heartbeat/data freshness, not retained birth alone.
            avail_topic = self.topic_map.sensor_availability_topic(sensor_id)
            self.mqtt_clients.publish_text(
                avail_topic,
                self._sensor_availability_for_discovery(sensor_id),
                qos=self.qos,
                retain=True,
            )
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

            # availability retained; Nodus devices start offline unless liveness is fresh.
            avail_topic = self.topic_map.switch_availability_topic(switch_id)
            self.mqtt_clients.publish_text(
                avail_topic,
                self._switch_availability_for_discovery(switch_id),
                qos=self.qos,
                retain=True,
            )

        # publish retained initial switch states once so HA entities don't remain unknown
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
                    if not item:
                        self._refresh_channel_index(reason="initial switch state fallback lookup")
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
        normalized_channel_id = (channel_id or "").strip()
        # Labels can be renamed at runtime, so refresh before resolving command routing.
        self._refresh_channel_index(reason=f"command resolve switch_id={switch_id} channel_id={normalized_channel_id}")
        item = self._channel_index.get(normalized_channel_id)
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
                
def _metric_meta(
    unit: str,
    *,
    device_class: str | None = None,
    state_class: str = "measurement",
    precision: int | None = None,
) -> dict:
    meta = {"unit": unit, "state_class": state_class}
    if device_class:
        meta["device_class"] = device_class
    if precision is not None:
        meta["precision"] = precision
    return meta

def _normalized_metric_key(metric_name: str) -> str:
    return "".join(ch for ch in str(metric_name or "").strip().lower() if ch.isalnum())

# Home Assistant discovery metadata for Sensorius metric names.
# Units mirror the local sensor modules and normalized Nodus/WeeWX metric names.
METRIC_META = {
    "Temperature": _metric_meta("°C", device_class="temperature", precision=2),
    "Temperature_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Rel-Humidity": _metric_meta("%", device_class="humidity", precision=1),
    "Humidity": _metric_meta("g/m³", device_class="absolute_humidity", precision=2),
    "Dew Point": _metric_meta("°C", device_class="temperature", precision=2),
    "Dew Point_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Dew Point Deficit": _metric_meta("°C", device_class="temperature_delta", precision=2),
    "DewVPD Risk": _metric_meta("%", precision=1),
    "Ambient VPD": _metric_meta("kPa", precision=2),
    "Baro-Pressure": _metric_meta("hPa", device_class="atmospheric_pressure", precision=1),
    "CO2": _metric_meta("ppm", device_class="carbon_dioxide", precision=0),
    "Air Quality": _metric_meta("AQI", precision=0),
    "Gas": _metric_meta("Ω", precision=0),
    "Plant Temperature": _metric_meta("°C", device_class="temperature", precision=2),
    "Plant Temperature_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Plant Rel-Humidity": _metric_meta("%", device_class="humidity", precision=1),
    "Plant Humidity": _metric_meta("g/m³", device_class="absolute_humidity", precision=2),
    "Plant Dew Point": _metric_meta("°C", device_class="temperature", precision=2),
    "Plant Dew Point_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Plant Dew Point Deficit": _metric_meta("°C", device_class="temperature_delta", precision=2),
    "Plant Dewpoint Deficit": _metric_meta("°C", device_class="temperature_delta", precision=2),
    "Plant DewVPD Risk": _metric_meta("%", precision=1),
    "Plant VPD": _metric_meta("kPa", precision=2),
    "Plant Baro-Pressure": _metric_meta("hPa", device_class="atmospheric_pressure", precision=1),
    "Light Intensity": _metric_meta("lx", device_class="illuminance", precision=1),
    "Auto Light": _metric_meta("lx", device_class="illuminance", precision=1),
    "Estimated PPFD": _metric_meta("µmol/m²/s", precision=0),
    "Visible Light Intensity": _metric_meta("mol/m²/day", precision=2),
    "Soil Moisture": _metric_meta("%", device_class="moisture", precision=1),
    "Soil-Moisture": _metric_meta("%", device_class="moisture", precision=1),
    "Soil Moisture Deficit": _metric_meta("%", precision=1),
    "SMD": _metric_meta("%", precision=1),
    "Soil Stress Index": _metric_meta("%", precision=1),
    "SSI": _metric_meta("%", precision=1),
    "Soil Temp_C": _metric_meta("°C", device_class="temperature", precision=2),
    "Soil-Temp": _metric_meta("°C", device_class="temperature", precision=2),
    "Soil Temp_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Soil-Temp_F": _metric_meta("°F", device_class="temperature", precision=1),
    "Soil pH": _metric_meta("pH", precision=2),
    "Soil-pH": _metric_meta("pH", precision=2),
    "Soil EC": _metric_meta("mS/cm", device_class="conductivity", precision=2),
    "Soil-EC": _metric_meta("mS/cm", device_class="conductivity", precision=2),
    "Soil Nitrogen": _metric_meta("mg/kg", precision=0),
    "Soil Phosphorus": _metric_meta("mg/kg", precision=0),
    "Soil Potassium": _metric_meta("mg/kg", precision=0),
    "Soil Fertility Index": _metric_meta("%", precision=1),
    "PM1": _metric_meta("µg/m³", device_class="pm1", precision=1),
    "PM2.5": _metric_meta("µg/m³", device_class="pm25", precision=1),
    "PM4": _metric_meta("µg/m³", device_class="pm4", precision=1),
    "PM10": _metric_meta("µg/m³", device_class="pm10", precision=1),
    "Wind Speed": _metric_meta("mph", device_class="wind_speed", precision=1),
    "Wind Direction": _metric_meta("°", device_class="wind_direction", state_class="measurement_angle", precision=0),
    "Rain": _metric_meta("in", device_class="precipitation", precision=2),
    "Rain Last 24h": _metric_meta("in", device_class="precipitation", precision=2),
    "Rain Rate": _metric_meta("in/h", device_class="precipitation_intensity", precision=2),
}

METRIC_ALIAS_NAMES = [
    ("Dew-Point", "Dew Point"),
    ("Dew-Point_F", "Dew Point_F"),
    ("Dewpoint Deficit", "Dew Point Deficit"),
    ("Dewpoint Depression", "Dew Point Deficit"),
    ("dewVPD Risk", "DewVPD Risk"),
    ("Bar-Pressure", "Baro-Pressure"),
    ("Plant Dewpoint Deficit", "Plant Dew Point Deficit"),
    ("Plant dewVPD Risk", "Plant DewVPD Risk"),
]
METRIC_ALIASES = {
    _normalized_metric_key(alias): canonical
    for alias, canonical in METRIC_ALIAS_NAMES
}

def canonical_metric_name_for_ha(metric_name: str) -> str:
    """Return the metadata key used for HA discovery without changing DB metric names."""
    name = str(metric_name or "").strip()
    if name in METRIC_META:
        return name
    normalized = _normalized_metric_key(name)
    aliased = METRIC_ALIASES.get(normalized)
    if aliased in METRIC_META:
        return aliased
    for known_name in METRIC_META.keys():
        if _normalized_metric_key(known_name) == normalized:
            return known_name
    return name

def metric_meta_for_metric(metric_name: str) -> dict:
    return dict(METRIC_META.get(canonical_metric_name_for_ha(metric_name), {}))

def metric_value_lookup_names(metric_name: str) -> list[str]:
    """Return equivalent state-payload keys for old and canonical metric spellings."""
    name = str(metric_name or "").strip()
    canonical = canonical_metric_name_for_ha(name)
    normalized = _normalized_metric_key(canonical)
    names: list[str] = []
    for candidate in [name, canonical]:
        if candidate and candidate not in names:
            names.append(candidate)
    for known_name in METRIC_META.keys():
        if _normalized_metric_key(known_name) == normalized and known_name not in names:
            names.append(known_name)
    for alias, alias_canonical in METRIC_ALIAS_NAMES:
        if (
            _normalized_metric_key(alias_canonical) == normalized
            and alias not in names
        ):
            names.append(alias)
    return names

def _escape_template_key(metric_name: str) -> str:
    return str(metric_name or "").replace("\\", "\\\\").replace('"', '\\"')

def ha_value_template_for_metric(
    metric_name: str,
    *,
    precision: int | None = None,
    lookup_names: list[str] | None = None,
) -> str:
    """
    HA Jinja value_template for:
      - Nested JSON: {"values": {"Temperature": 21.1, ...}, ...}
      - Flat JSON:   {"Temperature": 21.1, ...}
    """
    metric_names = []
    for candidate in [metric_name, *(lookup_names or [])]:
        text = str(candidate or "").strip()
        if text and text not in metric_names:
            metric_names.append(text)
    if not metric_names:
        metric_names = [str(metric_name or "")]

    tmpl = '{% set v = none %}{% if value_json is defined %}'
    for candidate in metric_names:
        m = _escape_template_key(candidate)
        tmpl += (
            '{% if v is none and value_json.values is defined and value_json.values is mapping %}'
            '{% set v = value_json.values.get("' + m + '") %}'
            '{% endif %}'
            '{% if v is none and value_json is mapping %}'
            '{% set v = value_json.get("' + m + '") %}'
            '{% endif %}'
        )
    tmpl += '{% endif %}'

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

    meta = metric_meta_for_metric(metric_name)
    unit = meta.get("unit")
    device_class = meta.get("device_class")
    state_class = meta.get("state_class", "measurement")
    precision = meta.get("precision")

    payload: dict = {
        "name": metric_name,  # preserve your existing metric name for HA display
        "unique_id": unique_id,
        "state_topic": state_topic,
        "value_template": ha_value_template_for_metric(
            metric_name,
            precision=precision,
            lookup_names=metric_value_lookup_names(metric_name),
        ),
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
