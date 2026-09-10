"""Discover and control GW1200 AC1100 plugs through the local IoT API.

Weather ingestion remains independent. Controllers expose the normal switch
contract, schedule nonblocking commands, and log only gateway-confirmed states.
"""

import asyncio
import re
import time

import httpx

from .saiSwitch import SwitchController, RemoteSwitchController
from .saiSwitchSettingsManager import SwitchSettingsManager

IOT_TASK_NAME = "Ecowitt Smart Plugs"


def supports_smart_plugs(version):
    """Limit smart-plug discovery to the supported GW1200 gateway family."""
    return bool(re.search(r"\bGW1200[A-Z]?(?:_|\b)", str(version), re.I))


def query_interval(value):
    """Validate a smart-plug query interval in whole seconds."""
    from .saiEcowitt import EcowittError
    try:
        interval = int(value)
        if str(value).strip() != str(interval) or not 15 <= interval <= 60:
            raise ValueError
    except (TypeError, ValueError):
        raise EcowittError("Smart plug query interval must be a whole number from 15 to 60 seconds.")
    return interval


async def discover_smart_plugs(client, base_url):
    """Read registered AC1100 identities without changing any relay state."""
    from .saiEcowitt import EcowittError, MAX_RESPONSE_BYTES
    response = await client.get(f"{base_url}/get_iot_device_list")
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise EcowittError("Gateway IoT device list was unexpectedly large.")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("command"), list):
        raise EcowittError("Gateway IoT device list schema is not supported.")
    plugs = {}
    for item in payload["command"]:
        if not isinstance(item, dict) or item.get("model") != 2:
            continue
        device_id = item.get("id")
        if type(device_id) is not int or not 0 < device_id <= 0xffffffff:
            raise EcowittError("Gateway returned an invalid AC1100 identity.")
        plugs[device_id] = {"id": device_id, "model": 2, "name": "AC1100",
                            "online": item.get("rfnet_state") == 1, "signal": item.get("signal", 0)}
    return list(plugs.values())


def save_smart_plugs(discovery, manager=None):
    """Materialize stable switch identities while preserving labels and locations."""
    manager = manager or SwitchSettingsManager("switch_settings")
    for plug in discovery.get("smart_plugs", []):
        sid = f"{discovery['sensor_id']}-ac1100-{plug['id']:08x}"
        doc = manager.load(sid) or {}
        sw = doc.setdefault("Switch", {})
        sw.update(TYPE="ecowitt", DEVICE="AC1100", SWITCH_DEVICE_ID=sid,
                  ECOWITT_GATEWAY_ID=discovery["sensor_id"], ECOWITT_DEVICE_ID=plug["id"])
        sw.setdefault("SWITCH_LOCATION", "Unknown")
        sw.setdefault("SWITCH_1_LABEL", "Plug")
        sw.setdefault("SWITCH_1_CHANNEL_ID", f"{sid}-1")
        sw.setdefault("SWITCH_1_LAST_STATE", False)
        if doc != (manager.load(sid) or {}):
            manager.save(sid, doc)


class EcowittSwitchBackend:
    """Expose one channel without sending startup or synchronous network writes."""

    def __init__(self, settings):
        sw = settings.get("Switch", {})
        self.channels = [{"n": 1, "name": sw.get("SWITCH_1_LABEL", "Plug")}]
        self.is_present = True

    def get_switch_names(self):
        """Return the user-defined plug label."""
        return [self.channels[0]["name"]]

    def set_state(self, name, on):
        """Leave startup state application to the first status query."""
        return False


