"""Test native install-location selection for source-based app installers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_COMMON = REPO_ROOT / "deploy_scripts" / "setup_common.sh"


def _resolve_location(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"; resolve_sensorius_install_location; printf "RESULT=%s\\n" "$PROJECT_DIR"'
    return subprocess.run(
        ["bash", "-c", command, "bash", str(SETUP_COMMON)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def test_explicit_unix_install_location_is_remembered(tmp_path: Path) -> None:
    runtime = tmp_path / "custom" / "Sensorius"
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SENSORIUS_INSTALL_DIR": str(runtime),
    }
    environment.pop("PROJECT_DIR", None)
    environment.pop("SUDO_USER", None)

    result = _resolve_location(environment)

    assert result.returncode == 0, result.stderr
    assert f"RESULT={runtime}" in result.stdout
    assert runtime.is_dir()
    state_file = tmp_path / "config" / "sensorius" / "install-location"
    assert state_file.read_text(encoding="utf-8").strip() == str(runtime)


def test_headless_unix_installer_reuses_remembered_location(tmp_path: Path) -> None:
    remembered = tmp_path / "remembered" / "Sensorius"
    remembered.parent.mkdir()
    state_file = tmp_path / "config" / "sensorius" / "install-location"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(f"{remembered}\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text("#!/bin/sh\nprintf 'TestOS\\n'\n", encoding="utf-8")
    fake_uname.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    for name in ("PROJECT_DIR", "SENSORIUS_INSTALL_DIR", "SUDO_USER"):
        environment.pop(name, None)

    result = _resolve_location(environment)

    assert result.returncode == 0, result.stderr
    assert f"using {remembered}" in result.stdout
    assert f"RESULT={remembered}" in result.stdout
    assert remembered.is_dir()


def test_macos_installer_uses_native_folder_selector(tmp_path: Path) -> None:
    selected_parent = tmp_path / "Applications"
    selected_parent.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    commands = {
        "uname": "#!/bin/sh\nprintf 'Darwin\\n'\n",
        "osascript": "#!/bin/sh\nprintf '%s\\n' \"$TEST_SELECTED_PARENT\"\n",
    }
    for name, content in commands.items():
        executable = fake_bin / name
        executable.write_text(content, encoding="utf-8")
        executable.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TEST_SELECTED_PARENT": str(selected_parent),
    }
    Path(environment["HOME"]).mkdir()
    for name in ("PROJECT_DIR", "SENSORIUS_INSTALL_DIR", "SUDO_USER"):
        environment.pop(name, None)

    result = _resolve_location(environment)

    runtime = selected_parent / "Sensorius"
    assert result.returncode == 0, result.stderr
    assert f"RESULT={runtime}" in result.stdout
    state_file = tmp_path / "config" / "sensorius" / "install-location"
    assert state_file.read_text(encoding="utf-8").strip() == str(runtime)


def test_current_app_installers_resolve_location_before_installing() -> None:
    for relative in (
        "install.sh",
        "deploy_scripts/setup_bookwork_uv.sh",
        "deploy_scripts/setup_bookworm.sh",
        "deploy_scripts/setup_linux.sh",
        "deploy_scripts/setup_mac.sh",
        "deploy_scripts/setup_mac_uv.sh",
        "deploy_scripts/setup_trixie.sh",
        "deploy_scripts/setup_trixie_uv.sh",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "resolve_sensorius_install_location" in source


def test_windows_installers_use_native_folder_selector_and_saved_location() -> None:
    helper = (REPO_ROOT / "deploy_scripts" / "setup_install_location.ps1").read_text(
        encoding="utf-8"
    )
    assert "System.Windows.Forms.FolderBrowserDialog" in helper
    assert "install-location.txt" in helper
    assert "$env:SENSORIUS_INSTALL_DIR" in helper
    assert "Join-Path $locationDialog.SelectedPath 'Sensorius'" in helper
    for name in ("setup_win.ps1", "setup_win_uv.ps1"):
        source = (REPO_ROOT / "deploy_scripts" / name).read_text(encoding="utf-8")
        assert "Resolve-SensoriusInstallLocation" in source
