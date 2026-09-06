"""Console entry point for the portable AnythingLLM PDF Assistant package."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
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
AUTOMATIC_RUN_PROGRESS_NAME = "run-progress.json"
AUTOMATIC_RUN_CANCELLATION_MARKER = ".cancel-requested.json"
AUTOMATIC_RUN_CANCELLATION_RECOVERY = "cancellation-recovery.json"
START_SHORTCUT_NAME = "Start AnythingLLM PDF Assistant.lnk"
STOP_SHORTCUT_NAME = "Stop AnythingLLM PDF Assistant.lnk"
POWERSHELL_COMMAND_TIMEOUT_SECONDS = 15
BRIDGE_COMMAND_TIMEOUT_SECONDS = 120
BROWSER_READY_TIMEOUT_SECONDS = 45
BROWSER_READY_POLL_SECONDS = 0.15
_SERVER_MARKER_WRITE_LOCK = threading.Lock()
_SERVER_START_LOCK = threading.Lock()
_NATIVE_READ_SLOT = threading.Lock()


class ServerOwnershipProbeError(RuntimeError):
    """Ownership is unknown, not evidence that a process is foreign."""


def _bounded_native_read(action, timeout_seconds=2.0):
    """Bound the caller's wait, not the OS call; at most one read can linger.

    Only read-only actions belong here. A timed-out result is never reused.
    A stuck read refuses subsequent probes until it returns or this process
    exits, rather than creating an unbounded collection of probe threads.
    """
    if not _NATIVE_READ_SLOT.acquire(blocking=False):
        raise ServerOwnershipProbeError("A Windows identity read is still pending; retry shortly.")
    done = threading.Event()
    result = []
    errors = []
    def read():
        try:
            result.append(action())
        except BaseException as exc:
            errors.append(exc)
        finally:
            _NATIVE_READ_SLOT.release()
            done.set()
    try:
        threading.Thread(target=read, name="windows-identity-read", daemon=True).start()
    except BaseException:
        _NATIVE_READ_SLOT.release()
        raise
    if not done.wait(timeout_seconds):
        raise ServerOwnershipProbeError("Windows identity verification timed out; no stop was authorized.")
    if errors:
        raise errors[0]
    return result[0]


def _required_process_identity(process):
    # psutil may suppress AccessDenied during construction and retain
    # (pid, None). Never use Process equality as sufficient identity proof.
    created = process.create_time()
    if not isinstance(created, (float, int)) or not math.isfinite(created) or created <= 0:
        raise ServerOwnershipProbeError("Windows did not supply a process creation identity.")
    return process.pid, created


@contextmanager
def _pinned_server_process(pid, expected_ticks=None):
    """Keep the verified root PID unrecyclable through the existing tree stop."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        raise ServerOwnershipProbeError("Cannot retain the recorded Windows process identity; no stop authorized.")
    try:
        creation, exit_time, kernel_time, user_time = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ServerOwnershipProbeError("Cannot read the retained Windows process identity.")
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if expected_ticks is not None and str(ticks) != str(expected_ticks):
            raise ServerOwnershipProbeError("The recorded PID belongs to a different process incarnation; refusing Stop.")
        yield ticks
    finally:
        kernel.CloseHandle(handle)


def _notify_launcher_message(message: str) -> None:
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "PDF assistant startup", 0x40)
        except (OSError, AttributeError):
            pass


def _notify_browser_readiness_timeout(port: int) -> None:
    message = (
        f"The PDF assistant has not responded within {BROWSER_READY_TIMEOUT_SECONDS} seconds. "
        "It may still be starting; it has not been stopped.\n\n"
        f"Try {_local_app_url(port)} shortly, or use Start again. "
        "If it remains unavailable, inspect the assistant logs."
    )
    _notify_launcher_message(message)


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
        probe.settimeout(1.0)
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
    except OSError as exc:
        _notify_launcher_message(f"The assistant is ready, but Windows could not open the browser ({type(exc).__name__}). Open {url} manually.")


