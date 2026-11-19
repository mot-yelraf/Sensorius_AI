"""Expose user-selected automation Save/Open dialogs to the native window.

Only the configured application origin can open dialogs. Paths come exclusively
from native dialogs and never from browser arguments; imports are bounded and
exports are validated before a selected file is atomically replaced.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .saiAutomationTransfer import MAX_BYTES, parse_document
from .saiUtils import printDM


def _selected_file(selection) -> Path | None:
    if not selection:
        return None
    if isinstance(selection, (list, tuple)):
        if len(selection) != 1:
            raise ValueError("Expected one selected file.")
        selection = selection[0]
    if not isinstance(selection, (str, os.PathLike)) or not os.fspath(selection):
        raise ValueError("Invalid selected file path.")
    return Path(selection)


def attach_automation_file_dialogs(window, base_url: str) -> None:
    """Attach bounded automation file operations to an existing native window."""
    if not callable(getattr(window, "expose", None)):
        return
    expected = urlsplit(base_url)

    def trusted_origin():
        current = urlsplit(window.get_current_url() or "")
        return current.scheme in {"http", "https"} and (current.scheme, current.netloc) == (expected.scheme, expected.netloc)

    def save_automation_export(content, filename):
        try:
            if not trusted_origin():
                return {"error": "Open Sensorius before exporting automations."}
            if not isinstance(content, str) or not isinstance(filename, str) or not re.fullmatch(r"automations\.\d{2}\.\d{3}\.\d+-\d{2}-\d{2}\.json", filename):
                return {"error": "Invalid automation export."}
            parse_document(content)
            import webview
            destination = _selected_file(window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename, file_types=("Automation JSON (*.json)",)))
            if destination is None:
                return {"cancelled": True}
            if not trusted_origin():
                return {"error": "Open Sensorius before exporting automations."}
            if destination.suffix.lower() != ".json":
                return {"error": "Save the automation file with a .json extension."}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {"ok": True, "name": destination.name}
        except Exception as exc:
            printDM(f"Automation export dialog failed: {exc}", location="saiAutomationFileDialogs")
            return {"error": "Unable to save the automation file."}

    def open_automation_import():
        try:
            if not trusted_origin():
                return {"error": "Open Sensorius before importing automations."}
            import webview
            source = _selected_file(window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=("Automation JSON (*.json)",)))
            if source is None:
                return {"cancelled": True}
            if not trusted_origin():
                return {"error": "Open Sensorius before importing automations."}
            if source.suffix.lower() != ".json":
                return {"error": "Choose a Sensorius automation .json file."}
            with source.open("rb") as handle:
                content = handle.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                return {"error": "Automation file is too large (maximum 4 MiB)."}
            return {"ok": True, "name": source.name, "content": content.decode("utf-8-sig")}
        except Exception as exc:
            printDM(f"Automation import dialog failed: {exc}", location="saiAutomationFileDialogs")
            return {"error": "Unable to open the automation file as UTF-8 JSON."}

    window.expose(save_automation_export, open_automation_import)
