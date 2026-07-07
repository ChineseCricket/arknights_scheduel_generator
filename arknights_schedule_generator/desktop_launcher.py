from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arknights_schedule_generator import web_app
from arknights_schedule_generator.data import REQUIRED_FILES


APP_DEFAULTS_MARKER = "arknights-schedule-generator-ui"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PORT_END = 8799
STARTUP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class RuntimeState:
    root_dir: Path
    state_dir: Path
    log_path: Path
    server_pid_path: Path
    server_port_path: Path
    browser_pid_path: Path


@dataclass(frozen=True)
class PortChoice:
    port: int
    url: str
    reuse_existing: bool


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root_dir = resolve_root(args.root)
    state = runtime_state(root_dir)
    prepare_runtime_root(root_dir)

    try:
        if args.server:
            return web_app.main(["--host", args.host, "--port", str(args.port), "--root", str(root_dir)])
        return launch_desktop_ui(args, state)
    except Exception as exc:  # pragma: no cover - message box path is Windows integration.
        write_log(state.log_path, "Launcher failed:\n" + traceback.format_exc())
        show_failure(str(exc), state.log_path)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ak-schedule-ui-launcher",
        description="Start the Arknights schedule UI as a desktop app.",
    )
    parser.add_argument("--server", action="store_true", help="Run the internal HTTP server process.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--port-end", type=int, default=None)
    parser.add_argument("--root", default=None, help="Workspace root for data, outputs, and served files.")
    parser.add_argument("--no-browser", action="store_true", help="Start only the local server.")
    return parser


def launch_desktop_ui(args: argparse.Namespace, state: RuntimeState) -> int:
    write_log(state.log_path, f"Launcher started. root={state.root_dir}")
    port_end = args.port_end if args.port_end is not None else max(args.port, DEFAULT_PORT_END)
    port_choice = choose_port(args.host, args.port, port_end)
    state.server_port_path.write_text(str(port_choice.port), encoding="ascii")

    if port_choice.reuse_existing:
        listening_pid = get_listening_pid(port_choice.port)
        if listening_pid:
            state.server_pid_path.write_text(str(listening_pid), encoding="ascii")
        write_log(state.log_path, f"Reusing existing UI server at {port_choice.url}")
    else:
        server_process = start_server(args.host, port_choice.port, state)
        state.server_pid_path.write_text(str(server_process.pid), encoding="ascii")
        wait_for_server(port_choice.url, server_process, state.log_path)
        write_log(state.log_path, f"Started UI server pid={server_process.pid} url={port_choice.url}")

    if not args.no_browser:
        open_browser_window(port_choice.url, state)
    return 0


def resolve_root(raw_root: str | None) -> Path:
    if raw_root:
        return Path(raw_root).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_state(root_dir: Path) -> RuntimeState:
    state_dir = root_dir / "outputs" / "ui_runtime"
    return RuntimeState(
        root_dir=root_dir,
        state_dir=state_dir,
        log_path=state_dir / "launcher.log",
        server_pid_path=state_dir / "server.pid",
        server_port_path=state_dir / "server.port",
        browser_pid_path=state_dir / "browser.pid",
    )


def prepare_runtime_root(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "outputs" / "ui_runtime").mkdir(parents=True, exist_ok=True)
    (root_dir / "data").mkdir(parents=True, exist_ok=True)
    copy_bundled_fixture(root_dir)
    copy_bundled_data_cache(root_dir)


def copy_bundled_fixture(root_dir: Path) -> None:
    bundled_root = bundled_resource_root()
    if not bundled_root:
        return
    source = bundled_root / "examples" / "fixtures" / "yituliu_full_roster_maxed.xlsx"
    if not source.is_file():
        return
    target = root_dir / "examples" / "fixtures" / source.name
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_bundled_data_cache(root_dir: Path) -> None:
    bundled_root = bundled_resource_root()
    if not bundled_root:
        return
    source_dir = bundled_root / "data" / "cache"
    if not source_dir.is_dir():
        return
    target_dir = root_dir / "data" / "cache"
    target_dir.mkdir(parents=True, exist_ok=True)
    for file_name in REQUIRED_FILES:
        source = source_dir / file_name
        target = target_dir / file_name
        if target.exists() or not source.is_file():
            continue
        shutil.copy2(source, target)


def bundled_resource_root() -> Path | None:
    raw_meipass = getattr(sys, "_MEIPASS", None)
    if not raw_meipass:
        return None
    return Path(raw_meipass)


