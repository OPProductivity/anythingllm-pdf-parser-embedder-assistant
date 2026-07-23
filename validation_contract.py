"""Pure native-validation completion rules shared by runtime and benchmarks."""

from __future__ import annotations

from typing import Any


SUCCESSFUL_UPLOAD_STATUSES = frozenset({"complete", "complete_with_key_cleanup_warning"})
SUCCESSFUL_POST_UPLOAD_STATUSES = frozenset({"pass", "pass_with_missing_workspace_document_records"})
SUCCESSFUL_RUNTIME_STATUSES = frozenset(
    {"pass"}
)
REVIEWABLE_POST_UPLOAD_STATUSES = frozenset(
    {
        *SUCCESSFUL_POST_UPLOAD_STATUSES,
        "verified_unavailable",
        "review",
        "concurrent_write_ambiguous",
    }
)


def post_upload_status_class(status: str, *, concurrent_writes_are_transient: bool = False) -> str:
    """Map storage-specific evidence to one shared operator-facing class."""
    normalized = str(status or "")
    if normalized in {"pass", "complete", "native_metadata_llm_visible"}:
        return "pass"
    if normalized in REVIEWABLE_POST_UPLOAD_STATUSES or normalized == "native_metadata_llm_visible_vector_only":
        if normalized == "concurrent_write_ambiguous" and concurrent_writes_are_transient:
            return "incomplete"
        return "review"
    if normalized in {"error", "workspace_missing", "blocked", "partial_vector_coverage"}:
        return "error"
    return "incomplete"


def evidence_layers_succeeded(upload_status: str, post_upload_status: str, runtime_status: str) -> bool:
    return (
        upload_status in SUCCESSFUL_UPLOAD_STATUSES
        and post_upload_status in SUCCESSFUL_POST_UPLOAD_STATUSES
        and runtime_status in SUCCESSFUL_RUNTIME_STATUSES
    )


def validation_report_succeeded(validation: dict[str, Any]) -> bool:
    """A summary flag cannot compensate for a failed live evidence layer."""
    return validation.get("status") == "complete" and evidence_layers_succeeded(
        str(validation.get("upload_status") or ""),
        str(validation.get("post_upload_status") or ""),
        str(validation.get("runtime_validation_status") or ""),
    )


def condition_satisfies_live_contract(condition: dict[str, Any]) -> bool:
    """Require reconciled prepared, embedded, and persisted-vector counts."""
    preparation = condition.get("preparation") or {}
    validation = condition.get("validation") or {}
    upload = validation.get("upload_report") or {}
    native = condition.get("native_observation") or {}
    prepared = int(preparation.get("payload_count") or 0)
    embedded = int(upload.get("embedded") or 0)
    observed = int(
        (native.get("after_runtime_validation") or {}).get("lancedb_vector_count")
        or native.get("lancedb_vector_count")
        or 0
    )
    return (
        condition.get("status") == "complete"
        and validation_report_succeeded(validation)
        and prepared > 0
        and embedded == prepared
        and observed >= prepared
    )
