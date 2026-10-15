"""Test Ecowitt gateway configuration, weather arrays, and persistence.

The suite covers service settings and persistence behavior around LAN gateway
discovery and ingestion without requiring physical Ecowitt hardware. GW1200
coverage uses the generic API shape expected from a WH65/WS69-class array; a
real Ambient Weather WH65B array still requires field verification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sensorius.saiEcowitt import (
    EcowittError,
    EcowittGatewayIngest,
    ensure_ecowitt_sensor_settings,
    migrate_ecowitt_display_defaults,
    normalize_gateway_url,
)
from sensorius.saiSensorSettingsManager import SensorSettingsManager
from sensorius.sensor_modules.station_ecowitt import normalize_ecowitt_livedata


class _Settings:
    def __init__(self):
        self.values = {
            ("Ecowitt", "ENABLED"): False,
            ("Ecowitt", "GATEWAY_URL"): "",
            ("Ecowitt", "POLL_INTERVAL_SEC"): 60,
            ("Ecowitt", "SENSOR_ID"): "",
        }

    def get_setting(self, section, key, default=None, **_kwargs):
        return self.values.get((section, key), default)

    def set_many_in_memory(self, updates):
        for section, key, value in updates:
            self.values[(section, key)] = value

    def save_settings(self):
        return None

    def replace_setting(self, section, key, value):
        self.values[(section, key)] = value


class _Logger:
    local_tz = None

    def __init__(self, latest=None, timestamp=""):
        self.latest = latest or {}
        self.timestamp = timestamp
        self.rows = []

    def get_latest_values(self, _sensor_id):
        return dict(self.latest)

    def get_latest_timestamp(self, _sensor_id):
        return self.timestamp

    def log_readings(self, timestamp, sensor_id, values):
        self.rows.append((timestamp, sensor_id, dict(values)))
        return True


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    responses = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params=None):
        endpoint = url.rsplit("/", 1)[-1]
        key = (endpoint, (params or {}).get("page"))
        return _Response(self.responses.get(key, self.responses.get(endpoint, {})))


def test_gateway_url_validation():
    assert normalize_gateway_url("http://gw1100.local/") == "http://gw1100.local"
    assert normalize_gateway_url("http://[fd00::10]:8080") == "http://[fd00::10]:8080"
    for value in ("https://gw.local", "http://user:pass@gw.local", "http://gw.local/path", "http://gw.local?q=1"):
        with pytest.raises(EcowittError):
            normalize_gateway_url(value)


def test_rain_delta_restart_and_same_day_reset_are_conservative():
    now = datetime.now(timezone.utc)
    ingest = EcowittGatewayIngest(
        settings=_Settings(),
        data_logger=_Logger({"Rain Day": 1.25}, (now - timedelta(minutes=1)).isoformat()),
    )
    ingest._rain_reset_hour = (now.hour + 1) % 24
    values = {"Rain Day": 1.5}
    ingest._add_interval_rain(values, "ecowitt-e8db840f1543")
    assert values["Rain"] == pytest.approx(0.25)

    reset = {"Rain Day": 0.1}
    ingest._add_interval_rain(reset, "ecowitt-e8db840f1543")
    assert reset["Rain"] == 0.0


def test_first_rain_sample_is_not_synthesized():
    ingest = EcowittGatewayIngest(settings=_Settings(), data_logger=_Logger())
    values = {"Rain Day": 2.0}
    ingest._add_interval_rain(values, "ecowitt-e8db840f1543")
    assert "Rain" not in values


def test_configured_rain_day_boundary_accepts_new_counter_value():
    now = datetime.now(timezone.utc)
    reset_hour = now.hour
    prior = now - timedelta(hours=1)
    ingest = EcowittGatewayIngest(
        settings=_Settings(),
        data_logger=_Logger({"Rain Day": 1.25}, prior.isoformat()),
    )
    ingest._rain_reset_hour = reset_hour
    values = {"Rain Day": 0.1}
    ingest._add_interval_rain(values, "ecowitt-e8db840f1543")
    assert values["Rain"] == 0.1


def test_configured_rain_day_boundary_does_not_subtract_previous_day_counter():
    now = datetime.now(timezone.utc)
    prior = now - timedelta(hours=1)
    ingest = EcowittGatewayIngest(
        settings=_Settings(),
        data_logger=_Logger({"Rain Day": 0.1}, prior.isoformat()),
    )
    ingest._rain_reset_hour = now.hour
    values = {"Rain Day": 0.2}

    ingest._add_interval_rain(values, "ecowitt-e8db840f1543")

    assert values["Rain"] == 0.2


def test_sensor_settings_materialization_is_idempotent(tmp_path):
    manager = SensorSettingsManager(str(tmp_path / "sensor_settings"))
    inventory = [{"id": "E8", "type": "0", "name": "Temp & Humidity & Solar & Wind & Rain"}]
    sensor_id = "ecowitt-e8db840f1543"

    ensure_ecowitt_sensor_settings(sensor_id, inventory=inventory, gateway_model="GW1100A_V2.3.1", manager=manager)
    first = manager.load(sensor_id)
    ensure_ecowitt_sensor_settings(sensor_id, inventory=inventory, gateway_model="GW1100A_V2.3.1", manager=manager)
    second = manager.load(sensor_id)

    assert first == second
    assert second["Sensor"]["TYPE"] == "station"
    assert second["Sensor"]["DEVICE"] == "ecowitt"
    assert second["Display"]["METRIC_1"] == "Temperature_F"
    assert second["Display"]["METRIC_6"] == "Gateway Baro-Pressure"


def test_sensor_settings_migrate_legacy_blank_pressure_default(tmp_path):
    manager = SensorSettingsManager(str(tmp_path / "sensor_settings"))
    sensor_id = "ecowitt-e8db840f1543"
    manager.save(sensor_id, {
        "Sensor": {"TYPE": "station", "DEVICE": "ecowitt", "SENSOR_ID": sensor_id},
        "Display": {"METRIC_6": "Baro-Pressure"},
    })

    assert migrate_ecowitt_display_defaults(sensor_id, manager=manager) is True

    assert manager.load(sensor_id)["Display"]["METRIC_6"] == "Gateway Baro-Pressure"
    assert migrate_ecowitt_display_defaults(sensor_id, manager=manager) is False


def test_poll_interval_is_clamped_for_existing_bad_settings():
    settings = _Settings()
    settings.values[("Ecowitt", "POLL_INTERVAL_SEC")] = 1
    ingest = EcowittGatewayIngest(settings=settings, data_logger=_Logger())
    assert ingest.poll_interval_sec == 60


@pytest.mark.asyncio
async def test_discovery_reads_both_pages_and_uses_gateway_mac(monkeypatch):
    _Client.responses = {
        "get_version": {"version": "Version: GW1100A_V2.3.1", "platform": "ecowitt"},
        "get_network_info": {"mac": "E8:DB:84:0F:15:43", "ssid": "private"},
        ("get_sensors_info", 1): [{"img": "wh69", "type": "0", "name": "Weather array", "id": "E8", "signal": "3"}],
        ("get_sensors_info", 2): [{"img": "wh51", "type": "14", "name": "Soil CH1", "id": "C4BC", "signal": "1"}],
        "get_livedata_info": {"common_list": [{"id": "0x02", "val": "68", "unit": "F"}], "ch_soil": [{"channel": "1", "humidity": "42%"}]},
        "get_rain_totals": {"rainFallPriority": "1"},
    }
    monkeypatch.setattr("sensorius.saiEcowitt.httpx.AsyncClient", _Client)
    ingest = EcowittGatewayIngest(settings=_Settings(), data_logger=_Logger())

    result = await ingest.discover("http://gw1100.local")

    assert result["sensor_id"] == "ecowitt-e8db840f1543"
    assert [sensor["id"] for sensor in result["inventory"]] == ["E8", "C4BC"]
    assert result["live_metric_count"] == 3
    assert "ssid" not in result


@pytest.mark.asyncio
async def test_gw1200_discovers_wh65_array_and_normalizes_ws2000_metrics(monkeypatch):
    """Keep the GW1200 and Ambient WS-2000 array path protocol-compatible."""
    live_data = {
        "common_list": [
            {"id": "0x02", "val": "72.5", "unit": "F"},
            {"id": "0x07", "val": "48%"},
            {"id": "0x0A", "val": "225°"},
            {"id": "0x0B", "val": "6.2 mph"},
            {"id": "0x0C", "val": "10.5 mph"},
            {"id": "0x15", "val": "412.3 W/m²"},
            {"id": "0x17", "val": "3"},
        ],
        "rain": [
            {"id": "0x0E", "val": "0.20 in/h"},
            {"id": "0x10", "val": "0.34 in"},
        ],
        "wh25": [{"intemp": "70.0 F", "inhumi": "41%", "abs": "24.80 inHg", "rel": "30.02 inHg"}],
    }
    _Client.responses = {
        "get_version": {"version": "Version: GW1200A_V1.0.0", "platform": "ecowitt"},
        "get_iot_device_list": {"command": []},
        "get_network_info": {"mac": "A4:CF:12:34:56:78"},
        ("get_sensors_info", 1): [{
            "img": "wh69",
            "type": "0",
            "name": "WH65/69 weather array",
            "id": "12AB34CD",
            "signal": "4",
            "idst": "1",
        }],
        ("get_sensors_info", 2): [],
        "get_livedata_info": live_data,
        "get_rain_totals": {"rainFallPriority": "1", "rstRainDay": "0"},
    }
    monkeypatch.setattr("sensorius.saiEcowitt.httpx.AsyncClient", _Client)
    ingest = EcowittGatewayIngest(settings=_Settings(), data_logger=_Logger())

    result = await ingest.discover("http://gw1200.local")

    assert result["gateway_model"] == "Version: GW1200A_V1.0.0"
    assert result["sensor_id"] == "ecowitt-a4cf12345678"
    assert result["inventory"] == [{
        "id": "12AB34CD",
        "type": "0",
        "family": "wh69",
        "name": "WH65/69 weather array",
        "battery": "",
        "signal": 4,
        "registered": True,
        "firmware": "",
        "reporting": True,
    }]
    assert result["live_metric_count"] == 17
    assert {"Temperature_F", "Wind Speed", "Rain Day", "Solar Radiation", "UV Index"} <= set(
        result["live_metrics"]
    )
    values = normalize_ecowitt_livedata(live_data)
    assert values["Temperature_F"] == 72.5
    assert values["Rel-Humidity"] == 48.0
    assert values["Wind Direction"] == 225
    assert values["Wind Speed"] == 6.2
    assert values["Wind Gust"] == 10.5
    assert values["Rain Rate"] == 0.2
    assert values["Rain Day"] == 0.34
    assert values["Solar Radiation"] == 412.3
    assert values["UV Index"] == 3.0
    assert values["Gateway Baro-Pressure"] == pytest.approx(1016.6, abs=0.1)


@pytest.mark.asyncio
async def test_poll_once_logs_normalized_values_and_first_rain_checkpoint(monkeypatch):
    _Client.responses = {
        "get_livedata_info": {
            "common_list": [
                {"id": "0x02", "val": "20", "unit": "C"},
                {"id": "0x07", "val": "65%"},
            ],
            "rain": [{"id": "0x10", "val": "1.5 in"}, {"id": "0x7C", "val": "2.3 in"}],
        },
        "get_rain_totals": {"rainFallPriority": "1"},
    }
    monkeypatch.setattr("sensorius.saiEcowitt.httpx.AsyncClient", _Client)
    settings = _Settings()
    settings.values.update({
        ("Ecowitt", "ENABLED"): True,
        ("Ecowitt", "GATEWAY_URL"): "http://gw1100.local",
        ("Ecowitt", "SENSOR_ID"): "ecowitt-e8db840f1543",
    })
    logger = _Logger()
    ingest = EcowittGatewayIngest(settings=settings, data_logger=logger)

    assert await ingest.poll_once() is True
    assert logger.rows[0][1] == "ecowitt-e8db840f1543"
    assert logger.rows[0][2]["Temperature_F"] == 68.0
    assert logger.rows[0][2]["Humidity"] == pytest.approx(11.22, abs=0.02)
    assert logger.rows[0][2]["Ambient VPD"] == pytest.approx(0.819, abs=0.002)
    assert logger.rows[0][2]["Rain Day"] == 1.5
    assert logger.rows[0][2]["Rain Last 24h"] == 2.3
    assert "Rain" not in logger.rows[0][2]


@pytest.mark.parametrize("raw,expected", [(0, "OK"), ("0", "OK"), (1, "LOW"), ("1", "LOW"), (None, "UNKNOWN"), ("9", "UNKNOWN"), ("", "UNKNOWN")])
def test_array_battery_flags_preserve_zero_and_reject_unknown(raw, expected):
    from sensorius.sensor_modules.station_ecowitt import normalize_sensor_inventory, weather_array_battery_status
    row = {"type": "0", "img": "wh69", "id": "E8", "signal": "3", "batt": raw}
    assert weather_array_battery_status(normalize_sensor_inventory([[row]])) == expected
    for overrides in ({"signal": "0"}, {"idst": "0"}, {"id": "FFFFFFFF"}, {"type": "48"}):
        assert weather_array_battery_status(normalize_sensor_inventory([[dict(row, **overrides)]])) == "UNKNOWN"


@pytest.mark.parametrize("network,url,expected", [
    ({"wifi_ip": "192.0.2.10", "wifi_pwd": "private"}, "http://gw1200.local", "192.0.2.10"),
    ({"wifi_ip": "0.0.0.0", "ethIP": "192.0.2.11"}, "http://gw.local", "192.0.2.11"),
    ({}, "http://192.0.2.12:8080", "192.0.2.12"),
    ({"wifi_gateway": "192.0.2.1"}, "http://gw.local", "Unknown"),
])
def test_gateway_ip_is_device_address(network, url, expected):
    from sensorius.saiEcowitt import gateway_ip_address
    assert gateway_ip_address(network, url) == expected


@pytest.mark.asyncio
async def test_battery_health_refresh_failure_and_expiry(monkeypatch):
    from sensorius.saiEcowitt import ecowitt_sensor_health
    settings = _Settings()
    sid = "ecowitt-e8db840f1543"
    settings.values.update({("Ecowitt", "ENABLED"): True, ("Ecowitt", "GATEWAY_URL"): "http://gw1200.local", ("Ecowitt", "SENSOR_ID"): sid})
    logger = _Logger()
    service = EcowittGatewayIngest(settings=settings, data_logger=logger)
    _Client.responses = {
        "get_livedata_info": {"common_list": [{"id": "0x02", "val": "20", "unit": "C"}]},
        "get_rain_totals": {},
        "get_network_info": {"wifi_ip": "192.0.2.10", "wifi_pwd": "secret"},
        ("get_sensors_info", 1): [{"type": "0", "id": "E8", "signal": "3", "batt": "0"}],
    }
    monkeypatch.setattr("sensorius.saiEcowitt.httpx.AsyncClient", _Client)
    assert ecowitt_sensor_health(logger, sid)["battery_status"] == "UNKNOWN"
    for raw, expected in [("0", "OK"), ("1", "LOW"), ("9", "UNKNOWN")]:
        _Client.responses[("get_sensors_info", 1)][0]["batt"] = raw
        await service.poll_once()
        assert ecowitt_sensor_health(logger, sid) == {"battery_status": expected, "gateway_ip": "192.0.2.10"}
        assert set(logger.rows[-1][2]) == {"Temperature", "Temperature_F"}
    _Client.responses[("get_sensors_info", 1)][0]["batt"] = "0"
    await service.poll_once()
    service._health_checked_mono -= 181
    assert service.status()["battery_status"] == "UNKNOWN"
    await service.poll_once()
    service._status["state"] = "offline"
    assert service.status()["battery_status"] == "UNKNOWN"
    await service.poll_once()
    assert ecowitt_sensor_health(logger, "ecowitt-other")["battery_status"] == "UNKNOWN"
    service.disable()
    assert service.status()["battery_status"] == "UNKNOWN"
    settings.values[("Ecowitt", "ENABLED")] = True
    original = service._get_json

    async def unavailable_inventory(client, url, endpoint, **params):
        if endpoint == "get_sensors_info":
            raise EcowittError("Unavailable")
        return await original(client, url, endpoint, **params)

    monkeypatch.setattr(service, "_get_json", unavailable_inventory)
    await service.poll_once()
    assert service.status()["battery_status"] == "UNKNOWN"
    assert service.status()["state"] == "online"


@pytest.mark.asyncio
@pytest.mark.parametrize('write_failure', ['false', 'exception'])
async def test_failed_write_preserves_rain_checkpoint_and_accepted_timestamp(monkeypatch, write_failure):
    settings = _Settings()
    settings.values.update({('Ecowitt', 'GATEWAY_URL'): 'http://gw.local',
                            ('Ecowitt', 'SENSOR_ID'): 'ecowitt-e8db840f1543'})
    logger = _Logger()
    service = EcowittGatewayIngest(settings=settings, data_logger=logger)
    _Client.responses = {'get_livedata_info': {'rain': [{'id': '0x10', 'val': '1.0 in'}]},
                         'get_rain_totals': {'rainFallPriority': '1'}}
    monkeypatch.setattr('sensorius.saiEcowitt.httpx.AsyncClient', _Client)
    await service.poll_once()
    accepted = service.status()['last_accepted_reading']
    checkpoint = service._last_rain_timestamp
    service._rain_reset_hour = (datetime.now(timezone.utc).hour + 1) % 24
    original = logger.log_readings

    def fail(*args):
        if write_failure == 'exception':
            raise OSError('temporary write error')
        return False

    monkeypatch.setattr(logger, 'log_readings', fail)
    _Client.responses['get_livedata_info']['rain'][0]['val'] = '1.5 in'
    with pytest.raises((EcowittError, OSError)):
        await service.poll_once()
    assert service._last_rain_day == 1.0
    assert service._last_rain_timestamp == checkpoint
    assert service.status()['last_accepted_reading'] == accepted
    monkeypatch.setattr(logger, 'log_readings', original)
    _Client.responses['get_livedata_info']['rain'][0]['val'] = '1.75 in'
    await service.poll_once()
    assert logger.rows[-1][2]['Rain'] == 0.75


@pytest.mark.asyncio
@pytest.mark.parametrize('saved_source', ['', 'piezo'])
async def test_rain_metadata_failure_preserves_weather_and_retries(monkeypatch, saved_source):
    from types import SimpleNamespace
    # A newly booted host must refresh even when saved configuration exists.
    monkeypatch.setattr('sensorius.saiEcowitt.time', SimpleNamespace(monotonic=lambda: 1000.0))
    settings = _Settings()
    settings.values.update({('Ecowitt', 'ENABLED'): True,
                            ('Ecowitt', 'GATEWAY_URL'): 'http://gw.local',
                            ('Ecowitt', 'SENSOR_ID'): 'ecowitt-e8db840f1543',
                            ('Ecowitt', 'RAIN_SOURCE'): saved_source,
                            ('Ecowitt', 'RAIN_RESET_HOUR'): 7})
    logger = _Logger()
    service = EcowittGatewayIngest(settings=settings, data_logger=logger)
    _Client.responses = {
        'get_livedata_info': {'common_list': [{'id': '0x02', 'val': '20', 'unit': 'C'}],
                             'rain': [{'id': '0x10', 'val': '1 in'}],
                             'piezoRain': [{'id': '0x10', 'val': '2 in'}]},
        'get_rain_totals': {'rainFallPriority': '2', 'rstRainDay': '9'},
    }
    monkeypatch.setattr('sensorius.saiEcowitt.httpx.AsyncClient', _Client)
    original = service._get_json
    failures = True

    async def get(client, base, endpoint, **params):
        if failures and endpoint == 'get_rain_totals':
            raise EcowittError('rain metadata unavailable')
        return await original(client, base, endpoint, **params)

    monkeypatch.setattr(service, '_get_json', get)
    for _ in range(2):
        await service.poll_once()
        assert logger.rows[-1][2]['Temperature_F'] == 68.0
        assert ('Rain Day' in logger.rows[-1][2]) == bool(saved_source)
        assert service.status()['state'] == 'online'
        assert service.status()['rain_config_error']
        assert service._rain_reset_hour == 7
    failures = False
    await service.poll_once()
    assert service.status()['rain_config_error'] == ''
    assert service._rain_reset_hour == 9
    # Failed daily refresh retains the last successful runtime configuration.
    failures = True
    service._last_rain_config_refresh_mono = 0
    await service.poll_once()
    assert service._rain_source == 'piezo'
    assert service._rain_reset_hour == 9


@pytest.mark.asyncio
async def test_direct_connection_recovers_through_worker_then_returns_to_direct(monkeypatch):
    import httpx
    import sensorius.saiEcowitt as module
    settings = _Settings()
    settings.values.update({('Ecowitt', 'ENABLED'): True, ('Ecowitt', 'GATEWAY_URL'): 'http://gw.local'})
    service = EcowittGatewayIngest(settings=settings, data_logger=_Logger())
    recovered = []

    class Client:
        broken = True

        async def get(self, *args, **kwargs):
            if self.broken:
                raise httpx.ConnectError('No route to host')
            return _Response({'direct': True})

    def recover(base, endpoint, params):
        recovered.append((endpoint, params))
        return {'worker': True}

    monkeypatch.setattr(module, 'request_in_fresh_process', recover)
    client = Client()
    for _ in range(2):
        with pytest.raises(EcowittError, match='No route to host'):
            await service._get_json(client, 'http://gw.local', 'get_livedata_info')
    for _ in range(2):
        assert await service._get_json(client, 'http://gw.local', 'get_livedata_info') == {'worker': True}
    assert len(recovered) == 2
    assert service.status()['recovery_active']
    assert 'No route to host' in service.status()['connection_error']
    # Page counters are independent.
    with pytest.raises(EcowittError):
        await service._get_json(client, 'http://gw.local', 'get_sensors_info', page=2)
    client.broken = False
    assert await service._get_json(client, 'http://gw.local', 'get_livedata_info') == {'direct': True}
    assert not service.status()['recovery_active']
    settings.values[('Ecowitt', 'GATEWAY_URL')] = 'http://other.local'
    assert service.status()['connection_error'] == ''


@pytest.mark.asyncio
async def test_worker_failure_is_retried_and_http_response_resets_failures(monkeypatch):
    import httpx
    import subprocess
    import sensorius.saiEcowitt as module
    service = EcowittGatewayIngest(settings=_Settings(), data_logger=_Logger())
    error = httpx.TimeoutException('timeout')
    calls = []

    class Client:
        async def get(self, *args, **kwargs):
            raise error

    def recover(*args):
        calls.append(True)
        raise subprocess.TimeoutExpired('worker', 8)

    monkeypatch.setattr(module, 'request_in_fresh_process', recover)
    for i in range(4):
        with pytest.raises(EcowittError, match='fresh-process recovery failed' if i >= 2 else 'timeout'):
            await service._get_json(Client(), 'http://gw.local', 'get_livedata_info')
    assert len(calls) == 2
    response = httpx.Response(503, request=httpx.Request('GET', 'http://gw.local/get_livedata_info'))
    error = httpx.HTTPStatusError('503', request=response.request, response=response)
    with pytest.raises(EcowittError, match='HTTP 503'):
        await service._get_json(Client(), 'http://gw.local', 'get_livedata_info')
    error = httpx.ConnectError('offline again')
    with pytest.raises(EcowittError):
        await service._get_json(Client(), 'http://gw.local', 'get_livedata_info')
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_worker_recovery_reaches_normal_persistence(monkeypatch):
    import httpx
    import sensorius.saiEcowitt as module
    settings = _Settings()
    settings.values.update({('Ecowitt', 'ENABLED'): True,
                            ('Ecowitt', 'GATEWAY_URL'): 'http://gw.local',
                            ('Ecowitt', 'SENSOR_ID'): 'ecowitt-e8db840f1543'})
    logger = _Logger()
    service = EcowittGatewayIngest(settings=settings, data_logger=logger)

    class Client(_Client):
        async def get(self, url, **kwargs):
            if url.endswith('get_livedata_info'):
                raise httpx.ConnectError('process lost LAN access')
            return _Response({})

    def recover(base, endpoint, params):
        assert endpoint == 'get_livedata_info'
        return {'common_list': [{'id': '0x02', 'val': '20', 'unit': 'C'}]}

    monkeypatch.setattr(module.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(module, 'request_in_fresh_process', recover)
    for _ in range(2):
        with pytest.raises(EcowittError):
            await service.poll_once()
    assert not logger.rows
    assert await service.poll_once()
    assert logger.rows[-1][2]['Temperature_F'] == 68.0
    assert service.status()['state'] == 'online'
    assert service.status()['recovery_active']
    assert service.status()['last_accepted_reading']


@pytest.mark.asyncio
async def test_run_retries_connection_and_storage_failures(monkeypatch):
    import asyncio
    settings = _Settings()
    settings.values.update({('Ecowitt', 'ENABLED'): True,
                            ('Ecowitt', 'GATEWAY_URL'): 'http://gw.local',
                            ('Ecowitt', 'SENSOR_ID'): 'ecowitt-e8db840f1543'})
    service = EcowittGatewayIngest(settings=settings, data_logger=_Logger())
    service._migrated_sensor_id = service.sensor_id
    calls = 0
    states = []

    async def poll():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EcowittError('gateway unavailable')
        if calls == 2:
            raise OSError('database temporarily unavailable')
        service._status.update(state='online', last_error='')

    async def sleep(seconds):
        states.append(service.status()['state'])
        if calls == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, 'poll_once', poll)
    monkeypatch.setattr(service, '_sleep_with_heartbeat', sleep)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert calls == 3
    assert states == ['offline', 'offline', 'online']
