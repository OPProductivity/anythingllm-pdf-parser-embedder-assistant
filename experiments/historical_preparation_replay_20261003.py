"""Fresh production-worker preparation against retained historical configurations.

Never uploads or mutates AnythingLLM. Each document gets new OCR checkpoints.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manifest_text import read_manifest_rows
from portable_paths import DATA_DIRECTORY_ENVIRONMENT_VARIABLE, application_paths
from run_evidence import read_run_json

REPO = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding='utf8')


def main():
    original_root = application_paths()['run_state'] / 'automatic-runs'
    receipt = REPO / 'tmp-output/historical-replay-20261003'
    inventory = json.loads((receipt / 'inventory.json').read_text())
    configs = {}
    for path in sorted(original_root.glob('*/**/.automatic-worker-config.json')):
        row = read_run_json(path)
        configs[row['pdf_path']] = (path, row)
    report = {'kind': 'fresh_preparation_not_live_embedding', 'commit': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(), 'cases': []}
    home = receipt / 'home'
    env = dict(os.environ)
    env[DATA_DIRECTORY_ENVIRONMENT_VARIABLE] = str(home)
    for index, (digest, paths) in enumerate(inventory['unique_content'].items(), 1):
        source = next(path for path in paths if path in configs)
        historical_config, config = configs[source]
        run_root = home / 'run-state/automatic-runs' / f'r-replay-{index:02d}'
        already_prepared = (run_root / 'result.json').is_file()
        if run_root.exists() and not already_prepared:
            run_root = run_root.with_name(run_root.name + '-retry')
        run_root.mkdir(parents=True, exist_ok=already_prepared)
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
        write(receipt / 'results.json', report)
        print(f"START {index}/34 {Path(source).name}", flush=True)
        started = time.monotonic()
        try:
            with (run_root / 'stdout.log').open('a') as stdout, (run_root / 'stderr.log').open('a') as stderr:
                if already_prepared:
                    code = 0
                else:
                    process = subprocess.Popen([sys.executable, '-m', 'cancellable_preparation_worker', str(config_path)],
                                               cwd=REPO, env=env, stdout=stdout, stderr=stderr)
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
                        metadata_before={key: previous.get(key) for key in ('title', 'author')},
                        metadata_after={key: current.get(key) for key in ('title', 'author')})
            for path in run_root.rglob('*.json'):
                read_run_json(path)
            assert all(isinstance(row.get('text'), str) for row in after)
            assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == digest
            case['references_decodable'] = True
        except Exception as exc:
            case.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        case['seconds'] = round(time.monotonic() - started, 3)
        write(receipt / 'results.json', report)
        print(f"END {index}/34 {case['status']} {case['seconds']}s", flush=True)
    print(json.dumps({'complete': sum(row['status'] == 'complete' for row in report['cases']),
                      'failed': sum(row['status'] == 'failed' for row in report['cases'])}), flush=True)
    return int(any(row['status'] != 'complete' for row in report['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
