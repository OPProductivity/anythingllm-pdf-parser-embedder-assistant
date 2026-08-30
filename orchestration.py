"""Common persisted-control façade for CLI, Gradio, and tests.

This module deliberately records high-level lifecycle evidence around the
current preparation monolith.  It does not pretend that every conceptual stage
is already implemented as a separate executable function.  Until the pipeline
is decomposed, extraction, segmentation, artifact writing, optional upload,
and optional post-upload verification are correctly represented as delegated
work inside ``legacy_preparation_engine``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from anythingllm_compatibility import characterize
from anythingllm_state import resolve_state
from preflight import validate_planned_path
from run_control import RunRecorder, RunResult, atomic_write_json
from segmentation_policy import policy_for
from validation_contract import evidence_layers_succeeded


def new_run_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def build_phase_timing_breakdown(events):
    """Summarize pipeline timing events without turning overlaps into wall time.

    The preparation pipeline emits small, durable timing events from inside the
    legacy composite stage.  They are useful for diagnosing a slow run, but
    some are nested (for example an extraction candidate can perform a runtime
    capability check), so their values must never be presented as additive
    wall-clock phases.
    """
    phases = {}
    queue = {
        "batches_completed": 0,
        "records_submitted": 0,
        "batch_elapsed_seconds": 0.0,
        "submission_seconds": 0.0,
        "verification_seconds": 0.0,
    }
    captured = 0

    for raw_event in events or ():
        if not isinstance(raw_event, dict):
            continue
        stage = str(raw_event.get("stage") or "").strip()
        event_name = str(raw_event.get("timing_event") or "").strip()
        if not stage or not event_name:
            continue
        captured += 1
        if event_name == "phase_completed":
            elapsed = max(0.0, float(raw_event.get("phase_elapsed_seconds") or 0.0))
            if elapsed:
                phases[stage] = round(phases.get(stage, 0.0) + elapsed, 3)
        elif event_name == "batch_completed":
            queue["batches_completed"] += 1
            # The legacy embedding batch receipt calls this field
            # ``requested``. Both describe the same completed Desktop queue
            # batch; accepting either prevents a truthful 7-record receipt
            # from being rendered as "0 records" in the run timing report.
            record_value = (
                raw_event.get("records")
                if "records" in raw_event and raw_event.get("records") is not None
                else raw_event.get("requested", 0)
            )
            try:
                record_count = max(0, int(record_value))
            except (TypeError, ValueError):
                record_count = 0
            queue["records_submitted"] += record_count
            for key in ("batch_elapsed_seconds", "submission_seconds", "verification_seconds"):
                queue[key] += max(0.0, float(raw_event.get(key) or 0.0))

    return {
        "schema_version": 1,
        "capture": "pipeline_timing_event_relay",
        "event_count": captured,
        "phase_seconds": dict(sorted(phases.items())),
        "extraction_seconds": round(
            sum(value for stage, value in phases.items() if stage.startswith("extraction_backend:")),
            3,
        ),
        "desktop_queue": {
            key: (round(value, 3) if isinstance(value, float) else value)
            for key, value in queue.items()
        },
        "interpretation": (
            "Phase timers may overlap; use total_elapsed_seconds for wall time. "
            "Desktop queue totals describe completed native submission batches."
        ),
    }


def persist_phase_timing_breakdown(output: Path, breakdown: dict):
    """Add the observed timing breakdown to an existing durable run summary.

    Flat no-log exports intentionally remove this summary later.  This helper
    therefore never creates a new artifact merely to retain diagnostics.
    """
    summary_path = output / "run-summary.json"
    if not summary_path.is_file():
        return
    try:
        stored = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(stored, dict):
        return
    stored["phase_timing"] = breakdown
    atomic_write_json(summary_path, stored)


def compact_ready_run_control(output: Path, result: RunResult, summary: dict):
    """Merge durable control facts into the lean summary, then remove duplicates.

    A completed ready run has no resume obligation. Its separate checkpoint,
    event stream, and full run-result would repeat the same outcome while
    retaining a large stage-evidence payload. Review-needed and failed runs
    deliberately do not call this helper.
    """
    summary_path = output / "run-summary.json"
    if not summary_path.is_file():
        return
    try:
        compact = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    compact["recovery"] = {
        **dict(compact.get("recovery") or {}),
        "state": "completed",
        "resume_required": False,
        "run_id": result.run_id,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "total_elapsed_seconds": result.total_elapsed_seconds,
        "orchestration_status": result.status,
    }
    # Only remove the richer recovery files after the merged summary is safely
    # on disk. A direct write here could turn a ready run into an unrecoverable
    # partial summary during a crash or short Windows file-sharing failure.
    atomic_write_json(summary_path, compact)
    for name in ("run-checkpoint.json", "run-checkpoints.jsonl", "run-result.json"):
        (output / name).unlink(missing_ok=True)


def execute_preparation(pdf_path, output_root, args, prepare_callable):
    """Run the legacy preparation engine through the new persisted control loop.

    `prepare_callable` remains injected while the monolith is decomposed. This
    keeps UI, CLI, and tests on one façade without introducing circular imports.
    """
    output = Path(output_root)
    mode = getattr(args, "segment_mode", "passages")
    policy = policy_for(mode)
    result = RunResult(
        run_id=getattr(args, "run_id", "") or new_run_id(),
        output_root=str(output),
        selected_mode=mode,
        selected_policy=policy.to_dict(),
    )
    # The legacy engine and the app's timing ledger share these opaque IDs.
    # They correlate phases and submission receipts without exposing source
    # text or credentials.
    if not getattr(args, "run_id", ""):
        setattr(args, "run_id", result.run_id)
    if not getattr(args, "correlation_id", ""):
        setattr(args, "correlation_id", f"run-{result.run_id}-{uuid.uuid4().hex[:12]}")
    recorder = RunRecorder(result)
    storage = getattr(args, "anythingllm_storage_dir", "") or None
    summary = {}
    pipeline_timing_events = []
    original_timing_callback = getattr(args, "timing_event_callback", None)

    def capture_pipeline_timing(stage, report=None):
        event = dict(report or {})
        event["stage"] = str(stage or "")
        pipeline_timing_events.append(event)
        if callable(original_timing_callback):
            original_timing_callback(stage, report)

    try:
        compatibility = recorder.execute(
            "compatibility_fingerprint",
            lambda: characterize(storage),
        )
        external_compatibility = getattr(args, "external_compatibility_evidence", None)
        if bool(getattr(args, "external_preflight_managed", False)):
            # The per-source worker deliberately avoids hashing the 60 MB
            # Desktop package again. Preserve its fast read-only snapshot, but
            # bind mutation authority to the compact evidence produced by the
            # authoritative batch-level fingerprint gate. Without this label,
            # retained runs misleadingly appeared to both reject and accept
            # the same Desktop contract.
            compatibility = dict(compatibility)
            compatibility["preparation_worker_scope"] = "read_only_non_authoritative"
            compatibility["mutation_authority"] = (
                dict(external_compatibility)
                if isinstance(external_compatibility, dict)
                else {"status": "external_preflight_managed_without_embedded_evidence"}
            )
        result.compatibility = compatibility
        state = recorder.execute(
            "state_resolution",
            lambda: resolve_state(storage),
        )
        result.resolved_state = state
        preflight = recorder.execute(
            "preflight",
            lambda: validate_planned_path(
                state,
                mode=mode,
                target_length=int(getattr(args, "target_passage_length", 750)),
                requested_chunk_size=int(getattr(args, "anythingllm_chunk_size", 0) or 0),
                prepare_upload=(
                    bool(getattr(args, "prepare_and_upload", False))
                    and not bool(getattr(args, "external_preflight_managed", False))
                ),
                run_simulation=(
                    bool(getattr(args, "run_vector_eval", False))
                    and not bool(getattr(args, "external_preflight_managed", False))
                ),
                runtime_probe=getattr(args, "runtime_probe", None),
            ).to_dict(),
        )
        if preflight["status"] == "error":
            recorder.skip("pdf_extraction_normalization", "Blocked by preflight.")
            recorder.skip("segmentation", "Blocked by preflight.")
            recorder.skip("artifact_writing", "Blocked by preflight.")
            blocking = next(
                (
                    row for row in preflight.get("findings", [])
                    if str(row.get("severity") or "").casefold() == "blocking"
                ),
                {},
            )
            if blocking.get("code") == "chunk_size_exceeds_operational_limit":
                return recorder.finish(
                    "error",
                    "AUTO-CHUNK-LIMIT-001: " + str(blocking.get("message") or "Chunk size exceeds the active embedding limit."),
                )
            return recorder.finish(
                "error",
                "AUTO-PREFLIGHT-001: " + str(blocking.get("message") or "Preparation was blocked by preflight."),
            )

        # The legacy engine currently owns extraction, segmentation, artifact
        # writing, optional upload, and optional verification. Keep the façade
        # honest by recording that delegated block as one composite stage until
        # those phases are actually split out of the monolith.
        setattr(args, "timing_event_callback", capture_pipeline_timing)
        try:
            summary = recorder.execute(
                "legacy_preparation_engine",
                lambda: prepare_callable(Path(pdf_path), output, args),
            )
        finally:
            setattr(args, "timing_event_callback", original_timing_callback)
        phase_timing = build_phase_timing_breakdown(pipeline_timing_events)
        summary["phase_timing"] = phase_timing
        persist_phase_timing_breakdown(output, phase_timing)
        upload_status = str(summary.get("api_upload_status") or "not_applicable")
        post_upload_status = str(summary.get("post_upload_verification_status") or "not_applicable")
        runtime_status = str(summary.get("anythingllm_runtime_validation_status") or "not_applicable")
        result.upload_evidence = {
            "status": upload_status,
            "uploaded": int(summary.get("api_uploaded") or 0),
            "embedding_update_requested": int(summary.get("api_embedding_update_requested") or 0),
            "embedding_update_accepted": int(summary.get("api_embedding_update_accepted") or 0),
            "error": str(summary.get("api_upload_error") or ""),
        }
        result.verification_evidence = {
            "post_upload_status": post_upload_status,
            "post_upload_classification": str(summary.get("post_upload_classification") or ""),
            "matching_vectors": int(summary.get("post_upload_matching_vectors") or 0),
            "expected_payloads": int(summary.get("post_upload_expected_payloads") or 0),
            "runtime_status": runtime_status,
        }
        if bool(getattr(args, "prepare_and_upload", False)) and not evidence_layers_succeeded(
            upload_status,
            post_upload_status,
            runtime_status,
        ):
            delegated_stage = result.stages["legacy_preparation_engine"]
            delegated_stage.status = "degraded"
            delegated_stage.warnings.append(
                "Local preparation completed, but the AnythingLLM upload/indexing/retrieval evidence contract did not pass."
            )
            delegated_stage.operator_message = (
                "Preparation completed; AnythingLLM evidence is not confirmed."
            )
            recorder.persist("legacy_preparation_engine:degraded")
        recorder.skip("pdf_extraction_normalization", "Delegated to legacy_preparation_engine.")
        recorder.skip("segmentation", "Delegated to legacy_preparation_engine.")
        recorder.skip("artifact_writing", "Delegated to legacy_preparation_engine.")
        recorder.skip("settings_mutation", "No façade-owned settings mutation requested.")
        recorder.skip("upload", "Delegated to legacy_preparation_engine when enabled.")
        recorder.skip("post_upload_verification", "Delegated to legacy_preparation_engine when enabled.")
        recorder.execute("reporting", lambda: {"legacy_summary": summary})
        recorder.skip("cleanup", "No façade-owned cleanup obligation.")
        result.artifacts.extend(
            str(value) for key, value in summary.items()
            if isinstance(value, str) and key in {"manifest", "report", "upload_file", "diagnostics_report"}
        )
        finished = recorder.finish("pass", "Preparation completed through the common orchestration façade.")
        if (
            bool(getattr(args, "lean_retention", False))
            and str(summary.get("readiness_status") or "") == "ready"
            and bool((summary.get("lean_retention") or {}).get("applied"))
        ):
            compact_ready_run_control(output, finished, summary)
        return finished
    except Exception:
        return recorder.finish("error", "Preparation failed; inspect persisted stage evidence.")


def legacy_summary_from_run(result):
    """Return the pipeline summary stored in the delegated reporting stage.

    The name is a compatibility seam. The returned dictionary is the current
    app-facing run summary, not an obsolete summary format that callers should
    replace independently of the orchestration migration.
    """
    reporting = result.stages.get("reporting")
    if reporting:
        return dict(reporting.evidence.get("legacy_summary") or {})
    return {}
