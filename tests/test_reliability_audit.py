import json
import os
import time
from pathlib import Path

import pytest

from reliability_audit import (
    audit_run_directory,
    prune_failure_bundles,
    write_failure_bundle,
)


pytestmark = pytest.mark.offline_deterministic


def _write(path: Path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _green_run(root: Path):
    locations = ["custom-documents/a.json", "custom-documents/b.json"]
    _write(root / "run-progress.json", {
        "state": "warning",
        "completed_units": 2,
        "total_units": 2,
        "details": "Optional live retrieval was deferred.",
    })
    _write(root / "batch-native-upload-report.json", {
        "status": "complete", "uploaded": 2, "embedded": 2, "locations": locations,
    })
    _write(root / "batch-embedding-ledger.json", {
        "workspace_slug": "private workspace", "requested": 2, "accepted": 2,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })
    _write(root / "source-transaction-ledger.json", {
        "workspace_slug": "private workspace", "transaction_count": 1,
        "transactions": [{
            "source_index": 1, "source_count": 1, "source_path": r"C:\Private\paper.pdf",
            "planned_records": 2, "state": "exact_vectors_proven",
            "uploaded": 2, "embedded": 2, "locations": locations, "errors": [],
        }],
        "stopped_after_source_transaction": None, "stop_reason": "",
    })


def test_exact_vectors_with_optional_retrieval_deferral_passes_integrity_audit(tmp_path):
    _green_run(tmp_path)
    audit = audit_run_directory(tmp_path)
    assert audit["audit_status"] == "pass"
    assert audit["summary"]["confirmed_records"] == 2
    assert audit["findings"] == []


def test_single_source_route_does_not_require_multi_source_transaction_ledger(tmp_path):
    _green_run(tmp_path)
    (tmp_path / "source-transaction-ledger.json").unlink()
    audit = audit_run_directory(tmp_path)
    assert audit["audit_status"] == "pass"


def test_definite_pre_mutation_failure_does_not_require_embedding_ledger(tmp_path):
    _write(tmp_path / "run-progress.json", {"state": "failed"})
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "error_authentication_required",
        "uploaded": 0,
        "embedded": 0,
        "locations": [],
        "errors": [{"classification": "authentication_unavailable"}],
    })
    audit = audit_run_directory(tmp_path)
    assert audit["audit_status"] == "pass"


def test_success_cannot_hide_cross_ledger_count_contradictions(tmp_path):
    _green_run(tmp_path)
    report = json.loads((tmp_path / "batch-native-upload-report.json").read_text())
    report["embedded"] = 1
    _write(tmp_path / "batch-native-upload-report.json", report)
    progress = json.loads((tmp_path / "run-progress.json").read_text())
    progress["state"] = "successful"
    _write(tmp_path / "run-progress.json", progress)

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}
    assert audit["audit_status"] == "fail"
    assert "AUDIT-CROSS-COUNT-002" in codes
    assert "AUDIT-CROSS-SOURCE-002" in codes


def test_definite_rejection_releases_later_source_and_remains_consistent(tmp_path):
    _green_run(tmp_path)
    locations = ["custom-documents/b.json"]
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "error", "uploaded": 1, "embedded": 1, "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 1, "accepted": 1,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 2,
        "transactions": [
            {"source_index": 1, "planned_records": 1,
             "state": "source_rejected_without_remote_mutation", "uploaded": 0,
             "embedded": 0, "locations": [], "later_sources_released": True},
            {"source_index": 2, "planned_records": 1, "state": "exact_vectors_proven",
             "uploaded": 1, "embedded": 1, "locations": locations},
        ],
        "stopped_after_source_transaction": None, "stop_reason": "",
    })
    audit = audit_run_directory(tmp_path)
    assert audit["audit_status"] == "pass"


def test_ambiguous_mutation_requires_stop_boundary_and_recovery(tmp_path):
    _green_run(tmp_path)
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 0, "embedded": 0, "locations": [],
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 0, "accepted": 0,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 2,
        "transactions": [{
            "source_index": 1, "planned_records": 1,
            "state": "ambiguous_external_mutation_held", "uploaded": 0,
            "embedded": 0, "locations": [], "later_sources_released": False,
        }],
        "stopped_after_source_transaction": 1,
        "stop_reason": "ambiguous_external_mutation_held",
    })
    audit = audit_run_directory(tmp_path)
    assert audit["audit_status"] == "fail"
    assert "AUDIT-RECOVERY-001" in {row["code"] for row in audit["findings"]}


