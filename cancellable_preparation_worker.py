"""Owned child worker for one cancellable Automatic PDF preparation.

The Gradio process deliberately keeps ownership of the UI and durable run
status.  This worker owns only the expensive one-document pipeline.  Running
that pipeline out of process is what makes a Windows ``taskkill /T`` a real
cancel operation rather than an optimistic browser-state change.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from auto_anythingllm_pipeline import prepare_pdf
from orchestration import execute_preparation, legacy_summary_from_run


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _emit_event(path: Path, value: float, stage: str, *, desktop_required: bool = False) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "value": float(value),
                    "stage": str(stage),
                    "desktop_required": bool(desktop_required),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()


def _emit_timing_event(path: Path, stage: str, batch_report=None) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"type": "timing", "stage": str(stage), "batch_report": batch_report or {}},
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
    cancel_marker = run_root / ".cancel-requested.json"
    args = SimpleNamespace(**dict(config.get("args") or {}))
    args.progress_callback = lambda value, stage, desktop_required=False: _emit_event(
        events_path, value, stage, desktop_required=desktop_required
    )
    args.timing_event_callback = lambda stage, batch_report=None: _emit_timing_event(
        events_path, stage, batch_report
    )
    args.cancel_callback = lambda: cancel_marker.is_file()
    if cancel_marker.is_file():
        _write_json(result_path, {"status": "cancelled", "message": "Stop requested before worker start."})
        return 0
    started = time.time()
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
