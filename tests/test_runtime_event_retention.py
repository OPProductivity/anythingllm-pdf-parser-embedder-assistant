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
    journal = [json.loads(line) for line in path.with_suffix('.events.jsonl').read_text().splitlines()]
    assert [row['event']['position'] for row in journal] == list(range(109))
    assert ledger['runtime_event_history_complete'] is True
    pipeline._write_embedding_batch_ledger(path, "workspace", aggregate)
    assert len(path.with_suffix('.events.jsonl').read_text().splitlines()) == 109


def test_child_already_capped_total_survives_merge():
    aggregate = {"runtime_events": [{"event": "earlier"}]}
    pipeline._merge_embedding_runtime_events(aggregate, {
        "runtime_events": [{"position": index} for index in range(96)],
        "runtime_event_count": 400,
    })
    assert aggregate["runtime_event_count"] == 401
    assert len(aggregate["runtime_events"]) == 97


def test_small_queue_is_not_marked_truncated(tmp_path):
    aggregate = {"runtime_events": []}
    pipeline._merge_embedding_runtime_events(aggregate, {"runtime_events": [{"event": "receipt"}]})
    path = tmp_path / "ledger.json"
    pipeline._write_embedding_batch_ledger(path, "workspace", aggregate)
    ledger = json.loads(path.read_text())
    assert ledger["runtime_event_count"] == 1
    assert ledger["runtime_events_truncated"] is False


def test_journal_restart_and_new_sequence_preserve_prior_evidence(tmp_path):
    import runtime_event_journal as journal

    path = tmp_path / 'events.jsonl'
    first = [{'event': 'first'}, {'event': 'second'}]
    journal.retain_runtime_events(path, first)
    journal._STATE.pop(path.resolve())
    journal.retain_runtime_events(path, first + [{'event': 'third'}])
    journal.retain_runtime_events(path, first)
    result = journal.retain_runtime_events(path, [{'event': 'resumed'}])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row['event']['event'] for row in rows] == ['first', 'second', 'third', 'resumed']
    assert [row['sequence'] for row in rows] == [1, 1, 1, 2]
    assert result['retained_events'] == 4