def test_cancelled_held_source_is_recovery_required_not_a_count_contradiction(tmp_path):
    """A cooperative stop preserves partial proof without becoming a false failure."""
    locations = ["custom-documents/a.json", "custom-documents/b.json", "custom-documents/c.json"]
    _write(tmp_path / "run-progress.json", {
        "state": "cancelled", "completed_units": 2, "total_units": 3,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 3, "embedded": 2,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 3, "accepted": 2,
        "recovery": {"state": "resume_available", "remaining_locations": [locations[-1]]},
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 2,
        "transactions": [
            {"source_index": 1, "planned_records": 2, "state": "exact_vectors_proven",
             "uploaded": 2, "embedded": 2, "locations": locations[:2]},
            {"source_index": 2, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 1, "embedded": 0, "locations": locations[2:]},
        ],
        "stopped_after_source_transaction": 2,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}

    assert audit["audit_status"] == "recovery_required"
    assert audit["summary"]["cancelled_reconciliation"] == {
        "required": True,
        "newly_attached_records": 3,
        "exactly_confirmed_records": 2,
        "unresolved_records": 1,
        "held_source": 2,
    }
    assert "AUDIT-CANCELLED-RECONCILIATION-001" in codes
    assert "AUDIT-CROSS-COUNT-002" not in codes
    assert "AUDIT-CROSS-SOURCE-002" not in codes


def test_held_accepted_source_can_require_observation_without_false_recovery_error(tmp_path):
    locations = ["custom-documents/a.json", "custom-documents/b.json"]
    _write(tmp_path / "run-progress.json", {
        "state": "warning", "completed_units": 1, "total_units": 2,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 2, "embedded": 1,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2, "accepted": 2,
        "recovery": {
            "state": "observation_required",
            "remaining_locations": [locations[-1]],
            "resubmission_forbidden": True,
        },
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 2,
        "transactions": [
            {"source_index": 1, "planned_records": 1, "state": "exact_vectors_proven",
             "uploaded": 1, "embedded": 1, "locations": locations[:1]},
            {"source_index": 2, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 1, "embedded": 0, "locations": locations[1:]},
        ],
        "stopped_after_source_transaction": 2,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    codes = {row["code"] for row in audit_run_directory(tmp_path)["findings"]}
    assert "AUDIT-RECOVERY-001" not in codes


def test_cancelled_held_queue_group_is_recovery_required_not_an_integrity_failure(tmp_path):
    """A bounded group may retain multiple held sources after one ambiguous receipt."""
    locations = [
        "custom-documents/a.json", "custom-documents/b.json",
        "custom-documents/c.json", "custom-documents/d.json",
    ]
    _write(tmp_path / "run-progress.json", {
        "state": "cancelled", "completed_units": 2, "total_units": 4,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 4, "embedded": 2,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 4, "accepted": 2,
        "recovery": {"state": "resume_available", "remaining_locations": locations[2:]},
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 6,
        "transactions": [
            {"source_index": 1, "planned_records": 2, "state": "exact_vectors_proven",
             "uploaded": 2, "embedded": 2, "locations": locations[:2],
             "source_queue_group_index": 1},
            {"source_index": 2, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 1, "embedded": 0, "locations": locations[2:3],
             "source_queue_group_index": 2, "later_sources_released": False},
            {"source_index": 3, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 1, "embedded": 0, "locations": locations[3:],
             "source_queue_group_index": 2, "later_sources_released": False},
        ],
        "stopped_after_source_transaction": 2,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}

    assert audit["audit_status"] == "recovery_required"
    assert "AUDIT-SOURCE-STOP-001" not in codes
    assert "AUDIT-SOURCE-STOP-002" not in codes
    assert "AUDIT-SOURCE-STOP-003" not in codes


def test_active_external_queue_is_recovery_required_not_a_count_contradiction(tmp_path):
    """A timed-out receipt can still have an owned Desktop queue in motion."""
    locations = ["custom-documents/a.json", "custom-documents/b.json"]
    _write(tmp_path / "run-progress.json", {"state": "failed"})
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 2, "embedded": 0,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2, "accepted": 0,
        "recovery": {"state": "resume_available", "remaining_locations": locations},
        "progress_observations": [{
            "source_queue_group_index": 1,
            "final_queue_snapshot": {
                "queue_records": 2,
                "desktop_queue_current": 1,
                "desktop_queue_completed": 0,
                "desktop_queue_observer_state": "connected",
            },
        }],
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 1,
        "transactions": [{
            "source_index": 1, "planned_records": 2,
            "state": "ambiguous_external_mutation_held", "uploaded": 2,
            "embedded": 0, "locations": locations,
        }],
        "stopped_after_source_transaction": 1,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}

    assert audit["audit_status"] == "recovery_required"
    assert audit["summary"]["external_queue_reconciliation"]["required"] is True
    assert "AUDIT-EXTERNAL-QUEUE-OBSERVATION-001" in codes
    assert "AUDIT-CROSS-SOURCE-002" not in codes


def test_held_queue_without_a_live_observer_is_recovery_not_a_contradiction(tmp_path):
    """A lost observer cannot turn an unresolved receipt into bad arithmetic."""
    locations = ["custom-documents/a.json", "custom-documents/b.json"]
    _write(tmp_path / "run-progress.json", {"state": "failed"})
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 2, "embedded": 0,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2, "accepted": 0,
        "recovery": {"state": "resume_available", "remaining_locations": locations},
        "progress_observations": [],
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 1,
        "transactions": [{
            "source_index": 1, "planned_records": 2,
            "state": "ambiguous_external_mutation_held", "uploaded": 2,
            "embedded": 0, "locations": locations,
        }],
        "stopped_after_source_transaction": 1,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}

    assert audit["audit_status"] == "recovery_required"
    assert "AUDIT-CROSS-SOURCE-002" not in codes


def test_reconciliation_pending_partial_counts_are_warning_not_integrity_failure(tmp_path):
    """A timeout receipt is not a completed-run count invariant."""
    locations = ["custom-documents/a.json", "custom-documents/b.json"]
    _write(tmp_path / "run-progress.json", {"state": "failed"})
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "reconciliation_pending", "uploaded": 2, "embedded": 0,
        "locations": locations,
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2, "accepted": 0,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })

    audit = audit_run_directory(tmp_path)
    codes = {row["code"] for row in audit["findings"]}
    assert "AUDIT-CROSS-COUNT-002" not in codes
    assert "AUDIT-RECONCILIATION-PENDING-COUNT-001" in codes


