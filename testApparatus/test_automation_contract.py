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

    saved_path = mgr.get_storage_path()
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
def test_storage_is_shared_across_switch_ids(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    mgr.upsert_advanced_rule(
        "sw-alpha",
        "rule_shared",
        enabled=True,
        script={"enabled": True, "actions": [{"switch_key": "sw-alpha::Fan", "set": True}]},
    )
    loaded_from_other_context = mgr.load("sw-beta")
    assert "rule_shared" in loaded_from_other_context["Advanced"]


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_enable_and_delete_work_across_switch_contexts(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    payload = {
        "Meta": {"version": 1},
        "Advanced": {
            "rule_shared": {
                "enabled": False,
                "script_json": "{\"actions\":[{\"switch_key\":\"sw-alpha::Fan\",\"set\":true}]}",
            }
        },
        "Scripts": {},
    }
    adapter.save_fn(mgr, "sw-alpha", payload)

    assert mgr.set_rule_enabled("sw-beta", "Advanced", "rule_shared", True) is True
    assert mgr.load("sw-alpha")["Advanced"]["rule_shared"]["enabled"] is True

    assert mgr.delete_rule("sw-gamma", "Advanced", "rule_shared") is True
    assert "rule_shared" not in mgr.load("sw-alpha")["Advanced"]


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_upsert_same_rule_id_updates_in_place(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    host = "sw-edit"

    mgr.upsert_advanced_rule(
        host,
        "pump_on",
        enabled=True,
        script={"name": "Pump On", "enabled": True, "actions": [{"switch_key": "sw-edit::Pump", "set": True}]},
    )
    mgr.upsert_advanced_rule(
        host,
        "pump_on",
        enabled=False,
        script={"name": "Pump On Updated", "enabled": False, "actions": [{"switch_key": "sw-edit::Pump", "set": False}]},
    )

    data = mgr.load(host)
    assert sorted(data["Advanced"].keys()) == ["pump_on"]
    assert data["Advanced"]["pump_on"]["enabled"] is False
    assert "Pump On Updated" in data["Advanced"]["pump_on"]["script_json"]
