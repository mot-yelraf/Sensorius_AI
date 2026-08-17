"""Test desktop webview geometry defaults and environment overrides.

The cases ensure platform-independent sizing decisions remain predictable
without opening an actual desktop window.
"""

import os
import sys
from types import ModuleType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiGuiLauncher as saiGuiLauncher
import sensorius.saiWebServer as saiWebServer


def test_native_desktop_icon_is_a_rendered_png():
    assert saiGuiLauncher.DESKTOP_ICON_PATH.is_file()
    assert saiGuiLauncher.DESKTOP_ICON_PATH.name == "sensorius-icon.png"
    native_svg = saiGuiLauncher.DESKTOP_ICON_PATH.with_name(
        "sensorius-native-icon.svg"
    ).read_text(encoding="utf-8")
    assert 'viewBox="-50 -50 612 612"' in native_svg


def test_macos_icon_uses_its_platform_specific_scale():
    assert saiWebServer.MACOS_ICON_PATH.is_file()
    macos_svg = saiWebServer.MACOS_ICON_PATH.with_suffix(".svg").read_text(
        encoding="utf-8"
    )
    assert 'viewBox="-51.5355 -51.5355 615.071 615.071"' in macos_svg


def test_linux_launcher_uses_standard_interruptible_gtk_start(monkeypatch):
    calls = []
    identity_calls = []
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: None,
        start=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(saiGuiLauncher.sys, "platform", "linux")
    monkeypatch.setattr(saiGuiLauncher, "_wait_for_health", lambda _url: True)
    monkeypatch.setattr(
        saiGuiLauncher,
        "configure_linux_app_identity",
        lambda: identity_calls.append(True),
    )
    monkeypatch.setenv("DISPLAY", ":0")

    assert saiGuiLauncher.main() == 0
    assert identity_calls == [True]
    assert calls == [((), {"gui": "gtk"})]


def test_linux_identity_installs_matching_desktop_entry(monkeypatch, tmp_path):
    identity_calls = []
    fake_glib = SimpleNamespace(
        set_prgname=lambda value: identity_calls.append(("prgname", value)),
        set_application_name=lambda value: identity_calls.append(("name", value)),
    )
    fake_gi = ModuleType("gi")
    fake_gi.__path__ = []
    fake_repository = ModuleType("gi.repository")
    fake_repository.GLib = fake_glib
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)
    monkeypatch.setattr(saiWebServer.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    desktop_path = saiWebServer.configure_linux_app_identity()

    assert identity_calls == [
        ("prgname", "ai.sensorius.Sensorius"),
        ("name", "Sensorius"),
    ]
    assert desktop_path == (
        tmp_path / "applications" / "ai.sensorius.Sensorius.desktop"
    )
    desktop_text = desktop_path.read_text(encoding="utf-8")
    assert "Name=Sensorius\n" in desktop_text
    assert f"Icon={saiWebServer.DESKTOP_ICON_PATH}\n" in desktop_text
    assert "StartupWMClass=ai.sensorius.Sensorius\n" in desktop_text
    assert (
        tmp_path
        / "icons"
        / "hicolor"
        / "512x512"
        / "apps"
        / "ai.sensorius.Sensorius.png"
    ).read_bytes() == saiWebServer.DESKTOP_ICON_PATH.read_bytes()


def test_macos_launcher_sets_native_app_icon(monkeypatch):
    native_icon_calls = []
    shown_handlers = []

    class FakeEvent:
        def __iadd__(self, handler):
            shown_handlers.append(handler)
            return self

    fake_window = SimpleNamespace(events=SimpleNamespace(shown=FakeEvent()))
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: fake_window,
        start=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(saiGuiLauncher.sys, "platform", "darwin")
    monkeypatch.setattr(saiGuiLauncher, "_wait_for_health", lambda _url: True)
    monkeypatch.setattr(
        saiGuiLauncher,
        "set_macos_app_icon",
        lambda: native_icon_calls.append(True),
    )

    assert saiGuiLauncher.main() == 0
    assert shown_handlers == [saiGuiLauncher.set_macos_app_icon]
    shown_handlers[0]()
    assert native_icon_calls == [True]


def test_window_geometry_defaults_keep_titlebar_visible(monkeypatch):
    for key in (
        "SENSORIUS_GUI_WIDTH",
        "SENSORIUS_GUI_HEIGHT",
        "SENSORIUS_GUI_X",
        "SENSORIUS_GUI_Y",
    ):
        monkeypatch.delenv(key, raising=False)

    assert saiGuiLauncher._window_geometry() == {
        "width": 1920,
        "height": 1000,
        "x": 0,
        "y": 48,
    }


def test_window_geometry_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SENSORIUS_GUI_WIDTH", "1280")
    monkeypatch.setenv("SENSORIUS_GUI_HEIGHT", "720")
    monkeypatch.setenv("SENSORIUS_GUI_X", "12")
    monkeypatch.setenv("SENSORIUS_GUI_Y", "64")

    assert saiGuiLauncher._window_geometry() == {
        "width": 1280,
        "height": 720,
        "x": 12,
        "y": 64,
    }


def test_window_geometry_ignores_invalid_overrides(monkeypatch):
    monkeypatch.setenv("SENSORIUS_GUI_WIDTH", "bad")
    monkeypatch.setenv("SENSORIUS_GUI_Y", "bad")

    geometry = saiGuiLauncher._window_geometry()

    assert geometry["width"] == 1920
    assert geometry["y"] == 48
