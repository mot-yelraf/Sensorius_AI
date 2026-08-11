"""Test Ecowitt LAN payload normalization and inventory compatibility.

Representative gateway responses protect canonical units, metric names,
channel identities, and filtering of unusable sensor records.
"""

from __future__ import annotations

import pytest

from sensorius.sensor_modules.station_ecowitt import (
    ecowitt_gauge_config_for_metric,
    normalize_ecowitt_livedata,
    normalize_sensor_inventory,
    normalized_gateway_sensor_id,
    rain_reset_hour_from_totals,
    rain_source_from_totals,
)
from sensorius.saiHtml import extend_gauge_config_for_metrics, get_gauge_config


def test_metric_and_imperial_payloads_normalize_to_same_values():
    metric = {
        "common_list": [
            {"id": "0x02", "val": "20.0", "unit": "C"},
            {"id": "0x07", "val": "65%"},
            {"id": "0x09", "val": "1013.2 hPa"},
            {"id": "0x0B", "val": "4.4704 m/s"},
            {"id": "0x0C", "val": "8.9408 m/s"},
            {"id": "0x15", "val": "350.0 W/m2"},
            {"id": "0x17", "val": "3"},
        ],
        "rain": [
            {"id": "0x0E", "val": "25.4 mm/Hr"},
            {"id": "0x10", "val": "50.8 mm"},
        ],
    }
    imperial = {
        "common_list": [
            {"id": "0x02", "val": "68.0", "unit": "F"},
            {"id": "0x07", "val": "65%"},
            {"id": "0x09", "val": "29.919 inHg"},
            {"id": "0x0B", "val": "10.00 mph"},
            {"id": "0x0C", "val": "20.00 mph"},
            {"id": "0x15", "val": "350.0 W/m2"},
            {"id": "0x17", "val": "3"},
        ],
        "rain": [
            {"id": "0x0E", "val": "1.00 in/Hr"},
            {"id": "0x10", "val": "2.00 in"},
        ],
    }

    metric_values = normalize_ecowitt_livedata(metric)
    imperial_values = normalize_ecowitt_livedata(imperial)

    for name in ("Temperature", "Temperature_F", "Rel-Humidity", "Wind Speed", "Wind Gust", "Rain Rate", "Rain Day"):
        assert metric_values[name] == pytest.approx(imperial_values[name], abs=0.02)
    assert metric_values["Baro-Pressure"] == pytest.approx(imperial_values["Baro-Pressure"], abs=0.1)
    assert metric_values["Solar Radiation"] == 350.0
    assert "Light Intensity" not in metric_values
    assert metric_values["Humidity"] == pytest.approx(imperial_values["Humidity"], abs=0.01)
    assert metric_values["Ambient VPD"] == pytest.approx(imperial_values["Ambient VPD"], abs=0.001)
    assert metric_values["Humidity"] == pytest.approx(11.22, abs=0.02)
    assert metric_values["Ambient VPD"] == pytest.approx(0.819, abs=0.002)


def test_lux_is_not_mislabeled_as_solar_radiation():
    values = normalize_ecowitt_livedata({"common_list": [{"id": "0x15", "val": "12000 lux"}]})
    assert values == {"Light Intensity": 12000.0}


def test_authoritative_rain_source_is_selected_without_combining_arrays():
    payload = {
        "rain": [{"id": "0x10", "val": "1.00 in"}],
        "piezoRain": [{"id": "0x10", "val": "2.00 in"}],
    }
    assert normalize_ecowitt_livedata(payload, rain_source="traditional")["Rain Day"] == 1.0
    assert normalize_ecowitt_livedata(payload, rain_source="piezo")["Rain Day"] == 2.0
    assert "Rain Day" not in normalize_ecowitt_livedata(payload, rain_source="none")
    assert rain_source_from_totals({"rainFallPriority": "0"}) == "none"
    assert rain_source_from_totals({"rainFallPriority": "1"}) == "traditional"
    assert rain_source_from_totals({"rainFallPriority": "2"}) == "piezo"
    assert rain_reset_hour_from_totals({"rstRainDay": "9"}) == 9
    assert rain_reset_hour_from_totals({"rstRainDay": "99"}) == 23


