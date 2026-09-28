"""Exercise NWS location gating, CAP lifecycle and persistent actor deduplication.

Network traffic and physical switch/email actors are replaced with local fakes;
SQLite persistence is real so restarts exercise the event receipt contract.
"""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiWeatherAlerts import WeatherAlertService
import sensorius.saiWeatherAlerts as alerts_module


class Settings:
    provider = "us"
    lat = 39.7
    lon = -104.9

    def get_setting(self, section, key, default=None):
        return self.provider if (section, key) == ("WeatherForecast", "PROVIDER") else default

    def get_section(self, *_args, **_kwargs):
        return {}

    def resolve_astral_location(self, **_kwargs):
        return {"lat": self.lat, "lon": self.lon, "tz": "America/Denver"}


def feature(identifier="event-1", **changes):
    now = datetime.now(timezone.utc)
    props = {
        "id": identifier, "status": "Actual", "messageType": "Alert",
        "event": "Severe Thunderstorm Warning", "category": ["Met"],
        "severity": "Severe", "certainty": "Observed", "areaDesc": "Test County",
        "sent": now.isoformat(), "effective": (now - timedelta(minutes=5)).isoformat(),
        "onset": now.isoformat(), "ends": (now + timedelta(hours=2)).isoformat(),
        "expires": (now + timedelta(hours=3)).isoformat(),
        "headline": "Damaging winds expected", "description": "Winds to 70 mph and large hail.",
        "instruction": "Move indoors.", "references": [], "parameters": {},
    }
    props.update(changes)
    return {"type": "Feature", "id": identifier, "properties": props}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = saiDataLogger(tmp_path / "weather.db")
    service = WeatherAlertService(settings=Settings(), data_logger=logger)
    service._location = (39.7, -104.9)
    logger.weather_alert_service = service
    yield service
    logger.close()


def ingest(service, *features):
    service.ingest({"type": "FeatureCollection", "features": list(features)})


def test_event_updates_restarts_and_cancellation(service):
    initial = feature()
    ingest(service, initial)
    event = service.snapshot()["events"][0]
    service.mark_triggered([event], "actor-a")
    restarted = WeatherAlertService(settings=service.settings, data_logger=service.data_logger)
    restarted._location = service._location
    update = feature("event-2", messageType="Update", references=[{"identifier": "event-1"}])
    ingest(restarted, update)
    assert restarted.snapshot()["active"]
    assert restarted.pending(restarted.snapshot()["events"], "actor-a") == []
    assert len(restarted.pending(restarted.snapshot()["events"], "actor-b")) == 1
    assert "70 mph" in restarted.snapshot()["message"]
    assert "Starts:" in restarted.snapshot()["message"]
    assert "Ends:" in restarted.snapshot()["message"]
    assert "2h 0m" in restarted.snapshot()["message"]
    cancel = feature("event-3", messageType="Cancel", references=[{"identifier": "event-2"}])
    ingest(restarted, cancel)
    assert not restarted.snapshot()["active"]


def test_vtec_identity_survives_update_without_references(service):
    ingest(service, feature(parameters={"VTEC": ["/O.NEW.KBOU.SV.W.0012.260928T1200Z-260928T1500Z/"]}))
    service.mark_triggered(service.snapshot()["events"], "actor")
    ingest(service, feature("new-cap-id", parameters={"VTEC": ["/O.CON.KBOU.SV.W.0012.260928T1200Z-260928T1700Z/"]}))
    assert service.pending(service.snapshot()["events"], "actor") == []


@pytest.mark.parametrize("changes", [
    {"status": "Test"}, {"event": "Wind Advisory"}, {"event": "Special Weather Statement"},
    {"category": ["Safety"], "event": "Civil Danger Warning"},
    {"expires": "2020-01-01T00:00:00Z"}, {"ends": "2020-01-01T00:00:00Z"},
    {"effective": "2099-01-01T00:00:00Z"},
])
def test_nonqualifying_alerts_do_not_activate(service, changes):
    ingest(service, feature(**changes))
    assert not service.snapshot()["active"]


