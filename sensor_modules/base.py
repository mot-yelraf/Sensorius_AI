# sensor_modules/base.py - base sensor class
import time
import math
from collections.abc import Iterable as _Iterable
from saiUtils import printDM, debug_enabled, get_timestamp

MODULE = "BaseSensor"
DEBUG = debug_enabled("saiSensorFactory")


class BaseSensor:
    def __init__(self, settings, supervisor):
        self.settings = settings
        self.supervisor = supervisor

        # ---- core state ----
        self.present = False
        self.measurements = []
        self.meas_types = []
        self.unit_map = {}
        self.filtered_data = {}
        self.latest_raw = {}
        self.current_values = {}
        self.current_ts = None
        self.meas_status = ""

        # filters & timing
        self.FILTER_SIZE = 7
        self.IIR_ALPHA = 1.0 / self.FILTER_SIZE
        self.meas_interval = int(15)
        self.publish_interval = int(60)

        # ---- Sensor identity (aligned with [Sensor]) ----
        try:
            self.device = self.settings.get_setting("Sensor", "DEVICE", "Unknown")
            self.serial_num = self.settings.get_setting("Sensor", "SERIAL_NUM", "Unknown")
            self.sensor_id = self.settings.get_setting("Sensor", "SENSOR_ID", "Unknown")
            self.location = self.settings.get_setting("Sensor", "LOCATION", "Unknown")
        except Exception:
            # Very defensive: in unit tests or dict-style configs we may not have get_setting
            self.device = "Unknown"
            self.serial_num = "Unknown"
            self.sensor_id = "Unknown"
            self.location = "Unknown"

        # ---- Calibration metadata (generic, not sensor-specific) ----
        #
        # We now expect the new nested layout:
        #
        # [Calibration]
        # CALIBRATED   = false
        # CALIB_STATUS = "Not Calibrated"
        #
        # [Calibration.System]
        # [Calibration.Device]
        # [Calibration.Soil]
        #
        # Concrete sensor classes (CO2, AQI, APVPD, VEML, Soil…) are responsible
        # for loading and applying their own per-metric offsets from these blocks.
        #
        self.can_be_calibrated = False
        self.is_calibrated_flag = False
        self.is_calibrated = "Not Calibrated"

        # Convenience dicts so subclasses can use them directly if desired
        self.calibration_root: dict = {}
        self.calibration_device: dict = {}
        self.calibration_system: dict = {}
        self.calibration_soil: dict = {}

        try:
            # Legacy flag (if present) still honored
            self.can_be_calibrated = bool(
                self.settings.get_setting("Calibration", "CAN_BE_CALD", False)
            )
        except Exception:
            self.can_be_calibrated = False

        # Try to resolve underlying config for nested sections as dicts
        cfg = {}
        try:
            if hasattr(self.settings, "get_all_settings"):
                cfg = self.settings.get_all_settings() or {}
            elif hasattr(self.settings, "settings"):
                cfg = getattr(self.settings, "settings", {}) or {}
        except Exception:
            cfg = {}

        # Fallback: if we didn't get a dict, we still use get_setting()
        self.calibration_root = cfg.get("Calibration", {}) if isinstance(cfg, dict) else {}

        # Root/calibration flags
        try:
            # CALIBRATED: bool flag
            self.is_calibrated_flag = bool(
                self.settings.get_setting("Calibration", "CALIBRATED", False)
            )
        except Exception:
            # fall back to dict view if available
            self.is_calibrated_flag = bool(self.calibration_root.get("CALIBRATED", False))

        try:
            self.is_calibrated = self.settings.get_setting(
                "Calibration", "CALIB_STATUS", "Not Calibrated"
            )
        except Exception:
            self.is_calibrated = self.calibration_root.get(
                "CALIB_STATUS",
                "Calibrated" if self.is_calibrated_flag else "Not Calibrated",
            )

        # Nested calibration blocks – best-effort; sensors may override / re-read.
        def _get_section_fallback(section_name: str) -> dict:
            """
            Try settings.get_section("Calibration.X") first (your existing pattern),
            then fall back to cfg["Calibration"]["X"] if available.
            """
            # Example section_name: "Device", "System", "Soil"
            result = {}
            try:
                if hasattr(self.settings, "get_section"):
                    # Existing pattern you already use:
                    #   get_section("Calibration.Device"), etc.
                    sec = self.settings.get_section(f"Calibration.{section_name}") or {}
                    if isinstance(sec, dict):
                        return sec
            except Exception:
                pass

            try:
                if isinstance(self.calibration_root, dict):
                    sec = self.calibration_root.get(section_name, {}) or {}
                    if isinstance(sec, dict):
                        return sec
            except Exception:
                pass

            return result

        self.calibration_device = _get_section_fallback("Device")
        self.calibration_system = _get_section_fallback("System")
        self.calibration_soil = _get_section_fallback("Soil")

        # ---- Display section (optional, but aligned with sensor.toml) ----
        #
        # [Display]
        # METRIC_1..METRIC_6
        #
        self.display_metrics: list[str] = []
        try:
            display_section = {}
            if hasattr(self.settings, "get_section"):
                display_section = self.settings.get_section("Display") or {}
            elif isinstance(cfg, dict):
                display_section = cfg.get("Display", {}) or {}

            self.display_metrics = [
                str(display_section.get(f"METRIC_{i}", "") or "")
                for i in range(1, 7)
            ]
        except Exception:
            self.display_metrics = [""] * 6

    def is_present(self):
        return self.present

    def _find_sensor_bus(self, **kwargs):
        """
        Back-compat wrapper so existing sensors calling self._find_sensor_bus(...)
        keep working. Delegates to module-level find_sensor_bus(**kwargs).
        """
        return find_sensor_bus(**kwargs)

    def find_sensor_bus(self, **kwargs):
        """
        Friendly alias without the leading underscore.
        """
        return find_sensor_bus(**kwargs)

    def _clamp_if_number(self, value, lo, hi):
        if value is None:
            return None
        try:
            v = float(value)
            if v < lo:
                return lo
            if v > hi:
                return hi
            return v
        except Exception:
            return None

    def calculate_absolute_humidity(self, temp_C, rh):
        """
        Calculate absolute humidity in grams per cubic meter (g/m³)
        using temperature (°C) and relative humidity (%)
        """
        # Constants
        mw = 18.016  # molar mass of water vapor [g/mol]
        R = 8314.3   # universal gas constant [J/(kmol·K)]
        # Saturation vapor pressure in Pa
        svp = 610.78 * 10 ** ((7.5 * temp_C) / (237.3 + temp_C))
        # Actual vapor pressure in Pa
        avp = svp * (rh / 100.0)
        # Temperature in Kelvin
        temp_K = temp_C + 273.15
        # AH in g/m³
        ah = (avp * mw) / (R * temp_K) * 1000
        return ah

    def calculate_vpd(self, temp_C, rh):
        svp = 610.78 * 10 ** ((7.5 * temp_C) / (237.3 + temp_C))
        return (1 - (rh / 100.0)) * svp / 1000.0

    def calculate_dewpoint(self, temp_C, rh):
        """
        Magnus-style dew point estimate in °C from air temp (°C) and RH (%).
        """
        try:
            t = float(temp_C)
            h = max(1e-6, min(100.0, float(rh)))
            a = 17.625
            b = 243.04
            gamma = (a * t) / (b + t) + math.log(h / 100.0)
            return (b * gamma) / (a - gamma)
        except Exception:
            return None

    def calculate_dewpoint_depression(self, temp_C, rh):
        """
        Difference between dry bulb temp and dew point (°C).
        """
        try:
            dp = self.calculate_dewpoint(temp_C, rh)
            if dp is None:
                return None
            return float(temp_C) - float(dp)
        except Exception:
            return None

    def calculate_dewvpd_risk(self, temp_C, rh, vpd=None):
        """
        Heuristic condensation/stress risk score (0-100):
        higher risk when dewpoint depression is low and VPD is low.
        """
        try:
            dep = self.calculate_dewpoint_depression(temp_C, rh)
            if dep is None:
                return None
            v = self.calculate_vpd(temp_C, rh) if vpd is None else float(vpd)
            dep = max(0.0, float(dep))
            v = max(0.0, float(v))
            dep_score = max(0.0, min(1.0, (2.0 - dep) / 2.0))
            vpd_score = max(0.0, min(1.0, (0.8 - v) / 0.8))
            return max(0.0, min(100.0, ((0.65 * dep_score) + (0.35 * vpd_score)) * 100.0))
        except Exception:
            return None

    def iir_filter(self, key, new_val):
        prev = self.filtered_data.get(key)
        if prev is None:
            self.filtered_data[key] = new_val
        else:
            self.filtered_data[key] = self.IIR_ALPHA * new_val + (1 - self.IIR_ALPHA) * prev

    def read_sensor_data(self):
        ts = get_timestamp()
        try:
            raw = {name: getter() for name, _, getter, _ in self.measurements}
            for name in self.meas_types:
                val = raw[name]
                self.iir_filter(name, val)
                self.latest_raw[name] = val

            self.current_ts = ts
            self.meas_status = "online"

            for name, _, _, precision in self.measurements:
                filtered = self.filtered_data[name]
                self.current_values[name] = (
                    int(filtered) if precision is None else round(filtered, precision)
                )

            return dict(self.current_values), dict(self.unit_map), ts

        except Exception as exc:
            self.meas_status = "pending"
            printDM(
                f"read_sensor_data error: {exc}",
                location=f"{__name__}.{self.__class__.__name__}.read_sensor_data",
            )
            return {n: None for n in self.meas_types}, {n: u for n, u in self.unit_map.items()}, ts

    def current_data_set(self):
        if not self.current_values or self.current_ts is None:
            return {n: None for n in self.meas_types}, "No Data", get_timestamp()
        return dict(self.current_values), self.meas_status, self.current_ts

