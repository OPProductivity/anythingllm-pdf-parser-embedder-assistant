"""Console entry point for the portable AnythingLLM PDF Assistant package."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from portable_paths import application_paths, ensure_application_directories, package_resource_path


SERVER_MARKER_NAME = "localhost-server.json"
SERVER_ROOT_PID_ENV = "ANYTHINGLLM_PDF_ASSISTANT_SERVER_ROOT_PID"
START_SHORTCUT_NAME = "Start AnythingLLM PDF Assistant.lnk"
STOP_SHORTCUT_NAME = "Stop AnythingLLM PDF Assistant.lnk"
POWERSHELL_COMMAND_TIMEOUT_SECONDS = 15
BRIDGE_COMMAND_TIMEOUT_SECONDS = 120
BROWSER_READY_TIMEOUT_SECONDS = 45
BROWSER_READY_POLL_SECONDS = 0.15
_SERVER_MARKER_WRITE_LOCK = threading.Lock()
_SERVER_START_LOCK = threading.Lock()


@contextmanager
def _server_start_ownership_lock(port: int):
    """Serialize the check-and-claim boundary across shortcut processes.

    The marker's atomic write protects file integrity, but it cannot stop two
    separate shortcut processes from both observing "no server" before either
    writes it. A Windows named mutex is released by the kernel even if a
    starter crashes, avoiding a stale lock-file failure mode.
    """
    if os.name != "nt":
        with _SERVER_START_LOCK:
            yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    wait_for_single = kernel32.WaitForSingleObject
    wait_for_single.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait_for_single.restype = ctypes.c_uint32
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = [ctypes.c_void_p]
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    name = f"Local\\AnythingLLMPdfAssistantStart-{int(port)}"
    handle = create_mutex(None, False, name)
    if not handle:
        raise RuntimeError("Windows could not create the local server startup lock.")
    acquired = False
    try:
        # Do not invent a second startup deadline here. The guarded preflight
        # operations already have their own bounded calls, and Windows releases
        # this kernel object automatically if the owning starter crashes.
        # A duplicate shortcut invocation can therefore wait until it can read
        # the first process's published marker instead of failing after an
        # arbitrary 15-second race window.
        result = wait_for_single(handle, 0xFFFFFFFF)  # INFINITE
        if result not in (0x00000000, 0x00000080):  # OBJECT_0 or ABANDONED
            raise RuntimeError("Another local server start is still being resolved.")
        acquired = True
        yield
    finally:
        if acquired:
            release_mutex(handle)
        close_handle(handle)


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _local_app_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/"


def _local_app_is_responding(port: int, *, timeout_seconds: float = 0.75) -> bool:
    """Return whether the assistant's HTTP endpoint can serve a browser now."""
    try:
        with urllib.request.urlopen(_local_app_url(port), timeout=timeout_seconds) as response:
            return int(getattr(response, "status", response.getcode())) == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _open_local_app_browser(port: int) -> None:
    """Open the local URL through the operating system's normal browser route."""
    url = _local_app_url(port)
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]  # Windows ShellExecute
        else:
            webbrowser.open_new_tab(url)
    except OSError:
        # The server remains usable when the OS has no default browser. The
        # caller still prints the localhost URL for a manual open.
        return


def _open_browser_when_local_app_is_ready(port: int) -> threading.Thread:
    """Open one browser tab after the app accepts HTTP, without blocking startup.

    This deliberately lives in the CLI rather than delegating to Gradio's
    ``inbrowser`` flag. The latter is not reliable from a hidden desktop
    shortcut and does nothing on a duplicate Start while the current process
    is still coming up.
    """

    def wait_and_open() -> None:
        deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _local_app_is_responding(port):
                _open_local_app_browser(port)
                return
            time.sleep(BROWSER_READY_POLL_SECONDS)

    watcher = threading.Thread(
        target=wait_and_open,
        name=f"anythingllm-pdf-browser-{int(port)}",
        daemon=True,
    )
    watcher.start()
    return watcher


def _print_paths() -> int:
    for name, path in application_paths().items():
        print(f"{name}: {path}")
    return 0


