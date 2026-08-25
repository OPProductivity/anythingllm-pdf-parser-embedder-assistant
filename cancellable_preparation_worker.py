"""Owned child worker for one cancellable Automatic PDF preparation.

The Gradio process deliberately keeps ownership of the UI and durable run
status.  This worker owns only the expensive one-document pipeline.  Running
that pipeline out of process is what makes a Windows ``taskkill /T`` a real
cancel operation rather than an optimistic browser-state change.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from automatic_worker_protocol import AUTOMATIC_WORKER_TRANSPORT_ARTIFACTS
from auto_anythingllm_pipeline import prepare_pdf
from orchestration import execute_preparation, legacy_summary_from_run


_EVENT_WRITE_LOCK = threading.Lock()
_RESULT_WRITE_LOCK = threading.Lock()


def _write_heartbeat(path: Path, payload: dict) -> None:
    """Atomically refresh ownership evidence without pretending work advanced.

    Some native/OCR calls can run for minutes without a progress callback.  A
    durable heartbeat lets a newly started Gradio server recognise that the
    child is still the owned worker for this run.  It is deliberately separate
    from the visible progress stream: a liveness pulse must not make an
    extraction look as if it completed useful work.
    """
    _write_json(path, payload)
def _write_json(path: Path, payload: dict) -> None:
    # Windows can reject simultaneous replacements of one result path even
    # with separate temporary files. The terminal record is tiny, so this
    # lock avoids that race without serialising OCR or upload work. Antivirus
    # and indexing can still hold the old destination briefly, so retain the
    # same bounded sharing-violation retry used for durable run controls.
    with _RESULT_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(3):
                try:
                    os.replace(temporary, path)
                    temporary = None
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.08 * (attempt + 1))
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _emit_event(
    path: Path,
    value: float,
    stage: str,
    *,
    desktop_required: bool = False,
    phase: str = "",
    completed_units=None,
    total_units=None,
    evidence_kind: str = "",
) -> None:
    # The main pipeline and the read-only Desktop SSE observer can both relay
    # status into this file.  Serialise writes so the Gradio owner never sees
    # a torn JSON line while it polls the worker event stream.
    with _EVENT_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "recorded_at": time.time(),
                        # ``time.monotonic`` is comparable across local
                        # Windows processes for one boot.  Keep it beside the
                        # audit-friendly wall clock so benchmark attribution
                        # never depends on a clock adjustment.
                        "recorded_monotonic": time.monotonic(),
                        "value": float(value),
                        "stage": str(stage),
                        "desktop_required": bool(desktop_required),
                        "phase": str(phase or ""),
                        "completed_units": completed_units,
                        "total_units": total_units,
                        "evidence_kind": str(evidence_kind or ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()


def _emit_timing_event(path: Path, stage: str, event=None, **details) -> None:
    """Persist the pipeline timing payload without narrowing its schema.

    The canonical pipeline emits detailed ``timing_event`` payloads.  The
    former worker adapter accepted only a positional ``batch_report`` and
    silently discarded keyword-rich phase evidence, leaving live runs with
    less timing data than the Gradio production path.  Preserve every
    JSON-safe detail so the benchmark observes the same orchestration route.
    """
    payload = dict(event) if isinstance(event, dict) else {}
    payload.update(details)
    with _EVENT_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "recorded_at": time.time(),
                        "recorded_monotonic": time.monotonic(),
                        "type": "timing",
                        "stage": str(stage),
                        "batch_report": payload,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            handle.flush()


def main(config_path: str) -> int:
    config_file = Path(config_path)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    run_root = Path(config["run_root"])
    result_path = Path(config["result_path"])
    events_path = Path(config["events_path"])
    heartbeat_path = run_root / ".automatic-worker-heartbeat.json"
    cancel_marker = run_root / ".cancel-requested.json"
    argument_values = dict(config.get("args") or {})
    # The parent deliberately keeps credentials out of the durable JSON
    # contract. Consume the one-child environment value before invoking the
    # pipeline, then remove it from this process environment as well.
    api_key_env = str(config.get("anythingllm_api_key_env") or "").strip()
    if api_key_env:
        argument_values["anythingllm_api_key"] = os.environ.pop(api_key_env, "")
    else:
        # Preserve the runner's established namespace contract without
        # serialising even an empty credential field into run artifacts.
        argument_values.setdefault("anythingllm_api_key", "")
    args = SimpleNamespace(**argument_values)
    # The Gradio parent is still reading these files while this child prepares
    # the document.  Ask the compact-output routine to leave them alone; the
    # parent removes them only after this process has exited.
    args.retain_generated_children_until_worker_exit = AUTOMATIC_WORKER_TRANSPORT_ARTIFACTS
    def emit_progress(value, stage, desktop_required=False, **metadata):
        heartbeat_state["stage"] = str(stage or "Preparing PDF")
        _emit_event(
            events_path, value, stage, desktop_required=desktop_required, **metadata
        )

    args.progress_callback = emit_progress
    args.timing_event_callback = lambda stage, event=None, **details: _emit_timing_event(
        events_path, stage, event, **details
    )
    args.cancel_callback = lambda: cancel_marker.is_file()
    if cancel_marker.is_file():
        _write_json(result_path, {"status": "cancelled", "message": "Stop requested before worker start."})
        return 0
    started = time.time()
    heartbeat_stop = threading.Event()
    heartbeat_state = {"stage": "Preparing PDF"}

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(15.0):
            try:
                _write_heartbeat(
                    heartbeat_path,
                    {
                        "pid": os.getpid(),
                        "run_root": str(run_root),
                        "recorded_at": time.time(),
                        "stage": str(heartbeat_state.get("stage") or "Preparing PDF"),
                    },
                )
            except OSError:
                # The parent retains process ownership even if the output
                # drive becomes temporarily unavailable.  Never turn a
                # heartbeat write problem into a silent worker crash.
                pass

    try:
        _write_heartbeat(
            heartbeat_path,
            {
                "pid": os.getpid(),
                "run_root": str(run_root),
                "recorded_at": started,
                "stage": "Preparing PDF",
            },
        )
    except OSError:
        pass
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        name="automatic-preparation-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    _emit_event(
        events_path,
        0.0,
        "Preparing PDF",
        phase="worker_lifecycle",
        evidence_kind="worker_started",
    )
    try:
        controlled = execute_preparation(
            Path(config["pdf_path"]),
            Path(config["output_dir"]),
            args,
            prepare_pdf,
        )
        summary = legacy_summary_from_run(controlled)
        _write_json(
            result_path,
            {
                "status": "cancelled" if cancel_marker.is_file() else "completed",
                "run_status": str(controlled.status),
                "operator_summary": str(controlled.operator_summary),
                "summary": summary,
                "run_control": controlled.to_dict(),
                "batch_inspection_context": getattr(args, "batch_inspection_context", {}),
                "elapsed_seconds": round(time.time() - started, 3),
            },
        )
        _emit_event(
            events_path,
            1.0,
            "PDF preparation finished",
            phase="worker_lifecycle",
            completed_units=1,
            total_units=1,
            evidence_kind="worker_finished",
        )
        return 0
    except BaseException as exc:
        _write_json(
            result_path,
            {
                "status": "cancelled" if cancel_marker.is_file() else "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.time() - started, 3),
            },
        )
        _emit_event(
            events_path,
            0.0,
            "Worker failed",
            phase="worker_lifecycle",
            completed_units=0,
            total_units=1,
            evidence_kind="worker_failed",
        )
        return 1
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
