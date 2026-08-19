"""Manage user-created visual themes and their processed image assets.

Custom themes are stored beneath the writable Sensorius runtime directory and
are merged with the built-in, read-only themes by the web UI. Uploaded images
are validated and converted to bounded WebP assets before they can be served.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .saiRuntimePaths import resolve_runtime_base_dir


THEME_SECTIONS = frozenset({"sensorius", "caelus", "biodynamic"})
MAX_THEME_IMAGES = 5
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
OUTPUT_SIZE = (1920, 1080)
THUMBNAIL_SIZE = (480, 270)
CUSTOM_THEME_PREFIX = "custom:"

THEME_PALETTES = {
    "pale_sage": {
        "name": "Pale Sage",
        "panel": "#e4f1e4",
        "strong": "#f7fcf7",
        "soft": "#cfe2cf",
        "border": "#668366",
        "text": "#1d301f",
        "muted": "#49604c",
        "accent": "#477a50",
    },
    "pale_earth": {
        "name": "Pale Earth",
        "panel": "#efe2c6",
        "strong": "#fffaf1",
        "soft": "#dcc8a4",
        "border": "#8b704b",
        "text": "#2f2114",
        "muted": "#65513c",
        "accent": "#73522f",
    },
    "pale_water": {
        "name": "Pale Water",
        "panel": "#dcebf3",
        "strong": "#f8fcff",
        "soft": "#bfd8e6",
        "border": "#5f8298",
        "text": "#122633",
        "muted": "#385569",
        "accent": "#22658c",
    },
    "pale_sky": {
        "name": "Pale Sky",
        "panel": "#dfeaf8",
        "strong": "#f8fbff",
        "soft": "#c5d8ef",
        "border": "#6687ad",
        "text": "#142b43",
        "muted": "#425d79",
        "accent": "#356ea3",
    },
    "pale_blossom": {
        "name": "Pale Blossom",
        "panel": "#f1e1ed",
        "strong": "#fff8fd",
        "soft": "#dfc4d8",
        "border": "#95708b",
        "text": "#382033",
        "muted": "#6b4a62",
        "accent": "#8b4776",
    },
    "pale_fruit": {
        "name": "Pale Fruit",
        "panel": "#fde1d3",
        "strong": "#fff8f3",
        "soft": "#efc6b2",
        "border": "#a96d53",
        "text": "#3b1c12",
        "muted": "#704739",
        "accent": "#a64f31",
    },
    "warm_neutral": {
        "name": "Warm Neutral",
        "panel": "#ece7df",
        "strong": "#fffdf9",
        "soft": "#d8d0c4",
        "border": "#80766a",
        "text": "#302b25",
        "muted": "#5e574f",
        "accent": "#6d5d49",
    },
    "cool_neutral": {
        "name": "Cool Neutral",
        "panel": "#e4eaec",
        "strong": "#fbfdfe",
        "soft": "#cad5d9",
        "border": "#687b82",
        "text": "#203035",
        "muted": "#4b5e64",
        "accent": "#496f78",
    },
}


class ThemeValidationError(ValueError):
    """Report invalid custom-theme metadata or image content."""


def _clean_name(value: object, *, field: str) -> str:
    name = " ".join(str(value or "").strip().split())
    if not name:
        raise ThemeValidationError(f"{field} is required.")
    if len(name) > 60:
        raise ThemeValidationError(f"{field} must be 60 characters or fewer.")
    return name


def custom_theme_selection(theme_id: str, image_id: str) -> str:
    """Return the stable settings value for one custom theme image."""
    return f"{CUSTOM_THEME_PREFIX}{theme_id}:{image_id}"


def is_custom_theme_selection(value: object) -> bool:
    """Return whether a value has the safe generated custom-theme shape."""
    parts = str(value or "").split(":")
    return (
        len(parts) == 3
        and parts[0] == "custom"
        and all(len(part) == 32 and all(ch in "0123456789abcdef" for ch in part) for part in parts[1:])
    )


def normalize_theme_selection(
    manager: "ThemeManager",
    section: str,
    value: object,
    default: str,
    builtin_normalizer,
) -> str:
    """Normalize either a registered custom selection or a built-in value."""
    raw = str(value or "").strip()
    if is_custom_theme_selection(raw):
        return manager.normalize_selection(section, raw, default)
    return str(builtin_normalizer(raw) or default)


class ThemeManager:
    """Persist, resolve, and remove user-created theme collections."""

    _lock = threading.RLock()

    def __init__(self, runtime_root: str | Path | None = None):
        root = (
            Path(runtime_root).expanduser().resolve()
            if runtime_root is not None
            else resolve_runtime_base_dir("system_settings").parent
        )
        self.runtime_root = root
        self.settings_dir = root / "theme_settings"
        self.assets_dir = root / "theme_assets"
        self.manifest_path = self.settings_dir / "themes.json"
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        for candidate in (self.manifest_path, self.manifest_path.with_suffix(".json.bak")):
            if not candidate.exists():
                continue
            try:
                if candidate.stat().st_size > 1024 * 1024:
                    continue
                document = json.loads(candidate.read_text(encoding="utf-8"))
                themes = document.get("themes") if isinstance(document, dict) else None
                if isinstance(themes, list):
                    return {"version": 1, "themes": themes}
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return {"version": 1, "themes": []}

    def _write(self, document: dict) -> None:
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.manifest_path.with_name(f".{self.manifest_path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
        temp_path.write_text(encoded, encoding="utf-8")
        had_manifest = self.manifest_path.exists()
        if had_manifest:
            shutil.copy2(self.manifest_path, self.manifest_path.with_suffix(".json.bak"))
        os.replace(temp_path, self.manifest_path)
        if not had_manifest:
            shutil.copy2(self.manifest_path, self.manifest_path.with_suffix(".json.bak"))

    @staticmethod
    def palettes() -> list[dict]:
        """Return safe palette choices for the creator dialog."""
        return [
            {"id": palette_id, **values}
            for palette_id, values in THEME_PALETTES.items()
        ]

    def list_themes(self, section: str | None = None) -> list[dict]:
        """Return validated custom collections, optionally for one section."""
        wanted = str(section or "").strip().lower()
        result = []
        for raw_theme in self._load().get("themes", []):
            if not isinstance(raw_theme, dict):
                continue
            theme_section = str(raw_theme.get("section") or "").strip().lower()
            if theme_section not in THEME_SECTIONS or (wanted and theme_section != wanted):
                continue
            theme_id = str(raw_theme.get("id") or "")
            if len(theme_id) != 32:
                continue
            images = []
            for raw_image in raw_theme.get("images", []):
                if not isinstance(raw_image, dict):
                    continue
                image_id = str(raw_image.get("id") or "")
                filename = str(raw_image.get("file") or "")
                thumb = str(raw_image.get("thumbnail") or "")
                palette = str(raw_image.get("palette") or "")
                if len(image_id) != 32 or palette not in THEME_PALETTES:
                    continue
                if Path(filename).name != filename or Path(thumb).name != thumb:
                    continue
                if not (self.assets_dir / theme_id / filename).is_file() or not (self.assets_dir / theme_id / thumb).is_file():
                    continue
                images.append({
                    "id": image_id,
                    "name": str(raw_image.get("name") or "Custom Theme"),
                    "palette": palette,
                    "palette_name": THEME_PALETTES[palette]["name"],
                    "asset_url": f"/theme-assets/{theme_id}/{filename}",
                    "thumbnail_url": f"/theme-assets/{theme_id}/{thumb}",
                    "selection": custom_theme_selection(theme_id, image_id),
                })
            if images:
                result.append({
                    "id": theme_id,
                    "section": theme_section,
                    "name": str(raw_theme.get("name") or "Custom Theme"),
                    "images": images,
                    "custom": True,
                })
        return result

    def resolve(self, section: str, selection: object) -> dict | None:
        """Resolve a custom selection to its collection, image, and palette."""
        raw = str(selection or "")
        if not is_custom_theme_selection(raw):
            return None
        _prefix, theme_id, image_id = raw.split(":")
        for theme in self.list_themes(section):
            if theme["id"] != theme_id:
                continue
            for image in theme["images"]:
                if image["id"] == image_id:
                    return {
                        "selection": raw,
                        "theme": theme,
                        "image": image,
                        "palette": dict(THEME_PALETTES[image["palette"]]),
                    }
        return None

    def normalize_selection(self, section: str, value: object, default: str) -> str:
        """Keep an existing custom selection only while its assets exist."""
        raw = str(value or "").strip()
        if is_custom_theme_selection(raw):
            return raw if self.resolve(section, raw) else default
        return raw

    def style_values(self, section: str, selection: object) -> dict[str, str]:
        """Return safe CSS property values for a resolved custom selection."""
        resolved = self.resolve(section, selection)
        if not resolved:
            return {}
        palette = resolved["palette"]
        image_url = resolved["image"]["asset_url"]
        common = {
            "background-image": f"url('{image_url}')",
            "background-position": "center",
            "background-repeat": "no-repeat",
            "background-size": "cover",
            "background-attachment": "fixed",
        }
        if section == "sensorius":
            return {
                **common,
                "--dashboard-card-bg": palette["panel"],
                "--dashboard-card-border": palette["border"],
                "--dashboard-card-text": palette["text"],
            }
        if section == "caelus":
            return {
                "--scene-image": f"url('{image_url}')",
                "--scene-position": "center",
                "--scene-repeat": "no-repeat",
                "--scene-size": "cover",
                "--accent": palette["strong"],
                "--accent-2": palette["soft"],
                "--line": "rgba(235, 247, 240, 0.38)",
            }
        return {
            **common,
            "--theme-panel": palette["panel"],
            "--theme-panel-strong": palette["strong"],
            "--theme-panel-soft": palette["soft"],
            "--theme-border": palette["border"],
            "--theme-ink": palette["text"],
            "--theme-muted": palette["muted"],
            "--theme-accent": palette["accent"],
            "--theme-button": palette["soft"],
            "--theme-lunar": palette["text"],
            "--theme-lunar-edge": palette["border"],
        }

    @staticmethod
    def style_attribute(values: dict[str, str]) -> str:
        """Serialize registry-owned CSS properties for an inline style."""
        return ";".join(f"{key}:{value}" for key, value in values.items())

    @staticmethod
    def _process_image(content: bytes, output_path: Path, thumbnail_path: Path) -> None:
        if not content:
            raise ThemeValidationError("Uploaded image is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ThemeValidationError("Each image must be 5 MB or smaller.")
        try:
            with Image.open(io.BytesIO(content)) as probe:
                if str(probe.format or "").upper() not in {"WEBP", "JPEG", "PNG"}:
                    raise ThemeValidationError("Only valid WebP, JPEG, or PNG images are supported.")
                width, height = probe.size
                if width < 320 or height < 180:
                    raise ThemeValidationError("Images must be at least 320 x 180 pixels.")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ThemeValidationError("Image dimensions are too large.")
                if bool(getattr(probe, "is_animated", False)):
                    raise ThemeValidationError("Animated images are not supported.")
                probe.verify()
            with Image.open(io.BytesIO(content)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                fitted = ImageOps.fit(image, OUTPUT_SIZE, method=Image.Resampling.LANCZOS)
                fitted.save(output_path, "WEBP", quality=84, method=4)
                thumb = ImageOps.fit(image, THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS)
                thumb.save(thumbnail_path, "WEBP", quality=78, method=4)
        except ThemeValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ThemeValidationError("Only valid WebP, JPEG, or PNG images are supported.") from exc

    def create_theme(
        self,
        *,
        section: str,
        name: object,
        images: list[dict],
    ) -> dict:
        """Validate and atomically register one custom theme collection."""
        normalized_section = str(section or "").strip().lower()
        if normalized_section not in THEME_SECTIONS:
            raise ThemeValidationError("Theme section is not supported.")
        theme_name = _clean_name(name, field="Theme name")
        if not 1 <= len(images) <= MAX_THEME_IMAGES:
            raise ThemeValidationError("Choose between one and five images.")

        theme_id = uuid4().hex
        theme_dir = self.assets_dir / theme_id
        created_images = []
        try:
            theme_dir.mkdir(parents=True, exist_ok=False)
            for upload in images:
                image_id = uuid4().hex
                image_name = _clean_name(upload.get("name"), field="Image name")
                palette = str(upload.get("palette") or "").strip().lower()
                if palette not in THEME_PALETTES:
                    raise ThemeValidationError("Choose one of the predefined palettes.")
                output_name = f"{image_id}.webp"
                thumb_name = f"{image_id}-thumb.webp"
                self._process_image(
                    bytes(upload.get("content") or b""),
                    theme_dir / output_name,
                    theme_dir / thumb_name,
                )
                created_images.append({
                    "id": image_id,
                    "name": image_name,
                    "palette": palette,
                    "file": output_name,
                    "thumbnail": thumb_name,
                })

            with self._lock:
                document = self._load()
                document["themes"].append({
                    "id": theme_id,
                    "section": normalized_section,
                    "name": theme_name,
                    "images": created_images,
                })
                self._write(document)
        except Exception:
            shutil.rmtree(theme_dir, ignore_errors=True)
            raise

        return next(theme for theme in self.list_themes(normalized_section) if theme["id"] == theme_id)

    def delete_theme(self, theme_id: str) -> bool:
        """Delete one custom collection and its generated assets."""
        safe_id = str(theme_id or "").strip().lower()
        if len(safe_id) != 32 or any(ch not in "0123456789abcdef" for ch in safe_id):
            raise ThemeValidationError("Custom theme ID is invalid.")
        with self._lock:
            document = self._load()
            themes = document.get("themes", [])
            kept = [theme for theme in themes if str(theme.get("id") or "") != safe_id]
            if len(kept) == len(themes):
                return False
            document["themes"] = kept
            self._write(document)
        shutil.rmtree(self.assets_dir / safe_id, ignore_errors=True)
        return True
