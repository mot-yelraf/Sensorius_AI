"""Test foreground shutdown helpers used by terminal-launched Sensorius."""

import asyncio
import os
import sys
from threading import Event
from types import SimpleNamespace

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
