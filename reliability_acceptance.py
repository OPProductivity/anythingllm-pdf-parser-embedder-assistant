"""Offline subprocess crash certification for durable source transactions.

This runner uses the production atomic JSON and append-only receipt writers.
Worker scenarios terminate with ``os._exit`` at deliberate checkpoints so the
parent can judge only retained evidence, just as it must after a real crash.
It never contacts AnythingLLM.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prepared_recovery import build_prepared_recovery_plan
from prepared_batch_recovery import write_prepared_batch_checkpoint
from reliability_audit import audit_run_directory
from run_control import atomic_write_json
from source_transaction_journal import (
    append_source_transaction_event,
    finalize_source_transaction_journal,
    initialize_source_transaction_journal,
)


ACCEPTANCE_SCHEMA = "anythingllm_pdf_assistant_offline_crash_acceptance_v1"
SOURCE_HASH = "a" * 64
LOCATIONS = ["custom-documents/p1.json", "custom-documents/p2.json"]
CRASH_EXIT_CODE = 91


def _transaction(state: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_index": 1,
        "source_count": 1,
        "source_key": "fixture-source",
        "source_path": "",
        "source_filename": "fixture.pdf",
        "source_sha256": SOURCE_HASH,
        "chunk_sources": ["page-parent://fixture::p1", "page-parent://fixture::p2"],
        "planned_records": 2,
        "state": state,
        "mutation_scope": "current_source",
    }
    if state == "exact_vectors_proven":
        row.update(uploaded=2, embedded=2, locations=list(LOCATIONS), errors=[])
    elif state == "source_rejected_without_remote_mutation":
        row.update(
            uploaded=0,
            embedded=0,
            locations=[],
            errors=[{"classification": "explicit_rejection"}],
            later_sources_released=True,
        )
    return row


def _append_ledger_state(root: Path, state: str) -> None:
    held = state in {"ambiguous_external_mutation_held", "global_run_hold"}
    ledger = root / "source-transaction-ledger.json"
    event_path = ledger.with_name("source-transaction-events.jsonl")
    if not ledger.exists():
        initialize_source_transaction_journal(
            ledger,
            workspace_slug="fixture-workspace",
            run_id=root.name,
            transaction_count=1,
        )
    append_source_transaction_event(
        event_path,
        _transaction(state),
        stopped_after_source_transaction=1 if held else None,
        stop_reason=state if held else "",
    )


def _finalize_ledger(root: Path, state: str) -> None:
    held = state in {"ambiguous_external_mutation_held", "global_run_hold"}
    finalize_source_transaction_journal(
        root / "source-transaction-ledger.json",
        workspace_slug="fixture-workspace",
        run_id=root.name,
        transaction_count=1,
        transactions=[_transaction(state)],
        stopped_after_source_transaction=1 if held else None,
        stop_reason=state if held else "",
    )


def _append_receipt(root: Path, state: str, *, location: str = "") -> None:
    # Import only inside the worker: this is the production fsync-backed JSONL
    # writer whose crash durability the scenario is certifying.
    from auto_anythingllm_pipeline import append_jsonl_receipt

    append_jsonl_receipt(root / "batch-submission-receipts.jsonl", {
        "recorded_at": datetime.now(UTC).isoformat(),
        "run_id": root.name,
        "correlation_id": "fixture-correlation",
        "workspace_slug": "fixture-workspace",
        "transport": "file_upload",
        "state": state,
        "pdf_sha256": SOURCE_HASH,
        "prepared_payload_sha256": "b" * 64,
        "chunk_source": "page-parent://fixture::p1",
        "http_status": 200 if state == "attached" else 422 if state == "rejected" else None,
        "document_location": location,
        "error": "",
        "next_check": "fixture",
    })


def _write_partial_embedding_ledger(root: Path, *, confirmed: int) -> None:
    """Retain a realistic non-terminal child ledger for observation crashes."""
    atomic_write_json(root / "batch-embedding-ledger.json", {
        "workspace_slug": "fixture-workspace",
        "requested": 2,
        "accepted": 2,
        "batches": [{
            "batch": 1,
            "requested": 2,
            "accepted": 2,
            "locations": list(LOCATIONS),
            "submission_state": "accepted",
            "verification": {
                "confirmed_locations": list(LOCATIONS[:confirmed]),
                "matching_vector_rows": confirmed,
            },
        }],
        "recovery": {
            "state": "resume_available",
            "remaining_locations": list(LOCATIONS[confirmed:]),
        },
    })


def _write_complete_terminal_evidence(root: Path, *, include_progress: bool) -> None:
    transaction = _transaction("exact_vectors_proven")
    atomic_write_json(root / "batch-native-upload-report.json", {
        "status": "complete", "uploaded": 2, "embedded": 2,
        "locations": list(LOCATIONS), "source_transactions": [transaction],
    })
    atomic_write_json(root / "batch-embedding-ledger.json", {
        "workspace_slug": "fixture-workspace", "requested": 2, "accepted": 2,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })
    if include_progress:
        atomic_write_json(root / "run-progress.json", {
            "state": "successful", "completed_units": 2, "total_units": 2,
        })


def _write_prepared_batch_boundary(root: Path, stage: str) -> None:
    """Create the real hashed pre-submission checkpoint used by the app."""
    source_root = root / "prepared-source"
    source_root.mkdir(parents=True, exist_ok=True)
    text_path = source_root / "fixture.txt"
    text_path.write_text("durable prepared fixture", encoding="utf-8")
    plan_path = source_root / "upload-plan.csv"
    plan_path.write_text(
        "filename,title,docAuthor,description,docSource,chunkSource,text_file\n"
        f"fixture.txt,fixture,,,local-pdf://sha256/{SOURCE_HASH},page-parent://fixture::p1,{text_path}\n",
        encoding="utf-8",
    )
    summary = {
        "pdf": str(root / "fixture.pdf"),
        "source_sha256": SOURCE_HASH,
        "output_root": str(source_root),
        "native_upload_plan": str(plan_path),
        "api_upload_status": "not_started",
    }
    atomic_write_json(source_root / "run-summary.json", summary)
    write_prepared_batch_checkpoint(
        root,
        [summary],
        total_sources=1,
        workspace_slug="fixture-workspace",
        api_url="http://127.0.0.1:3001/api",
        stage=stage,
    )


def checkpoint_worker(root: Path, scenario: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    if scenario == "crash_after_batch_preparation_complete":
        _write_prepared_batch_boundary(root, "preparation_complete")
        os._exit(CRASH_EXIT_CODE)
    if scenario == "crash_after_batch_submission_started_before_source_ledger":
        _write_prepared_batch_boundary(root, "submission_started")
        os._exit(CRASH_EXIT_CODE)
    _append_ledger_state(root, "prepared")
    if scenario == "crash_after_prepared":
        os._exit(CRASH_EXIT_CODE)
    _append_ledger_state(root, "attachment_intent_durable")
    if scenario == "crash_after_intent":
        os._exit(CRASH_EXIT_CODE)
    if scenario == "crash_after_request_started":
        _append_receipt(root, "submitted")
        os._exit(CRASH_EXIT_CODE)
    if scenario == "crash_after_response_accepted":
        _append_receipt(root, "attached")
        os._exit(CRASH_EXIT_CODE)
    if scenario == "crash_after_first_workspace_link":
        _append_receipt(root, "attached_reconciled", location=LOCATIONS[0])
        _write_partial_embedding_ledger(root, confirmed=0)
        os._exit(CRASH_EXIT_CODE)
    if scenario == "crash_after_first_exact_vector":
        _append_receipt(root, "attached_reconciled", location=LOCATIONS[0])
        _write_partial_embedding_ledger(root, confirmed=1)
        os._exit(CRASH_EXIT_CODE)
    if scenario == "definite_rejection":
        _append_receipt(root, "rejected")
        _append_ledger_state(root, "source_rejected_without_remote_mutation")
        _finalize_ledger(root, "source_rejected_without_remote_mutation")
        atomic_write_json(root / "batch-native-upload-report.json", {
            "status": "error", "uploaded": 0, "embedded": 0, "locations": [],
        })
        atomic_write_json(root / "run-progress.json", {"state": "failed"})
        return 0
    if scenario in {
        "crash_after_all_exact_vectors",
        "crash_after_terminal_audit",
        "crash_after_terminal_progress",
        "exact_vectors_proven",
    }:
        _append_receipt(root, "attached", location=LOCATIONS[0])
        _append_ledger_state(root, "exact_vectors_proven")
        if scenario == "crash_after_all_exact_vectors":
            os._exit(CRASH_EXIT_CODE)
        _finalize_ledger(root, "exact_vectors_proven")
        _write_complete_terminal_evidence(
            root,
            include_progress=scenario in {"crash_after_terminal_progress", "exact_vectors_proven"},
        )
        if scenario == "crash_after_terminal_audit":
            atomic_write_json(root / "integrity-audit.json", audit_run_directory(root))
            os._exit(CRASH_EXIT_CODE)
        if scenario == "crash_after_terminal_progress":
            os._exit(CRASH_EXIT_CODE)
        return 0
    raise ValueError(f"Unknown checkpoint scenario: {scenario}")


SCENARIOS = {
    "crash_after_batch_preparation_complete": {"exit": CRASH_EXIT_CODE, "action": "safe_to_submit", "checkpoint": 1},
    "crash_after_prepared": {"exit": CRASH_EXIT_CODE, "action": "safe_to_submit", "checkpoint": 2},
    "crash_after_intent": {"exit": CRASH_EXIT_CODE, "action": "safe_to_submit", "checkpoint": 3},
    "crash_after_request_started": {"exit": CRASH_EXIT_CODE, "action": "hold_for_reconciliation", "checkpoint": 4},
    "crash_after_response_accepted": {"exit": CRASH_EXIT_CODE, "action": "hold_for_reconciliation", "checkpoint": 5},
    "crash_after_first_workspace_link": {"exit": CRASH_EXIT_CODE, "action": "hold_for_reconciliation", "checkpoint": 6},
    "crash_after_first_exact_vector": {"exit": CRASH_EXIT_CODE, "action": "hold_for_reconciliation", "checkpoint": 7},
    "crash_after_all_exact_vectors": {"exit": CRASH_EXIT_CODE, "action": "preserve_completed", "checkpoint": 8},
    "crash_after_terminal_audit": {"exit": CRASH_EXIT_CODE, "action": "preserve_completed", "checkpoint": 9},
    "crash_after_terminal_progress": {"exit": CRASH_EXIT_CODE, "action": "preserve_completed", "checkpoint": 10, "audit": "pass"},
    # Earlier coarse batch boundary remains a compatibility/regression case.
    "crash_after_batch_submission_started_before_source_ledger": {"exit": CRASH_EXIT_CODE, "action": "hold_for_reconciliation"},
    "definite_rejection": {"exit": 0, "action": "preserve_rejection_and_continue"},
    "exact_vectors_proven": {"exit": 0, "action": "preserve_completed", "audit": "pass"},
}


def run_offline_crash_acceptance(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for name, expected in SCENARIOS.items():
        scenario_root = root / name
        command = [
            sys.executable,
            "-m",
            "reliability_acceptance",
            "--checkpoint-worker",
            name,
            "--run-root",
            str(scenario_root),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
            recovery = build_prepared_recovery_plan(scenario_root)
            action = (recovery.get("sources") or [{}])[0].get("action", "")
            audit = audit_run_directory(scenario_root)
            passed = (
                completed.returncode == expected["exit"]
                and action == expected["action"]
                and (not expected.get("audit") or audit.get("audit_status") == expected["audit"])
            )
            results.append({
                "scenario": name,
                "status": "pass" if passed else "fail",
                "worker_exit": completed.returncode,
                "expected_exit": expected["exit"],
                "restart_action": action,
                "expected_action": expected["action"],
                "checkpoint": expected.get("checkpoint"),
                "integrity_audit": audit.get("audit_status"),
                "stderr_class": "present" if completed.stderr else "empty",
            })
        except subprocess.TimeoutExpired:
            results.append({
                "scenario": name,
                "status": "fail",
                "failure": "checkpoint_worker_timeout",
            })
    report = {
        "schema": ACCEPTANCE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if all(row["status"] == "pass" for row in results) else "fail",
        "scenario_count": len(results),
        "results": results,
    }
    atomic_write_json(root / "offline-crash-acceptance-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline PDF assistant crash certification.")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--checkpoint-worker", choices=tuple(SCENARIOS), default="")
    parser.add_argument("--run-root", default="")
    args = parser.parse_args(argv)
    if args.checkpoint_worker:
        if not args.run_root:
            parser.error("--run-root is required for a checkpoint worker")
        return checkpoint_worker(Path(args.run_root), args.checkpoint_worker)
    if args.output_root:
        report = run_offline_crash_acceptance(args.output_root)
    else:
        with tempfile.TemporaryDirectory(prefix="anythingllm-pdf-crash-acceptance-") as temp_dir:
            report = run_offline_crash_acceptance(temp_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
