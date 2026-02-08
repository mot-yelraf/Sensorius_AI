"""Sensor factory for creating concrete sensor backends from settings.

Flow:
1) saiSensorSettingsManager loads per-sensor TOML into a SettingsWrapper.
2) saiSensorFactory reads the settings and instantiates the correct sensor class.
3) saiSensor wraps the created sensor in a controller that runs the read loop
   and logs values to the shared data logger.
"""

import importlib
import time
from saiUtils import printDM, debug_enabled
from dataclasses import dataclass
from typing import Optional
try:
    # lets us open specific /dev/i2c-* numbers if we want to later
    from adafruit_extended_bus import ExtendedI2C as ExtI2C
except Exception:
    ExtI2C = None

MODULE = "saiSensorFactory"
DEBUG = debug_enabled(MODULE)

# Map device key -> (module path, class name) for create_sensor
SENSOR_MODULES = {
    "dummy": ("sensor_modules.sensor_template",  "SensorTemplate"),
    "test":  ("sensor_modules.sensor_template",  "SensorTemplate"),
    "aqi":   ("sensor_modules.sensor_aqi",    "AQISensor"),
    "co2":   ("sensor_modules.sensor_co2",    "SCD30Sensor"),
    "vpd":   ("sensor_modules.sensor_vpd",    "VPDSensor"),
    "avpd":  ("sensor_modules.sensor_vpd",    "VPDSensor"),
    "apvpd": ("sensor_modules.sensor_apvpd",  "VPDPlantSensor"),
    "veml":  ("sensor_modules.sensor_veml",   "VEMLSensor"),
    "soil":("sensor_modules.sensor_soil",   "SoilSensor"),
}

def create_sensor(settings, supervisor):
    """
    Construct sensor instance purely from [Sensor].DEVICE.
    detection happens before this call, in Sensorius.ensure_local_sensor_ids.
    """
    device = (settings.get_setting("Sensor", "DEVICE", "") or "").strip().lower()

    try:
        mod_path, cls_name = SENSOR_MODULES[device]
        if DEBUG:
            printDM(f"sensor config path: {mod_path}:{cls_name}", location=f"{MODULE}.create_sensor")
    except KeyError:
        raise ValueError(f"Unsupported sensor type: {device}")

    try:
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
    except Exception as e:
        raise ImportError(f"Failed to load sensor '{device}' from {mod_path}.{cls_name}: {e}") from e

    return cls(settings, supervisor)

# --- Autodetection on first boot ---
@dataclass
class DeviceDescriptor:
    kind: str                 # "apvpd" | "aqi" | "avpd" | "co2" | "veml" | "soil" 
    bus:  str                 # "i2c-1" | "i2c-0" | None (for soil)
    addrs: tuple[int, ...]    # addresses consumed by this device

# Simple scan cache to avoid repeated i2c scans in one boot phase
_last_scan = None  # dict like: {"i2c-1": set([...]), "i2c-0": set([...])}

# I2C lock behavior for one-shot startup probing.
I2C_LOCK_TIMEOUT_SEC = 1.0
I2C_LOCK_POLL_SEC = 0.01

