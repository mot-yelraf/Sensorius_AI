"""Sensor controller loop that wraps factory-built sensor backends.

Flow:
1) saiSensorSettingsManager loads TOML settings per sensor.
2) saiSensorFactory creates the concrete sensor backend.
3) saiSensor runs the async read loop, logs readings, and exposes current state
   to the rest of the system.
"""

import asyncio
from saiUtils import printDM, debug_enabled, get_timestamp
from saiSensorFactory import create_sensor
from saiDataLogger import saiDataLogger
import time

MODULE = "saiSensor"
DEBUG = debug_enabled(MODULE)

# Registry of live SensorController instances by sensor_id
_SENSOR_CONTROLLERS: dict[str, "SensorController"] = {}

def register_sensor_controller(controller: "SensorController") -> None:
    """
    Register a SensorController by its sensor_id for later lookup.
    Used by calibration and other runtime features that need to access
    the live sensor object.
    """
    try:
        sensor_id = getattr(controller, "sensor_id", None)
        if sensor_id:
            _SENSOR_CONTROLLERS[sensor_id] = controller
            if DEBUG:
                printDM(f"Registered SensorController for {sensor_id}", location=MODULE)
    except Exception as exc:
        printDM(f"register_sensor_controller failed: {exc}", location=MODULE)


def unregister_sensor_controller(sensor_id: str, controller: "SensorController | None" = None) -> None:
    """
    Remove a SensorController from the registry.
    If controller is provided, only remove if it is the registered instance.
    """
    try:
        current = _SENSOR_CONTROLLERS.get(sensor_id)
        if current is None:
            return
        if controller is not None and current is not controller:
            return
        _SENSOR_CONTROLLERS.pop(sensor_id, None)
        if DEBUG:
            printDM(f"Unregistered SensorController for {sensor_id}", location=MODULE)
    except Exception as exc:
        printDM(f"unregister_sensor_controller failed: {exc}", location=MODULE)


def get_sensor_controller(sensor_id: str) -> "SensorController | None":
    return _SENSOR_CONTROLLERS.get(sensor_id)

