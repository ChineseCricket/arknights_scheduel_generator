param(
    [string]$Python = "python",
    [switch]$InstallPackageDeps
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$SpecFile = Join-Path $Root "tools\ArknightsScheduleUI.spec"
$OutputDir = Join-Path $Root "dist\ArknightsScheduleUI"
$PackageSpec = "$Root`[package`]"

if ($InstallPackageDeps) {
    Write-Host "Installing packaging dependencies from $PackageSpec"
    & $Python -m pip install -e $PackageSpec
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install packaging dependencies."
    }
}

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
