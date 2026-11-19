"""Verify persisted automation grouping without changing runtime scripts.

Exercise group boundaries, legacy rules, editor saves, and the HTTP contract.
"""
import json

import pytest

from sensorius.saiAutomationManager import AutomationManager, automation_group, automation_position


def ordered(rules, group):
    return sorted((key for key, rule in rules.items() if automation_group(rule) == group),
                  key=lambda key: (automation_position(rules[key]), key))


def test_grouping_reordering_and_editor_save_preserve_runtime(tmp_path):
    mgr = AutomationManager(str(tmp_path))
    script = {"enabled": True, "conditions": [], "actions": [{"switch_key": "node::Fan", "set": True}]}
    for key in ("a", "b", "c"):
        mgr.upsert_advanced_rule("hub", key, enabled=True, script=script)
    assert mgr.arrange_rule("hub", "b", "Up")
    assert ordered(mgr.load("hub")["Advanced"], "Other") == ["b", "a", "c"]
    mgr.arrange_rule("hub", "b", "Down")
    assert ordered(mgr.load("hub")["Advanced"], "Other") == ["a", "b", "c"]
    mgr.arrange_rule("hub", "a", "Up")
    mgr.arrange_rule("hub", "c", "Down")
    for group in ("Indoor", "Outdoor", "Greenhouse", "Other"):
        mgr.arrange_rule("hub", "b", group)
        assert automation_group(mgr.load("hub")["Advanced"]["b"]) == group
    mgr.arrange_rule("hub", "a", "Indoor")
    mgr.arrange_rule("hub", "b", "Indoor")
    assert ordered(mgr.load("hub")["Advanced"], "Indoor") == ["a", "b"]
    mgr.upsert_advanced_rule("hub", "b", enabled=True, script=script)
    AutomationManager._shared_cache.clear()
    rules = AutomationManager(str(tmp_path)).load("hub")["Advanced"]
    assert ordered(rules, "Indoor") == ["a", "b"]
    for rule in rules.values():
        assert rule["enabled"] is True
        assert json.loads(rule["script_json"]) == script
    assert mgr.get_advanced_state_for_switch_key("hub", "node::Fan")["enabled_any"] is True
    with pytest.raises(ValueError):
        mgr.arrange_rule("hub", "b", "Invalid")
    assert mgr.arrange_rule("hub", "missing", "Indoor") is False


def test_arrange_legacy_preserves_source_and_scripts(tmp_path):
    from testApparatus.test_automation_contract import _write_legacy_automation_file

    legacy = tmp_path / "switch_settings" / "automations" / "automations.toml"
    _write_legacy_automation_file(legacy)
    original = legacy.read_bytes()
    mgr = AutomationManager(str(tmp_path / "automation_settings"), legacy_base_dir=tmp_path / "switch_settings")
    before = mgr.load("hub")["Advanced"]
    mgr.arrange_rule("hub", "legacy_b", "Up")
    after = mgr.load("hub")["Advanced"]
    assert ordered(after, "Other") == ["legacy_b", "legacy_a"]
    assert legacy.read_bytes() == original
    for key in before:
        assert after[key]["enabled"] == before[key]["enabled"]
        assert after[key]["script_json"] == before[key]["script_json"]


@pytest.mark.asyncio
async def test_arrangement_http_contract(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as managers
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    app, *_ = await _build_app(tmp_path, monkeypatch)

    class Manager(AutomationManager):
        def __init__(self, _base="automation_settings"):
            super().__init__(str(tmp_path / "automation-test"))

    monkeypatch.setattr(managers, "AutomationManager", Manager)
    Manager().upsert_advanced_rule("hub", "a", enabled=False, script={"actions": []})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        payload = {"switch_id": "__all__", "rule_id": "a", "choice": "Greenhouse"}
        assert (await client.post("/advanced/automations/arrange", data=payload)).status_code == 200
        items = (await client.get("/advanced/automations", params={"switch_id": "__all__"})).json()["items"]
        assert items[0]["group"] == "Greenhouse"
        assert items[0]["position"] == 0
        payload["choice"] = "Invalid"
        assert (await client.post("/advanced/automations/arrange", data=payload)).status_code == 400
        payload.update(choice="Indoor", rule_id="missing")
        assert (await client.post("/advanced/automations/arrange", data=payload)).status_code == 404
        group_payload = {"group": "Nursery", "choice": "Add"}
        assert (await client.post("/advanced/automations/groups", data=group_payload)).status_code == 200
        assert (await client.post("/advanced/automations/groups", data=group_payload)).status_code == 400
        groups = (await client.get("/advanced/automations", params={"switch_id": "__all__"})).json()["groups"]
        assert groups == ["Indoor", "Outdoor", "Greenhouse", "Other", "Nursery"]
        group_payload["choice"] = "Up"
        assert (await client.post("/advanced/automations/groups", data=group_payload)).status_code == 200
        groups = (await client.get("/advanced/automations", params={"switch_id": "__all__"})).json()["groups"]
        assert groups[-2:] == ["Nursery", "Other"]
        group_payload.update(group="Greenhouse", choice="Remove")
        assert (await client.post("/advanced/automations/groups", data=group_payload)).status_code == 400
        result = (await client.get("/advanced/automations", params={"switch_id": "__all__"})).json()
        assert "Greenhouse" in result["groups"]
        assert result["items"][0]["group"] == "Greenhouse"
        assert (await client.post("/advanced/automations/arrange", data={
            "switch_id": "__all__", "rule_id": "a", "choice": "Other",
        })).status_code == 200
        assert (await client.post("/advanced/automations/groups", data=group_payload)).status_code == 200


def test_custom_groups_order_removal_and_new_rule_default(tmp_path):
    mgr = AutomationManager(str(tmp_path))
    mgr.upsert_advanced_rule('hub', 'a', enabled=True, script={'actions': []})
    original = mgr.load('hub')['Advanced']['a']['script_json']
    mgr.update_group('hub', 'Nursery', 'Add')
    mgr.arrange_rule('hub', 'a', 'Nursery')
    mgr.update_group('hub', 'Nursery', 'Up')
    assert mgr.groups_from_data(mgr.load('hub')) == ['Indoor', 'Outdoor', 'Greenhouse', 'Nursery', 'Other']
    mgr.update_group('hub', 'Nursery', 'Down')
    before = mgr.get_storage_path().read_bytes()
    with pytest.raises(ValueError, match='Move all automations'):
        mgr.update_group('hub', 'Nursery', 'Remove')
    assert mgr.get_storage_path().read_bytes() == before
    mgr.arrange_rule('hub', 'a', 'Other')
    mgr.update_group('hub', 'Nursery', 'Remove')
    rule = mgr.load('hub')['Advanced']['a']
    assert rule['group'] == 'Other'
    assert rule['enabled'] is True and rule['script_json'] == original
    mgr.arrange_rule('hub', 'a', 'Indoor')
    for name in ('Other', 'Outdoor', 'Greenhouse'):
        mgr.update_group('hub', name, 'Remove')
    mgr.upsert_advanced_rule('hub', 'new', enabled=False, script={'actions': []})
    AutomationManager._shared_cache.clear()
    loaded = AutomationManager(str(tmp_path)).load('hub')
    assert mgr.groups_from_data(loaded) == ['Indoor']
    assert all(rule['group'] == 'Indoor' for rule in loaded['Advanced'].values())
    with pytest.raises(ValueError, match='at least one'):
        mgr.update_group('hub', 'Indoor', 'Remove')
    for name in ('indoor', 'Up', '', 'x' * 81):
        with pytest.raises(ValueError):
            mgr.update_group('hub', name, 'Add')
