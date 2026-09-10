"""Exercise the diagnostic with payloads from the real segmentation producer."""

from copy import deepcopy
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def description_result(tmp_path, segments, payloads):
    report = pipeline.evaluate_edge_cases(
        {"pdf_page_count": 3},
        {"backend": "pymupdf", "start_page": 1, "segments": segments},
        tmp_path,
        payloads,
    )
    return next(
        row for row in report["rows"]
        if row["check"] == "native_payload_metadata_description"
    )


@pytest.mark.parametrize("mode", ["none", "custom_page_ranges", "page"])
@pytest.mark.parametrize("page_count", [1, 3])
def test_generated_page_and_range_descriptions_pass(tmp_path, mode, page_count):
    source_meta = {
        "source_id": "source-1", "source_title": "Example", "source_author": "",
        "source_short_label": "Example", "source_sha256": "a" * 64,
        "source_published_epoch_ms": None, "metadata_provenance": {},
        "body_start": 1, "end_matter_start": None, "boundary_confidence": "high",
        "repeated_headers": [], "repeated_footers": [], "duplicate_pages": {},
    }
    segments = pipeline.make_segments(
        Path("example.pdf"), "pymupdf",
        [{"page": page, "text": "A complete body sentence. " * 30}
         for page in range(1, page_count + 1)],
        1, None, source_meta, 300, outline=None, segment_mode=mode,
        custom_page_group_sizes="2",
    )
    payloads = pipeline.generate_api_payloads(segments, "native_header")
    original = deepcopy(payloads)
    # Check every generated record, including the last one-page custom group.
    for segment, payload in zip(segments, payloads, strict=True):
        result = description_result(tmp_path, [segment], [payload])
        assert result["status"] == "pass", payload["metadata"]["description"]
    assert payloads == original


@pytest.mark.parametrize("description", [
    "", "Source title: Example. Segment: segment-1.",
    "PDF page: 1.", "PDF page range: 1-3.",
])
def test_missing_provenance_labels_still_fail(tmp_path, description):
    assert description_result(
        tmp_path, [], [{"metadata": {"description": description}}],
    )["status"] == "fail"


def test_missing_payload_still_fails(tmp_path):
    assert description_result(tmp_path, [], [])["status"] == "fail"
