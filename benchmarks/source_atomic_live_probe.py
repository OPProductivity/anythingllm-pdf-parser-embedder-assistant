"""Opt-in, disposable proof that the guarded Desktop source-atomic worker runs.

This creates one temporary workspace and two uniquely named page-parent text
records belonging to one synthetic PDF source.  It writes no production
workspace and emits no secrets.  The probe passes only when Desktop's SSE
stream returns the source-atomic staging events from the patched worker.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auto_anythingllm_pipeline import (  # noqa: E402
    cleanup_validation_document_records,
    cleanup_temporary_desktop_api_key,
    create_temporary_desktop_api_key,
    create_validation_workspace,
    default_anythingllm_storage_dir,
    delete_validation_workspace,
    maybe_upload_segment_files_source_transactions,
)


def _managed_locations_absent(storage_dir: Path, locations: list[str]) -> bool:
    """Return true only when this probe's exact document files are absent.

    AnythingLLM can continue a large document deletion after its client-side
    request timeout.  The probe must not call that a successful cleanup merely
    because the request was accepted, but it also must not retain a disposable
    workspace when the exact managed files subsequently disappear.
    """
    documents_root = (storage_dir / "documents").resolve()
    for location in locations:
        candidate = (documents_root / Path(location)).resolve()
        try:
            candidate.relative_to(documents_root)
        except ValueError:
            return False
        if candidate.exists():
            return False
    return True


def _wait_for_managed_locations_absent(
    storage_dir: Path,
    locations: list[str],
    *,
    timeout_seconds: float = 300.0,
    poll_seconds: float = 2.0,
) -> dict:
    """Observe bounded, exact-file completion of an asynchronous deletion."""
    started = time.monotonic()
    while True:
        if _managed_locations_absent(storage_dir, locations):
            return {
                "confirmed_absent": True,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        elapsed = time.monotonic() - started
        if elapsed >= timeout_seconds:
            return {
                "confirmed_absent": False,
                "elapsed_seconds": round(elapsed, 3),
            }
        time.sleep(min(poll_seconds, max(0.1, timeout_seconds - elapsed)))


def _write_probe_report(path: Path | None, value: dict) -> None:
    if path is not None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_probe(
    api_url: str,
    source_record_counts: tuple[int, ...] = (2,),
    *,
    upload_evidence_path: Path | None = None,
) -> dict:
    token = uuid.uuid4().hex
    storage_dir = default_anythingllm_storage_dir()
    workspace = create_validation_workspace(
        api_url,
        storage_dir=storage_dir,
        name_prefix="Source Atomic Runtime Probe",
        workspace_name=f"Source Atomic Runtime Probe {token[:12]}",
    )
    result = {
        "probe_id": token,
        "workspace_status": workspace.get("status"),
        "workspace_slug": workspace.get("workspace_slug"),
        "events": [],
        "source_atomic_events": [],
        "source_record_counts": list(source_record_counts),
        "upload_status": "not_started",
        "cleanup": {},
    }
    if workspace.get("status") != "created":
        result["error"] = workspace.get("error") or "temporary workspace creation failed"
        return result

    temporary_key = create_temporary_desktop_api_key(api_url)
    work_dir = Path(tempfile.mkdtemp(prefix="anythingllm-source-atomic-probe-"))
    upload = {}
    try:
        if temporary_key.get("status") != "created":
            result["error"] = "temporary API key creation failed"
            return result
        rows = []
        # Exercise one genuinely large source. Multi-source continuation is a
        # coordinator contract and has separate transaction tests; mixing
        # seven synthetic sources here made a worker probe fail at the first
        # source boundary while still calling the patch healthy.
        source_path = f"source-atomic-runtime-probe-{token}.pdf"
        for source_index, record_count in enumerate(source_record_counts, start=1):
            for page_index in range(1, record_count + 1):
                text_file = work_dir / f"probe-source-{source_index}-page-{page_index}.txt"
                text_file.write_text(
                    f"Source-atomic runtime proof {token}; source {source_index}; page {page_index}.\n"
                    "This uniquely generated page-parent record must enter the staged OpenRouter path.\n",
                    encoding="utf-8",
                )
                rows.append(
                    {
                        "filename": text_file.name,
                        "text_file": str(text_file),
                        "title": "Source atomic runtime probe",
                        "docAuthor": "Probe",
                        "description": "Disposable guarded worker runtime proof",
                        "docSource": source_path,
                        "chunkSource": f"{source_path}#page-{page_index}",
                        "_automatic_source_path": source_path,
                        "_anythingllm_folder_name": f"custom-documents/{workspace['workspace_slug']}-docs",
                    }
                )

        def observe(message, details):
            event = {"message": str(message or ""), **dict(details or {})}
            result["events"].append(event)
            if str(event.get("desktop_queue_event_type") or "").startswith("source_"):
                result["source_atomic_events"].append(event)

        upload = maybe_upload_segment_files_source_transactions(
            api_url,
            temporary_key["secret"],
            rows,
            workspace_slug=workspace["workspace_slug"],
            folder_name=f"custom-documents/{workspace['workspace_slug']}-docs",
            storage_dir=storage_dir,
            status_callback=observe,
            record_label="page-parent records",
        )
        result["upload_status"] = upload.get("status")
        result["upload_errors"] = list(upload.get("errors") or [])
        embedding = dict(upload.get("embedding_update") or {})
        result["requested"] = int(embedding.get("requested") or 0)
        result["accepted"] = int(embedding.get("accepted") or 0)
        result["runtime_event_types"] = sorted(
            {
                str(event.get("type") or "")
                for event in embedding.get("runtime_events") or []
                if isinstance(event, dict)
            }
        )
        result["source_atomic_runtime_events"] = [
            event
            for event in embedding.get("runtime_events") or []
            if isinstance(event, dict) and str(event.get("type") or "").startswith("source_")
        ]
        raw_provider_batches = [
            event
            for event in result["source_atomic_runtime_events"]
            if str(event.get("type") or "") == "source_staging_provider_batch"
        ]
        # Individual SSE messages are deliberately best-effort.  The source
        # terminal event is the canonical replay of every provider batch and
        # is what production state uses to repair coalesced notifications.
        canonical_by_batch = {}
        for event in result["source_atomic_runtime_events"]:
            if str(event.get("type") or "") != "source_staging_finished":
                continue
            source_key = str(event.get("sourceKey") or "")
            for batch in event.get("providerBatches") or event.get("provider_batches") or []:
                if not isinstance(batch, dict):
                    continue
                batch_index = batch.get("batchIndex", batch.get("batch_index"))
                canonical_by_batch[(source_key, str(batch_index))] = batch
        canonical_provider_batches = list(canonical_by_batch.values()) or raw_provider_batches
        result["outer_embedding_update_requests"] = len(embedding.get("batches") or [])
        result["provider_batch_requests"] = len(canonical_provider_batches)
        result["provider_batch_records"] = sum(
            max(0, int(event.get("chunkCount") or 0))
            for event in canonical_provider_batches
        )
        result["provider_batch_elapsed_ms"] = sum(
            max(0, int(event.get("elapsed_ms") or 0))
            for event in canonical_provider_batches
        )
        result["raw_provider_batch_notifications"] = len(raw_provider_batches)
        result["raw_provider_batch_records"] = sum(
            max(0, int(event.get("chunkCount") or 0))
            for event in raw_provider_batches
        )
        finished_source_keys = {
            str(event.get("sourceKey") or "")
            for event in result["source_atomic_runtime_events"]
            if str(event.get("type") or "") == "source_staging_finished"
            and bool(event.get("success"))
            and str(event.get("sourceKey") or "")
        }
        expected_provider_records = sum(source_record_counts)
        result["finished_source_count"] = len(finished_source_keys)
        result["expected_source_count"] = 1
        result["expected_provider_records"] = expected_provider_records
        result["passed"] = bool(
            str(result["upload_status"] or "").casefold()
            in {"complete", "reconciliation_pending"}
            and not result["upload_errors"]
            and result["accepted"] == result["requested"]
            and result["finished_source_count"] == result["expected_source_count"]
            and result["provider_batch_records"] == expected_provider_records
        )
        if not result["passed"]:
            result["error"] = (
                "The disposable upload did not complete every source with complete "
                "canonical provider-batch evidence."
            )
        # Persist only the completed upload/receipt evidence before the
        # intentionally slower asynchronous cleanup. This makes throughput
        # evidence available even when Desktop keeps deleting probe documents
        # after its API request has timed out.
        _write_probe_report(upload_evidence_path, result)
        return result
    finally:
        try:
            if workspace.get("workspace_slug") and temporary_key.get("secret"):
                # The benchmark can deliberately exceed the production
                # validation cleaner's conservative 100-location guard.  It
                # still deletes only this probe's exact, managed locations,
                # in bounded calls, *before* deleting the temporary
                # workspace.  Keeping an anomalous workspace instead of
                # discarding its tracking boundary is safer than claiming a
                # cleanup that was not completed.
                locations = list(upload.get("locations") or [])
                record_cleanups = []
                for start in range(0, len(locations), 100):
                    record_cleanups.append(
                        cleanup_validation_document_records(
                            api_url,
                            temporary_key["secret"],
                            locations[start : start + 100],
                            storage_dir=storage_dir,
                            workspace_slug=workspace["workspace_slug"],
                        )
                    )
                result["cleanup"]["document_records"] = record_cleanups
                records_clean_immediate = all(
                    str(item.get("status") or "") in {"deleted", "not_applicable"}
                    for item in record_cleanups
                )
                asynchronous_cleanup = {
                    "attempted": False,
                    "confirmed_absent": records_clean_immediate,
                    "elapsed_seconds": 0.0,
                }
                records_clean = records_clean_immediate
                if locations and not records_clean_immediate:
                    asynchronous_cleanup = {
                        "attempted": True,
                        **_wait_for_managed_locations_absent(
                            storage_dir,
                            locations,
                        ),
                    }
                    records_clean = bool(asynchronous_cleanup["confirmed_absent"])
                result["cleanup"]["asynchronous_document_observation"] = asynchronous_cleanup
                if records_clean:
                    result["cleanup"]["workspace"] = delete_validation_workspace(
                        api_url,
                        workspace["workspace_slug"],
                        api_key=temporary_key["secret"],
                        storage_dir=storage_dir,
                        document_folder_path=str(upload.get("document_folder_path") or ""),
                        document_locations=[],
                    )
                else:
                    result["cleanup"]["workspace"] = {
                        "status": "retained_after_document_cleanup_warning",
                        "error": "Probe workspace retained because exact document-record cleanup was incomplete.",
                    }
        except Exception as exc:  # retained in the non-secret probe report
            result["cleanup"]["workspace"] = f"error:{type(exc).__name__}"
        if temporary_key.get("id"):
            result["cleanup"]["temporary_key"] = cleanup_temporary_desktop_api_key(
                api_url,
                temporary_key["id"],
            )
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Authorize the disposable local Desktop probe.")
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument(
        "--source-records",
        default="2",
        help="Comma-separated disposable page-parent record counts, one number per source.",
    )
    parser.add_argument("--report-path", help="Optional non-secret JSON report path.")
    parser.add_argument(
        "--upload-evidence-path",
        help="Optional non-secret upload evidence written before cleanup completes.",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Number of sequential disposable probes.")
    args = parser.parse_args()
    if not args.live:
        parser.error("Refusing to contact AnythingLLM without --live.")
    try:
        source_record_counts = tuple(
            max(1, int(value.strip()))
            for value in str(args.source_records).split(",")
            if value.strip()
        )
    except ValueError:
        parser.error("--source-records must contain positive integers separated by commas.")
    if not source_record_counts:
        parser.error("--source-records must include at least one source.")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1.")
    evidence_path = Path(args.upload_evidence_path) if args.upload_evidence_path else None
    reports = [
        run_probe(
            args.api_url,
            source_record_counts,
            upload_evidence_path=(
                evidence_path
                if args.repeat == 1
                else evidence_path.with_name(f"{evidence_path.stem}-{index}{evidence_path.suffix}")
                if evidence_path is not None
                else None
            ),
        )
        for index in range(1, args.repeat + 1)
    ]
    report = reports[0] if args.repeat == 1 else {
        "source_record_counts": list(source_record_counts),
        "repeat": args.repeat,
        "runs": reports,
        "passed": all(bool(item.get("passed")) for item in reports),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report_path:
        Path(args.report_path).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
