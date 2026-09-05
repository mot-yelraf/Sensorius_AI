"""Focused coverage for bounded dashboard graph payloads.

These checks keep long history windows usable without allowing every stored
reading to become a browser-resident Chart.js point.
"""

from sensorius.saiWebRoutes import GRAPH_MAX_POINTS_PER_SERIES, _downsample_graph_points


def test_downsample_graph_points_bounds_series_and_preserves_spike():
    timestamps = [f"2026-08-17T00:{idx:04d}:00-06:00" for idx in range(5000)]
    values = [10.0] * len(timestamps)
    values[2711] = 999.0

    sampled_ts, sampled_values = _downsample_graph_points(timestamps, values)

    assert len(sampled_ts) == len(sampled_values)
    assert len(sampled_ts) <= GRAPH_MAX_POINTS_PER_SERIES
    assert sampled_ts[0] == timestamps[0]
    assert sampled_ts[-1] == timestamps[-1]
    assert 999.0 in sampled_values


def test_downsample_graph_points_leaves_short_series_unchanged():
    timestamps = ["a", "b", "c"]
    values = [1.0, 2.0, 3.0]

    assert _downsample_graph_points(timestamps, values) == (timestamps, values)



def test_streaming_samples_match_existing_bucket_semantics():
    import random
    from sensorius.saiGraphData import sample_graph_rows
    rng = random.Random(12)
    for count in (0, 1, 3, 901, 5000, 12000):
        values = [rng.choice([None, 'invalid', 1, 3, 12, 999]) for _ in range(count)]
        times = list(range(count))
        expected = _downsample_graph_points(times, values)
        assert sample_graph_rows(zip(times, values), count) == expected


def test_indexed_graph_merges_legacy_rows_and_preserves_offset_order(tmp_path, monkeypatch):
    from sensorius.saiDataLogger import saiDataLogger
    from sensorius.saiGraphData import fetch_graph_series
    monkeypatch.setenv('SENSORIUS_DB_RETENTION_DAYS', '0')
    with saiDataLogger(str(tmp_path / 'graph.db')) as logger:
        logger.log_readings('2026-01-01T01:00:00+01:00', 'sensor', {'Temp': 1})
        logger.log_readings('2026-01-01T00:02:00+00:00', 'sensor', {'Temp': 3})
        # Narrow migration fixture: legacy epoch-less rows cannot be created through log_readings.
        with logger._writer_conn:
            logger._writer_conn.execute("INSERT INTO readings(timestamp, sensor_id, metric, value) VALUES ('2025-12-31T17:01:00-07:00', 'sensor', 'Temp', 2)")
        ts, values = fetch_graph_series(logger._writer_conn, 'SENSOR', 'temp', '2026-01-01T00:00:00+00:00', '2026-01-01T00:03:00+00:00')
        assert values == [1, 2, 3]
        assert len(ts) == 3


def test_graph_history_python_memory_is_bounded(tmp_path, monkeypatch):
    import tracemalloc
    from datetime import datetime, timezone
    from sensorius.saiDataLogger import saiDataLogger
    from sensorius.saiGraphData import fetch_graph_series
    monkeypatch.setenv('SENSORIUS_DB_RETENTION_DAYS', '0')
    with saiDataLogger(str(tmp_path / 'graph.db')) as logger:
        # Bulk synthetic history is a narrow performance-fixture bypass of the logging API.
        # Measuring ingest/fsync here would obscure the graph's allocation behavior.
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        with logger._writer_conn:
            logger._writer_conn.executemany('INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)', (
                (datetime.fromtimestamp(start+i, timezone.utc).isoformat(), start+i, 'sensor', 'Temp', 999 if i == 27111 else i % 10)
                for i in range(50000)
            ))
        tracemalloc.start()
        try:
            ts, values = fetch_graph_series(logger._writer_conn, 'sensor', 'Temp', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(ts) <= 900
        assert 999 in values
        assert peak < 2 * 1024 * 1024


async def _assert_graph_does_not_block_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    import sensorius.saiWebRoutes as routes
    import sensorius.saiGraphData as graph
    from sensorius.saiDataLogger import saiDataLogger
    from testApparatus.test_onboarding_v2_routes import _DummyFastStats, _FakeSettings, _FakeIngest, _FakeNetMgr, _FakeGcMgr
    monkeypatch.setattr(routes, 'FastStats', _DummyFastStats)
    with saiDataLogger(str(tmp_path / 'graph.db')) as logger:
        monkeypatch.setattr(routes, 'data_logger', logger)
        app = FastAPI()
        await routes.register_routes(app, _FakeSettings(), _FakeNetMgr(), _FakeGcMgr(), _FakeIngest())
        entered = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()

        def slow_query(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(3)
            return ['2026-01-01T00:00:00+00:00'], [21]

        monkeypatch.setattr(graph, 'fetch_graph_series', slow_query)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.get('/graph-data', params={'sensor_id': 'sensor', 'metric1': 'Temp', 'range': '24h'}))
            try:
                for _ in range(200):
                    if entered.is_set() or task.done():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set(), (await task).text if task.done() else 'worker never entered'
                # This coroutine can run while the database query is deliberately blocked.
                assert not task.done()
            finally:
                release.set()
            response = await task
            assert response.status_code == 200
            assert response.json()['series']['sensor::Temp']['vals'] == [21]


def test_graph_route_yields_during_database_work(tmp_path, monkeypatch):
    import asyncio
    asyncio.run(_assert_graph_does_not_block_event_loop(tmp_path, monkeypatch))
