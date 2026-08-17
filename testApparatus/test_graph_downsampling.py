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

