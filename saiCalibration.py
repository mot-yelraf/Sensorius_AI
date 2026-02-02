"""Sensor calibration routines and persistence helpers for Sensorius.

Provides device and system calibration workflows, applying offsets to sensor
settings and coordinating calibration tasks used by the web UI and runtime.
"""
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from saiSensor import get_sensor_controller
from saiDataLogger import saiDataLogger
from saiSensorSettingsManager import SensorSettingsManager
from saiUtils import printDM, debug_enabled

MODULE = "saiCalibration"
DEBUG = debug_enabled(MODULE)

MIN_SPAN_SECONDS = 24 * 3600
MAX_ALIGNMENT_DELTA = 60.0  # seconds for pairing T/RH
MIN_PAIRS = 50

@dataclass
class SystemCalResult:
    temp_offset: float
    rh_offset: float
    temp_rms: float
    rh_rms: float
    n_pairs: int
    ref_sensor_id: str
    start_ts: float
    end_ts: float

def apply_calibration_updates_local(sensor_id: str, offsets: list[dict] | dict) -> bool:
    """
    Persist calibration offsets into sensor_settings/<sensor_id>/sensor.toml.
    `offsets` may be:
      - list of { "key": "Calibration.Device.CO2_OFFSET", "value": -700.0 }
      - or a nested dict under keys 'system', 'device', 'soil', 'apvpd'
    """
    mgr = SensorSettingsManager("sensor_settings")
    doc = mgr.load(sensor_id) or {}

    def _set_path(path: str, value):
        parts = path.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    if isinstance(offsets, list):
        for item in offsets:
            key = item.get("key")
            if not key:
                continue
            _set_path(key, item.get("value"))
    else:
        calib = offsets or {}
        for section_key, table_name in (
            ("system", "Calibration.System"),
            ("device", "Calibration.Device"),
            ("soil",   "Calibration.Soil"),
            ("apvpd",  "Calibration"),
        ):
            block = calib.get(section_key)
            if not block:
                continue
            for k, v in block.items():
                _set_path(f"{table_name}.{k}", v)

    mgr.save(sensor_id, doc)
    if DEBUG:
        printDM(f"Calibration updated for {sensor_id}", location=MODULE)
    return True

def notify_sensor_runtime_of_calibration(supervisor, sensor_id: str) -> None:
    """
    After calibration offsets are written to sensor_settings/<sensor_id>/sensor.toml,
    ask the *running* SensorController (if any) to reload calibration values from
    a fresh settings snapshot.

    'supervisor' is kept for back-compat but not used here; we resolve the
    controller via saiSensor's registry.
    """
    # 1) Find the controller via the registry, not via TaskSupervisor
    controller = get_sensor_controller(sensor_id)
    if not controller:
        if DEBUG:
            printDM(
                f"notify_sensor_runtime_of_calibration: no SensorController for {sensor_id}",
                location=MODULE,
            )
        return

    # 2) Load fresh settings from disk for this sensor_id
    try:
        mgr = SensorSettingsManager("sensor_settings")
        fresh_cfg = mgr.load(sensor_id)  # OrderedDict/dict with [Calibration] etc.
    except FileNotFoundError:
        if DEBUG:
            printDM(
                f"notify_sensor_runtime_of_calibration: no TOML for {sensor_id}",
                location=MODULE,
            )
        return
    except Exception as exc:
        printDM(
            f"notify_sensor_runtime_of_calibration: load({sensor_id}) failed: {exc}",
            location=MODULE,
        )
        return

    # 3) Ask the sensor to reload calibration from these fresh settings
    sensor_obj = getattr(controller, "sensor", None)
    if sensor_obj is None:
        if DEBUG:
            printDM(
                f"notify_sensor_runtime_of_calibration: controller.sensor is None for {sensor_id}",
                location=MODULE,
            )
        return

    try:
        reload_fn = getattr(sensor_obj, "reload_calibration_from_settings", None)
        if callable(reload_fn):
            reload_fn(fresh_cfg)
            if DEBUG:
                printDM(
                    f"notify_sensor_runtime_of_calibration: reloaded calibration "
                    f"for {sensor_id} via reload_calibration_from_settings()",
                    location=MODULE,
                )
        else:
            # Backwards compat: fall back to full sensor recreation if supported
            if hasattr(controller, "reload_sensor_instance"):
                controller.reload_sensor_instance()
                if DEBUG:
                    printDM(
                        f"notify_sensor_runtime_of_calibration: "
                        f"reload_sensor_instance() used for {sensor_id}",
                        location=MODULE,
                    )
            elif DEBUG:
                printDM(
                    f"notify_sensor_runtime_of_calibration: sensor for {sensor_id} "
                    f"does not support reload_calibration_from_settings or "
                    f"controller.reload_sensor_instance()",
                    location=MODULE,
                )
    except Exception as exc:
        printDM(
            f"notify_sensor_runtime_of_calibration: error reloading {sensor_id}: {exc}",
            location=MODULE,
        )
            
