"""Test desktop webview geometry defaults and environment overrides."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiGuiLauncher as saiGuiLauncher

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
