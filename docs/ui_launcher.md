# UI launcher

Windows users can start and stop the local web UI from the repository root:

- `start_ui.bat`: starts the local service and opens `http://127.0.0.1:8765/`.
- `stop_ui.bat`: closes the browser window opened by `start_ui.bat` and stops the local service.

Packaged Windows builds use the portable app folder produced by:

```powershell
.\tools\build_windows_app.ps1
```

The build output is `dist/ArknightsScheduleUI/`. Users can unzip or copy that folder and double-click
`ArknightsScheduleUI.exe`; no local Python installation is required.

The launcher records runtime state under `outputs/ui_runtime/`, including the server PID and the browser PID.
It also writes `launcher.log` and `server.port` for startup diagnostics and port fallback.

Notes:

- If Edge or Chrome is available, the UI opens in a dedicated app-style browser window so `stop_ui.bat` can close it.
- If neither Edge nor Chrome is found, the launcher falls back to the default browser. In that case `stop_ui.bat` stops the service, but the browser tab may need to be closed manually.
- If backend Python code changes while the UI is running, run `stop_ui.bat` and then `start_ui.bat` again.
