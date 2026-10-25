"""Track Nodus MQTT liveness independently of transport and discovery.

This component owns receipt timestamps, availability, and heartbeat timing rules.
Ingest supplies resolved device identities and retains MQTT subscriptions, message
ordering, locking, persistence, and callbacks. No network work is performed here.
"""

import time
from collections.abc import Callable

from .saiUtils import normalize_hostname_base

DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_HEARTBEAT_INTERVAL_S = 10.0
HEARTBEAT_STALE_AFTER_S = 90.0
HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 15.0


class NodusLiveness:
    """Own liveness state and derive snapshots from already-resolved identities.

    Callers retain their existing synchronization and update ordering. The maps
    remain mutable so ingest diagnostics and device removal share the same state.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None):
        """Create empty liveness state with an optional receipt-time clock."""
        self._clock = clock if clock is not None else lambda: time.time()
        self.device_status: dict[str, str] = {}
        self.last_mqtt_seen: dict[str, float] = {}
        self.retained_mqtt_seen: dict[str, float] = {}
        self.last_nodus_report_seen: dict[str, float] = {}
        self.nodus_availability: dict[str, str] = {}
        self.last_heartbeat_ts: dict[str, float] = {}
        self.heartbeat_interval_s_by_host: dict[str, float] = {}
        self.heartbeat_stale: dict[str, bool] = {}

    @staticmethod
    def normalize_state(status: str | None) -> str:
        """Normalize status text while retaining legacy migration states."""
        s = str(status or "").strip().lower()
        if s in {"online", "degraded", "offline", "unknown", "pending", "migration_required"}:
            if s == "pending":
                return "unknown"
            return s
        return "unknown"

    def record_seen(
        self,
        key: str | None,
        *,
        ts: float | None = None,
        retain: bool = False,
        report: bool = False,
    ) -> None:
        """Record a receipt, separating retained replay from live reports."""
        key_text = str(key or "").strip()
        if not key_text:
            return
        now_v = float(ts if ts is not None else self._clock())
        target = self.retained_mqtt_seen if retain else self.last_mqtt_seen
        target[key_text] = now_v
        if not retain and report:
            self.last_nodus_report_seen[key_text] = now_v

    def latest_seen(self, keys, source: dict[str, float] | None = None) -> float:
        """Return the newest timestamp among the supplied identity aliases."""
        source = source or {}
        latest = 0.0
        for key in keys or []:
            try:
                latest = max(latest, float(source.get(str(key or "").strip(), 0.0) or 0.0))
            except Exception:
                continue
        return latest

    def heartbeat_liveness_timestamp(
        self,
        hb_ts: float | None,
        *,
        retain: bool,
        now_ts: float,
        interval_s: float,
    ) -> float:
        """Use receipt time for a live heartbeat with an old device clock."""
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

    def derive_heartbeat_interval_s(self, data: dict | None) -> float:
        """Read the advertised heartbeat interval with existing bounds."""
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

    def extract_heartbeat_timestamp(self, data: dict | None) -> float | None:
        """Read a numeric heartbeat timestamp, if present."""
        if not isinstance(data, dict):
            return None
        raw_ts = data.get("timestamp", None)
        if raw_ts is None:
            return None
        try:
            return float(raw_ts)
        except Exception:
            return None

    def heartbeat_is_stale(self, ts_epoch: float | None, *, retain: bool, now_ts: float | None = None) -> bool:
        """
        Freshness rule:
        - retained heartbeat older than HEARTBEAT_STALE_AFTER_S is stale
        - heartbeat timestamp beyond skew tolerance into the future is stale
        """
        if ts_epoch is None:
            return bool(retain)
        now_v = float(now_ts if now_ts is not None else self._clock())
        if ts_epoch > (now_v + HEARTBEAT_CLOCK_SKEW_TOLERANCE_S):
            return True
        if retain and (now_v - ts_epoch) > HEARTBEAT_STALE_AFTER_S:
            return True
        return False

    def heartbeat_timeout_state(self, base: str, now_ts: float | None = None) -> str:
        """
        Derive online/degraded/offline from last heartbeat timing:
          online   <= 2 intervals
          degraded > 2 and < 3 intervals
          offline  >= 3 intervals
        """
        base = normalize_hostname_base(base) or ""
        if not base:
            return "unknown"
        now_v = float(now_ts if now_ts is not None else self._clock())
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
                stored = self.normalize_state(self.device_status.get(base))
                return stored if stored in {"offline", "degraded"} else "unknown"
            if self.get_availability(base) == "offline":
                return "offline"
            missed = max(0.0, now_v - last_seen)

        if missed <= (2.0 * interval):
            return "online"
        if missed < (3.0 * interval):
            return "degraded"
        return "offline"

    def get_availability(self, host_like: str | None) -> str | None:
        """Read explicit availability using canonical hostname aliases."""
        base = normalize_hostname_base(host_like)
        if not base:
            return None
        return self.nodus_availability.get(base) or self.nodus_availability.get(f"{base}.local")

    def snapshot(
        self,
        device_id: str | None,
        *,
        base: str,
        keys: list[str],
        availability_keys: list[str] | None = None,
        peer_ids=(),
        now_ts: float | None = None,
    ) -> dict:
        """Return canonical liveness without resolving identities or performing I/O.

        Offline availability is authoritative. Online availability alone is a
        hint; fresh heartbeat or live report traffic establishes online state.
        """
        dev = str(device_id or "").strip()
        now_v = float(now_ts if now_ts is not None else self._clock())
        state = "unknown"
        reason = "no_recent_mqtt"
        availability = None
        try:
            for key in availability_keys if availability_keys is not None else keys:
                availability = self.get_availability(key)
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

        last_seen = self.latest_seen(keys, self.last_mqtt_seen)
        retained_seen = self.latest_seen(keys, self.retained_mqtt_seen)
        report_seen = self.latest_seen(keys, self.last_nodus_report_seen)
        heartbeat_seen = self.latest_seen(keys, self.last_heartbeat_ts)
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
            state = self.heartbeat_timeout_state(base or dev, now_ts=now_v)
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
            stored = self.normalize_state(
                self.device_status.get(base)
                or self.device_status.get(f"{base}.local")
                or self.device_status.get(dev)
            )
            if stored in {"offline", "degraded"}:
                state = stored
                reason = "stored_status"

        peers = list(peer_ids or [])
        if dev and dev not in peers:
            peers.append(dev)

        return {
            "device_id": dev or base,
            "host": base,
            "state": self.normalize_state(state),
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

    def parse_availability_payload(self, payload_text: str, data: dict | None) -> str | None:
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
