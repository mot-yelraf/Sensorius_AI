"""Verify opt-in native diagnostics without opening windows or changing renderers.

Controlled clocks exercise input validation, GTK timing, file recording and
cleanup. The operator's real wheel/scrollbar run remains the hardware test.
"""

import json
from types import SimpleNamespace

import pytest

from sensorius import saiNativeScrollProfile as profile


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def window():
    exposed = []
    return SimpleNamespace(events=SimpleNamespace(shown=Event(), loaded=Event(), closed=Event()),
                           expose=lambda fn: exposed.append(fn), exposed=exposed)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for key in ("SENSORIUS_GUI_SCROLL_PROFILE", "SENSORIUS_GUI_SCROLL_PROFILE_PATH", "SENSORIUS_GUI_SCROLL_PROFILE_SECONDS"):
        monkeypatch.delenv(key, raising=False)


def test_disabled_diagnostic_does_not_touch_window():
    assert profile.attach_native_scroll_profile(object()) is None


def test_enabled_diagnostic_attaches_once_to_existing_window(tmp_path, monkeypatch):
    monkeypatch.setattr(profile.sys, "platform", "linux")
    monkeypatch.setenv("SENSORIUS_GUI_SCROLL_PROFILE", "1")
    monkeypatch.setenv("SENSORIUS_GUI_SCROLL_PROFILE_PATH", str(tmp_path / "native.jsonl"))
    target = window()
    recorder = profile.attach_native_scroll_profile(target)
    assert recorder.window is target
    assert target.exposed == [recorder.receive_native_scroll_sample]
    assert target.events.loaded.handlers == [recorder.loaded]
    assert target.events.shown.handlers == [recorder.shown]
    assert target.events.closed.handlers == [recorder.stop]
    assert profile.attach_native_scroll_profile(target) is recorder
    assert len(target.exposed) == 1
    assert not recorder.path.exists()


def test_invalid_relative_log_path_does_not_disable_the_app(monkeypatch):
    monkeypatch.setattr(profile.sys, "platform", "linux")
    monkeypatch.setenv("SENSORIUS_GUI_SCROLL_PROFILE", "1")
    monkeypatch.setenv("SENSORIUS_GUI_SCROLL_PROFILE_PATH", "native.jsonl")
    target = window()
    assert profile.attach_native_scroll_profile(target) is None
    assert target.exposed == []


def test_browser_snapshots_are_validated_and_not_recounted(tmp_path, monkeypatch):
    monkeypatch.setattr(profile.time, "monotonic", lambda: 102)
    recorder = profile.NativeScrollRecorder(window(), tmp_path / "native.jsonl", 120, "")
    recorder.started = 100
    assert recorder.receive_native_scroll_sample({"wheel_events": 3, "hover_ms": float("nan"), "scroll_delta": True, "visibility": {}, "secret": "not logged"})
    sample = recorder.snapshot()
    assert sample["javascript"]["wheel_events"] == 3
    assert "hover_ms" not in sample["javascript"]
    assert "scroll_delta" not in sample["javascript"]
    assert "secret" not in sample["javascript"]
    assert sample["javascript"]["visibility"] == "unknown"
    assert recorder.snapshot()["javascript"] is None
    recorder.stop()
    assert not recorder.receive_native_scroll_sample({"wheel_events": 99})


def test_native_heartbeat_and_paint_gaps_are_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(profile.time, "monotonic", lambda: 102)
    recorder = profile.NativeScrollRecorder(window(), tmp_path / "native.jsonl", 120, "")
    recorder.started = 100
    recorder.last_heartbeat = 101
    assert recorder._heartbeat()
    recorder.last_paint = 1900
    clock = SimpleNamespace(get_frame_time=lambda: 2000000, get_frame_counter=lambda: 2, get_timings=lambda _: None)
    recorder._after_paint(clock)
    sample = recorder.snapshot()
    assert sample["gtk_heartbeat"]["max_ms"] == 1000
    assert sample["gtk_paint"]["max_ms"] == 100
    assert sample["gtk_presentation"]["p95_ms"] is None
    assert not sample["presentation_available"]
    assert sample["gtk_heartbeat_age_seconds"] == 0
    assert sample["gtk_paint_age_seconds"] == 0
    assert recorder.snapshot()["gtk_paint"]["count"] == 0


def test_stop_removes_timing_sources_without_closing_window(tmp_path):
    calls = []
    recorder = profile.NativeScrollRecorder(window(), tmp_path / "native.jsonl", 120, "")
    recorder.clock = SimpleNamespace(disconnect=lambda value: calls.append(("disconnect", value)))
    recorder.clock_handler, recorder.heartbeat_source = 7, 8
    recorder.glib = SimpleNamespace(idle_add=lambda fn: fn(), source_remove=lambda value: calls.append(("source_remove", value)))
    recorder.stop()
    recorder.stop()
    assert calls == [("disconnect", 7), ("source_remove", 8)]
    assert not recorder._heartbeat()


def test_writer_records_version_and_timing_without_changing_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(profile.time, "monotonic", lambda: 102)
    monkeypatch.setattr(profile, "version", lambda _: "5.4")
    monkeypatch.setenv("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    recorder = profile.NativeScrollRecorder(window(), tmp_path / "native.jsonl", 120, "")
    recorder.started = 100
    waits = iter((False, True))
    monkeypatch.setattr(recorder.stopped, "wait", lambda _: next(waits))
    recorder._write()
    rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert rows[0]["pywebview_version"] == "5.4"
    assert rows[0]["render_environment"]["WEBKIT_DISABLE_COMPOSITING_MODE"] == "1"
    assert rows[1]["elapsed_seconds"] == 2
    assert rows[-1]["kind"] == "finished"
    assert recorder.stopped.is_set()
