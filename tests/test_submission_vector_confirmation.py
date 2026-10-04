import csv
import sqlite3
from unittest import mock

import lancedb
import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
from ingestion_observation import observe_submission_vector_ids


pytestmark = pytest.mark.offline_deterministic


def storage_fixture(tmp_path, *, physical=True, namespace='target', expanded=False):
    location = 'custom-documents/owned.json'
    connection = sqlite3.connect(tmp_path / 'anythingllm.db')
    connection.executescript('''
        create table workspaces(id integer, name text, slug text);
        create table workspace_documents(docId text, filename text, docpath text,
                                        workspaceId integer, metadata text);
        create table document_vectors(docId text, vectorId text);
    ''')
    connection.execute('insert into workspaces values (1, ?, ?)', ('Target', 'target'))
    connection.execute('insert into workspace_documents values (?, ?, ?, 1, ?)',
                       ('owned-doc', 'owned.txt', location, '{}'))
    ids = ['owned-1', 'owned-2', 'owned-3'] if expanded else ['owned-1']
    connection.executemany('insert into document_vectors values (?, ?)',
                           [('owned-doc', identity) for identity in ids])
    connection.commit()
    connection.close()
    if physical:
        lancedb.connect(str(tmp_path / 'lancedb')).create_table(namespace, data=[
            {'id': identity, 'vector': [1.0, 2.0, 3.0]} for identity in ids
        ])
    return location, ids


@pytest.mark.parametrize('physical,namespace,expanded,complete', [
    (False, 'target', False, False),
    (True, 'other-workspace', False, False),
    (True, 'target', False, True),
    (True, 'target', True, True),
])
def test_mappings_require_exact_physical_ids_in_target(tmp_path, physical, namespace, expanded, complete):
    location, ids = storage_fixture(tmp_path, physical=physical, namespace=namespace, expanded=expanded)
    with mock.patch.object(pipeline, 'inspect_uploaded_location_files', return_value={
        'matching_files': 1, 'existing_files': 1,
    }), mock.patch.object(pipeline, 'inspect_native_metadata_count', return_value={
        'matching_rows': 0, 'matching_table_names': [],
        'identity_set_checked': True, 'identity_set_complete': False,
    }):
        report = pipeline.verify_anythingllm_post_upload(
            tmp_path, 'target', '', [{'metadata': {'chunkSource': 'segment://owned'}}],
            upload_locations=[location], observation_mode='fast',
        )
    assert report['current_upload_mapping_evidence_complete']
    assert report['current_upload_vector_evidence_complete'] == complete
    assert report['status'] == ('pass' if complete else 'partial_vector_coverage')
    assert report['current_upload_documents_with_vectors'] == 1
    assert report['current_upload_document_vector_count'] == len(ids)
    assert report['current_upload_locations_with_vectors'] == ([location] if complete else [])


def test_old_physical_vectors_cannot_impersonate_new_mapping(tmp_path):
    db = lancedb.connect(str(tmp_path / 'lancedb'))
    db.create_table('target', data=[{'id': 'old-vector', 'vector': [1.0, 2.0, 3.0]}])
    result = observe_submission_vector_ids(tmp_path, 'target', {'owned': {'new-vector'}})
    assert result['status'] == 'complete'
    assert not result['complete']
    assert result['matched_vector_count'] == 0


def test_one_missing_internal_vector_keeps_record_unconfirmed(tmp_path):
    db = lancedb.connect(str(tmp_path / 'lancedb'))
    db.create_table('target', data=[{'id': 'one', 'vector': [1.0, 2.0, 3.0]}])
    result = observe_submission_vector_ids(tmp_path, 'target', {'owned': {'one', 'two'}})
    assert not result['complete']
    assert result['locations_with_vectors'] == []


