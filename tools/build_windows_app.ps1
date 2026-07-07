param(
    [string]$Python = "python",
    [switch]$InstallPackageDeps,
    [switch]$SkipDataRefresh
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$SpecFile = Join-Path $Root "tools\ArknightsScheduleUI.spec"
$OutputDir = Join-Path $Root "dist\ArknightsScheduleUI"
$PackageSpec = "$Root`[package`]"
$DataDir = Join-Path $Root "data\cache"
$RequiredDataFiles = @(
    "building_data.json",
    "character_table.json",
    "item_table.json",
    "data_version.txt"
)

$PythonExe = (Get-Command $Python -ErrorAction Stop).Source
$PythonRoot = Split-Path -Parent $PythonExe
$PythonDllDirs = @(
    $PythonRoot,
    (Join-Path $PythonRoot "DLLs"),
    (Join-Path $PythonRoot "Library\bin")
) | Where-Object { Test-Path -LiteralPath $_ }
$env:PATH = (($PythonDllDirs + $env:PATH) -join [System.IO.Path]::PathSeparator)

function Assert-DataCacheReady {
    $missing = @()
    foreach ($fileName in $RequiredDataFiles) {
        $path = Join-Path $DataDir $fileName
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $missing += $fileName
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing game data cache files for packaging: $($missing -join ', '). Run update-data or omit -SkipDataRefresh."
    }
}

function Remove-BuildOutputDirectory {
    if (-not (Test-Path -LiteralPath $OutputDir)) {
        return
    }

    $resolvedOutput = (Resolve-Path -LiteralPath $OutputDir).Path
    $expectedRoot = [System.IO.Path]::GetFullPath((Join-Path $Root "dist"))
    if (-not $resolvedOutput.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected build output path: $resolvedOutput"
    }

    Write-Host "Removing stale build output: $resolvedOutput"
    $longPath = "Microsoft.PowerShell.Core\FileSystem::\\?\$resolvedOutput"
    try {
        Remove-Item -LiteralPath $longPath -Recurse -Force
    }
    catch {
        throw "Failed to remove stale build output. Close any running ArknightsScheduleUI or browser app windows and try again. $($_.Exception.Message)"
    }
}

if ($InstallPackageDeps) {
    Write-Host "Installing packaging dependencies from $PackageSpec"
    & $Python -m pip install -e $PackageSpec
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install packaging dependencies."
    }
}

if (-not $SkipDataRefresh) {
    Write-Host "Refreshing bundled game data cache..."
    Push-Location $Root
    try {
        & $Python -m arknights_schedule_generator.cli update-data --data-dir $DataDir --force
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to refresh bundled game data cache."
        }
    }
    finally {
        Pop-Location
    }
}
Assert-DataCacheReady

Remove-BuildOutputDirectory

& $Python -c "import PyInstaller; print(PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\tools\build_windows_app.ps1 -InstallPackageDeps"
}

Write-Host "Building Windows portable app..."
& $Python -m PyInstaller --noconfirm --clean $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

if (-not (Test-Path -LiteralPath (Join-Path $OutputDir "ArknightsScheduleUI.exe"))) {
    throw "Build completed but ArknightsScheduleUI.exe was not found under $OutputDir."
}

Write-Host "Portable app ready: $OutputDir"
