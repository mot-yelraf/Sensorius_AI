"""Define normalized runtime configuration models for firmware startup.

These dataclasses translate persisted settings and detected hardware into a
stable in-memory model that the rest of the firmware can consume without
depending on legacy configuration layout details.
"""

from dataclasses import dataclass, field


def _raw_setattr(instance, name, value):
    setattr(instance, name, value)


def _normalize_profile(profile):
    value = str(profile or "nodusweb").strip().lower()
    if value == "standalone":
        return "nodusweb"
    return value or "nodusweb"


def _clean_str(value):
    return str(value or "").strip()


def _looks_like_ip_literal(value):
    text = str(value or "").strip()
    if not text:
        return False
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    return ":" in text


def _normalize_tz_offset(value):
    offset = int(value or 0)
    if -14 <= offset <= 14:
        return offset * 3600
    return offset


@dataclass
class I2CConfig:
    """Normalized I2C sensor transport settings."""

    bus: int = 0
    scl_pin: str = ""
    sda_pin: str = ""
    address: int = 0

    def __post_init__(self):
        _raw_setattr(self, "bus", int(self.bus or 0))
        _raw_setattr(self, "scl_pin", _clean_str(self.scl_pin))
        _raw_setattr(self, "sda_pin", _clean_str(self.sda_pin))
        _raw_setattr(self, "address", int(self.address or 0))


@dataclass
class SoilModbusChannelConfig:
    """Normalized soil sensor Modbus settings for one RS485 channel."""

    name: str = ""
    uart_tx: str = "GP0"
    uart_rx: str = "GP1"
    baud: int = 9600
    timeout_s: float = 0.30
    address: int = 1
    variant: str = "canonical"

    def __post_init__(self):
        _raw_setattr(self, "name", _clean_str(self.name).upper())
        _raw_setattr(self, "uart_tx", _clean_str(self.uart_tx) or "GP0")
        _raw_setattr(self, "uart_rx", _clean_str(self.uart_rx) or "GP1")
        _raw_setattr(self, "baud", int(self.baud or 9600))
        _raw_setattr(self, "timeout_s", float(self.timeout_s or 0.30))
        _raw_setattr(self, "address", int(self.address or 1))
        _raw_setattr(self, "variant", _clean_str(self.variant) or "canonical")


@dataclass
class SoilModbusConfig:
    """Normalized soil sensor Modbus transport settings."""

    uart_tx: str = "GP0"
    uart_rx: str = "GP1"
    baud: int = 9600
    timeout_s: float = 0.30
    address: int = 1
    variant: str = "canonical"
    channels: tuple = ()

    def __post_init__(self):
        _raw_setattr(self, "uart_tx", _clean_str(self.uart_tx) or "GP0")
        _raw_setattr(self, "uart_rx", _clean_str(self.uart_rx) or "GP1")
        _raw_setattr(self, "baud", int(self.baud or 9600))
        _raw_setattr(self, "timeout_s", float(self.timeout_s or 0.30))
        _raw_setattr(self, "address", int(self.address or 1))
        _raw_setattr(self, "variant", _clean_str(self.variant) or "canonical")
        channels = tuple(self.channels or ())
        if not channels:
            channels = (
                SoilModbusChannelConfig(
                    name="CH1",
                    uart_tx=self.uart_tx,
                    uart_rx=self.uart_rx,
                    baud=self.baud,
                    timeout_s=self.timeout_s,
                    address=self.address,
                    variant=self.variant,
                ),
            )
        _raw_setattr(self, "channels", channels)


@dataclass
class SoilRegisterMap:
    """Normalized soil register mapping."""

    temperature: int = 0
    moisture: int = 1
    ec: int = 2
    ph: int = 3
    n: int = 4
    p: int = 5
    k: int = 6


@dataclass
class SoilScaleMap:
    """Normalized soil scaling values."""

    moisture: float = 10.0
    temperature: float = 10.0
    ec: float = 1.0
    ph: float = 10.0
    n: float = 1.0
    p: float = 1.0
    k: float = 1.0


@dataclass
class SoilThresholdConfig:
    """Normalized soil deficit thresholds."""

    wet_pct: float = 38.0
    dry_pct: float = 18.0


