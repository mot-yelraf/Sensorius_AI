"""Pytest coverage for the persisted automation contract and helpers.

These tests verify rule storage, enablement, shared-file behavior, and runtime
views so automation edits remain backward compatible.
"""

import os
import sys
import json
from pathlib import Path

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.saiAutomationManager import AutomationManager
from testApparatus.automation_test_harness import adapters, make_manager


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_round_trip_advanced_and_scripts_sections(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    host = "sw-alpha"

    mgr.upsert_advanced_rule(
        host,
        "rule_a",
        enabled=True,
        script={"enabled": True, "actions": [{"switch_key": "sw-alpha::Fan", "set": True, "revert_action": "previous_state", "delay_s": 15}]},
    )
    mgr.set_script_enabled(host, "NightMode", True)

    data = mgr.load(host)
    assert data["Advanced"]["rule_a"]["enabled"] is True
    assert "script_json" in data["Advanced"]["rule_a"]
    assert json.loads(data["Advanced"]["rule_a"]["script_json"])["actions"][0]["revert_action"] == "previous_state"
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


@pytest.mark.parametrize("adapter", adapters(), ids=lambda a: a.name)
def test_aggregate_enabled_state_treats_string_false_as_disabled(tmp_path: Path, adapter):
    mgr = make_manager(adapter, tmp_path)
    if not hasattr(mgr, "get_advanced_state_for_switch_key"):
        pytest.skip("manager does not expose aggregate advanced state helper")
    payload = {
        "Meta": {"version": 1},
        "Advanced": {
            "rule_shared": {
                "enabled": "false",
                "script_json": "{\"enabled\":false,\"actions\":[{\"switch_key\":\"sw-alpha::Fan\",\"set\":true}]}",
            }
        },
        "Scripts": {},
    }
    adapter.save_fn(mgr, "sw-alpha", payload)

    state = mgr.get_advanced_state_for_switch_key("sw-alpha", "sw-alpha::Fan")

    assert state["found"] is True
    assert state["enabled_count"] == 0
    assert state["enabled_any"] is False


def test_automation_manager_runtime_view_returns_parsed_scripts(tmp_path: Path):
    mgr = AutomationManager(base_dir=str(tmp_path))
    host = "sw-runtime"

    mgr.upsert_advanced_rule(
        host,
        "rule_runtime",
        enabled=True,
        script={"enabled": True, "conditions": [{"type": "time", "start": "00:00", "end": "24:00"}], "actions": [{"switch_key": "sw-runtime::Fan", "set": True}]},
    )

    runtime_adv = mgr.load_runtime_advanced(host)

    assert isinstance(runtime_adv["rule_runtime"]["script_json"], dict)
    assert runtime_adv["rule_runtime"]["script_json"]["actions"][0]["switch_key"] == "sw-runtime::Fan"


def test_automation_manager_refreshes_cache_after_external_file_edit(tmp_path: Path):
    mgr = AutomationManager(base_dir=str(tmp_path))
    host = "sw-cache"

    mgr.upsert_advanced_rule(
        host,
        "rule_a",
        enabled=True,
        script={"enabled": True, "actions": [{"switch_key": "sw-cache::Fan", "set": True}]},
    )

    first = mgr.get_advanced_state_for_switch_key(host, "sw-cache::Fan")
    assert first["enabled_any"] is True

    raw_path = mgr.get_storage_path()
    raw_path.write_text(
        "\n".join([
            "[Meta]",
            "version = 1",
            'notes = "Switch automation configuration. Edit carefully."',
            "",
            "[Advanced]",
            'rule_a = { enabled=false, script_json="{\\"enabled\\":false,\\"actions\\":[{\\"switch_key\\":\\"sw-cache::Fan\\",\\"set\\":true}]}" }',
            "",
        ]),
        encoding="utf-8",
    )

    refreshed = mgr.get_advanced_state_for_switch_key(host, "sw-cache::Fan")

    assert refreshed["found"] is True
    assert refreshed["enabled_any"] is False


def test_automation_manager_rule_lookup_expands_case_and_channel_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mgr = AutomationManager(base_dir=str(tmp_path))
    host = "switch-x943fm"

    mgr.upsert_advanced_rule(
        host,
        "pump_on",
        enabled=True,
        script={
            "enabled": True,
            "actions": [{"switch_key": "switch-x943fm::S2-x943fm", "set": True}],
        },
    )

    class _FakeSwitchSettingsManager:
        def __init__(self, _base_dir: str):
            pass

        def load(self, _hostname: str):
            return {
                "Switch": {
                    "SWITCH_1_LABEL": "Fan",
                    "SWITCH_1_CHANNEL_ID": "S1-x943fm",
                    "SWITCH_2_LABEL": "Pump",
                    "SWITCH_2_CHANNEL_ID": "S2-x943fm",
                }
            }

    monkeypatch.setattr("sensorius.saiSwitchSettingsManager.SwitchSettingsManager", _FakeSwitchSettingsManager)

    state = mgr.get_advanced_state_for_switch_key("SWITCH-X943FM", "SWITCH-X943FM::Pump")
    rule = mgr.get_advanced_rule_for_switch_key("SWITCH-X943FM", "SWITCH-X943FM::Pump")

    assert state["found"] is True
    assert state["enabled_any"] is True
    assert state["rule_ids"] == ["pump_on"]
    assert rule == {"found": True, "enabled": True, "rule_id": "pump_on"}