class SensorController:

    def __init__(self, sensor_settings, supervisor, gc_mgr, data_logger=None):
        self.sensor = create_sensor(sensor_settings, supervisor)
        self.settings = sensor_settings
        self.supervisor = supervisor
        self.gc_mgr = gc_mgr

        self.device = ""
        self.serial_num = ""
        self.sensor_id = ""
        self.location = ""
        self.present = False
        self.meas_interval = 1.0
        self.publish_interval = 1.0
        self.data_logger = data_logger or saiDataLogger()
        self._last_read_error_log = 0.0
        self._read_error_log_interval_s = 30.0
        self._sensor_read_timeout_s = 25.0
        self._db_write_timeout_s = 20.0

        self._sync_from_sensor()

        # --- register controller for live calibration reload ---        
        register_sensor_controller(self)

    def _safe_interval(self, value, default: float, name: str) -> float:
        try:
            numeric = float(value)
            if numeric <= 0.0:
                raise ValueError(f"{name} must be > 0")
            return numeric
        except Exception:
            printDM(
                f"{self.sensor_id or 'unknown'} invalid {name}={value!r}; using {default}",
                location=MODULE,
            )
            return default

    def _sync_from_sensor(self) -> None:
        sensor = self.sensor
        self.device = getattr(sensor, "device", self.device)
        self.serial_num = getattr(sensor, "serial_num", self.serial_num)
        self.sensor_id = getattr(sensor, "sensor_id", self.sensor_id)
        self.location = getattr(sensor, "location", self.location)
        self.present = bool(getattr(sensor, "present", self.present))
        self.meas_interval = self._safe_interval(
            getattr(sensor, "meas_interval", self.meas_interval), 5.0, "meas_interval"
        )
        self.publish_interval = self._safe_interval(
            getattr(sensor, "publish_interval", self.publish_interval), 60.0, "publish_interval"
        )

    def _report_issue(self, message: str, *, recommend_restart: bool = True, issue_type: str = "sensor_warning") -> None:
        task_name = f"{self.sensor_id} Data Collection" if self.sensor_id else "Unknown Data Collection"
        if self.supervisor and hasattr(self.supervisor, "report_issue"):
            self.supervisor.report_issue(
                task_name,
                message,
                recommend_restart=recommend_restart,
                issue_type=issue_type,
            )

    # Series of read-only properties
    @property
    def meas_status(self) -> str:
        return getattr(getattr(self, "sensor", None), "meas_status", "pending")

    @meas_status.setter
    def meas_status(self, value: str) -> None:
        # forward to the underlying sensor object
        if hasattr(self, "sensor"):
            setattr(self.sensor, "meas_status", str(value).strip().lower())

    @property
    def measurements(self):
        return self.sensor.measurements

    @property
    def unit_map(self):
        return self.sensor.unit_map

    @property
    def meas_types(self):
        return self.sensor.meas_types

    def read_sensor_data(self):
        return self.sensor.read_sensor_data()

    def current_data_set(self):
        return self.sensor.current_data_set()

    def reload_calibration(self):
        """
        Called after sensor.toml calibration fields are updated.
        By default, ask the sensor object to re-read calibration from settings.
        """
        try:
            reload_fn = getattr(self.sensor, "reload_calibration_from_settings", None)
            if callable(reload_fn):
                reload_fn(self.settings)
            else:
                # Fallback: recreate the sensor object if the driver doesn't implement reload
                self.reload_sensor_instance()
        except Exception as exc:
            printDM(f"reload_calibration failed for {self.sensor_id}: {exc}", location=MODULE)

    def reload_sensor_instance(self):
        """
        Drop-in backward-compatible rebuild of the sensor object.
        This guarantees any calibration read at construction is refreshed.
        """
        from saiSensorFactory import create_sensor
        old_sensor_id = self.sensor_id
        try:
            self.settings.invalidate_this_cache()
        except Exception:
            pass
        try:
            self.settings._maybe_reload()
        except Exception:
            pass

        self.sensor = create_sensor(self.settings, self.supervisor)
        self._sync_from_sensor()
        if old_sensor_id and old_sensor_id != self.sensor_id:
            unregister_sensor_controller(old_sensor_id, self)
        register_sensor_controller(self)

    def shutdown(self) -> None:
        """Best-effort cleanup for registry/lifecycle management."""
        if self.sensor_id:
            unregister_sensor_controller(self.sensor_id, self)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass

    async def data_collection(self):
        if DEBUG:
            printDM("Starting data collection", location=f"{__name__}.{self.__class__.__name__}.data_collection")

        # ---- throttle + optional reinit knobs (user-defined vars first) ----
        not_present_log_interval_s = 5.0
        not_present_sleep_s        = 2.0
        reinit_attempt_interval_s  = 60.0  # set to 0 to disable reinit attempts
        # --------------------------------------------------------------------

        last_not_present_log   = 0.0
        last_reinit_attempt    = 0.0
     
        while True:
            # If sensor isn't present, throttle logs and avoid a hot loop
            if not self.sensor.present:
   
                self.sensor.meas_status = "pending"
                now = time.monotonic()

                if now - last_not_present_log >= not_present_log_interval_s:
                    printDM(f"Sensor not present: {self.sensor.sensor_id}", location=f"{__name__}.{self.__class__.__name__}.data_collection")
                    last_not_present_log = now

                # Optional: periodic reinit attempt if sensor exposes try_reinit()
                if reinit_attempt_interval_s > 0 and (now - last_reinit_attempt) >= reinit_attempt_interval_s:
                    last_reinit_attempt = now
                    try:
                        maybe_reinit = getattr(self.sensor, "try_reinit", None)
                        if callable(maybe_reinit):
                            if DEBUG:
                                printDM("Attempting sensor reinit...", location=f"{__name__}.{self.__class__.__name__}.data_collection")
                            maybe_reinit()
                            if self.sensor.present and DEBUG:
                                printDM("Sensor reinit succeeded; now present", location=f"{__name__}.{self.__class__.__name__}.data_collection")
                    except Exception as e:
                        if DEBUG:
                            printDM(f"Sensor reinit failed: {e}", location=f"{__name__}.{self.__class__.__name__}.data_collection")

                # feed the watchdog and wait a bit before checking again
                self.supervisor.feedthedogs(f"{self.sensor_id} Data Collection")
                await asyncio.sleep(not_present_sleep_s)
                await asyncio.sleep(0)
                continue

            # --- Normal present path ---
            loop_start = time.monotonic()
            meas_end = 0.0
            try:
                values, units, ts = await asyncio.wait_for(
                    asyncio.to_thread(self.sensor.read_sensor_data),
                    timeout=self._sensor_read_timeout_s,
                )
            except asyncio.TimeoutError:
                self.sensor.meas_status = "pending"
                self.sensor.present = False
                msg = (
                    f"{self.sensor_id or 'unknown'} sensor read exceeded timeout "
                    f"({self._sensor_read_timeout_s:.0f}s); marking sensor not present until reinit succeeds"
                )
                printDM(msg, location=f"{__name__}.{self.__class__.__name__}.data_collection")
                self._report_issue(msg, recommend_restart=True, issue_type="sensor_timeout")
                values = units = ts = None
            except Exception as e:
                self.sensor.meas_status = "pending"
                now = time.monotonic()
                if now - self._last_read_error_log >= self._read_error_log_interval_s:
                    printDM(f"Data collection error: {e}", location=f"{__name__}.{self.__class__.__name__}.data_collection")
                    self._last_read_error_log = now
                values = units = ts = None

            if values is not None and ts is not None:
                try:
                    # Keep DB writes off the event loop so storage latency does not
                    # stall unrelated coroutines and trip the watchdog.
                    await asyncio.wait_for(
                        asyncio.to_thread(self.data_logger.log_readings, ts, self.sensor_id, values),
                        timeout=self._db_write_timeout_s,
                    )
                except asyncio.TimeoutError:
                    msg = (
                        f"{self.sensor_id or 'unknown'} DB write exceeded timeout "
                        f"({self._db_write_timeout_s:.0f}s); readings skipped for this cycle"
                    )
                    printDM(msg, location=f"{__name__}.{self.__class__.__name__}.data_collection")
                    self._report_issue(msg, recommend_restart=False, issue_type="db_timeout")
                except Exception as e:
                    now = time.monotonic()
                    if now - self._last_read_error_log >= self._read_error_log_interval_s:
                        printDM(f"Data collection error: {e}", location=f"{__name__}.{self.__class__.__name__}.data_collection")
                        self._last_read_error_log = now

                if any(value is not None for value in (values or {}).values()):
                    self.sensor.meas_status = "online"
                else:
                    self.sensor.meas_status = "pending"

                if DEBUG:
                    printDM(f"{self.sensor_id} secs, values: {values}", location=f"{__name__}.{self.__class__.__name__}.data_collection")

                meas_end = time.monotonic()

            self.supervisor.feedthedogs(f"{self.sensor_id} Data Collection")

            self.meas_interval = self._safe_interval(
                getattr(self.sensor, "meas_interval", self.meas_interval), self.meas_interval, "meas_interval"
            )
            await asyncio.sleep(self.meas_interval)
            loop_end = time.monotonic()

            if DEBUG:
                printDM(f"{self.sensor_id} loop time: {loop_end - loop_start:.2f}s meas time: {meas_end - loop_start:.2f}s", location=MODULE)

            # Keep original cadence semantics (sleep full meas_interval each loop)
            await asyncio.sleep(0)  # Hook for REPL

    