@dataclass
class SoilStressConfig:
    """Normalized soil stress weighting and temperature bands."""

    temp_low_crit_c: float = 15.0
    temp_low_ok_c: float = 18.0
    temp_high_ok_c: float = 24.0
    temp_high_crit_c: float = 30.0
    moisture_weight_pct: float = 70.0
    temp_weight_pct: float = 30.0


@dataclass
class SoilNPKConfig:
    """Normalized NPK target values for soil fertility scoring."""

    n_target: float = 20.0
    p_target: float = 30.0
    k_target: float = 70.0

    def __post_init__(self):
        _raw_setattr(self, "n_target", float(self.n_target or 0.0))
        _raw_setattr(self, "p_target", float(self.p_target or 0.0))
        _raw_setattr(self, "k_target", float(self.k_target or 0.0))


@dataclass
class SensorCalibration:
    """Normalized calibration values for sensor metrics and device setup."""

    temp_offset: float = 0.0
    rh_offset: float = 0.0
    co2_offset: float = 0.0
    aqi_offset: float = 0.0
    gas_offset: float = 0.0
    lux_offset: float = 0.0
    ppfd_offset: float = 0.0
    apvpd_temp_cal_val: float = 0.0
    apvpd_rh_cal_val: float = 0.0
    altitude_meters: float = 0.0
    soil_temp_cal_val: float = 0.0
    soil_moist_cal_val: float = 0.0
    soil_ph_cal_val: float = 0.0
    soil_ec_cal_val: float = 0.0

    def __post_init__(self):
        _raw_setattr(self, "temp_offset", float(self.temp_offset or 0.0))
        _raw_setattr(self, "rh_offset", float(self.rh_offset or 0.0))
        _raw_setattr(self, "co2_offset", float(self.co2_offset or 0.0))
        _raw_setattr(self, "aqi_offset", float(self.aqi_offset or 0.0))
        _raw_setattr(self, "gas_offset", float(self.gas_offset or 0.0))
        _raw_setattr(self, "lux_offset", float(self.lux_offset or 0.0))
        _raw_setattr(self, "ppfd_offset", float(self.ppfd_offset or 0.0))
        _raw_setattr(self, "apvpd_temp_cal_val", float(self.apvpd_temp_cal_val or 0.0))
        _raw_setattr(self, "apvpd_rh_cal_val", float(self.apvpd_rh_cal_val or 0.0))
        _raw_setattr(self, "altitude_meters", float(self.altitude_meters or 0.0))
        _raw_setattr(self, "soil_temp_cal_val", float(self.soil_temp_cal_val or 0.0))
        _raw_setattr(self, "soil_moist_cal_val", float(self.soil_moist_cal_val or 0.0))
        _raw_setattr(self, "soil_ph_cal_val", float(self.soil_ph_cal_val or 0.0))
        _raw_setattr(self, "soil_ec_cal_val", float(self.soil_ec_cal_val or 0.0))

    @property
    def soil_temp_moist_val(self):
        """Legacy alias for the soil moisture calibration offset."""
        return self.soil_moist_cal_val

    @soil_temp_moist_val.setter
    def soil_temp_moist_val(self, value):
        _raw_setattr(self, "soil_moist_cal_val", float(value or 0.0))


@dataclass
class DisplayConfig:
    """Normalized display metric and style selections."""

    metrics: tuple = ()
    styles: tuple = ()

    def __post_init__(self):
        metrics = tuple(_clean_str(value) for value in (self.metrics or ()))
        styles = tuple(_clean_str(value) for value in (self.styles or ()))
        while len(metrics) < 6:
            metrics = metrics + ("",)
        while len(styles) < 6:
            styles = styles + ("",)
        _raw_setattr(self, "metrics", metrics[:6])
        _raw_setattr(self, "styles", styles[:6])


@dataclass
class NetworkConfig:
    """Normalized network and AP bootstrap settings."""

    ssid: str = ""
    password: str = ""
    ap_ssid: str = "Nodus_Setup"
    ap_password: str = "password"
    ap_channel: int = 6
    hostname: str = ""
    http_port: int = 8000

    def __post_init__(self):
        _raw_setattr(self, "ssid", _clean_str(self.ssid))
        _raw_setattr(self, "password", _clean_str(self.password))
        _raw_setattr(self, "ap_ssid", _clean_str(self.ap_ssid) or "Nodus_Setup")
        _raw_setattr(self, "ap_password", _clean_str(self.ap_password) or "password")
        _raw_setattr(self, "ap_channel", max(1, min(11, int(self.ap_channel or 6))))
        _raw_setattr(self, "hostname", _clean_str(self.hostname))
        _raw_setattr(self, "http_port", int(self.http_port or 8000))


