import json
import threading
import hashlib
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
from experiments.audit_isolated_live_replay_20261003 import exact_source_identity, journal_events

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('case,outcome,expected', [
    ({'sources': ['a', 'b']}, {'document_results': {'a': {}, 'b': {}}}, True),
    ({'sources': ['a', 'b']}, {'document_results': {'a': {}, 'c': {}}}, False),
    ({'source': 'a'}, {'document_results': {}}, False),
    ({}, {'document_results': {}}, False),
    ({'sources': ['a', 'a']}, {'document_results': {'a': {}}}, False),
])
def test_source_identity_requires_exact_nonempty_unique_set(case, outcome, expected):
    assert exact_source_identity(case, outcome) is expected


@pytest.mark.parametrize('still_alive', [False, True])
def test_desktop_queue_final_ledger_contains_observer_events(tmp_path, monkeypatch, still_alive):
    path = tmp_path / 'ledger.json'
    connected = threading.Event()
    connected.set()
    observed = [{'type': 'doc_complete', 'filename': 'custom/a.txt'}]

    class ObserverThread:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return still_alive

    monkeypatch.setattr(pipeline, 'start_anythingllm_embed_progress_listener', lambda *a, **k: {
        'thread': ObserverThread(), 'stop_event': threading.Event(),
        'connected_event': connected, 'events': observed, 'errors': [],
    })

    def submit(*args, **kwargs):
        result = {'runtime_events': [{'event': 'receipt'}], 'batches': [], 'requested': 1}
        pipeline._write_embedding_batch_ledger(path, 'workspace', result)
        assert json.loads(path.read_text())['runtime_event_history_complete'] is False
        return result

    monkeypatch.setattr(pipeline, 'update_workspace_embeddings_batched', submit)
    result = pipeline.update_workspace_embeddings_desktop_queue(
        'http://unused', 'unused', 'workspace', ['custom/a.txt'], ledger_path=path)
    ledger = json.loads(path.read_text())
    assert ledger['runtime_event_count'] == 2
    assert ledger['runtime_event_history_complete'] is (not still_alive)
    if still_alive:
        with pytest.raises(ValueError, match='Incomplete'):
            journal_events(path)
    else:
        assert journal_events(path) == result['runtime_events']


def test_journal_audit_resolves_latest_observation_sequence(tmp_path):
    path = tmp_path / 'ledger.json'
    pipeline._write_embedding_batch_ledger(path, 'workspace', {'runtime_events': [{'event': 'old'}]})
    pipeline._write_embedding_batch_ledger(path, 'workspace', {'runtime_events': [{'event': 'new'}]})
    assert journal_events(path) == [{'event': 'new'}]


@pytest.mark.parametrize('status', ['review_needed', 'assessment_incomplete'])
@pytest.mark.parametrize('canonical', [True, False])
def test_diagnostic_directions_use_canonical_roles(status, canonical):
    selected = {
        'quality': {'scanned_likelihood': 'possible'},
        'visual_text_review': {'status': status, 'unresolved_page_count': 1},
        'artifact_paths': {'visual-text-review.json': 'private/canonical-visual.json',
                           'extraction-report.csv': 'private/canonical-extraction.csv'} if canonical else {},
    }
    diagnostics = pipeline.build_run_diagnostics({}, selected, [], {}, {}, {}, {}, {}, {}, {})
    actions = '\n'.join(row['recommended_action'] for row in diagnostics)
    if canonical:
        assert 'private/canonical-visual.json' in actions
        assert 'private/canonical-extraction.csv' in actions
    else:
        assert 'artifact-locations.json (role: visual-text-review.json)' in actions
        assert 'artifact-locations.json (role: extraction-report.csv)' in actions


def test_replay_always_launches_fresh_workers_without_replacing_old_receipt(tmp_path, monkeypatch):
    from experiments import historical_preparation_replay_20261003 as replay

    code_root = replay.REPO
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'unchanged fixture source')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    original = tmp_path / 'original/automatic-runs/run/document'
    replay.write(original / '.automatic-worker-config.json', {'pdf_path': str(source), 'args': {}})
    replay.write(original / 'run-summary.json', {'manifest': 'historical'})
    receipts = tmp_path / 'tmp-output/historical-replay-20261003'
    replay.write(receipts / 'inventory.json', {
        'unique_content': {digest: [str(source)]}, 'sources': [{'path': str(source), 'pages': 1}]})
    historical_receipt = receipts / 'results.json'
    historical_receipt.write_text('preserve earlier evidence')
    monkeypatch.setattr(replay, 'REPO', tmp_path)
    monkeypatch.setattr(replay, 'application_paths', lambda: {'run_state': tmp_path / 'original'})
    monkeypatch.setattr(replay.subprocess, 'check_output', lambda *a, **k: 'fixture-commit')
    monkeypatch.setattr(replay, 'read_manifest_rows', lambda path: [{'text': 'body'}])
    monkeypatch.setattr(replay.sys, 'argv', ['replay', '--limit', '1', '--code-root', str(code_root)])
    launched = []

    class FreshWorker:
        def __init__(self, command, **kwargs):
            assert kwargs['cwd'] == code_root
            assert command[1:3] == ['-m', 'cancellable_preparation_worker']
            config = json.loads(Path(command[-1]).read_text())
            result = Path(config['result_path'])
            assert not result.exists()
            launched.append(result)
            replay.write(result, {'status': 'completed'})
            replay.write(Path(config['output_dir']) / 'run-summary.json', {'manifest': 'fresh'})

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(replay.subprocess, 'Popen', FreshWorker)
    assert replay.main() == 0
    assert replay.main() == 0
    assert len(launched) == 2 and launched[0] != launched[1]
    assert historical_receipt.read_text() == 'preserve earlier evidence'
    new_receipts = list(receipts.glob('preparation-*/results.json'))
    assert len(new_receipts) == 2
    for path in new_receipts:
        report = json.loads(path.read_text())
        assert report['worker_code_root'] == str(code_root)
        assert report['worker_pipeline_sha256'] == hashlib.sha256(
            (code_root / 'auto_anythingllm_pipeline.py').read_bytes()).hexdigest()
