from types import SimpleNamespace

import pytest

from run_request import LOCAL_ONLY, NATIVE_UPLOAD, RunRequest


pytestmark = pytest.mark.offline_deterministic


def test_cli_adapter_excludes_api_key_and_round_trips_legacy_execution_fields():
    args = SimpleNamespace(
        input="C:/approved/source.pdf", out_dir="C:/runs", document_label="Source",
        document_author="Author", document_short_label="S", lean_retention=False,
        backend_mode="automatic", deep_extraction=True, include_front_matter=True,
        first_page_override=2, end_page_override=4, end_section_names=["References"],
        validation_phrases=["anchor"], unstructured_strategy="auto", marker_style="short",
        disable_inline_markers=False, segment_mode="page_limit", target_passage_length=750,
        anythingllm_chunk_size=768, anythingllm_chunk_overlap=128, run_vector_eval=True,
        ollama_model="bge-m3:latest", ollama_url="http://127.0.0.1:11434/api/embed",
        prepare_and_upload=True, anythingllm_api_url="http://127.0.0.1:3001",
        anythingllm_api_key="must-not-live-on-request", workspace_slug="approved-workspace",  # pragma: allowlist secret -- synthetic request-redaction fixture
        upload_limit=2, native_metadata_upload_mode="strict", native_upload_representation="segments",
        native_upload_transport="file_upload", anythingllm_create_document_folders=True,
        run_chunk_survival_validation=False, temporary_validation_cleanup_policy="cleanup_always",
    )

    request = RunRequest.from_cli_namespace(args)
    legacy = request.to_legacy_namespace(resolved_api_key="injected-at-execution")  # pragma: allowlist secret -- synthetic execution-only credential

    assert request.mode == NATIVE_UPLOAD
    assert not hasattr(request, "anythingllm_api_key")
    assert request.retain_diagnostic_evidence is True
    assert legacy.anythingllm_api_key == "injected-at-execution"  # pragma: allowlist secret -- synthetic execution-only credential assertion
    assert legacy.segment_mode == "page_limit"
    assert legacy.anythingllm_chunk_size == 768
    assert legacy.anythingllm_chunk_overlap == 128


def test_automatic_adapter_normalizes_local_only_native_fields_away():
    request = RunRequest.from_automatic_settings(
        {
            "pdf_files": ["C:/approved/source.pdf"], "mode": "Local only",
            "api_url": "http://127.0.0.1:3001", "workspace_slug": "stale-workspace",
            "anythingllm_create_document_folders": True, "anythingllm_document_folder_name": "stale",
            "target_passage_length": 750, "backend_mode": "Automatic",
        },
        mode=LOCAL_ONLY,
        segment_mode="page_limit",
    )

    assert request.api_url is None
    assert request.workspace_slug is None
    assert request.create_document_folders is False
    assert request.to_legacy_namespace().prepare_and_upload is False


def test_native_automatic_adapter_defaults_to_document_subfolders_when_field_is_missing():
    request = RunRequest.from_automatic_settings(
        {
            "pdf_files": ["C:/approved/source.pdf"],
            "api_url": "http://127.0.0.1:3001",
            "workspace_slug": "approved-workspace",
            "target_passage_length": 750,
            "backend_mode": "Automatic",
        },
        mode=NATIVE_UPLOAD,
        segment_mode="page_limit",
    )

    legacy = request.to_legacy_namespace()

    assert request.create_document_folders is True
    assert legacy.anythingllm_create_document_folders is True


def test_native_cli_adapter_defaults_to_document_subfolders_when_field_is_missing():
    args = SimpleNamespace(
        input="C:/approved/source.pdf", out_dir="", document_label="",
        document_author="", document_short_label="", lean_retention=True,
        backend_mode="automatic", deep_extraction=False, include_front_matter=False,
        first_page_override=0, end_page_override=0, end_section_names=[],
        validation_phrases=[], unstructured_strategy="auto", marker_style="short",
        disable_inline_markers=False, segment_mode="page_limit", target_passage_length=750,
        anythingllm_chunk_size=0, anythingllm_chunk_overlap=-1, run_vector_eval=False,
        ollama_model="bge-m3:latest", ollama_url="http://127.0.0.1:11434/api/embed",
        prepare_and_upload=True, anythingllm_api_url="http://127.0.0.1:3001",
        workspace_slug="approved-workspace", upload_limit=0,
        native_metadata_upload_mode="native_header", native_upload_representation="segments",
        native_upload_transport="raw_text", run_chunk_survival_validation=False,
        temporary_validation_cleanup_policy="cleanup_always",
    )

    request = RunRequest.from_cli_namespace(args)

    assert request.create_document_folders is True
    assert request.to_legacy_namespace().anythingllm_create_document_folders is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_paths": ("C:/source.pdf",), "anythingllm_chunk_size_override": 500, "anythingllm_chunk_overlap_override": 500},
        {"input_paths": ("C:/source.pdf",), "mode": LOCAL_ONLY, "workspace_slug": "not-allowed"},
        {"input_paths": (), "mode": LOCAL_ONLY},
    ],
)
def test_request_rejects_invalid_intent_combinations(kwargs):
    with pytest.raises(ValueError):
        RunRequest(**kwargs)
