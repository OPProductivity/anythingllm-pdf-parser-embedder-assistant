"""Fresh native backend test with entirely separate data and owned processes."""

import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anythingllm_state import read_env_values
import auto_anythingllm_pipeline as pipeline
from run_evidence import read_run_json

REPO = Path(__file__).resolve().parents[1]
PORT, COLLECTOR = 43131, 43132
API = f'http://127.0.0.1:{PORT}'


def main():
    production = Path.home() / 'AppData/Roaming/anythingllm-desktop/storage'
    receipt = REPO / 'tmp-output/historical-replay-20261003'
    kind = 'isolated-mixed-' if '--mixed' in sys.argv else 'isolated-pilot-' if '--pilot' in sys.argv else 'isolated-full-'
    base = receipt / (kind + secrets.token_hex(3))
    storage = base / 'roaming/anythingllm-desktop/storage'
    assert not storage.exists(), 'Every live qualification must start with empty storage'
    assert not storage.resolve().is_relative_to(production.resolve())
    for port in (PORT, COLLECTOR):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    storage.mkdir(parents=True)
    for name in ('hotdir', 'tmp', 'documents', 'direct-uploads', 'logs', 'vector-cache',
                 'lancedb', 'models', 'engines', 'plugins', 'generated-files'):
        (storage / name).mkdir()
    key = secrets.token_urlsafe(36)
    with sqlite3.connect((production / 'anythingllm.db').as_uri() + '?mode=ro', uri=True) as source:
        # Copy schema only. No documents, vectors, workspaces, users or chats.
        schema = source.execute("select type,sql from sqlite_master where sql is not null "
                                "and name not like 'sqlite_%' order by type='index'").fetchall()
        settings = source.execute("select label,value from system_settings "
                                  "where label in ('text_splitter_chunk_size','text_splitter_chunk_overlap')").fetchall()
    with sqlite3.connect(storage / 'anythingllm.db') as target:
        for _, sql in schema:
            target.execute(sql)
        target.executemany('insert into system_settings(label,value) values (?,?)', settings)
        target.execute('insert into api_keys(name,secret) values (?,?)', ('isolated-replay', key))
        assert target.execute('select count(*) from workspace_documents').fetchone()[0] == 0
        assert target.execute('select count(*) from document_vectors').fetchone()[0] == 0
    backend = Path.home() / 'AppData/Local/Programs/AnythingLLM/resources/backend'
    executable = backend.parents[1] / 'AnythingLLM.exe'
    assert executable.is_file() and (backend / 'server.js').is_file()
    env = dict(os.environ)
    configured = read_env_values(production / '.env')
    for name in ('EMBEDDING_ENGINE', 'EMBEDDING_MODEL_PREF', 'EMBEDDING_MODEL_MAX_CHUNK_LENGTH',
                 'OPENROUTER_API_KEY', 'OPENROUTER_TIMEOUT_MS'):
        if name in configured:
            env[name] = configured[name]
    env.update(ELECTRON_RUN_AS_NODE='1', NODE_ENV='production', STORAGE_DIR=str(storage),
               DATABASE_URL='file:' + (storage / 'anythingllm.db').as_posix(),
               SERVER_PORT=str(PORT), COLLECTOR_PORT=str(COLLECTOR), APP_DISCOVERABLE='false',
               APPDATA=str(base / 'roaming'), DISABLE_TELEMETRY='true',
               JWT_SECRET=secrets.token_urlsafe(32), SIG_KEY=secrets.token_hex(32),
               SIG_SALT=secrets.token_hex(16), LLM_PROVIDER='ollama', VECTOR_DB='lancedb')
    children, handles, workspaces, locations = [], [], [], set()
    report = {'isolation': {'storage': str(storage), 'server_port': PORT, 'collector_port': COLLECTOR,
                            'empty_initial_documents': True, 'empty_initial_vectors': True,
                            'production_database_opened_read_only': True}, 'cases': []}
    try:
        for filename in ('collector.js', 'server.js'):
            stdout = (base / (filename + '.stdout.log')).open('w')
            stderr = (base / (filename + '.stderr.log')).open('w')
            handles.extend([stdout, stderr])
            children.append(subprocess.Popen([str(executable), str(backend / filename)], cwd=backend,
                                             env=env, stdout=stdout, stderr=stderr,
                                             creationflags=subprocess.CREATE_NO_WINDOW))
        deadline = time.monotonic() + 40
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('An isolated backend process exited during startup; inspect isolated logs')
            try:
                status, _ = pipeline.get_json(API + '/api/ping', api_key=key, timeout=1)
                if status == 200:
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Isolated backend did not become healthy within 40 seconds')
            time.sleep(0.2)
        # Scope the ordinary production uploader's default path to this process
        # only. Neither the existing assistant nor Desktop inherits this value.
        os.environ['APPDATA'] = str(base / 'roaming')
        os.environ['ANYTHINGLLM_PDF_ASSISTANT_HOME'] = str(base / 'assistant-home')
        import rag_pdf_gradio_app as app
        assert app.default_anythingllm_storage_dir().resolve() == storage.resolve()
        prepared = json.loads((receipt / 'results.json').read_text())['cases']
        if '--pilot' in sys.argv:
            prepared = prepared[:1]
        groups = [prepared[-9:]] if '--mixed' in sys.argv else [[case] for case in prepared]
        for index, group in enumerate(groups, 1):
            assert all(case['status'] == 'complete' for case in group)
            summaries = [read_run_json(Path(case['run_root']) / 'document/run-summary.json') for case in group]
            for summary, case in zip(summaries, group):
                # The GUI coordinator adds source identity after worker return;
                # the worker's standalone run-summary intentionally omits it.
                summary['pdf'] = case['source']
            status, body = pipeline.post_json(API + '/api/v1/workspace/new',
                                             {'name': f'Isolated Replay {index:02d}'}, api_key=key)
            assert status == 200, f'Isolated workspace creation HTTP {status}'
            workspace = json.loads(body)['workspace']
            slug = workspace['slug']
            workspaces.append(slug)
            root = base / 'assistant-home/run-state/automatic-runs' / f'r-live-{index:02d}'
            root.mkdir(parents=True)
            print(f'LIVE START {index}/{len(groups)} {len(group)} PDF(s)', flush=True)
            outcome = app.upload_prepared_automatic_batch(summaries, api_url=API, api_key=key,
                                                         workspace_slug=slug, run_root=root)
            pipeline.write_json(root / 'batch-native-upload-report.json', outcome)
            locations.update(outcome.get('locations') or [])
            result = {'sources': [case['source'] for case in group], 'workspace': slug,
                      'status': outcome.get('status'), 'cache_reused': outcome.get('prepared_record_cache_reused_count'),
                      'embedded': outcome.get('embedded'), 'errors': outcome.get('errors'),
                      'report': str(root / 'batch-native-upload-report.json')}
            report['cases'].append(result)
            (base / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
            assert not result['cache_reused'], 'Cache reuse invalidates fresh qualification'
            assert result['status'] == 'complete', 'Fresh upload did not complete'
            assert len(outcome['document_results']) == len(group)
            assert all(value['status'] == 'complete' and value['post_status'] == 'pass'
                       and value['searchability_proven'] and not value.get('error')
                       for value in outcome['document_results'].values())
            assert outcome['selected_records'] == outcome['vector_confirmed_records']
            print(f'LIVE END {index} complete', flush=True)
    finally:
        # All deletions target only the isolated URL and its freshly created
        # workspaces/documents. The user instance is never a cleanup target.
        cleanup_errors = []
        for slug in workspaces:
            try:
                with sqlite3.connect(storage / 'anythingllm.db') as db:
                    linked = [row[0] for row in db.execute(
                        'select docpath from workspace_documents where workspaceId in '
                        '(select id from workspaces where slug=?)', (slug,))]
                locations.update(linked)
                status, _ = pipeline.post_json(API + '/api/v1/workspace/' + slug + '/update-embeddings',
                                              {'adds': [], 'deletes': linked}, api_key=key, timeout=120)
                if not 200 <= status < 300:
                    cleanup_errors.append(f'embeddings {slug}: HTTP {status}')
                status, _ = pipeline.delete_json(API + '/api/v1/workspace/' + slug, api_key=key)
                if not 200 <= status < 300:
                    cleanup_errors.append(f'workspace {slug}: HTTP {status}')
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if locations:
            try:
                status, _ = pipeline.delete_json(API + '/api/v1/system/remove-documents', api_key=key,
                                                 body={'names': sorted(locations)}, timeout=120)
                if not 200 <= status < 300:
                    cleanup_errors.append(f'isolated documents: HTTP {status}')
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        for child in reversed(children):
            if child.poll() is None:
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True, check=False)
                child.wait(timeout=15)
        for handle in handles:
            handle.close()
        report['cleanup_errors'] = cleanup_errors
        report['owned_test_processes_stopped'] = True
        with sqlite3.connect((storage / 'anythingllm.db').as_uri() + '?mode=ro', uri=True) as db:
            report['remaining_test_rows'] = {table: db.execute('select count(*) from ' + table).fetchone()[0]
                                            for table in ('workspaces', 'workspace_documents', 'document_vectors')}
        report['remaining_cache_files'] = len([p for p in (storage / 'vector-cache').rglob('*') if p.is_file()])
        (base / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
