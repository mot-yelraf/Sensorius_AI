$ErrorActionPreference = 'Stop'

$ProjectDir = if ($env:PROJECT_DIR) { $env:PROJECT_DIR } else { Join-Path $HOME 'Sensorius' }
$VenvPath = if ($env:VENV_PATH) { $env:VENV_PATH } else { Join-Path $ProjectDir '.venv' }
$TaskName = if ($env:SENSORIUS_TASK_NAME) { $env:SENSORIUS_TASK_NAME } else { 'SensoriusStartup' }

function Ask-YesNo {
    param([string]$Prompt)
    $answer = Read-Host "$Prompt [y/N]"
    return ($answer -match '^[Yy]$')
}

Write-Host "Sensorius uninstall (Windows)."
Write-Host "Project: $ProjectDir"
Write-Host "Venv: $VenvPath"
Write-Host ""

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        if (Ask-YesNo "Remove scheduled task '$TaskName'?") {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Removed scheduled task '$TaskName'."
        }
    }
} catch {
    Write-Host "Warning: failed checking/removing scheduled task: $($_.Exception.Message)"
}

if (Test-Path $VenvPath) {
    if (Ask-YesNo "Remove virtual environment at '$VenvPath'?") {
        Remove-Item -Recurse -Force $VenvPath
        Write-Host "Removed $VenvPath."
    }
}

if (Ask-YesNo "Stop mosquitto service and remove anonymous config?") {
    try {
        Stop-Service mosquitto -ErrorAction SilentlyContinue
    } catch {}

    $mosqRoot = Join-Path $env:ProgramFiles 'mosquitto'
    $anonConf = Join-Path $mosqRoot 'conf.d\anon.conf'
    if (Test-Path $anonConf) {
        Remove-Item -Force $anonConf
        Write-Host "Removed $anonConf."
    }
}

if (Ask-YesNo "Uninstall Mosquitto package via winget?") {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget uninstall --id Eclipse.Mosquitto -e --accept-source-agreements
    } else {
        Write-Host "winget not found; skip package uninstall."
    }
}

Write-Host "Uninstall script completed."
