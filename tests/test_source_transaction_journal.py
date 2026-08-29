import hashlib
import json

import pytest

from prepared_recovery import build_prepared_recovery_plan
from source_transaction_journal import (
    EVENT_JOURNAL_NAME,
    ExistingSourceTransactionEvidence,
    append_source_transaction_event,
    finalize_source_transaction_journal,
    initialize_source_transaction_journal,
    materialize_source_transaction_journal,
)


pytestmark = pytest.mark.offline_deterministic


def _transaction(state="prepared", source_index=1):
    return {
        "source_index": source_index,
        "source_count": 1,
        "source_sha256": "a" * 64,
        "planned_records": 2,
        "state": state,
    }


def test_reentry_never_truncates_interrupted_source_evidence(tmp_path):
    ledger = tmp_path / "source-transaction-ledger.json"
    journal = initialize_source_transaction_journal(
        ledger, workspace_slug="workspace", run_id="run-1", transaction_count=1
    )
    append_source_transaction_event(journal, _transaction("attachment_intent_durable"))
    before = hashlib.sha256(journal.read_bytes()).hexdigest()

    with pytest.raises(ExistingSourceTransactionEvidence):
        initialize_source_transaction_journal(
            ledger, workspace_slug="workspace", run_id="run-1", transaction_count=1
        )

    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before
    assert build_prepared_recovery_plan(tmp_path)["sources"][0]["action"] == "safe_to_submit"


def test_torn_journal_tail_preserves_valid_state_but_blocks_automatic_replay(tmp_path):
    ledger = tmp_path / "source-transaction-ledger.json"
    journal = initialize_source_transaction_journal(
        ledger, workspace_slug="workspace", run_id="run-torn", transaction_count=1
    )
    append_source_transaction_event(journal, _transaction("prepared"))
    with journal.open("ab") as handle:
        handle.write(b'{"transaction":')

    plan = build_prepared_recovery_plan(tmp_path)

    assert plan["status"] == "blocked"
    assert plan["sources"][0]["durable_state"] == "prepared"
    assert plan["sources"][0]["action"] == "hold_for_reconciliation"
    assert "malformed_source_transaction_event_line:2" in plan["reason"]


def test_final_snapshot_is_materially_compatible_and_skips_journal_replay(tmp_path):
    ledger = tmp_path / "source-transaction-ledger.json"
    journal = initialize_source_transaction_journal(
        ledger, workspace_slug="workspace", run_id="run-final", transaction_count=1
    )
    complete = _transaction("exact_vectors_proven")
    complete.update(uploaded=2, embedded=2, locations=["custom-documents/a.json"])
    append_source_transaction_event(journal, complete)
    finalize_source_transaction_journal(
        ledger,
        workspace_slug="workspace",
        run_id="run-final",
        transaction_count=1,
        transactions=[complete],
    )
    with journal.open("ab") as handle:
        handle.write(b"torn terminal tail")

    stored = json.loads(ledger.read_text(encoding="utf-8"))
    materialized, errors = materialize_source_transaction_journal(tmp_path, stored)

    assert errors == []
    assert materialized["journal_finalized"] is True
    assert materialized["transactions"] == [complete]
    assert build_prepared_recovery_plan(tmp_path)["sources"][0]["action"] == "preserve_completed"


def test_out_of_range_source_transaction_blocks_automatic_recovery(tmp_path):
    ledger = tmp_path / "source-transaction-ledger.json"
    journal = initialize_source_transaction_journal(
        ledger, workspace_slug="workspace", run_id="run-range", transaction_count=1
    )
    append_source_transaction_event(journal, _transaction("prepared", source_index=2))

    plan = build_prepared_recovery_plan(tmp_path)

    assert plan["status"] == "blocked"
    assert "malformed_source_transaction_event_line:1" in plan["reason"]


def test_orphan_event_reservation_is_not_silently_adopted(tmp_path):
    (tmp_path / EVENT_JOURNAL_NAME).write_text("", encoding="utf-8")

    with pytest.raises(ExistingSourceTransactionEvidence):
        initialize_source_transaction_journal(
            tmp_path / "source-transaction-ledger.json",
            workspace_slug="workspace",
            run_id="run-orphan",
            transaction_count=1,
        )
