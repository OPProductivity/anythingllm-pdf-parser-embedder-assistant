"""Image-backed split-word OCR review without penalizing clean dissertations."""

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def _quality(text, *, pages=10, image_pages=10):
    rows = [{"page": number, "text": text} for number in range(1, pages + 1)]
    stats = [
        pipeline.page_stats_for(row, {"image_count": int(row["page"] <= image_pages)})
        for row in rows
    ]
    return pipeline.extraction_quality(rows, stats, 1, None)


def _native_candidate(quality):
    return {
        "backend": "pymupdf", "quality": quality, "error": "",
        "segments": [{"text": "source passage"}],
        "native_chunk_eval": {"status": "pass"},
        "start_page": 1, "end_page": quality["included_pages"] + 1,
    }


def test_image_backed_split_words_request_independent_ocr_only_candidate():
    damaged = ("M exican A m erican c om m unities and s ocial p olitical "
               "ch ange in t he U nited S tates.\n") * 18
    quality = _quality(damaged)
    candidate = _native_candidate(quality)
    assert quality["image_backed_split_word_pages"] == 10
    assert quality["text_integrity_status"] == "review"
    assert quality["text_integrity_interpretation"] == "image_backed_split_word_pattern"
    assert not pipeline.has_complete_native_text_candidate([candidate], 10)
    route = pipeline.resolve_unstructured_strategy(
        "auto", prior_candidates=[candidate], runtime_probe={"tesseract_available": True},
        pdf_page_count=10,
    )
    assert route["resolved"] == "ocr_only"
    clean_ocr = _native_candidate(_quality(
        ("Mexican American communities discussed social and political change.\n") * 32
    ))
    clean_ocr.update(backend="unstructured", unstructured_strategy="ocr_only", score=70)
    assert pipeline.cleaner_ocr_candidate_for_split_words([candidate, clean_ocr]) is clean_ocr
    poorer_ocr = {**clean_ocr, "quality": {**clean_ocr["quality"], "included_words": 10}}
    assert pipeline.cleaner_ocr_candidate_for_split_words([candidate, poorer_ocr]) is None


def test_clean_dissertation_and_isolated_scanned_heading_keep_fast_route():
    clean = _quality(("Mexican American communities and political organizations "
                      "promoted change across the country.\n") * 20)
    isolated = _quality(("M exican American communities discussed change.\n") * 25,
                        image_pages=2)
    for quality in (clean, isolated):
        assert quality["text_integrity_status"] == "not_flagged"
        route = pipeline.resolve_unstructured_strategy(
            "auto", prior_candidates=[_native_candidate(quality)],
            runtime_probe={"tesseract_available": True}, pdf_page_count=10,
        )
        assert route["resolved"] == "fast"
