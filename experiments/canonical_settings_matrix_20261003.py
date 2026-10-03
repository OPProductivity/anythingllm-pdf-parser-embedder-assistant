"""Production preparation/export parity across sources and settings; no API writes."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_anythingllm_pipeline as pipeline
import portable_paths
import rag_pdf_gradio_app as app
from run_evidence import read_run_json
from run_artifact_tools import evidence_path, materialize_optional_artifacts, read_rows
from prepared_batch_recovery import write_prepared_batch_checkpoint, verify_prepared_batch_checkpoint


PROFILES = [
    dict(segment_mode='none', target_passage_length=4096, native_upload_representation='segments',
         native_upload_transport='raw_text', disable_inline_markers=True, include_front_matter=True,
         include_back_matter=True, backend_mode='pymupdf', anythingllm_chunk_size=4096),
    dict(segment_mode='page', target_passage_length=2048, native_upload_representation='page_parents',
         native_upload_transport='file_upload', disable_inline_markers=False, include_front_matter=False,
         include_back_matter=False, backend_mode='pymupdf', anythingllm_chunk_size=2048,
         anythingllm_chunk_overlap=100, first_page_override=2, end_page_override=7),
    dict(segment_mode='page_limit', target_passage_length=1024, native_upload_representation='segments',
         native_upload_transport='raw_text', disable_inline_markers=False, marker_style='full',
         include_front_matter=True, include_back_matter=False, backend_mode='pymupdf',
         anythingllm_chunk_size=1024, native_metadata_upload_mode='strict', lean_retention=True,
         defer_lean_retention=True),
    dict(segment_mode='page_passages', target_passage_length=350, native_upload_representation='page_parents',
         native_upload_transport='file_upload', disable_inline_markers=True, include_front_matter=False,
         include_back_matter=True, backend_mode='pymupdf4llm', anythingllm_chunk_size=1024,
         anythingllm_chunk_overlap=150, first_page_override=2, end_page_override=7),
    dict(segment_mode='passages', target_passage_length=700, native_upload_representation='segments',
         native_upload_transport='file_upload', disable_inline_markers=False, include_front_matter=True,
         include_back_matter=True, backend_mode='pymupdf', anythingllm_chunk_size=2048,
         native_metadata_upload_mode='strict', document_label='Matrix custom title',
         document_author='Matrix custom author', document_short_label='matrix-custom', deep_extraction=True),
    dict(segment_mode='custom_page_ranges', custom_page_group_sizes=(2, 3), target_passage_length=8191,
         native_upload_representation='segments', native_upload_transport='raw_text',
         disable_inline_markers=True, include_front_matter=True, include_back_matter=True,
         backend_mode='pymupdf4llm', anythingllm_chunk_size=8191),
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run():
    kit_followup = '--kit-followup' in sys.argv
    optional_followup = '--optional-followup' in sys.argv
    parent_followup = '--parent-followup' in sys.argv
    receipts = Path('tmp-output/' + ('run-log-repair-matrix-20261003' if '--repair-followup' in sys.argv else
                                   'optional-parent-followup-20261003' if parent_followup else
                                   'optional-artifact-followup-20261003' if optional_followup else
                                   'canonical-kit-followup-20261003' if kit_followup
                                   else 'canonical-settings-matrix-20261003')).resolve()
    receipts.mkdir(parents=True, exist_ok=True)
    resume = '--resume' in sys.argv and (receipts / 'results.json').is_file()
    report = json.loads((receipts / 'results.json').read_text(encoding='utf8')) if resume else None
    base = Path(report['test_root']) if report else Path(tempfile.mkdtemp(prefix='cm-'))
    home = base / 'h'
    os.environ[portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE] = str(home)
    inventory = json.loads(Path('tmp-output/ocr-corpus-20261003/inventory.json').read_text(encoding='utf8'))
    prefixes = ['Miriam Hansen - Early Silent', 'David Bordwell - The Idea of Montage',
                'Francesco Casetti - The Persistence', 'Thomas Schatz - Hollywood',
                'Aparicio-Reading the Latino']
    sources = [Path(next(row['path'] for row in inventory if Path(row['path']).name.startswith(prefix)))
               for prefix in prefixes]
    source_hashes = {str(source): digest(source) for source in sources}
    report = report or {'test_root': str(base), 'sources': source_hashes, 'cases': []}
    unique_cases = {}
    for case in report['cases']:
        key = (case['source'], json.dumps(case['settings'], sort_keys=True))
        if key not in unique_cases or unique_cases[key]['error']:
            if key in unique_cases:
                report.setdefault('earlier_attempts', []).append(unique_cases[key])
            unique_cases[key] = case
        else:
            report.setdefault('earlier_attempts', []).append(case)
    report['cases'] = list(unique_cases.values())
    for source_index, source in enumerate(sources):
        for profile_index, settings in enumerate(PROFILES):
            if parent_followup and (source_index != 2 or profile_index not in (1, 3)):
                continue
            if (kit_followup or optional_followup) and profile_index != source_index:
                continue
            settings = json.loads(json.dumps(settings))
            if parent_followup:
                settings.update(segment_mode='page_passages', target_passage_length=350,
                                anythingllm_chunk_size=8191, native_upload_representation='page_parents',
                                first_page_override=1, end_page_override=2, include_front_matter=True,
                                backend_mode='pymupdf',
                                native_upload_transport='file_upload' if profile_index == 1 else 'raw_text',
                                native_metadata_upload_mode='native_header' if profile_index == 1 else 'strict')
            if source_index in (0, 1, 4) and settings['backend_mode'] == 'pymupdf4llm':
                settings['backend_mode'] = 'pymupdf'
            identity = f'c{source_index}{profile_index}'
            previous = next((case for case in report['cases']
                             if case['source'] == str(source) and case['settings'] == settings), None)
            if previous and not previous['error']:
                continue
            if previous:
                identity += '-retry'
                report.setdefault('earlier_attempts', []).append(previous)
                report['cases'].remove(previous)
                suffix = 2
                while (base / 'legacy' / identity).exists():
                    identity = f'c{source_index}{profile_index}-retry-{suffix}'
                    suffix += 1
            case = {'source': str(source), 'settings': settings, 'checks': [], 'runs': [], 'error': ''}
            started = time.monotonic()
            try:
                prepared, exported = [], []
                for private in (False, True):
                    root = ((home / 'run-state/automatic-runs' / identity / 'd') if private
                            else base / 'legacy' / identity)
                    args = dict(document_label='', document_author='', document_short_label='',
                                use_file_title_fallback=True, deep_extraction=False, first_page_override=0,
                                end_page_override=0, custom_page_group_sizes=(),
                                end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS, validation_phrases=[],
                                unstructured_strategy='fast', marker_style='short', lean_retention=False,
                                defer_lean_retention=False, run_vector_eval=False, ollama_model='bge-m3:latest',
                                ollama_url='http://127.0.0.1:11434/api/embed', max_vector_probes=0,
                                max_vector_chunks=0, prepare_and_upload=False, anythingllm_api_url='',
                                anythingllm_api_key='', workspace_slug='', test_workspace_slug=identity,
                                upload_limit=0, native_metadata_upload_mode='native_header',
                                anythingllm_create_document_folders=False, anythingllm_document_folder_name='',
                                anythingllm_storage_dir=str(base / 'absent-storage'), anythingllm_chunk_overlap=0,
                                run_author_inference_sample_evaluation=False)
                    args.update(settings)
                    summary = pipeline.prepare_pdf(source, root, SimpleNamespace(**args))
                    if parent_followup:
                        assert summary['native_upload_representation'] == 'page_parents'
                    assert summary['segment_mode'] == settings['segment_mode']
                    assert summary['include_back_matter'] == settings['include_back_matter']
                    if settings['segment_mode'] == 'custom_page_ranges':
                        assert summary['custom_page_group_sizes'] == [2, 3]
                    manifest = read_rows(Path(summary['manifest']))
                    if settings.get('first_page_override'):
                        assert all(settings['first_page_override'] <= row['pdf_page'] <= settings['end_page_override']
                                   for row in manifest)
                    plan = pipeline.load_upload_plan_rows(Path(summary['native_upload_plan'])) if Path(summary['native_upload_plan']).is_file() else []
                    normalized = [{**{key: value for key, value in row.items() if key != 'text_file'},
                                   'body_sha256': digest(row['text_file'])} for row in plan]
                    primary = Path(summary['upload_file']).read_bytes()
                    assert primary and manifest
                    prepared.append((primary, manifest, normalized,
                                     {key: summary[key] for key in ('segment_mode', 'start_page', 'end_page',
                                      'include_back_matter', 'native_upload_representation_requested',
                                      'native_upload_representation', 'native_upload_transport', 'selected_backend',
                                      'readiness_status')}))
                    paths = read_run_json(Path(summary['provenance_review_manifest']))['review_artifacts']
                    assert all((root / relative).is_file() for relative in paths.values() if relative)
                    if private:
                        kits = (summary['native_test_kit'], summary['native_probe_kit'])
                        for kit in kits:
                            assert not kit['files_dir']
                            if not kit['upload_plan']:
                                assert kit['file_count'] == 0
                                continue
                            manual_rows = pipeline.load_upload_plan_rows(Path(kit['upload_plan']))
                            assert len(manual_rows) == kit['file_count']
                            assert all(Path(row['text_file']).is_file() if row['text_file'] else
                                       Path(row['text_manifest']).is_file() for row in manual_rows)
                            assert not list(Path(kit['upload_plan']).parent.rglob('*.txt'))
                        index = json.loads((root / 'artifact-locations.json').read_text(encoding='utf8'))
                        assert all((root / relative).is_file() for relative in index['roles'].values())
                        assert all(path.stat().st_nlink == 1 for path in root.rglob('*') if path.is_file())
                        assert not list(root.rglob('supplementary-content-candidates.txt'))
                        assert not list(root.rglob('page-transition-companions/*.txt'))
                        expected_plans = (2 if summary['native_upload_representation'] == 'page_parents'
                                          and settings.get('native_metadata_upload_mode') == 'strict' else 1)
                        assert len(list((root / 'metadata-api').glob('file-upload-plan-*.csv'))) == expected_plans
                        write_prepared_batch_checkpoint(
                            root, [summary], total_sources=1, workspace_slug='test', api_url='http://localhost',
                            stage='preparation_complete')
                        recovery = verify_prepared_batch_checkpoint(root)
                        assert recovery['reusable'], recovery
                        evidence, error = app.diagnostic_evidence_paths(root)
                        assert not error, error
                        app.gradio_download_cache_path = lambda name: base / name
                        bundle = app.package_downloadable_paths(evidence, identity + '.zip')
                        with tempfile.TemporaryDirectory(prefix='cm-zip-') as scratch:
                            with zipfile.ZipFile(bundle) as archive:
                                assert len(archive.namelist()) == len(set(archive.namelist()))
                                archive.extractall(scratch)
                            moved = next(Path(scratch).rglob('artifact-locations.json')).parent
                            assert all((moved / relative).is_file() for relative in index['roles'].values())
                            assert read_run_json(moved / 'run-summary.json') == read_run_json(root / 'run-summary.json')
                            before_summary = (moved / 'run-summary.json').read_bytes()
                            for kind in ('diagnostic-text', 'manual-kits', 'upload-alternatives'):
                                generated = materialize_optional_artifacts(moved, kind)
                                assert all(path.is_file() and path.resolve().is_relative_to(Path(scratch).resolve())
                                           for path in generated)
                            for representation in ('segments', 'page-parents'):
                                for mode in ('strict', 'native_header'):
                                    name = ('raw-text-payloads-' + ('page-parents-' if representation == 'page-parents' else '')
                                            + mode.replace('_', '-') + '.jsonl')
                                    expected = read_rows(evidence_path(moved, 'metadata-api/' + name))
                                    actual = pipeline.load_upload_plan_rows(
                                        moved / 'on-demand/upload-alternatives' / f'upload-plan-{representation}-{mode}.csv')
                                    assert len(actual) == len(expected)
                                    for row, payload in zip(actual, expected):
                                        assert row['filename'] == payload['filename']
                                        assert Path(row['text_file']).read_text(encoding='utf8') == payload['textContent']
                                        assert all(row[key] == str(payload['metadata'].get(key) or '')
                                                   for key in ('title', 'docAuthor', 'description', 'docSource', 'chunkSource'))
                            assert (moved / 'run-summary.json').read_bytes() == before_summary
                    size = sum(path.stat().st_size for path in root.rglob('*') if path.is_file())
                    retention = pipeline.retain_successful_run_without_logs(
                        root, summary, {'filename': source.name}, Path(summary['upload_file']),
                        segments=manifest, preexisting_children=())
                    assert retention['applied'] or retention.get('reason') == 'run_needs_review', retention
                    summary['lean_retention'] = retention
                    public, _ = pipeline.publish_cli_text_outputs(base / 'public' / str(private) / identity, root, [summary])
                    exported.append({path.name: digest(path) for path in public.iterdir()})
                    assert all(path.is_file() and path.suffix == '.txt' for path in public.iterdir())
                    case['runs'].append({'private': private, 'summary': str(root / 'run-summary.json'),
                                         'private_bytes': size, 'segments': len(manifest), 'plan_rows': len(plan),
                                         'effective_settings': prepared[-1][3], 'public_files': len(exported[-1]),
                                         'retention': retention})
                assert prepared[0] == prepared[1], 'Prepared text, manifest, plan metadata/body or effective settings changed'
                assert exported[0] == exported[1], 'Public filenames or bytes changed'
                case['checks'] = ['text', 'manifest', 'upload-plan-body-and-metadata', 'effective-settings',
                                  'provenance-paths', 'canonical-index', 'no-filesystem-links',
                                  'relocated-diagnostics', 'public-filenames-and-bytes', 'flat-output-only']
            except Exception as exc:
                case['error'] = f'{type(exc).__name__}: {exc}'
                case['traceback'] = traceback.format_exc()
            case['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['cases'].append(case)
            (receipts / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            print(identity, source.name, settings['segment_mode'], case['error'] or 'PASS', flush=True)
    assert all(digest(source) == source_hashes[str(source)] for source in sources), 'Source PDF changed'
    failures = [case for case in report['cases'] if case['error']]
    print('MATRIX_COMPLETE', len(report['cases']), 'FAILURES', len(failures), 'ROOT', base, flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(run())