@dataclass
class MQTTConfig:
    """Normalized MQTT connection and topic settings."""

    broker: str = ""
    broker_ip: str = ""
    port: int = 1883
    use_tls: bool = False
    base_topic: str = "nodus"
    username: str = ""
    password: str = ""

    def __post_init__(self):
        _raw_setattr(self, "broker", _clean_str(self.broker))
        _raw_setattr(self, "broker_ip", _clean_str(self.broker_ip))
        _raw_setattr(self, "port", int(self.port or 1883))
        _raw_setattr(self, "use_tls", bool(self.use_tls))
        _raw_setattr(self, "base_topic", _clean_str(self.base_topic) or "nodus")
        _raw_setattr(self, "username", _clean_str(self.username))
        _raw_setattr(self, "password", _clean_str(self.password))

    @property
    def preferred_host(self):
        return self.broker_ip or self.broker

    @property
    def connection_targets(self):
        targets = []
        if self.broker_ip:
            targets.append(self.broker_ip)
        if _looks_like_ip_literal(self.broker) and self.broker not in targets:
            targets.append(self.broker)
        return tuple(targets)


@dataclass
class HomeAssistantConfig:
    """Normalized Home Assistant integration settings."""

    discovery_prefix: str = "homeassistant"
    base_topic: str = "nodus"
    publish_discovery_retain: bool = True
    publish_state_retain: bool = True
    publish_legacy_sensor_topic: bool = True

    def __post_init__(self):
        _raw_setattr(
            self,
            "discovery_prefix",
            _clean_str(self.discovery_prefix) or "homeassistant",
        )
        _raw_setattr(self, "base_topic", _clean_str(self.base_topic) or "nodus")
        _raw_setattr(
            self, "publish_discovery_retain", bool(self.publish_discovery_retain)
        )
        _raw_setattr(self, "publish_state_retain", bool(self.publish_state_retain))
        _raw_setattr(
            self, "publish_legacy_sensor_topic", bool(self.publish_legacy_sensor_topic)
        )


@dataclass
class TimeConfig:
    """Normalized time settings."""

    tz: str = "America/Denver"
    tz_offset: int = -25200
    tz_name: str = "MST"
    ntp_server: str = ""
    ntp_server_ip: str = ""

    def __post_init__(self):
        _raw_setattr(self, "tz", _clean_str(self.tz) or "America/Denver")
        _raw_setattr(self, "tz_offset", _normalize_tz_offset(self.tz_offset or -25200))
        _raw_setattr(self, "tz_name", _clean_str(self.tz_name) or "MST")
        _raw_setattr(self, "ntp_server", _clean_str(self.ntp_server))
        _raw_setattr(self, "ntp_server_ip", _clean_str(self.ntp_server_ip))


