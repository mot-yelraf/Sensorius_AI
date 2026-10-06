"""Verify Finder launcher generation and desktop dispatch in isolated runtimes.

Tests exercise path quoting, signing commands, bundle ownership and hub attach
behavior without starting production services or changing network permissions.
"""

from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sensorius import saiAppLauncher


def make_runtime(tmp_path):
    runtime = tmp_path / "runtime 'with $ spaces"
    runtime.mkdir(parents=True)
    (runtime / "Sensorius.py").touch()
    python = runtime / "fake python"
    python.write_text('#!/bin/bash\nprintf "%s\\n" "$PWD" "$@" "$SENSORIUS_MACOS_BUNDLE_RELAUNCHED" "$SENSORIUS_GUI" "$SENSORIUS_PROJECT_ROOT" "$SENSORIUS_RUNTIME_ROOT" "$SENSORIUS_ENV_FILE"\n')
    python.chmod(0o755)
    return runtime, python


def test_bundle_signing_quoting_and_reinstallation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime, python = make_runtime(tmp_path)
    monkeypatch.setenv("SENSORIUS_RUNTIME_ROOT", str(tmp_path / "wrong runtime"))
    monkeypatch.setenv("SENSORIUS_ENV_FILE", str(tmp_path / "wrong.env"))
    venv_python = runtime / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(python)
    python = venv_python
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(saiAppLauncher.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    bundle = saiAppLauncher.write_macos_app_launcher(runtime, python)
    assert bundle == tmp_path / "Applications/Sensorius.app"
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == saiAppLauncher.MACOS_LAUNCHER_ID
    assert "MQTT brokers" in info["NSLocalNetworkUsageDescription"]
    assert info["SensoriusInstallDirectory"] == str(runtime)
    executable = bundle / "Contents/MacOS/Sensorius"
    assert not executable.is_symlink()
    assert executable.read_bytes() == (saiAppLauncher.RESOURCES / "sensorius-macos-launcher").read_bytes()
    assert calls[0] == ["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", saiAppLauncher.MACOS_LAUNCHER_ID, str(bundle)]
    assert calls[1] == ["/usr/bin/codesign", "--verify", "--strict", str(bundle)]
    assert ".venv/bin/python" in (bundle / "Contents/Resources/launch.sh").read_text()
    real_run(["bash", str(bundle / "Contents/Resources/launch.sh"), "argument with spaces"], check=True)
    log = tmp_path / "Library/Logs/Sensorius/desktop-launch.log"
    content = log.read_text()
    assert str(runtime) in content
    assert "argument with spaces\n1\n1\n" in content
    assert "sensorius.saiAppLauncher\n--launch\n" in content
    assert f"{runtime}\n{runtime}\n{runtime / '.env'}\n" in content
    assert saiAppLauncher.write_macos_app_launcher(runtime, python) == bundle
    assert log.read_text() == content
    history = runtime / "sensorius_data.db"
    history.write_bytes(b"preserve installed history")
    relocated, relocated_python = make_runtime(tmp_path / "another location")
    assert saiAppLauncher.write_macos_app_launcher(relocated, relocated_python) == bundle
    updated = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert updated["SensoriusInstallDirectory"] == str(relocated)
    real_run(["bash", str(bundle / "Contents/Resources/launch.sh")], check=True)
    assert f"{relocated}\n{relocated}\n{relocated / '.env'}\n" in log.read_text()
    assert history.read_bytes() == b"preserve installed history"


def test_refuse_unrelated_application(tmp_path):
    runtime, python = make_runtime(tmp_path)
    bundle = tmp_path / "Sensorius.app"
    bundle.mkdir()
    marker = bundle / "keep"
    marker.write_text("user data")
    with pytest.raises(FileExistsError):
        saiAppLauncher.write_macos_app_launcher(runtime, python, bundle=bundle)
    assert marker.read_text() == "user data"


def test_signing_failure_is_reported(tmp_path, monkeypatch):
    runtime, python = make_runtime(tmp_path)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="signing failed")
    monkeypatch.setattr(saiAppLauncher.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        saiAppLauncher.write_macos_app_launcher(runtime, python, bundle=tmp_path / "Sensorius.app")


@pytest.mark.parametrize("healthy", [True, False])
def test_desktop_attaches_or_starts_hub(tmp_path, monkeypatch, healthy):
    from sensorius import saiGuiLauncher
    calls = []
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    settings = SimpleNamespace(get_setting=lambda *args: 8123)
    monkeypatch.setitem(sys.modules, "sensorius.saiSettings", SimpleNamespace(saiSettings=lambda **kwargs: settings))
    def run_application(*, require_gui):
        assert require_gui is True
        calls.append("hub")
    monkeypatch.setitem(sys.modules, "sensorius.app", SimpleNamespace(run_application=run_application))
    monkeypatch.setattr(saiGuiLauncher, "main", lambda: calls.append("gui") or 0)
    monkeypatch.delenv("SENSORIUS_GUI_URL", raising=False)
    monkeypatch.delenv("SENSORIUS_HTTP_PORT", raising=False)
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_url(url, timeout):
        assert url == "http://127.0.0.1:8123/healthz"
        if not healthy:
            raise OSError("not running")
        return Response()
    monkeypatch.setattr(saiAppLauncher.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_url))
    assert saiAppLauncher.launch_desktop() == 0
    assert calls == (["gui"] if healthy else ["hub"])
    assert saiAppLauncher.os.environ["SENSORIUS_GUI_URL"] == "http://127.0.0.1:8123/"


@pytest.mark.skipif(sys.platform != "darwin", reason="native bundle signing requires macOS")
def test_real_native_launcher_and_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime, python = make_runtime(tmp_path)
    bundle = saiAppLauncher.write_macos_app_launcher(runtime, python)
    result = subprocess.run([str(bundle / "Contents/MacOS/Sensorius"), "native argument"], check=False, timeout=10)
    assert result.returncode == 0
    assert "native argument" in (tmp_path / "Library/Logs/Sensorius/desktop-launch.log").read_text()


def test_both_macos_installers_create_launcher():
    root = Path(__file__).resolve().parents[1]
    for name in ("setup_mac_legacy.sh", "setup_mac_uv_legacy.sh"):
        text = (root / "deploy_scripts" / name).read_text()
        assert '-m sensorius.saiAppLauncher' in text
        assert '--python "${VENV_PATH}/bin/python"' in text


def test_linux_icon_installer_uses_selected_interpreter(tmp_path, monkeypatch):
    from sensorius import saiWebServer
    runtime, python = make_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(saiAppLauncher.sys, "platform", "linux")
    monkeypatch.setattr(saiAppLauncher.sys, "argv", ["launcher", str(runtime), "--python", str(python)])
    monkeypatch.delenv("SENSORIUS_PROJECT_ROOT", raising=False)
    def install(*, python_executable):
        calls.append(python_executable)
        assert saiAppLauncher.os.environ["SENSORIUS_PROJECT_ROOT"] == str(runtime)
        return tmp_path / "Sensorius.desktop"
    monkeypatch.setattr(saiWebServer, "configure_linux_app_identity", install)
    assert saiAppLauncher.main() == 0
    assert calls == [python]
