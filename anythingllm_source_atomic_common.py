"""Shared atomic storage and Desktop activation checks for embedding adapters."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE = 36


SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE = 64


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _desktop_root_started_after(
    executable: Path,
    not_before_epoch: float,
) -> tuple[bool | None, str]:
    """Return whether the exact Desktop root process is newer than a patch.

    The backend module is loaded by Desktop's Node service.  Updating its file
    while Desktop is already running does not establish that a running backend
    has reloaded it.  We therefore use the root process creation time only as
    an activation boundary: a fresh Desktop launch after the patch is required
    before the source-atomic path can be advertised as available.

    ``None`` is deliberately distinct from ``False``.  If Windows process
    inspection itself is unavailable, the caller fails closed instead of
    claiming activation from the file hash alone.
    """
    if os.name != "nt":
        return None, "desktop_restart_detection_requires_windows"
    quoted_executable = str(executable).replace("'", "''")
    command = (
        "$target='" + quoted_executable + "';"
        "$roots=Get-CimInstance Win32_Process -Filter \"Name='AnythingLLM.exe'\" | "
        "Where-Object { $_.ExecutablePath -eq $target -and $_.CommandLine -and $_.CommandLine -notmatch '(?:^|\\s)--type=' } | "
        # Get-CimInstance already materializes CreationDate as DateTime.  The
        # older ManagementDateTimeConverter expects a DMTF string and would
        # otherwise discard every live Desktop process.
        "ForEach-Object { ([datetime]$_.CreationDate).ToUniversalTime().ToString('o') };"
        "$roots | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"desktop_restart_detection_error:{type(exc).__name__}"
    if completed.returncode != 0:
        return None, "desktop_restart_detection_command_failed"
    raw = str(completed.stdout or "").strip()
    if not raw:
        return False, "anythingllm_desktop_not_running"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "desktop_restart_detection_invalid_output"
    starts = parsed if isinstance(parsed, list) else [parsed]
    try:
        started_epochs = [
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            for value in starts
            if str(value).strip()
        ]
    except (TypeError, ValueError):
        return None, "desktop_restart_detection_invalid_timestamp"
    if not started_epochs:
        return False, "anythingllm_desktop_not_running"
    return all(started >= float(not_before_epoch) for started in started_epochs), ""


def _activation_state_for_installed_worker(
    executable: Path | None,
    worker: Path,
    manifest: Path,
) -> tuple[bool, str, bool]:
    """Return ``(active, reason, restart_required)`` for a verified patch."""
    if executable is None or not executable.is_file():
        return False, "desktop_executable_missing_for_restart_check", False
    threshold = worker.stat().st_mtime
    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        threshold = float(manifest_payload.get("restart_required_since_epoch") or threshold)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        # The worker mtime is a conservative fallback for manifests written by
        # an earlier assistant version that lacked the activation marker.
        pass
    restarted, reason = _desktop_root_started_after(executable, threshold)
    if restarted is True:
        return True, "", False
    if restarted is False:
        return False, reason or "anythingllm_desktop_restart_required", True
    return False, reason or "desktop_restart_state_unknown", False
