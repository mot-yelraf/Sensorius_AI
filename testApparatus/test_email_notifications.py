import json
from pathlib import Path

from saiEmailNotifications import (
    EmailConfig,
    EmailNotificationService,
    SMTPEmailSender,
    normalize_notification_rules,
)


class FakeSettings:
    def __init__(self, rules):
        self.rules = rules

    def get_setting(self, section, key, default=None, **_kwargs):
        if (section, key) == ("Notifications", "RULES_JSON"):
            return json.dumps(self.rules)
        return default


class FakeLogger:
    def __init__(self, states=None, guards=None, events=None):
        self.states = dict(states or {})
        self.guards = dict(guards or {})
        self.events = list(events or [])
        self.persisted = []
        self.listener = None

    def get_notification_rule_states(self):
        return dict(self.states)

    def set_notification_rule_state(self, rule_id, active, value, timestamp):
        self.states[rule_id] = bool(active)
        self.persisted.append((rule_id, bool(active), value, timestamp))

    def get_notification_delivery_guards(self):
        return dict(self.guards)

    def record_notification_delivery(self, rule_id, event_type, sent_epoch):
        guard = self.guards.setdefault(rule_id, {})
        guard["failure_retry_after_epoch"] = 0.0
        if event_type == "recovery":
            guard["last_recovery_sent_epoch"] = float(sent_epoch)
        self.events.append(float(sent_epoch))

    def set_notification_failure_cooldown(self, rule_id, retry_after_epoch):
        self.guards.setdefault(rule_id, {})["failure_retry_after_epoch"] = float(retry_after_epoch)

    def record_notification_recovery(self, rule_id, recovery_epoch):
        self.guards.setdefault(rule_id, {})["last_recovery_sent_epoch"] = float(recovery_epoch)

    def get_notification_rate_limit(self, now_epoch, *, hourly_cap, daily_cap):
        daily = sorted(epoch for epoch in self.events if epoch > now_epoch - 86400)
        hourly = [epoch for epoch in daily if epoch > now_epoch - 3600]
        retry_at = 0.0
        if hourly_cap > 0 and len(hourly) >= hourly_cap:
            retry_at = max(retry_at, hourly[len(hourly) - hourly_cap] + 3600)
        if daily_cap > 0 and len(daily) >= daily_cap:
            retry_at = max(retry_at, daily[len(daily) - daily_cap] + 86400)
        return retry_at <= now_epoch, retry_at

    def add_readings_listener(self, listener):
        self.listener = listener


def _rule(**overrides):
    rule = {
        "id": "temperature-high",
        "sensor_id": "sensor-1",
        "metric": "Temperature",
        "operator": ">",
        "threshold": 100,
        "hysteresis": 5,
        "enabled": True,
        "notify_recovery": True,
    }
    rule.update(overrides)
    return rule


