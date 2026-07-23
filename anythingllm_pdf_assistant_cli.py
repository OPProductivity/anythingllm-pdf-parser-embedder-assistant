"""Console entry point for the portable AnythingLLM PDF Assistant package."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from portable_paths import application_paths, ensure_application_directories, package_resource_path


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


def _start(port: int, browser: bool) -> int:
    if _doctor(port) != 0:
        return 1
    os.environ["GRADIO_SERVER_PORT"] = str(port)
    if not browser:
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    from rag_pdf_gradio_app import launch_application

    launch_application(port=port, inbrowser=browser)
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
    if command == "bridge":
        return _bridge(args.action, args.resources_path)
    raise AssertionError(f"unexpected command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
