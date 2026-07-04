"""Load, normalize, bootstrap, and persist firmware settings documents.

This module owns TOML template handling, factory bootstrap writes, hardware
detection materialization, password obfuscation on write, and conversion from
raw files into runtime configuration models.
"""

import os
import random
import time

import cpynodus_ii.core.toml_compat as toml_compat
from cpynodus_ii.core.config import (
    DetectedSensor,
    DisplayConfig,
    HomeAssistantConfig,
    I2CConfig,
    MQTTConfig,
    NetworkConfig,
    RuntimeConfig,
    SensorCalibration,
    SoilModbusChannelConfig,
    SoilModbusConfig,
    SoilNPKConfig,
    SoilRegisterMap,
    SoilScaleMap,
    SoilStressConfig,
    SoilThresholdConfig,
    SwitchChannelConfig,
    SwitchConfig,
    TimeConfig,
)
from cpynodus_ii.core.obfuscation import decode_password, encode_password


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _path_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return -1


def _join_path(root, name):
    root_text = str(root or ".")
    if not root_text or root_text == ".":
        return str(name or "")
    if root_text.endswith("/"):
        return "{}{}".format(root_text, name)
    return "{}/{}".format(root_text, name)


_FACTORY_I2C_PINS = (
    ("GP1", "GP0"),
    ("GP3", "GP2"),
)
_FACTORY_RESET_PIN_NAME = "GP17"
_FACTORY_RESET_HOLD_S = 5.0
_FACTORY_RESET_SAMPLE_S = 0.25
_FACTORY_SWITCH_PINS = {
    1: {"enable": "GP5", "control": "GP28", "label": "Fan"},
    2: {"enable": "GP10", "control": "GP21", "label": "Light"},
}
_FACTORY_SOIL_CHANNELS = (("CH1", "GP0", "GP1"), ("CH2", "GP4", "GP5"))
_FACTORY_SENSOR_DISPLAY_DEFAULTS = {
    "aht": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Temperature",
        "Dew Point",
        "DewVPD Risk",
    ),
    "aqi": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Temperature",
        "Baro-Pressure",
        "Air Quality",
    ),
    "apvpd": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Plant Temperature_F",
        "Plant Rel-Humidity",
        "Plant VPD",
    ),
    "apvpd_aht": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Plant Temperature_F",
        "Plant Rel-Humidity",
        "Plant VPD",
    ),
    "avpd": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Temperature",
        "Baro-Pressure",
        "Dew Point",
    ),
    "co2": (
        "Temperature_F",
        "Rel-Humidity",
        "Ambient VPD",
        "Temperature",
        "Dew Point",
        "CO2",
    ),
    "lux": (
        "Light Intensity",
        "Auto Light",
        "Estimated PPFD",
        "Visible Light Intensity",
        "",
        "",
    ),
}


def _factory_board_profile(board_module=None):
    if _use_pico_factory_defaults(board_module):
        return _PicoFactoryProfile
    from cpynodus_ii.core.board_profile import selected_board_profile

    return selected_board_profile(board_module=board_module)


def _factory_i2c_pins(board_module=None):
    if _use_pico_factory_defaults(board_module):
        return _FACTORY_I2C_PINS
    profile = _factory_board_profile(board_module)
    pins = tuple(getattr(profile, "i2c_pins", ()) or ())
    return pins or _FACTORY_I2C_PINS


def _factory_switch_pins(board_module=None):
    if _use_pico_factory_defaults(board_module):
        return dict(_FACTORY_SWITCH_PINS)
    from cpynodus_ii.core.board_profile import switch_pin_defaults

    defaults = switch_pin_defaults(_factory_board_profile(board_module))
    return defaults or dict(_FACTORY_SWITCH_PINS)


def _factory_soil_channels(board_module=None):
    if _use_pico_factory_defaults(board_module):
        return _FACTORY_SOIL_CHANNELS
    from cpynodus_ii.core.board_profile import soil_channel_defaults

    profile = _factory_board_profile(board_module)
    channels = soil_channel_defaults(profile)
    if channels:
        return channels
    return ()


def _factory_reset_pin_name(board_module=None):
    if _use_pico_factory_defaults(board_module):
        return _FACTORY_RESET_PIN_NAME
    return str(getattr(_factory_board_profile(board_module), "factory_reset_pin", ""))


class _PicoFactoryProfile:
    key = "pico2w"
    i2c_pins = _FACTORY_I2C_PINS
    factory_reset_pin = _FACTORY_RESET_PIN_NAME


def _use_pico_factory_defaults(board_module=None):
    if board_module is None:
        return True
    board_id = str(getattr(board_module, "board_id", "") or "").lower()
    if "xiao" in board_id or ("esp32" in board_id and "s3" in board_id):
        return False
    if getattr(board_module, "GP0", None) is not None:
        return True
    if getattr(board_module, "GP28", None) is not None:
        return True
    has_xiao_pin_shape = (
        getattr(board_module, "SCL", None) is not None
        and getattr(board_module, "SDA", None) is not None
        and getattr(board_module, "D0", None) is not None
    )
    return not has_xiao_pin_shape


