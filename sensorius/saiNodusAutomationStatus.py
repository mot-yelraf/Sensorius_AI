"""Publish Sensorius automation ownership leases to Nodus devices."""

from __future__ import annotations

import asyncio
import socket
import time

from .saiAutomationManager import AutomationManager
from .saiUtils import debug_enabled, printDM

MODULE = "saiNodusAutomationStatus"
DEBUG = debug_enabled(MODULE)
STATUS_SCHEMA = "nodus-automation-status/v1"
AVAILABILITY_SCHEMA = "nodus-automation-availability/v1"
REFRESH_SECONDS = 60.0
SCAN_SECONDS = 5.0


def _enabled(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _device_inventory(ingest) -> dict[str, dict]:
    """Return physical device IDs and their known logical switch channels."""
    inventory: dict[str, dict] = {}
    for meta in (getattr(ingest, "discovery_cache", {}) or {}).values():
        if not isinstance(meta, dict):
            continue
        device_id = str(meta.get("device_id") or "").strip()
        switch = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
        switch_id = str(
            switch.get("switch_device_id") or switch.get("device_id") or ""
        ).strip()
        channels = switch.get("channels")
        if not device_id or not switch_id or not isinstance(channels, list):
            continue
        channel_ids = {
            str(row.get("channel_id") or "").strip()
            for row in channels
            if isinstance(row, dict) and str(row.get("channel_id") or "").strip()
        }
        topic_root = "nodus"
        topic_maps = (
            getattr(ingest, "nodus_switch_command_topics", {}) or {},
            getattr(ingest, "nodus_switch_state_topics", {}) or {},
        )
        for topic_map in topic_maps:
            for channel_id in channel_ids:
                topic = str(topic_map.get((switch_id, channel_id)) or "").strip()
                marker = f"/{channel_id}/"
                if marker in topic:
                    topic_root = topic.split(marker, 1)[0]
                    break
            if topic_root != "nodus":
                break
        inventory[device_id] = {
            "switch_id": switch_id,
            "channels": channel_ids,
            "topic_root": topic_root,
        }
    return inventory


def build_status_channels(rules: dict, switch_id: str, channel_ids) -> dict[str, list[str]]:
    """Map enabled Advanced rules to known channels for one Nodus switch."""
    known = {str(value or "").strip() for value in (channel_ids or [])}
    controlled: dict[str, set[str]] = {}
    switch_prefix = f"{str(switch_id or '').strip()}::"
    for rule_id, rule in (rules or {}).items():
        if not isinstance(rule, dict) or not _enabled(rule.get("enabled", False)):
            continue
        script = rule.get("script_json")
        if not isinstance(script, dict) or not _enabled(script.get("enabled", True)):
            continue
        for action in script.get("actions") or []:
            if not isinstance(action, dict):
                continue
            switch_key = str(action.get("switch_key") or "").strip()
            if not switch_key.startswith(switch_prefix):
                continue
            channel_id = switch_key[len(switch_prefix):].strip()
            if channel_id in known:
                controlled.setdefault(channel_id, set()).add(str(rule_id))
    return {
        channel_id: sorted(names)
        for channel_id, names in sorted(controlled.items())
    }


class NodusAutomationStatusPublisher:
    """Maintain retained ownership status and short-lived availability leases."""

    def __init__(
        self,
        mqtt_ingest,
        *,
        manager=None,
        controller_id=None,
        supervisor=None,
    ) -> None:
        self.mqtt_ingest = mqtt_ingest
        self.manager = manager or AutomationManager("switch_settings")
        self.supervisor = supervisor
        self.controller_id = str(
            controller_id or socket.gethostname() or "sensorius"
        ).strip()
        self._known_devices: dict[str, str] = {}
        self._last_status: dict[str, tuple] = {}
        self._last_availability: dict[str, float] = {}

    def _feed_watchdog(self) -> None:
        """Report progress without adding work to MQTT publish paths."""
        try:
            if self.supervisor is not None:
                self.supervisor.feedthedogs("Nodus Automation Status")
        except Exception:
            pass

    @staticmethod
    def _topics(device_id: str, topic_root: str = "nodus") -> tuple[str, str]:
        root = f"{topic_root}/{device_id}/automation/sensorius"
        return f"{root}/status", f"{root}/availability"

    def _publish(self, topic: str, payload: dict) -> bool:
        return bool(
            self.mqtt_ingest.publish_json(
                topic,
                payload,
                qos=0,
                retain=True,
                use_ha_client=False,
            )
        )

    def publish_once(self, *, force: bool = False, now: float | None = None) -> int:
        """Publish changed status plus periodic online leases."""
        now_value = float(time.time() if now is None else now)
        inventory = _device_inventory(self.mqtt_ingest)
        rules = self.manager.load_runtime_advanced("sensorius")
        published = 0
        for device_id, device in inventory.items():
            topic_root = str(device.get("topic_root") or "nodus")
            self._known_devices[device_id] = topic_root
            channels = build_status_channels(
                rules,
                device.get("switch_id", ""),
                device.get("channels") or set(),
            )
            signature = tuple(
                (channel_id, tuple(names))
                for channel_id, names in sorted(channels.items())
            )
            status_topic, availability_topic = self._topics(
                device_id, topic_root
            )
            refresh = (
                force
                or now_value - self._last_availability.get(device_id, 0.0)
                >= REFRESH_SECONDS
            )
            if refresh or self._last_status.get(device_id) != signature:
                if self._publish(status_topic, {
                    "schema": STATUS_SCHEMA,
                    "controller": "sensorius",
                    "controller_id": self.controller_id,
                    "updated_at": int(now_value),
                    "channels": [
                        {
                            "channel_id": channel_id,
                            "automations": names,
                            "enabled": True,
                        }
                        for channel_id, names in sorted(channels.items())
                    ],
                }):
                    self._last_status[device_id] = signature
                    published += 1
            if refresh:
                if self._publish(availability_topic, {
                    "schema": AVAILABILITY_SCHEMA,
                    "controller": "sensorius",
                    "controller_id": self.controller_id,
                    "status": "online",
                    "updated_at": int(now_value),
                }):
                    self._last_availability[device_id] = now_value
                    published += 1
        return published

    def publish_offline(self, *, now: float | None = None) -> int:
        """Mark every device reached during this process as offline."""
        now_value = float(time.time() if now is None else now)
        published = 0
        for device_id in sorted(self._known_devices):
            _status_topic, availability_topic = self._topics(
                device_id, self._known_devices[device_id]
            )
            if self._publish(availability_topic, {
                "schema": AVAILABILITY_SCHEMA,
                "controller": "sensorius",
                "controller_id": self.controller_id,
                "status": "offline",
                "updated_at": int(now_value),
            }):
                published += 1
        return published

    async def run(self) -> None:
        """Run the ownership publisher until the supervised task is cancelled."""
        try:
            while True:
                self.publish_once()
                self._feed_watchdog()
                await asyncio.sleep(SCAN_SECONDS)
        except asyncio.CancelledError:
            self.publish_offline()
            raise
        except Exception as exc:
            if DEBUG:
                printDM(f"automation status publisher failed: {exc}", location=MODULE)
            raise
