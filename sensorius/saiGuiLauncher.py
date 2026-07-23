"""Launch the Sensorius desktop webview against an existing backend service."""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_WINDOW_X = 0
DEFAULT_WINDOW_Y = 48
DEFAULT_WINDOW_WIDTH = 1920
DEFAULT_WINDOW_HEIGHT = 1000


def _base_url() -> str:
    configured = os.environ.get("SENSORIUS_GUI_URL")
    if configured:
        return configured.rstrip("/") + "/"
    port = os.environ.get("SENSORIUS_HTTP_PORT", "8000")
    return f"http://127.0.0.1:{port}/"


def _int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _window_geometry() -> dict[str, int]:
    return {
        "width": _int_env("SENSORIUS_GUI_WIDTH", DEFAULT_WINDOW_WIDTH),
        "height": _int_env("SENSORIUS_GUI_HEIGHT", DEFAULT_WINDOW_HEIGHT),
        "x": _int_env("SENSORIUS_GUI_X", DEFAULT_WINDOW_X),
        "y": _int_env("SENSORIUS_GUI_Y", DEFAULT_WINDOW_Y),
    }


def _wait_for_health(base_url: str) -> bool:
    health_url = base_url.rstrip("/") + "/healthz"
    retries = int(os.environ.get("SENSORIUS_GUI_RETRIES", "60"))
    delay_sec = float(os.environ.get("SENSORIUS_GUI_RETRY_DELAY", "2.0"))

    for _ in range(retries):
        try:
            with urllib.request.urlopen(health_url, timeout=3.0) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(delay_sec)
    return False


def main() -> int:
    base_url = _base_url()
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

    if sys.platform.startswith("linux"):
        os.environ.setdefault("GDK_BACKEND", "wayland,x11")
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            print("Sensorius GUI not started: no DISPLAY or WAYLAND_DISPLAY is set.")
            return 1

    try:
        import webview
    except Exception as exc:
        print(f"Sensorius GUI not started: pywebview import failed: {exc}")
        return 1

    if not _wait_for_health(base_url):
        print(f"Sensorius GUI not started: {base_url.rstrip('/')} did not become ready.")
        return 1

    geometry = _window_geometry()
    webview.create_window(
        "Sensorius Automatio Instrumentorum",
        base_url,
        width=geometry["width"],
        height=geometry["height"],
        x=geometry["x"],
        y=geometry["y"],
        resizable=True,
        frameless=False,
        confirm_close=True,
    )
    if sys.platform.startswith("linux"):
        webview.start(gui="gtk")
    else:
        webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
