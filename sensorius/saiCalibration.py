"""Sensor calibration routines and persistence helpers for Sensorius.

Provides device and system calibration workflows, applying offsets to sensor
settings and coordinating calibration tasks used by the web UI and runtime.
"""
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from .saiSensor import get_sensor_controller
from .saiDataLogger import saiDataLogger
from .saiSensorSettingsManager import SensorSettingsManager
from .saiUtils import printDM, debug_enabled

MODULE = "saiCalibration"
DEBUG = debug_enabled(MODULE)

MIN_SPAN_SECONDS = 24 * 3600
MAX_ALIGNMENT_DELTA = 60.0  # seconds for pairing T/RH
MIN_PAIRS = 50
METRIC_PAIR_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("Temperature", "Rel-Humidity"),
    ("Plant Temperature", "Plant Rel-Humidity"),
)

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
      - or a nested dict under keys 'system', 'device', 'soil', 'apvpd';
        soil values are stored under Calibration.Device
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
            ("soil",   "Calibration.Device"),
            ("apvpd",  "Calibration"),
        ):
            block = calib.get(section_key)
            if not block:
                continue
            for k, v in block.items():
                _set_path(f"{table_name}.{k}", v)

    try:
        mgr.save(sensor_id, doc)
    except Exception as exc:
        printDM(
            f"apply_calibration_updates_local: failed to save calibration for {sensor_id}: {exc}",
            location=MODULE,
        )
        return False

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

    def _interpolate_at(
        self,
        target_ts: float,
        ts_list: List[float],
        values: List[float],
        max_delta: float,
    ) -> Optional[float]:
        """
        Linear interpolation for target_ts using sorted ts_list/values.
        Returns None if the nearest sample is farther than max_delta.
        """
        if not ts_list:
            return None

        idx = bisect_left(ts_list, target_ts)
        if idx <= 0:
            return values[0] if abs(ts_list[0] - target_ts) <= max_delta else None
        if idx >= len(ts_list):
            return values[-1] if abs(ts_list[-1] - target_ts) <= max_delta else None

        t0 = ts_list[idx - 1]
        t1 = ts_list[idx]
        v0 = values[idx - 1]
        v1 = values[idx]

        nearest_delta = min(abs(target_ts - t0), abs(t1 - target_ts))
        if nearest_delta > max_delta:
            return None

        if t1 == t0:
            return v0

        frac = (target_ts - t0) / (t1 - t0)
        return v0 + (v1 - v0) * frac

    def _filter_outliers_mad(self, values: List[float]) -> List[float]:
        """
        Remove outliers using a MAD-based modified z-score.
        Falls back to original values if filtering becomes too aggressive.
        """
        if len(values) < 10:
            return values

        median = statistics.median(values)
        abs_dev = [abs(v - median) for v in values]
        mad = statistics.median(abs_dev)
        if mad == 0:
            return values

        filtered: List[float] = []
        for v in values:
            z = 0.6745 * (v - median) / mad
            if abs(z) <= 3.5:
                filtered.append(v)

        return filtered if len(filtered) >= MIN_PAIRS else values

    def _metric_pairs_for_sensor(self, sensor_id: str) -> List[Tuple[str, str]]:
        """Return supported temperature/humidity metric pairs for the sensor."""
        metrics = set(self.data_logger.get_available_metrics(sensor_id) or [])
        return [pair for pair in METRIC_PAIR_CANDIDATES if pair[0] in metrics and pair[1] in metrics]

    def get_calibratable_sensors(self) -> List[str]:
        if hasattr(self.data_logger, "get_available_metrics_by_sensor"):
            try:
                metrics_by_sensor = self.data_logger.get_available_metrics_by_sensor() or {}
                result: List[str] = []
                for sensor_id, metrics in metrics_by_sensor.items():
                    metric_set = set(metrics or [])
                    if any(temp in metric_set and rh in metric_set for temp, rh in METRIC_PAIR_CANDIDATES):
                        result.append(sensor_id)
                return result
            except Exception as exc:
                if DEBUG:
                    printDM(
                        f"get_calibratable_sensors: bulk metric lookup failed: {exc}",
                        location=MODULE,
                    )

        sensor_ids = self.data_logger.get_available_sensors()
        result: List[str] = []
        for sensor_id in sensor_ids:
            if self._metric_pairs_for_sensor(sensor_id):
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

        ref_pairs = self._metric_pairs_for_sensor(reference_id)
        if not ref_pairs:
            raise RuntimeError("Reference sensor has no supported metric pair")

        results: Dict[str, SystemCalResult] = {}
        ref_series_cache: Dict[Tuple[str, str], Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]] = {}
        for sensor_id in calibratable:
            if sensor_id == reference_id:
                continue

            sensor_pairs = self._metric_pairs_for_sensor(sensor_id)
            shared_pairs = [pair for pair in METRIC_PAIR_CANDIDATES if pair in ref_pairs and pair in sensor_pairs]
            if not shared_pairs:
                printDM(
                    f"Calibration skipped for {sensor_id}: no shared temp/RH metric pair with reference {reference_id}",
                    location=MODULE,
                )
                continue
            pair = shared_pairs[0]

            if pair not in ref_series_cache:
                ref_T_ts, ref_T_vals = self.data_logger.get_time_series(
                    reference_id, pair[0], start_ts, end_ts
                )
                ref_RH_ts, ref_RH_vals = self.data_logger.get_time_series(
                    reference_id, pair[1], start_ts, end_ts
                )
                if not ref_T_ts or not ref_RH_ts:
                    printDM(
                        f"Calibration skipped for pair {pair}: reference {reference_id} has insufficient data",
                        location=MODULE,
                    )
                    continue
                ref_series_cache[pair] = (list(zip(ref_T_ts, ref_T_vals)), list(zip(ref_RH_ts, ref_RH_vals)))

            ref_T, ref_RH = ref_series_cache[pair]
            try:
                result = self._compute_for_sensor(
                    sensor_id, reference_id, ref_T, ref_RH, start_ts, end_ts, pair
                )
            except Exception as exc:
                printDM(
                    f"Calibration failed for {sensor_id} (ref={reference_id}, pair={pair}): {exc}",
                    location=MODULE,
                )
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
        metric_pair: Tuple[str, str],
    ) -> SystemCalResult:
        temp_metric, rh_metric = metric_pair
        sT_ts, sT_vals = self.data_logger.get_time_series(
            sensor_id, temp_metric, start_ts, end_ts
        )
        sRH_ts, sRH_vals = self.data_logger.get_time_series(
            sensor_id, rh_metric, start_ts, end_ts
        )

        if not sT_ts or not sRH_ts:
            raise RuntimeError("Insufficient data")

        # align via interpolation (per-metric); reject outliers before offsets
        temp_diffs: List[float] = []
        rh_diffs: List[float] = []

        for t_ref, temp_ref in ref_T:
            temp_sensor = self._interpolate_at(t_ref, sT_ts, sT_vals, MAX_ALIGNMENT_DELTA)
            if temp_sensor is None:
                continue
            temp_diffs.append(temp_ref - temp_sensor)

        for t_ref, rh_ref in ref_RH:
            rh_sensor = self._interpolate_at(t_ref, sRH_ts, sRH_vals, MAX_ALIGNMENT_DELTA)
            if rh_sensor is None:
                continue
            rh_diffs.append(rh_ref - rh_sensor)

        temp_used = self._filter_outliers_mad(temp_diffs)
        rh_used = self._filter_outliers_mad(rh_diffs)

        if len(temp_used) < MIN_PAIRS or len(rh_used) < MIN_PAIRS:
            raise RuntimeError("Too few matched pairs")

        temp_offset, temp_rms = self._mean_and_rms(temp_used)
        rh_offset, rh_rms = self._mean_and_rms(rh_used)

        return SystemCalResult(
            temp_offset=temp_offset,
            rh_offset=rh_offset,
            temp_rms=temp_rms,
            rh_rms=rh_rms,
            n_pairs=len(temp_used),
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
        cal["CALIB_STATUS"] = "Calibrated"

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
        
