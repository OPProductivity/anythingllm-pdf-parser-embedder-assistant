"""Run the approved medium-PDF benchmark through the production worker path.

The public manifest is intentionally anonymous.  Real paths and fingerprints
are read only from an ignored private map; raw event data never crosses into a
public result.  This module has no benchmark-specific upload or polling logic:
it delegates the real work to the Gradio application's canonical worker route.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import fitz

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MANIFEST = Path(__file__).with_name("manifest.json")
PRIVATE_ROOT = Path(__file__).with_name("private")
PUBLIC_FORBIDDEN_KEYWORDS = {
    "path", "filename", "file_name", "fingerprint", "sha256", "workspace", "api_key", "secret", "text",
}
SHA256_PATTERN = re.compile(r"\b[a-fA-F0-9]{64}\b")
WINDOWS_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\r\n\"]+")
API_KEY_PATTERN = re.compile(r"(?i)\b(?:sk|api)[_-][a-z0-9_-]{8,}\b")
PHASE_BUCKET = {
    "metadata": "preparation",
    "extraction": "preparation",
    "candidate_evaluation": "preparation",
    "payloads": "preparation",
    "attachments": "preparation",
    "queue_receipt": "submission",
    "desktop_queue": "shared_ingestion",
    "identity_set": "shared_ingestion",
    "retrieval_sample": "validation",
    "validation": "validation",
    "reporting": "reporting",
}
# Version 11: no synthetic four-percent worker-start checkpoint, monotonic
# visible evidence, and mature/bounded queue-rate ETA repricing.
PRODUCTION_PRESENTATION_CONTROLLER_VERSION = 11
# Revision 3 adds exact, provenance-matched cached page-parent reuse before
# submission. That can materially change local preparation and queue time, so
# results from the older runtime protocol must stay historical for timing.
BENCHMARK_RUNTIME_PROTOCOL_REVISION = 3


@dataclass(frozen=True)
class BenchmarkDocument:
    document_id: str
    page_count: int
    size_mib: float
    ocr_risk: str


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def archive_invalid_public_result(result_path: Path) -> Path | None:
    """Preserve a safe invalid trial before its numbered slot is rerun."""
    try:
        prior = read_json(result_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(prior, dict) or not bool(prior.get("invalid_for_calibration")):
        return None
    # The archive is public only if it satisfies the same privacy boundary as
    # an ordinary result. It stays outside the aggregate report's trial glob.
    assert_public_payload_safe(prior)
    archive_directory = result_path.parent.parent / "invalid"
    for attempt in range(1, 1_000):
        archive_path = archive_directory / f"{result_path.stem}-attempt-{attempt}.json"
        if not archive_path.exists():
            write_json(archive_path, prior)
            return archive_path
    raise OSError(f"Could not archive invalid benchmark result for {result_path.name}")


def load_manifest(path: Path = PUBLIC_MANIFEST) -> dict[str, BenchmarkDocument]:
    payload = read_json(path)
    if payload.get("schema_version") != 1 or payload.get("profile") != "ordinary-page-preserving-upload":
        raise ValueError("Benchmark manifest does not describe the approved ordinary profile.")
    documents = {}
    for row in payload.get("documents") or []:
        document = BenchmarkDocument(
            document_id=str(row.get("document_id") or ""),
            page_count=int(row.get("page_count") or 0),
            size_mib=float(row.get("size_mib") or 0.0),
            ocr_risk=str(row.get("ocr_risk") or ""),
        )
        if not re.fullmatch(r"B\d{2}", document.document_id) or document.page_count <= 0 or document.ocr_risk != "low":
            raise ValueError("Benchmark manifest contains an invalid document row.")
        if document.document_id in documents:
            raise ValueError("Benchmark manifest contains duplicate document IDs.")
        documents[document.document_id] = document
    if len(documents) != 8:
        raise ValueError("The approved benchmark requires exactly eight anonymous documents.")
    return documents


def load_private_source_map(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    rows = payload.get("documents") if isinstance(payload, dict) else None
    result: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        document_id = str((row or {}).get("document_id") or "")
        source_path = Path(str((row or {}).get("path") or ""))
        if not re.fullmatch(r"B\d{2}", document_id) or not source_path.is_file():
            raise ValueError("Private source map contains a missing or invalid source path.")
        result[document_id] = {"path": source_path, "fingerprint": str((row or {}).get("fingerprint") or "")}
    return result


def public_payload_violations(payload: Any, *, forbidden_values: Iterable[str] = ()) -> list[str]:
    """Return privacy violations before a result can be made public."""
    violations: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                normalized = str(child_key).casefold()
                if (
                    normalized in PUBLIC_FORBIDDEN_KEYWORDS
                    or normalized.endswith("_path")
                    or normalized.startswith("workspace")
                ):
                    violations.append(f"forbidden key: {child_key}")
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if WINDOWS_PATH_PATTERN.search(value):
                violations.append("absolute path")
            if SHA256_PATTERN.search(value):
                violations.append("sha256-like value")
            if API_KEY_PATTERN.search(value):
                violations.append("API-key-like value")
            for forbidden in forbidden_values:
                if forbidden and forbidden in value:
                    violations.append("private source value")

    walk(payload)
    return sorted(set(violations))


def assert_public_payload_safe(payload: Any, *, forbidden_values: Iterable[str] = ()) -> None:
    violations = public_payload_violations(payload, forbidden_values=forbidden_values)
    if violations:
        raise ValueError("Refusing to write unsafe public benchmark data: " + "; ".join(violations))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def event_monotonic(event: dict[str, Any]) -> float | None:
    try:
        value = float(event.get("recorded_monotonic"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and event_monotonic(row) is not None:
            events.append(row)
    return sorted(events, key=lambda row: float(event_monotonic(row) or 0.0))


def read_progress_trace(path: Path) -> list[dict[str, Any]]:
    """Read the production presentation trace retained beside each run."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            elapsed = float(row.get("elapsed_seconds") or 0.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if elapsed >= 0.0:
            rows.append(row)
    return sorted(rows, key=lambda row: float(row.get("elapsed_seconds") or 0.0))


