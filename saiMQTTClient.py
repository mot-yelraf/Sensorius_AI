"""MQTT publisher for local Sensorius sensors and switch controllers.

Responsibilities:
- maintain a per-sensor MQTT client connection
- publish sensor state payloads to HA-style topics
- optionally publish legacy sensor payloads for backward compatibility
- publish switch state/event topics for local relay changes

This module is used only for locally attached sensors/switches; remote devices
are handled by the ingest/bridge layer.
"""

import asyncio
import time
import random
import json
import paho.mqtt.client as mqtt
from saiUtils import printDM, debug_enabled

MODULE = "saiMQTTClient"
DEBUG = debug_enabled(MODULE)

class saiMQTTClient:
    def __init__(self, sensor, settings, supervisor=None):
        self.sensor = sensor
        self.settings = settings
        self.supervisor = supervisor  # to be assigned externally if needed

        self.broker = self.settings.broker
        self.port = 1883
        # updated for Home Assistant
        self.base_topic = settings.get_setting("HomeAssistant", "BASE_TOPIC", "sensorius")
        self.sensor_state_topic = f"{self.base_topic}/sensor/{self.sensor.sensor_id}/state"
        self.ha_state_retain = bool(settings.get_setting("HomeAssistant", "PUBLISH_STATE_RETAIN", True))
        # this is the legacy topic
        self.topic = f"sensor/{self.sensor.sensor_id}/data"
        client_id = f"sensorius-{self.sensor.sensor_id}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.reconnect_attempts = 0

        # Start paho network thread only once
        self._loop_started = False

        # Optional: throttle reconnect spam
        self._last_reconnect_ts = 0.0
        self._min_reconnect_interval_s = 3.0
        
        mqtt_username = str(self.settings.get_setting("MQTT", "USERNAME", "") or "").strip()
        mqtt_password = str(self.settings.get_setting("MQTT", "PASSWORD", "") or "").strip()
        if not mqtt_username:
            mqtt_username = str(self.settings.get_setting("HomeAssistant", "HA_USERNAME", "") or "").strip()
            mqtt_password = str(self.settings.get_setting("HomeAssistant", "HA_PASSWORD", "") or "").strip()

        if mqtt_username:
            self.client.username_pw_set(mqtt_username, mqtt_password)
            if DEBUG:
                printDM("MQTT username/password configured", location=MODULE)

    def _on_connect(self, client, userdata, flags, rc):
        if self.broker == "":
            return
        printDM(f"MQTT connected with code {rc}", location=MODULE)

    def _on_disconnect(self, client, userdata, rc):
        if self.broker == "":
            return
        printDM(f"MQTT disconnected with code {rc}", location=MODULE)

    def is_connected(self) -> bool:
        try:
            return bool(self.client and self.client.is_connected())
        except Exception:
            return False

    def publish(self, topic, payload, qos=0, retain=False):
        if not self.client:
            return None
        return self.client.publish(topic, payload, qos=qos, retain=retain)

    def close(self) -> None:
        """
        Best-effort shutdown for the background paho loop thread/socket.
        Safe to call multiple times.
        """
        try:
            if self.client:
                try:
                    self.client.disconnect()
                finally:
                    self.client.loop_stop()
            self._loop_started = False
            if DEBUG:
                printDM("MQTT client closed", location=MODULE)
        except Exception as e:
            printDM(f"MQTT close() failed: {e}", location=MODULE)

    def _ensure_loop_started(self) -> None:
        """
        Start the background paho loop exactly once.
        Safe to call repeatedly.
        """
        if self._loop_started:
            return
        try:
            self.client.loop_start()
            self._loop_started = True
            if DEBUG:
                printDM("MQTT loop_start() started", location=MODULE)
        except Exception as e:
            printDM(f"MQTT loop_start() failed: {e}", location=MODULE)

    async def ensure_connected(self, timeout=5):
        if self.broker == "":
            return False
        self._ensure_loop_started()

        start = time.monotonic()
        while not self.client.is_connected():
            if time.monotonic() - start > timeout:
                printDM("MQTT not connected after timeout", location=MODULE)
                return False
            await asyncio.sleep(0.5)
        return True

    async def mqtt_reconnect(self):
        """
        Attempt to connect to the broker. Does NOT call loop_start().
        """
        if self.broker == "":
            return

        # ensure the background loop is running before/while we connect
        self._ensure_loop_started()

        # simple reconnect throttle
        now = time.monotonic()
        if (now - self._last_reconnect_ts) < self._min_reconnect_interval_s:
            return
        self._last_reconnect_ts = now

        self.reconnect_attempts += 1

        for attempt in range(3):
            try:
                if DEBUG:
                    printDM(f"MQTT connect attempt {attempt+1}/3 to {self.broker}:{self.port}", location=MODULE)

                # connect() is non-blocking enough for typical use; it triggers on_connect via loop thread
                self.client.connect(self.broker, self.port)

                # Give on_connect a moment to run (without busy waiting)
                ok = await self.ensure_connected(timeout=5)
                if ok:
                    if DEBUG:
                        printDM("MQTT reconnected", location=MODULE)
                    return

                raise RuntimeError("connect() called but client not connected within timeout")

            except Exception as e:
                printDM(f"MQTT reconnect failed: {e}", location=MODULE)
                await asyncio.sleep(min(2**attempt, 10))

        printDM("All MQTT reconnect attempts failed", location=MODULE)

    async def mqtt_loop(self):
        """
        Health/reconnect/watchdog task.
        Never calls client.loop() because loop_start() owns the network loop.
        """
        interval = 13
        jitter = 1

        # Start loop thread once at entry
        if self.broker == "":
            return
        self._ensure_loop_started()

        while True:
            try:
                if self.broker == "":
                    return

                if not self.client.is_connected():
                    await self.mqtt_reconnect()

                # Optional keepalive “tick” (only if you want)
                # If you already publish regularly (sensor data), you can omit this.
                # if self.client.is_connected():
                #     self.client.publish("sensorius/status", "ok", qos=0, retain=False)

            except Exception as e:
                printDM(f"MQTT loop error: {e}", location=MODULE)

            if self.supervisor:
                self.supervisor.feedthedogs(f"{self.sensor.sensor_id} MQTT Loop")

            await asyncio.sleep(interval + random.uniform(-jitter, jitter))
            await asyncio.sleep(0)
            
    def publish_switch_state(
        self,
        switch_id: str,
        channel_id: str,
        is_on: bool,
        *,
        include_event: bool = True,
        qos: int = 0,
        retain: bool = True,
    ) -> bool:
        """
        Publish a switch state update for a local Pi switch using the new
        SWITCH_#_ID-based schema.

        Topics (ID-based, to match saiMQTTIngest):
          - state: "<base_topic>/switch/<switch_id>/<channel_id>/state"   payload: "ON"|"OFF"
          - event: "<base_topic>/switch/<switch_id>/<channel_id>/event"   payload: "ON"|"OFF"

        - switch_id:  the SWITCH_ID from switch.toml (e.g. "sensoria-hub-0-switch")
        - channel_id: the SWITCH_N_ID (e.g. "S1-123456")
        - is_on:      True for ON, False for OFF
        - include_event: also publish an '/event' message (for DB logging)
        Returns True if the state publish succeeded (rc == 0).
        """
        try:
            if not switch_id or not channel_id:
                return False
            if self.broker == "":
                return False
            if not self.client or not self.client.is_connected():
                printDM(
                    "MQTT switch publish skipped — client not connected",
                    location=MODULE,
                )
                return False

            state_str = "ON" if is_on else "OFF"
            base = f"{self.base_topic}/switch/{switch_id}/{channel_id}"

            # State topic used by saiMQTTIngest.handle_switch_state_slug
            state_topic = f"{base}/state"
            info_state = self.client.publish(state_topic, state_str, qos=qos, retain=retain)
            rc_state = getattr(info_state, "rc", 0) if info_state is not None else 0

            # Optional event topic used by saiMQTTIngest.handle_switch_event_slug
            if include_event:
                event_topic = f"{base}/event"
                info_ev = self.client.publish(event_topic, state_str, qos=qos, retain=False)
                rc_ev = getattr(info_ev, "rc", 0) if info_ev is not None else 0
            else:
                rc_ev = 0

            ok = (rc_state == 0 and rc_ev == 0)
            if not ok:
                printDM(
                    f"MQTT switch publish rc_state={rc_state} rc_ev={rc_ev} "
                    f"for {switch_id}::{channel_id}",
                    location=MODULE,
                )
            elif DEBUG:
                printDM(
                    f"Published switch [{switch_id}::{channel_id}] -> {state_str} "
                    f"({state_topic}{' + /event' if include_event else ''})",
                    location=MODULE,
                )
            return ok

        except Exception as e:
            printDM(f"publish_switch_state error: {e}", location=MODULE)
            return False

    async def mqtt_publish_data(self):
        publish_interval = self.sensor.publish_interval
        location = self.sensor.location
        next_publish = time.monotonic()
        next_feeding = time.monotonic() + self.sensor.meas_interval

        ha_retain = bool(self.settings.get_setting("HomeAssistant", "PUBLISH_STATE_RETAIN", True))
        publish_legacy = self.settings.get_setting("HomeAssistant", "PUBLISH_LEGACY_SENSOR_TOPIC", True)

        while True:
            now = time.monotonic()

            if now >= next_feeding:
                if self.supervisor:
                    self.supervisor.feedthedogs(f"{self.sensor.sensor_id} MQTT Publisher")
                next_feeding = time.monotonic() + int(self.sensor.publish_interval / 3)
                if DEBUG:
                    printDM(f"{self.sensor.sensor_id} fed the dogs", location=f"{MODULE}:mqtt_publish_data")

            if now >= next_publish:
                next_publish = now + publish_interval
                if self.broker == "":
                    return

                values, status, ts = self.sensor.current_data_set()

                ha_payload = {"ts": ts}
                ha_payload.update(values)

                try:
                    if not self.client.is_connected():
                        printDM("MQTT publish skipped — client not connected", location=MODULE)
                        continue

                    if publish_legacy:
                        legacy_payload = {
                            "timestamp": ts,
                            "status": status,
                            "location": location,
                            "values": values
                        }
                        legacy_str = json.dumps(legacy_payload, separators=(",", ":"))
                        self.client.publish(self.topic, legacy_str, qos=0, retain=False)

                    ha_str = json.dumps(ha_payload, separators=(",", ":"))
                    self.client.publish(self.sensor_state_topic, ha_str, qos=0, retain=ha_retain)

                    if DEBUG:
                        printDM(f"Published HA state {self.sensor_state_topic}: {ha_str}", location=MODULE)

                except Exception as e:
                    printDM(f"Publish error: {e}", location=MODULE)

            await asyncio.sleep(1)
            await asyncio.sleep(0)
            
_global_mqtt_clients = {}
_global_primary_mqtt_client = None

def get_mqtt_client(sensor_id):
    global _global_mqtt_clients
    global _global_primary_mqtt_client
    return _global_mqtt_clients.get(sensor_id) or _global_primary_mqtt_client

def set_mqtt_client(sensor_id, client):
    global _global_mqtt_clients
    global _global_primary_mqtt_client
    _global_mqtt_clients[sensor_id] = client
    if _global_primary_mqtt_client is None:
        _global_primary_mqtt_client = client
    
def get_all_mqtt_clients():
    seen = set()
    unique = []
    for c in _global_mqtt_clients.values():
        ident = id(c)
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(c)
    return unique
