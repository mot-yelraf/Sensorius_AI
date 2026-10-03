"""Verify preferred-unit input preserves canonical calibration and rule behavior.

Exercise conversion math and HTTP boundaries, including legacy requests and
unit annotations that must never reach Nodus or stored automation conditions.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from sensorius.saiInputUnits import (
    calibration_input_offsets, input_descriptor, native_input_value,
    normalize_condition_inputs,
)
from testApparatus.test_nodus_settings_schema_writes import (
    _build_app, _build_route_app_with_settings, _RouteFakeSaiSettings,
    _REAL_SENSOR_SETTINGS_MANAGER,
)


@pytest.mark.parametrize('metric,native,entered,unit,expected', [
    ('Temperature', '°C', 77, '°F', 25),
    ('Plant Temperature', '°C', 77, '°F', 25),
    ('Temperature_F', '°F', 25, '°C', 77),
    ('Plant Temperature_F', '°F', 25, '°C', 77),
    ('Outdoor Temperature', '°C', 77, '°F', 25),
])
def test_threshold_and_hysteresis_have_different_conversion(metric, native, entered, unit, expected):
    result = normalize_condition_inputs({'type': 'sensor', 'metric': metric,
        'value': entered, 'value_unit': unit, 'hyst': 1.8 if native == '°C' else 1,
        'hyst_unit': unit})
    assert result['value'] == pytest.approx(expected)
    assert result['hyst'] == pytest.approx(1 if native == '°C' else 1.8)
    assert 'value_unit' not in result and 'hyst_unit' not in result
    assert input_descriptor(metric, 'Metric')['native_unit'] == native


def test_unitless_legacy_rules_and_precision_are_unchanged():
    for metric in ('Temperature', 'Temperature_F', 'Unknown'):
        condition = {'metric': metric, 'value': 82.123456789, 'hyst': .123456789}
        assert normalize_condition_inputs(condition) == condition
    assert native_input_value(1.8, None, '°C', delta=True) == 1.8
    assert native_input_value(1000, 'ft', 'm') == 304.8


@pytest.mark.parametrize('unit,value', [('kelvin', 10), ('°F', float('inf')), ('°C', float('nan'))])
def test_bad_inputs_are_rejected(unit, value):
    with pytest.raises(ValueError):
        native_input_value(value, unit, '°C')
    with pytest.raises(ValueError):
        normalize_condition_inputs({'metric': 'Unknown', 'value': 77, 'value_unit': '°F'})


@pytest.mark.parametrize('key', ['Calibration.Device.TEMP_OFFSET', 'Calibration.System.TEMP_OFFSET',
                               'ambient_temp_offset', 'soil_temp_offset', 'Sensor.THP280_PLANT_TEMP_CAL'])
def test_temperature_offsets_are_deltas(key):
    assert calibration_input_offsets([{'key': key, 'value': -1.8, 'input_unit': '°F'}]) == [
        {'key': key, 'value': -1.0}]


@pytest.mark.asyncio
async def test_remote_calibration_converts_before_mqtt_and_shadow(tmp_path, monkeypatch):
    app, ingest, _, sensor_root, _ = await _build_app(tmp_path, monkeypatch)
    manager = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    sid = 'apvpd-test123'
    manager.save(sid, {'Sensor': {'TYPE': 'nodus', 'DEVICE': 'aqi', 'SENSOR_ID': sid}})
    ingest.meta_patches_by_message['test-1'] = {
        'schema': 'nodus-meta-patch/v1', 'device_id': sid, 'message_id': 'test-1',
        'source': 'calibration_set', 'updates': [
            {'section': 'Calibration.Device', 'key': 'TEMP_OFFSET', 'value': 1.0}]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/calibration/device/apply', json={
            'sensor_id': sid, 'device_kind': 'aqi', 'offsets': [
                {'key': 'Calibration.Device.TEMP_OFFSET', 'value': 1.8, 'input_unit': '°F'}]})
        assert response.status_code == 200, response.text
        assert ingest.calibration_commands[-1]['payload']['offsets'] == [
            {'key': 'Calibration.Device.TEMP_OFFSET', 'value': 1.0}]
        assert manager.load(sid)['Calibration']['Device']['TEMP_OFFSET'] == 1.0
        before = len(ingest.calibration_commands)
        response = await client.post('/calibration/device/apply', json={
            'sensor_id': sid, 'device_kind': 'aqi', 'offsets': [
                {'key': 'Calibration.Device.RH_OFFSET', 'value': 1.8, 'input_unit': '°F'}]})
        assert response.status_code == 400
        assert len(ingest.calibration_commands) == before


@pytest.mark.asyncio
async def test_altitude_units_are_bound_to_form_not_new_preference(tmp_path, monkeypatch):
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {'Display': {'unit_system': 'Imperial'}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/submit-pi-setup', data={
            'astral_lat': '40', 'astral_lon': '-105', 'astral_altitude': '1000',
            'astral_altitude_unit': 'ft', 'unit_system': 'Metric'}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert float(_RouteFakeSaiSettings.STORED_SETTINGS['Astral']['ALTITUDE']) == 304.8


@pytest.mark.asyncio
async def test_automation_save_consumes_units_and_rejects_unknown_units(tmp_path, monkeypatch):
    app, _, _, _, _ = await _build_app(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        script = {'name': 'Temperature test', 'enabled': True, 'conditions': [
            {'type': 'sensor', 'sensor': 'test', 'metric': 'Temperature', 'op': '>',
             'value': 77, 'value_unit': '°F', 'hyst': 1.8, 'hyst_unit': '°F'}],
            'actions': [{'type': 'none', 'executor_switch_id': '__system__'}]}
        response = await client.post('/submit-advanced-trigger', json={
            'switch_id': '__system__', 'rule_id': 'unit-test', 'enabled': 'true', 'script_json': json.dumps(script)})
        assert response.status_code == 200, response.text
        response = await client.get('/advanced/automations?switch_id=__all__')
        rule = next(item for item in response.json()['items'] if item['rule_id'] == 'unit-test')
        condition = json.loads(rule['script_json'])['conditions'][0]
        assert condition['value'] == 25 and condition['hyst'] == 1
        assert 'value_unit' not in condition and 'hyst_unit' not in condition
        script['conditions'][0]['value_unit'] = 'ft'
        response = await client.post('/submit-advanced-trigger', json={
            'switch_id': '__system__', 'rule_id': 'unit-test', 'script_json': json.dumps(script)})
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_unchanged_altitude_preserves_stored_precision(tmp_path, monkeypatch):
    meters = '304.812345678'
    app = await _build_route_app_with_settings(tmp_path, monkeypatch, {
        'Astral': {'ALTITUDE': meters}, 'Display': {'unit_system': 'Imperial'}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/submit-pi-setup', data={
            'astral_lat': '40', 'astral_lon': '-105',
            'astral_altitude': str(float(meters) / .3048), 'astral_altitude_unit': 'ft'}, follow_redirects=False)
    assert response.status_code == 303
    assert _RouteFakeSaiSettings.STORED_SETTINGS['Astral']['ALTITUDE'] == meters


@pytest.mark.parametrize('metric', ['Dew Point Deficit', 'Plant Dew Point Deficit', 'Dewpoint Depression'])
def test_temperature_difference_conditions_never_add_32(metric):
    native = input_descriptor(metric, 'Metric')['native_unit']
    assert native in {'°C', '°F'}
    unit, value, expected = ('°F', 1.8, 1) if native == '°C' else ('°C', 1, 1.8)
    result = normalize_condition_inputs({'metric': metric, 'value': value, 'value_unit': unit})
    assert result['value'] == pytest.approx(expected)


def test_conversion_overflow_and_negative_hysteresis_are_rejected():
    with pytest.raises(ValueError):
        native_input_value(1e308, '°C', '°F')
    with pytest.raises(ValueError):
        normalize_condition_inputs({'metric': 'Temperature', 'hyst': -1, 'hyst_unit': '°F'})
