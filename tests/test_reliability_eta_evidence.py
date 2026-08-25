import pytest

from reliability_eta_evidence import build_eta_regression_evidence


pytestmark = pytest.mark.offline_deterministic


def test_eta_regression_evidence_preserves_classic_architecture_properties():
    report = build_eta_regression_evidence()

    assert report["status"] == "pass"
    assert report["private_history_used"] is False
    assert all(report["checks"].values())
    assert report["estimates_seconds"]["large_text"] > report["estimates_seconds"]["medium_text"]
