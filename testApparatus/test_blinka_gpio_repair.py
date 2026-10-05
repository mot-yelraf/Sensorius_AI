"""Verify the pinned Blinka backport without opening real GPIO hardware.

Synthetic upstream files exercise chip selection, idempotence, backup safety,
and refusal to modify unexpected code.
"""

from pathlib import Path
from types import SimpleNamespace
import pytest
from deploy_scripts.repair_blinka_gpio import repair_file, OLD, NEW


@pytest.mark.parametrize("chip", [0, 4, 5, 15, 123])
def test_repair_selects_full_chip_number_and_preserves_backup(tmp_path, chip):
    path = tmp_path / "lgpio_pin.py"
    original = f"def select(dev, lgpio):\n    return {OLD}\n"
    path.write_text(original)
    path.chmod(0o644)
    assert repair_file(path)
    namespace = {}
    exec(path.read_text(), namespace)
    assert namespace["select"](Path(f"gpiochip{chip}"), SimpleNamespace(gpiochip_open=lambda n: n)) == chip
    backup = path.with_name(path.name + ".sensorius-gpio-backup")
    assert backup.read_text() == original
    assert path.stat().st_mode & 0o777 == 0o644
    assert not repair_file(path)
    assert backup.read_text() == original
    path.write_text(original)  # A dependency reinstall restores upstream code.
    assert repair_file(path)
    assert backup.read_text() == original


def test_repair_refuses_unrecognized_source(tmp_path):
    path = tmp_path / "lgpio_pin.py"
    path.write_text("# Unexpected implementation\n")
    with pytest.raises(RuntimeError, match="refusing"):
        repair_file(path)
    assert path.read_text() == "# Unexpected implementation\n"
    assert list(tmp_path.iterdir()) == [path]
