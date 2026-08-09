"""Focused coverage for the Sensorius single-instance process lock."""

import sensorius.app as sensorius_app

from sensorius.saiInstanceLock import SensoriusInstanceLock


def test_instance_lock_rejects_duplicate_and_releases(tmp_path):
    first = SensoriusInstanceLock(8000, lock_dir=tmp_path)
    second = SensoriusInstanceLock(8000, lock_dir=tmp_path)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_instance_locks_are_independent_by_port(tmp_path):
    first = SensoriusInstanceLock(8000, lock_dir=tmp_path)
    second = SensoriusInstanceLock(8001, lock_dir=tmp_path)

    assert first.acquire() is True
    assert second.acquire() is True

    first.release()
    second.release()


def test_run_application_stops_before_backend_thread_when_lock_is_busy(monkeypatch):
    events = []
    monkeypatch.delenv("SENSORIUS_HTTP_PORT", raising=False)

    class _Settings:
        def __init__(self, **kwargs):
            events.append(("settings", kwargs))

        def get_setting(self, section, key, default=None):
            assert (section, key) == ("Network", "HTTPPORT")
            return 8765

    class _BusyLock:
        def __init__(self, port):
            events.append(("lock", port))

        def acquire(self):
            return False

        def release(self):
            events.append(("release", None))

    monkeypatch.setattr(sensorius_app, "configure_logging", lambda: events.append(("logging", None)))
    monkeypatch.setattr(sensorius_app, "saiSettings", _Settings)
    monkeypatch.setattr(sensorius_app, "SensoriusInstanceLock", _BusyLock)
    monkeypatch.setattr(
        sensorius_app,
        "Thread",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("backend thread must not start")),
    )

    sensorius_app.run_application()

    assert ("lock", 8765) in events
    assert ("release", None) in events
