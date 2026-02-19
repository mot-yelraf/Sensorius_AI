$ErrorActionPreference = 'Stop'

$PY_VERSION = if ($env:PY_VERSION) { $env:PY_VERSION } else { '3.13.5' }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceRepoDir = Split-Path -Parent $ScriptDir
$ProjectDir = if ($env:PROJECT_DIR) { $env:PROJECT_DIR } else { Join-Path $HOME 'Sensorius' }
$VenvPath = if ($env:VENV_PATH) { $env:VENV_PATH } else { Join-Path $ProjectDir '.venv' }
$ReqFile = if ($env:REQ_FILE) { $env:REQ_FILE } else { Join-Path $ScriptDir 'setup_reqs_win.txt' }
$InstallPywebview = if ($env:INSTALL_PYWEBVIEW) { $env:INSTALL_PYWEBVIEW } else { '1' }
$PipOnlyBinary = if ($env:PIP_ONLY_BINARY) { $env:PIP_ONLY_BINARY } else { '1' }
$BrokerScope = if ($env:BROKER_SCOPE) { ($env:BROKER_SCOPE).ToLowerInvariant() } else { '' }

$CreatedVenv = $false

function Cleanup {
    if ($CreatedVenv -and (Test-Path $VenvPath)) {
        Write-Host "Cleaning up virtual environment at $VenvPath..."
        Remove-Item -Recurse -Force $VenvPath
    }
}

function Deploy-ProjectFiles {
    if ($SourceRepoDir -eq $ProjectDir) {
        Write-Host "Source and target are the same ($ProjectDir); skipping file sync."
        return
    }

    if (-not (Test-Path $ProjectDir)) {
        New-Item -ItemType Directory -Path $ProjectDir | Out-Null
    }

    $args = @(
        $SourceRepoDir, $ProjectDir, '/MIR', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
        '/XD', '.git', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'deploy_scripts',
        '/XF', '*.pyc', '*.pyo', 'sensor_data.db', '*.log'
    )

    & robocopy @args | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -gt 7) {
        throw "robocopy failed with exit code $rc"
    }

    Write-Host "Application files deployed to $ProjectDir"
}

function Ensure-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Please re-run this script in an elevated PowerShell (Run as Administrator).'
        Cleanup
        exit 1
    }
}

function Resolve-BrokerScope {
    if ($BrokerScope -in @('user', 'system')) {
        return
    }
    $answer = Read-Host 'Mosquitto scope [system/user] (default: system)'
    $choice = if ($null -eq $answer) { '' } else { $answer.Trim().ToLowerInvariant() }
    if ([string]::IsNullOrWhiteSpace($choice)) {
        $choice = 'system'
    }
    if ($choice -notin @('user', 'system')) {
        Write-Host "Invalid scope '$choice'. Defaulting to system."
        $choice = 'system'
    }
    $script:BrokerScope = $choice
}

function Ensure-AdminIfNeeded {
    if ($BrokerScope -eq 'system') {
        Ensure-Admin
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
        [Parameter(Mandatory = $true)][string]$PackageId,
        [switch]$ScopeMachine
    )

    $installArgs = @(
        'install', '--id', $PackageId, '-e',
        '--accept-package-agreements', '--accept-source-agreements'
    )
    if ($ScopeMachine) {
        $installArgs += @('--scope', 'machine')
    }

    & winget @installArgs
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    Write-Host "winget install for $PackageId returned code $LASTEXITCODE; trying upgrade..."
    & winget upgrade --id $PackageId -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Continuing after winget non-zero code for $PackageId ($LASTEXITCODE)."
        return $false
    }
    return $true
}

function Get-MosquittoService {
    $svc = Get-Service -Name 'mosquitto' -ErrorAction SilentlyContinue
    if ($svc) {
        return $svc
    }

    try {
        $candidates = Get-CimInstance Win32_Service | Where-Object {
            $_.Name -match 'mosquitto' -or $_.DisplayName -match 'mosquitto' -or $_.PathName -match 'mosquitto'
        }
        if ($candidates) {
            return Get-Service -Name $candidates[0].Name -ErrorAction SilentlyContinue
        }
    } catch {}
    return $null
}

function Resolve-MosquittoRoot {
    param([Parameter(Mandatory = $false)]$ServiceObj)

    if ($ServiceObj) {
        try {
            $wmi = Get-CimInstance Win32_Service -Filter "Name='$($ServiceObj.Name)'"
            if ($wmi -and $wmi.PathName) {
                $p = $wmi.PathName.Trim()
                if ($p.StartsWith('"')) {
                    $exe = ($p -split '"')[1]
                } else {
                    $exe = ($p -split '\s+')[0]
                }
                if ($exe -and (Test-Path $exe)) {
                    return Split-Path -Parent $exe
                }
            }
        } catch {}
    }

    $fallbacks = @(
        (Join-Path $env:ProgramFiles 'mosquitto'),
        (Join-Path ${env:ProgramFiles(x86)} 'mosquitto'),
        (Join-Path $env:LocalAppData 'Programs\mosquitto')
    )
    foreach ($path in $fallbacks) {
        if ($path -and (Test-Path $path)) {
            return $path
        }
    }
    return $null
}

function Ensure-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'Installing uv via winget...'
        if ($BrokerScope -eq 'system') {
            Install-WingetPackage -PackageId 'Astral.uv' -ScopeMachine
        } else {
            Install-WingetPackage -PackageId 'Astral.uv'
        }
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv was not found after installation attempt.'
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

