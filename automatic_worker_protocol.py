"""Small, explicit contract shared by the Automatic parent and child worker.

The worker hand-off crosses both a process and a durable JSON boundary.  Keep
the names of parent-owned transport files and the JSON-safe argument allowlist
in one dependency-light module so cleanup and credential handling cannot drift
between the Gradio parent, worker, and pipeline.
"""

from __future__ import annotations

import json
from typing import Any


AUTOMATIC_WORKER_TRANSPORT_ARTIFACTS = (
    ".automatic-worker-config.json",
    ".automatic-worker-events.jsonl",
    ".automatic-worker-result.json",
    ".automatic-worker-stderr.log",
)

# This is the complete data-only namespace built for the active Automatic
# worker.  Callbacks and the API key cross separate, non-durable boundaries.
AUTOMATIC_WORKER_ARGUMENT_FIELDS = frozenset({
    "anythingllm_api_url",
    "anythingllm_chunk_overlap",
    "anythingllm_chunk_size",
    "anythingllm_create_document_folders",
    "anythingllm_document_folder_name",
    "anythingllm_storage_dir",
    "backend_mode",
    "batch_inspection_context",
    "custom_page_group_sizes",
    "deep_extraction",
    "defer_lean_retention",
    "disable_inline_markers",
    "document_author",
    "document_label",
    "document_short_label",
    "end_page_override",
    "end_section_names",
    "external_compatibility_evidence",
    "external_preflight_managed",
    "first_page_override",
    "flat_output_without_logs",
    "include_back_matter",
    "include_front_matter",
    "lean_retention",
    "marker_style",
    "max_vector_chunks",
    "max_vector_probes",
    "native_metadata_upload_mode",
    "native_upload_representation",
    "native_upload_transport",
    "ocr_preflight_hint",
    "ollama_model",
    "ollama_url",
    "precomputed_source_fingerprint",
    "precomputed_source_sha256",
    "prepare_and_upload",
    "run_vector_eval",
    "segment_mode",
    "simulation_adapter",
    "simulation_embedder_choice",
    "target_passage_length",
    "temporary_validation_cleanup_policy",
    "test_workspace_slug",
    "unstructured_circuit_breaker",
    "unstructured_ocr_checkpoint_dir",
    "unstructured_runtime_probe",
    "unstructured_strategy",
    "upload_indices",
    "upload_limit",
    "use_file_title_fallback",
    "validation_phrases",
    "workspace_slug",
})

AUTOMATIC_WORKER_EPHEMERAL_ARGUMENT_FIELDS = frozenset({
    "anythingllm_api_key",
    "cancel_callback",
    "progress_callback",
    "timing_event_callback",
})


def can_share_native_batch_runtime(files, preflight, backend_mode, strategy, *, deep_extraction=False):
    """Share a deferred capability observation only within a verified native batch."""
    rows = (preflight or {}).get("files") or []
    return (
        len(files) >= 2
        and not deep_extraction
        and str(backend_mode or "").casefold() == "automatic"
        and str(strategy or "").casefold() == "auto"
        and str((preflight or {}).get("backend_mode") or "").casefold() == "automatic"
        and str((preflight or {}).get("unstructured_strategy") or "").casefold() == "auto"
        and ((preflight or {}).get("runtime") or {}).get("status") == "deferred_native_text_clear"
        and {str(path) for path in files} == {str(row.get("file") or "") for row in rows}
        and all(
            (row.get("full_native_text_coverage") or {}).get("status") == "verified"
            and row.get("risk") != "likely"
            and not row.get("targeted_visual_text_pages")
            for row in rows
        )
    )


def remember_native_batch_runtime(context, runtime):
    """Remember capabilities, never one PDF's OCR selection."""
    if (
        isinstance(context, dict)
        and context.get("share_native_runtime_probe") is True
        and not context.get("unstructured_runtime_probe")
        and isinstance(runtime, dict)
        and isinstance(runtime.get("backend_available"), bool)
        and isinstance(runtime.get("tesseract_available"), bool)
    ):
        context["unstructured_runtime_probe"] = {
            key: value for key, value in runtime.items() if key != "ocr_required"
        }


def native_batch_runtime_probe(context, tesseract_factory):
    """Restore cheap process-local OCR setup without importing the PDF backend."""
    runtime = context.get("unstructured_runtime_probe") if isinstance(context, dict) else None
    if not isinstance(runtime, dict) or not runtime:
        return None
    if context.get("share_native_runtime_probe") is not True:
        return runtime
    tesseract = tesseract_factory()
    return {
        **runtime,
        "tesseract_available": bool(tesseract.get("available")),
        "tesseract_executable": tesseract.get("executable") or "",
        "tessdata_prefix": tesseract.get("tessdata_prefix") or "",
    }


def serializable_automatic_worker_arguments(namespace: Any) -> tuple[dict[str, Any], str]:
    """Return the exact durable worker payload and its separately held key.

    An allowlist is intentionally stricter than ``vars(namespace)``: adding a
    future provider token or runtime object now fails at the parent boundary
    rather than being stringified into a retained run artifact.  The JSON
    round-trip also validates nested values before a child process is spawned.
    """
    values = dict(vars(namespace))
    unknown = sorted(
        set(values) - AUTOMATIC_WORKER_ARGUMENT_FIELDS - AUTOMATIC_WORKER_EPHEMERAL_ARGUMENT_FIELDS
    )
    if unknown:
        raise ValueError(
            "Automatic worker request contains unsupported durable fields: "
            + ", ".join(unknown)
        )
    payload = {
        field: values[field]
        for field in AUTOMATIC_WORKER_ARGUMENT_FIELDS
        if field in values
    }
    try:
        payload = json.loads(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Automatic worker request contains a non-JSON-safe value; "
            "keep runtime objects and credentials out of the worker contract."
        ) from exc
    return payload, str(values.get("anythingllm_api_key") or "")
