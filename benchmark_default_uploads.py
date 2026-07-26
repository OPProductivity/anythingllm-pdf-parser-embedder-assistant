"""Run a reproducible default-mode timing study against AnythingLLM Desktop.

This is intentionally a serial runner: AnythingLLM's Desktop queue is the
system under measurement, so overlapping runs would measure contention rather
than the default PDF workflow.  It creates one clearly named, otherwise normal
document workspace per sample and leaves the resulting workspaces intact for
manual inspection.  The final ``benchmark-summary.json`` contains exact phase
timestamps and is sufficient to recompute every reported percentage later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import rag_pdf_gradio_app as app


DEFAULT_SOURCE_DIR = Path(
    r"C:\Users\Ninkear\Documents\Documenten 2025 - 2026\studie\_blok 1 & 2 & 3 & 4\MMT - Keywords Resit\sources"
)
DEFAULT_OUTPUT_BASE = Path(
    r"C:\Users\Ninkear\AppData\Local\AnythingLLM PDF Parser Embedder Assistant\outputs\default-upload-benchmarks"
)
PHASE_ORDER = (
    "metadata",
    "extraction",
    "candidate_evaluation",
    "payloads",
    "attachments",
    "desktop_queue",
    "searchable_vectors",
    "validation",
    "reporting",
)


def safe_directory_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value)).strip(" .-")
    return cleaned or "document"


def json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamped_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("recorded_at"), (int, float)):
            events.append(item)
    return events


def first_event_time(events: list[dict[str, Any]], predicate) -> float | None:
    for event in events:
        if predicate(event):
            return float(event["recorded_at"])
    return None


def last_event_time(events: list[dict[str, Any]], predicate) -> float | None:
    for event in reversed(events):
        if predicate(event):
            return float(event["recorded_at"])
    return None


def observed_activity_windows(events: list[dict[str, Any]]) -> dict[str, float | None]:
    """Capture Desktop queue and vector observation as overlapping activities.

    AnythingLLM can continue its serialized Desktop queue while the app starts
    checking early exact vectors after a slow request response.  These are not
    sequential pipeline phases, so this records both wall-clock spans and their
    overlap instead of pretending their durations add up to the run total.
    """
    queue_start = first_event_time(events, lambda event: event.get("phase") == "desktop_queue")
    queue_complete = last_event_time(
        events,
        lambda event: (
            event.get("phase") == "desktop_queue"
            and int(event.get("total_units") or 0) > 0
            and int(event.get("completed_units") or 0) >= int(event.get("total_units") or 0)
        ),
    )
    vector_start = first_event_time(events, lambda event: event.get("phase") == "searchable_vectors")
    vector_complete = first_event_time(
        events,
        lambda event: (
            event.get("phase") == "searchable_vectors"
            and int(event.get("total_units") or 0) > 0
            and int(event.get("completed_units") or 0) >= int(event.get("total_units") or 0)
        ),
    )
    validation_start = first_event_time(
        events,
        lambda event: event.get("evidence_kind") == "validation_started",
    )
    validation_complete = first_event_time(
        events,
        lambda event: event.get("evidence_kind") == "validation_completed",
    )
    reporting_start = first_event_time(events, lambda event: event.get("phase") == "reporting")

    def elapsed(start: float | None, end: float | None) -> float | None:
        return round(max(0.0, end - start), 3) if start is not None and end is not None else None

    overlap = None
    if all(value is not None for value in (queue_start, queue_complete, vector_start, vector_complete)):
        overlap = round(
            max(0.0, min(queue_complete, vector_complete) - max(queue_start, vector_start)),
            3,
        )
    return {
        "desktop_queue_started_at": queue_start,
        "desktop_queue_completed_at": queue_complete,
        "desktop_queue_wall_seconds": elapsed(queue_start, queue_complete),
        "vector_observation_started_at": vector_start,
        "vector_observation_completed_at": vector_complete,
        "vector_observation_wall_seconds": elapsed(vector_start, vector_complete),
        "queue_vector_overlap_seconds": overlap,
        "validation_started_at": validation_start,
        "validation_completed_at": validation_complete,
        "validation_wall_seconds": elapsed(validation_start, validation_complete),
        "reporting_started_at": reporting_start,
    }


def phase_times(events: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for event in events:
        phase = str(event.get("phase") or "")
        observed = float(event.get("recorded_at") or 0.0)
        if phase in PHASE_ORDER and observed > 0:
            result.setdefault(phase, observed)
    return result


def slot_seconds(preflight_started: float, worker_ended: float, phases: dict[str, float]) -> dict[str, float | None]:
    """Map event boundaries to the six user-facing progress slots."""
    metadata = phases.get("metadata")
    queue = phases.get("desktop_queue")
    vectors = phases.get("searchable_vectors")
    validation = phases.get("validation")
    reporting = phases.get("reporting")
    def interval(start: float | None, end: float | None) -> float | None:
        return round(max(0.0, end - start), 3) if start is not None and end is not None else None
    return {
        "startup_preflight": interval(preflight_started, metadata),
        "local_preparation": interval(metadata, queue),
        "desktop_queue": interval(queue, vectors),
        "exact_vectors": interval(vectors, validation),
        "validation": interval(validation, reporting),
        "reports_output": interval(reporting, worker_ended),
    }


def default_worker_args(pdf_path: Path, workspace_slug: str, ocr_hint: dict[str, Any]) -> dict[str, Any]:
    short_label = f"benchmark-{hashlib.sha256(str(pdf_path).encode()).hexdigest()[:8]}"
    return {
        "document_label": pdf_path.stem,
        "document_author": "",
        "document_short_label": short_label,
        "use_file_title_fallback": True,
        "deep_extraction": False,
        "include_front_matter": True,
        "include_back_matter": True,
        "backend_mode": "automatic",
        "first_page_override": 0,
        "end_page_override": 0,
        "target_passage_length": 8191,
        "segment_mode": "page_limit",
        "end_section_names": ["Notes", "Bibliography", "Index", "References", "Works Cited", "Endnotes"],
        "validation_phrases": [],
        "unstructured_strategy": "auto",
        "anythingllm_chunk_size": 0,
        "anythingllm_chunk_overlap": -1,
        "marker_style": "short",
        "disable_inline_markers": False,
        "lean_retention": True,
        "flat_output_without_logs": False,
        "run_vector_eval": False,
        "simulation_adapter": None,
        "simulation_embedder_choice": "None",
        "ollama_model": "bge-m3:latest",
        "ollama_url": "http://127.0.0.1:11434/api/embed",
        "max_vector_probes": 8,
        "max_vector_chunks": 0,
        "prepare_and_upload": True,
        "anythingllm_api_url": app.DEFAULT_ANYTHINGLLM_API_URL,
        "anythingllm_api_key": "",
        "workspace_slug": workspace_slug,
        "test_workspace_slug": workspace_slug,
        "upload_limit": 0,
        "upload_indices": [],
        "native_upload_transport": "file_upload",
        "native_metadata_upload_mode": "native_header",
        "native_upload_representation": "page_parents",
        "anythingllm_create_document_folders": False,
        "anythingllm_document_folder_name": "",
        "anythingllm_storage_dir": "",
        "batch_inspection_context": {},
        "ocr_preflight_hint": ocr_hint,
        "unstructured_runtime_probe": None,
        "unstructured_circuit_breaker": {},
        "unstructured_ocr_cache_dir": str(DEFAULT_OUTPUT_BASE / "_unstructured-ocr-cache"),
        "external_preflight_managed": True,
        "temporary_validation_cleanup_policy": "cleanup_always",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-base", type=Path, default=DEFAULT_OUTPUT_BASE)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-pages", type=int, default=0, help="Skip PDFs below this page count.")
    parser.add_argument("--max-pages", type=int, default=0, help="Skip PDFs above this page count (0 means no maximum).")
    parser.add_argument(
        "--resume",
        type=Path,
        help="Existing benchmark directory to resume without rerunning completed samples.",
    )
    args = parser.parse_args()

    if args.resume:
        benchmark_root = args.resume.resolve()
        summary_path = benchmark_root / "benchmark-summary.json"
        if not summary_path.is_file():
            raise FileNotFoundError(f"Resume summary not found: {summary_path}")
        prior_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        entries = list(prior_summary.get("entries") or [])
        stamp = benchmark_root.name.removeprefix("default-page-preserve-")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        benchmark_root = args.output_base / f"default-page-preserve-{stamp}"
        benchmark_root.mkdir(parents=True, exist_ok=False)
        summary_path = benchmark_root / "benchmark-summary.json"
        entries = []
    completed_files = {str(entry.get("file")) for entry in entries if entry.get("file")}
    seen_hashes: set[str] = {
        str(entry["file_sha256"])
        for entry in entries
        if entry.get("file_sha256")
    }
    for source_index, pdf_path in enumerate(sorted(args.source_dir.glob("*.pdf")), 1):
        if len([entry for entry in entries if entry.get("selected")]) >= max(1, args.limit):
            break
        if str(pdf_path) in completed_files:
            continue
        file_hash = sha256_file(pdf_path)
        if file_hash in seen_hashes:
            entries.append({"source_index": source_index, "file": str(pdf_path), "selected": False, "reason": "byte_for_byte_duplicate"})
            continue
        seen_hashes.add(file_hash)
        preflight_started = time.time()
        manifest = app.automatic_ocr_preflight_manifest([pdf_path])
        ocr_hint = dict((manifest.get("files") or [{}])[0])
        page_count = int(ocr_hint.get("pages") or 0)
        if page_count < max(0, args.min_pages) or (args.max_pages > 0 and page_count > args.max_pages):
            entries.append({
                "source_index": source_index,
                "file": str(pdf_path),
                "selected": False,
                "reason": "page_count_outside_requested_range",
                "ocr_preflight": ocr_hint,
            })
            continue
        if str(ocr_hint.get("risk")) == "likely":
            entries.append({"source_index": source_index, "file": str(pdf_path), "selected": False, "reason": "scan_only_ocr_excluded", "ocr_preflight": ocr_hint})
            continue
        sample_number = len([entry for entry in entries if entry.get("selected")]) + 1
        # Windows normalizes trailing spaces in directory names.  Trim after
        # truncation and add a short stable hash so long nearby titles stay unique.
        title_component = safe_directory_component(pdf_path.stem)[:64].strip(" .-") or "document"
        run_dir = benchmark_root / f"{sample_number:02d}-{title_component}-{file_hash[:8]}"
        run_dir.mkdir(parents=True, exist_ok=False)
        workspace_name = f"BENCHMARK {stamp} {sample_number:02d} {pdf_path.stem[:56]}"
        workspace = app.create_new_document_workspace(
            app.DEFAULT_ANYTHINGLLM_API_URL, "", pdf_path.stem, [str(pdf_path)], workspace_name
        )
        entry: dict[str, Any] = {
            "source_index": source_index,
            "file": str(pdf_path),
            "name": pdf_path.name,
            "file_bytes": pdf_path.stat().st_size,
            "file_sha256": file_hash,
            "selected": True,
            "ocr_preflight": ocr_hint,
            "workspace": workspace,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        if workspace.get("status") != "created" or not workspace.get("workspace_slug"):
            entry.update({"status": "workspace_failed", "error": workspace.get("error") or workspace.get("status")})
            entries.append(entry)
            json_write(summary_path, {"benchmark_root": str(benchmark_root), "entries": entries})
            continue
        events_path = run_dir / ".automatic-worker-events.jsonl"
        result_path = run_dir / ".automatic-worker-result.json"
        config = {
            "pdf_path": str(pdf_path),
            "output_dir": str(run_dir),
            "run_root": str(benchmark_root),
            "result_path": str(result_path),
            "events_path": str(events_path),
            "args": default_worker_args(pdf_path, workspace["workspace_slug"], ocr_hint),
        }
        config_path = run_dir / ".automatic-worker-config.json"
        json_write(config_path, config)
        process = subprocess.run([sys.executable, "cancellable_preparation_worker.py", str(config_path)], check=False)
        worker_ended = time.time()
        worker_result = {}
        if result_path.is_file():
            try:
                worker_result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                worker_result = {"status": "invalid_result_json"}
        events = timestamped_events(events_path)
        phases = phase_times(events)
        entry.update({
            "status": str(worker_result.get("status") or ("completed" if process.returncode == 0 else "failed")),
            "worker_returncode": process.returncode,
            "worker_result": worker_result,
            "phase_started_at_unix": phases,
            "slot_seconds": slot_seconds(preflight_started, worker_ended, phases),
            "observed_activity_windows": observed_activity_windows(events),
            "total_wall_seconds": round(worker_ended - preflight_started, 3),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })
        entries.append(entry)
        json_write(summary_path, {"benchmark_root": str(benchmark_root), "entries": entries})
        print(f"[{sample_number}/{args.limit}] {pdf_path.name}: {entry['status']} in {entry['total_wall_seconds']:.1f}s", flush=True)
    json_write(summary_path, {"benchmark_root": str(benchmark_root), "entries": entries})
    print(summary_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
