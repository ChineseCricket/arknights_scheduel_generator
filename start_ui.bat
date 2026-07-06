@echo off
setlocal

set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\start_ui.ps1" %*

if errorlevel 1 (
  echo.
  echo Failed to start the UI. Press any key to close this window.
  pause >nul
)
