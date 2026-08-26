import pytest

from reliability_fault_injection import run_transport_fault_acceptance


pytestmark = pytest.mark.offline_deterministic


def test_loopback_transport_fault_matrix_preserves_request_and_replay_invariants(tmp_path):
    report = run_transport_fault_acceptance(tmp_path)

    assert report["status"] == "pass"
    assert report["scenario_count"] == 5
    assert report["scope"] == "loopback_transport_recovery_and_production_classifier"
    assert all(report["production_classifier_checks"].values())
    by_name = {row["scenario"]: row for row in report["results"]}
    assert by_name["definite_rejection_then_success"]["request_sources"] == [1, 2]
    assert by_name["lost_response_after_acceptance"]["request_sources"] == [1]
    assert by_name["lost_response_after_acceptance"]["mutation_acceptances"] == 1
    assert by_name["connection_refused_before_request"]["request_sources"] == []
    assert by_name["delayed_vectors"]["vector_observations"] >= 3
    assert by_name["sqlite_busy_then_vectors"]["vector_observations"] >= 3