#future proof for soil sensor
def _probe_soil_rs485() -> bool:
    """
    Minimal Modbus-RTU 'ping' for soil sensor.
    Pi pins: TX=GPIO14, RX=GPIO15, DE=GPIO18. 9600 baud, addr=1.
    """
    import busio, board, digitalio

    # GPIO → Blinka pins
    uart_tx = board.D14   # GPIO14 (TXD)
    uart_rx = board.D15   # GPIO15 (RXD)
    de_io   = digitalio.DigitalInOut(board.D18)  # GPIO18 for DE/RE
    de_io.direction = digitalio.Direction.OUTPUT

    # Open UART (Blinka exposes /dev/serial0)
    uart = busio.UART(uart_tx, uart_rx, baudrate=9600, timeout=0.3)

    try:
        # Read Holding Registers: addr=1, start=0x0001, count=1
        req = bytearray([0x01, 0x03, 0x00, 0x01, 0x00, 0x01])
        crc = _modbus_crc16(req)
        req += bytes([crc & 0xFF, (crc >> 8) & 0xFF])

        # Drive TX (DE high), send, short settle, DE low → listen
        de_io.value = True
        uart.write(req)
        time.sleep(0.010)
        de_io.value = False

        resp = uart.read(16)  # small buffer
        if not resp or len(resp) < 5:
            return False

        body, lo, hi = resp[:-2], resp[-2], resp[-1]
        calc = _modbus_crc16(body)
        return (lo == (calc & 0xFF)) and (hi == ((calc >> 8) & 0xFF))
    finally:
        try:
            uart.deinit()
        except Exception:
            pass
        try:
            de_io.deinit()
        except Exception:
            pass

