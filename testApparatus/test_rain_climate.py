"""Verify rain climate aggregation, caching, failures, and gauge unit compatibility."""

from datetime import date, timedelta
from types import SimpleNamespace
import asyncio
import json

import httpx
import pytest

from sensorius import saiRainClimate as climate
from sensorius.saiDisplayUnits import apply_display_units_to_gauge_config
from sensorius.saiHtml import get_gauge_config


END = date(2020, 12, 31)


@pytest.fixture(autouse=True)
def cutoff(monkeypatch):
    monkeypatch.setattr(climate, 'latest_history_date', lambda: END)


def history(value=1.0):
    days = (END - climate.START).days + 1
    return {"daily_units": {"rain_sum": "mm"}, "daily": {
        "time": [(climate.START + timedelta(days=i)).isoformat() for i in range(days)],
        "rain_sum": [value] * days,
    }}


def test_complete_baseline_includes_leap_days_and_calendar_periods():
    assert climate.summarize_rain_history(history(), END) == {
        "day": 1, "week": 7, "month": 31, "year": 366,
    }


def test_rolling_week_crosses_month_and_year_boundaries():
    data = history(0)
    for day in ('1999-12-30', '1999-12-31', '2000-01-01', '2000-01-02'):
        data['daily']['rain_sum'][data['daily']['time'].index(day)] = 10
    assert climate.summarize_rain_history(data, END) == {"day": 10, "week": 40, "month": 20, "year": 20}


@pytest.mark.parametrize('value', [None, -1, float('nan'), float('inf'), True, 'invalid'])
def test_invalid_rain_is_not_treated_as_dry_weather(value):
    data = history()
    data['daily']['rain_sum'][100] = value
    with pytest.raises((ValueError, TypeError)):
        climate.summarize_rain_history(data, END)


def test_missing_or_duplicate_days_rejected():
    data = history()
    data['daily']['time'][1] = data['daily']['time'][0]
    with pytest.raises(ValueError):
        climate.summarize_rain_history(data, END)
    data = history()
    data['daily']['rain_sum'].pop()
    with pytest.raises(ValueError):
        climate.summarize_rain_history(data, END)


def test_blue_bands_preserve_metric_units_and_source_values():
    base = get_gauge_config()
    for metric, period in climate.RAIN_PERIODS.items():
        gauge = base[metric]
        assert gauge['rain_period'] == period
        assert [z['strokeStyle'] for z in gauge['zones']] == list(climate.RAIN_COLORS)
        assert gauge['zones'][0]['max'] == pytest.approx(gauge['max'] * .2)
        assert gauge['zones'][1]['max'] == pytest.approx(gauge['max'] * .6)
        metric_gauge = apply_display_units_to_gauge_config(base, 'Metric')[metric]
        assert metric_gauge['max'] == pytest.approx(gauge['max'] * 25.4)
        assert metric_gauge['source_unit'] == 'in'


