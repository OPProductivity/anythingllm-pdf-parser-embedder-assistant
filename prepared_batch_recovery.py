"""Durable, content-verified checkpoints for prepared PDF batches.

This module covers the interval before the AnythingLLM source-transaction
ledger exists.  It never submits, retries, or mutates AnythingLLM.  A prepared
batch is reusable only when every upload-bearing artifact still matches the
hash recorded before submission began.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_prepared_batch_checkpoint_v1"
MANIFEST_NAME = "prepared-batch-recovery-manifest.json"
REUSABLE_STAGE = "preparation_complete"
AMBIGUOUS_STAGES = frozenset({"submission_started", "submission_in_progress"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact(path: Path, role: str) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"required prepared artifact is not a file: {role}")
    return {
        "role": role,
        "path": str(resolved),
        "size": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _summary_path(summary: dict[str, Any]) -> Path | None:
    root = str(summary.get("output_root") or "").strip()
    if not root:
        return None
    candidate = Path(root) / "run-summary.json"
    if candidate.is_file():
        return candidate
    duplicate_receipt = Path(root) / "selected-input-duplicate.json"
    return duplicate_receipt if duplicate_receipt.is_file() else None


def _upload_plan_path(summary: dict[str, Any]) -> Path | None:
    value = str(
        summary.get("native_upload_plan")
        or summary.get("page_parent_upload_plan")
        or ""
    ).strip()
    if not value:
        return None
    return Path(value)


def _plan_text_files(plan_path: Path) -> list[Path]:
    paths: list[Path] = []
    with plan_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = str(row.get("text_file") or "").strip()
            if value:
                paths.append(Path(value))
    # Preserve plan order but hash a repeated payload only once.
    return list(dict.fromkeys(paths))


def _source_state(summary: dict[str, Any]) -> str:
    status = str(summary.get("api_upload_status") or "").strip().casefold()
    classification = str(summary.get("post_upload_classification") or "").strip().casefold()
    if status == "skipped_exact_duplicate":
        return "selected_exact_duplicate"
    if classification == "workspace_existing_content_skipped":
        return "workspace_existing_content"
    if str(summary.get("app_error_code") or "").strip() or status.startswith("error"):
        return "preparation_failed"
    if _upload_plan_path(summary):
        return "prepared_for_submission"
    return "prepared_without_upload_records"


def build_prepared_batch_checkpoint(
    run_root: str | Path,
    summaries: Iterable[dict[str, Any]],
    *,
    total_sources: int,
    workspace_slug: str,
    api_url: str,
    stage: str,
) -> dict[str, Any]:
    """Build a checkpoint from durable artifacts, raising on missing inputs."""
    root = Path(run_root).resolve(strict=True)
    sources: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries, start=1):
        state = _source_state(summary)
        artifacts: list[dict[str, Any]] = []
        summary_path = _summary_path(summary)
        if summary_path is None:
            raise ValueError(f"source {index} has no durable source-summary receipt")
        if state == "prepared_for_submission":
            artifacts.append(_artifact(summary_path, "source_summary"))
            plan_path = _upload_plan_path(summary)
            if plan_path is None:
                raise ValueError(f"source {index} has no upload plan")
            artifacts.append(_artifact(plan_path, "upload_plan"))
            text_files = _plan_text_files(plan_path)
            if not text_files:
                raise ValueError(f"source {index} upload plan contains no text files")
            artifacts.extend(_artifact(path, "prepared_text") for path in text_files)
        else:
            artifacts.append(_artifact(summary_path, "source_summary"))

        source_hash = str(summary.get("source_sha256") or "").strip().lower()
        sources.append({
            "source_index": index,
            "source_identity": f"sha256:{source_hash}" if source_hash else "unavailable",
            "state": state,
            "artifacts": artifacts,
        })

    return {
        "schema": SCHEMA,
        "run_id": root.name,
        "stage": str(stage),
        "total_sources": max(0, int(total_sources)),
        "checkpointed_sources": len(sources),
        # The workspace is operational state required for deliberate recovery.
        # Credentials are never written.  The API URL is retained only as an
        # origin, not with query/fragment or authorization material.
        "workspace_slug": str(workspace_slug or ""),
        "api_origin": str(api_url or "").split("?", 1)[0].split("#", 1)[0],
        "sources": sources,
    }


def write_prepared_batch_checkpoint(
    run_root: str | Path,
    summaries: Iterable[dict[str, Any]],
    *,
    total_sources: int,
    workspace_slug: str,
    api_url: str,
    stage: str,
) -> Path:
    root = Path(run_root)
    payload = build_prepared_batch_checkpoint(
        root,
        summaries,
        total_sources=total_sources,
        workspace_slug=workspace_slug,
        api_url=api_url,
        stage=stage,
    )
    path = root / MANIFEST_NAME
    atomic_write_json(path, payload)
    return path


def verify_prepared_batch_checkpoint(run_root: str | Path) -> dict[str, Any]:
    """Verify a checkpoint without reading source PDF contents or mutating state."""
    root = Path(run_root)
    path = root / MANIFEST_NAME
    if not path.is_file():
        return {
            "schema": SCHEMA,
            "status": "not_available",
            "reason": "prepared_batch_checkpoint_missing",
            "reusable": False,
            "sources": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": f"prepared_batch_checkpoint_unreadable:{type(exc).__name__}",
            "reusable": False,
            "sources": [],
        }
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "prepared_batch_checkpoint_schema_mismatch",
            "reusable": False,
            "sources": [],
        }

    problems: list[str] = []
    verified_sources: list[dict[str, Any]] = []
    for source in payload.get("sources") or []:
        if not isinstance(source, dict):
            problems.append("malformed_source_record")
            continue
        source_problems: list[str] = []
        for artifact in source.get("artifacts") or []:
            try:
                artifact_path = Path(str(artifact.get("path") or ""))
                if not artifact_path.is_file():
                    source_problems.append(f"missing:{artifact.get('role') or 'artifact'}")
                    continue
                if artifact_path.stat().st_size != int(artifact.get("size") or -1):
                    source_problems.append(f"size_changed:{artifact.get('role') or 'artifact'}")
                    continue
                if _sha256(artifact_path) != str(artifact.get("sha256") or ""):
                    source_problems.append(f"hash_changed:{artifact.get('role') or 'artifact'}")
            except (OSError, TypeError, ValueError):
                source_problems.append(f"unreadable:{artifact.get('role') or 'artifact'}")
        if source_problems:
            problems.extend(
                f"source_{int(source.get('source_index') or 0)}:{problem}"
                for problem in source_problems
            )
        verified_sources.append({
            "source_index": int(source.get("source_index") or 0),
            "source_identity": str(source.get("source_identity") or "unavailable"),
            "state": str(source.get("state") or "unknown"),
            "verified": not source_problems,
            "problems": source_problems,
        })

    stage = str(payload.get("stage") or "")
    complete = int(payload.get("checkpointed_sources") or 0) == int(
        payload.get("total_sources") or -1
    )
    source_rows = [
        source for source in payload.get("sources") or [] if isinstance(source, dict)
    ]
    uploadable = any(
        source.get("state") == "prepared_for_submission"
        for source in source_rows
    )
    unclassified = any(
        source.get("state") == "prepared_without_upload_records"
        for source in source_rows
    )
    reusable = (
        stage == REUSABLE_STAGE
        and complete
        and not unclassified
        and not problems
    )
    if stage in AMBIGUOUS_STAGES:
        reason = "submission_may_have_started; reconcile source transactions before replay"
    elif problems:
        reason = ";".join(problems)
    elif not complete:
        reason = "preparation_checkpoint_is_incomplete"
    elif unclassified:
        reason = "checkpoint_contains_unclassified_prepared_source"
    elif stage != REUSABLE_STAGE:
        reason = f"checkpoint_stage_not_reusable:{stage or 'missing'}"
    elif not uploadable:
        reason = "all_prepared_artifacts_match_no_submission_needed"
    else:
        reason = "all_prepared_artifacts_match"
    return {
        "schema": SCHEMA,
        "status": "ready" if reusable else "blocked",
        "reason": reason,
        "reusable": reusable,
        "stage": stage,
        "total_sources": int(payload.get("total_sources") or 0),
        "checkpointed_sources": int(payload.get("checkpointed_sources") or 0),
        "workspace_slug": str(payload.get("workspace_slug") or ""),
        "api_origin": str(payload.get("api_origin") or ""),
        "sources": verified_sources,
    }


def load_verified_prepared_summaries(run_root: str | Path) -> list[dict[str, Any]]:
    """Load summaries only after the whole reusable checkpoint verifies."""
    verification = verify_prepared_batch_checkpoint(run_root)
    if not verification.get("reusable"):
        raise RuntimeError(str(verification.get("reason") or "prepared batch is not reusable"))
    payload = json.loads((Path(run_root) / MANIFEST_NAME).read_text(encoding="utf-8"))
    summaries: list[dict[str, Any]] = []
    for source in payload.get("sources") or []:
        summary_artifact = next(
            (item for item in source.get("artifacts") or [] if item.get("role") == "source_summary"),
            None,
        )
        if not summary_artifact:
            # A workspace-existing source may have no local upload plan, but it
            # still normally has a durable summary. Exact-selection duplicates
            # use their dedicated receipt as the source summary.
            raise RuntimeError("verified source has no durable source-summary artifact")
        value = json.loads(Path(summary_artifact["path"]).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("run-summary artifact is not a JSON object")
        summaries.append(value)
    return summaries
