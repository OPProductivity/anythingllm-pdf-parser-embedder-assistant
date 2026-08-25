"""Read-only restart decisions for interrupted per-PDF source transactions."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from prepared_batch_recovery import verify_prepared_batch_checkpoint


RECOVERY_SCHEMA = "anythingllm_pdf_assistant_prepared_recovery_v1"
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
AMBIGUOUS_RECEIPT_STATES = frozenset({
    "submitted",
    "submission_unknown",
    "attached",
    "attached_reconciled",
    "reused_cached_location",
})
SAFE_REJECTED_RECEIPT_STATES = frozenset({"rejected"})


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def _read_receipts(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        return [], ["submission_receipt_file_too_large"]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("receipt is not an object")
                    rows.append(row)
                except (json.JSONDecodeError, ValueError):
                    errors.append(f"malformed_submission_receipt_line:{line_number}")
    except (OSError, UnicodeError) as exc:
        errors.append(f"submission_receipt_read_error:{type(exc).__name__}")
    return rows, errors


def _prepared_batch_only_recovery_plan(root: Path) -> dict[str, Any]:
    """Classify the earlier prepared-batch boundary when no source ledger exists.

    A complete ``preparation_complete`` checkpoint predates every external
    mutation and can therefore release only its upload-bearing sources.  Once
    ``submission_started`` is durable, absence of the finer source ledger is
    uncertainty, never evidence that nothing happened.
    """
    verification = verify_prepared_batch_checkpoint(root)
    if verification.get("status") == "not_available":
        return {
            "schema": RECOVERY_SCHEMA,
            "status": "not_available",
            "reason": "source_transaction_ledger_and_prepared_batch_checkpoint_missing",
            "sources": [],
            "automatic_submission_allowed": False,
        }

    stage = str(verification.get("stage") or "")
    reusable = bool(verification.get("reusable"))
    sources: list[dict[str, Any]] = []
    for row in verification.get("sources") or []:
        source_state = str(row.get("state") or "unknown")
        verified = bool(row.get("verified"))
        if not verified:
            action = "hold_for_reconciliation"
            reason = "prepared_artifact_verification_failed"
        elif reusable and source_state == "prepared_for_submission":
            action = "safe_to_submit"
            reason = "complete_preparation_checkpoint_predates_external_mutation"
        elif reusable and source_state in {
            "selected_exact_duplicate",
            "workspace_existing_content",
        }:
            action = "preserve_non_mutating_skip"
            reason = "source_was_durably_classified_as_not_requiring_submission"
        elif reusable and source_state == "preparation_failed":
            action = "preserve_source_failure_and_continue"
            reason = "source_local_preparation_failure_is_durable"
        elif stage == "preparation_in_progress":
            action = "resume_local_preparation"
            reason = "external_submission_has_not_started_but_batch_is_incomplete"
        else:
            action = "hold_for_reconciliation"
            reason = (
                "prepared_batch_submission_may_have_started"
                if stage in {"submission_started", "submission_in_progress", "submission_terminal"}
                else "prepared_batch_checkpoint_is_not_reusable"
            )
        sources.append({
            "source_index": int(row.get("source_index") or 0),
            "source_identity": str(row.get("source_identity") or "unavailable"),
            "planned_records": 0,
            "durable_state": f"prepared_batch:{source_state}",
            "receipt_states": [],
            "action": action,
            "reason": reason,
        })

    action_counts: dict[str, int] = defaultdict(int)
    for source in sources:
        action_counts[source["action"]] += 1
    has_hold = any(source["action"] == "hold_for_reconciliation" for source in sources)
    automatic_submission_allowed = (
        reusable
        and not has_hold
        and any(source["action"] == "safe_to_submit" for source in sources)
    )
    return {
        "schema": RECOVERY_SCHEMA,
        "status": "ready" if reusable and not has_hold else "blocked",
        "reason": str(verification.get("reason") or ""),
        "recovery_boundary": "prepared_batch_checkpoint",
        "checkpoint_stage": stage,
        "sources": sources,
        "action_counts": dict(sorted(action_counts.items())),
        "automatic_submission_allowed": automatic_submission_allowed,
    }


def build_prepared_recovery_plan(run_root: str | Path) -> dict[str, Any]:
    """Classify restart actions without submitting or changing any artifact.

    ``safe_to_submit`` is granted only when the durable source state proves no
    request intent reached the transport boundary. Once a submitted receipt
    exists, elapsed time can never make replay safe: exact external evidence
    must reconcile the source first.
    """
    root = Path(run_root)
    ledger_path = root / "source-transaction-ledger.json"
    receipt_path = root / "batch-submission-receipts.jsonl"
    if not ledger_path.is_file():
        return _prepared_batch_only_recovery_plan(root)
    try:
        ledger = _read_object(ledger_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "schema": RECOVERY_SCHEMA,
            "status": "blocked",
            "reason": f"source_transaction_ledger_unreadable:{type(exc).__name__}",
            "sources": [],
            "automatic_submission_allowed": False,
        }
    receipts, receipt_errors = _read_receipts(receipt_path)
    receipts_by_pdf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        receipts_by_pdf[str(receipt.get("pdf_sha256") or "")].append(receipt)

    sources: list[dict[str, Any]] = []
    global_hold = bool(receipt_errors)
    for row in ledger.get("transactions") or []:
        if not isinstance(row, dict):
            global_hold = True
            continue
        source_hash = str(row.get("source_sha256") or "")
        source_receipts = receipts_by_pdf.get(source_hash, []) if source_hash else []
        receipt_states = [str(item.get("state") or "") for item in source_receipts]
        state = str(row.get("state") or "")
        action = "hold_for_reconciliation"
        reason = "source_state_does_not_prove_safe_replay"
        if state == "exact_vectors_proven":
            action = "preserve_completed"
            reason = "exact_vectors_already_proven"
        elif state == "source_rejected_without_remote_mutation":
            action = "preserve_rejection_and_continue"
            reason = "definite_pre_mutation_rejection"
        elif state == "prepared":
            action = "safe_to_submit"
            reason = "no_attachment_intent_was_persisted"
        elif state == "attachment_intent_durable" and not source_receipts and not receipt_errors:
            # The production route durably appends ``submitted`` before the
            # HTTP function is entered. Intent without a receipt therefore
            # proves the transport boundary was not crossed.
            action = "safe_to_submit"
            reason = "intent_persisted_but_no_transport_receipt"
        elif receipt_states and set(receipt_states).issubset(SAFE_REJECTED_RECEIPT_STATES):
            action = "preserve_rejection_and_continue"
            reason = "all_transport_receipts_are_definite_rejections"
        elif any(value in AMBIGUOUS_RECEIPT_STATES for value in receipt_states):
            action = "hold_for_reconciliation"
            reason = "transport_may_have_mutated_anythingllm"
        elif state in {"ambiguous_external_mutation_held", "global_run_hold"}:
            action = "hold_for_reconciliation"
            reason = "durable_source_hold"

        if global_hold and action == "safe_to_submit":
            action = "hold_for_reconciliation"
            reason = "receipt_journal_is_not_trustworthy"
        sources.append({
            "source_index": int(row.get("source_index") or 0),
            "source_identity": (
                "sha256:" + source_hash[:16] if source_hash else "unavailable"
            ),
            "planned_records": int(row.get("planned_records") or 0),
            "durable_state": state,
            "receipt_states": sorted(set(receipt_states)),
            "action": action,
            "reason": reason,
        })

    action_counts: dict[str, int] = defaultdict(int)
    for source in sources:
        action_counts[source["action"]] += 1
    return {
        "schema": RECOVERY_SCHEMA,
        "status": "blocked" if global_hold else "ready",
        "reason": ";".join(receipt_errors),
        "sources": sources,
        "action_counts": dict(sorted(action_counts.items())),
        "automatic_submission_allowed": (
            not global_hold
            and any(source["action"] == "safe_to_submit" for source in sources)
            and all(source["action"] != "hold_for_reconciliation" for source in sources)
        ),
    }
