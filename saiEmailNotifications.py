"""SMTP email notifications for threshold crossings in sensor readings."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
import json
import math
import os
import smtplib
import socket
import threading
import time
from typing import Any

from saiUtils import debug_enabled, printDM


MODULE = "saiEmailNotifications"
DEBUG = debug_enabled(MODULE)
RULES_SECTION = "Notifications"
RULES_KEY = "RULES_JSON"
DEFAULT_REARM_COOLDOWN_SEC = 10 * 60
DEFAULT_FAILURE_COOLDOWN_SEC = 10 * 60
DEFAULT_HOURLY_CAP = 10
DEFAULT_DAILY_CAP = 40


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 100000) -> int:
    try:
        value = int(_env_text(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class EmailConfig:
    """Validated SMTP connection and message-address configuration."""

    enabled: bool
    smtp_host: str
    port: int
    security: str
    username: str
    app_password: str
    from_address: str
    to_addresses: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "EmailConfig":
        try:
            port = int(_env_text("SENSORIUS_EMAIL_SMTP_PORT", "465"))
        except Exception:
            port = 465
        recipients = tuple(
            item.strip()
            for item in _env_text("SENSORIUS_EMAIL_TO", "").replace(";", ",").split(",")
            if item.strip()
        )
        return cls(
            enabled=_env_bool("SENSORIUS_EMAIL_ENABLED", False),
            smtp_host=_env_text("SENSORIUS_EMAIL_SMTP_HOST", "smtp.gmail.com"),
            port=port,
            security=_env_text("SENSORIUS_EMAIL_SECURITY", "ssl").lower(),
            username=_env_text("SENSORIUS_EMAIL_USERNAME", ""),
            app_password=_env_text("SENSORIUS_EMAIL_APP_PASSWORD", "").replace(" ", ""),
            from_address=_env_text("SENSORIUS_EMAIL_FROM", "") or _env_text("SENSORIUS_EMAIL_USERNAME", ""),
            to_addresses=recipients,
        )

    def validation_error(self, *, require_enabled: bool = True) -> str:
        if require_enabled and not self.enabled:
            return "Email notifications are disabled."
        if not self.smtp_host:
            return "SMTP server is required."
        if not 1 <= self.port <= 65535:
            return "SMTP port must be between 1 and 65535."
        if self.security not in {"ssl", "starttls"}:
            return "Email security must be SSL/TLS or STARTTLS."
        if not self.username:
            return "Email username is required."
        if not self.app_password:
            return "Email app password is required."
        if not self.from_address or "@" not in self.from_address:
            return "A valid From address is required."
        if not self.to_addresses or any("@" not in item for item in self.to_addresses):
            return "At least one valid To address is required."
        return ""


@dataclass(frozen=True)
class NotificationRule:
    """One directional sensor metric threshold rule with a hysteresis band."""

    rule_id: str
    sensor_id: str
    metric: str
    operator: str
    threshold: float
    hysteresis: float
    enabled: bool = True
    notify_recovery: bool = True

    @property
    def trigger_boundary(self) -> float:
        if self.operator == "<":
            return self.threshold - self.hysteresis
        return self.threshold + self.hysteresis

    @property
    def recovery_boundary(self) -> float:
        if self.operator == "<":
            return self.threshold + self.hysteresis
        return self.threshold - self.hysteresis

    @property
    def recovery_operator(self) -> str:
        return ">" if self.operator == "<" else "<"

    @property
    def alert_label(self) -> str:
        return "LOW" if self.operator == "<" else "HIGH"

    def is_triggered(self, value: float) -> bool:
        if self.operator == "<":
            return value < self.trigger_boundary
        return value > self.trigger_boundary

    def is_recovered(self, value: float) -> bool:
        if self.operator == "<":
            return value > self.recovery_boundary
        return value < self.recovery_boundary


def normalize_notification_rules(raw: Any) -> list[dict[str, Any]]:
    """Return compact, validated rule dictionaries suitable for JSON storage."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id") or f"rule-{index + 1}").strip()[:80]
        sensor_id = str(item.get("sensor_id") or "").strip()[:160]
        metric = str(item.get("metric") or "").strip()[:160]
        operator = str(item.get("operator") or ">").strip()
        try:
            threshold = float(item.get("threshold"))
            hysteresis = float(item.get("hysteresis", 0))
        except Exception:
            continue
        if (
            not rule_id
            or rule_id in seen
            or not sensor_id
            or not metric
            or operator not in {">", "<"}
            or not math.isfinite(threshold)
            or not math.isfinite(hysteresis)
            or hysteresis < 0
        ):
            continue
        seen.add(rule_id)
        out.append({
            "id": rule_id,
            "sensor_id": sensor_id,
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "hysteresis": hysteresis,
            "enabled": bool(item.get("enabled", True)),
            "notify_recovery": bool(item.get("notify_recovery", True)),
        })
    return out