def test_inventory_filters_protocol_sentinels_but_retains_reporting_zero_id():
    inventory = normalize_sensor_inventory([[{
        "img": "wh34", "type": "31", "name": "Temp CH1", "id": "0", "signal": "3", "idst": "1"
    }, {
        "img": "wh34", "type": "32", "name": "Temp CH2", "id": "0", "signal": "0", "idst": "1"
    }, {
        "img": "wh31", "type": "6", "name": "Temp & Humidity CH1", "id": "FFFFFFFF", "signal": "0"
    }, {
        "img": "wh25", "type": "4", "name": "Disabled", "id": "1234", "signal": "4", "idst": "0"
    }], [{
        "img": "wh51", "type": "14", "name": "Soil moisture CH1", "id": "C4BC", "signal": "0", "idst": "1"
    }]])

    assert [(item["type"], item["id"]) for item in inventory] == [("31", "0"), ("14", "C4BC")]
    assert inventory[1]["signal"] == 0


def test_additional_channel_arrays_are_normalized():
    values = normalize_ecowitt_livedata({
        "ch_aisle": [{"channel": "1", "temp": "68.0", "unit": "F", "humidity": "55%"}],
        "ch_temp": [{"channel": "2", "temp": "10.0 C", "battery": "3"}],
        "ch_soil": [{"channel": "3", "humidity": "42%"}],
        "ch_leaf": [{"channel": "1", "humidity": "7%"}],
        "ch_pm25": [{"channel": "2", "PM25": "12.5"}],
        "ch_ec": [{"channel": "2", "ec": "1250 us/cm"}],
        "ch_leak": [{"channel": "4", "status": "Leak"}],
        "ch_lds": [{"channel": "1", "air": "100 mm", "depth": "3900 mm"}],
    })

    assert values["WH31 CH1 Temperature"] == 20.0
    assert values["WH31 CH1 Temperature_F"] == 68.0
    assert values["WH31 CH1 Rel-Humidity"] == 55.0
    assert values["WH34 CH2 Temperature"] == 10.0
    assert values["Soil Moisture CH3"] == 42.0
    assert values["Leaf Wetness CH1"] == 7.0
    assert values["PM2.5 CH2"] == 12.5
    assert values["Soil EC CH2"] == 1.25
    assert values["Leak CH4"] == 1.0
    assert values["LDS CH1 Depth"] == 3900.0


def test_unknown_ec_unit_is_not_mislabeled():
    values = normalize_ecowitt_livedata({"ch_ec": [{"channel": "1", "ec": "1250"}]})
    assert "Soil EC CH1" not in values


def test_ecowitt_fixed_and_channel_metrics_have_dashboard_gauges():
    gauge_config = get_gauge_config()
    metrics = [
        "Wind Gust",
        "Solar Radiation",
        "UV Index",
        "Rain Day",
        "Lightning Count",
        "WH31 CH1 Temperature",
        "WH31 CH1 Rel-Humidity",
        "Soil Moisture CH3",
        "PM2.5 CH2",
        "Soil EC CH2",
        "LDS CH1 Depth",
        "Leak CH4",
    ]
    extend_gauge_config_for_metrics(gauge_config, metrics)
    assert all(metric in gauge_config for metric in metrics)
    assert gauge_config["WH31 CH1 Temperature"]["unit"] == "°C"
    assert gauge_config["Soil EC CH2"]["unit"] == "mS/cm"
    assert ecowitt_gauge_config_for_metric("unsupported", gauge_config) is None


def test_gateway_id_is_stable_across_mac_formatting():
    expected = "ecowitt-e8db840f1543"
    assert normalized_gateway_sensor_id("E8:DB:84:0F:15:43") == expected
    assert normalized_gateway_sensor_id("e8-db-84-0f-15-43") == expected
    with pytest.raises(ValueError):
        normalized_gateway_sensor_id("missing")


def test_missing_and_malformed_values_are_ignored_not_zeroed():
    values = normalize_ecowitt_livedata({
        "common_list": [
            {"id": "0x02", "val": "None", "unit": "C"},
            {"id": "0x07", "val": "bad"},
            {"id": "0x99", "val": "123"},
        ]
    })
    assert values == {}


def test_derived_air_metrics_require_both_temperature_and_relative_humidity():
    temperature_only = normalize_ecowitt_livedata({
        "common_list": [{"id": "0x02", "val": "20", "unit": "C"}],
    })
    humidity_only = normalize_ecowitt_livedata({
        "common_list": [{"id": "0x07", "val": "65%"}],
    })
    assert "Humidity" not in temperature_only
    assert "Ambient VPD" not in temperature_only
    assert "Humidity" not in humidity_only
    assert "Ambient VPD" not in humidity_only
