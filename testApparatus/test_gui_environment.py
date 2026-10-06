"""Verify rendering defaults are scoped to Pi/Trixie and preserve overrides."""

import pytest

from sensorius import saiGuiEnvironment as gui


@pytest.fixture(autouse=True)
def isolated_rendering_environment(monkeypatch):
    for name in ("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "WEBKIT_DISABLE_DMABUF_RENDERER",
                 "WEBKIT_DISABLE_COMPOSITING_MODE", "GDK_BACKEND"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("model,codename,software", [
    ("Raspberry Pi 5 Model B", "trixie", True),
    ("Raspberry Pi 4 Model B", "trixie", True),
    ("Raspberry Pi 5 Model B", "bookworm", False),
    ("Other computer", "trixie", False),
])
def test_renderer_defaults(monkeypatch, model, codename, software):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    monkeypatch.setattr(gui.Path, "read_text", lambda self: model)
    monkeypatch.setattr(gui.platform, "freedesktop_os_release", lambda: {"VERSION_CODENAME": codename})
    for name in ("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "WEBKIT_DISABLE_DMABUF_RENDERER"):
        monkeypatch.delenv(name, raising=False)
    gui.configure_gui_environment()
    for name in ("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "WEBKIT_DISABLE_DMABUF_RENDERER"):
        assert gui.os.environ.get(name) == ("1" if software else None)


def test_renderer_preserves_explicit_overrides(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    monkeypatch.setattr(gui.Path, "read_text", lambda self: "Raspberry Pi 5")
    monkeypatch.setattr(gui.platform, "freedesktop_os_release", lambda: {"VERSION_CODENAME": "trixie"})
    for name in ("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "WEBKIT_DISABLE_DMABUF_RENDERER", "WEBKIT_DISABLE_COMPOSITING_MODE"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("GDK_BACKEND", "x11")
    gui.configure_gui_environment()
    assert gui.os.environ["WEBKIT_SKIA_ENABLE_CPU_RENDERING"] == "0"
    assert gui.os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] == "0"
    assert gui.os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] == "0"
    assert gui.os.environ["GDK_BACKEND"] == "x11"


def test_missing_pi_model_is_supported(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    def missing(self):
        raise FileNotFoundError
    monkeypatch.setattr(gui.Path, "read_text", missing)
    monkeypatch.delenv("WEBKIT_SKIA_ENABLE_CPU_RENDERING", raising=False)
    gui.configure_gui_environment()
    assert "WEBKIT_SKIA_ENABLE_CPU_RENDERING" not in gui.os.environ


def test_non_linux_does_not_probe_pi(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.Path, "read_text", lambda self: pytest.fail("unexpected Pi probe"))
    gui.configure_gui_environment()
