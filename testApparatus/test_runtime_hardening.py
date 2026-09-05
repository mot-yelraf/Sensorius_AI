"""Exercise failure boundaries with isolated storage and controlled concurrency.

These tests cover the interactions between existing recovery mechanisms, rather
than treating each component's successful path as evidence of recovery.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from sensorius.saiDataLogger import saiDataLogger
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.saiSwitchSettingsManager import SwitchSettingsManager
from sensorius.saiTaskSupervisor import TaskSupervisor


@pytest.mark.parametrize('manager_class,section', [
    (SensorSettingsManager, 'Sensor'), (SwitchSettingsManager, 'Switch'),
])
def test_concurrent_settings_updates_preserve_independent_keys(tmp_path, manager_class, section):
    manager = manager_class(str(tmp_path))
    manager.save('review', {section: {'TYPE': 'nodus'}})
    start = threading.Barrier(8)

    def update(index):
        instance = manager_class(str(tmp_path))
        start.wait(timeout=5)
        key = f'key_{index}'
        if section == 'Sensor':
            key = f'{section}.{key}'
        instance.update_setting('review', key, index)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(8)))
    document = manager.load('review')
    assert document[section]['TYPE'] == 'nodus'
    assert all(document[section][f'key_{i}'] == i for i in range(8))
    assert not list(tmp_path.rglob('*.tmp'))


def test_failed_batch_rolls_back_before_next_writer(tmp_path, monkeypatch):
    monkeypatch.setenv('SENSORIUS_DB_RETENTION_DAYS', '0')
    with saiDataLogger(str(tmp_path / 'readings.db')) as logger:
        # A trigger injects a mid-batch SQLite failure after an earlier row inserted.
        logger._writer_conn.execute("CREATE TRIGGER reject_metric BEFORE INSERT ON readings WHEN NEW.metric = 'bad' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        observed = []
        logger.add_readings_listener(lambda *args: observed.append(args))
        logger.log_readings(None, 'sensor', {'good': 1, 'bad': 2})
        assert not logger._writer_conn.in_transaction
        assert logger._writer_conn.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 0
        assert not observed
        assert 'sensor' not in logger.sensor_values
        logger.log_readings(None, 'sensor', {'next': 3})
        assert [tuple(row) for row in logger._writer_conn.execute('SELECT metric, value FROM readings')] == [('next', 3)]
        assert len(observed) == 1


def test_invalid_batch_is_rejected_without_partial_storage(tmp_path):
    with saiDataLogger(str(tmp_path / 'readings.db')) as logger:
        logger.log_readings(None, 'sensor', {'good': 1, 'bad': {'nested': True}})
        assert not logger._writer_conn.in_transaction
        assert logger._writer_conn.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 0


def test_retention_is_bounded_and_not_part_of_ingestion(tmp_path, monkeypatch):
    monkeypatch.setenv('SENSORIUS_DB_RETENTION_DAYS', '90')
    with saiDataLogger(str(tmp_path / 'readings.db')) as logger:
        for i in range(12):
            logger.log_readings('2000-01-01T00:00:00+00:00', 'old', {'value': i})
            logger.log_switch_event('switch::channel', True, timestamp='2000-01-01T00:00:00+00:00')
            logger.log_sensor_event('old', 'liveness', timestamp='2000-01-01T00:00:00+00:00')
        logger.log_readings(None, 'new', {'value': 42})
        assert logger._writer_conn.execute('SELECT COUNT(*) FROM readings').fetchone()[0] == 13
        assert logger.prune_old_rows_batch(batch_size=5) == 15
        assert logger.prune_old_rows_batch(batch_size=5) == 15
        assert logger.prune_old_rows_batch(batch_size=5) == 6
        assert logger.prune_old_rows_batch(batch_size=5) == 0
        assert logger._writer_conn.execute('SELECT sensor_id FROM readings').fetchone()[0] == 'new'
        monkeypatch.setattr(logger, '_db_retention_days', 0)
        assert logger.prune_old_rows_batch() == 0


@pytest.mark.asyncio
async def test_startup_heartbeat_does_not_reset_crash_backoff(monkeypatch):
    import sensorius.saiTaskSupervisor as module
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name='Worker', fatal_on_error=False)
    delays = []

    async def worker():
        supervisor.feedthedogs('Worker')
        if len(delays) == 4:
            supervisor._shutdown = True
        raise RuntimeError('after heartbeat')

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    await supervisor.runner(worker, (), {}, 'Worker')
    assert delays == [5, 10, 20, 40, 60]


def test_configurable_database_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('SENSORIUS_DB_CACHE_KIB', '8192')
    with saiDataLogger(str(tmp_path / 'readings.db')) as logger:
        assert logger._writer_conn.execute('PRAGMA cache_size').fetchone()[0] == -8192


@pytest.mark.asyncio
async def test_sustained_execution_resets_crash_backoff(monkeypatch):
    import sensorius.saiTaskSupervisor as module
    supervisor = TaskSupervisor()
    supervisor.add(lambda: None, name='Worker', fatal_on_error=False)
    clock = {'mono': 1000.0}
    delays = []

    async def worker():
        supervisor.feedthedogs('Worker')
        if len(delays) == 2:
            clock['mono'] += module.CRASH_BACKOFF_RESET_SEC
            supervisor._shutdown = True
        raise RuntimeError('simulated failure')

    async def sleep(delay):
        delays.append(delay)
        clock['mono'] += delay

    monkeypatch.setattr(module.time, 'monotonic', lambda: clock['mono'])
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    await supervisor.runner(worker, (), {}, 'Worker')
    assert delays == [5, 10, 5]
