import json

import pytest

import auto_anythingllm_pipeline as pipeline
import portable_paths
import rag_pdf_gradio_app as app
from run_evidence import read_run_json


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('mode', ['none', 'page_passages'])
@pytest.mark.parametrize('transport', ['local', 'shared_upload'])
@pytest.mark.parametrize('publisher', ['gui', 'cli'])
def test_private_evidence_survives_with_identical_flat_exports(tmp_path, monkeypatch, mode, transport, publisher):
    monkeypatch.setenv(portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(tmp_path / 'home'))
    exports = []
    for private in (False, True):
        root = (tmp_path / 'home' / 'run-state' if private else tmp_path / 'legacy') / 'automatic-runs' / 'r-check'
        document = root / 'paper'
        selected = document / 'selected'
        selected.mkdir(parents=True)
        parsed = selected / 'Paper-pdf-parsed.txt'
        parsed.write_text('First part.\n\nSecond part.', encoding='utf8')
        segments = [{'pdf_page': 1, 'text': 'First part.'}, {'pdf_page': 2, 'text': 'Second part.'}]
        manifest = selected / 'segment-manifest.jsonl'
        manifest.write_text(''.join(json.dumps(row) + '\n' for row in segments), encoding='utf8')
        evidence = [manifest, parsed]
        for name in (*app.AUTOMATIC_WORKER_TRANSPORT_ARTIFACTS, 'diagnostics.json', 'probe.txt'):
            path = document / name
            path.write_text('preserved evidence', encoding='utf8')
            evidence.append(path)
        for name in app.SUCCESSFUL_BATCH_TRANSIENT_ARTIFACTS:
            path = root / name
            path.write_text('batch evidence', encoding='utf8')
            evidence.append(path)
        before = {path: path.read_bytes() for path in evidence}
        summary = {
            'pdf': 'Paper.pdf', 'output_root': str(document), 'upload_file': str(parsed),
            'manifest': str(manifest), 'segments': 2, 'segment_mode': mode, 'readiness_status': 'ready',
            'include_back_matter': True,
            'api_upload_status': 'skipped_prepare_only',
            'post_upload_verification_status': 'not_checked_no_upload',
            'anythingllm_runtime_validation_status': 'not_checked_no_upload',
            'compatibility': {'qualified_runtime': 'runtime evidence ' * 1000},
        }
        if transport == 'shared_upload':
            summary.update({
                'api_upload_status': 'complete', 'post_upload_verification_status': 'pass',
                'anythingllm_runtime_validation_status': 'deferred_after_exact_vector_proof',
                'post_upload_expected_payloads': 2, 'post_upload_matching_vectors': 2,
                'batch_upload_result': {'searchability_proven': True},
                'lean_retention': {'deferred': True, 'reason': 'awaiting_shared_automatic_batch_upload'},
            })
            retained = pipeline.finalize_deferred_batch_lean_retention(document, summary)
        else:
            retained = pipeline.retain_successful_run_without_logs(
                document, summary, {'filename': 'Paper.pdf'}, parsed,
                segments=segments, preexisting_children=(),
            )
        assert retained['applied']
        summary['lean_retention'] = retained
        app.automatic_success_worker_artifact_cleanup_report(document, summary)
        app.compact_successful_automatic_batch_root(root)
        output_base = tmp_path / ('private-exports' if private else 'legacy-exports')
        if publisher == 'gui':
            output = app.promote_flat_no_logs_batch_output(output_base, root, [], [summary])
            app.finalize_published_text_separation(root, [summary])
        else:
            output, _ = pipeline.publish_cli_text_outputs(output_base, root, [summary])
        assert output is not None
        children = list(output.iterdir())
        assert len(children) == (1 if mode == 'none' else 3)
        assert all(path.is_file() and path.suffix == '.txt' for path in children)
        exports.append({path.name: path.read_bytes() for path in children})
        if private:
            assert all(path.read_bytes() == content for path, content in before.items())
            stored = read_run_json(document / 'run-summary.json')
            assert stored['include_back_matter'] is True
            assert stored['compatibility'] == summary['compatibility']
            assert stored['manifest'] == str(manifest)
            assert app.cleanup_flat_local_staging(root) == ''
            assert root.is_dir()
    assert exports[0] == exports[1]


def test_private_boundary_does_not_match_similarly_named_output(tmp_path, monkeypatch):
    monkeypatch.setenv(portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(tmp_path))
    assert portable_paths.is_private_run_state_path(tmp_path / 'run-state' / 'automatic-runs' / 'r-one')
    assert not portable_paths.is_private_run_state_path(tmp_path / 'outputs' / 'run-state' / 'r-one')
    assert not portable_paths.is_private_run_state_path(tmp_path / 'run-state-other' / 'r-one')