def choose_port(host: str, start_port: int, end_port: int) -> PortChoice:
    if start_port > end_port:
        raise ValueError("--port must be less than or equal to --port-end.")
    for port in range(start_port, end_port + 1):
        url = make_base_url(host, port)
        if is_app_server(url):
            return PortChoice(port=port, url=url, reuse_existing=True)
        if is_port_available(host, port):
            return PortChoice(port=port, url=url, reuse_existing=False)
    raise RuntimeError(f"No available port found from {start_port} to {end_port}.")


def make_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def is_app_server(base_url: str) -> bool:
    payload = request_defaults(base_url)
    if not payload:
        return False
    if payload.get("application") == APP_DEFAULTS_MARKER:
        return True
    return {"root", "layouts", "modes", "paths"}.issubset(payload)


def request_defaults(base_url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(base_url.rstrip("/") + "/api/defaults")
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def start_server(host: str, port: int, state: RuntimeState) -> subprocess.Popen[bytes]:
    command = server_command(host, port, state.root_dir)
    write_log(state.log_path, "Starting server: " + subprocess.list2cmdline(command))
    last_error: OSError | None = None
    for flags in server_creationflag_candidates():
        log_handle = state.log_path.open("ab")
        try:
            return subprocess.Popen(
                command,
                cwd=state.root_dir,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            last_error = exc
            write_log(state.log_path, f"Server start failed with creationflags={flags}: {exc}")
        finally:
            log_handle.close()
    raise RuntimeError(f"Failed to start UI server: {last_error}")


def server_command(host: str, port: int, root_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--server",
            "--host",
            host,
            "--port",
            str(port),
            "--root",
            str(root_dir),
        ]
    return [
        sys.executable,
        "-m",
        "arknights_schedule_generator.desktop_launcher",
        "--server",
        "--host",
        host,
        "--port",
        str(port),
        "--root",
        str(root_dir),
    ]


def wait_for_server(base_url: str, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_app_server(base_url):
            return
        return_code = process.poll()
        if return_code is not None:
            detail = read_log_tail(log_path)
            raise RuntimeError(
                f"UI server exited during startup with code {return_code}.\n"
                f"See log: {log_path}\n{detail}"
            )
        time.sleep(0.5)

    if process.poll() is None:
        process.terminate()
    detail = read_log_tail(log_path)
    raise RuntimeError(f"UI server did not become ready at {base_url}.\nSee log: {log_path}\n{detail}")


def open_browser_window(url: str, state: RuntimeState) -> None:
    browser = find_browser()
    if browser:
        profile_dir = state.state_dir / "browser_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                str(browser),
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--new-window",
            ],
            creationflags=subprocess_creationflags(detach=True),
        )
        state.browser_pid_path.write_text(str(process.pid), encoding="ascii")
        write_log(state.log_path, f"Opened browser app window pid={process.pid}")
        return

    remove_file(state.browser_pid_path)
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)
    write_log(state.log_path, "Opened UI with the default browser.")


def find_browser() -> Path | None:
    for executable in ("msedge.exe", "chrome.exe"):
        found = shutil.which(executable)
        if found:
            return Path(found)

    for raw_path in (
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ):
        path = Path(raw_path)
        if path.is_file():
            return path
    return None


def get_listening_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess_creationflags(),
        )
    except OSError:
        return None

    needle = f":{port}"
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if needle in parts[1]:
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


def server_creationflag_candidates() -> tuple[int, ...]:
    if sys.platform != "win32":
        return (0,)
    return (
        subprocess_creationflags(detach=True, breakaway=True),
        subprocess_creationflags(detach=True, breakaway=False),
        subprocess_creationflags(detach=False, breakaway=False),
    )


def subprocess_creationflags(*, detach: bool = False, breakaway: bool = False) -> int:
    if sys.platform != "win32":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detach:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    return flags


def write_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message.rstrip()}\n")


def read_log_tail(log_path: Path, max_chars: int = 4000) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def show_failure(message: str, log_path: Path) -> None:
    full_message = f"{message}\n\nLog file:\n{log_path}"
    if (
        sys.platform == "win32"
        and getattr(sys, "frozen", False)
        and os.environ.get("AK_SCHEDULE_NO_MESSAGE_BOX") != "1"
    ):
        ctypes.windll.user32.MessageBoxW(None, full_message, "ArknightsScheduleUI", 0x00000010)
        return
    print(full_message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
