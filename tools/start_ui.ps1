param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $Root "outputs\ui_runtime"
$ServerPidFile = Join-Path $StateDir "server.pid"
$BrowserPidFile = Join-Path $StateDir "browser.pid"
$Url = "http://${HostName}:${Port}/"

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
    throw "Could not find Python. Install the project environment or activate a Python with this package installed."
}

function Get-ListeningPid {
    param([int]$Port)

    $lines = netstat -ano | Select-String ":$Port"
    foreach ($line in $lines) {
        $parts = ($line.Line -split "\s+") | Where-Object { $_ }
        if ($parts.Count -ge 5 -and $parts[0] -eq "TCP" -and $parts[3] -eq "LISTENING") {
            return [int]$parts[4]
        }
    }
    return $null
}

function Test-UiServer {
    param([string]$Url)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "${Url}api/defaults" -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-UiServer {
    param([string]$Url)

    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-UiServer -Url $Url) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Find-Browser {
    foreach ($name in @("msedge.exe", "chrome.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    foreach ($path in @(
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    )) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            return $path
        }
    }
    return $null
}

if (Test-UiServer -Url $Url) {
    $listeningPid = Get-ListeningPid -Port $Port
    if ($listeningPid) {
        Set-Content -LiteralPath $ServerPidFile -Value $listeningPid -Encoding ascii
    }
    Write-Host "UI server is already running: $Url"
} else {
    $pythonExe = Get-PythonExe
    $serverArgs = @(
        "-m", "arknights_schedule_generator.web_app",
        "--host", $HostName,
        "--port", $Port,
        "--root", $Root
    )
    $serverProcess = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList $serverArgs `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $ServerPidFile -Value $serverProcess.Id -Encoding ascii

    if (-not (Wait-UiServer -Url $Url)) {
        throw "UI server did not become ready on $Url."
    }
    Write-Host "UI server started: $Url"
}

if (-not $NoBrowser) {
    $browser = Find-Browser
    if ($browser) {
        $profileDir = Join-Path $StateDir "browser_profile"
        New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
        $browserArgs = @(
            "--app=$Url",
            "--user-data-dir=""$profileDir""",
            "--no-first-run",
            "--new-window"
        )
        $browserProcess = Start-Process -FilePath $browser -ArgumentList $browserArgs -PassThru
        Set-Content -LiteralPath $BrowserPidFile -Value $browserProcess.Id -Encoding ascii
        Write-Host "Browser window opened."
    } else {
        Start-Process $Url
        if (Test-Path -LiteralPath $BrowserPidFile) {
            Remove-Item -LiteralPath $BrowserPidFile -Force
        }
        Write-Host "Opened with the default browser. The stop script can stop the server, but may not close that browser tab."
    }
}

Write-Host "Ready: $Url"