def merge_presentation_rows(*row_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order callback traces and UI-timer samples as one visible timeline.

    The durable callback trace remains the source for phase attribution.  This
    merged view is deliberately only for calibration: it lets the benchmark
    observe the same in-phase paced value that a localhost page receives on
    its one-second status refresh, including a frozen cancelled/failed value.
    """
    rows = [dict(row) for row_set in row_sets for row in row_set]
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("elapsed_seconds") or 0.0),
            0 if row.get("presentation_source") == "ui_timer" else 1,
        ),
    )


def progress_trace_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt visible production progress rows into safe timing observations."""
    return [
        {
            "recorded_monotonic": float(row.get("elapsed_seconds") or 0.0),
            "phase": str(row.get("progress_phase") or ""),
            "value": float(row.get("visible_progress_percent") or 0.0) / 100.0,
            "evidence_kind": str(row.get("evidence_kind") or ""),
        }
        for row in rows
    ]


def disjoint_wall_clock_attribution(events: list[dict[str, Any]], started: float, finished: float) -> dict[str, float]:
    """Attribute every completed second once, with queue/vector as one union.

    Individual queue, vector, polling, and validation spans are retained
    separately below.  They are evidence observations and can overlap; using
    them as additive phase shares would corrupt the total.
    """
    buckets = {name: 0.0 for name in ("preparation", "submission", "shared_ingestion", "validation", "reporting", "unattributed")}
    points = [(max(started, min(finished, event_monotonic(row) or started)), str(row.get("phase") or "")) for row in events if row.get("type") != "timing"]
    points.append((finished, ""))
    cursor = started
    current = "unattributed"
    for observed, phase in points:
        if observed > cursor:
            buckets[current] += observed - cursor
        cursor = observed
        current = PHASE_BUCKET.get(phase, current if phase else "unattributed")
    total = max(0.0, finished - started)
    drift = total - sum(buckets.values())
    buckets["unattributed"] += drift
    return {name: round(max(0.0, seconds), 3) for name, seconds in buckets.items()}


def overlapping_evidence_spans(events: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    def span(predicate) -> dict[str, float | None]:
        matched = [event_monotonic(row) for row in events if predicate(row)]
        values = [value for value in matched if value is not None]
        return {
            "started_monotonic": round(min(values), 3) if values else None,
            "finished_monotonic": round(max(values), 3) if values else None,
            "wall_seconds": round(max(values) - min(values), 3) if len(values) > 1 else (0.0 if values else None),
        }

    return {
        "owned_queue": span(lambda row: str(row.get("phase") or "") == "desktop_queue"),
        "confirmed_vectors": span(lambda row: str(row.get("phase") or "") == "identity_set"),
        "validation": span(lambda row: str(row.get("phase") or "") in {"retrieval_sample", "validation"}),
        "reconnecting": span(lambda row: str((row.get("batch_report") or {}).get("desktop_queue_observer_state") or "") == "reconnecting"),
        "quiet": span(lambda row: "quiet" in str((row.get("batch_report") or {}).get("observer_status") or "").casefold()),
    }


def observed_queue_rate_per_minute(events: list[dict[str, Any]]) -> float | None:
    rates = []
    for row in events:
        report = row.get("batch_report") if isinstance(row.get("batch_report"), dict) else {}
        try:
            rate = float(report.get("desktop_queue_records_per_minute"))
        except (TypeError, ValueError):
            continue
        if rate > 0:
            rates.append(rate)
    return round(sum(rates) / len(rates), 3) if rates else None


def retrospective_calibration(events: list[dict[str, Any]], started: float, finished: float) -> dict[str, Any]:
    duration = max(0.001, finished - started)
    checkpoints: dict[str, Any] = {}
    for threshold in (20, 40, 60, 80):
        matching = [row for row in events if row.get("type") != "timing" and float(row.get("value") or 0.0) * 100 >= threshold]
        if not matching:
            checkpoints[str(threshold)] = {"status": "absent"}
            continue
        row = matching[0]
        observed = event_monotonic(row) or started
        elapsed = max(0.0, observed - started)
        shown = float(row.get("value") or 0.0) * 100
        actual = elapsed / duration * 100
        checkpoints[str(threshold)] = {
            "status": "recorded",
            "shown_percent": round(shown, 3),
            "elapsed_seconds": round(elapsed, 3),
            "actual_elapsed_percent": round(actual, 3),
            "progress_error_points": round(shown - actual, 3),
            # ETA is an independently measured value.  The worker event
            # stream does not invent one when no production ETA trace exists.
            "eta_error_seconds": None,
        }
    return checkpoints


def _is_substantive_ingestion_evidence(row: dict[str, Any]) -> bool:
    """True for a real queue/vector observation, not an ETA-only repaint."""
    return (
        not str(row.get("eta_reprice_reason") or "").strip()
        and str(row.get("progress_phase") or "") in {"desktop_queue", "identity_set"}
        and row.get("completed_units") is not None
        and row.get("total_units") is not None
    )


def _checkpoint_tolerance(rows: list[dict[str, Any]], checkpoint_row: dict[str, Any]) -> tuple[float, str | None]:
    """Apply the agreed one-evidence-update reprice allowance."""
    checkpoint_elapsed = float(checkpoint_row.get("elapsed_seconds") or 0.0)
    reprices = [
        row for row in rows
        if str(row.get("eta_reprice_reason") or "").strip()
        and float(row.get("elapsed_seconds") or 0.0) <= checkpoint_elapsed
    ]
    if not reprices:
        return 5.0, None
    latest = reprices[-1]
    reprice_elapsed = float(latest.get("elapsed_seconds") or 0.0)
    has_following_evidence = any(
        _is_substantive_ingestion_evidence(row)
        and float(row.get("elapsed_seconds") or 0.0) > reprice_elapsed
        and float(row.get("elapsed_seconds") or 0.0) <= checkpoint_elapsed
        for row in rows
    )
    if has_following_evidence:
        return 5.0, None
    return 10.0, str(latest.get("eta_reprice_reason") or "")


def retrospective_trace_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calibrate the displayed UI against final duration after completion.

    This deliberately reads the visible percentage from the production trace,
    rather than reusing raw queue/vector evidence or its own forecast.
    """
    final_duration = max((float(row.get("elapsed_seconds") or 0.0) for row in rows), default=0.0)
    checkpoints: dict[str, Any] = {}
    for threshold in (20, 40, 60, 80):
        matching = [
            row for row in rows
            if float(row.get("visible_progress_percent") or 0.0) >= threshold
            and float(row.get("visible_progress_percent") or 0.0) < 100.0
            and str(row.get("state") or "").casefold() not in {"successful", "warning"}
        ]
        if not matching or final_duration <= 0.0:
            terminal_crossed = any(float(row.get("visible_progress_percent") or 0.0) >= threshold for row in rows)
            checkpoints[str(threshold)] = {"status": "terminal_only" if terminal_crossed else "absent"}
            continue
        row = matching[0]
        elapsed = max(0.0, float(row.get("elapsed_seconds") or 0.0))
        shown = float(row.get("visible_progress_percent") or 0.0)
        actual = elapsed / final_duration * 100.0
        expected = max(0.0, float(row.get("expected_seconds") or 0.0))
        tolerance, temporary_reprice_reason = _checkpoint_tolerance(rows, row)
        error = round(shown - actual, 3)
        checkpoints[str(threshold)] = {
            "status": "recorded",
            "shown_percent": round(shown, 3),
            "elapsed_seconds": round(elapsed, 3),
            "actual_elapsed_percent": round(actual, 3),
            "progress_error_points": error,
            "allowed_error_points": tolerance,
            "temporary_reprice_allowance": temporary_reprice_reason is not None,
            "reprice_reason": temporary_reprice_reason,
            "eta_error_seconds": round((expected - elapsed) - (final_duration - elapsed), 3),
        }
    return checkpoints


def progress_calibration_passes(checkpoints: dict[str, Any]) -> bool:
    for checkpoint in checkpoints.values():
        if checkpoint.get("status") != "recorded":
            return False
        allowed = float(checkpoint.get("allowed_error_points") or 5.0)
        if allowed not in {5.0, 10.0}:
            return False
        if abs(float(checkpoint.get("progress_error_points") or 0.0)) > allowed:
            return False
    return True


def benchmark_status_state(missing_or_stale: list[tuple[str, int]], calibration_failures: int) -> str:
    if missing_or_stale:
        return "awaiting-rerun"
    if calibration_failures:
        return "calibration-failed"
    return "completed"


def private_environment_baseline() -> dict[str, Any]:
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        revision = "unavailable"
    health = app.anythingllm_observer_api_health(app.DEFAULT_ANYTHINGLLM_API_URL)
    raw = {
        "app_revision": revision,
        "desktop_reachable": bool(health.get("reachable")),
        "desktop_status": health.get("http_status"),
        "desktop_process_seen": app.anythingllm_desktop_process_seen(),
        "provider_category": "configured-local-desktop-route",
        "segmentation_profile": "Page - preserve automatically",
        "workspace_template_version": 1,
    }
    raw["configuration_fingerprint"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return raw


def queue_guard(api_url: str, api_key: str = "") -> dict[str, Any]:
    """Require a connected, quiet observation; never clear another queue."""
    health = app.anythingllm_observer_api_health(api_url)
    if not health.get("reachable"):
        return {"status": "uncertain", "reason": "Desktop API is unavailable", "observations": []}
    choices, _ = app.local_workspace_choices()
    slugs = [str(value) for _label, value in choices if str(value or "").strip()]
    if not slugs:
        return {"status": "idle", "reason": "No existing workspace queue feeds to inspect", "observations": []}
    def observe(slug: str) -> dict[str, Any]:
        observation = pipeline.observe_workspace_embedding_queue_activity(
            api_url, api_key, slug, [], observation_seconds=1.0,
        )
        return {
            "status": observation.get("status"),
            "stream_connected": bool(observation.get("stream_connected")),
            "non_owned_event_count": int(observation.get("non_owned_event_count") or 0),
        }

    # These are passive SSE reads, not concurrent embedding work.  A bounded
    # pool keeps a large historic workspace list from turning the pre-run
    # safety check into an unbounded delay, while still refusing the run if
    # any feed is active or cannot be observed.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(slugs))) as executor:
        observations = list(executor.map(observe, slugs))
    if any(row["non_owned_event_count"] for row in observations):
        return {"status": "active", "reason": "Non-owned queue activity was observed", "observations": observations}
    if not all(row["stream_connected"] for row in observations):
        return {"status": "uncertain", "reason": "A workspace queue feed was unavailable", "observations": observations}
    return {"status": "idle", "reason": "All observed workspace queue feeds were quiet", "observations": observations}


class BenchmarkProgressSink:
    """Small stand-in for Gradio's progress object on the actual UI route.

    ``run_automatic`` still owns all status persistence and ETA repricing.
    This object only preserves the browser-facing calls in private evidence so
    the benchmark never recreates its own parent progress callback.
    """

    def __init__(self, event_path: Path) -> None:
        self.event_path = event_path

    def __call__(self, value=None, *, desc=None, **_kwargs) -> None:
        row = {
            "recorded_monotonic": time.monotonic(),
            "value": value,
            "description": str(desc or ""),
        }
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class ProductionUiTimerSampler:
    """Privately sample the live UI's own paced presentation once per second.

    The normal localhost page uses a server-side one-second timer to render
    ``automatic_live_status_html``.  A direct Python invocation of the
    Gradio handler cannot receive that browser timer automatically, so this
    read-only sampler records its exact progress helper for calibration.  It
    does not submit work, mutate application status, or write public data.
    """

    def __init__(self, event_path: Path, *, run_root: Path, started_epoch: float, interval_seconds: float = 1.0) -> None:
        self.event_path = event_path
        self.run_root = str(run_root)
        self.started_epoch = started_epoch
        self.interval_seconds = interval_seconds
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, name="benchmark-ui-timer", daemon=True)

    def _snapshot(self) -> None:
        record = dict(app.LIVE_AUTOMATIC_RUN_STATUS or {})
        # Do not let a just-finished prior run contaminate the fresh trial.
        if str(record.get("run_root") or "") != self.run_root:
            return
        now = time.time()
        record_started = float(record.get("started_epoch") or 0.0)
        if record_started + 0.001 < self.started_epoch:
            return
        row = {
            "presentation_source": "ui_timer",
            "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_seconds": round(max(0.0, now - record_started), 3),
            "state": str(record.get("state") or ""),
            "phase": str(record.get("phase") or ""),
            "progress_phase": str(record.get("progress_phase") or ""),
            "completed_units": record.get("completed_units"),
            "total_units": record.get("total_units"),
            "evidence_kind": str(record.get("evidence_kind") or ""),
            "eta_reprice_reason": str(record.get("eta_reprice_reason") or ""),
            "expected_seconds": max(0, int(record.get("expected_seconds") or 0)),
            # This is the exact helper used by automatic_live_status_html.
            "visible_progress_percent": app.paced_progress_percent(record, now),
        }
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _sample_until_stopped(self) -> None:
        while not self._stopped.is_set():
            self._snapshot()
            self._stopped.wait(self.interval_seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 2.0))
        # Capture the terminal/frozen state too; trace rows remain responsible
        # for terminal-only checkpoint protection.
        self._snapshot()


def timing_timeline_events(path: Path, *, started_epoch: float, started_monotonic: float) -> list[dict[str, Any]]:
    """Adapt the production timing callback's durable timeline for analysis."""
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            recorded_epoch = datetime.fromisoformat(str(row.get("recorded_at") or "")).timestamp()
        except (TypeError, ValueError):
            recorded_epoch = started_epoch
        relative = max(0.0, recorded_epoch - started_epoch)
        events.append({
            "type": "timing",
            "recorded_monotonic": started_monotonic + relative,
            "phase": str(row.get("stage") or ""),
            "batch_report": {
                "timing_event": row.get("event"),
                "desktop_queue_records_per_minute": row.get("desktop_queue_records_per_minute"),
                "desktop_queue_estimated_remaining_seconds": row.get("desktop_queue_estimated_remaining_seconds"),
                "desktop_queue_observer_state": row.get("desktop_queue_observer_state"),
                "observer_status": row.get("desktop_queue_observer_state"),
            },
        })
    return events


def production_reprices(trace_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read ETA reprices from the production presentation trace exactly once."""
    reprices: list[dict[str, Any]] = []
    prior: int | None = None
    for row in trace_rows:
        try:
            expected = int(row.get("expected_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if prior is not None and expected != prior:
            reprices.append({
                "reason": str(row.get("eta_reprice_reason") or "production_eta_reprice"),
                "elapsed_seconds": round(float(row.get("elapsed_seconds") or 0.0), 3),
                "expected_seconds": expected,
            })
        prior = expected
    return reprices


def observer_uncertainty_reasons(events: list[dict[str, Any]]) -> list[str]:
    """Return calibration blockers from the production observer timeline."""
    uncertain = {"reconnecting", "unknown", "unavailable", "disconnected", "error"}
    states = {
        str((row.get("batch_report") or {}).get("desktop_queue_observer_state") or "").casefold()
        for row in events
        if row.get("type") == "timing"
    }
    return ["observer_uncertainty"] if states & uncertain else []


def trace_observer_uncertainty_reasons(trace_rows: list[dict[str, Any]]) -> list[str]:
    text = " ".join(
        f"{row.get('phase') or ''} {row.get('details') or ''}".casefold()
        for row in trace_rows
    )
    markers = ("sse observer reconnecting", "sse observer unavailable", "sse observer unknown")
    return ["observer_uncertainty"] if any(marker in text for marker in markers) else []


def refresh_public_calibration_eligibility(result: dict[str, Any], *, trace_rows: list[dict[str, Any]] | None = None) -> bool:
    """Add derived exclusions without rewriting the historical run outcome."""
    reconnecting = ((result.get("overlapping_evidence") or {}).get("reconnecting") or {}).get("wall_seconds")
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    observer_uncertain = reconnecting is not None or any(
        marker in serialized
        for marker in ("sse observer reconnecting", "sse observer unavailable", "sse observer unknown")
    ) or bool(trace_observer_uncertainty_reasons(trace_rows or []))
    reasons = list(result.get("invalid_reasons") or [])
    exclusions = list(result.get("calibration_exclusion_reasons") or [])
    changed = False
    if observer_uncertain:
        if "observer_uncertainty" not in reasons:
            reasons.append("observer_uncertainty")
            result["invalid_for_calibration"] = True
            changed = True
        if "observer_uncertainty" not in exclusions:
            exclusions.append("observer_uncertainty")
            changed = True
    # A warning can be operationally complete but follows a different timing
    # path. It is excluded unless a named, tested timing-neutral allowlist is
    # intentionally introduced in a future protocol revision.
    terminal_state = str(result.get("status") or "").casefold()
    if terminal_state and terminal_state != "successful" and "terminal_not_successful" not in exclusions:
        exclusions.append("terminal_not_successful")
        changed = True
    if changed:
        result["invalid_reasons"] = reasons
        result["calibration_exclusion_reasons"] = exclusions
    return changed


def current_runtime_protocol_result(row: dict[str, Any]) -> bool:
    """Return whether a public result measures the current timing protocol."""
    return (
        int(row.get("presentation_controller_version") or 0)
        == PRODUCTION_PRESENTATION_CONTROLLER_VERSION
        and int(row.get("benchmark_runtime_protocol_revision") or 0)
        == BENCHMARK_RUNTIME_PROTOCOL_REVISION
    )


def calibration_eligible_public_result(row: dict[str, Any]) -> bool:
    """Return the strict cohort permitted to influence progress allocation."""
    return bool(
        current_runtime_protocol_result(row)
        and str(row.get("status") or "").casefold() == "successful"
        and bool(row.get("environment_comparable_to_session_baseline"))
        and not bool(row.get("invalid_for_calibration"))
    )


def run_one(document: BenchmarkDocument, source: dict[str, Any], *, trial: int, private_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one production-worker trial and return safe/public plus private data."""
    pdf_path = Path(source["path"])
    source_fingerprint = sha256_file(pdf_path)
    expected_fingerprint = str(source.get("fingerprint") or "")
    if expected_fingerprint and expected_fingerprint != source_fingerprint:
        raise ValueError(f"Private source for {document.document_id} no longer matches its approved fingerprint.")
    with fitz.open(pdf_path) as pdf:
        actual_pages = len(pdf)
    if actual_pages != document.page_count:
        raise ValueError(f"Private source for {document.document_id} no longer matches its approved page count.")
    ocr_manifest = app.automatic_ocr_preflight_manifest([pdf_path])
    ocr_hint = dict((ocr_manifest.get("files") or [{}])[0])
    if str(ocr_hint.get("risk") or "") == "likely":
        raise RuntimeError(f"{document.document_id} is no longer eligible for the low-OCR benchmark.")
    run_id = f"{document.document_id}-t{trial}-{uuid.uuid4().hex[:8]}"
    run_root = private_root / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    browser_progress_path = run_root / "browser-progress-calls.jsonl"
    workspace = app.create_new_document_workspace(
        app.DEFAULT_ANYTHINGLLM_API_URL, "", "", [str(pdf_path)], f"BENCHMARK {run_id}",
    )
    if workspace.get("status") != "created" or not workspace.get("workspace_slug"):
        raise RuntimeError("Benchmark workspace could not be created through the ordinary Desktop route.")
    started = time.monotonic()
    started_epoch = time.time()
    ui_timer_samples_path = run_root / "ui-timer-presentation.jsonl"
    ui_timer_sampler = ProductionUiTimerSampler(
        ui_timer_samples_path,
        run_root=run_root,
        started_epoch=started_epoch,
    )
    # This is deliberately the actual Gradio event handler, not the worker
    # with a benchmark-owned callback.  It therefore uses precisely the
    # presentation controller that customers see, including evidence-based
    # ETA reprices and late-callback/cancellation protections.
    ui_timer_sampler.start()
    try:
        ui_result = app.run_automatic(
            [str(pdf_path)], None, "", "", f"benchmark-{document.document_id}", True,
            app.MODE_NATIVE_UPLOAD_LABEL, "", app.DEFAULT_ANYTHINGLLM_API_URL, "",
            str(workspace["workspace_slug"]), app.NATIVE_UPLOAD_SCOPE_ALL_LABEL, "",
            "Native title header (priority)", False, "", "None", "", "", "Full corpus",
            False, True, True, "Automatic", 0, 0, app.DEFAULT_TARGET_PASSAGE_LENGTH, 0,
            app.SEGMENT_PAGE_LIMIT_LABEL, "", "", "auto", True, True, 0, 0, False,
            False, False, expected_seconds=0, ocr_preflight_manifest=ocr_manifest,
            run_root_override=str(run_root), retain_detailed_evidence=False,
            progress=BenchmarkProgressSink(browser_progress_path),
        )
    finally:
        ui_timer_sampler.stop()
    finished = time.monotonic()
    trace_rows = read_progress_trace(run_root / "progress-trace.jsonl")
    ui_timer_rows = read_progress_trace(ui_timer_samples_path)
    calibration_rows = merge_presentation_rows(trace_rows, ui_timer_rows)
    presentation_events = progress_trace_events(trace_rows)
    observed_events = timing_timeline_events(
        run_root / "timing-evidence-timeline.jsonl",
        started_epoch=started_epoch,
        started_monotonic=started,
    )
    evidence_events = [*presentation_events, *observed_events]
    final_duration = max((float(row.get("elapsed_seconds") or 0.0) for row in trace_rows), default=finished - started)
    attribution = disjoint_wall_clock_attribution(presentation_events, 0.0, final_duration)
    calibration = retrospective_trace_calibration(calibration_rows)
    terminal = dict(app.LIVE_AUTOMATIC_RUN_STATUS or {})
    terminal_state = str(terminal.get("state") or "failed")
    invalid_reasons = []
    calibration_exclusion_reasons = []
    if terminal_state != "successful":
        calibration_exclusion_reasons.append("terminal_not_successful")
    if not trace_rows:
        invalid_reasons.append("missing_attributable_timeline")
    invalid_reasons.extend(observer_uncertainty_reasons(observed_events))
    invalid_reasons.extend(trace_observer_uncertainty_reasons(trace_rows))
    invalid_reasons = list(dict.fromkeys(invalid_reasons))
    calibration_exclusion_reasons.extend(invalid_reasons)
    calibration_exclusion_reasons = list(dict.fromkeys(calibration_exclusion_reasons))
    public_result = {
        "schema_version": 2,
        "document_id": document.document_id,
        "trial": int(trial),
        "profile": "ordinary-page-preserving-upload",
        "presentation_route": "run_automatic_gradio_handler",
        "presentation_controller_version": PRODUCTION_PRESENTATION_CONTROLLER_VERSION,
        "benchmark_runtime_protocol_revision": BENCHMARK_RUNTIME_PROTOCOL_REVISION,
        "page_count": document.page_count,
        "size_bytes": pdf_path.stat().st_size,
        "size_mib": round(pdf_path.stat().st_size / (1024 * 1024), 3),
        "ocr_risk": document.ocr_risk,
        "status": terminal_state,
        "total_wall_seconds": round(final_duration, 3),
        "disjoint_wall_clock_seconds": attribution,
        "disjoint_wall_clock_percent": {
            key: round(value / max(0.001, final_duration) * 100, 3) for key, value in attribution.items()
        },
        "overlapping_evidence": overlapping_evidence_spans(evidence_events),
        "queue_rate_records_per_minute": observed_queue_rate_per_minute(observed_events),
        "progress_calibration": calibration,
        "progress_calibration_passed": progress_calibration_passes(calibration),
        "justified_reprices": production_reprices(trace_rows),
        "invalid_for_calibration": bool(invalid_reasons),
        "invalid_reasons": invalid_reasons,
        "calibration_exclusion_reasons": calibration_exclusion_reasons,
    }
    private_result = {
        "public_result": public_result,
        "source_path": str(pdf_path),
        "source_fingerprint": source_fingerprint,
        "workspace_slug": workspace.get("workspace_slug"),
        "workspace_name": workspace.get("workspace_name"),
        "gradio_result": ui_result,
        "browser_progress_path": str(browser_progress_path),
        "ui_timer_samples_path": str(ui_timer_samples_path),
        "timing_evidence_path": str(run_root / "timing-evidence-timeline.jsonl"),
        "run_root": str(run_root),
        "environment": private_environment_baseline(),
    }
    assert_public_payload_safe(public_result, forbidden_values=[pdf_path.name, str(workspace.get("workspace_slug") or "")])
    return public_result, private_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-map", type=Path, required=True, help="Ignored map from B01..B08 to local sources.")
    parser.add_argument("--document-id", choices=[f"B{index:02d}" for index in range(1, 9)])
    parser.add_argument("--trial", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--warm-up",
        action="store_true",
        help="Run one excluded readiness check; retain private evidence only and do not alter the 16-trial cohort.",
    )
    parser.add_argument("--private-root", type=Path, default=PRIVATE_ROOT)
    parser.add_argument("--status-path", type=Path, default=Path("benchmarks/results/benchmark-status.json"))
    parser.add_argument("--rerun", action="store_true", help="Replace an existing result for the same document/trial.")
    parser.add_argument(
        "--session-id",
        default="medium-low-ocr-v1-session",
        help="Reuse for both trials so configuration drift can make the pair incomparable.",
    )
    args = parser.parse_args(argv)
    if args.warm_up and not args.document_id:
        parser.error("--warm-up requires exactly one --document-id")
    manifest = load_manifest()
    source_map = load_private_source_map(args.private_map)
    selected = [args.document_id] if args.document_id else sorted(manifest)
    if args.warm_up:
        # A provider/Desktop readiness check is operationally valuable, but it
        # is deliberately not a benchmark trial: it may include cold-start
        # work and must never occupy or replace one of the 16 public slots.
        document_id = selected[0]
        guard = queue_guard(app.DEFAULT_ANYTHINGLLM_API_URL)
        if guard["status"] != "idle":
            raise RuntimeError(f"Warm-up blocked: {guard['reason']}")
        _public_result, private_result = run_one(
            manifest[document_id], source_map[document_id], trial=args.trial,
            private_root=args.private_root,
        )
        warmup_path = args.private_root / "warm-ups" / f"{document_id}-{uuid.uuid4().hex[:8]}.json"
        write_json(warmup_path, {
            "purpose": "excluded_desktop_provider_warm_up",
            "document_id": document_id,
            "private_result": private_result,
        })
        return 0
    public_results_dir = args.status_path.parent / "runs"
    public_results_dir.mkdir(parents=True, exist_ok=True)
    session_root = args.private_root / "sessions" / re.sub(r"[^a-zA-Z0-9_-]+", "-", args.session_id).strip("-")
    session_root.mkdir(parents=True, exist_ok=True)
    baseline_path = session_root / "environment-baseline.json"
    baseline = private_environment_baseline()
    if baseline_path.is_file():
        prior = read_json(baseline_path).get("baseline") or {}
    else:
        prior = baseline
        write_json(baseline_path, {"baseline": baseline})
    completed = []
    for document_id in selected:
        prior_result_path = public_results_dir / f"{document_id}-trial-{args.trial}.json"
        if prior_result_path.is_file() and not args.rerun:
            try:
                completed.append(read_json(prior_result_path))
                continue
            except (OSError, json.JSONDecodeError):
                # A damaged public result must not be silently trusted; the
                # new run will replace it only after a complete safe write.
                pass
        # Check immediately before every serial submission. The runner never
        # treats its earlier quiet observation as permission to ignore later
        # manual activity.
        guard = queue_guard(app.DEFAULT_ANYTHINGLLM_API_URL)
        if guard["status"] != "idle":
            status = {
                "state": "blocked",
                "completed_run_count": len(completed),
                "next_action": guard["reason"],
                "queue_guard": guard,
                "runs": completed,
            }
            assert_public_payload_safe(status)
            write_json(args.status_path, status)
            return 2
        public_result, private_result = run_one(manifest[document_id], source_map[document_id], trial=args.trial, private_root=args.private_root)
        current = dict(private_result.get("environment") or {})
        comparable = str(current.get("configuration_fingerprint") or "") == str(prior.get("configuration_fingerprint") or "")
        public_result["environment_comparable_to_session_baseline"] = comparable
        if not comparable:
            public_result["invalid_for_calibration"] = True
            public_result["invalid_reasons"] = [*public_result["invalid_reasons"], "environment_changed"]
            public_result["calibration_exclusion_reasons"] = [
                *public_result["calibration_exclusion_reasons"], "environment_changed"
            ]
        # Evidence is durable before any future cleanup implementation is
        # allowed to consider deletion. Default behavior is preservation;
        # cleanup needs a separate, thread-aware ownership gate.
        public_result["cleanup_state"] = "preserved_pending_thread_safe_gate"
        write_json(args.private_root / f"{document_id}-trial-{args.trial}.json", private_result)
        assert_public_payload_safe(public_result)
        if args.rerun:
            archive_invalid_public_result(prior_result_path)
        write_json(public_results_dir / f"{document_id}-trial-{args.trial}.json", public_result)
        completed.append(public_result)
    published_runs = []
    for result_path in sorted(public_results_dir.glob("B??-trial-[12].json")):
        try:
            row = read_json(result_path)
        except (OSError, json.JSONDecodeError):
            continue
        private_trace_rows = []
        private_result_path = args.private_root / f"{row.get('document_id')}-trial-{row.get('trial')}.json"
        try:
            private_result = read_json(private_result_path)
            private_trace_rows = read_progress_trace(
                Path(str(private_result.get("run_root") or "")) / "progress-trace.jsonl"
            )
        except (OSError, json.JSONDecodeError):
            pass
        if refresh_public_calibration_eligibility(row, trace_rows=private_trace_rows):
            write_json(result_path, row)
        published_runs.append(row)
    expected_keys = {
        (document_id, trial)
        for document_id in manifest
        for trial in (1, 2)
    }
    current_protocol_keys = {
        (str(row.get("document_id") or ""), int(row.get("trial") or 0))
        for row in published_runs
        if current_runtime_protocol_result(row)
    }
    fresh_keys = {
        (str(row.get("document_id") or ""), int(row.get("trial") or 0))
        for row in published_runs
        if calibration_eligible_public_result(row)
    }
    missing_or_stale = sorted(expected_keys - current_protocol_keys)
    calibration_failures = sum(
        not bool(row.get("progress_calibration_passed"))
        for row in published_runs
        if (str(row.get("document_id") or ""), int(row.get("trial") or 0)) in fresh_keys
    )
    status = {
        "state": benchmark_status_state(missing_or_stale, calibration_failures),
        # A historical 16-row directory is not a completed timing set after a
        # presentation-controller change. Keep this separately explicit so a
        # reader cannot mistake stale trials for a calibration-ready cohort.
        "timing_runs_state": (
            "completed"
            if not missing_or_stale and len(current_protocol_keys) == len(expected_keys)
            else "awaiting-rerun"
        ),
        "completed_run_count": len(published_runs),
        "calibration_eligible_run_count": len(fresh_keys),
        "invalid_for_calibration_count": sum(bool(row["invalid_for_calibration"]) for row in published_runs),
        "calibration_acceptance": "failed" if calibration_failures else ("passed" if len(fresh_keys) == 16 else "pending"),
        "calibration_failure_count": calibration_failures,
        "elapsed_seconds": round(sum(float(row["total_wall_seconds"]) for row in published_runs), 3),
        "next_action": (
            f"Run {len(missing_or_stale)} remaining or stale serial trial(s) through the production presentation route."
            if missing_or_stale
            else "All production-route trials are present; repair any failed calibration or reliability findings before reweighting."
        ),
        "missing_or_stale_runs": [f"{document_id}-trial-{trial}" for document_id, trial in missing_or_stale],
        "runs": published_runs,
    }
    assert_public_payload_safe(status)
    write_json(args.status_path, status)
    # Import lazily to avoid the report module's public-contract helpers
    # creating an import cycle while this runner is used as a library.
    from benchmarks.report import write_report
    write_report(args.status_path.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
