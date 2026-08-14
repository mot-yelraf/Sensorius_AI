"""Test safe cleanup of legacy deployment layouts.

The deployment script runs only against temporary directory trees, including a
case with owner-protected bytecode.
"""

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_COMMON = REPO_ROOT / "deploy_scripts" / "setup_common.sh"
DEPLOY_SAI = REPO_ROOT / "deploy_scripts" / "deploy_sai.sh"


def _run_cleanup(target: Path) -> subprocess.CompletedProcess[str]:
    command = f'source "$1"; remove_legacy_python_layout "$2"'
    return subprocess.run(
        ["bash", "-c", command, "bash", str(SETUP_COMMON), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cleanup_removes_flat_and_transitional_python_layout(tmp_path):
    (tmp_path / "sensorius").mkdir()
    (tmp_path / "sensorius" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "Sensorius.py").write_text("# launcher\n", encoding="utf-8")
    (tmp_path / "saiSettings.py").write_text("# legacy\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "saiSettings.cpython-313.pyc").write_bytes(b"legacy")
    (tmp_path / "sensor_modules").mkdir()
    (tmp_path / "sensor_modules" / "base.py").write_text("# legacy\n", encoding="utf-8")
    (tmp_path / "src" / "sensorius").mkdir(parents=True)
    (tmp_path / "src" / "sensorius" / "app.py").write_text("# transitional\n", encoding="utf-8")

    result = _run_cleanup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Sensorius.py").is_file()
    assert (tmp_path / "sensorius" / "__init__.py").is_file()
    assert not (tmp_path / "saiSettings.py").exists()
    assert not (tmp_path / "__pycache__").exists()
    assert not (tmp_path / "sensor_modules").exists()
    assert not (tmp_path / "src").exists()


def test_cleanup_refuses_to_remove_legacy_files_without_replacement_package(tmp_path):
    legacy = tmp_path / "saiSettings.py"
    legacy.write_text("# legacy\n", encoding="utf-8")

    result = _run_cleanup(tmp_path)

    assert result.returncode != 0
    assert "replacement sensorius package is incomplete" in result.stderr
    assert legacy.is_file()


def test_cleanup_tolerates_owner_protected_legacy_bytecode(tmp_path):
    (tmp_path / "sensorius").mkdir()
    (tmp_path / "sensorius" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "Sensorius.py").write_text("# launcher\n", encoding="utf-8")
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    legacy_bytecode = cache_dir / "saiStats.cpython-313.pyc"
    legacy_bytecode.write_bytes(b"legacy")
    cache_dir.chmod(0o555)

    try:
        result = _run_cleanup(tmp_path)

        assert result.returncode == 0, result.stderr
        assert legacy_bytecode.is_file()
        assert "owner-protected legacy bytecode remains" in result.stderr
    finally:
        cache_dir.chmod(0o755)


def test_deploy_syncs_preserve_system_automation_state():
    setup_text = SETUP_COMMON.read_text(encoding="utf-8")
    deploy_text = DEPLOY_SAI.read_text(encoding="utf-8")

    assert "--include 'automation_settings/'" in setup_text
    assert "--exclude 'automation_settings/***'" in setup_text
    assert '"automation_settings/"' in deploy_text
    assert '"automation_settings/***"' in deploy_text
