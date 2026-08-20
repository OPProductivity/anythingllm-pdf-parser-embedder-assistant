from __future__ import annotations

import json
from pathlib import Path
import threading

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


def test_worker_terminal_json_writer_stays_valid_with_overlapping_writers(tmp_path: Path):
    result_path = tmp_path / "result.json"
    start = threading.Event()
    writers = [
        threading.Thread(
            target=lambda index=index: (start.wait(), worker._write_json(result_path, {"writer": index})),
        )
        for index in range(12)
    ]
    for writer in writers:
        writer.start()
    start.set()
    for writer in writers:
        writer.join(timeout=10)

    assert all(not writer.is_alive() for writer in writers)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["writer"] in range(12)
    assert list(tmp_path.glob(".result.json.*.tmp")) == []
