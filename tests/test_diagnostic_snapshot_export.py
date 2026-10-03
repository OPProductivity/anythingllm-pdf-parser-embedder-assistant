import hashlib
import json
import zipfile

import pytest

import rag_pdf_gradio_app as app
from run_evidence import read_run_json


pytestmark = pytest.mark.offline_deterministic


def test_diagnostic_bundle_transports_only_reachable_snapshots(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    source = run / 'source'
    source.mkdir(parents=True)
    pool = run / '.run-evidence'
    pool.mkdir()

    def store(value):
        data = json.dumps(value, separators=(',', ':')).encode()
        digest = hashlib.sha256(data).hexdigest()
        path = pool / (digest + '.json')
        path.write_bytes(data)
        return {'$run_evidence': 1, 'sha256': digest}, path

    child, child_path = store({'status': 'ready', 'settings': {'mode': 'custom'}})
    parent, parent_path = store({'evidence': child})
    _, unrelated = store({'different_pdf': 'unrelated'})
    (source / 'run-summary.json').write_text(json.dumps({'run_control': parent}))
    paths, error = app.diagnostic_evidence_paths(source)
    assert error == ''
    assert child_path in paths and parent_path in paths
    assert unrelated not in paths
    monkeypatch.setattr(app, 'gradio_download_cache_path', lambda name: tmp_path / name)
    bundle = app.package_downloadable_paths(paths, 'diagnostics.zip')
    extracted = tmp_path / 'moved'
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extracted)
    assert read_run_json(extracted / 'source/run-summary.json') == read_run_json(source / 'run-summary.json')
    assert set((extracted / '.run-evidence').glob('*.json')) == {
        extracted / '.run-evidence' / child_path.name,
        extracted / '.run-evidence' / parent_path.name,
    }


def test_diagnostics_export_blocks_missing_dependency(tmp_path):
    (tmp_path / 'run-summary.json').write_text(json.dumps({'run_control': {
        '$run_evidence': 1, 'sha256': 'a' * 64,
    }}))
    paths, error = app.diagnostic_evidence_paths(tmp_path)
    assert paths == []
    assert 'complete diagnostics evidence' in error


def test_canonical_artifact_bundle_is_self_contained(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'run-summary.json').write_text('{}')
    (run / 'Paper-parsed.txt').write_text('Original body')
    candidate = run / 'candidates' / 'native'
    candidate.mkdir(parents=True)
    (candidate / 'report.json').write_text('{}')
    roles = {'candidate/body.txt': 'Paper-parsed.txt',
             'selected/body.txt': 'Paper-parsed.txt',
             'selected/report.json': 'candidates/native/report.json'}
    (run / 'artifact-locations.json').write_text(json.dumps({'schema_version': 1, 'roles': roles}))
    paths, error = app.diagnostic_evidence_paths(run)
    assert error == ''
    monkeypatch.setattr(app, 'gradio_download_cache_path', lambda name: tmp_path / name)
    bundle = app.package_downloadable_paths(paths, 'canonical.zip')
    moved = tmp_path / 'moved'
    with zipfile.ZipFile(bundle) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        archive.extractall(moved)
    for relative in roles.values():
        assert (moved / relative).is_file()
    assert (moved / 'Paper-parsed.txt').read_text() == 'Original body'


@pytest.mark.parametrize('relative', ['../outside.txt', 'missing.txt'])
def test_canonical_bundle_rejects_missing_or_escaped_files(tmp_path, relative):
    (tmp_path / 'run-summary.json').write_text('{}')
    (tmp_path / 'artifact-locations.json').write_text(json.dumps({'roles': {'body.txt': relative}}))
    paths, error = app.diagnostic_evidence_paths(tmp_path)
    assert paths == []
    assert 'Missing or unsafe canonical artifact' in error