def test_held_queue_group_cannot_release_a_later_group(tmp_path):
    """Group-aware auditing must still reject work after the held group."""
    _green_run(tmp_path)
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 3,
        "transactions": [
            {"source_index": 1, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 0, "embedded": 0, "locations": [], "source_queue_group_index": 1},
            {"source_index": 2, "planned_records": 1, "state": "ambiguous_external_mutation_held",
             "uploaded": 0, "embedded": 0, "locations": [], "source_queue_group_index": 1},
            {"source_index": 3, "planned_records": 1, "state": "exact_vectors_proven",
             "uploaded": 1, "embedded": 1, "locations": ["custom-documents/late.json"],
             "source_queue_group_index": 2},
        ],
        "stopped_after_source_transaction": 1,
        "stop_reason": "ambiguous_external_mutation_held",
    })

    audit = audit_run_directory(tmp_path)
    assert "AUDIT-SOURCE-STOP-001" in {row["code"] for row in audit["findings"]}


def test_failure_bundle_is_compact_and_contains_no_source_path_or_workspace_name(tmp_path):
    _green_run(tmp_path)
    report = json.loads((tmp_path / "batch-native-upload-report.json").read_text())
    report["embedded"] = 9
    _write(tmp_path / "batch-native-upload-report.json", report)
    audit = audit_run_directory(tmp_path)
    bundle = write_failure_bundle(tmp_path, audit)
    assert bundle is not None
    text = (bundle / "integrity-audit.json").read_text(encoding="utf-8")
    assert "private workspace" not in text
    assert "Private" not in text
    assert len(text.encode("utf-8")) < 32_000


def test_failure_bundle_retention_removes_only_expired_owned_directories(tmp_path):
    directory = tmp_path / "reliability-failures"
    directory.mkdir()
    old = directory / "audit-old"
    old.mkdir()
    keep = directory / "manual-evidence"
    keep.mkdir()
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    removed = prune_failure_bundles(tmp_path, max_age_days=14)
    assert removed == ["audit-old"]
    assert keep.is_dir()


def test_terminal_state_override_audits_before_final_progress_record_is_written(tmp_path):
    _green_run(tmp_path)
    progress = json.loads((tmp_path / "run-progress.json").read_text())
    progress["state"] = "running"
    _write(tmp_path / "run-progress.json", progress)
    audit = audit_run_directory(tmp_path, terminal_state_override="successful")
    assert audit["audit_status"] == "pass"
    assert audit["run_outcome"] == "successful"


