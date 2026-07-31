"""Keep switch-monitor diagnostics available in advanced debug choices."""

from pathlib import Path


def test_advanced_debug_module_choices_include_monitor_diagnostics():
    source = (Path(__file__).resolve().parents[1] / "sensorius" / "saiWebRoutes.py").read_text(encoding="utf-8")

    assert '"saiTaskSupervisor"' in source
    assert '"saiAutomationManager"' in source
    assert '"saiSwitchFactory"' in source
    assert '"saiDataLogger"' in source
