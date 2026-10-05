"""Local safety checks. No production HTTP requests or full-PDF runs."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.offline_deterministic

SCRIPT = Path(__file__).with_name('isolated_full_pdf_sequence_20261005.py')
spec = importlib.util.spec_from_file_location('isolated_harness', SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class IsolationTests(unittest.TestCase):
    def test_guard_inherited_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = harness.guard_env(root, {'api': 'http://127.0.0.1:43199', 'collector_port': 43198}, root / 'case')
            # A fake application module exercises the import-time patch with
            # no application imports or provider/backend requests.
            (root / 'auto_anythingllm_pipeline.py').write_text(
                "DEFAULT_ANYTHINGLLM_API_URL='http://127.0.0.1:3001'\n"
                "ANYTHINGLLM_API_CANDIDATE_URLS=('http://127.0.0.1:3001',)\n"
                "def preferred_anythingllm_api_urls(url=''): return ['unsafe']\n"
                "def start_anythingllm_desktop(): return 'unsafe'\n", encoding='utf-8')
            code = r'''
import asyncio, os, socket, subprocess, sys
import auto_anythingllm_pipeline as p
assert os.environ['ISOLATED_FULL_PDF_GUARD_ACTIVE'] == '1'
assert p.preferred_anythingllm_api_urls() == ['http://127.0.0.1:43199']
left, right = socket.socketpair()
left.send(b'x')
assert right.recv(1) == b'x'
left.close(); right.close()
loop = asyncio.new_event_loop()
loop.close()
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
internal_port = listener.getsockname()[1]
client = socket.create_connection(('127.0.0.1', internal_port))
peer, _ = listener.accept()
client.close(); peer.close(); listener.close()
for call in [lambda: p.preferred_anythingllm_api_urls('http://localhost:3001'),
             p.start_anythingllm_desktop,
             lambda: socket.create_connection(('127.0.0.1',3001),timeout=.1),
             lambda: socket.create_connection(('localhost',8888),timeout=.1),
             lambda: socket.create_connection(('127.0.0.1',internal_port),timeout=.1),
             lambda: subprocess.Popen(['taskkill','/IM','AnythingLLM.exe'])]:
    try: call()
    except PermissionError: pass
    else: raise AssertionError('unsafe operation allowed')
child = subprocess.run([sys.executable,'-c',
    "import os,socket; assert os.environ['ISOLATED_FULL_PDF_GUARD_ACTIVE']=='1'; "
    "s=socket.socket(); s.connect(('127.0.0.1',3001))"], capture_output=True)
assert child.returncode != 0 and b'Non-isolated loopback endpoint blocked' in child.stderr
print('guard and descendant verified')
'''
            result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('descendant verified', result.stdout)

    def test_guard_invalid_environment_exits_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = harness.guard_env(root, {'api': 'http://127.0.0.1:3001', 'collector_port': 43198}, root / 'case')
            result = subprocess.run([sys.executable, '-c', "print('UNGUARDED')"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 92)
            self.assertNotIn('UNGUARDED', result.stdout)

    def test_owned_path_rejects_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertEqual(harness.contained(root / 'case/file', root), root / 'case/file')
            with self.assertRaises(RuntimeError):
                harness.contained(root / '../outside', root)

    def test_manifest_scope_order_and_hashes(self):
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mmt, toc = root / 'MMT', root / 'TOC'
            for relative, pages in [('MMT/a.pdf', 2), ('TOC/excluded.pdf', 1), ('TOC/sub/b.pdf', 1)]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with fitz.open() as pdf:
                    for _ in range(pages):
                        pdf.new_page()
                    pdf.save(path)
            rows = harness.manifest(mmt, toc, 2)
            self.assertEqual([row['pages'] for row in rows], [1, 2])
            self.assertEqual([row['index'] for row in rows], [1, 2])
            self.assertTrue(all(row['sha256'] == harness.digest(row['source']) for row in rows))
            with self.assertRaises(RuntimeError):
                harness.manifest(mmt, toc, 132)

    def test_exact_cleanup_with_physical_orphan_and_mock_api(self):
        import lancedb
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            storage = base / 'storage'
            case = base / 'case-001'
            case.mkdir()
            for name in ('documents', 'vector-cache', 'lancedb'):
                (storage / name).mkdir(parents=True)
            (storage / 'documents/owned.json').write_text('{}', encoding='utf-8')
            (storage / 'vector-cache/owned.json').write_text('{}', encoding='utf-8')
            with closing(sqlite3.connect(storage / 'anythingllm.db')) as db, db:
                db.executescript('CREATE TABLE workspaces(id INTEGER,name TEXT,slug TEXT);'
                                 'CREATE TABLE workspace_documents(id INTEGER,docId TEXT,docpath TEXT,workspaceId INTEGER);'
                                 'CREATE TABLE document_vectors(docId TEXT,vectorId TEXT);')
                db.execute("INSERT INTO workspaces VALUES (1,'test-owner-001','owned')")
                db.execute("INSERT INTO workspace_documents VALUES (1,'doc','owned.json',1)")
                db.execute("INSERT INTO document_vectors VALUES ('doc','v1')")
            vector_db = lancedb.connect(str(storage / 'lancedb'))
            vector_db.create_table('owned', [{'id': 'v1', 'vector': [1.0, 2.0]},
                                             {'id': 'orphan', 'vector': [2.0, 3.0]}])
            ledger = {'schema': 1, 'base': str(base), 'storage': str(storage), 'owner': 'test-owner',
                      'api': 'http://127.0.0.1:43199', 'server_port': 43199, 'state': 'ready',
                      'cases': {'1': {'root': str(case), 'cleaned': False}}}
            harness.save(base / 'ledger.json', ledger)
            calls = []

            def fake_api(ledger, method, path, body=None, timeout=120):
                calls.append((method, path, body))
                with closing(sqlite3.connect(storage / 'anythingllm.db')) as db, db:
                    if path.endswith('/update-embeddings'):
                        db.execute('DELETE FROM workspace_documents')
                        db.execute('DELETE FROM document_vectors')
                    elif path.endswith('/workspace/owned'):
                        db.execute('DELETE FROM workspaces')
                return {}

            with patch.object(harness, 'api', fake_api):
                self.assertEqual(harness.cleanup(SimpleNamespace(base=base, index=1)), 0)
            owned = json.loads((case / 'cleanup-ownership.json').read_text())
            self.assertEqual(owned['vector_ids'], ['orphan', 'v1'])
            self.assertEqual(vector_db.table_names(), [])
            self.assertFalse((storage / 'documents/owned.json').exists())
            self.assertFalse((storage / 'vector-cache/owned.json').exists())
            self.assertEqual(len(calls), 3)

    def test_retry_preserves_cleaned_failure_and_rejects_success(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            source = base / 'source.pdf'
            source.write_bytes(b'%PDF mock')
            original = base / 'case-006'
            original.mkdir()
            row = {'index': 6, 'source': str(source), 'sha256': harness.digest(source)}
            harness.save(base / 'manifest.json', [row])
            ledger = {'schema': 1, 'base': str(base), 'storage': str(base / 'storage'), 'owner': 'test',
                      'api': 'http://127.0.0.1:43199', 'server_port': 43199, 'collector_port': 43198,
                      'cases': {'6': dict(row, root=str(original), state='failed', cleaned=True)}}
            harness.save(base / 'ledger.json', ledger)
            empty = {name: [] for name in ('workspaces', 'workspace_documents', 'document_vectors', 'cache_files', 'document_files')}
            options = SimpleNamespace(base=base, index=6, retry_failed=True)
            with patch.object(harness, 'require_servers'), patch.object(harness, 'resource_snapshot', return_value=empty), \
                 patch.object(harness, 'api', return_value={'workspace': {'slug': 'mock', 'id': 1}}), \
                 patch.object(harness, 'identity', return_value={'pid': 0, 'created': 0}), \
                 patch.object(harness.subprocess, 'Popen') as process:
                process.return_value.wait.return_value = 0
                self.assertEqual(harness.run_one(options), 0)
                saved = json.loads((base / 'ledger.json').read_text())
                case = saved['cases']['6']
                self.assertEqual(Path(case['root']).name, 'case-006-attempt-02')
                self.assertEqual(case['prior_attempts'][0]['root'], str(original))
                self.assertTrue(original.is_dir())
                case['cleaned'] = True
                harness.save(base / 'ledger.json', saved)
                with self.assertRaisesRegex(RuntimeError, 'cleaned failed case'):
                    harness.run_one(options)


if __name__ == '__main__':
    unittest.main()
