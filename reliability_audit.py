"""Independent, read-only integrity audit for retained Automatic run evidence.

The normal pipeline writes each artifact for its own recovery purpose.  This
module deliberately does not import the Gradio application or trust its final
classification.  It reads the durable artifacts again and checks that their
counts and state transitions agree.  It never changes AnythingLLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = "anythingllm_pdf_assistant_reliability_audit_v2"
FAILURE_BUNDLE_DIRECTORY = "reliability-failures"
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
TERMINAL_STATES = frozenset({"successful", "warning", "failed", "cancelled"})
COMPLETE_UPLOAD_STATES = frozenset({"complete", "complete_with_key_cleanup_warning"})
PROVEN_SOURCE_STATE = "exact_vectors_proven"
REJECTED_SOURCE_STATE = "source_rejected_without_remote_mutation"
HELD_SOURCE_STATES = frozenset({"ambiguous_external_mutation_held", "global_run_hold"})


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    artifact: str = ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _canonical_count(mapping: dict[str, Any], canonical_key: str, *legacy_keys: str) -> int:
    """Read a canonical count without treating an explicit zero as absent."""
    values = mapping if isinstance(mapping, dict) else {}
    for key in (canonical_key, *legacy_keys):
        if key not in values or values.get(key) is None:
            continue
        return max(0, _safe_int(values.get(key)))
    return 0


def _audit_count_aliases(
    mapping: dict[str, Any], findings: list[Finding], artifact: str, *, scope: str
) -> None:
    """Fail closed if a retained canonical count contradicts its alias."""
    retained_conflicts = mapping.get("count_alias_conflicts") if isinstance(mapping, dict) else None
    if isinstance(retained_conflicts, list) and retained_conflicts:
        findings.append(Finding(
            "AUDIT-COUNT-ALIAS-001", "error",
            f"{scope} retained {len(retained_conflicts)} count-alias conflict(s) during schema normalisation.",
            artifact,
        ))
    for canonical_key, legacy_key in (
        ("newly_attached_records", "uploaded"),
        ("vector_confirmed_records", "confirmed_vector_records"),
        ("vector_confirmed_records", "embedded"),
    ):
        if (
            canonical_key in mapping and mapping.get(canonical_key) is not None
            and legacy_key in mapping and mapping.get(legacy_key) is not None
            and _safe_int(mapping.get(canonical_key)) != _safe_int(mapping.get(legacy_key))
        ):
            findings.append(Finding(
                "AUDIT-COUNT-ALIAS-001", "error",
                f"{scope} has conflicting {canonical_key} and legacy {legacy_key} values.",
                artifact,
            ))


def _audit_progress_trace(root: Path, findings: list[Finding]) -> dict[str, int | bool]:
    """Check every retained live x/y checkpoint, not only final status."""
    path = root / "progress-trace.jsonl"
    if not path.is_file():
        return {"present": False, "entries": 0, "count_violations": 0}
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            findings.append(Finding(
                "AUDIT-PROGRESS-TRACE-TOO-LARGE-001", "error",
                f"Progress trace exceeds the {MAX_ARTIFACT_BYTES}-byte audit safety limit.",
                path.name,
            ))
            return {"present": True, "entries": 0, "count_violations": 0}
        entries = violations = 0
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            entries += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                findings.append(Finding(
                    "AUDIT-PROGRESS-TRACE-UNREADABLE-001", "error",
                    f"Progress trace line {line_number} is not valid JSON.", path.name,
                ))
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("completed_units") is None or entry.get("total_units") is None:
                continue
            completed = _safe_int(entry.get("completed_units"))
            total = _safe_int(entry.get("total_units"))
            if total > 0 and completed > total:
                violations += 1
                findings.append(Finding(
                    "AUDIT-PROGRESS-TRACE-COUNT-001", "error",
                    f"Progress trace line {line_number} has impossible completed/total units ({completed}/{total}).",
                    path.name,
                ))
        return {"present": True, "entries": entries, "count_violations": violations}
    except (OSError, UnicodeError) as exc:
        findings.append(Finding(
            "AUDIT-PROGRESS-TRACE-UNREADABLE-001", "error",
            f"Progress trace cannot be read ({type(exc).__name__}).", path.name,
        ))
        return {"present": True, "entries": 0, "count_violations": 0}


def _identity(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _read_json(path: Path) -> tuple[dict[str, Any] | None, Finding | None]:
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            return None, Finding(
                "AUDIT-ARTIFACT-TOO-LARGE-001",
                "error",
                f"Artifact exceeds the {MAX_ARTIFACT_BYTES}-byte audit safety limit.",
                path.name,
            )
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top-level JSON value is not an object")
        return value, None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, Finding(
            "AUDIT-ARTIFACT-UNREADABLE-001",
            "error",
            f"Artifact is not a readable JSON object ({type(exc).__name__}).",
            path.name,
        )


def _add(
    findings: list[Finding],
    condition: bool,
    code: str,
    message: str,
    artifact: str,
    *,
    severity: str = "error",
) -> None:
    if condition:
        findings.append(Finding(code, severity, message, artifact))


def _audit_source_transactions(
    ledger: dict[str, Any], findings: list[Finding]
) -> dict[str, Any]:
    artifact = "source-transaction-ledger.json"
    transactions = ledger.get("transactions")
    if not isinstance(transactions, list):
        findings.append(Finding(
            "AUDIT-SOURCE-LEDGER-SHAPE-001", "error",
            "Source transaction ledger has no transaction list.", artifact,
        ))
        transactions = []
    transactions = [row for row in transactions if isinstance(row, dict)]
    declared = _safe_int(ledger.get("transaction_count"))
    indices = [_safe_int(row.get("source_index")) for row in transactions]
    states = [str(row.get("state") or "") for row in transactions]
    stop_index = _safe_int(ledger.get("stopped_after_source_transaction"))
    stop_reason = str(ledger.get("stop_reason") or "")

    _add(findings, declared < len(transactions), "AUDIT-SOURCE-COUNT-001",
         "More source transactions were retained than the declared source count.", artifact)
    _add(findings, indices != list(range(1, len(indices) + 1)), "AUDIT-SOURCE-ORDER-001",
         "Source transaction indices are duplicated, missing, or out of order.", artifact)
    _add(findings, any(not state for state in states), "AUDIT-SOURCE-STATE-001",
         "At least one source transaction has no state.", artifact)

    if stop_index:
        # A bounded queue group may prove earlier sources before a later source
        # becomes ambiguous.  The durable stop index therefore names the first
        # held source; only that source and any retained successors must remain
        # held.  Older all-held groups continue to satisfy the same rule.
        try:
            held_position = indices.index(stop_index)
        except ValueError:
            held_position = -1
        held_rows = transactions[held_position:] if held_position >= 0 else []
        held_group = (
            _safe_int(held_rows[0].get("source_queue_group_index"))
            if held_rows else 0
        )
        grouped_hold_is_coherent = bool(
            held_group
            and held_rows
            and all(
                _safe_int(row.get("source_queue_group_index")) == held_group
                and str(row.get("state") or "") in HELD_SOURCE_STATES
                for row in held_rows
            )
        )
        legacy_hold_is_coherent = bool(
            held_position == len(transactions) - 1
            and held_rows
            and str(held_rows[0].get("state") or "") in HELD_SOURCE_STATES
        )
        _add(findings, not (grouped_hold_is_coherent or legacy_hold_is_coherent),
             "AUDIT-SOURCE-STOP-001",
             "A held mutation may retain only held sources from its final queue group.", artifact)
        final_state = states[-1] if states else ""
        _add(findings, final_state not in HELD_SOURCE_STATES, "AUDIT-SOURCE-STOP-002",
             "The ledger records a stop boundary but its final source is not held.", artifact)
        _add(findings, not held_rows or any(
            str(row.get("state") or "") != stop_reason for row in held_rows
        ), "AUDIT-SOURCE-STOP-003",
             "The stop reason does not match every retained held source state.", artifact)
    else:
        _add(findings, any(state in HELD_SOURCE_STATES for state in states),
             "AUDIT-SOURCE-STOP-004",
             "A held source exists without a durable stop boundary.", artifact)
        _add(findings, bool(stop_reason), "AUDIT-SOURCE-STOP-005",
             "A stop reason exists without a stop boundary.", artifact)
        _add(findings, declared != len(transactions), "AUDIT-SOURCE-COUNT-002",
             "A non-held source ledger does not contain every declared transaction.", artifact)

    for row in transactions:
        index = _safe_int(row.get("source_index"))
        _audit_count_aliases(row, findings, artifact, scope=f"Source transaction {index}")
        state = str(row.get("state") or "")
        planned = _canonical_count(row, "selected_records", "planned_records")
        uploaded = _canonical_count(row, "newly_attached_records", "uploaded")
        embedded = _canonical_count(
            row, "vector_confirmed_records", "confirmed_vector_records", "embedded"
        )
        locations = [str(value) for value in (row.get("locations") or []) if str(value)]
        later_released = bool(row.get("later_sources_released"))
        suffix = f" Source {index}."
        if state == PROVEN_SOURCE_STATE:
            _add(findings, planned <= 0, "AUDIT-PROVEN-SOURCE-001",
                 "A proven source has no planned records." + suffix, artifact)
            _add(findings, not (planned == uploaded == embedded == len(locations)),
                 "AUDIT-PROVEN-SOURCE-002",
                 "A proven source does not reconcile planned, uploaded, embedded, and location counts." + suffix,
                 artifact)
            _add(findings, len(set(locations)) != len(locations), "AUDIT-PROVEN-SOURCE-003",
                 "A proven source contains duplicate document locations." + suffix, artifact)
            _add(findings, later_released, "AUDIT-PROVEN-SOURCE-004",
                 "A proven source incorrectly carries rejection-continuation evidence." + suffix, artifact)
        elif state == REJECTED_SOURCE_STATE:
            _add(findings, uploaded != 0 or embedded != 0 or bool(locations),
                 "AUDIT-REJECTED-SOURCE-001",
                 "A definitely rejected source nevertheless claims a remote mutation." + suffix, artifact)
            _add(findings, not later_released, "AUDIT-REJECTED-SOURCE-002",
                 "A definitely rejected source did not release later sources." + suffix, artifact)
        elif state in HELD_SOURCE_STATES:
            _add(findings, later_released, "AUDIT-HELD-SOURCE-001",
                 "A held external mutation incorrectly released later sources." + suffix, artifact)
        elif state in {"prepared", "attachment_intent_durable"}:
            findings.append(Finding(
                "AUDIT-INCOMPLETE-SOURCE-001", "error",
                "A non-active run retained an unfinished source transaction." + suffix, artifact,
            ))
        else:
            findings.append(Finding(
                "AUDIT-UNKNOWN-SOURCE-STATE-001", "error",
                "A source transaction uses an unknown terminal state." + suffix, artifact,
            ))

    return {
        "declared_sources": declared,
        "retained_sources": len(transactions),
        "state_counts": dict(sorted(Counter(states).items())),
        "planned_records": sum(
            _canonical_count(row, "selected_records", "planned_records") for row in transactions
        ),
        "newly_attached_records": sum(
            _canonical_count(row, "newly_attached_records", "uploaded") for row in transactions
        ),
        "confirmed_vector_records": sum(
            _canonical_count(
                row, "vector_confirmed_records", "confirmed_vector_records", "embedded"
            ) for row in transactions
        ),
        "location_count": sum(len(row.get("locations") or []) for row in transactions),
        "held_at_source": stop_index or None,
    }


def _audit_document_results(
    report: dict[str, Any], findings: list[Finding]
) -> dict[str, Any]:
    """Reconcile per-PDF selected, attached, existing, and confirmed counts."""
    raw = report.get("document_results")
    if not isinstance(raw, dict) or not raw:
        return {"present": False}
    rows = [value for value in raw.values() if isinstance(value, dict)]
    if len(rows) != len(raw):
        findings.append(Finding(
            "AUDIT-DOCUMENT-RESULT-SHAPE-001", "error",
            "At least one per-PDF result is not an object.",
            "batch-native-upload-report.json",
        ))
    selected = attached = existing = confirmed = 0
    inferred_existing_rows = 0
    for index, row in enumerate(rows, start=1):
        _audit_count_aliases(
            row, findings, "batch-native-upload-report.json", scope=f"PDF result {index}"
        )
        records = _canonical_count(row, "selected_records", "records")
        uploaded = _canonical_count(row, "newly_attached_records", "uploaded")
        embedded = _canonical_count(
            row, "vector_confirmed_records", "confirmed_vector_records", "embedded"
        )
        has_explicit_existing = "existing_workspace_records" in row
        existing_records = _safe_int(row.get("existing_workspace_records"))
        status = str(row.get("status") or "")
        classification = str(row.get("post_classification") or "")
        if (
            not has_explicit_existing
            and status in COMPLETE_UPLOAD_STATES
            and classification == "workspace_existing_content_skipped"
            and uploaded == 0
            and 0 <= embedded <= records
        ):
            # Older retained reports predate the explicit count field, but
            # this exact terminal classification was issued only after the
            # selected identities were observed in the workspace.
            existing_records = embedded
            inferred_existing_rows += 1
        suffix = f" PDF result {index}."
        _add(findings, min(records, uploaded, embedded, existing_records) < 0,
             "AUDIT-DOCUMENT-COUNT-001", "A per-PDF count is negative." + suffix,
             "batch-native-upload-report.json")
        _add(findings, uploaded > records,
             "AUDIT-DOCUMENT-COUNT-002", "Newly attached records exceed selected records." + suffix,
             "batch-native-upload-report.json")
        _add(findings, existing_records > records,
             "AUDIT-DOCUMENT-COUNT-003", "Existing-workspace records exceed selected records." + suffix,
             "batch-native-upload-report.json")
        _add(findings, embedded > records,
             "AUDIT-DOCUMENT-COUNT-004", "Confirmed selected records exceed selected records." + suffix,
             "batch-native-upload-report.json")
        _add(findings, uploaded + existing_records > records,
             "AUDIT-DOCUMENT-COUNT-005", "New and existing records overlap beyond the selected total." + suffix,
             "batch-native-upload-report.json")
        if status in COMPLETE_UPLOAD_STATES:
            _add(findings, records > 0 and embedded != records,
                 "AUDIT-DOCUMENT-COMPLETE-001",
                 "A completed PDF does not have exact confirmation for every selected record." + suffix,
                 "batch-native-upload-report.json")
            _add(findings, embedded != uploaded + existing_records,
                 "AUDIT-DOCUMENT-COMPLETE-002",
                 "A completed PDF's confirmed records are not partitioned into newly attached and already indexed records." + suffix,
                 "batch-native-upload-report.json")
        selected += records
        attached += uploaded
        existing += existing_records
        confirmed += embedded
    return {
        "present": True,
        "result_count": len(rows),
        "selected_records": selected,
        "newly_attached_records": attached,
        "existing_workspace_records": existing,
        "confirmed_vector_records": confirmed,
        "confirmed_records": confirmed,
        "inferred_existing_result_rows": inferred_existing_rows,
    }


def audit_run_directory(
    run_root: str | Path, *, terminal_state_override: str = ""
) -> dict[str, Any]:
    """Audit one retained run without writing to it or contacting AnythingLLM."""
    root = Path(run_root)
    names = (
        "run-progress.json",
        "batch-native-upload-report.json",
        "batch-embedding-ledger.json",
        "source-transaction-ledger.json",
        "resume-embedding-manifest.json",
    )
    artifacts: dict[str, dict[str, Any]] = {}
    findings: list[Finding] = []
    presence: dict[str, bool] = {}
    for name in names:
        path = root / name
        presence[name] = path.is_file()
        if path.is_file():
            value, error = _read_json(path)
            if error:
                findings.append(error)
            elif value is not None:
                artifacts[name] = value

    native_names = {
        "batch-native-upload-report.json",
        "batch-embedding-ledger.json",
        "source-transaction-ledger.json",
    }
    native_present = native_names.intersection(artifacts)
    progress = artifacts.get("run-progress.json", {})
    terminal_state = str(terminal_state_override or progress.get("state") or "")
    active = bool(terminal_state and terminal_state not in TERMINAL_STATES)
    if not native_present:
        status = "not_applicable" if not findings else "fail"
        return {
            "schema": AUDIT_SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "audit_status": status,
            "run_outcome": terminal_state or "unknown",
            "native_evidence_present": False,
            "artifact_presence": presence,
            "summary": {},
            "findings": [asdict(item) for item in findings],
        }

    report = artifacts.get("batch-native-upload-report.json", {})
    _audit_count_aliases(
        report, findings, "batch-native-upload-report.json", scope="Batch upload report"
    )
    report_status = str(report.get("status") or "")
    document_summary = _audit_document_results(report, findings)
    report_uploaded = _canonical_count(report, "newly_attached_records", "uploaded")
    report_embedded = _canonical_count(
        report, "vector_confirmed_records", "confirmed_vector_records", "embedded"
    )
    documented_existing = _safe_int(document_summary.get("existing_workspace_records"))
    documented_existing_only = bool(
        document_summary.get("present")
        and report_uploaded == 0
        and report_embedded > 0
        and report_embedded == documented_existing
        and report_embedded == _safe_int(document_summary.get("confirmed_vector_records"))
    )
    claimed_remote_records = max(
        report_uploaded,
        report_embedded,
        len(report.get("locations") or []),
    )
    # A definite pre-mutation rejection can truthfully end before an embedding
    # ledger exists. A complete or remotely-mutated run cannot. The separate
    # source ledger is expected only for the multi-source transaction route;
    # the established single-PDF route has one ordinary embedding ledger.
    required_native = {"batch-native-upload-report.json"}
    if (
        report_uploaded > 0
        or (claimed_remote_records and not documented_existing_only)
    ):
        required_native.add("batch-embedding-ledger.json")
    if report.get("source_transactions") or "source-transaction-ledger.json" in artifacts:
        required_native.add("source-transaction-ledger.json")
    missing_native = required_native.difference(artifacts)
    for name in sorted(missing_native):
        findings.append(Finding(
            "AUDIT-NATIVE-ARTIFACT-MISSING-001", "error",
            "A native run is missing a required reconciliation artifact.", name,
        ))
    if active:
        findings.append(Finding(
            "AUDIT-RUN-NOT-TERMINAL-001", "warning",
            "The run is still active; this audit is only a point-in-time snapshot.",
            "run-progress.json",
        ))

    source_summary: dict[str, Any] = {}
    source_ledger = artifacts.get("source-transaction-ledger.json")
    if source_ledger:
        source_summary = _audit_source_transactions(source_ledger, findings)

    embedding = artifacts.get("batch-embedding-ledger.json", {})
    report_locations = [str(value) for value in (report.get("locations") or []) if str(value)]
    _add(findings, len(set(report_locations)) != len(report_locations),
         "AUDIT-REPORT-LOCATION-001", "The upload report contains duplicate locations.",
         "batch-native-upload-report.json")
    _add(findings, report_uploaded != len(report_locations),
         "AUDIT-REPORT-COUNT-001", "Upload count does not equal the retained location count.",
         "batch-native-upload-report.json")
    _add(findings, report_embedded > report_uploaded + documented_existing,
         "AUDIT-REPORT-COUNT-002",
         "Confirmed selected records exceed newly attached plus explicitly documented existing-workspace records.",
         "batch-native-upload-report.json")
    if document_summary.get("present"):
        _add(findings, report_uploaded != _safe_int(document_summary.get("newly_attached_records")),
             "AUDIT-CROSS-DOCUMENT-001",
             "Batch newly attached count disagrees with the per-PDF results.",
             "batch-native-upload-report.json")
        _add(findings, report_embedded != _safe_int(document_summary.get("confirmed_vector_records")),
             "AUDIT-CROSS-DOCUMENT-002",
             "Batch confirmed count disagrees with the per-PDF results.",
             "batch-native-upload-report.json")

    requested = _safe_int(embedding.get("requested"))
    accepted = _safe_int(embedding.get("accepted"))
    recovery = embedding.get("recovery") if isinstance(embedding.get("recovery"), dict) else {}
    remaining = list(recovery.get("remaining_locations") or [])
    has_held_source = bool(source_summary.get("held_at_source"))
    queue_observations = embedding.get("progress_observations")
    if not isinstance(queue_observations, list):
        queue_observations = []
    externally_active_queue = False
    for observation in queue_observations:
        snapshot = (
            observation.get("final_queue_snapshot")
            if isinstance(observation, dict) else None
        )
        if not isinstance(snapshot, dict):
            continue
        total = _safe_int(snapshot.get("queue_records"))
        position = max(
            _safe_int(snapshot.get("desktop_queue_current")),
            _safe_int(snapshot.get("desktop_queue_completed")),
        )
        if (
            total > 0
            and 0 < position < total
            and str(snapshot.get("desktop_queue_observer_state") or "") == "connected"
        ):
            externally_active_queue = True
            break
    # A cooperative cancellation can land after AnythingLLM has attached a
    # source window but before every location in that window has exact vector
    # proof.  That is deliberately a recoverable boundary, not an internal
    # count contradiction: the durable report retains the attached total,
    # while the ledger retains only the exact confirmations and a manifest for
    # the held window.  Keep this narrowly scoped so a normal completed run
    # with mismatched totals still fails the audit.
    cancelled_pending_recovery = (
        terminal_state == "cancelled"
        and report_status == "reconciliation_pending"
        and has_held_source
        and str(recovery.get("state") or "") == "resume_available"
        and bool(remaining)
    )
    external_queue_pending_recovery = (
        report_status == "reconciliation_pending"
        and has_held_source
        and str(recovery.get("state") or "") == "resume_available"
        and bool(remaining)
        and externally_active_queue
    )
    # A durable held source means the external queue receipt is unresolved.
    # Attached-record totals can therefore be nonzero while exact vector proof
    # remains zero. That is a recovery boundary, not a contradiction: the
    # source ledger must still reconcile attached records with the native
    # report, but vector totals must be compared with vector totals below.
    held_pending_recovery = (
        report_status == "reconciliation_pending"
        and has_held_source
        and str(recovery.get("state") or "") == "resume_available"
        and bool(remaining)
    )
    pending_recovery_is_not_count_contradiction = held_pending_recovery
    _add(findings, accepted > requested, "AUDIT-EMBEDDING-COUNT-001",
         "Accepted embedding records exceed requested records.", "batch-embedding-ledger.json")
    _add(findings, requested and requested != report_uploaded, "AUDIT-CROSS-COUNT-001",
         "Embedding requested count and upload-report count disagree.", "batch-embedding-ledger.json")
    accepted_target = report_uploaded if document_summary.get("present") else report_embedded
    if cancelled_pending_recovery:
        findings.append(Finding(
            "AUDIT-CANCELLED-RECONCILIATION-001", "warning",
            "Cancellation retained a held AnythingLLM source window with incomplete exact vector confirmation; "
            "the recovery manifest must be reviewed before resuming.",
            "batch-embedding-ledger.json",
        ))
    elif external_queue_pending_recovery:
        findings.append(Finding(
            "AUDIT-EXTERNAL-QUEUE-OBSERVATION-001", "warning",
            "AnythingLLM was still processing this run's held page-parent records when assistant-side observation ended; exact vector evidence was incomplete.",
            "batch-embedding-ledger.json",
        ))
    elif report_status == "reconciliation_pending":
        # The receipt is explicitly unresolved, so its accepted counter is
        # necessarily a partial observation rather than a completed-run
        # invariant. Keep the discrepancy visible without falsely converting
        # a live/recoverable external queue into an internal integrity fault.
        findings.append(Finding(
            "AUDIT-RECONCILIATION-PENDING-COUNT-001", "warning",
            "AnythingLLM receipt reconciliation is still pending; accepted and submitted totals are not compared until the held queue group reaches a terminal observation.",
            "batch-embedding-ledger.json",
        ))
    else:
        _add(findings, accepted and accepted != accepted_target, "AUDIT-CROSS-COUNT-002",
             "Embedding accepted count disagrees with the records submitted by this run.", "batch-embedding-ledger.json")

    if source_summary:
        _add(findings, source_summary["newly_attached_records"] != report_uploaded,
             "AUDIT-CROSS-SOURCE-001",
             "Source transaction uploaded totals disagree with the batch report.",
             "source-transaction-ledger.json")
        # Source transactions cover only the current run's queue work. Fully
        # indexed PDFs are intentionally absent from that ledger, so subtract
        # their explicitly documented proof before comparing vector evidence.
        source_vector_target = max(0, report_embedded - documented_existing)
        _add(findings, source_summary["confirmed_vector_records"] != source_vector_target,
             "AUDIT-CROSS-SOURCE-002",
             "Source transaction vector totals disagree with the batch report's current-run confirmed-vector total.",
             "source-transaction-ledger.json")
        recovery_state = str(recovery.get("state") or "")
        _add(findings, has_held_source and recovery_state not in {
            "resume_available", "observation_required"
        },
             "AUDIT-RECOVERY-001", "A held source has no safe recovery or observation state.",
             "batch-embedding-ledger.json")
        all_proven = (
            source_summary["retained_sources"] > 0
            and source_summary["state_counts"] == {
                PROVEN_SOURCE_STATE: source_summary["retained_sources"]
            }
        )
        _add(findings, all_proven and (remaining or str(recovery.get("state") or "") != "not_needed"),
             "AUDIT-RECOVERY-002",
             "Every source is proven but the embedding ledger still claims recovery work.",
             "batch-embedding-ledger.json")
        _add(findings, all_proven and report_status not in COMPLETE_UPLOAD_STATES,
             "AUDIT-TERMINAL-CLASSIFICATION-001",
             "Every source is proven but the upload report is not complete.",
             "batch-native-upload-report.json")

    _add(findings, terminal_state == "successful" and report_status not in COMPLETE_UPLOAD_STATES,
         "AUDIT-TERMINAL-CLASSIFICATION-002",
         "The UI reports success although native upload evidence is incomplete.",
         "run-progress.json")
    completed_units = _safe_int(progress.get("completed_units"))
    total_units = _safe_int(progress.get("total_units"))
    _add(findings, total_units > 0 and completed_units > total_units,
         "AUDIT-PROGRESS-COUNT-001", "Completed progress units exceed total units.",
         "run-progress.json")
    progress_trace = _audit_progress_trace(root, findings)

    error_count = sum(1 for item in findings if item.severity == "error")
    warning_count = sum(1 for item in findings if item.severity == "warning")
    audit_status = (
        "fail" if error_count
        else "recovery_required" if pending_recovery_is_not_count_contradiction
        else "active" if active
        else "pass"
    )
    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "audit_status": audit_status,
        "run_outcome": terminal_state or "unknown",
        "native_evidence_present": True,
        "artifact_presence": presence,
        "summary": {
            "workspace_identity": _identity(
                source_ledger.get("workspace_slug") if source_ledger else embedding.get("workspace_slug")
            ),
            "source_transactions": source_summary,
            "upload_status": report_status,
            "selected_records": _canonical_count(report, "selected_records"),
            "selected_documents": _canonical_count(report, "selected_documents"),
            "cache_eligible_records": _canonical_count(report, "cache_eligible_records"),
            "cache_eligible_documents": _canonical_count(report, "cache_eligible_documents"),
            "newly_attached_records": report_uploaded,
            "queue_requested_records": _canonical_count(report, "queue_requested_records"),
            "queue_accepted_records": _canonical_count(report, "queue_accepted_records"),
            "queue_completed_records": _canonical_count(report, "queue_completed_records"),
            "existing_workspace_records": documented_existing,
            "vector_confirmed_records": report_embedded,
            "confirmed_vector_records": report_embedded,
            # Backward-compatible aliases for older report readers. New code
            # should use the explicit names above.
            "uploaded_records": report_uploaded,
            "confirmed_records": report_embedded,
            "count_semantics": {
                "newly_attached_records": "records attached to the workspace by this run",
                "vector_confirmed_records": "all selected records proven searchable, including safe reuse",
                "confirmed_vector_records": "legacy alias of vector_confirmed_records",
                "uploaded_records": "legacy alias of newly_attached_records",
                "confirmed_records": "legacy alias of confirmed_vector_records",
            },
            "document_results": document_summary,
            "progress_trace": progress_trace,
            "embedding_requested": requested,
            "embedding_accepted": accepted,
            "recovery_state": str(recovery.get("state") or ""),
            "recovery_remaining_records": len(remaining),
            "cancelled_reconciliation": {
                "required": cancelled_pending_recovery,
                "newly_attached_records": report_uploaded,
                "exactly_confirmed_records": report_embedded,
                "unresolved_records": max(0, report_uploaded - report_embedded),
                "held_source": source_summary.get("held_at_source"),
            },
            "external_queue_reconciliation": {
                "required": external_queue_pending_recovery,
                "queue_was_active": externally_active_queue,
                "newly_attached_records": report_uploaded,
                "exactly_confirmed_records": report_embedded,
                "unresolved_records": max(0, report_uploaded - report_embedded),
            },
            "error_findings": error_count,
            "warning_findings": warning_count,
        },
        "findings": [asdict(item) for item in findings],
    }


def prune_failure_bundles(
    run_root: str | Path, *, max_age_days: int = 14, max_count: int = 20
) -> list[str]:
    """Delete only expired assistant-owned audit bundles under one run root."""
    directory = Path(run_root) / FAILURE_BUNDLE_DIRECTORY
    if not directory.is_dir():
        return []
    now = time.time()
    cutoff = now - max(1, int(max_age_days)) * 86400
    candidates = sorted(
        (child for child in directory.iterdir() if child.is_dir() and child.name.startswith("audit-")),
        key=lambda child: child.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for index, child in enumerate(candidates):
        if index >= max(1, int(max_count)) or child.stat().st_mtime < cutoff:
            shutil.rmtree(child)
            removed.append(child.name)
    return removed


def write_failure_bundle(run_root: str | Path, audit: dict[str, Any]) -> Path | None:
    """Write a compact redacted bundle only when the integrity audit fails."""
    if str(audit.get("audit_status") or "") != "fail":
        return None
    root = Path(run_root)
    directory = root / FAILURE_BUNDLE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"audit-{stamp}-{os.getpid()}"
    target.mkdir(parents=False, exist_ok=False)
    compact = {
        "schema": AUDIT_SCHEMA,
        "generated_at": audit.get("generated_at"),
        "audit_status": audit.get("audit_status"),
        "run_outcome": audit.get("run_outcome"),
        "artifact_presence": audit.get("artifact_presence"),
        "summary": audit.get("summary"),
        "findings": audit.get("findings"),
    }
    (target / "integrity-audit.json").write_text(
        json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    prune_failure_bundles(root)
    return target