def _open_browser_when_local_app_is_ready(port: int, *, cancelled=None, loaded=None) -> threading.Thread:
    """Open one browser tab after the app accepts HTTP, without blocking startup.

    This deliberately lives in the CLI rather than delegating to Gradio's
    ``inbrowser`` flag. The latter is not reliable from a hidden desktop
    shortcut and does nothing on a duplicate Start while the current process
    is still coming up.
    """

    def wait_and_open() -> None:
        deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                return
            if (loaded is None or loaded.is_set()) and _local_app_is_responding(port):
                _open_local_app_browser(port)
                return
            time.sleep(BROWSER_READY_POLL_SECONDS)
        if cancelled is None or not cancelled.is_set():
            _notify_browser_readiness_timeout(port)
            # Startup may have finished while the notice was being read.
            if (cancelled is None or not cancelled.is_set()) and (loaded is None or loaded.is_set()) and _local_app_is_responding(port):
                _open_local_app_browser(port)

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


def _owned_server_process_command(pid: int) -> str | None:
    """Return a PID's command line; ``None`` means the ownership probe failed."""
    api = _native_process_api()
    if sys.platform == "win32" and api is not None and int(pid) > 0:
        try:
            def read_command():
                process = api.Process(int(pid))
                identity = _required_process_identity(process)
                command = subprocess.list2cmdline(process.cmdline())
                if identity != _required_process_identity(api.Process(int(pid))):
                    raise ServerOwnershipProbeError("Process identity changed during verification.")
                return command if process.is_running() else ""
            return _bounded_native_read(read_command)
        except api.NoSuchProcess:
            return ""
        except (api.Error, OSError, ServerOwnershipProbeError):
            return None
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
                "$ErrorActionPreference = 'Stop'; (Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $env:ANYTHINGLLM_SERVER_PID)).CommandLine",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (observed.stdout or "") if observed.returncode == 0 else None


def _native_process_api():
    # Lazy import keeps unrelated CLI commands light. Legacy installations can
    # still use the bounded PowerShell path; denied reads never fall back to a
    # weaker ownership decision.
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _native_listener_belongs_to_root(api, port: int, root_pid: int) -> bool | None:
    return _bounded_native_read(lambda: _native_listener_identity_read(api, port, root_pid))


def _native_listener_identity_read(api, port: int, root_pid: int) -> bool | None:
    try:
        # Windows' IPv4 table also includes dual-stack listeners. Querying
        # both families would falsely reject an unrelated IPv6-only listener
        # on the same numeric port, which cannot serve our IPv4 URL.
        listeners = [connection for connection in api.net_connections(kind="tcp4")
                     if connection.status == api.CONN_LISTEN and connection.laddr
                     and connection.laddr.port == int(port)
                     and connection.laddr.ip in {"127.0.0.1", "0.0.0.0"}]
        if not listeners:
            return None
        root = api.Process(int(root_pid))
        root_identity = _required_process_identity(root)
        for listener in listeners:
            if not listener.pid:
                raise ServerOwnershipProbeError("Windows did not identify the local port owner; retry.")
            candidate = api.Process(listener.pid)
            seen = set()
            while candidate is not None and candidate.pid not in seen:
                if _required_process_identity(candidate) == root_identity:
                    break
                seen.add(candidate.pid)
                candidate = candidate.parent()
            else:
                return False
        if not root.is_running() or _required_process_identity(api.Process(int(root_pid))) != root_identity:
            raise ServerOwnershipProbeError("The server changed during ownership verification; retry.")
        return True
    except (api.Error, OSError) as exc:
        raise ServerOwnershipProbeError("Windows could not read a stable process/port identity; retry.") from exc