class Settings:
    """Provide a narrow host-testable view of runtime configuration."""

    SETTINGS_FILE = "settings.toml"
    SENSOR_I2C_FILE = "sensor_i2c.toml"
    SENSOR_SOIL_FILE = "sensor_soil.toml"
    SWITCH_FILE = "switch.toml"
    BOARD_TEMPLATE_ROOT = "boards"
    BOARD_TEMPLATE_DIR = "templates"
    SETTINGS_DEF_FILE = "settings.toml.def"
    SENSOR_I2C_DEF_FILE = "sensor_i2c.toml.def"
    SENSOR_SOIL_DEF_FILE = "sensor_soil.toml.def"
    SWITCH_DEF_FILE = "switch.toml.def"

    def __init__(
        self,
        *,
        active_profile="nodusweb",
        sensor_enabled=False,
        sensor_kind="",
        sensor_family="",
        sensor_interface="",
        switch_enabled=False,
        mqtt_broker="",
        mqtt_port=1883,
    ):
        if sensor_enabled and not (sensor_family or sensor_kind):
            sensor_family = "i2c"
        sensor = DetectedSensor(
            family=(sensor_family or sensor_kind),
            interface=sensor_interface,
        )
        self._runtime_config = RuntimeConfig(
            active_profile=active_profile,
            mqtt=MQTTConfig(broker=mqtt_broker, port=mqtt_port),
            sensor=sensor,
            switch=SwitchConfig(present=switch_enabled),
        )

    @classmethod
    def from_runtime_config(cls, runtime_config):
        instance = cls()
        instance._runtime_config = runtime_config
        return instance

    @classmethod
    def from_directory(cls, root, *, board_module=None, digitalio_module=None):
        root_path = str(root or ".")
        settings_doc = cls._read_toml_file(_join_path(root_path, cls.SETTINGS_FILE))
        sensor_i2c_doc = cls._read_toml_file(_join_path(root_path, cls.SENSOR_I2C_FILE))
        sensor_soil_doc = cls._read_toml_file(
            _join_path(root_path, cls.SENSOR_SOIL_FILE)
        )
        switch_doc = cls._read_toml_file(_join_path(root_path, cls.SWITCH_FILE))
        runtime_config = cls._runtime_config_from_documents(
            settings_doc=settings_doc,
            sensor_i2c_doc=sensor_i2c_doc,
            sensor_soil_doc=sensor_soil_doc,
            switch_doc=switch_doc,
            board_module=board_module,
            digitalio_module=digitalio_module,
        )
        return cls.from_runtime_config(runtime_config)

    @classmethod
    def from_working_directory(cls):
        if _path_exists(cls.SETTINGS_FILE):
            return cls.from_directory(".")
        return cls()

    @staticmethod
    def _read_toml_file(path):
        if _path_size(path) <= 0:
            backup_path = "{}.bak".format(path)
            if _path_size(backup_path) > 0:
                return toml_compat.load_file(backup_path)
            return {}
        return toml_compat.load_file(path)

    @classmethod
    def _write_toml_file(cls, path, document):
        serialized_document = cls._document_for_write(path, document)
        cls._replace_toml_file(path, cls._dump_toml_for_path(path, serialized_document))

    @classmethod
    def apply_factory_profile_reset_if_requested(
        cls,
        root=".",
        *,
        board_module=None,
        digitalio_module=None,
        time_module=None,
    ):
        """Force the live profile back to nodusweb when reset is held low."""
        path = _join_path(str(root or "."), cls.SETTINGS_FILE)
        if not _path_exists(path):
            return False
        reset_pin_name = _factory_reset_pin_name(board_module)
        if not reset_pin_name:
            return False
        if not cls._pin_held_low(
            reset_pin_name,
            hold_s=_FACTORY_RESET_HOLD_S,
            sample_s=_FACTORY_RESET_SAMPLE_S,
            board_module=board_module,
            digitalio_module=digitalio_module,
            time_module=time_module,
        ):
            return False
        document = cls._read_toml_file(path)
        profile_doc = document.setdefault("Profile", {})
        profile_doc["ACTIVE_PROFILE"] = "nodusweb"
        cls._write_toml_file(path, document)
        return True

    @classmethod
    def make_serial_number(cls, n=6):
        """Return a short lowercase serial suffix for seeded device IDs."""
        chars = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(random.choice(chars) for _ in range(max(1, int(n or 6))))

    @classmethod
    def bootstrap_factory_defaults(
        cls,
        root=".",
        *,
        detect_fn=None,
        board_module=None,
        busio_module=None,
        digitalio_module=None,
    ):
        """Create first-boot TOML files and seed IDs for detected hardware."""
        root_path = str(root or ".")
        cls._ensure_file_from_def(
            root_path,
            cls.SETTINGS_DEF_FILE,
            cls.SETTINGS_FILE,
            board_module=board_module,
        )

        is_first_bootstrap = not any(
            _path_exists(_join_path(root_path, path))
            for path in (cls.SENSOR_I2C_FILE, cls.SENSOR_SOIL_FILE, cls.SWITCH_FILE)
        )

        if is_first_bootstrap:
            detected_device, interfaces = cls._detect_factory_sensor(
                detect_fn=detect_fn,
                board_module=board_module,
                busio_module=busio_module,
            )
            if detected_device == "soil":
                cls._bootstrap_soil_sensor(
                    root_path, interfaces, board_module=board_module
                )
            elif detected_device:
                cls._bootstrap_i2c_sensor(
                    root_path,
                    detected_device,
                    interfaces,
                    board_module=board_module,
                )

            active_switches = cls._detect_factory_switch_channels(
                board_module=board_module,
                digitalio_module=digitalio_module,
            )
            if active_switches:
                cls._bootstrap_switch(
                    root_path, active_switches, board_module=board_module
                )

        cls._ensure_seeded_ids_and_hostname(root_path)
        return cls.from_directory(root_path).runtime_config()

    @classmethod
    def clear_onboarding_state(cls, root="."):
        """Remove any persisted onboarding runtime state file."""
        path = _join_path(str(root or "."), "onboarding_state.json")
        try:
            os.remove(path)
        except OSError:
            return False
        return True

    @classmethod
    def _ensure_file_from_def(cls, root, def_name, live_name, *, board_module=None):
        live_path = _join_path(root, live_name)
        if _path_exists(live_path):
            return False
        for def_path in cls._template_path_candidates(
            root, def_name, board_module=board_module
        ):
            if not _path_exists(def_path):
                continue
            cls._write_toml_file(live_path, cls._read_toml_file(def_path))
            return True
        return False

    @classmethod
    def _bootstrap_i2c_sensor(cls, root, device, interfaces, *, board_module=None):
        cls._ensure_file_from_def(
            root,
            cls.SENSOR_I2C_DEF_FILE,
            cls.SENSOR_I2C_FILE,
            board_module=board_module,
        )
        path = _join_path(root, cls.SENSOR_I2C_FILE)
        document = cls._read_toml_file(path)
        sensor_doc = document.setdefault("Sensor", {})
        i2c_doc = document.setdefault("I2Cbus", {})
        plant_i2c_doc = i2c_doc.setdefault("Plant", {})
        display_doc = document.setdefault("Display", {})

        sensor_doc["DEVICE"] = str(device or "").strip().lower()
        if str(device or "").strip().lower() in {"apvpd", "apvpd_aht"}:
            cls._apply_i2c_bootstrap(
                i2c_doc,
                interfaces.get("i2c0", {}) if isinstance(interfaces, dict) else {},
            )
            cls._apply_i2c_bootstrap(
                plant_i2c_doc,
                interfaces.get("i2c1", {}) if isinstance(interfaces, dict) else {},
            )
        else:
            cls._apply_i2c_bootstrap(
                i2c_doc,
                interfaces.get("i2c", {}) if isinstance(interfaces, dict) else {},
            )
            for key in tuple(plant_i2c_doc.keys()):
                plant_i2c_doc.pop(key, None)

        metrics = _FACTORY_SENSOR_DISPLAY_DEFAULTS.get(sensor_doc["DEVICE"], ())
        for index, metric in enumerate(metrics, start=1):
            key = "METRIC_{}".format(index)
            if not str(display_doc.get(key, "") or "").strip():
                display_doc[key] = metric

        cls._write_toml_file(path, document)

    @staticmethod
    def _apply_i2c_bootstrap(target_doc, i2c):
        if not isinstance(target_doc, dict):
            return
        i2c = i2c if isinstance(i2c, dict) else {}
        if "bus" in i2c:
            target_doc["I2C_BUS"] = int(i2c.get("bus", 0))
        if i2c.get("scl"):
            target_doc["I2C_SCL"] = i2c.get("scl")
        if i2c.get("sda"):
            target_doc["I2C_SDA"] = i2c.get("sda")
        if "addr" in i2c:
            target_doc["I2C_ADDR"] = int(i2c.get("addr", 0))

    @classmethod
    def _bootstrap_soil_sensor(cls, root, interfaces, *, board_module=None):
        cls._ensure_file_from_def(
            root,
            cls.SENSOR_SOIL_DEF_FILE,
            cls.SENSOR_SOIL_FILE,
            board_module=board_module,
        )
        path = _join_path(root, cls.SENSOR_SOIL_FILE)
        document = cls._read_toml_file(path)
        sensor_doc = document.setdefault("Sensor", {})
        modbus_doc = document.setdefault("Modbus", {})

        sensor_doc["DEVICE"] = "soil"
        uart = interfaces.get("uart", {}) if isinstance(interfaces, dict) else {}
        modbus = interfaces.get("modbus", {}) if isinstance(interfaces, dict) else {}
        if uart.get("tx"):
            modbus_doc["UART_TX"] = uart.get("tx")
        if uart.get("rx"):
            modbus_doc["UART_RX"] = uart.get("rx")
        if "baud" in modbus:
            modbus_doc["MODBUS_BAUD"] = int(modbus.get("baud", 9600))
        if "timeout_s" in modbus:
            modbus_doc["MODBUS_TIMEOUT_S"] = float(modbus.get("timeout_s", 0.30))
        if "addr" in modbus:
            modbus_doc["MODBUS_ADDR"] = int(modbus.get("addr", 1))
        if modbus.get("soil_variant"):
            modbus_doc["SOIL_VARIANT"] = str(modbus.get("soil_variant", "canonical"))
        channels = (
            interfaces.get("modbus_channels", ())
            if isinstance(interfaces, dict)
            else ()
        )
        if channels:
            for name in ("CH1", "CH2"):
                channel_doc = modbus_doc.setdefault(name, {})
                channel_doc["UART_TX"] = ""
                channel_doc["UART_RX"] = ""
        for channel in channels or ():
            name = str(channel.get("name", "") or "").strip().upper()
            if name not in {"CH1", "CH2"}:
                continue
            channel_doc = modbus_doc.setdefault(name, {})
            if channel.get("tx"):
                channel_doc["UART_TX"] = channel.get("tx")
            if channel.get("rx"):
                channel_doc["UART_RX"] = channel.get("rx")
            if "baud" in channel:
                channel_doc["MODBUS_BAUD"] = int(channel.get("baud", 9600))
            if "timeout_s" in channel:
                channel_doc["MODBUS_TIMEOUT_S"] = float(channel.get("timeout_s", 0.30))
            if "addr" in channel:
                channel_doc["MODBUS_ADDR"] = int(channel.get("addr", 1))
            if channel.get("soil_variant"):
                channel_doc["SOIL_VARIANT"] = str(
                    channel.get("soil_variant", "canonical")
                )

        cls._write_toml_file(path, document)

    @classmethod
    def _bootstrap_switch(cls, root, active_channels, *, board_module=None):
        cls._ensure_file_from_def(
            root,
            cls.SWITCH_DEF_FILE,
            cls.SWITCH_FILE,
            board_module=board_module,
        )
        path = _join_path(root, cls.SWITCH_FILE)
        document = cls._read_toml_file(path)
        switch_doc = document.setdefault("Switch", {})

        inactive_keys = []
        for index in (1, 2):
            if index in active_channels:
                spec = active_channels[index]
                switch_doc["SWITCH_{}_ENABLE_PIN".format(index)] = spec.get(
                    "enable", ""
                )
                switch_doc["SWITCH_{}_PIN".format(index)] = spec.get("control", "")
                if not str(
                    switch_doc.get("SWITCH_{}_LABEL".format(index), "") or ""
                ).strip():
                    switch_doc["SWITCH_{}_LABEL".format(index)] = spec.get("label", "")
                continue
            for suffix in (
                "_LABEL",
                "_CHANNEL_ID",
                "_ENABLE_PIN",
                "_PIN",
                "_LAST_STATE",
                "_OVERRIDE_SCRIPT",
            ):
                inactive_keys.append("SWITCH_{}{}".format(index, suffix))

        for key in inactive_keys:
            switch_doc.pop(key, None)

        cls._write_toml_file(path, document)

    @classmethod
    def _ensure_seeded_ids_and_hostname(cls, root):
        sensor_doc = {}
        sensor_path = ""
        for candidate in (cls.SENSOR_SOIL_FILE, cls.SENSOR_I2C_FILE):
            path = _join_path(root, candidate)
            document = cls._read_toml_file(path)
            section = document.get("Sensor", {})
            device = str(section.get("DEVICE", "") or "").strip().lower()
            if candidate == cls.SENSOR_SOIL_FILE and device == "soil":
                sensor_doc = document
                sensor_path = path
                break
            if candidate == cls.SENSOR_I2C_FILE and device:
                sensor_doc = document
                sensor_path = path
                break

        sensor_section = (
            sensor_doc.get("Sensor", {}) if isinstance(sensor_doc, dict) else {}
        )
        sensor_device = str(sensor_section.get("DEVICE", "") or "").strip().lower()
        sensor_serial = str(sensor_section.get("SERIAL_NUM", "") or "").strip().lower()
        sensor_id = str(sensor_section.get("SENSOR_ID", "") or "").strip().lower()
        if sensor_device:
            if not sensor_serial:
                sensor_serial = cls.make_serial_number()
                sensor_section["SERIAL_NUM"] = sensor_serial
            if not sensor_id:
                sensor_id = "{}-{}".format(sensor_device, sensor_serial)
                sensor_section["SENSOR_ID"] = sensor_id
            if sensor_path:
                cls._write_toml_file(sensor_path, sensor_doc)

        switch_path = _join_path(root, cls.SWITCH_FILE)
        switch_doc = cls._read_toml_file(switch_path)
        switch_section = (
            switch_doc.get("Switch", {}) if isinstance(switch_doc, dict) else {}
        )
        has_switch = any(
            str(
                switch_section.get("SWITCH_{}_ENABLE_PIN".format(index), "") or ""
            ).strip()
            for index in (1, 2)
        )
        if has_switch:
            switch_serial = (
                str(switch_section.get("DEVICE_SERIAL_NUM", "") or "").strip().lower()
            )
            if not switch_serial:
                switch_serial = sensor_serial or cls.make_serial_number()
                switch_section["DEVICE_SERIAL_NUM"] = switch_serial
            if not str(switch_section.get("SWITCH_DEVICE_ID", "") or "").strip():
                switch_section["SWITCH_DEVICE_ID"] = "switch-{}".format(switch_serial)
            for index in (1, 2):
                en_key = "SWITCH_{}_ENABLE_PIN".format(index)
                if not str(switch_section.get(en_key, "") or "").strip():
                    continue
                id_key = "SWITCH_{}_CHANNEL_ID".format(index)
                existing = str(switch_section.get(id_key, "") or "").strip()
                prefix = "S{}-".format(index)
                if (not existing) or existing == prefix:
                    switch_section[id_key] = "{}{}".format(prefix, switch_serial)
            cls._write_toml_file(switch_path, switch_doc)

        settings_path = _join_path(root, cls.SETTINGS_FILE)
        settings_doc = cls._read_toml_file(settings_path)
        network_doc = settings_doc.setdefault("Network", {})
        if not str(network_doc.get("HOSTNAME", "") or "").strip():
            if sensor_id:
                network_doc["HOSTNAME"] = sensor_id
            elif has_switch:
                network_doc["HOSTNAME"] = (
                    str(switch_section.get("SWITCH_DEVICE_ID", "") or "")
                    .strip()
                    .lower()
                )
        cls._write_toml_file(settings_path, settings_doc)

    @classmethod
    def _detect_factory_sensor(
        cls, *, detect_fn=None, board_module=None, busio_module=None
    ):
        if callable(detect_fn):
            detected = detect_fn()
            if isinstance(detected, tuple) and len(detected) == 2:
                return detected[0], detected[1] or {}
            return "", {}

        board_module = board_module or cls._try_import_module("board")
        busio_module = busio_module or cls._try_import_module("busio")
        if board_module is None or busio_module is None:
            return "", {}

        time.sleep(0.05)
        i2c_pins = _factory_i2c_pins(board_module)
        scans = {}
        for bus_index, pins in enumerate(i2c_pins):
            scl_name, sda_name = pins
            scl = getattr(board_module, scl_name, None)
            sda = getattr(board_module, sda_name, None)
            if scl is None or sda is None:
                continue
            bus = None
            try:
                bus = busio_module.I2C(scl, sda)
                if hasattr(bus, "try_lock") and callable(bus.try_lock):
                    started = time.monotonic()
                    while not bus.try_lock():
                        if (time.monotonic() - started) > 0.2:
                            break
                        time.sleep(0.01)
                if hasattr(bus, "scan"):
                    scans[bus_index] = set(bus.scan())
            except Exception:
                continue
            finally:
                if bus is not None:
                    try:
                        if hasattr(bus, "unlock"):
                            bus.unlock()
                    except Exception:
                        pass
                    try:
                        if hasattr(bus, "deinit"):
                            bus.deinit()
                    except Exception:
                        pass

        if (
            len(i2c_pins) >= 2
            and 0x76 in scans.get(0, set())
            and 0x76 in scans.get(1, set())
        ):
            return "apvpd", {
                "i2c0": {
                    "bus": 0,
                    "scl": i2c_pins[0][0],
                    "sda": i2c_pins[0][1],
                    "addr": 0x76,
                },
                "i2c1": {
                    "bus": 1,
                    "scl": i2c_pins[1][0],
                    "sda": i2c_pins[1][1],
                    "addr": 0x76,
                },
            }

        if (
            len(i2c_pins) >= 2
            and 0x38 in scans.get(0, set())
            and 0x38 in scans.get(1, set())
        ):
            return "apvpd_aht", {
                "i2c0": {
                    "bus": 0,
                    "scl": i2c_pins[0][0],
                    "sda": i2c_pins[0][1],
                    "addr": 0x38,
                },
                "i2c1": {
                    "bus": 1,
                    "scl": i2c_pins[1][0],
                    "sda": i2c_pins[1][1],
                    "addr": 0x38,
                },
            }

        for address, device in (
            (0x77, "aqi"),
            (0x76, "avpd"),
            (0x62, "co2"),
            (0x61, "co2"),
            (0x38, "aht"),
            (0x10, "lux"),
        ):
            for bus_index, pins in enumerate(i2c_pins):
                if address not in scans.get(bus_index, set()):
                    continue
                scl_name, sda_name = pins
                return device, {
                    "i2c": {
                        "bus": bus_index,
                        "scl": scl_name,
                        "sda": sda_name,
                        "addr": address,
                    }
                }

        soil_channels = cls._probe_soil_rs485(
            board_module=board_module,
            busio_module=busio_module,
        )
        if soil_channels:
            first = soil_channels[0]
            return "soil", {
                "uart": {"tx": first.get("tx"), "rx": first.get("rx")},
                "modbus": {
                    "baud": first.get("baud", 9600),
                    "timeout_s": 0.30,
                    "addr": first.get("addr", 1),
                    "soil_variant": first.get("soil_variant", "canonical"),
                },
                "modbus_channels": soil_channels,
            }

        return "", {}

    @classmethod
    def _detect_factory_switch_channels(
        cls, *, board_module=None, digitalio_module=None
    ):
        board_module = board_module or cls._try_import_module("board")
        digitalio_module = digitalio_module or cls._try_import_module("digitalio")
        if board_module is None or digitalio_module is None:
            return {}

        active = {}
        direction = getattr(getattr(digitalio_module, "Direction", None), "INPUT", None)
        pull = getattr(getattr(digitalio_module, "Pull", None), "UP", None)
        for index, spec in _factory_switch_pins(board_module).items():
            pin = getattr(board_module, spec["enable"], None)
            if pin is None:
                continue
            handle = None
            try:
                handle = digitalio_module.DigitalInOut(pin)
                if direction is not None and hasattr(handle, "direction"):
                    handle.direction = direction
                if pull is not None and hasattr(handle, "pull"):
                    handle.pull = pull
                if getattr(handle, "value", True) is False:
                    active[index] = dict(spec)
            except Exception:
                continue
            finally:
                try:
                    if handle is not None and hasattr(handle, "deinit"):
                        handle.deinit()
                except Exception:
                    pass
        return active

    @classmethod
    def _pin_held_low(
        cls,
        pin_name,
        *,
        hold_s,
        sample_s,
        board_module=None,
        digitalio_module=None,
        time_module=None,
    ):
        board_module = board_module or cls._try_import_module("board")
        digitalio_module = digitalio_module or cls._try_import_module("digitalio")
        time_module = time_module or time
        if board_module is None or digitalio_module is None:
            return False
        pin = getattr(board_module, pin_name, None)
        if pin is None:
            return False
        direction = getattr(getattr(digitalio_module, "Direction", None), "INPUT", None)
        pull = getattr(getattr(digitalio_module, "Pull", None), "UP", None)
        handle = None
        try:
            handle = digitalio_module.DigitalInOut(pin)
            if direction is not None and hasattr(handle, "direction"):
                handle.direction = direction
            if pull is not None and hasattr(handle, "pull"):
                handle.pull = pull
            if getattr(handle, "value", True) is not False:
                return False
            started = float(time_module.monotonic())
            while (float(time_module.monotonic()) - started) < float(hold_s):
                if getattr(handle, "value", True) is not False:
                    return False
                time_module.sleep(float(sample_s))
            return getattr(handle, "value", True) is False
        except Exception:
            return False
        finally:
            try:
                if handle is not None and hasattr(handle, "deinit"):
                    handle.deinit()
            except Exception:
                pass

    @classmethod
    def _probe_soil_rs485(cls, *, board_module=None, busio_module=None):
        if (
            board_module is None
            or busio_module is None
            or not hasattr(busio_module, "UART")
        ):
            return None

        def _read_regs(uart_obj, addr, start, count):
            req = bytearray(
                [
                    addr,
                    0x03,
                    (start >> 8) & 0xFF,
                    start & 0xFF,
                    (count >> 8) & 0xFF,
                    count & 0xFF,
                ]
            )
            crc = cls._modbus_crc16(req)
            req += bytes([crc & 0xFF, (crc >> 8) & 0xFF])
            uart_obj.write(req)
            time.sleep(0.05)
            resp = uart_obj.read(64)
            if not resp or len(resp) < 5:
                return None
            body, lo, hi = resp[:-2], resp[-2], resp[-1]
            calc = cls._modbus_crc16(body)
            if lo != (calc & 0xFF) or hi != ((calc >> 8) & 0xFF):
                return None
            if resp[0] != addr or resp[1] != 0x03:
                return None
            byte_count = resp[2]
            if byte_count != int(count) * 2:
                return None
            data = resp[3 : 3 + byte_count]
            if len(data) != byte_count:
                return None
            return tuple((data[i] << 8) | data[i + 1] for i in range(0, len(data), 2))

        def _variant_for_probe(uart_obj, addr):
            regs7 = _read_regs(uart_obj, addr, 0x0000, 7)
            if regs7 is not None and len(regs7) >= 7:
                return "soil_7in1"
            regs4 = _read_regs(uart_obj, addr, 0x0000, 4)
            if regs4 is not None and len(regs4) >= 4:
                return "soil_4in1"
            regs2 = _read_regs(uart_obj, addr, 0x0000, 2)
            if regs2 is not None and len(regs2) >= 2:
                return "soil_2in1"
            return ""

        found = []
        for channel_name, tx_name, rx_name in _factory_soil_channels(board_module):
            tx = getattr(board_module, tx_name, None)
            rx = getattr(board_module, rx_name, None)
            if tx is None or rx is None:
                continue
            for baud in (9600, 4800, 2400):
                uart = None
                found_channel = False
                try:
                    uart = busio_module.UART(tx, rx, baudrate=baud, timeout=0.3)
                    for addr in range(1, 6):
                        variant = _variant_for_probe(uart, addr)
                        if variant:
                            found.append(
                                {
                                    "name": channel_name,
                                    "tx": tx_name,
                                    "rx": rx_name,
                                    "baud": baud,
                                    "addr": addr,
                                    "soil_variant": variant,
                                }
                            )
                            found_channel = True
                            break
                except Exception:
                    continue
                finally:
                    try:
                        if uart is not None and hasattr(uart, "deinit"):
                            uart.deinit()
                    except Exception:
                        pass
                if found_channel:
                    break
        return tuple(found)

    @staticmethod
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

    @staticmethod
    def _try_import_module(module_name):
        try:
            return __import__(module_name)
        except ImportError:
            return None

    @classmethod
    def apply_updates_to_directory(
        cls, root, runtime_config, updates, *, reload_runtime=True
    ):
        """Persist supported TOML updates and return runtime config plus diagnostics."""
        root_path = str(root or ".")
        if not updates:
            return runtime_config, (), ()

        grouped_updates = {}
        for update in updates:
            filename = cls._target_file_for_update(runtime_config, update)
            if not filename:
                continue
            grouped_updates.setdefault(filename, []).append(update)

        applied_updates = []
        try:
            for filename, file_updates in grouped_updates.items():
                path = _join_path(root_path, filename)
                patched_updates = cls._try_patch_toml_scalar_file(
                    path,
                    filename,
                    file_updates,
                )
                if patched_updates is not None:
                    applied_updates.extend(patched_updates)
                    continue
                document = cls._read_toml_file(path)
                for update in file_updates:
                    if cls._apply_update_to_document(document, update):
                        applied_updates.append(update)
                serialized_document = cls._document_for_write(path, document)
                cls._replace_toml_file(
                    path, cls._dump_toml_for_path(path, serialized_document)
                )
        except OSError as exc:
            code = getattr(exc, "errno", None)
            if code in {30} or "read-only" in str(exc).lower():
                return runtime_config, tuple(applied_updates), ("read_only_filesystem",)
            return (
                runtime_config,
                tuple(applied_updates),
                ("persistence_failed", str(exc)),
            )

        if not reload_runtime:
            return runtime_config, tuple(applied_updates), ()

        reloaded = cls.from_directory(root_path).runtime_config()
        return reloaded, tuple(applied_updates), ()

    @staticmethod
    def filesystem_writable(root="/"):
        """Return best-effort filesystem writability for diagnostics."""
        try:
            import storage  # type: ignore
        except ImportError:
            return None
        try:
            mount = storage.getmount(str(root))
        except Exception:
            try:
                mount = storage.getmount("/")
            except Exception:
                return None
        readonly = getattr(mount, "readonly", None)
        if readonly is None:
            return None
        return not bool(readonly)

    @classmethod
    def _runtime_config_from_documents(
        cls,
        *,
        settings_doc,
        sensor_i2c_doc,
        sensor_soil_doc,
        switch_doc,
        board_module=None,
        digitalio_module=None,
    ):
        profile_doc = settings_doc.get("Profile", {})
        network_doc = settings_doc.get("Network", {})
        mqtt_doc = settings_doc.get("MQTT", {})
        homeassistant_doc = settings_doc.get("HomeAssistant", {})
        time_doc = settings_doc.get("Time", {})
        sensor = cls._detect_sensor(
            sensor_i2c_doc=sensor_i2c_doc, sensor_soil_doc=sensor_soil_doc
        )
        switch = cls._detect_switch(
            switch_doc=switch_doc,
            board_module=board_module,
            digitalio_module=digitalio_module,
        )
        return RuntimeConfig(
            active_profile=profile_doc.get("ACTIVE_PROFILE", "nodusweb"),
            network=NetworkConfig(
                ssid=network_doc.get("SSID", ""),
                password=decode_password(
                    network_doc.get("PASSWORD", ""),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
                ap_ssid=network_doc.get("AP_SSID", "Nodus_Setup"),
                ap_password=decode_password(
                    network_doc.get("AP_PASSWORD", "password"),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
                ap_channel=network_doc.get("AP_CHANNEL", 6),
                hostname=network_doc.get("HOSTNAME", ""),
                http_port=network_doc.get("HTTPPORT", 8000),
            ),
            mqtt=MQTTConfig(
                broker=mqtt_doc.get("BROKER", ""),
                broker_ip=mqtt_doc.get("BROKER_IP", ""),
                port=mqtt_doc.get("PORT", 1883),
                use_tls=mqtt_doc.get("USE_TLS", False),
                base_topic=mqtt_doc.get("BASE_TOPIC", "nodus"),
                username=mqtt_doc.get("USERNAME", ""),
                password=decode_password(
                    mqtt_doc.get("PASSWORD", ""),
                    hostname=network_doc.get("HOSTNAME", ""),
                ),
            ),
            homeassistant=HomeAssistantConfig(
                discovery_prefix=homeassistant_doc.get(
                    "DISCOVERY_PREFIX", "homeassistant"
                ),
                base_topic=homeassistant_doc.get("BASE_TOPIC", "nodus"),
                publish_discovery_retain=homeassistant_doc.get(
                    "PUBLISH_DISCOVERY_RETAIN", True
                ),
                publish_state_retain=homeassistant_doc.get(
                    "PUBLISH_STATE_RETAIN", True
                ),
                publish_legacy_sensor_topic=homeassistant_doc.get(
                    "PUBLISH_LEGACY_SENSOR_TOPIC", True
                ),
            ),
            time=TimeConfig(
                tz=time_doc.get("TZ", "America/Denver"),
                tz_offset=time_doc.get("TZ_OFFSET", -25200),
                tz_name=time_doc.get("TZ_NAME", "MST"),
                ntp_server=time_doc.get("NTP_SERVER", ""),
                ntp_server_ip=time_doc.get("NTP_SERVER_IP", ""),
            ),
            sensor=sensor,
            switch=switch,
        )

    @classmethod
    def _detect_sensor(cls, *, sensor_i2c_doc, sensor_soil_doc):
        soil_sensor_doc = sensor_soil_doc.get("Sensor", {})
        soil_modbus_doc = sensor_soil_doc.get("Modbus", {})
        soil_register_doc = sensor_soil_doc.get("SoilSensorRegisters", {})
        soil_scale_doc = sensor_soil_doc.get("SoilSensorScales", {})
        soil_deficit_doc = sensor_soil_doc.get("SoilDeficit", {})
        soil_stress_doc = sensor_soil_doc.get("SoilStress", {})
        soil_npk_doc = sensor_soil_doc.get("NPK", {})
        soil_display_doc = sensor_soil_doc.get("Display", {})
        soil_display_style_doc = sensor_soil_doc.get("Display.Style", {})
        if not soil_display_style_doc and isinstance(soil_display_doc, dict):
            soil_display_style_doc = soil_display_doc.get("Style", {})
        soil_calibration_doc = sensor_soil_doc.get("Calibration", {})
        soil_device_cal_doc = soil_calibration_doc.get("Device", {})
        i2c_sensor_doc = sensor_i2c_doc.get("Sensor", {})
        i2c_bus_doc = sensor_i2c_doc.get("I2Cbus", {})
        i2c_plant_bus_doc = (
            i2c_bus_doc.get("Plant", {}) if isinstance(i2c_bus_doc, dict) else {}
        )
        i2c_display_doc = sensor_i2c_doc.get("Display", {})
        i2c_display_style_doc = sensor_i2c_doc.get("Display.Style", {})
        if not i2c_display_style_doc and isinstance(i2c_display_doc, dict):
            i2c_display_style_doc = i2c_display_doc.get("Style", {})
        i2c_calibration_doc = sensor_i2c_doc.get("Calibration", {})
        i2c_system_cal_doc = i2c_calibration_doc.get("System", {})
        i2c_device_cal_doc = i2c_calibration_doc.get("Device", {})
        soil_device = str(soil_sensor_doc.get("DEVICE", "") or "").strip().lower()
        i2c_device = str(i2c_sensor_doc.get("DEVICE", "") or "").strip().lower()

        if soil_device == "soil":
            return DetectedSensor(
                family="soil",
                interface="modbus_rs485",
                active_config_file=cls.SENSOR_SOIL_FILE,
                device=soil_device,
                sensor_id=soil_sensor_doc.get("SENSOR_ID", ""),
                serial_number=soil_sensor_doc.get("SERIAL_NUM", ""),
                location=soil_sensor_doc.get("LOCATION", ""),
                modbus=SoilModbusConfig(
                    uart_tx=soil_modbus_doc.get("UART_TX", "GP0"),
                    uart_rx=soil_modbus_doc.get("UART_RX", "GP1"),
                    baud=soil_modbus_doc.get("MODBUS_BAUD", 9600),
                    timeout_s=soil_modbus_doc.get("MODBUS_TIMEOUT_S", 0.30),
                    address=soil_modbus_doc.get("MODBUS_ADDR", 1),
                    variant=soil_modbus_doc.get("SOIL_VARIANT", "canonical"),
                    channels=cls._soil_modbus_channels(soil_modbus_doc),
                ),
                soil_registers=SoilRegisterMap(
                    temperature=int(soil_register_doc.get("TEMPERATURE_REG", 0)),
                    moisture=int(soil_register_doc.get("MOISTURE_REG", 1)),
                    ec=int(soil_register_doc.get("EC_REG", 2)),
                    ph=int(soil_register_doc.get("PH_REG", 3)),
                    n=int(soil_register_doc.get("N_REG", 4)),
                    p=int(soil_register_doc.get("P_REG", 5)),
                    k=int(soil_register_doc.get("K_REG", 6)),
                ),
                soil_scales=SoilScaleMap(
                    moisture=float(soil_scale_doc.get("MOISTURE_SCALE", 10.0)),
                    temperature=float(soil_scale_doc.get("TEMPERATURE_SCALE", 10.0)),
                    ec=float(soil_scale_doc.get("EC_SCALE", 1.0)),
                    ph=float(soil_scale_doc.get("PH_SCALE", 10.0)),
                    n=float(soil_scale_doc.get("N_SCALE", 1.0)),
                    p=float(soil_scale_doc.get("P_SCALE", 1.0)),
                    k=float(soil_scale_doc.get("K_SCALE", 1.0)),
                ),
                soil_thresholds=SoilThresholdConfig(
                    wet_pct=float(soil_deficit_doc.get("SPD_WET_THRESHOLD_PCT", 38.0)),
                    dry_pct=float(soil_deficit_doc.get("SPD_DRY_THRESHOLD_PCT", 18.0)),
                ),
                soil_stress=SoilStressConfig(
                    temp_low_crit_c=float(
                        soil_stress_doc.get("SSI_TEMP_LOW_CRIT_C", 15.0)
                    ),
                    temp_low_ok_c=float(soil_stress_doc.get("SSI_TEMP_LOW_OK_C", 18.0)),
                    temp_high_ok_c=float(
                        soil_stress_doc.get("SSI_TEMP_HIGH_OK_C", 24.0)
                    ),
                    temp_high_crit_c=float(
                        soil_stress_doc.get("SSI_TEMP_HIGH_CRIT_C", 30.0)
                    ),
                    moisture_weight_pct=float(
                        soil_stress_doc.get("SSI_MOISTURE_WEIGHT_PCT", 70.0)
                    ),
                    temp_weight_pct=float(
                        soil_stress_doc.get("SSI_TEMP_WEIGHT_PCT", 30.0)
                    ),
                ),
                soil_npk=SoilNPKConfig(
                    n_target=float(soil_npk_doc.get("N_TARGET", 20.0)),
                    p_target=float(soil_npk_doc.get("P_TARGET", 30.0)),
                    k_target=float(soil_npk_doc.get("K_TARGET", 70.0)),
                ),
                display=DisplayConfig(
                    metrics=tuple(
                        soil_display_doc.get(f"METRIC_{index}", "")
                        for index in range(1, 7)
                    ),
                    styles=tuple(
                        soil_display_style_doc.get(f"METRIC_{index}", "")
                        for index in range(1, 7)
                    ),
                ),
                calibration_device=SensorCalibration(
                    soil_temp_cal_val=float(
                        soil_device_cal_doc.get("SOIL_TEMP_CAL_VAL", 0.0)
                    ),
                    soil_moist_cal_val=float(
                        soil_device_cal_doc.get(
                            "SOIL_MOIST_CAL_VAL",
                            soil_device_cal_doc.get("SOIL_TEMP_MOIST_VAL", 0.0),
                        )
                    ),
                    soil_ph_cal_val=float(
                        soil_device_cal_doc.get("SOIL_PH_CAL_VAL", 0.0)
                    ),
                    soil_ec_cal_val=float(
                        soil_device_cal_doc.get("SOIL_EC_CAL_VAL", 0.0)
                    ),
                ),
            )

        if i2c_device:
            return DetectedSensor(
                family="i2c",
                interface="i2c",
                active_config_file=cls.SENSOR_I2C_FILE,
                device=i2c_device,
                sensor_id=i2c_sensor_doc.get("SENSOR_ID", ""),
                serial_number=i2c_sensor_doc.get("SERIAL_NUM", ""),
                location=i2c_sensor_doc.get("LOCATION", ""),
                i2c=I2CConfig(
                    bus=i2c_bus_doc.get("I2C_BUS", 0),
                    scl_pin=i2c_bus_doc.get("I2C_SCL", ""),
                    sda_pin=i2c_bus_doc.get("I2C_SDA", ""),
                    address=i2c_bus_doc.get("I2C_ADDR", 0),
                ),
                secondary_i2c=cls._optional_i2c_config(i2c_plant_bus_doc),
                display=DisplayConfig(
                    metrics=tuple(
                        i2c_display_doc.get(f"METRIC_{index}", "")
                        for index in range(1, 7)
                    ),
                    styles=tuple(
                        i2c_display_style_doc.get(f"METRIC_{index}", "")
                        for index in range(1, 7)
                    ),
                ),
                calibration_system=SensorCalibration(
                    temp_offset=float(i2c_system_cal_doc.get("TEMP_OFFSET", 0.0)),
                    rh_offset=float(i2c_system_cal_doc.get("RH_OFFSET", 0.0)),
                    co2_offset=float(i2c_system_cal_doc.get("CO2_OFFSET", 0.0)),
                ),
                calibration_device=SensorCalibration(
                    temp_offset=float(i2c_device_cal_doc.get("TEMP_OFFSET", 0.0)),
                    rh_offset=float(i2c_device_cal_doc.get("RH_OFFSET", 0.0)),
                    co2_offset=float(i2c_device_cal_doc.get("CO2_OFFSET", 0.0)),
                    aqi_offset=float(i2c_device_cal_doc.get("AQI_OFFSET", 0.0)),
                    gas_offset=float(i2c_device_cal_doc.get("GAS_OFFSET", 0.0)),
                    lux_offset=float(i2c_device_cal_doc.get("LUX_OFFSET", 0.0)),
                    ppfd_offset=float(i2c_device_cal_doc.get("PPFD_OFFSET", 0.0)),
                    apvpd_temp_cal_val=float(
                        i2c_device_cal_doc.get("APVPD_TEMP_CAL_VAL", 0.0)
                    ),
                    apvpd_rh_cal_val=float(
                        i2c_device_cal_doc.get("APVPD_RH_CAL_VAL", 0.0)
                    ),
                    altitude_meters=float(
                        i2c_device_cal_doc.get("ALTITUDE_METERS", 0.0)
                    ),
                ),
            )

        return DetectedSensor()

    @staticmethod
    def _soil_modbus_channels(modbus_doc):
        channels = []
        for name in ("CH1", "CH2"):
            channel_doc = (
                modbus_doc.get(name, {}) if isinstance(modbus_doc, dict) else {}
            )
            if not isinstance(channel_doc, dict) or not channel_doc:
                continue
            tx = channel_doc.get("UART_TX", "")
            rx = channel_doc.get("UART_RX", "")
            if not (str(tx or "").strip() or str(rx or "").strip()):
                continue
            channels.append(
                SoilModbusChannelConfig(
                    name=name,
                    uart_tx=tx,
                    uart_rx=rx,
                    baud=channel_doc.get(
                        "MODBUS_BAUD",
                        modbus_doc.get("MODBUS_BAUD", 9600),
                    ),
                    timeout_s=channel_doc.get(
                        "MODBUS_TIMEOUT_S",
                        modbus_doc.get("MODBUS_TIMEOUT_S", 0.30),
                    ),
                    address=channel_doc.get(
                        "MODBUS_ADDR",
                        modbus_doc.get("MODBUS_ADDR", 1),
                    ),
                    variant=channel_doc.get(
                        "SOIL_VARIANT",
                        modbus_doc.get("SOIL_VARIANT", "canonical"),
                    ),
                )
            )
        return tuple(channels)

    @staticmethod
    def _optional_i2c_config(document):
        if not isinstance(document, dict):
            return None
        has_pin = bool(
            str(document.get("I2C_SCL", "") or "").strip()
            or str(document.get("I2C_SDA", "") or "").strip()
        )
        has_addr = int(document.get("I2C_ADDR", 0) or 0) > 0
        if not (has_pin or has_addr):
            return None
        return I2CConfig(
            bus=document.get("I2C_BUS", 0),
            scl_pin=document.get("I2C_SCL", ""),
            sda_pin=document.get("I2C_SDA", ""),
            address=document.get("I2C_ADDR", 0),
        )

    @classmethod
    def _detect_switch(cls, *, switch_doc, board_module=None, digitalio_module=None):
        switch_section = switch_doc.get("Switch", {})
        if not switch_section:
            return SwitchConfig()

        channels = []
        for index in (1, 2):
            control_pin = str(
                switch_section.get(f"SWITCH_{index}_PIN", "") or ""
            ).strip()
            enable_pin = str(
                switch_section.get(f"SWITCH_{index}_ENABLE_PIN", "") or ""
            ).strip()
            if not enable_pin:
                continue
            asserted = cls._switch_enable_pin_asserted(
                enable_pin,
                board_module=board_module,
                digitalio_module=digitalio_module,
            )
            if asserted is False:
                continue
            channels.append(
                SwitchChannelConfig(
                    key=f"SWITCH_{index}",
                    label=switch_section.get(f"SWITCH_{index}_LABEL", ""),
                    channel_id=switch_section.get(f"SWITCH_{index}_CHANNEL_ID", ""),
                    enable_pin=enable_pin,
                    control_pin=control_pin,
                    last_state=switch_section.get(f"SWITCH_{index}_LAST_STATE", False),
                    override_script=switch_section.get(
                        f"SWITCH_{index}_OVERRIDE_SCRIPT", False
                    ),
                )
            )

        if not channels:
            return SwitchConfig()

        return SwitchConfig(
            present=True,
            device_id=switch_section.get("SWITCH_DEVICE_ID", ""),
            serial_number=switch_section.get("DEVICE_SERIAL_NUM", ""),
            location=switch_section.get("SWITCH_LOCATION", ""),
            channel_count=len(channels),
            channels=tuple(channels),
        )

    @classmethod
    def _switch_enable_pin_asserted(
        cls, pin_name, *, board_module=None, digitalio_module=None
    ):
        board_module = board_module or cls._try_import_module("board")
        digitalio_module = digitalio_module or cls._try_import_module("digitalio")
        if board_module is None or digitalio_module is None:
            return None

        pin = getattr(board_module, str(pin_name or ""), None)
        if pin is None:
            return None

        direction = getattr(getattr(digitalio_module, "Direction", None), "INPUT", None)
        pull = getattr(getattr(digitalio_module, "Pull", None), "UP", None)
        handle = None
        try:
            handle = digitalio_module.DigitalInOut(pin)
            if direction is not None and hasattr(handle, "direction"):
                handle.direction = direction
            if pull is not None and hasattr(handle, "pull"):
                handle.pull = pull
            return getattr(handle, "value", True) is False
        except Exception:
            return None
        finally:
            try:
                if handle is not None and hasattr(handle, "deinit"):
                    handle.deinit()
            except Exception:
                pass

    @classmethod
    def _target_file_for_update(cls, runtime_config, update):
        section = str(update.get("section", "") or "").strip()
        if section == "NPK":
            if runtime_config.sensor.device == "soil":
                if runtime_config.sensor.active_config_file:
                    return runtime_config.sensor.active_config_file
                return cls.SENSOR_SOIL_FILE
            return ""
        if section in {"Network", "Profile", "MQTT", "HomeAssistant", "Time"}:
            return cls.SETTINGS_FILE
        if section == "Switch":
            return cls.SWITCH_FILE
        if section in {
            "Sensor",
            "I2Cbus",
            "Modbus",
            "SoilSensorRegisters",
            "SoilSensorScales",
            "SoilDeficit",
            "SoilStress",
        }:
            if runtime_config.sensor.active_config_file:
                return runtime_config.sensor.active_config_file
            return cls.SENSOR_I2C_FILE
        if section.startswith("Calibration") or section in {"Display", "Display.Style"}:
            if runtime_config.sensor.active_config_file:
                return runtime_config.sensor.active_config_file
            return cls.SENSOR_I2C_FILE
        return ""

    @staticmethod
    def _apply_update_to_document(document, update):
        section = str(update.get("section", "") or "").strip()
        key = str(update.get("key", "") or "").strip()
        if not (section and key):
            return False
        current = document
        for part in section.split("."):
            current = current.setdefault(part, {})
        current[key] = update.get("value")
        return True

    @classmethod
    def _try_patch_toml_scalar_file(cls, path, filename, updates):
        if _path_size(path) <= 0:
            return None
        if cls._scalar_patch_needs_full_write(filename, updates):
            return None

        targets = cls._scalar_patch_targets(updates)
        if targets is None:
            return None
        if not targets:
            return ()

        if not cls._patch_scalar_file_stream(path, targets):
            return None
        return tuple(updates)

    @classmethod
    def _scalar_patch_needs_full_write(cls, filename, updates):
        base_filename = str(filename or "").split("/")[-1]
        if base_filename != cls.SETTINGS_FILE:
            return False
        for update in updates:
            section = str(update.get("section", "") or "").strip()
            key = str(update.get("key", "") or "").strip().upper()
            if (section == "Network" and key in {"PASSWORD", "AP_PASSWORD"}) or (
                section == "MQTT" and key == "PASSWORD"
            ):
                return True
        return False

    @classmethod
    def _scalar_patch_targets(cls, updates):
        targets = []
        seen = []
        for update in updates:
            section = str(update.get("section", "") or "").strip()
            key = str(update.get("key", "") or "").strip()
            if not (section and key):
                return None
            target = (section, key)
            if target in seen:
                return None
            seen.append(target)
            targets.append((section, key, cls._format_toml_scalar(update.get("value"))))
        return tuple(targets)

    @staticmethod
    def _format_toml_scalar(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            text = repr(float(value))
            if "." not in text and "e" not in text.lower():
                text += ".0"
            return text
        text = str(value or "")
        return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))

    @classmethod
    def _patch_scalar_file_stream(cls, path, targets):
        path_text = str(path or "")
        tmp_path = "{}.tmp".format(path_text)
        backup_path = "{}.bak".format(path_text)
        found = [False] * len(targets)
        current_section = ""
        try:
            with open(path_text, "r", encoding="utf-8") as source:
                with open(tmp_path, "w", encoding="utf-8") as target:
                    while True:
                        raw_line = source.readline()
                        if raw_line == "":
                            break
                        section = cls._section_from_toml_line(raw_line)
                        if section is not None:
                            current_section = section
                            target.write(raw_line)
                            continue
                        key = cls._key_from_toml_line(raw_line)
                        replacement = ""
                        if key:
                            replacement = cls._replacement_scalar_line(
                                current_section,
                                key,
                                raw_line,
                                targets,
                                found,
                            )
                        target.write(replacement if replacement else raw_line)
                    try:
                        target.flush()
                    except AttributeError:
                        pass
            if not cls._all_targets_found(found):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return False
            if _path_size(tmp_path) <= 0:
                raise OSError("toml_write_empty_tmp")
            if _path_exists(backup_path):
                os.remove(backup_path)
            if _path_exists(path_text):
                os.rename(path_text, backup_path)
            os.rename(tmp_path, path_text)
            return True
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _section_from_toml_line(raw_line):
        body = str(raw_line or "").split("#", 1)[0].strip()
        if body.startswith("[") and body.endswith("]"):
            return body[1:-1].strip()
        return None

    @staticmethod
    def _key_from_toml_line(raw_line):
        body = str(raw_line or "").split("#", 1)[0]
        if "=" not in body:
            return ""
        return body.split("=", 1)[0].strip()

    @classmethod
    def _replacement_scalar_line(cls, section, key, raw_line, targets, found):
        for index, target in enumerate(targets):
            if found[index]:
                continue
            if section == target[0] and key == target[1]:
                found[index] = True
                return "{} = {}{}".format(key, target[2], cls._line_ending(raw_line))
        return ""

    @staticmethod
    def _line_ending(raw_line):
        text = str(raw_line or "")
        if text.endswith("\r\n"):
            return "\r\n"
        if text.endswith("\n"):
            return "\n"
        return ""

    @staticmethod
    def _all_targets_found(found):
        for item in found:
            if not item:
                return False
        return True

    @classmethod
    def _dump_toml(cls, document):
        return toml_compat.dumps(document)

    @classmethod
    def _dump_toml_for_path(cls, path, document):
        template_path = cls._template_path_for_live_path(path)
        if template_path and _path_exists(template_path):
            try:
                with open(template_path, "r", encoding="utf-8") as handle:
                    return toml_compat.dumps_with_template(document, handle.read())
            except OSError:
                pass
        return cls._dump_toml(document)

    @classmethod
    def _replace_toml_file(cls, path, text):
        path_text = str(path or "")
        tmp_path = "{}.tmp".format(path_text)
        backup_path = "{}.bak".format(path_text)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            try:
                handle.flush()
            except AttributeError:
                pass
        if _path_size(tmp_path) <= 0:
            raise OSError("toml_write_empty_tmp")

        if _path_exists(backup_path):
            os.remove(backup_path)
        if _path_exists(path_text):
            os.rename(path_text, backup_path)
        os.rename(tmp_path, path_text)

    @classmethod
    def _document_for_write(cls, path, document):
        copied = cls._deep_copy_document(document)
        filename = str(path or "").split("/")[-1]
        if filename == cls.SETTINGS_FILE:
            cls._obfuscate_settings_passwords(copied)
        return copied

    @staticmethod
    def _deep_copy_document(value):
        if isinstance(value, dict):
            copied = {}
            for key, item in value.items():
                copied[key] = Settings._deep_copy_document(item)
            return copied
        return value

    @staticmethod
    def _obfuscate_settings_passwords(document):
        if not isinstance(document, dict):
            return
        network_doc = document.get("Network", {})
        mqtt_doc = document.get("MQTT", {})
        hostname = ""
        if isinstance(network_doc, dict):
            hostname = str(network_doc.get("HOSTNAME", "") or "").strip()
            for key in ("PASSWORD", "AP_PASSWORD"):
                if key in network_doc:
                    network_doc[key] = encode_password(
                        network_doc.get(key, ""),
                        hostname=hostname,
                    )
        if isinstance(mqtt_doc, dict) and "PASSWORD" in mqtt_doc:
            mqtt_doc["PASSWORD"] = encode_password(
                mqtt_doc.get("PASSWORD", ""),
                hostname=hostname,
            )

    @classmethod
    def _template_path_for_live_path(cls, path):
        filename = str(path or "").split("/")[-1]
        template_name = {
            cls.SETTINGS_FILE: cls.SETTINGS_DEF_FILE,
            cls.SENSOR_I2C_FILE: cls.SENSOR_I2C_DEF_FILE,
            cls.SENSOR_SOIL_FILE: cls.SENSOR_SOIL_DEF_FILE,
            cls.SWITCH_FILE: cls.SWITCH_DEF_FILE,
        }.get(filename, "")
        if not template_name:
            return ""
        path_text = str(path or "")
        if "/" not in path_text:
            parent = "."
        else:
            parent = path_text.rsplit("/", 1)[0]
        for candidate in cls._template_path_candidates(parent, template_name):
            if _path_exists(candidate):
                return candidate
        return ""

    @classmethod
    def _template_path_candidates(cls, root, def_name, *, board_module=None):
        profile_key = cls._template_board_profile_key(board_module)
        candidates = (
            _join_path(
                _join_path(
                    _join_path(root, cls.BOARD_TEMPLATE_ROOT),
                    profile_key,
                ),
                "{}/{}".format(cls.BOARD_TEMPLATE_DIR, def_name),
            ),
            _join_path(_join_path(root, cls.BOARD_TEMPLATE_ROOT), def_name),
            _join_path(root, def_name),
        )
        unique = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        return tuple(unique)

    @classmethod
    def _template_board_profile_key(cls, board_module=None):
        probe_board = board_module
        if probe_board is None:
            probe_board = cls._try_import_module("board")
        if _use_pico_factory_defaults(probe_board):
            return "pico2w"
        profile = _factory_board_profile(probe_board)
        return str(getattr(profile, "key", "") or "pico2w")

    def active_profile(self):
        return self._runtime_config.active_profile

    def sensor_enabled(self):
        return self._runtime_config.sensor.present

    def sensor_family(self):
        return self._runtime_config.sensor.family

    def sensor_interface(self):
        return self._runtime_config.sensor.interface

    def sensor_kind(self):
        return self._runtime_config.sensor.family

    def active_sensor_file(self):
        return self._runtime_config.sensor.active_config_file

    def switch_enabled(self):
        return self._runtime_config.switch_config_present

    def mqtt_enabled(self):
        return self._runtime_config.mqtt_enabled

    def mqtt_config(self):
        return {
            "BROKER": self._runtime_config.mqtt.broker,
            "PORT": self._runtime_config.mqtt.port,
        }

    def runtime_config(self):
        return self._runtime_config