@pytest.mark.parametrize('mode,should_read', [('fast', False), ('current_upload', True)])
def test_partial_fast_snapshot_defers_physical_read_but_explicit_recovery_checks(tmp_path, mode, should_read):
    location, _ = storage_fixture(tmp_path)
    with mock.patch.object(pipeline, 'inspect_uploaded_location_files', return_value={
        'matching_files': 2, 'existing_files': 2,
    }), mock.patch.object(pipeline, 'inspect_native_metadata_count', return_value={}), \
            mock.patch.object(pipeline, 'observe_submission_vector_ids',
                              wraps=observe_submission_vector_ids) as physical:
        report = pipeline.verify_anythingllm_post_upload(
            tmp_path, 'target', '', [
                {'metadata': {'chunkSource': 'segment://one'}},
                {'metadata': {'chunkSource': 'segment://two'}},
            ], upload_locations=[location, 'custom-documents/not-yet-mapped.json'],
            observation_mode=mode,
        )
    assert physical.call_count == int(should_read)
    assert not report['current_upload_vector_evidence_complete']
    assert report['current_upload_locations_with_vectors'] == ([location] if should_read else [])


def test_busy_physical_store_is_uncertain_not_success(tmp_path):
    (tmp_path / 'lancedb').mkdir()
    with mock.patch.object(lancedb, 'connect', side_effect=RuntimeError('temporarily busy')):
        result = observe_submission_vector_ids(tmp_path, 'target', {'owned': {'one'}})
    assert result['status'] == 'unavailable'
    assert not result['complete']


def test_verifier_transport_retains_exception_classification_without_replay():
    def broken_callback(_batch):
        raise RuntimeError('broken callback')

    with mock.patch.object(pipeline, 'post_json', return_value=(200, '{}')) as submit:
        result = pipeline.update_workspace_embeddings_batched(
            'http://anythingllm', 'test-key', 'target', ['custom-documents/one.json'],
            batch_verifier=broken_callback,
        )
    submit.assert_called_once()
    evidence = result['batches'][0]['verification']
    assert evidence['classification'] == 'verification_callback_exception'
    assert evidence['exception_type'] == 'RuntimeError'
    assert 'broken_callback' in evidence['traceback']


def test_grouped_outcome_preserves_verifier_exception_for_terminal_reporting(tmp_path):
    summary = {'pdf': str(tmp_path / 'paper.pdf')}
    location = 'custom-documents/one.json'
    text = tmp_path / 'prepared.txt'
    text.write_text('prepared', encoding='utf-8')
    plan = tmp_path / 'plan.csv'
    with plan.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['filename', 'chunkSource', 'docSource', 'text_file'])
        writer.writeheader()
        writer.writerow({'filename': 'one.txt', 'chunkSource': 'segment://one',
                         'docSource': 'local-pdf://fixture', 'text_file': str(text)})
    summary.update({'native_upload_plan': str(plan), 'native_upload_transport': 'file_upload'})
    response = {
        'status': 'reconciliation_pending', 'uploaded': 1, 'embedded': 0,
        'errors': [{'error': 'broken callback'}],
        'attachment_results': [{'source_path': summary['pdf'], 'location': location,
                                'status': 'attached', 'chunk_source': 'segment://one'}],
        'embedding_update': {'batches': [{
            'locations': [location], 'requested': 1, 'submission_state': 'verification_failed',
            'verification': {'classification': 'verification_callback_exception',
                             'exception_type': 'RuntimeError',
                             'current_upload_vector_evidence_complete': False,
                             'current_upload_locations_with_vectors': [],
                             'observed_chunk_sources': ['segment://one']},
        }]},
    }
    with mock.patch.object(app, 'maybe_upload_to_anythingllm', return_value=response):
        app.upload_prepared_automatic_batch(
            [summary], api_url='http://anythingllm', api_key='', workspace_slug='target',
            run_root=tmp_path / 'run',
        )
    assert summary['api_verification_error_type'] == 'RuntimeError'
    assert summary['api_embedded'] == 0
    assert app.automatic_completion([summary], True)['code'] == 'AUTO-EMBEDDING-VERIFIER-001'


