"""Record real scrolling in an opt-in, existing GTK pywebview window.

The diagnostic observes user input and JavaScript/GTK timing without moving
the page, changing render flags, or creating another window. Logging runs in
a bounded daemon worker so a stalled GUI cannot block the hub runtime.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

from . import __version__
from .project_paths import PROJECT_ROOT
from .saiUtils import printDM

SCRIPT_PATH = PROJECT_ROOT / "ui_static" / "js" / "native_scroll_profile.js"
RENDER_KEYS = ("GDK_BACKEND", "WEBKIT_DISABLE_COMPOSITING_MODE", "WEBKIT_SKIA_ENABLE_CPU_RENDERING", "WEBKIT_DISABLE_DMABUF_RENDERER")
NUMERIC_FIELDS = frozenset(("browser_ms", "scroll_top", "scroll_delta", "document_height", "viewport_width", "viewport_height", "frame_count", "raf_p50_ms", "raf_p95_ms", "raf_max_ms", "frames_over_50ms", "pending_micrographs", "chart_count", "wheel_events", "wheel_delta_y", "scroll_events", "pointer_moves", "hover_calls", "hover_ms"))


def _stats(values):
    ordered = sorted(values)
    return {"count": len(ordered), "p95_ms": ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else None, "max_ms": ordered[-1] if ordered else None}


class NativeScrollRecorder:
    """Collect input and frame timing from one existing native window."""

    def __init__(self, window, path: Path, seconds: int, script: str):
        self.window, self.path, self.seconds, self.script = window, path, seconds, script
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.started = None
        self.latest = None
        self.received = None
        self.sequence = self.written_sequence = 0
        self.glib = self.clock = None
        self.clock_handler = self.heartbeat_source = None
        self.last_heartbeat = self.last_paint = self.last_presentation = self.last_paint_seen = None
        self.heartbeats, self.paints, self.presentations = (deque(maxlen=600) for _ in range(3))
        self.native_status = "waiting_for_gtk"
        self.webkit_version = None
        self.presentation_available = False

    def shown(self):
        """Schedule GTK timing hooks on the existing GUI main loop."""
        try:
            from gi.repository import GLib
            self.glib = GLib
            GLib.idle_add(self._connect_gtk)
        except Exception as exc:
            self.native_status = f"GTK timing unavailable: {exc}"

    def loaded(self):
        """Inject passive listeners after the actual page loads."""
        with self.lock:
            first = self.started is None
            if first:
                self.started = time.monotonic()
        if first:
            threading.Thread(target=self._write, name="native-scroll-log", daemon=True).start()
        threading.Thread(target=self._inject, name="native-scroll-inject", daemon=True).start()

    def receive_native_scroll_sample(self, sample):
        """Accept bounded timing fields from this diagnostic's JavaScript bridge."""
        if self.stopped.is_set() or not isinstance(sample, dict):
            return False
        row = {key: value for key, value in sample.items() if key in NUMERIC_FIELDS and
               (value is None or isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))}
        visibility = sample.get("visibility")
        row["visibility"] = visibility if isinstance(visibility, str) and visibility in {"visible", "hidden"} else "unknown"
        row["focused"] = sample.get("focused") is True
        with self.lock:
            self.latest, self.received = row, time.monotonic()
            self.sequence += 1
        return True

    def stop(self, *args):
        """Stop collection without closing the window or changing the hub."""
        if self.stopped.is_set():
            return
        self.stopped.set()
        if self.glib:
            self.glib.idle_add(self._disconnect_gtk)

    def _inject(self):
        try:
            if self.stopped.is_set():
                return
            remaining = max(1, self.seconds - (time.monotonic() - self.started))
            self.window.evaluate_js(f"window.__sensNativeScrollDurationMs={int(remaining * 1000)};\n" + self.script)
        except Exception as exc:
            printDM(f"Native scroll listener could not attach: {exc}", location=__name__, level="warning")

    def _connect_gtk(self):
        if self.stopped.is_set() or self.heartbeat_source is not None:
            return False
        try:
            self.clock = self.window.native.get_frame_clock()
            if self.clock:
                self.clock_handler = self.clock.connect("after-paint", self._after_paint)
            self.last_heartbeat = time.monotonic()
            self.heartbeat_source = self.glib.timeout_add(100, self._heartbeat)
            self.native_status = "connected" if self.clock else "GTK heartbeat only; frame clock unavailable"
            try:
                from gi.repository import WebKit2
                self.webkit_version = ".".join(str(getattr(WebKit2, f"get_{part}_version")()) for part in ("major", "minor", "micro"))
            except (ImportError, AttributeError):
                self.webkit_version = None
        except Exception as exc:
            self.native_status = f"GTK timing unavailable: {exc}"
        return False

    def _heartbeat(self):
        if self.stopped.is_set():
            self.heartbeat_source = None
            return False
        now = time.monotonic()
        with self.lock:
            self.heartbeats.append((now - self.last_heartbeat) * 1000)
            self.last_heartbeat = now
        return True

    def _after_paint(self, clock):
        if self.stopped.is_set():
            return
        now = clock.get_frame_time() / 1000
        with self.lock:
            self.last_paint_seen = time.monotonic()
            if self.last_paint is not None:
                self.paints.append(now - self.last_paint)
            self.last_paint = now
            try:
                timing = clock.get_timings(clock.get_frame_counter() - 1)
                presented = timing.get_presentation_time() / 1000 if timing and timing.get_complete() else 0
            except Exception:
                presented = 0
            if presented > 0:
                self.presentation_available = True
                if self.last_presentation is not None and presented > self.last_presentation:
                    self.presentations.append(presented - self.last_presentation)
                self.last_presentation = presented

    def _disconnect_gtk(self):
        if self.clock and self.clock_handler is not None:
            self.clock.disconnect(self.clock_handler)
            self.clock_handler = None
        if self.heartbeat_source is not None:
            self.glib.source_remove(self.heartbeat_source)
            self.heartbeat_source = None
        return False

    def snapshot(self):
        """Drain bounded GTK counters while retaining the latest browser snapshot."""
        now = time.monotonic()
        with self.lock:
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(now - self.started, 3) if self.started is not None else 0,
                "javascript_age_seconds": round(now - self.received, 3) if self.received else None,
                "javascript_sequence": self.sequence,
                "javascript": self.latest if self.sequence != self.written_sequence else None,
                "gtk_status": self.native_status,
                "gtk_heartbeat_age_seconds": round(now - self.last_heartbeat, 3) if self.last_heartbeat is not None else None,
                "gtk_paint_age_seconds": round(now - self.last_paint_seen, 3) if self.last_paint_seen is not None else None,
                "webkitgtk_version": self.webkit_version,
                "presentation_available": self.presentation_available,
                "gtk_heartbeat": _stats(self.heartbeats),
                "gtk_paint": _stats(self.paints),
                "gtk_presentation": _stats(self.presentations),
            }
            self.heartbeats.clear()
            self.paints.clear()
            self.presentations.clear()
            self.written_sequence = self.sequence
        return row

    def _write(self):
        try:
            try:
                webview_version = version("pywebview")
            except PackageNotFoundError:
                webview_version = "unknown"
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "session", "timestamp": datetime.now(timezone.utc).isoformat(), "host": socket.gethostname(), "app_version": __version__, "pywebview_version": webview_version, "pid": os.getpid(), "seconds": self.seconds, "render_environment": {key: os.environ.get(key) for key in RENDER_KEYS}}) + "\n")
                stream.flush()
                printDM(f"Native scroll recording started: {self.path} ({self.seconds}s)", location=__name__, level="info")
                while not self.stopped.wait(1) and time.monotonic() - self.started < self.seconds:
                    stream.write(json.dumps(self.snapshot(), allow_nan=False) + "\n")
                    stream.flush()
                stream.write(json.dumps({"kind": "finished", "elapsed_seconds": round(time.monotonic() - self.started, 3)}) + "\n")
        except Exception as exc:
            printDM(f"Native scroll recording failed: {exc}", location=__name__, level="warning")
        finally:
            self.stop()
            printDM(f"Native scroll recording finished: {self.path}", location=__name__, level="info")