@dataclass
class DetectedSensor:
    """Describe the sensor family detected or configured for this boot."""

    family: str = ""
    interface: str = ""
    active_config_file: str = ""
    device: str = ""
    sensor_id: str = ""
    serial_number: str = ""
    location: str = ""
    i2c: I2CConfig | None = None
    secondary_i2c: I2CConfig | None = None
    modbus: SoilModbusConfig | None = None
    soil_registers: SoilRegisterMap | None = None
    soil_scales: SoilScaleMap | None = None
    soil_thresholds: SoilThresholdConfig | None = None
    soil_stress: SoilStressConfig | None = None
    soil_npk: SoilNPKConfig | None = None
    calibration_system: SensorCalibration = field(default_factory=SensorCalibration)
    calibration_device: SensorCalibration = field(default_factory=SensorCalibration)
    display: DisplayConfig = field(default_factory=DisplayConfig)

    def __post_init__(self):
        family = str(self.family or "").strip().lower()
        interface = str(self.interface or "").strip().lower()

        if family not in {"", "i2c", "soil"}:
            family = ""
        if not interface:
            interface = self.default_interface_for_family(family)
        if interface not in {"", "i2c", "modbus_rs485"}:
            interface = ""

        active_config_file = str(self.active_config_file or "").strip()
        if not active_config_file:
            active_config_file = self.default_config_file_for_family(family)

        _raw_setattr(self, "family", family)
        _raw_setattr(self, "interface", interface)
        _raw_setattr(self, "active_config_file", active_config_file)
        _raw_setattr(self, "device", _clean_str(self.device).lower())
        _raw_setattr(self, "sensor_id", _clean_str(self.sensor_id))
        _raw_setattr(self, "serial_number", _clean_str(self.serial_number))
        _raw_setattr(self, "location", _clean_str(self.location))

    @staticmethod
    def default_interface_for_family(family):
        if family == "i2c":
            return "i2c"
        if family == "soil":
            return "modbus_rs485"
        return ""

    @staticmethod
    def default_config_file_for_family(family):
        if family == "i2c":
            return "sensor_i2c.toml"
        if family == "soil":
            return "sensor_soil.toml"
        return ""

    @property
    def present(self):
        return bool(self.family)

    @property
    def hardware(self):
        """Return the concrete sensor hardware family when it is known."""
        device = _clean_str(self.device).lower()
        if device == "soil":
            modbus = getattr(self, "modbus", None)
            if modbus is not None:
                return _clean_str(getattr(modbus, "variant", ""))
            return ""
        if device in {"avpd", "apvpd"}:
            return "BME280"
        if device == "aqi":
            return "BME680"
        if device in {"aht", "apvpd_aht"}:
            return "AHTx0"
        if device == "lux":
            return "VEML7700"
        if device == "co2":
            address = 0
            if self.i2c is not None:
                try:
                    address = int(getattr(self.i2c, "address", 0) or 0)
                except (TypeError, ValueError):
                    address = 0
            if address == 0x62:
                return "SCD4x"
            return "SCD30"
        return ""


@dataclass
class SwitchConfig:
    """Normalized switch runtime identity and enablement."""

    present: bool = False
    device_id: str = ""
    serial_number: str = ""
    location: str = ""
    channel_count: int = 0
    channels: tuple = ()

    def __post_init__(self):
        _raw_setattr(self, "present", bool(self.present))
        _raw_setattr(self, "device_id", _clean_str(self.device_id))
        _raw_setattr(self, "serial_number", _clean_str(self.serial_number))
        _raw_setattr(self, "location", _clean_str(self.location))
        _raw_setattr(self, "channel_count", max(0, int(self.channel_count or 0)))
        _raw_setattr(self, "channels", tuple(self.channels or ()))


@dataclass
class SwitchChannelConfig:
    """Normalized switch channel definition."""

    key: str = ""
    label: str = ""
    channel_id: str = ""
    enable_pin: str = ""
    control_pin: str = ""
    last_state: bool = False
    override_script: bool = False

    def __post_init__(self):
        _raw_setattr(self, "key", _clean_str(self.key))
        _raw_setattr(self, "label", _clean_str(self.label))
        _raw_setattr(self, "channel_id", _clean_str(self.channel_id))
        _raw_setattr(self, "enable_pin", _clean_str(self.enable_pin))
        _raw_setattr(self, "control_pin", _clean_str(self.control_pin))
        _raw_setattr(self, "last_state", bool(self.last_state))
        _raw_setattr(self, "override_script", bool(self.override_script))


@dataclass
class RuntimeConfig:
    """Describe the normalized runtime configuration for one boot."""

    active_profile: str = "nodusweb"
    network: NetworkConfig = field(default_factory=NetworkConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    sensor: DetectedSensor = field(default_factory=DetectedSensor)
    switch: SwitchConfig = field(default_factory=SwitchConfig)
    ap_mode: bool = False

    def __post_init__(self):
        _raw_setattr(self, "active_profile", _normalize_profile(self.active_profile))
        _raw_setattr(self, "ap_mode", bool(self.ap_mode))

    @property
    def mqtt_enabled(self):
        return self.active_profile in {"sensorius", "weewx", "homeassistant"}

    @property
    def web_enabled(self):
        return self.ap_mode or self.active_profile == "nodusweb"

    @property
    def ntp_enabled(self):
        return (not self.ap_mode) and self.active_profile in {
            "nodusweb",
            "sensorius",
            "weewx",
            "homeassistant",
        }

    @property
    def calibration_mqtt_available(self):
        return self.mqtt_enabled

    @property
    def onboarding_allowed(self):
        return self.ap_mode or self.active_profile == "nodusweb"

    @property
    def switch_config_present(self):
        return self.switch.present
