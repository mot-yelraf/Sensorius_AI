"""Pytest coverage for initial settings materialization defaults.

These tests verify auto-generated bootstrap values stay aligned with current
Nodus and broker configuration expectations.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import saiSensorSettingsManager
import saiSettings


def test_apply_auto_values_does_not_default_sensornetwork_broker_to_localhost(tmp_path, monkeypatch):
    system_root = tmp_path / "system_settings"
    factory_dir = system_root / "factory"
    factory_dir.mkdir(parents=True)
    (factory_dir / "settings.toml").write_text(
        "\n".join(
            [
                "[Network]",
                'HOSTNAME = ""',
                "",
                "[SensorNetwork]",
                'BROKER = ""',
                "MQTTPORT = 1883",
                "",
                "[Time]",
                'TZ = "America/Denver"',
                "TZ_OFFSET = -21600",
                'TZ_NAME = "MST"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(saiSettings, "get_pi_network_info", lambda: {"hostname": "sensorius-pbp-0"})
    monkeypatch.setattr(saiSettings, "get_time_settings", lambda: {})

    settings = saiSettings.saiSettings(
        apply_live=True,
        make_startup_backup=False,
        base_dir=str(system_root),
        device_id="sensorius-pbp-0",
    )

    assert settings.get_setting("SensorNetwork", "BROKER", None) == ""


def test_sensor_factory_seed_uses_nodus_aligned_display_defaults(tmp_path):
    sensor_root = tmp_path / "sensor_settings"
    sensor_root.mkdir()
    mgr = saiSensorSettingsManager.SensorSettingsManager(str(sensor_root))

    mgr.seed_from_factory("apvpd-test123", "apvpd")
    mgr.seed_from_factory("lux-123", "lux")
    mgr.seed_from_factory("soil-123", "soil")

    assert mgr.get_display_metrics("apvpd-test123") == [
        "Ambient VPD",
        "Temperature",
        "Rel-Humidity",
        "Plant VPD",
        "Plant Temperature",
        "Plant Rel-Humidity",
    ]
    assert mgr.get_display_metrics("lux-123") == [
        "Light Intensity",
        "Auto Light",
        "Estimated PPFD",
        "Visible Light Intensity",
    ]
    assert mgr.get_display_metrics("soil-123") == [
        "Soil Moisture",
        "Soil Moisture Deficit",
        "Soil Stress Index",
        "Soil Temp_C",
        "Soil pH",
        "Soil EC",
    ]
    assert mgr.load("apvpd-test123")["Display"]["Style"]["METRIC_1"] == ""
    assert mgr.load("apvpd-test123")["Display"]["Style"]["METRIC_6"] == ""
