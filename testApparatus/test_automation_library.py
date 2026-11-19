"""Verify library imports, provenance, stage gates, and atomic persistence.

Examples have no horticultural defaults. Tests use explicit synthetic bindings
and dates to exercise unit conversion, calendar gating, and batch retries.
"""
import copy
from datetime import datetime, timezone
import json
import uuid

import pytest

from sensorius.saiAutomationLibrary import catalog, normalize_stage_window, prepare_import
from sensorius.saiAutomationManager import AutomationManager, automation_position
from sensorius.biodynamic_calendar.storage_validation import _normalize_planting
from testApparatus.test_automation_evaluation import make_evaluator


def selection():
    return {"batch_id": str(uuid.uuid4()), "group": "Greenhouse", "entries": [
        {"crop_id": "greenhouse-lettuce", "template_id": "temperature-low", "stage": "seedling",
         "sensor": "climate-test", "metric": "Temperature", "unit": "°F", "value": 68, "hyst": 1.8,
         "start": "2026-12-01", "end": "2026-12-10"},
        {"crop_id": "greenhouse-lettuce", "template_id": "bd-tending", "stage": "seedling"},
    ]}


def test_catalog_covers_every_environment_part_and_stage():
    data = catalog()
    assert len(data['crops']) == 12
    for environment in data['environments']:
        assert {crop['part'] for crop in data['crops'] if crop['environment'] == environment} == set(data['parts'])
    assert list(data['stages']) == ['seedling', 'immature', 'mature', 'harvest']
    assert all('value' not in template for template in data['templates'])


def test_batch_import_disabled_normalized_independent_and_idempotent(tmp_path):
    payload = selection()
    prepared = prepare_import(payload, timezone='UTC', plantings=[])
    script = json.loads(prepared[0]['script_json'])
    assert script['conditions'][1]['value'] == 20
    assert script['conditions'][1]['hyst'] == pytest.approx(1)
    assert script['conditions'][0]['calendar_kind'] == 'date_range'
    mgr = AutomationManager(str(tmp_path))
    mgr.upsert_advanced_rule('hub', 'original', enabled=True, script={'name': 'original', 'actions': []})
    ids = mgr.import_library_rules('Greenhouse', prepared)
    assert len(set(ids)) == 2
    saved = mgr.load('hub')['Advanced']
    assert saved['original']['enabled']
    assert all(saved[key]['enabled'] is False and json.loads(saved[key]['script_json'])['enabled'] is False for key in ids)
    mgr.upsert_advanced_rule('hub', ids[0], enabled=False, script={**script, 'name': 'Edited copy'})
    assert mgr.import_library_rules('Greenhouse', prepared) == ids
    saved = AutomationManager(str(tmp_path)).load('hub')['Advanced']
    assert len(saved) == 3
    assert json.loads(saved[ids[0]]['script_json'])['name'] == 'Edited copy'
    assert json.loads(saved[ids[0]]['library_json'])['template_id'] == 'temperature-low'
    assert json.loads(saved[ids[1]]['script_json'])['conditions'][0]['calendar_event'] == 'water'
    before = mgr.get_storage_path().read_bytes()
    conflicting = copy.deepcopy(prepared)
    conflicting[1]['library_json'] = json.dumps({'digest': 'changed'})
    with pytest.raises(ValueError):
        mgr.import_library_rules('Greenhouse', conflicting)
    with pytest.raises(ValueError):
        mgr.import_library_rules('Removed group', prepared)
    assert mgr.get_storage_path().read_bytes() == before


def test_import_appends_after_existing_unpositioned_rules(tmp_path):
    mgr = AutomationManager(str(tmp_path))
    mgr.upsert_advanced_rule('hub', 'z-existing', enabled=True, script={'actions': []})
    mgr.arrange_rule('hub', 'z-existing', 'Greenhouse')
    ids = mgr.import_library_rules('Greenhouse', prepare_import(selection(), timezone='UTC', plantings=[]))
    saved = mgr.load('hub')['Advanced']
    assert automation_position(saved['z-existing']) < automation_position(saved[ids[0]]) < automation_position(saved[ids[1]])


def test_invalid_batch_does_not_write_and_calendar_planting_is_resolved(tmp_path):
    payload = selection()
    payload['entries'][1]['planting_id'] = 'test-planting'
    planting = {'id': 'test-planting', 'name': 'Lettuce bed', 'plant_part': 'Leaf', 'environment': 'Greenhouse',
                'stage_schedule': {'seedling': {'start': '2026-12-02', 'end': '2026-12-09'}}}
    prepared = prepare_import(payload, timezone='UTC', plantings=[planting])
    script = json.loads(prepared[1]['script_json'])
    assert script['conditions'][0]['calendar_start'] == '2026-12-02'
    assert json.loads(prepared[1]['library_json'])['planting_id'] == 'test-planting'
    with pytest.raises(ValueError, match='no longer exists'):
        prepare_import(payload, timezone='UTC', plantings=[])
    payload['entries'][0]['value'] = float('nan')
    with pytest.raises(ValueError):
        prepare_import(payload, timezone='UTC', plantings=[planting])


@pytest.mark.parametrize('day,matched', [('2026-11-30', False), ('2026-12-01', True), ('2026-12-09', True), ('2026-12-10', False)])
def test_date_range_evaluation_is_half_open(day, matched):
    at = datetime.fromisoformat(day + 'T12:00:00').replace(tzinfo=timezone.utc).timestamp()
    evaluator = make_evaluator(epoch=lambda: at)
    condition = {'type': 'calendar', 'calendar_kind': 'date_range', 'calendar_start': '2026-12-01',
                 'calendar_end': '2026-12-10', 'calendar_timezone': 'UTC'}
    assert evaluator.evaluate(condition, '') is matched
    condition['calendar_timezone'] = 'missing-zone'
    assert evaluator.evaluate(condition, '') is False


