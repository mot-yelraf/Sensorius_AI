"""Enrollment token manager for onboarding V2.

Provides short-lived, single-use tokens with hash-at-rest storage and replay
protection through the onboarding session store.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Dict, Optional

from .saiUtils import debug_enabled, printDM

from .saiOnboardingStore import OnboardingSessionStore
from .saiSettings import saiSettings

MODULE = "saiOnboardingToken"
DEBUG = debug_enabled(MODULE)


class OnboardingTokenManager:
    """Issue, hash, validate, and expire onboarding authorization tokens."""

    def __init__(self, store: OnboardingSessionStore, default_ttl_sec: int = 600):
        self.store = store
        self.default_ttl_sec = max(60, int(default_ttl_sec or 600))

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def issue_token(self, *, session_id: str, expected_device_id: str = "", ttl_sec: Optional[int] = None) -> Dict[str, Any]:
        ttl = max(60, int(ttl_sec if ttl_sec is not None else self.default_ttl_sec))
        token = secrets.token_urlsafe(24)
        token_hash = self.hash_token(token)
        exp = time.time() + ttl
        token_secret = saiSettings.obfuscate_secret(token)
        session = self.store.create_session(
            session_id=session_id,
            onboard_token_hash=token_hash,
            onboard_token_secret=token_secret,
            token_expires_at=exp,
            expected_device_id=expected_device_id,
        )
        if DEBUG:
            printDM(f"Issued onboarding token for session {session_id}", location=MODULE)
        return {
            "token": token,
            "token_hash": token_hash,
            "expires_at": exp,
            "session": session,
        }

    def validate_for_session(self, *, session_id: str, token: str, device_id: str = "") -> tuple[bool, str]:
        session = self.store.get_session(session_id)
        if not session:
            return False, "session_not_found"

        now = time.time()
        if now > float(session.get("token_expires_at", 0.0) or 0.0):
            return False, "token_expired"

        if bool(session.get("token_consumed", False)):
            return False, "token_already_used"

        if self.hash_token(token) != str(session.get("onboard_token_hash", "")):
            return False, "token_mismatch"

        bound = str(session.get("expected_device_id", "") or "").strip()
        incoming = (device_id or "").strip()
        if bound and incoming and bound != incoming:
            return False, "device_mismatch"

        return True, "ok"

    def consume_for_session(self, *, session_id: str, token: str, device_id: str = "") -> tuple[bool, str, Optional[Dict[str, Any]]]:
        ok, reason = self.validate_for_session(session_id=session_id, token=token, device_id=device_id)
        if not ok:
            return False, reason, None

        updates = {
            "token_consumed": True,
        }
        if device_id:
            updates["device_id"] = device_id
        updated = self.store.update_session(session_id, **updates)
        return True, "ok", updated

    def invalidate_session_token(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.store.update_session(
            session_id,
            token_consumed=True,
            token_expires_at=time.time() - 1.0,
            onboard_token_secret="",
        )
