@echo off
setlocal

set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\stop_ui.ps1" %*

if errorlevel 1 (
  echo.
  echo Failed to stop the UI cleanly. Press any key to close this window.
  pause >nul
)
