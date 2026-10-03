from types import SimpleNamespace
import json

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("mode", ["none", "page_limit"])
@pytest.mark.parametrize("include_front,override,expected_start", [
    (True, 0, 1), (True, 2, 2), (False, 0, 2),
])
def test_opening_ocr_scope_preserves_recovered_cover_and_user_boundary(
    tmp_path, monkeypatch, mode, include_front, override, expected_start,
):
    pdf = tmp_path / "scanned-cover.pdf"
    document = fitz.open()
    for _ in range(12):
        document.new_page()
    document.save(pdf)
    document.close()
    body = "Reliable native body prose with consistent reading order. " * 150
    cover = "Recovered scanned cover title and author with readable publication information."

    def extract(path, backend, strategy, **kwargs):
        if backend == "pymupdf":
            return ([{"page": 1, "text": "", "kind": "page"}] + [
                {"page": page, "text": ("Introduction\n\n" if page == 2 else "")
                 + f"Physical page {page}. " + body, "kind": "page"}
                for page in range(2, 13)
            ], 12, [])
        assert backend == "unstructured"
        assert kwargs["unstructured_page_numbers"] == [1]
        return ([{"page": 1, "text": cover, "kind": "unstructured_elements"}], 12, [])

    monkeypatch.setattr(pipeline, "get_backend_pages", extract)
    args = SimpleNamespace(
        document_label="", document_author="", document_short_label="",
        use_file_title_fallback=True, deep_extraction=False,
        include_front_matter=include_front, include_back_matter=True,
        backend_mode="automatic", first_page_override=override, end_page_override=0,
        target_passage_length=500, segment_mode=mode,
        end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
        validation_phrases=[], unstructured_strategy="auto", marker_style="short",
        disable_inline_markers=False, run_vector_eval=False,
        ollama_model="", ollama_url="", max_vector_probes=0,
        prepare_and_upload=False, anythingllm_api_url="", anythingllm_api_key="",
        workspace_slug="", test_workspace_slug="test", upload_limit=0,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        anythingllm_chunk_size=400, anythingllm_chunk_overlap=40,
        ocr_preflight_hint={"full_native_text_coverage": {
            "status": "verified", "page_count": 12,
            "image_backed_low_text_pages": [{
                "page": 1, "native_text_characters": 0, "image_count": 1,
                "largest_image_area_ratio": 0.95,
            }],
        }},
        unstructured_runtime_probe={"backend_available": True, "tesseract_available": True},
    )
    result = pipeline.prepare_pdf(pdf, tmp_path / "output", args)
    assert result["start_page"] == expected_start
    profile = json.loads((tmp_path / "output" / "source-profile.json").read_text())
    assert {row["start_page"] for row in profile["backends"] if not row.get("error")} == {expected_start}
    if expected_start == 1:
        assert result["selected_backend"] == "unstructured"
        assert result["ocr_page_evidence"]["selected_ocr_page_count"] == 1
        rows = [json.loads(line) for line in (tmp_path / "output" / "segment-manifest.jsonl").read_text().splitlines()]
        assert cover in "\n".join(row["text"] for row in rows)


def test_layout_diagnostics_use_canonical_artifact_path():
    path = "candidates/unstructured/layout-region-review.json"
    selected = {"layout_evidence": {
        "removed_marginalia_count": 1,
        "note_candidates_retained_count": 1,
        "excluded_footnote_count": 1,
    }, "artifact_paths": {"layout-region-review.json": path}}
    diagnostics = pipeline.build_run_diagnostics({}, selected, [], {}, {}, {}, {}, {}, {}, {})
    layout_rows = [row for row in diagnostics if row["code"].startswith("PDF_LAYOUT_")]
    assert len(layout_rows) == 3
    assert all(path in row["recommended_action"] for row in layout_rows)
    assert all("selected/" not in row["recommended_action"] for row in layout_rows)


@pytest.mark.parametrize("changed", [True, False])
def test_lane_diagnostics_use_canonical_artifact_path(changed):
    path = "candidates/pymupdf/retrieval-lane-review.json"
    selected = {"lane_review": {
        "proposed_supplementary_count": 1, "primary_payload_changed": changed,
    }, "artifact_paths": {"retrieval-lane-review.json": path}}
    diagnostics = pipeline.build_run_diagnostics({}, selected, [], {}, {}, {}, {}, {}, {}, {})
    rows = [row for row in diagnostics if row["code"].startswith("PDF_SUPPLEMENTARY_")]
    assert len(rows) == 1
    assert path in rows[0]["recommended_action"]