def test_all_existing_workspace_records_do_not_require_a_new_embedding_ledger(tmp_path):
    _write(tmp_path / "run-progress.json", {
        "state": "successful", "completed_units": 1, "total_units": 1,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "complete",
        "uploaded": 0,
        "embedded": 3,
        "locations": [],
        "document_results": {
            "source-a": {
                "status": "complete",
                "records": 3,
                "uploaded": 0,
                "embedded": 3,
                "existing_workspace_records": 3,
            },
        },
    })

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "pass"
    assert audit["summary"]["document_results"]["existing_workspace_records"] == 3


def test_mixed_existing_and_new_records_reconcile_without_false_overcount(tmp_path):
    locations = ["custom-documents/new-1.json", "custom-documents/new-2.json"]
    _write(tmp_path / "run-progress.json", {
        "state": "successful", "completed_units": 2, "total_units": 2,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "complete",
        "uploaded": 2,
        "embedded": 5,
        "locations": locations,
        "document_results": {
            "mixed-source": {
                "status": "complete",
                "records": 5,
                "uploaded": 2,
                "embedded": 5,
                "existing_workspace_records": 3,
            },
        },
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2,
        "accepted": 2,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "pass"
    assert audit["summary"]["newly_attached_records"] == 2
    assert audit["summary"]["confirmed_vector_records"] == 5


def test_mixed_existing_and_new_records_reconcile_with_source_transaction_ledger(tmp_path):
    """The source ledger owns only current-run queue work, not prior vectors."""
    locations = ["custom-documents/new-1.json", "custom-documents/new-2.json"]
    _write(tmp_path / "run-progress.json", {
        "state": "successful", "completed_units": 5, "total_units": 5,
    })
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "complete",
        "selected_records": 5,
        "selected_documents": 2,
        "newly_attached_records": 2,
        "vector_confirmed_records": 5,
        "existing_workspace_records": 3,
        "uploaded": 2,
        "embedded": 5,
        "locations": locations,
        "document_results": {
            "existing-source": {
                "status": "complete", "selected_records": 3,
                "newly_attached_records": 0, "vector_confirmed_records": 3,
                "existing_workspace_records": 3,
            },
            "new-source": {
                "status": "complete", "selected_records": 2,
                "newly_attached_records": 2, "vector_confirmed_records": 2,
                "existing_workspace_records": 0,
            },
        },
    })
    _write(tmp_path / "batch-embedding-ledger.json", {
        "requested": 2, "accepted": 2,
        "recovery": {"state": "not_needed", "remaining_locations": []},
    })
    _write(tmp_path / "source-transaction-ledger.json", {
        "transaction_count": 1,
        "transactions": [{
            "source_index": 1, "selected_records": 2,
            "state": "exact_vectors_proven", "newly_attached_records": 2,
            "vector_confirmed_records": 2, "locations": locations,
        }],
        "stopped_after_source_transaction": None, "stop_reason": "",
    })

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "pass"
    assert "AUDIT-CROSS-SOURCE-002" not in {row["code"] for row in audit["findings"]}


def test_live_progress_trace_rejects_impossible_completed_count(tmp_path):
    _green_run(tmp_path)
    (tmp_path / "progress-trace.jsonl").write_text(
        json.dumps({"completed_units": 69, "total_units": 44}) + "\n",
        encoding="utf-8",
    )

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "fail"
    assert "AUDIT-PROGRESS-TRACE-COUNT-001" in {row["code"] for row in audit["findings"]}


def test_canonical_count_conflicting_with_legacy_alias_fails_closed(tmp_path):
    _green_run(tmp_path)
    report = json.loads((tmp_path / "batch-native-upload-report.json").read_text())
    report["newly_attached_records"] = 0
    report["vector_confirmed_records"] = 2
    _write(tmp_path / "batch-native-upload-report.json", report)

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "fail"
    assert "AUDIT-COUNT-ALIAS-001" in {row["code"] for row in audit["findings"]}


def test_existing_record_claim_without_per_pdf_evidence_fails_closed(tmp_path):
    _write(tmp_path / "run-progress.json", {"state": "successful"})
    _write(tmp_path / "batch-native-upload-report.json", {
        "status": "complete", "uploaded": 0, "embedded": 4, "locations": [],
    })

    audit = audit_run_directory(tmp_path)

    assert audit["audit_status"] == "fail"
    codes = {row["code"] for row in audit["findings"]}
    assert "AUDIT-NATIVE-ARTIFACT-MISSING-001" in codes
    assert "AUDIT-REPORT-COUNT-002" in codes