def test_planting_stage_schedule_round_trip_and_overlap_rejection(tmp_path):
    from sensorius.saiDataLogger import saiDataLogger
    raw = {'name': 'Lettuce', 'start_date': '2026-12-01', 'environment': 'Greenhouse',
           'stage_schedule': {'seedling': {'start': '2026-12-01', 'end': '2026-12-10'}}}
    planting, error = _normalize_planting(raw)
    assert not error
    with saiDataLogger(str(tmp_path / 'plantings.db')) as logger:
        assert logger.save_biodynamic_planting(planting)
        assert logger.get_biodynamic_plantings()[0]['stage_schedule'] == raw['stage_schedule']
    raw['stage_schedule']['immature'] = {'start': '2026-12-09', 'end': '2026-12-19'}
    assert _normalize_planting(raw)[0] is None
    for start, end in [('', '2026-12-10'), ('2026-12-10', '2026-12-01'), ('bad', '2026-12-10')]:
        with pytest.raises(ValueError):
            normalize_stage_window(start, end)


@pytest.mark.asyncio
async def test_library_http_import_and_date_range_editor_round_trip(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as managers
    from testApparatus.test_nodus_settings_schema_writes import _build_app
    app, *_ = await _build_app(tmp_path, monkeypatch)

    class Manager(AutomationManager):
        def __init__(self, _base='automation_settings'):
            super().__init__(str(tmp_path / 'automation-library'))

    monkeypatch.setattr(managers, 'AutomationManager', Manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test', headers={'sec-fetch-site': 'same-origin'}) as client:
        assert (await client.get('/advanced/automation-library')).status_code == 200
        payload = selection()
        response = await client.post('/advanced/automation-library/import', json=payload)
        assert response.status_code == 200, response.text
        ids = response.json()['rule_ids']
        assert (await client.post('/advanced/automation-library/import', json=payload)).json()['rule_ids'] == ids
        rules = (await client.get('/advanced/automations', params={'switch_id': '__all__'})).json()['items']
        rule = next(rule for rule in rules if rule['rule_id'] == ids[0])
        script = json.loads(rule['script_json'])
        saved = await client.post('/submit-advanced-trigger', json={'switch_id': '__system__', 'rule_id': ids[0], 'enabled': False, 'script_json': json.dumps(script)})
        assert saved.status_code == 200, saved.text
        assert Manager().load('hub')['Advanced'][ids[0]]['library_json']
        assert (await client.post('/advanced/automation-library/import', content=b'x' * 131073)).status_code == 413
        payload['entries'][0]['value'] = None
        assert (await client.post('/advanced/automation-library/import', json=payload)).status_code == 400


def test_stage_start_reminder_uses_resolved_midnight_and_requires_dates():
    payload = selection()
    payload['entries'] = [{**payload['entries'][0], 'template_id': 'stage-start'}]
    rules = prepare_import(payload, timezone='America/Denver', plantings=[])
    condition = json.loads(rules[0]['script_json'])['conditions'][0]
    assert condition['calendar_at'] == '2026-12-01T00:00:00-07:00'
    assert condition['calendar_kind'] == 'date_time'
    payload['entries'][0].update(start='', end='')
    with pytest.raises(ValueError, match='require a stage window'):
        prepare_import(payload, timezone='UTC', plantings=[])


@pytest.mark.asyncio
async def test_calendar_api_preserves_stage_fields_for_older_clients(tmp_path):
    from fastapi import FastAPI, APIRouter
    from httpx import ASGITransport, AsyncClient
    from sensorius.saiBiodynamicCalendarApp import register_biodynamic_calendar_routes
    from sensorius.saiDataLogger import saiDataLogger
    from testApparatus.test_integrated_biodynamic_calendar import _Settings

    with saiDataLogger(str(tmp_path / 'calendar.db')) as logger:
        app, router = FastAPI(), APIRouter()
        service = register_biodynamic_calendar_routes(router, app=app, settings=_Settings(), data_logger=logger)
        service._legacy_import_done = True
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            body = {'id': 'bed', 'name': 'Lettuce', 'start_date': '2026-12-01', 'environment': 'Greenhouse',
                    'stage_schedule': {'seedling': {'start': '2026-12-01', 'end': '2026-12-09'}}}
            response = await client.post('/api/biodynamic-calendar-app/planting', json=body)
            assert response.status_code == 200, response.text
            previous = dict(body)
            del body['stage_schedule']
            del body['environment']
            body['name'] = 'Edited lettuce'
            response = await client.post('/api/biodynamic-calendar-app/planting', json=body)
            assert response.status_code == 200, response.text
            saved = response.json()['planting']
            assert saved['stage_schedule'] == previous['stage_schedule']
            assert saved['environment'] == 'Greenhouse'


def test_explicit_stage_windows_drive_calendar_guidance_without_guessing():
    from datetime import date
    from sensorius.biodynamic_calendar.hints import _planting_stage_for_day
    planting = {'name': 'Lettuce', 'start_date': '2026-12-01', 'stage_schedule': {
        'seedling': {'start': '2026-12-01', 'end': '2026-12-10'},
        'immature': {'start': '2026-12-10', 'end': '2026-12-20'},
    }}
    assert _planting_stage_for_day(planting, date(2026, 12, 9)) == 'seedling'
    assert _planting_stage_for_day(planting, date(2026, 12, 10)) == 'immature'
    assert _planting_stage_for_day(planting, date(2026, 12, 20)) == 'general'
