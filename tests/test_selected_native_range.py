"""Selected-range density must not dilute image evidence with unopened pages."""

import pytest
import auto_anythingllm_pipeline as a

pytestmark = pytest.mark.offline_deterministic


def candidate(**quality):
    return {
        "start_page": 101,
        "end_page": 111,
        "segments": [{}],
        "quality": {
            "included_pages": 10,
            "included_words": 2000,
            "empty_pages": 0,
            "scanned_likelihood": "low",
            "average_words_per_page": 200,
            **quality,
        },
        "native_chunk_eval": {"status": "pass"},
    }


def hint(*pages):
    return {
        "full_native_text_coverage": {
            "status": "verified",
            "image_backed_low_text_pages": [{"page": p} for p in pages],
        }
    }


def test_short_native_range_does_not_owe_whole_book_density():
    c = candidate()
    assert a.has_complete_native_text_candidate([c], 1000)
    c.pop("start_page")
    c.pop("end_page")
    assert not a.has_complete_native_text_candidate([c], 1000)


@pytest.mark.parametrize(
    "quality",
    [
        {"included_pages": 9},
        {"included_words": 1499},
        {"text_integrity_status": "review"},
        {"scanned_likelihood": "high"},
        {"scanned_likelihood": "possible"},
    ],
)
def test_partial_range_still_requires_clean_complete_native_proof(quality):
    assert not a.has_complete_native_text_candidate([candidate(**quality)], 1000)


def test_sparse_image_ratio_uses_selected_range():
    c = candidate(scanned_likelihood="possible")
    assert a.has_complete_native_text_candidate([c], 1000, hint(101, 110))
    assert not a.has_complete_native_text_candidate([c], 1000, hint(101, 102, 110))
    assert not a.has_complete_native_text_candidate([c], 1000, hint(100, 111))
    assert a.has_complete_native_text_candidate([c], 1000, hint(1, 101, 110, 999))


@pytest.mark.parametrize("pages", [(None,), ("bad",), (0,), (1001,)])
def test_bad_coverage_cannot_authorize_sparse_exception(pages):
    assert not a.has_complete_native_text_candidate(
        [candidate(scanned_likelihood="possible")], 1000, hint(*pages)
    )


def test_blank_pages_do_not_lower_density_requirement():
    assert not a.has_complete_native_text_candidate(
        [candidate(empty_pages=8, included_pages=2, included_words=600)], 1000
    )


@pytest.mark.parametrize(
    "change",
    [
        {"error": "failed"},
        {"segments": []},
        {"native_chunk_eval": {"status": "fail"}},
        {"end_page": 101},
        {"start_page": 1001},
    ],
)
def test_other_gates_remain_effective(change):
    c = candidate()
    c.update(change)
    assert not a.has_complete_native_text_candidate([c], 1000)
