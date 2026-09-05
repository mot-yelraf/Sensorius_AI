"""Stream indexed history into bounded, spike-preserving graph series.

Count and stream within one SQLite snapshot so concurrent ingestion cannot
change bucket boundaries. Legacy rows without epochs remain queryable.
"""

import heapq
import math
from datetime import datetime


def sample_graph_rows(rows, count: int, max_points: int = 900):
    """Sample ordered timestamp/value pairs with bounded Python memory."""
    limit = max(3, int(max_points))
    if count <= limit:
        pairs = list(rows)
        return [r[0] for r in pairs], [r[1] for r in pairs]
    width = max(1, math.ceil((count - 2) / max(1, (limit - 2) // 2)))
    selected = []
    first = low = high = None

    def finish_bucket():
        if low is None:
            selected.append(first)
        else:
            selected.extend(sorted({low[0]: low, high[0]: high}.values()))

    for index, row in enumerate(rows):
        point = (index, row[0], row[1])
        if index == 0 or index == count - 1:
            selected.append(point)
            continue
        if (index - 1) % width == 0:
            first, low, high = point, None, None
            low_value = high_value = None
        try:
            value = float(row[1])
        except (ValueError, TypeError):
            value = float('nan')
        if math.isfinite(value):
            if low is None or value < low_value:
                low, low_value = point, value
            if high is None or value > high_value:
                high, high_value = point, value
        if index == count - 2 or index % width == 0:
            finish_bucket()
    if len(selected) > limit:
        stride = (len(selected) - 1) / (limit - 1)
        selected = [selected[round(i * stride)] for i in range(limit)]
    return [r[1] for r in selected], [r[2] for r in selected]


def fetch_graph_series(conn, sensor_id, metric, since_iso, until_iso, *, max_points=900):
    """Read an indexed history window, including legacy timestamps, in bounded memory."""
    start = datetime.fromisoformat(since_iso).timestamp()
    end = datetime.fromisoformat(until_iso).timestamp()
    modern = "sensor_id = ? COLLATE NOCASE AND metric = ? COLLATE NOCASE AND ts_epoch >= ? AND ts_epoch <= ?"
    legacy = "sensor_id = ? COLLATE NOCASE AND metric = ? COLLATE NOCASE AND ts_epoch IS NULL AND julianday(timestamp) >= julianday(?) AND julianday(timestamp) <= julianday(?)"
    params = (sensor_id, metric, start, end)
    old_params = (sensor_id, metric, since_iso, until_iso)
    conn.execute("SAVEPOINT graph_read")
    cursors = []
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM readings WHERE {modern}", params).fetchone()[0]
        old_count = conn.execute(f"SELECT COUNT(*) FROM readings WHERE {legacy}", old_params).fetchone()[0]
        current = conn.execute(f"SELECT timestamp, value, ts_epoch FROM readings WHERE {modern} ORDER BY ts_epoch", params)
        cursors.append(current)
        rows = current
        if old_count:
            old = conn.execute(f"SELECT timestamp, value, (julianday(timestamp)-2440587.5)*86400.0 AS epoch FROM readings WHERE {legacy} ORDER BY julianday(timestamp)", old_params)
            cursors.append(old)
            rows = heapq.merge(current, old, key=lambda row: row[2])
        return sample_graph_rows(rows, count + old_count, max_points)
    finally:
        for cursor in cursors:
            cursor.close()
        conn.execute("RELEASE graph_read")
