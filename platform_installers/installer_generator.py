#!/usr/bin/env python3
"""Generate native installer build projects for supported Sensorius targets.

The generator stages a deterministic application payload and emits target-
native build scripts. It intentionally does not cross-compile: native Python
extensions, desktop webviews, Raspberry Pi GPIO libraries, and code signing
must be resolved on the target operating system and architecture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_OUTPUT = HERE / "generated"
TARGETS = (
    "linux-amd64",
    "rpi-arm64",
    "rpi-armhf",
    "macos-arm64",
    "macos-x86_64",
    "windows-x64",
)

PAYLOAD_PATHS = (
    "Sensorius.py",
    "sensorius",
    "ui_static",
    "ui_templates",
    "data",
    "system_settings/factory",
    "system_settings/factory_nodus",
    "sensor_settings/factory",
    "sensor_settings/factory_nodus",
    "switch_settings/factory",
    "switch_settings/factory_nodus",
    "ota_packages",
    ".env.def",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)

COMMON_REQUIREMENTS = "deploy_scripts/setup_reqs_linux.txt"
RPI_BOOKWORM_REQUIREMENTS = "deploy_scripts/setup_reqs.txt"
RPI_TRIXIE_REQUIREMENTS = "deploy_scripts/setup_reqs_trixie.txt"
MACOS_REQUIREMENTS = "deploy_scripts/setup_reqs_mac.txt"
WINDOWS_REQUIREMENTS = "deploy_scripts/setup_reqs_win.txt"


@dataclass(frozen=True)
class GeneratedFile:
    path: str
    size: int
    sha256: str


def canonical_version() -> str:
    """Read and normalize the canonical version from the Sensorius package."""
    source = (REPO_ROOT / "sensorius" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']v?([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to read canonical version from sensorius/__init__.py")
    return match.group(1)


def safe_version(value: str) -> str:
    """Validate a version before using it in filesystem or package metadata."""
    value = value.strip().lstrip("v")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,4}", value):
        raise ValueError(f"Unsupported version format: {value!r}")
    return value


def macos_bundle_version(value: str) -> str:
    """Return Apple's three-component numeric bundle version."""
    parts = safe_version(value).split(".")
    return ".".join(parts[-3:])


