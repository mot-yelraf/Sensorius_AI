from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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

