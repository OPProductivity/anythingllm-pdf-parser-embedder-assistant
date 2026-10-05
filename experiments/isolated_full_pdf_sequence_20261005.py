"""Full one-PDF automatic runs against a fresh owned AnythingLLM backend.

start --base NEW_DIRECTORY [--expected-count 132]
run-one --base DIRECTORY --index 1
run-one --base DIRECTORY --index 1 --retry-failed  # only a cleaned failed case
cleanup --base DIRECTORY --index 1   # after reviewing retained evidence
cleanup --base DIRECTORY            # all remaining resources, then owned servers

Each run-one starts a fresh guarded Python interpreter. Cleanup is required
before the next index, preventing prepared-record/vector cache reuse. Source PDFs,
run artifacts, logs, and cleanup receipts are preserved. Production storage is
opened read-only solely for schema and embedder configuration during start.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parents[1]
STUDY = Path.home() / 'Documents/Documenten 2025 - 2026/studie/_blok 1 & 2 & 3 & 4'
PRODUCTION = Path.home() / 'AppData/Roaming/anythingllm-desktop/storage'
GUARD = Path(__file__).with_name('isolated_full_pdf_guard')
sys.path.insert(0, str(REPO))


def save(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def contained(path, base):
    resolved = Path(path).resolve()
    if resolved == base or not resolved.is_relative_to(base) or resolved.is_relative_to(PRODUCTION.resolve()):
        raise RuntimeError('Resource path is outside owned isolation tree')
    return resolved


def load(base):
    base = Path(base).resolve()
    ledger = json.loads((base / 'ledger.json').read_text(encoding='utf-8'))
    if ledger.get('base') != str(base) or ledger.get('schema') != 1:
        raise RuntimeError('Invalid isolation ledger')
    contained(ledger['storage'], base)
    if ledger['api'] != 'http://127.0.0.1:' + str(ledger['server_port']):
        raise RuntimeError('Unexpected API URL')
    if ledger['server_port'] not in range(43100, 44000):
        raise RuntimeError('Unsafe API port')
    return base, ledger


def database(ledger):
    path = Path(ledger['storage']) / 'anythingllm.db'
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def key(ledger):
    with closing(database(ledger)) as db:
        row = db.execute('select secret from api_keys where name=?', (ledger['owner'],)).fetchone()
    if not row:
        raise RuntimeError('Isolated API credential missing')
    return row[0]


def api(ledger, method, path, body=None, timeout=120):
    if not path.startswith('/api/') or '://' in path:
        raise RuntimeError('Invalid isolated API path')
    request = urllib.request.Request(ledger['api'] + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Authorization': 'Bearer ' + key(ledger), 'Content-Type': 'application/json'})
    # Never inherit a proxy for the isolated loopback endpoint, or follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError('Isolated API redirect rejected')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        payload = response.read()
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Desktop DELETE endpoints can return plain-text 2xx responses.
            # Persist only status, avoiding accidental sensitive response data.
            return {'http_status': response.status, 'response_format': 'non_json'}


def identity(pid):
    import psutil
    process = psutil.Process(pid)
    return {'pid': pid, 'created': process.create_time()}


def owned_process(record):
    import psutil
    try:
        process = psutil.Process(record['pid'])
        return process if process.create_time() == record['created'] else None
    except psutil.NoSuchProcess:
        return None


def require_servers(ledger):
    if ledger.get('state') != 'ready' or len(ledger['servers']) != 2:
        raise RuntimeError('Isolated backend is not ready')
    if not all(owned_process(record) for record in ledger['servers']):
        raise RuntimeError('Owned isolated backend process is absent; no fallback permitted')
    import psutil
    owners = {record['pid'] for record in ledger['servers']}
    listeners = [connection for connection in psutil.net_connections(kind='tcp')
                 if connection.status == psutil.CONN_LISTEN and connection.laddr.port == ledger['server_port']]
    if not listeners or any(connection.pid not in owners for connection in listeners):
        raise RuntimeError('API listener does not belong to the recorded isolated backend')
    api(ledger, 'GET', '/api/ping', timeout=2)


def manifest(mmt, toc, expected):
    import fitz
    paths = list(mmt.rglob('*.pdf')) + [p for p in toc.rglob('*.pdf') if p.parent != toc]
    paths = sorted(set(p.resolve() for p in paths), key=lambda p: str(p).casefold())
    if len(paths) != expected:
        raise RuntimeError(f'Expected {expected} PDFs, found {len(paths)}')
    rows = []
    for path in paths:
        with fitz.open(path) as pdf:
            pages = len(pdf)
        rows.append({'source': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size, 'pages': pages})
    rows.sort(key=lambda row: (row['pages'], row['bytes'], row['source'].casefold()))
    for index, row in enumerate(rows, 1):
        row['index'] = index
    return rows


def start(options):
    base = options.base.resolve()
    if base.exists() or base.is_relative_to(PRODUCTION.resolve()):
        raise RuntimeError('Start requires a new directory outside production storage')
    rows = manifest(options.mmt_root.resolve(), options.toc_root.resolve(), options.expected_count)
    for port in (options.port, options.collector_port):
        if port not in range(43100, 44000):
            raise RuntimeError('Ports must be in the reserved harness range 43100..43999')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    if options.port == options.collector_port:
        raise RuntimeError('Server and collector ports must differ')
    base.mkdir(parents=True)
    storage = base / 'roaming/anythingllm-desktop/storage'
    owner = 'isolated-full-' + secrets.token_hex(8)
    ledger = {'schema': 1, 'base': str(base), 'storage': str(storage), 'owner': owner,
              'api': f'http://127.0.0.1:{options.port}', 'server_port': options.port,
              'collector_port': options.collector_port, 'state': 'starting', 'servers': [], 'cases': {}}
    save(base / 'ledger.json', ledger)
    save(base / 'manifest.json', rows)
    for name in ('hotdir', 'tmp', 'documents', 'direct-uploads', 'logs', 'vector-cache',
                 'lancedb', 'models', 'engines', 'plugins', 'generated-files'):
        (storage / name).mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect((PRODUCTION / 'anythingllm.db').as_uri() + '?mode=ro', uri=True)) as source:
        schema = source.execute("select sql from sqlite_master where sql is not null "
                                "and name not like 'sqlite_%' order by type='index'").fetchall()
        settings = source.execute("select label,value from system_settings where label in "
                                  "('text_splitter_chunk_size','text_splitter_chunk_overlap')").fetchall()
    with closing(sqlite3.connect(storage / 'anythingllm.db')) as target, target:
        for (sql,) in schema:
            target.execute(sql)
        target.executemany('insert into system_settings(label,value) values (?,?)', settings)
        target.execute('insert into api_keys(name,secret) values (?,?)', (owner, secrets.token_urlsafe(36)))
    from anythingllm_state import read_env_values
    configured = read_env_values(PRODUCTION / '.env')
    embed_keys = ('EMBEDDING_ENGINE', 'EMBEDDING_MODEL_PREF', 'EMBEDDING_MODEL_MAX_CHUNK_LENGTH',
                  'OPENROUTER_API_KEY', 'OPENROUTER_TIMEOUT_MS')
    if configured.get('EMBEDDING_ENGINE') != 'openrouter' or not configured.get('OPENROUTER_API_KEY'):
        raise RuntimeError('This harness requires the currently configured OpenRouter embedder')
    embedding = {name: configured[name] for name in embed_keys if name in configured}
    # The app also reads this isolated env file to resolve current embedder limits.
    (storage / '.env').write_text('\n'.join(f'{name}={value}' for name, value in embedding.items()) + '\n', encoding='utf-8')
    ledger['embedding'] = {name: value for name, value in embedding.items() if 'KEY' not in name}
    ledger['splitter_settings'] = dict(settings)
    save(base / 'ledger.json', ledger)
    backend = Path.home() / 'AppData/Local/Programs/AnythingLLM/resources/backend'
    executable = backend.parents[1] / 'AnythingLLM.exe'
    env = dict(os.environ)
    env.update(embedding)
    env.update(ELECTRON_RUN_AS_NODE='1', NODE_ENV='production', STORAGE_DIR=str(storage),
               DATABASE_URL='file:' + (storage / 'anythingllm.db').as_posix(),
               SERVER_PORT=str(options.port), COLLECTOR_PORT=str(options.collector_port),
               APPDATA=str(base / 'roaming'), APP_DISCOVERABLE='false', DISABLE_TELEMETRY='true',
               JWT_SECRET=secrets.token_urlsafe(32), SIG_KEY=secrets.token_hex(32),
               SIG_SALT=secrets.token_hex(16), LLM_PROVIDER='ollama', VECTOR_DB='lancedb')
    for filename in ('collector.js', 'server.js'):
        with (base / (filename + '.stdout.log')).open('w') as stdout, (base / (filename + '.stderr.log')).open('w') as stderr:
            child = subprocess.Popen([str(executable), str(backend / filename)], cwd=backend,
                                     env=env, stdout=stdout, stderr=stderr,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ledger['servers'].append(dict(identity(child.pid), entrypoint=filename))
        save(base / 'ledger.json', ledger)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if not all(owned_process(record) for record in ledger['servers']):
            raise RuntimeError('Owned backend exited; inspect preserved logs and run cleanup')
        try:
            api(ledger, 'GET', '/api/ping', timeout=1)
            ledger['state'] = 'ready'
            save(base / 'ledger.json', ledger)
            print(json.dumps({'base': str(base), 'pdfs': len(rows), 'state': 'ready'}))
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError('Owned backend failed startup; run cleanup to stop recorded processes')


def resource_snapshot(ledger):
    with closing(database(ledger)) as db:
        db.row_factory = sqlite3.Row
        columns = {'workspaces': 'id,name,slug',
                   'workspace_documents': 'id,docId,docpath,workspaceId',
                   'document_vectors': 'docId,vectorId'}
        tables = {table: [dict(row) for row in db.execute('select ' + fields + ' from ' + table)]
                  for table, fields in columns.items()}
    storage = Path(ledger['storage'])
    tables['document_files'] = [p.relative_to(storage / 'documents').as_posix() for p in (storage / 'documents').rglob('*') if p.is_file()]
    tables['vector_bytes'] = sum(p.stat().st_size for p in (storage / 'lancedb').rglob('*') if p.is_file())
    tables['cache_files'] = [str(p.relative_to(storage / 'vector-cache')) for p in (storage / 'vector-cache').rglob('*') if p.is_file()]
    return tables


def guard_env(base, ledger, case_root):
    env = dict(os.environ)
    env.update(ISOLATED_FULL_PDF_API=ledger['api'], ISOLATED_FULL_PDF_BASE=str(base),
               ISOLATED_FULL_PDF_COLLECTOR_PORT=str(ledger['collector_port']),
               APPDATA=str(base / 'roaming'), ANYTHINGLLM_PDF_ASSISTANT_HOME=str(case_root / 'assistant-home'),
               PYTHONPATH=str(GUARD) + os.pathsep + str(REPO),
               NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    return env


def run_one(options):
    expected_python = REPO / '.venv' / 'Scripts' / 'python.exe'
    if Path(sys.executable).resolve() != expected_python.resolve():
        raise RuntimeError(f'Run the harness with the repository virtual environment: {expected_python}')
    base, ledger = load(options.base)
    require_servers(ledger)
    if any(not case.get('cleaned') for case in ledger['cases'].values()):
        raise RuntimeError('Clean up the preceding case before starting another PDF')
    rows = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    row = next((r for r in rows if r['index'] == options.index), None)
    prior = ledger['cases'].get(str(options.index))
    if row is None:
        raise RuntimeError('Unknown source index')
    if prior and not (options.retry_failed and prior.get('cleaned') and prior.get('state') == 'failed'):
        raise RuntimeError('Index already attempted; --retry-failed requires a cleaned failed case')
    if digest(row['source']) != row['sha256']:
        raise RuntimeError('PDF changed since manifest creation')
    before = resource_snapshot(ledger)
    if any(before[name] for name in ('workspaces', 'workspace_documents', 'document_vectors', 'cache_files', 'document_files')):
        raise RuntimeError('Isolated storage is not empty before this PDF')
    history = []
    if prior:
        history = list(prior.get('prior_attempts', []))
        history.append({name: value for name, value in prior.items() if name != 'prior_attempts'})
    suffix = f'-attempt-{len(history) + 1:02d}' if history else ''
    case_root = base / f'case-{options.index:03d}{suffix}'
    case_root.mkdir()
    case = dict(row, root=str(case_root), state='creating-workspace', cleaned=False)
    if history:
        case['prior_attempts'] = history
    ledger['cases'][str(options.index)] = case
    save(base / 'ledger.json', ledger)
    save(case_root / 'before.json', before)
    workspace = api(ledger, 'POST', '/api/v1/workspace/new', {'name': ledger['owner'] + f'-{options.index:03d}'})['workspace']
    case.update(workspace=workspace['slug'], workspace_id=workspace['id'], state='running')
    save(base / 'ledger.json', ledger)
    command = [sys.executable, str(Path(__file__).resolve()), '_worker', '--base', str(base), '--index', str(options.index)]
    with (case_root / 'worker.stdout.log').open('w') as stdout, (case_root / 'worker.stderr.log').open('w') as stderr:
        process = subprocess.Popen(command, cwd=REPO, env=guard_env(base, ledger, case_root), stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        case['process'] = identity(process.pid)
        save(base / 'ledger.json', ledger)
        code = process.wait()
    case.update(state='finished' if code == 0 else 'failed', exit_code=code)
    save(case_root / 'resources.json', resource_snapshot(ledger))
    save(base / 'ledger.json', ledger)
    print(json.dumps({'index': options.index, 'exit_code': code, 'root': str(case_root), 'cleanup_required': True}))
    return code


def worker(options):
    base, ledger = load(options.base)
    if os.environ.get('ISOLATED_FULL_PDF_GUARD_ACTIVE') != '1':
        raise RuntimeError('Worker guard is absent')
    require_servers(ledger)
    case = ledger['cases'][str(options.index)]
    root = contained(case['root'], base)
    import rag_pdf_gradio_app as app
    if app.default_anythingllm_storage_dir().resolve() != Path(ledger['storage']).resolve():
        raise RuntimeError('App did not resolve isolated storage')
    settings = app.builtin_automatic_run_setting_values([case['source']], [])
    settings.update(mode=app.MODE_NATIVE_UPLOAD_LABEL, api_url=ledger['api'], api_key=key(ledger),
                    workspace_slug=case['workspace'], output_root_override=str(root / 'output'),
                    native_upload_scope=app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                    native_upload_custom_range='', include_front_matter=True, include_back_matter=True,
                    first_page_override=0, end_page_override=0, segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL,
                    run_root_override=str(root / 'assistant-home' / 'run-state' / 'automatic-runs'
                                          / f'r-full-{options.index:03d}'),
                    retain_detailed_evidence=True, auto_apply_recommended_settings=False,
                    progress=lambda *args, **kwargs: None)
    if not app.is_private_run_state_path(Path(settings['run_root_override'])):
        raise RuntimeError('Run state is not classified as private by the production application')
    serializable = {name: value for name, value in settings.items() if name not in ('api_key', 'progress')}
    save(root / 'settings.json', serializable)
    parameters = inspect.signature(app.run_automatic).parameters
    result = app.run_automatic(**{name: value for name, value in settings.items() if name in parameters})
    # UI widgets can contain non-JSON objects. Completion is established by
    # durable run evidence, independently of this optional display receipt.
    try:
        save(root / 'return.json', result)
    except (TypeError, ValueError):
        save(root / 'return.json', {'kind': type(result).__name__, 'serialization': 'UI return omitted'})
    from run_evidence import read_run_json
    progress = read_run_json(Path(settings['run_root_override']) / 'run-progress.json')
    save(root / 'terminal.json', progress)
    if progress.get('state') not in ('successful', 'complete', 'completed'):
        raise RuntimeError('Automatic run did not report successful completion')
    print('Full automatic run completed; semantic audit and cleanup still required', flush=True)


def cleanup(options):
    base, ledger = load(options.base)
    selected = [str(options.index)] if options.index is not None else list(ledger['cases'])
    errors = []
    for index in selected:
        case = ledger['cases'][index]
        if case.get('cleaned'):
            continue
        if case.get('process') and owned_process(case['process']):
            raise RuntimeError('A case worker is still alive; cleanup refuses active runs')
        root = contained(case['root'], base)
        attempt = root / f'cleanup-attempt-{len(list(root.glob("cleanup-attempt-*"))) + 1:02d}'
        attempt.mkdir()

        def receipt(name, data):
            save(attempt / name, data)
            # Retain first ownership evidence across interrupted retries; the
            # after receipt is a convenient latest-state pointer, with every
            # version also preserved inside its own attempt directory.
            if name == 'cleanup-after.json' or not (root / name).exists():
                save(root / name, data)

        before = resource_snapshot(ledger)
        receipt('cleanup-before.json', {name: len(value) if isinstance(value, list) else value
                                        for name, value in before.items()})
        # A single active case owns this entire originally empty backend. An
        # interrupted workspace-creation response is recoverable by owner name.
        workspaces = [w for w in before['workspaces'] if w['name'] == ledger['owner'] + f'-{int(index):03d}']
        if len(workspaces) != len(before['workspaces']):
            raise RuntimeError('Unexpected workspace ownership; refusing cleanup')
        live_slugs = {workspace['slug'] for workspace in workspaces}
        previous_ownership = root / 'cleanup-ownership.json'
        if previous_ownership.exists():
            previous = json.loads(previous_ownership.read_text(encoding='utf-8'))
            for workspace in previous.get('workspaces', []):
                if workspace['name'] != ledger['owner'] + f'-{int(index):03d}':
                    raise RuntimeError('Invalid prior cleanup ownership')
                if workspace['slug'] not in live_slugs:
                    workspaces.append(workspace)
        locations = sorted({r['docpath'] for r in before['workspace_documents']} | set(before['document_files']))
        vector_ids = sorted({str(r['vectorId']) for r in before['document_vectors']})
        import lancedb
        vector_db = lancedb.connect(str(Path(ledger['storage']) / 'lancedb'))
        physical = {}
        for workspace in workspaces:
            slug = workspace['slug']
            if slug in vector_db.table_names():
                table = vector_db.open_table(slug)
                count = table.count_rows()
                physical[slug] = ([row['id'] for row in table.search().select(['id']).limit(count).to_list()]
                                  if count else [])
        vector_ids = sorted(set(vector_ids).union(*(set(str(item) for item in ids) for ids in physical.values())))
        receipt('cleanup-ownership.json', {'workspaces': workspaces, 'locations': locations, 'vector_ids': vector_ids})
        for workspace in workspaces:
            slug = workspace['slug']
            linked = [r['docpath'] for r in before['workspace_documents'] if r['workspaceId'] == workspace['id']]
            if slug in live_slugs:
                api(ledger, 'POST', '/api/v1/workspace/' + slug + '/update-embeddings', {'adds': [], 'deletes': linked})
        # API removal can omit physical Lance rows on failed uploads. Delete
        # only recorded IDs, then remove empty owned namespaces via the API.
        for workspace in workspaces:
            slug = workspace['slug']
            if slug in vector_db.table_names():
                table = vector_db.open_table(slug)
                for offset in range(0, len(vector_ids), 200):
                    ids = vector_ids[offset:offset + 200]
                    table.delete('id IN (' + ','.join("'" + item.replace("'", "''") + "'" for item in ids) + ')')
                if table.count_rows() != 0:
                    raise RuntimeError('Unowned/unrecorded physical vector rows remain; preserving them for diagnosis')
            if slug in live_slugs:
                api(ledger, 'DELETE', '/api/v1/workspace/' + slug)
            if slug in vector_db.table_names():
                if vector_db.open_table(slug).count_rows() != 0:
                    raise RuntimeError('Workspace API left nonempty physical namespace')
                vector_db.drop_table(slug)
        if locations:
            api(ledger, 'DELETE', '/api/v1/system/remove-documents', {'names': locations})
        # A failed upload can leave cache or unlinked document files that the
        # API cannot enumerate. They were recorded in the empty-owned-store
        # snapshot above; only those exact files are eligible for deletion.
        for folder, field in (('vector-cache', 'cache_files'), ('documents', 'document_files')):
            for relative in before[field]:
                path = contained(Path(ledger['storage']) / folder / relative, base)
                if path.is_file():
                    path.unlink()
        after = resource_snapshot(ledger)
        after['vector_namespaces'] = vector_db.table_names()
        receipt('cleanup-after.json', after)
        if any(after[name] for name in ('workspaces', 'workspace_documents', 'document_vectors', 'document_files', 'cache_files', 'vector_namespaces')):
            errors.append('Case ' + index + ' has remaining isolated resources')
        else:
            case['cleaned'] = True
        save(base / 'ledger.json', ledger)
    if options.index is None and not errors:
        import psutil
        for record in reversed(ledger['servers']):
            process = owned_process(record)
            if process:
                children = process.children(recursive=True)
                for child in reversed(children):
                    child.terminate()
                process.terminate()
                _, alive = psutil.wait_procs(children + [process], timeout=10)
                for child in alive:
                    child.kill()
                psutil.wait_procs(alive, timeout=5)
        if any(owned_process(record) for record in ledger['servers']):
            raise RuntimeError('Owned backend processes did not stop')
        with closing(sqlite3.connect(Path(ledger['storage']) / 'anythingllm.db')) as db, db:
            db.execute('delete from api_keys where name=?', (ledger['owner'],))
        env_path = contained(Path(ledger['storage']) / '.env', base)
        if env_path.exists():
            env_path.unlink()
        ledger['state'] = 'cleaned'
        save(base / 'ledger.json', ledger)
    print(json.dumps({'cleaned_indices': selected, 'state': ledger['state'], 'errors': errors}))
    return int(bool(errors))


@contextmanager
def command_lock(base):
    path = Path(base).resolve() / 'command-lock.json'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        previous = json.loads(path.read_text(encoding='utf-8'))
        if owned_process(previous):
            raise RuntimeError('Another harness command is active')
        path.unlink()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(identity(os.getpid()), handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        yield
    finally:
        path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('start', 'run-one', 'cleanup', '_worker'))
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--index', type=int)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--mmt-root', type=Path, default=STUDY / 'MMT - Keywords Resit/sources')
    parser.add_argument('--toc-root', type=Path, default=STUDY / 'TOC III')
    parser.add_argument('--expected-count', type=int, default=132)
    parser.add_argument('--port', type=int, default=43131)
    parser.add_argument('--collector-port', type=int, default=43132)
    options = parser.parse_args()
    if options.command in ('run-one', '_worker') and options.index is None:
        parser.error('--index is required')
    handler = {'start': start, 'run-one': run_one, 'cleanup': cleanup, '_worker': worker}[options.command]
    if options.command in ('run-one', 'cleanup'):
        with command_lock(options.base):
            return handler(options) or 0
    return handler(options) or 0


if __name__ == '__main__':
    raise SystemExit(main())
