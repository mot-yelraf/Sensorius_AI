function Resolve-SensoriusInstallLocation {
    $defaultInstallDir = Join-Path $HOME 'Sensorius'
    $stateRoot = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Join-Path $HOME 'AppData\Local'
    } else {
        $env:LOCALAPPDATA
    }
    $stateDir = Join-Path $stateRoot 'Sensorius'
    $stateFile = Join-Path $stateDir 'install-location.txt'
    $rememberedInstallDir = $defaultInstallDir
    if (Test-Path -LiteralPath $stateFile -PathType Leaf) {
        $storedInstallDir = (Get-Content -LiteralPath $stateFile -Raw).Trim()
        if (-not [string]::IsNullOrWhiteSpace($storedInstallDir)) {
            $rememberedInstallDir = $storedInstallDir
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:SENSORIUS_INSTALL_DIR)) {
        $installDir = $env:SENSORIUS_INSTALL_DIR
    } elseif (-not [string]::IsNullOrWhiteSpace($env:PROJECT_DIR)) {
        $installDir = $env:PROJECT_DIR
    } else {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Application]::EnableVisualStyles()
        $initialParent = Split-Path -Parent $rememberedInstallDir
        if ([string]::IsNullOrWhiteSpace($initialParent) -or
            -not (Test-Path -LiteralPath $initialParent -PathType Container)) {
            $initialParent = $HOME
        }
        $locationDialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $locationDialog.Description = 'Choose where Sensorius should be installed. A Sensorius folder will be created here.'
        $locationDialog.SelectedPath = $initialParent
        $locationDialog.ShowNewFolderButton = $true
        if ($locationDialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            $locationDialog.Dispose()
            throw 'Sensorius installation was cancelled.'
        }
        $installDir = Join-Path $locationDialog.SelectedPath 'Sensorius'
        $locationDialog.Dispose()
    }

    if ([string]::IsNullOrWhiteSpace($installDir)) {
        throw 'Install location must name a dedicated Sensorius directory.'
    }
    $installDir = [System.IO.Path]::GetFullPath($installDir)
    if ($installDir -eq [System.IO.Path]::GetPathRoot($installDir) -or
        $installDir -eq [System.IO.Path]::GetFullPath($HOME)) {
        throw 'Install location must name a dedicated Sensorius directory.'
    }

    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $stateTemp = "$stateFile.tmp.$PID"
    Set-Content -LiteralPath $stateTemp -Value $installDir -Encoding UTF8
    Move-Item -LiteralPath $stateTemp -Destination $stateFile -Force
    return $installDir
}
