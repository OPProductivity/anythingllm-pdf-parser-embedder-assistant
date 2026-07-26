from __future__ import annotations

import json
from pathlib import Path

import pytest

import cancellable_preparation_worker as worker


pytestmark = pytest.mark.offline_deterministic


def test_worker_preserves_keyword_rich_pipeline_timing_payload(tmp_path: Path):
    events = tmp_path / "events.jsonl"

    worker._emit_timing_event(
        events,
        "identity_set",
        {"timing_event": "phase_completed", "phase_elapsed_seconds": 2.5},
        desktop_queue_observer_state="connected",
    )

    row = json.loads(events.read_text(encoding="utf-8"))
    assert row["type"] == "timing"
    assert row["recorded_monotonic"] > 0
    assert row["batch_report"] == {
        "timing_event": "phase_completed",
        "phase_elapsed_seconds": 2.5,
        "desktop_queue_observer_state": "connected",
    }
