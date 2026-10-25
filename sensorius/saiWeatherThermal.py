"""Derive outdoor thermal comfort temperatures using the Caelus formulas.

Inputs use degrees Fahrenheit, relative humidity percent, and station wind in
mph. Invalid or inapplicable observations return None rather than an estimate.
"""

import math
from typing import Any


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def calculate_feels_like(
    temperature_f: Any, relative_humidity: Any, wind_mph: Any
) -> float | None:
    """Return Steadman apparent temperature in °F from temperature, RH and wind.

    Uses the Bureau of Meteorology's shade approximation (no solar radiation):
    https://www.bom.gov.au/info/thermal_stress/#atapproximation.
    Wind is the station measurement, without adjustment for sensor height.
    """
    temperature = _finite_number(temperature_f)
    humidity = _finite_number(relative_humidity)
    wind = _finite_number(wind_mph)
    if (temperature is None or humidity is None or wind is None
            or not 0 <= humidity <= 100 or wind < 0):
        return None
    temperature_c = (temperature - 32) * 5 / 9
    if temperature_c <= -237.7:
        return None
    try:
        vapor_pressure_hpa = humidity / 100 * 6.105 * math.exp(
            17.27 * temperature_c / (237.7 + temperature_c)
        )
        apparent_c = temperature_c + 0.33 * vapor_pressure_hpa - 0.70 * wind * 0.44704 - 4
        apparent_f = apparent_c * 9 / 5 + 32
    except OverflowError:
        return None
    return apparent_f if math.isfinite(apparent_f) else None


def calculate_wind_chill(temperature_f: Any, wind_mph: Any) -> float | None:
    """Return NWS wind chill in °F at ≤50°F and wind >3 mph, else unavailable."""
    temperature = _finite_number(temperature_f)
    wind = _finite_number(wind_mph)
    if temperature is None or wind is None or temperature > 50 or wind <= 3:
        return None
    wind_factor = wind**0.16
    chill = 35.74 + 0.6215 * temperature - 35.75 * wind_factor + 0.4275 * temperature * wind_factor
    return chill if math.isfinite(chill) else None
