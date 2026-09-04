"""Test desktop webview geometry defaults and environment overrides.

The cases ensure platform-independent sizing decisions remain predictable
without opening an actual desktop window.
"""

import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zipfile
from types import ModuleType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sensorius.saiGuiLauncher as saiGuiLauncher
import sensorius.saiWebServer as saiWebServer


def test_native_desktop_icon_is_a_rendered_png():
    assert saiGuiLauncher.DESKTOP_ICON_PATH.is_file()
    assert saiGuiLauncher.DESKTOP_ICON_PATH.name == "sensorius-icon.png"
    native_svg = saiGuiLauncher.DESKTOP_ICON_PATH.with_name(
        "sensorius-native-icon.svg"
    ).read_text(encoding="utf-8")
    assert 'viewBox="-50 -50 612 612"' in native_svg


def test_macos_icon_uses_its_platform_specific_scale():
    assert saiWebServer.MACOS_ICON_PATH.is_file()
    macos_svg = saiWebServer.MACOS_ICON_PATH.with_suffix(".svg").read_text(
        encoding="utf-8"
    )
    assert 'viewBox="-51.5355 -51.5355 615.071 615.071"' in macos_svg


def test_linux_launcher_uses_standard_interruptible_gtk_start(monkeypatch):
    calls = []
    identity_calls = []
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: None,
        start=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(saiGuiLauncher.sys, "platform", "linux")
    monkeypatch.setattr(saiGuiLauncher, "_wait_for_health", lambda _url: True)
    monkeypatch.setattr(
        saiGuiLauncher,
        "configure_linux_app_identity",
        lambda: identity_calls.append(True),
    )
    monkeypatch.setenv("DISPLAY", ":0")

    assert saiGuiLauncher.main() == 0
    assert identity_calls == [True]
    assert calls == [((), {"gui": "gtk"})]


def test_linux_identity_installs_matching_desktop_entry(monkeypatch, tmp_path):
    identity_calls = []
    fake_glib = SimpleNamespace(
        set_prgname=lambda value: identity_calls.append(("prgname", value)),
        set_application_name=lambda value: identity_calls.append(("name", value)),
    )
    fake_gi = ModuleType("gi")
    fake_gi.__path__ = []
    fake_repository = ModuleType("gi.repository")
    fake_repository.GLib = fake_glib
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repository)
    monkeypatch.setattr(saiWebServer.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    desktop_path = saiWebServer.configure_linux_app_identity()

    assert identity_calls == [
        ("prgname", "ai.sensorius.Sensorius"),
        ("name", "Sensorius"),
    ]
    assert desktop_path == (
        tmp_path / "applications" / "ai.sensorius.Sensorius.desktop"
    )
    desktop_text = desktop_path.read_text(encoding="utf-8")
    assert "Name=Sensorius\n" in desktop_text
    assert f"Icon={saiWebServer.DESKTOP_ICON_PATH}\n" in desktop_text
    assert "StartupWMClass=ai.sensorius.Sensorius\n" in desktop_text
    assert (
        tmp_path
        / "icons"
        / "hicolor"
        / "512x512"
        / "apps"
        / "ai.sensorius.Sensorius.png"
    ).read_bytes() == saiWebServer.DESKTOP_ICON_PATH.read_bytes()


def test_macos_launcher_sets_native_app_icon(monkeypatch):
    native_icon_calls = []
    shown_handlers = []

    class FakeEvent:
        def __iadd__(self, handler):
            shown_handlers.append(handler)
            return self

    fake_window = SimpleNamespace(events=SimpleNamespace(shown=FakeEvent()))
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: fake_window,
        start=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(saiGuiLauncher.sys, "platform", "darwin")
    monkeypatch.setattr(saiGuiLauncher, "_wait_for_health", lambda _url: True)
    monkeypatch.setattr(
        saiGuiLauncher,
        "set_macos_app_icon",
        lambda: native_icon_calls.append(True),
    )

    assert saiGuiLauncher.main() == 0
    assert shown_handlers == [saiGuiLauncher.set_macos_app_icon]
    shown_handlers[0]()
    assert native_icon_calls == [True]


def test_macos_source_gui_relaunches_from_named_app_bundle(monkeypatch, tmp_path):
    calls = []
    registrations = []
    python_executable = tmp_path / "python3"
    python_executable.touch()
    bundle_root = tmp_path / "Sensorius.app"
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(
        saiWebServer.subprocess,
        "run",
        lambda args, **kwargs: registrations.append((args, kwargs))
        or SimpleNamespace(returncode=0),
    )

    relaunched = saiWebServer.relaunch_as_named_macos_app(
        bundle_root=bundle_root,
        executable=python_executable,
        argv=["Sensorius.py", "--example"],
        environ={},
        execve=lambda path, args, env: calls.append((path, args, env)),
    )

    assert relaunched is True
    bundle_executable = bundle_root / "Contents" / "MacOS" / "Sensorius"
    assert bundle_executable.is_symlink()
    assert bundle_executable.resolve() == python_executable.resolve()
    info = plistlib.loads((bundle_root / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleDisplayName"] == "Sensorius"
    assert info["CFBundleIdentifier"] == "com.peacehillstudios.sensorius"
    assert info["CFBundleIconFile"] == "Sensorius.icns"
    expected_version = saiWebServer.__version__.removeprefix("v")
    assert info["CFBundleShortVersionString"] == expected_version
    assert info["CFBundleVersion"] == expected_version
    assert info["NSHighResolutionCapable"] is True
    assert (
        bundle_root / "Contents" / "Resources" / "Sensorius.icns"
    ).read_bytes() == saiWebServer.MACOS_ICNS_PATH.read_bytes()
    assert registrations[0][0] == [
        str(saiWebServer.MACOS_LSREGISTER),
        "-f",
        str(bundle_root),
    ]
    assert calls[0][0] == str(bundle_executable)
    assert calls[0][1] == [
        str(bundle_executable),
        "-m",
        "sensorius.app",
        "--example",
    ]
    assert calls[0][2]["SENSORIUS_MACOS_BUNDLE_RELAUNCHED"] == "1"
    assert calls[0][2]["PYTHONPATH"].split(os.pathsep)[0] == str(
        saiWebServer.PYTHON_PACKAGE_ROOT
    )


def test_macos_named_app_relaunch_skips_headless_and_recursive_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    unexpected_exec = lambda *_args: (_ for _ in ()).throw(AssertionError("must not relaunch"))

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=tmp_path / "headless.app",
        environ={"SENSORIUS_GUI": "0"},
        execve=unexpected_exec,
    ) is False
    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=tmp_path / "recursive.app",
        environ={"SENSORIUS_MACOS_BUNDLE_RELAUNCHED": "yes"},
        execve=unexpected_exec,
    ) is False


def test_macos_named_app_touches_registers_then_executes(monkeypatch, tmp_path):
    events = []
    python_executable = tmp_path / "python3"
    python_executable.touch()
    bundle_root = tmp_path / "Sensorius.app"
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(
        saiWebServer.os,
        "utime",
        lambda path, _times: events.append(("touch", path)),
    )
    monkeypatch.setattr(
        saiWebServer.subprocess,
        "run",
        lambda args, **_kwargs: events.append(("register", args))
        or SimpleNamespace(returncode=0),
    )

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=bundle_root,
        executable=python_executable,
        argv=["Sensorius.py"],
        environ={},
        execve=lambda *_args: events.append(("exec", None)),
    ) is True

    assert [event[0] for event in events] == ["touch", "register", "exec"]
    assert events[0][1] == bundle_root


