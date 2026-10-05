import pytest

from rag_pdf_tools import unstructured_execution_evidence


pytestmark = pytest.mark.offline_deterministic


def test_checkpoint_path_is_runtime_only_not_a_retained_artifact_claim():
    execution = {"mode": "run_local_ocr_checkpoint_hit", "strategy": "ocr_only",
                 "cache_path": r"C:\run\.ocr-page-checkpoints\page.json"}
    page = {"unstructured_execution": execution}

    persisted = unstructured_execution_evidence([page])

    assert "cache_path" not in persisted
    assert persisted["checkpoint_retention"] == "temporary_until_source_preparation_complete"
    assert execution["cache_path"].endswith("page.json")


def test_uncached_execution_evidence_is_unchanged():
    execution = {"mode": "isolated_parallel_pages", "actual_workers": 2}
    assert unstructured_execution_evidence([{"unstructured_execution": execution}]) == execution