def test_rechunking_counts_records_not_internal_vectors():
    for vectors, records in [(4, 1), (7, 2), (9, 3)]:
        assert app.current_source_vector_progress_count({
            'current_upload_document_vector_count': vectors,
            'current_upload_documents_with_vectors': records,
            'observed_chunk_source_count': 100,
        }, expected_records=3) == records


def test_identity_only_status_is_complete_and_does_not_claim_submission_proof():
    message, event = app.vector_observation_status_message(
        quiet_queue_recovery=False, covered_records=0, unique_identities=1, expected_records=3,
    )
    assert '1/3 selected source identities observed' in message
    assert 'confirmation pending' in message
    assert event == 'exact_vector_observation'


@pytest.mark.parametrize('status', ['error', 'reconciliation_pending'])
def test_verifier_exception_is_not_reported_as_timeout_or_rejection(status):
    summary = {'api_upload_status': status, 'api_upload_error': 'broken callback',
               'api_verification_error_type': 'UnboundLocalError'}
    result = app.automatic_completion([summary], True)
    assert result['code'] == 'AUTO-EMBEDDING-VERIFIER-001'
    assert 'not a reconciliation timeout' in result['message']
    assert result['error_category'] == 'verification_callback_exception'
    assert app.automatic_completion_phase(result, True) == 'Assistant vector confirmation interrupted'


def test_production_batch_verifier_survives_identity_only_then_rechunked_progress(tmp_path):
    text = tmp_path / 'prepared.txt'
    text.write_text('prepared', encoding='utf-8')
    plan = tmp_path / 'plan.csv'
    with plan.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'filename', 'title', 'docAuthor', 'description', 'docSource', 'chunkSource', 'text_file',
        ])
        writer.writeheader()
        for index in range(3):
            writer.writerow({'filename': f'page-{index}.txt', 'chunkSource': f'segment://{index}',
                             'docSource': 'local-pdf://fixture', 'text_file': str(text)})
    clock = [0.0]
    captured = {}
    observed = iter([(0, 0, 1, False), (4, 1, 1, False), (9, 3, 3, True)])
    statuses = []

    def verify(*args, **kwargs):
        vectors, records, identities, complete = next(observed)
        return {'status': 'pass' if complete else 'partial_vector_coverage',
                'current_upload_document_vector_count': vectors,
                'current_upload_documents_with_vectors': records,
                'observed_chunk_source_count': identities, 'matching_vector_rows': vectors,
                'current_upload_vector_evidence_complete': complete}

    def upload(*args, **kwargs):
        captured['verification'] = kwargs['batch_verifier']({
            'start_index': 0, 'end_index': 3, 'locations': ['a', 'b', 'c'],
        })
        return {'status': 'complete', 'uploaded': 3, 'embedded': 3,
                'attachment_results': [], 'embedding_update': {'batches': []}}

    with mock.patch.object(app, 'maybe_upload_to_anythingllm', side_effect=upload) as submit, \
            mock.patch.object(app, 'verify_anythingllm_post_upload', side_effect=verify), \
            mock.patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]), \
            mock.patch.object(app.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
        app.upload_prepared_automatic_batch([
            {'pdf': str(tmp_path / 'fixture.pdf'), 'source_sha256': 'fixture',
             'native_upload_plan': str(plan), 'native_upload_transport': 'file_upload'},
        ], api_url='http://anythingllm', api_key='', workspace_slug='target', run_root=tmp_path / 'run',
           status_callback=lambda message, details: statuses.append((message, details)))
    submit.assert_called_once()
    assert captured['verification']['status'] == 'pass'
    assert any('source identities observed' in message for message, _ in statuses)
    assert any(details.get('matching_vectors') == 1 for _, details in statuses)
