"""Soil-only Modbus sensor startup and snapshot collection."""

from time import sleep

from cpynodus_ii.features.derived_metrics import enrich_metrics
from cpynodus_ii.features.sensor_service import (
    SensorService,
    _apply_linear_calibration,
    _compact_metrics,
    _maybe_round,
    _ready_sensor_snapshot,
)

_SOIL_ALT_PH_REG = 0x0007
_SOIL_ALT_EC_REG = 0x000C
_SOIL_BLOCK_READ_MAX = 8


class SoilModbusClient:
    """Provide minimal Modbus register reads over a UART transport."""

    def __init__(self, uart_transport, *, address, channel_name=""):
        self.uart_transport = uart_transport
        self.address = int(address or 1)
        self.channel_name = str(channel_name or "").strip().upper()

    def read_registers(self, start, count):
        """Read one or more holding registers from the configured sensor."""
        count = int(count)
        request = bytes(
            [
                self.address & 0xFF,
                0x03,
                (int(start) >> 8) & 0xFF,
                int(start) & 0xFF,
                (count >> 8) & 0xFF,
                count & 0xFF,
            ]
        )
        crc = _modbus_crc16(request)
        _drain_uart_input(self.uart_transport)
        self.uart_transport.write(request + bytes([crc & 0xFF, (crc >> 8) & 0xFF]))
        sleep(0.05)
        response_len = 5 + (count * 2)
        response = self.uart_transport.read(response_len + len(request) + 2)
        registers = _parse_modbus_register_response(
            response, address=self.address, count=count
        )
        if registers is None:
            return None
        if int(count) == 1:
            return registers[0]
        return registers

    def deinit(self):
        """Keep shutdown compatible with the generic service stop path."""
        return None


def start_soil_sensor_service(sensor_runtime, sensor_adapter, runtime_config):
    """Create a soil sensor service from a bound UART/RS485 adapter."""
    if sensor_adapter.phase != "bound":
        return SensorService(
            phase=sensor_adapter.phase,
            device=sensor_runtime.device,
            interface=sensor_runtime.interface,
            driver_kind="",
            transport=sensor_adapter.transport,
            errors=sensor_adapter.errors or sensor_runtime.errors,
        )

    sensor = runtime_config.sensor
    transport = sensor_adapter.transport
    driver = transport
    if isinstance(transport, tuple):
        driver = tuple(
            (
                channel,
                item
                if hasattr(item, "read_registers")
                else SoilModbusClient(
                    item,
                    address=getattr(channel, "address", 1),
                    channel_name=getattr(channel, "name", ""),
                ),
            )
            for channel, item in transport
        )
    elif transport is not None and not hasattr(transport, "read_registers"):
        channels = getattr(getattr(sensor, "modbus", None), "channels", ())
        channel = tuple(channels or (None,))[0]
        fallback_address = getattr(getattr(sensor, "modbus", None), "address", 1)
        driver = SoilModbusClient(
            transport,
            address=getattr(channel, "address", fallback_address),
            channel_name=getattr(channel, "name", ""),
        )
    return SensorService(
        phase="ready",
        device=sensor.device,
        interface=sensor.interface,
        driver_kind="soil_modbus_uart",
        driver=driver,
        transport=transport,
        errors=(),
    )


def read_soil_sensor_snapshot(sensor_service, runtime_config, sensor):
    """Read a normalized soil snapshot from a ready soil sensor service."""
    transport = sensor_service.driver or sensor_service.transport
    metrics = _read_soil_snapshot_metrics(transport, sensor)
    metrics = enrich_metrics(sensor.device, metrics, runtime_config=runtime_config)
    return _ready_sensor_snapshot(sensor, metrics)


def _drain_uart_input(uart_transport):
    try:
        pending = int(getattr(uart_transport, "in_waiting", 0) or 0)
    except (TypeError, ValueError):
        pending = 0
    if pending <= 0:
        return
    try:
        uart_transport.read(min(pending, 64))
    except Exception:
        pass


def _read_soil_snapshot_metrics(transport, sensor):
    if isinstance(transport, tuple):
        result = {}
        active = []
        for item in transport:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            channel, channel_transport = item[0], item[1]
            metrics = _compact_metrics(
                _read_soil_metrics(channel_transport, sensor, channel=channel)
            )
            if metrics:
                active.append(
                    (
                        getattr(channel, "name", "") or "CH{}".format(len(active) + 1),
                        metrics,
                    )
                )
        if len(active) == 1:
            return active[0][1]
        for name, metrics in active:
            prefix = "{} ".format(str(name or "").strip().upper())
            for key, value in metrics.items():
                result["{}{}".format(prefix, key)] = value
        return result
    return _compact_metrics(_read_soil_metrics(transport, sensor))


