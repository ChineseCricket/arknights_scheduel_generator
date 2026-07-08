from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def write_crash_log(message: str) -> Path:
    log_path = app_root() / "outputs" / "ui_runtime" / "launcher_crash.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(message, encoding="utf-8")
    return log_path


def show_crash_message(message: str, log_path: Path) -> None:
    full_message = f"{message}\n\nLog file:\n{log_path}"
    if sys.platform == "win32" and os.environ.get("AK_SCHEDULE_NO_MESSAGE_BOX") != "1":
        ctypes.windll.user32.MessageBoxW(None, full_message, "ArknightsScheduleUI", 0x00000010)
        return
    print(full_message, file=sys.stderr)


def run_launcher() -> int:
    try:
        from arknights_schedule_generator.desktop_launcher import main

        return main()
    except SystemExit:
        raise
    except BaseException as exc:
        details = traceback.format_exc()
        path = write_crash_log(details)
        show_crash_message(str(exc), path)
        raise


if __name__ == "__main__":
    # Required for ProcessPoolExecutor workers in the frozen Windows build.
    # Keep this before importing the launcher, otherwise multiprocessing child
    # processes re-enter the desktop UI instead of running the worker payload.
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(run_launcher())
