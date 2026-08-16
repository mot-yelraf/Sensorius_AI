"""Test safe cleanup of legacy deployment layouts.

The deployment script runs only against temporary directory trees, including a
case with owner-protected bytecode.
"""

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_COMMON = REPO_ROOT / "deploy_scripts" / "setup_common.sh"
DEPLOY_SAI = REPO_ROOT / "deploy_scripts" / "deploy_sai.sh"


def _deploy_fixture(tmp_path: Path, runtime_override: bool = True):
    source = tmp_path / "source"
    (source / "sensorius").mkdir(parents=True)
    (source / "sensorius" / "__init__.py").write_text(
        '__version__ = "v0.26.228.1"\n', encoding="utf-8"
    )
    (source / "Sensorius.py").write_text("# launcher\n", encoding="utf-8")
    requirements_dir = source / "deploy_scripts"
    requirements_dir.mkdir()
    (requirements_dir / "setup_reqs_trixie.txt").write_text(
        "packaging>=25.0\n", encoding="utf-8"
    )

    hosts = tmp_path / "hosts.txt"
    inventory_line = "fake-pi|/remote/Sensorius|"
    if runtime_override:
        inventory_line += "|/home/twfarley/py313/bin/python"
    hosts.write_text(inventory_line + "\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_rsync.chmod(0o755)

    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        """#!/bin/sh
if [ "${1:-}" = "-n" ]; then
  shift
  exec </dev/null
fi
script=$(cat)
if printf '%s' "$script" | grep -q 'profile=mac'; then
  for arg in "$@"; do configured_python=$arg; done
  if [ -n "$configured_python" ]; then
    printf '%s\\n' "pi-trixie|$configured_python|inventory"
  else
    printf '%s\\n' 'pi-trixie|/remote/Sensorius/.venv/bin/python|target-venv'
  fi
  exit 0
fi
if printf '%s' "$script" | grep -q 'Dependencies need reconciliation:'; then
  if [ -f "$FAKE_DEPENDENCY_STATE" ]; then
    printf '%s\\n' 'Dependencies satisfy the pi-trixie runtime profile.'
    exit 0
  fi
  printf '%s\\n' 'Dependencies need reconciliation:' '  - adafruit-circuitpython-sgp41: missing'
  exit 10
fi
if printf '%s' "$script" | grep -q 'sensorius-requirements.XXXXXX'; then
  : > "$FAKE_DEPENDENCY_STATE"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = "{}:{}".format(fake_bin, env.get("PATH", ""))
    env["FAKE_DEPENDENCY_STATE"] = str(tmp_path / "dependencies-installed")
    return source, hosts, fake_rsync, env


def _run_deploy_dependency_fixture(
    tmp_path: Path, mode: str, runtime_override: bool = True
):
    source, hosts, fake_rsync, env = _deploy_fixture(tmp_path, runtime_override)
    return subprocess.run(
        [
            "bash",
            str(DEPLOY_SAI),
            mode,
            "--hosts",
            str(hosts),
            "--source",
            str(source),
            "--rsync-bin",
            str(fake_rsync),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _run_cleanup(target: Path) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"; remove_legacy_python_layout "$2"'
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


def test_deploy_dry_run_reports_required_dependency_install(tmp_path):
    result = _run_deploy_dependency_fixture(tmp_path, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "profile=pi-trixie" in result.stdout
    assert "python=/home/twfarley/py313/bin/python" in result.stdout
    assert "source=inventory" in result.stdout
    assert "adafruit-circuitpython-sgp41: missing" in result.stdout
    assert "DRY RUN: dependency installation would run" in result.stdout


def test_deploy_apply_installs_and_rechecks_dependencies(tmp_path):
    result = _run_deploy_dependency_fixture(tmp_path, "--apply")

    assert result.returncode == 0, result.stderr
    assert "Installing missing or outdated dependencies" in result.stdout
    assert "Dependencies satisfy the pi-trixie runtime profile" in result.stdout
    assert "Deploy completed successfully" in result.stdout


def test_deploy_inventory_runtime_python_remains_optional(tmp_path):
    result = _run_deploy_dependency_fixture(
        tmp_path, "--dry-run", runtime_override=False
    )

    assert result.returncode == 0, result.stderr
    assert "python=/remote/Sensorius/.venv/bin/python" in result.stdout
    assert "source=target-venv" in result.stdout


def test_post_deploy_ssh_does_not_consume_the_next_inventory_host(tmp_path):
    source, hosts, fake_rsync, env = _deploy_fixture(tmp_path)
    hosts.write_text(
        "restart-host|/remote/Sensorius|restart-sensorius|\n"
        "samhain|/Users/twfarley/Sensorius||\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SAI),
            "--apply",
            "--skip-deps",
            "--hosts",
            str(hosts),
            "--source",
            str(source),
            "--rsync-bin",
            str(fake_rsync),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "Post-deploy -> restart-host: restart-sensorius" in result.stdout
    assert "Deploying -> samhain:/Users/twfarley/Sensorius/" in result.stdout
    assert result.stdout.count("Deploying ->") == 2


def test_deploy_runtime_discovery_supports_external_virtual_environments():
    deploy_text = DEPLOY_SAI.read_text(encoding="utf-8")

    inventory = deploy_text.index('python_source="inventory"')
    active_process = deploy_text.index('python_source="active-process"')
    systemd_service = deploy_text.index('python_source="systemd-execstart"')
    target_venv = deploy_text.index('python_source="target-venv"')

    assert "/proc/[0-9]*" in deploy_text
    assert "VIRTUAL_ENV=" in deploy_text
    assert "process_exe=$(tr '\\000' '\\n'" in deploy_text
    assert "systemctl show sensorius.service" in deploy_text
    assert inventory < active_process < systemd_service < target_venv
