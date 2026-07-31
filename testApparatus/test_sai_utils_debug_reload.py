"""Test dynamic debug configuration without module reloads.

Environment and dotenv state are isolated with pytest monkeypatching.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiUtils as saiUtils

def test_debug_flag_reflects_env_change_without_module_reload(monkeypatch):
    monkeypatch.setenv("SENSORIUS_DEBUG_MODULES", "Sensorius")

    flag = saiUtils.debug_enabled("saiSwitch")
    assert bool(flag) is False

    monkeypatch.setenv("SENSORIUS_DEBUG_MODULES", "Sensorius,saiSwitch")
    assert bool(flag) is True


def test_get_env_setting_refreshes_dotenv_backed_debug_modules(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SENSORIUS_DEBUG_MODULES=Sensorius\n", encoding="utf-8")

    monkeypatch.setattr(saiUtils, "_DOTENV_PATH", dotenv_path)
    monkeypatch.setattr(saiUtils, "_DOTENV_FILE_VALUES", {})
    monkeypatch.setattr(saiUtils, "_DOTENV_MTIME_NS", None)
    monkeypatch.setattr(saiUtils, "load_dotenv", lambda *args, **kwargs: None)

    def _fake_dotenv_values(*_args, **_kwargs):
        rows = {}
        for line in Path(dotenv_path).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                rows[key.strip()] = value.strip()
        return rows

    monkeypatch.setattr(saiUtils, "dotenv_values", _fake_dotenv_values)
    monkeypatch.delenv("SENSORIUS_DEBUG_MODULES", raising=False)

    flag = saiUtils.debug_enabled("saiSwitch")
    assert bool(flag) is False

    dotenv_path.write_text("SENSORIUS_DEBUG_MODULES=Sensorius,saiSwitch\n", encoding="utf-8")
    assert bool(flag) is True


def test_debug_enabled_returns_bool_like_flag():
    flag = saiUtils.debug_enabled("Sensorius")
    assert hasattr(flag, "__bool__")
    assert "_DynamicDebugFlag" in repr(flag)
