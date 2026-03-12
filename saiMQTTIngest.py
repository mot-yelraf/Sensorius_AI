"""MQTT ingest, discovery, and HA bridge for remote Sensorius devices.

Responsibilities:
- subscribe to sensor/switch topics from Pico2 W/Nodus devices
- ingest readings and switch events into the local database
- mirror Nodus topics to the HA broker when configured
- publish HA discovery configs for switches and (optionally) sensors
- accept HA commands and forward them to the correct device/topic
- maintain device liveness, topic maps, and discovery caches
"""
import asyncio
import json
import re
import socket
import time
import threading
try:
    import tomllib
except Exception:
    tomllib = None
import httpx
import paho.mqtt.client as mqtt
from collections import defaultdict, OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo
from saiUtils import printDM, debug_enabled, get_timestamp, normalize_hostname_base, mdns_hostname
from saiDataLogger import saiDataLogger, build_switch_key

MODULE = "saiMQTTIngest"
DEBUG = debug_enabled(MODULE)
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_HEARTBEAT_INTERVAL_S = 10.0
HEARTBEAT_STALE_AFTER_S = 90.0
HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 15.0
LEGACY_POLLER_SUNSET_DATE = "2026-06-30"

# module helpers
def _slugify(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "_")

def _to_bool(raw, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
    
def _norm_label(label: str | None) -> str:
    return (label or "").strip().lower()

def _looks_like_channel_id(token: str | None) -> bool:
    """
    Nodus per-channel IDs are commonly shaped like 'S1-<serial>', 'S2-<serial>', etc.
    These are not hostnames and must not be treated as discovery targets.
    """
    s = (token or "").strip()
    if not s:
        return False
    return bool(re.fullmatch(r"S\d+-[A-Za-z0-9_-]+", s, flags=re.IGNORECASE))

def _iso_from_payload_ts(raw_ts) -> str | None:
    """
    Accepts epoch seconds (float/int) or ISO8601 string; returns ISO8601 with TZ.
    Falls back to None if we cannot parse.
    """
    try:
        if isinstance(raw_ts, (int, float)):
            # Prefer your app TZ if available; else "America/Denver"
            try:
                from saiSettings import saiSettings
                _settings = saiSettings(apply_live=False)
                tzname = (_settings.get_setting("Time", "TZ")
                          or _settings.get_setting("Time", "tz")
                          or "America/Denver")
            except Exception:
                tzname = "America/Denver"
            tz = ZoneInfo(tzname)
            return datetime.fromtimestamp(float(raw_ts), tz=tz).isoformat()
        if isinstance(raw_ts, str) and raw_ts:
            # Assume already ISO8601-ish
            return raw_ts
    except Exception:
        pass
    return None

def split_switch_id_and_pin(sw_part: str) -> tuple[str, str | None]:
    """
    sw_part examples:
      "switch-oqs3lr-GP28" -> ("switch-oqs3lr", "GP28")
      "switch-oqs3lr"      -> ("switch-oqs3lr", None)
    We split on the *last* '-' so IDs with dashes still work.
    """
    try:
        if "-" not in sw_part:
            return (sw_part, None)
        base, tail = sw_part.rsplit("-", 1)
        return (base, tail or None)
    except Exception:
        return (sw_part, None)

_current_ingest = None

def set_current_ingest(inst):
    global _current_ingest
    _current_ingest = inst

def get_current_ingest():
    return _current_ingest
    
class saiMQTTIngest:
    def __init__(self, broker="localhost", client_id="rpi_ingest", mqtt_clients=None, supervisor=None, settings=None, data_logger=None):
        self._started = False
        self.supervisor = supervisor
        self.settings = settings
        self.broker = broker
        self.client_id = client_id
        self.port = 1883
        try:
            ha_broker = (
                self.settings.get_setting("HomeAssistant", "HA_BROKER", "")
                or self.settings.get_setting("HomeAssistant", "BROKER", "")
                or ""
            )
            self.ha_broker = str(ha_broker).strip() if self.settings else ""
        except Exception:
            self.ha_broker = ""
        if not self.ha_broker:
            self.ha_broker = self.broker
        try:
            ha_port = (
                self.settings.get_setting("HomeAssistant", "HA_MQTTPORT", 1883)
                or self.settings.get_setting("HomeAssistant", "PORT", 1883)
                or 1883
            )
            self.ha_port = int(ha_port) if self.settings else 1883
        except Exception:
            self.ha_port = 1883
        try:
            self.nodus_passthrough = bool(self.settings.get_setting("HomeAssistant", "NODUS_PASSTHROUGH", False))
        except Exception:
            self.nodus_passthrough = False
        try:
            default_mirror = bool(self.ha_broker and self.ha_broker != self.broker)
            self.mirror_nodus = bool(self.settings.get_setting("HomeAssistant", "MIRROR_NODUS", default_mirror))
        except Exception:
            self.mirror_nodus = bool(self.ha_broker and self.ha_broker != self.broker)
        self.data_logger = data_logger or saiDataLogger()
        self.latest_meta = {}  # sensor_id-based dict with fault/battery/memory

        self.device_type = {}         # Maps TYPE
        self.topic_dev_id_map = {}    # Maps topic sensor_id or switch_id
        self.device_location = {}  # device location
        self.registered_topics = set()
        self.mqtt_clients = mqtt_clients or []
        try:
            raw_debug = self.settings.get_setting("SensorNetwork", "NODUS_DEBUG_DATA_ONLY", False) if self.settings else False
            self.nodus_debug_data_only = _to_bool(raw_debug, default=False)
        except Exception:
            self.nodus_debug_data_only = False
        self.expected_gauge_map = {}
        self._host_ip_cache: dict[str, str] = {}
        self.discovery_failures = {}  # track failures by hostname, time.monotonic()
        self.device_status = {}       # track device status by hostname → "online"/"degraded"/"offline"/"unknown"
        self.device_offline_count = defaultdict(int)  # track device offline by hostname 
        self.discovery_cache: dict[str, dict] = {} # cache of last /itaot per host (optional but handy)
        self.host_to_peer_ids: dict[str, list[str]] = {}  # map host -> [peer_ids] (already guarded in loop, but ensure exists early)
        self.last_mqtt_seen: dict[str, float] = {}  # last mqtt seen map (guarded elsewhere; set here for safety)
        self.nodus_availability: dict[str, str] = {}  # host -> "online"|"offline" (from MQTT /availability)
        self.last_heartbeat_ts: dict[str, float] = {}  # host -> unix epoch seconds from heartbeat payload
        self.last_heartbeat_payload: dict[str, dict] = {}  # host -> last heartbeat payload object
        self.heartbeat_interval_s_by_host: dict[str, float] = {}  # host -> advertised interval
        self.heartbeat_stale: dict[str, bool] = {}  # host -> heartbeat freshness diagnostic
        # Retained startup replays can include stale hosts. Track repeated retained traffic and
        # only promote to discovery after we see sustained retained data from the same host.
        self._retained_data_seen: dict[str, int] = {}
        self._retained_avail_probe_inflight: set[str] = set()
        self._legacy_firmware_hosts: set[str] = self._load_legacy_firmware_hosts()
        self._legacy_poller_sunset_epoch: float = self._load_legacy_poller_sunset_epoch()

        self.last_check_time = defaultdict(lambda: 0)
        self.mqtt_clients = set(self.mqtt_clients or [])

        self._callback_lock = threading.RLock()
        self._callback_filters: set[str] = set()
        self._connected_evt = asyncio.Event()
        self._calibration_lock = threading.RLock()
        self.calibration_ack_by_message: dict[str, dict] = {}
        self.calibration_result_by_message: dict[str, dict] = {}
        self.calibration_status_by_sensor: dict[str, dict] = {}
        self.calibration_progress_by_sensor: dict[str, dict] = {}
        self.calibration_event_result_by_sensor: dict[str, dict] = {}
        self.calibration_message_device: dict[str, str] = {}

        unique_id = f"{socket.gethostname()}-ingest"
        self.client = mqtt.Client(client_id=unique_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        # HA broker client (optional separate connection)
        self.ha_client = None
        self._ha_connected_evt = asyncio.Event()
        if self.ha_broker and self.ha_broker != self.broker:
            ha_id = f"{socket.gethostname()}-ha"
            self.ha_client = mqtt.Client(client_id=ha_id)
            self.ha_client.on_connect = self._on_ha_connect
            self.ha_client.on_disconnect = self._on_ha_disconnect
            # ensure HA-broker messages dispatch through the same handlers
            self.ha_client.on_message = self._on_message
        else:
            self.ha_client = self.client
            self._ha_connected_evt = self._connected_evt

        # Apply auth if configured.
        # When HA uses the same broker/client as ingest, prefer HomeAssistant
        # credentials with MQTT as fallback so UI-entered HA creds still work.
        if self.ha_client is self.client:
            self._apply_mqtt_auth(self.client, section="HomeAssistant", fallback_section="MQTT")
        else:
            self._apply_mqtt_auth(self.client, section="MQTT")
            self._apply_mqtt_auth(self.ha_client, section="HomeAssistant", fallback_section="MQTT")

        self._known_switch_ids: set[str] = set()
        self._switch_state_cache: dict[str, dict] = {}  # switch_id -> {channel: "on"/"off"}
        self._last_persisted_switch_state: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> "on"/"off"

        self.switch_channel_map: dict[tuple[str, str], str] = {}  # (switch_id, "SWITCH_n") -> label
        self.event_topic_to_label: dict[str, str] = {}  # "switch/<id>-<pin>/event"   -> "Label"
        self.nodus_switch_topic_map: dict[str, dict] = {}  # topic -> {"switch_id","channel_id","label","kind"}
        self.nodus_switch_command_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_channel_command_topics: dict[str, str] = {}  # channel_id -> topic
        self.nodus_switch_state_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_switch_event_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_switch_availability_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_sensor_topics: dict[str, str] = {}  # sensor_id -> topic
        self.nodus_label_to_channel: dict[tuple[str, str], str] = {}  # (switch_id, norm_label) -> channel_id
        try:
            self.base_topic = (self.settings.get_setting("HomeAssistant", "BASE_TOPIC", "sensorius")
                            if self.settings else "sensorius")
        except Exception:
            self.base_topic = "sensorius"

        # Default wildcards so Nodus data is ingested even before /itaot discovery.
        # Topics observed: nodus/<sensor_id>/data (and sometimes /state).
        base_topics = {
            "nodus/+/data",
            "nodus/+/state",
        }
        if not self.nodus_debug_data_only:
            base_topics.update({
                "nodus/+/availability",
                "nodus/+/status/heartbeat",
                "nodus/+/meta",
                "nodus/+/calibration/ack",
                "nodus/+/calibration/result",
                "nodus/+/event/calibration_status",
                "nodus/+/event/calibration_progress",
                "nodus/+/event/calibration_result",
                "nodus/+/onboard/hello",
                "nodus/+/config/ack",
                "nodus/+/config/result",
            })
        self.registered_topics.update(base_topics)
        if self.base_topic:
            prefixed_topics = {
                f"{self.base_topic}/nodus/+/data",
                f"{self.base_topic}/nodus/+/state",
            }
            if not self.nodus_debug_data_only:
                prefixed_topics.update({
                    f"{self.base_topic}/nodus/+/availability",
                    f"{self.base_topic}/nodus/+/status/heartbeat",
                    f"{self.base_topic}/nodus/+/calibration/ack",
                    f"{self.base_topic}/nodus/+/calibration/result",
                    f"{self.base_topic}/nodus/+/event/calibration_status",
                    f"{self.base_topic}/nodus/+/event/calibration_progress",
                    f"{self.base_topic}/nodus/+/event/calibration_result",
                    f"{self.base_topic}/nodus/+/meta",
                    f"{self.base_topic}/nodus/+/onboard/hello",
                    f"{self.base_topic}/nodus/+/config/ack",
                    f"{self.base_topic}/nodus/+/config/result",
                })
            self.registered_topics.update(prefixed_topics)
        self.onboarding_event_handler = None
        self._pending_set: dict[tuple[str, str], float] = {}
        self._loop = None  # set in start()
        
        try:
            from saiHomeAssistantMqtt import HomeAssistantTopicMap
            node_id = socket.gethostname()  # stable per-Sensorius node
            self.topic_map = HomeAssistantTopicMap(
                node_id=node_id,
                base_topic=self.base_topic,
                discovery_prefix="homeassistant",
            )
        except Exception:
            self.topic_map = None

        self._ha_discovered_sensor_metrics: set[str] = set()   # f"{sensor_id}::{metric_slug}"
        self._ha_discovered_switch_channels: set[str] = set()  # f"{switch_id}::{channel_id}"
        
    # helpers
    def _apply_mqtt_auth(self, client: mqtt.Client, *, section: str, fallback_section: str | None = None) -> None:
        """
        Apply username/password from settings to a paho client.
        """
        if not self.settings or not client:
            return
        try:
            user = str(self.settings.get_setting(section, "USERNAME", "") or "").strip()
            pwd = str(self.settings.get_setting(section, "PASSWORD", "") or "").strip()
            if not user and section == "HomeAssistant":
                user = str(self.settings.get_setting(section, "HA_USERNAME", "") or "").strip()
                pwd_raw = str(self.settings.get_setting(section, "HA_PASSWORD", "") or "").strip()
                pwd = str(self.settings.deobfuscate_secret(pwd_raw) or "").strip()
            if not user and fallback_section:
                user = str(self.settings.get_setting(fallback_section, "USERNAME", "") or "").strip()
                pwd = str(self.settings.get_setting(fallback_section, "PASSWORD", "") or "").strip()
            if user:
                client.username_pw_set(user, pwd)
        except Exception:
            return

    def _load_legacy_firmware_hosts(self) -> set[str]:
        """
        Hostnames explicitly marked as non-upgraded firmware that still require legacy pollers.
        """
        hosts: set[str] = set()
        try:
            sec = self.settings.get_section("SensorNetwork") if self.settings else {}
            raw = (sec or {}).get("LEGACY_FIRMWARE_HOSTS", [])
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    base = self._normalize_host_key(str(item or ""))
                    if base:
                        hosts.add(base)
        except Exception:
            pass
        return hosts

    def _load_legacy_poller_sunset_epoch(self) -> float:
        try:
            sec = self.settings.get_section("SensorNetwork") if self.settings else {}
            raw = str((sec or {}).get("LEGACY_POLLER_SUNSET_DATE", LEGACY_POLLER_SUNSET_DATE) or LEGACY_POLLER_SUNSET_DATE).strip()
            dt = datetime.strptime(raw, "%Y-%m-%d")
            return dt.timestamp()
        except Exception:
            try:
                return datetime.strptime(LEGACY_POLLER_SUNSET_DATE, "%Y-%m-%d").timestamp()
            except Exception:
                return 0.0

    def _legacy_pollers_allowed(self) -> bool:
        try:
            return time.time() < float(self._legacy_poller_sunset_epoch or 0.0)
        except Exception:
            return False

    def _use_legacy_pollers_for(self, host_like: str | None) -> bool:
        base = self._normalize_host_key(host_like)
        if not base:
            return False
        return (base in self._legacy_firmware_hosts) and self._legacy_pollers_allowed()

    def _allow_background_http_meta_discovery(self) -> bool:
        """
        Normal startup discovery should be MQTT-meta-first.
        HTTP /itaot-meta remains available for Add Device and explicit refreshes.
        """
        try:
            raw = self.settings.get_setting(
                "SensorNetwork",
                "BACKGROUND_HTTP_META_DISCOVERY",
                False,
            ) if self.settings else False
        except Exception:
            raw = False
        return _to_bool(raw, default=False)

    @staticmethod
    def _normalize_liveness_state(status: str | None) -> str:
        s = str(status or "").strip().lower()
        if s in {"online", "degraded", "offline", "unknown", "pending", "migration_required"}:
            if s == "pending":
                return "unknown"
            return s
        return "unknown"

    def _derive_heartbeat_interval_s(self, data: dict | None) -> float:
        interval = DEFAULT_HEARTBEAT_INTERVAL_S
        try:
            if isinstance(data, dict):
                raw = data.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S)
                interval = float(raw or DEFAULT_HEARTBEAT_INTERVAL_S)
        except Exception:
            interval = DEFAULT_HEARTBEAT_INTERVAL_S
        if interval < MIN_HEARTBEAT_INTERVAL_S:
            interval = MIN_HEARTBEAT_INTERVAL_S
        return interval

    def _extract_heartbeat_timestamp(self, data: dict | None) -> float | None:
        if not isinstance(data, dict):
            return None
        raw_ts = data.get("timestamp", None)
        if raw_ts is None:
            return None
        try:
            return float(raw_ts)
        except Exception:
            return None

    def _heartbeat_is_stale(self, ts_epoch: float | None, *, retain: bool, now_ts: float | None = None) -> bool:
        """
        Freshness rule:
        - retained heartbeat older than HEARTBEAT_STALE_AFTER_S is stale
        - heartbeat timestamp beyond skew tolerance into the future is stale
        """
        if ts_epoch is None:
            return bool(retain)
        now_v = float(now_ts if now_ts is not None else time.time())
        if ts_epoch > (now_v + HEARTBEAT_CLOCK_SKEW_TOLERANCE_S):
            return True
        if retain and (now_v - ts_epoch) > HEARTBEAT_STALE_AFTER_S:
            return True
        return False

    def _apply_heartbeat_timeout_state(self, base: str, now_ts: float | None = None) -> str:
        """
        Derive online/degraded/offline from last heartbeat timing:
          online   <= 2 intervals
          degraded > 2 and < 3 intervals
          offline  >= 3 intervals
        """
        base = self._normalize_host_key(base) or ""
        if not base:
            return "unknown"
        now_v = float(now_ts if now_ts is not None else time.time())
        interval = float(self.heartbeat_interval_s_by_host.get(base, DEFAULT_HEARTBEAT_INTERVAL_S) or DEFAULT_HEARTBEAT_INTERVAL_S)
        if interval < MIN_HEARTBEAT_INTERVAL_S:
            interval = MIN_HEARTBEAT_INTERVAL_S

        hb_ts = self.last_heartbeat_ts.get(base)
        if hb_ts:
            missed = max(0.0, now_v - float(hb_ts))
        else:
            # No heartbeat yet: derive liveness from general MQTT activity so hosts
            # can still age to OFFLINE when traffic stops.
            last_seen = float(
                self.last_mqtt_seen.get(base)
                or self.last_mqtt_seen.get(f"{base}.local")
                or 0.0
            )
            if last_seen <= 0.0:
                return self.device_status.get(base, "unknown")
            if self._get_nodus_availability(base) == "offline":
                return "offline"
            missed = max(0.0, now_v - last_seen)

        if missed <= (2.0 * interval):
            return "online"
        if missed < (3.0 * interval):
            return "degraded"
        return "offline"

    def _feed_watchdog(self, name: str = "MQTT Discovery Loop") -> None:
        """Best-effort watchdog feed; safe if supervisor is missing."""
        try:
            sup = getattr(self, "supervisor", None)
            if sup and hasattr(sup, "feedthedogs"):
                sup.feedthedogs(name)
        except Exception:
            return

    def _schedule_coro(self, coro) -> bool:
        """
        Schedule a coroutine on the ingest event loop from any thread.
        Returns True if scheduling succeeded.
        """
        loop = getattr(self, "_loop", None)
        if not loop:
            try:
                coro.close()
            except Exception:
                pass
            return False
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(asyncio.create_task, coro)
                return True
        except Exception:
            pass
        try:
            coro.close()
        except Exception:
            pass
        return False

    def set_onboarding_event_handler(self, handler) -> None:
        """
        Register callback to receive parsed onboarding topic events.
        Handler signature: fn(event_dict) -> None
        """
        self.onboarding_event_handler = handler

    def _normalize_calibration_payload(self, sensor_id: str, payload: dict | None, *, topic: str, retain: bool, kind: str) -> dict:
        body = dict(payload or {})
        normalized = {
            "sensor_id": str(body.get("sensor_id") or sensor_id or "").strip(),
            "status": str(body.get("status") or "").strip().lower(),
            "calibrated": body.get("calibrated"),
            "timestamp": body.get("timestamp"),
            "temp_offset": body.get("temp_offset"),
            "rh_offset": body.get("rh_offset"),
            "device_temp_offset": body.get("device_temp_offset"),
            "device_rh_offset": body.get("device_rh_offset"),
            "system_temp_offset": body.get("system_temp_offset"),
            "system_rh_offset": body.get("system_rh_offset"),
            "sample_index": body.get("sample_index"),
            "sample_total": body.get("sample_total"),
            "error": str(body.get("error") or "").strip(),
            "topic": topic,
            "retain": bool(retain),
            "kind": kind,
            "received_at": time.time(),
        }
        if normalized["sensor_id"]:
            return normalized
        return {}

    def _maybe_handle_calibration_topics(self, topic: str, payload_text: str, data: dict | None, *, retain: bool) -> bool:
        try:
            parts = topic.split("/")
            root_idx = -1
            if len(parts) >= 4 and parts[0] == "nodus":
                root_idx = 0
            elif (
                self.base_topic
                and len(parts) >= 5
                and parts[0] == self.base_topic
                and parts[1] == "nodus"
            ):
                root_idx = 1
            if root_idx < 0:
                return False

            device_id = str(parts[root_idx + 1] or "").strip()
            family = str(parts[root_idx + 2] or "").strip()
            leaf = str(parts[root_idx + 3] or "").strip()
            if not device_id:
                return False

            body = data if isinstance(data, dict) else {}
            now = time.time()
            with self._calibration_lock:
                if family == "calibration" and leaf == "ack":
                    message_id = str(body.get("message_id") or "").strip()
                    if not message_id:
                        return True
                    self.calibration_ack_by_message[message_id] = {
                        "message_id": message_id,
                        "device_id": device_id,
                        "accepted": bool(body.get("accepted", False)),
                        "topic": topic,
                        "retain": bool(retain),
                        "received_at": now,
                    }
                    return True

                if family == "calibration" and leaf == "result":
                    message_id = str(body.get("message_id") or "").strip()
                    status_payload = body.get("status") if isinstance(body.get("status"), dict) else {}
                    sensor_id = str(status_payload.get("sensor_id") or device_id).strip()
                    result = {
                        "message_id": message_id,
                        "device_id": device_id,
                        "applied": bool(body.get("applied", False)),
                        "started": bool(body.get("started", False)),
                        "updated": body.get("updated"),
                        "error": str(body.get("error") or "").strip(),
                        "status": self._normalize_calibration_payload(
                            sensor_id,
                            status_payload,
                            topic=topic,
                            retain=retain,
                            kind="result_status",
                        ),
                        "topic": topic,
                        "retain": bool(retain),
                        "received_at": now,
                    }
                    if message_id:
                        self.calibration_result_by_message[message_id] = result
                    if result["status"]:
                        self.calibration_status_by_sensor[sensor_id] = dict(result["status"])
                    return True

                if family == "event" and leaf in {"calibration_status", "calibration_progress", "calibration_result"}:
                    normalized = self._normalize_calibration_payload(
                        device_id,
                        body,
                        topic=topic,
                        retain=retain,
                        kind=leaf,
                    )
                    if not normalized:
                        return True
                    sensor_id = str(normalized.get("sensor_id") or device_id).strip()
                    self.calibration_status_by_sensor[sensor_id] = dict(normalized)
                    if leaf == "calibration_progress":
                        self.calibration_progress_by_sensor[sensor_id] = dict(normalized)
                    elif leaf == "calibration_result":
                        self.calibration_event_result_by_sensor[sensor_id] = dict(normalized)
                    return True

            return False
        except Exception as e:
            if DEBUG:
                printDM(f"[calibration] parse error: {e}", location=MODULE)
            return False

    def publish_nodus_calibration(self, device_id: str, *, action: str, payload: dict | None = None, message_id: str | None = None, qos: int = 1) -> dict:
        device = str(device_id or "").strip()
        action_name = str(action or "").strip().lower()
        if not device or not action_name:
            return {"ok": False, "message_id": "", "topic": ""}
        if not message_id:
            message_id = f"cal-{int(time.time())}-{action_name}-{device[:24]}"
        envelope = {
            "message_id": message_id,
            "action": action_name,
        }
        if payload is not None or action_name in {"apply", "set", "update"}:
            envelope["payload"] = payload or {}
        topic = f"nodus/{device}/calibration/set"
        ok = bool(self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False))
        if ok:
            with self._calibration_lock:
                self.calibration_message_device[message_id] = device
        return {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}

    async def wait_for_calibration_ack(self, message_id: str, timeout: float = 3.0) -> dict | None:
        deadline = time.time() + max(float(timeout), 0.0)
        while time.time() < deadline:
            with self._calibration_lock:
                hit = self.calibration_ack_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_calibration_result(self, message_id: str, timeout: float = 8.0) -> dict | None:
        deadline = time.time() + max(float(timeout), 0.0)
        while time.time() < deadline:
            with self._calibration_lock:
                hit = self.calibration_result_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    def get_nodus_calibration_state(self, sensor_id: str) -> dict | None:
        sid = str(sensor_id or "").strip()
        if not sid:
            return None
        with self._calibration_lock:
            status = self.calibration_status_by_sensor.get(sid)
            progress = self.calibration_progress_by_sensor.get(sid)
            final_result = self.calibration_event_result_by_sensor.get(sid)
            if not any((status, progress, final_result)):
                return None
            out = {}
            if status:
                out["status"] = dict(status)
            if progress:
                out["progress"] = dict(progress)
            if final_result:
                out["result"] = dict(final_result)
            return out

    def _maybe_handle_onboarding_topics(self, topic: str, payload_text: str, data: dict | None) -> bool:
        """
        Parse V2 onboarding topics and optionally dispatch to the registered callback.
        Topics:
          nodus/<device_id>/onboard/hello
          nodus/<device_id>/config/ack
          nodus/<device_id>/config/result
        """
        try:
            parts = topic.split("/")
            root_idx = -1
            if len(parts) >= 4 and parts[0] == "nodus":
                root_idx = 0
            elif (
                self.base_topic
                and len(parts) >= 5
                and parts[0] == self.base_topic
                and parts[1] == "nodus"
            ):
                root_idx = 1
            if root_idx < 0:
                return False

            device_id = str(parts[root_idx + 1] or "").strip()
            family = str(parts[root_idx + 2] or "").strip()
            leaf = str(parts[root_idx + 3] or "").strip()
            if not device_id:
                return False

            event_type = ""
            if family == "onboard" and leaf == "hello":
                event_type = "onboarding_hello"
            elif family == "config" and leaf == "ack":
                event_type = "onboarding_config_ack"
            elif family == "config" and leaf == "result":
                event_type = "onboarding_config_result"
            else:
                return False

            payload = data if isinstance(data, dict) else {}
            event = {
                "event_type": event_type,
                "topic": topic,
                "device_id": device_id,
                "payload": payload,
                "received_at": time.time(),
            }
            cb = getattr(self, "onboarding_event_handler", None)
            if callable(cb):
                try:
                    cb(event)
                except Exception as e:
                    if DEBUG:
                        printDM(f"[onboarding] callback error: {e}", location=MODULE)
            return True
        except Exception as e:
            if DEBUG:
                printDM(f"[onboarding] parse error: {e}", location=MODULE)
            return False

    def _mirror_to_ha(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        """
        Mirror Nodus topics to the HA broker unchanged.
        """
        if not self.mirror_nodus or not topic:
            return
        if not self.ha_client or self.ha_client is self.client:
            return
        # Avoid echoing command topics back to HA
        if topic.endswith("/set"):
            return
        if topic.startswith("nodus/") or (self.base_topic and topic.startswith(f"{self.base_topic}/nodus/")):
            try:
                self.ha_client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:
                pass

    async def start(self):
        if self._started:
            if DEBUG:
                printDM("MQTTIngest already started — skipping", location=MODULE)
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._start_ingest_loop()
        if self.ha_client is not self.client:
            self._start_ha_loop()

    def stop(self):
        try:
            self.client.disconnect()
            self.client.loop_stop(force=True)
            printDM("MQTT ingest client disconnected cleanly", location=MODULE)
        except Exception as e:
            printDM(f"Error stopping MQTT ingest: {e}", location=MODULE)
        if self.ha_client is not self.client:
            try:
                self.ha_client.disconnect()
                self.ha_client.loop_stop(force=True)
                printDM("MQTT HA client disconnected cleanly", location=MODULE)
            except Exception as e:
                printDM(f"Error stopping MQTT HA client: {e}", location=MODULE)

    def _start_ingest_loop(self):
        if DEBUG:
            printDM("Entered _start_ingest_loop", location=MODULE)
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            printDM(f"MQTT Ingest start error: {e}", location=MODULE)
        if self.ha_client is self.client:
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(self._ha_connected_evt.set)
            except Exception:
                pass

    def _start_ha_loop(self):
        if DEBUG:
            printDM("Entered _start_ha_loop", location=MODULE)
        try:
            self.ha_client.connect(self.ha_broker, self.ha_port, keepalive=60)
            self.ha_client.loop_start()
        except Exception as e:
            printDM(f"MQTT HA start error: {e}", location=MODULE)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            if DEBUG:
                printDM(f"Connected to MQTT Broker: {self.broker}", location=MODULE)
                    
            # signal ready (thread-safe)
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(self._connected_evt.set)
            except Exception:
                pass  
                      
            for topic in self.registered_topics:
                client.subscribe(topic)
                if DEBUG:
                    printDM(f"Subscribed to topic: {topic}", location=MODULE)

        else:
            printDM(f"Connection failed with code {rc}", location=MODULE)

    def _on_ha_connect(self, client, userdata, flags, rc):
        if rc == 0:
            if DEBUG:
                printDM(f"Connected to HA MQTT Broker: {self.ha_broker}", location=MODULE)
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(self._ha_connected_evt.set)
            except Exception:
                pass
        else:
            printDM(f"HA MQTT connection failed with code {rc}", location=MODULE)

    def _on_disconnect(self, client, userdata, rc):
        printDM(f"Disconnected from MQTT broker with rc={rc}", location=MODULE)
        try:
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected_evt.clear)
        except Exception:
            pass

    def _on_ha_disconnect(self, client, userdata, rc):
        printDM(f"Disconnected from HA MQTT broker with rc={rc}", location=MODULE)
        try:
            if self._loop:
                self._loop.call_soon_threadsafe(self._ha_connected_evt.clear)
        except Exception:
            pass

    def _on_message(self, client, userdata, msg):
        # --- basic, safe extraction ---
        topic = getattr(msg, "topic", "") or ""
        try:
            raw = msg.payload
            payload_text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            payload_text = ""
        try:
            qos = getattr(msg, "qos", 0) or 0
            retain = bool(getattr(msg, "retain", False))
        except Exception:
            qos = 0
            retain = False

        # Mirror Nodus traffic to HA broker from ingest-side connection only.
        try:
            if client is self.client:
                self._mirror_to_ha(topic, payload_text, qos=qos, retain=retain)
        except Exception as e:
            if DEBUG:
                printDM(f"[on_message] mirror skipped: {e}", location=MODULE)

        # --- Nodus time request (boot sync) ---
        try:
            if self._maybe_handle_nodus_time_request(client, topic):
                return
        except Exception as e:
            if DEBUG:
                printDM(f"[on_message] time request skipped: {e}", location=MODULE)

        # --- let your existing helpers try first (but never crash this handler) ---
        if not self.nodus_debug_data_only:
            try:
                self.handle_switch_event_slug(topic, payload_text)
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] slug parser skipped: {e}", location=MODULE)
            # Slug-state updates (cache only; no DB writes)
            try:
                self.handle_switch_state_slug(topic, payload_text)
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] state slug skipped: {e}", location=MODULE)
            try:
                self.handle_switch_event_device(topic, payload_text)
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] device parser skipped: {e}", location=MODULE)
            # Nodus switch topics: nodus/<channel_id>/(state|event)
            try:
                self.handle_nodus_switch_topic(topic, payload_text)
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] nodus switch parser skipped: {e}", location=MODULE)

        # --- parse JSON if possible ---
        try:
            data = json.loads(payload_text)
        except Exception:
            data = None

        if not self.nodus_debug_data_only:
            try:
                if self._maybe_handle_onboarding_topics(topic, payload_text, data):
                    return
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] onboarding parser skipped: {e}", location=MODULE)
            try:
                if self._maybe_handle_calibration_topics(topic, payload_text, data, retain=retain):
                    return
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] calibration parser skipped: {e}", location=MODULE)

        try:
            parts = topic.split("/")
            if not parts:
                return

            # ==================== SENSOR DATA ====================
            """
            # future implementation, requires Nodus update
            if parts[0] == self.base_topic and len(parts) >= 4 and parts[1] == "nodus" and parts[-1] == "state":
                sensor_id = parts[2]
            """
            is_nodus_root = (parts[0] == "nodus" and len(parts) >= 2)
            is_nodus_prefixed = (
                self.base_topic
                and parts[0] == self.base_topic
                and len(parts) >= 3
                and parts[1] == "nodus"
            )
            if is_nodus_root or is_nodus_prefixed:
                id_index = 1 if is_nodus_root else 2
                if (not self.nodus_debug_data_only) and len(parts) > id_index + 1 and parts[id_index + 1] == "meta":
                    nodus_id = parts[id_index]
                    if _looks_like_channel_id(nodus_id):
                        return
                    ok_meta, _ = self._parse_and_subscribe_from_nodus_meta(
                        data if isinstance(data, dict) else {},
                        topic_device_id=nodus_id,
                        retain=retain,
                    )
                    if ok_meta:
                        return
                if (not self.nodus_debug_data_only) and len(parts) > id_index + 2 and parts[id_index + 1] == "status" and parts[id_index + 2] == "heartbeat":
                    nodus_id = parts[id_index]
                    if _looks_like_channel_id(nodus_id):
                        return
                    base = self._host_from_sid_base(nodus_id)
                    if not base:
                        return
                    now_t = time.time()
                    if not retain:
                        self._maybe_add_mqtt_client(base)
                    else:
                        self._maybe_promote_retained_host(base, source="state")

                    peers = self.host_to_peer_ids.setdefault(base, [])
                    if nodus_id and nodus_id not in peers:
                        peers.append(nodus_id)

                    hb_state = self._normalize_liveness_state((data or {}).get("status") if isinstance(data, dict) else None)
                    hb_ts = self._extract_heartbeat_timestamp(data if isinstance(data, dict) else None)
                    hb_interval = self._derive_heartbeat_interval_s(data if isinstance(data, dict) else None)
                    self.heartbeat_interval_s_by_host[base] = hb_interval
                    self.heartbeat_interval_s_by_host[f"{base}.local"] = hb_interval

                    stale = self._heartbeat_is_stale(hb_ts, retain=retain, now_ts=now_t)
                    self.heartbeat_stale[base] = bool(stale)
                    self.heartbeat_stale[f"{base}.local"] = bool(stale)
                    self.last_mqtt_seen[base] = now_t
                    self.last_mqtt_seen[f"{base}.local"] = now_t

                    if isinstance(data, dict):
                        self.last_heartbeat_payload[base] = dict(data)
                        self.last_heartbeat_payload[f"{base}.local"] = dict(data)

                    if stale:
                        self._mark_host_status(base, "unknown")
                        return

                    if hb_ts is not None:
                        self.last_heartbeat_ts[base] = float(hb_ts)
                        self.last_heartbeat_ts[f"{base}.local"] = float(hb_ts)
                    else:
                        self.last_heartbeat_ts[base] = now_t
                        self.last_heartbeat_ts[f"{base}.local"] = now_t

                    # Keep explicit offline status authoritative, otherwise derive from heartbeat timing.
                    if hb_state == "offline":
                        self._mark_host_status(base, "offline")
                    else:
                        derived = self._apply_heartbeat_timeout_state(base, now_ts=now_t)
                        self._mark_host_status(base, derived)
                        if derived == "online":
                            self.device_offline_count[base] = 0
                    return

                if (not self.nodus_debug_data_only) and len(parts) > id_index + 1 and parts[id_index + 1] == "availability":
                    nodus_id = parts[id_index]
                    if _looks_like_channel_id(nodus_id):
                        if DEBUG:
                            printDM(f"[availability] ignoring channel id as host candidate: {nodus_id}", location=MODULE)
                        return
                    status = self._parse_availability_payload(payload_text, data)
                    if status:
                        base = self._host_from_sid_base(nodus_id)
                        if base:
                            if not retain:
                                self._maybe_add_mqtt_client(base)
                            else:
                                self._maybe_promote_retained_host(base, source="availability")
                            now_t = time.time()
                            self.last_mqtt_seen[base] = now_t
                            self.last_mqtt_seen[f"{base}.local"] = now_t
                            self.nodus_availability[base] = status
                            self.nodus_availability[f"{base}.local"] = status

                            peers = self.host_to_peer_ids.setdefault(base, [])
                            if nodus_id and nodus_id not in peers:
                                peers.append(nodus_id)

                            if status == "online":
                                self.device_offline_count[base] = 0
                                self._mark_host_status(base, "online")
                                # Recovery on fresh availability when heartbeat is stale/missing.
                                if base not in self.last_heartbeat_ts:
                                    self.heartbeat_stale[base] = True
                                    self.heartbeat_stale[f"{base}.local"] = True
                            elif status == "offline":
                                self._mark_host_status(base, "offline")
                    return
            if (is_nodus_root or is_nodus_prefixed):
                # Accept nodus/<id>/data or <base>/nodus/<id>/data (also /state or legacy no-suffix)
                if len(parts) > id_index + 1 and parts[id_index + 1] not in ("data", "state"):
                    return
                sensor_id = parts[id_index]
                values = self._parse_nodus_values_payload(payload_text, data)
                if not isinstance(values, dict) or not values:
                    return

                display_metrics = []
                if isinstance(data, dict):
                    display_metrics = self._normalize_display_metrics(
                        data.get("display_metrics") or data.get("metrics")
                    )
                    if display_metrics:
                        self.expected_gauge_map[sensor_id] = display_metrics

                # Always use local "now" for stored timestamps; ignore device payload ts.
                self.data_logger.log_readings(None, sensor_id, values)
    
                self.latest_meta[sensor_id] = {
                    "bcc_fault":    data.get("bcc_fault", "N/A"),
                    "bcc_charging": data.get("bcc_charging", "N/A"),
                    "free_mem":     data.get("free_mem", "N/A"),
                    "display_metrics": display_metrics,
                }
                # update time of message received
                try:
                    self.last_mqtt_seen[sensor_id] = time.time()
                except Exception:
                    pass
                # --- FAST LIVENESS PATH: mark host online and refresh host↔peer mapping
                try:
                    host = self._host_from_topic_or_sid(topic, sensor_id)
                    if host:
                        if not retain:
                            self._maybe_add_mqtt_client(host)
                        else:
                            self._maybe_promote_retained_host(host, source="data")
                        peers = self.host_to_peer_ids.setdefault(host, [])
                        if sensor_id and sensor_id not in peers:
                            peers.append(sensor_id)
                            
                        now_t = time.time()
                        self.last_mqtt_seen[host] = now_t
                        self.last_mqtt_seen[f"{host}.local"] = now_t 

                        self._mark_host_status(host, "online")
                        if (not self.nodus_debug_data_only) and host not in self.last_heartbeat_ts:
                            self.heartbeat_stale[host] = True
                            self.heartbeat_stale[f"{host}.local"] = True
                except Exception:
                    pass
                
                if DEBUG:
                    printDM(f"Stored MQTT data from {sensor_id}:{values}", location=MODULE)

                return

            # ==================== SWITCH INFO ====================
            elif (not self.nodus_debug_data_only) and parts and parts[0] == "nodus" and len(parts) > 1:
                sw_part = parts[1]  # "switch-xxxx" or "switch-xxxx-GP28" or "<channel_id>"
                base_id = None
                if parts[-1] in ("state", "event", "set"):
                    # New schema: topic uses channel_id directly
                    try:
                        info = self.nodus_switch_topic_map.get(topic)
                        base_id = info.get("switch_id") if info else None
                    except Exception:
                        base_id = None
                if not base_id:
                    base_id, _pin = split_switch_id_and_pin(sw_part)
                if base_id:
                    now_t = time.time()
                    self.last_mqtt_seen[base_id] = now_t
                    try:
                        host = self._host_from_topic_or_sid(None, base_id)
                        if host:
                            self.last_mqtt_seen[host] = now_t
                            self.last_mqtt_seen[f"{host}.local"] = now_t
                    except Exception:
                        pass   
                """
                try:
                    import saiWebRoutes as routes
                    switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
                    if switch_broadcast:
                        import asyncio
                        asyncio.create_task(switch_broadcast({
                            "type": "switch_event",
                            "key": f"{switch_id}::{label}",   # resolve label the same way you log sw_events
                            "state": is_on_bool,
                            "timestamp": iso_ts,
                            "source": "mqtt",
                        }))
                except Exception:
                    pass
                """
                
                try:
                    host = self._host_from_topic_or_sid(None, base_id)
                    if host:
                        peers = self.host_to_peer_ids.setdefault(host, [])
                        if base_id not in peers:
                            peers.append(base_id)
                        self._mark_host_status(host, "online")
                except Exception:
                    pass
    
        except Exception as e:
            printDM(f"Failed to process MQTT message on {topic}: {e}", location=MODULE)

    def _maybe_handle_nodus_time_request(self, client, topic: str) -> bool:
        """
        Handle Nodus boot-time time request:
          topic: "nodus/<id>/time/request" or "<base>/nodus/<id>/time/request"
        Responds on the same prefix with:
          "nodus/<id>/time/response" or "<base>/nodus/<id>/time/response"
        """
        try:
            parts = topic.split("/")
            if not parts:
                return False

            is_nodus_root = (parts[0] == "nodus" and len(parts) >= 4)
            is_nodus_prefixed = (
                self.base_topic
                and parts[0] == self.base_topic
                and len(parts) >= 5
                and parts[1] == "nodus"
            )
            if not (is_nodus_root or is_nodus_prefixed):
                return False

            id_index = 1 if is_nodus_root else 2
            if len(parts) <= id_index + 2:
                return False
            if parts[id_index + 1] != "time" or parts[id_index + 2] != "request":
                return False

            nodus_id = parts[id_index]
            payload = self._build_time_payload()
            if not payload:
                return False

            if is_nodus_root:
                resp_topic = f"nodus/{nodus_id}/time/response"
            else:
                resp_topic = f"{self.base_topic}/nodus/{nodus_id}/time/response"

            info = client.publish(resp_topic, json.dumps(payload, separators=(",", ":")), qos=0, retain=False)
            rc = getattr(info, "rc", 0) if info is not None else 0
            if DEBUG:
                printDM(
                    f"[time] {topic} -> {resp_topic} rc={rc} payload={payload}",
                    location=MODULE,
                )
            return True
        except Exception as e:
            if DEBUG:
                printDM(f"[time] handler error: {e}", location=MODULE)
            return False

    def _build_time_payload(self) -> dict | None:
        """
        Full JSON time payload for Nodus:
        - epoch: float seconds
        - iso: ISO8601 with TZ offset
        - tz: IANA zone name
        - tz_offset: seconds offset from UTC
        - tz_name: short name (e.g., MDT)
        """
        try:
            from saiSettings import saiSettings
            settings = saiSettings(apply_live=False)
            tz_name = (settings.get_setting("Time", "TZ", "") or "").strip() or "America/Denver"
            tz_short = (settings.get_setting("Time", "TZ_NAME", "") or "").strip()
            tz_offset = settings.get_setting("Time", "TZ_OFFSET", None)
        except Exception:
            tz_name = "America/Denver"
            tz_short = ""
            tz_offset = None

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Denver")

        now = datetime.now(tz)
        if tz_offset is None:
            try:
                tz_offset = int(now.utcoffset().total_seconds())
            except Exception:
                tz_offset = 0
        else:
            try:
                tz_offset = int(tz_offset)
            except Exception:
                tz_offset = 0

        if not tz_short:
            try:
                tz_short = now.tzname() or ""
            except Exception:
                tz_short = ""

        return {
            "epoch": time.time(),
            "iso": now.isoformat(),
            "tz": tz_name,
            "tz_offset": tz_offset,
            "tz_name": tz_short,
        }

    # ——— helper: derive hostname from topic/sensor_id ———
    def _normalize_host_key(self, name: str | None) -> str | None:
        """
        Canonical host key used for all dicts:
        - strip whitespace
        - remove a single trailing '.local'
        - never return empty
        """
        s = normalize_hostname_base(name)
        return s or None

    def _host_from_sid_base(self, sid: str | None) -> str | None:
        """
        Canonicalize sensor_id to the base host:
        - For Pi-hosted local IDs like 'avpd-i2c-0-sensoria-hub-0' (>=3 dashes or '-i2c-'), take the last token → 'sensoria-hub-0'
        - For MQTT Nodus IDs like 'apvpd-luvk44', use the whole id → 'apvpd-luvk44'
        """
        s = (sid or "").strip()
        if not s:
            return None
        if ("-i2c-" in s) or (s.count("-") >= 3):
            base = s.rsplit("-", 1)[-1].strip()
        else:
            base = s
        return self._normalize_host_key(base)

    def _host_from_topic_or_sid(self, topic: str | None, sensor_id: str | None = None) -> str | None:
        """
        Prefer <sid> from 'sensor/<sid>/...' and canonicalize it via _host_from_sid_base.
        """
        sid = sensor_id
        if not sid and isinstance(topic, str):
            parts = topic.split("/", 2)
            if len(parts) >= 2 and parts[0].strip().lower() == "sensor":
                sid = parts[1]
        return self._host_from_sid_base(sid)

    def _parse_availability_payload(self, payload_text: str, data: dict | None) -> str | None:
        """
        Normalize availability payloads to "online" or "offline".
        Accepts raw text or common JSON shapes.
        """
        raw = None
        if isinstance(data, dict):
            for key in ("status", "state", "availability"):
                if key in data:
                    raw = data.get(key)
                    break
        if raw is None:
            raw = payload_text

        if isinstance(raw, bool):
            return "online" if raw else "offline"

        s = str(raw or "").strip().lower()
        if s in {"online", "up", "ready", "ok", "1", "true"}:
            return "online"
        if s in {"offline", "down", "dead", "0", "false"}:
            return "offline"
        return None

    def _parse_nodus_values_payload(self, payload_text: str, data: dict | None) -> dict | None:
        """
        Extract metric values from Nodus data payloads.

        Preferred payload shape:
          {"values": {"Temperature": 21.0, ...}}

        Back-compat text shape (best effort):
          "Temperature=21.0, Rel-Humidity=39.1, ..."
        """
        if isinstance(data, dict) and isinstance(data.get("values"), dict):
            return data["values"]

        raw = str(payload_text or "").strip()
        if not raw:
            return None
        if raw.startswith("{") and raw.endswith("}"):
            return None

        out: dict[str, float | str] = {}
        for part in raw.split(","):
            chunk = part.strip()
            if not chunk or "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = key.strip()
            val = value.strip()
            if not key or not val:
                continue
            if key.lower().startswith("values[") and "=" in val:
                key, val = val.split("=", 1)
                key = key.strip()
                val = val.strip()
            try:
                out[key] = float(val)
            except Exception:
                out[key] = val
        return out or None

    def _normalize_display_metrics(self, raw_metrics) -> list[str]:
        """
        Normalize display metric hints into an ordered de-duplicated list.
        """
        if isinstance(raw_metrics, dict):
            values = [raw_metrics.get(f"METRIC_{idx}", "") for idx in range(1, 7)]
        elif isinstance(raw_metrics, (list, tuple)):
            values = list(raw_metrics)
        else:
            return []

        ordered: list[str] = []
        seen: set[str] = set()
        for raw in values:
            metric = str(raw or "").strip()
            if not metric or metric in seen:
                continue
            seen.add(metric)
            ordered.append(metric)
            if len(ordered) >= 6:
                break
        return ordered

    def _infer_sensor_device_name(self, raw_device, sensor_id: str | None = None) -> str:
        """
        Prefer an explicit device name; otherwise infer from <device>-<serial> sensor IDs.
        """
        device = str(raw_device or "").strip()
        if device:
            return device
        sid = str(sensor_id or "").strip().lower()
        if "-" in sid:
            prefix = sid.split("-", 1)[0].strip()
            if prefix and prefix not in {"sensor", "nodus", "remote", "mqtt"}:
                return prefix
        return ""

    def _extract_sensor_serial(self, sensor_blob: dict | None, payload: dict | None) -> str:
        """
        Accept serial from sensor-level metadata first, then top-level metadata.
        """
        if isinstance(sensor_blob, dict):
            serial = str(
                sensor_blob.get("serial")
                or sensor_blob.get("SERIAL_NUM")
                or sensor_blob.get("device_serial_num")
                or ""
            ).strip()
            if serial:
                return serial
        if isinstance(payload, dict):
            return str(
                payload.get("serial")
                or payload.get("SERIAL_NUM")
                or payload.get("device_serial_num")
                or ""
            ).strip()
        return ""

    def _get_nodus_availability(self, host_like: str | None) -> str | None:
        base = self._normalize_host_key(host_like)
        if not base:
            return None
        return self.nodus_availability.get(base) or self.nodus_availability.get(f"{base}.local")

    def _mark_host_status(self, host_like: str, status: str) -> None:
        """
        Write status to BOTH keys: 'base' and 'base.local'.
        All internal dicts should use 'base' as the index.
        """
        base = self._normalize_host_key(host_like)
        if not base:
            return
        s = self._normalize_liveness_state(status)
        self.device_status[base] = s
        self.device_status[f"{base}.local"] = s

    def _maybe_add_mqtt_client(self, host_like: str | None) -> None:
        """
        Auto-register a host for discovery if it isn't already tracked.
        """
        base = self._normalize_host_key(host_like)
        if not base:
            return
        if base in (self.mqtt_clients or set()):
            return
        self.add_client(base)

    def _maybe_promote_retained_host(self, host_like: str | None, *, source: str) -> None:
        """
        Handle retained MQTT messages without immediately re-enrolling stale hosts.
        Policy:
          - retained availability must pass /hayd once before auto-enroll
          - retained data/state can promote a host after 2 sightings
        """
        base = self._normalize_host_key(host_like)
        if not base:
            return
        if base in (self.mqtt_clients or set()):
            return

        src = (source or "").strip().lower()
        if src in {"data", "state"}:
            n = int(self._retained_data_seen.get(base, 0)) + 1
            self._retained_data_seen[base] = n
            if n >= 2:
                if DEBUG:
                    printDM(f"[retained] promoting host after repeated {src}: {base}", location=MODULE)
                self._maybe_add_mqtt_client(base)
            elif DEBUG:
                printDM(f"[retained] defer auto-enroll {base} ({src} seen {n}/2)", location=MODULE)
        elif src == "availability":
            if base in self._retained_avail_probe_inflight:
                return
            self._retained_avail_probe_inflight.add(base)
            ok = self._schedule_coro(self._validate_retained_availability_and_add(base))
            if not ok:
                self._retained_avail_probe_inflight.discard(base)
                if DEBUG:
                    printDM(f"[retained] skip auto-enroll for {base} (availability; no loop)", location=MODULE)
            elif DEBUG:
                printDM(f"[retained] validating availability before enroll: {base}", location=MODULE)
        elif DEBUG:
            printDM(f"[retained] skip auto-enroll for {base} ({src})", location=MODULE)

    async def _validate_retained_availability_and_add(self, base: str) -> None:
        """
        For retained availability, only enroll after a quick /hayd validation.
        This blocks stale retained ghosts while letting real devices onboard.
        """
        try:
            if not base or base in (self.mqtt_clients or set()):
                return

            def _parse_hayd_ok(data: object) -> bool:
                if not isinstance(data, dict):
                    return False
                status = str((data or {}).get("STATUS", "")).strip().lower()
                return status in {"ok", "online", "ready"}

            targets: list[str] = []
            mdns = mdns_hostname(base)
            if mdns:
                targets.append(mdns)
            if base and base != mdns:
                targets.append(base)
            try:
                ip_cached = (self._host_ip_cache or {}).get(base)
                if ip_cached:
                    targets.append(ip_cached)
            except Exception:
                pass
            try:
                ip_itaot = (self._host_ipv4addr or {}).get(base)
                if ip_itaot:
                    targets.append(ip_itaot)
            except Exception:
                pass

            seen = set()
            ordered_targets = []
            for t in targets:
                tt = str(t or "").strip()
                if tt and tt not in seen:
                    seen.add(tt)
                    ordered_targets.append(tt)

            timeout_cfg = httpx.Timeout(connect=1.5, read=1.5, write=1.5, pool=1.0)
            async with httpx.AsyncClient(timeout=timeout_cfg, http2=False) as client:
                for target in ordered_targets:
                    try:
                        resp = await client.get(f"http://{target}:8000/hayd", headers={"Connection": "close"})
                        if resp.status_code != 200:
                            continue
                        try:
                            data = resp.json()
                        except Exception:
                            continue
                        if _parse_hayd_ok(data):
                            if target.replace(".", "").isdigit():
                                self._host_ip_cache[base] = target
                            self._maybe_add_mqtt_client(base)
                            if DEBUG:
                                printDM(f"[retained] availability validated via /hayd: {base} ({target})", location=MODULE)
                            return
                    except Exception:
                        continue

            if DEBUG:
                printDM(f"[retained] availability validation failed: {base}", location=MODULE)
        finally:
            self._retained_avail_probe_inflight.discard(base)

    def _safe_get_switch_block(self, info: dict) -> dict:
        """
        Return the Switch block from the /itaot 'info' structure.
        Accepts several shapes defensively: top-level 'Switch' dict or nested 'switch_settings' -> 'Switch'.
        """
        if not isinstance(info, dict):
            return {}

        # common shapes
        if "Switch" in info and isinstance(info["Switch"], dict):
            return info["Switch"]

        # some /itaot payloads may nest this
        maybe = info.get("switch_settings") or info.get("nodus") or {}
        if isinstance(maybe, dict) and isinstance(maybe.get("Switch"), dict):
            return maybe["Switch"]

        return {}

    def _extract_switch_channels(self, info: dict) -> list[tuple[str, str]]:
        """
        Return list of (label, channel_id) from either a Switch block or top-level SWITCH_n_LABEL keys.
        """
        channels: list[tuple[str, str]] = []

        def _scan_block(block: dict) -> None:
            for key, val in block.items():
                m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
                if not m:
                    continue
                label = (val or "").strip()
                if not label:
                    continue
                idx = int(m.group(1))
                ch_id = str(block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                if ch_id:
                    channels.append((label, ch_id))

        switch_blk = self._safe_get_switch_block(info)
        if isinstance(switch_blk, dict) and switch_blk:
            _scan_block(switch_blk)
        elif isinstance(info, dict):
            _scan_block(info)

        return channels

    def _channel_id_from_topic(self, topic: str) -> str | None:
        try:
            parts = topic.split("/")
            if len(parts) < 3:
                return None
            if parts[0] == "nodus":
                return parts[1]
            if self.base_topic and parts[0] == self.base_topic and len(parts) >= 4 and parts[1] == "nodus":
                return parts[2]
        except Exception:
            return None
        return None

    def _register_nodus_switch_topics(
        self,
        switch_id: str,
        switch_location: str,
        *,
        event_topics: dict | None = None,
        state_topics: dict | None = None,
        command_topics: dict | None = None,
        availability_topics: dict | None = None,
        label_by_channel_id: dict[str, str] | None = None,
    ) -> bool:
        """
        Register Nodus switch topics from /itaot.
        Returns True if any new subscriptions were added.
        """
        if not switch_id:
            return False
        self.device_type[switch_id] = "nodus"
        self._known_switch_ids.add(switch_id)

        any_new = False
        label_by_channel_id = label_by_channel_id or {}

        def _register(kind: str, topic: str):
            nonlocal any_new
            if not topic:
                return
            ch_id = self._channel_id_from_topic(topic)
            if not ch_id:
                return
            label = label_by_channel_id.get(ch_id)
            self.nodus_switch_topic_map[topic] = {
                "switch_id": switch_id,
                "channel_id": ch_id,
                "label": label,
                "kind": kind,
            }
            self.device_location[topic] = switch_location or "Unknown"
            if kind == "state":
                self.nodus_switch_state_topics[(switch_id, ch_id)] = topic
            elif kind == "event":
                self.nodus_switch_event_topics[(switch_id, ch_id)] = topic
            elif kind == "command":
                self.nodus_switch_command_topics[(switch_id, ch_id)] = topic
                self.nodus_channel_command_topics[ch_id] = topic
            elif kind == "availability":
                self.nodus_switch_availability_topics[(switch_id, ch_id)] = topic

            if kind in ("state", "event", "availability"):
                if topic not in self.registered_topics:
                    self.registered_topics.add(topic)
                    self.client.subscribe(topic)
                    any_new = True
                    if DEBUG:
                        printDM(f"Subscribed to nodus switch {kind}: {topic}", location=MODULE)

        for _m in (event_topics or {}).values():
            _register("event", str(_m))
        for _m in (state_topics or {}).values():
            _register("state", str(_m))
        for _m in (command_topics or {}).values():
            _register("command", str(_m))
        for _m in (availability_topics or {}).values():
            _register("availability", str(_m))

        return any_new

    def _parse_and_subscribe_from_nodus_meta(
        self,
        meta: dict,
        *,
        topic_device_id: str | None = None,
        retain: bool = False,
    ) -> tuple[bool, bool]:
        """
        Parse retained MQTT-first metadata from "nodus/<device_id>/meta".
        Returns (meta_valid, any_new_subscriptions).
        """
        if not isinstance(meta, dict):
            return False, False

        schema = str(meta.get("schema") or "").strip().lower()
        if schema and schema != "nodus-meta/v1":
            return False, False

        def _meta_topic(raw_topic: str | None) -> str:
            t = str(raw_topic or "").strip()
            if not t:
                return ""
            if t.startswith("nodus/"):
                return t
            if self.base_topic and t.startswith(f"{self.base_topic}/nodus/"):
                return t
            return ""

        def _is_unknown_loc(val: str | None) -> bool:
            v = (val or "").strip().lower()
            return v in ("", "unknown", "n/a", "na", "none", "-")

        def _pick_location(*vals: str) -> str:
            for raw in vals:
                loc = str(raw or "").strip()
                if loc and not _is_unknown_loc(loc):
                    return loc
            return "Unknown"

        def _coerce_switch_state(raw_state) -> bool | None:
            if isinstance(raw_state, bool):
                return raw_state
            txt = str(raw_state or "").strip().lower()
            if txt in ("on", "1", "true", "t", "yes", "y"):
                return True
            if txt in ("off", "0", "false", "f", "no", "n"):
                return False
            return None

        device_id = str(meta.get("device_id") or topic_device_id or "").strip()
        if not device_id:
            return False, False
        base = self._normalize_host_key(device_id) or device_id
        now_t = time.time()

        sensor_blob = meta.get("sensor") if isinstance(meta.get("sensor"), dict) else {}
        switch_blob = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
        location_group = meta.get("location_group") if isinstance(meta.get("location_group"), dict) else {}

        sensor_id = str(sensor_blob.get("sensor_id") or "").strip()
        switch_id = str(
            switch_blob.get("switch_device_id")
            or switch_blob.get("device_id")
            or ""
        ).strip()
        group_location = str(location_group.get("location") or "").strip()
        sensor_location = str(sensor_blob.get("location") or "").strip()
        switch_location = str(switch_blob.get("location") or "").strip()
        resolved_location = _pick_location(group_location, switch_location, sensor_location)

        if not retain:
            self._maybe_add_mqtt_client(base)

        self.last_mqtt_seen[base] = now_t
        self.last_mqtt_seen[f"{base}.local"] = now_t

        peer_ids_for_host: list[str] = []
        discovered_sensors: list[dict] = []
        discovered_switches: list[dict] = []
        subscribed = False
        touched = False

        members = location_group.get("members")
        if isinstance(members, list):
            for member in members:
                m = str(member or "").strip()
                if m and m not in peer_ids_for_host:
                    peer_ids_for_host.append(m)

        if device_id and device_id not in peer_ids_for_host:
            peer_ids_for_host.append(device_id)

        # sensor metadata
        if sensor_id:
            touched = True
            if sensor_id not in peer_ids_for_host:
                peer_ids_for_host.append(sensor_id)
            self.device_type[sensor_id] = "nodus"
            self.last_mqtt_seen[sensor_id] = now_t
            sensor_device = self._infer_sensor_device_name(sensor_blob.get("device"), sensor_id)
            sensor_serial = self._extract_sensor_serial(sensor_blob, meta)
            display_metrics = self._normalize_display_metrics(
                sensor_blob.get("display_metrics") or sensor_blob.get("metrics")
            )
            if display_metrics:
                self.expected_gauge_map[sensor_id] = display_metrics

            register_sensor = getattr(self.data_logger, "register_sensor", None)
            if callable(register_sensor):
                try:
                    register_sensor(sensor_id)
                except Exception:
                    pass

            data_topic = _meta_topic(sensor_blob.get("data_topic"))
            avail_topic = _meta_topic(sensor_blob.get("availability_topic"))
            event_topic = _meta_topic(sensor_blob.get("event_topic"))
            sensor_loc = _pick_location(sensor_location, resolved_location)

            if data_topic:
                self.nodus_sensor_topics[sensor_id] = data_topic

            for t in (data_topic, avail_topic, event_topic):
                if not t:
                    continue
                self.topic_dev_id_map[t] = sensor_id
                self.device_location[t] = sensor_loc
                if t not in self.registered_topics:
                    self.registered_topics.add(t)
                    self.client.subscribe(t)
                    subscribed = True

            discovered_sensors.append({
                "sensor_id": sensor_id,
                "device_type": "nodus",
                "device": sensor_device,
                "sensor_type": str(sensor_blob.get("type") or "nodus").strip(),
                "location": sensor_loc,
                "serial": sensor_serial,
                "display_metrics": display_metrics,
            })

        # switch metadata
        channels = switch_blob.get("channels")
        if switch_id and isinstance(channels, list):
            touched = True
            if switch_id not in peer_ids_for_host:
                peer_ids_for_host.append(switch_id)

            switch_loc = _pick_location(switch_location, resolved_location)
            self.device_type[switch_id] = "nodus"
            self._known_switch_ids.add(switch_id)
            self.last_mqtt_seen[switch_id] = now_t

            event_topics: dict[str, str] = {}
            state_topics: dict[str, str] = {}
            command_topics: dict[str, str] = {}
            availability_topics: dict[str, str] = {}
            label_by_channel: dict[str, str] = {}
            channels_with_ids: list[tuple[str, str]] = []
            switch_payload: dict[str, object] = {
                "TYPE": "nodus",
                "SWITCH_DEVICE_ID": switch_id,
                "SWITCH_LOCATION": switch_loc,
            }

            for fallback_idx, row in enumerate(channels, start=1):
                if not isinstance(row, dict):
                    continue
                channel_id = str(row.get("channel_id") or "").strip()
                if not channel_id:
                    continue
                label = str(row.get("label") or channel_id).strip() or channel_id
                try:
                    idx = int(row.get("index"))
                except Exception:
                    idx = fallback_idx
                idx = max(1, idx)

                ev_t = _meta_topic(row.get("event_topic"))
                st_t = _meta_topic(row.get("state_topic"))
                set_t = _meta_topic(row.get("set_topic"))
                av_t = _meta_topic(row.get("availability_topic"))

                channels_with_ids.append((label, channel_id))
                label_by_channel[channel_id] = label
                self.nodus_label_to_channel[(switch_id, _norm_label(label))] = channel_id
                self.last_mqtt_seen[channel_id] = now_t

                if ev_t:
                    event_topics[str(idx)] = ev_t
                if st_t:
                    state_topics[str(idx)] = st_t
                if set_t:
                    command_topics[str(idx)] = set_t
                if av_t:
                    availability_topics[str(idx)] = av_t

                switch_payload[f"SWITCH_{idx}_LABEL"] = label
                switch_payload[f"SWITCH_{idx}_CHANNEL_ID"] = channel_id
                enable_pin = str(row.get("enable_pin") or "").strip()
                pin = str(row.get("pin") or "").strip()
                switch_payload[f"SWITCH_{idx}_ENABLE_PIN"] = enable_pin
                switch_payload[f"SWITCH_{idx}_PIN"] = pin

                state_bool = _coerce_switch_state(row.get("state"))
                if state_bool is not None:
                    switch_payload[f"SWITCH_{idx}_LAST_STATE"] = state_bool
                    cache = self._switch_state_cache.setdefault(switch_id, {})
                    cache[channel_id] = "on" if state_bool else "off"
                    cache[label] = "on" if state_bool else "off"

                try:
                    self.data_logger.upsert_switch_identity(
                        switch_key=build_switch_key(channel_id, label),
                        switch_id=switch_id,
                        label=label,
                        location=switch_loc,
                    )
                except Exception:
                    pass

            if channels_with_ids:
                discovered_switches.append({
                    "switch_id": switch_id,
                    "switch_location": switch_loc,
                    "channels": len(channels_with_ids),
                    "switch_payload": switch_payload,
                    "switch_type": "nodus",
                    "serial": str(switch_blob.get("serial") or "").strip(),
                })
                new_subs = self._register_nodus_switch_topics(
                    switch_id,
                    switch_loc,
                    event_topics=event_topics,
                    state_topics=state_topics,
                    command_topics=command_topics,
                    availability_topics=availability_topics,
                    label_by_channel_id=label_by_channel,
                )
                subscribed = subscribed or new_subs

        if not touched:
            return False, False

        peers = self.host_to_peer_ids.setdefault(base, [])
        for pid in peer_ids_for_host:
            if pid and pid not in peers:
                peers.append(pid)

        if not retain:
            self._mark_host_status(base, "online")

        try:
            self.discovery_cache[base] = meta
        except Exception:
            pass

        try:
            if discovered_sensors or discovered_switches:
                network_meta = meta.get("network") if isinstance(meta.get("network"), dict) else {}
                profile_meta = meta.get("profile") if isinstance(meta.get("profile"), dict) else {}
                mqtt_meta = meta.get("mqtt") if isinstance(meta.get("mqtt"), dict) else {}
                settings_info = {
                    "HOSTNAME": base,
                    "Network": {
                        "HOSTNAME": str(
                            network_meta.get("hostname")
                            or meta.get("hostname")
                            or base
                        ).strip() or base,
                        "SSID": str(network_meta.get("ssid") or "").strip(),
                        "PASSWORD": str(network_meta.get("password") or ""),
                    },
                    "Profile": {
                        "ACTIVE_PROFILE": str(profile_meta.get("active_profile") or "").strip(),
                    },
                    "MQTT": {
                        "BROKER": str(mqtt_meta.get("broker") or "").strip(),
                        "PORT": mqtt_meta.get("port"),
                        "USE_TLS": mqtt_meta.get("use_tls"),
                        "BASE_TOPIC": str(mqtt_meta.get("base_topic") or "").strip(),
                        "USERNAME": str(mqtt_meta.get("username") or ""),
                        "PASSWORD": str(mqtt_meta.get("password") or ""),
                    },
                }
                self._ensure_settings_from_itaot(
                    settings_info,
                    base,
                    discovered_sensors,
                    discovered_switches,
                )
        except Exception as e:
            printDM(f"[nodus-meta] settings seed failed: {e}", location=MODULE)

        if discovered_switches:
            try:
                import saiWebRoutes as routes
                switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
                if switch_broadcast:
                    self._schedule_coro(switch_broadcast({
                        "type": "switch_inventory_changed",
                        "host": base,
                        "timestamp": get_timestamp(),
                    }))
            except Exception:
                pass

        return True, subscribed

    def _normalize_itaot_meta_to_nodus_meta(self, payload: dict, *, topic_device_id: str | None = None) -> dict:
        """
        Convert /itaot-meta payload (schema: itaot-meta/v1) to nodus-meta/v1 shape.
        """
        if not isinstance(payload, dict):
            return {}

        sensor_blob = payload.get("sensor") if isinstance(payload.get("sensor"), dict) else {}
        switch_blob = payload.get("switch") if isinstance(payload.get("switch"), dict) else {}
        group_blob = payload.get("location_group") if isinstance(payload.get("location_group"), dict) else {}

        device_id = str(payload.get("device_id") or topic_device_id or "").strip()
        sensor_id = str(sensor_blob.get("sensor_id") or device_id).strip()
        switch_id = str(
            switch_blob.get("switch_device_id")
            or switch_blob.get("device_id")
            or ""
        ).strip()
        location = str(
            group_blob.get("location")
            or switch_blob.get("location")
            or sensor_blob.get("location")
            or ""
        ).strip()

        channels_out = []
        raw_channels = switch_blob.get("channels")
        if isinstance(raw_channels, list):
            for idx, row in enumerate(raw_channels, start=1):
                if not isinstance(row, dict):
                    continue
                channel_id = str(row.get("channel_id") or "").strip()
                if not channel_id:
                    continue
                try:
                    channel_idx = int(row.get("index"))
                except Exception:
                    channel_idx = idx
                channels_out.append({
                    "index": channel_idx,
                    "label": str(row.get("label") or channel_id).strip() or channel_id,
                    "channel_id": channel_id,
                    "state": row.get("state"),
                    "event_topic": f"nodus/{channel_id}/event",
                    "state_topic": f"nodus/{channel_id}/state",
                    "set_topic": f"nodus/{channel_id}/set",
                    "availability_topic": f"nodus/{channel_id}/availability",
                })

        return {
            "schema": "nodus-meta/v1",
            "device_id": device_id,
            "sensor": {
                "sensor_id": sensor_id,
                "device": self._infer_sensor_device_name(sensor_blob.get("device"), sensor_id),
                "serial": self._extract_sensor_serial(sensor_blob, payload),
                "location": str(sensor_blob.get("location") or location).strip(),
                "data_topic": f"nodus/{sensor_id}/data" if sensor_id else "",
                "event_topic": f"nodus/{sensor_id}/event" if sensor_id else "",
                "availability_topic": f"nodus/{sensor_id}/availability" if sensor_id else "",
                "display_metrics": self._normalize_display_metrics(
                    sensor_blob.get("display_metrics") or sensor_blob.get("metrics")
                ),
            },
            "switch": {
                "switch_device_id": switch_id,
                "location": str(switch_blob.get("location") or location).strip(),
                "channels": channels_out,
            },
            "location_group": {
                "location": location,
                "members": group_blob.get("members") if isinstance(group_blob.get("members"), list) else [],
            },
        }

    def _parse_and_subscribe_from_http_meta(self, payload: dict, hostname: str) -> tuple[bool, bool]:
        """
        Parse metadata payload from HTTP fallback endpoint.
        Supports:
          - nodus-meta/v1
          - itaot-meta/v1 (normalized to nodus-meta/v1)
          - legacy /itaot shape
        """
        if not isinstance(payload, dict):
            return False, False

        schema = str(payload.get("schema") or "").strip().lower()
        if schema == "nodus-meta/v1":
            return self._parse_and_subscribe_from_nodus_meta(payload, topic_device_id=hostname, retain=False)
        if schema == "itaot-meta/v1":
            normalized = self._normalize_itaot_meta_to_nodus_meta(payload, topic_device_id=hostname)
            return self._parse_and_subscribe_from_nodus_meta(normalized, topic_device_id=hostname, retain=False)
        return self._parse_and_subscribe_from_itaot(payload, hostname)

    def discover_enabled_switch_labels(self, info: dict) -> list[tuple[int, str]]:
        """
        Enumerate enabled channels and their labels from a /itaot 'info' blob.
        Handles:
          - Pi multi-relay board: single SWITCH_ENABLE_PIN for all channels, enable = label present AND SWITCH_x_PIN set.
          - Pico2 W: per-channel enable via non-empty SWITCH_x_EN; enable = SWITCH_x_LABEL present AND SWITCH_x_EN non-empty.

        Returns a list of (channel_index, label), where channel_index is the integer X in SWITCH_X.
        """
        switch_blk = self._safe_get_switch_block(info)
        if not switch_blk:
            if DEBUG:
                printDM("[discover_enabled_switch_labels] No Switch block found", location=MODULE)
            return []

        is_picow = (switch_blk.get("TYPE") or switch_blk.get("type") or "").strip().lower() in ("picow", "pico2w", "nodus")
        has_global_enable_pin = bool(str(switch_blk.get("SWITCH_ENABLE_PIN", "") or "").strip())

        enabled: list[tuple[int, str]] = []

        # find all indexed SWITCH_<n>_LABEL values
        for key, label in switch_blk.items():
            m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
            if not m:
                continue

            idx = int(m.group(1))
            label_str = (label or "").strip()
            if not label_str:
                continue  # no label → not installed/used

            # channel-specific pins/enable flags
            pin_key = f"SWITCH_{idx}_PIN"
            en_key  = f"SWITCH_{idx}_EN"
            en_pin_key = f"SWITCH_{idx}_ENABLE_PIN"

            pin_val = str(switch_blk.get(pin_key, "") or "").strip()
            en_val  = str(switch_blk.get(en_key, switch_blk.get(en_pin_key, "")) or "").strip()

            if is_picow:
                # Pico2 W: enabled iff per-channel EN is present (non-empty)
                if en_val:
                    enabled.append((idx, label_str))
            else:
                # Pi relay board: enabled iff channel PIN is present (non-empty)
                # (global SWITCH_ENABLE_PIN can exist, but channel PIN presence is the decisive signal)
                if pin_val:
                    enabled.append((idx, label_str))

        if DEBUG:
            pretty = ", ".join(f"{i}:{lbl}" for i, lbl in enabled) or "none"
            printDM(f"[discover_enabled_switch_labels] Enabled -> {pretty}", location=MODULE)

        return enabled

    def set_switch_by_channel_id(self, switch_id: str, channel_id: str, new_state: bool, qos: int = 0, retain: bool = False) -> bool:
        """
        Forward HA switch commands to Nodus using the ID-based command topic.
        Nodus listens on: switch/<switch_id>/<channel_id>/set   (or base_topic-prefixed if you choose)
        """
        try:
            if not switch_id or not channel_id:
                # allow channel-only routing if we learned a command topic
                pass
            topic = (self.nodus_switch_command_topics.get((switch_id, channel_id))
                     or self.nodus_channel_command_topics.get(channel_id)
                     or f"nodus/{channel_id}/set")  # fallback for Nodus channel-id commands
            payload = "ON" if new_state else "OFF"          # match your slug/state convention
            info = self.client.publish(topic, payload, qos=qos, retain=retain)
            rc = getattr(info, "rc", 0) if info is not None else 0
            if DEBUG:
                printDM(
                    f"[set_switch_by_channel_id] publish topic={topic} payload={payload} rc={rc}",
                    location=MODULE,
                )
            return rc == 0
        except Exception as e:
            printDM(f"[set_switch_by_channel_id] error: {e}", location=MODULE)
            return False
            
    def _host_candidates(self, hostname: str) -> list[str]:
        base = (hostname or "").strip()
        if not base:
            return []
        base = self._normalize_host_key(base) or base
        norm = f"{base}.local"
        cached_ip = getattr(self, "_host_ip_cache", {}).get(norm)
        return [cached_ip, norm] if cached_ip else [norm]

    async def _ipv4_first(self, host: str, port: int, timeout: float = 2.0) -> str | None:
        try:
            loop = asyncio.get_running_loop()
            # asyncio's getaddrinfo is non-blocking via threadpool under the hood
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM),
                timeout=timeout
            )
            if infos:
                return infos[0][4][0]
        except Exception:
            return None
        finally:
            # always feed; DNS was previously a starvation point
            try:
                self.supervisor.feedthedogs("MQTT Discovery Loop")
            except Exception:
                pass
        return None

    def get_known_devices(self):
        """Return list of sensor_id and switch_id seen in discovery."""
        return list(self.device_type.keys())

    def get_known_locations(self):
        """Return {sensor or switch: location} for discovered devices."""
        dev_locs = {}
        for topic, dev_id in self.topic_dev_id_map.items():
            loc = self.device_location.get(topic)
            if dev_id and loc:
                dev_locs[dev_id] = loc
        return dev_locs

    def add_client(self, hostname: str):
        """Register a new client and force an immediate probe in the discovery loop."""
        if not hostname:
            return
        normalized = self._normalize_host_key(hostname) or hostname.strip()
        if _looks_like_channel_id(normalized):
            if DEBUG:
                printDM(f"[add_client] skip channel id: {normalized}", location=MODULE)
            return
        # add to the tracked set
        self.mqtt_clients.add(normalized)
        # mark unknown and clear any previous OFFLINE cooldown
        self._mark_host_status(normalized, "unknown")
        if normalized in self.discovery_failures:
            del self.discovery_failures[normalized]
        # nudge discovery loop to run right away for this host
        # by pushing its last_check_time sufficiently in the past
        try:
            import time
            # make it look overdue by more than REFRESH_INTERVAL
            self.last_check_time[normalized] = time.monotonic() - 9999.0
        except Exception:
            self.last_check_time[normalized] = 0

    def _resolve_hostname_for(self, sensor_or_host: str) -> str | None:
        """
        Accepts a sensor_id (e.g., 'aqi-nz6g89') or a hostname ('aqi-nz6g89.local').
        If we have a known mapping (host_to_peer_ids), prefer it; else assume '<id>.local'.
        """
        s = (sensor_or_host or "").strip()
        if not s:
            return None
        if s.endswith(".local"):
            return mdns_hostname(s)

        # Try to find a host that already advertises this peer id
        for host, peers in (self.host_to_peer_ids or {}).items():
            try:
                if s in (peers or []):
                    return host
            except Exception:
                pass

        # Last-resort: assume mDNS hostname matches id
        return mdns_hostname(s)

    def get_measure_status(self, name: str, grace_sec: float = 120.0) -> str:
        """
        Return 'online' | 'degraded' | 'offline' | 'unknown' for either a host ('apvpd-luvk44' or '.local')
        or a peer id ('apvpd-luvk44').
        Rule:
          - If /availability reported a status, honor it first
          - If any peer mapped to this host has MQTT within grace_sec → 'online'
          - Else if we have a device_status for this host → that value
          - Else if the name itself (peer id) has recent MQTT → 'online'
          - Else 'unknown'
        """
        base = self._normalize_host_key(name) or (name or "").strip()
        if not base:
            return "unknown"

        now_ts = time.time()

        # 0) Explicit MQTT availability status if present
        try:
            avail = self._get_nodus_availability(base)
            if avail in ("online", "offline"):
                return avail
        except Exception:
            pass

        # 1) Recent MQTT from any mapped peer → online
        try:
            peers = self.host_to_peer_ids.get(base, [])
            for pid in peers or []:
                if (now_ts - self.last_mqtt_seen.get(pid, 0.0)) < grace_sec:
                    return "online"
        except Exception:
            pass

        # 2) If the thing we were given is itself a peer id and it's fresh → online
        try:
            if (now_ts - self.last_mqtt_seen.get(base, 0.0)) < grace_sec:
                return "online"
            if (now_ts - self.last_mqtt_seen.get(f"{base}.local", 0.0)) < grace_sec:
                return "online"
        except Exception:
            pass

        # 3) Fall back to discovery/HTTP opinion
        s = (self.device_status.get(base)
             or self.device_status.get(f"{base}.local")
             or "unknown")
        return self._normalize_liveness_state(s)

    def resolve_nodus_hostname(self, device_id: str, device_type: str | None = None) -> str | None:
        """
        Public resolver for WebRoutes:
        - If we already mapped this peer id via /itaot (host_to_peer_ids), return that host (no .local).
        - For switches ('switch-<serial>') without a direct map yet, try pairing by serial against known mqtt_clients.
        - For sensors, return the id itself (strip '.local') if no mapping exists.
        """
        try:
            dev = (device_id or "").strip()
            if not dev:
                return None

            # 1) Exact peer-id → host mapping from discovery
            for host, peers in (self.host_to_peer_ids or {}).items():
                try:
                    if dev in (peers or []):
                        return host  # return the bare hostname we use in mqtt_clients (e.g., "aqi-nz6g89")
                except Exception:
                    pass

            # 2) Switch heuristic: use serial suffix to match any known mqtt_client host (e.g., "aqi-<serial>")
            if (device_type or "").lower() == "switch" or dev.startswith("switch-"):
                serial = dev.rsplit("-", 1)[-1] if "-" in dev else dev
                try:
                    for cand in (self.mqtt_clients or []):  # set of bare hostnames like "aqi-nz6g89"
                        if str(cand).endswith(f"-{serial}"):
                            return str(cand)
                except Exception:
                    pass
                return None  # do NOT invent "switch-<serial>.local" here—switch host is the sensor's host

            # 3) Sensor fallback: if caller passed a sensor id, use it as the host
            if dev.endswith(".local"):
                dev = dev[:-6]  # strip ".local"
            return dev or None
        except Exception:
            return None

    async def _send_nodus_restart(self, hostname: str, restart: str = "hard", *, port: int = 8000, timeout_sec: float = 4.0) -> bool:
        """
        POST http://<hostname>:<port>/nodus-restart?restart=soft|hard
        Returns True on HTTP 200..299.
        """
        if not hostname:
            return False
        url = f"http://{hostname}:{port}/nodus-restart?restart={restart}"
        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.post(url, content=b"")  # empty body OK
                ok = 200 <= resp.status_code < 300
                if ok:
                    if DEBUG:
                        printDM(f"[nodus-restart] {hostname} → {restart} OK ({resp.status_code})", location=MODULE)
                    return True
                else:
                    if DEBUG:
                        printDM(f"[nodus-restart] {hostname} → {restart} failed ({resp.status_code})", location=MODULE)
        except Exception as e:
            if DEBUG:
                printDM(f"[nodus-restart] {hostname} → {restart} error: {e}", location=MODULE)
        return False

    def _parse_and_subscribe_from_itaot(self, info: dict, hostname: str) -> tuple[bool, bool]:
        """
        Promoted from the discovery loop so we can reuse it for manual refresh calls.
        Returns (itaot_valid, any_new_subscriptions).
        """
        itaot_valid = False
        subscribed = False
        peer_ids_for_host: list[str] = []
        discovered_sensors: list[dict] = []
        discovered_switches: list[dict] = []

        is_pi_multi = isinstance(info.get("sensors"), list)
        base = self._normalize_host_key(hostname) or hostname
        
        def _is_unknown_loc(val: str | None) -> bool:
            v = (val or "").strip().lower()
            return v in ("", "unknown", "n/a", "na", "none", "-")

        def _resolve_switch_location(sw_blob: dict | None = None) -> str:
            """
            Prefer explicit switch location; if unknown, inherit from paired sensor location.
            For single-sensor /itaot payloads this falls back to top-level LOCATION.
            """
            sw_blob = sw_blob or {}
            try:
                loc = str(sw_blob.get("SWITCH_LOCATION", "") or "").strip()
            except Exception:
                loc = ""
            if loc and not _is_unknown_loc(loc):
                return loc

            # Try serial pairing against sensors discovered from this same /itaot payload.
            sw_serial = str(
                sw_blob.get("DEVICE_SERIAL_NUM")
                or ""
            ).strip().lower()
            if sw_serial:
                try:
                    for srow in (discovered_sensors or []):
                        s_serial = str(srow.get("serial") or "").strip().lower()
                        s_loc = str(srow.get("location") or "").strip()
                        if s_serial and s_serial == sw_serial and s_loc and not _is_unknown_loc(s_loc):
                            return s_loc
                except Exception:
                    pass

            # Single-sensor payloads usually carry LOCATION at the top level.
            top_loc = str(info.get("LOCATION") or "").strip()
            if top_loc and not _is_unknown_loc(top_loc):
                return top_loc

            return "Unknown"

        def _norm_ipv4(addr: str | None) -> str | None:
            raw = (addr or "").strip()
            if not raw:
                return None
            parts = raw.split(".")
            if len(parts) != 4:
                return None
            for part in parts:
                if not part.isdigit():
                    return None
                try:
                    val = int(part)
                except Exception:
                    return None
                if val < 0 or val > 255:
                    return None
            return raw

        def _extract_ipv4addr(payload: dict) -> str | None:
            if not isinstance(payload, dict):
                return None
            for key in ("ipv4addr", "IPV4ADDR", "IPv4Addr", "ipv4"):
                ip = _norm_ipv4(payload.get(key))
                if ip:
                    return ip
            for key, val in payload.items():
                if isinstance(key, str) and key.lower() in {"ipv4addr", "ipv4"}:
                    ip = _norm_ipv4(val)
                    if ip:
                        return ip
            return None

        def _coerce_switch_state(raw) -> bool | None:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(int(raw))
            txt = str(raw or "").strip().lower()
            if not txt:
                return None
            if txt in {"on", "1", "true", "t", "yes", "y"}:
                return True
            if txt in {"off", "0", "false", "f", "no", "n"}:
                return False
            return None

        def _seed_switch_cache_from_itaot(switch_id: str, switch_blob: dict, channels_with_ids: list[tuple[str, str]]) -> None:
            """
            Seed initial switch state cache from /itaot metadata when available.
            This updates only in-memory cache; no synthetic sw_events writes.
            """
            try:
                if not switch_id:
                    return

                by_label: dict[str, str] = {}
                by_channel_lc: dict[str, str] = {}
                for lbl, ch in (channels_with_ids or []):
                    label = str(lbl or "").strip()
                    ch_id = str(ch or "").strip()
                    if not label or not ch_id:
                        continue
                    by_label[label.lower()] = ch_id
                    by_channel_lc[ch_id.lower()] = ch_id

                switch_blk = self._safe_get_switch_block(switch_blob)
                idx_to_channel: dict[int, str] = {}
                if isinstance(switch_blk, dict):
                    for idx in range(1, 33):
                        ch_id = str(switch_blk.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                        if ch_id:
                            idx_to_channel[idx] = ch_id
                if isinstance(switch_blob, dict):
                    for idx in range(1, 33):
                        ch_id = str(switch_blob.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                        if ch_id:
                            idx_to_channel.setdefault(idx, ch_id)

                seeded: dict[str, bool] = {}

                # Indexed fields (preferred): SWITCH_n_LAST_STATE / SWITCH_n_STATE
                if isinstance(switch_blk, dict):
                    for idx, ch_id in idx_to_channel.items():
                        val = switch_blk.get(f"SWITCH_{idx}_LAST_STATE", None)
                        if val is None:
                            val = switch_blk.get(f"SWITCH_{idx}_STATE", None)
                        st = _coerce_switch_state(val)
                        if st is not None:
                            seeded[ch_id] = st

                # Flat top-level fields (common in Nodus single-payload /itaot):
                # SWITCH_n_LAST_STATE / SWITCH_n_STATE alongside SWITCH_n_CHANNEL_ID.
                if isinstance(switch_blob, dict):
                    for idx, ch_id in idx_to_channel.items():
                        val = switch_blob.get(f"SWITCH_{idx}_LAST_STATE", None)
                        if val is None:
                            val = switch_blob.get(f"SWITCH_{idx}_STATE", None)
                        st = _coerce_switch_state(val)
                        if st is not None:
                            seeded[ch_id] = st

                # Mapping payloads (tolerant of Nodus shape variants)
                map_candidates: list[dict] = []
                for key in ("state", "switch_state", "switch_states", "last_state", "last_states", "event"):
                    obj = switch_blob.get(key) if isinstance(switch_blob, dict) else None
                    if isinstance(obj, dict):
                        map_candidates.append(obj)

                for mp in map_candidates:
                    for raw_key, raw_val in mp.items():
                        st = _coerce_switch_state(raw_val)
                        if st is None:
                            continue
                        k = str(raw_key or "").strip()
                        if not k:
                            continue
                        kl = k.lower()
                        target_channel = None

                        if kl in by_channel_lc:
                            target_channel = by_channel_lc[kl]
                        elif kl in by_label:
                            target_channel = by_label[kl]
                        else:
                            m = re.fullmatch(r"SWITCH_(\d+)", k, flags=re.IGNORECASE)
                            if m:
                                try:
                                    idx = int(m.group(1))
                                    target_channel = idx_to_channel.get(idx)
                                except Exception:
                                    target_channel = None
                        if target_channel:
                            seeded[target_channel] = st

                # Single-value fallback for single-channel devices
                if not seeded and len(channels_with_ids) == 1 and isinstance(switch_blob, dict):
                    only_ch = str(channels_with_ids[0][1] or "").strip()
                    if only_ch:
                        for key in ("state", "switch_state", "last_state"):
                            st = _coerce_switch_state(switch_blob.get(key))
                            if st is not None:
                                seeded[only_ch] = st
                                break

                if not seeded:
                    return

                cache = self._switch_state_cache.setdefault(str(switch_id), {})
                for label, ch_id in (channels_with_ids or []):
                    ch = str(ch_id or "").strip()
                    if not ch or ch not in seeded:
                        continue
                    st_txt = "on" if seeded[ch] else "off"
                    cache[ch] = st_txt
                    lbl = str(label or "").strip()
                    if lbl:
                        cache[lbl] = st_txt
                self._known_switch_ids.add(str(switch_id))
            except Exception as e:
                if DEBUG:
                    printDM(f"[onboard] switch cache seed failed for {switch_id}: {e}", location=MODULE)

        try:
            ip_from_itaot = _extract_ipv4addr(info)
            if ip_from_itaot:
                if not hasattr(self, "_host_ipv4addr"):
                    self._host_ipv4addr = {}
                self._host_ipv4addr[base] = ip_from_itaot
        except Exception:
            pass

        # ---------- Pi multi-sensor schema ----------
        if is_pi_multi:
            for entry in info["sensors"]:
                try:
                    dev_id       = entry.get("SENSOR_ID")
                    topic        = entry.get("mqtt_sensor_topic")
                    location     = entry.get("LOCATION", "Unknown")
                    device_type  = entry.get("TYPE", "pi")
                    display_list = entry.get("display_metrics", []) or entry.get("metrics", [])
                    metrics = self._normalize_display_metrics(display_list)
                    if dev_id and metrics:
                        self.expected_gauge_map[dev_id] = metrics

                    if topic and dev_id:
                        peer_ids_for_host.append(dev_id)
                        self.nodus_sensor_topics[dev_id] = topic
                        self.device_location[topic] = location
                        self.topic_dev_id_map[topic] = dev_id
                        self.device_type[dev_id] = device_type
                        self.data_logger.register_sensor(dev_id)
                        discovered_sensors.append({
                            "sensor_id": dev_id,
                            "device_type": device_type,
                            "device": entry.get("DEVICE") or entry.get("device") or entry.get("SENSOR_DEVICE") or "",
                            "sensor_type": entry.get("TYPE") or entry.get("type") or device_type,
                            "location": location,
                            "serial": entry.get("SERIAL_NUM", ""),
                            "display_metrics": metrics,
                        })
                        if topic not in self.registered_topics:
                            self.registered_topics.add(topic)
                            self.client.subscribe(topic)
                            subscribed = True
                            itaot_valid = True
                            if DEBUG:
                                printDM(f"Subscribed to sensor topic: {topic} ({location}) [{device_type} {metrics}]",
                                        location=MODULE)
                except Exception as e:
                    printDM(f"[onboard] multi-sensor entry parse failed: {e}", location=MODULE)

        # ---------- Switch descriptors (array) ----------
        switches_block = info.get("switches")
        if isinstance(switches_block, list):
            for sw in switches_block:
                try:
                    _switch_id       = sw.get("SWITCH_DEVICE_ID")
                    _switch_location = _resolve_switch_location(sw)

                    event_topics  = sw.get("mqtt_switch_topics") or {}
                    state_topics  = sw.get("mqtt_switch_state_topics") or {}
                    command_topics = sw.get("mqtt_switch_command_topics") or {}
                    single_topic = sw.get("mqtt_switch_topic")
                    if single_topic and not (event_topics or state_topics or command_topics):
                        # Simple single-topic schema (e.g., "switch/<id>/<channel_id>")
                        event_topics = {"event": single_topic}

                    if not (_switch_id and (event_topics or state_topics or command_topics)):
                        continue

                    peer_ids_for_host.append(_switch_id)
                    discovered_switches.append({
                        "switch_id": _switch_id,
                        "switch_location": _switch_location,
                        "channels": 0,
                        "switch_payload": sw,
                        "switch_type": sw.get("TYPE") or sw.get("type") or "",
                        "serial": sw.get("DEVICE_SERIAL_NUM") or "",
                    })

                    # --- derive (label, channel_id) pairs ---
                    channels_with_ids: list[tuple[str, str]] = []
                    try:
                        channels_with_ids = self._extract_switch_channels(sw)
                    except Exception:
                        channels_with_ids = []

                    # Fallback: use provided 'channels' list (labels only, no IDs)
                    if not channels_with_ids:
                        _channels = sw.get("channels") or []
                        for lbl in _channels:
                            if isinstance(lbl, str) and lbl.strip():
                                # channel_id will be inferred from topic strings
                                channels_with_ids.append((lbl.strip(), ""))
                    if channels_with_ids:
                        discovered_switches[-1]["channels"] = len(channels_with_ids)

                    # --- register identities using IDs when available ---
                    try:
                        for label_str, channel_id in channels_with_ids:
                            if not label_str:
                                continue

                            if channel_id:
                                _switch_key = build_switch_key(channel_id, label_str)
                            else:
                                continue

                            self.data_logger.upsert_switch_identity(
                                switch_key=_switch_key,
                                switch_id=_switch_id,
                                label=label_str,
                                location=_switch_location,
                            )
                            if channel_id:
                                self.nodus_label_to_channel[(str(_switch_id), _norm_label(label_str))] = channel_id
                    except Exception as e:
                        printDM(f"[onboard] upsert_switch_identity failed for {_switch_id}: {e}", location=MODULE)

                    label_by_channel = {ch_id: lbl for (lbl, ch_id) in channels_with_ids if ch_id}
                    new = self._register_nodus_switch_topics(
                        _switch_id,
                        _switch_location,
                        event_topics=event_topics,
                        state_topics=state_topics,
                        command_topics=command_topics,
                        label_by_channel_id=label_by_channel,
                    )
                    _seed_switch_cache_from_itaot(_switch_id, sw, channels_with_ids)
                    subscribed = subscribed or new
                    itaot_valid = True

                except Exception as e:
                    printDM(f"[onboard] switch entry parse failed: {e}", location=MODULE)

        # ---------- Legacy flat switch fields (back-compat) ----------
        try:
            _switch_id_flat         = info.get("SWITCH_DEVICE_ID")
            _switch_location_flat   = _resolve_switch_location(info)
            _switch_event_map_flat  = info.get("mqtt_switch_topics") or {}
            _switch_state_map_flat  = info.get("mqtt_switch_state_topics") or {}
            _switch_cmd_map_flat    = info.get("mqtt_switch_command_topics") or {}
            if _switch_id_flat and (_switch_event_map_flat or _switch_state_map_flat or _switch_cmd_map_flat):
                peer_ids_for_host.append(_switch_id_flat)
                discovered_switches.append({
                    "switch_id": _switch_id_flat,
                    "switch_location": _switch_location_flat,
                    "channels": 0,
                    "switch_payload": info,
                    "switch_type": info.get("TYPE") or info.get("type") or "",
                    "serial": info.get("DEVICE_SERIAL_NUM") or "",
                })
                channels_with_ids = []
                try:
                    channels_with_ids = self._extract_switch_channels(info)
                    for lbl, channel_id in channels_with_ids:
                        if not lbl:
                            continue

                        if channel_id:
                            _switch_key = build_switch_key(channel_id, lbl)
                        else:
                            continue

                        self.data_logger.upsert_switch_identity(
                            switch_key=_switch_key,
                            switch_id=_switch_id_flat,
                            label=lbl,
                            location=_switch_location_flat,
                        )
                        if channel_id:
                            self.nodus_label_to_channel[(str(_switch_id_flat), _norm_label(lbl))] = channel_id
                except Exception as e:
                    printDM(f"[onboard] switch channel parse failed: {e}", location=MODULE)
                if channels_with_ids:
                    discovered_switches[-1]["channels"] = len(channels_with_ids)

                label_by_channel = {ch_id: lbl for (lbl, ch_id) in (channels_with_ids or []) if ch_id}
                new = self._register_nodus_switch_topics(
                    _switch_id_flat,
                    _switch_location_flat,
                    event_topics=_switch_event_map_flat,
                    state_topics=_switch_state_map_flat,
                    command_topics=_switch_cmd_map_flat,
                    label_by_channel_id=label_by_channel,
                )
                _seed_switch_cache_from_itaot(_switch_id_flat, info, channels_with_ids)
                subscribed = subscribed or new
                itaot_valid = True
        except Exception as e:
            printDM(f"[onboard] legacy flat switch parse failed: {e}", location=MODULE)

        # ---------- Pico2 W single-sensor schema ----------
        if not is_pi_multi:
            try:
                dev_id       = info.get("SENSOR_ID")
                topic        = info.get("mqtt_sensor_topic")
                location     = info.get("LOCATION", "Unknown")
                device_type  = info.get("TYPE", "picow")
                display_list = info.get("display_metrics", []) or info.get("metrics", [])
                metrics = self._normalize_display_metrics(display_list)
                if dev_id and metrics:
                    self.expected_gauge_map[dev_id] = metrics

                if topic and dev_id:
                    peer_ids_for_host.append(dev_id)
                    self.nodus_sensor_topics[dev_id] = topic
                    self.device_location[topic] = location
                    self.topic_dev_id_map[topic] = dev_id
                    self.device_type[dev_id] = device_type
                    self.data_logger.register_sensor(dev_id)
                    discovered_sensors.append({
                        "sensor_id": dev_id,
                        "device_type": device_type,
                        "device": info.get("DEVICE") or info.get("device") or "",
                        "sensor_type": info.get("TYPE") or info.get("type") or device_type,
                        "location": location,
                        "serial": info.get("SERIAL_NUM", ""),
                        "display_metrics": metrics,
                    })
                    if topic not in self.registered_topics:
                        self.registered_topics.add(topic)
                        self.client.subscribe(topic)
                        subscribed = True
                        itaot_valid = True
                        if DEBUG:
                            printDM(f"Subscribed to sensor topic: {topic} ({location}) [{device_type} {metrics}]",
                                    location=MODULE)
            except Exception as e:
                printDM(f"[onboard] Pico2 W single-sensor parse failed: {e}", location=MODULE)

        # ---------- finalize host status ----------
        if itaot_valid:
            self._mark_host_status(base, "online")
            if base in self.discovery_failures:
                del self.discovery_failures[base]
            if peer_ids_for_host:
                self.host_to_peer_ids[base] = peer_ids_for_host
            # Nudge dashboard clients to re-evaluate layout immediately when
            # discovery adds switch/sensor metadata, instead of waiting for poll.
            if subscribed:
                try:
                    import saiWebRoutes as routes
                    switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
                    if switch_broadcast:
                        self._schedule_coro(switch_broadcast({
                            "type": "switch_inventory_changed",
                            "host": base,
                            "timestamp": get_timestamp(),
                        }))
                except Exception:
                    pass

        # ---------- ensure settings files from itaot ----------
        try:
            if discovered_sensors or discovered_switches or info:
                self._ensure_settings_from_itaot(info, hostname, discovered_sensors, discovered_switches)
        except Exception as e:
            printDM(f"[onboard] settings seed failed: {e}", location=MODULE)

        # cache last payload for host
        try:
            self.discovery_cache[base] = info
        except Exception:
            pass

        return itaot_valid, subscribed

    def _ensure_settings_from_itaot(
        self,
        info: dict,
        hostname: str,
        sensors: list[dict],
        switches: list[dict],
    ) -> None:
        """
        Ensure sensor/switch/system settings files exist and reflect metadata
        for devices described in /itaot or retained nodus meta.
        """
        from pathlib import Path
        try:
            from saiSensorSettingsManager import SensorSettingsManager
            from saiSwitchSettingsManager import SwitchSettingsManager
            from saiSettings import saiSettings
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] import error: {exc}", location=MODULE)
            return

        def _strip_local(host: str) -> str:
            host = (host or "").strip()
            return host[:-6] if host.endswith(".local") else host

        def _is_soil_device(name: str) -> bool:
            dn = (name or "").strip().lower()
            return dn.startswith("soil") or dn in {"soil", "soil4in1", "rs485", "modbus"}

        def _parse_simple_toml(path: Path) -> OrderedDict:
            if tomllib:
                try:
                    with path.open("rb") as f:
                        data = tomllib.load(f) or {}
                    return SensorSettingsManager._to_ordered(data)
                except Exception:
                    pass
            settings = OrderedDict()
            section = None
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("[") and line.endswith("]"):
                            section = line[1:-1]
                            settings[section] = OrderedDict()
                        elif "=" in line and section:
                            key, value = map(str.strip, line.split("=", 1))
                            if value.startswith('[') and value.endswith(']'):
                                value = json.loads(value.replace("'", '"'))
                            elif value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            else:
                                lv = value.lower()
                                if lv == "true":
                                    value = True
                                elif lv == "false":
                                    value = False
                                else:
                                    try:
                                        value = float(value) if "." in value else int(value)
                                    except Exception:
                                        pass
                            settings[section][key] = value
            except Exception as exc:
                if DEBUG:
                    printDM(f"[itaot-settings] parse error for {path}: {exc}", location=MODULE)
            return settings

        def _emit_simple_toml(path: Path, settings: OrderedDict) -> None:
            def _toml_escape(v):
                if isinstance(v, bool):
                    return "true" if v else "false"
                if isinstance(v, (int, float)):
                    return f"{v}"
                if isinstance(v, list):
                    return json.dumps(v)
                s = "" if v is None else str(v)
                s = s.replace("\\", "\\\\").replace('"', '\\"')
                return f"\"{s}\""

            lines = []
            for section, pairs in (settings or {}).items():
                lines.append(f"[{section}]\n")
                for key, value in (pairs or {}).items():
                    lines.append(f"{key} = {_toml_escape(value)}\n")
                lines.append("\n")
            text = "".join(lines)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(path)

        def _display_defaults_for_device(device: str) -> list[str]:
            base_device = (device or "").split("_", 1)[0].lower()
            mapping: dict[str, list[str]] = {
                "apvpd": ["Ambient VPD", "Temperature", "Rel-Humidity", "Plant VPD", "Plant Temperature", "Plant Rel-Humidity"],
                "aqi":   ["Air Quality", "Gas", "Temperature", "Rel-Humidity", "Ambient VPD", "Baro-Pressure"],
                "avpd":  ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure", "", ""],
                "co2":   ["CO2", "Temperature", "Rel-Humidity", "Ambient VPD", "", ""],
                "veml":  ["PPFD", "DLI", "Light Intensity", "Lux", "", ""],
                "soil":  ["Soil-Moisture", "Soil-Temp", "Soil-pH", "Soil-EC", "", ""],
            }
            return mapping.get(base_device, ["", "", "", "", "", ""])

        def _display_block_is_blank(display: dict | None) -> bool:
            if not isinstance(display, dict):
                return True
            return not any(str(display.get(f"METRIC_{idx}", "")).strip() for idx in range(1, 7))

        def _set_if_present(section: OrderedDict, key: str, value, *, allow_blank: bool = False) -> bool:
            if not isinstance(section, dict):
                return False
            if value is None:
                return False
            if isinstance(value, str):
                if not allow_blank and not value.strip():
                    return False
                value = value.strip() if not allow_blank else value
            if section.get(key) == value:
                return False
            section[key] = value
            return True

        # ---- system_settings/<HOSTNAME>/settings.toml ----
        system_id = _strip_local(str((info or {}).get("HOSTNAME") or hostname or ""))
        if system_id:
            sys_path = Path(saiSettings.DEFAULT_BASE_DIR) / system_id / saiSettings.STANDARD_FILENAME
            existed_before = sys_path.exists()
            nodus_tpl = Path(saiSettings.DEFAULT_BASE_DIR) / "factory_nodus" / f"{saiSettings.STANDARD_FILENAME}.def"
            fallback_tpl = Path(saiSettings.DEFAULT_BASE_DIR) / "factory" / saiSettings.STANDARD_FILENAME
            tpl_path = nodus_tpl if nodus_tpl.exists() else (fallback_tpl if fallback_tpl.exists() else None)

            settings_doc = _parse_simple_toml(sys_path) if existed_before else (_parse_simple_toml(tpl_path) if tpl_path else OrderedDict())
            changed = not existed_before
            for block_name in ("Network", "Profile", "MQTT", "HomeAssistant", "Time"):
                if block_name not in settings_doc or not isinstance(settings_doc.get(block_name), dict):
                    settings_doc[block_name] = OrderedDict()
                    changed = True

            net_block = info.get("Network") if isinstance(info, dict) else None
            if isinstance(net_block, dict):
                for k, v in net_block.items():
                    changed = _set_if_present(settings_doc["Network"], str(k), v, allow_blank=str(k).upper() in {"PASSWORD"}) or changed
            changed = _set_if_present(settings_doc["Network"], "HOSTNAME", system_id) or changed

            profile_block = info.get("Profile") if isinstance(info, dict) else None
            if isinstance(profile_block, dict):
                for k, v in profile_block.items():
                    changed = _set_if_present(settings_doc["Profile"], str(k), v) or changed

            mqtt_block = info.get("MQTT") if isinstance(info, dict) else None
            if isinstance(mqtt_block, dict):
                for k, v in mqtt_block.items():
                    changed = _set_if_present(settings_doc["MQTT"], str(k), v, allow_blank=str(k).upper() in {"USERNAME", "PASSWORD"}) or changed

            ha_block = info.get("HomeAssistant") if isinstance(info, dict) else None
            if isinstance(ha_block, dict):
                for k, v in ha_block.items():
                    changed = _set_if_present(settings_doc["HomeAssistant"], str(k), v) or changed

            time_block = info.get("Time") if isinstance(info, dict) else None
            if isinstance(time_block, dict):
                for k, v in time_block.items():
                    changed = _set_if_present(settings_doc["Time"], str(k), v) or changed

            if changed:
                _emit_simple_toml(sys_path, settings_doc)
                if DEBUG:
                    verb = "updated" if existed_before else "created"
                    printDM(f"[itaot-settings] {verb} system settings for {system_id}", location=MODULE)

        # ---- sensor_settings/<SENSOR_ID>/sensor.toml ----
        try:
            sensor_mgr = SensorSettingsManager()
            for s in (sensors or []):
                sensor_id = str(s.get("sensor_id") or "").strip()
                if not sensor_id:
                    continue
                device_type = (s.get("device_type") or "picow")
                device_name = self._infer_sensor_device_name(
                    s.get("device") or s.get("sensor_type"),
                    sensor_id,
                )
                location = (s.get("location") or "Unknown")
                serial = (s.get("serial") or "")
                remote_display_metrics = self._normalize_display_metrics(
                    s.get("display_metrics") or s.get("metrics")
                )
                new_path, legacy_path = sensor_mgr.get_candidate_paths(sensor_id)

                existing_path = new_path if new_path.exists() else (legacy_path if legacy_path.exists() else None)
                if existing_path:
                    try:
                        data = sensor_mgr.load(sensor_id)
                    except Exception:
                        data = OrderedDict()
                    changed = False

                    if "Sensor" not in data or not isinstance(data["Sensor"], dict):
                        data["Sensor"] = OrderedDict()
                    sb = data["Sensor"]
                    if device_name and str(sb.get("DEVICE", "") or "").strip() != device_name:
                        sb["DEVICE"] = device_name
                        changed = True
                    if serial and str(sb.get("SERIAL_NUM", "") or "").strip() != serial:
                        sb["SERIAL_NUM"] = serial
                        changed = True
                    if location and location.strip() and str(sb.get("LOCATION", "") or "").strip() != location:
                        sb["LOCATION"] = location
                        changed = True
                    if sensor_id and str(sb.get("SENSOR_ID", "") or "").strip() != sensor_id:
                        sb["SENSOR_ID"] = sensor_id
                        changed = True
                    if device_type and str(sb.get("TYPE", "") or "").strip() != device_type:
                        sb["TYPE"] = device_type
                        changed = True

                    if "Display" not in data or not isinstance(data["Display"], dict):
                        data["Display"] = OrderedDict()
                    display = data["Display"]
                    if remote_display_metrics:
                        for idx in range(6):
                            metric_key = f"METRIC_{idx + 1}"
                            metric_val = remote_display_metrics[idx] if idx < len(remote_display_metrics) else ""
                            if str(display.get(metric_key, "") or "") != metric_val:
                                display[metric_key] = metric_val
                                changed = True

                    if changed:
                        sensor_mgr.save(sensor_id, data)
                        if DEBUG:
                            printDM(f"[itaot-settings] updated existing sensor settings for {sensor_id}", location=MODULE)
                    continue

                nodus_dir = sensor_mgr.base_dir / "factory_nodus"
                tpl_soil = nodus_dir / "sensor_soil.toml.def"
                tpl_i2c = nodus_dir / "sensor_i2c.toml.def"
                use_soil = _is_soil_device(device_name) or _is_soil_device(sensor_id) or _is_soil_device(device_type)

                tpl_path = tpl_soil if (use_soil and tpl_soil.exists()) else (tpl_i2c if tpl_i2c.exists() else None)
                if tpl_path:
                    data = sensor_mgr._parse_toml_from_disk(tpl_path)
                    if "Sensor" not in data or not isinstance(data["Sensor"], dict):
                        data["Sensor"] = OrderedDict()
                    sb = data["Sensor"]
                    if device_name:
                        sb["DEVICE"] = device_name
                    if device_type:
                        sb["TYPE"] = device_type
                    sb["SENSOR_ID"] = sensor_id
                    sb["LOCATION"] = location
                    if serial:
                        sb["SERIAL_NUM"] = serial

                    if "Display" not in data or not isinstance(data["Display"], dict):
                        data["Display"] = OrderedDict()
                    display = data["Display"]
                    chosen_metrics = remote_display_metrics or _display_defaults_for_device(device_name or device_type)
                    for idx in range(6):
                        display[f"METRIC_{idx + 1}"] = chosen_metrics[idx] if idx < len(chosen_metrics) else ""

                    sensor_mgr.save(sensor_id, data)
                else:
                    sensor_mgr.seed_from_factory(sensor_id, device_name or device_type, location, serial_num=serial)
                if DEBUG:
                    printDM(f"[itaot-settings] seeded sensor settings for {sensor_id}", location=MODULE)
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] sensor seed error: {exc}", location=MODULE)

        # ---- switch_settings/<SWITCH_DEVICE_ID>/switch.toml ----
        try:
            switch_mgr = SwitchSettingsManager()
            for sw in (switches or []):
                switch_id = str(sw.get("switch_id") or "").strip()
                if not switch_id:
                    continue
                sw_path = switch_mgr.get_path(switch_id)
                switch_loc = sw.get("switch_location") or "Unknown"
                switch_type = (sw.get("switch_type") or "").strip()
                switch_serial = (sw.get("serial") or "").strip()
                switch_payload = sw.get("switch_payload") if isinstance(sw, dict) else None

                nodus_dir = switch_mgr.base_dir / "factory_nodus"
                tpl_path = nodus_dir / "switch.toml.def"
                if sw_path.exists():
                    doc = switch_mgr.load(switch_id)
                elif tpl_path.exists():
                    doc = switch_mgr._parse_toml_from_disk(tpl_path)
                else:
                    switch_mgr.ensure_host_switch(switch_id, switch_loc=switch_loc)
                    doc = switch_mgr.load(switch_id)

                changed = False
                if "Switch" not in doc or not isinstance(doc["Switch"], dict):
                    doc["Switch"] = OrderedDict()
                    changed = True
                sb = doc["Switch"]
                if str(sb.get("DEVICE", "") or "").strip() != "switch":
                    sb["DEVICE"] = "switch"
                    changed = True
                if str(sb.get("SWITCH_DEVICE_ID", "") or "").strip() != switch_id:
                    sb["SWITCH_DEVICE_ID"] = switch_id
                    changed = True
                if switch_loc and str(sb.get("SWITCH_LOCATION", "") or "").strip() != switch_loc:
                    sb["SWITCH_LOCATION"] = switch_loc
                    changed = True
                if switch_type and str(sb.get("TYPE", "") or "").strip() != switch_type:
                    sb["TYPE"] = switch_type
                    changed = True
                if switch_serial and str(sb.get("DEVICE_SERIAL_NUM", "") or "").strip() != switch_serial:
                    sb["DEVICE_SERIAL_NUM"] = switch_serial
                    changed = True
                # Overlay indexed switch fields from metadata so rendering tracks
                # authoritative channel IDs, labels, last state, and mqtt wiring.
                try:
                    src = {}
                    if isinstance(switch_payload, dict):
                        src = switch_payload.get("Switch") if isinstance(switch_payload.get("Switch"), dict) else switch_payload
                    if isinstance(src, dict):
                        incoming_indices: set[int] = set()
                        for k, v in src.items():
                            ks = str(k or "")
                            match = re.fullmatch(r"SWITCH_(\d+)_(.+)", ks)
                            if match:
                                incoming_indices.add(int(match.group(1)))
                            if not ks.startswith("SWITCH_") or ks == "SWITCH_DEVICE_ID":
                                continue

                            existing_val = sb.get(ks)
                            incoming_text = str(v or "").strip()
                            existing_text = str(existing_val or "").strip()

                            # Preserve real Nodus hardware wiring when metadata only
                            # provides shadow MQTT install markers or blank pins.
                            if ks.endswith("_PIN") and incoming_text == "":
                                continue
                            if (
                                ks.endswith("_ENABLE_PIN")
                                and incoming_text.lower() == "mqtt"
                                and existing_text
                                and existing_text.lower() != "mqtt"
                            ):
                                continue

                            if existing_val != v:
                                sb[ks] = v
                                changed = True

                        if incoming_indices:
                            for existing_key in list(sb.keys()):
                                match = re.fullmatch(r"SWITCH_(\d+)_(.+)", str(existing_key or ""))
                                if not match:
                                    continue
                                existing_idx = int(match.group(1))
                                existing_suffix = match.group(2)
                                if existing_idx not in incoming_indices:
                                    sb.pop(existing_key, None)
                                    changed = True
                                    continue
                                if existing_suffix == "EN" and f"SWITCH_{existing_idx}_EN" not in src:
                                    sb.pop(existing_key, None)
                                    changed = True
                except Exception:
                    pass
                try:
                    switch_mgr._ensure_channel_ids(sb)
                except Exception:
                    pass
                if changed:
                    switch_mgr.save(switch_id, doc)

                if DEBUG:
                    action = "updated" if sw_path.exists() else "seeded"
                    printDM(f"[itaot-settings] {action} switch settings for {switch_id}", location=MODULE)
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] switch seed error: {exc}", location=MODULE)

    async def force_refresh_device_metadata(self, sensor_or_host: str, *, port: int = 8000, timeout_sec: float = 6.0) -> bool:
        """
        Immediately GET /itaot-meta from the given device (sensor_id or hostname),
        update expected_gauge_map, topics, and host status using the same path as discovery.
        Returns True if refreshed OK.
        """
        hostname = self._resolve_hostname_for(sensor_or_host)
        if not hostname:
            if DEBUG:
                printDM("[force_refresh] could not resolve hostname", location=MODULE)
            return False

        url = f"http://{hostname}:{port}/itaot-meta?t={int(time.time())}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=timeout_sec, write=2.0, pool=2.0)) as client:
                resp = await client.get(url, headers={"Accept": "application/json"})
                if resp.status_code != 200:
                    if DEBUG:
                        printDM(f"[force_refresh] {hostname} returned {resp.status_code}: {resp.text[:200]}", location=MODULE)
                    return False
                try:
                    info = resp.json()
                except Exception:
                    # salvage once if needed
                    txt = resp.text
                    s = txt.find("{"); e = txt.rfind("}")
                    info = json.loads(txt[s:e+1]) if (s != -1 and e > s) else {}
        except Exception as exc:
            if DEBUG:
                printDM(f"[force_refresh] error for {hostname}: {exc}", location=MODULE)
            return False

        ok, _ = self._parse_and_subscribe_from_http_meta(info, hostname)
        if ok and DEBUG:
            printDM(f"[force_refresh] updated metadata for {hostname}", location=MODULE)
        return ok

    def _ensure_ha_switch_channel(self, switch_id: str, channel_id: str, *, label_override: str | None = None) -> None:
        if not self.topic_map or not switch_id or not channel_id:
            return

        key = f"{switch_id}::{channel_id}"
        if key in self._ha_discovered_switch_channels:
            return

        # Resolve a friendly label if you can; else channel_id
        label = None
        try:
            # If you have a DB helper, use it; otherwise leave None
            label = getattr(self.data_logger, "get_switch_label", lambda _a, _b: None)(switch_id, channel_id)
        except Exception:
            label = None
        name = (label_override or label or channel_id)

        object_id = f"{switch_id}__{_slugify(name)}"
        unique_id = f"sensorius__{switch_id}__{channel_id}"

        state_topic = None
        cmd_topic = None
        if self.nodus_passthrough:
            state_topic = self.nodus_switch_state_topics.get((switch_id, channel_id))
            cmd_topic = self.nodus_switch_command_topics.get((switch_id, channel_id))

        disc_topic = self.topic_map.switch_discovery_topic(object_id)
        payload = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": state_topic or self.topic_map.switch_state_topic(switch_id, channel_id),
            "command_topic": cmd_topic or self.topic_map.switch_command_topic(switch_id, channel_id),
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": self.topic_map.switch_availability_topic(switch_id),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [f"sensorius:{switch_id}"],
                "name": switch_id,
                "manufacturer": "Sensorius",
                "model": "Nodus Switch",
            },
        }

        self.publish_json(disc_topic, payload, retain=True)

        # Subscribe to HA command topic once
        cmd_topic = cmd_topic or self.topic_map.switch_command_topic(switch_id, channel_id)
        self.subscribe(cmd_topic, self._on_ha_switch_command)

        self._ha_discovered_switch_channels.add(key)

    def _on_ha_switch_command(self, client, userdata, msg):
        topic = getattr(msg, "topic", "") or ""
        raw = getattr(msg, "payload", b"")
        try:
            txt = raw.decode("utf-8", errors="ignore").strip().upper()
        except Exception:
            txt = str(raw).strip().upper()

        if txt not in {"ON", "OFF"}:
            return

        desired_on = (txt == "ON")

        # topic: sensorius/switch/<switch_id>/<channel_id>/set
        parts = topic.split("/")
        if len(parts) >= 5 and parts[0] == self.base_topic and parts[1] == "switch":
            switch_id  = parts[2]
            channel_id = parts[3]
            ok = self.set_switch_by_channel_id(switch_id, channel_id, desired_on)
            if DEBUG and not ok:
                printDM(f"[HA cmd] forward failed {switch_id}/{channel_id} -> {txt}", location=MODULE)
            return

        # topic: nodus/<channel_id>/set (passthrough)
        if len(parts) >= 3 and parts[0] == "nodus" and parts[-1] == "set":
            channel_id = parts[1]
            switch_id = None
            try:
                for (sid, ch), cmd_topic in self.nodus_switch_command_topics.items():
                    if ch == channel_id and cmd_topic == topic:
                        switch_id = sid
                        break
            except Exception:
                switch_id = None

            ok = self.set_switch_by_channel_id(switch_id or "", channel_id, desired_on)
            if DEBUG and not ok:
                printDM(f"[HA cmd] forward failed nodus/{channel_id}/set -> {txt}", location=MODULE)
            return
        

    def set_switch(self, switch_id: str, channel_label: str, new_state: bool, qos: int = 0, retain: bool = False) -> bool:
        """
        Publish a command to a remote switch channel using channel-id topics.
        Returns True if publish was queued with rc==0.
        """
        try:
            if not getattr(self, "client", None):
                printDM("[set_switch] No MQTT client on ingest", location=MODULE)
                return False

            # Prefer Nodus command topic if we can resolve channel_id
            channel_id = None
            try:
                channel_id = getattr(self.data_logger, "get_switch_channel_id", lambda _a, _b: None)(switch_id, channel_label)
            except Exception:
                channel_id = None

            if not channel_id:
                label_norm = _norm_label(channel_label)
                channel_id = self.nodus_label_to_channel.get((str(switch_id), label_norm))

            # Fallback: derive channel_id from DB switch_ids mapping
            if not channel_id:
                try:
                    target_sid = str(switch_id or "").strip().lower()
                    target_label = str(channel_label or "").strip().lower()
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "")).strip().lower()
                        rlab = str(row.get("label", "")).strip().lower()
                        if rsid == target_sid and rlab == target_label:
                            sk = str(row.get("switch_key", "")).strip()
                            if "::" in sk:
                                channel_id = sk.split("::", 1)[0].strip()
                                break
                except Exception:
                    channel_id = None

            if not channel_id:
                printDM(f"[set_switch] No channel_id for {switch_id}::{channel_label}", location=MODULE)
                return False

            topic = (self.nodus_switch_command_topics.get((switch_id, channel_id))
                     or self.nodus_channel_command_topics.get(channel_id)
                     or f"nodus/{channel_id}/set")
            payload = "ON" if new_state else "OFF"
            if DEBUG:
                printDM(f"[set_switch] computed new_state={new_state} topic={topic}", location=MODULE)

            info = self.client.publish(topic, payload, qos=qos, retain=retain)
            # paho >=1.6 returns MQTTMessageInfo; accept rc==0 as success
            rc = getattr(info, "rc", 0) if info is not None else 0
            ok = (rc == 0)
            if ok:
                self._pending_set[(str(switch_id), str(channel_label))] = time.time()
                try:
                    switch_id_str = str(switch_id)
                    channel_id_str = str(channel_id)
                    state_txt = "on" if new_state else "off"
                    cache = self._switch_state_cache.setdefault(switch_id_str, {})
                    cache[channel_id_str] = state_txt
                    cache[str(channel_label)] = state_txt
                    try:
                        target_sid = switch_id_str.strip().lower()
                        target_ch = channel_id_str.strip().lower()
                        for row in (self.data_logger.get_switch_identities() or []):
                            rsid = str(row.get("switch_id", "") or "").strip().lower()
                            rch = str(row.get("channel_id", "") or "").strip().lower()
                            rlab = str(row.get("label", "") or "").strip()
                            if rsid == target_sid and rch == target_ch and rlab:
                                cache[rlab] = state_txt
                    except Exception:
                        pass
                except Exception:
                    pass
                if DEBUG:
                    printDM(f"[set_switch] → {topic} {payload}", location=MODULE)
            else:
                printDM(f"[set_switch] publish rc={rc} for {topic}", location=MODULE)
            return ok
        except Exception as e:
            printDM(f"[set_switch] error: {e}", location=MODULE)
            return False

    def toggle_switch(self, switch_id: str, channel_label: str, default_on: bool = True) -> bool:
        """
        Toggle using last cached state; if unknown, uses default_on.
        """
        cache = self._switch_state_cache.get(str(switch_id), {})
        last = str(cache.get(str(channel_label), "")).lower()
        new_state = default_on if last not in ("on","off") else (last != "on")
        return self.set_switch(switch_id, channel_label, new_state)

    # extend signature + use lineage if provided
    def _maybe_persist_switch_event(
        self,
        switch_id: str,
        channel_id: str,
        is_on: bool,
        ts_iso: str | None,
        source: str,
        sensor_lineage: str | None = None,
        force_write: bool = False,
    ):
        """
        Persist a switch event *only if* it represents a state change relative to cache.

        Notes:
          - channel_id is the stable per-channel identifier (e.g. SWITCH_1_CHANNEL_ID = "S1-123456").
          - The canonical DB key is build_switch_key(channel_id, label) => "<channel_id>::<label>".
          - sensor_lineage lets us store the specific origin (e.g., "switch-oqs3lr-GP28") when known.
        """
        try:
            if not switch_id or not channel_id:
                return

            switch_id_str  = str(switch_id)
            channel_id_str = str(channel_id)

            last_cache = self._switch_state_cache.setdefault(switch_id_str, {})
            new_state  = "on" if is_on else "off"
            state_key = (switch_id_str, channel_id_str)

            last_state = str(self._last_persisted_switch_state.get(state_key, "")).lower()
            if last_state not in ("on", "off"):
                label_resolved = None
                try:
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "") or "").strip()
                        rch = str(row.get("channel_id", "") or "").strip()
                        rlab = str(row.get("label", "") or "").strip()
                        if rsid == switch_id_str and rch == channel_id_str and rlab:
                            label_resolved = rlab
                            break
                except Exception:
                    label_resolved = None
                if not label_resolved:
                    label_resolved = channel_id_str
                try:
                    latest = self.data_logger.get_latest_switch_state(
                        build_switch_key(channel_id_str, label_resolved),
                        sensor_id=(sensor_lineage or switch_id_str),
                    )
                    if latest is not None:
                        last_state = "on" if str(latest).strip().lower() == "on" else "off"
                except Exception:
                    pass

            if (not force_write) and last_state == new_state:
                if DEBUG:
                    printDM(
                        f"[dedupe] {switch_id_str}::{channel_id_str} unchanged ({new_state}) — skip DB",
                        location=MODULE,
                    )
                return

            # update cache
            last_cache[channel_id_str] = new_state
            self._known_switch_ids.add(switch_id_str)

            label_resolved = None
            try:
                for row in (self.data_logger.get_switch_identities() or []):
                    rsid = str(row.get("switch_id", "") or "").strip()
                    rch = str(row.get("channel_id", "") or "").strip()
                    rlab = str(row.get("label", "") or "").strip()
                    if rsid == switch_id_str and rch == channel_id_str and rlab:
                        label_resolved = rlab
                        break
            except Exception:
                label_resolved = None
            if not label_resolved:
                label_resolved = channel_id_str

            writer = getattr(self.data_logger, "log_switch_event", None)
            if callable(writer):
                writer(
                    switch_key=build_switch_key(channel_id_str, label_resolved),
                    is_on=is_on,
                    timestamp=ts_iso,
                    sensor_id=(sensor_lineage or switch_id_str),
                    source=source,
                )
            else:
                ts = ts_iso
                if not ts:
                    try:
                        tz = getattr(self.data_logger, "local_tz", ZoneInfo("America/Denver"))
                    except Exception:
                        tz = ZoneInfo("America/Denver")
                    ts = datetime.now(tz).isoformat()
                # Legacy fallback: write as sensor reading under the channel_id metric
                self.data_logger.log_readings(
                    timestamp=ts,
                    sensor_id=(sensor_lineage or switch_id_str),
                    values={channel_id_str: 1 if is_on else 0},
                )

            self._last_persisted_switch_state[state_key] = new_state

            if DEBUG:
                printDM(
                    f"[persist] {switch_id_str}::{channel_id_str} -> {new_state} (src={source})",
                    location=MODULE,
                )

        except Exception as e:
            printDM(
                f"[persist] error for {switch_id}::{channel_id}: {e}",
                location=MODULE,
            )

    def handle_switch_event_slug(self, topic: str, payload: str):
        """
        New-style event topic (ID-based):
          topic:   "switch/<switch_id>/<channel_id>/event"
          payload: "ON" | "OFF"
        channel_id is the stable SWITCH_N_ID (e.g. "S1-123456").
        """
        try:
            parts = topic.split("/")
            if len(parts) < 4 or parts[0] != "switch" or parts[-1] != "event":
                return

            switch_id  = parts[1]
            channel_id = parts[2]  # do NOT .lower(); preserve canonical ID
            is_on = str(payload).strip().upper() == "ON"
            ts_iso = None  # let logger fill

            self._maybe_persist_switch_event(
                switch_id=switch_id,
                channel_id=channel_id,
                is_on=is_on,
                ts_iso=ts_iso,
                source="mqtt-slug",
                sensor_lineage=None,
            )
        except Exception as e:
            printDM(f"[handle_switch_event_slug] err: {e}", location=MODULE)

    # tweak debug in handle_switch_state_slug (optional)
    def handle_switch_state_slug(self, topic: str, payload: str):
        """
        New-style state topic (ID-based):
          topic:   "switch/<switch_id>/<channel_id>/state"
          payload: "ON" | "OFF"
        This updates the in-memory cache only (no DB writes).
        """
        try:
            parts = topic.split("/")
            if len(parts) < 4 or parts[0] != "switch" or parts[-1] != "state":
                return

            switch_id  = parts[1]
            channel_id = parts[2]  # canonical SWITCH_N_ID
            is_on = str(payload).strip().upper() == "ON"
            label = None
            try:
                label = getattr(self.data_logger, "get_switch_label", lambda _a, _b: None)(switch_id, channel_id)
            except Exception:
                label = None

            cache = self._switch_state_cache.setdefault(switch_id, {})
            cache[channel_id] = "on" if is_on else "off"
            if label:
                cache[label] = "on" if is_on else "off"
            self._known_switch_ids.add(switch_id)
            try:
                self.last_mqtt_seen[switch_id] = time.time()
            except Exception:
                pass

            if DEBUG:
                db_key = build_switch_key(channel_id, label or channel_id)
                printDM(
                    f"[state] {switch_id} [{channel_id}] -> {cache[channel_id]} db_key={db_key} (topic {topic})",
                    location=MODULE,
                )
        except Exception as e:
            printDM(f"[handle_switch_state_slug] err: {e}", location=MODULE)

    def handle_nodus_switch_topic(self, topic: str, payload: str):
        """
        Nodus state/event topics:
          topic:   "nodus/<channel_id>/state" or "nodus/<channel_id>/event"
          payload: "ON" | "OFF"  (legacy)
                   or JSON {"timestamp": <epoch|iso>, "event": {...}, "source": "..."}
        Uses /itaot-derived maps to resolve switch_id/channel_id/label.
        """
        try:
            def _labels_for_channel(sid: str, ch_id: str, hint: str | None = None) -> list[str]:
                labels: list[str] = []
                seen: set[str] = set()

                def _add(val: str | None) -> None:
                    s = str(val or "").strip()
                    if not s or s in seen:
                        return
                    seen.add(s)
                    labels.append(s)

                _add(hint)
                try:
                    sid_l = str(sid or "").strip().lower()
                    ch_l = str(ch_id or "").strip().lower()
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "") or "").strip().lower()
                        rch = str(row.get("channel_id", "") or "").strip().lower()
                        rlab = str(row.get("label", "") or "").strip()
                        if rsid == sid_l and rch == ch_l and rlab:
                            _add(rlab)
                except Exception:
                    pass

                try:
                    sid_l = str(sid or "").strip().lower()
                    ch_l = str(ch_id or "").strip().lower()
                    for meta in (self.nodus_switch_topic_map or {}).values():
                        msid = str(meta.get("switch_id", "") or "").strip().lower()
                        mch = str(meta.get("channel_id", "") or "").strip().lower()
                        mlab = str(meta.get("label", "") or "").strip()
                        if msid == sid_l and mch == ch_l and mlab:
                            _add(mlab)
                except Exception:
                    pass

                return labels

            def _cache_channel_state(sid: str, ch_id: str, state_on: bool, hint: str | None = None) -> list[str]:
                cache = self._switch_state_cache.setdefault(sid, {})
                state_txt = "on" if state_on else "off"
                cache[ch_id] = state_txt
                labels = _labels_for_channel(sid, ch_id, hint=hint)
                for lbl in labels:
                    cache[lbl] = state_txt
                self._known_switch_ids.add(sid)
                return labels

            info = self.nodus_switch_topic_map.get(topic)
            if not info:
                return

            switch_id = info.get("switch_id")
            channel_id = info.get("channel_id")
            label = info.get("label")
            kind = info.get("kind")

            if not switch_id or not channel_id or kind not in ("state", "event"):
                return

            payload_text = "" if payload is None else str(payload).strip()
            is_on: bool | None = None
            ts_iso: str | None = None
            source = "mqtt-nodus"

            # JSON payload (preferred)
            if payload_text.startswith("{") and payload_text.endswith("}"):
                try:
                    obj = json.loads(payload_text)
                except Exception:
                    obj = None

                if isinstance(obj, dict):
                    # optional source
                    source = (obj.get("source") or source) if isinstance(obj.get("source"), str) else source

                    # parse timestamp if present
                    ts_val = obj.get("timestamp")
                    if ts_val is not None:
                        ts_iso = _iso_from_payload_ts(ts_val)

                    # extract ON/OFF from "event" dict
                    ev = obj.get("event") or {}
                    if isinstance(ev, dict) and ev:
                        # Prefer a key that matches the channel label or SWITCH_n
                        state_val = None
                        if label and label in ev:
                            state_val = ev.get(label)
                        else:
                            # fall back to first value
                            try:
                                state_val = list(ev.values())[0]
                            except Exception:
                                state_val = None
                        if state_val is not None:
                            is_on = str(state_val).strip().lower() in ("on", "1", "true", "t", "yes", "y")

            # Legacy plain-text payload
            if is_on is None:
                is_on = payload_text.upper() == "ON"

            if kind == "state":
                # Persist state transitions too; dedupe prevents retained-state spam.
                force_write = False
                try:
                    label_resolved = None
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "") or "").strip()
                        rch = str(row.get("channel_id", "") or "").strip()
                        rlab = str(row.get("label", "") or "").strip()
                        if rsid == str(switch_id) and rch == str(channel_id) and rlab:
                            label_resolved = rlab
                            break
                    if not label_resolved:
                        label_resolved = label or str(channel_id)
                    db_key = build_switch_key(str(channel_id), str(label_resolved))
                    getter = getattr(self.data_logger, "get_latest_switch_state_by_source_prefix", None)
                    if callable(getter):
                        latest = getter(db_key, source_prefix="mqtt", sensor_id=f"Switch_{switch_id}")
                    else:
                        latest = self.data_logger.get_latest_switch_state(db_key)
                    if latest is not None:
                        latest_on = str(latest).strip().lower() == "on"
                        force_write = (latest_on != bool(is_on))
                except Exception:
                    force_write = False

                self._maybe_persist_switch_event(
                    switch_id=switch_id,
                    channel_id=channel_id,
                    is_on=is_on,
                    ts_iso=ts_iso,
                    source=f"{source}-state",
                    sensor_lineage=f"Switch_{switch_id}",
                    force_write=force_write,
                )
                _cache_channel_state(switch_id, channel_id, is_on, hint=label)
            elif kind == "event":
                self._maybe_persist_switch_event(
                    switch_id=switch_id,
                    channel_id=channel_id,
                    is_on=is_on,
                    ts_iso=ts_iso,
                    source=source,
                    sensor_lineage=f"Switch_{switch_id}",
                )
                labels = _cache_channel_state(switch_id, channel_id, is_on, hint=label)
                ui_label = labels[0] if labels else (label or channel_id)
                # Push live updates to the UI (label-based key for listbox match)
                try:
                    import saiWebRoutes as routes
                    switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
                    if switch_broadcast:
                        self._schedule_coro(switch_broadcast({
                            "type": "switch_event",
                            "key": f"{switch_id}::{ui_label}",
                            "state": bool(is_on),
                            "timestamp": ts_iso or get_timestamp(),
                            "source": source,
                        }))
                except Exception:
                    pass

        except Exception as e:
            printDM(f"[handle_nodus_switch_topic] err: {e}", location=MODULE)
            
    # replace handle_switch_event_device with this version
    def handle_switch_event_device(self, topic: str, payload: str):
        """
        JSON event shape, examples:
          topic: "switch/switch-oqs3lr-GP28/event"
          payload: {"timestamp": 1758635536.73, "event": {"SWITCH_1": "on"}}
        We persist only if we can map to a human label; otherwise skip (slug events will cover it).
        """
        try:
            obj = json.loads(payload)
        except Exception:
            return  # not JSON

        try:
            ev = obj.get("event") or {}
            if not isinstance(ev, dict) or not ev:
                return

            # Extract single (channel_key -> state) pair
            (label_key, state_str) = list(ev.items())[0]
            is_on = str(state_str).strip().lower() in ("on", "1", "true", "t", "yes", "y")

            # topic: "switch/<switch_id>-<pin>/event"  or  "switch/<switch_id>/<SWITCH_n>/event"
            parts = topic.split("/")
            if len(parts) < 3 or parts[0] != "switch":
                return
            sw_part = parts[1]  # e.g. "switch-oqs3lr-GP28" or "switch-oqs3lr"
            base_id, pin = split_switch_id_and_pin(sw_part)
            switch_id = base_id
            sensor_lineage = f"{base_id}-{pin}" if pin else base_id

            # 1) Best: bind from discovery (exact topic → pretty label)
            label = self.event_topic_to_label.get(topic)

            # 2) Next: try channel map keyed by ("<switch_id>", "SWITCH_n")
            if not label:
                ch_key = None
                if isinstance(label_key, str) and label_key:
                    up = label_key.strip().upper()
                    if up.startswith("SWITCH_"):
                        ch_key = up
                    else:
                        # allow "1" -> "SWITCH_1"
                        try:
                            ch_key = f"SWITCH_{int(label_key)}"
                        except Exception:
                            ch_key = None
                if ch_key:
                    label = self.switch_channel_map.get((switch_id, ch_key))

            # 3) If we still don't have a human label, SKIP persisting this JSON event.
            #    We'll rely on the slug event (".../<label_slug>/event"), which you also publish.
            if not label:
                if DEBUG:
                    printDM(f"[device_event] no label mapping for {topic}; skipping JSON event", location=MODULE)
                return

            # Persist only on change & update cache (local "now" timestamp only)
            ts_iso = None

            channel_id = None
            try:
                # Add a helper in DataLogger if you can:
                channel_id = getattr(self.data_logger, "get_switch_channel_id", lambda _a, _b: None)(switch_id, label)
            except Exception:
                channel_id = None

            if not channel_id:
                if DEBUG:
                    printDM(f"[device_event] no channel_id for {switch_id} label={label}; skipping persist", location=MODULE)
                return

            self._maybe_persist_switch_event(
                switch_id=switch_id,
                channel_id=channel_id,
                is_on=is_on,
                ts_iso=ts_iso,
                source="mqtt",
                sensor_lineage=sensor_lineage,
            )

        except Exception as e:
            printDM(f"[handle_switch_event_device] err: {e}", location=MODULE)

    def get_known_switch_devices(self) -> list[str]:
        """
        Return switch_id list discovered via MQTT (if you record them).
        Safe to return [] if not tracked.
        """
        return sorted(list(self._known_switch_ids)) if hasattr(self, "_known_switch_ids") else []

    def get_last_switch_state(self, switch_id: str) -> dict | None:
        """
        Return latest known state per channel for a given switch_id:
        Example: {'Fan':'on','Light':'off'} or None if unknown.
        """
        store = getattr(self, "_switch_state_cache", {})
        return store.get(switch_id)

    async def wait_until_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout)
            return True
        except Exception:
            return False

    async def wait_until_ha_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._ha_connected_evt.wait(), timeout=timeout)
            return True
        except Exception:
            return False
        
    def publish_text(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False, use_ha_client: bool = True) -> bool:
        try:
            if not topic:
                return False
            client = (self.ha_client or self.client) if use_ha_client else self.client
            info = client.publish(topic, payload, qos=qos, retain=retain)
            rc = getattr(info, "rc", 0) if info is not None else 0
            return rc == 0
        except Exception as e:
            printDM(f"[publish_text] {topic} error: {e}", location=MODULE)
            return False

    def publish_json(self, topic: str, obj: dict, *, qos: int = 0, retain: bool = False, use_ha_client: bool = True) -> bool:
        try:
            return self.publish_text(
                topic,
                json.dumps(obj, separators=(",", ":")),
                qos=qos,
                retain=retain,
                use_ha_client=use_ha_client,
            )
        except Exception:
            return False

    def subscribe(self, topic_filter: str, callback=None, *, qos: int = 0) -> bool:
        """
        Subscribe to a topic filter.
        If callback is provided, bind it using paho's message_callback_add.
        """
        try:
            if not topic_filter:
                return False

            self.registered_topics.add(topic_filter)

            # Optional callback binding (needed for HA command topics)
            if callback is not None:
                try:
                    # message_callback_add causes matching messages to call callback
                    client = self.ha_client or self.client
                    client.message_callback_add(topic_filter, callback)
                except Exception as e:
                    printDM(f"[subscribe] callback_add failed {topic_filter}: {e}", location=MODULE)
                    # keep going; subscription may still succeed

            client = self.ha_client or self.client
            res = client.subscribe(topic_filter, qos=qos)

            # paho returns (result, mid)
            if isinstance(res, tuple) and len(res) >= 1:
                return res[0] == 0
            return True

        except Exception as e:
            printDM(f"[subscribe] {topic_filter} error: {e}", location=MODULE)
            return False
        
    async def mqtt_discovery_loop(self):
        """
        Discovery + liveness loop.
          Typical startup is MQTT-meta-first and avoids background /itaot-meta probes.
          HTTP /itaot-meta is reserved for Add Device, explicit refreshes, and
          optional legacy fallback when BACKGROUND_HTTP_META_DISCOVERY is enabled.
        Implementation details:
          - Single host target per tick (cached IP preferred).
          - No HTTP keep-alive; small connection pool.
          - Close every connection; short timeouts.
          - Gentle pacing (≈29.33s per host).
        """
        from datetime import datetime
        import logging, random, time, json, asyncio, inspect, httpx

        logging.getLogger("httpx").setLevel(logging.WARNING)

        # ───────────── User-tunable constants (top) ─────────────
        PORT = 8000
        TICK_INTERVAL_S = 29.33               # per-host cadence
        HAYD_TIMEOUT_S  = 7.0
        ITAOT_TIMEOUT_S = 7.0
        MAX_ITAOT_RETRIES = 1                 # single try keeps bursts down
        OFFLINE_RETRIES = 5
        MQTT_GRACE_S   = 120.0                # if /hayd fails but we saw recent MQTT, keep ONLINE
        # Global spacing between /itaot onboarding parses across hosts.
        # Keeps startup/add-device bursts from starving the event loop.
        try:
            cfg_spacing = self.settings.get_setting("SensorNetwork", "DISCOVERY_ONBOARD_SPACING_SEC", 12.0) if self.settings else 12.0
            ITAOT_HOST_SPACING_S = float(cfg_spacing or 12.0)
        except Exception:
            ITAOT_HOST_SPACING_S = 12.0
        if ITAOT_HOST_SPACING_S < 0.0:
            ITAOT_HOST_SPACING_S = 0.0
        # ───────────── Internal defaults / guards ─────────────
        REQUEST_HEADERS = {
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "Sensorius/1.0",
        }
        if not hasattr(self, "last_mqtt_seen"):       self.last_mqtt_seen = {}
        if not hasattr(self, "host_to_peer_ids"):     self.host_to_peer_ids = {}
        if not hasattr(self, "device_status"):        self.device_status = {}
        if not hasattr(self, "discovery_failures"):   self.discovery_failures = {}
        if not hasattr(self, "last_check_time"):      self.last_check_time = {}
        if not hasattr(self, "device_offline_count"): self.device_offline_count = {}
        if not hasattr(self, "_host_ip_cache"):       self._host_ip_cache = {}
        if not hasattr(self, "_host_ipv4addr"):      self._host_ipv4addr = {}

        allow_http_meta_discovery = self._allow_background_http_meta_discovery()

        # Per-host state for optional legacy HTTP fallback:
        # - first_hayd_done: set True after the first successful /hayd since process start
        # - onboarding_done: set True after the first /itaot post-startup hayd
        # - last_hayd_ok: tracks last tick result to detect recovery edges
        first_hayd_done:   dict[str, bool]  = {}
        onboarding_done:   dict[str, bool]  = {}
        last_hayd_ok:      dict[str, bool]  = {}
        if not hasattr(self, "_disc_sem"):
            self._disc_sem = asyncio.Semaphore(1)

        itaot_due_at: dict[str, float] = {}
        next_itaot_slot_at: float = 0.0
        # ───────────── host helpers ─────────────
        async def _ipv4_first_maybe_async(host_in: str, port_num: int) -> str | None:
            try:
                res = self._ipv4_first(host_in, port_num)
                if inspect.isawaitable(res):
                    return await res
                return res
            except Exception:
                return None

        async def _pick_host_for(hostname: str, port_num: int) -> tuple[str, str]:
            """
            Returns (host_to_use, base_key)
            base_key is canonical and should be used for cache/status dicts.
            """
            base = self._normalize_host_key(hostname)  # canonical: no .local
            cached = self._host_ip_cache.get(base)
            if cached:
                return cached, base

            # candidates/resolve
            try:
                candidates = list(self._host_candidates(hostname))
            except Exception:
                candidates = [hostname]
            base_candidate = candidates[0] if candidates else hostname
            ip = await _ipv4_first_maybe_async(base_candidate, port_num)
            return (ip or base_candidate), base

        async def _probe_hayd(client: httpx.AsyncClient, hostname: str) -> bool:
            async with self._disc_sem:
                host, base = await _pick_host_for(hostname, PORT)

                def _is_ip(h: str | None) -> bool:
                    try:
                        return bool(h) and h.replace(".", "").isdigit()
                    except Exception:
                        return False

                is_ip = _is_ip(host)

                # Determine if this IP came from cache (so we only invalidate cache on failures of cached IPs)
                cached_ip = None
                try:
                    cached_ip = self._host_ip_cache.get(base)
                except Exception:
                    cached_ip = None
                host_was_cached = bool(cached_ip) and (cached_ip == host)

                # One-shot fallback hostname target (deterministic)
                fallback_host = hostname if str(hostname).endswith(".local") else f"{hostname}.local"

                t0 = time.monotonic()
                try:
                    r = await client.get(
                        f"http://{host}:{PORT}/hayd",
                        timeout=HAYD_TIMEOUT_S,
                        headers=REQUEST_HEADERS,
                    )
                    took = time.monotonic() - t0
                    if DEBUG:
                        printDM(f"→ {host}/hayd | {r} took {took:.2f}s", location=MODULE)

                    if r.status_code != 200:
                        # treat non-200 as failure without throwing a synthetic RequestError
                        return False

                    try:
                        data = r.json()
                    except Exception:
                        return False

                    status_text = str((data or {}).get("STATUS", "")).strip().lower()
                    ok = status_text in {"ok", "online", "ready"}

                    # Cache IP only on confirmed success
                    if ok and is_ip:
                        self._host_ip_cache[base] = host

                    return ok

                except (httpx.TimeoutException, httpx.RequestError) as e:
                    took = time.monotonic() - t0
                    if DEBUG:
                        printDM(
                            f"[mqtt_discovery_loop] /hayd error for {host}: {type(e).__name__}: {e} took {took:.2f}s",
                            location=MODULE,
                        )

                    # If a cached IP failed, invalidate cache; then still try .local fallback once.
                    if is_ip and host_was_cached:
                        self._host_ip_cache.pop(base, None)

                    # One-shot fallback to hostname (.local), even when the primary target was not an IP.
                    t1 = time.monotonic()
                    try:
                        r2 = await client.get(
                            f"http://{fallback_host}:{PORT}/hayd",
                            timeout=HAYD_TIMEOUT_S,
                            headers=REQUEST_HEADERS,
                        )
                        took2 = time.monotonic() - t1
                        if DEBUG:
                            printDM(f"→ {fallback_host}/hayd | {r2} took {took2:.2f}s (fallback)", location=MODULE)

                        if r2.status_code != 200:
                            return False

                        try:
                            data2 = r2.json()
                        except Exception:
                            return False

                        status2 = str((data2 or {}).get("STATUS", "")).strip().lower()
                        return status2 in {"ok", "online", "ready"}
                    except Exception:
                        return False

                    return False

                finally:
                    self._feed_watchdog("MQTT Discovery Loop")


        async def _probe_http_meta(client: httpx.AsyncClient, hostname: str) -> bool:
            """
            Return True if a valid payload was received and parsed (even if no new subs).

            Behavior:
              - Use hostname first for discovery (2-3 attempts).
              - If hostname fails, resolve mDNS IPv4 and verify against cached ipv4addr.
              - If mDNS IP matches, try it once; if it fails, try cached ipv4addr once.
            """
            async with self._disc_sem:
                # Canonical identity used for status/cache dicts
                base = self._normalize_host_key(hostname)  # e.g. "apvpd-luvk44" (no .local)

                def _is_ip(h: str | None) -> bool:
                    try:
                        return bool(h) and h.replace(".", "").isdigit()
                    except Exception:
                        return False

                # Prefer mDNS hostname first for bare Nodus ids like "apvpd-xxxxxx".
                # On many LANs the bare hostname is not resolvable, but "<host>.local" is.
                try:
                    requested_host = (hostname or base or "").strip()
                except Exception:
                    requested_host = str(hostname or "").strip()
                if not requested_host:
                    requested_host = base

                if _is_ip(requested_host):
                    primary_host = requested_host
                    secondary_host = ""
                elif requested_host.endswith(".local"):
                    primary_host = requested_host
                    secondary_host = requested_host[:-6].strip()
                else:
                    primary_host = f"{requested_host}.local"
                    secondary_host = requested_host

                # mDNS host (used for resolution and as secondary URL candidate)
                mdns_host = primary_host if str(primary_host).endswith(".local") else f"{primary_host}.local"

                async def _fetch_meta(target_host: str, retries: int) -> tuple[bool, str]:
                    """
                    Returns (ok, err_type) where err_type is '' on ok,
                    otherwise a short string for debug context.
                    """
                    url = f"http://{target_host}:{PORT}/itaot-meta"
                    max_tries = max(1, int(retries))
                    last_err = "NoValidPayload"
                    for attempt in range(max_tries):
                        # Feed before each network attempt so long retry chains don't trip watchdog.
                        self._feed_watchdog("MQTT Discovery Loop")
                        await asyncio.sleep(0)
                        t0 = time.monotonic()
                        try:
                            resp = await client.get(url, timeout=ITAOT_TIMEOUT_S, headers=REQUEST_HEADERS)
                            took = time.monotonic() - t0
                            if DEBUG:
                                printDM(
                                    f"→ {target_host}/itaot-meta took {took:.2f}s (try {attempt+1}/{max_tries})",
                                    location=MODULE,
                                )

                            if resp.status_code != 200:
                                await asyncio.sleep(0)
                                continue

                            # Parse JSON (tolerate stray text)
                            try:
                                payload = resp.json()
                            except Exception:
                                txt = resp.text or ""
                                s = txt.find("{")
                                eidx = txt.rfind("}")
                                if s != -1 and eidx > s:
                                    payload = json.loads(txt[s : eidx + 1])
                                else:
                                    await asyncio.sleep(0)
                                    continue

                            try:
                                ok_from_parser, _new = self._parse_and_subscribe_from_http_meta(payload, hostname)
                            except Exception as e:
                                ok_from_parser = False
                                if DEBUG:
                                    printDM(f"[mqtt_discovery_loop] /itaot-meta parse error: {e}", location=MODULE)

                            if ok_from_parser:
                                self._feed_watchdog("MQTT Discovery Loop")
                                return True, ""

                            last_err = "NoValidPayload"
                            await asyncio.sleep(0)

                        except (httpx.TimeoutException, httpx.RequestError) as e:
                            took = time.monotonic() - t0
                            if DEBUG:
                                printDM(
                                    f"[mqtt_discovery_loop] /itaot-meta failed for {target_host}: {type(e).__name__}: {e} took {took:.2f}s",
                                    location=MODULE,
                                )
                            last_err = type(e).__name__
                            await asyncio.sleep(0)
                            continue
                        except Exception as e:
                            if DEBUG:
                                printDM(
                                    f"[mqtt_discovery_loop] /itaot-meta unexpected for {target_host}: {type(e).__name__}: {e}",
                                    location=MODULE,
                                )
                            last_err = type(e).__name__
                            await asyncio.sleep(0)
                            return False, type(e).__name__
                        finally:
                            self._feed_watchdog("MQTT Discovery Loop")

                    return False, last_err

                try:
                    # 1) Preferred hostname first (mDNS for bare ids)
                    ok, _err = await _fetch_meta(primary_host, retries=3)
                    if ok:
                        return True

                    # 1b) Secondary hostname fallback before any IP checks.
                    # For primary ".local", secondary is bare host. For primary bare host, secondary may be ".local".
                    for host_alt in (secondary_host, mdns_host):
                        self._feed_watchdog("MQTT Discovery Loop")
                        host_alt = (host_alt or "").strip()
                        if not host_alt or host_alt == primary_host:
                            continue
                        ok_local, _err_local = await _fetch_meta(host_alt, retries=2)
                        if ok_local:
                            return True

                    # 2) Resolve mDNS IP and verify it matches cached ipv4addr.
                    mdns_ip = await _ipv4_first_maybe_async(mdns_host, PORT)
                    cached_ipv4 = None
                    try:
                        cached_ipv4 = self._host_ipv4addr.get(base)
                    except Exception:
                        cached_ipv4 = None

                    mdns_ok_to_try = bool(mdns_ip and cached_ipv4 and mdns_ip == cached_ipv4)
                    if mdns_ok_to_try:
                        self._feed_watchdog("MQTT Discovery Loop")
                        ok2, _err2 = await _fetch_meta(mdns_ip, retries=2)
                        if ok2:
                            self._host_ip_cache[base] = mdns_ip
                            return True
                    elif DEBUG and mdns_ip and cached_ipv4 and mdns_ip != cached_ipv4:
                        printDM(
                            f"[mqtt_discovery_loop] mdns ip {mdns_ip} != cached ipv4addr {cached_ipv4} for {base}",
                            location=MODULE,
                        )

                    # 3) Final fallback: cached ipv4addr.
                    if cached_ipv4:
                        self._feed_watchdog("MQTT Discovery Loop")
                        ok3, _err3 = await _fetch_meta(cached_ipv4, retries=2)
                        if ok3:
                            self._host_ip_cache[base] = cached_ipv4
                            return True

                    return False

                finally:
                    self._feed_watchdog("MQTT Discovery Loop")


        # ───────────── main loop ─────────────
        try:
            await asyncio.sleep(1.0)
            timeout_cfg = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=2.0)
            limits_cfg  = httpx.Limits(max_keepalive_connections=0, max_connections=6)

            async with httpx.AsyncClient(timeout=timeout_cfg, limits=limits_cfg, http2=False) as client:
            #async with httpx.AsyncClient(limits=limits_cfg, http2=False) as client:
                while True:
                    if not getattr(self, "mqtt_clients", None):
                        if DEBUG:
                            printDM("[mqtt_discovery_loop] No clients found, skipping tick", location=MODULE)
                    else:
                        for hostname in list(self.mqtt_clients):
                            now_mono = time.monotonic()
                            base = self._normalize_host_key(hostname) or str(hostname)
                            try:
                                if _looks_like_channel_id(hostname):
                                    # Defensive cleanup: old runtimes may have channel ids in mqtt_clients.
                                    self.mqtt_clients.discard(hostname)
                                    continue
                                self._feed_watchdog("MQTT Discovery Loop")
                                await asyncio.sleep(0)
                                base = self._normalize_host_key(hostname)

                                # per-host cadence
                                if (now_mono - self.last_check_time.get(base, 0.0)) < TICK_INTERVAL_S:
                                    continue
                                self.last_check_time[base] = now_mono

                                now_ts = time.time()
                                legacy_mode = self._use_legacy_pollers_for(base)
                                if (base in self._legacy_firmware_hosts) and (not self._legacy_pollers_allowed()):
                                    self._mark_host_status(base, "migration_required")
                                    continue

                                # New default path: heartbeat/availability/data-derived liveness; no /hayd or /itaot polling.
                                if not legacy_mode:
                                    derived = self._apply_heartbeat_timeout_state(base, now_ts=now_ts)
                                    if (not self.nodus_debug_data_only) and self.heartbeat_stale.get(base) and derived == "online":
                                        # Fresh data/availability recovery without fresh heartbeat stays degraded until heartbeat catches up.
                                        derived = "degraded"
                                    self._mark_host_status(base, derived)
                                    continue

                                # Legacy compatibility path (explicitly marked hosts only, pre-sunset).
                                # 1) Prefer MQTT /availability; fallback to /hayd if unseen
                                avail = self._get_nodus_availability(base)
                                if avail is None:
                                    hayd_ok = await _probe_hayd(client, hostname)
                                else:
                                    hayd_ok = (avail == "online")

                                # Track recovery edges (FAIL -> OK)
                                prev_ok = last_hayd_ok.get(base, False)
                                last_hayd_ok[base] = hayd_ok
                                recovered = bool(hayd_ok and not prev_ok)

                                # Optional legacy behavior: after recovery, allow one
                                # background /itaot-meta refresh if explicitly enabled.
                                if allow_http_meta_discovery and recovered and onboarding_done.get(base, False):
                                    itaot_due_at[base] = time.monotonic() + 5.0

                                # 2) Startup path for legacy HTTP fallback
                                if not first_hayd_done.get(base, False):
                                    if hayd_ok:
                                        first_hayd_done[base] = True
                                        if allow_http_meta_discovery:
                                            onboarding_done.setdefault(base, False)
                                            itaot_due_at[base] = time.monotonic() + 5.0
                                        self._mark_host_status(base, "unknown")
                                    else:
                                        # still before first hayd success; rely on MQTT grace for status
                                        pids = self.host_to_peer_ids.get(base, [])
                                        recent = any((now_ts - self.last_mqtt_seen.get(pid, 0.0)) < MQTT_GRACE_S for pid in pids)
                                        # If /hayd is unavailable but MQTT data is clearly flowing,
                                        # bootstrap status from MQTT liveness only.
                                        if recent:
                                            first_hayd_done[base] = True
                                            if allow_http_meta_discovery:
                                                onboarding_done.setdefault(base, False)
                                                itaot_due_at.setdefault(base, time.monotonic() + 5.0)
                                            self._mark_host_status(base, "online")
                                        else:
                                            self._mark_host_status(base, "unknown")
                                    continue  # next host

                                # 3) Post-startup steady state
                                if hayd_ok:
                                    if self.device_status.get(base) != "online":
                                        self._mark_host_status(base, "online")
                                        self.device_offline_count[base] = 0

                                    if allow_http_meta_discovery:
                                        due = itaot_due_at.get(base)
                                        now_probe = time.monotonic()
                                        if due and now_probe >= due:
                                            # Enforce global /itaot spacing so hosts are onboarded in sequence.
                                            if now_probe < next_itaot_slot_at:
                                                continue
                                            itaot_due_at.pop(base, None)
                                            next_itaot_slot_at = now_probe + ITAOT_HOST_SPACING_S
                                            if await _probe_http_meta(client, hostname):
                                                onboarding_done[base] = True
                                                self._mark_host_status(base, "online")
                                                self.device_offline_count[base] = 0
                                            else:
                                                self._mark_host_status(base, "unknown")

                                else:
                                    # Offline or unknown — decide using MQTT grace + retries
                                    pids = self.host_to_peer_ids.get(base, [])
                                    recent = any((now_ts - self.last_mqtt_seen.get(pid, 0.0)) < MQTT_GRACE_S for pid in pids)

                                    if recent:
                                        self._mark_host_status(base, "degraded")
                                        if allow_http_meta_discovery:
                                            # Also honor scheduled /itaot onboarding using MQTT liveness
                                            # when /hayd is unavailable on the device.
                                            due = itaot_due_at.get(base)
                                            now_probe = time.monotonic()
                                            if due and now_probe >= due:
                                                if now_probe < next_itaot_slot_at:
                                                    continue
                                                itaot_due_at.pop(base, None)
                                                next_itaot_slot_at = now_probe + ITAOT_HOST_SPACING_S
                                                if await _probe_http_meta(client, hostname):
                                                    onboarding_done[base] = True
                                                    self._mark_host_status(base, "online")
                                                    self.device_offline_count[base] = 0
                                                else:
                                                    self._mark_host_status(base, "degraded")
                                        # do NOT increment offline counter while MQTT is still flowing
                                    else:
                                        n = self.device_offline_count.get(base, 0) + 1
                                        self.device_offline_count[base] = n
                                        self.discovery_failures[base] = now_mono

                                        if n < OFFLINE_RETRIES:
                                            self._mark_host_status(base, "degraded")
                                            if DEBUG:
                                                printDM(f"[{base}] /hayd failed → DEGRADED ({n}/{OFFLINE_RETRIES})", location=MODULE)
                                        else:
                                            self._mark_host_status(base, "offline")
                                            if DEBUG:
                                                printDM(f"[{base}] /hayd failed → OFFLINE (retries={n})", location=MODULE)

                            except Exception as host_loop_err:
                                self._feed_watchdog("MQTT Discovery Loop")
                                printDM(
                                    f"[mqtt_discovery_loop] Unexpected error while probing {base}: {host_loop_err}",
                                    location=MODULE,
                                )

                    # global pacing
                    end_at = time.monotonic() + (TICK_INTERVAL_S + random.uniform(-0.5, 0.5))
                    while time.monotonic() < end_at:
                        await asyncio.sleep(0.5)
                        self._feed_watchdog("MQTT Discovery Loop")
                    await asyncio.sleep(0)

        except Exception as outer_e:
            printDM(f"[mqtt_discovery_loop] fatal exception: {outer_e}", location=MODULE)