def _marker_root_pid(record: dict) -> int:
    """Read the launch-root PID, accepting a marker written by an older build."""
    try:
        return int(record.get("root_pid") or record.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _owned_server_process_command(pid: int) -> str:
    """Return a PID's command line on Windows, without trusting a recycled PID."""
    powershell = _powershell()
    if sys.platform != "win32" or not powershell or int(pid) <= 0:
        return ""
    environment = os.environ.copy()
    environment["ANYTHINGLLM_SERVER_PID"] = str(int(pid))
    try:
        observed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $env:ANYTHINGLLM_SERVER_PID)).CommandLine",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return observed.stdout or ""


def _listener_belongs_to_server_root(port: int, root_pid: int) -> bool | None:
    """Return whether the listener is the owned root or its child.

    ``None`` means that no listener exists yet.  This is intentionally a
    read-only check: marker ownership must never authorize a broad kill based
    on a port alone.
    """
    powershell = _powershell()
    if sys.platform != "win32" or not powershell or int(root_pid) <= 0:
        return False
    environment = os.environ.copy()
    environment.update({"ANYTHINGLLM_SERVER_PORT": str(int(port)), "ANYTHINGLLM_SERVER_ROOT_PID": str(int(root_pid))})
    script = """
$listener = Get-NetTCPConnection -LocalPort ([int]$env:ANYTHINGLLM_SERVER_PORT) -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $listener) { exit 2 }
$listenerPid = [int]$listener.OwningProcess
$candidate = $listenerPid
$seen = @{}
while ($candidate -gt 0 -and -not $seen.ContainsKey($candidate)) {
  if ($candidate -eq [int]$env:ANYTHINGLLM_SERVER_ROOT_PID) { Write-Output 'owned'; exit 0 }
  $seen[$candidate] = $true
  $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $candidate) -ErrorAction SilentlyContinue
  if ($null -eq $process) { break }
  $candidate = [int]$process.ParentProcessId
}
Write-Output 'foreign'
exit 1
"""
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 2:
        return None
    return result.returncode == 0 and "owned" in (result.stdout or "").casefold()


def _recorded_server_is_alive_on_port(port: int) -> bool:
    """Return whether the marker names the owned server starting or serving ``port``.

    Gradio can create a listener child on Windows.  The marker therefore
    identifies the launch root, while the port is accepted only when that
    listener belongs to its process tree.  A brief no-listener interval is a
    legitimate owned start-up state and prevents duplicate Start shortcuts.
    """
    try:
        record = json.loads(_server_marker_path().read_text(encoding="utf-8"))
        pid = _marker_root_pid(record)
        recorded_port = int(record.get("port") or 0)
        if pid <= 0 or recorded_port != int(port):
            return False
        command_line = _owned_server_process_command(pid).casefold()
        if not any(token in command_line for token in ("anythingllm_pdf_assistant_cli", "anythingllm-pdf-assistant")):
            return False
        listener_owned = _listener_belongs_to_server_root(port, pid)
        return listener_owned is not False
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _doctor(port: int, *, allow_owned_running_server: bool = True) -> int:
    paths = ensure_application_directories()
    problems: list[str] = []
    for name in ("root", "outputs", "logs", "config"):
        if not os.access(paths[name], os.W_OK):
            problems.append(f"{name} is not writable: {paths[name]}")
    if not _port_is_available(port):
        if allow_owned_running_server and _recorded_server_is_alive_on_port(port):
            print("Portable installation is healthy.")
            print(f"The owned local PDF assistant is already serving on port {port}.")
            print(f"Data directory: {paths['root']}")
            return 0
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
    root_pid = int(os.environ.get(SERVER_ROOT_PID_ENV) or os.getpid())
    # A Windows child used by Gradio inherits this value.  It must never take
    # over Stop ownership merely because it is the process that binds the port.
    os.environ.setdefault(SERVER_ROOT_PID_ENV, str(root_pid))
    payload = {
        "pid": root_pid,
        "root_pid": root_pid,
        "port": int(port),
        "executable": str(Path(sys.executable).resolve()),
        "command": "anythingllm-pdf-assistant start",
        "started_at": time.time(),
    }
    # A fixed ``.tmp`` name allowed two shortcut launches to clobber each
    # other's marker staging file. The marker is the ownership boundary for
    # Stop, so publish it only after one complete, durable write.
    with _SERVER_MARKER_WRITE_LOCK:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=marker.parent,
                prefix=f".{marker.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return marker


