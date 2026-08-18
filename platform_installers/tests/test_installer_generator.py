"""Tests for the Sensorius native installer generator."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


GENERATOR_PATH = Path(__file__).resolve().parents[1] / "installer_generator.py"
SPEC = importlib.util.spec_from_file_location("installer_generator", GENERATOR_PATH)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def test_canonical_version_is_package_version():
    source = (generator.REPO_ROOT / "sensorius" / "__init__.py").read_text(encoding="utf-8")
    expected = re.search(r'__version__\s*=\s*"v([^"]+)"', source)
    assert expected
    assert generator.canonical_version() == expected.group(1)


def test_generate_all_targets(tmp_path):
    version = generator.canonical_version()
    release = generator.generate(tmp_path, list(generator.TARGETS), version, False)

    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version
    assert manifest["targets"] == list(generator.TARGETS)
    assert any(row["path"] == "Sensorius.py" for row in manifest["payload"])
    assert any(row["path"] == "data/skyfield/de421.bsp" for row in manifest["payload"])

    for target in generator.TARGETS:
        assert (release / target).is_dir()

    assert (release / "linux-amd64" / "build.sh").stat().st_mode & 0o111
    assert (release / "rpi-arm64" / "sensorius.service").is_file()
    assert (release / "rpi-arm64" / "postrm").stat().st_mode & 0o111
    assert (release / "macos-arm64" / "Sensorius.spec").is_file()
    assert (release / "windows-x64" / "Sensorius.iss").is_file()
    assert (release / "SUPPORT.md").is_file()

    linux_postinstall = (release / "linux-amd64" / "postinst").read_text(encoding="utf-8")
    mac_postinstall = (release / "macos-arm64" / "scripts" / "postinstall").read_text(encoding="utf-8")
    windows_iss = (release / "windows-x64" / "Sensorius.iss").read_text(encoding="utf-8")
    assert "LOG_DIR=/var/log/sensorius" in linux_postinstall
    assert "/Library/Logs/Sensorius" in mac_postinstall
    assert "SetupLogging=yes" in windows_iss
    assert r"Sensorius\Logs" in windows_iss
    windows_spec = (release / "windows-x64" / "Sensorius.spec").read_text(encoding="utf-8")
    mac_spec = (release / "macos-arm64" / "Sensorius.spec").read_text(encoding="utf-8")
    assert "sensorius-macos-icon.png" in windows_spec
    assert f'"CFBundleVersion": "{generator.macos_bundle_version(version)}"' in mac_spec
    assert 'test "${HOST_ARCH}" = "amd64"' in (
        release / "linux-amd64" / "build.sh"
    ).read_text(encoding="utf-8")
    assert 'python3 (>= ${PYTHON_VERSION}), python3 (<< ${PYTHON_NEXT_MINOR})' in (
        release / "linux-amd64" / "build.sh"
    ).read_text(encoding="utf-8")


def test_existing_output_requires_replace(tmp_path):
    version = generator.canonical_version()
    generator.generate(tmp_path, ["linux-amd64"], version, False)
    try:
        generator.generate(tmp_path, ["linux-amd64"], version, False)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing output was unexpectedly replaced")


def test_invalid_target_is_rejected_before_output_is_created(tmp_path):
    try:
        generator.generate(tmp_path, ["unsupported"], "1.2.3", False)
    except ValueError as exc:
        assert "Unsupported target" in str(exc)
    else:
        raise AssertionError("unsupported target was unexpectedly accepted")
    assert not (tmp_path / "1.2.3").exists()


def test_macos_bundle_version_uses_three_numeric_components():
    assert generator.macos_bundle_version("0.26.230.16") == "26.230.16"
    assert generator.macos_bundle_version("2.4.6") == "2.4.6"