def copy_payload(destination: Path) -> None:
    """Copy the runtime payload while excluding caches and local runtime state."""
    destination.mkdir(parents=True, exist_ok=False)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
    for relative in PAYLOAD_PATHS:
        source = REPO_ROOT / relative
        if not source.exists():
            raise FileNotFoundError(f"Required payload path is missing: {source}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copy2(source, target)


def copy_requirements(release_root: Path) -> None:
    """Copy target requirements into the generated release workspace."""
    req_root = release_root / "requirements"
    req_root.mkdir()
    mapping = {
        "linux.txt": COMMON_REQUIREMENTS,
        "rpi-bookworm.txt": RPI_BOOKWORM_REQUIREMENTS,
        "rpi-trixie.txt": RPI_TRIXIE_REQUIREMENTS,
        "macos.txt": MACOS_REQUIREMENTS,
        "windows.txt": WINDOWS_REQUIREMENTS,
    }
    for name, source_name in mapping.items():
        shutil.copy2(REPO_ROOT / source_name, req_root / name)


def write_text(path: Path, value: str, *, executable: bool = False) -> None:
    """Write normalized UTF-8 text and optionally mark it executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8", newline="\n")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def runtime_hook_text() -> str:
    """Return the frozen-app hook that separates resources from runtime state."""
    return r'''
"""Initialize resource and writable paths before importing Sensorius."""

import os
import sys
from pathlib import Path

resource_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
configured_runtime_root = str(os.environ.get("SENSORIUS_RUNTIME_ROOT") or "").strip()
if configured_runtime_root:
    runtime_root = Path(configured_runtime_root).expanduser().resolve()
elif sys.platform == "darwin":
    runtime_root = Path.home() / "Library" / "Application Support" / "Sensorius"
elif os.name == "nt":
    runtime_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Sensorius"
else:
    runtime_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "sensorius"

runtime_root.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("SENSORIUS_PROJECT_ROOT", str(resource_root))
os.environ.setdefault("SENSORIUS_RUNTIME_ROOT", str(runtime_root))
os.environ.setdefault("SENSORIUS_ENV_FILE", str(runtime_root / ".env"))
os.chdir(runtime_root)
'''


def pyinstaller_spec(version: str, target: str) -> str:
    """Return a PyInstaller specification for a desktop target."""
    name = "Sensorius"
    console = "False" if target.startswith("macos-") else "True"
    bundle_version = macos_bundle_version(version)
    executable_icon = (
        'str(payload / "ui_static" / "sensorius-macos-icon.png")'
        if target.startswith(("macos-", "windows-"))
        else "None"
    )
    bundle = ""
    if target.startswith("macos-"):
        bundle = f'''\napp = BUNDLE(
    coll,
    name="{name}.app",
    icon=str(payload / "ui_static" / "sensorius-macos-icon.png"),
    bundle_identifier="com.peacehillstudios.sensorius",
    info_plist={{
        "CFBundleShortVersionString": "{bundle_version}",
        "CFBundleVersion": "{bundle_version}",
        "NSHighResolutionCapable": True,
    }},
)
'''
    return f'''
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project = Path(SPECPATH).resolve()
payload = (project / ".." / "payload").resolve()
datas = [
    (str(payload / "ui_static"), "ui_static"),
    (str(payload / "ui_templates"), "ui_templates"),
    (str(payload / "data"), "data"),
    (str(payload / "system_settings"), "system_settings"),
    (str(payload / "sensor_settings"), "sensor_settings"),
    (str(payload / "switch_settings"), "switch_settings"),
    (str(payload / "ota_packages"), "ota_packages"),
    (str(payload / ".env.def"), "."),
    (str(payload / "LICENSE"), "."),
    (str(payload / "THIRD_PARTY_NOTICES.md"), "."),
]

a = Analysis(
    [str(payload / "Sensorius.py")],
    pathex=[str(payload)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "sensorius.sensor_modules.sensor_aht",
        "sensorius.sensor_modules.sensor_apvpd",
        "sensorius.sensor_modules.sensor_aqi",
        "sensorius.sensor_modules.sensor_co2",
        "sensorius.sensor_modules.sensor_dummy",
        "sensorius.sensor_modules.sensor_sgp",
        "sensorius.sensor_modules.sensor_veml",
        "sensorius.sensor_modules.sensor_vpd",
        "sensorius.sensor_modules.station_ecowitt",
        "sensorius.sensor_modules.station_weewx",
        "webview",
    ],
    hookspath=[],
    runtime_hooks=[str(project / "runtime_hook.py")],
    excludes=["pytest", "coverage"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="{name}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console={console},
    icon={executable_icon},
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="{name}")
{bundle}
'''


def linux_build_script(profile: str, architecture: str, version: str) -> str:
    """Return the native Debian package build script for a Linux profile."""
    if profile == "rpi":
        requirement_setup = '''RPI_RELEASE="${RPI_RELEASE:-bookworm}"
case "${RPI_RELEASE}" in
  bookworm) DEFAULT_REQ="${RELEASE_DIR}/requirements/rpi-bookworm.txt" ;;
  trixie) DEFAULT_REQ="${RELEASE_DIR}/requirements/rpi-trixie.txt" ;;
  *) echo "Unsupported RPI_RELEASE: ${RPI_RELEASE} (expected bookworm or trixie)" >&2; exit 1 ;;
esac'''
    else:
        requirement_setup = 'DEFAULT_REQ="${RELEASE_DIR}/requirements/linux.txt"'
    package = "sensorius-rpi" if profile == "rpi" else "sensorius"
    extra_depends = ", i2c-tools" if profile == "rpi" else ""
    return f'''#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
RELEASE_DIR="$(cd "${{PROJECT_DIR}}/.." && pwd)"
BUILD_DIR="${{PROJECT_DIR}}/build"
ROOT_DIR="${{BUILD_DIR}}/root"
DIST_DIR="${{PROJECT_DIR}}/dist"
PYTHON_BIN="${{PYTHON_BIN:-python3}}"
{requirement_setup}
REQ_FILE="${{REQ_FILE:-${{DEFAULT_REQ}}}}"

command -v dpkg-deb >/dev/null 2>&1 || {{ echo 'dpkg-deb is required' >&2; exit 1; }}
HOST_ARCH="$(dpkg --print-architecture)"
test "${{HOST_ARCH}}" = "{architecture}" || {{
  echo "Expected {architecture} build host, found ${{HOST_ARCH}}" >&2
  exit 1
}}
PYTHON_VERSION="$("${{PYTHON_BIN}}" -c 'import sys; print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")')"
PYTHON_MAJOR="${{PYTHON_VERSION%%.*}}"
PYTHON_MINOR="${{PYTHON_VERSION##*.}}"
PYTHON_NEXT_MINOR="${{PYTHON_MAJOR}}.$((PYTHON_MINOR + 1))"

rm -rf "${{BUILD_DIR}}" "${{DIST_DIR}}"
mkdir -p "${{ROOT_DIR}}/DEBIAN" "${{ROOT_DIR}}/opt/sensorius/vendor" \
  "${{ROOT_DIR}}/usr/bin" "${{ROOT_DIR}}/lib/systemd/system" "${{ROOT_DIR}}/var/lib/sensorius/Sensorius" \
  "${{DIST_DIR}}"

cp -a "${{RELEASE_DIR}}/payload/." "${{ROOT_DIR}}/opt/sensorius/"
"${{PYTHON_BIN}}" -m pip install --disable-pip-version-check --no-compile \
  --target "${{ROOT_DIR}}/opt/sensorius/vendor" -r "${{REQ_FILE}}"

cat > "${{ROOT_DIR}}/DEBIAN/control" <<EOF
Package: {package}
Version: {version}
Section: misc
Priority: optional
Architecture: {architecture}
Maintainer: Peace Hill Studios
Depends: python3 (>= ${{PYTHON_VERSION}}), python3 (<< ${{PYTHON_NEXT_MINOR}}), mosquitto{extra_depends}
Description: Sensorius environmental sensing and automation hub
 Cross-platform MQTT, web UI, environmental sensing, and automation runtime.
EOF

cp "${{PROJECT_DIR}}/postinst" "${{ROOT_DIR}}/DEBIAN/postinst"
cp "${{PROJECT_DIR}}/prerm" "${{ROOT_DIR}}/DEBIAN/prerm"
chmod 0755 "${{ROOT_DIR}}/DEBIAN/postinst" "${{ROOT_DIR}}/DEBIAN/prerm"

cat > "${{ROOT_DIR}}/usr/bin/sensorius" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export HOME=/var/lib/sensorius
export SENSORIUS_PROJECT_ROOT=/opt/sensorius
export SENSORIUS_RUNTIME_ROOT=/var/lib/sensorius/Sensorius
export SENSORIUS_ENV_FILE=/var/lib/sensorius/Sensorius/.env
cd /var/lib/sensorius/Sensorius
exec /usr/bin/python3 /opt/sensorius/Sensorius.py "$@"
EOF
chmod 0755 "${{ROOT_DIR}}/usr/bin/sensorius"

cp "${{PROJECT_DIR}}/sensorius.service" "${{ROOT_DIR}}/lib/systemd/system/sensorius.service"
dpkg-deb --root-owner-group --build "${{ROOT_DIR}}" "${{DIST_DIR}}/{package}_{version}_{architecture}.deb"
echo "Built ${{DIST_DIR}}/{package}_{version}_{architecture}.deb"
'''


def generate_linux(release_root: Path, target: str, version: str) -> None:
    """Generate Debian package inputs for Linux or Raspberry Pi OS."""
    profile, architecture = target.split("-", 1)
    target_root = release_root / target
    write_text(target_root / "build.sh", linux_build_script(profile, architecture, version), executable=True)
    write_text(
        target_root / "sensorius.service",
        '''[Unit]
Description=Sensorius environmental sensing and automation hub
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=sensorius
Group=sensorius
WorkingDirectory=/var/lib/sensorius/Sensorius
Environment=HOME=/var/lib/sensorius
Environment=SENSORIUS_PROJECT_ROOT=/opt/sensorius
Environment=SENSORIUS_RUNTIME_ROOT=/var/lib/sensorius/Sensorius
Environment=SENSORIUS_ENV_FILE=/var/lib/sensorius/Sensorius/.env
ExecStart=/usr/bin/sensorius
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
''',
    )
    write_text(
        target_root / "postinst",
        f'''#!/bin/sh
set -e
LOG_DIR=/var/log/sensorius
LOG_FILE="${{LOG_DIR}}/install.log"
mkdir -p "${{LOG_DIR}}"
touch "${{LOG_FILE}}"
chmod 0644 "${{LOG_FILE}}"
log() {{ printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "${{LOG_FILE}}"; }}
finish() {{ rc=$?; trap - EXIT; log "post-install finished status=${{rc}}"; exit "${{rc}}"; }}
trap finish EXIT
log "post-install started version={version} action=${{1:-configure}} architecture=$(dpkg --print-architecture 2>/dev/null || uname -m)"
uname -a >> "${{LOG_FILE}}" 2>&1 || true
if ! getent group sensorius >/dev/null 2>&1; then
  addgroup --system sensorius >> "${{LOG_FILE}}" 2>&1
fi
if ! getent passwd sensorius >/dev/null 2>&1; then
  adduser --system --ingroup sensorius --home /var/lib/sensorius --no-create-home sensorius >> "${{LOG_FILE}}" 2>&1
fi
install -d -o sensorius -g sensorius -m 0750 /var/lib/sensorius /var/lib/sensorius/Sensorius
if [ ! -f /var/lib/sensorius/Sensorius/.env ] && [ -f /opt/sensorius/.env.def ]; then
  install -o sensorius -g sensorius -m 0600 /opt/sensorius/.env.def /var/lib/sensorius/Sensorius/.env
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >> "${{LOG_FILE}}" 2>&1
  systemctl enable sensorius.service >> "${{LOG_FILE}}" 2>&1 || true
fi
exit 0
''',
        executable=True,
    )
    write_text(
        target_root / "prerm",
        '''#!/bin/sh
set -e
LOG_DIR=/var/log/sensorius
LOG_FILE="${LOG_DIR}/install.log"
mkdir -p "${LOG_DIR}"
touch "${LOG_FILE}"
chmod 0644 "${LOG_FILE}"
log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "${LOG_FILE}"; }
finish() { rc=$?; trap - EXIT; log "pre-remove finished status=${rc}"; exit "${rc}"; }
trap finish EXIT
log "pre-remove started action=${1:-remove}"
if [ "$1" = remove ] && command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now sensorius.service >> "${LOG_FILE}" 2>&1 || true
fi
exit 0
''',
        executable=True,
    )
    write_text(
        target_root / "postrm",
        '''#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
''',
        executable=True,
    )


def generate_macos(release_root: Path, target: str, version: str) -> None:
    """Generate a PyInstaller application and product package project for macOS."""
    arch = target.removeprefix("macos-")
    package_version = macos_bundle_version(version)
    target_root = release_root / target
    write_text(target_root / "runtime_hook.py", runtime_hook_text())
    write_text(target_root / "Sensorius.spec", pyinstaller_spec(version, target))
    write_text(
        target_root / "scripts" / "postinstall",
        f'''#!/bin/sh
set -e
LOG_DIR=/Library/Logs/Sensorius
LOG_FILE="${{LOG_DIR}}/install.log"
mkdir -p "${{LOG_DIR}}"
touch "${{LOG_FILE}}"
chmod 0644 "${{LOG_FILE}}"
log() {{ printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "${{LOG_FILE}}"; }}
finish() {{ rc=$?; trap - EXIT; log "post-install finished status=${{rc}}"; exit "${{rc}}"; }}
trap finish EXIT
log "post-install started version={version} architecture=$(uname -m) installer_target=${{3:-/}}"
sw_vers >> "${{LOG_FILE}}" 2>&1 || true
uname -a >> "${{LOG_FILE}}" 2>&1 || true
if [ ! -d /Applications/Sensorius.app ]; then
  log "error: /Applications/Sensorius.app is missing"
  exit 1
fi
codesign --verify --deep --strict /Applications/Sensorius.app >> "${{LOG_FILE}}" 2>&1 || \
  log "warning: application signature verification did not pass"
log "application installed at /Applications/Sensorius.app"
exit 0
''',
        executable=True,
    )
    write_text(
        target_root / "build.sh",
        f'''#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
RELEASE_DIR="$(cd "${{PROJECT_DIR}}/.." && pwd)"
BUILD_DIR="${{PROJECT_DIR}}/build"
DIST_DIR="${{PROJECT_DIR}}/dist"
VENV_DIR="${{PROJECT_DIR}}/.build-venv"
if [[ -n "${{PYTHON_BIN:-}}" ]]; then
  PYTHON_BIN="${{PYTHON_BIN}}"
elif command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.13)"
else
  echo 'Python 3.13 is required; set PYTHON_BIN to its absolute path.' >&2
  exit 1
fi

test "$(uname -s)" = Darwin || {{ echo 'macOS build host required' >&2; exit 1; }}
test "$(uname -m)" = "{arch}" || {{ echo 'Expected {arch} build host' >&2; exit 1; }}
rm -rf "${{BUILD_DIR}}" "${{DIST_DIR}}"
if [[ ! -x "${{VENV_DIR}}/bin/python" ]]; then
  "${{PYTHON_BIN}}" -m venv "${{VENV_DIR}}"
fi
"${{VENV_DIR}}/bin/python" -m pip install --upgrade pip pyinstaller
"${{VENV_DIR}}/bin/python" -m pip install -r "${{RELEASE_DIR}}/requirements/macos.txt"
"${{VENV_DIR}}/bin/pyinstaller" --noconfirm --clean --workpath "${{BUILD_DIR}}/pyinstaller" \
  --distpath "${{BUILD_DIR}}/dist" "${{PROJECT_DIR}}/Sensorius.spec"

APP="${{BUILD_DIR}}/dist/Sensorius.app"
if [[ -n "${{MACOS_APP_SIGN_IDENTITY:-}}" ]]; then
  codesign --force --deep --options runtime --timestamp --sign "${{MACOS_APP_SIGN_IDENTITY}}" "$APP"
fi
mkdir -p "${{BUILD_DIR}}/pkg-root/Applications" "${{DIST_DIR}}"
ditto --noextattr --noqtn "$APP" "${{BUILD_DIR}}/pkg-root/Applications/Sensorius.app"
xattr -cr "${{BUILD_DIR}}/pkg-root/Applications/Sensorius.app"
pkgbuild --root "${{BUILD_DIR}}/pkg-root" --scripts "${{PROJECT_DIR}}/scripts" \
  --identifier com.peacehillstudios.sensorius \
  --version "{package_version}" "${{BUILD_DIR}}/Sensorius-component.pkg"
if [[ -n "${{MACOS_INSTALLER_SIGN_IDENTITY:-}}" ]]; then
  productbuild --package "${{BUILD_DIR}}/Sensorius-component.pkg" \
    --sign "${{MACOS_INSTALLER_SIGN_IDENTITY}}" "${{DIST_DIR}}/Sensorius-{version}-{arch}.pkg"
else
  productbuild --package "${{BUILD_DIR}}/Sensorius-component.pkg" \
    "${{DIST_DIR}}/Sensorius-{version}-{arch}.pkg"
fi
echo "Built ${{DIST_DIR}}/Sensorius-{version}-{arch}.pkg"
''',
        executable=True,
    )


def generate_windows(release_root: Path, target: str, version: str) -> None:
    """Generate a PyInstaller and Inno Setup project for Windows."""
    target_root = release_root / target
    write_text(target_root / "runtime_hook.py", runtime_hook_text())
    write_text(target_root / "Sensorius.spec", pyinstaller_spec(version, target))
    write_text(
        target_root / "build.ps1",
        rf'''$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseDir = Split-Path -Parent $ProjectDir
$BuildDir = Join-Path $ProjectDir 'build'
$DistDir = Join-Path $ProjectDir 'dist'
$VenvDir = Join-Path $ProjectDir '.build-venv'
$Python = if ($env:PYTHON_BIN) {{
    $env:PYTHON_BIN
}} else {{
    $resolved = & py -3.13 -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -ne 0 -or -not $resolved) {{
        throw 'Python 3.13 is required; set PYTHON_BIN to python.exe.'
    }}
    $resolved.Trim()
}}

Remove-Item $BuildDir, $DistDir -Recurse -Force -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {{
    & $Python -m venv $VenvDir
}}
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip pyinstaller
& $VenvPython -m pip install -r (Join-Path $ReleaseDir 'requirements\windows.txt')
& (Join-Path $VenvDir 'Scripts\pyinstaller.exe') --noconfirm --clean `
  --workpath (Join-Path $BuildDir 'pyinstaller') --distpath (Join-Path $BuildDir 'dist') `
  (Join-Path $ProjectDir 'Sensorius.spec')

$Iscc = if ($env:ISCC_EXE) {{ $env:ISCC_EXE }} else {{ Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe' }}
if (-not (Test-Path $Iscc)) {{ throw 'Inno Setup 6 compiler not found; set ISCC_EXE.' }}
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
& $Iscc (Join-Path $ProjectDir 'Sensorius.iss')
Write-Host "Built installer under $DistDir"
''',
    )
    write_text(
        target_root / "Sensorius.iss",
        f'''#define MyAppName "Sensorius"
#define MyAppVersion "{version}"
#define MyAppPublisher "Peace Hill Studios"
#define MyAppExeName "Sensorius.exe"

[Setup]
AppId={{{{9A98E149-4082-4A75-927C-E5A611EAEF4C}}}}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppPublisher={{#MyAppPublisher}}
DefaultDirName={{autopf}}\\Sensorius
DefaultGroupName=Sensorius
OutputDir=dist
OutputBaseFilename=Sensorius-{version}-x64-setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupLogging=yes
UninstallDisplayIcon={{app}}\\{{#MyAppExeName}}
LicenseFile=..\\payload\\LICENSE

[Files]
Source: "build\\dist\\Sensorius\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{group}}\\Sensorius"; Filename: "{{app}}\\{{#MyAppExeName}}"
Name: "{{autodesktop}}\\Sensorius"; Filename: "{{app}}\\{{#MyAppExeName}}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
Name: "autostart"; Description: "Start Sensorius when I sign in"; GroupDescription: "Startup:"

[Registry]
Root: HKA; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"; ValueType: string; ValueName: "Sensorius"; ValueData: """{{app}}\\{{#MyAppExeName}}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{{app}}\\{{#MyAppExeName}}"; Description: "Launch Sensorius"; Flags: nowait postinstall skipifsilent

[Code]
procedure AppendSupportLog(Message: String);
var
  LogDir: String;
  LogPath: String;
  Timestamp: String;
begin
  LogDir := ExpandConstant('{{localappdata}}\\Sensorius\\Logs');
  ForceDirectories(LogDir);
  LogPath := LogDir + '\\install.log';
  Timestamp := GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':');
  SaveStringToFile(LogPath, Timestamp + ' ' + Message + #13#10, True);
end;

function YesNo(Value: Boolean): String;
begin
  if Value then
    Result := 'yes'
  else
    Result := 'no';
end;

function InitializeSetup(): Boolean;
begin
  AppendSupportLog('install started version={{#MyAppVersion}}');
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    AppendSupportLog('application installed at ' + ExpandConstant('{{app}}'));
    AppendSupportLog('desktop shortcut=' + YesNo(WizardIsTaskSelected('desktopicon')));
    AppendSupportLog('autostart=' + YesNo(WizardIsTaskSelected('autostart')));
    AppendSupportLog('install finished status=0');
  end;
end;
''',
    )


def file_manifest(root: Path) -> list[GeneratedFile]:
    """Return hashes for every staged payload file."""
    result: list[GeneratedFile] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append(GeneratedFile(str(path.relative_to(root)), path.stat().st_size, digest))
    return result


def generate(output: Path, targets: list[str], version: str, replace: bool) -> Path:
    """Generate a complete versioned release workspace."""
    version = safe_version(version)
    unknown_targets = sorted(set(targets) - set(TARGETS))
    if unknown_targets:
        raise ValueError(f"Unsupported target(s): {', '.join(unknown_targets)}")
    if not targets:
        raise ValueError("At least one installer target is required")
    if len(targets) != len(set(targets)):
        raise ValueError("Installer targets must not be repeated")

    release_root = output.resolve() / version
    if release_root.exists():
        if not replace:
            raise FileExistsError(f"Output already exists: {release_root}; pass --replace to replace it")
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True)

    payload = release_root / "payload"
    copy_payload(payload)
    copy_requirements(release_root)

    for target in targets:
        if target.startswith(("linux-", "rpi-")):
            generate_linux(release_root, target, version)
        elif target.startswith("macos-"):
            generate_macos(release_root, target, version)
        elif target == "windows-x64":
            generate_windows(release_root, target, version)

    manifest = {
        "schema": 1,
        "name": "sensorius",
        "version": version,
        "targets": targets,
        "payload": [item.__dict__ for item in file_manifest(payload)],
    }
    write_text(release_root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    write_text(
        release_root / "SUPPORT.md",
        '''# Installer support logs

- Linux and Raspberry Pi: `/var/log/sensorius/install.log`; package manager
  history is also available in `/var/log/dpkg.log` and the system journal.
- macOS: `/Library/Logs/Sensorius/install.log`; the native Installer transcript
  is available in `/var/log/install.log`.
- Windows: `%LOCALAPPDATA%\\Sensorius\\Logs\\install.log`; Inno Setup also
  creates a detailed, timestamped Setup Log file in `%TEMP%`.

The Sensorius logs record version, platform, target paths, service setup, and
exit status. They intentionally do not record `.env`, credentials, API keys,
MQTT payloads, or Wi-Fi passwords.
''',
    )
    return release_root


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse installer generator command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate", help="generate installer build projects")
    generate_parser.add_argument("--target", action="append", choices=TARGETS, dest="targets")
    generate_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    generate_parser.add_argument("--version", default=canonical_version())
    generate_parser.add_argument("--replace", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the installer generator command-line interface."""
    args = parse_args(argv or sys.argv[1:])
    if args.command == "generate":
        targets = args.targets or list(TARGETS)
        result = generate(args.output, targets, safe_version(args.version), args.replace)
        print(result)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
