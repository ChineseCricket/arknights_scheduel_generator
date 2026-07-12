from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import ssl
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
SERVER_STOP_WAIT_SECONDS = 5.0


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


@dataclass(frozen=True)
class BrowserSession:
    process: subprocess.Popen[bytes]
    profile_dir: Path | None = None


@dataclass(frozen=True)
class ServerSession:
    server: web_app.ScheduleUIServer
    thread: threading.Thread


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.runtime_check:
        return write_runtime_check(resolve_root(args.root))
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


def runtime_check() -> dict[str, Any]:
    if not ssl.OPENSSL_VERSION:
        raise RuntimeError("This Python runtime does not expose OpenSSL.")
    if not hasattr(http.client, "HTTPSConnection") or not hasattr(urllib.request, "HTTPSHandler"):
        raise RuntimeError("This Python runtime does not support HTTPS downloads.")
    return {
        "ok": True,
        "openssl": ssl.OPENSSL_VERSION,
        "httpsConnection": True,
        "httpsHandler": True,
    }


def write_runtime_check(root_dir: Path) -> int:
    state_dir = runtime_state(root_dir).state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    output_path = state_dir / "runtime_check.json"
    try:
        payload = runtime_check()
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        output_path.write_text(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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
    parser.add_argument("--runtime-check", action="store_true", help=argparse.SUPPRESS)
    return parser


def launch_desktop_ui(args: argparse.Namespace, state: RuntimeState) -> int:
    write_log(state.log_path, f"Launcher started. root={state.root_dir}")
    port_end = args.port_end if args.port_end is not None else max(args.port, DEFAULT_PORT_END)
    port_choice = choose_port(args.host, args.port, port_end)
    state.server_port_path.write_text(str(port_choice.port), encoding="ascii")
    server_session = start_embedded_server(args.host, port_choice.port, state)
    state.server_pid_path.write_text(str(os.getpid()), encoding="ascii")
    try:
        wait_for_server(port_choice.url, server_session, state.log_path)
        write_log(state.log_path, f"Started embedded UI server pid={os.getpid()} url={port_choice.url}")

        if not args.no_browser:
            browser_session = open_browser_window(port_choice.url, state)
            if browser_session is not None:
                wait_for_browser_then_stop_server(browser_session, server_session, state)
            else:
                write_log(
                    state.log_path,
                    "Browser process is not monitorable; UI server will keep running until stop_ui is used.",
                )
                wait_for_manual_stop(server_session)
    finally:
        if server_session.thread.is_alive():
            stop_embedded_server(server_session, state)
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
        if is_port_available(host, port):
            return PortChoice(port=port, url=url)
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


def start_embedded_server(host: str, port: int, state: RuntimeState) -> ServerSession:
    """Bind the local service before opening the browser, in the UI process itself."""
    try:
        server = web_app.ScheduleUIServer((host, port), state.root_dir)
    except OSError as exc:
        raise RuntimeError(f"Failed to bind UI server on {host}:{port}: {exc}") from exc
    thread = threading.Thread(target=server.serve_forever, name="ui-server", daemon=True)
    thread.start()
    return ServerSession(server=server, thread=thread)


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


def wait_for_server(base_url: str, session: ServerSession, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_app_server(base_url):
            return
        if not session.thread.is_alive():
            detail = read_log_tail(log_path)
            raise RuntimeError(
                "UI server exited during startup.\n"
                f"See log: {log_path}\n{detail}"
            )
        time.sleep(0.5)

    detail = read_log_tail(log_path)
    raise RuntimeError(f"UI server did not become ready at {base_url}.\nSee log: {log_path}\n{detail}")


def open_browser_window(url: str, state: RuntimeState) -> BrowserSession | None:
    browser = find_browser()
    if browser:
        profile_dir = new_browser_profile_dir(state)
        profile_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                str(browser),
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--new-window",
                "--disable-background-mode",
            ],
            creationflags=subprocess_creationflags(),
        )
        state.browser_pid_path.write_text(str(process.pid), encoding="ascii")
        write_log(state.log_path, f"Opened browser app window pid={process.pid}")
        return BrowserSession(process=process, profile_dir=profile_dir)

    remove_file(state.browser_pid_path)
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)
    write_log(state.log_path, "Opened UI with the default browser.")
    return None


def new_browser_profile_dir(state: RuntimeState) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return state.state_dir / "browser_profiles" / f"{stamp}_{os.getpid()}"


def wait_for_browser_then_stop_server(
    browser_session: BrowserSession,
    server_session: ServerSession,
    state: RuntimeState,
) -> None:
    write_log(
        state.log_path,
        f"Monitoring browser pid={browser_session.process.pid}; UI server pid={os.getpid()}.",
    )
    browser_session.process.wait()
    write_log(state.log_path, f"Browser pid={browser_session.process.pid} exited; stopping UI server.")
    stop_embedded_server(server_session, state)
    remove_file(state.browser_pid_path)
    cleanup_browser_profile(browser_session.profile_dir, state)


def stop_embedded_server(session: ServerSession, state: RuntimeState) -> None:
    session.server.shutdown()
    session.thread.join(timeout=SERVER_STOP_WAIT_SECONDS)
    session.server.server_close()
    remove_file(state.server_pid_path)
    remove_file(state.server_port_path)
    write_log(state.log_path, "Stopped embedded UI server.")


def wait_for_manual_stop(session: ServerSession) -> None:
    """Keep the UI process alive when the platform cannot expose a browser PID."""
    while session.thread.is_alive():
        time.sleep(1.0)


def cleanup_browser_profile(profile_dir: Path | None, state: RuntimeState) -> None:
    if profile_dir is None:
        return
    try:
        resolved_profile = profile_dir.resolve()
        resolved_state = state.state_dir.resolve()
        if not resolved_profile.is_relative_to(resolved_state):
            return
        shutil.rmtree(resolved_profile, ignore_errors=True)
    except OSError as exc:
        write_log(state.log_path, f"Failed to clean browser profile {profile_dir}: {exc}")


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


def subprocess_creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
