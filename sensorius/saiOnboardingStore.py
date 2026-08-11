"""Durable onboarding session store for Add Device V2.

Persists onboarding sessions under:
  system_settings/<hub_hostname>/onboarding_sessions/<session_id>.json

This module is Sensorius-side only. It intentionally keeps runtime onboarding
state out of remote Nodus settings schemas.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .saiRuntimePaths import resolve_runtime_base_dir
from .saiUtils import debug_enabled, printDM

MODULE = "saiOnboardingStore"
DEBUG = debug_enabled(MODULE)


class OnboardingStates:
    """Define persisted lifecycle states for Nodus onboarding sessions."""

    AP_DISCOVERED = "AP_DISCOVERED"
    INIT_SENDING = "INIT_SENDING"
    INIT_SENT = "INIT_SENT"
    WAITING_REBOOT = "WAITING_REBOOT"
    WAITING_MQTT_HELLO = "WAITING_MQTT_HELLO"
    CONFIG_SENDING = "CONFIG_SENDING"
    WAITING_CONFIG_ACK = "WAITING_CONFIG_ACK"
    WAITING_CONFIG_RESULT = "WAITING_CONFIG_RESULT"
    ONLINE = "ONLINE"
    FAILED = "FAILED"


class OnboardingSessionStore:
    """Persist and coordinate onboarding session records on disk."""

    def __init__(self, base_dir: str = "system_settings"):
        hub_host = socket.gethostname().strip() or "sensorius"
        self._root = resolve_runtime_base_dir(base_dir) / hub_host / "onboarding_sessions"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _sanitize_session_id(session_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", (session_id or "").strip())

    def _path_for(self, session_id: str) -> Path:
        sid = self._sanitize_session_id(session_id)
        return self._root / f"{sid}.json"

    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _atomic_write_json(path: Path, doc: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(doc, indent=2, sort_keys=True, separators=(",", ": "))
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def create_session(
        self,
        *,
        session_id: str,
        onboard_token_hash: str,
        onboard_token_secret: str = "",
        token_expires_at: float,
        expected_device_id: str = "",
        state: str = OnboardingStates.AP_DISCOVERED,
    ) -> Dict[str, Any]:
        now = self._now_iso()
        doc: Dict[str, Any] = {
            "session_id": (session_id or "").strip(),
            "state": state,
            "expected_device_id": (expected_device_id or "").strip(),
            "device_id": "",
            "onboard_token_hash": (onboard_token_hash or "").strip(),
            "onboard_token_secret": (onboard_token_secret or "").strip(),
            "token_expires_at": float(token_expires_at),
            "token_consumed": False,
            "message_id": "",
            "retry_count": 0,
            "failure_reason": "",
            "created_at": now,
            "updated_at": now,
            "last_event_at": now,
        }
        with self._lock:
            self._atomic_write_json(self._path_for(session_id), doc)
        if DEBUG:
            printDM(f"Created onboarding session {session_id}", location=MODULE)
        return doc

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            printDM(f"Failed reading session {session_id}: {e}", location=MODULE)
            return None

    def list_sessions(self) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []
        try:
            for p in sorted(self._root.glob("*.json")):
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    continue
        except Exception as e:
            printDM(f"Failed listing sessions: {e}", location=MODULE)
        return out

    def list_active_sessions(self) -> list[Dict[str, Any]]:
        terminal = {OnboardingStates.ONLINE, OnboardingStates.FAILED}
        return [s for s in self.list_sessions() if str(s.get("state", "")).strip() not in terminal]

    def update_session(self, session_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self.get_session(session_id)
            if not doc:
                return None
            doc.update(fields)
            now = self._now_iso()
            doc["updated_at"] = now
            doc["last_event_at"] = now
            self._atomic_write_json(self._path_for(session_id), doc)
            return doc

    def set_state(self, session_id: str, state: str, *, failure_reason: str = "") -> Optional[Dict[str, Any]]:
        updates: Dict[str, Any] = {"state": (state or "").strip()}
        if failure_reason:
            updates["failure_reason"] = failure_reason
        return self.update_session(session_id, **updates)

    def set_message_id(self, session_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        return self.update_session(session_id, message_id=(message_id or "").strip())

    def set_device_id(self, session_id: str, device_id: str) -> Optional[Dict[str, Any]]:
        return self.update_session(session_id, device_id=(device_id or "").strip())

    def increment_retry(self, session_id: str) -> Optional[Dict[str, Any]]:
        doc = self.get_session(session_id)
        if not doc:
            return None
        current = int(doc.get("retry_count", 0) or 0)
        return self.update_session(session_id, retry_count=current + 1)

    def find_active_by_device_id(self, device_id: str) -> Optional[Dict[str, Any]]:
        wanted = (device_id or "").strip()
        if not wanted:
            return None
        for session in self.list_active_sessions():
            if (session.get("device_id") or "").strip() == wanted:
                return session
            if (session.get("expected_device_id") or "").strip() == wanted:
                return session
        return None

    def find_active_by_device_and_message(self, device_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        wanted_device = (device_id or "").strip()
        wanted_msg = (message_id or "").strip()
        if not wanted_device or not wanted_msg:
            return None
        for session in self.list_active_sessions():
            sid = (session.get("device_id") or "").strip()
            if sid != wanted_device:
                continue
            mid = (session.get("message_id") or "").strip()
            if mid == wanted_msg:
                return session
        return None
