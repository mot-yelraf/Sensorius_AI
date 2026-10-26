"""Track Nodus MQTT commands independently of transport and settings writes.

This component owns reply correlation, switch request bookkeeping, calibration
receipt state, and the existing wait deadlines. Ingest retains MQTT I/O, topic
routing, payload normalization, and device-state persistence. Mutable compatibility
aliases share this state with existing callers; no device traffic is generated.
"""

import asyncio
import json
import threading
import time

from .saiUtils import debug_enabled, printDM

# Keep the existing ingest debug flag for the extracted operator messages.
MODULE = "saiMQTTIngest"
DEBUG = debug_enabled(MODULE)
NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S = 45.0
NODUS_SWITCH_COMMAND_FAILED_COOLDOWN_S = 5.0


class MQTTCommandLifecycle:
    """Own correlated command receipts and pending switch request state."""

    def __init__(self, *, clock=None):
        """Create isolated command state without opening storage or connections."""
        self._clock = clock if clock is not None else lambda: time.time()
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
        self._pending_set = {}
        self._recent_switch_origin = {}

    async def wait_for_config_ack(self, message_id: str, timeout: float = 3.0) -> dict | None:
        """Wait for config ack."""
        deadline = self._clock() + max(float(timeout), 0.0)
        while self._clock() < deadline:
            with self._config_lock:
                hit = self.config_ack_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_config_result(self, message_id: str, timeout: float = 8.0) -> dict | None:
        """Wait for config result."""
        deadline = self._clock() + max(float(timeout), 0.0)
        while self._clock() < deadline:
            with self._config_lock:
                hit = self.config_result_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_calibration_ack(self, message_id: str, timeout: float = 3.0) -> dict | None:
        """Wait for calibration ack."""
        deadline = self._clock() + max(float(timeout), 0.0)
        while self._clock() < deadline:
            with self._calibration_lock:
                hit = self.calibration_ack_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_calibration_result(self, message_id: str, timeout: float = 8.0) -> dict | None:
        """Wait for calibration result."""
        deadline = self._clock() + max(float(timeout), 0.0)
        while self._clock() < deadline:
            with self._calibration_lock:
                hit = self.calibration_result_by_message.get(message_id)
                if hit is not None:
                    return dict(hit)
            await asyncio.sleep(0.05)
        return None

    async def wait_for_calibration_samples(self, message_id: str, expected_count: int | None = None, timeout: float = 30.0) -> list[dict]:
        """Wait for calibration samples."""
        deadline = self._clock() + max(float(timeout), 0.0)
        target = None
        try:
            if expected_count is not None:
                target = max(int(expected_count), 0)
        except Exception:
            target = None

        last_seen: list[dict] = []
        while self._clock() < deadline:
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
        """Wait for nodus meta patch."""
        deadline = self._clock() + max(float(timeout), 0.0)
        want_source = str(source or "").strip().lower()
        while self._clock() < deadline:
            with self._meta_patch_lock:
                hit = self.meta_patch_by_message.get(str(message_id or "").strip())
                if isinstance(hit, dict):
                    if not want_source or str(hit.get("source") or "").strip().lower() == want_source:
                        return dict(hit)
            await asyncio.sleep(0.05)
        return None

    def get_nodus_calibration_state(self, sensor_id: str) -> dict | None:
        """Get nodus calibration state."""
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

    def _switch_config_command_key(self, switch_id: str, channel_id: str) -> str:
        """Switch config command key."""
        ch = str(channel_id or "").strip().lower()
        if ch:
            return ch
        sid = str(switch_id or "").strip().lower()
        return f"switch:{sid}" if sid else ""

    def _prune_switch_config_commands_locked(self, now_ts: float | None = None) -> None:
        """Prune switch config commands locked."""
        now_v = self._clock() if now_ts is None else float(now_ts)
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
        """Begin switch config command."""
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return "blocked"
        now_v = self._clock()
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
        """Mark switch config command published."""
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
        """Mark switch config command failed."""
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return
        now_v = self._clock()
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
        """Clear switch config command."""
        key = self._switch_config_command_key(switch_id, channel_id)
        if not key:
            return
        with self._switch_config_command_lock:
            meta = self._switch_config_command_inflight.pop(key, None) or {}
            mid = str(meta.get("message_id") or "").strip() if isinstance(meta, dict) else ""
            if mid:
                self._switch_config_command_by_message.pop(mid, None)

    def _clear_switch_config_command_by_message(self, message_id: str) -> None:
        """Clear switch config command by message."""
        mid = str(message_id or "").strip()
        if not mid:
            return
        with self._switch_config_command_lock:
            key = self._switch_config_command_by_message.pop(mid, None)
            if key:
                self._switch_config_command_inflight.pop(key, None)

    def _pending_switch_meta(self, switch_id: str, channel_id: str | None = None, label: str | None = None) -> dict | None:
        """Pending switch meta."""
        try:
            sid = str(switch_id or "").strip()
            ch = str(channel_id or "").strip().lower()
            lbl = str(label or "").strip().lower()
            if not sid:
                return None
            for key, meta in list((self._pending_set or {}).items()):
                if self._clock() - float((meta or {}).get("ts") or 0.0) > NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S:
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
            now = self._clock()
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

    def get_switch_command_status(self, switch_id: str, label: str = "", channel_id: str = "") -> dict:
        """Describe an outbound request without treating it as device state."""
        for (sid, pending_label), meta in list(self._pending_set.items()):
            if str(sid) != str(switch_id):
                continue
            if pending_label != label and (not channel_id or str(meta.get("channel_id")) != channel_id):
                continue
            until = float(meta.get("ts") or 0) + NODUS_SWITCH_COMMAND_INFLIGHT_TTL_S
            mid = str(meta.get("message_id") or "")
            result = self.config_result_by_message.get(mid) or {}
            ack = self.config_ack_by_message.get(mid) or {}
            error = ""
            if result and not result.get("applied"):
                error = str(result.get("error") or "Device rejected the switch command.")
            elif ack and not ack.get("accepted"):
                error = str(ack.get("error") or "Device rejected the switch command.")
            elif self._clock() >= until:
                error = "Device did not confirm the switch change."
            return {"pending": not bool(error), "requested_state": bool(meta.get("state")),
                    "pending_until": until, "command_message_id": mid, "command_error": error,
                    "command_warning": "Device applied the change temporarily; settings were not saved." if result.get("persistence_mode") == "volatile" else ""}
        for (sid, recent_label), meta in list(self._recent_switch_origin.items()):
            if str(sid) != str(switch_id) or float(meta.get("expires_at") or 0) <= self._clock():
                continue
            if recent_label != label and (not channel_id or str(meta.get("channel_id")) != channel_id):
                continue
            result = self.config_result_by_message.get(str(meta.get("message_id") or "")) or {}
            if result.get("persistence_mode") == "volatile":
                return {"pending": False, "command_error": "", "command_warning": "Device applied the change temporarily; settings were not saved."}
        return {"pending": False, "command_error": ""}

    def clear_pending_switch_set(self, switch_id: str, channel_id: str | None = None, label: str | None = None, *, observed_state: bool | None = None) -> None:
        """
        Clear a request after a matching device state report, preserving its origin.
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
                if observed_state is not None and bool((meta or {}).get("state")) != observed_state:
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
                        "expires_at": self._clock() + 8.0,
                        "confirmed_at": self._clock(),
                    }
                    meta_channel = str(meta.get("channel_id") or "").strip()
                    if meta_channel:
                        self._clear_switch_config_command(sid, meta_channel)
            if ch and (observed_state is None or keys_to_remove):
                self._clear_switch_config_command(sid, ch)
        except Exception:
            pass

    def _retained_command_filters(self, base_topic="") -> list[str]:
        """Retained command filters."""
        filters = [
            "nodus/+/config/set",
            "nodus/+/calibration/set",
        ]
        base = str(base_topic or "").strip().strip("/")
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
        """Summarize retained command payload."""
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

    @staticmethod
    def _is_empty_retained_cleanup_payload(payload) -> bool:
        """Is empty retained cleanup payload."""
        if payload is None:
            return True
        if isinstance(payload, bytes):
            return len(payload) == 0
        return str(payload) == ""

    def record_calibration(self, device_id, family, leaf, body, *, topic, retain,
                           normalize_status, normalize_sample) -> bool:
        """Store routed calibration receipts using the supplied payload normalizers."""
        now = self._clock()
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
                    "status": normalize_status(
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
                normalized_sample = normalize_sample(
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
                normalized = normalize_status(
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

    def record_config_reply(self, device_id, leaf, payload, *, topic) -> None:
        """Store config ACK/results and release their correlated switch slot."""
        now = self._clock()
        with self._config_lock:
            if leaf == "ack":
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
                    if not payload.get("accepted", False):
                        self._clear_switch_config_command_by_message(message_id)
            elif leaf == "result":
                message_id = str(payload.get("message_id") or "").strip()
                if message_id:
                    result = {
                        "message_id": message_id,
                        "device_id": device_id,
                        "sensor_id": str(payload.get("sensor_id") or "").strip(),
                        "name": str(payload.get("name") or "").strip(),
                        "applied": bool(payload.get("applied", False)),
                        "persistence_mode": str(payload.get("persistence_mode") or ""),
                        "updated": payload.get("updated"),
                        "halt": payload.get("halt"),
                        "phase": payload.get("phase"),
                        "relays": payload.get("relays"),
                        "duplicate": bool(payload.get("duplicate", False)),
                        "error": str(payload.get("error") or "").strip(),
                        "topic": topic,
                        "received_at": now,
                    }
                    self.config_result_by_message[message_id] = result
                    self._clear_switch_config_command_by_message(message_id)

    def record_published(self, message_id, device_id, *, calibration=False) -> None:
        """Remember the target only after the MQTT client accepts publication."""
        lock = self._calibration_lock if calibration else self._config_lock
        targets = self.calibration_message_device if calibration else self.config_message_device
        with lock:
            targets[message_id] = device_id

    def record_meta_patch(self, message_id, patch) -> None:
        """Correlate an accepted, sanitized metadata patch after shadow application."""
        with self._meta_patch_lock:
            self.meta_patch_by_message[message_id] = dict(patch)

    def track_switch_request(self, switch_id, channel_label, channel_id, new_state,
                             *, event_origin=None, event_label=None) -> None:
        """Track the requested state without overwriting confirmed device state."""
        pending_key = (str(switch_id), str(channel_label))
        command_key = self._switch_config_command_key(switch_id, channel_id)
        command = self._switch_config_command_inflight.get(command_key) or {}
        message_id = str(command.get("message_id") or "")
        prior = self._pending_set.get(pending_key) or {}
        self._pending_set[pending_key] = {
            "ts": prior["ts"] if prior.get("message_id") == message_id and message_id else self._clock(),
            "message_id": message_id,
            "state": bool(new_state),
            "channel_id": str(channel_id or "").strip(),
            "event_origin": str(event_origin or "").strip().lower(),
            "event_label": str(event_label or "").strip(),
        }
