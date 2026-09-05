from copy import deepcopy

import pytest
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def test_decision_metrics_do_not_mutate_or_include_source_text():
    candidate = {
        "backend": "pymupdf",
        "segments": [{"text": "private source text"}],
        "quality": {
            "included_pages": 20,
            "included_words": 5000,
            "text_integrity_status": "review",
        },
        "score": 70,
        "score_reasons": ["fragmented_text_integrity_review"],
        "comparison_elapsed_seconds": 1.25,
        "native_chunk_eval": {"status": "pass"},
        "start_page": 2,
        "end_page": None,
        "outline_validation": {"reliability": "untrusted", "pass_rate": 0.2},
    }
    before = deepcopy(candidate)
    result = pipeline.extraction_candidate_decision_metrics(candidate)
    assert candidate == before
    assert "private source text" not in str(result)
    assert result["quality"]["text_integrity_status"] == "review"
    assert result["score"] == 70
    assert result["elapsed_seconds"] == 1.25
    assert result["selected_start_page"] == 2
    assert result["outline_validation"]["reliability"] == "untrusted"


def test_failed_extractor_keeps_failure_distinct_from_a_low_score():
    result = pipeline.extraction_candidate_decision_metrics(
        {"backend": "unstructured", "error": "failed"}
    )
    assert result["failed"] is True
    assert result["score"] is None
    assert not result["has_segments"]
