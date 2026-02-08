import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from testApparatus.automation_test_harness import adapters, make_manager


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_round_trip_advanced_and_scripts_sections(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    host = "sw-alpha"

    mgr.upsert_advanced_rule(
        host,
        "rule_a",
        enabled=True,
        script={"enabled": True, "actions": [{"switch_key": "sw-alpha::Fan", "set": True}]},
    )
    mgr.set_script_enabled(host, "NightMode", True)

    data = mgr.load(host)
    assert data["Advanced"]["rule_a"]["enabled"] is True
    assert "script_json" in data["Advanced"]["rule_a"]
    assert data["Scripts"]["NightMode"] is True

    saved_path = tmp_path / host / adapter.filename
    assert saved_path.exists()
    raw = saved_path.read_text(encoding="utf-8")
    assert "[Advanced]" in raw
    assert "[Scripts]" in raw


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_enable_and_remove_helpers_cover_advanced_and_scripts(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    host = "sw-beta"

    payload = {
        "Meta": {"version": 1},
        "Advanced": {
            "rule_1": {
                "enabled": False,
                "script_json": "{\"actions\":[{\"switch_key\":\"sw-beta::Fan\",\"set\":true}]}",
            }
        },
        "Scripts": {"NightMode": False},
    }
    adapter.save_fn(mgr, host, payload)

    assert adapter.enable_fn(mgr, host, "Advanced", "rule_1", True) is True
    assert mgr.load(host)["Advanced"]["rule_1"]["enabled"] is True

    assert adapter.enable_fn(mgr, host, "Scripts", "NightMode", True) is True
    assert mgr.load(host)["Scripts"]["NightMode"] is True

    assert adapter.remove_fn(mgr, host, "Scripts", "NightMode") is True
    assert "NightMode" not in mgr.load(host)["Scripts"]


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_invalid_hostnames_are_rejected(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    for bad in ("", ".", "..", "../x", "x/../y", "x/y", r"x\y"):
        with pytest.raises(ValueError):
            mgr.load(bad)