def _modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

def close_all_i2c(*objs) -> int:
    """
    Deinit any ExtI2C or busio.I2C handles contained in *objs.
    Accepts single handles, iterables (list/tuple/set), and dicts (uses values).
    Ignores None and non-I2C objects. Returns count of handles closed.
    """
    closed = 0
    seen = set()

    def _walk(x):
        nonlocal closed
        if x is None:
            return
        # dict → values
        if isinstance(x, dict):
            for v in x.values():
                _walk(v)
            return
        # iterable but not string/bytes
        if isinstance(x, (list, tuple, set)):
            for v in x:
                _walk(v)
            return
        # candidate I2C-like object
        obj_id = id(x)
        if obj_id in seen:
            return
        if hasattr(x, "deinit") and callable(getattr(x, "deinit")):
            seen.add(obj_id)
            try:
                x.deinit()
            except Exception as e:
                if DEBUG:
                    printDM(f"I2C deinit error: {e}", location="close_all_i2c")
            else:
                closed += 1

    for o in objs:
        _walk(o)
    if DEBUG:
        printDM(f"Closed {closed} I2C handle(s)", location="close_all_i2c")
    return closed

def find_sensor_bus(
    address=0x76,
    delay: float = 0.2,
    want: str = "any",           # "any" or "both"
    lock_timeout: float = 0.5,
    buses=(1, 0),                # probe /dev/i2c-1 then /dev/i2c-0
    key_style: str = "int",      # "int" -> {1: i2c, 0: i2c}, "name"/"str" -> {"i2c1": i2c, "i2c0": i2c}
):
    """
    Scan specific Linux I²C buses using ExtendedI2C (no board/Pin).

    address: int OR iterable of ints (e.g., {0x76, 0x77})
    want   : "any"  -> return first matching ExtI2C
             "both" -> return dict of bus->ExtI2C for all matches (caller checks completeness)
    key_style: "int" or "name" keys in the returned dict
    """
    # IMPORT HERE so the symbol is always bound in this function scope
    try:
        from adafruit_extended_bus import ExtendedI2C as ExtendedI2C
    except Exception as e:
        raise RuntimeError(f"ExtendedI2C not available; install adafruit-extended-bus: {e}")

    # Normalize target addresses to a set
    from collections.abc import Iterable as _Iterable
    targets = set(address) if isinstance(address, _Iterable) and not isinstance(address, (bytes, bytearray)) else {address}

    found = {}

    for busno in buses:
        i2c = None
        try:
            i2c = ExtendedI2C(busno)  # /dev/i2c-{busno}
            time.sleep(delay)

            t0 = time.monotonic()
            while not i2c.try_lock():
                if time.monotonic() - t0 > lock_timeout:
                    raise TimeoutError(f"I2C lock timeout on i2c-{busno}")
                time.sleep(0.005)

            addrs = set(i2c.scan() or [])
            i2c.unlock()

            # Match if ANY of the target addresses are present on this bus
            if targets & addrs:
                if DEBUG:
                    printDM(f"Found {sorted(hex(a) for a in (targets & addrs))} on i2c-{busno}", location="find_i2c_bus")

                if want == "any":
                    return i2c  # hand ownership to caller

                key = busno if key_style == "int" else f"i2c{busno}"
                found[key] = i2c
                i2c = None  # prevent deinit below; caller owns handles in 'found'
            else:
                try:
                    i2c.deinit()
                except Exception:
                    pass

        except Exception as e:
            if DEBUG:
                printDM(f"Scan failed on i2c-{busno}: {e}", location="find_i2c_bus")
            if i2c:
                try:
                    i2c.deinit()
                except Exception:
                    pass

    if want == "both":
        return found  # possibly partial; caller validates required keys
    return None