class EcowittSwitchController(SwitchController):
    """Use shared automation and timers with asynchronous confirmed HTTP control."""

    is_ecowitt = True
    _capture_settings_signature = RemoteSwitchController._capture_settings_signature
    _refresh_definition_from_settings = RemoteSwitchController._refresh_definition_from_settings

    def __init__(self, **kwargs):
        self._settings_signature = None
        self.service = None
        self.online = False
        self.confirmed = False
        self.last_error = "Awaiting gateway status"
        self.last_query = 0.0
        self.command_task = None
        self._io_lock = asyncio.Lock()
        super().__init__(**kwargs)

    def _apply_remote_settings_doc(self, doc):
        prior = {key: dict(getattr(self, key, {}) or {}) for key in
                 ("last_state", "last_set_time", "auto_off_seconds", "auto_off_deadline", "override_script")}
        RemoteSwitchController._apply_remote_settings_doc(self, doc)
        label = self.switch.get_switch_names()[0]
        for key, values in prior.items():
            if len(values) == 1:
                getattr(self, key)[label] = next(iter(values.values()))
        self.data_logger.upsert_switch_identity(switch_key=self._switch_key(label),
                                               switch_id=self.switch_id, label=label, location=self.location)

    def get_switch_names(self):
        """Return labels refreshed from the normal switch settings dialog."""
        self._refresh_definition_from_settings()
        return super().get_switch_names()

    def get_state(self, name):
        """Return the last confirmed relay state without network I/O."""
        self._refresh_definition_from_settings()
        return super().get_state(name)

    @property
    def available(self):
        """Report freshness and whether this plug belongs to the enabled gateway."""
        return bool(self.service and self.service.enabled and self.online and self.confirmed
                    and self._switch_block().get("ECOWITT_GATEWAY_ID") == self.service.sensor_id
                    and time.monotonic() - self.last_query <= self.service.smart_plug_interval_sec * 2 + 10)

    async def _request(self, command):
        from .saiEcowitt import EcowittError, MAX_RESPONSE_BYTES, normalize_gateway_url
        if not self.service or not self.service.enabled:
            raise EcowittError("Ecowitt gateway is disabled.")
        sw = self._switch_block()
        if sw.get("ECOWITT_GATEWAY_ID") != self.service.sensor_id:
            raise EcowittError("Plug belongs to a different Ecowitt gateway.")
        command = {**command, "model": 2, "id": int(sw["ECOWITT_DEVICE_ID"])}
        async with self.service._request_lock:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(
                    f"{normalize_gateway_url(self.service.gateway_url)}/parse_quick_cmd_iot",
                    json={"command": [command]},
                )
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise EcowittError("Gateway IoT response was unexpectedly large.")
                return response.json()

    async def _read_status(self, source="ecowitt/poll"):
        from .saiEcowitt import EcowittError
        payload = await self._request({"cmd": "read_device"})
        device_id = int(self._switch_block()["ECOWITT_DEVICE_ID"])
        items = payload.get("command", []) if isinstance(payload, dict) else []
        item = next((row for row in items if isinstance(row, dict)
                     and row.get("model") == 2 and row.get("id") == device_id), None)
        if not item or type(item.get("ac_status")) is not int or item["ac_status"] not in (0, 1):
            raise EcowittError("Gateway returned no valid AC1100 relay status.")
        if int(item.get("warning", 0)) & 128:
            raise EcowittError("AC1100 is offline at the gateway.")
        label = self.get_switch_names()[0]
        on = item["ac_status"] == 1
        previous = self.last_state.get(label, False)
        changed = previous != on
        record_event = changed
        if not self.confirmed:
            latest = await asyncio.to_thread(self.data_logger.get_latest_switch_state, self._switch_key(label))
            record_event = latest is None or (latest == "On") != on
        self.last_state[label] = on
        if not self.confirmed or changed:
            if self._switch_block().get("SWITCH_1_LAST_STATE") != on:
                await asyncio.to_thread(SwitchSettingsManager("switch_settings").update_setting,
                                        self.switch_id, "SWITCH_1_LAST_STATE", on)
            self._sync_auto_off_state(label, on, restart=bool(changed and on and source.startswith("manual")),
                                      allow_create_if_missing=source.startswith("manual"))
            if changed:
                self.last_set_time[label] = time.monotonic()
        if record_event:
            from .saiUtils import get_timestamp
            timestamp = get_timestamp()
            await asyncio.to_thread(self.data_logger.log_switch_event,
                                    switch_key=self._switch_key(label), is_on=on,
                                    timestamp=timestamp, source=source, sensor_id=f"Switch_{self.switch_id}")
            from . import saiWebRoutes as routes
            broadcast = getattr(getattr(getattr(routes, "app", None), "state", None), "switch_broadcast", None)
            if broadcast:
                await broadcast({"type": "switch_event", "key": self._switch_key(label),
                                 "ui_key": f"{self.switch_id}::{label}", "state": on,
                                 "timestamp": timestamp, "source": source,
                                 **self.get_auto_off_status(label)})
        self.confirmed = self.online = True
        self.last_query = time.monotonic()
        self.last_error = ""
        return on

    async def poll_status(self):
        """Refresh relay status and record physical/app/automation transitions."""
        async with self._io_lock:
            try:
                return await self._read_status()
            except Exception as exc:
                self.online = False
                self.last_error = str(exc)
                return None

    def set_state(self, name, on, *, force=False, event_source="manual/ui"):
        """Queue one command; success here means accepted, not yet confirmed."""
        if name not in self.get_switch_names() or not self.available:
            return False
        if self.command_task and not self.command_task.done():
            return False
        previous = self.get_state(name)
        if not force and previous == bool(on):
            return False
        elapsed = time.monotonic() - self.last_set_time.get(name, 0)
        if not self.override_script.get(name, False) and elapsed < (self.min_off_time if on else self.min_on_time):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.last_error = "AC1100 commands require the runtime event loop."
            return False
        self.command_task = loop.create_task(self._command(bool(on), event_source))
        return True

    async def _command(self, on, source):
        async with self._io_lock:
            try:
                command = {"cmd": "quick_stop"}
                if on:
                    command = dict(cmd="quick_run", on_type=0, off_type=0, always_on=1,
                                   on_time=0, off_time=0, val_type=0, val=0)
                await self._request(command)
                for attempt in range(5):
                    await asyncio.sleep(2)
                    if await self._read_status(source) == on:
                        if str(source).startswith("auto"):
                            self.auto_off_deadline[self.get_switch_names()[0]] = None
                        return True
                self.last_error = "Gateway did not confirm the requested plug state."
            except Exception as exc:
                self.online = False
                self.last_error = str(exc)
            return False
