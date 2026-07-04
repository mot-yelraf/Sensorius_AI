$ErrorActionPreference = 'Stop'

$PY_VERSION = if ($env:PY_VERSION) { $env:PY_VERSION } else { '3.13.5' }
$PY_MM = if ($PY_VERSION -match '^(\d+\.\d+)') { $Matches[1] } else { '3.13' }
$PY_WINGET_ID = if ($env:PY_WINGET_ID) { $env:PY_WINGET_ID } else { 'Python.Python.3.13' }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceRepoDir = Split-Path -Parent $ScriptDir
$ProjectDir = if ($env:PROJECT_DIR) { $env:PROJECT_DIR } else { Join-Path $HOME 'Sensorius' }
$VenvPath = if ($env:VENV_PATH) { $env:VENV_PATH } else { Join-Path $ProjectDir '.venv' }
$ReqFile = if ($env:REQ_FILE) { $env:REQ_FILE } else { Join-Path $ScriptDir 'setup_reqs_win.txt' }
$InstallPywebview = if ($env:INSTALL_PYWEBVIEW) { $env:INSTALL_PYWEBVIEW } else { '1' }
$PipOnlyBinary = if ($env:PIP_ONLY_BINARY) { $env:PIP_ONLY_BINARY } else { '1' }
$BrokerScope = if ($env:BROKER_SCOPE) { ($env:BROKER_SCOPE).ToLowerInvariant() } else { '' }

$CreatedVenv = $false
$InstallLog = if ($env:SENSORIUS_INSTALL_LOG) { $env:SENSORIUS_INSTALL_LOG } else { Join-Path $ProjectDir 'install.log' }
$TranscriptStarted = $false

function Start-InstallLog {
    $logDir = Split-Path -Parent $InstallLog
    if ([string]::IsNullOrWhiteSpace($logDir)) {
        $logDir = (Get-Location).Path
    }
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }

    try {
        Start-Transcript -Path $InstallLog -Append -ErrorAction Stop | Out-Null
        $script:TranscriptStarted = $true
        Write-Host "Logging install output to $InstallLog"
    } catch {
        Write-Host "WARNING: unable to create install log at $InstallLog; continuing without transcript."
    }
}

function Stop-InstallLog {
    if ($TranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
        } catch {}
    }
}

function Format-BytesForLog {
    param($Bytes)
    if ($null -eq $Bytes -or $Bytes -eq '') {
        return 'unknown'
    }
    return ('{0:N1} GiB ({1} bytes)' -f ([double]$Bytes / 1GB), $Bytes)
}

function Write-OptionalSetting {
    param(
        [string]$Name,
        [string]$Value
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        Write-Host "${Name}: $Value"
    }
}

function Write-ToolVersion {
    param(
        [string]$Label,
        [string]$Command,
        [string[]]$Arguments = @()
    )
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        Write-Host "${Label}: not found"
        return
    }

    try {
        $output = & $Command @Arguments 2>&1 | Select-Object -First 1
        if ($output) {
            Write-Host "${Label}: $output"
        } else {
            Write-Host "${Label}: available"
        }
    } catch {
        Write-Host "${Label}: error: $($_.Exception.Message)"
    }
}