def _listener_belongs_to_server_root(port: int, root_pid: int) -> bool | None:
    """Return whether the listener is the owned root or its child.

    ``None`` means that no listener exists yet.  This is intentionally a
    read-only check: marker ownership must never authorize a broad kill based
    on a port alone.
    """
    api = _native_process_api()
    if sys.platform == "win32" and api is not None and int(root_pid) > 0:
        return _native_listener_belongs_to_root(api, port, root_pid)
    powershell = _powershell()
    if sys.platform != "win32" or not powershell or int(root_pid) <= 0:
        return False
    environment = os.environ.copy()
    environment.update({"ANYTHINGLLM_SERVER_PORT": str(int(port)), "ANYTHINGLLM_SERVER_ROOT_PID": str(int(root_pid))})
    script = """
try {
  $listeners = @(Get-NetTCPConnection -ErrorAction Stop | Where-Object { $_.LocalPort -eq [int]$env:ANYTHINGLLM_SERVER_PORT -and $_.State -eq 'Listen' -and $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::', '::ffff:127.0.0.1') })
} catch { exit 3 }
$ipv4Listeners = @($listeners | Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0') })
if ($ipv4Listeners.Count -gt 0) { $listeners = $ipv4Listeners }
if ($listeners.Count -eq 0) { exit 2 }
foreach ($listener in $listeners) {
  $candidate = [int]$listener.OwningProcess
  if ($candidate -le 0) { exit 3 }
  $seen = @{}
  $owned = $false
  while ($candidate -gt 0 -and -not $seen.ContainsKey($candidate)) {
    if ($candidate -eq [int]$env:ANYTHINGLLM_SERVER_ROOT_PID) { $owned = $true; break }
    $seen[$candidate] = $true
    try { $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $candidate) -ErrorAction Stop } catch { exit 3 }
    if ($null -eq $process) { exit 3 }
    $candidate = [int]$process.ParentProcessId
  }
  if (-not $owned) { Write-Output 'foreign'; exit 1 }
}
Write-Output 'owned'
exit 0
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServerOwnershipProbeError(
            "Windows could not complete the port-ownership check; no process was stopped."
        ) from exc
    if result.returncode == 2:
        return None
    if result.returncode == 0 and (result.stdout or "").strip().casefold() == "owned":
        return True
    if result.returncode == 1 and (result.stdout or "").strip().casefold() == "foreign":
        return False
    raise ServerOwnershipProbeError("Windows returned an inconclusive port-ownership result; no process was stopped.")


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
        command = _owned_server_process_command(pid)
        if command is None:
            raise ServerOwnershipProbeError("Windows could not verify the recorded server process; retry Start.")
        command_line = command.casefold()
        if not any(token in command_line for token in ("anythingllm_pdf_assistant_cli", "anythingllm-pdf-assistant")):
            return False
        if record.get("process_creation_ticks") is not None:
            with _pinned_server_process(pid, record["process_creation_ticks"]):
                return _listener_belongs_to_server_root(port, pid) is not False
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
        print("Portable installation is incomplete:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("Portable installation is ready.")
    print(f"Data directory: {paths['root']}")
    print(f"Output directory: {paths['outputs']}")
    return 0


def _server_marker_path() -> Path:
    return ensure_application_directories()["config"] / SERVER_MARKER_NAME


def _read_json_object(path: Path) -> dict:
    """Read one local JSON object, treating absent/corrupt state as unusable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomically(path: Path, payload: dict) -> None:
    """Publish a recovery snapshot without exposing a partial JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _run_server_root_pid(progress: dict) -> int:
    try:
        return int(progress.get("server_root_pid") or 0)
    except (TypeError, ValueError):
        return 0


def _owned_active_run_roots(server_root_pid: int) -> list[Path]:
    """Return only non-terminal runs explicitly owned by this server root.

    Older run records have no owner PID and are deliberately excluded: a Stop
    shortcut must never reinterpret an unknown historical run as its own.
    """
    output_root = application_paths()["automatic_outputs"]
    try:
        candidates = [path for path in output_root.iterdir() if path.is_dir()]
    except OSError:
        return []
    owned: list[Path] = []
    for run_root in candidates:
        progress = _read_json_object(run_root / AUTOMATIC_RUN_PROGRESS_NAME)
        if str(progress.get("state") or "").casefold() not in {"running", "preparing"}:
            continue
        if _run_server_root_pid(progress) == int(server_root_pid):
            owned.append(run_root)
    return owned


def _write_server_stop_recovery(run_root: Path, server_root_pid: int, *, terminal: bool) -> None:
    """Persist one owned run's stop evidence before or after server teardown."""
    progress_path = run_root / AUTOMATIC_RUN_PROGRESS_NAME
    progress = _read_json_object(progress_path)
    now = time.time()
    reason = "owned_local_server_stop"
    marker_payload = {
        "requested_at": now,
        "reason": reason,
        "server_root_pid": int(server_root_pid),
    }
    _write_json_atomically(run_root / AUTOMATIC_RUN_CANCELLATION_MARKER, marker_payload)
    recovery_payload = {
        "schema_version": 1,
        "status": "cancelled" if terminal else "server_stop_requested",
        "reason": reason,
        "server_root_pid": int(server_root_pid),
        "requested_at_epoch": now,
        "cancelled_at_epoch": now if terminal else 0.0,
        "phase": str(progress.get("phase") or "Working"),
        "confirmed_fraction": progress.get("confirmed_fraction"),
        "local_result": "The local assistant server stopped the active worker.",
        "anythingllm_result": (
            "No later PDF was submitted. A previously accepted AnythingLLM queue group may still finish "
            "externally; exact vector confirmation remains unresolved."
        ),
        "resume_guidance": (
            "Inspect this run before retrying unresolved sources. Only exact vector evidence establishes a completed source."
        ),
    }
    _write_json_atomically(run_root / AUTOMATIC_RUN_CANCELLATION_RECOVERY, recovery_payload)
    if not terminal:
        return
    if not progress:
        raise OSError(f"Owned run has no readable progress record: {run_root}")
    progress.update(
        {
            "state": "cancelled",
            "phase": "Run stopped because the local assistant server stopped",
            "details": (
                "The local assistant server stopped the active worker. No later PDF was submitted; "
                "previously accepted AnythingLLM work may still need exact vector confirmation."
            ),
            "cancel_requested": True,
            "cancel_available": False,
            "confirmation_in_flight": False,
            "activity_observed": False,
            "finished_epoch": now,
            "updated_epoch": now,
            "server_stop_reason": reason,
        }
    )
    _write_json_atomically(progress_path, progress)