@pytest.mark.asyncio
async def test_background_download_cache_restart_and_changed_location(monkeypatch, tmp_path):
    location = {'lat': 40, 'lon': -105, 'tz': 'America/Denver'}
    factory = lambda: SimpleNamespace(resolve_astral_location=lambda **kwargs: location.copy())
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.params['daily'] == 'rain_sum'
        assert request.url.params['models'] == 'era5'
        assert request.url.params['start_date'] == '1991-01-01'
        return httpx.Response(200, json=history())
    client = httpx.AsyncClient
    monkeypatch.setattr(climate.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    path = tmp_path / 'weather_cache' / 'rain_climate.json'
    service = climate.RainClimateService(factory, path)
    assert service.snapshot()['status'] == 'warming'
    task = service.task
    service.snapshot()
    assert service.task is task
    await task
    assert service.snapshot()['maxima_mm']['week'] == 7
    assert len(requests) == 1
    assert path.exists()
    restored = climate.RainClimateService(factory, path)
    restored.snapshot()
    await restored.task
    assert restored.payload['status'] == 'ready'
    assert len(requests) == 1
    location['lon'] = -106
    restored.invalidate()
    assert 'maxima_mm' not in restored.snapshot()
    await restored.task
    assert len(requests) == 2
    assert restored.payload['location']['longitude'] == -106
    await service.close()
    await restored.close()


@pytest.mark.asyncio
async def test_network_failure_keeps_default_status_and_backs_off(monkeypatch, tmp_path):
    client = httpx.AsyncClient
    monkeypatch.setattr(climate.httpx, 'AsyncClient', lambda **kwargs: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    factory = lambda: SimpleNamespace(resolve_astral_location=lambda **kwargs: {'lat': 0, 'lon': 0, 'tz': 'UTC'})
    service = climate.RainClimateService(factory, tmp_path / 'cache.json')
    service.snapshot()
    await service.task
    task = service.task
    assert service.snapshot()['status'] == 'unavailable'
    assert service.task is task
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_load_does_not_publish_old_location(tmp_path):
    service = climate.RainClimateService(lambda: None, tmp_path / 'cache.json')
    service.task = asyncio.create_task(asyncio.sleep(100))
    task = service.task
    service.payload = {'status': 'ready', 'maxima_mm': {'day': 100}}
    service.invalidate()
    await asyncio.gather(task, return_exceptions=True)
    assert service.payload == {'status': 'warming'}
    assert task.cancelled()


def extended_history(end, value=1.0):
    count = (end - climate.START).days + 1
    return {'daily_units': {'rain_sum': 'mm'}, 'daily': {
        'time': [(climate.START + timedelta(days=i)).isoformat() for i in range(count)],
        'rain_sum': [value] * count,
    }}


def test_recent_extremes_include_complete_days_but_exclude_partial_month_and_year():
    end = date(2026, 9, 13)
    data = extended_history(end)
    data['daily']['rain_sum'][-1] = 1000
    assert climate.summarize_rain_history(data, end) == {
        'day': 1000, 'week': 1006, 'month': 31, 'year': 366,
    }
    for end in (date(2026, 9, 30), date(2026, 12, 31)):
        data = extended_history(end)
        data['daily']['rain_sum'][-1] = 1000
        result = climate.summarize_rain_history(data, end)
        assert result['month'] == (1029 if end.month == 9 else 1030)
        assert result['year'] == (366 if end.month == 9 else 1364)


@pytest.mark.asyncio
async def test_expanding_cache_refresh_and_failure_preserves_previous_summary(monkeypatch, tmp_path):
    cutoff = date(2026, 9, 12)
    monkeypatch.setattr(climate, 'latest_history_date', lambda: cutoff)
    requests = []
    fail = False
    def respond(request):
        requests.append(request)
        assert request.url.params['end_date'] == cutoff.isoformat()
        return httpx.Response(503) if fail else httpx.Response(200, json=extended_history(cutoff))
    client = httpx.AsyncClient
    monkeypatch.setattr(climate.httpx, 'AsyncClient', lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs))
    factory = lambda: SimpleNamespace(resolve_astral_location=lambda **kwargs: {'lat': 0, 'lon': 0, 'tz': 'UTC'})
    path = tmp_path / 'climate.json'
    # The previous fixed-range schema must not be reused.
    path.write_text(json.dumps({'schema': 1, 'location': {'latitude': 0, 'longitude': 0, 'timezone': 'UTC'},
                                'maxima_mm': dict.fromkeys(('day', 'week', 'month', 'year'), 9999)}))
    service = climate.RainClimateService(factory, path)
    await service._load()
    assert service.payload['end_date'] == '2026-09-12'
    assert service.payload['baseline'] == '1991–2026'
    await service._load()
    assert len(requests) == 1
    cutoff += timedelta(days=1)
    await service._load()
    assert len(requests) == 2
    assert service.payload['end_date'] == '2026-09-13'
    cutoff += timedelta(days=1)
    fail = True
    await service._load()
    assert service.payload['status'] == 'ready'
    assert service.payload['end_date'] == '2026-09-13'
