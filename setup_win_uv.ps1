$ErrorActionPreference = 'Stop'

$PY_VERSION = if ($env:PY_VERSION) { $env:PY_VERSION } else { '3.13.5' }
$ProjectDir = if ($env:PROJECT_DIR) { $env:PROJECT_DIR } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$VenvPath = if ($env:VENV_PATH) { $env:VENV_PATH } else { Join-Path $ProjectDir '.venv' }
$ReqFile = if ($env:REQ_FILE) { $env:REQ_FILE } else { Join-Path $ProjectDir 'setup_reqs_win.txt' }
$InstallPywebview = if ($env:INSTALL_PYWEBVIEW) { $env:INSTALL_PYWEBVIEW } else { '1' }
$PipOnlyBinary = if ($env:PIP_ONLY_BINARY) { $env:PIP_ONLY_BINARY } else { '1' }

$CreatedVenv = $false

function Cleanup {
    if ($CreatedVenv -and (Test-Path $VenvPath)) {
        Write-Host "Cleaning up virtual environment at $VenvPath..."
        Remove-Item -Recurse -Force $VenvPath
    }
}

function Ensure-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Please re-run this script in an elevated PowerShell (Run as Administrator).'
        Cleanup
        exit 1
    }
}

function Ensure-Winget {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        return
    }

    Write-Host 'winget not found. It is required to install dependencies.'
    Write-Host 'On Windows 10/11, winget is provided by the Microsoft Store "App Installer" package.'
    $answer = Read-Host 'Install winget now? [y/N]'
    if ($answer -notmatch '^[Yy]$') {
        Write-Host 'winget install declined. Aborting.'
        Cleanup
        exit 1
    }

    Write-Host 'Please install "App Installer" from the Microsoft Store, then re-run this script.'
    Cleanup
    exit 1
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$PackageId
    )

    & winget install --id $PackageId -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "winget install for $PackageId returned code $LASTEXITCODE; trying upgrade..."
    & winget upgrade --id $PackageId -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Continuing after winget non-zero code for $PackageId ($LASTEXITCODE)."
    }
}

function Ensure-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'Installing uv via winget...'
        Install-WingetPackage -PackageId 'Astral.uv'
    }
}

function Install-Python {
    uv python install $PY_VERSION
}

function Install-Requirements {
    if (-not (Test-Path $ReqFile)) {
        Write-Host "ERROR: requirements file not found at $ReqFile"
        Cleanup
        exit 1
    }

    uv venv $VenvPath --python $PY_VERSION
    $CreatedVenv = $true

    $venvPython = Join-Path $VenvPath 'Scripts\python.exe'

    if ($InstallPywebview -eq '0') {
        Write-Host 'INSTALL_PYWEBVIEW=0 set — installing without pywebview.'
        $tmpReqs = New-TemporaryFile
        Get-Content $ReqFile | Where-Object { $_ -notmatch '^pywebview==' } | Set-Content $tmpReqs
        if ($PipOnlyBinary -eq '1') {
            Write-Host 'PIP_ONLY_BINARY=1 set — requiring wheel/binary packages only.'
            uv pip install --only-binary=:all: -r $tmpReqs --python $venvPython
        } else {
            uv pip install -r $tmpReqs --python $venvPython
        }
        Remove-Item $tmpReqs
    } else {
        if ($PipOnlyBinary -eq '1') {
            Write-Host 'PIP_ONLY_BINARY=1 set — requiring wheel/binary packages only.'
            uv pip install --only-binary=:all: -r $ReqFile --python $venvPython
        } else {
            uv pip install -r $ReqFile --python $venvPython
        }
    }
}

function Ensure-WebView2Runtime {
    if ($InstallPywebview -eq '0') {
        return
    }

    Write-Host 'Ensuring Microsoft Edge WebView2 Runtime is installed...'
    Install-WingetPackage -PackageId 'Microsoft.EdgeWebView2Runtime'
}

function Install-Mosquitto {
    Install-WingetPackage -PackageId 'Eclipse.Mosquitto'

    $mosqRoot = Join-Path $env:ProgramFiles 'mosquitto'
    $mosqConf = Join-Path $mosqRoot 'mosquitto.conf'
    $mosqConfDir = Join-Path $mosqRoot 'conf.d'

    if (-not (Test-Path $mosqConfDir)) {
        New-Item -ItemType Directory -Path $mosqConfDir | Out-Null
    }

    if (Test-Path $mosqConf) {
        $confText = Get-Content $mosqConf -Raw
        if ($confText -notmatch '^include_dir .*conf\.d' -and $confText -notmatch '^include_dir .*conf.d') {
            Add-Content $mosqConf "`ninclude_dir $mosqConfDir"
        }
    }

    @"
listener 1883
allow_anonymous true
"@ | Set-Content (Join-Path $mosqConfDir 'anon.conf')

    Stop-Service mosquitto -ErrorAction SilentlyContinue
    Start-Service mosquitto
}

try {
    Ensure-Admin
    Ensure-Winget
    Ensure-Uv
    Install-Python
    Install-Requirements
    Ensure-WebView2Runtime
    Install-Mosquitto

    Write-Host ''
    Write-Host 'Setup complete.'
    Write-Host "Activate your environment: $VenvPath\\Scripts\\Activate.ps1"
    Write-Host "Start Sensorius: python $ProjectDir\\Sensorius.py"
    Write-Host 'Web UI: open http://127.0.0.1:8000 (or http://<host-ip>:8000 from another device)'
} catch {
    Write-Host "Setup failed: $($_.Exception.Message)"
    Cleanup
    exit 1
}
