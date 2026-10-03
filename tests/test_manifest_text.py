import json
import shutil

import pytest

from canonical_artifacts import CanonicalArtifacts, checked_role_path
from manifest_text import ManifestTextWriter, read_manifest_rows
import auto_anythingllm_pipeline as pipeline
from run_evidence import read_run_json

pytestmark = pytest.mark.offline_deterministic


def test_distinct_records_share_text_without_changing_reader_contract(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    text = 'Preserved book text with newlines.\n' * 200
    left = {'text': text, 'backend': 'native', 'page': 1}
    right = {'textContent': text, 'metadata': {'author': 'Other Author'}}
    one = catalog.jsonl(tmp_path / 'candidates/native/manifest.jsonl', [left])
    two = catalog.jsonl(tmp_path / 'metadata/payloads.jsonl', [right])
    assert len((tmp_path / 'manifest-text.jsonl').read_text().splitlines()) == 1
    assert read_manifest_rows(one) == [left]
    assert read_manifest_rows(two) == [right]
    assert left['text'] == text
    assert one.stat().st_size < 300


def test_relocated_document_and_legacy_inline_manifests_still_read(tmp_path):
    root = tmp_path / 'original'
    catalog = CanonicalArtifacts(root, pipeline.atomic_write_text)
    row = {'text': 'body ' * 1000, 'pdf_page': 7}
    catalog.jsonl(root / 'metadata/manifest.jsonl', [row])
    moved = tmp_path / 'moved'
    shutil.copytree(root, moved)
    assert read_manifest_rows(moved / 'metadata/manifest.jsonl') == [row]
    legacy = tmp_path / 'legacy.jsonl'
    legacy.write_text(json.dumps(row) + '\n')
    assert read_manifest_rows(legacy) == [row]


@pytest.mark.parametrize('failure', ['missing', 'modified'])
def test_corrupt_text_evidence_fails_visibly(tmp_path, failure):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    path = catalog.jsonl(tmp_path / 'manifest.jsonl', [{'text': 'content ' * 1000}])
    store = tmp_path / 'manifest-text.jsonl'
    if failure == 'missing':
        store.unlink()
    else:
        store.write_text(store.read_text().replace('content', 'changed', 1))
    with pytest.raises(ValueError):
        read_manifest_rows(path)


def test_writer_reopens_existing_store_without_duplicating_bodies(tmp_path):
    row = {'text': 'Repeated text. ' * 400}
    assert ManifestTextWriter(tmp_path).record(row) == ManifestTextWriter(tmp_path).record(row)
    assert len((tmp_path / 'manifest-text.jsonl').read_text().splitlines()) == 1


def test_global_static_artifacts_share_bytes_and_remain_portable(tmp_path, monkeypatch):
    monkeypatch.setenv('ANYTHINGLLM_PDF_ASSISTANT_HOME', str(tmp_path))
    root = tmp_path / 'run-state/automatic-runs/r-test'
    actuals = []
    for name in ('one', 'two'):
        doc = root / name
        catalog = CanonicalArtifacts(doc, pipeline.atomic_write_text)
        path = doc / 'inspection/columns.csv'
        path.parent.mkdir(parents=True)
        path.write_text('name,meaning\ntext,body\n')
        actual = catalog.shared_file(path)
        actuals.append(actual)
        assert not path.exists()
        assert checked_role_path(doc, catalog.roles['inspection\\columns.csv']) == actual
        pipeline.write_json(doc / 'inspection/lancedb-before.json', {'rows': 'large shared evidence ' * 1000})
    assert actuals[0] == actuals[1]
    assert len(list((root / '.run-evidence').glob('*.csv'))) == 1
    assert len(list((root / '.run-evidence').glob('*.json'))) == 1
    assert read_run_json(root / 'one/inspection/lancedb-before.json')['rows'].startswith('large')
    moved = tmp_path / 'moved'
    shutil.copytree(root, moved)
    relative = str(actuals[0].relative_to(root))
    assert checked_role_path(moved / 'one', '../' + relative).read_text().startswith('name')


def test_unverified_external_static_role_is_rejected(tmp_path):
    doc = tmp_path / 'run/doc'
    doc.mkdir(parents=True)
    pool = doc.parent / '.run-evidence'
    pool.mkdir()
    path = pool / ('a' * 64 + '.md')
    path.write_text('not matching hash')
    with pytest.raises(ValueError):
        checked_role_path(doc, '../.run-evidence/' + path.name)
