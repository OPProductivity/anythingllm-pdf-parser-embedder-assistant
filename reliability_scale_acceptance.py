"""Prepared-checkpoint scale acceptance without contacting AnythingLLM.

The runner creates one thousand independent prepared source receipts using the
production checkpoint writer, verifies every artifact, proves one changed
source blocks reuse, restores it, and proves a durable submission-start marker
prevents replay of the entire batch.  It exercises filesystem scale and
recovery classification while remaining deterministic and free of source data.
It does not exercise PDF extraction, workers, Gradio, Desktop queues, or
embedding; those paths require separate production-path and live checks.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prepared_batch_recovery import (
    load_verified_prepared_summaries,
    verify_prepared_batch_checkpoint,
    write_prepared_batch_checkpoint,
)
from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_scale_acceptance_v1"
DEFAULT_SOURCE_COUNT = 1000


def _prepare_sources(root: Path, source_count: int) -> tuple[list[dict[str, Any]], list[Path]]:
    summaries: list[dict[str, Any]] = []
    texts: list[Path] = []
    for index in range(1, source_count + 1):
        source_root = root / "prepared" / f"source-{index:04d}"
        source_root.mkdir(parents=True)
        text = source_root / "page-0001.txt"
        text.write_text(f"anonymous durable source {index}\n", encoding="utf-8")
        plan = source_root / "upload-plan.csv"
        plan.write_text(
            "filename,title,docAuthor,description,docSource,chunkSource,text_file\n"
            f"page-0001.txt,Source {index},,,local-pdf://sha256/{index:064x},"
            f"page-parent://source-{index:04d}::p1,{text}\n",
            encoding="utf-8",
        )
        summary = {
            "pdf": "",
            "source_sha256": f"{index:064x}",
            "output_root": str(source_root),
            "native_upload_plan": str(plan),
            "api_upload_status": "not_started",
        }
        atomic_write_json(source_root / "run-summary.json", summary)
        summaries.append(summary)
        texts.append(text)
    return summaries, texts


def run_scale_acceptance(output_root: str | Path, *, source_count: int = DEFAULT_SOURCE_COUNT) -> dict[str, Any]:
    count = int(source_count)
    if not 50 <= count <= DEFAULT_SOURCE_COUNT:
        raise ValueError(f"source_count must be between 50 and {DEFAULT_SOURCE_COUNT}")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    summaries, texts = _prepare_sources(root, count)
    common = {
        "total_sources": count,
        "workspace_slug": "anonymous-scale-acceptance",
        "api_url": "http://127.0.0.1:3001",
    }
    write_prepared_batch_checkpoint(root, summaries, stage="preparation_complete", **common)
    initial = verify_prepared_batch_checkpoint(root)
    loaded_count = len(load_verified_prepared_summaries(root)) if initial.get("reusable") else 0

    probe_index = count // 2
    original = texts[probe_index].read_bytes()
    texts[probe_index].write_bytes(original + b"changed")
    changed = verify_prepared_batch_checkpoint(root)
    texts[probe_index].write_bytes(original)
    restored = verify_prepared_batch_checkpoint(root)

    write_prepared_batch_checkpoint(root, summaries, stage="submission_started", **common)
    ambiguous = verify_prepared_batch_checkpoint(root)
    checks = {
        "all_sources_checkpointed": (
            initial.get("checkpointed_sources") == count
            and initial.get("total_sources") == count
        ),
        "all_sources_reloadable": initial.get("reusable") is True and loaded_count == count,
        "single_changed_artifact_blocks_reuse": (
            changed.get("reusable") is False
            and any(
                marker in str(changed.get("reason") or "")
                for marker in ("size_changed", "hash_changed")
            )
        ),
        "restored_artifact_revalidates": restored.get("reusable") is True,
        "submission_started_never_replays": (
            ambiguous.get("reusable") is False
            and "reconcile source transactions" in str(ambiguous.get("reason") or "")
        ),
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if all(checks.values()) else "fail",
        "source_count": count,
        "artifact_count": count * 3,
        "external_mutation_attempted": False,
        "scope": "prepared_checkpoint_durability_only",
        "not_covered": [
            "pdf_extraction",
            "worker_supervision",
            "gradio_event_chains",
            "anythingllm_submission",
            "embedding_confirmation",
        ],
        "checks": checks,
    }
    atomic_write_json(root / "scale-acceptance-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run anonymous large-batch durability acceptance.")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--source-count", type=int, default=DEFAULT_SOURCE_COUNT)
    args = parser.parse_args(argv)
    if args.output_root:
        report = run_scale_acceptance(args.output_root, source_count=args.source_count)
    else:
        with tempfile.TemporaryDirectory(prefix="anythingllm-scale-acceptance-") as temp_dir:
            report = run_scale_acceptance(temp_dir, source_count=args.source_count)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