def _remove_own_server_marker(marker: Path) -> None:
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if _marker_root_pid(record) == os.getpid():
        marker.unlink(missing_ok=True)


def _powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh") or ""


def _desktop_directory() -> Path:
    powershell = _powershell()
    if sys.platform != "win32" or not powershell:
        raise RuntimeError("Desktop shortcuts are supported on Windows with PowerShell.")
    try:
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
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Windows Desktop-folder lookup did not complete: {exc}") from exc
    desktop = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    if desktop is None:
        raise RuntimeError("Windows did not return a usable Desktop folder.")
    return desktop


def _shortcut_arguments(action: str) -> str:
    executable = Path(sys.executable).resolve()
    escaped_executable = str(executable).replace("'", "''")
    working_directory = Path(__file__).resolve().parent
    escaped_working_directory = str(working_directory).replace("'", "''")
    command = "start --browser" if action == "start" else "stop"
    # A Stop shortcut otherwise appears to do nothing: it closes its tiny
    # PowerShell host at exactly the moment the user needs confirmation. Keep
    # that host minimized, but alive briefly so it is visible on the taskbar
    # and can be restored to read the success/failure line if desired.
    if action == "stop":
        command = "stop; $assistantExitCode = $LASTEXITCODE; Start-Sleep -Seconds 4; exit $assistantExitCode"
        command_text = f"& '{escaped_executable}' -m anythingllm_pdf_assistant_cli {command}"
    else:
        # Launch the long-running Python server in its own hidden process.
        # Hiding only the short PowerShell host leaves the child python.exe
        # console visible, which makes a desktop shortcut look stuck even
        # though the browser app is already serving.
        command_text = (
            f"Start-Process -FilePath '{escaped_executable}' "
            "-ArgumentList @('-m', 'anythingllm_pdf_assistant_cli', 'start', '--browser') "
            f"-WorkingDirectory '{escaped_working_directory}' "
            "-WindowStyle Hidden"
        )
    # ``-Command`` accepts one command string. Without these quotes, the
    # leading invocation operator becomes the complete argument and a Python
    # path containing spaces is split at ``C:\\Program`` by PowerShell.
    return (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "
        f'"{command_text}"'
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
            # ``python -m anythingllm_pdf_assistant_cli`` must resolve the
            # local source/package. The application data directory contains
            # only settings and output, so using it as a shortcut working
            # directory makes Start/Stop fail silently in a source checkout.
            "ANYTHINGLLM_SHORTCUT_WORKING_DIRECTORY": str(Path(__file__).resolve().parent),
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
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"PowerShell did not create the shortcut before the timeout: {exc}") from exc
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
        pid = _marker_root_pid(record)
        port = int(record.get("port") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print("No owned local PDF assistant server is recorded.", file=sys.stderr)
        return 1
    if pid <= 0 or port <= 0:
        print("The local server marker is invalid; refusing to stop an unknown process.", file=sys.stderr)
        return 1
    command_line = _owned_server_process_command(pid).casefold()
    if not any(token in command_line for token in ("anythingllm_pdf_assistant_cli", "anythingllm-pdf-assistant")):
        print("The recorded process is no longer the owned PDF assistant server; refusing to stop it.", file=sys.stderr)
        return 1
    listener_owned = _listener_belongs_to_server_root(port, pid)
    if listener_owned is False:
        print("Port ownership does not match the recorded local PDF assistant; refusing to stop it.", file=sys.stderr)
        return 1
    try:
        stopped = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Could not stop the owned local PDF assistant server: {exc}", file=sys.stderr)
        return 1
    if stopped.returncode != 0:
        print((stopped.stderr or stopped.stdout or "Could not stop the local server.").strip(), file=sys.stderr)
        return 1
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _port_is_available(port):
            marker.unlink(missing_ok=True)
            print("Stopped the owned local PDF assistant server.")
            return 0
        time.sleep(0.15)
    print("The owned server process stopped, but port %s is still in use; keeping the marker for diagnosis." % port, file=sys.stderr)
    return 1


def _start(port: int, browser: bool) -> int:
    # A desktop Start shortcut is an "open the assistant" action, not merely
    # a process-creation command. If its owned server is already listening or
    # still completing startup, attach to that one rather than failing in a
    # hidden process where the person receives no feedback.
    try:
        with _server_start_ownership_lock(port):
            if _recorded_server_is_alive_on_port(port):
                if browser:
                    # This short-lived command is itself launched by a desktop
                    # shortcut. Wait for its browser helper here: a daemon watcher
                    # would otherwise be discarded the moment this process returns.
                    watcher = _open_browser_when_local_app_is_ready(port)
                    watcher.join(BROWSER_READY_TIMEOUT_SECONDS + BROWSER_READY_POLL_SECONDS)
                print(f"The owned local PDF assistant is already starting or serving at {_local_app_url(port)}")
                return 0

            if _doctor(port, allow_owned_running_server=False) != 0:
                return 1
            # Claim ownership before importing the large Gradio application.
            # The cross-process lock covers the observation and publication as
            # one transaction; after publication later starters can attach.
            os.environ.setdefault(SERVER_ROOT_PID_ENV, str(os.getpid()))
            marker = _write_server_marker(port)
    except RuntimeError as exc:
        print(f"Could not claim local server startup ownership: {exc}", file=sys.stderr)
        return 1
    # Shortcut creation belongs to install/repair, not to every launch. The
    # old per-start Desktop/PowerShell lookup added avoidable startup work and
    # could contend with Explorer while the shortcut was itself being opened.
    # Claim ownership before importing the large Gradio application. A second
    # Desktop click can otherwise arrive during that import while neither a
    # listener nor a marker exists, causing two full servers to race for the
    # same port.
    try:
        os.environ["GRADIO_SERVER_PORT"] = str(port)
        # This is a local document assistant.  Opening the browser is only a
        # UI convenience; it must never decide whether Gradio telemetry is
        # enabled.
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
        from rag_pdf_gradio_app import launch_application

        if browser:
            _open_browser_when_local_app_is_ready(port)
        # Browser opening is owned by the readiness watcher above. In
        # particular, do not rely on Gradio to open a tab from a hidden
        # shortcut process after the relatively heavy app build completes.
        launch_application(port=port, inbrowser=False)
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
    try:
        return subprocess.run(command, timeout=BRIDGE_COMMAND_TIMEOUT_SECONDS, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"AnythingLLM Desktop refresh bridge did not complete: {exc}", file=sys.stderr)
        return 1


def _compatibility_inspect(
    storage_dir: str,
    include_package_fingerprint: bool,
    emit_json: bool,
    api_url: str = "",
) -> int:
    """Run the read-only Desktop compatibility discovery from the public CLI."""
    from anythingllm_compatibility import characterize

    result = characterize(
        storage_dir or None,
        include_package_fingerprint=include_package_fingerprint,
        api_url=api_url,
    )
    if emit_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Desktop version: {result['desktop_version'] or 'unavailable'}")
    print(f"Normalized version: {result['desktop_version_normalized'] or 'unavailable'}")
    print(f"Desktop release status: {result['desktop_release_status']}")
    print(f"Storage schema status: {result['storage_schema_status']}")
    print(f"Guarded settings profile: {result['matched_profile'] or 'not qualified'}")
    print(f"Package fingerprint: {result['desktop_package']['fingerprint_status']}")
    for name in (
        "can_read_sqlite_state",
        "can_write_sqlite_settings",
        "can_upload_native_metadata",
    ):
        capability = result["capabilities"][name]
        print(f"{name}: {capability['status']} - {capability['message']}")
    if result.get("api_contract"):
        api_contract = result["api_contract"]
        print(f"API documentation contract: {api_contract['status']}")
        if api_contract["missing_core_routes"]:
            print("Missing documented core routes: " + ", ".join(api_contract["missing_core_routes"]))
        else:
            print("Documented core routes: " + ", ".join(api_contract["documented_core_routes"]))
        print("API contract does not grant write authority.")
    return 0


def _reliability_audit_run(run_root: str, write_bundle: bool, emit_json: bool) -> int:
    """Run the independent retained-evidence audit without contacting Desktop."""
    from reliability_audit import audit_run_directory, write_failure_bundle

    audit = audit_run_directory(run_root)
    bundle = write_failure_bundle(run_root, audit) if write_bundle else None
    if emit_json:
        print(json.dumps({**audit, "failure_bundle": bundle.name if bundle else ""}, indent=2, ensure_ascii=False))
    else:
        print(f"Integrity audit: {audit['audit_status']}")
        print(f"Recorded run outcome: {audit['run_outcome']}")
        for finding in audit.get("findings") or []:
            print(f"{finding['severity'].upper()} {finding['code']}: {finding['message']}")
        if bundle:
            print(f"Compact redacted failure bundle: {bundle.name}")
    return 1 if audit.get("audit_status") == "fail" else 0


def _reliability_self_test(output_root: str, emit_json: bool) -> int:
    """Run the offline crash and real process-boundary transport matrices."""
    import tempfile
    from pathlib import Path

    from reliability_acceptance import run_offline_crash_acceptance
    from reliability_fault_injection import run_transport_fault_acceptance

    def execute(root: Path):
        crash = run_offline_crash_acceptance(root / "crash-matrix")
        transport = run_transport_fault_acceptance(root / "transport-matrix")
        return {
            "schema": "anythingllm_pdf_assistant_reliability_self_test_v1",
            "status": "pass" if crash["status"] == transport["status"] == "pass" else "fail",
            "crash_matrix": crash,
            "transport_matrix": transport,
        }

    if output_root:
        report = execute(Path(output_root))
    else:
        with tempfile.TemporaryDirectory(prefix="anythingllm-pdf-reliability-") as temp_dir:
            report = execute(Path(temp_dir))
    if emit_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Reliability self-test: {report['status']}")
        print(
            f"Crash checkpoints: {report['crash_matrix']['scenario_count']} "
            f"({report['crash_matrix']['status']})"
        )
        print(
            f"Transport faults: {report['transport_matrix']['scenario_count']} "
            f"({report['transport_matrix']['status']})"
        )
    return 0 if report["status"] == "pass" else 1


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

    compatibility = subcommands.add_parser(
        "compatibility",
        help="inspect local AnythingLLM Desktop compatibility without changing Desktop",
    )
    compatibility.add_argument("action", choices=("inspect",), nargs="?", default="inspect")
    compatibility.add_argument("--storage-dir", default="", help="optional AnythingLLM Desktop storage directory")
    compatibility.add_argument(
        "--api-url",
        default="",
        help="optional loopback API root; reads Swagger documentation only and never grants write authority",
    )
    compatibility.add_argument(
        "--package-fingerprint",
        action="store_true",
        help="also hash app.asar; this is slower and intended for compatibility audits",
    )
    compatibility.add_argument("--json", action="store_true", help="emit the full redacted evidence record")

    reliability = subcommands.add_parser(
        "reliability",
        help="run read-only reliability checks against retained run evidence",
    )
    reliability.add_argument("action", choices=("audit-run", "self-test"))
    reliability.add_argument("--run-root", default="", help="retained Automatic run directory")
    reliability.add_argument(
        "--output-root",
        default="",
        help="optional retained output directory for the offline self-test",
    )
    reliability.add_argument(
        "--write-failure-bundle",
        action="store_true",
        help="write a compact redacted bundle only when contradictions are found",
    )
    reliability.add_argument("--json", action="store_true", help="emit the machine-readable audit")
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
    if command == "compatibility":
        return _compatibility_inspect(args.storage_dir, args.package_fingerprint, args.json, args.api_url)
    if command == "reliability":
        if args.action == "self-test":
            return _reliability_self_test(args.output_root, args.json)
        if not args.run_root:
            print("reliability audit-run requires --run-root", file=sys.stderr)
            return 2
        return _reliability_audit_run(
            args.run_root,
            args.write_failure_bundle,
            args.json,
        )
    raise AssertionError(f"unexpected command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
