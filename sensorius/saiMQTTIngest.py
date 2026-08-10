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
import copy
import json
import re
import socket
import time
import threading
import uuid
try:
    import tomllib
except Exception:
    tomllib = None
import httpx
import paho.mqtt.client as mqtt
from collections import defaultdict, OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo
from .saiUtils import printDM, debug_enabled, get_timestamp, normalize_hostname_base, mdns_hostname
from .saiDataLogger import SENSOR_EVENT_STATE_OFFLINE, SENSOR_EVENT_TYPE_LIVENESS, saiDataLogger, build_switch_key
from .saiRuntimePaths import resolve_runtime_base_dir
from .sensor_modules.station_weewx import (
    DEFAULT_MQTT_TOPIC as WEEWX_DEFAULT_MQTT_TOPIC,
    DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID,
    DEFAULT_UPDATE_PERIOD_SEC as WEEWX_DEFAULT_UPDATE_PERIOD_SEC,
    WEEWX_DISPLAY_METRICS,
    WEEWX_RAIN_24H_METRIC,
    apply_weewx_station_metadata,
    mqtt_topic_matches as weewx_topic_matches,
    normalize_weewx_mqtt_payload,
)

_REMOVED_NODUS_IDS_SETTING = "REMOVED_NODUS_IDS"
_NODUS_FAMILY_PREFIXES = {
    "apvpd", "avpd", "aqi", "aht", "aht10", "ahtx0", "co2", "lux",
    "nodus", "soil", "switch", "veml",
}

MODULE = "saiMQTTIngest"
DEBUG = debug_enabled(MODULE)
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_HEARTBEAT_INTERVAL_S = 10.0
HEARTBEAT_STALE_AFTER_S = 90.0
HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 15.0
LEGACY_POLLER_SUNSET_DATE = "2026-06-30"
NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S = 45.0
NODUS_SWITCH_COMMAND_FAILED_COOLDOWN_S = 5.0

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

def _values_equal(left, right) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except Exception:
        return left == right

def _norm_ipv4addr(addr) -> str | None:
    raw = str(addr or "").strip()
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

