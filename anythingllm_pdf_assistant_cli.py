"""Console entry point for the portable AnythingLLM PDF Assistant package."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from portable_paths import application_paths, ensure_application_directories, package_resource_path


SERVER_MARKER_NAME = "localhost-server.json"
START_SHORTCUT_NAME = "Start AnythingLLM PDF Assistant.lnk"
STOP_SHORTCUT_NAME = "Stop AnythingLLM PDF Assistant.lnk"


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _print_paths() -> int:
    for name, path in application_paths().items():
        print(f"{name}: {path}")
    return 0


def _doctor(port: int) -> int:
    paths = ensure_application_directories()
    problems: list[str] = []
    for name in ("root", "outputs", "logs", "config"):
        if not os.access(paths[name], os.W_OK):
            problems.append(f"{name} is not writable: {paths[name]}")
    if not _port_is_available(port):
        problems.append(f"port {port} is already in use")
    if problems:
        print("Portable installation needs attention:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("Portable installation is ready.")
    print(f"Data directory: {paths['root']}")
    print(f"Output directory: {paths['outputs']}")
    return 0


def _server_marker_path() -> Path:
    return ensure_application_directories()["config"] / SERVER_MARKER_NAME


def _write_server_marker(port: int) -> Path:
    marker = _server_marker_path()
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": int(port),
                "executable": str(Path(sys.executable).resolve()),
                "command": "anythingllm-pdf-assistant start",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(marker)
    return marker


def _remove_own_server_marker(marker: Path) -> None:
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if int(record.get("pid") or 0) == os.getpid():
        marker.unlink(missing_ok=True)


def _powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh") or ""


def _desktop_directory() -> Path:
    powershell = _powershell()
    if sys.platform != "win32" or not powershell:
        raise RuntimeError("Desktop shortcuts are supported on Windows with PowerShell.")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    desktop = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    if desktop is None:
        raise RuntimeError("Windows did not return a usable Desktop folder.")
    return desktop


def _shortcut_arguments(action: str) -> str:
    executable = Path(sys.executable).resolve()
    escaped_executable = str(executable).replace("'", "''")
    command = "start --browser" if action == "start" else "stop"
    # A Stop shortcut otherwise appears to do nothing: it closes its tiny
    # PowerShell host at exactly the moment the user needs confirmation. Keep
    # that host minimized, but alive briefly so it is visible on the taskbar
    # and can be restored to read the success/failure line if desired.
    if action == "stop":
        command = "stop; $assistantExitCode = $LASTEXITCODE; Start-Sleep -Seconds 4; exit $assistantExitCode"
    return (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -Command "
        f"& '{escaped_executable}' -m anythingllm_pdf_assistant_cli {command}"
    )


def _write_windows_shortcut(path: Path, arguments: str, icon_path: Path, description: str) -> None:
    powershell = _powershell()
    if not powershell:
        raise RuntimeError("PowerShell is required to create Windows shortcuts.")
    environment = os.environ.copy()
    environment.update(
        {
            "ANYTHINGLLM_SHORTCUT_PATH": str(path),
            "ANYTHINGLLM_SHORTCUT_TARGET": powershell,
            "ANYTHINGLLM_SHORTCUT_ARGUMENTS": arguments,
            "ANYTHINGLLM_SHORTCUT_WORKING_DIRECTORY": str(ensure_application_directories()["root"]),
            "ANYTHINGLLM_SHORTCUT_DESCRIPTION": description,
            "ANYTHINGLLM_SHORTCUT_ICON": str(icon_path),
        }
    )
    script = "\n".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            "$shortcut = $shell.CreateShortcut($env:ANYTHINGLLM_SHORTCUT_PATH)",
            "$shortcut.TargetPath = $env:ANYTHINGLLM_SHORTCUT_TARGET",
            "$shortcut.Arguments = $env:ANYTHINGLLM_SHORTCUT_ARGUMENTS",
            "$shortcut.WorkingDirectory = $env:ANYTHINGLLM_SHORTCUT_WORKING_DIRECTORY",
            "$shortcut.Description = $env:ANYTHINGLLM_SHORTCUT_DESCRIPTION",
            "$shortcut.IconLocation = $env:ANYTHINGLLM_SHORTCUT_ICON",
            "$shortcut.WindowStyle = 7",
            "$shortcut.Save()",
        ]
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not path.is_file():
        detail = (result.stderr or result.stdout or "PowerShell did not create the shortcut.").strip()
        raise RuntimeError(detail)


def install_desktop_shortcuts(overwrite: bool = False) -> tuple[Path, ...]:
    """Create the Start and Stop shortcuts for the current Windows user."""

    if sys.platform != "win32":
        raise RuntimeError("Desktop shortcuts are available only on Windows.")
    desktop = _desktop_directory()
    desktop.mkdir(parents=True, exist_ok=True)
    specs = (
        (
            desktop / START_SHORTCUT_NAME,
            "start",
            package_resource_path("assets/anythingllm-pdf-assistant-start.ico"),
            "Start the local RAG PDF app server and open the browser tab.",
        ),
        (
            desktop / STOP_SHORTCUT_NAME,
            "stop",
            package_resource_path("assets/anythingllm-pdf-assistant-stop.ico"),
            "Stop the owned local RAG PDF app server.",
        ),
    )
    created: list[Path] = []
    for path, action, icon_path, description in specs:
        if overwrite or not path.is_file():
            _write_windows_shortcut(path, _shortcut_arguments(action), icon_path, description)
        created.append(path)
    return tuple(created)


def _stop() -> int:
    if sys.platform != "win32":
        print("Stopping the local server is supported only on Windows.", file=sys.stderr)
        return 1
    marker = _server_marker_path()
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(record.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print("No owned local PDF assistant server is recorded.", file=sys.stderr)
        return 1
    if pid <= 0:
        print("The local server marker is invalid; refusing to stop an unknown process.", file=sys.stderr)
        return 1
    powershell = _powershell()
    if not powershell:
        print("PowerShell is required to verify the owned server process.", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment["ANYTHINGLLM_SERVER_PID"] = str(pid)
    observed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_Process -Filter 'ProcessId = ' + $env:ANYTHINGLLM_SERVER_PID).CommandLine"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    command_line = observed.stdout.casefold()
    if not any(token in command_line for token in ("anythingllm_pdf_assistant_cli", "anythingllm-pdf-assistant")):
        print("The recorded process is no longer the owned PDF assistant server; refusing to stop it.", file=sys.stderr)
        return 1
    stopped = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
    if stopped.returncode != 0:
        print((stopped.stderr or stopped.stdout or "Could not stop the local server.").strip(), file=sys.stderr)
        return 1
    marker.unlink(missing_ok=True)
    print("Stopped the owned local PDF assistant server.")
    return 0


def _start(port: int, browser: bool) -> int:
    if _doctor(port) != 0:
        return 1
    if sys.platform == "win32":
        try:
            install_desktop_shortcuts()
        except RuntimeError as exc:
            print(f"Desktop shortcuts were not created: {exc}", file=sys.stderr)
    os.environ["GRADIO_SERVER_PORT"] = str(port)
    if not browser:
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    from rag_pdf_gradio_app import launch_application

    marker = _write_server_marker(port)
    try:
        launch_application(port=port, inbrowser=browser)
    finally:
        _remove_own_server_marker(marker)
    return 0


def _bridge_script_path() -> Path:
    """Locate the optional bridge installer bundled with this pipx package."""

    return package_resource_path("Install-AnythingLLMDesktopRefreshBridge.ps1")


def _bridge(action: str, resources_path: str) -> int:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not powershell:
        print("PowerShell is required for the optional Desktop refresh bridge.", file=sys.stderr)
        return 1
    command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_bridge_script_path())]
    if resources_path:
        command.extend(["-ResourcesPath", resources_path])
    if action == "validate":
        command.append("-Validate")
    elif action == "uninstall":
        command.append("-Uninstall")
    elif action == "upgrade":
        command.append("-Upgrade")
    return subprocess.run(command, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AnythingLLM PDF Parser Embedder Assistant")
    subcommands = parser.add_subparsers(dest="command")

    start = subcommands.add_parser("start", help="start the local PDF assistant")
    start.add_argument("--port", type=int, default=7860, help="localhost port (default: 7860)")
    start.add_argument("--browser", action="store_true", help="open the app in the default browser")

    doctor = subcommands.add_parser("doctor", help="check writable paths and the local port")
    doctor.add_argument("--port", type=int, default=7860, help="localhost port to check")

    subcommands.add_parser("paths", help="show the current user's application paths")

    shortcuts = subcommands.add_parser("shortcuts", help="create or repair Windows desktop shortcuts")
    shortcuts.add_argument("action", choices=("install", "repair"), nargs="?", default="install")

    subcommands.add_parser("stop", help="stop the owned local PDF assistant server")

    bridge = subcommands.add_parser("bridge", help="manage the optional AnythingLLM Desktop refresh bridge")
    bridge.add_argument("action", choices=("install", "validate", "upgrade", "uninstall"))
    bridge.add_argument("--resources-path", default="", help="non-standard AnythingLLM Desktop resources directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "start"
    if command == "paths":
        return _print_paths()
    if command == "doctor":
        return _doctor(args.port)
    if command == "start":
        return _start(args.port, args.browser)
    if command == "stop":
        return _stop()
    if command == "shortcuts":
        try:
            paths = install_desktop_shortcuts(overwrite=args.action == "repair")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for path in paths:
            print(f"Desktop shortcut ready: {path}")
        return 0
    if command == "bridge":
        return _bridge(args.action, args.resources_path)
    raise AssertionError(f"unexpected command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
