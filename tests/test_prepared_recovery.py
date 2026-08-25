import json
from pathlib import Path

import pytest

from prepared_recovery import build_prepared_recovery_plan
from prepared_batch_recovery import write_prepared_batch_checkpoint


pytestmark = pytest.mark.offline_deterministic


def _ledger(root: Path, state: str, *, source_hash: str = "a" * 64):
    (root / "source-transaction-ledger.json").write_text(json.dumps({
        "transaction_count": 1,
        "transactions": [{
            "source_index": 1,
            "source_sha256": source_hash,
            "planned_records": 2,
            "state": state,
        }],
    }), encoding="utf-8")


def _receipt(root: Path, state: str, *, source_hash: str = "a" * 64):
    (root / "batch-submission-receipts.jsonl").write_text(json.dumps({
        "pdf_sha256": source_hash,
        "state": state,
    }) + "\n", encoding="utf-8")


def _prepared_batch_checkpoint(root: Path, stage: str):
    source_root = root / "source-one"
    source_root.mkdir(parents=True)
    text = source_root / "one.txt"
    text.write_text("durable prepared text", encoding="utf-8")
    plan = source_root / "upload-plan.csv"
    plan.write_text(
        "filename,title,docAuthor,description,docSource,chunkSource,text_file\n"
        f"one.txt,one,,,local-pdf://sha256/{'a' * 64},one-p001,{text}\n",
        encoding="utf-8",
    )
    summary = {
        "pdf": str(root / "one.pdf"),
        "source_sha256": "a" * 64,
        "output_root": str(source_root),
        "native_upload_plan": str(plan),
        "api_upload_status": "not_started",
    }
    (source_root / "run-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    write_prepared_batch_checkpoint(
        root,
        [summary],
        total_sources=1,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage=stage,
    )


def test_prepared_source_is_safe_because_no_intent_was_persisted(tmp_path):
    _ledger(tmp_path, "prepared")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["action"] == "safe_to_submit"
    assert plan["automatic_submission_allowed"]


def test_intent_without_transport_receipt_is_safe_to_submit(tmp_path):
    _ledger(tmp_path, "attachment_intent_durable")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["reason"] == "intent_persisted_but_no_transport_receipt"


@pytest.mark.parametrize("receipt_state", ["submitted", "submission_unknown", "attached"])
def test_any_possible_external_mutation_forbids_automatic_replay(tmp_path, receipt_state):
    _ledger(tmp_path, "attachment_intent_durable")
    _receipt(tmp_path, receipt_state)
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["action"] == "hold_for_reconciliation"
    assert not plan["automatic_submission_allowed"]


def test_definite_rejection_can_release_later_work(tmp_path):
    _ledger(tmp_path, "attachment_intent_durable")
    _receipt(tmp_path, "rejected")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["action"] == "preserve_rejection_and_continue"


def test_malformed_receipt_journal_fails_closed(tmp_path):
    _ledger(tmp_path, "attachment_intent_durable")
    (tmp_path / "batch-submission-receipts.jsonl").write_text("not-json\n", encoding="utf-8")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["status"] == "blocked"
    assert plan["sources"][0]["action"] == "hold_for_reconciliation"


def test_completed_source_is_never_resubmitted(tmp_path):
    _ledger(tmp_path, "exact_vectors_proven")
    _receipt(tmp_path, "attached")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["action"] == "preserve_completed"


def test_restart_submits_only_unstarted_source_beside_completed_source(tmp_path):
    (tmp_path / "source-transaction-ledger.json").write_text(json.dumps({
        "transaction_count": 2,
        "transactions": [
            {"source_index": 1, "source_sha256": "a" * 64, "planned_records": 1,
             "state": "exact_vectors_proven"},
            {"source_index": 2, "source_sha256": "b" * 64, "planned_records": 1,
             "state": "prepared"},
        ],
    }), encoding="utf-8")
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["automatic_submission_allowed"]
    assert [source["action"] for source in plan["sources"]] == [
        "preserve_completed", "safe_to_submit",
    ]


def test_complete_prepared_batch_without_source_ledger_is_safe_to_submit(tmp_path):
    _prepared_batch_checkpoint(tmp_path, "preparation_complete")

    plan = build_prepared_recovery_plan(tmp_path)

    assert plan["recovery_boundary"] == "prepared_batch_checkpoint"
    assert plan["sources"][0]["action"] == "safe_to_submit"
    assert plan["automatic_submission_allowed"] is True


def test_submission_started_without_source_ledger_is_ambiguous(tmp_path):
    _prepared_batch_checkpoint(tmp_path, "submission_started")

    plan = build_prepared_recovery_plan(tmp_path)

    assert plan["status"] == "blocked"
    assert plan["sources"][0]["action"] == "hold_for_reconciliation"
    assert plan["automatic_submission_allowed"] is False


def test_in_progress_preparation_can_resume_locally_but_never_submit(tmp_path):
    _prepared_batch_checkpoint(tmp_path, "preparation_in_progress")
    manifest = json.loads((tmp_path / "prepared-batch-recovery-manifest.json").read_text(encoding="utf-8"))
    manifest["total_sources"] = 2
    (tmp_path / "prepared-batch-recovery-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_prepared_recovery_plan(tmp_path)

    assert plan["sources"][0]["action"] == "resume_local_preparation"
    assert plan["automatic_submission_allowed"] is False
