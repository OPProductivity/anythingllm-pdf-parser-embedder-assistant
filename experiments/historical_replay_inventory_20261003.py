"""Read-only inventory of retained production runs for fresh-run qualification."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portable_paths import application_paths
from run_evidence import read_run_json


def main():
    root = application_paths()['run_state'] / 'automatic-runs'
    runs, sources, errors = [], {}, []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        configs = list(directory.glob('*/.automatic-worker-config.json'))
        summaries = list(directory.glob('*/run-summary.json'))
        source_paths = []
        batch = directory / '.automatic-batch-upload-config.json'
        if batch.exists():
            try:
                source_paths.extend(read_run_json(batch).get('source_paths') or [])
            except Exception as exc:
                errors.append({'run': directory.name, 'file': batch.name, 'error': str(exc)})
        documents = []
        for path in summaries:
            try:
                row = read_run_json(path)
                pdf = str(row.get('pdf') or '')
                if pdf:
                    source_paths.append(pdf)
                documents.append({key: row.get(key) for key in (
                    'pdf', 'segment_mode', 'selected_backend', 'start_page', 'end_page',
                    'include_front_matter', 'include_back_matter', 'native_upload_representation',
                    'native_upload_transport', 'readiness_status', 'api_upload_status',
                    'author', 'title', 'manifest', 'upload_file')})
            except Exception as exc:
                errors.append({'run': directory.name, 'file': str(path.relative_to(directory)), 'error': str(exc)})
        for pdf in dict.fromkeys(source_paths):
            if not pdf:
                continue
            path = Path(pdf)
            entry = sources.setdefault(pdf, {'path': pdf, 'exists': path.is_file(), 'runs': []})
            entry['runs'].append(directory.name)
        progress = directory / 'run-progress.json'
        state = read_run_json(progress).get('state') if progress.exists() else 'unknown'
        runs.append({'run': directory.name, 'state': state, 'documents': documents,
                     'source_paths': list(dict.fromkeys(source_paths)), 'config_count': len(configs)})
    by_hash = {}
    for entry in sources.values():
        if not entry['exists']:
            continue
        path = Path(entry['path'])
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        entry['sha256'] = digest
        by_hash.setdefault(digest, []).append(entry['path'])
        try:
            import pymupdf
            with pymupdf.open(path) as document:
                entry['pages'] = len(document)
        except Exception as exc:
            entry['inspection_error'] = str(exc)
    receipt = Path('tmp-output/historical-replay-20261003/inventory.json')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({'runs': runs, 'sources': list(sources.values()),
                                  'unique_content': by_hash, 'errors': errors}, indent=2), encoding='utf8')
    print(json.dumps({'runs': len(runs), 'source_paths': len(sources),
                      'available_unique_pdfs': len(by_hash),
                      'missing_paths': sum(not item['exists'] for item in sources.values()),
                      'unique_pages': sum(sources[paths[0]].get('pages', 0) for paths in by_hash.values()),
                      'errors': len(errors), 'receipt': str(receipt)}))


if __name__ == '__main__':
    main()