function Test-RuntimeImports {
    $venvPython = Join-Path $VenvPath 'Scripts\python.exe'
    & $venvPython -c "import fastapi; import requests; import paho.mqtt.client as mqtt; from zoneinfo import ZoneInfo; ZoneInfo('America/Denver'); print('Python dependency check passed')"
}

function Ensure-WebView2Runtime {
    if ($InstallPywebview -eq '0') {
        return
    }

    Write-Host 'Ensuring Microsoft Edge WebView2 Runtime is installed...'
    Install-WingetPackage -PackageId 'Microsoft.EdgeWebView2Runtime'
}

function Install-Mosquitto {
    if ($BrokerScope -eq 'user') {
        Write-Host 'Ensuring Mosquitto is installed via winget (user scope)...'
        $installed = Install-WingetPackage -PackageId 'Eclipse.Mosquitto'
        if (-not $installed) {
            throw 'Mosquitto winget install/upgrade did not report success.'
        }

        $mosqRoot = Resolve-MosquittoRoot -ServiceObj $null
        if (-not $mosqRoot) {
            throw 'Mosquitto appears installed, but install path could not be resolved.'
        }
        $mosqExe = Join-Path $mosqRoot 'mosquitto.exe'
        if (-not (Test-Path $mosqExe)) {
            throw "Mosquitto executable not found at $mosqExe"
        }

        $userRoot = Join-Path $env:LOCALAPPDATA 'Sensorius\mosquitto'
        $userRun = Join-Path $userRoot 'run'
        $userLib = Join-Path $userRoot 'lib'
        $userLog = Join-Path $userRoot 'log'
        $userConf = Join-Path $userRoot 'mosquitto.conf'
        New-Item -ItemType Directory -Force -Path $userRun, $userLib, $userLog | Out-Null

        @"
pid_file $userRun\mosquitto.pid
persistence true
persistence_location $userLib\
log_dest file $userLog\mosquitto.log
listener 1883
allow_anonymous true
"@ | Set-Content $userConf

        $taskName = 'SensoriusMosquittoUser'
        $taskCmd = "`"$mosqExe`" -c `"$userConf`""
        & schtasks /Create /TN $taskName /SC ONLOGON /TR $taskCmd /F | Out-Null
        Start-Process -FilePath $mosqExe -ArgumentList @('-c', $userConf) -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
        Write-Host "Mosquitto configured for user startup task '$taskName'."
        return
    }

    Write-Host 'Ensuring Mosquitto is installed via winget (machine scope)...'
    $installed = Install-WingetPackage -PackageId 'Eclipse.Mosquitto' -ScopeMachine
    if (-not $installed) {
        throw 'Mosquitto winget install/upgrade did not report success.'
    }

    $mosqService = Get-MosquittoService
    $mosqRoot = Resolve-MosquittoRoot -ServiceObj $mosqService

    if (-not $mosqRoot) {
        throw 'Mosquitto appears installed, but install path could not be resolved.'
    }

    $mosqConf = Join-Path $mosqRoot 'mosquitto.conf'
    $mosqConfDir = Join-Path $mosqRoot 'conf.d'

    if (-not (Test-Path $mosqConfDir)) {
        New-Item -ItemType Directory -Path $mosqConfDir | Out-Null
    }

    if (-not (Test-Path $mosqConf)) {
        throw "Mosquitto config file not found at $mosqConf"
    }

    $confText = Get-Content $mosqConf -Raw
    if ($confText -notmatch '^include_dir .*conf\.d' -and $confText -notmatch '^include_dir .*conf.d') {
        Add-Content $mosqConf "`ninclude_dir $mosqConfDir"
    }

    @"
listener 1883
allow_anonymous true
"@ | Set-Content (Join-Path $mosqConfDir 'anon.conf')

    $mosqService = Get-MosquittoService
    if (-not $mosqService) {
        throw "Mosquitto service was not found after installation. Root path: $mosqRoot"
    }

    try {
        Set-Service -Name $mosqService.Name -StartupType Automatic -ErrorAction SilentlyContinue
    } catch {}

    Stop-Service -Name $mosqService.Name -ErrorAction SilentlyContinue
    Start-Service -Name $mosqService.Name -ErrorAction Stop
    $mosqService = Get-Service -Name $mosqService.Name
    if ($mosqService.Status -ne 'Running') {
        throw "Mosquitto service '$($mosqService.Name)' failed to start."
    }

    Write-Host "Mosquitto installed and running as service '$($mosqService.Name)'."
}

function Configure-BootStartup {
    $answer = Read-Host 'Start Sensorius automatically at system boot? [y/N]'
    if ($answer -notmatch '^[Yy]$') {
        return
    }

    $taskName = if ($BrokerScope -eq 'system') { 'SensoriusStartup' } else { 'SensoriusStartupUser' }
    $venvPython = Join-Path $VenvPath 'Scripts\python.exe'
    $scriptPath = Join-Path $ProjectDir 'Sensorius.py'
    if ($BrokerScope -eq 'system') {
        $action = New-ScheduledTaskAction -Execute $venvPython -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectDir
        $trigger = New-ScheduledTaskTrigger -AtStartup
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    } else {
        $taskCmd = "`"$venvPython`" `"$scriptPath`""
        & schtasks /Create /TN $taskName /SC ONLOGON /TR $taskCmd /F | Out-Null
    }
    Write-Host "Configured startup task '$taskName' ($BrokerScope scope)."
}

try {
    Deploy-ProjectFiles
    Resolve-BrokerScope
    Ensure-AdminIfNeeded
    Ensure-Winget
    Ensure-Uv
    Install-Python
    Install-Requirements
    Test-RuntimeImports
    Ensure-WebView2Runtime
    Install-Mosquitto
    Configure-BootStartup

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