def test_new_event_and_simultaneous_events_are_independent(service):
    ingest(service, feature("a"), feature("b", event="Tornado Watch"))
    service.mark_triggered([service.snapshot()["events"][0]], "actor")
    assert len(service.pending(service.snapshot()["events"], "actor")) == 1
    ingest(service)
    assert not service.snapshot()["active"]
    ingest(service, feature("c"))
    assert len(service.pending(service.snapshot()["events"], "actor")) == 1
    service.settings.provider = "open_meteo"
    assert not service.snapshot()["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,state,status", [("met_no", "CO", 200), ("none", "CO", 200), ("us", "ON", 200), ("us", "CO", 404), ("us", "CO", 200)])
async def test_only_validated_us_astral_points_fetch_alerts(service, monkeypatch, provider, state, status):
    requests = []
    service.settings.provider = provider
    service._location = None

    def handle(request):
        requests.append(request)
        if "/points/" in request.url.path:
            return httpx.Response(status, json={"properties": {"relativeLocation": {"properties": {"state": state}}, "forecastZone": "https://api.weather.gov/zones/forecast/COZ040"}})
        return httpx.Response(200, json={"features": [feature()]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(alerts_module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    await service.refresh()
    expected = provider == "us" and state == "CO" and status == 200
    assert any(request.url.path == "/alerts/active" for request in requests) == expected
    assert service.snapshot()["active"] == expected
    if expected:
        assert requests[-1].url.params["point"] == "39.7000,-104.9000"
        await service.refresh()
        assert sum("/points/" in request.url.path for request in requests) == 1


def controller(service, actions, extra_conditions=None):
    from testApparatus.test_sai_switch_controller import _make_controller
    ctrl = _make_controller()
    ctrl.data_logger = service.data_logger
    script = {"name": "Protect greenhouse", "enabled": True,
              "conditions": [{"type": "severe_weather"}] + (extra_conditions or []), "actions": actions}
    ctrl._load_triggers_dict = lambda: {"Advanced": {"weather-rule": {"enabled": True, "script_json": copy.deepcopy(script)}}}
    ctrl._persist_advanced_runtime_state = lambda: None
    ctrl._recover_advanced_revert_from_history = lambda *_args: False
    ctrl.get_state = lambda label: ctrl.last_state[label]
    ctrl.calls = []

    def set_state(label, value, **kwargs):
        ctrl.calls.append((label, value))
        ctrl.last_state[label] = value
        return True

    ctrl.set_state = set_state
    return ctrl


def test_switch_actor_fires_once_and_can_revert_at_end(service):
    action = {"type": "switch", "switch_key": "sw1::Fan", "set": True, "revert_action": "previous_state"}
    ctrl = controller(service, [action])
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({})
    ctrl._evaluate_and_apply_advanced({})
    assert ctrl.calls == [("Fan", True)]
    ingest(service)
    ctrl._evaluate_and_apply_advanced({})
    assert ctrl.calls == [("Fan", True), ("Fan", False)]
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({})
    assert len(ctrl.calls) == 2
    restarted = controller(service, [action])
    restarted._evaluate_and_apply_advanced({})
    assert restarted.calls == []


def test_alert_and_notify_get_details_once_even_after_restart(service):
    actions = [{"type": "none", "executor_switch_id": "sw1"},
               {"type": "notify", "to": "test@example.com", "executor_switch_id": "sw1"}]
    ctrl = controller(service, actions)
    toasts, emails = [], []
    ctrl._broadcast_automation_notification = lambda **kw: toasts.append(kw) or True
    ctrl.email_delivery_service = SimpleNamespace(persisted_automation_state=lambda *a, **k: False,
                                                  enqueue_automation_transition=lambda **kw: emails.append(kw) or True)
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({})
    ctrl._evaluate_and_apply_advanced({})
    assert len(toasts) == len(emails) == 1
    assert "Severe Thunderstorm Warning" in emails[0]["subject"]
    assert "Starts:" in emails[0]["body"] and "70 mph" in emails[0]["body"]
    restarted = controller(service, actions)
    restarted._broadcast_automation_notification = ctrl._broadcast_automation_notification
    restarted.email_delivery_service = ctrl.email_delivery_service
    restarted._evaluate_and_apply_advanced({})
    assert len(toasts) == len(emails) == 1
    ingest(service, feature("another-event", event="Tornado Warning"))
    restarted._evaluate_and_apply_advanced({})
    assert len(toasts) == len(emails) == 2
    ingest(service)
    restarted._evaluate_and_apply_advanced({})
    assert len(emails) == 2  # no synthetic recovery notification for event actors


def test_weather_actor_waits_for_other_conditions_without_consuming_event(service):
    ctrl = controller(service, [{"type": "switch", "switch_key": "sw1::Fan", "set": True}],
                      [{"type": "sensor", "sensor": "s1", "metric": "Temperature", "op": ">", "value": 25}])
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({"s1": {"Temperature": 10}})
    assert ctrl.calls == []
    ctrl._evaluate_and_apply_advanced({"s1": {"Temperature": 30}})
    assert ctrl.calls == [("Fan", True)]


@pytest.mark.asyncio
async def test_location_change_and_invalid_astral_clear_previous_events(service, monkeypatch):
    ingest(service, feature())
    service.settings.lat = "not-a-coordinate"
    await service.refresh()
    assert not service.snapshot()["active"]
    assert service.snapshot()["reason"] == "location_unavailable"
    service.settings.lat, service.settings.lon = 51.5, -0.1
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(404, json={})

    monkeypatch.setattr(alerts_module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    await service.refresh()
    assert not service.snapshot()["active"]
    assert len(requests) == 1
    assert "/points/51.5000,-0.1000" in str(requests[0].url)


def test_delayed_weather_switch_and_failed_delivery_do_not_consume_early(service, monkeypatch):
    action = {"type": "switch", "switch_key": "sw1::Fan", "set": True, "delay_s": 10}
    ctrl = controller(service, [action])
    ingest(service, feature())
    clock = [100.0]
    import sensorius.saiSwitch as switch_module
    monkeypatch.setattr(switch_module.time, "monotonic", lambda: clock[0])
    ctrl._evaluate_and_apply_advanced({})
    assert ctrl.calls == []
    assert not next(iter(service.records.values()))["triggered"]
    clock[0] = 111.0
    ctrl._evaluate_and_apply_advanced({})
    ctrl._evaluate_and_apply_advanced({})
    assert ctrl.calls == [("Fan", True)]

    notify = controller(service, [{"type": "notify", "to": "test@example.com", "executor_switch_id": "sw1"}])
    queued = []
    accepted = [False]
    notify.email_delivery_service = SimpleNamespace(
        persisted_automation_state=lambda *a, **kw: False,
        enqueue_automation_transition=lambda **kw: queued.append(kw) or accepted[0],
    )
    notify._evaluate_and_apply_advanced({})
    accepted[0] = True
    notify._evaluate_and_apply_advanced({})
    notify._evaluate_and_apply_advanced({})
    assert len(queued) == 2


def test_unavailable_alert_broadcaster_retries_until_accepted(service):
    ctrl = controller(service, [{"type": "none", "executor_switch_id": "sw1"}])
    ingest(service, feature())
    accepted = [False]
    calls = []
    ctrl._broadcast_automation_notification = lambda **kw: calls.append(kw) or accepted[0]
    ctrl._evaluate_and_apply_advanced({})
    accepted[0] = True
    ctrl._evaluate_and_apply_advanced({})
    ctrl._evaluate_and_apply_advanced({})
    assert len(calls) == 2


def test_pending_dashboard_alert_survives_restart_update_and_dismissal(service):
    ingest(service, feature())
    event = service.snapshot()['events'][0]
    payload = {'type': 'automation_notification', 'weather_event_ids': [event['event_key']],
               'trigger_conditions': [event['message']]}
    service.queue_alert(payload, 'actor-a')
    assert not service.pending([event], 'actor-a')
    restarted = WeatherAlertService(settings=service.settings, data_logger=service.data_logger)
    assert restarted.pending_alerts() == []  # Wait for location/feed validation.
    restarted._location = service._location
    ingest(restarted, feature('update-1', references=[{'identifier': 'event-1'}]))
    assert len(restarted.pending_alerts()) == 1
    alert_id = restarted.pending_alerts()[0]['weather_alert_id']
    assert restarted.dismiss_alert(alert_id)
    again = WeatherAlertService(settings=service.settings, data_logger=service.data_logger)
    again._location = service._location
    ingest(again, feature())
    assert again.pending_alerts() == []
    assert not again.pending(again.snapshot()['events'], 'actor-a')


def test_pending_dashboard_alert_stops_on_cancellation(service):
    ingest(service, feature())
    service.queue_alert({'weather_event_ids': ['event-1']}, 'actor-a')
    assert service.pending_alerts()
    ingest(service, feature(messageType='Cancel'))
    assert service.pending_alerts() == []


def test_alert_actor_queues_without_connected_dashboard(service, monkeypatch):
    from sensorius import saiWebRoutes
    monkeypatch.setattr(saiWebRoutes, 'app', SimpleNamespace(state=SimpleNamespace()), raising=False)
    ctrl = controller(service, [{'type': 'none', 'executor_switch_id': 'sw1'}])
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({})
    ctrl._evaluate_and_apply_advanced({})
    pending = service.pending_alerts()
    assert len(pending) == 1
    assert 'Severe Thunderstorm Warning' in '\n'.join(pending[0]['trigger_conditions'])


@pytest.mark.parametrize('save_mode', ['editor', 'toggle', 'switch'])
def test_saved_reenable_rearms_weather_actor_only_once(service, tmp_path, save_mode):
    import json
    from sensorius.saiAutomationManager import AutomationManager
    manager = AutomationManager(str(tmp_path / 'automations'))
    ctrl = controller(service, [{'type': 'none', 'executor_switch_id': 'sw1'}])
    script = ctrl._load_triggers_dict()['Advanced']['weather-rule']['script_json']
    emails = []
    ctrl.email_delivery_service = SimpleNamespace(persisted_automation_state=lambda *a, **k: False,
        enqueue_automation_transition=lambda **kw: emails.append(kw) or True)
    script['actions'].append({'type': 'notify', 'to': 'test@example.com', 'executor_switch_id': 'sw1'})
    # A switch action also permits exercising the switch-card enable/disable API.
    script['actions'].append({'type': 'switch', 'switch_key': 'sw1::Fan', 'set': True})
    manager.upsert_advanced_rule('sw1', 'weather-rule', enabled=True, script=script)
    ctrl._load_triggers_dict = lambda: {'Advanced': manager.load_runtime_advanced('sw1')}
    ingest(service, feature())
    ctrl._evaluate_and_apply_advanced({})
    first = service.pending_alerts()[0]['weather_alert_id']
    assert len(emails) == 1
    service.dismiss_alert(first)

    def save(enabled):
        if save_mode == 'editor':
            manager.upsert_advanced_rule('sw1', 'weather-rule', enabled=enabled, script=script)
        elif save_mode == 'toggle':
            manager.set_rule_enabled('sw1', 'Advanced', 'weather-rule', enabled)
        else:
            manager.set_advanced_enabled_for_switch_key('sw1', 'sw1::Fan', enabled)

    save(True)  # Saving an already-enabled automation must not rearm it.
    ctrl._evaluate_and_apply_advanced({})
    assert service.pending_alerts() == []
    save(False)
    save(True)  # No evaluator tick while disabled is needed.
    ctrl._evaluate_and_apply_advanced({})
    pending = service.pending_alerts()
    assert len(pending) == 1
    assert pending[0]['weather_alert_id'] != first
    assert len(emails) == 2
    assert emails[0]['revision'] != emails[1]['revision']
    service.dismiss_alert(pending[0]['weather_alert_id'])
    save(True)
    ingest(service, feature('updated', references=[{'identifier': 'event-1'}]))
    ctrl._evaluate_and_apply_advanced({})
    assert service.pending_alerts() == []
    restarted = controller(service, script['actions'])
    restarted.email_delivery_service = ctrl.email_delivery_service
    restarted._load_triggers_dict = lambda: {'Advanced': AutomationManager(str(tmp_path / 'automations')).load_runtime_advanced('sw1')}
    restarted._evaluate_and_apply_advanced({})
    assert service.pending_alerts() == []
    assert len(emails) == 2
    stored = json.loads(manager.load('sw1')['Advanced']['weather-rule']['script_json'])
    revision = stored['_weather_rearm_revision']
    # An editor payload cannot reset the server-owned arming identity.
    script['_weather_rearm_revision'] = 'forged'
    manager.upsert_advanced_rule('sw1', 'weather-rule', enabled=True, script=script)
    assert json.loads(manager.load('sw1')['Advanced']['weather-rule']['script_json'])['_weather_rearm_revision'] == revision