function Write-InstallHostConfig {
    Write-Host '--- Host system ---'
    Write-Host "Timestamp: $(Get-Date -Format o)"
    Write-Host "Computer name: $env:COMPUTERNAME"
    Write-Host "User: $env:USERDOMAIN\$env:USERNAME"
    Write-Host "Working directory: $((Get-Location).Path)"
    Write-Host "Command line: $([Environment]::CommandLine)"
    Write-Host "PowerShell: $($PSVersionTable.PSVersion)"

    try {
        $os = Get-CimInstance Win32_OperatingSystem
        Write-Host "OS: $($os.Caption) $($os.Version) build $($os.BuildNumber)"
        Write-Host "Memory visible to OS: $(Format-BytesForLog ([int64]$os.TotalVisibleMemorySize * 1KB))"
        Write-Host "Memory free: $(Format-BytesForLog ([int64]$os.FreePhysicalMemory * 1KB))"
    } catch {
        Write-Host "OS: unavailable ($($_.Exception.Message))"
    }

    try {
        $computer = Get-CimInstance Win32_ComputerSystem
        Write-Host "Hardware model: $($computer.Manufacturer) $($computer.Model)"
        Write-Host "System type: $($computer.SystemType)"
        Write-Host "Physical memory: $(Format-BytesForLog $computer.TotalPhysicalMemory)"
    } catch {
        Write-Host "Hardware model: unavailable ($($_.Exception.Message))"
    }

    try {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        Write-Host "CPU: $($cpu.Name)"
        Write-Host "CPU cores/logical processors: $($cpu.NumberOfCores)/$($cpu.NumberOfLogicalProcessors)"
    } catch {
        Write-Host "CPU: unavailable ($($_.Exception.Message))"
    }

    Write-Host 'Disk space:'
    $roots = @()
    foreach ($path in @($ProjectDir, $HOME, $env:SystemDrive)) {
        if ([string]::IsNullOrWhiteSpace($path)) {
            continue
        }
        $root = $null
        try {
            $resolved = Resolve-Path -LiteralPath $path -ErrorAction SilentlyContinue
            if ($resolved) {
                $root = [System.IO.Path]::GetPathRoot($resolved.Path)
            }
        } catch {}
        if (-not $root) {
            $root = [System.IO.Path]::GetPathRoot($path)
        }
        if ($root -and $roots -notcontains $root) {
            $roots += $root
        }
    }
    foreach ($root in $roots) {
        $driveName = $root.TrimEnd('\').TrimEnd(':')
        $drive = Get-PSDrive -Name $driveName -PSProvider FileSystem -ErrorAction SilentlyContinue
        if ($drive) {
            Write-Host ('  {0}: used={1:N1} GiB free={2:N1} GiB root={3}' -f $drive.Name, ($drive.Used / 1GB), ($drive.Free / 1GB), $drive.Root)
        }
    }

    Write-Host '--- Installer context ---'
    Write-Host "Source repo: $SourceRepoDir"
    Write-Host "Project dir: $ProjectDir"
    Write-Host "Script dir: $ScriptDir"
    Write-Host "Venv path: $VenvPath"
    Write-Host "Requirements file: $ReqFile"
    Write-OptionalSetting 'PY_VERSION' $PY_VERSION
    Write-OptionalSetting 'PY_MM' $PY_MM
    Write-OptionalSetting 'PY_WINGET_ID' $PY_WINGET_ID
    Write-OptionalSetting 'BROKER_SCOPE' $BrokerScope
    Write-OptionalSetting 'INSTALL_PYWEBVIEW' $InstallPywebview
    Write-OptionalSetting 'PIP_ONLY_BINARY' $PipOnlyBinary

    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $SourceRepoDir '.git'))) {
        $branch = (& git -C $SourceRepoDir rev-parse --abbrev-ref HEAD 2>$null)
        $revision = (& git -C $SourceRepoDir rev-parse --short HEAD 2>$null)
        Write-Host "Git branch: $branch"
        Write-Host "Git revision: $revision"
        & git -C $SourceRepoDir diff --quiet --ignore-submodules -- 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'Git worktree: clean'
        } elseif ($LASTEXITCODE -eq 1) {
            Write-Host 'Git worktree: modified'
        } else {
            Write-Host 'Git worktree: unknown'
        }
    }

    Write-Host '--- Tool versions ---'
    Write-ToolVersion 'winget' 'winget' @('--version')
    Write-ToolVersion 'py' 'py' @('--version')
    Write-ToolVersion 'python' 'python' @('--version')
    Write-ToolVersion 'pip' 'pip' @('--version')
    Write-ToolVersion 'uv' 'uv' @('--version')
    Write-ToolVersion 'git' 'git' @('--version')
    Write-ToolVersion 'mosquitto' 'mosquitto' @('-h')
}

function Cleanup {
    if ($CreatedVenv -and (Test-Path $VenvPath)) {
        Write-Host "Cleaning up virtual environment at $VenvPath..."
        Remove-Item -Recurse -Force $VenvPath
    }
}

function Invoke-NativeChecked {
    param(
        [scriptblock]$Command,
        [string]$FailureMessage
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit $LASTEXITCODE)"
    }
}

