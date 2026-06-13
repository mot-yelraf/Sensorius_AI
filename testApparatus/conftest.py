"""Pytest runtime-state isolation for Sensorius tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME_ROOT_ENV = "SENSORIUS_TEST_RUNTIME_ROOT"
RUNTIME_DIR_NAMES = ("sensor_settings", "switch_settings", "system_settings")
ALLOWED_REPO_RUNTIME_ENTRIES = {"__init__.py", "factory", "factory_nodus"}


def _repo_runtime_entries() -> set[Path]:
    entries: set[Path] = set()
    for dirname in RUNTIME_DIR_NAMES:
        root = REPO_ROOT / dirname
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.name in ALLOWED_REPO_RUNTIME_ENTRIES:
                continue
            entries.add(child.relative_to(REPO_ROOT))
    return entries


def _remove_repo_runtime_entry(relative_path: Path) -> None:
    path = REPO_ROOT / relative_path
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


@pytest.fixture(autouse=True)
def isolate_runtime_settings_roots(tmp_path, monkeypatch):
    """Keep bare settings managers and accidental runtime writes out of the repo."""
    monkeypatch.setenv(TEST_RUNTIME_ROOT_ENV, str(tmp_path / "sensorius-runtime"))
    before = _repo_runtime_entries()
    yield
    after = _repo_runtime_entries()
    for relative_path in sorted(after - before):
        _remove_repo_runtime_entry(relative_path)