def find_sensors(known_used: Optional[dict[str, set[int]]] = None) -> list[DeviceDescriptor]:
    """
    Find devices from the current scan, honoring already-used addresses.
    known_used: e.g., {"i2c-1": {0x76}, "i2c-0": set()}
    """
    bus_map = _scan_pi_i2c_busses()
    used = {"i2c-1": set(), "i2c-0": set()}
    if known_used:
        used["i2c-1"] |= set(known_used.get("i2c-1", set()))
        used["i2c-0"] |= set(known_used.get("i2c-0", set()))

    BME280_ADDRS = (0x76, 0x77)
    SCD30_ADDR = 0x61
    VEML7700_ADDR = 0x10

    # free address helpers
    def free(bus, addr): return (addr in bus_map[bus]) and (addr not in used[bus])

    found: list[DeviceDescriptor] = []

    # 1) apvpd: dual BME280
    # two-bus mode: one BME280 per bus, each can be 0x76 or 0x77
    def bme280_free_addrs(bus):
        out = []
        for a in BME280_ADDRS:
            if free(bus, a) and _read_chip_id(bus, a) == 0x60:
                out.append(a)
        return out

    bme_1 = bme280_free_addrs("i2c-1")
    bme_0 = bme280_free_addrs("i2c-0")

    if bme_1 and bme_0:
        chosen_1 = bme_1[0]
        chosen_0 = bme_0[0]
        used["i2c-1"].add(chosen_1)
        used["i2c-0"].add(chosen_0)
        # For two-bus mode, addrs are informational; runtime wiring resolves bus use.
        found.append(DeviceDescriptor("apvpd", "i2c-1", (chosen_1, chosen_0)))
    else:
        # same-bus dual mode: require both 0x76 and 0x77 to be BME280 on one bus
        for bus in ("i2c-1", "i2c-0"):
            if all(free(bus, a) and _read_chip_id(bus, a) == 0x60 for a in BME280_ADDRS):
                used[bus].update(BME280_ADDRS)
                found.append(DeviceDescriptor("apvpd", bus, (0x76, 0x77)))
                break

    # 2) aqi: BME680 is identified by chip-id 0x61 at 0x76/0x77
    found_aqi = False
    for bus in ("i2c-1", "i2c-0"):
        for a in (0x76, 0x77):
            if free(bus, a):
                cid = _read_chip_id(bus, a)
                if cid == 0x61:
                    used[bus].add(a)
                    found.append(DeviceDescriptor("aqi", bus, (a,)))
                    found_aqi = True
                    break
        if found_aqi:
            # only one AQI for now; remove this break if you support multiple
            break

    # 3) avpd: any remaining single BME280 (0x76 or 0x77)
    for bus in ("i2c-1", "i2c-0"):
        for a in (0x76, 0x77):
            if free(bus, a):
                # if it's a 680, skip, already handled above
                cid = _read_chip_id(bus, a)
                if cid == 0x60:
                    used[bus].add(a)
                    found.append(DeviceDescriptor("avpd", bus, (a,)))

    # 4) co2: SCD30 at 0x61 (does not collide with BME addrs)
    for bus in ("i2c-1", "i2c-0"):
        if free(bus, SCD30_ADDR):
            used[bus].add(SCD30_ADDR)
            found.append(DeviceDescriptor("co2", bus, (SCD30_ADDR,)))
            # support multiple if you want; otherwise break
        
    # 5) veml: VEML7700 at 0x10 
    for bus in ("i2c-1", "i2c-0"):
        if free(bus, VEML7700_ADDR):
            used[bus].add(VEML7700_ADDR)
            found.append(DeviceDescriptor("veml", bus, (VEML7700_ADDR,)))
            # support multiple if you want; otherwise break
            
    # 6) soil via RS485 (no I2C address consumption)
    try:
        if _probe_soil_rs485():
            found.append(DeviceDescriptor("soil", None, ()))
    except Exception as e:
        printDM(f"RS485 probe skipped/failed: {e}", location="saiSensorFactory")

    if DEBUG:
        printDM(f"found: {found}", location=f"{MODULE}.find_sensors")
        
    return found
    
# private helpers for find_sensors
def _scan_pi_i2c_busses():
    global _last_scan
    if _last_scan is not None:
        return _last_scan

    import board, busio
    addrs1 = set()
    i2c1 = None
    try:
        i2c1 = busio.I2C(board.SCL, board.SDA)
        if not _try_lock_with_timeout(i2c1, "i2c-1 scan"):
            raise TimeoutError("lock timeout")
        try:
            addrs1 = set(i2c1.scan() or [])
        finally:
            i2c1.unlock()
    except Exception as e:
        printDM(f"i2c-1 scan failed: {e}", location="saiSensorFactory")
    finally:
        try:
            if i2c1: i2c1.deinit()
        except Exception:
            pass

    addrs0 = set()
    if ExtI2C:
        try:
            i2c0 = ExtI2C(0)
            if not _try_lock_with_timeout(i2c0, "i2c-0 scan"):
                raise TimeoutError("lock timeout")
            try:
                addrs0 = set(i2c0.scan() or [])
            finally:
                i2c0.unlock()
        except Exception as e:
            printDM(f"i2c-0 scan failed: {e}", location="saiSensorFactory")
        finally:
            try:
                i2c0.deinit()
            except Exception:
                pass

    _last_scan = {"i2c-1": addrs1, "i2c-0": addrs0}
    printDM(f"I2C scan summary: i2c-1={sorted(addrs1)} i2c-0={sorted(addrs0)}", location="saiSensorFactory")
    return _last_scan

def _read_chip_id(bus_name: str, addr: int) -> Optional[int]:
    """Return BME chip-id (0x60=BME280, 0x61=BME680) or None."""
    i2c = None
    locked = False
    try:
        if bus_name == "i2c-1":
            import board, busio
            i2c = busio.I2C(board.SCL, board.SDA)
        else:
            if not ExtI2C: return None
            i2c = ExtI2C(0)
        if not _try_lock_with_timeout(i2c, f"chip-id read {bus_name}@0x{addr:02x}"):
            return None
        locked = True
        w = bytes([0xD0]); r = bytearray(1)
        i2c.writeto_then_readfrom(addr, w, r)
        return r[0]
    except Exception:
        return None
    finally:
        try:
            if i2c and locked:
                i2c.unlock()
        except Exception:
            pass
        try:
            i2c.deinit()
        except Exception:
            pass

def _try_lock_with_timeout(i2c, what: str) -> bool:
    deadline = time.monotonic() + I2C_LOCK_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if i2c.try_lock():
            return True
        time.sleep(I2C_LOCK_POLL_SEC)
    printDM(f"{what}: failed to acquire I2C lock within {I2C_LOCK_TIMEOUT_SEC:.2f}s", location=MODULE)
    return False

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
