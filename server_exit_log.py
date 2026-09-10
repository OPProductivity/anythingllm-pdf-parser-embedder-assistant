"""Rare, bounded lifecycle evidence; never controls server/process ownership."""

from datetime import datetime, UTC
import json
import os
from pathlib import Path
import threading
import traceback

from portable_paths import application_paths
from process_lock import named_process_lock

_LOCK = threading.Lock()
MAX_BYTES = 128 * 1024


def record_server_event(event, *, record=None, error=None, **facts):
    """Best effort, without tokens, command lines or exception messages.

    A killed process cannot report its reason or exit code. A marker found on
    next Start is discovery evidence, not a timestamp/cause of the old exit.
    Rotation and writes share one short cross-process lock, including Stop.
    """
    try:
        payload = {
            "schema_version": 1,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event, "reporter_pid": os.getpid(), **facts,
        }
        if record:
            payload["server"] = {key: record[key] for key in (
                "pid", "root_pid", "port", "started_at", "process_creation_ticks",
            ) if key in record}
        if error is not None:
            payload["exception_type"] = type(error).__name__
            payload["frames"] = [
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(error.__traceback__)[-6:]
            ]
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        path = application_paths()["logs"] / "server-lifecycle.jsonl"
        if not _LOCK.acquire(timeout=0.1):
            return
        try:
            with named_process_lock("server-lifecycle-log", str(path.resolve()), timeout_seconds=0.1):
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size + len(data.encode("utf-8")) > MAX_BYTES:
                    os.replace(path, path.with_suffix(".jsonl.1"))
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(data)
        finally:
            _LOCK.release()
    except Exception:
        # Unavailable diagnostics must not break Start or Stop.
        pass