def test_email_config_uses_gmail_environment(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    monkeypatch.setenv("SENSORIUS_EMAIL_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_SMTP_PORT", "465")
    monkeypatch.setenv("SENSORIUS_EMAIL_SECURITY", "ssl")
    monkeypatch.setenv("SENSORIUS_EMAIL_USERNAME", "sender@gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("SENSORIUS_EMAIL_FROM", "sender@gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_TO", "one@gmail.com, two@gmail.com")

    config = EmailConfig.from_environment()

    assert config.validation_error() == ""
    assert config.app_password == "abcdefghijklmnop"
    assert config.to_addresses == ("one@gmail.com", "two@gmail.com")


def test_rule_normalization_rejects_negative_hysteresis_and_invalid_numbers():
    valid = _rule()
    rules = normalize_notification_rules([
        valid,
        _rule(id="negative", hysteresis=-1),
        _rule(id="invalid", threshold="not-a-number"),
    ])

    assert rules == [valid]


def test_existing_rule_without_operator_defaults_to_greater_than():
    legacy = _rule()
    legacy.pop("operator")

    assert normalize_notification_rules([legacy])[0]["operator"] == ">"


def test_high_and_recovery_messages_are_edge_triggered(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    logger = FakeLogger()
    service = EmailNotificationService(settings=FakeSettings([_rule()]), data_logger=logger)

    service._on_readings_written("sensor-1", "t1", {"Temperature": 105})
    assert service._pop() is None

    service._on_readings_written("sensor-1", "t2", {"Temperature": 106})
    high = service._pop()
    assert high["active"] is True
    assert "HIGH" in high["subject"]

    service._on_readings_written("sensor-1", "t3", {"Temperature": 120})
    assert service._pop() is None

    service._pending_by_rule.discard("temperature-high")
    service._set_state("temperature-high", True, 106, "t2")
    service._on_readings_written("sensor-1", "t4", {"Temperature": 95})
    assert service._pop() is None

    service._on_readings_written("sensor-1", "t5", {"Temperature": 94})
    recovery = service._pop()
    assert recovery["active"] is False
    assert "RECOVERED" in recovery["subject"]


def test_greater_than_rule_email_describes_effective_boundaries(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    service = EmailNotificationService(
        settings=FakeSettings([_rule(threshold=39, hysteresis=1)]),
        data_logger=FakeLogger(),
    )

    service._on_readings_written("sensor-1", "2026-07-21T12:00:00-06:00", {"Temperature": 40.5})
    message = service._pop()

    assert message["subject"] == "Sensorius HIGH: Temperature > 40 (value 40.5)"
    assert "Rule: Temperature > 39" in message["body"]
    assert "Alert boundary: > 40" in message["body"]
    assert "Recovery boundary: < 38" in message["body"]


def test_less_than_rule_uses_mirrored_hysteresis(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    logger = FakeLogger()
    service = EmailNotificationService(
        settings=FakeSettings([_rule(operator="<", threshold=39, hysteresis=1)]),
        data_logger=logger,
    )

    service._on_readings_written("sensor-1", "t1", {"Temperature": 37.5})
    low = service._pop()
    assert low["subject"] == "Sensorius LOW: Temperature < 38 (value 37.5)"
    assert "Recovery boundary: > 40" in low["body"]

    service._pending_by_rule.discard("temperature-high")
    service._set_state("temperature-high", True, 37.5, "t1")
    service._on_readings_written("sensor-1", "t2", {"Temperature": 40.5})
    recovery = service._pop()
    assert recovery["subject"] == "Sensorius RECOVERED: Temperature > 40 (value 40.5)"


def test_persisted_active_state_prevents_restart_duplicate(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    logger = FakeLogger({"temperature-high": True})
    service = EmailNotificationService(settings=FakeSettings([_rule()]), data_logger=logger)

    service._on_readings_written("sensor-1", "t1", {"Temperature": 120})
    assert service._pop() is None

    service._on_readings_written("sensor-1", "t2", {"Temperature": 90})
    assert service._pop()["active"] is False


def test_recovery_can_reset_without_sending(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    monkeypatch.setattr("saiEmailNotifications.time.time", lambda: 1000.0)
    logger = FakeLogger({"temperature-high": True})
    service = EmailNotificationService(
        settings=FakeSettings([_rule(notify_recovery=False)]),
        data_logger=logger,
    )

    service._on_readings_written("sensor-1", "t1", {"Temperature": 90})

    assert service._pop() is None
    assert logger.states["temperature-high"] is False
    assert logger.guards["temperature-high"]["last_recovery_sent_epoch"] == 1000.0

    service._on_readings_written("sensor-1", "t2", {"Temperature": 120})
    assert service._pop() is None


def test_recovered_rule_waits_ten_minutes_before_new_high(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    monkeypatch.setenv("SENSORIUS_EMAIL_REARM_COOLDOWN_SEC", "600")
    now = [1500.0]
    monkeypatch.setattr("saiEmailNotifications.time.time", lambda: now[0])
    logger = FakeLogger(
        {"temperature-high": False},
        {"temperature-high": {"last_recovery_sent_epoch": 1000.0}},
    )
    service = EmailNotificationService(settings=FakeSettings([_rule()]), data_logger=logger)

    service._on_readings_written("sensor-1", "t1", {"Temperature": 120})
    assert service._pop() is None

    now[0] = 1601.0
    service._on_readings_written("sensor-1", "t2", {"Temperature": 120})
    assert service._pop()["active"] is True


def test_failure_circuit_blocks_rule_for_ten_minutes(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    now = [1500.0]
    monkeypatch.setattr("saiEmailNotifications.time.time", lambda: now[0])
    logger = FakeLogger(
        guards={"temperature-high": {"failure_retry_after_epoch": 1600.0}},
    )
    service = EmailNotificationService(settings=FakeSettings([_rule()]), data_logger=logger)

    service._on_readings_written("sensor-1", "t1", {"Temperature": 120})
    assert service._pop() is None

    now[0] = 1601.0
    service._on_readings_written("sensor-1", "t2", {"Temperature": 120})
    assert service._pop()["active"] is True


def test_global_caps_are_rolling_and_use_requested_defaults(monkeypatch):
    monkeypatch.delenv("SENSORIUS_EMAIL_MAX_PER_HOUR", raising=False)
    monkeypatch.delenv("SENSORIUS_EMAIL_MAX_PER_DAY", raising=False)
    now = 100000.0
    logger = FakeLogger(events=[now - 100 + index for index in range(10)])
    service = EmailNotificationService(settings=FakeSettings([_rule()]), data_logger=logger)

    allowed, retry_at = service._rate_limit(now)

    assert allowed is False
    assert retry_at == now - 100 + 3600
    assert service._hourly_cap() == 10
    assert service._daily_cap() == 40


def test_sqlite_persists_delivery_guards_and_rate_history(tmp_path):
    from saiDataLogger import saiDataLogger

    saiDataLogger._schema_ready = False
    logger = saiDataLogger(db_path=str(tmp_path / "notifications.db"))
    try:
        logger.set_notification_rule_state("rule-1", False, 90.0, "t1")
        logger.record_notification_delivery("rule-1", "recovery", 1000.0)
        logger.set_notification_failure_cooldown("rule-1", 1700.0)
        guards = logger.get_notification_delivery_guards()
        assert guards["rule-1"] == {
            "last_recovery_sent_epoch": 1000.0,
            "failure_retry_after_epoch": 1700.0,
        }

        for index in range(9):
            logger.record_notification_delivery(f"rule-{index + 2}", "high", 1001.0 + index)
        allowed, retry_at = logger.get_notification_rate_limit(
            1100.0,
            hourly_cap=10,
            daily_cap=40,
        )
        assert allowed is False
        assert retry_at == 4600.0
    finally:
        logger.close()
        saiDataLogger._schema_ready = False


def test_smtp_ssl_sender_logs_in_and_sends(monkeypatch):
    monkeypatch.setenv("SENSORIUS_EMAIL_ENABLED", "true")
    monkeypatch.setenv("SENSORIUS_EMAIL_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_SMTP_PORT", "465")
    monkeypatch.setenv("SENSORIUS_EMAIL_SECURITY", "ssl")
    monkeypatch.setenv("SENSORIUS_EMAIL_USERNAME", "sender@gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("SENSORIUS_EMAIL_FROM", "sender@gmail.com")
    monkeypatch.setenv("SENSORIUS_EMAIL_TO", "sender@gmail.com")
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["Subject"], message["To"]))

    monkeypatch.setattr("saiEmailNotifications.smtplib.SMTP_SSL", FakeSMTP)

    SMTPEmailSender().send("Test subject", "Test body")

    assert calls == [
        ("connect", "smtp.gmail.com", 465, 15),
        ("login", "sender@gmail.com", "abcdefghijklmnop"),
        ("send", "Test subject", "sender@gmail.com"),
    ]


def test_smtp_sender_accepts_unsaved_form_config(monkeypatch):
    monkeypatch.delenv("SENSORIUS_EMAIL_USERNAME", raising=False)
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, _message):
            calls.append(("send",))

    monkeypatch.setattr("saiEmailNotifications.smtplib.SMTP_SSL", FakeSMTP)
    config = EmailConfig(
        enabled=True,
        smtp_host="smtp.gmail.com",
        port=465,
        security="ssl",
        username="visible-form@gmail.com",
        app_password="abcdefghijklmnop",
        from_address="visible-form@gmail.com",
        to_addresses=("visible-form@gmail.com",),
    )

    SMTPEmailSender().send("Test", "Body", require_enabled=False, config=config)

    assert ("login", "visible-form@gmail.com", "abcdefghijklmnop") in calls


def test_notifications_ui_has_requested_email_grid_and_metric_rules():
    source = Path(__file__).resolve().parents[1] / "ui_templates" / "modals" / "system_settings.html"
    text = source.read_text(encoding="utf-8")

    email_start = text.index("<summary>Email Settings</summary>")
    rules_start = text.index("<summary>Notification Rules</summary>")
    assert email_start < rules_start
    assert text.index('id="email_smtp_host"', email_start) < text.index('id="email_smtp_port"', email_start)
    assert text.index('id="email_smtp_port"', email_start) < text.index('id="email_security"', email_start)
    assert text.index('id="email_username"', email_start) < text.index('id="email_app_password"', email_start)
    assert text.index('id="email_from"', email_start) < text.index('id="email_to"', email_start)
    assert text.index('id="email_to"', email_start) < text.index('id="email_enabled"', email_start)
    assert text.index('id="email_enabled"', email_start) < text.index('id="btn-email-test"', email_start)
    assert 'class="notification-enable-control"' in text
    assert 'class="email-grid-two email-action-grid"' in text
    assert 'class="notification-test-control"' in text
    assert 'app_password: document.getElementById("email_app_password")?.value || ""' in text
    assert 'id="email_app_password" name="email_app_password" value=""' in text
    assert 'id="btn-add-notification-rule"' in text
    assert 'class="notification-sensor"' in text
    assert 'class="notification-metric"' in text
    assert 'class="notification-operator"' in text
