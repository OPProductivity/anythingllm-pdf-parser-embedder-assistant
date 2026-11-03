"""Verified author-preview reuse through the actual local preparation worker."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


def _worker_args(tmp_path, snapshot, *, author_override="", elaborate=False):
    return SimpleNamespace(
        document_label="", document_author=author_override, document_short_label="",
        use_file_title_fallback=True, recognition_effort="thorough",
        author_preview_snapshot=snapshot,
        enable_elaborate_metadata_extraction=elaborate,
        deep_extraction=False, include_front_matter=True, include_back_matter=True,
        backend_mode="pymupdf", first_page_override=0, end_page_override=0,
        target_passage_length=500, segment_mode="none",
        end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
        validation_phrases=[], unstructured_strategy="fast", marker_style="short",
        disable_inline_markers=False, run_vector_eval=False,
        ollama_model="", ollama_url="", max_vector_probes=0,
        prepare_and_upload=False, anythingllm_api_url="", anythingllm_api_key="",
        workspace_slug="", test_workspace_slug="test", upload_limit=0,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        anythingllm_chunk_size=400, anythingllm_chunk_overlap=40,
    )


def _fixture_pdf(path):
    document = fitz.open()
    document.set_metadata({"title": "A Known Title", "author": "Ada Example"})
    page = document.new_page()
    page.insert_text((72, 72), "A Known Title\nAda Example\n" + "A complete sentence of body text. " * 30)
    document.save(path)
    document.close()


def test_preview_reuse_verifies_bytes_and_applies_run_override(tmp_path, monkeypatch):
    pdf = tmp_path / "known.pdf"
    _fixture_pdf(pdf)
    snapshots = []
    app.detected_metadata_preview(
        [str(pdf)], snapshot_callback=lambda _index, _path, value: snapshots.append(value),
        recognition_effort="thorough",
    )
    assert len(snapshots) == 1
    assert snapshots[0]["source_sha256"] == pipeline.sha256_file(pdf)

    original_infer = pipeline.infer_profiled_author
    calls = []

    def record_infer(*args, **kwargs):
        calls.append(1)
        return original_infer(*args, **kwargs)

    monkeypatch.setattr(pipeline, "infer_profiled_author", record_infer)
    out = tmp_path / "reused"
    reused = pipeline.prepare_pdf(
        pdf, out, _worker_args(tmp_path, snapshots[0], author_override="Manual Scholar"),
    )
    profile = json.loads((out / "source-profile.json").read_text(encoding="utf-8"))
    assert profile["author_preview_reuse_status"] == "verified_preview_reused"
    assert profile["detected_author"] == "Manual Scholar"
    assert profile["metadata_provenance"]["source_author"] == "user_override"
    assert profile["author_field_review"]["status"] == "not_requested"
    assert calls == []

    out = tmp_path / "reused_with_review"
    reviewed = pipeline.prepare_pdf(
        pdf, out, _worker_args(
            tmp_path, snapshots[0], author_override="Manual Scholar", elaborate=True,
        ),
    )
    profile = json.loads((out / "source-profile.json").read_text(encoding="utf-8"))
    assert profile["author_preview_reuse_status"] == "verified_preview_reused"
    assert profile["detected_author"] == "Manual Scholar"
    assert profile["metadata_provenance"]["source_author"] == "user_override"
    assert profile["author_field_review"]["status"] == "reviewed"
    assert Path(profile["author_field_review"]["review_file"]).is_file()
    assert Path(reused["upload_file"]).read_bytes() == Path(reviewed["upload_file"]).read_bytes()
    assert calls == []

    stale = deepcopy(snapshots[0])
    stale["source_sha256"] = "0" * 64
    out = tmp_path / "stale"
    pipeline.prepare_pdf(pdf, out, _worker_args(tmp_path, stale))
    profile = json.loads((out / "source-profile.json").read_text(encoding="utf-8"))
    assert profile["author_preview_reuse_status"] == "stale_or_incompatible"
    assert calls == [1]

    out = tmp_path / "stale_with_review"
    pipeline.prepare_pdf(pdf, out, _worker_args(tmp_path, stale, elaborate=True))
    profile = json.loads((out / "source-profile.json").read_text(encoding="utf-8"))
    assert profile["author_preview_reuse_status"] == "stale_or_incompatible"
    assert profile["author_field_review"]["status"] == "reviewed"
    assert calls == [1, 1]


def test_preview_cache_is_bound_to_session_selection_and_file_revision(tmp_path):
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"source content")
    request = SimpleNamespace(session_hash="preview-reuse-session")
    state = {"preview_token": "token", "revision": 3, "selection_signature": "selected"}
    scope = app._metadata_preview_cache_scope(
        request, state, [str(pdf)], ("", "", "", True, False, "thorough", True),
    )
    try:
        app._metadata_preview_cached_prefix(scope, [str(pdf)])
        app._metadata_preview_cache_store(scope, 0, str(pdf), "<section>Ready</section>", {"schema_version": 1})
        assert app._metadata_preview_snapshots_for_selection(request, state, [str(pdf)])
        assert not app._metadata_preview_snapshots_for_selection(
            request, {**state, "revision": 4}, [str(pdf)],
        )
        assert not app._metadata_preview_snapshots_for_selection(
            SimpleNamespace(session_hash="different-session"), state, [str(pdf)],
        )
        pdf.write_bytes(b"changed content")
        assert not app._metadata_preview_snapshots_for_selection(request, state, [str(pdf)])
    finally:
        app._metadata_preview_cache_clear(request)


def test_elaborate_checkbox_defaults_off_below_confirm():
    components = app.demo.config["components"]
    elaborate = next(row for row in components if row.get("props", {}).get("elem_id")
                     == "automatic-elaborate-metadata-control")
    confirm_index = next(index for index, row in enumerate(components)
                         if row.get("props", {}).get("elem_id") == "confirm-automatic-run-button")
    assert elaborate["props"]["label"] == "Run additional author-field and OCR review (diagnostic only)"
    assert elaborate["props"]["value"] is False
    assert components.index(elaborate) > confirm_index
    assert app.fresh_automatic_run_setting_values()["enable_elaborate_metadata_extraction"] is False
