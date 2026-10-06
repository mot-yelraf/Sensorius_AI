"""Run desktop launchers and install the per-user macOS Finder app.

On macOS the native bundle remains the responsible parent of the Python runtime
so the system can attribute local-network consent to Sensorius. Installation
never starts the hub or changes the user's privacy or firewall settings.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from . import __version__

MACOS_LAUNCHER_ID = "com.peacehillstudios.sensorius.launcher"
RESOURCES = Path(__file__).resolve().parent / "resources"
LOCAL_NETWORK_DESCRIPTION = (
    "Sensorius connects to MQTT brokers, Nodus sensors and switches, "
    "and other configured services on your local network."
)


def write_macos_app_launcher(
    runtime_dir: Path, python_executable: Path, *, bundle: Path | None = None,
    signing_identity: str = "-",
) -> Path:
    """Create a signed Finder app targeting an installed runtime and its venv."""
    runtime_dir = runtime_dir.expanduser().resolve()
    # Keep the venv path: resolving its Python symlink loses venv ownership.
    python_executable = python_executable.expanduser().absolute()
    if not (runtime_dir / "Sensorius.py").is_file():
        raise FileNotFoundError(f"Sensorius runtime is missing: {runtime_dir}")
    if not python_executable.is_file():
        raise FileNotFoundError(f"Python interpreter is missing: {python_executable}")
    bundle = bundle or Path.home() / "Applications" / "Sensorius.app"
    info_path = bundle / "Contents" / "Info.plist"
    if bundle.exists():
        if not info_path.is_file() or plistlib.loads(info_path.read_bytes()).get(
            "CFBundleIdentifier"
        ) != MACOS_LAUNCHER_ID:
            raise FileExistsError(f"Refusing to overwrite another application: {bundle}")
    executable_dir = bundle / "Contents" / "MacOS"
    resources_dir = bundle / "Contents" / "Resources"
    executable_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)
    version = __version__.removeprefix("v0.")
    info_path.write_bytes(plistlib.dumps({
        "CFBundleDisplayName": "Sensorius",
        "CFBundleName": "Sensorius",
        "CFBundleExecutable": "Sensorius",
        "CFBundleIdentifier": MACOS_LAUNCHER_ID,
        "CFBundleIconFile": "Sensorius.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSUIElement": True,
        "NSLocalNetworkUsageDescription": LOCAL_NETWORK_DESCRIPTION,
        "SensoriusInstallDirectory": str(runtime_dir),
    }))
    shutil.copyfile(RESOURCES / "Sensorius.icns", resources_dir / "Sensorius.icns")
    executable = executable_dir / "Sensorius"
    temporary_executable = executable.with_suffix(".tmp")
    shutil.copyfile(RESOURCES / "sensorius-macos-launcher", temporary_executable)
    temporary_executable.chmod(0o755)
    temporary_executable.replace(executable)
    log_dir = Path.home() / "Library" / "Logs" / "Sensorius"
    script = resources_dir / "launch.sh"
    script.write_text(
        "#!/bin/bash\nset -eu\n"
        f"runtime_dir={shlex.quote(str(runtime_dir))}\n"
        f"python_executable={shlex.quote(str(python_executable))}\n"
        f"log_dir={shlex.quote(str(log_dir))}\n"
        'mkdir -p "$log_dir"\n'
        'log_file="$log_dir/desktop-launch.log"\n'
        'export SENSORIUS_MACOS_BUNDLE_RELAUNCHED=1 SENSORIUS_GUI=1\n'
        'export SENSORIUS_PROJECT_ROOT="$runtime_dir"\n'
        'export SENSORIUS_RUNTIME_ROOT="$runtime_dir"\n'
        'export SENSORIUS_ENV_FILE="$runtime_dir/.env"\n'
        'export PYTHONPATH="$runtime_dir${PYTHONPATH:+:$PYTHONPATH}"\n'
        'if (cd "$runtime_dir" && "$python_executable" -m sensorius.saiAppLauncher --launch "$@") >>"$log_file" 2>&1; then\n'
        '  exit 0\n'
        'fi\n'
        '/usr/bin/osascript - "$log_file" <<\'APPLESCRIPT\'\n'
        'on run argv\n'
        '  display alert "Sensorius could not start" message '
        '("Run the installer again to repair the application. Details: " & item 1 of argv) as critical\n'
        'end run\nAPPLESCRIPT\nexit 1\n', encoding="utf-8",
    )
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", signing_identity,
         "--identifier", MACOS_LAUNCHER_ID, str(bundle)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", str(bundle)],
        check=True, capture_output=True, text=True,
    )
    try:
        subprocess.run(
            ["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
             "LaunchServices.framework/Support/lsregister", "-f", str(bundle)],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not refresh Sensorius Finder metadata: {exc}", file=sys.stderr)
    return bundle


def launch_desktop() -> int:
    """Open the running hub, or run the hub and desktop together."""
    from .saiSettings import saiSettings
    from . import saiGuiLauncher
    from .saiGuiEnvironment import configure_gui_environment

    configure_gui_environment()
    import webview  # Fail visibly before starting a hub when GUI support is missing.

    settings = saiSettings(make_startup_backup=False, apply_live=False)
    port = int(os.environ.get("SENSORIUS_HTTP_PORT") or settings.get_setting("Network", "HTTPPORT", 8000))
    configured_url = os.environ.get("SENSORIUS_GUI_URL")
    base_url = configured_url or f"http://127.0.0.1:{port}/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip("/") + "/healthz", timeout=3) as response:
            healthy = response.status == 200
    except (OSError, urllib.error.URLError):
        healthy = False
    os.environ["SENSORIUS_GUI_URL"] = base_url
    if healthy or configured_url:
        return saiGuiLauncher.main()
    from .app import run_application

    run_application(require_gui=True)
    return 0


def main() -> int:
    """Install a platform launch icon, or start or attach to the desktop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_dir", nargs="?", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--signing-identity", default="-")
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    try:
        if args.launch:
            return launch_desktop()
        if args.runtime_dir is None:
            parser.error("Installing a launcher requires a runtime directory.")
        runtime_dir = args.runtime_dir.expanduser().resolve()
        if sys.platform == "darwin":
            path = write_macos_app_launcher(runtime_dir, args.python, signing_identity=args.signing_identity)
        elif sys.platform.startswith("linux"):
            os.environ["SENSORIUS_PROJECT_ROOT"] = str(runtime_dir)
            from .saiWebServer import configure_linux_app_identity

            path = configure_linux_app_identity(python_executable=args.python)
            if path is None:
                raise OSError("Could not install the Sensorius application-menu entry.")
        else:
            parser.error("This icon installer supports macOS and Linux.")
        print(f"Click-to-launch icon: {path}")
        return 0
    except (OSError, ValueError, ImportError, RuntimeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else None
        print(f"Sensorius launcher failed: {exc}{': ' + detail.strip() if detail else ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
