"""Focused checks for the Raspberry Pi driverless-printer deployment helper."""

import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "setup_rpi_printer.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_printer_path(tmp_path: Path, uris: list[str]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    quoted_uris = "\\n".join(uris)
    _write_executable(
        bin_dir / "driverless",
        f"if [[ \"${{1:-}}\" == \"--std-ipp-uris\" ]]; then printf '%b\\n' '{quoted_uris}'; fi",
    )
    _write_executable(
        bin_dir / "ipptool",
        'uri="$2"; host="${uri#*://}"; host="${host%%:*}"; '
        'printf "        printer-info (textWithoutLanguage) = Test Printer %s\\n" "$host"',
    )
    for command in ("lp", "lpadmin", "lpoptions", "lpstat", "systemctl", "sudo"):
        _write_executable(bin_dir / command, "exit 0")
    return bin_dir


def _run_helper(bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_printer_helper_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(HELPER)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_printer_helper_lists_discovered_printers_without_mutation(tmp_path):
    uri = "ipp://printer-one.local:631/ipp/print"
    result = _run_helper(_fake_printer_path(tmp_path, [uri]), "--list")

    assert result.returncode == 0, result.stderr
    assert "Driverless IPP printers discovered: 1" in result.stdout
    assert "Test Printer printer-one.local" in result.stdout
    assert uri in result.stdout


def test_printer_helper_refuses_automatic_multi_printer_choice(tmp_path):
    bin_dir = _fake_printer_path(
        tmp_path,
        [
            "ipp://printer-one.local:631/ipp/print",
            "ipp://printer-two.local:631/ipp/print",
        ],
    )

    result = _run_helper(bin_dir, "--yes")

    assert result.returncode == 2
    assert "multiple printers were found" in result.stderr


def test_printer_helper_skips_an_enabled_matching_default(tmp_path):
    uri = "ipp://printer-one.local:631/ipp/print"
    bin_dir = _fake_printer_path(tmp_path, [uri])
    _write_executable(
        bin_dir / "lpstat",
        f'''case "$1" in
  -d) echo "system default destination: Test_Printer" ;;
  -v) echo "device for Test_Printer: {uri}" ;;
  -p) echo "printer Test_Printer is idle. enabled since today" ;;
esac''',
    )

    result = _run_helper(bin_dir, "--yes")

    assert result.returncode == 0, result.stderr
    assert "Printer setup already complete" in result.stdout


def test_pi_deployment_paths_install_and_invoke_printer_helper():
    bookworm_uv = (REPO_ROOT / "deploy_scripts" / "setup_bookwork_uv.sh").read_text(encoding="utf-8")
    trixie_uv = (REPO_ROOT / "deploy_scripts" / "setup_trixie_uv.sh").read_text(encoding="utf-8")
    bookworm = (REPO_ROOT / "deploy_scripts" / "setup_bookworm.sh").read_text(encoding="utf-8")
    trixie = (REPO_ROOT / "deploy_scripts" / "setup_trixie.sh").read_text(encoding="utf-8")

    for script in (bookworm_uv, trixie_uv):
        assert "cups-ipp-utils" in script
        assert "cups-filters-core-drivers" in script
        assert "configure_rpi_printer" in script
    assert "configure_rpi_printer" in bookworm
    assert "configure_rpi_printer" in trixie
