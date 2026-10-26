"""Exercise shadow persistence independently of MQTT transport.

Repeated snapshots and patches must avoid redundant disk writes while preserving
hub display preferences, paired switch identity, and nested calibration values.
"""

from types import SimpleNamespace
from pathlib import Path

from sensorius import saiNodusShadowPersistence as shadows
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
from sensorius.saiSettings import saiSettings


def test_metadata_replay_is_idempotent_and_partial_snapshot_preserves_shadows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(saiSettings, 'DEFAULT_BASE_DIR', str(tmp_path / 'system_settings'))
    sensor_mgr = SensorSettingsManager()
    sensor_mgr.save('avpd-test', {
        'Sensor': {'TYPE': 'nodus', 'DEVICE': 'avpd', 'SENSOR_ID': 'avpd-test', 'LOCATION': 'Bench'},
        'Display': {'METRIC_1': 'Temperature', 'METRIC_2': 'Rel-Humidity'},
        'Calibration': {'Device': {'TEMP_CAL_VAL': 1.25}},
    })
    sensor = {'sensor_id': 'avpd-test', 'device_type': 'nodus', 'device': 'avpd', 'location': 'Bench'}
    switch = {'switch_id': 'switch-test', 'switch_type': 'nodus', 'switch_location': 'Bench',
              'switch_payload': {'Switch': {
                  'SWITCH_1_LABEL': 'Fan', 'SWITCH_1_CHANNEL_ID': 'S1-test',
                  'SWITCH_2_LABEL': 'Pump', 'SWITCH_2_CHANNEL_ID': 'S2-test',
              }}}
    writes = []
    original_replace = Path.replace
    original_sensor_save = SensorSettingsManager.save
    original_switch_save = SwitchSettingsManager.save

    def replace(path, target):
        writes.append(str(target))
        return original_replace(path, target)

    def sensor_save(manager, *args, **kwargs):
        writes.append(('sensor', args[0]))
        return original_sensor_save(manager, *args, **kwargs)

    def switch_save(manager, *args, **kwargs):
        writes.append(('switch', args[0]))
        return original_switch_save(manager, *args, **kwargs)

    monkeypatch.setattr(Path, 'replace', replace)
    monkeypatch.setattr(SensorSettingsManager, 'save', sensor_save)
    monkeypatch.setattr(SwitchSettingsManager, 'save', switch_save)
    logger = SimpleNamespace(prune_switch_identities=lambda **kwargs: None)
    info = {'HOSTNAME': 'avpd-test', 'MQTT': {'BROKER': 'broker.local'}}
    shadows.persist_metadata(logger, info, 'avpd-test', [sensor], [switch])
    assert writes
    writes.clear()
    shadows.persist_metadata(logger, info, 'avpd-test', [sensor], [switch])
    assert writes == []
    shadows.persist_metadata(logger, {'HOSTNAME': 'avpd-test'}, 'avpd-test', [sensor], [])
    assert writes == []
    saved = sensor_mgr.load('avpd-test')
    assert saved['Display']['METRIC_1'] == 'Temperature'
    assert saved['Calibration']['Device']['TEMP_CAL_VAL'] == 1.25
    saved_switch = SwitchSettingsManager().load('switch-test')['Switch']
    assert saved_switch['SWITCH_1_CHANNEL_ID'] == 'S1-test'
    assert saved_switch['SWITCH_2_CHANNEL_ID'] == 'S2-test'


def test_calibration_patch_updates_only_requested_values_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SensorSettingsManager()
    manager.save('avpd-test', {
        'Sensor': {'TYPE': 'nodus', 'DEVICE': 'avpd', 'SENSOR_ID': 'avpd-test'},
        'Calibration': {'Device': {'TEMP_CAL_VAL': 1.0, 'HUM_CAL_VAL': 2.0}},
    })
    saved = []
    original_save = SensorSettingsManager.save

    def save(manager, *args, **kwargs):
        saved.append(args[0])
        return original_save(manager, *args, **kwargs)

    monkeypatch.setattr(SensorSettingsManager, 'save', save)
    patch = {'avpd-test': {'Calibration.Device': {'TEMP_CAL_VAL': 1.5}}}
    shadows.persist_calibration_patch(patch)
    shadows.persist_calibration_patch(patch)
    assert saved == ['avpd-test']
    assert manager.load('avpd-test')['Calibration']['Device'] == {'TEMP_CAL_VAL': 1.5, 'HUM_CAL_VAL': 2.0}
