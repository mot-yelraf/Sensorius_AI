"""Verify Caelus thermal reference values and applicability boundaries.

Exercise cold, hot, calm, missing, and invalid observations independently of
weather providers and station hardware.
"""

import pytest


@pytest.mark.parametrize(('temperature', 'humidity', 'wind', 'expected'), [
    (41, 91, 0, 38.51), (41, 91, 0.2, 38.40), (45, 72, 0, 42.14),
    (20, 70, 15, 5.89), (90, 70, 0, 102.75), (0, 0, 0, -7.2),
    (75, 100, 0, 85.36), (85, 90, 0, 99.70),
])
def test_feels_like_uses_apparent_temperature_in_cool_and_hot_weather(temperature, humidity, wind, expected):
    from sensorius.saiWeatherThermal import calculate_feels_like

    assert calculate_feels_like(temperature, humidity, wind) == pytest.approx(expected, abs=0.02)


@pytest.mark.parametrize(('temperature', 'humidity', 'wind'), [
    (None, 50, 0), (70, None, 0), (70, -1, 0), (70, 101, 0), ('bad', 50, 0),
    (float('nan'), 50, 0), (70, float('inf'), 0), (True, 50, 0),
    (41, 91, None), (41, 91, -1), (41, 91, float('nan')),
    (41, 91, float('inf')), (41, 91, True), (41, 91, 'bad'),
    (-400, 91, 0), (float('1e308'), 91, 0),
])
def test_feels_like_requires_valid_paired_inputs(temperature, humidity, wind):
    from sensorius.saiWeatherThermal import calculate_feels_like

    assert calculate_feels_like(temperature, humidity, wind) is None


@pytest.mark.parametrize(('temperature', 'wind', 'expected'), [
    (20, 15, 6.22), (0, 10, -15.93), (50, 4, 48.88),
    (50.1, 10, None), (30, 3, None), (30, 0, None), (30, -1, None),
    (None, 10, None), (30, None, None), ('bad', 10, None),
    (30, float('inf'), None), (False, 10, None),
])
def test_wind_chill_validity_and_reference_values(temperature, wind, expected):
    from sensorius.saiWeatherThermal import calculate_wind_chill

    result = calculate_wind_chill(temperature, wind)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.02)
