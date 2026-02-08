import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSwitchSettingsManager import SwitchSettingsManager


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dir_for_rejects_traversal_and_invalid_ids(tmp_path):
    mgr = SwitchSettingsManager(base_dir=str(tmp_path))
    for bad in ("", ".", "..", "../x", "x/../y", "x/y", r"x\y"):
        with pytest.raises(ValueError):
            mgr._dir_for(bad)


def test_ensure_host_switch_factory_falls_back_to_switch_3_template(tmp_path):
    base = tmp_path / "switch_settings"
    mgr = SwitchSettingsManager(base_dir=str(base))

    _write(
        base / "factory" / "switch_3_relay.toml",
        (
            "[Switch]\n"
            "DEVICE = \"template\"\n"
            "SWITCH_ID = \"\"\n"
            "SWITCH_LOCATION = \"Unknown\"\n"
            "SWITCH_1 = \"Fan\"\n"
            "SWITCH_1_PIN = 26\n"
            "SWITCH_2 = \"Light\"\n"
            "SWITCH_2_PIN = 20\n"
            "SWITCH_3 = \"Pump\"\n"
            "SWITCH_3_PIN = 21\n"
        ),
    )

    sid = mgr.ensure_host_switch(host_id="host-a", template_id="factory", switch_loc="Lab")
    assert sid == "host-a"
    doc = mgr.load("host-a")
    assert doc is not None
    sw = doc.get("Switch", {})
    assert sw.get("DEVICE") == "switch"
    assert sw.get("SWITCH_ID") == "host-a"
    assert sw.get("SWITCH_LOCATION") == "Lab"
    assert sw.get("SWITCH_3") == "Pump"


def test_save_and_update_preserve_non_switch_sections(tmp_path):
    mgr = SwitchSettingsManager(base_dir=str(tmp_path))
    settings = {
        "Switch": {
            "DEVICE": "switch",
            "SWITCH_ID": "alpha",
            "SWITCH_LOCATION": "Unknown",
            "SWITCH_1": "Fan",
            "SWITCH_1_PIN": 26,
        },
        "Calibration": {
            "CALIBRATED": True,
            "OFFSET": 1.5,
        },
    }
    mgr.save("alpha", settings)
    first = mgr.load("alpha")
    assert first is not None
    assert first.get("Calibration", {}).get("CALIBRATED") is True
    assert float(first.get("Calibration", {}).get("OFFSET")) == 1.5

    mgr.update_setting("alpha", "SWITCH_LOCATION", "Rack")
    second = mgr.load("alpha")
    assert second is not None
    assert second.get("Switch", {}).get("SWITCH_LOCATION") == "Rack"
    assert second.get("Calibration", {}).get("CALIBRATED") is True
    assert float(second.get("Calibration", {}).get("OFFSET")) == 1.5
