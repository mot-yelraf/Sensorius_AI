"""Test custom theme persistence, image processing, and safe resolution.

The custom-theme registry is runtime state, so every test uses a temporary
Sensorius root and never touches host configuration or uploaded assets.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from sensorius.saiThemeManager import (
    THEME_PALETTES,
    ThemeManager,
    ThemeValidationError,
    is_custom_theme_selection,
    normalize_theme_selection,
)


def _image_bytes(*, size=(640, 480), image_format="PNG", color="#8bbf8b") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, image_format)
    return output.getvalue()


def _relative_luminance(hex_color: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(hex_color[offset:offset + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])


def test_predefined_palettes_keep_dark_text_high_contrast():
    assert len(THEME_PALETTES) == 8
    for palette in THEME_PALETTES.values():
        panel_lum = _relative_luminance(palette["panel"])
        text_lum = _relative_luminance(palette["text"])
        contrast = (max(panel_lum, text_lum) + 0.05) / (min(panel_lum, text_lum) + 0.05)
        assert contrast >= 4.5, palette["name"]


def test_create_resolve_and_delete_custom_theme(tmp_path):
    manager = ThemeManager(tmp_path)
    created = manager.create_theme(
        section="sensorius",
        name="My Farm",
        images=[
            {"name": "Morning Garden", "palette": "pale_sage", "content": _image_bytes()},
            {"name": "Orchard", "palette": "pale_fruit", "content": _image_bytes(color="#c78b75")},
        ],
    )

    assert created["name"] == "My Farm"
    assert len(created["images"]) == 2
    selection = created["images"][0]["selection"]
    assert is_custom_theme_selection(selection)
    resolved = manager.resolve("sensorius", selection)
    assert resolved["image"]["name"] == "Morning Garden"
    assert resolved["palette"]["name"] == "Pale Sage"
    assert manager.resolve("caelus", selection) is None

    image_path = tmp_path / created["images"][0]["asset_url"].removeprefix("/theme-assets/")
    image_path = tmp_path / "theme_assets" / image_path.relative_to(tmp_path)
    with Image.open(image_path) as processed:
        assert processed.format == "WEBP"
        assert processed.size == (1920, 1080)

    styles = manager.style_values("sensorius", selection)
    assert styles["--dashboard-card-bg"] == "#e4f1e4"
    assert created["images"][0]["asset_url"] in styles["background-image"]

    assert manager.delete_theme(created["id"]) is True
    assert manager.resolve("sensorius", selection) is None
    assert not (tmp_path / "theme_assets" / created["id"]).exists()


def test_custom_biodynamic_theme_is_static_and_has_no_auto_role(tmp_path):
    manager = ThemeManager(tmp_path)
    created = manager.create_theme(
        section="biodynamic",
        name="Moon Garden",
        images=[{"name": "Moonlit Beds", "palette": "pale_water", "content": _image_bytes()}],
    )

    image = created["images"][0]
    assert "season" not in image
    assert "automatic" not in image
    styles = manager.style_values("biodynamic", image["selection"])
    assert styles["--theme-panel"] == "#dcebf3"
    assert styles["background-size"] == "cover"


def test_theme_validation_rejects_invalid_inputs(tmp_path):
    manager = ThemeManager(tmp_path)
    valid = {"name": "Garden", "palette": "pale_sage", "content": _image_bytes()}

    with pytest.raises(ThemeValidationError, match="between one and five"):
        manager.create_theme(section="sensorius", name="Empty", images=[])
    with pytest.raises(ThemeValidationError, match="Theme section"):
        manager.create_theme(section="unknown", name="Theme", images=[valid])
    with pytest.raises(ThemeValidationError, match="predefined palettes"):
        manager.create_theme(
            section="caelus",
            name="Theme",
            images=[{**valid, "palette": "#000000"}],
        )
    with pytest.raises(ThemeValidationError, match="valid WebP, JPEG, or PNG"):
        manager.create_theme(
            section="caelus",
            name="Theme",
            images=[{**valid, "content": b"not an image"}],
        )


def test_normalize_custom_selection_falls_back_when_assets_are_missing(tmp_path):
    manager = ThemeManager(tmp_path)
    missing = "custom:" + ("a" * 32) + ":" + ("b" * 32)

    assert normalize_theme_selection(manager, "sensorius", missing, "leaf", lambda value: value) == "leaf"
    assert normalize_theme_selection(manager, "sensorius", "ROOT", "leaf", lambda value: value.lower()) == "root"


def test_manifest_backup_recovers_custom_themes(tmp_path):
    manager = ThemeManager(tmp_path)
    first = manager.create_theme(
        section="sensorius",
        name="First",
        images=[{"name": "First Image", "palette": "pale_sage", "content": _image_bytes()}],
    )
    manager.create_theme(
        section="caelus",
        name="Second",
        images=[{"name": "Second Image", "palette": "pale_sky", "content": _image_bytes()}],
    )
    manager.manifest_path.write_text("{broken", encoding="utf-8")

    recovered = manager.list_themes()

    assert [theme["id"] for theme in recovered] == [first["id"]]