def parse_notification_rules(raw: Any) -> list[NotificationRule]:
    return [
        NotificationRule(
            rule_id=item["id"],
            sensor_id=item["sensor_id"],
            metric=item["metric"],
            operator=item["operator"],
            threshold=item["threshold"],
            hysteresis=item["hysteresis"],
            enabled=item["enabled"],
            notify_recovery=item["notify_recovery"],
        )
        for item in normalize_notification_rules(raw)
    ]


class SMTPEmailSender:
    """Send one message using the current Sensorius SMTP environment."""

    def send(
        self,
        subject: str,
        body: str,
        *,
        require_enabled: bool = True,
        config: EmailConfig | None = None,
    ) -> None:
        config = config or EmailConfig.from_environment()
        error = config.validation_error(require_enabled=require_enabled)
        if error:
            raise ValueError(error)

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.from_address
        message["To"] = ", ".join(config.to_addresses)
        message.set_content(body)

        if config.security == "ssl":
            with smtplib.SMTP_SSL(config.smtp_host, config.port, timeout=15) as smtp:
                smtp.login(config.username, config.app_password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(config.smtp_host, config.port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(config.username, config.app_password)
            smtp.send_message(message)


class EmailNotificationService:
    """Evaluate reading edges and deliver queued SMTP messages off the hot path."""

    def __init__(self, *, settings, data_logger, supervisor=None, sender=None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self.sender = sender or SMTPEmailSender()
        self._queue: deque[dict[str, Any]] = deque()
        self._queue_lock = threading.RLock()
        self._listener_registered = False
        self._active_by_rule = self._load_states()
        self._delivery_guards = self._load_delivery_guards()
        self._pending_by_rule: set[str] = set()
        self._sent_epochs_fallback: deque[float] = deque()
        self._last_error = ""

    def _load_states(self) -> dict[str, bool]:
        try:
            return dict(self.data_logger.get_notification_rule_states() or {})
        except Exception:
            return {}

    def _load_delivery_guards(self) -> dict[str, dict[str, float]]:
        try:
            return dict(self.data_logger.get_notification_delivery_guards() or {})
        except Exception:
            return {}

    def _rearm_cooldown_sec(self) -> int:
        return _env_int("SENSORIUS_EMAIL_REARM_COOLDOWN_SEC", DEFAULT_REARM_COOLDOWN_SEC)

    def _failure_cooldown_sec(self) -> int:
        return _env_int("SENSORIUS_EMAIL_FAILURE_COOLDOWN_SEC", DEFAULT_FAILURE_COOLDOWN_SEC)

    def _hourly_cap(self) -> int:
        return _env_int("SENSORIUS_EMAIL_MAX_PER_HOUR", DEFAULT_HOURLY_CAP)

    def _daily_cap(self) -> int:
        return _env_int("SENSORIUS_EMAIL_MAX_PER_DAY", DEFAULT_DAILY_CAP)

    def _rules(self) -> list[NotificationRule]:
        try:
            raw = self.settings.get_setting(
                RULES_SECTION,
                RULES_KEY,
                "[]",
                reload_if_changed=True,
            )
        except TypeError:
            raw = self.settings.get_setting(RULES_SECTION, RULES_KEY, "[]")
        except Exception:
            raw = "[]"
        return parse_notification_rules(raw)

    @staticmethod
    def _metric_value(values: dict, metric: str) -> float | None:
        target = metric.strip().lower()
        for key, raw in values.items():
            if str(key or "").strip().lower() != target:
                continue
            try:
                value = float(raw)
                return value if math.isfinite(value) else None
            except Exception:
                return None
        return None

    def _set_state(self, rule_id: str, active: bool, value: float, timestamp: str) -> None:
        self._active_by_rule[rule_id] = bool(active)
        try:
            self.data_logger.set_notification_rule_state(rule_id, active, value, timestamp)
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not persist rule state {rule_id}: {exc}", location=MODULE)

    def _record_delivery(self, rule_id: str, active: bool, sent_epoch: float) -> None:
        guard = self._delivery_guards.setdefault(rule_id, {})
        guard["failure_retry_after_epoch"] = 0.0
        if not active:
            guard["last_recovery_sent_epoch"] = float(sent_epoch)
        self._sent_epochs_fallback.append(float(sent_epoch))
        try:
            self.data_logger.record_notification_delivery(
                rule_id,
                "high" if active else "recovery",
                sent_epoch,
            )
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not persist notification delivery {rule_id}: {exc}", location=MODULE)

    def _set_failure_cooldown(self, rule_id: str, retry_after_epoch: float) -> None:
        guard = self._delivery_guards.setdefault(rule_id, {})
        guard["failure_retry_after_epoch"] = float(retry_after_epoch)
        try:
            self.data_logger.set_notification_failure_cooldown(rule_id, retry_after_epoch)
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not persist notification failure cooldown {rule_id}: {exc}", location=MODULE)

    def _record_recovery_without_email(self, rule_id: str, recovery_epoch: float) -> None:
        self._delivery_guards.setdefault(rule_id, {})["last_recovery_sent_epoch"] = float(recovery_epoch)
        try:
            self.data_logger.record_notification_recovery(rule_id, recovery_epoch)
        except Exception as exc:
            if DEBUG:
                printDM(f"Could not persist notification recovery {rule_id}: {exc}", location=MODULE)

    def _rate_limit(self, now_epoch: float) -> tuple[bool, float]:
        hourly_cap = self._hourly_cap()
        daily_cap = self._daily_cap()
        try:
            return self.data_logger.get_notification_rate_limit(
                now_epoch,
                hourly_cap=hourly_cap,
                daily_cap=daily_cap,
            )
        except Exception:
            pass

        day_start = float(now_epoch) - (24 * 60 * 60)
        while self._sent_epochs_fallback and self._sent_epochs_fallback[0] <= day_start:
            self._sent_epochs_fallback.popleft()
        daily = list(self._sent_epochs_fallback)
        hourly = [epoch for epoch in daily if epoch > float(now_epoch) - (60 * 60)]
        retry_at = 0.0
        if hourly_cap > 0 and len(hourly) >= hourly_cap:
            retry_at = max(retry_at, hourly[len(hourly) - hourly_cap] + (60 * 60))
        if daily_cap > 0 and len(daily) >= daily_cap:
            retry_at = max(retry_at, daily[len(daily) - daily_cap] + (24 * 60 * 60))
        return retry_at <= now_epoch, retry_at

    def _enqueue_transition(
        self,
        rule: NotificationRule,
        *,
        active: bool,
        value: float,
        timestamp: str,
    ) -> None:
        if rule.rule_id in self._pending_by_rule:
            return
        self._pending_by_rule.add(rule.rule_id)
        state_label = rule.alert_label if active else "RECOVERED"
        transition_operator = rule.operator if active else rule.recovery_operator
        boundary = rule.trigger_boundary if active else rule.recovery_boundary
        subject = (
            f"Sensorius {state_label}: {rule.metric} {transition_operator} "
            f"{boundary:g} (value {value:g})"
        )
        body = (
            f"Sensorius notification\n\n"
            f"State: {state_label}\n"
            f"Sensor: {rule.sensor_id}\n"
            f"Metric: {rule.metric}\n"
            f"Rule: {rule.metric} {rule.operator} {rule.threshold:g}\n"
            f"Value: {value:g}\n"
            f"Threshold: {rule.threshold:g}\n"
            f"Hysteresis: {rule.hysteresis:g}\n"
            f"Alert boundary: {rule.operator} {rule.trigger_boundary:g}\n"
            f"Recovery boundary: {rule.recovery_operator} {rule.recovery_boundary:g}\n"
            f"Reading time: {timestamp}\n"
            f"Hub: {socket.gethostname()}\n"
        )
        with self._queue_lock:
            self._queue.append({
                "rule_id": rule.rule_id,
                "active": bool(active),
                "value": value,
                "timestamp": timestamp,
                "subject": subject,
                "body": body,
                "attempts": 0,
            })

    def _on_readings_written(self, sensor_id: str, timestamp_iso: str, values: dict) -> None:
        if not EmailConfig.from_environment().enabled or not sensor_id or not isinstance(values, dict):
            return
        sid = str(sensor_id).strip().lower()
        for rule in self._rules():
            if not rule.enabled or rule.sensor_id.strip().lower() != sid:
                continue
            value = self._metric_value(values, rule.metric)
            if value is None:
                continue
            active = bool(self._active_by_rule.get(rule.rule_id, False))
            guard = self._delivery_guards.get(rule.rule_id, {})
            now_epoch = time.time()
            retry_after = float(guard.get("failure_retry_after_epoch", 0.0) or 0.0)
            if retry_after > now_epoch:
                continue
            if not active and rule.is_triggered(value):
                last_recovery = float(guard.get("last_recovery_sent_epoch", 0.0) or 0.0)
                if last_recovery and now_epoch < last_recovery + self._rearm_cooldown_sec():
                    continue
                self._enqueue_transition(rule, active=True, value=value, timestamp=str(timestamp_iso or ""))
            elif active and rule.is_recovered(value):
                if rule.notify_recovery:
                    self._enqueue_transition(rule, active=False, value=value, timestamp=str(timestamp_iso or ""))
                else:
                    self._set_state(rule.rule_id, False, value, str(timestamp_iso or ""))
                    self._record_recovery_without_email(rule.rule_id, now_epoch)

    def _register_listener_once(self) -> None:
        if self._listener_registered:
            return
        self.data_logger.add_readings_listener(self._on_readings_written)
        self._listener_registered = True

    def _pop(self) -> dict[str, Any] | None:
        with self._queue_lock:
            return self._queue.popleft() if self._queue else None

    async def send_test(self) -> None:
        await asyncio.to_thread(
            self.sender.send,
            "Sensorius test notification",
            f"Sensorius email notifications are configured on {socket.gethostname()}.",
            require_enabled=False,
        )

    async def run(self) -> None:
        self._register_listener_once()
        while True:
            if self.supervisor:
                self.supervisor.feedthedogs("Email Notifications")
            item = self._pop()
            if not item:
                await asyncio.sleep(1.0)
                continue
            rule_id = str(item["rule_id"])
            now_epoch = time.time()
            allowed, retry_at = self._rate_limit(now_epoch)
            if not allowed:
                with self._queue_lock:
                    self._queue.appendleft(item)
                await asyncio.sleep(min(30.0, max(1.0, retry_at - now_epoch)))
                continue
            try:
                await asyncio.to_thread(self.sender.send, item["subject"], item["body"])
                self._set_state(rule_id, bool(item["active"]), float(item["value"]), str(item["timestamp"]))
                self._record_delivery(rule_id, bool(item["active"]), time.time())
                self._last_error = ""
                self._pending_by_rule.discard(rule_id)
            except Exception as exc:
                self._last_error = str(exc)
                printDM(f"Email notification failed for rule {rule_id}: {exc}", location=MODULE, level="warning")
                attempts = int(item.get("attempts", 0)) + 1
                if attempts < 3 and EmailConfig.from_environment().enabled:
                    item["attempts"] = attempts
                    with self._queue_lock:
                        self._queue.appendleft(item)
                    await asyncio.sleep(float(2 ** attempts))
                else:
                    self._set_failure_cooldown(
                        rule_id,
                        time.time() + self._failure_cooldown_sec(),
                    )
                    self._pending_by_rule.discard(rule_id)


def email_test_sender() -> SMTPEmailSender:
    """Factory kept small so the web route can send a test without runtime wiring."""
    return SMTPEmailSender()
