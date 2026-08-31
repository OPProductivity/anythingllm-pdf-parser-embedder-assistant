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
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auto_anythingllm_pipeline import (  # noqa: E402
    cleanup_temporary_desktop_api_key,
    create_temporary_desktop_api_key,
    create_validation_workspace,
    default_anythingllm_storage_dir,
    delete_json,
    maybe_upload_segment_files,
)


def run_probe(api_url: str) -> dict:
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
        "upload_status": "not_started",
        "cleanup": {},
    }
    if workspace.get("status") != "created":
        result["error"] = workspace.get("error") or "temporary workspace creation failed"
        return result

    temporary_key = create_temporary_desktop_api_key(api_url)
    work_dir = Path(tempfile.mkdtemp(prefix="anythingllm-source-atomic-probe-"))
    try:
        if temporary_key.get("status") != "created":
            result["error"] = "temporary API key creation failed"
            return result
        source_path = f"source-atomic-runtime-probe-{token}.pdf"
        rows = []
        for index in range(1, 3):
            text_file = work_dir / f"probe-page-{index}.txt"
            text_file.write_text(
                f"Source-atomic runtime proof {token}; page {index}.\n"
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
                    "chunkSource": f"{source_path}#page-{index}",
                    "_automatic_source_path": source_path,
                    "_anythingllm_folder_name": f"custom-documents/{workspace['workspace_slug']}-docs",
                }
            )

        def observe(message, details):
            event = {"message": str(message or ""), **dict(details or {})}
            result["events"].append(event)
            if str(event.get("desktop_queue_event_type") or "").startswith("source_"):
                result["source_atomic_events"].append(event)

        upload = maybe_upload_segment_files(
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
        result["passed"] = any(
            str(event.get("desktop_queue_event_type") or "").startswith("source_staging_")
            for event in result["source_atomic_events"]
        ) or any(
            str(event.get("type") or "").startswith("source_staging_")
            for event in result["source_atomic_runtime_events"]
        )
        if not result["passed"]:
            result["error"] = "Desktop accepted the probe but emitted no source-atomic staging event."
        return result
    finally:
        try:
            if workspace.get("workspace_slug") and temporary_key.get("secret"):
                status, _ = delete_json(
                    api_url.rstrip("/") + f"/api/v1/workspace/{workspace['workspace_slug']}",
                    api_key=temporary_key["secret"],
                    timeout=60,
                )
                result["cleanup"]["workspace"] = "deleted" if 200 <= status < 300 else f"HTTP {status}"
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
    args = parser.parse_args()
    if not args.live:
        parser.error("Refusing to contact AnythingLLM without --live.")
    report = run_probe(args.api_url)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