def test_macos_registration_failure_warns_but_still_launches(monkeypatch, tmp_path):
    warnings = []
    launches = []
    python_executable = tmp_path / "python3"
    python_executable.touch()
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(
        saiWebServer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=7),
    )
    monkeypatch.setattr(
        saiWebServer,
        "printDM",
        lambda message, **_kwargs: warnings.append(message),
    )

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=tmp_path / "Sensorius.app",
        executable=python_executable,
        environ={},
        execve=lambda *args: launches.append(args),
    ) is True

    assert launches
    assert any("exit 7" in warning for warning in warnings)


def test_macos_relaunch_preserves_installed_pythonpath(monkeypatch, tmp_path):
    calls = []
    package_root = tmp_path / "site-packages"
    python_executable = tmp_path / "venv" / "bin" / "python3"
    python_executable.parent.mkdir(parents=True)
    python_executable.touch()
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(saiWebServer, "PYTHON_PACKAGE_ROOT", package_root)
    monkeypatch.setattr(
        saiWebServer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=tmp_path / "Sensorius.app",
        executable=python_executable,
        environ={"PYTHONPATH": f"/first{os.pathsep}{package_root}{os.pathsep}/last"},
        execve=lambda _path, _args, env: calls.append(env),
    ) is True

    assert calls[0]["PYTHONPATH"].split(os.pathsep) == [
        str(package_root),
        "/first",
        "/last",
    ]


def test_macos_relaunch_skips_unsupported_runtime_contexts(monkeypatch, tmp_path):
    unexpected_exec = lambda *_args: (_ for _ in ()).throw(AssertionError("must not relaunch"))
    monkeypatch.setattr(saiWebServer.sys, "platform", "linux")
    assert saiWebServer.relaunch_as_named_macos_app(execve=unexpected_exec) is False

    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(saiWebServer.sys, "frozen", True, raising=False)
    assert saiWebServer.relaunch_as_named_macos_app(execve=unexpected_exec) is False
    monkeypatch.delattr(saiWebServer.sys, "frozen")
    monkeypatch.setattr(saiWebServer.sys, "_MEIPASS", "/tmp/frozen", raising=False)
    assert saiWebServer.relaunch_as_named_macos_app(execve=unexpected_exec) is False
    monkeypatch.delattr(saiWebServer.sys, "_MEIPASS")

    bundled_python = tmp_path / "Existing.app" / "Contents" / "MacOS" / "python3"
    bundled_python.parent.mkdir(parents=True)
    bundled_python.touch()
    assert saiWebServer.relaunch_as_named_macos_app(
        executable=bundled_python,
        execve=unexpected_exec,
    ) is False


