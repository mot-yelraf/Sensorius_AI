"""Test foreground shutdown helpers used by terminal-launched Sensorius."""

import asyncio
import os
import sys
from threading import Event
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius import app as sensorius_app


def test_linux_shutdown_quits_gtk_application_without_confirmation():
    calls = []
    application = SimpleNamespace(quit=lambda: calls.append("quit"))
    native_window = SimpleNamespace(get_application=lambda: application)
    window = SimpleNamespace(
        native=native_window,
        destroy=lambda: calls.append("destroy"),
    )

    sensorius_app._close_window_for_shutdown(window, is_linux=True)

    assert calls == ["quit"]


def test_shutdown_uses_webview_destroy_without_a_gtk_application():
    calls = []
    window = SimpleNamespace(
        native=None,
        destroy=lambda: calls.append("destroy"),
    )

    sensorius_app._close_window_for_shutdown(window, is_linux=True)

    assert calls == ["destroy"]


def test_shutdown_request_wakes_async_runtime():
    async def exercise():
        shutdown_requested = Event()
        waiter = asyncio.create_task(
            sensorius_app._wait_for_shutdown_request(shutdown_requested)
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        shutdown_requested.set()
        await asyncio.wait_for(waiter, timeout=1.0)

    asyncio.run(exercise())


def test_webview_exit_requests_backend_shutdown():
    shutdown_requested = Event()

    sensorius_app._request_shutdown_after_webview_exit(shutdown_requested)

    assert shutdown_requested.is_set()


def test_required_desktop_failure_stops_backend_and_releases_lock(monkeypatch):
    calls = []
    shutdown_events = []
    monkeypatch.setattr(sensorius_app, "relaunch_as_named_macos_app", lambda: False)
    monkeypatch.setattr(sensorius_app, "configure_logging", lambda: None)
    monkeypatch.setattr(sensorius_app, "saiSettings", lambda **kwargs: SimpleNamespace(
        get_setting=lambda *args: 8123,
    ))
    lock = SimpleNamespace(acquire=lambda: True, release=lambda: calls.append("release"))
    monkeypatch.setattr(sensorius_app, "SensoriusInstanceLock", lambda port: lock)
    def make_thread(*, target, args, daemon):
        shutdown_events.append(args[0])
        return SimpleNamespace(start=lambda: calls.append("start"), is_alive=lambda: False)
    monkeypatch.setattr(sensorius_app, "Thread", make_thread)
    async def fail_window(*, url, retries, delay):
        assert url == "http://127.0.0.1:8123/"
        return None
    monkeypatch.setattr(sensorius_app, "launch_webview", fail_window)
    monkeypatch.setenv("SENSORIUS_GUI", "1")
    monkeypatch.delenv("SENSORIUS_GUI_URL", raising=False)
    monkeypatch.delenv("SENSORIUS_HTTP_PORT", raising=False)
    with pytest.raises(RuntimeError, match="No Sensorius desktop window"):
        sensorius_app.run_application(require_gui=True)
    assert calls == ["start", "release"]
    assert shutdown_events[0].is_set()
