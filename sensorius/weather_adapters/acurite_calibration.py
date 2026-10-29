"""Apply AcuRite station calibration before persistence and downstream delivery.

Outdoor offsets use Celsius and RH percentage points. System altitude converts
console absolute pressure to sea-level pressure using the same standard-atmosphere
formula as local Sensorius pressure sensors; absolute pressure remains unchanged.
"""

import math
from .metrics import derive_outdoor_air_metrics


def system_altitude_meters(value) -> float | None:
    """Accept a finite configured system altitude, including sea level and negatives."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        altitude = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return altitude if math.isfinite(altitude) and -500 <= altitude <= 10000 else None


def calibrate_acurite_measurements(values: dict, calibration: dict, altitude: float | None) -> dict:
    """Correct a fresh snapshot once, retaining raw absolute and indoor readings."""
    offsets = {}
    for key in ("TEMP_OFFSET", "RH_OFFSET"):
        value = calibration.get(key, 0.0)
        if isinstance(value, bool):
            raise ValueError(f"AcuRite {key} must be a finite number.")
        try:
            offset = float(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"AcuRite {key} must be a finite number.") from exc
        if not math.isfinite(offset):
            raise ValueError(f"AcuRite {key} must be a finite number.")
        offsets[key] = offset
    corrected = dict(values)
    if "Temperature" in corrected:
        temperature = corrected["Temperature"] + offsets["TEMP_OFFSET"]
        corrected["Temperature"] = round(temperature, 2)
        corrected["Temperature_F"] = round(temperature * 1.8 + 32, 1)
    if "Rel-Humidity" in corrected:
        corrected["Rel-Humidity"] = round(max(0.0, min(100.0, corrected["Rel-Humidity"] + offsets["RH_OFFSET"])), 1)
    for metric in ("Humidity", "Ambient VPD", "Baro-Pressure"):
        corrected.pop(metric, None)
    derive_outdoor_air_metrics(corrected)
    absolute = corrected.get("Console Absolute Baro-Pressure")
    if absolute is not None and altitude is not None:
        corrected["Baro-Pressure"] = round(absolute / (1 - altitude / 44330.0) ** 5.255, 1)
    return corrected
