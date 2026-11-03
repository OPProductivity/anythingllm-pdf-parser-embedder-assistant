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


@pytest.mark.parametrize("snapshot_change", [
    "missing", "recognition_effort", "use_file_title_fallback", "title",
    "author_samples_fingerprint", "schema_version",
])
def test_missing_or_incompatible_preview_runs_fresh_author_inference(
        tmp_path, monkeypatch, snapshot_change):
    pdf = tmp_path / "known.pdf"
    _fixture_pdf(pdf)
    snapshots = []
    app.detected_metadata_preview(
        [str(pdf)], snapshot_callback=lambda _index, _path, value: snapshots.append(value),
        recognition_effort="thorough",
    )
    assert len(snapshots) == 1
    snapshot = deepcopy(snapshots[0])
    if snapshot_change == "missing":
        snapshot = {}
    elif snapshot_change == "recognition_effort":
        snapshot["recognition_effort"] = "extremely_thorough"
    elif snapshot_change == "use_file_title_fallback":
        snapshot["use_file_title_fallback"] = False
    elif snapshot_change == "author_samples_fingerprint":
        snapshot["author_samples_fingerprint"] = "0" * 64
    elif snapshot_change == "schema_version":
        snapshot["schema_version"] = 1
    else:
        snapshot["title"] = "A Different Work"

    original_infer = pipeline.infer_profiled_author
    calls = []

    def record_infer(*args, **kwargs):
        calls.append(1)
        return original_infer(*args, **kwargs)

    monkeypatch.setattr(pipeline, "infer_profiled_author", record_infer)
    out = tmp_path / "fresh"
    pipeline.prepare_pdf(pdf, out, _worker_args(tmp_path, snapshot))
    profile = json.loads((out / "source-profile.json").read_text(encoding="utf-8"))
    expected_status = "not_available" if snapshot_change == "missing" else "stale_or_incompatible"
    assert profile["author_preview_reuse_status"] == expected_status
    assert calls == [1]


def test_fifth_page_credit_matches_fresh_worker_and_reuses_preview(tmp_path, monkeypatch):
    pdf = tmp_path / "shared-histories.pdf"
    with fitz.open() as doc:
        doc.set_metadata({"title": "Shared Histories"})
        for number in range(1, 7):
            page = doc.new_page()
            text = ("Praise for Shared Histories" if number == 1 else
                    "Shared Histories\nAlice Smith\nUniversity Press" if number == 5 else
                    "Front matter")
            page.insert_text((72, 72), text)
        doc.save(pdf)

    snapshots = []
    app.detected_metadata_preview(
        [str(pdf)], snapshot_callback=lambda _index, _path, value: snapshots.append(value),
        recognition_effort="thorough",
    )
    assert len(snapshots) == 1
    assert snapshots[0]["author"] == "Alice Smith"
    metadata = pipeline.pdf_metadata(pdf, include_author_samples=True)
    assert snapshots[0]["author_samples_fingerprint"] == pipeline.author_samples_fingerprint(
        metadata["_author_text_samples"], metadata["_author_sample_error"],
    )
    assert 5 in [sample["page"] for sample in metadata["_author_text_samples"]]

    original_infer = pipeline.infer_profiled_author
    calls = []

    def record_infer(*args, **kwargs):
        calls.append(1)
        return original_infer(*args, **kwargs)

    monkeypatch.setattr(pipeline, "infer_profiled_author", record_infer)
    reused_dir = tmp_path / "reused_fifth_page"
    pipeline.prepare_pdf(pdf, reused_dir, _worker_args(tmp_path, snapshots[0]))
    reused_profile = json.loads((reused_dir / "source-profile.json").read_text(encoding="utf-8"))
    assert reused_profile["author_preview_reuse_status"] == "verified_preview_reused"
    assert reused_profile["detected_author"] == "Alice Smith"
    assert calls == []

    fresh_dir = tmp_path / "fresh_fifth_page"
    pipeline.prepare_pdf(pdf, fresh_dir, _worker_args(tmp_path, {}))
    fresh_profile = json.loads((fresh_dir / "source-profile.json").read_text(encoding="utf-8"))
    assert fresh_profile["detected_author"] == reused_profile["detected_author"]
    assert fresh_profile["metadata_provenance"]["source_author"] == reused_profile["metadata_provenance"]["source_author"]
    assert calls == [1]


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
