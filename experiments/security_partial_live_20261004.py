"""Fresh normal-browser ingestion in a disposable workspace; always clean up."""
import ast
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import fitz
from gradio_client import Client, handle_file

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import auto_anythingllm_pipeline as pipeline
    from run_evidence import read_run_json

    unique = uuid.uuid4().hex
    directory = ROOT / 'tmp-output' / ('security-live-' + unique)
    directory.mkdir()
    sources = []
    for index in range(3):
        path = directory / f'academic-{index}.pdf'
        with fitz.open() as document:
            for page_index in range(3):
                page = document.new_page()
                page.insert_textbox(fitz.Rect(72, 72, 510, 740),
                    f'Academic transport qualification {unique}. Source {index}, page {page_index + 1}.\n'
                    + 'An original native-text passage for source identity and fresh embedding qualification. ' * 12)
            document.save(path)
        sources.append(str(path))
    storage = pipeline.default_anythingllm_storage_dir()
    def document_snapshot():
        with sqlite3.connect(storage.resolve().as_uri() + '/anythingllm.db?mode=ro', uri=True) as connection:
            return (connection.execute('SELECT * FROM workspace_documents ORDER BY id').fetchall(),
                    connection.execute('SELECT * FROM document_vectors ORDER BY id').fetchall())
    before_documents = document_snapshot()
    before_pids = {}
    import psutil
    for process in psutil.process_iter(['name', 'create_time']):
        if str(process.info['name']).casefold() == 'anythingllm.exe':
            before_pids[process.pid] = process.info['create_time']
    created = pipeline.create_validation_workspace('http://127.0.0.1:3001', storage_dir=storage,
        workspace_name='Security Partial Qualification ' + unique[:12])
    slug = created.get('workspace_slug')
    assert slug, created.get('error')
    run = None
    report = {'test_workspace': slug, 'sources': sources}
    try:
        client = Client('http://127.0.0.1:7860/', verbose=False)
        module = ast.parse((ROOT / 'rag_pdf_gradio_app.py').read_text(encoding='utf-8'))
        fields = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'AUTOMATIC_RUN_FIELDS' for target in node.targets))
        dependency = next(item for item in client.config['dependencies']
            if item.get('api_name') == 'run_automatic_for_browser_stream')
        components = {item['id']: item for item in client.config['components']}
        overrides = {'workspace_slug': slug, 'api_url': 'http://127.0.0.1:3001', 'api_key': '',
            'document_label': '', 'document_author': '', 'document_short_label': '',
            'segment_mode': 'Page - preserve automatically', 'include_front_matter': True,
            'include_back_matter': True}
        params = []
        for index, identifier in enumerate(dependency['inputs'][2:]):
            component = components[identifier]
            if component['type'] == 'state':
                continue
            value = component['props'].get('value')
            if index < len(fields):
                value = overrides.get(fields[index], value)
            if index == 0:
                value = [handle_file(source) for source in sources]
            params.append(value)
        runs = Path(os.environ['LOCALAPPDATA']) / 'AnythingLLM PDF Parser Embedder Assistant/run-state/automatic-runs'
        before_runs = set(runs.iterdir())
        started = time.monotonic()
        job = client.submit(*params, api_name='/run_automatic_for_browser_stream')
        while not job.done():
            candidates = set(runs.iterdir()) - before_runs
            if candidates:
                assert len(candidates) == 1, 'Concurrent assistant run; cannot guess ownership'
                run = candidates.pop()
            time.sleep(1)
        job.result()
        if run is None:
            candidates = set(runs.iterdir()) - before_runs
            assert len(candidates) == 1
            run = candidates.pop()
        progress = read_run_json(run / 'run-progress.json')
        audit = read_run_json(run / 'integrity-audit.json')
        report.update(state=progress['state'], details=progress['details'], run_root=str(run),
            seconds=round(time.monotonic() - started, 3), integrity=audit['audit_status'],
            freshly_attached=audit['summary']['newly_attached_records'],
            confirmed=audit['summary']['confirmed_vector_records'])
        assert progress['state'] == 'successful' and audit['audit_status'] == 'pass'
        assert report['freshly_attached'] == report['confirmed'] == 9
    finally:
        locations = []
        if run is not None:
            ledger_path = run / 'batch-embedding-ledger.json'
            if ledger_path.exists():
                ledger = read_run_json(ledger_path)
                locations = sorted({location for batch in ledger.get('batches', [])
                                    for location in batch.get('locations', [])})
        cleanup = pipeline.delete_validation_workspace('http://127.0.0.1:3001', slug,
            storage_dir=storage, document_locations=locations)
        report['cleanup'] = cleanup
        report['protected_documents_unchanged'] = document_snapshot() == before_documents
        report['desktop_processes_unchanged'] = all(psutil.Process(pid).create_time() == created
            for pid, created in before_pids.items())
        (directory / 'result.json').write_text(json.dumps(report, separators=(',', ':')), encoding='utf-8')
        assert cleanup['status'] == 'deleted', cleanup
        assert report['protected_documents_unchanged'] and report['desktop_processes_unchanged']
        assert not any((storage / 'documents' / location).exists() for location in locations)
    print(json.dumps({key: value for key, value in report.items() if key != 'cleanup'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
