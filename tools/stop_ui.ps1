param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $Root "outputs\ui_runtime"
$ServerPidFile = Join-Path $StateDir "server.pid"
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
        [string]$Label
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
    if ($process) {
        Stop-Process -Id $processId -Force
        Write-Host "Stopped $Label process $processId."
    }
    Remove-Item -LiteralPath $PidFile -Force
    return $true
}

if (Test-Path -LiteralPath $StateDir) {
    Stop-TrackedProcess -PidFile $BrowserPidFile -Label "browser" | Out-Null
    Stop-TrackedProcess -PidFile $ServerPidFile -Label "server" | Out-Null
}

$listeningPid = Get-ListeningPid -Port $Port
if ($listeningPid) {
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