def test_macos_missing_icon_fails_gracefully(monkeypatch, tmp_path):
    warnings = []
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(saiWebServer, "MACOS_ICNS_PATH", tmp_path / "missing.icns")
    monkeypatch.setattr(
        saiWebServer,
        "printDM",
        lambda message, **_kwargs: warnings.append(message),
    )

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=tmp_path / "Sensorius.app",
        environ={},
        execve=lambda *_args: None,
    ) is False
    assert any("Unable to prepare" in warning for warning in warnings)


def test_macos_relaunch_replaces_stale_icon_and_symlink(monkeypatch, tmp_path):
    bundle_root = tmp_path / "Sensorius.app"
    executable_dir = bundle_root / "Contents" / "MacOS"
    resources_dir = bundle_root / "Contents" / "Resources"
    executable_dir.mkdir(parents=True)
    resources_dir.mkdir()
    old_python = tmp_path / "old-python"
    new_python = tmp_path / "new-python"
    old_python.touch()
    new_python.touch()
    bundle_executable = executable_dir / "Sensorius"
    bundle_executable.symlink_to(old_python)
    bundle_icon = resources_dir / "Sensorius.icns"
    bundle_icon.write_bytes(b"stale icon")
    monkeypatch.setattr(saiWebServer.sys, "platform", "darwin")
    monkeypatch.setattr(
        saiWebServer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert saiWebServer.relaunch_as_named_macos_app(
        bundle_root=bundle_root,
        executable=new_python,
        environ={},
        execve=lambda *_args: None,
    ) is True

    assert bundle_executable.is_symlink()
    assert bundle_executable.resolve() == new_python.resolve()
    assert bundle_icon.read_bytes() == saiWebServer.MACOS_ICNS_PATH.read_bytes()


def _icns_png_dimensions(path):
    data = path.read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    dimensions = set()
    offset = 8
    while offset < len(data):
        chunk_length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        png = data[offset + 8 : offset + chunk_length]
        if png.startswith(b"\x89PNG\r\n\x1a\n"):
            dimensions.add(struct.unpack(">II", png[16:24]))
        offset += chunk_length
    return dimensions


def test_packaged_icns_contains_required_physical_png_sizes():
    dimensions = _icns_png_dimensions(saiWebServer.MACOS_ICNS_PATH)
    assert {(size, size) for size in (32, 64, 128, 256, 512, 1024)} <= dimensions


def test_icns_resource_is_in_built_wheel(tmp_path):
    build_source = tmp_path / "source"
    shutil.copytree(
        saiWebServer.PROJECT_ROOT / "sensorius",
        build_source / "sensorius",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(saiWebServer.PROJECT_ROOT / "pyproject.toml", build_source)
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(build_source),
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(wheel_dir.glob("sensorius-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "sensorius/resources/Sensorius.icns" in archive.namelist()
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        assert f"Version: {saiWebServer.__version__.removeprefix('v')}" in metadata


def test_window_geometry_defaults_keep_titlebar_visible(monkeypatch):
    for key in (
        "SENSORIUS_GUI_WIDTH",
        "SENSORIUS_GUI_HEIGHT",
        "SENSORIUS_GUI_X",
        "SENSORIUS_GUI_Y",
    ):
        monkeypatch.delenv(key, raising=False)

    assert saiGuiLauncher._window_geometry() == {
        "width": 1920,
        "height": 1000,
        "x": 0,
        "y": 48,
    }


def test_window_geometry_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SENSORIUS_GUI_WIDTH", "1280")
    monkeypatch.setenv("SENSORIUS_GUI_HEIGHT", "720")
    monkeypatch.setenv("SENSORIUS_GUI_X", "12")
    monkeypatch.setenv("SENSORIUS_GUI_Y", "64")

    assert saiGuiLauncher._window_geometry() == {
        "width": 1280,
        "height": 720,
        "x": 12,
        "y": 64,
    }


def test_window_geometry_ignores_invalid_overrides(monkeypatch):
    monkeypatch.setenv("SENSORIUS_GUI_WIDTH", "bad")
    monkeypatch.setenv("SENSORIUS_GUI_Y", "bad")

    geometry = saiGuiLauncher._window_geometry()

    assert geometry["width"] == 1920
    assert geometry["y"] == 48
