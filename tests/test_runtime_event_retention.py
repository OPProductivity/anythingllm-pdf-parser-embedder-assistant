import json

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def test_merged_queue_tail_preserves_true_total_across_multiple_groups(tmp_path):
    aggregate = {"runtime_events": []}
    pipeline._merge_embedding_runtime_events(
        aggregate, {"runtime_events": [{"position": index} for index in range(108)]},
    )
    pipeline._merge_embedding_runtime_events(
        aggregate, {"runtime_events": [{"position": 108}]},
    )
    path = tmp_path / "ledger.json"
    pipeline._write_embedding_batch_ledger(path, "workspace", aggregate)
    ledger = json.loads(path.read_text())
    assert ledger["runtime_event_count"] == 109
    assert ledger["runtime_events_truncated"] is True
    assert len(ledger["runtime_events"]) == 96
    assert ledger["runtime_events"][0]["position"] == 13
    assert ledger["runtime_events"][-1]["position"] == 108


def test_child_already_capped_total_survives_merge():
    aggregate = {"runtime_events": [{"event": "earlier"}]}
    pipeline._merge_embedding_runtime_events(aggregate, {
        "runtime_events": [{"position": index} for index in range(96)],
        "runtime_event_count": 400,
    })
    assert aggregate["runtime_event_count"] == 401
    assert len(aggregate["runtime_events"]) == 96


def test_small_queue_is_not_marked_truncated(tmp_path):
    aggregate = {"runtime_events": []}
    pipeline._merge_embedding_runtime_events(aggregate, {"runtime_events": [{"event": "receipt"}]})
    path = tmp_path / "ledger.json"
    pipeline._write_embedding_batch_ledger(path, "workspace", aggregate)
    ledger = json.loads(path.read_text())
    assert ledger["runtime_event_count"] == 1
    assert ledger["runtime_events_truncated"] is False