def _prepare_owned_active_runs_for_server_stop(server_root_pid: int) -> list[Path]:
    """Request cancellation durably before force-stopping an owned server tree."""
    run_roots = _owned_active_run_roots(server_root_pid)
    for run_root in run_roots:
        _write_server_stop_recovery(run_root, server_root_pid, terminal=False)
    return run_roots


def _finalize_owned_runs_after_server_stop(run_roots: list[Path], server_root_pid: int) -> None:
    for run_root in run_roots:
        _write_server_stop_recovery(run_root, server_root_pid, terminal=True)


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
    if sys.platform == "win32":
        with _pinned_server_process(root_pid) as ticks:
            payload["process_creation_ticks"] = str(ticks)
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
    # Stop is an explicitly visible interactive shortcut: leave failures on
    # screen rather than hiding the very explanation the user needs.
    if action == "stop":
        command = ("stop; $assistantExitCode = $LASTEXITCODE; "
                   "if ($assistantExitCode -eq 0) { Start-Sleep -Seconds 2 } "
                   "else { Read-Host 'Press Enter to close' }; exit $assistantExitCode")
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
        f"-NoProfile -ExecutionPolicy Bypass -WindowStyle {'Normal' if action == 'stop' else 'Hidden'} -Command "
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
            "ANYTHINGLLM_SHORTCUT_WINDOW_STYLE": "1" if path.name == STOP_SHORTCUT_NAME else "7",
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
            "$shortcut.WindowStyle = [int]$env:ANYTHINGLLM_SHORTCUT_WINDOW_STYLE",
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
    # Start must not publish a replacement marker between termination and
    # cleanup. Use the same ownership mutex, not a separate competing lock.
    try:
        record = json.loads(_server_marker_path().read_text(encoding="utf-8"))
        port = int(record.get("port") or 0)
        with _server_start_ownership_lock(port):
            return _stop_under_start_lock()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"Could not resolve local server Stop: {exc}", file=sys.stderr)
        return 1


def _stop_under_start_lock() -> int:
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
    command = _owned_server_process_command(pid)
    if command is None:
        print("Could not verify the recorded local server process; refusing to stop it and keeping its marker for diagnosis.", file=sys.stderr)
        return 1
    command_line = command.casefold()
    if not any(token in command_line for token in ("anythingllm_pdf_assistant_cli", "anythingllm-pdf-assistant")):
        # A process can disappear between the shortcut reading its marker and
        # this ownership check.  When the recorded port is also free, keeping
        # the stale marker only makes the next Start/Stop action misleading.
        # Never remove it when something is listening: that could be a PID
        # reuse or an unrelated application and must remain diagnostic-only.
        if _port_is_available(port):
            marker.unlink(missing_ok=True)
            print("Removed a stale local PDF assistant server marker; no owned server is running.")
            return 0
        print("The recorded process is no longer the owned PDF assistant server; refusing to stop it.", file=sys.stderr)
        return 1
    try:
        with _pinned_server_process(pid, record.get("process_creation_ticks")):
            # Re-read under the retained handle: the initial read above is
            # useful for stale-marker cleanup but does not authorize a kill.
            if _owned_server_process_command(pid) != command:
                raise ServerOwnershipProbeError("The server command changed during verification.")
            return _stop_pinned_server(marker, record, pid, port)
    except (ServerOwnershipProbeError, OSError) as exc:
        print(f"Could not retain server ownership: {exc}", file=sys.stderr)
        return 1


