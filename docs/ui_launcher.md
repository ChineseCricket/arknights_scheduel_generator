# UI launcher

Windows users can start and stop the local web UI from the repository root:

- `start_ui.bat`: starts the local service and opens `http://127.0.0.1:8765/`.
- `stop_ui.bat`: closes the browser window opened by `start_ui.bat` and stops the local service.

The launcher records runtime state under `outputs/ui_runtime/`, including the server PID and the browser PID.

Notes:

- If Edge or Chrome is available, the UI opens in a dedicated app-style browser window so `stop_ui.bat` can close it.
- If neither Edge nor Chrome is found, the launcher falls back to the default browser. In that case `stop_ui.bat` stops the service, but the browser tab may need to be closed manually.
- If backend Python code changes while the UI is running, run `stop_ui.bat` and then `start_ui.bat` again.
