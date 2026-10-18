"""Exercise legacy selections against current packets and mixed-unit history.

Readings are written through the logger so compatibility is checked without
changing the native identities used by integrations or automation rules.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sensorius.saiDisplayUnits import apply_display_units_to_gauge_config
from sensorius.saiHtml import canonicalize_metric_name, get_gauge_config, render_dashboard
from sensorius.saiMetricNames import normalize_metric_keys, normalize_metric_stats


TEMPERATURE_PAIRS = [
    ("Temperature_F", "Temperature"),
    ("Dew Point_F", "Dew Point"),
    ("Plant Temperature_F", "Plant Temperature"),
    ("Plant Dew Point_F", "Plant Dew Point"),
    ("Soil Temp_F", "Soil Temperature"),
    ("Wind Chill_F", "Wind Chill"),
    ("Heat Index_F", "Heat Index"),
]
SPELLING_PAIRS = [
    ("Bar-Pressure", "Baro-Pressure"), ("Plant Bar-Pressure", "Plant Baro-Pressure"),
    ("Soil-Temp", "Soil Temperature"), ("Soil Temp_C", "Soil Temperature"),
    ("SMD", "Soil Moisture Deficit"), ("SSI", "Soil Stress Index"),
    ("Soil-Moisture", "Soil Moisture"), ("Soil-pH", "Soil pH"), ("Soil-EC", "Soil EC"),
    ("Dewpoint Deficit", "Dew Point Deficit"), ("dewVPD Risk", "DewVPD Risk"),
    ("PPFD", "Estimated PPFD"),
    ("Dew-Point", "Dew Point"),
    ("Dewpoint Depression", "Dew Point Deficit"),
    ("Plant Dewpoint Deficit", "Plant Dew Point Deficit"),
    ("Plant dewVPD Risk", "Plant DewVPD Risk"),
]


@pytest.mark.parametrize("old,current", SPELLING_PAIRS)
def test_spelling_aliases_resolve_values_statistics_and_selections(old, current):
    assert canonicalize_metric_name(old) == current
    assert normalize_metric_keys({old: 12}) == {current: 12}
    assert normalize_metric_stats({old: {"min": 10, "avg": 12, "max": 14}})[current]["avg"] == 12
    assert current in get_gauge_config()
    for system in ("Metric", "Imperial"):
        gauges = apply_display_units_to_gauge_config(get_gauge_config(), system)
        assert gauges[old] == gauges[current]


@pytest.mark.parametrize("old,current", SPELLING_PAIRS)
def test_graph_queries_accept_history_under_legacy_spellings(tmp_path, monkeypatch, old, current):
    from sensorius.saiDataLogger import saiDataLogger
    from sensorius.saiGraphData import fetch_graph_series
    monkeypatch.setenv("SENSORIUS_DB_RETENTION_DAYS", "0")
    with saiDataLogger(str(tmp_path / "aliases.db")) as logger:
        logger.log_readings("2026-10-18T00:00:00+00:00", "sensor", {old: 10})
        logger.log_readings("2026-10-18T00:01:00+00:00", "sensor", {current: 20})
        for selected in (old, current):
            times, values = fetch_graph_series(logger._writer_conn, "sensor", selected,
                "2026-10-18T00:00:00+00:00", "2026-10-18T00:02:00+00:00")
            assert len(times) == 2
            assert values == [10, 20]


@pytest.mark.parametrize("old,current", TEMPERATURE_PAIRS)
@pytest.mark.parametrize("system,unit,expected", [("Metric", "°C", "20.0"), ("Imperial", "°F", "68.0")])
def test_saved_fahrenheit_selection_renders_current_celsius_packet(old, current, system, unit, expected):
    packet = {"sensor": {current: 20.0}}
    html = "".join(render_dashboard(
        "All", None, ["sensor"], packet,
        {"sensor": {current: {"min": 10, "avg": 15, "max": 20}}},
        SimpleNamespace(expected_gauge_map={}),
        expected_gauge_map={"sensor": [old]},
        gauge_config=apply_display_units_to_gauge_config(get_gauge_config(), system),
    ))
    assert f"{old} ({unit})" in html
    assert f">{expected} {unit}</span>" in html
    assert packet == {"sensor": {current: 20.0}}


@pytest.mark.parametrize("old,current", TEMPERATURE_PAIRS)
def test_current_selection_reads_legacy_packet_and_keeps_other_channels_separate(old, current):
    result = normalize_metric_keys({old: 68, "WH31 CH1 Temperature_F": 77}, [current])
    assert result[current] == pytest.approx(20)
    assert result[old] == 68
    assert result["WH31 CH1 Temperature_F"] == 77
    assert normalize_metric_keys({old: 68, current: 19}, [current])[current] == 19


@pytest.mark.parametrize("old,current", TEMPERATURE_PAIRS)
def test_graphs_and_statistics_convert_mixed_native_units(tmp_path, monkeypatch, old, current):
    from sensorius.saiDataLogger import saiDataLogger
    from sensorius.saiGraphData import fetch_graph_series
    from sensorius.saiStats import saiStats

    monkeypatch.setenv("SENSORIUS_DB_RETENTION_DAYS", "0")
    now = datetime.now(timezone.utc)
    with saiDataLogger(str(tmp_path / "history.db")) as logger:
        for minutes, name, value in [(3, old, 50), (2, old, 68), (1, current, 30)]:
            logger.log_readings((now - timedelta(minutes=minutes)).isoformat(), "sensor", {name: value})
        for selected, expected in [(old, [50, 68, 86]), (current, [10, 20, 30])]:
            ts, values = fetch_graph_series(logger._writer_conn, "sensor", selected,
                (now - timedelta(hours=1)).isoformat(), now.isoformat())
            assert len(ts) == 3
            assert values == pytest.approx(expected)
        statter = saiStats(logger.db_path)
        for raw in (statter.get_24hr_stats("sensor"), statter.get_all_stats_fast()["sensor"]):
            before = deepcopy(raw)
            stats = normalize_metric_stats(raw, [old, current])
            assert stats[current]["avg"] == pytest.approx(20)
            assert stats[current]["min"] == pytest.approx(10)
            assert stats[current]["max"] == pytest.approx(30)
            assert stats[old]["avg"] == pytest.approx(68)
            assert stats[old]["max"] == pytest.approx(86)
            assert stats[old]["min_ts"] == raw[old]["min_ts"]
            assert stats[current]["samples"] == 3
            assert raw == before
        assert set(logger.get_available_metrics("sensor")) == {old, current}


def test_temperature_trend_conversion_has_no_offset_and_does_not_mutate_source():
    raw = {"Temperature_F": {"min": 50, "avg": 59, "max": 68, "trend": {"rate_per_hour": 1.8}}}
    before = deepcopy(raw)
    stats = normalize_metric_stats(raw, ["Temperature"])
    assert stats["Temperature"]["trend"]["rate_per_hour"] == pytest.approx(1)
    assert raw == before


def test_missing_current_value_does_not_hide_a_valid_legacy_value():
    for packet in (
        {"Soil Temperature": None, "Soil Temp_C": 15},
        {"Soil Temp_C": 15, "Soil Temperature": None},
        {"Soil Temperature": None, "Soil Temp_F": 59},
    ):
        assert normalize_metric_keys(packet, ["Soil Temperature"])["Soil Temperature"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_selection", [True, False])
async def test_dashboard_json_resolves_equivalent_units_without_rewriting_settings(tmp_path, monkeypatch, legacy_selection):
    from httpx import ASGITransport, AsyncClient
    from testApparatus.test_nodus_settings_schema_writes import _build_app, _REAL_SENSOR_SETTINGS_MANAGER
    import sensorius.saiWebRoutes as routes

    app, ingest, _, sensor_root, _ = await _build_app(tmp_path, monkeypatch)
    sid = "apvpd-legacy"
    manager = _REAL_SENSOR_SETTINGS_MANAGER(str(sensor_root))
    selected = [pair[0 if legacy_selection else 1] for pair in TEMPERATURE_PAIRS[:5]]
    selected.append("SMD" if legacy_selection else "Soil Moisture Deficit")
    manager.save(sid, {
        "Sensor": {"TYPE": "nodus", "DEVICE": "apvpd", "SENSOR_ID": sid},
        "Display": {f"METRIC_{index}": name for index, name in enumerate(selected, 1)},
    })
    before = manager.load(sid)
    source_names = [pair[1 if legacy_selection else 0] for pair in TEMPERATURE_PAIRS[:5]]
    source_value = 25 if legacy_selection else 77
    packet = {name: source_value for name in source_names}
    packet["Soil Moisture Deficit" if legacy_selection else "SMD"] = 12
    raw_stats = {name: {"min": source_value, "avg": source_value, "max": source_value, "samples": 1} for name in source_names}
    routes._DASHBOARD_JSON_CACHE.clear()
    routes._DASHBOARD_INVENTORY_CACHE = None
    routes._DASHBOARD_DISPLAY_SETTINGS_CACHE = None
    stamp = datetime.now().isoformat()
    monkeypatch.setattr(routes.data_logger, "get_available_sensors", lambda: [sid])
    monkeypatch.setattr(routes.data_logger, "get_available_metrics", lambda _: list(packet))
    monkeypatch.setattr(routes.data_logger, "get_latest_timestamp", lambda _: stamp)
    monkeypatch.setattr(routes.data_logger, "get_latest_values_and_timestamps", lambda ids: ({sid: packet}, {sid: stamp}))
    monkeypatch.setattr(routes.data_logger, "get_switch_identities", lambda: [])
    monkeypatch.setattr(routes.statter, "get_all_stats_fast", lambda: {sid: raw_stats})
    ingest.mqtt_clients = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", params={"json_only": "true"})
    assert response.status_code == 200
    payload = response.json()
    expected = 77 if legacy_selection else 25
    for name in selected[:5]:
        assert payload["values"][sid][name] == pytest.approx(expected)
        assert payload["stats"][sid][name]["avg"] == pytest.approx(expected)
    assert payload["values"][sid]["Soil Moisture Deficit"] == 12
    assert manager.load(sid) == before
    assert packet[source_names[0]] == source_value
    assert ingest.published_json == []
