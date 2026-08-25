"""Disposable live canary for the Gradio app's grouped multi-PDF path."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
from reliability_audit import audit_run_directory
from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_grouped_live_canary_v1"
MAX_CANARY_PDFS = 1000


class _NoopProgress:
    def __call__(self, *_args, **_kwargs):
        return None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def run_grouped_live_canary(
    pdf_paths: list[str | Path],
    output_root: str | Path,
    *,
    api_url: str = "http://127.0.0.1:3001",
    cleanup: bool = True,
    workspace_slug: str = "",
    first_page: int = 0,
    end_page: int = 0,
) -> dict[str, Any]:
    sources = [Path(path).resolve(strict=True) for path in pdf_paths]
    if not 2 <= len(sources) <= MAX_CANARY_PDFS:
        raise ValueError(
            f"The grouped canary requires 2-{MAX_CANARY_PDFS} copied PDF sources."
        )
    if any(path.suffix.casefold() != ".pdf" for path in sources):
        raise ValueError("Every grouped canary source must be a PDF.")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / "grouped-run"
    supplied_workspace = bool(str(workspace_slug or "").strip())
    if supplied_workspace:
        workspace = {"status": "reused", "workspace_slug": str(workspace_slug).strip()}
    else:
        workspace = pipeline.create_validation_workspace(
            api_url,
            api_key="",
            name_prefix="PDF Assistant Grouped Canary",
            top_n=8,
            storage_dir=pipeline.default_anythingllm_storage_dir(),
        )
    workspace_slug = str(workspace.get("workspace_slug") or "")
    if workspace.get("status") not in {"created", "reused"} or not workspace_slug:
        report = {
            "schema": SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "blocked",
            "stage": "workspace_creation",
            "workspace_create_status": workspace.get("status") or "error",
            "error": workspace.get("error") or "Temporary workspace was not created.",
        }
        atomic_write_json(root / "grouped-live-canary.json", report)
        return report

    canary_folder_name = f"canary-{workspace_slug}-docs"
    settings = app.fresh_automatic_run_setting_values()
    settings.update({
        "pdf_files": [str(path) for path in sources],
        "folder_pdf_files": [],
        "output_root_override": str(root),
        "api_url": api_url,
        "api_key": "",
        "workspace_slug": workspace_slug,
        # A canary-owned folder makes cleanup exact and leaves normal user
        # defaults untouched. The grouped queue/source-window path is shared.
        "anythingllm_create_document_folders": True,
        "anythingllm_document_folder_name": canary_folder_name,
        "run_root_override": str(run_root),
        "retain_detailed_evidence": True,
        "first_page_override": max(0, int(first_page or 0)),
        "end_page_override": max(0, int(end_page or 0)),
        "ocr_preflight_manifest": app.automatic_ocr_preflight_manifest(
            [str(path) for path in sources],
            backend_mode="Automatic",
            unstructured_strategy="auto",
        ),
        "progress": _NoopProgress(),
    })
    app.run_automatic(**settings)

    progress = _read_object(run_root / "run-progress.json")
    upload = _read_object(run_root / "batch-native-upload-report.json")
    source_ledger = _read_object(run_root / "source-transaction-ledger.json")
    audit = audit_run_directory(run_root)
    transactions = [
        row for row in (source_ledger.get("transactions") or []) if isinstance(row, dict)
    ]
    source_states = [str(row.get("state") or "") for row in transactions]
    selected_duplicates = [
        row for row in (upload.get("selected_input_exact_duplicates") or [])
        if isinstance(row, dict)
    ]
    document_results = upload.get("document_results") or {}
    if not isinstance(document_results, dict):
        document_results = {}
    selected_outcomes = []
    for source in sources:
        outcome = document_results.get(str(source))
        if not isinstance(outcome, dict):
            selected_outcomes.append("missing")
        elif str(outcome.get("status") or "") == "skipped_exact_duplicate":
            selected_outcomes.append("selected_exact_duplicate")
        elif (
            str(outcome.get("status") or "")
            in {"complete", "complete_with_key_cleanup_warning", "already_indexed"}
            and bool(outcome.get("searchability_proven"))
        ):
            selected_outcomes.append("exact_vectors_proven")
        else:
            selected_outcomes.append(str(outcome.get("status") or "unproven"))
    accounted_sources = sum(
        outcome in {"selected_exact_duplicate", "exact_vectors_proven"}
        for outcome in selected_outcomes
    )
    exact = bool(
        accounted_sources == len(sources)
        and all(state == "exact_vectors_proven" for state in source_states)
        and str(upload.get("status") or "") in {"complete", "complete_with_key_cleanup_warning"}
        and audit.get("audit_status") == "pass"
    )
    ambiguous = any(state in {"ambiguous_external_mutation_held", "global_run_hold"} for state in source_states)
    cleanup_result = {"status": "not_requested", "error": ""}
    document_folder_path = str(
        pipeline.default_anythingllm_storage_dir()
        / "documents"
        / "custom-documents"
        / pipeline.sanitize_anythingllm_folder_name(canary_folder_name)
    )
    # A supplied workspace may contain evidence from an earlier canary pass.
    # Never delete it implicitly; its creator remains responsible for cleanup.
    effective_cleanup = bool(cleanup and not supplied_workspace)
    if effective_cleanup and not ambiguous:
        cleanup_result = pipeline.delete_validation_workspace(
            api_url,
            workspace_slug,
            api_key="",
            storage_dir=pipeline.default_anythingllm_storage_dir(),
            document_folder_path=document_folder_path,
        )
    elif effective_cleanup and ambiguous:
        cleanup_result = {
            "status": "deferred_ambiguous_mutation",
            "error": "",
            "message": "The canary workspace was retained for exact reconciliation.",
        }
    cleaned = str(cleanup_result.get("status") or "") in {
        "deleted", "deleted_with_document_cleanup_warning",
    }
    folder_cleanup_status = str(
        (cleanup_result.get("document_folder_cleanup") or {}).get("status") or "not_reported"
    )
    cleanup_complete = bool(
        cleaned
        and folder_cleanup_status in {"deleted", "already_absent", "not_applicable"}
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if exact and (not effective_cleanup or cleanup_complete) else "fail",
        "selected_pdf_count": len(sources),
        "batch_scale": (
            "large" if len(sources) >= 50 else
            "medium" if len(sources) >= 9 else
            "small"
        ),
        "page_range": {
            "first_page": max(0, int(first_page or 0)),
            "end_page": max(0, int(end_page or 0)),
        },
        "run_state": progress.get("state") or "unavailable",
        "upload_status": upload.get("status") or "unavailable",
        "uploaded_records": int(upload.get("uploaded") or 0),
        "confirmed_records": int(upload.get("embedded") or 0),
        "source_transaction_count": len(transactions),
        "source_states": source_states,
        "selected_source_outcomes": selected_outcomes,
        "selected_exact_duplicate_count": len(selected_duplicates),
        "accounted_source_count": accounted_sources,
        "integrity_audit": audit.get("audit_status"),
        "ambiguous_mutation": ambiguous,
        "cleanup_status": cleanup_result.get("status") or "unavailable",
        "document_folder_cleanup_status": folder_cleanup_status,
        "workspace_retained": not cleaned,
        "workspace_ownership": "supplied_retained" if supplied_workspace else "canary_owned",
        "run_root": str(run_root),
    }
    atomic_write_json(root / "grouped-live-canary.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a disposable grouped PDF live canary.")
    parser.add_argument("--pdf", action="append", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--first-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=0)
    parser.add_argument("--retain-workspace", action="store_true")
    args = parser.parse_args(argv)
    report = run_grouped_live_canary(
        args.pdf,
        args.output_root,
        api_url=args.api_url,
        cleanup=not args.retain_workspace,
        first_page=args.first_page,
        end_page=args.end_page,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
