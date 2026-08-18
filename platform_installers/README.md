# Sensorius platform installer generator

This project generates native installer build trees for Sensorius. The
generator, tests, and documentation are version-controlled; staged payloads,
build environments, and binary artifacts remain ignored.

The generator produces build inputs rather than cross-compiling binaries.
Build each target on its native operating system and CPU architecture:

- Debian/Ubuntu Linux: `amd64`
- Raspberry Pi OS: `arm64` or `armhf`
- macOS: `arm64` or `x86_64`
- Windows: `x64`

The resulting native packages are:

- Debian/Ubuntu and Raspberry Pi OS: `.deb`
- macOS: `.pkg` containing `Sensorius.app`
- Windows: Inno Setup `.exe`

## Generate

From the Sensorius repository root:

```bash
python platform_installers/installer_generator.py generate
```

The output is placed under `platform_installers/generated/<version>/` and
contains one shared payload plus target-specific build projects.

Generate selected targets or override the output directory:

```bash
python platform_installers/installer_generator.py generate \
  --target linux-amd64 \
  --target rpi-arm64 \
  --output /absolute/build/workspace
```

Use `--replace` to replace an existing generated version directory. The
generator refuses to overwrite existing output otherwise.

## Build

Linux or Raspberry Pi, on the target architecture:

```bash
cd platform_installers/generated/<version>/linux-amd64
./build.sh
```

Raspberry Pi builds default to Bookworm. Select Trixie explicitly:

```bash
RPI_RELEASE=trixie ./build.sh
```

macOS, on the target architecture:

```bash
cd platform_installers/generated/<version>/macos-arm64
./build.sh
```

The macOS and Windows frozen builds require Python 3.13. Set `PYTHON_BIN` when
it is not available as `python3.13` on macOS or through `py -3.13` on Windows.

Optional signing variables:

```bash
export MACOS_APP_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export MACOS_INSTALLER_SIGN_IDENTITY='Developer ID Installer: Example (TEAMID)'
./build.sh
```

Windows, in PowerShell with Python and Inno Setup installed:

```powershell
cd platform_installers\generated\<version>\windows-x64
.\build.ps1
```

Set `$env:WINDOWS_SIGNTOOL_ARGS` when Authenticode signing is configured.

## Installation logs

Each generated installer writes a small, append-only Sensorius support log:

- Linux and Raspberry Pi: `/var/log/sensorius/install.log`
- macOS: `/Library/Logs/Sensorius/install.log`
- Windows: `%LOCALAPPDATA%\Sensorius\Logs\install.log`

The operating-system installer keeps a more detailed native transcript in
`/var/log/dpkg.log` on Debian-family systems, `/var/log/install.log` on macOS,
and a timestamped Inno Setup log under `%TEMP%` on Windows. The generated
release also contains `SUPPORT.md` with these locations. Sensorius support logs
exclude environment-file contents, credentials, API keys, MQTT payloads, and
Wi-Fi passwords.

## Packaging model

- The application payload is immutable inside the installed application.
- Runtime settings and the SQLite database remain under the user's existing
  `Sensorius` runtime directory.
- Linux packages install a system service with its writable runtime under
  `/var/lib/sensorius/Sensorius`.
- macOS and Windows frozen builds use a PyInstaller runtime hook to set the
  resource root and enter a writable per-user runtime directory before the
  application imports.
- Mosquitto is treated as an external prerequisite. Generated installers do
  not overwrite or disable an existing broker configuration.

Unsigned output is suitable for development and internal testing only. Public
macOS and Windows releases should be signed; macOS releases should also be
notarized and stapled.