class CalibrationManager:
    def __init__(self, data_logger: saiDataLogger, sensor_mgr: SensorSettingsManager):
        self.data_logger = data_logger
        self.sensor_mgr = sensor_mgr
        self._last_results: Dict[str, SystemCalResult] = {}

    def _nearest_timestamp(
        self,
        target_ts: float,
        candidates: List[float],
        max_delta: float,
    ) -> Optional[float]:
        """
        Return the candidate timestamp closest to target_ts within ±max_delta seconds.
        If no candidate is within that window, return None.
        """
        if not candidates:
            return None

        best_ts: Optional[float] = None
        best_delta = max_delta
        for ts in candidates:
            delta = abs(ts - target_ts)
            if delta <= best_delta:
                best_delta = delta
                best_ts = ts

        return best_ts

    def _value_at_time(
        self,
        target_ts: float,
        ts_list: List[float],
        values: List[float],
    ) -> Optional[float]:
        """
        Given a timestamp and parallel lists of timestamps/values, return the value
        for an exact timestamp match. Used after _nearest_timestamp picks a ts.
        """
        for ts, val in zip(ts_list, values):
            if ts == target_ts:
                return val
        return None

    def _mean_and_rms(self, diffs: List[float]) -> Tuple[float, float]:
        """
        Compute (mean, RMS) of a list of differences.
        """
        if not diffs:
            return 0.0, 0.0

        n = float(len(diffs))
        mean = sum(diffs) / n
        sq = [(d - mean) ** 2 for d in diffs]
        rms = (sum(sq) / n) ** 0.5
        return mean, rms

    def get_calibratable_sensors(self) -> List[str]:
        sensor_ids = self.data_logger.get_available_sensors()
        result: List[str] = []
        for sensor_id in sensor_ids:
            metrics = self.data_logger.get_available_metrics(sensor_id) or []
            has_temp = any(m in ("Temperature", "Plant Temperature") for m in metrics)
            has_rh = any(m in ("Rel-Humidity", "Plant Rel-Humidity") for m in metrics)
            if has_temp and has_rh:
                result.append(sensor_id)
        return result

    def compute_system_calibration(
        self,
        reference_id: str,
        start_ts: float,
        end_ts: float,
    ) -> Dict[str, SystemCalResult]:
        if end_ts <= start_ts:
            raise ValueError("end_ts must be greater than start_ts")

        if end_ts - start_ts < MIN_SPAN_SECONDS:
            raise ValueError("Time window too short for system calibration")

        calibratable = self.get_calibratable_sensors()
        if reference_id not in calibratable:
            raise ValueError(f"Reference sensor {reference_id} is not calibratable")

        # get reference series
        ref_T_ts, ref_T_vals = self.data_logger.get_time_series(
            reference_id, "Temperature", start_ts, end_ts
        )
        ref_RH_ts, ref_RH_vals = self.data_logger.get_time_series(
            reference_id, "Rel-Humidity", start_ts, end_ts
        )

        if not ref_T_ts or not ref_RH_ts:
            raise RuntimeError("Reference sensor has insufficient data")

        ref_T = list(zip(ref_T_ts, ref_T_vals))
        ref_RH = list(zip(ref_RH_ts, ref_RH_vals))

        results: Dict[str, SystemCalResult] = {}
        for sensor_id in calibratable:
            if sensor_id == reference_id:
                continue
            try:
                result = self._compute_for_sensor(
                    sensor_id, reference_id, ref_T, ref_RH, start_ts, end_ts
                )
            except Exception as exc:
                if DEBUG:
                    printDM(f"Calibration failed for {exc}", location=MODULE)
                continue
            self._last_results[sensor_id] = result
            results[sensor_id] = result

        return results
    
    def _compute_for_sensor(
        self,
        sensor_id: str,
        reference_id: str,
        ref_T: List[Tuple[float, float]],
        ref_RH: List[Tuple[float, float]],
        start_ts: float,
        end_ts: float,
    ) -> SystemCalResult:
        sT_ts, sT_vals = self.data_logger.get_time_series(
            sensor_id, "Temperature", start_ts, end_ts
        )
        sRH_ts, sRH_vals = self.data_logger.get_time_series(
            sensor_id, "Rel-Humidity", start_ts, end_ts
        )

        if not sT_ts or not sRH_ts:
            raise RuntimeError("Insufficient data")

        # naive nearest-neighbor; can be optimized later
        temp_diffs: List[float] = []
        rh_diffs: List[float] = []

        for t_ref, temp_ref in ref_T:
            t_sensor = self._nearest_timestamp(t_ref, sT_ts, MAX_ALIGNMENT_DELTA)
            if t_sensor is None:
                continue

            temp_sensor = self._value_at_time(t_sensor, sT_ts, sT_vals)
            rh_ref = self._value_at_time(t_ref, [ts for ts, _ in ref_RH], [v for _, v in ref_RH])
            rh_sensor = self._value_at_time(t_sensor, sRH_ts, sRH_vals)
            if temp_sensor is None or rh_ref is None or rh_sensor is None:
                continue

            temp_diffs.append(temp_ref - temp_sensor)
            rh_diffs.append(rh_ref - rh_sensor)

        if len(temp_diffs) < MIN_PAIRS or len(rh_diffs) < MIN_PAIRS:
            raise RuntimeError("Too few matched pairs")

        temp_offset, temp_rms = self._mean_and_rms(temp_diffs)
        rh_offset, rh_rms = self._mean_and_rms(rh_diffs)

        return SystemCalResult(
            temp_offset=temp_offset,
            rh_offset=rh_offset,
            temp_rms=temp_rms,
            rh_rms=rh_rms,
            n_pairs=len(temp_diffs),
            ref_sensor_id=reference_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )

    def apply_system_calibration(
        self,
        sensor_id: str,
        result: SystemCalResult,
    ) -> None:
        doc = self.sensor_mgr.load(sensor_id) or {}
        cal = doc.setdefault("Calibration", {})
        cal["CALIBRATED"] = True
        cal["CALIB_STATUS "] = "Calibrated"

        system = cal.setdefault("System", {})
        system["TEMP_OFFSET"] = round(result.temp_offset, 3)
        system["RH_OFFSET"] = round(result.rh_offset, 3)
        system["REF_SENSOR_ID"] = result.ref_sensor_id
        system["REF_RANGE_HOURS"] = int(
            (result.end_ts - result.start_ts) / 3600.0
        )
        system["REF_START_TS"] = int(result.start_ts)
        system["REF_END_TS"] = int(result.end_ts)

        self.sensor_mgr.save(sensor_id, doc)
        
