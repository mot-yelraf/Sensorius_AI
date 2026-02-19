import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSwitchTriggerManager import SwitchTriggerManager, load_triggers, save_triggers


def test_load_defaults_when_automation_file_missing(tmp_path: Path):
    mgr = SwitchTriggerManager(base_dir=str(tmp_path))
    data = mgr.load("sw-legacy")
    assert data["Advanced"] == {}
    assert mgr.get_storage_path().exists() is False


def test_save_writes_automations_toml(tmp_path: Path):
    mgr = SwitchTriggerManager(base_dir=str(tmp_path))
    mgr.upsert_advanced_rule(
        "sw-new",
        "rule_1",
        enabled=True,
        script={"actions": [{"switch_key": "sw-new::Fan", "set": True}]},
    )

    saved = mgr.get_storage_path()
    assert saved.exists()
    text = saved.read_text(encoding="utf-8")
    assert "[Advanced]" in text
    assert "rule_1" in text


def test_module_helpers_type_checks(tmp_path: Path):
    mgr = SwitchTriggerManager(base_dir=str(tmp_path))
    with pytest.raises(TypeError):
        load_triggers(object(), "sw1")
    with pytest.raises(TypeError):
        save_triggers(mgr, "sw1", "not-a-dict")
