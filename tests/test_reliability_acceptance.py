import pytest

from reliability_acceptance import run_offline_crash_acceptance


pytestmark = pytest.mark.offline_deterministic


def test_offline_subprocess_crash_matrix_preserves_replay_safety(tmp_path):
    report = run_offline_crash_acceptance(tmp_path)
    assert report["status"] == "pass"
    assert report["scenario_count"] == 13
    assert {row["scenario"] for row in report["results"]} == {
        "crash_after_batch_preparation_complete",
        "crash_after_batch_submission_started_before_source_ledger",
        "crash_after_prepared",
        "crash_after_intent",
        "crash_after_request_started",
        "crash_after_response_accepted",
        "crash_after_first_workspace_link",
        "crash_after_first_exact_vector",
        "crash_after_all_exact_vectors",
        "crash_after_terminal_audit",
        "crash_after_terminal_progress",
        "definite_rejection",
        "exact_vectors_proven",
    }
    assert {
        row["checkpoint"] for row in report["results"] if row.get("checkpoint")
    } == set(range(1, 11))
