import sqlite3
import threading
from unittest.mock import Mock

import pytest

import auto_anythingllm_pipeline as pipeline
from ingestion_observation import SubmissionCommitSignal


pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def storage(tmp_path):
    with sqlite3.connect(tmp_path / 'anythingllm.db') as connection:
        connection.executescript('''
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY, slug TEXT);
            CREATE TABLE workspace_documents (workspaceId INTEGER, docpath TEXT, docId TEXT);
            CREATE TABLE document_vectors (docId TEXT, vectorId TEXT);
            INSERT INTO workspaces VALUES (1, 'target'), (2, 'other');
            INSERT INTO workspace_documents VALUES (1, 'custom-documents/one.json', 'one');
            INSERT INTO document_vectors VALUES ('one', 'vector-one');
        ''')
    return tmp_path


def test_missing_completion_event_does_not_impose_90_seconds(storage):
    signal = SubmissionCommitSignal(storage, 'target')
    queue = {'queue_records': 1, 'desktop_queue_current': 1,
             'desktop_queue_completed': 0, 'desktop_queue_observer_state': 'connected',
             'desktop_queue_last_event_age_seconds': 6}
    common = dict(has_cached_evidence=True, last_observation_position=1,
                  elapsed_seconds=6, last_observation_elapsed_seconds=0)
    assert not pipeline.storage_observation_due_for_queue(queue, 1, **common)
    assert pipeline.storage_observation_due_for_queue(
        queue, 1, **common,
        committed_records_complete=signal.observe(['custom-documents/one.json']))


def test_signal_requires_every_record_in_correct_workspace(storage):
    signal = SubmissionCommitSignal(storage, 'target')
    assert not signal.observe(['custom-documents/one.json', 'custom-documents/two.json'])
    assert not SubmissionCommitSignal(storage, 'other').observe(['custom-documents/one.json'])
    assert signal.observe(['custom-documents/one.json'])
    assert not signal.observe([])


def test_commit_after_initial_incomplete_observation_wakes_verification(storage):
    signal = SubmissionCommitSignal(storage, 'target')
    locations = ['custom-documents/one.json', 'custom-documents/two.json']
    assert not signal.observe(locations)
    with sqlite3.connect(storage / 'anythingllm.db') as connection:
        connection.execute('INSERT INTO workspace_documents VALUES (1, ?, ?)', (locations[1], 'two'))
        connection.execute("INSERT INTO document_vectors VALUES ('two', 'vector-two')")
    assert signal.observe(locations)


def test_unchanged_mapping_does_not_repeat_sqlite_query(storage, monkeypatch):
    signal = SubmissionCommitSignal(storage, 'target')
    assert signal.observe(['custom-documents/one.json'])
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: pytest.fail('repeated query'))
    assert signal.observe(['custom-documents/one.json'])


def test_busy_storage_recovers_without_becoming_failure(storage):
    signal = SubmissionCommitSignal(storage, 'target')
    connection = sqlite3.connect(storage / 'anythingllm.db')
    try:
        connection.execute('BEGIN EXCLUSIVE')
        assert not signal.observe(['custom-documents/one.json'])
    finally:
        connection.close()
    assert signal.observe(['custom-documents/one.json'])


def test_missing_database_is_not_created(tmp_path):
    assert not SubmissionCommitSignal(tmp_path, 'target').observe(['one.json'])
    assert not (tmp_path / 'anythingllm.db').exists()


def test_foreign_activity_ends_recovery_probe_without_waiting(monkeypatch):
    event = {'type': 'doc_starting', 'filename': 'custom-documents/foreign.json'}
    connected = threading.Event()
    connected.set()
    listener = {'connected_event': connected, 'stop_event': threading.Event(),
                'thread': Mock(), 'events': [event], 'errors': []}

    def start(*args, **kwargs):
        kwargs['observer_callback'](event)
        return listener

    monkeypatch.setattr(pipeline, 'start_anythingllm_embed_progress_listener', start)
    waits = []
    original_wait = threading.Event.wait

    def wait(self, timeout=None):
        # The settle event must already be signalled, not spend its budget.
        waits.append(self.is_set())
        return original_wait(self, timeout=0)

    monkeypatch.setattr(threading.Event, 'wait', wait)
    result = pipeline.observe_workspace_embedding_queue_activity(
        'http://local', '', 'target', ['custom-documents/owned.json'], observation_seconds=3)
    assert waits == [True]
    assert result['status'] == 'non_owned_activity_observed'
    assert not result['automatic_mutation_allowed']
    assert not result['automatic_restart_allowed']
    assert listener['stop_event'].is_set()
