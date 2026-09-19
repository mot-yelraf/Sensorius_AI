"""Check daily weather climatology, local-date selection and persistent caching."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from sensorius.saiRainClimate import START
from sensorius import saiRainClimate as climate

END = date(2020, 12, 31)
from sensorius.saiWeatherClimate import WeatherClimateService, summarize_weather_history, EXTREME_VARIABLES, VARIABLES


@pytest.fixture(autouse=True)
def cutoff(monkeypatch):
    monkeypatch.setattr(climate, 'latest_history_date', lambda: END)


def add_extremes(data):
    for kind, variables in EXTREME_VARIABLES.items():
        for variable, field in variables.items():
            if variable not in data["daily"]:
                mean_variable = next(key for key, value in VARIABLES.items() if value == field)
                offset = -1 if kind == "min" else 1
                data["daily"][variable] = [value + offset for value in data["daily"][mean_variable]]
    return data


def history():
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    return add_extremes({"daily_units": WeatherClimateService.expected_units, "daily": {
        "time": [d.isoformat() for d in days],
        "temperature_2m_mean": [d.year - 1990 for d in days],
        "relative_humidity_2m_mean": [50.0] * len(days),
        "wind_speed_10m_mean": [10.0] * len(days),
        "rain_sum": [4.0 if d.year % 2 == 0 else 0.0 for d in days],
    }})


def test_calendar_date_means_include_dry_days_and_leap_day_samples():
    averages = summarize_weather_history(history(), END)
    assert len(averages) == 366
    assert {key: value for key, value in averages['09-18'].items() if key != 'extremes'} == {'temperature_c': 15.5, 'humidity_pct': 50,
                                'wind_kmh': 10, 'rain_mm': 2, 'samples': 30}
    assert averages['02-29']['samples'] == 8
    assert averages['02-29']['temperature_c'] == 16


@pytest.mark.parametrize('variable,value', [('rain_sum', None), ('relative_humidity_2m_mean', 101),
                                          ('wind_speed_10m_mean', -1), ('temperature_2m_mean', float('nan'))])
def test_missing_or_invalid_values_do_not_become_zero(variable, value):
    data = history()
    data['daily'][variable][0] = value
    with pytest.raises(ValueError):
        summarize_weather_history(data, END)


def test_incomplete_calendar_rejected():
    data = history()
    data['daily']['time'][10] = data['daily']['time'][9]
    with pytest.raises(ValueError):
        summarize_weather_history(data, END)


@pytest.mark.asyncio
async def test_cache_reuse_and_selection_in_station_timezone(monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.params['daily'] == WeatherClimateService.daily_variables
        assert request.url.params['wind_speed_unit'] == 'kmh'
        return httpx.Response(200, json=history())
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    location = {'lat': 40, 'lon': -105, 'tz': 'America/Denver'}
    factory = lambda: SimpleNamespace(resolve_astral_location=lambda **kwargs: location)
    path = tmp_path / 'weather_climate.json'
    service = WeatherClimateService(factory, path)
    assert service.snapshot()['status'] == 'warming'
    await service.task
    utc = datetime(2026, 9, 19, 1, tzinfo=timezone.utc)
    result = service.snapshot(now=utc)
    assert result['date'] == '2026-09-18'
    assert result['averages']['rain_mm'] == 2
    assert 'calendar_averages' not in result
    assert service.snapshot(now=utc + timedelta(hours=8))['date'] == '2026-09-19'
    restored = WeatherClimateService(factory, path)
    restored.snapshot()
    await restored.task
    assert restored.snapshot(now=utc)['averages'] == result['averages']
    assert len(calls) == 1
    restored.invalidate()
    assert restored.payload == {'status': 'warming'}
    await service.close()
    await restored.close()


def test_extended_averages_use_available_samples_and_validate_new_cache():
    end = date(2026, 9, 13)
    days = [START + timedelta(days=i) for i in range((end - START).days + 1)]
    data = {'daily': {
        'time': [day.isoformat() for day in days],
        'temperature_2m_mean': [day.year - 1990 for day in days],
        'relative_humidity_2m_mean': [50] * len(days),
        'wind_speed_10m_mean': [10] * len(days),
        'rain_sum': [1] * len(days),
    }}
    averages = summarize_weather_history(add_extremes(data), end)
    assert averages['09-13']['samples'] == 36
    assert averages['09-13']['temperature_c'] == 18.5
    assert averages['09-14']['samples'] == 35
    assert averages['09-14']['temperature_c'] == 18
    assert averages['02-29']['samples'] == 9
    service = WeatherClimateService(None, 'unused.json')
    payload = {'end_date': end.isoformat(), 'calendar_averages': averages}
    assert service._valid_summary(payload)
    averages['09-14']['samples'] = 36
    assert not service._valid_summary(payload)


@pytest.mark.asyncio
async def test_unpublished_tail_uses_actual_cutoff_and_reuses_cache(monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        data = history()
        for field in WeatherClimateService.expected_units:
            data['daily'][field][-2:] = [None, None]
        return httpx.Response(200, json=data)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    factory = lambda: SimpleNamespace(resolve_astral_location=lambda **kwargs: {'lat': 0, 'lon': 0, 'tz': 'UTC'})
    service = WeatherClimateService(factory, tmp_path / 'cache.json')
    await service._load()
    assert service.payload['status'] == 'ready'
    assert service.payload['end_date'] == '2020-12-29'
    assert service.payload['requested_end_date'] == '2020-12-31'
    assert service.payload['calendar_averages']['12-30']['samples'] == 29
    await service._load()
    assert len(calls) == 1
    data = history()
    data['daily']['rain_sum'][100] = None
    trimmed, end = service._available_history(data, END)
    with pytest.raises(ValueError):
        service._summarize(trimmed, end)
    data = history()
    data['daily']['rain_sum'][-8:] = [None] * 8
    with pytest.raises(ValueError):
        service._available_history(data, END)


def test_extremes_track_daily_lows_highs_years_and_earliest_ties():
    rows = summarize_weather_history(history(), END)
    assert rows["09-18"]["extremes"]["min"]["temperature_c"] == {"value": 0, "year": 1991}
    assert rows["09-18"]["extremes"]["max"]["temperature_c"] == {"value": 31, "year": 2020}
    assert rows["09-18"]["extremes"]["min"]["rain_mm"] == {"value": 0, "year": 1991}
    assert rows["09-18"]["extremes"]["max"]["rain_mm"] == {"value": 4, "year": 1992}
    assert rows["02-29"]["extremes"]["min"]["humidity_pct"]["year"] == 1992
    service = WeatherClimateService(None, "unused.json")
    payload = {"end_date": END.isoformat(), "calendar_averages": rows}
    assert service._valid_summary(payload)
    del rows["09-18"]["extremes"]
    assert not service._valid_summary(payload)


def test_invalid_daily_extreme_rejected():
    data = history()
    data["daily"]["temperature_2m_min"][0] = None
    with pytest.raises(ValueError):
        summarize_weather_history(data, END)
