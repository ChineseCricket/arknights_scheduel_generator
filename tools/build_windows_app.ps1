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
