"""Runtime path helpers for Sensorius writable state.

Bare settings roots such as ``switch_settings`` should resolve under the
installed runtime directory (``~/Sensorius``), not under the source checkout.
During pytest, a test runtime root can be supplied to keep unqualified settings
managers from writing ignored runtime state into the checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_NAME = "Sensorius"
TEST_RUNTIME_ROOT_ENV = "SENSORIUS_TEST_RUNTIME_ROOT"
_RUNTIME_DIR_NAMES = {"sensor_settings", "switch_settings", "system_settings"}


def resolve_runtime_base_dir(base_dir: str | Path) -> Path:
    """Resolve a settings base directory to the runtime root when appropriate."""
    raw = str(base_dir or "").strip()
    if not raw:
        raw = "."

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()

    # Keep pytest isolation intact; tests frequently rely on temporary relative roots.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        test_root = os.environ.get(TEST_RUNTIME_ROOT_ENV, "").strip()
        if test_root and raw in _RUNTIME_DIR_NAMES:
            return (Path(test_root).expanduser() / raw).resolve()
        return path.resolve()

    if raw in _RUNTIME_DIR_NAMES:
        return (Path.home() / RUNTIME_ROOT_NAME / raw).expanduser().resolve()

    return path.resolve()