function Invoke-NativeAllowFailure {
    param([scriptblock]$Command)
    & $Command
    return $LASTEXITCODE
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
        [switch]$Silent,
        [switch]$ScopeMachine
    )

    $installArgs = @(
        'install', '--id', $PackageId, '-e',
        '--accept-package-agreements', '--accept-source-agreements'
    )
    if ($Silent) {
        $installArgs += '--silent'
    }
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

function Install-Python {
    Write-Host "Ensuring Python $PY_MM is installed via winget package ($PY_WINGET_ID)..."
    if ($BrokerScope -eq 'system') {
        Install-WingetPackage -PackageId $PY_WINGET_ID -Silent -ScopeMachine
    } else {
        Install-WingetPackage -PackageId $PY_WINGET_ID -Silent
    }

    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        Write-Host 'Python launcher (py.exe) not found after install. Open a new elevated PowerShell and re-run.'
        Cleanup
        exit 1
    }

    try {
        & py "-$PY_MM" -c "import sys; print(sys.version)"
    } catch {
        Write-Host "Python $PY_MM is not available via py launcher. Ensure '$PY_WINGET_ID' installed successfully."
        Cleanup
        exit 1
    }
}

function Install-Requirements {
    if (-not (Test-Path $ReqFile)) {
        Write-Host "ERROR: requirements file not found at $ReqFile"
        Cleanup
        exit 1
    }

    & py "-$PY_MM" -m venv $VenvPath
    $CreatedVenv = $true

    $activate = Join-Path $VenvPath 'Scripts\Activate.ps1'
    . $activate

    $venvPython = Join-Path $VenvPath 'Scripts\python.exe'
    Invoke-NativeChecked { & $venvPython -m pip install --upgrade pip } 'pip upgrade failed'

    if ($InstallPywebview -eq '0') {
        Write-Host 'INSTALL_PYWEBVIEW=0 set — installing without pywebview.'
        $tmpReqs = New-TemporaryFile
        Get-Content $ReqFile | Where-Object { $_ -notmatch '^pywebview==' } | Set-Content $tmpReqs
        if ($PipOnlyBinary -eq '1') {
            Write-Host 'PIP_ONLY_BINARY=1 set — trying wheel/binary packages first.'
            $rc = Invoke-NativeAllowFailure { & $venvPython -m pip install --only-binary=:all: -r $tmpReqs }
            if ($rc -ne 0) {
                Write-Host 'Binary-only install failed; retrying with source builds enabled.'
                Invoke-NativeChecked { & $venvPython -m pip install -r $tmpReqs } 'pip install requirements fallback failed'
            }
        } else {
            Invoke-NativeChecked { & $venvPython -m pip install -r $tmpReqs } 'pip install requirements failed'
        }
        Remove-Item $tmpReqs
    } else {
        if ($PipOnlyBinary -eq '1') {
            Write-Host 'PIP_ONLY_BINARY=1 set — trying wheel/binary packages first.'
            $rc = Invoke-NativeAllowFailure { & $venvPython -m pip install --only-binary=:all: -r $ReqFile }
            if ($rc -ne 0) {
                Write-Host 'Binary-only install failed; retrying with source builds enabled.'
                Invoke-NativeChecked { & $venvPython -m pip install -r $ReqFile } 'pip install requirements fallback failed'
            }
        } else {
            Invoke-NativeChecked { & $venvPython -m pip install -r $ReqFile } 'pip install requirements failed'
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
    if ($confText -notmatch '(?m)^\s*include_dir\s+.*conf[.]d\b') {
        Add-Content $mosqConf "`ninclude_dir $mosqConfDir"
    }

    $backupSuffix = "disabled-by-sensorius-{0:yyyyMMddHHmmss}" -f (Get-Date)
    Get-ChildItem -Path $mosqConfDir -Filter '*.conf' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'anon.conf' } |
        ForEach-Object {
            Write-Host "Disabling existing Mosquitto drop-in $($_.FullName)"
            Rename-Item -Path $_.FullName -NewName "$($_.Name).$backupSuffix" -Force
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

Start-InstallLog
Write-InstallHostConfig

try {
    Deploy-ProjectFiles
    Resolve-BrokerScope
    Ensure-AdminIfNeeded
    Ensure-Winget
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
} finally {
    Stop-InstallLog
}
