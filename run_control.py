"""Persisted run/checkpoint/timing primitives shared by UI, CLI, and tests.

The recorder is a durable lifecycle envelope, not an alternate PDF pipeline.
It writes checkpoints while a run can still need recovery. A ready lean run is
allowed to compact that envelope into ``run-summary.json`` and remove duplicate
checkpoint files; review-needed and failed runs retain the richer evidence.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


RUN_SCHEMA_VERSION = 1
MAJOR_STAGES = (
    "compatibility_fingerprint",
    "state_resolution",
    "preflight",
    "legacy_preparation_engine",
    "pdf_extraction_normalization",
    "segmentation",
    "artifact_writing",
    "settings_mutation",
    "upload",
    "post_upload_verification",
    "reporting",
    "cleanup",
)
TERMINAL_STAGE_STATUSES = {"success", "degraded", "blocked", "failed", "skipped"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict, *, retries: int = 3) -> None:
    """Durably replace a recovery record without ever exposing partial JSON.

    Windows antivirus and file indexing can hold a just-written checkpoint for
    a moment. Retry only that transient sharing violation; all other failures
    remain visible to the caller and the previous checkpoint is preserved.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        attempts = max(1, int(retries or 1))
        for attempt in range(attempts):
            try:
                os.replace(temporary, path)
                temporary = None
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.08 * (attempt + 1))
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass
class CleanupObligation:
    kind: str
    resource_id: str
    status: str = "pending"
    message: str = ""


@dataclass
class StageResult:
    stage: str
    status: str = "pending"
    started_at: str = ""
    ended_at: str = ""
    elapsed_seconds: float = 0.0
    safe_retry: bool = True
    artifacts: list[str] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    operator_message: str = ""


@dataclass
class RunResult:
    run_id: str
    output_root: str
    selected_mode: str
    selected_policy: dict
    status: str = "running"
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    total_elapsed_seconds: float = 0.0
    compatibility: dict = field(default_factory=dict)
    resolved_state: dict = field(default_factory=dict)
    stages: dict[str, StageResult] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    cleanup_obligations: list[CleanupObligation] = field(default_factory=list)
    upload_evidence: dict = field(default_factory=dict)
    verification_evidence: dict = field(default_factory=dict)
    operator_summary: str = ""

    def to_dict(self):
        return asdict(self)


class RunRecorder:
    def __init__(self, result: RunResult):
        self.result = result
        self.output_root = Path(result.output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.output_root / "run-checkpoint.json"
        self.event_path = self.output_root / "run-checkpoints.jsonl"
        self._run_started = time.perf_counter()
        self.persist("run_started")

    def persist(self, event):
        payload = self.result.to_dict()
        payload["event"] = event
        payload["persisted_at"] = utc_now()
        atomic_write_json(self.checkpoint_path, payload)
        # ``run-checkpoint.json`` is the authoritative resumable snapshot.
        # Repeating that entire, ever-growing object in JSONL made diagnostic
        # history quadratic for large documents (large evidence blobs were
        # copied once per stage transition). Keep the event stream durable but
        # store only the transition and a compact stage-status index.
        event_payload = {
            "schema_version": 2,
            "run_id": self.result.run_id,
            "event": event,
            "persisted_at": payload["persisted_at"],
            "status": self.result.status,
            "stage_statuses": {
                name: stage.status for name, stage in self.result.stages.items()
            },
        }
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def execute(self, stage, action: Callable[[], Any], safe_retry=True):
        if stage not in MAJOR_STAGES:
            raise ValueError(f"Unknown major stage: {stage}")
        record = StageResult(stage=stage, status="running", started_at=utc_now(), safe_retry=safe_retry)
        self.result.stages[stage] = record
        self.persist(f"{stage}:started")
        started = time.perf_counter()
        try:
            value = action()
            record.status = "success"
            if isinstance(value, dict):
                record.evidence = value
            record.operator_message = f"{stage.replace('_', ' ').title()} completed."
            return value
        except Exception as exc:
            record.status = "failed"
            record.errors.append(f"{type(exc).__name__}: {exc}")
            record.evidence["traceback"] = traceback.format_exc()
            record.operator_message = f"{stage.replace('_', ' ').title()} failed."
            self.result.status = "error"
            raise
        finally:
            record.ended_at = utc_now()
            record.elapsed_seconds = round(time.perf_counter() - started, 4)
            self.persist(f"{stage}:{record.status}")

    def skip(self, stage, reason):
        record = StageResult(
            stage=stage,
            status="skipped",
            started_at=utc_now(),
            ended_at=utc_now(),
            operator_message=reason,
        )
        self.result.stages[stage] = record
        self.persist(f"{stage}:skipped")

    def finish(self, status, summary):
        self.result.status = status
        self.result.operator_summary = summary
        self.result.ended_at = utc_now()
        self.result.total_elapsed_seconds = round(time.perf_counter() - self._run_started, 4)
        self.persist("run_finished")
        atomic_write_json(self.output_root / "run-result.json", self.result.to_dict())
        return self.result