def _stop_pinned_server(marker, record, pid, port):
    # The caller must retain the root process handle until this returns.
    # Preserve all existing tree-stop and cancellation-recovery semantics.
    try:
        if json.loads(marker.read_text(encoding="utf-8")) != record:
            raise ServerOwnershipProbeError("The server marker changed; retry Stop.")
    except (OSError, ValueError) as exc:
        raise ServerOwnershipProbeError("Cannot recheck the server marker; retry Stop.") from exc
    try:
        listener_owned = _listener_belongs_to_server_root(port, pid)
    except ServerOwnershipProbeError as exc:
        print(f"Could not verify server ownership: {exc} Please retry Stop.", file=sys.stderr)
        return 1
    if listener_owned is False:
        print("Port ownership does not match the recorded local PDF assistant; refusing to stop it.", file=sys.stderr)
        return 1
    try:
        stopped_runs = _prepare_owned_active_runs_for_server_stop(pid)
    except OSError as exc:
        print(
            "Could not retain active-run cancellation recovery; refusing to stop the owned server: "
            f"{exc}",
            file=sys.stderr,
        )
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
    try:
        _finalize_owned_runs_after_server_stop(stopped_runs, pid)
    except OSError as exc:
        print(
            "The owned server stopped, but final cancellation recovery could not be retained: "
            f"{exc}",
            file=sys.stderr,
        )
        return 1
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _port_is_available(port):
            marker.unlink(missing_ok=True)
            suffix = f" Preserved cancellation recovery for {len(stopped_runs)} active run(s)." if stopped_runs else ""
            print(f"Stopped the owned local PDF assistant server.{suffix}")
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
            already_running = _recorded_server_is_alive_on_port(port)
            if not already_running:
                if _doctor(port, allow_owned_running_server=False) != 0:
                    raise RuntimeError("The local port or data directories are unavailable. No existing server was stopped. Run the assistant doctor command for details.")
                os.environ.setdefault(SERVER_ROOT_PID_ENV, str(os.getpid()))
                marker = _write_server_marker(port)
    except RuntimeError as exc:
        message = f"Could not claim local server startup ownership: {exc}"
        if browser:
            _notify_launcher_message(message)
        else:
            print(message, file=sys.stderr)
        return 1
    if already_running:
        if browser:
            # Do not hold the startup mutex while waiting for readiness or
            # acknowledgement of a timeout notice. Keep this short-lived
            # process alive until its notification has actually been delivered.
            _open_browser_when_local_app_is_ready(port).join()
        print(f"The owned local PDF assistant is already starting or serving at {_local_app_url(port)}")
        return 0
    # Shortcut creation belongs to install/repair, not to every launch. The
    # old per-start Desktop/PowerShell lookup added avoidable startup work and
    # could contend with Explorer while the shortcut was itself being opened.
    # Claim ownership before importing the large Gradio application. A second
    # Desktop click can otherwise arrive during that import while neither a
    # listener nor a marker exists, causing two full servers to race for the
    # same port.
    cancelled = threading.Event()
    loaded = threading.Event()
    if browser:
        _open_browser_when_local_app_is_ready(port, cancelled=cancelled, loaded=loaded)
    try:
        os.environ["GRADIO_SERVER_PORT"] = str(port)
        # This is a local document assistant.  Opening the browser is only a
        # UI convenience; it must never decide whether Gradio telemetry is
        # enabled.
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
        from rag_pdf_gradio_app import launch_application
        loaded.set()
        # Browser opening is owned by the readiness watcher above. In
        # particular, do not rely on Gradio to open a tab from a hidden
        # shortcut process after the relatively heavy app build completes.
        launch_application(port=port, inbrowser=False)
    except Exception as exc:
        cancelled.set()
        message = f"PDF assistant startup/server failed ({type(exc).__name__}): {exc}"
        if browser:
            _notify_launcher_message(message)
        else:
            print(message, file=sys.stderr)
        return 1
    finally:
        cancelled.set()
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
        try:
            return _doctor(args.port)
        except ServerOwnershipProbeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
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
