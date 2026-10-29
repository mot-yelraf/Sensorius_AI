"""Normalize station fields and shared derived metrics in Sensorius storage units.

Absolute console pressure stays distinct from relative sea-level pressure, and
raw rolling rain counters are never presented as daily or lifetime rainfall.
"""

FIELD_NAMES = {
    "temperature": "Temperature_F", "humidity": "Rel-Humidity",
    "wind_speed": "Wind Speed", "wind_dir": "Wind Direction",
    "indoor_temperature": "Console Temperature_F",
    "indoor_absolute_pressure": "Console Absolute Baro-Pressure",
    "rain_increment": "Rain",
}


def normalize_acurite(values):
    """Convert Fahrenheit and inHg decoder values into the existing storage units."""
    result = {FIELD_NAMES[key]: value for key, value in values.items() if key in FIELD_NAMES}
    for source, target in (("temperature", "Temperature"), ("indoor_temperature", "Console Temperature")):
        if source in values:
            result[target] = round((values[source] - 32) / 1.8, 2)
    if "indoor_absolute_pressure" in values:
        result["Console Absolute Baro-Pressure"] = round(values["indoor_absolute_pressure"] * 33.8638866667, 2)
    derive_outdoor_air_metrics(result)
    return result


def derive_outdoor_air_metrics(out: dict[str, float]) -> None:
    """Add Sensorius absolute-humidity and VPD metrics from outdoor T/RH."""
    temperature = out.get("Temperature")
    relative_humidity = out.get("Rel-Humidity")
    if temperature is None or relative_humidity is None:
        return
    try:
        temp_c = float(temperature)
        rh = max(0.0, min(100.0, float(relative_humidity)))
        saturation_pa = 610.78 * 10 ** ((7.5 * temp_c) / (237.3 + temp_c))
        actual_pa = saturation_pa * (rh / 100.0)
        absolute_humidity = (actual_pa * 18.016) / (8314.3 * (temp_c + 273.15)) * 1000.0
        vpd = (1.0 - (rh / 100.0)) * saturation_pa / 1000.0
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return
    out["Humidity"] = round(max(0.0, absolute_humidity), 2)
    out["Ambient VPD"] = round(max(0.0, min(5.0, vpd)), 3)



ACURITE_GAUGE_CONFIG = {
    "Console Temperature": {"unit": "°C", "min": -40, "max": 85, "ticks": [-40, 0, 20, 40, 60, 85]},
    "Console Temperature_F": {"unit": "°F", "min": -40, "max": 185, "ticks": [-40, 0, 32, 70, 120, 185]},
    "Console Absolute Baro-Pressure": {"display_label": "Station Pressure", "unit": "hPa", "min": 300, "max": 1100, "ticks": [300, 500, 700, 900, 1100]},
}
