from pathlib import Path


def test_switch_monitor_emits_startup_markers():
    source = (Path(__file__).resolve().parents[1] / "saiSwitch.py").read_text(encoding="utf-8")

    assert "[monitor-start]" in source
    assert "[monitor-first-tick]" in source


def test_sensorius_logs_switch_monitor_queueing():
    source = (Path(__file__).resolve().parents[1] / "Sensorius.py").read_text(encoding="utf-8")

    assert "Queueing switch monitor:" in source


def test_sensorius_startup_log_includes_version():
    source = (Path(__file__).resolve().parents[1] / "Sensorius.py").read_text(encoding="utf-8")

    assert "from __init__ import __version__" in source
    assert 'Sensorius startup... version={__version__}' in source
