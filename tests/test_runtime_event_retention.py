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
    assert not hasattr(journal, '_STATE')
    journal.retain_runtime_events(path, first + [{'event': 'third'}])
    journal.retain_runtime_events(path, first)
    result = journal.retain_runtime_events(path, [{'event': 'resumed'}])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row['event']['event'] for row in rows] == ['first', 'second', 'third', 'resumed']
    assert [row['sequence'] for row in rows] == [1, 1, 1, 2]
    assert result['retained_events'] == 4


def test_observation_scope_marks_intermediate_ledger_and_resets_after_error(tmp_path):
    import runtime_event_journal as journal

    path = tmp_path / 'ledger.json'
    aggregate = {'runtime_events': [{'event': 'receipt'}]}
    with pytest.raises(RuntimeError):
        with journal.observation_scope(path.with_suffix('.events.jsonl')):
            pipeline._write_embedding_batch_ledger(path, 'workspace', aggregate)
            assert json.loads(path.read_text())['runtime_event_history_complete'] is False
            raise RuntimeError('observer interrupted')
    pipeline._write_embedding_batch_ledger(path, 'workspace', aggregate)
    assert json.loads(path.read_text())['runtime_event_history_complete'] is True


def test_unstopped_observer_cannot_claim_complete_history(tmp_path):
    path = tmp_path / 'ledger.json'
    aggregate = {'runtime_events': [{'event': 'receipt'}],
                 'runtime_event_observation_complete': False}
    pipeline._write_embedding_batch_ledger(path, 'workspace', aggregate)
    assert json.loads(path.read_text())['runtime_event_history_complete'] is False


def test_journal_rejects_corrupt_position(tmp_path):
    import runtime_event_journal as journal

    path = tmp_path / 'events.jsonl'
    path.write_text(json.dumps({'sequence': 1, 'position': 2, 'event': {}}) + '\n')
    with pytest.raises(ValueError, match='position'):
        journal.retain_runtime_events(path, [{}])


def test_shorter_prefix_does_not_claim_exact_complete_history(tmp_path):
    path = tmp_path / 'ledger.json'
    pipeline._write_embedding_batch_ledger(path, 'workspace', {
        'runtime_events': [{'event': 'first'}, {'event': 'second'}]})
    pipeline._write_embedding_batch_ledger(path, 'workspace', {
        'runtime_events': [{'event': 'first'}]})
    assert json.loads(path.read_text())['runtime_event_history_complete'] is False