def attach_native_scroll_profile(window):
    """Attach an explicitly enabled GTK recorder to the existing app window."""
    if os.environ.get("SENSORIUS_GUI_SCROLL_PROFILE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    if not sys.platform.startswith("linux") or window is None:
        printDM("Native scroll diagnostics require the existing Linux GTK window.", location=__name__, level="warning")
        return None
    if getattr(window, "_sensorius_scroll_recorder", None) is not None:
        return window._sensorius_scroll_recorder
    try:
        path = Path(os.environ.get("SENSORIUS_GUI_SCROLL_PROFILE_PATH") or str(Path(tempfile.gettempdir()) / f"sensorius-native-scroll-{os.getpid()}.jsonl")).expanduser()
        if not path.is_absolute():
            raise ValueError("SENSORIUS_GUI_SCROLL_PROFILE_PATH must be an absolute path")
        seconds = max(15, min(600, int(os.environ.get("SENSORIUS_GUI_SCROLL_PROFILE_SECONDS", "120"))))
        recorder = NativeScrollRecorder(window, path, seconds, SCRIPT_PATH.read_text(encoding="utf-8"))
        window.expose(recorder.receive_native_scroll_sample)
        window.events.shown += recorder.shown
        window.events.loaded += recorder.loaded
        window.events.closed += recorder.stop
        window._sensorius_scroll_recorder = recorder
        return recorder
    except Exception as exc:
        printDM(f"Native scroll diagnostics could not be enabled: {exc}", location=__name__, level="warning")
        return None
