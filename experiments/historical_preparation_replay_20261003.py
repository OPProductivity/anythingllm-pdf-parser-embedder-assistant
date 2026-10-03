"""Fresh production-worker preparation against retained historical configurations.

Never uploads or mutates AnythingLLM. Each document gets new OCR checkpoints.
"""

import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manifest_text import read_manifest_rows
from portable_paths import DATA_DIRECTORY_ENVIRONMENT_VARIABLE, application_paths
from run_evidence import read_run_json

REPO = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, help='Bound an explicit smoke test to this many PDFs')
    parser.add_argument('--include-run', help='Also replay sources from this retained automatic run ID')
    parser.add_argument('--source-name', action='append', help='Restrict a requalification to exact source filenames')
    parser.add_argument('--short-home', action='store_true', help='Use a short isolated Windows test home')
    parser.add_argument('--code-root', type=Path, help='Run workers from an explicitly preserved baseline checkout')
    options = parser.parse_args()
    if options.limit is not None and options.limit < 1:
        parser.error('--limit must be positive')
    original_root = application_paths()['run_state'] / 'automatic-runs'
    receipt = REPO / 'tmp-output/historical-replay-20261003'
    inventory = json.loads((receipt / 'inventory.json').read_text())
    configs = {}
    for path in sorted(original_root.glob('*/**/.automatic-worker-config.json')):
        row = read_run_json(path)
        configs[row['pdf_path']] = (path, row)
    if options.include_run:
        included = original_root / options.include_run
        if included.parent != original_root or not included.is_dir():
            parser.error('--include-run must name an existing direct child of automatic-runs')
        for path in sorted(included.glob('*/.automatic-worker-config.json')):
            row = read_run_json(path)
            summary = read_run_json(path.parent / 'run-summary.json')
            source = row['pdf_path']
            digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
            assert digest == summary['source_sha256'], 'Retained source PDF changed'
            configs[source] = (path, row)
            if digest not in inventory['unique_content']:
                inventory['unique_content'][digest] = [source]
                inventory['sources'].append({'path': source, 'pages': summary['pdf_page_count']})
    report = {'kind': 'fresh_preparation_not_live_embedding', 'commit': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(), 'cases': []}
    invocation = receipt / ('preparation-' + uuid.uuid4().hex)
    invocation.mkdir()
    results_path = invocation / 'results.json'
    home = Path(tempfile.mkdtemp(prefix='hpr-')) / 'home' if options.short_home else invocation / 'home'
    report['test_home'] = str(home)
    env = dict(os.environ)
    env[DATA_DIRECTORY_ENVIRONMENT_VARIABLE] = str(home)
    code_root = options.code_root.resolve() if options.code_root else REPO
    if not (code_root / 'cancellable_preparation_worker.py').is_file():
        parser.error('--code-root has no preparation worker')
    report['worker_code_root'] = str(code_root)
    report['worker_pipeline_sha256'] = hashlib.sha256((code_root / 'auto_anythingllm_pipeline.py').read_bytes()).hexdigest()
    inputs = list(inventory['unique_content'].items())
    if options.source_name:
        selected = set(options.source_name)
        inputs = [(digest, paths) for digest, paths in inputs
                  if any(Path(path).name in selected for path in paths)]
        if not inputs:
            parser.error('--source-name did not match any inventoried PDF')
    if options.limit is not None:
        inputs = inputs[:options.limit]
    report['invocation'] = str(invocation)
    report['fresh_worker_required'] = True
    for index, (digest, paths) in enumerate(inputs, 1):
        source = next(path for path in paths if path in configs)
        historical_config, config = configs[source]
        run_root = home / 'run-state/automatic-runs' / f'r-replay-{index:02d}'
        run_root.mkdir(parents=True, exist_ok=False)
        doc = run_root / 'document'
        args = config['args']
        args.update(prepare_and_upload=False, run_vector_eval=False,
                    anythingllm_api_url='', anythingllm_api_key='', workspace_slug='',
                    test_workspace_slug='', anythingllm_storage_dir='',
                    lean_retention=False, defer_lean_retention=False,
                    unstructured_ocr_checkpoint_dir=str(run_root / 'ocr-checkpoints'),
                    run_author_inference_sample_evaluation=False)
        config.update(output_dir=str(doc), run_root=str(run_root),
                      result_path=str(run_root / 'result.json'),
                      events_path=str(run_root / 'events.jsonl'), anythingllm_api_key_env='')
        config_path = run_root / 'config.json'
        write(config_path, config)
        case = {'source': source, 'sha256': digest, 'historical_config': str(historical_config),
                'pages': next(item.get('pages') for item in inventory['sources'] if item['path'] == source),
                'status': 'running', 'run_root': str(run_root)}
        report['cases'].append(case)
        write(results_path, report)
        print(f"START {index}/{len(inputs)} {Path(source).name}", flush=True)
        started = time.monotonic()
        try:
            with (run_root / 'stdout.log').open('a') as stdout, (run_root / 'stderr.log').open('a') as stderr:
                process = subprocess.Popen([sys.executable, '-m', 'cancellable_preparation_worker', str(config_path)],
                                           cwd=code_root, env=env, stdout=stdout, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    code = process.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=False,
                                   capture_output=True)
                    process.wait(timeout=15)
                    raise RuntimeError('Owned preparation exceeded the 240-second per-document bound')
            result = read_run_json(run_root / 'result.json')
            if code or result.get('status') != 'completed':
                raise RuntimeError(f"Worker exit {code}, status {result.get('status')}: {result.get('message', '')}")
            current = read_run_json(doc / 'run-summary.json')
            previous = read_run_json(historical_config.parent / 'run-summary.json')
            before = read_manifest_rows(previous['manifest'])
            after = read_manifest_rows(current['manifest'])
            old_text = '\n'.join(row['text'] for row in before)
            new_text = '\n'.join(row['text'] for row in after)
            case.update(status='complete', body_identical=old_text == new_text,
                        old_characters=len(old_text), new_characters=len(new_text),
                        old_body_contained=old_text in new_text,
                        old_rows=len(before), new_rows=len(after),
                        settings={key: current.get(key) for key in (
                            'segment_mode', 'include_front_matter', 'include_back_matter',
                            'native_upload_representation', 'native_upload_transport')},
                        boundary_before=[previous.get('start_page'), previous.get('end_page')],
                        boundary_after=[current.get('start_page'), current.get('end_page')],
                        metadata_before={key: previous.get(key) for key in ('detected_title', 'detected_author')},
                        metadata_after={key: current.get(key) for key in ('detected_title', 'detected_author')})
            for path in run_root.rglob('*.json'):
                read_run_json(path)
            assert all(isinstance(row.get('text'), str) for row in after)
            assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == digest
            case['references_decodable'] = True
        except Exception as exc:
            case.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        case['seconds'] = round(time.monotonic() - started, 3)
        write(results_path, report)
        print(f"END {index}/{len(inputs)} {case['status']} {case['seconds']}s", flush=True)
    print(json.dumps({'complete': sum(row['status'] == 'complete' for row in report['cases']),
                      'failed': sum(row['status'] == 'failed' for row in report['cases']),
                      'results': str(results_path)}), flush=True)
    return int(any(row['status'] != 'complete' for row in report['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
