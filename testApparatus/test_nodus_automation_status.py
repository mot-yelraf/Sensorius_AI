import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiNodusAutomationStatus import (
    AVAILABILITY_SCHEMA,
    STATUS_SCHEMA,
    NodusAutomationStatusPublisher,
    build_status_channels,
)


class FakeManager:
    def __init__(self, rules):
        self.rules = rules

    def load_runtime_advanced(self, _hostname):
        return self.rules


class FakeIngest:
    def __init__(self):
        self.discovery_cache = {
            "avpd-abc": {
                "device_id": "avpd-abc",
                "switch": {
                    "switch_device_id": "switch-abc",
                    "channels": [
                        {"channel_id": "switch-abc-1"},
                        {"channel_id": "switch-abc-2"},
                    ],
                },
            }
        }
        self.nodus_switch_command_topics = {
            ("switch-abc", "switch-abc-1"): "farm/nodus/switch-abc-1/config/set",
            ("switch-abc", "switch-abc-2"): "farm/nodus/switch-abc-2/config/set",
        }
        self.nodus_switch_state_topics = {}
        self.published = []

    def publish_json(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))
        return True


def _rules(enabled=True):
    return {
        "Night Lights": {
            "enabled": enabled,
            "script_json": {
                "enabled": True,
                "actions": [
                    {"switch_key": "switch-abc::switch-abc-2", "set": True},
                    {"switch_key": "other::other-1", "set": False},
                ],
            },
        }
    }


def test_build_status_channels_includes_only_enabled_known_targets():
    assert build_status_channels(
        _rules(), "switch-abc", {"switch-abc-1", "switch-abc-2"}
    ) == {"switch-abc-2": ["Night Lights"]}
    assert build_status_channels(
        _rules(enabled=False), "switch-abc", {"switch-abc-2"}
    ) == {}


def test_publisher_uses_physical_device_topic_and_retained_primary_client():
    ingest = FakeIngest()
    publisher = NodusAutomationStatusPublisher(
        ingest,
        manager=FakeManager(_rules()),
        controller_id="sensorius-main",
    )

    assert publisher.publish_once(force=True, now=1000) == 2

    status_topic, status, status_options = ingest.published[0]
    assert status_topic == "farm/nodus/avpd-abc/automation/sensorius/status"
    assert status == {
        "schema": STATUS_SCHEMA,
        "controller": "sensorius",
        "controller_id": "sensorius-main",
        "updated_at": 1000,
        "channels": [
            {
                "channel_id": "switch-abc-2",
                "automations": ["Night Lights"],
                "enabled": True,
            }
        ],
    }
    assert status_options == {"qos": 0, "retain": True, "use_ha_client": False}
    assert ingest.published[1][1]["schema"] == AVAILABILITY_SCHEMA
    assert ingest.published[1][1]["status"] == "online"


def test_publisher_clears_removed_ownership_and_marks_offline():
    ingest = FakeIngest()
    manager = FakeManager(_rules())
    publisher = NodusAutomationStatusPublisher(ingest, manager=manager)
    publisher.publish_once(force=True, now=1000)
    manager.rules = _rules(enabled=False)

    assert publisher.publish_once(now=1005) == 1
    assert ingest.published[-1][1]["channels"] == []
    assert publisher.publish_offline(now=1010) == 1
    assert ingest.published[-1][1]["status"] == "offline"


def test_publisher_run_feeds_watchdog_each_scan(monkeypatch):
    class FakeSupervisor:
        def __init__(self):
            self.fed = []

        def feedthedogs(self, name):
            self.fed.append(name)

    async def cancel_after_first_scan(_seconds):
        raise asyncio.CancelledError

    supervisor = FakeSupervisor()
    publisher = NodusAutomationStatusPublisher(
        FakeIngest(),
        manager=FakeManager(_rules()),
        supervisor=supervisor,
    )
    monkeypatch.setattr(
        "saiNodusAutomationStatus.asyncio.sleep",
        cancel_after_first_scan,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(publisher.run())

    assert supervisor.fed == ["Nodus Automation Status"]
