import pytest

from reliability_scale_acceptance import run_scale_acceptance


pytestmark = pytest.mark.offline_deterministic


def test_scale_acceptance_proves_thousand_source_checkpoint_integrity(tmp_path):
    report = run_scale_acceptance(tmp_path, source_count=1000)

    assert report["status"] == "pass"
    assert report["source_count"] == 1000
    assert report["artifact_count"] == 3000
    assert report["external_mutation_attempted"] is False
    assert all(report["checks"].values())
