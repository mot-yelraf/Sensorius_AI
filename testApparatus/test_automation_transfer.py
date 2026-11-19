"""Exercise readable automation transfers, atomic imports, and native file dialogs.

Checks use temporary settings and fake dialogs, with no live runtime or hardware.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import uuid

import pytest

from sensorius.saiAutomationManager import AutomationManager
from sensorius.saiAutomationTransfer import MAX_BYTES, TransferFormatError, export_document, parse_document
from sensorius.saiAutomationFileDialogs import attach_automation_file_dialogs


def source_document(tmp_path):
    manager = AutomationManager(str(tmp_path / "source"))
    manager.upsert_advanced_rule("__all__", "sensor-rule", enabled=True, script={
        "name": "Greenhouse temperature alert", "enabled": True,
        "conditions": [{"type": "sensor", "sensor": "node-a", "metric": "Temperature", "op": ">", "value": 25.5, "hyst": 1}],
        "actions": [{"type": "switch", "switch_key": "switch-a::Fan", "set": True, "delay_s": 0, "revert_action": "previous_state"}],
        "_notification_revision": 5,
    })
    manager.arrange_rule("__all__", "sensor-rule", "Greenhouse")
    filename, content = export_document(manager.load("__all__"), now=datetime(2026, 11, 19, 15, 42, tzinfo=timezone.utc))
    return manager, filename, content


def test_readable_export_and_disabled_atomic_retry_safe_import(tmp_path):
    source, filename, content = source_document(tmp_path)
    assert filename.startswith("automations.26.323.") and filename.endswith("-15-42.json")
    assert '\n      "script": {' in content
    assert "script_json" not in content and "_notification_revision" not in content
    document = parse_document(content)
    assert document["automations"][0]["script"]["conditions"][0]["value"] == 25.5
    target = AutomationManager(str(tmp_path / "target"))
    target.upsert_advanced_rule("__all__", "existing", enabled=True, script={"name": "Existing", "conditions": [], "actions": []})
    target.update_group("__all__", "Nursery", "Add")
    source.update_group("__all__", "Custom bed", "Add")
    _, content = export_document(source.load("__all__"))
    document = parse_document(content)
    batch = str(uuid.uuid4())
    ids = target.import_transfer_rules(document, batch)
    data = target.load("__all__")
    assert target.groups_from_data(data)[-2:] == ["Nursery", "Custom bed"]
    assert data["Advanced"]["existing"]["enabled"] is True
    imported = data["Advanced"][ids[0]]
    assert imported["group"] == "Greenhouse" and imported["enabled"] is False
    script = json.loads(imported["script_json"])
    assert script["enabled"] is False
    assert script["actions"][0]["switch_key"] == "switch-a::Fan"
    target.upsert_advanced_rule("__all__", ids[0], enabled=True, script={**script, "name": "Reviewed"})
    assert target.import_transfer_rules(document, batch) == ids
    assert len(target.load("__all__")["Advanced"]) == 2
    assert json.loads(target.load("__all__")["Advanced"][ids[0]]["script_json"])["name"] == "Reviewed"
    assert len(target.import_transfer_rules(document, str(uuid.uuid4()))) == 1
    assert len(target.load("__all__")["Advanced"]) == 3


@pytest.mark.parametrize("change", [
    lambda doc: doc.update(format="other-app"),
    lambda doc: doc.update(format_version=999),
    lambda doc: doc.update(format_version=True),
    lambda doc: doc.update(groups=["Indoor", "indoor"]),
    lambda doc: doc["automations"][0].update(group="Missing"),
    lambda doc: doc["automations"][0]["script"]["conditions"][0].update(type="execute"),
    lambda doc: doc["automations"][0]["script"]["conditions"][0].update(value="25"),
    lambda doc: doc["automations"][0]["script"]["actions"][0].update(set="true"),
    lambda doc: doc["automations"].append(doc["automations"][0]),
    lambda doc: doc["automations"][0].update(script=None),
    lambda doc: doc["automations"][0]["script"].update(actions=[{"type": "notify", "to": "x" * 1000 + "@test.example"}] * 100),
])
def test_invalid_file_never_changes_settings(tmp_path, change):
    target, _, content = source_document(tmp_path)
    document = json.loads(content)
    change(document)
    before = target.get_storage_path().read_bytes()
    with pytest.raises(TransferFormatError):
        target.import_transfer_rules(document, str(uuid.uuid4()))
    assert target.get_storage_path().read_bytes() == before


@pytest.mark.parametrize("content", [b'{', b'\xff', b'{"format":1,"format":2}', b'{"value":NaN}', b'x' * (MAX_BYTES + 1), b'[' * 1500])
def test_rejects_malformed_ambiguous_and_oversized_json(content):
    with pytest.raises(TransferFormatError):
        parse_document(content)


def test_import_merges_group_case_and_preserves_source_order(tmp_path):
    source, _, content = source_document(tmp_path)
    source.upsert_advanced_rule("__all__", "second", enabled=False, script={"name": "Second", "conditions": [], "actions": []})
    source.arrange_rule("__all__", "second", "Greenhouse")
    _, content = export_document(source.load("__all__"))
    document = parse_document(content)
    document["automations"].reverse()
    target = AutomationManager(str(tmp_path / "ordered"))
    target.update_group("__all__", "Greenhouse", "Remove")
    target.update_group("__all__", "greenhouse", "Add")
    target.import_transfer_rules(document, str(uuid.uuid4()))
    data = target.load("__all__")
    assert "Greenhouse" not in target.groups_from_data(data)
    rules = sorted(data["Advanced"].values(), key=lambda rule: rule["position"])
    assert [json.loads(rule["script_json"])["name"] for rule in rules] == ["Greenhouse temperature alert", "Second"]
    assert all(rule["group"] == "greenhouse" for rule in rules)


@pytest.mark.parametrize("selection_kind", ["string", "tuple", "list"])
def test_native_dialog_save_open_cancel_and_origin_guard(tmp_path, monkeypatch, selection_kind):
    _, filename, content = source_document(tmp_path)
    documents = tmp_path / "Documents"
    documents.mkdir()
    path = documents / filename
    calls, functions = [], {}
    current_url = ["http://127.0.0.1:8000/"]
    selected = [(str(path),)]
    def dialog(kind, **options):
        calls.append((kind, options))
        selection = selected[0]
        if selection is None:
            return None
        if selection_kind == "string":
            return selection[0]
        return list(selection) if selection_kind == "list" else selection
    window = SimpleNamespace(expose=lambda *items: functions.update({item.__name__: item for item in items}),
                             get_current_url=lambda: current_url[0], create_file_dialog=dialog)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG=30, OPEN_DIALOG=10))
    attach_automation_file_dialogs(window, "http://127.0.0.1:8000")
    assert functions["save_automation_export"](content, filename) == {"ok": True, "name": filename}
    assert path.read_text() == content
    assert not (tmp_path / filename).exists()
    assert list(documents.iterdir()) == [path]
    assert calls[-1] == (30, {"save_filename": filename, "file_types": ("Automation JSON (*.json)",)})
    assert functions["open_automation_import"]() == {"ok": True, "name": filename, "content": content}
    before = path.read_bytes()
    count = len(calls)
    assert "error" in functions["save_automation_export"]("malformed JSON", filename)
    assert len(calls) == count and path.read_bytes() == before
    selected[0] = (str(tmp_path / "wrong.txt"),)
    assert "error" in functions["save_automation_export"](content, filename)
    assert not (tmp_path / "wrong.txt").exists()
    assert "error" in functions["open_automation_import"]()
    selected[0] = None
    assert functions["open_automation_import"]() == {"cancelled": True}
    assert functions["save_automation_export"](content, filename) == {"cancelled": True}
    count = len(calls)
    current_url[0] = "https://untrusted.example/"
    assert "error" in functions["open_automation_import"]()
    assert "error" in functions["save_automation_export"](content, filename)
    assert len(calls) == count


@pytest.mark.asyncio
async def test_transfer_http_preview_apply_and_no_partial_writes(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as managers
    from testApparatus.test_nodus_settings_schema_writes import _build_app
    app, *_ = await _build_app(tmp_path, monkeypatch)
    _, _, content = source_document(tmp_path)
    class Manager(AutomationManager):
        def __init__(self, _base="automation_settings"):
            super().__init__(str(tmp_path / "api-target"))
    monkeypatch.setattr(managers, "AutomationManager", Manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"sec-fetch-site": "same-origin"}) as client:
        preview = await client.post("/advanced/automations/import/preview", content=content)
        assert preview.json() == {"automation_count": 1, "group_count": 4}
        assert not Manager().load("__all__")["Advanced"]
        batch = {"X-Automation-Import-Batch": str(uuid.uuid4())}
        imported = await client.post("/advanced/automations/import", content=content, headers=batch)
        assert imported.status_code == 200, imported.text
        assert (await client.post("/advanced/automations/import", content=content, headers=batch)).json() == imported.json()
        exported = await client.get("/advanced/automations/export")
        assert exported.status_code == 200
        document = parse_document(exported.json()["content"])
        assert len(document["automations"]) == 1 and document["automations"][0]["enabled"] is False
        before = Manager().get_storage_path().read_bytes()
        document["automations"][0]["script"]["conditions"][0]["value"] = "private parser text"
        rejected = await client.post("/advanced/automations/import", content=json.dumps(document), headers=batch)
        assert rejected.status_code == 400 and "private parser text" not in rejected.text
        assert Manager().get_storage_path().read_bytes() == before
        assert (await client.post("/advanced/automations/import", content=content)).status_code == 400
        assert (await client.post("/advanced/automations/import/preview", content=b'x' * (MAX_BYTES + 1))).status_code == 400