def _read_soil_metrics(transport, sensor, channel=None):
    if transport is None or not hasattr(transport, "read_registers"):
        return {}
    registers = sensor.soil_registers
    scales = sensor.soil_scales
    if registers is None or scales is None:
        return {}
    calibration = getattr(sensor, "calibration_device", None)
    fallback_registers = ()
    if _soil_variant_supports_ph(sensor, channel=channel):
        fallback_registers = (_SOIL_ALT_PH_REG, _SOIL_ALT_EC_REG)
    raw_values = _read_soil_register_values(
        transport,
        (
            registers.temperature,
            registers.moisture,
            registers.ec,
            registers.ph,
            registers.n,
            registers.p,
            registers.k,
        )
        + fallback_registers,
    )
    metrics = {
        "Soil Temp_C": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    _soil_raw_value(raw_values, registers.temperature),
                    scales.temperature,
                    None,
                ),
                getattr(calibration, "soil_temp_cal_val", 0.0),
            ),
            2,
        ),
        "Soil Moisture": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    _soil_raw_value(raw_values, registers.moisture),
                    scales.moisture,
                    None,
                ),
                getattr(calibration, "soil_moist_cal_val", 0.0),
            ),
            0,
        ),
        "Soil EC": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    _soil_raw_value(raw_values, registers.ec), scales.ec, None
                ),
                getattr(calibration, "soil_ec_cal_val", 0.0),
            ),
            2,
        ),
        "Soil pH": _maybe_round(
            _apply_linear_calibration(
                _scale_register(
                    _soil_raw_value(raw_values, registers.ph), scales.ph, None
                ),
                getattr(calibration, "soil_ph_cal_val", 0.0),
            ),
            1,
        ),
        "Soil Nitrogen": _scale_register(
            _soil_raw_value(raw_values, registers.n), scales.n, 0
        ),
        "Soil Phosphorus": _scale_register(
            _soil_raw_value(raw_values, registers.p), scales.p, 0
        ),
        "Soil Potassium": _scale_register(
            _soil_raw_value(raw_values, registers.k), scales.k, 0
        ),
    }
    return _apply_soil_register_fallbacks(
        sensor, channel, registers, scales, calibration, metrics, raw_values
    )


def _read_soil_register_values(transport, registers):
    values = {}
    pending = sorted(
        {int(reg) for reg in registers if reg is not None and int(reg) >= 0}
    )
    index = 0
    while index < len(pending):
        start = pending[index]
        end = start
        index += 1
        while index < len(pending):
            candidate = pending[index]
            if candidate - start >= _SOIL_BLOCK_READ_MAX:
                break
            end = candidate
            index += 1
        count = end - start + 1
        block = transport.read_registers(start, count)
        if isinstance(block, (tuple, list)) and len(block) == count:
            for offset, value in enumerate(block):
                reg = start + offset
                if reg in pending:
                    values[reg] = value
            continue
        for reg in pending:
            if start <= reg <= end and reg not in values:
                single = transport.read_registers(reg, 1)
                if single is not None:
                    values[reg] = single
    return values


def _soil_raw_value(raw_values, register):
    try:
        return raw_values.get(int(register))
    except (TypeError, ValueError):
        return None


def _apply_soil_register_fallbacks(
    sensor, channel, registers, scales, calibration, metrics, raw_values
):
    if not _soil_variant_supports_ph(sensor, channel=channel):
        return metrics

    fallback_active = False
    if (
        getattr(registers, "ph", None) != _SOIL_ALT_PH_REG
        and not _soil_ph_is_valid(metrics.get("Soil pH"))
    ):
        fallback_ph = _calibrated_soil_value(
            _soil_raw_value(raw_values, _SOIL_ALT_PH_REG),
            scales.ph,
            1,
            getattr(calibration, "soil_ph_cal_val", 0.0),
        )
        if _soil_ph_is_valid(fallback_ph):
            metrics["Soil pH"] = fallback_ph
            fallback_active = True

    if (
        fallback_active
        and getattr(registers, "ec", None) != _SOIL_ALT_EC_REG
        and not _soil_positive_value(metrics.get("Soil EC"))
    ):
        fallback_ec = _calibrated_soil_value(
            _soil_raw_value(raw_values, _SOIL_ALT_EC_REG),
            scales.ec,
            2,
            getattr(calibration, "soil_ec_cal_val", 0.0),
        )
        if _soil_positive_value(fallback_ec):
            metrics["Soil EC"] = fallback_ec

    return metrics


def _calibrated_soil_value(raw_value, scale, digits, offset):
    value = _scale_register(raw_value, scale, None)
    value = _apply_linear_calibration(value, offset)
    return _maybe_round(value, digits)


def _soil_variant_supports_ph(sensor, *, channel=None):
    variant = ""
    if channel is not None:
        variant = getattr(channel, "variant", "")
    if not variant:
        modbus = getattr(sensor, "modbus", None)
        channels = tuple(getattr(modbus, "channels", ()) or ())
        if len(channels) == 1:
            variant = getattr(channels[0], "variant", "")
        else:
            variant = getattr(modbus, "variant", "")
    variant = str(variant or "canonical").strip().lower()
    return variant in {"canonical", "soil_4in1", "4in1", "soil_7in1", "7in1"}


def _soil_ph_is_valid(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return 3.0 <= numeric <= 10.5


def _soil_positive_value(value):
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _scale_register(raw_value, scale, digits):
    if raw_value is None:
        return None
    try:
        numeric = float(raw_value) / float(scale or 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if digits is None:
        return numeric
    return round(numeric, digits)


def _modbus_crc16(data):
    crc = 0xFFFF
    for value in bytes(data):
        crc ^= value
        for _ in range(8):
            if (crc & 1) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc


def _parse_modbus_register_response(response, *, address, count):
    expected_len = 5 + (int(count) * 2)
    if not response or len(response) < expected_len:
        return None
    response = bytes(response)
    address = int(address) & 0xFF
    expected_byte_count = int(count) * 2
    max_start = len(response) - expected_len
    for start in range(max_start + 1):
        frame = response[start : start + expected_len]
        body = frame[:-2]
        crc_low = frame[-2]
        crc_high = frame[-1]
        expected_crc = _modbus_crc16(body)
        if (
            crc_low != (expected_crc & 0xFF)
            or crc_high != ((expected_crc >> 8) & 0xFF)
        ):
            continue
        if frame[0] != address:
            continue
        if frame[1] == 0x83:
            return None
        if frame[1] != 0x03:
            continue
        byte_count = frame[2]
        if byte_count != expected_byte_count:
            continue
        data = frame[3 : 3 + byte_count]
        if len(data) != byte_count:
            continue
        return tuple(
            (data[index] << 8) | data[index + 1] for index in range(0, len(data), 2)
        )
    return None
