param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $Root "outputs\ui_runtime"
$LogFile = Join-Path $StateDir "launcher.log"

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Get-PythonExe {
    $candidates = @()
    if ($env:USERPROFILE) {
        $candidates += Join-Path $env:USERPROFILE ".conda\envs\arknights-schedule-generator\python.exe"
    }
    $candidates += Join-Path $Root ".venv\Scripts\python.exe"
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates += $pythonCommand.Source
    }

    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw "Could not find Python. Use the packaged ArknightsScheduleUI.exe, or install the project environment."
}

function Test-PythonEnvironment {
    param([string]$PythonExe)

    $check = "import arknights_schedule_generator, openpyxl; print('OK')"
    $output = & $PythonExe -c $check 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "Python environment is not ready: $PythonExe`n$detail`nInstall dependencies with: python -m pip install -e ."
    }
}

$pythonExe = Get-PythonExe
Write-Host "Using Python: $pythonExe"

Push-Location $Root
try {
    Test-PythonEnvironment -PythonExe $pythonExe

    $launcherArgs = @(
        "-m", "arknights_schedule_generator.desktop_launcher",
        "--host", $HostName,
        "--port", $Port,
        "--root", $Root
    )
    if ($NoBrowser) {
        $launcherArgs += "--no-browser"
    }

    & $pythonExe @launcherArgs
    if ($LASTEXITCODE -ne 0) {
        throw "UI launcher failed. See log: $LogFile"
    }
} finally {
    Pop-Location
}
