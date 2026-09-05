"""Process-boundary acceptance for loss of the Gradio orchestration parent.

This runner starts the real Automatic local-preparation route in a subprocess,
waits until that parent owns a real ``cancellable_preparation_worker`` child,
terminates only the parent, and then judges the retained child result from
disk. Its scope is intentionally narrow: it certifies survival of the active
PDF source, not automatic continuation of later PDFs in the batch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_parent_loss_acceptance_v1"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _wait_for_first(root: Path, name: str, deadline: float) -> Path | None:
    while time.monotonic() < deadline:
        paths = sorted(root.rglob(name))
        if paths:
            return paths[0]
        time.sleep(0.1)
    return None


def launch_parent(pdf_path: str | Path, output_root: str | Path, run_root: str | Path) -> int:
    """Run the same orchestration body a Gradio request would own."""
    import rag_pdf_gradio_app as app

    pdf = str(Path(pdf_path).resolve(strict=True))
    settings = app.fresh_automatic_run_setting_values([pdf], [])
    settings.update({
        "pdf_files": [pdf],
        "folder_pdf_files": [],
        "mode": app.MODE_LOCAL_ONLY_LABEL,
        "output_root_override": str(Path(output_root).resolve()),
        "run_root_override": str(Path(run_root).resolve()),
        "retain_detailed_evidence": True,
        "first_page_override": 0,
        "end_page_override": 0,
    })
    app.run_automatic(**settings)
    return 0


def run_parent_loss_acceptance(
    pdf_path: str | Path,
    output_root: str | Path,
    *,
    marker_timeout: float = 120.0,
    result_timeout: float = 900.0,
) -> dict[str, Any]:
    pdf = Path(pdf_path).resolve(strict=True)
    root = Path(output_root).resolve()
    run_root = root / "parent-loss-run"
    run_root.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        "-m",
        "reliability_parent_loss_acceptance",
        "--launch-parent",
        "--pdf",
        str(pdf),
        "--output-root",
        str(root),
        "--run-root",
        str(run_root),
    ]
    # The intentionally surviving worker may inherit stderr. An undrained
    # pipe can both stall the parent and keep read() waiting after parent death.
    stderr_path = root / "parent-stderr.log"
    with stderr_path.open("wb") as stderr_log:
        parent = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
        )
    marker = _wait_for_first(
        run_root,
        ".active-preparation-worker.json",
        time.monotonic() + max(1.0, marker_timeout),
    )
    if marker is None:
        try:
            parent.wait(timeout=1)
        except subprocess.TimeoutExpired:
            parent.terminate()
            parent.wait(timeout=10)
        with stderr_path.open("rb") as stderr_log:
            stderr_log.seek(max(0, stderr_path.stat().st_size - 12000))
            stderr = stderr_log.read(12000).decode("utf-8", errors="replace")
        report = {
            "schema": SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "fail",
            "stage": "worker_ownership",
            "parent_exit": parent.returncode,
            "stderr_tail": stderr,
            "active_source_survived_parent_loss": False,
            "remaining_batch_continuation_certified": False,
        }
        atomic_write_json(root / "parent-loss-acceptance.json", report)
        return report

    marker_record = _read_object(marker)
    child_pid = int(marker_record.get("pid") or 0)
    parent.terminate()
    try:
        parent.wait(timeout=15)
    except subprocess.TimeoutExpired:
        parent.kill()
        parent.wait(timeout=15)

    import rag_pdf_gradio_app as app

    child_alive_after_parent_loss = app.automatic_worker_is_live(run_root)
    result_path = _wait_for_first(
        run_root,
        ".automatic-worker-result.json",
        time.monotonic() + max(1.0, result_timeout),
    )
    result: dict[str, Any] = {}
    if result_path is not None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            result = _read_object(result_path)
            if str(result.get("status") or ""):
                break
            time.sleep(0.1)
    completed = str(result.get("status") or "") == "completed"
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if child_alive_after_parent_loss and completed else "fail",
        "scope": "active_source_result_survives_orchestration_parent_loss",
        "parent_exit": parent.returncode,
        "child_pid": child_pid,
        "child_alive_immediately_after_parent_loss": child_alive_after_parent_loss,
        "active_source_survived_parent_loss": completed,
        "worker_result_status": str(result.get("status") or "unavailable"),
        "worker_run_status": str(result.get("run_status") or "unavailable"),
        "worker_elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
        "remaining_batch_continuation_certified": False,
        "remaining_batch_continuation_limit": (
            "Only the active PDF worker survives. A server restart can observe "
            "its durable result, but does not automatically resume later PDFs."
        ),
        "run_root": str(run_root),
    }
    atomic_write_json(root / "parent-loss-acceptance.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Certify active-PDF durability after parent loss.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-root", default="")
    parser.add_argument("--launch-parent", action="store_true")
    args = parser.parse_args(argv)
    if args.launch_parent:
        if not args.run_root:
            parser.error("--run-root is required with --launch-parent")
        return launch_parent(args.pdf, args.output_root, args.run_root)
    report = run_parent_loss_acceptance(args.pdf, args.output_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
