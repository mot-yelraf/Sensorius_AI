"""Paths to source-tree resources used by the Sensorius runtime."""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_project_root() -> Path:
    override = str(os.environ.get("SENSORIUS_PROJECT_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "Sensorius.py").is_file():
        return source_root

    working_root = Path.cwd().resolve()
    if (working_root / "Sensorius.py").is_file():
        return working_root

    return source_root


PROJECT_ROOT = _resolve_project_root()


def project_path(*parts: str) -> Path:
    """Return an absolute path below the deployed Sensorius project root."""
    return PROJECT_ROOT.joinpath(*parts)
