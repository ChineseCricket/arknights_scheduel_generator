param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $Root "outputs\ui_runtime"
$ServerPidFile = Join-Path $StateDir "server.pid"
$ServerPortFile = Join-Path $StateDir "server.port"
$BrowserPidFile = Join-Path $StateDir "browser.pid"

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

function Stop-TrackedProcess {
    param(
        [string]$PidFile,
        [string]$Label,
        [string]$CommandLinePattern = ""
    )

    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $false
    }

    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if (-not $raw) {
        Remove-Item -LiteralPath $PidFile -Force
        return $false
    }

    $processId = [int]$raw
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($process -and $CommandLinePattern -and (-not $processInfo -or $processInfo.CommandLine -notmatch $CommandLinePattern)) {
        Write-Host "Skipping stale $Label PID $processId."
        Remove-Item -LiteralPath $PidFile -Force
        return $false
    }
    if ($process) {
        Stop-Process -Id $processId -Force
        Write-Host "Stopped $Label process $processId."
    }
    Remove-Item -LiteralPath $PidFile -Force
    return $true
}

function Test-ProcessCommandLine {
    param(
        [int]$ProcessId,
        [string]$CommandLinePattern
    )

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    return $processInfo -and $processInfo.CommandLine -match $CommandLinePattern
}

if (Test-Path -LiteralPath $StateDir) {
    if (Test-Path -LiteralPath $ServerPortFile) {
        $rawPort = (Get-Content -LiteralPath $ServerPortFile -Raw).Trim()
        if ($rawPort -match "^\d+$") {
            $Port = [int]$rawPort
        }
    }
    $browserProfilePattern = [regex]::Escape((Join-Path $StateDir "browser_profile"))
    Stop-TrackedProcess -PidFile $BrowserPidFile -Label "browser" -CommandLinePattern $browserProfilePattern | Out-Null
    Stop-TrackedProcess -PidFile $ServerPidFile -Label "server" -CommandLinePattern "arknights_schedule_generator|ArknightsScheduleUI" | Out-Null
    if (Test-Path -LiteralPath $ServerPortFile) {
        Remove-Item -LiteralPath $ServerPortFile -Force
    }
}

$listeningPid = Get-ListeningPid -Port $Port
if ($listeningPid) {
    if (-not (Test-ProcessCommandLine -ProcessId $listeningPid -CommandLinePattern "arknights_schedule_generator|ArknightsScheduleUI")) {
        Write-Host "Port $Port is used by another process, not the UI server. Leaving it running."
        exit 0
    }
    Stop-Process -Id $listeningPid -Force
    Write-Host "Stopped server listening on port $Port, process $listeningPid."
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not (Get-ListeningPid -Port $Port)) {
        Write-Host "UI server is stopped."
        exit 0
    }
    Start-Sleep -Milliseconds 300
}

throw "Port $Port is still listening after stop."
