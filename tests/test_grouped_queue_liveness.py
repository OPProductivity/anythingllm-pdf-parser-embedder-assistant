import csv
from unittest import mock

import pytest

import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('gap,state,should_extend', [
    (49.0, 'connected', True),
    (89.0, 'connected', True),
    (90.0, 'connected', False),
    (91.0, 'connected', False),
    (49.0, 'disconnected', False),
])
def test_owned_queue_deadline_uses_existing_stall_allowance(tmp_path, gap, state, should_extend):
    text = tmp_path / 'prepared.txt'
    text.write_text('prepared', encoding='utf8')
    plan = tmp_path / 'plan.csv'
    with plan.open('w', encoding='utf8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'filename', 'title', 'docAuthor', 'description', 'docSource', 'chunkSource', 'text_file',
        ])
        writer.writeheader()
        for index in range(214):
            writer.writerow({
                'filename': f'page-{index}.txt', 'title': f'Page {index}',
                'docSource': 'local-pdf://sha256/liveness', 'chunkSource': f'liveness-p{index:04d}',
                'text_file': str(text),
            })
    summary = {
        'pdf': str(tmp_path / 'liveness.pdf'), 'source_sha256': 'liveness',
        'native_upload_plan': str(plan), 'native_upload_transport': 'file_upload',
    }
    clock = [0.0]
    captured = {}
    queue = {
        'queue_records': 214, 'completed': 128, 'current': 129,
        'events_observed': 1, 'last_event_monotonic': 0.0,
        'first_progress_monotonic': 0.0, 'last_progress_monotonic': 0.0,
        'first_progress_position': 1, 'last_progress_position': 129,
        'observer_state': 'connected',
    }
    locations = [f'custom-documents/liveness-{index}.json' for index in range(214)]

    def sleep(_seconds):
        if clock[0] == 0:
            clock[0] = 480.0 - gap
            queue.update({
                'completed': 129, 'current': 130,
                'last_event_monotonic': clock[0], 'last_progress_monotonic': clock[0],
                'last_progress_position': 130, 'events_observed': 2, 'observer_state': state,
            })
        elif clock[0] < 480:
            clock[0] = 480.0
        else:
            # Completion occurs only after the initial deadline. No resubmit
            # is needed: the same owned queue finishes within its extension.
            clock[0] += 1.0
            queue.update({'completed': 214, 'current': 214, 'last_event_monotonic': clock[0]})

    def verify(*_args, **_kwargs):
        complete = queue['completed'] == 214
        return {
            'status': 'pass' if complete else 'partial_vector_coverage',
            'matching_vector_rows': 214 if complete else 0,
            'lancedb_matching_rows': 214 if complete else 0,
            'current_upload_document_vector_count': 214 if complete else 0,
            'current_upload_vector_evidence_complete': complete,
        }

    def upload(*_args, **kwargs):
        captured['verification'] = kwargs['batch_verifier']({
            'start_index': 0, 'end_index': 214, 'locations': locations,
            'desktop_queue_observer': queue,
        })
        return {
            'status': 'complete' if should_extend else 'reconciliation_pending',
            'uploaded': 214, 'embedded': 214 if should_extend else 0,
            'attachment_results': [],
            'embedding_update': {'requested': 214, 'accepted': 214, 'batches': []},
        }

    with mock.patch.object(app, 'maybe_upload_to_anythingllm', side_effect=upload) as submit, \
            mock.patch.object(app, 'verify_anythingllm_post_upload', side_effect=verify), \
            mock.patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]), \
            mock.patch.object(app.time, 'sleep', side_effect=sleep):
        app.upload_prepared_automatic_batch(
            [summary], api_url='http://anythingllm', api_key='',
            workspace_slug='workspace', run_root=tmp_path / 'run',
        )
    submit.assert_called_once()
    evidence = captured['verification']
    if should_extend:
        assert evidence['status'] == 'pass'
        assert queue['completed'] == 214
        assert clock[0] > 480
    else:
        assert evidence['status'] != 'pass'
        assert evidence['reconciliation_deadline_extensions'] == 0
        assert clock[0] == 480
