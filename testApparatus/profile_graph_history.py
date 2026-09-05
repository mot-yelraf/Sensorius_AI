"""Benchmark graph latency and allocations against isolated synthetic history.

Run on the target hub to compare SQLite cache sizes before changing its default.
The fixture intentionally bulk-inserts historical rows so ingestion and fsync
costs do not dominate a graph-query benchmark; no live database is opened.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc
from unittest.mock import patch


def main():
    """Print reproducible graph timing and Python-memory measurements as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=250000)
    parser.add_argument('--cache-kib', type=int, nargs='+', default=[8192, 65536])
    args = parser.parse_args()
    if args.rows < 1 or any(value < 512 for value in args.cache_kib):
        parser.error('rows must be positive and cache sizes at least 512 KiB')
    old_runtime = os.environ.get('SENSORIUS_RUNTIME_ROOT')
    old_cache = os.environ.get('SENSORIUS_DB_CACHE_KIB')
    with tempfile.TemporaryDirectory(prefix='sensorius-graph-profile-') as tmp:
        os.environ['SENSORIUS_RUNTIME_ROOT'] = tmp
        try:
            from sensorius.saiDataLogger import saiDataLogger
            from sensorius.saiGraphData import fetch_graph_series
            with patch('sensorius.saiSettings.get_pi_network_info', return_value={'hostname': 'graph-profile'}), saiDataLogger(str(Path(tmp) / 'history.db')) as logger:
                epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
                with logger._writer_conn:
                    logger._writer_conn.executemany(
                        'INSERT INTO readings(timestamp, ts_epoch, sensor_id, metric, value) VALUES (?, ?, ?, ?, ?)',
                        ((datetime.fromtimestamp(epoch+i*30, timezone.utc).isoformat(), epoch+i*30, 'profile', 'Temp', i % 100) for i in range(args.rows)),
                    )
                results = []
                end = datetime.fromtimestamp(epoch+args.rows*30, timezone.utc).isoformat()
                for cache in args.cache_kib:
                    os.environ['SENSORIUS_DB_CACHE_KIB'] = str(cache)
                    conn = logger._open_conn()
                    try:
                        durations = []
                        for _ in range(3):
                            start = time.perf_counter()
                            ts, _ = fetch_graph_series(conn, 'profile', 'Temp', '2026-01-01T00:00:00+00:00', end)
                            durations.append(time.perf_counter()-start)
                        tracemalloc.start()
                        try:
                            fetch_graph_series(conn, 'profile', 'Temp', '2026-01-01T00:00:00+00:00', end)
                            _, peak = tracemalloc.get_traced_memory()
                        finally:
                            tracemalloc.stop()
                        results.append({'cache_kib': cache, 'rows': args.rows, 'points': len(ts), 'median_seconds': round(statistics.median(durations), 4), 'peak_python_bytes': peak})
                    finally:
                        conn.close()
                print(json.dumps(results, indent=2))
        finally:
            for key, old in [('SENSORIUS_RUNTIME_ROOT', old_runtime), ('SENSORIUS_DB_CACHE_KIB', old_cache)]:
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old


if __name__ == '__main__':
    main()
