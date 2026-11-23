"""Verify library imports, provenance, stage gates, and atomic persistence.

Tests use synthetic bindings and dates to exercise starter and recipe defaults,
unit conversion, calendar gating, and batch retries.
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
    assert len([crop for crop in data['crops'] if not crop.get('recipe')]) == 12
    for environment in data['environments']:
        assert {crop['part'] for crop in data['crops'] if crop['environment'] == environment} == set(data['parts'])
    assert list(data['stages']) == ['seedling', 'immature', 'mature', 'harvest']
    assert all('default_value' not in template for template in data['templates'] if not template.get('recipe_only'))


@pytest.mark.parametrize('stage,expected', [
    ('incubation', {'Temperature low alert': ('<', 65), 'Temperature high alert': ('>', 70)}),
    ('pinning', {'Temperature low alert': ('<', 62), 'Temperature high alert': ('>', 68),
                 'Turn on humidifier': ('<', 90), 'VPD low alert': ('<', 0.190),
                 'VPD high alert': ('>', 0.234), 'Turn on fan (scheduled)': None, 'Turn on lights': None}),
    ('fruiting', {'Temperature low alert': ('<', 62), 'Temperature high alert': ('>', 68),
                  'Turn on humidifier': ('<', 85), 'Relative humidity high alert': ('>', 90),
                  'VPD low alert': ('<', 0.190), 'VPD high alert': ('>', 0.351),
                  'Turn on fan (CO₂)': ('>=', 800), 'Turn on fan (scheduled)': None, 'Turn on lights': None}),
])
def test_oyster_recipe_presets_import_as_alerts(stage, expected, tmp_path):
    from sensorius.saiAutomationTransfer import export_document, parse_document

    data = catalog()
    crop = next(crop for crop in data['crops'] if crop['id'] == 'indoor-oyster-mushroom')
    assert (crop['environment'], crop['part']) == ('Indoor', 'Fruit')
    templates = {template['id']: template for template in data['templates']}
    entries = []
    for key in crop['recipe'][stage]['templates']:
        template = templates[key]
        entry = dict(crop_id=crop['id'], stage=stage, template_id=key, start='2026-12-01', end='2026-12-10')
        if template['kind'] == 'sensor':
            entry.update(sensor='grow-room', metric=template['metrics'][-1])
        else:
            entry.update(time_start='08:00', time_end='20:00')
        entries.append(entry)

    rules = prepare_import(dict(batch_id=str(uuid.uuid4()), entries=entries), timezone='America/Denver', plantings=[])
    assert len(rules) == len(expected)
    for rule in rules:
        script = json.loads(rule['script_json'])
        gate, condition = script['conditions']
        target = expected[script['name'].split(' / ')[-1]]
        assert not rule['enabled'] and not script['enabled']
        assert script['actions'] == [{'type': 'none', 'executor_switch_id': '__system__'}]
        assert gate['calendar_kind'] == 'date_range'
        assert gate['calendar_timezone'] == 'America/Denver'
        if target is None:
            assert condition == {'type': 'time', 'start': '08:00', 'end': '20:00'}
        else:
            assert (condition['op'], condition['value']) == target
        if condition.get('metric') == 'CO2':
            for ppm, active in ((799.9, False), (800, True), (801, True)):
                evaluator = make_evaluator(values_for_sensor=lambda _: {'CO2': ppm})
                assert evaluator.evaluate(condition, '') is active

    manager = AutomationManager(str(tmp_path))
    manager.import_library_rules('Indoor', rules)
    _, content = export_document(manager.load('hub'))
    assert len(parse_document(content)['automations']) == len(expected)


@pytest.mark.parametrize('changes,match', [
    ({'stage': 'incubation'}, 'Unknown stage or template'),
    ({'crop_id': 'indoor-tomato', 'stage': 'mature'}, 'Unknown crop, stage, or library template'),
    ({'time_start': '08:00:00'}, 'distinct start and end times'),
    ({'time_end': '08:00'}, 'distinct start and end times'),
    ({'time_end': '08:30Z'}, 'distinct start and end times'),
])
def test_oyster_schedule_validates_stage_and_times(changes, match):
    entry = dict(crop_id='indoor-oyster-mushroom', template_id='mushroom-lights', stage='pinning',
                 time_start='08:00', time_end='20:00')
    with pytest.raises(ValueError, match=match):
        prepare_import(dict(batch_id=str(uuid.uuid4()), entries=[entry | changes]), timezone='UTC', plantings=[])


def cannabis_selection():
    data = catalog()
    crop = next(item for item in data['crops'] if item['id'] == 'indoor-cannabis')
    assert (crop['environment'], crop['part']) == ('Indoor', 'Flower')
    templates = {item['id']: item for item in data['templates']}
    entries = [
        {'crop_id': crop['id'], 'stage': stage, 'template_id': key,
         'sensor': 'drying-room', 'metric': templates[key]['metrics'][0]}
        for stage, spec in crop['recipe'].items() for key in spec['templates']
    ]
    return dict(batch_id=str(uuid.uuid4()), group='Indoor', drying_started_at='2027-03-13T14:30', entries=entries)


def test_cannabis_gates_use_elapsed_hours_across_daylight_saving(tmp_path):
    from sensorius.saiAutomationTransfer import export_document, parse_document

    payload = cannabis_selection()
    rules = prepare_import(payload, timezone='America/Denver', plantings=[])
    scripts = [json.loads(rule['script_json']) for rule in rules]
    assert len(scripts) == 10
    assert all(not script['enabled'] for script in scripts)
    assert all(script['actions'] == [{'type': 'none', 'executor_switch_id': '__system__'}] for script in scripts)
    for instant, target in [
        ('2027-03-13T21:29:59+00:00', None), ('2027-03-13T21:30:00+00:00', 40),
        ('2027-03-15T21:29:59+00:00', 40), ('2027-03-15T21:30:00+00:00', 60),
        ('2027-03-23T21:29:59+00:00', 60), ('2027-03-23T21:30:00+00:00', 60),
        ('2027-05-01T00:00:00+00:00', 60),
    ]:
        evaluator = make_evaluator(epoch=lambda: datetime.fromisoformat(instant).timestamp())
        active = [script for script in scripts if evaluator.evaluate(script['conditions'][0], '')]
        if target is None:
            assert not active
            continue
        assert len(active) == 4
        phase = 'Curing' if instant >= '2027-03-23T21:30:00+00:00' else 'Drying'
        assert all(f' / {phase} / ' in script['name'] for script in active)
        assert {(cond['metric'], cond['op'], cond['value']) for script in active for cond in script['conditions'][1:]} == {
            ('Temperature', '<', 18), ('Temperature', '>', 21),
            ('Rel-Humidity', '<', target), ('Rel-Humidity', '>', target),
        }
    manager = AutomationManager(str(tmp_path))
    manager.import_library_rules('Indoor', rules)
    _, content = export_document(manager.load('hub'))
    assert len(parse_document(content)['automations']) == 10


@pytest.mark.parametrize('start', ['', '2027-03-13', '2027-03-14T02:30', '2027-11-07T01:30'])
def test_cannabis_requires_unambiguous_drying_start(start):
    payload = cannabis_selection()
    payload['drying_started_at'] = start
    with pytest.raises(ValueError, match='valid drying start'):
        prepare_import(payload, timezone='America/Denver', plantings=[])


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

        oyster = {'crop_id': 'indoor-oyster-mushroom', 'stage': 'fruiting', 'sensor': 'climate-test'}
        response = await client.post('/advanced/automation-library/import', json={
            'batch_id': str(uuid.uuid4()), 'group': 'Indoor', 'entries': [
                {**oyster, 'template_id': 'mushroom-temperature-low', 'metric': 'Temperature', 'unit': '°C'},
                {**oyster, 'template_id': 'mushroom-fruiting-fan', 'metric': 'CO2'},
            ],
        })
        assert response.status_code == 200, response.text
        temperature_id, fan_id = response.json()['rule_ids']
        advanced = Manager().load('hub')['Advanced']
        assert json.loads(advanced[temperature_id]['script_json'])['conditions'][0]['value'] == pytest.approx(50 / 3)
        fan_script = json.loads(advanced[fan_id]['script_json'])
        saved = await client.post('/submit-advanced-trigger', json={'switch_id': '__system__', 'rule_id': fan_id,
                                  'enabled': False, 'script_json': json.dumps(fan_script)})
        assert saved.status_code == 200, saved.text
        fan_script = json.loads(Manager().load('hub')['Advanced'][fan_id]['script_json'])
        assert fan_script['conditions'][0]['op'] == '>='

        payload = cannabis_selection()
        response = await client.post('/advanced/automation-library/import', json=payload)
        assert response.status_code == 200, response.text
        rule_id = response.json()['rule_ids'][-1]
        script = json.loads(Manager().load('hub')['Advanced'][rule_id]['script_json'])
        script['conditions'][0]['calendar_end'] = '2027-04-01T11:00:00-06:00'
        saved = await client.post('/submit-advanced-trigger', json={'switch_id': '__system__', 'rule_id': rule_id,
                                  'enabled': False, 'script_json': json.dumps(script)})
        assert saved.status_code == 200, saved.text
        stored = json.loads(Manager().load('hub')['Advanced'][rule_id]['script_json'])['conditions'][0]
        assert stored['calendar_kind'] == 'datetime_range'
        assert stored['calendar_end'] == '2027-04-01T17:00:00+00:00'


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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,exception_type", [
    ("import", ValueError), ("import", TypeError), ("import", KeyError),
    ("groups", ValueError), ("arrange", ValueError),
])
async def test_automation_api_validation_does_not_expose_internal_exceptions(tmp_path, monkeypatch, operation, exception_type):
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiAutomationManager as managers
    from testApparatus.test_nodus_settings_schema_writes import _build_app

    app, *_ = await _build_app(tmp_path, monkeypatch)
    private_detail = "/private/runtime/automation_settings/automations.toml: internal parser detail"

    class Manager(AutomationManager):
        def __init__(self, _base="automation_settings"):
            super().__init__(str(tmp_path / "private-errors"))

        def import_library_rules(self, *args):
            raise exception_type(private_detail)

        def update_group(self, *args):
            raise exception_type(private_detail)

        def arrange_rule(self, *args):
            raise exception_type(private_detail)

    monkeypatch.setattr(managers, "AutomationManager", Manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        if operation == "import":
            response = await client.post("/advanced/automation-library/import", json=selection())
            expected = "Invalid library selection. Review sensors, units, targets, stage dates, planting links, and the destination group."
        elif operation == "groups":
            response = await client.post("/advanced/automations/groups", data={"group": "Nursery", "choice": "Add"})
            expected = "Unable to change group. Use a unique name, remove only empty groups, and keep at least one group."
        else:
            response = await client.post("/advanced/automations/arrange", data={"switch_id": "__all__", "rule_id": "a", "choice": "Indoor"})
            expected = "Invalid arrangement. Choose Up, Down, or a current automation group."
        assert response.status_code == 400
        assert response.json() == {"error": expected}
        assert private_detail not in response.text
