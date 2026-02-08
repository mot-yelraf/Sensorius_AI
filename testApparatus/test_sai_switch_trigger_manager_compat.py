import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSwitchTriggerManager import SwitchTriggerManager, load_triggers, save_triggers


def test_load_reads_legacy_triggers_toml_when_automations_missing(tmp_path: Path):
    host = "sw-legacy"
    host_dir = tmp_path / host
    host_dir.mkdir(parents=True, exist_ok=True)
    (host_dir / "triggers.toml").write_text(
        (
            "[Meta]\n"
            "version = 1\n"
            "notes = \"legacy\"\n\n"
            "[Advanced]\n"
            "rule_1 = { enabled=true, script_json=\"{\\\"actions\\\":[{\\\"switch_key\\\":\\\"sw-legacy::Fan\\\",\\\"set\\\":true}]}\" }\n"
        ),
        encoding="utf-8",
    )

    mgr = SwitchTriggerManager(base_dir=str(tmp_path))
    data = mgr.load(host)
    assert data["Advanced"]["rule_1"]["enabled"] is True
    assert (tmp_path / host / "automations.toml").exists() is False


def test_save_writes_automations_toml(tmp_path: Path):
    mgr = SwitchTriggerManager(base_dir=str(tmp_path))
    mgr.upsert_advanced_rule(
        "sw-new",
        "rule_1",
        enabled=True,
        script={"actions": [{"switch_key": "sw-new::Fan", "set": True}]},
    )

    saved = tmp_path / "sw-new" / "automations.toml"
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