def _extract_runtime_ipv4addr(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("ipv4addr", "IPV4ADDR", "IPv4Addr", "ipv4"):
        ip = _norm_ipv4addr(payload.get(key))
        if ip:
            return ip
    for key, val in payload.items():
        if isinstance(key, str) and key.lower() in {"ipv4addr", "ipv4"}:
            ip = _norm_ipv4addr(val)
            if ip:
                return ip
    return None

def _extract_nodus_board_type(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("mcu", "MCU", "board_type", "BOARD_TYPE", "boardtype", "BOARDTYPE", "board", "BOARD"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    legacy_type = str(payload.get("type") or payload.get("TYPE") or "").strip()
    if legacy_type and legacy_type.lower() not in {"nodus", "remote", "mqtt"}:
        return legacy_type
    return "pico2w"

def _extract_nodus_sensor_hardware(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    containers: list[dict] = []
    sensor_blob = payload.get("sensor")
    if isinstance(sensor_blob, dict):
        containers.append(sensor_blob)
    containers.append(payload)
    for container in containers:
        for key in ("hardware", "HARDWARE", "sensor_hardware", "SENSOR_HARDWARE"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""

def _local_epoch_seconds(settings=None, now_epoch: float | None = None) -> int:
    """Return local-naive epoch seconds for MQTT command identifiers."""
    try:
        epoch = float(time.time() if now_epoch is None else now_epoch)
    except Exception:
        epoch = time.time()
    tzname = None
    if settings is not None:
        try:
            tzname = (
                settings.get_setting("Time", "TZ")
                or settings.get_setting("Time", "tz")
            )
        except Exception:
            tzname = None
    try:
        if not tzname:
            from .saiSettings import saiSettings

            loaded_settings = saiSettings(apply_live=False)
            tzname = (
                loaded_settings.get_setting("Time", "TZ")
                or loaded_settings.get_setting("Time", "tz")
            )
    except Exception:
        pass
    try:
        tz = ZoneInfo(str(tzname or "").strip()) if tzname else None
    except Exception:
        tz = None
    try:
        current = (
            datetime.fromtimestamp(epoch, tz)
            if tz
            else datetime.fromtimestamp(epoch).astimezone()
        )
        offset = current.utcoffset()
        if offset is not None:
            epoch += offset.total_seconds()
    except Exception:
        pass
    return int(epoch)

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
                from .saiSettings import saiSettings
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

def _mqtt_topic_matches(filter_text: str | None, topic: str | None) -> bool:
    """
    Minimal MQTT topic filter matcher for + and # wildcards.
    Used to avoid redundant overlapping subscriptions on a single client.
    """
    flt = str(filter_text or "").strip()
    top = str(topic or "").strip()
    if not flt or not top:
        return False
    f_parts = flt.split("/")
    t_parts = top.split("/")
    for idx, part in enumerate(f_parts):
        if part == "#":
            return idx == len(f_parts) - 1
        if idx >= len(t_parts):
            return False
        if part == "+":
            continue
        if part != t_parts[idx]:
            return False
    return len(t_parts) == len(f_parts)

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
        self._host_ipv4addr: dict[str, str] = {}
        self.discovery_failures = {}  # track failures by hostname, time.monotonic()
        self.device_status = {}       # track device status by hostname → "online"/"degraded"/"offline"/"unknown"
        self.device_offline_count = defaultdict(int)  # track device offline by hostname 
        self.discovery_cache: dict[str, dict] = {} # cache of last /itaot per host (optional but handy)
        self.host_to_peer_ids: dict[str, list[str]] = {}  # map host -> [peer_ids] (already guarded in loop, but ensure exists early)
        self.last_mqtt_seen: dict[str, float] = {}  # last mqtt seen map (guarded elsewhere; set here for safety)
        self.nodus_availability: dict[str, str] = {}  # host -> "online"|"offline" (from MQTT /availability)
        self.last_heartbeat_ts: dict[str, float] = {}  # host -> unix epoch seconds from heartbeat payload
        self.last_heartbeat_payload: dict[str, dict] = {}  # host -> last heartbeat payload object
        self.last_nodus_report_seen: dict[str, float] = {}  # host/peer -> last live heartbeat/data/state/event report
        self.retained_mqtt_seen: dict[str, float] = {}  # host/peer -> retained broker replay receipt time
        self.nodus_firmware_versions: dict[str, str] = {}  # host/peer id -> firmware version from nodus meta
        self.nodus_board_types: dict[str, str] = {}  # host/peer id -> MCU/board target from nodus meta
        self.nodus_sensor_hardware: dict[str, str] = {}  # host/peer id -> concrete sensor hardware from nodus meta
        self.fwupdate_result_by_device: dict[str, dict] = {}  # device_id/host -> last OTA result payload
        self.heartbeat_interval_s_by_host: dict[str, float] = {}  # host -> advertised interval
        self.heartbeat_stale: dict[str, bool] = {}  # host -> heartbeat freshness diagnostic
        self._liveness_status_callbacks: list = []
        # Retained startup replays can include stale hosts. Track repeated retained traffic and
        # only promote to discovery after we see sustained retained data from the same host.
        self._retained_data_seen: dict[str, int] = {}
        self._retained_avail_probe_inflight: set[str] = set()
        self._legacy_firmware_hosts: set[str] = self._load_legacy_firmware_hosts()
        self._legacy_poller_sunset_epoch: float = self._load_legacy_poller_sunset_epoch()
        self._live_sensor_shadow_seeded: set[str] = set()
        self._live_sensor_shadow_attempt_at: dict[str, float] = {}
        self._dashboard_inventory_notified_sensors: set[str] = set()

        self.last_check_time = defaultdict(lambda: 0)
        self.mqtt_clients = set(self.mqtt_clients or [])

        self._callback_lock = threading.RLock()
        self._removed_nodus_ids: set[str] = self._load_removed_nodus_ids()
        self._callback_filters: set[str] = set()
        self._connected_evt = asyncio.Event()
        self._config_lock = threading.RLock()
        self.config_ack_by_message: dict[str, dict] = {}
        self.config_result_by_message: dict[str, dict] = {}
        self.config_message_device: dict[str, str] = {}
        self._switch_config_command_lock = threading.RLock()
        self._switch_config_command_inflight: dict[str, dict] = {}
        self._switch_config_command_by_message: dict[str, str] = {}
        self._calibration_lock = threading.RLock()
        self.calibration_ack_by_message: dict[str, dict] = {}
        self.calibration_result_by_message: dict[str, dict] = {}
        self.calibration_status_by_sensor: dict[str, dict] = {}
        self.calibration_progress_by_sensor: dict[str, dict] = {}
        self.calibration_sample_by_sensor: dict[str, dict] = {}
        self.calibration_samples_by_message: dict[str, list[dict]] = {}
        self.calibration_event_result_by_sensor: dict[str, dict] = {}
        self.calibration_message_device: dict[str, str] = {}
        self._meta_patch_lock = threading.RLock()
        self.meta_patch_by_message: dict[str, dict] = {}

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
        self.nodus_switch_ack_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_switch_result_topics: dict[tuple[str, str], str] = {}  # (switch_id, channel_id) -> topic
        self.nodus_sensor_topics: dict[str, str] = {}  # sensor_id -> topic
        self.nodus_sensor_hosts: dict[str, str] = {}  # sensor_id -> physical device_id
        self.nodus_sensor_config_files: dict[str, str] = {}  # sensor_id -> remote TOML name
        self.nodus_host_sensors: dict[str, list[str]] = {}  # physical device_id -> child sensor IDs
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
                "nodus/+/meta/switch",
                "nodus/+/fwupdate/result",
                "nodus/+/meta/patch",
                "nodus/+/calibration/ack",
                "nodus/+/calibration/result",
                "nodus/+/event/calibration_status",
                "nodus/+/event/calibration_progress",
                "nodus/+/event/calibration_sample",
                "nodus/+/event/calibration_result",
                "nodus/+/onboard/hello",
                "nodus/+/config/ack",
                "nodus/+/config/result",
            })
        self.registered_topics.update(base_topics)
        self.weewx_mqtt_enabled = False
        self.weewx_mqtt_topic = WEEWX_DEFAULT_MQTT_TOPIC
        self.weewx_sensor_id = WEEWX_DEFAULT_SENSOR_ID
        self.weewx_update_period_sec = WEEWX_DEFAULT_UPDATE_PERIOD_SEC
        self._last_weewx_mqtt_signature = None
        self._last_weewx_mqtt_burst_signature = None
        self._last_weewx_mqtt_signature_mono = 0.0
        try:
            self.weewx_mqtt_enabled = bool(self.settings.get_setting("WeeWX", "MQTT_ENABLED", False))
            self.weewx_mqtt_topic = str(
                self.settings.get_setting("WeeWX", "MQTT_TOPIC", WEEWX_DEFAULT_MQTT_TOPIC)
                or WEEWX_DEFAULT_MQTT_TOPIC
            ).strip()
            self.weewx_sensor_id = str(
                self.settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID)
                or WEEWX_DEFAULT_SENSOR_ID
            ).strip() or WEEWX_DEFAULT_SENSOR_ID
            self.weewx_update_period_sec = max(
                15.0,
                float(
                    self.settings.get_setting("WeeWX", "UPDATE_PERIOD_SEC", WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
                    or WEEWX_DEFAULT_UPDATE_PERIOD_SEC
                ),
            )
        except Exception:
            self.weewx_mqtt_enabled = False
        if self.weewx_mqtt_enabled:
            self._ensure_weewx_sensor_settings()
        if self.weewx_mqtt_enabled and self.weewx_mqtt_topic:
            self.registered_topics.add(self.weewx_mqtt_topic)
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
                    f"{self.base_topic}/nodus/+/event/calibration_sample",
                    f"{self.base_topic}/nodus/+/event/calibration_result",
                    f"{self.base_topic}/nodus/+/meta",
                    f"{self.base_topic}/nodus/+/meta/switch",
                    f"{self.base_topic}/nodus/+/fwupdate/result",
                    f"{self.base_topic}/nodus/+/meta/patch",
                    f"{self.base_topic}/nodus/+/onboard/hello",
                    f"{self.base_topic}/nodus/+/config/ack",
                    f"{self.base_topic}/nodus/+/config/result",
                })
            self.registered_topics.update(prefixed_topics)
        self.onboarding_event_handler = None
        self._pending_set: dict[tuple[str, str], dict[str, object]] = {}
        self._recent_switch_origin: dict[tuple[str, str], dict[str, object]] = {}
        self._loop = None  # set in start()
        
        try:
            from .saiHomeAssistantMqtt import HomeAssistantTopicMap
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

    def _ensure_weewx_sensor_settings(self) -> None:
        """Materialize the local sensor.toml shadow for the WeeWX station."""
        try:
            from .saiSensorSettingsManager import SensorSettingsManager

            sensor_id = str(getattr(self, "weewx_sensor_id", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID).strip()
            sensor_id = sensor_id or WEEWX_DEFAULT_SENSOR_ID
            mgr = SensorSettingsManager("sensor_settings")
            try:
                doc = mgr.load(sensor_id) or OrderedDict()
            except FileNotFoundError:
                mgr.seed_from_factory(sensor_id, device="weewx", location="Weather Station")
                doc = mgr.load(sensor_id) or OrderedDict()

            if not isinstance(doc, OrderedDict):
                doc = OrderedDict(doc)
            changed = False

            sensor_block = doc.get("Sensor")
            if not isinstance(sensor_block, dict):
                sensor_block = OrderedDict()
                doc["Sensor"] = sensor_block
                changed = True
            for key, value in (("TYPE", "weewx"), ("DEVICE", "weewx"), ("SENSOR_ID", sensor_id)):
                if sensor_block.get(key) != value:
                    sensor_block[key] = value
                    changed = True
            if not str(sensor_block.get("LOCATION", "") or "").strip():
                sensor_block["LOCATION"] = "Weather Station"
                changed = True
            if apply_weewx_station_metadata(sensor_block):
                changed = True

            display_block = doc.get("Display")
            if not isinstance(display_block, dict):
                display_block = OrderedDict()
                doc["Display"] = display_block
                changed = True
            for idx, metric in enumerate(WEEWX_DISPLAY_METRICS[:6], start=1):
                key = f"METRIC_{idx}"
                if key not in display_block:
                    display_block[key] = metric
                    changed = True

            if changed:
                mgr.save(sensor_id, doc)
        except Exception as exc:
            if DEBUG:
                printDM(f"[weewx-mqtt] sensor settings materialization skipped: {exc}", location=MODULE)

    def configure_weewx_mqtt(
        self,
        *,
        enabled: bool,
        topic_filter: str,
        sensor_id: str | None = None,
        update_period_sec: float | None = None,
    ) -> bool:
        """Apply WeeWX MQTT settings to the running ingest client."""
        topic = str(topic_filter or "").strip()
        if not topic:
            return False

        old_topic = str(getattr(self, "weewx_mqtt_topic", "") or "").strip()
        self.weewx_mqtt_enabled = bool(enabled)
        self.weewx_mqtt_topic = topic
        self.weewx_sensor_id = str(sensor_id or WEEWX_DEFAULT_SENSOR_ID).strip() or WEEWX_DEFAULT_SENSOR_ID
        try:
            if update_period_sec is not None:
                self.weewx_update_period_sec = max(15.0, float(update_period_sec))
        except Exception:
            self.weewx_update_period_sec = WEEWX_DEFAULT_UPDATE_PERIOD_SEC

        if self.weewx_mqtt_enabled:
            self._ensure_weewx_sensor_settings()

        if old_topic and old_topic != topic:
            try:
                self.client.unsubscribe(old_topic)
            except Exception:
                pass

        if not self.weewx_mqtt_enabled:
            return True

        self.registered_topics.add(topic)
        try:
            res = self.client.subscribe(topic)
            if DEBUG:
                printDM(f"[weewx-mqtt] subscribed to {topic}", location=MODULE)
            if isinstance(res, tuple) and len(res) >= 1:
                return res[0] == 0
            return True
        except Exception as exc:
            printDM(f"[weewx-mqtt] subscribe failed for {topic}: {exc}", location=MODULE)
            return False

    def _has_covering_subscription(self, topic_filter: str) -> bool:
        candidate = str(topic_filter or "").strip()
        if not candidate:
            return False
        if "+" in candidate or "#" in candidate:
            return candidate in self.registered_topics
        for existing in self.registered_topics:
            if _mqtt_topic_matches(existing, candidate):
                return True
        return False

    @staticmethod
    def _nodus_identity_suffix(device_id: str | None) -> str:
        """Return the shared Nodus hardware suffix for a sensor, switch, or channel ID."""
        value = normalize_hostname_base(device_id)
        if not value or "-" not in value:
            return ""
        prefix = value.split("-", 1)[0]
        suffix = value.rsplit("-", 1)[-1]
        prefix_l = prefix.lower()
        if prefix_l not in _NODUS_FAMILY_PREFIXES and not re.fullmatch(r"s\d+", prefix_l):
            return ""
        suffix = suffix.lower().strip()
        return suffix if re.fullmatch(r"[a-z0-9]{5,32}", suffix) else ""

    def _load_removed_nodus_ids(self) -> set[str]:
        removed: set[str] = set()
        try:
            raw = self.settings.get_setting(
                "SensorNetwork",
                _REMOVED_NODUS_IDS_SETTING,
                [],
            ) if self.settings else []
            if isinstance(raw, str):
                text = raw.strip()
                if text.startswith("["):
                    try:
                        raw = json.loads(text)
                    except Exception:
                        raw = [text]
                else:
                    raw = [part.strip() for part in text.split(",")]
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    value = normalize_hostname_base(str(item or ""))
                    if value:
                        removed.add(value.lower())
        except Exception:
            pass
        return removed

    def _persist_removed_nodus_ids(self) -> bool:
        if not self.settings:
            return False
        writer = getattr(self.settings, "replace_setting", None)
        if not callable(writer):
            return False
        try:
            writer(
                "SensorNetwork",
                _REMOVED_NODUS_IDS_SETTING,
                sorted(self._removed_nodus_ids),
            )
            return True
        except Exception as exc:
            printDM(f"[removed-nodus] failed to persist suppression list: {exc}", location=MODULE)
            return False

    def is_nodus_device_removed(self, device_id: str | None) -> bool:
        """Return True when a Nodus identity or another member of its hardware family was removed."""
        value = normalize_hostname_base(device_id)
        if not value:
            return False
        value_l = value.lower()
        with self._callback_lock:
            if value_l in self._removed_nodus_ids:
                return True
            suffix = self._nodus_identity_suffix(value_l)
            if not suffix:
                return False
            return any(self._nodus_identity_suffix(item) == suffix for item in self._removed_nodus_ids)

    def suppress_nodus_devices(self, device_ids, *, persist: bool = True) -> dict:
        """Block removed Nodus identities before cache, database, or shadow settings updates."""
        added: list[str] = []
        with self._callback_lock:
            for item in (device_ids or []):
                value = normalize_hostname_base(str(item or ""))
                if not value:
                    continue
                value_l = value.lower()
                if value_l not in self._removed_nodus_ids:
                    self._removed_nodus_ids.add(value_l)
                    added.append(value_l)
            persistence_supported = bool(self.settings and callable(getattr(self.settings, "replace_setting", None)))
            persisted = self._persist_removed_nodus_ids() if persist else False
        return {
            "added": added,
            "persisted": persisted,
            "persistence_supported": persistence_supported,
            "active": bool(self._removed_nodus_ids),
        }

    def allow_nodus_devices(self, device_ids, *, persist: bool = True) -> dict:
        """Allow an explicitly re-onboarded Nodus identity family to be discovered again."""
        requested = {
            value.lower()
            for value in (normalize_hostname_base(str(item or "")) for item in (device_ids or []))
            if value
        }
        suffixes = {self._nodus_identity_suffix(value) for value in requested}
        suffixes.discard("")
        removed: list[str] = []
        with self._callback_lock:
            for existing in list(self._removed_nodus_ids):
                if existing in requested or self._nodus_identity_suffix(existing) in suffixes:
                    self._removed_nodus_ids.discard(existing)
                    removed.append(existing)
            persisted = self._persist_removed_nodus_ids() if persist else False
        return {"removed": sorted(removed), "persisted": persisted}

    def refresh_nodus_retained_metadata(self, device_id: str = "") -> dict:
        """Renew metadata subscriptions so retained Nodus identity is replayed."""
        client = getattr(self, "client", None)
        if client is None or not callable(getattr(client, "subscribe", None)):
            return {"ok": False, "topics": [], "error": "mqtt_client_unavailable"}
        try:
            connected = getattr(client, "is_connected", None)
            if callable(connected) and not bool(connected()):
                return {"ok": False, "topics": [], "error": "mqtt_not_connected"}
        except Exception:
            pass

        candidates = ["nodus/+/meta", "nodus/+/meta/switch"]
        if self.base_topic:
            candidates.extend(
                [
                    f"{self.base_topic}/nodus/+/meta",
                    f"{self.base_topic}/nodus/+/meta/switch",
                ]
            )
        topics = [topic for topic in candidates if topic in self.registered_topics]
        renewed: list[str] = []
        errors: list[str] = []
        for topic in topics:
            try:
                result = client.subscribe(topic, qos=0)
                rc = result[0] if isinstance(result, tuple) and result else 0
                if int(rc or 0) == 0:
                    renewed.append(topic)
                else:
                    errors.append(f"{topic}:rc={rc}")
            except Exception as exc:
                errors.append(f"{topic}:{type(exc).__name__}")
        if DEBUG:
            printDM(
                "[retained-refresh] device={} renewed={} errors={}".format(
                    normalize_hostname_base(device_id) or str(device_id or "").strip() or "unknown",
                    ",".join(renewed) or "none",
                    ",".join(errors) or "none",
                ),
                location=MODULE,
            )
        return {
            "ok": bool(renewed) and not errors,
            "topics": renewed,
            "errors": errors,
        }

    def _removed_nodus_topic_id(self, topic: str | None) -> str:
        parts = str(topic or "").strip().split("/")
        if len(parts) >= 2 and parts[0] == "nodus":
            return parts[1]
        if self.base_topic and len(parts) >= 3 and parts[0] == self.base_topic and parts[1] == "nodus":
            return parts[2]
        if len(parts) >= 2 and parts[0] == "switch":
            if len(parts) >= 4:
                return parts[1]
            switch_id, _pin = split_switch_id_and_pin(parts[1])
            return switch_id or parts[1]
        return ""
        
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
        return False

    def _allow_background_http_meta_discovery(self) -> bool:
        """
        Runtime Nodus discovery/settings must stay on MQTT.
        AP-mode onboarding uses /itaot-meta and /itaot-init outside this ingest loop.
        """
        return False

    @staticmethod
    def _normalize_liveness_state(status: str | None) -> str:
        s = str(status or "").strip().lower()
        if s in {"online", "degraded", "offline", "unknown", "pending", "migration_required"}:
            if s == "pending":
                return "unknown"
            return s
        return "unknown"

    def register_liveness_callback(self, callback) -> None:
        """Register a best-effort callback for Nodus liveness state changes."""
        if not callable(callback):
            return
        if callback not in self._liveness_status_callbacks:
            self._liveness_status_callbacks.append(callback)

    def _record_mqtt_seen(
        self,
        key: str | None,
        *,
        ts: float | None = None,
        retain: bool = False,
        report: bool = False,
    ) -> None:
        key_text = str(key or "").strip()
        if not key_text:
            return
        now_v = float(ts if ts is not None else time.time())
        target = self.retained_mqtt_seen if retain else self.last_mqtt_seen
        target[key_text] = now_v
        if not retain and report:
            self.last_nodus_report_seen[key_text] = now_v

    def _record_host_seen(
        self,
        host_like: str | None,
        *,
        ts: float | None = None,
        retain: bool = False,
        report: bool = False,
    ) -> None:
        base = self._normalize_host_key(host_like)
        if not base:
            return
        self._record_mqtt_seen(base, ts=ts, retain=retain, report=report)
        self._record_mqtt_seen(f"{base}.local", ts=ts, retain=retain, report=report)

    def _latest_seen(self, keys, source: dict[str, float] | None = None) -> float:
        source = source or {}
        latest = 0.0
        for key in keys or []:
            try:
                latest = max(latest, float(source.get(str(key or "").strip(), 0.0) or 0.0))
            except Exception:
                continue
        return latest

    def _switch_id_for_channel_id(self, channel_id: str | None) -> str:
        ch = str(channel_id or "").strip()
        if not ch:
            return ""
        ch_l = ch.lower()
        try:
            for meta in (self.nodus_switch_topic_map or {}).values():
                if str(meta.get("channel_id") or "").strip().lower() == ch_l:
                    return str(meta.get("switch_id") or "").strip()
        except Exception:
            pass
        try:
            for row in (self.data_logger.get_switch_identities() or []):
                row_ch = str(row.get("channel_id") or "").strip()
                if row_ch.lower() == ch_l:
                    return str(row.get("switch_id") or "").strip()
        except Exception:
            pass
        return ""

    def _liveness_keys_for(self, device_id: str | None, device_type: str | None = None) -> list[str]:
        keys: list[str] = []

        def _add(value: str | None) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            options = [raw]
            base = self._normalize_host_key(raw)
            if base:
                options.extend([base, f"{base}.local"])
            for item in options:
                text = str(item or "").strip()
                if text and text not in keys:
                    keys.append(text)

        dev = str(device_id or "").strip()
        _add(dev)

        channel_switch_id = ""
        if _looks_like_channel_id(dev):
            channel_switch_id = self._switch_id_for_channel_id(dev)
            _add(channel_switch_id)

        resolved_from = channel_switch_id or dev
        try:
            _add(self.resolve_nodus_hostname(resolved_from, device_type=device_type))
        except Exception:
            pass

        for host, peers in (self.host_to_peer_ids or {}).items():
            try:
                peer_set = {str(p or "").strip() for p in (peers or []) if str(p or "").strip()}
                if dev in peer_set or channel_switch_id in peer_set or host in keys or f"{host}.local" in keys:
                    _add(host)
                    for peer in peer_set:
                        _add(peer)
            except Exception:
                continue

        return keys

    def _resolve_liveness_base(self, device_id: str | None, device_type: str | None = None) -> str:
        dev = str(device_id or "").strip()
        physical = str((self.nodus_sensor_hosts or {}).get(dev) or "").strip()
        if physical:
            return self._normalize_host_key(physical) or physical

        keys = self._liveness_keys_for(device_id, device_type=device_type)
        if (device_type or "").lower() == "switch" or str(device_id or "").strip().startswith("switch-"):
            for key in keys:
                base = self._normalize_host_key(key)
                if not base or base.startswith("switch-"):
                    continue
                if (
                    base in (self.host_to_peer_ids or {})
                    or base in (self.mqtt_clients or set())
                    or base in (self.last_heartbeat_ts or {})
                    or base in (self.last_nodus_report_seen or {})
                ):
                    return base
        for key in keys:
            base = self._normalize_host_key(key)
            if base and base in (self.host_to_peer_ids or {}):
                return base
        try:
            resolved = self.resolve_nodus_hostname(str(device_id or "").strip(), device_type=device_type)
            base = self._normalize_host_key(resolved)
            if base:
                return base
        except Exception:
            pass
        return self._normalize_host_key(device_id) or str(device_id or "").strip()

    def get_nodus_liveness(
        self,
        device_id: str | None,
        *,
        device_type: str | None = None,
        now_ts: float | None = None,
    ) -> dict:
        """
        Return the canonical Nodus liveness snapshot for UI, HA, and commands.

        Availability "offline" is authoritative. Availability "online" is only
        a hint; fresh heartbeat or fresh data/state/event traffic is required
        before the device is considered online.
        """
        dev = str(device_id or "").strip()
        base = self._resolve_liveness_base(dev, device_type=device_type)
        keys = self._liveness_keys_for(dev or base, device_type=device_type)
        if base:
            for item in (base, f"{base}.local"):
                if item not in keys:
                    keys.append(item)

        now_v = float(now_ts if now_ts is not None else time.time())
        state = "unknown"
        reason = "no_recent_mqtt"
        availability = None
        try:
            availability_keys = keys
            if (
                base in self.nodus_host_sensors
                and (self._normalize_host_key(dev) or dev) == base
            ):
                availability_keys = [base, f"{base}.local"]
            for key in availability_keys:
                availability = self._get_nodus_availability(key)
                if availability:
                    break
        except Exception:
            availability = None

        interval = DEFAULT_HEARTBEAT_INTERVAL_S
        try:
            interval = float(self.heartbeat_interval_s_by_host.get(base, DEFAULT_HEARTBEAT_INTERVAL_S) or DEFAULT_HEARTBEAT_INTERVAL_S)
        except Exception:
            interval = DEFAULT_HEARTBEAT_INTERVAL_S
        if interval < MIN_HEARTBEAT_INTERVAL_S:
            interval = MIN_HEARTBEAT_INTERVAL_S

        last_seen = self._latest_seen(keys, self.last_mqtt_seen)
        retained_seen = self._latest_seen(keys, self.retained_mqtt_seen)
        report_seen = self._latest_seen(keys, self.last_nodus_report_seen)
        heartbeat_seen = self._latest_seen(keys, self.last_heartbeat_ts)
        heartbeat_age_s = (now_v - heartbeat_seen) if heartbeat_seen > 0.0 else None
        report_age_s = (now_v - report_seen) if report_seen > 0.0 else None
        last_seen_s = (now_v - last_seen) if last_seen > 0.0 else None

        if availability == "offline":
            state = "offline"
            reason = "availability_offline"
        elif report_seen > 0.0 and (heartbeat_seen <= 0.0 or report_seen >= heartbeat_seen):
            missed = max(0.0, now_v - report_seen)
            if missed <= (2.0 * interval):
                state = "online"
                reason = "report_recent"
            elif missed < (3.0 * interval):
                state = "degraded"
                reason = "report_stale"
            else:
                state = "offline"
                reason = "report_timeout"
        elif heartbeat_seen > 0.0:
            state = self._apply_heartbeat_timeout_state(base or dev, now_ts=now_v)
            reason = f"heartbeat_{state}"
            if self.heartbeat_stale.get(base) and state == "online":
                state = "degraded"
                reason = "heartbeat_stale"
        elif availability == "online" and last_seen > 0.0:
            missed = max(0.0, now_v - last_seen)
            if missed < (3.0 * interval):
                state = "degraded"
                reason = "availability_only"
            else:
                state = "offline"
                reason = "availability_timeout"
        else:
            stored = self._normalize_liveness_state(
                self.device_status.get(base)
                or self.device_status.get(f"{base}.local")
                or self.device_status.get(dev)
            )
            if stored in {"offline", "degraded"}:
                state = stored
                reason = "stored_status"

        peers = []
        try:
            peers = list((self.host_to_peer_ids or {}).get(base, []) or [])
        except Exception:
            peers = []
        if dev and dev not in peers:
            peers.append(dev)

        return {
            "device_id": dev or base,
            "host": base,
            "state": self._normalize_liveness_state(state),
            "reason": reason,
            "availability": availability,
            "last_seen_s": round(max(last_seen_s, 0.0), 1) if last_seen_s is not None else None,
            "last_report_s": round(max(report_age_s, 0.0), 1) if report_age_s is not None else None,
            "last_heartbeat_s": round(max(heartbeat_age_s, 0.0), 1) if heartbeat_age_s is not None else None,
            "retained_seen_s": round(max(now_v - retained_seen, 0.0), 1) if retained_seen > 0.0 else None,
            "heartbeat_stale": bool(self.heartbeat_stale.get(base) or self.heartbeat_stale.get(f"{base}.local")),
            "heartbeat_interval_s": interval,
            "peer_ids": peers,
        }

    def _heartbeat_liveness_timestamp(
        self,
        hb_ts: float | None,
        *,
        retain: bool,
        now_ts: float,
        interval_s: float,
    ) -> float:
        if hb_ts is None:
            return now_ts
        try:
            payload_ts = float(hb_ts)
        except Exception:
            return now_ts
        if retain:
            return payload_ts
        max_past_drift = max(HEARTBEAT_STALE_AFTER_S, float(interval_s or DEFAULT_HEARTBEAT_INTERVAL_S) * 3.0)
        if payload_ts < (now_ts - max_past_drift):
            return now_ts
        return payload_ts

    def _notify_liveness_status_change(self, base: str, status: str) -> None:
        callbacks = list(getattr(self, "_liveness_status_callbacks", []) or [])
        if not callbacks:
            return
        try:
            snapshot = self.get_nodus_liveness(base)
        except Exception:
            snapshot = {"device_id": base, "host": base, "state": status}
        for callback in callbacks:
            try:
                callback(base, status, snapshot)
            except Exception as exc:
                if DEBUG:
                    printDM(f"[liveness] callback failed for {base}: {exc}", location=MODULE)

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
            # No heartbeat yet: derive liveness from live Nodus reports
            # (data/state/event), not from retained broker replays or bare
            # availability=online hints.
            last_seen = float(
                self.last_nodus_report_seen.get(base)
                or self.last_nodus_report_seen.get(f"{base}.local")
                or 0.0
            )
            if last_seen <= 0.0:
                stored = self._normalize_liveness_state(self.device_status.get(base))
                return stored if stored in {"offline", "degraded"} else "unknown"
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

    def _broadcast_dashboard_inventory_changed(
        self,
        *,
        host: str | None = None,
        sensor_id: str | None = None,
        switch_id: str | None = None,
    ) -> None:
        """Tell dashboard clients to re-check sensor/switch inventory."""
        try:
            from . import saiWebRoutes as routes
            switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
            if not switch_broadcast:
                return

            payload = {
                "type": "dashboard_inventory_changed",
                "timestamp": get_timestamp(),
            }
            host_text = str(host or "").strip()
            sensor_text = str(sensor_id or "").strip()
            switch_text = str(switch_id or "").strip()
            if host_text:
                payload["host"] = host_text
            if sensor_text:
                payload["sensor_id"] = sensor_text
            if switch_text:
                payload["switch_id"] = switch_text

            self._schedule_coro(switch_broadcast(payload))
        except Exception:
            pass

    def _broadcast_switch_event(
        self,
        *,
        switch_id: str,
        channel_id: str | None,
        label: str | None,
        is_on: bool,
        source: str | None,
        timestamp: str | None = None,
    ) -> None:
        """Push a live switch update to the dashboard using both host and channel keys."""
        try:
            from . import saiWebRoutes as routes
            switch_broadcast = getattr(getattr(routes, "app", object()), "state", object()).switch_broadcast
            if not switch_broadcast:
                return

            switch_id_text = str(switch_id or "").strip()
            channel_id_text = str(channel_id or "").strip()
            label_text = str(label or channel_id_text or "").strip()
            if not switch_id_text or not label_text:
                return
            canonical_key = (
                f"{switch_id_text}::{channel_id_text}"
                if channel_id_text
                else f"{switch_id_text}::{label_text}"
            )

            payload = {
                "type": "switch_event",
                "key": canonical_key,
                "ui_key": f"{switch_id_text}::{label_text}",
                "state": bool(is_on),
                "timestamp": timestamp or get_timestamp(),
                "source": source,
            }
            if channel_id_text:
                payload["legacy_ui_key"] = f"{channel_id_text}::{label_text}"
            self._schedule_coro(switch_broadcast(payload))
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
        if not isinstance(payload, dict) or not payload:
            return {}
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

    def _normalize_calibration_sample_payload(self, sensor_id: str, payload: dict | None, *, topic: str, retain: bool) -> dict:
        body = dict(payload or {})

        def _to_int(value):
            try:
                return None if value is None or value == "" else int(value)
            except Exception:
                return None

        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        units = body.get("units") if isinstance(body.get("units"), dict) else {}
        metrics = body.get("metrics") if isinstance(body.get("metrics"), list) else []
        display_metrics = body.get("display_metrics") if isinstance(body.get("display_metrics"), list) else []
        normalized = {
            "message_id": str(body.get("message_id") or "").strip(),
            "sensor_id": str(body.get("sensor_id") or sensor_id or "").strip(),
            "status": str(body.get("status") or "").strip().lower(),
            "timestamp": body.get("timestamp"),
            "reference_ph": _to_float(body.get("reference_ph")),
            "sample_index": _to_int(body.get("sample_index")),
            "sample_count": _to_int(body.get("sample_count")),
            "metrics": list(metrics),
            "display_metrics": list(display_metrics),
            "values": dict(values),
            "units": dict(units),
            "soil_ph_offset": _to_float(body.get("soil_ph_offset")),
            "corrected_ph": _to_float(body.get("corrected_ph")),
            "raw_ph": _to_float(body.get("raw_ph")),
            "topic": topic,
            "retain": bool(retain),
            "kind": "calibration_sample",
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
                    sensor_id = str(
                        body.get("sensor_id")
                        or status_payload.get("sensor_id")
                        or device_id
                    ).strip()
                    result = {
                        "message_id": message_id,
                        "device_id": device_id,
                        "sensor_id": sensor_id,
                        "name": str(body.get("name") or "").strip(),
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

                if family == "event" and leaf == "calibration_sample":
                    normalized_sample = self._normalize_calibration_sample_payload(
                        device_id,
                        body,
                        topic=topic,
                        retain=retain,
                    )
                    if not normalized_sample:
                        return True
                    sensor_id = str(normalized_sample.get("sensor_id") or device_id).strip()
                    self.calibration_sample_by_sensor[sensor_id] = dict(normalized_sample)

                    message_id = str(normalized_sample.get("message_id") or "").strip()
                    if message_id:
                        bucket = self.calibration_samples_by_message.setdefault(message_id, [])
                        sample_index = normalized_sample.get("sample_index")
                        replaced = False
                        if sample_index is not None:
                            for idx, row in enumerate(bucket):
                                if row.get("sample_index") == sample_index:
                                    bucket[idx] = dict(normalized_sample)
                                    replaced = True
                                    break
                        if not replaced:
                            bucket.append(dict(normalized_sample))
                            bucket.sort(key=lambda row: (row.get("sample_index") is None, row.get("sample_index") or 0))

                    sample_index = normalized_sample.get("sample_index")
                    sample_total = normalized_sample.get("sample_count")
                    derived_progress = {
                        "sensor_id": sensor_id,
                        "status": "in_progress",
                        "calibrated": False,
                        "timestamp": normalized_sample.get("timestamp"),
                        "sample_index": sample_index,
                        "sample_total": sample_total,
                        "topic": topic,
                        "retain": bool(retain),
                        "kind": "calibration_sample",
                        "received_at": normalized_sample.get("received_at"),
                    }
                    self.calibration_progress_by_sensor[sensor_id] = derived_progress
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

    def publish_nodus_calibration(
        self,
        device_id: str,
        *,
        action: str,
        payload: dict | None = None,
        message_id: str | None = None,
        qos: int = 1,
        sensor_id: str = "",
        name: str = "",
    ) -> dict:
        """Publish a physical-device calibration command with optional sensor target."""
        device = str(device_id or "").strip()
        action_name = str(action or "").strip().lower()
        if not device or not action_name:
            return {"ok": False, "message_id": "", "topic": ""}
        if not message_id:
            local_epoch = _local_epoch_seconds(self.settings)
            message_id = f"cal-{local_epoch}-{action_name}-{device[:24]}"
        envelope = {
            "message_id": message_id,
            "action": action_name,
        }
        target_sensor = str(sensor_id or "").strip()
        target_name = str(name or "").strip()
        if target_sensor:
            envelope["sensor_id"] = target_sensor
        if target_name:
            envelope["name"] = target_name
        if payload is not None or action_name in {"apply", "set", "update"}:
            envelope["payload"] = payload or {}
        topic = f"nodus/{device}/calibration/set"
        ok = bool(self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False))
        if ok:
            with self._calibration_lock:
                self.calibration_message_device[message_id] = device
        return {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}

    def publish_nodus_config(
        self,
        device_id: str,
        *,
        payload: dict,
        message_id: str | None = None,
        qos: int = 1,
        restart: bool = False,
        onboard_token: str = "",
    ) -> dict:
        device = str(device_id or "").strip()
        if not device or not isinstance(payload, dict):
            return {"ok": False, "message_id": "", "topic": ""}
        if not message_id:
            local_epoch = _local_epoch_seconds(self.settings)
            message_id = f"cfg-{local_epoch}-{uuid.uuid4().hex[:8]}"
        envelope = {
            "message_id": message_id,
            "payload": dict(payload),
            "restart": bool(restart),
        }
        token = str(onboard_token or "").strip()
        if token:
            envelope["onboard_token"] = token
        topic = f"nodus/{device}/config/set"
        if DEBUG:
            printDM(
                f"[publish_nodus_config] topic={topic} message_id={message_id} client={self._describe_publish_client(use_ha_client=False)} payload={self._summarize_nodus_config_payload(envelope)}",
                location=MODULE,
            )
        ok = bool(self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False))
        if DEBUG:
            printDM(
                f"[publish_nodus_config] publish_result topic={topic} message_id={message_id} ok={ok}",
                location=MODULE,
            )
        if ok:
            with self._config_lock:
                self.config_message_device[message_id] = device
        return {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}

    def publish_nodus_restart(
        self,
        device_id: str,
        *,
        restart_mode: str = "soft",
        message_id: str | None = None,
        qos: int = 1,
    ) -> dict:
        """Publish a Nodus restart request over the config/set topic."""
        device = str(device_id or "").strip()
        mode = str(restart_mode or "soft").strip().lower() or "soft"
        if not device:
            return {"ok": False, "message_id": "", "topic": ""}
        if not message_id:
            local_epoch = _local_epoch_seconds(self.settings)
            message_id = f"rst-{local_epoch}-{uuid.uuid4().hex[:8]}"
        envelope = {
            "message_id": message_id,
            "payload": {},
            "restart": True,
            "restart_mode": mode,
        }
        topic = f"nodus/{device}/config/set"
        if DEBUG:
            printDM(
                f"[publish_nodus_restart] topic={topic} message_id={message_id} restart_mode={mode} client={self._describe_publish_client(use_ha_client=False)}",
                location=MODULE,
            )
        ok = bool(self.publish_json(topic, envelope, qos=qos, retain=False, use_ha_client=False))
        if DEBUG:
            printDM(
                f"[publish_nodus_restart] publish_result topic={topic} message_id={message_id} ok={ok}",
                location=MODULE,
            )
        if ok:
            with self._config_lock:
                self.config_message_device[message_id] = device
        return {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}

    async def wait_for_config_ack(self, message_id: str, timeout: float = 3.0) -> dict | None:
        deadline = time.time() + max(float(timeout), 0.0)
        while time.time() < deadline:
            with self._config_lock:
                hit = self.config_ack_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_config_result(self, message_id: str, timeout: float = 8.0) -> dict | None:
        deadline = time.time() + max(float(timeout), 0.0)
        while time.time() < deadline:
            with self._config_lock:
                hit = self.config_result_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

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

    async def wait_for_calibration_samples(self, message_id: str, expected_count: int | None = None, timeout: float = 30.0) -> list[dict]:
        deadline = time.time() + max(float(timeout), 0.0)
        target = None
        try:
            if expected_count is not None:
                target = max(int(expected_count), 0)
        except Exception:
            target = None

        last_seen: list[dict] = []
        while time.time() < deadline:
            with self._calibration_lock:
                rows = [dict(item) for item in self.calibration_samples_by_message.get(message_id, [])]
            last_seen = rows
            if rows:
                if target is None or target <= 0:
                    return rows
                if len(rows) >= target:
                    return rows
                highest_index = max(
                    (int(row.get("sample_index")) for row in rows if row.get("sample_index") is not None),
                    default=0,
                )
                if highest_index >= target:
                    return rows
            await asyncio.sleep(0.05)
        return last_seen

    async def wait_for_nodus_meta_patch(self, message_id: str, *, source: str | None = None, timeout: float = 4.0) -> dict | None:
        deadline = time.time() + max(float(timeout), 0.0)
        want_source = str(source or "").strip().lower()
        while time.time() < deadline:
            with self._meta_patch_lock:
                hit = self.meta_patch_by_message.get(str(message_id or "").strip())
                if isinstance(hit, dict):
                    if not want_source or str(hit.get("source") or "").strip().lower() == want_source:
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
            latest_sample = self.calibration_sample_by_sensor.get(sid)
            final_result = self.calibration_event_result_by_sensor.get(sid)
            if not any((status, progress, latest_sample, final_result)):
                return None
            out = {}
            if status:
                out["status"] = dict(status)
            if progress:
                out["progress"] = dict(progress)
            if latest_sample:
                out["sample"] = dict(latest_sample)
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
            now = time.time()
            with self._config_lock:
                if family == "config" and leaf == "ack":
                    message_id = str(payload.get("message_id") or "").strip()
                    if message_id:
                        self.config_ack_by_message[message_id] = {
                            "message_id": message_id,
                            "device_id": device_id,
                            "accepted": bool(payload.get("accepted", False)),
                            "duplicate": bool(payload.get("duplicate", False)),
                            "error": str(payload.get("error") or "").strip(),
                            "topic": topic,
                            "received_at": now,
                        }
                elif family == "config" and leaf == "result":
                    message_id = str(payload.get("message_id") or "").strip()
                    if message_id:
                        result = {
                            "message_id": message_id,
                            "device_id": device_id,
                            "sensor_id": str(payload.get("sensor_id") or "").strip(),
                            "name": str(payload.get("name") or "").strip(),
                            "applied": bool(payload.get("applied", False)),
                            "updated": payload.get("updated"),
                            "error": str(payload.get("error") or "").strip(),
                            "topic": topic,
                            "received_at": now,
                        }
                        self.config_result_by_message[message_id] = result
                        self._clear_switch_config_command_by_message(message_id)
                elif family == "onboard" and leaf == "hello":
                    self._record_nodus_board_type(_extract_nodus_board_type(payload), device_id)
                    sensor_payload = payload.get("sensor") if isinstance(payload.get("sensor"), dict) else {}
                    self._record_nodus_sensor_hardware(
                        _extract_nodus_sensor_hardware(payload),
                        device_id,
                        payload.get("hostname"),
                        sensor_payload.get("sensor_id"),
                    )
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
                    
            for topic in self.registered_topics:
                client.subscribe(topic)
                if DEBUG:
                    printDM(f"Subscribed to topic: {topic}", location=MODULE)

            # signal ready after baseline subscriptions have been registered
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(self._connected_evt.set)
            except Exception:
                pass

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

    def _mark_weewx_station_seen(self, sensor_id: str, display_values: dict) -> None:
        """Update runtime station identity, display metrics, and liveness state."""
        values = dict(display_values or {})
        if "Rain" in values:
            values.setdefault(WEEWX_RAIN_24H_METRIC, None)
        self.expected_gauge_map[sensor_id] = [
            metric for metric in WEEWX_DISPLAY_METRICS if metric in values
        ]
        self.device_type[sensor_id] = "weewx"
        station_location = "Weather Station"
        try:
            from .saiSensorSettingsManager import SensorSettingsManager

            station_location = (
                SensorSettingsManager("sensor_settings").get_setting(
                    sensor_id,
                    "Sensor.LOCATION",
                    station_location,
                )
                or station_location
            )
        except Exception:
            pass
        self.device_location[sensor_id] = station_location
        self.last_mqtt_seen[sensor_id] = time.time()
        self._mark_host_status(sensor_id, "online")

    def _maybe_handle_weewx_mqtt(self, topic: str, payload_text: str) -> bool:
        """Ingest configured WeeWX MQTT publications as a station sensor."""
        try:
            if not self.weewx_mqtt_enabled:
                return False
            if not weewx_topic_matches(self.weewx_mqtt_topic, topic):
                return False
            reading = normalize_weewx_mqtt_payload(
                topic,
                payload_text,
                base_topic=self.weewx_mqtt_topic.rstrip("/#"),
            )
            if reading is None or not reading.values:
                return False

            sensor_id = self.weewx_sensor_id or WEEWX_DEFAULT_SENSOR_ID
            values = dict(reading.values)
            single_field_update = len(values) == 1 and reading.timestamp is None
            previous_values = {}
            if single_field_update:
                try:
                    previous = self.data_logger.get_latest_values(sensor_id) or {}
                    previous_values = dict(previous) if isinstance(previous, dict) else {}
                except Exception:
                    previous_values = {}
                if previous_values:
                    metric_name, metric_value = next(iter(values.items()))
                    if (
                        metric_name in previous_values
                        and _values_equal(previous_values.get(metric_name), metric_value)
                    ):
                        display_values = dict(previous_values)
                        display_values.update(values)
                        self._mark_weewx_station_seen(sensor_id, display_values)
                        if DEBUG:
                            printDM(
                                f"Skipped duplicate WeeWX MQTT field from {sensor_id}: {metric_name}",
                                location=MODULE,
                            )
                        return True

            value_signature = tuple(sorted((str(k), values[k]) for k in values.keys()))
            signature = (sensor_id, reading.timestamp, value_signature)
            burst_signature = (sensor_id, value_signature)
            now_mono = time.monotonic()
            last_signature = getattr(self, "_last_weewx_mqtt_signature", None)
            last_burst_signature = getattr(self, "_last_weewx_mqtt_burst_signature", None)
            last_signature_mono = float(getattr(self, "_last_weewx_mqtt_signature_mono", 0.0) or 0.0)
            burst_window_sec = min(10.0, max(2.0, float(getattr(self, "weewx_update_period_sec", 300.0) or 300.0) * 0.1))
            duplicate_payload = (
                signature == last_signature
                or (
                    burst_signature == last_burst_signature
                    and (now_mono - last_signature_mono) <= burst_window_sec
                )
            )
            if duplicate_payload:
                self.last_mqtt_seen[sensor_id] = time.time()
                self._mark_host_status(sensor_id, "online")
                return True
            self.data_logger.log_readings(reading.timestamp, sensor_id, values)
            self._last_weewx_mqtt_signature = signature
            self._last_weewx_mqtt_burst_signature = burst_signature
            self._last_weewx_mqtt_signature_mono = now_mono
            display_values = dict(previous_values) if previous_values else {}
            display_values.update(values)
            self._mark_weewx_station_seen(sensor_id, display_values)
            if DEBUG:
                printDM(f"Stored WeeWX MQTT data from {sensor_id}:{values}", location=MODULE)
            return True
        except Exception as exc:
            printDM(f"[weewx-mqtt] ingest failed: {exc}", location=MODULE)
            return False

    def _on_message(self, client, userdata, msg):
        """Serialize MQTT callbacks with device removal and suppress removed identities."""
        topic = str(getattr(msg, "topic", "") or "")
        with self._callback_lock:
            device_id = self._removed_nodus_topic_id(topic)
            is_onboarding_hello = topic.endswith("/onboard/hello")
            if device_id and not is_onboarding_hello and self.is_nodus_device_removed(device_id):
                if DEBUG:
                    printDM(f"[removed-nodus] ignored {topic}", location=MODULE)
                return
            return self._on_message_unlocked(client, userdata, msg)

    def _on_message_unlocked(self, client, userdata, msg):
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
                self.handle_nodus_switch_topic(topic, payload_text, retain=retain)
            except Exception as e:
                if DEBUG:
                    printDM(f"[on_message] nodus switch parser skipped: {e}", location=MODULE)

        # --- parse JSON if possible ---
        try:
            data = json.loads(payload_text)
        except Exception:
            data = None

        try:
            if self._maybe_handle_weewx_mqtt(topic, payload_text):
                return
        except Exception as e:
            if DEBUG:
                printDM(f"[on_message] WeeWX MQTT parser skipped: {e}", location=MODULE)

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
                if (
                    (not self.nodus_debug_data_only)
                    and len(parts) > id_index + 2
                    and parts[id_index + 1] == "meta"
                    and parts[id_index + 2] == "patch"
                ):
                    nodus_id = parts[id_index]
                    ok_patch, _ = self._apply_nodus_meta_patch(
                        data if isinstance(data, dict) else {},
                        topic_device_id=nodus_id,
                        retain=retain,
                    )
                    if ok_patch:
                        return
                if (
                    (not self.nodus_debug_data_only)
                    and len(parts) == id_index + 3
                    and parts[id_index + 1] == "meta"
                    and parts[id_index + 2] == "switch"
                ):
                    nodus_id = parts[id_index]
                    if _looks_like_channel_id(nodus_id):
                        return
                    ok_switch_meta, _ = self._parse_and_subscribe_from_nodus_switch_meta(
                        data if isinstance(data, dict) else {},
                        topic_device_id=nodus_id,
                        retain=retain,
                    )
                    if ok_switch_meta:
                        return
                if (
                    (not self.nodus_debug_data_only)
                    and len(parts) == id_index + 2
                    and parts[id_index + 1] == "meta"
                ):
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
                if (
                    (not self.nodus_debug_data_only)
                    and len(parts) == id_index + 3
                    and parts[id_index + 1] == "fwupdate"
                    and parts[id_index + 2] == "result"
                ):
                    nodus_id = parts[id_index]
                    if _looks_like_channel_id(nodus_id):
                        return
                    base = self._host_from_sid_base(nodus_id)
                    if not base:
                        return
                    payload_obj = data if isinstance(data, dict) else {"raw": payload_text}
                    result = dict(payload_obj)
                    result["device_id"] = nodus_id
                    result["topic"] = topic
                    result["received_at"] = time.time()
                    self.fwupdate_result_by_device[base] = result
                    self.fwupdate_result_by_device[f"{base}.local"] = result
                    self.fwupdate_result_by_device[nodus_id] = result
                    self._record_host_seen(base, ts=result["received_at"], retain=retain, report=False)
                    phase = str(result.get("phase") or "").strip().lower()
                    if phase in {"applied", "complete", "completed"}:
                        self._mark_host_status(base, "online")
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
                    self._record_host_seen(base, ts=now_t, retain=retain, report=(not retain and not stale))

                    if isinstance(data, dict):
                        self.last_heartbeat_payload[base] = dict(data)
                        self.last_heartbeat_payload[f"{base}.local"] = dict(data)

                    if stale:
                        self._mark_host_status(base, "offline" if retain else "unknown")
                        return

                    heartbeat_liveness_ts = self._heartbeat_liveness_timestamp(
                        hb_ts,
                        retain=retain,
                        now_ts=now_t,
                        interval_s=hb_interval,
                    )
                    self.last_heartbeat_ts[base] = heartbeat_liveness_ts
                    self.last_heartbeat_ts[f"{base}.local"] = heartbeat_liveness_ts

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
                    status = self._parse_availability_payload(payload_text, data)
                    if _looks_like_channel_id(nodus_id):
                        if status:
                            now_t = time.time()
                            self._record_mqtt_seen(nodus_id, ts=now_t, retain=retain, report=False)
                            self.nodus_availability[nodus_id] = status
                            switch_id = self._switch_id_for_channel_id(nodus_id)
                            if switch_id:
                                self._record_mqtt_seen(switch_id, ts=now_t, retain=retain, report=False)
                                host = self.resolve_nodus_hostname(switch_id, device_type="switch")
                                if host:
                                    peers = self.host_to_peer_ids.setdefault(host, [])
                                    for peer in (switch_id, nodus_id):
                                        if peer and peer not in peers:
                                            peers.append(peer)
                                    self._record_host_seen(host, ts=now_t, retain=retain, report=False)
                                    derived = self.get_nodus_liveness(nodus_id, device_type="switch", now_ts=now_t).get("state", "unknown")
                                    if derived == "unknown" and status == "online" and not retain:
                                        derived = "degraded"
                                    self._mark_host_status(host, derived)
                        return
                    if status:
                        base = (
                            self.nodus_sensor_hosts.get(nodus_id)
                            or self._host_from_sid_base(nodus_id)
                        )
                        if base:
                            if not retain:
                                self._maybe_add_mqtt_client(base)
                            else:
                                self._maybe_promote_retained_host(base, source="availability")
                            now_t = time.time()
                            self._record_host_seen(base, ts=now_t, retain=retain, report=False)
                            # Sensor availability is child-scoped even when its
                            # liveness heartbeat is emitted by the physical host.
                            self.nodus_availability[nodus_id] = status
                            self.nodus_availability[f"{nodus_id}.local"] = status

                            peers = self.host_to_peer_ids.setdefault(base, [])
                            if nodus_id and nodus_id not in peers:
                                peers.append(nodus_id)

                            if status == "online":
                                self.device_offline_count[base] = 0
                                derived = self.get_nodus_liveness(base, now_ts=now_t).get("state", "unknown")
                                if retain and derived == "degraded":
                                    derived = "unknown"
                                self._mark_host_status(base, derived)
                                # Availability-only recovery is weak until heartbeat/data resumes.
                                if base not in self.last_heartbeat_ts:
                                    self.heartbeat_stale[base] = True
                                    self.heartbeat_stale[f"{base}.local"] = True
                            elif status == "offline" and nodus_id == base:
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

                if not self.nodus_debug_data_only:
                    self._ensure_shadow_for_live_nodus_sensor_data(
                        sensor_id=sensor_id,
                        topic=topic,
                        values=values,
                        display_metrics=display_metrics,
                    )

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
                    self._record_mqtt_seen(sensor_id, retain=retain, report=(not retain))
                except Exception:
                    pass
                if not retain:
                    try:
                        notify_key = str(sensor_id or "").strip().lower()
                        if notify_key and notify_key not in self._dashboard_inventory_notified_sensors:
                            self._dashboard_inventory_notified_sensors.add(notify_key)
                            notify_host = self._host_from_topic_or_sid(topic, sensor_id)
                            self._broadcast_dashboard_inventory_changed(host=notify_host, sensor_id=sensor_id)
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
                        self._record_host_seen(host, ts=now_t, retain=retain, report=(not retain))

                        if not retain:
                            self._mark_host_status(host, self.get_nodus_liveness(host, now_ts=now_t).get("state", "online"))
                        else:
                            self._mark_host_status(host, self.get_nodus_liveness(host, now_ts=now_t).get("state", "unknown"))
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
                    self._record_mqtt_seen(base_id, ts=now_t, retain=retain, report=(not retain))
                    try:
                        host = self._host_from_topic_or_sid(None, base_id)
                        if host:
                            self._record_host_seen(host, ts=now_t, retain=retain, report=(not retain))
                    except Exception:
                        pass   
                """
                try:
                    from . import saiWebRoutes as routes
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
                        self._mark_host_status(host, self.get_nodus_liveness(host, now_ts=time.time()).get("state", "unknown"))
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
            from .saiSettings import saiSettings
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
        mapped = str(
            self.nodus_sensor_hosts.get(str(sid or "").strip()) or ""
        ).strip()
        if mapped:
            return self._normalize_host_key(mapped)
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

    def _normalize_display_styles(self, raw_styles, default_style: str = "Graph24hr") -> list[str]:
        """
        Normalize per-metric display style hints into an ordered six-slot list.
        Returns [] when the payload does not contain any usable style hints.
        """
        if isinstance(raw_styles, dict):
            values = [raw_styles.get(f"METRIC_{idx}", "") for idx in range(1, 7)]
        elif isinstance(raw_styles, (list, tuple)):
            values = list(raw_styles)
        else:
            return []

        def _canonical_style(raw_value) -> str:
            style = str(raw_value or "").strip().lower()
            if not style:
                return default_style
            if style == "gauge":
                return "Gauge"
            if style in {"graph", "graph24", "graph24hr", "24h", "24hr"}:
                return "Graph24hr"
            if style in {"graph6", "graph6hr", "6h", "6hr"}:
                return "Graph6hr"
            return default_style

        if not any(str(item or "").strip() for item in values):
            return []

        ordered: list[str] = []
        for idx in range(6):
            raw_value = values[idx] if idx < len(values) else default_style
            ordered.append(_canonical_style(raw_value))
        return ordered

    def _ensure_shadow_for_live_nodus_sensor_data(
        self,
        *,
        sensor_id: str,
        topic: str,
        values: dict,
        display_metrics: list[str] | None = None,
    ) -> None:
        """Create missing local shadow settings for a Nodus sensor seen via live data."""
        sid = str(sensor_id or "").strip()
        if not sid:
            return

        key = sid.lower()
        if key in self._live_sensor_shadow_seeded:
            return

        now_mono = time.monotonic()
        last_attempt = float(self._live_sensor_shadow_attempt_at.get(key, 0.0) or 0.0)
        if last_attempt and now_mono - last_attempt < 300.0:
            return
        self._live_sensor_shadow_attempt_at[key] = now_mono

        try:
            from .saiSensorSettingsManager import SensorSettingsManager

            sensor_mgr = SensorSettingsManager()
            new_path, legacy_path = sensor_mgr.get_candidate_paths(sid)
            if new_path.exists() or legacy_path.exists():
                self._live_sensor_shadow_seeded.add(key)
                return
        except Exception as exc:
            if DEBUG:
                printDM(f"[live-shadow] settings existence check failed for {sid}: {exc}", location=MODULE)

        device_name = self._infer_sensor_device_name("", sid)
        if not device_name:
            metric_names = {str(metric or "").strip().lower() for metric in (values or {}).keys()}
            if any(name.startswith("soil") for name in metric_names):
                device_name = "soil"
            elif "co2" in metric_names:
                device_name = "co2"
            elif "air quality" in metric_names or "gas" in metric_names:
                device_name = "aqi"
            elif "plant vpd" in metric_names or "plant temperature" in metric_names:
                device_name = "apvpd"
            elif "light intensity" in metric_names or "estimated ppfd" in metric_names:
                device_name = "lux"
            elif "ambient vpd" in metric_names and "rel-humidity" in metric_names:
                device_name = "aht"

        serial = ""
        if "-" in sid and "-i2c-" not in sid and sid.count("-") < 3:
            serial = sid.split("-", 1)[1].strip()

        location = "Unknown"
        try:
            for loc_key in (topic, sid):
                loc = str((self.device_location or {}).get(loc_key, "") or "").strip()
                if loc and loc.lower() not in {"unknown", "n/a", "na", "none", "-"}:
                    location = loc
                    break
        except Exception:
            location = "Unknown"

        host = self._host_from_topic_or_sid(topic, sid) or sid
        try:
            self._ensure_settings_from_itaot(
                {"HOSTNAME": host},
                host,
                [
                    {
                        "sensor_id": sid,
                        "device_type": "nodus",
                        "device": device_name,
                        "sensor_type": "nodus",
                        "location": location,
                        "serial": serial,
                        "display_metrics": list(display_metrics or []),
                    }
                ],
                [],
            )
            self._live_sensor_shadow_seeded.add(key)
        except Exception as exc:
            if DEBUG:
                printDM(f"[live-shadow] seed failed for {sid}: {exc}", location=MODULE)

    @staticmethod
    def _meta_metric_slot_map(raw_values) -> OrderedDict[str, str]:
        slots: OrderedDict[str, str] = OrderedDict(
            (f"METRIC_{idx}", "") for idx in range(1, 7)
        )
        if isinstance(raw_values, dict):
            for idx in range(1, 7):
                key = f"METRIC_{idx}"
                value = raw_values.get(key)
                if value is not None:
                    slots[key] = str(value)
            return slots
        if isinstance(raw_values, (list, tuple)):
            for idx, value in enumerate(list(raw_values)[:6], start=1):
                slots[f"METRIC_{idx}"] = "" if value is None else str(value)
        return slots

    def _find_nodus_meta_cache_key(
        self,
        device_id: str | None,
        *,
        topic_device_id: str | None = None,
    ) -> str | None:
        candidates: list[str] = []
        search_tokens: set[str] = set()

        def _add_candidate(raw_value: str | None, *, device_type: str | None = None) -> None:
            raw = str(raw_value or "").strip()
            if not raw:
                return
            for candidate in (
                raw,
                self._normalize_host_key(raw),
                self._host_from_sid_base(raw),
                self.resolve_nodus_hostname(raw, device_type=device_type),
            ):
                text = str(candidate or "").strip()
                if text and text not in candidates:
                    candidates.append(text)
                if text:
                    search_tokens.add(text.lower())
                normalized = self._normalize_host_key(text)
                if normalized:
                    search_tokens.add(normalized.lower())

        device_text = str(device_id or "").strip()
        topic_text = str(topic_device_id or "").strip()
        _add_candidate(device_text)
        _add_candidate(topic_text)
        if device_text.startswith("switch-"):
            _add_candidate(device_text, device_type="switch")
        if topic_text.startswith("switch-"):
            _add_candidate(topic_text, device_type="switch")

        for candidate in candidates:
            if candidate in self.discovery_cache:
                return candidate

        def _meta_contains_identifier(meta: dict, raw_value: str) -> bool:
            if not isinstance(meta, dict):
                return False
            want = str(raw_value or "").strip().lower()
            if not want:
                return False

            observed: set[str] = set()

            def _observe(value) -> None:
                text = str(value or "").strip()
                if not text:
                    return
                observed.add(text.lower())
                normalized = self._normalize_host_key(text)
                if normalized:
                    observed.add(normalized.lower())

            _observe(meta.get("device_id"))
            _observe(meta.get("hostname"))

            sensor = meta.get("sensor") if isinstance(meta.get("sensor"), dict) else {}
            _observe(sensor.get("sensor_id"))
            sensors = meta.get("sensors")
            if isinstance(sensors, list):
                for row in sensors:
                    if isinstance(row, dict):
                        _observe(row.get("sensor_id"))
                        _observe(row.get("config_file"))

            switch = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
            _observe(switch.get("device_id"))
            _observe(switch.get("switch_device_id"))

            channels = switch.get("channels")
            if isinstance(channels, list):
                for row in channels:
                    if not isinstance(row, dict):
                        continue
                    _observe(row.get("channel_id"))

            return want in observed

        if search_tokens:
            for host, meta in (self.discovery_cache or {}).items():
                for token in search_tokens:
                    if _meta_contains_identifier(meta, token):
                        return host

        for raw in (device_text, topic_text):
            if not raw:
                continue
            for host, peers in (self.host_to_peer_ids or {}).items():
                try:
                    if raw == host or raw in (peers or []):
                        if host in self.discovery_cache:
                            return host
                    normalized = self._normalize_host_key(raw)
                    if normalized and normalized == host and host in self.discovery_cache:
                        return host
                except Exception:
                    continue
        return None

    def _apply_nodus_meta_patch_update(self, meta: dict, update: dict) -> bool:
        if not isinstance(meta, dict) or not isinstance(update, dict):
            return False

        section = str(update.get("section") or "").strip()
        key = str(update.get("key") or "").strip()
        if not section or not key:
            return False

        value = update.get("value")
        section_key = section.lower()
        key_upper = key.upper()

        def _ensure_block(parent: dict, name: str) -> dict:
            block = parent.get(name)
            if not isinstance(block, dict):
                block = {}
                parent[name] = block
            return block

        def _update_group_location(raw_location) -> None:
            location_group = _ensure_block(meta, "location_group")
            location_group["location"] = "" if raw_location is None else str(raw_location)

        def _sensor_targets() -> list[dict]:
            want_id = str(update.get("sensor_id") or "").strip().lower()
            want_name = str(update.get("name") or "").strip().lower()
            primary = meta.get("sensor") if isinstance(meta.get("sensor"), dict) else None
            targets: list[dict] = []
            sensors = meta.get("sensors")
            if isinstance(sensors, list) and (want_id or want_name):
                for row in sensors:
                    if not isinstance(row, dict):
                        continue
                    row_id = str(row.get("sensor_id") or "").strip().lower()
                    row_name = str(row.get("config_file") or row.get("name") or "").strip().lower()
                    if want_id and row_id != want_id:
                        continue
                    if want_name and row_name != want_name:
                        continue
                    targets.append(row)
            if not targets and primary is not None:
                primary_id = str(primary.get("sensor_id") or "").strip().lower()
                primary_name = str(
                    primary.get("config_file")
                    or primary.get("name")
                    or ""
                ).strip().lower()
                if (
                    (not want_id or primary_id == want_id)
                    and (not want_name or primary_name == want_name)
                ):
                    targets.append(primary)
            if not targets and not (want_id or want_name):
                if primary is None:
                    primary = _ensure_block(meta, "sensor")
                targets.append(primary)

            # Keep the primary compatibility view synchronized when it describes
            # the same child as a targeted `meta.sensors` entry.
            if targets and primary is not None and primary not in targets:
                target_id = str(targets[0].get("sensor_id") or "").strip().lower()
                primary_id = str(primary.get("sensor_id") or "").strip().lower()
                if target_id and target_id == primary_id:
                    targets.append(primary)
            return targets

        if section_key == "display":
            targets = _sensor_targets()
            for sensor in targets:
                display_metrics = self._meta_metric_slot_map(sensor.get("display_metrics"))
                display_metrics[key_upper] = "" if value is None else str(value)
                sensor["display_metrics"] = dict(display_metrics)
            return bool(targets)

        if section_key == "display.style":
            targets = _sensor_targets()
            for sensor in targets:
                display_styles = self._meta_metric_slot_map(sensor.get("display_styles"))
                display_styles[key_upper] = "" if value is None else str(value)
                sensor["display_styles"] = dict(display_styles)
            return bool(targets)

        if section_key == "sensor":
            field_name = {
                "LOCATION": "location",
                "DEVICE": "device",
                "TYPE": "type",
                "SENSOR_ID": "sensor_id",
                "SERIAL_NUM": "serial",
                "DEVICE_SERIAL_NUM": "serial",
                "DATA_TOPIC": "data_topic",
                "EVENT_TOPIC": "event_topic",
                "AVAILABILITY_TOPIC": "availability_topic",
            }.get(key_upper, key.lower())
            targets = _sensor_targets()
            for sensor in targets:
                sensor[field_name] = value
            if key_upper == "LOCATION" and not (
                update.get("sensor_id") or update.get("name")
            ):
                _update_group_location(value)
            return bool(targets)

        if section_key == "profile":
            profile = _ensure_block(meta, "profile")
            field_name = {"ACTIVE_PROFILE": "active_profile"}.get(key_upper, key.lower())
            profile[field_name] = value
            return True

        if section_key == "network":
            network = _ensure_block(meta, "network")
            field_name = {
                "HOSTNAME": "hostname",
                "SSID": "ssid",
                "PASSWORD": "password",
            }.get(key_upper, key.lower())
            network[field_name] = value
            if field_name == "hostname":
                meta["hostname"] = value
            return True

        if section_key == "mqtt":
            mqtt_meta = _ensure_block(meta, "mqtt")
            field_name = {
                "BROKER": "broker",
                "PORT": "port",
                "USE_TLS": "use_tls",
                "BASE_TOPIC": "base_topic",
                "USERNAME": "username",
                "PASSWORD": "password",
            }.get(key_upper, key.lower())
            mqtt_meta[field_name] = value
            return True

        if section_key in {"homeassistant", "time"}:
            block = _ensure_block(meta, section_key)
            block[key.lower()] = value
            return True

        if section_key == "calibration":
            targets = _sensor_targets()
            if update.get("sensor_id") or update.get("name"):
                for sensor in targets:
                    calibration = _ensure_block(sensor, "calibration")
                    calibration[key_upper] = value
                return bool(targets)
            calibration = _ensure_block(meta, "calibration")
            calibration[key_upper] = value
            return True

        if section_key.startswith("calibration."):
            child_name = section.split(".", 1)[1].strip()
            if not child_name:
                return False
            targets = _sensor_targets()
            if update.get("sensor_id") or update.get("name"):
                for sensor in targets:
                    calibration = _ensure_block(sensor, "calibration")
                    child = _ensure_block(calibration, child_name)
                    child[key_upper] = value
                return bool(targets)
            calibration = _ensure_block(meta, "calibration")
            child = _ensure_block(calibration, child_name)
            child[key_upper] = value
            return True

        if section_key != "switch":
            return False

        switch = _ensure_block(meta, "switch")
        top_level_field = {
            "SWITCH_DEVICE_ID": "device_id",
            "SWITCH_LOCATION": "location",
            "DEVICE_SERIAL_NUM": "serial",
            "TYPE": "type",
        }.get(key_upper)
        if top_level_field:
            switch[top_level_field] = value
            if key_upper == "SWITCH_LOCATION":
                _update_group_location(value)
            return True

        match = re.fullmatch(r"SWITCH_(\d+)_(.+)", key_upper)
        if not match:
            return False

        channel_index = max(int(match.group(1)), 1)
        suffix = match.group(2)
        channels = switch.get("channels")
        if not isinstance(channels, list):
            channels = []
            switch["channels"] = channels
        while len(channels) < channel_index:
            channels.append({})
        row = channels[channel_index - 1]
        if not isinstance(row, dict):
            row = {}
            channels[channel_index - 1] = row
        row.setdefault("index", channel_index)

        field_name = {
            "LABEL": "label",
            "CHANNEL_ID": "channel_id",
            "LAST_STATE": "state",
            "ENABLE_PIN": "enable_pin",
            "PIN": "pin",
            "EVENT_TOPIC": "event_topic",
            "STATE_TOPIC": "state_topic",
            "SET_TOPIC": "set_topic",
            "AVAILABILITY_TOPIC": "availability_topic",
        }.get(suffix, suffix.lower())
        row[field_name] = value
        return True

    def _apply_nodus_meta_patch(
        self,
        patch: dict,
        *,
        topic_device_id: str | None = None,
        retain: bool = False,
    ) -> tuple[bool, bool]:
        if not isinstance(patch, dict):
            return False, False

        # Never retain or mirror Wi-Fi credentials echoed by older firmware.
        # They are write-only fleet-operation inputs from Sensorius' view.
        patch = copy.deepcopy(patch)
        patch["updates"] = [
            update
            for update in (patch.get("updates") or [])
            if not (
                isinstance(update, dict)
                and str(update.get("section") or "").strip().lower() == "network"
                and str(update.get("key") or "").strip().upper() in {"SSID", "PASSWORD"}
            )
        ]

        schema = str(patch.get("schema") or "").strip().lower()
        if schema and schema != "nodus-meta-patch/v1":
            return False, False

        device_id = str(patch.get("device_id") or topic_device_id or "").strip()
        if not device_id:
            return False, False

        cache_key = self._find_nodus_meta_cache_key(device_id, topic_device_id=topic_device_id)
        if not cache_key:
            if DEBUG:
                printDM(
                    f"[nodus-meta-patch] no cached meta snapshot for {device_id}",
                    location=MODULE,
                )
            return False, False

        cached_meta = self.discovery_cache.get(cache_key)
        if not isinstance(cached_meta, dict):
            return False, False

        patched_meta = copy.deepcopy(cached_meta)
        if "timestamp" in patch:
            patched_meta["timestamp"] = patch.get("timestamp")
        if not str(patched_meta.get("device_id") or "").strip():
            patched_meta["device_id"] = cache_key

        message_id = str(patch.get("message_id") or "").strip()
        patch_source = str(patch.get("source") or "").strip()
        applied_any = False
        system_patch_info: dict[str, object] = {"HOSTNAME": cache_key}
        system_patch_changed = False
        sensor_patch_info: dict[str, dict[str, dict[str, object]]] = {}
        sensor_patch_changed = False
        live_switch_state_updates: list[dict[str, object]] = []

        for update in (patch.get("updates") or []):
            applied_any = self._apply_nodus_meta_patch_update(patched_meta, update) or applied_any
            if not isinstance(update, dict):
                continue
            section_raw = str(update.get("section") or "").strip()
            section = section_raw.lower()
            key = str(update.get("key") or "").strip()
            if not key:
                continue
            if section == "calibration" or section.startswith("calibration."):
                target_sensor_id = str(update.get("sensor_id") or "").strip()
                target_name = str(update.get("name") or "").strip().lower()
                if not target_sensor_id and target_name:
                    for sid, config_file in self.nodus_sensor_config_files.items():
                        if (
                            str(config_file or "").strip().lower() == target_name
                            and self.nodus_sensor_hosts.get(sid) == cache_key
                        ):
                            target_sensor_id = sid
                            break
                if not target_sensor_id:
                    primary = (
                        patched_meta.get("sensor")
                        if isinstance(patched_meta.get("sensor"), dict)
                        else {}
                    )
                    target_sensor_id = str(primary.get("sensor_id") or cache_key).strip()
                block_name = "Calibration"
                if section.startswith("calibration."):
                    suffix = section_raw.split(".", 1)[1].strip() if "." in section_raw else ""
                    if suffix:
                        block_name = f"Calibration.{suffix}"
                target_blocks = sensor_patch_info.get(target_sensor_id)
                if not isinstance(target_blocks, dict):
                    target_blocks = {}
                    sensor_patch_info[target_sensor_id] = target_blocks
                block = target_blocks.get(block_name)
                if not isinstance(block, dict):
                    block = {}
                    target_blocks[block_name] = block
                block[key] = update.get("value")
                sensor_patch_changed = True
                continue
            if section == "switch":
                match = re.fullmatch(r"SWITCH_(\d+)_LAST_STATE", key, flags=re.IGNORECASE)
                if match:
                    try:
                        channel_index = max(int(match.group(1)), 1)
                    except Exception:
                        channel_index = 0
                    if channel_index > 0:
                        live_switch_state_updates.append(
                            {
                                "channel_index": channel_index,
                                "state": update.get("value"),
                            }
                        )
            block_name = {
                "network": "Network",
                "profile": "Profile",
                "mqtt": "MQTT",
                "homeassistant": "HomeAssistant",
                "time": "Time",
            }.get(section)
            if not block_name:
                continue
            if block_name == "Network" and key.strip().lower() in {"ipv4addr", "ipv4"}:
                continue
            block = system_patch_info.get(block_name)
            if not isinstance(block, dict):
                block = {}
                system_patch_info[block_name] = block
            block[key] = update.get("value")
            system_patch_changed = True

        if not applied_any:
            return False, False

        if message_id:
            with self._meta_patch_lock:
                self.meta_patch_by_message[message_id] = dict(patch)

        self.discovery_cache[cache_key] = patched_meta
        ok_meta, subscribed = self._parse_and_subscribe_from_nodus_meta(
            patched_meta,
            topic_device_id=cache_key,
            retain=retain,
        )
        if system_patch_changed:
            try:
                self._ensure_settings_from_itaot(
                    system_patch_info,
                    cache_key,
                    [],
                    [],
                )
            except Exception as e:
                if DEBUG:
                    printDM(f"[nodus-meta-patch] system settings apply failed: {e}", location=MODULE)
        if sensor_patch_changed:
            try:
                from collections import OrderedDict
                from .saiSensorSettingsManager import SensorSettingsManager

                def _ensure_block(parent: dict, name: str) -> dict:
                    block = parent.get(name)
                    if not isinstance(block, dict):
                        block = OrderedDict()
                        parent[name] = block
                    return block

                sensor_mgr = SensorSettingsManager()
                for target_sensor_id, target_blocks in sensor_patch_info.items():
                    try:
                        sensor_doc = sensor_mgr.load(target_sensor_id) or OrderedDict()
                    except Exception:
                        sensor_doc = OrderedDict()

                    changed = False
                    for block_name, items in target_blocks.items():
                        current = sensor_doc
                        for segment in [
                            seg for seg in str(block_name or "").split(".") if seg
                        ]:
                            current = _ensure_block(current, segment)
                        for key, value in (items or {}).items():
                            if current.get(key) != value:
                                current[key] = value
                                changed = True
                    if changed:
                        sensor_mgr.save(target_sensor_id, sensor_doc)
            except Exception as e:
                if DEBUG:
                    printDM(f"[nodus-meta-patch] sensor settings apply failed: {e}", location=MODULE)
        if live_switch_state_updates:
            try:
                switch_meta = patched_meta.get("switch") if isinstance(patched_meta, dict) else {}
                switch_id = str((switch_meta or {}).get("device_id") or "").strip()
                channels = (switch_meta or {}).get("channels") if isinstance(switch_meta, dict) else []
                if switch_id and isinstance(channels, list):
                    for item in live_switch_state_updates:
                        try:
                            channel_index = int(item.get("channel_index") or 0)
                        except Exception:
                            channel_index = 0
                        if channel_index <= 0 or channel_index > len(channels):
                            continue
                        channel_meta = channels[channel_index - 1]
                        if not isinstance(channel_meta, dict):
                            continue
                        channel_id = str(channel_meta.get("channel_id") or "").strip()
                        label = str(channel_meta.get("label") or channel_id).strip() or channel_id
                        if not channel_id:
                            continue
                        raw_state = item.get("state")
                        if isinstance(raw_state, bool):
                            is_on = raw_state
                        else:
                            state_text = str(raw_state or "").strip().lower()
                            if state_text not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                                continue
                            is_on = state_text in {"1", "true", "yes", "on"}
                        source = patch_source or "mqtt-meta-patch"
                        self._maybe_persist_switch_event(
                            switch_id=switch_id,
                            channel_id=channel_id,
                            is_on=is_on,
                            ts_iso=None,
                            source=source,
                            sensor_lineage=f"Switch_{switch_id}",
                        )
                        cache = self._switch_state_cache.setdefault(switch_id, {})
                        state_txt = "on" if is_on else "off"
                        cache[channel_id] = state_txt
                        if label:
                            cache[label] = state_txt
                        self.clear_pending_switch_set(switch_id, channel_id=channel_id, label=label)
                        self._known_switch_ids.add(switch_id)
                        self._broadcast_switch_event(
                            switch_id=switch_id,
                            channel_id=channel_id,
                            label=label,
                            is_on=bool(is_on),
                            source=source,
                        )
            except Exception as e:
                if DEBUG:
                    printDM(f"[nodus-meta-patch] live switch update failed: {e}", location=MODULE)
        return ok_meta, subscribed

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
        previous = self.device_status.get(base) or self.device_status.get(f"{base}.local")
        self.device_status[base] = s
        self.device_status[f"{base}.local"] = s
        if previous not in (None, s) and s == SENSOR_EVENT_STATE_OFFLINE:
            try:
                writer = getattr(self.data_logger, "log_sensor_event", None)
                if callable(writer):
                    writer(
                        base,
                        SENSOR_EVENT_TYPE_LIVENESS,
                        state=SENSOR_EVENT_STATE_OFFLINE,
                        source="mqtt_liveness",
                    )
            except Exception as exc:
                if DEBUG:
                    printDM(f"[liveness] failed to record offline event for {base}: {exc}", location=MODULE)
        if previous != s:
            self._notify_liveness_status_change(base, s)

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
          - retained availability alone does not auto-enroll a host
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
            if DEBUG:
                printDM(f"[retained] skip auto-enroll for {base} (availability-only hint)", location=MODULE)
        elif DEBUG:
            printDM(f"[retained] skip auto-enroll for {base} ({src})", location=MODULE)

    async def _validate_retained_availability_and_add(self, base: str) -> None:
        """
        Retained availability alone is not enough to verify a host via HTTP.
        Treat it as a lightweight MQTT discovery hint and enroll without
        contacting the device over the network.
        """
        try:
            if not base or base in (self.mqtt_clients or set()):
                return
            self._maybe_add_mqtt_client(base)
            if DEBUG:
                printDM(f"[retained] availability promoted via MQTT hint: {base}", location=MODULE)
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
        ack_topics: dict | None = None,
        result_topics: dict | None = None,
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
            elif kind == "ack":
                self.nodus_switch_ack_topics[(switch_id, ch_id)] = topic
            elif kind == "result":
                self.nodus_switch_result_topics[(switch_id, ch_id)] = topic

            if kind in ("state", "event", "availability", "ack", "result"):
                if topic not in self.registered_topics and not self._has_covering_subscription(topic):
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
        for _m in (ack_topics or {}).values():
            _register("ack", str(_m))
        for _m in (result_topics or {}).values():
            _register("result", str(_m))

        return any_new

    def _subscribe_nodus_switch_meta_topic(self, topic: str) -> bool:
        topic = str(topic or "").strip()
        if not topic:
            return False
        if not (
            topic.startswith("nodus/")
            or (self.base_topic and topic.startswith(f"{self.base_topic}/nodus/"))
        ):
            return False
        if topic in self.registered_topics or self._has_covering_subscription(topic):
            return False
        self.registered_topics.add(topic)
        try:
            self.client.subscribe(topic)
            if DEBUG:
                printDM(f"[nodus-meta] subscribed to split switch metadata: {topic}", location=MODULE)
            return True
        except Exception as exc:
            if DEBUG:
                printDM(f"[nodus-meta] switch metadata subscribe failed for {topic}: {exc}", location=MODULE)
            return False

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

        # Wi-Fi credentials are operational inputs, not discovery metadata.
        # Keep received values out of the long-lived discovery cache and
        # Nodus shadow settings even when older firmware includes it in meta.
        meta = copy.deepcopy(meta)
        network_for_redaction = meta.get("network")
        if isinstance(network_for_redaction, dict):
            network_for_redaction.pop("ssid", None)
            network_for_redaction.pop("SSID", None)
            network_for_redaction.pop("password", None)
            network_for_redaction.pop("PASSWORD", None)

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

        def _canonical_location(value: str | None) -> str:
            loc = str(value or "").strip()
            return loc if loc and not _is_unknown_loc(loc) else "Unknown"

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
        firmware_version = str(meta.get("version") or "").strip()
        board_type = _extract_nodus_board_type(meta)

        primary_sensor_blob = dict(meta.get("sensor")) if isinstance(meta.get("sensor"), dict) else {}
        if not primary_sensor_blob:
            top_sensor_id = str(meta.get("sensor_id") or meta.get("SENSOR_ID") or "").strip()
            top_data_topic = str(meta.get("data_topic") or meta.get("mqtt_sensor_topic") or "").strip()
            top_has_sensor = bool(
                top_sensor_id
                or top_data_topic
                or meta.get("display_metrics")
                or meta.get("metrics")
            )
            if top_has_sensor:
                primary_sensor_blob = {
                    "sensor_id": top_sensor_id or device_id,
                    "device": meta.get("device") or meta.get("DEVICE") or meta.get("SENSOR_DEVICE") or "",
                    "type": meta.get("type") or meta.get("TYPE") or "nodus",
                    "location": meta.get("location") or meta.get("LOCATION") or "",
                    "serial": meta.get("serial") or meta.get("SERIAL_NUM") or "",
                    "data_topic": top_data_topic,
                    "availability_topic": meta.get("availability_topic") or meta.get("mqtt_availability_topic") or "",
                    "event_topic": meta.get("event_topic") or meta.get("mqtt_event_topic") or "",
                    "hardware": (
                        meta.get("hardware")
                        or meta.get("HARDWARE")
                        or meta.get("sensor_hardware")
                        or meta.get("SENSOR_HARDWARE")
                        or ""
                    ),
                    "display_metrics": meta.get("display_metrics") or meta.get("metrics") or meta.get("Display") or [],
                    "display_styles": meta.get("display_styles") or meta.get("styles") or [],
                }
        sensor_blobs: list[dict] = []
        raw_sensors = meta.get("sensors")
        if isinstance(raw_sensors, list):
            for item in raw_sensors:
                if isinstance(item, dict):
                    sensor_blobs.append(dict(item))
        if not sensor_blobs and primary_sensor_blob:
            sensor_blobs.append(primary_sensor_blob)

        switch_blob = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
        network_meta = meta.get("network") if isinstance(meta.get("network"), dict) else {}
        location_group = meta.get("location_group") if isinstance(meta.get("location_group"), dict) else {}

        switch_id = str(
            switch_blob.get("switch_device_id")
            or switch_blob.get("device_id")
            or ""
        ).strip()
        group_location = str(location_group.get("location") or "").strip()
        primary_sensor_location = str(
            primary_sensor_blob.get("location")
            or primary_sensor_blob.get("LOCATION")
            or ""
        ).strip()
        switch_location = str(switch_blob.get("location") or "").strip()
        resolved_location = _pick_location(
            group_location,
            switch_location,
            primary_sensor_location,
        )

        if not retain:
            self._maybe_add_mqtt_client(base)

        self._record_host_seen(base, ts=now_t, retain=retain, report=False)

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

        if firmware_version:
            self.nodus_firmware_versions[base] = firmware_version
            self.nodus_firmware_versions[f"{base}.local"] = firmware_version
            self.nodus_firmware_versions[device_id] = firmware_version
        self._record_nodus_board_type(board_type, base, f"{base}.local", device_id)

        # Sensor metadata. `meta.sensors` is authoritative for multi-sensor
        # devices; `meta.sensor` remains the single/primary compatibility view.
        sensor_ids_for_host: list[str] = []
        seen_sensor_ids: set[str] = set()
        for sensor_blob in sensor_blobs:
            sensor_id = str(
                sensor_blob.get("sensor_id")
                or sensor_blob.get("SENSOR_ID")
                or ""
            ).strip()
            if not sensor_id or sensor_id.lower() in seen_sensor_ids:
                continue
            seen_sensor_ids.add(sensor_id.lower())
            sensor_ids_for_host.append(sensor_id)
            touched = True
            if sensor_id not in peer_ids_for_host:
                peer_ids_for_host.append(sensor_id)
            if firmware_version:
                self.nodus_firmware_versions[sensor_id] = firmware_version
            self._record_nodus_board_type(board_type, sensor_id)
            sensor_hardware = (
                _extract_nodus_sensor_hardware({"sensor": sensor_blob})
                or _extract_nodus_sensor_hardware(meta)
            )
            self._record_nodus_sensor_hardware(sensor_hardware, base, f"{base}.local", device_id, sensor_id)
            self.device_type[sensor_id] = "nodus"
            self.nodus_sensor_hosts[sensor_id] = base
            config_file = str(
                sensor_blob.get("config_file")
                or sensor_blob.get("name")
                or ""
            ).strip()
            if config_file:
                self.nodus_sensor_config_files[sensor_id] = config_file
            self._record_mqtt_seen(sensor_id, ts=now_t, retain=retain, report=False)
            sensor_device = self._infer_sensor_device_name(
                sensor_blob.get("device")
                or sensor_blob.get("DEVICE")
                or sensor_blob.get("SENSOR_DEVICE"),
                sensor_id,
            )
            sensor_serial = self._extract_sensor_serial(sensor_blob, meta)
            display_metrics = self._normalize_display_metrics(
                sensor_blob.get("display_metrics")
                or sensor_blob.get("metrics")
                or sensor_blob.get("Display")
            )
            display_styles = self._normalize_display_styles(
                sensor_blob.get("display_styles") or sensor_blob.get("styles")
            )
            if display_metrics:
                self.expected_gauge_map[sensor_id] = display_metrics

            register_sensor = getattr(self.data_logger, "register_sensor", None)
            if callable(register_sensor):
                try:
                    register_sensor(sensor_id)
                except Exception:
                    pass

            data_topic = _meta_topic(sensor_blob.get("data_topic") or sensor_blob.get("mqtt_sensor_topic"))
            avail_topic = _meta_topic(sensor_blob.get("availability_topic") or sensor_blob.get("mqtt_availability_topic"))
            event_topic = _meta_topic(sensor_blob.get("event_topic") or sensor_blob.get("mqtt_event_topic"))
            sensor_location = str(
                sensor_blob.get("location")
                or sensor_blob.get("LOCATION")
                or ""
            ).strip()
            sensor_loc = _pick_location(sensor_location, resolved_location)

            if data_topic:
                self.nodus_sensor_topics[sensor_id] = data_topic

            for t in (data_topic, avail_topic, event_topic):
                if not t:
                    continue
                self.topic_dev_id_map[t] = sensor_id
                self.device_location[t] = sensor_loc
                if t not in self.registered_topics and not self._has_covering_subscription(t):
                    self.registered_topics.add(t)
                    self.client.subscribe(t)
                    subscribed = True

            discovered_sensors.append({
                "sensor_id": sensor_id,
                "device_type": "nodus",
                "device": sensor_device,
                "sensor_type": str(sensor_blob.get("type") or sensor_blob.get("TYPE") or "nodus").strip(),
                "location": sensor_loc,
                "serial": sensor_serial,
                "mcu": board_type,
                "hardware": sensor_hardware,
                "physical_device_id": base,
                "config_file": config_file,
                "display_metrics": display_metrics,
                "display_styles": display_styles,
            })
        if sensor_ids_for_host:
            self.nodus_host_sensors[base] = list(sensor_ids_for_host)

        # switch metadata
        channels = switch_blob.get("channels")
        if switch_id and isinstance(channels, list):
            touched = True
            if switch_id not in peer_ids_for_host:
                peer_ids_for_host.append(switch_id)
            if firmware_version:
                self.nodus_firmware_versions[switch_id] = firmware_version
            self._record_nodus_board_type(board_type, switch_id)

            switch_loc = _pick_location(switch_location, resolved_location)
            self.device_type[switch_id] = "nodus"
            self._known_switch_ids.add(switch_id)
            self._record_mqtt_seen(switch_id, ts=now_t, retain=retain, report=False)

            event_topics: dict[str, str] = {}
            state_topics: dict[str, str] = {}
            command_topics: dict[str, str] = {}
            availability_topics: dict[str, str] = {}
            ack_topics: dict[str, str] = {}
            result_topics: dict[str, str] = {}
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
                ack_t = _meta_topic(row.get("ack_topic"))
                result_t = _meta_topic(row.get("result_topic"))
                av_t = _meta_topic(row.get("availability_topic"))

                channels_with_ids.append((label, channel_id))
                label_by_channel[channel_id] = label
                self.nodus_label_to_channel[(switch_id, _norm_label(label))] = channel_id
                self._record_mqtt_seen(channel_id, ts=now_t, retain=retain, report=False)

                if ev_t:
                    event_topics[str(idx)] = ev_t
                if st_t:
                    state_topics[str(idx)] = st_t
                if set_t:
                    command_topics[str(idx)] = set_t
                if ack_t:
                    ack_topics[str(idx)] = ack_t
                if result_t:
                    result_topics[str(idx)] = result_t
                if av_t:
                    availability_topics[str(idx)] = av_t

                switch_payload[f"SWITCH_{idx}_LABEL"] = label
                switch_payload[f"SWITCH_{idx}_CHANNEL_ID"] = channel_id
                enable_pin = str(row.get("enable_pin") or channel_id).strip()
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
                        switch_key=build_switch_key(switch_id, channel_id),
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
                    "mcu": board_type,
                })
                new_subs = self._register_nodus_switch_topics(
                    switch_id,
                    switch_loc,
                    event_topics=event_topics,
                    state_topics=state_topics,
                    command_topics=command_topics,
                    availability_topics=availability_topics,
                    ack_topics=ack_topics,
                    result_topics=result_topics,
                    label_by_channel_id=label_by_channel,
                )
                subscribed = subscribed or new_subs
        elif switch_id:
            touched = True
            if switch_id not in peer_ids_for_host:
                peer_ids_for_host.append(switch_id)
            if firmware_version:
                self.nodus_firmware_versions[switch_id] = firmware_version
            self._record_nodus_board_type(board_type, switch_id)
            self.device_type[switch_id] = "nodus"
            self._known_switch_ids.add(switch_id)
            self._record_mqtt_seen(switch_id, ts=now_t, retain=retain, report=False)
            try:
                channel_count = int(switch_blob.get("channel_count") or 0)
            except Exception:
                channel_count = 0
            split_topic = _meta_topic(switch_blob.get("meta_topic"))
            if not split_topic and channel_count > 0:
                split_topic = f"nodus/{device_id}/meta/switch"
            if split_topic:
                subscribed = self._subscribe_nodus_switch_meta_topic(split_topic) or subscribed

        if not touched:
            return False, False

        peers = self.host_to_peer_ids.setdefault(base, [])
        for pid in peer_ids_for_host:
            if pid and pid not in peers:
                peers.append(pid)

        runtime_ipv4 = _extract_runtime_ipv4addr(network_meta) or _extract_runtime_ipv4addr(meta)
        if runtime_ipv4:
            for raw_key in (base, device_id, *sensor_ids_for_host, switch_id):
                key = str(raw_key or "").strip()
                if not key:
                    continue
                self._host_ipv4addr[key] = runtime_ipv4
                normalized_key = self._normalize_host_key(key)
                if normalized_key:
                    self._host_ipv4addr[normalized_key] = runtime_ipv4
                    self._host_ipv4addr[f"{normalized_key}.local"] = runtime_ipv4

        if not retain:
            derived = self.get_nodus_liveness(base, now_ts=now_t).get("state", "unknown")
            if derived == "unknown":
                derived = "degraded"
            self._mark_host_status(base, derived)

        try:
            self.discovery_cache[base] = meta
        except Exception:
            pass

        try:
            if discovered_sensors or discovered_switches:
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

        if discovered_sensors or discovered_switches:
            try:
                for sensor in discovered_sensors:
                    sensor_id_text = str((sensor or {}).get("sensor_id") or "").strip()
                    if sensor_id_text:
                        self._dashboard_inventory_notified_sensors.add(sensor_id_text.lower())
                        self._broadcast_dashboard_inventory_changed(host=base, sensor_id=sensor_id_text)
                for switch in discovered_switches:
                    switch_id_text = str((switch or {}).get("switch_id") or "").strip()
                    self._broadcast_dashboard_inventory_changed(host=base, switch_id=switch_id_text)
            except Exception:
                pass

        return True, subscribed

    def _parse_and_subscribe_from_nodus_switch_meta(
        self,
        payload: dict,
        *,
        topic_device_id: str | None = None,
        retain: bool = False,
    ) -> tuple[bool, bool]:
        """
        Parse retained nodus/<device_id>/meta/switch payloads and merge them
        with the latest compact nodus/<device_id>/meta snapshot.
        """
        if not isinstance(payload, dict):
            return False, False

        schema = str(payload.get("schema") or "").strip().lower()
        if schema and schema != "nodus-meta-switch/v1":
            return False, False

        device_id = str(payload.get("device_id") or topic_device_id or "").strip()
        if not device_id:
            return False, False

        channels = payload.get("channels")
        if not isinstance(channels, list):
            return False, False

        base = self._normalize_host_key(device_id) or device_id
        compact = copy.deepcopy(self.discovery_cache.get(base) or {})
        if not isinstance(compact, dict):
            compact = {}

        compact["schema"] = "nodus-meta/v1"
        compact["device_id"] = str(compact.get("device_id") or device_id).strip() or device_id
        compact.setdefault("location_group", {})
        if not isinstance(compact.get("location_group"), dict):
            compact["location_group"] = {}

        old_switch = compact.get("switch") if isinstance(compact.get("switch"), dict) else {}
        switch_id = str(
            payload.get("switch_device_id")
            or old_switch.get("switch_device_id")
            or old_switch.get("device_id")
            or ""
        ).strip()
        location = str(payload.get("location") or old_switch.get("location") or "").strip()

        switch_blob = dict(old_switch)
        if switch_id:
            switch_blob["switch_device_id"] = switch_id
            switch_blob.setdefault("device_id", switch_id)
        if location:
            switch_blob["location"] = location
        switch_blob["channel_count"] = payload.get("channel_count", len(channels))
        switch_blob["channels"] = channels
        compact["switch"] = switch_blob

        members = compact["location_group"].get("members")
        if not isinstance(members, list):
            members = []
        for member in (device_id, switch_id):
            if member and member not in members:
                members.append(member)
        compact["location_group"]["members"] = members
        if location and not str(compact["location_group"].get("location") or "").strip():
            compact["location_group"]["location"] = location

        return self._parse_and_subscribe_from_nodus_meta(
            compact,
            topic_device_id=device_id,
            retain=retain,
        )

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
                    "set_topic": f"nodus/{channel_id}/config/set",
                    "availability_topic": f"nodus/{channel_id}/availability",
                })

        return {
            "schema": "nodus-meta/v1",
            "device_id": device_id,
            "mcu": _extract_nodus_board_type(payload),
            "sensor": {
                "sensor_id": sensor_id,
                "device": self._infer_sensor_device_name(sensor_blob.get("device"), sensor_id),
                "serial": self._extract_sensor_serial(sensor_blob, payload),
                "hardware": _extract_nodus_sensor_hardware(payload),
                "location": str(sensor_blob.get("location") or location).strip(),
                "data_topic": f"nodus/{sensor_id}/data" if sensor_id else "",
                "event_topic": f"nodus/{sensor_id}/event" if sensor_id else "",
                "availability_topic": f"nodus/{sensor_id}/availability" if sensor_id else "",
                "display_metrics": self._normalize_display_metrics(
                    sensor_blob.get("display_metrics") or sensor_blob.get("metrics")
                ),
                "display_styles": self._normalize_display_styles(
                    sensor_blob.get("display_styles") or sensor_blob.get("styles")
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

    def _resolve_switch_channel_index(self, switch_id: str, channel_id: str, channel_label: str | None = None) -> int | None:
        switch_id_text = str(switch_id or "").strip()
        channel_id_text = str(channel_id or "").strip()
        if not channel_id_text:
            return None
        if switch_id_text:
            try:
                from .saiSwitchSettingsManager import SwitchSettingsManager

                switch_mgr = SwitchSettingsManager("switch_settings")
                doc = switch_mgr.load(switch_id_text) or {}
                switch_block = doc.get("Switch") if isinstance(doc, dict) else {}
                if isinstance(switch_block, dict):
                    for idx in range(1, 33):
                        candidate = str(switch_block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                        if candidate and candidate == channel_id_text:
                            return idx
            except Exception:
                pass

        try:
            match = re.fullmatch(r"S(\d+)-[A-Za-z0-9._-]+", channel_id_text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        except Exception:
            pass

        try:
            label_text = str(channel_label or "").strip().lower()
            for row in (self.data_logger.get_switch_identities() or []):
                rsid = str(row.get("switch_id", "") or "").strip().lower()
                rch = str(row.get("channel_id", "") or "").strip()
                if rsid != switch_id_text.lower() or rch != channel_id_text:
                    continue
                if label_text:
                    rlab = str(row.get("label", "") or "").strip().lower()
                    if rlab != label_text:
                        continue
                switch_key = str(row.get("switch_key", "") or "").strip()
                if not switch_key or "::" not in switch_key:
                    continue
                break
        except Exception:
            pass
        return None

    def _command_liveness_snapshot_for_switch(self, switch_id: str, channel_id: str | None = None) -> dict:
        target = str(switch_id or "").strip()
        if not target:
            target = str(channel_id or "").strip()
        switch_snapshot = self.get_nodus_liveness(target, device_type="switch")
        if channel_id:
            try:
                channel_snapshot = self.get_nodus_liveness(str(channel_id or "").strip(), device_type="switch")
                channel_state = self._normalize_liveness_state(channel_snapshot.get("state"))
                if channel_state == "offline":
                    return channel_snapshot
                switch_state = self._normalize_liveness_state(switch_snapshot.get("state"))
                if switch_state != "online" and channel_state == "online":
                    return channel_snapshot
            except Exception:
                pass
        return switch_snapshot

    def _switch_command_allowed(self, switch_id: str, channel_id: str | None = None) -> bool:
        snapshot = self._command_liveness_snapshot_for_switch(switch_id, channel_id)
        state = self._normalize_liveness_state(snapshot.get("state"))
        if state == "online":
            return True
        if DEBUG:
            printDM(
                f"[switch-command] blocked {switch_id or channel_id}: liveness={state} reason={snapshot.get('reason')}",
                location=MODULE,
            )
        return False

    def _mqtt_client_ready_for_live_command(self) -> bool:
        try:
            client = getattr(self, "client", None)
            if client is None:
                return False
            is_connected = getattr(client, "is_connected", None)
            if callable(is_connected):
                return bool(is_connected())
            return True
        except Exception:
            return False

    def _switch_config_command_key(self, switch_id: str, channel_id: str) -> str:
        ch = str(channel_id or "").strip().lower()
        if ch:
            return ch
        sid = str(switch_id or "").strip().lower()
        return f"switch:{sid}" if sid else ""

    def _prune_switch_config_commands_locked(self, now_ts: float | None = None) -> None:
        now_v = time.time() if now_ts is None else float(now_ts)
        stale_keys: list[str] = []
        for key, meta in list(self._switch_config_command_inflight.items()):
            try:
                expires_at = float((meta or {}).get("expires_at") or 0.0)
            except Exception:
                expires_at = 0.0
            if expires_at <= 0.0:
                try:
                    expires_at = float((meta or {}).get("started_at") or 0.0) + NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S
                except Exception:
                    expires_at = now_v - 1.0
            if expires_at <= now_v:
                stale_keys.append(key)
        for key in stale_keys:
            meta = self._switch_config_command_inflight.pop(key, None) or {}
            message_id = str(meta.get("message_id") or "").strip() if isinstance(meta, dict) else ""
            if message_id:
                self._switch_config_command_by_message.pop(message_id, None)

    def _begin_switch_config_command(self, switch_id: str, channel_id: str, desired_state: bool) -> str:
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return "blocked"
        now_v = time.time()
        with self._switch_config_command_lock:
            self._prune_switch_config_commands_locked(now_v)
            existing = self._switch_config_command_inflight.get(key)
            if isinstance(existing, dict):
                if existing.get("failed"):
                    if DEBUG:
                        printDM(
                            f"[switch-command] retry cooling down channel_id={channel_id}",
                            location=MODULE,
                        )
                    return "blocked"
                if bool(existing.get("state")) == bool(desired_state):
                    if DEBUG:
                        printDM(
                            f"[switch-command] coalesced duplicate channel_id={channel_id} state={bool(desired_state)} message_id={existing.get('message_id')}",
                            location=MODULE,
                        )
                    return "coalesced"
                if DEBUG:
                    printDM(
                        f"[switch-command] blocked conflicting command channel_id={channel_id} existing_state={bool(existing.get('state'))} new_state={bool(desired_state)} message_id={existing.get('message_id')}",
                        location=MODULE,
                    )
                return "blocked"
            self._switch_config_command_inflight[key] = {
                "switch_id": str(switch_id or "").strip(),
                "channel_id": str(channel_id or "").strip(),
                "state": bool(desired_state),
                "started_at": now_v,
                "expires_at": now_v + NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S,
                "message_id": "",
                "topic": "",
                "failed": False,
            }
            return "send"

    def _mark_switch_config_command_published(self, switch_id: str, channel_id: str, message_id: str, topic: str) -> None:
        key = self._switch_config_command_key(switch_id, channel_id)
        mid = str(message_id or "").strip()
        if not key:
            return
        with self._switch_config_command_lock:
            meta = self._switch_config_command_inflight.get(key)
            if not isinstance(meta, dict):
                return
            old_mid = str(meta.get("message_id") or "").strip()
            if old_mid:
                self._switch_config_command_by_message.pop(old_mid, None)
            meta["message_id"] = mid
            meta["topic"] = str(topic or "").strip()
            meta["failed"] = False
            if mid:
                self._switch_config_command_by_message[mid] = key

    def _mark_switch_config_command_failed(self, switch_id: str, channel_id: str) -> None:
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return
        now_v = time.time()
        with self._switch_config_command_lock:
            meta = self._switch_config_command_inflight.get(key)
            if not isinstance(meta, dict):
                return
            mid = str(meta.get("message_id") or "").strip()
            if mid:
                self._switch_config_command_by_message.pop(mid, None)
            meta["message_id"] = ""
            meta["failed"] = True
            meta["expires_at"] = now_v + NODUS_SWITCH_COMMAND_FAILED_COOLDOWN_S

    def _clear_switch_config_command(self, switch_id: str, channel_id: str) -> None:
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return
        with self._switch_config_command_lock:
            meta = self._switch_config_command_inflight.pop(key, None) or {}
            mid = str(meta.get("message_id") or "").strip() if isinstance(meta, dict) else ""
            if mid:
                self._switch_config_command_by_message.pop(mid, None)

    def _clear_switch_config_command_by_message(self, message_id: str) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        with self._switch_config_command_lock:
            key = self._switch_config_command_by_message.pop(mid, None)
            if key:
                self._switch_config_command_inflight.pop(key, None)

    def set_switch_by_channel_id(self, switch_id: str, channel_id: str, new_state: bool, qos: int = 0, retain: bool = False) -> bool:
        """
        Publish remote switch changes via Nodus config/set by updating
        Switch.SWITCH_n_LAST_STATE in switch.toml.
        """
        try:
            switch_id_text = str(switch_id or "").strip()
            channel_id_text = str(channel_id or "").strip()
            if not channel_id_text:
                return False
            if not self._switch_command_allowed(switch_id_text, channel_id_text):
                return False
            if not self._mqtt_client_ready_for_live_command():
                if DEBUG:
                    printDM(
                        f"[set_switch_by_channel_id] MQTT client not connected; refusing live command switch_id={switch_id_text} channel_id={channel_id_text}",
                        location=MODULE,
                    )
                return False

            channel_index = self._resolve_switch_channel_index(switch_id_text, channel_id_text)
            if channel_index:
                payload = {
                    "updates": [
                        {
                            "section": "Switch",
                            "key": f"SWITCH_{channel_index}_LAST_STATE",
                            "value": bool(new_state),
                            "name": "switch.toml",
                        }
                    ]
                }
                advertised_topic = (
                    self.nodus_switch_command_topics.get((switch_id_text, channel_id_text))
                    or self.nodus_channel_command_topics.get(channel_id_text)
                )
                command_topic = ""
                if advertised_topic and str(advertised_topic).strip().endswith("/config/set"):
                    command_topic = str(advertised_topic).strip()
                elif channel_id_text:
                    command_topic = f"nodus/{channel_id_text}/config/set"
                elif switch_id_text:
                    command_topic = f"nodus/{switch_id_text}/config/set"

                if command_topic:
                    begin_result = self._begin_switch_config_command(switch_id_text, channel_id_text, bool(new_state))
                    if begin_result == "coalesced":
                        return True
                    if begin_result != "send":
                        return False

                    local_epoch = _local_epoch_seconds(self.settings)
                    message_id = f"cfg-{local_epoch}-{uuid.uuid4().hex[:8]}"
                    envelope = {
                        "message_id": message_id,
                        "payload": payload,
                        "restart": False,
                    }
                    ok = bool(self.publish_json(
                        command_topic,
                        envelope,
                        qos=max(int(qos or 0), 1),
                        retain=False,
                        use_ha_client=False,
                    ))
                    if DEBUG:
                        printDM(
                            f"[set_switch_by_channel_id] config/set topic={command_topic} switch_id={switch_id_text} channel_id={channel_id_text} channel_index={channel_index} message_id={message_id} ok={ok}",
                            location=MODULE,
                        )
                    if ok:
                        self._mark_switch_config_command_published(
                            switch_id_text,
                            channel_id_text,
                            message_id,
                            command_topic,
                        )
                        with self._config_lock:
                            self.config_message_device[message_id] = channel_id_text
                        return True
                    self._mark_switch_config_command_failed(switch_id_text, channel_id_text)

            if DEBUG:
                printDM(
                    f"[set_switch_by_channel_id] config/set unresolved for switch_id={switch_id_text} channel_id={channel_id_text} channel_index={channel_index!r}",
                    location=MODULE,
                )
            return False
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
          - heartbeat freshness is canonical
          - live data/state/event reports are supplemental
          - availability=offline is authoritative
          - availability=online is only a weak hint
        """
        base = self._normalize_host_key(name) or (name or "").strip()
        if not base:
            return "unknown"

        now_ts = time.time()

        try:
            weewx_id = self._normalize_host_key(getattr(self, "weewx_sensor_id", WEEWX_DEFAULT_SENSOR_ID)) or WEEWX_DEFAULT_SENSOR_ID
            if base == weewx_id:
                last_seen = float(self.last_mqtt_seen.get(weewx_id, 0.0) or 0.0)
                if last_seen <= 0.0:
                    return "unknown"
                update_period = max(
                    15.0,
                    float(getattr(self, "weewx_update_period_sec", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC),
                )
                return "online" if (now_ts - last_seen) <= (update_period * 3.0) else "offline"
        except Exception:
            pass

        try:
            snapshot = self.get_nodus_liveness(base, now_ts=now_ts)
            status = self._normalize_liveness_state(snapshot.get("state"))
            if status in {"online", "degraded", "offline", "migration_required"}:
                return status
        except Exception:
            pass

        s = (self.device_status.get(base)
             or self.device_status.get(f"{base}.local")
             or "unknown")
        return self._normalize_liveness_state(s)

    def _record_nodus_board_type(self, board_type: str | None, *device_ids: str | None) -> None:
        board = str(board_type or "").strip()
        if not board:
            return
        for raw in device_ids:
            key = str(raw or "").strip()
            if not key:
                continue
            self.nodus_board_types[key] = board
            normalized = self._normalize_host_key(key)
            if normalized:
                self.nodus_board_types[normalized] = board
                self.nodus_board_types[f"{normalized}.local"] = board

    def _record_nodus_sensor_hardware(self, hardware: str | None, *device_ids: str | None) -> None:
        sensor_hardware = str(hardware or "").strip()
        if not sensor_hardware:
            return
        for raw in device_ids:
            key = str(raw or "").strip()
            if not key:
                continue
            self.nodus_sensor_hardware[key] = sensor_hardware
            normalized = self._normalize_host_key(key)
            if normalized:
                self.nodus_sensor_hardware[normalized] = sensor_hardware
                self.nodus_sensor_hardware[f"{normalized}.local"] = sensor_hardware

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

            mapped_sensor_host = str(
                self.nodus_sensor_hosts.get(dev)
                or self.nodus_sensor_hosts.get(self._normalize_host_key(dev) or "")
                or ""
            ).strip()
            if mapped_sensor_host:
                return self._normalize_host_key(mapped_sensor_host)

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
                    matches = [
                        str(cand)
                        for cand in (self.mqtt_clients or [])
                        if str(cand).endswith(f"-{serial}") and not _looks_like_channel_id(str(cand))
                    ]
                    for cand in matches:
                        if not cand.startswith("switch-"):
                            return cand
                    if matches:
                        return matches[0]
                except Exception:
                    pass
                return None  # do NOT invent "switch-<serial>.local" here—switch host is the sensor's host

            # 3) Sensor fallback: if caller passed a sensor id, use it as the host
            if dev.endswith(".local"):
                dev = dev[:-6]  # strip ".local"
            return dev or None
        except Exception:
            return None

    def resolve_nodus_sensor_target(self, sensor_id: str) -> dict:
        """Return physical host and remote config filename for a child sensor."""
        sid = str(sensor_id or "").strip()
        if not sid:
            return {"sensor_id": "", "device_id": "", "config_file": ""}
        device_id = str(
            self.nodus_sensor_hosts.get(sid)
            or self.resolve_nodus_hostname(sid, device_type="sensor")
            or ""
        ).strip()
        config_file = str(self.nodus_sensor_config_files.get(sid) or "").strip()
        return {
            "sensor_id": sid,
            "device_id": self._normalize_host_key(device_id) or device_id,
            "config_file": config_file,
        }

    def get_nodus_firmware_version(self, device_id: str | None, device_type: str | None = None) -> str:
        """
        Resolve a firmware version captured from nodus meta for a sensor, switch, or host id.
        """
        dev = str(device_id or "").strip()
        if not dev:
            return ""

        candidates: list[str] = []

        def _add_candidate(value: str | None) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            options = [raw]
            if raw.endswith(".local"):
                options.append(raw[:-6])
            for item in options:
                key = str(item or "").strip()
                if key and key not in candidates:
                    candidates.append(key)

        _add_candidate(dev)
        _add_candidate(self.resolve_nodus_hostname(dev, device_type=device_type))

        for host, peers in (self.host_to_peer_ids or {}).items():
            try:
                if dev in (peers or []):
                    _add_candidate(host)
                    for peer in (peers or []):
                        _add_candidate(peer)
            except Exception:
                continue

        if (device_type or "").lower() == "switch" or dev.startswith("switch-"):
            serial = dev.rsplit("-", 1)[-1] if "-" in dev else dev
            suffix = f"-{serial}"
            for key in (self.nodus_firmware_versions or {}).keys():
                text = str(key or "").strip()
                if not text or text == dev or text.startswith("switch-"):
                    continue
                if text.endswith(suffix):
                    _add_candidate(text)

        for key in candidates:
            version = str((self.nodus_firmware_versions or {}).get(key) or "").strip()
            if version:
                return version
        return ""

    def get_nodus_board_type(self, device_id: str | None, device_type: str | None = None) -> str:
        """
        Resolve a Nodus MCU/board target captured from hello/meta for a sensor, switch, or host id.
        """
        dev = str(device_id or "").strip()
        if not dev:
            return ""

        candidates: list[str] = []

        def _add_candidate(value: str | None) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            options = [raw]
            if raw.endswith(".local"):
                options.append(raw[:-6])
            for item in options:
                key = str(item or "").strip()
                if key and key not in candidates:
                    candidates.append(key)

        _add_candidate(dev)
        _add_candidate(self.resolve_nodus_hostname(dev, device_type=device_type))

        for host, peers in (self.host_to_peer_ids or {}).items():
            try:
                if dev in (peers or []):
                    _add_candidate(host)
                    for peer in (peers or []):
                        _add_candidate(peer)
            except Exception:
                continue

        if (device_type or "").lower() == "switch" or dev.startswith("switch-"):
            serial = dev.rsplit("-", 1)[-1] if "-" in dev else dev
            suffix = f"-{serial}"
            for key in (self.nodus_board_types or {}).keys():
                text = str(key or "").strip()
                if not text or text == dev or text.startswith("switch-"):
                    continue
                if text.endswith(suffix):
                    _add_candidate(text)

        for key in candidates:
            board = str((self.nodus_board_types or {}).get(key) or "").strip()
            if board:
                return board
        return ""

    def get_nodus_sensor_hardware(self, device_id: str | None, device_type: str | None = None) -> str:
        """
        Resolve concrete Nodus sensor hardware captured from hello/meta.
        """
        dev = str(device_id or "").strip()
        if not dev:
            return ""

        candidates: list[str] = []

        def _add_candidate(value: str | None) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            options = [raw]
            if raw.endswith(".local"):
                options.append(raw[:-6])
            for item in options:
                key = str(item or "").strip()
                if key and key not in candidates:
                    candidates.append(key)

        _add_candidate(dev)
        _add_candidate(self.resolve_nodus_hostname(dev, device_type=device_type))

        for host, peers in (self.host_to_peer_ids or {}).items():
            try:
                if dev == host or dev in (peers or []):
                    _add_candidate(host)
                    for peer in (peers or []):
                        _add_candidate(peer)
            except Exception:
                continue

        for key in candidates:
            sensor_hardware = str((self.nodus_sensor_hardware or {}).get(key) or "").strip()
            if sensor_hardware:
                return sensor_hardware
        return ""

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
                    styles = self._normalize_display_styles(
                        entry.get("display_styles", []) or entry.get("styles", [])
                    )
                    if dev_id and metrics:
                        self.expected_gauge_map[dev_id] = metrics

                    if topic and dev_id:
                        location = _preserve_known_location(location, self.device_location.get(topic))
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
                            "display_styles": styles,
                        })
                        if topic not in self.registered_topics and not self._has_covering_subscription(topic):
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
                                _switch_key = build_switch_key(_switch_id, channel_id)
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
                            _switch_key = build_switch_key(_switch_id_flat, channel_id)
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
                styles = self._normalize_display_styles(
                    info.get("display_styles", []) or info.get("styles", [])
                )
                if dev_id and metrics:
                    self.expected_gauge_map[dev_id] = metrics

                if topic and dev_id:
                    location = _preserve_known_location(location, self.device_location.get(topic))
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
                        "display_styles": styles,
                    })
                    if topic not in self.registered_topics and not self._has_covering_subscription(topic):
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
            try:
                for sensor in discovered_sensors:
                    sensor_id_text = str((sensor or {}).get("sensor_id") or "").strip()
                    if sensor_id_text:
                        self._dashboard_inventory_notified_sensors.add(sensor_id_text.lower())
                        self._broadcast_dashboard_inventory_changed(host=base, sensor_id=sensor_id_text)
                for switch in discovered_switches:
                    switch_id_text = str((switch or {}).get("switch_id") or "").strip()
                    self._broadcast_dashboard_inventory_changed(host=base, switch_id=switch_id_text)
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
            from .saiSensorSettingsManager import SensorSettingsManager
            from .saiSwitchSettingsManager import SwitchSettingsManager
            from .saiSettings import saiSettings
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
                "aqi":   ["Air Quality", "Temperature", "Rel-Humidity", "Ambient VPD", "Dewpoint Deficit", "dewVPD Risk"],
                "avpd":  ["Ambient VPD", "Temperature", "Rel-Humidity", "Baro-Pressure", "Dewpoint Deficit", "dewVPD Risk"],
                "aht":   ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
                "aht10": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
                "ahtx0": ["Ambient VPD", "Temperature", "Rel-Humidity", "Humidity", "Dew Point Deficit", "DewVPD Risk"],
                "co2":   ["CO2", "Temperature", "Rel-Humidity", "Ambient VPD", "Dewpoint Deficit", "dewVPD Risk"],
                "lux":   ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
                "veml":  ["Light Intensity", "Auto Light", "Estimated PPFD", "Visible Light Intensity", "", ""],
                "soil":  ["Soil Moisture", "Soil Moisture Deficit", "Soil Stress Index", "Soil Temp_C", "Soil pH", "Soil EC"],
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

        def _is_unknown_loc(val: str | None) -> bool:
            v = str(val or "").strip().lower()
            return v in ("", "unknown", "n/a", "na", "none", "-")

        def _canonical_location(value: str | None) -> str:
            loc = str(value or "").strip()
            return loc if loc and not _is_unknown_loc(loc) else "Unknown"

        # ---- system_settings/<HOSTNAME>/settings.toml ----
        system_id = _strip_local(str((info or {}).get("HOSTNAME") or hostname or ""))
        if system_id:
            system_base_dir = resolve_runtime_base_dir(saiSettings.DEFAULT_BASE_DIR)
            sys_path = system_base_dir / system_id / saiSettings.STANDARD_FILENAME
            existed_before = sys_path.exists()
            nodus_tpl = system_base_dir / "factory_nodus" / f"{saiSettings.STANDARD_FILENAME}.def"
            fallback_tpl = system_base_dir / "factory" / saiSettings.STANDARD_FILENAME
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
                board_type = str(s.get("mcu") or s.get("MCU") or "").strip()
                sensor_hardware = str(
                    s.get("hardware")
                    or s.get("HARDWARE")
                    or s.get("sensor_hardware")
                    or s.get("SENSOR_HARDWARE")
                    or ""
                ).strip()
                physical_device_id = str(
                    s.get("physical_device_id")
                    or s.get("device_id")
                    or system_id
                    or ""
                ).strip()
                config_file = str(
                    s.get("config_file")
                    or s.get("name")
                    or ""
                ).strip()
                remote_display_metrics = self._normalize_display_metrics(
                    s.get("display_metrics") or s.get("metrics")
                )
                remote_display_styles = self._normalize_display_styles(
                    s.get("display_styles") or s.get("styles")
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
                    if board_type and str(sb.get("MCU", "") or "").strip() != board_type:
                        sb["MCU"] = board_type
                        changed = True
                    if sensor_hardware and str(sb.get("HARDWARE", "") or "").strip() != sensor_hardware:
                        sb["HARDWARE"] = sensor_hardware
                        changed = True
                    canonical_location = _canonical_location(location)
                    if str(sb.get("LOCATION", "") or "").strip() != canonical_location:
                        sb["LOCATION"] = canonical_location
                        changed = True
                    if sensor_id and str(sb.get("SENSOR_ID", "") or "").strip() != sensor_id:
                        sb["SENSOR_ID"] = sensor_id
                        changed = True
                    if device_type and str(sb.get("TYPE", "") or "").strip() != device_type:
                        sb["TYPE"] = device_type
                        changed = True
                    if "Nodus" not in data or not isinstance(data["Nodus"], dict):
                        data["Nodus"] = OrderedDict()
                        changed = True
                    nodus_block = data["Nodus"]
                    if (
                        physical_device_id
                        and str(nodus_block.get("DEVICE_ID", "") or "").strip()
                        != physical_device_id
                    ):
                        nodus_block["DEVICE_ID"] = physical_device_id
                        changed = True
                    if (
                        config_file
                        and str(nodus_block.get("CONFIG_FILE", "") or "").strip()
                        != config_file
                    ):
                        nodus_block["CONFIG_FILE"] = config_file
                        changed = True

                    if "Display" not in data or not isinstance(data["Display"], dict):
                        data["Display"] = OrderedDict()
                    display = data["Display"]
                    chosen_metrics = remote_display_metrics or _display_defaults_for_device(device_name or device_type)
                    for idx in range(6):
                        metric_key = f"METRIC_{idx + 1}"
                        metric_val = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                        if str(display.get(metric_key, "") or "") != metric_val:
                            display[metric_key] = metric_val
                            changed = True

                    if remote_display_styles:
                        style_block = display.get("Style")
                        if not isinstance(style_block, dict):
                            style_block = OrderedDict()
                            display["Style"] = style_block
                            changed = True
                        for idx in range(6):
                            style_key = f"METRIC_{idx + 1}"
                            style_val = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"
                            if str(style_block.get(style_key, "") or "") != style_val:
                                style_block[style_key] = style_val
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
                    sb["LOCATION"] = _canonical_location(location)
                    if serial:
                        sb["SERIAL_NUM"] = serial
                    if board_type:
                        sb["MCU"] = board_type
                    if sensor_hardware:
                        sb["HARDWARE"] = sensor_hardware
                    data["Nodus"] = OrderedDict()
                    data["Nodus"]["DEVICE_ID"] = physical_device_id
                    data["Nodus"]["CONFIG_FILE"] = config_file

                    if "Display" not in data or not isinstance(data["Display"], dict):
                        data["Display"] = OrderedDict()
                    display = data["Display"]
                    chosen_metrics = remote_display_metrics or _display_defaults_for_device(device_name or device_type)
                    for idx in range(6):
                        display[f"METRIC_{idx + 1}"] = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                    if remote_display_styles:
                        style_block = display.get("Style")
                        if not isinstance(style_block, dict):
                            style_block = OrderedDict()
                            display["Style"] = style_block
                        for idx in range(6):
                            style_block[f"METRIC_{idx + 1}"] = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"

                    sensor_mgr.save(sensor_id, data)
                else:
                    data = OrderedDict()
                    data["Sensor"] = OrderedDict()
                    sb = data["Sensor"]
                    if device_name:
                        sb["DEVICE"] = device_name
                    if device_type:
                        sb["TYPE"] = device_type
                    sb["SENSOR_ID"] = sensor_id
                    sb["LOCATION"] = _canonical_location(location)
                    if serial:
                        sb["SERIAL_NUM"] = serial
                    if board_type:
                        sb["MCU"] = board_type
                    if sensor_hardware:
                        sb["HARDWARE"] = sensor_hardware

                    data["Nodus"] = OrderedDict()
                    data["Nodus"]["DEVICE_ID"] = physical_device_id
                    data["Nodus"]["CONFIG_FILE"] = config_file

                    data["Display"] = OrderedDict()
                    chosen_metrics = remote_display_metrics or _display_defaults_for_device(device_name or device_type)
                    for idx in range(6):
                        data["Display"][f"METRIC_{idx + 1}"] = chosen_metrics[idx] if idx < len(chosen_metrics) else ""
                    if remote_display_styles:
                        style_block = OrderedDict()
                        data["Display"]["Style"] = style_block
                        for idx in range(6):
                            style_block[f"METRIC_{idx + 1}"] = remote_display_styles[idx] if idx < len(remote_display_styles) else "Graph24hr"

                    sensor_mgr.save(sensor_id, data)
                if DEBUG:
                    printDM(f"[itaot-settings] seeded sensor settings for {sensor_id}", location=MODULE)
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] sensor seed error: {exc}", location=MODULE)

        source_hostname = _strip_local(str((info or {}).get("HOSTNAME") or hostname or ""))

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
                board_type = str(sw.get("mcu") or sw.get("MCU") or "").strip()
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
                canonical_switch_location = _canonical_location(switch_loc)
                if str(sb.get("SWITCH_LOCATION", "") or "").strip() != canonical_switch_location:
                    sb["SWITCH_LOCATION"] = canonical_switch_location
                    changed = True
                if switch_type and str(sb.get("TYPE", "") or "").strip() != switch_type:
                    sb["TYPE"] = switch_type
                    changed = True
                if switch_serial and str(sb.get("DEVICE_SERIAL_NUM", "") or "").strip() != switch_serial:
                    sb["DEVICE_SERIAL_NUM"] = switch_serial
                    changed = True
                if board_type and str(sb.get("MCU", "") or "").strip() != board_type:
                    sb["MCU"] = board_type
                    changed = True
                # Overlay indexed switch fields from metadata so rendering tracks
                # authoritative channel IDs, labels, last state, and mqtt wiring.
                try:
                    src = {}
                    if isinstance(switch_payload, dict):
                        src = switch_payload.get("Switch") if isinstance(switch_payload.get("Switch"), dict) else switch_payload
                    if isinstance(src, dict):
                        incoming_indices: set[int] = set()
                        existing_indices: set[int] = set()
                        for existing_key in list(sb.keys()):
                            match = re.fullmatch(r"SWITCH_(\d+)_(.+)", str(existing_key or ""))
                            if match:
                                existing_indices.add(int(match.group(1)))
                        for k, v in src.items():
                            ks = str(k or "")
                            match = re.fullmatch(r"SWITCH_(\d+)_(.+)", ks)
                            if match:
                                incoming_indices.add(int(match.group(1)))
                            if not ks.startswith("SWITCH_") or ks == "SWITCH_DEVICE_ID":
                                continue

                            existing_val = sb.get(ks)
                            if existing_val != v:
                                sb[ks] = v
                                changed = True

                        preserve_richer_existing_remote = (
                            str(switch_type or "").strip().lower() in {"nodus", "picow", "pico2w", "remote", "mqtt"}
                            and source_hostname
                            and source_hostname.lower() == switch_id.lower()
                            and incoming_indices
                            and existing_indices
                            and len(existing_indices) > len(incoming_indices)
                            and incoming_indices.issubset(existing_indices)
                        )

                        if incoming_indices:
                            if preserve_richer_existing_remote:
                                if DEBUG:
                                    printDM(
                                        f"[itaot-settings] preserve richer switch definition for {switch_id}: "
                                        f"host={source_hostname} existing={sorted(existing_indices)} incoming={sorted(incoming_indices)}",
                                        location=MODULE,
                                    )
                            else:
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
                try:
                    valid_channel_ids: list[str] = []
                    for idx in range(1, 33):
                        label = str(sb.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                        channel_id = str(sb.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                        if label and channel_id:
                            valid_channel_ids.append(channel_id)
                    if valid_channel_ids and getattr(self, "data_logger", None):
                        self.data_logger.prune_switch_identities(
                            switch_id=switch_id,
                            valid_channel_ids=valid_channel_ids,
                        )
                except Exception:
                    pass

                if DEBUG:
                    action = "updated" if sw_path.exists() else "seeded"
                    printDM(f"[itaot-settings] {action} switch settings for {switch_id}", location=MODULE)
        except Exception as exc:
            if DEBUG:
                printDM(f"[itaot-settings] switch seed error: {exc}", location=MODULE)

    async def force_refresh_device_metadata(self, sensor_or_host: str, *, port: int = 8000, timeout_sec: float = 6.0) -> bool:
        """
        Runtime refreshes are MQTT-only.
        This compatibility shim keeps existing callers from failing but does not
        perform HTTP requests against Nodus devices.
        """
        try:
            hostname = self._resolve_hostname_for(sensor_or_host)
            if not hostname:
                if DEBUG:
                    printDM("[force_refresh] could not resolve hostname", location=MODULE)
                return False
            self.add_client(hostname)
        except Exception as exc:
            if DEBUG:
                printDM(f"[force_refresh] mqtt-only refresh shim failed for {sensor_or_host}: {exc}", location=MODULE)
            return False
        return False

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

        # topic: nodus/<channel_id>/config/set or legacy nodus/<channel_id>/set (passthrough)
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
                printDM(f"[HA cmd] forward failed {topic} -> {txt}", location=MODULE)
            return
        

    def _pending_switch_meta(self, switch_id: str, channel_id: str | None = None, label: str | None = None) -> dict | None:
        try:
            sid = str(switch_id or "").strip()
            ch = str(channel_id or "").strip().lower()
            lbl = str(label or "").strip().lower()
            if not sid:
                return None
            for key, meta in list((self._pending_set or {}).items()):
                key_sid = str((key or ("", ""))[0] or "").strip()
                key_label = str((key or ("", ""))[1] or "").strip().lower()
                if key_sid != sid:
                    continue
                meta_channel = str((meta or {}).get("channel_id") or "").strip().lower()
                if lbl and key_label == lbl:
                    return dict(meta or {})
                if ch and meta_channel == ch:
                    return dict(meta or {})
            now = time.time()
            for key, meta in list((self._recent_switch_origin or {}).items()):
                expires_at = float((meta or {}).get("expires_at") or 0.0)
                if expires_at and expires_at < now:
                    self._recent_switch_origin.pop(key, None)
                    continue
                key_sid = str((key or ("", ""))[0] or "").strip()
                key_label = str((key or ("", ""))[1] or "").strip().lower()
                if key_sid != sid:
                    continue
                meta_channel = str((meta or {}).get("channel_id") or "").strip().lower()
                if lbl and key_label == lbl:
                    return dict(meta or {})
                if ch and meta_channel == ch:
                    return dict(meta or {})
            return None
        except Exception:
            return None

    def set_switch(
        self,
        switch_id: str,
        channel_label: str,
        new_state: bool,
        qos: int = 0,
        retain: bool = False,
        *,
        event_origin: str | None = None,
        event_label: str | None = None,
    ) -> bool:
        """
        Publish a remote switch change by writing SWITCH_n_LAST_STATE over
        Nodus config/set.
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
                            channel_id = str(row.get("channel_id", "") or "").strip()
                            if channel_id:
                                break
                except Exception:
                    channel_id = None

            if not channel_id:
                printDM(f"[set_switch] No channel_id for {switch_id}::{channel_label}", location=MODULE)
                return False

            ok = self.set_switch_by_channel_id(
                str(switch_id or "").strip(),
                str(channel_id or "").strip(),
                bool(new_state),
                qos=qos,
                retain=retain,
            )
            if ok:
                self._pending_set[(str(switch_id), str(channel_label))] = {
                    "ts": time.time(),
                    "state": bool(new_state),
                    "channel_id": str(channel_id or "").strip(),
                    "event_origin": str(event_origin or "").strip().lower(),
                    "event_label": str(event_label or "").strip(),
                }
                if DEBUG:
                    printDM(
                        f"[set_switch] queued switch_id={switch_id} channel_id={channel_id} label={channel_label} new_state={new_state}",
                        location=MODULE,
                    )
            return ok
        except Exception as e:
            printDM(f"[set_switch] error: {e}", location=MODULE)
            return False

    def clear_pending_switch_set(self, switch_id: str, channel_id: str | None = None, label: str | None = None) -> None:
        """
        Drop optimistic remote command markers once authoritative MQTT state arrives.
        """
        try:
            sid = str(switch_id or "").strip()
            ch = str(channel_id or "").strip().lower()
            lbl = str(label or "").strip().lower()
            if not sid:
                return

            keys_to_remove: list[tuple[str, str]] = []
            for key, meta in list((self._pending_set or {}).items()):
                key_sid = str((key or ("", ""))[0] or "").strip()
                key_label = str((key or ("", ""))[1] or "").strip().lower()
                if key_sid != sid:
                    continue
                meta_channel = str((meta or {}).get("channel_id") or "").strip().lower()
                if lbl and key_label == lbl:
                    keys_to_remove.append(key)
                    continue
                if ch and meta_channel == ch:
                    keys_to_remove.append(key)
                    continue
            for key in keys_to_remove:
                meta = self._pending_set.pop(key, None)
                if isinstance(meta, dict):
                    self._recent_switch_origin[key] = {
                        **dict(meta),
                        "expires_at": time.time() + 8.0,
                    }
                    meta_channel = str(meta.get("channel_id") or "").strip()
                    if meta_channel:
                        self._clear_switch_config_command(sid, meta_channel)
            if ch:
                self._clear_switch_config_command(sid, ch)
        except Exception:
            pass

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
        update_cache: bool = True,
        label_hint: str | None = None,
    ):
        """
        Persist a switch event *only if* it represents a state change relative to cache.

        Notes:
          - channel_id is the stable per-channel identifier (e.g. SWITCH_1_CHANNEL_ID = "S1-123456").
          - The canonical DB key is build_switch_key(switch_id, channel_id) => "<switch_id>::<channel_id>".
          - sensor_lineage lets us store the specific origin (e.g., "switch-oqs3lr-GP28") when known.
        """
        try:
            if not switch_id or not channel_id:
                return

            switch_id_str  = str(switch_id)
            channel_id_str = str(channel_id)
            label_hint_text = str(label_hint or "").strip()

            last_cache = self._switch_state_cache.setdefault(switch_id_str, {})
            new_state  = "on" if is_on else "off"
            state_key = (switch_id_str, channel_id_str)

            label_resolved = label_hint_text
            location_resolved = None
            if not label_resolved:
                try:
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "") or "").strip()
                        rch = str(row.get("channel_id", "") or "").strip()
                        rlab = str(row.get("label", "") or "").strip()
                        if rsid == switch_id_str and rch == channel_id_str and rlab:
                            label_resolved = rlab
                            location_resolved = row.get("location")
                            break
                except Exception:
                    label_resolved = ""
                    location_resolved = None
            else:
                try:
                    for row in (self.data_logger.get_switch_identities() or []):
                        rsid = str(row.get("switch_id", "") or "").strip()
                        rch = str(row.get("channel_id", "") or "").strip()
                        if rsid == switch_id_str and rch == channel_id_str:
                            location_resolved = row.get("location")
                            break
                except Exception:
                    location_resolved = None
            if not label_resolved:
                label_resolved = channel_id_str

            if label_hint_text:
                try:
                    upsert_identity = getattr(self.data_logger, "upsert_switch_identity", None)
                    if callable(upsert_identity):
                        upsert_identity(
                            switch_key=build_switch_key(switch_id_str, channel_id_str),
                            switch_id=switch_id_str,
                            label=label_resolved,
                            location=location_resolved,
                        )
                except Exception:
                    pass

            last_state = str(self._last_persisted_switch_state.get(state_key, "")).lower()
            if last_state not in ("on", "off"):
                try:
                    latest = self.data_logger.get_latest_switch_state(
                        build_switch_key(switch_id_str, channel_id_str),
                        sensor_id=(sensor_lineage or switch_id_str),
                    )
                    if latest is not None:
                        last_state = "on" if str(latest).strip().lower() == "on" else "off"
                except Exception:
                    pass

            if (not force_write) and last_state == new_state:
                return

            if update_cache:
                last_cache[channel_id_str] = new_state
                self._known_switch_ids.add(switch_id_str)

            writer = getattr(self.data_logger, "log_switch_event", None)
            if callable(writer):
                writer(
                    switch_key=build_switch_key(switch_id_str, channel_id_str),
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
                    f"[persist] {switch_id_str}::{channel_id_str} {last_state or 'unknown'} -> {new_state} (src={source})",
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
            self.clear_pending_switch_set(switch_id, channel_id=channel_id, label=label)
            self._known_switch_ids.add(switch_id)
            try:
                self.last_mqtt_seen[switch_id] = time.time()
            except Exception:
                pass

            if DEBUG:
                db_key = build_switch_key(switch_id, channel_id)
                printDM(
                    f"[state] {switch_id} [{channel_id}] -> {cache[channel_id]} db_key={db_key} (topic {topic})",
                    location=MODULE,
                )
        except Exception as e:
            printDM(f"[handle_switch_state_slug] err: {e}", location=MODULE)

    def handle_nodus_switch_topic(self, topic: str, payload: str, *, retain: bool = False):
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
                self.clear_pending_switch_set(sid, channel_id=ch_id, label=(labels[0] if labels else hint))
                self._known_switch_ids.add(sid)
                return labels

            payload_text = "" if payload is None else str(payload).strip()
            payload_obj = None
            if payload_text.startswith("{") and payload_text.endswith("}"):
                try:
                    parsed = json.loads(payload_text)
                    if isinstance(parsed, dict):
                        payload_obj = parsed
                except Exception:
                    payload_obj = None

            info = self.nodus_switch_topic_map.get(topic)
            if not info:
                topic_parts = str(topic or "").split("/")
                topic_kind = topic_parts[-1] if topic_parts else ""
                topic_channel_id = self._channel_id_from_topic(topic)
                if topic_kind in {"state", "event"} and topic_channel_id:
                    payload_switch_id = ""
                    payload_channel_id = ""
                    payload_label = ""
                    if isinstance(payload_obj, dict):
                        payload_switch_id = str(
                            payload_obj.get("device_id")
                            or payload_obj.get("switch_device_id")
                            or payload_obj.get("switch_id")
                            or ""
                        ).strip()
                        payload_channel_id = str(payload_obj.get("channel_id") or "").strip()
                        payload_label = str(payload_obj.get("label") or "").strip()
                    inferred_channel_id = payload_channel_id or topic_channel_id
                    inferred_switch_id = payload_switch_id or self._switch_id_for_channel_id(inferred_channel_id)
                    if inferred_switch_id and inferred_channel_id:
                        labels = _labels_for_channel(inferred_switch_id, inferred_channel_id, hint=payload_label)
                        inferred_label = payload_label or (labels[0] if labels else inferred_channel_id)
                        info = {
                            "switch_id": inferred_switch_id,
                            "channel_id": inferred_channel_id,
                            "label": inferred_label,
                            "kind": topic_kind,
                        }
                        self.nodus_switch_topic_map[topic] = info
                        if topic_kind == "state":
                            self.nodus_switch_state_topics[(inferred_switch_id, inferred_channel_id)] = topic
                        elif topic_kind == "event":
                            self.nodus_switch_event_topics[(inferred_switch_id, inferred_channel_id)] = topic
                        self.nodus_label_to_channel[(inferred_switch_id, _norm_label(inferred_label))] = inferred_channel_id
                        self._known_switch_ids.add(inferred_switch_id)
                        try:
                            upsert_identity = getattr(self.data_logger, "upsert_switch_identity", None)
                            if callable(upsert_identity) and inferred_label:
                                upsert_identity(
                                    switch_key=build_switch_key(inferred_switch_id, inferred_channel_id),
                                    switch_id=inferred_switch_id,
                                    label=inferred_label,
                                    location=self.device_location.get(topic),
                                )
                        except Exception:
                            pass
                if not info:
                    return

            switch_id = info.get("switch_id")
            channel_id = info.get("channel_id")
            label = info.get("label")
            kind = info.get("kind")
            pending_meta = self._pending_switch_meta(str(switch_id or ""), channel_id=str(channel_id or ""), label=str(label or ""))

            if not switch_id or not channel_id or kind not in ("state", "event"):
                return

            now_t = time.time()
            self._record_mqtt_seen(str(switch_id), ts=now_t, retain=retain, report=(not retain))
            try:
                host = self.resolve_nodus_hostname(str(switch_id), device_type="switch")
                if host:
                    self._record_host_seen(host, ts=now_t, retain=retain, report=(not retain))
            except Exception:
                host = None

            is_on: bool | None = None
            payload_label: str | None = None
            # Nodus switch payload timestamps are not trusted; persist with hub-local time.
            ts_iso: str | None = None
            source = "mqtt-nodus"
            pending_origin = str((pending_meta or {}).get("event_origin") or "").strip().lower()
            pending_label = str((pending_meta or {}).get("event_label") or "").strip()
            if pending_origin == "manual":
                source = "mqtt-manual"
            elif pending_origin == "auto":
                source = f"mqtt-auto:{pending_label}" if pending_label else "mqtt-auto"

            def _state_value_is_on(value: object) -> bool:
                return str(value).strip().lower() in ("on", "1", "true", "t", "yes", "y")

            # JSON payload (preferred)
            if isinstance(payload_obj, dict):
                if isinstance(payload_obj.get("label"), str):
                    payload_label = str(payload_obj.get("label") or "").strip() or None

                # optional source
                if isinstance(payload_obj.get("source"), str) and not pending_origin:
                    source = payload_obj.get("source") or source

                # Preferred Nodus switch event/state shape uses top-level state.
                if payload_obj.get("state") is not None:
                    is_on = _state_value_is_on(payload_obj.get("state"))

                # Backward-compatible shape: extract ON/OFF from "event" dict.
                ev = payload_obj.get("event") or {}
                if is_on is None and isinstance(ev, dict) and ev:
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
                        is_on = _state_value_is_on(state_val)

            # Legacy plain-text payload
            if is_on is None:
                is_on = payload_text.upper() == "ON"

            if kind == "state":
                # Persist state transitions too; dedupe prevents retained-state spam.
                force_write = False
                try:
                    label_resolved = payload_label
                    for row in (self.data_logger.get_switch_identities() or []):
                        if label_resolved:
                            break
                        rsid = str(row.get("switch_id", "") or "").strip()
                        rch = str(row.get("channel_id", "") or "").strip()
                        rlab = str(row.get("label", "") or "").strip()
                        if rsid == str(switch_id) and rch == str(channel_id) and rlab:
                            label_resolved = rlab
                            break
                    if not label_resolved:
                        label_resolved = label or str(channel_id)
                    db_key = build_switch_key(str(switch_id), str(channel_id))
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
                    source=(
                        source
                        if str(source or "").strip().lower().startswith(("mqtt-auto:", "mqtt-manual"))
                        else f"{source}-state"
                    ),
                    sensor_lineage=f"Switch_{switch_id}",
                    force_write=force_write,
                    label_hint=payload_label,
                )
                labels = _cache_channel_state(switch_id, channel_id, is_on, hint=(payload_label or label))
                try:
                    if host:
                        self._mark_host_status(host, self.get_nodus_liveness(host, now_ts=now_t).get("state", "unknown"))
                except Exception:
                    pass
                ui_label = labels[0] if labels else (label or channel_id)
                self._broadcast_switch_event(
                    switch_id=switch_id,
                    channel_id=channel_id,
                    label=ui_label,
                    is_on=bool(is_on),
                    source=source,
                )
            elif kind == "event":
                self._maybe_persist_switch_event(
                    switch_id=switch_id,
                    channel_id=channel_id,
                    is_on=is_on,
                    ts_iso=ts_iso,
                    source=source,
                    sensor_lineage=f"Switch_{switch_id}",
                    update_cache=False,
                    label_hint=payload_label,
                )
                labels = _labels_for_channel(switch_id, channel_id, hint=(payload_label or label))
                self._known_switch_ids.add(switch_id)
                try:
                    if host:
                        self._mark_host_status(host, self.get_nodus_liveness(host, now_ts=now_t).get("state", "unknown"))
                except Exception:
                    pass
                ui_label = labels[0] if labels else (label or channel_id)
                # Push live updates to the UI (label-based key for listbox match)
                self._broadcast_switch_event(
                    switch_id=switch_id,
                    channel_id=channel_id,
                    label=ui_label,
                    is_on=bool(is_on),
                    source=source,
                )

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
        parts = str(topic or "").split("/")
        if len(parts) < 3 or parts[0] != "switch" or parts[-1] != "event":
            return

        try:
            obj = json.loads(payload)
        except Exception:
            return  # not JSON
        if not isinstance(obj, dict):
            return

        try:
            ev = obj.get("event") or {}
            if not isinstance(ev, dict) or not ev:
                return

            # Extract single (channel_key -> state) pair
            (label_key, state_str) = list(ev.items())[0]
            is_on = str(state_str).strip().lower() in ("on", "1", "true", "t", "yes", "y")

            # topic: "switch/<switch_id>-<pin>/event"  or  "switch/<switch_id>/<SWITCH_n>/event"
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
            self._broadcast_switch_event(
                switch_id=switch_id,
                channel_id=channel_id,
                label=label,
                is_on=bool(is_on),
                source="mqtt",
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

    @staticmethod
    def _topic_matches_filter(topic_filter: str, topic: str) -> bool:
        filt_parts = str(topic_filter or "").split("/")
        topic_parts = str(topic or "").split("/")
        ti = 0
        for fi, part in enumerate(filt_parts):
            if part == "#":
                return fi == len(filt_parts) - 1
            if ti >= len(topic_parts):
                return False
            if part != "+" and part != topic_parts[ti]:
                return False
            ti += 1
        return ti == len(topic_parts)

    def _retained_command_filters(self) -> list[str]:
        filters = [
            "nodus/+/config/set",
            "nodus/+/calibration/set",
        ]
        base = str(getattr(self, "base_topic", "") or "").strip().strip("/")
        if base and base != "nodus":
            filters.extend([
                f"{base}/nodus/+/config/set",
                f"{base}/nodus/+/calibration/set",
            ])
        seen: set[str] = set()
        out: list[str] = []
        for item in filters:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def _summarize_retained_command_payload(self, payload_text: str) -> dict:
        raw = str(payload_text or "")
        summary: dict = {
            "payload_bytes": len(raw.encode("utf-8", errors="ignore")),
            "payload_format": "text",
        }
        try:
            data = json.loads(raw)
        except Exception:
            return summary
        if not isinstance(data, dict):
            summary["payload_format"] = type(data).__name__
            return summary

        summary["payload_format"] = "json"
        summary["envelope_keys"] = sorted(str(k) for k in data.keys())
        message_id = str(data.get("message_id") or "").strip()
        if message_id:
            summary["message_id"] = message_id
        if "restart" in data:
            summary["restart"] = bool(data.get("restart"))
        action = str(data.get("action") or "").strip()
        if action:
            summary["action"] = action

        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        updates = payload.get("updates") if isinstance(payload.get("updates"), list) else []
        redacted_updates: list[dict] = []
        for item in updates:
            if not isinstance(item, dict):
                continue
            redacted_updates.append({
                "section": str(item.get("section") or "").strip(),
                "key": str(item.get("key") or "").strip(),
                "name": str(item.get("name") or "").strip(),
            })
        if redacted_updates:
            summary["update_count"] = len(redacted_updates)
            summary["updates"] = redacted_updates[:12]

        settings_payload = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        if settings_payload:
            summary["settings_sections"] = sorted(str(k) for k in settings_payload.keys())
        return summary

    def scan_retained_command_topics(self, *, timeout: float = 1.0, limit: int = 64) -> dict:
        """
        Temporarily scan command topics for retained non-empty /set payloads.

        Returned payload details are intentionally redacted because retained
        config commands can contain Wi-Fi, MQTT, or integration credentials.
        """
        try:
            timeout_s = max(0.2, min(float(timeout), 5.0))
        except Exception:
            timeout_s = 1.0
        try:
            limit_n = max(1, min(int(limit), 200))
        except Exception:
            limit_n = 64

        filters = self._retained_command_filters()
        client = getattr(self, "client", None)
        connected = False
        try:
            checker = getattr(client, "is_connected", None)
            connected = bool(checker()) if callable(checker) else bool(client)
        except Exception:
            connected = False
        result = {
            "ok": False,
            "broker": str(getattr(self, "broker", "") or ""),
            "port": int(getattr(self, "port", 1883) or 1883),
            "client_connected": connected,
            "scanned_filters": filters,
            "retained_command_count": 0,
            "retained_commands": [],
        }
        if not client or not connected:
            result["error"] = "mqtt_client_not_connected"
            return result

        add_cb = getattr(client, "message_callback_add", None)
        remove_cb = getattr(client, "message_callback_remove", None)
        subscribe = getattr(client, "subscribe", None)
        unsubscribe = getattr(client, "unsubscribe", None)
        if not callable(add_cb) or not callable(subscribe):
            result["error"] = "mqtt_client_callbacks_unavailable"
            return result

        found: list[dict] = []
        seen_topics: set[str] = set()
        lock = threading.RLock()

        def _callback(_client, _userdata, msg) -> None:
            try:
                if not bool(getattr(msg, "retain", False)):
                    return
                topic = str(getattr(msg, "topic", "") or "").strip()
                raw = getattr(msg, "payload", b"")
                if isinstance(raw, (bytes, bytearray)):
                    payload_text = raw.decode("utf-8", errors="ignore")
                else:
                    payload_text = str(raw or "")
                if self._is_empty_retained_cleanup_payload(payload_text):
                    return
                with lock:
                    if topic in seen_topics or len(found) >= limit_n:
                        return
                    seen_topics.add(topic)
                    entry = {
                        "topic": topic,
                        "retain": True,
                    }
                    entry.update(self._summarize_retained_command_payload(payload_text))
                    found.append(entry)
            except Exception:
                return

        preexisting: dict[str, bool] = {}
        try:
            for topic_filter in filters:
                try:
                    preexisting[topic_filter] = (
                        topic_filter in getattr(self, "registered_topics", set())
                        or self._has_covering_subscription(topic_filter)
                    )
                except Exception:
                    preexisting[topic_filter] = False
                add_cb(topic_filter, _callback)
                subscribe(topic_filter, qos=0)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                with lock:
                    if len(found) >= limit_n:
                        break
                time.sleep(0.05)
        finally:
            for topic_filter in filters:
                try:
                    if callable(remove_cb):
                        remove_cb(topic_filter)
                except Exception:
                    pass
                try:
                    if callable(unsubscribe) and not preexisting.get(topic_filter, False):
                        unsubscribe(topic_filter)
                except Exception:
                    pass

        with lock:
            commands = [dict(item) for item in found]
        result["ok"] = True
        result["retained_command_count"] = len(commands)
        result["retained_commands"] = commands
        if len(commands) >= limit_n:
            result["truncated"] = True
        return result
        
    @staticmethod
    def _is_empty_retained_cleanup_payload(payload) -> bool:
        if payload is None:
            return True
        if isinstance(payload, bytes):
            return len(payload) == 0
        return str(payload) == ""

    def publish_text(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False, use_ha_client: bool = True) -> bool:
        try:
            if not topic:
                return False
            if retain and str(topic).endswith("/set") and not self._is_empty_retained_cleanup_payload(payload):
                printDM(
                    f"[publish_text] refusing retained command publish to {topic}; /set commands must be non-retained unless a cleanup flow owns clearing them",
                    location=MODULE,
                )
                return False
            client = (self.ha_client or self.client) if use_ha_client else self.client
            info = client.publish(topic, payload, qos=qos, retain=retain)
            rc = getattr(info, "rc", 0) if info is not None else 0
            if DEBUG and topic.endswith("/config/set"):
                printDM(
                    f"[publish_text] topic={topic} use_ha_client={bool(use_ha_client)} rc={rc} client={self._describe_publish_client(use_ha_client=use_ha_client)} bytes={len(payload or '')}",
                    location=MODULE,
                )
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

    def _describe_publish_client(self, *, use_ha_client: bool) -> str:
        client = (self.ha_client or self.client) if use_ha_client else self.client
        if client is self.ha_client and self.ha_client is not self.client:
            broker = self.ha_broker
            port = self.ha_port
            name = "ha_client"
        else:
            broker = self.broker
            port = self.port
            name = "client"
        try:
            connected = bool(client.is_connected()) if client else False
        except Exception:
            connected = False
        try:
            client_id = client._client_id.decode("utf-8", errors="ignore") if getattr(client, "_client_id", None) else ""
        except Exception:
            client_id = ""
        return f"{name}(client_id={client_id or 'unknown'}, connected={connected}, broker={broker}, port={port})"

    def _summarize_nodus_config_payload(self, envelope: dict | None) -> str:
        body = dict(envelope or {})
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        updates = payload.get("updates") if isinstance(payload.get("updates"), list) else []
        settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        if updates:
            parts = []
            for item in updates[:4]:
                if not isinstance(item, dict):
                    continue
                section = str(item.get("section") or "").strip()
                key = str(item.get("key") or "").strip()
                sensitive = key.upper() in {
                    "PASSWORD",
                    "PASS",
                    "TOKEN",
                    "SECRET",
                    "API_KEY",
                    "MQTT_PASSWORD",
                    "HA_PASSWORD",
                }
                value = "'<redacted>'" if sensitive else repr(item.get("value"))
                name = str(item.get("name") or "").strip()
                suffix = f"@{name}" if name else ""
                parts.append(f"{section}.{key}={value}{suffix}")
            extra = "" if len(updates) <= 4 else f" +{len(updates) - 4} more"
            return f"updates[{len(updates)}]: " + ", ".join(parts) + extra
        if settings:
            return f"settings_sections={list(settings.keys())}"
        return f"keys={list(body.keys())}"
        
    async def mqtt_discovery_loop(self):
        """
        Discovery + liveness loop.
          Runtime behavior is MQTT-only.
          AP-mode onboarding uses HTTP outside this ingest loop.
        """
        import random

        TICK_INTERVAL_S = 29.33
        OFFLINE_RETRIES = 5
        MQTT_GRACE_S = 120.0

        if not hasattr(self, "last_mqtt_seen"):
            self.last_mqtt_seen = {}
        if not hasattr(self, "last_nodus_report_seen"):
            self.last_nodus_report_seen = {}
        if not hasattr(self, "retained_mqtt_seen"):
            self.retained_mqtt_seen = {}
        if not hasattr(self, "host_to_peer_ids"):
            self.host_to_peer_ids = {}
        if not hasattr(self, "device_status"):
            self.device_status = {}
        if not hasattr(self, "discovery_failures"):
            self.discovery_failures = {}
        if not hasattr(self, "last_check_time"):
            self.last_check_time = {}
        if not hasattr(self, "device_offline_count"):
            self.device_offline_count = {}

        try:
            await asyncio.sleep(1.0)
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
                                self.mqtt_clients.discard(hostname)
                                continue

                            self._feed_watchdog("MQTT Discovery Loop")
                            await asyncio.sleep(0)
                            base = self._normalize_host_key(hostname)
                            if not base:
                                continue

                            if (now_mono - self.last_check_time.get(base, 0.0)) < TICK_INTERVAL_S:
                                continue
                            self.last_check_time[base] = now_mono

                            now_ts = time.time()
                            snapshot = self.get_nodus_liveness(base, now_ts=now_ts)
                            derived = self._normalize_liveness_state(snapshot.get("state"))

                            if derived == "offline":
                                self.device_offline_count[base] = OFFLINE_RETRIES
                                self.discovery_failures[base] = now_mono
                                self._mark_host_status(base, "offline")
                                continue

                            if derived == "unknown":
                                peers = self.host_to_peer_ids.get(base, [])
                                recent = any((now_ts - self.last_nodus_report_seen.get(pid, 0.0)) < MQTT_GRACE_S for pid in peers)
                                if recent:
                                    self.device_offline_count[base] = 0
                                    self._mark_host_status(base, "degraded")
                                    continue

                                n = self.device_offline_count.get(base, 0) + 1
                                self.device_offline_count[base] = n
                                self.discovery_failures[base] = now_mono
                                if n < OFFLINE_RETRIES:
                                    self._mark_host_status(base, "degraded")
                                else:
                                    self._mark_host_status(base, "offline")
                                continue

                            self.device_offline_count[base] = 0
                            self._mark_host_status(base, derived)

                        except Exception as host_loop_err:
                            self._feed_watchdog("MQTT Discovery Loop")
                            printDM(
                                f"[mqtt_discovery_loop] Unexpected error while probing {base}: {host_loop_err}",
                                location=MODULE,
                            )

                end_at = time.monotonic() + (TICK_INTERVAL_S + random.uniform(-0.5, 0.5))
                while time.monotonic() < end_at:
                    await asyncio.sleep(0.5)
                    self._feed_watchdog("MQTT Discovery Loop")
                await asyncio.sleep(0)

        except Exception as outer_e:
            printDM(f"[mqtt_discovery_loop] fatal exception: {outer_e}", location=MODULE)
