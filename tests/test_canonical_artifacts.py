from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
from canonical_artifacts import CanonicalArtifacts


pytestmark = pytest.mark.offline_deterministic


def test_identical_static_bodies_use_one_semantic_file(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    first = catalog.text(tmp_path / 'segments-strict' / 'first.txt', 'Exact body.\nNext line.')
    second = catalog.text(tmp_path / 'parents-native' / 'other.txt', 'Exact body.\nNext line.')
    assert first == second
    assert not (tmp_path / 'parents-native' / 'other.txt').exists()
    assert first.stat().st_nlink == 1
    assert catalog.roles[str(Path('parents-native') / 'other.txt')] == str(first.relative_to(tmp_path))


def test_different_manifest_metadata_remains_separate(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    first = catalog.jsonl(tmp_path / 'candidate.jsonl', [{'text': 'same', 'source_author': 'unknown'}])
    second = catalog.jsonl(tmp_path / 'selected.jsonl', [{'text': 'same', 'source_author': 'Recovered author'}])
    assert first != second
    assert first.read_bytes() != second.read_bytes()


def test_identical_manifest_reuses_candidate(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    rows = [{'text': 'same', 'page': 1}]
    first = catalog.jsonl(tmp_path / 'candidate.jsonl', rows)
    assert catalog.jsonl(tmp_path / 'selected.jsonl', rows) == first
    assert not (tmp_path / 'selected.jsonl').exists()


def test_promote_body_updates_aliases_without_copy(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    candidate = catalog.text(tmp_path / 'candidate' / 'body.txt', 'text')
    catalog.text(tmp_path / 'variant' / 'body.txt', 'text')
    parsed = catalog.promote(candidate, tmp_path / 'Paper-pdf-parsed.txt')
    assert not candidate.exists()
    assert parsed.read_text() == 'text'
    assert all(value == 'Paper-pdf-parsed.txt' for value in catalog.roles.values())
    assert catalog.text(tmp_path / 'future.txt', 'text') == parsed


def test_missing_or_modified_canonical_file_is_not_reused(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    first = catalog.text(tmp_path / 'first.txt', 'before')
    first.write_text('after')
    second = catalog.text(tmp_path / 'second.txt', 'before')
    assert second != first
    first.unlink()
    assert catalog.text(tmp_path / 'third.txt', 'before') == second


def test_canonical_paths_cannot_escape_document(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    with pytest.raises(ValueError):
        catalog.text(tmp_path / '..' / 'outside.txt', 'text')


def test_canonical_body_cannot_be_overwritten_or_deleted_by_self_promotion(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    path = catalog.text(tmp_path / 'body.txt', 'original')
    assert catalog.promote(path, path) == path
    with pytest.raises(ValueError, match='modify'):
        catalog.text(path, 'replacement')
    assert path.read_text() == 'original'


def test_shared_text_upload_rows_keep_independent_metadata_and_filenames(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    first = {'filename': 'segment.txt', 'textContent': 'Same text', 'metadata': {'title': 'Segment'}}
    second = {'filename': 'parent.txt', 'textContent': 'Same text', 'metadata': {'title': 'Parent'}}
    left = pipeline.build_file_upload_rows_from_payloads([first], tmp_path / 'left', artifact_catalog=catalog)[0]
    right = pipeline.build_file_upload_rows_from_payloads([second], tmp_path / 'right', artifact_catalog=catalog)[0]
    assert left['text_file'] == right['text_file']
    assert left['filename'] == 'segment.txt'
    assert right['filename'] == 'parent.txt'
    assert left['title'] != right['title']


def test_multipart_can_keep_upload_identity_when_disk_name_is_canonical(tmp_path, monkeypatch):
    path = tmp_path / 'canonical.txt'
    path.write_text('Exact text')
    requests = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return b'{}'

    def open_request(request, **_):
        requests.append(request.data)
        return Response()

    monkeypatch.setattr(pipeline, '_api_urlopen', open_request)
    pipeline.post_multipart_form('http://localhost/upload', {}, 'file', path, upload_filename='parent.txt')
    assert b'filename="parent.txt"' in requests[0]
    assert b'filename="canonical.txt"' not in requests[0]
    assert b'Exact text' in requests[0]


@pytest.mark.parametrize('filename', ['../bad.txt', 'bad".txt', 'bad\r\n.txt'])
def test_multipart_rejects_unsafe_filename_override(tmp_path, filename):
    with pytest.raises(ValueError):
        pipeline.post_multipart_form('http://localhost/upload', {}, 'file', tmp_path / 'canonical.txt',
                                     file_bytes=b'text', upload_filename=filename)


@pytest.mark.parametrize('canonical', [False, True])
def test_manual_kits_keep_checks_without_duplicate_private_payloads(tmp_path, canonical):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text) if canonical else None
    segments = [dict(text='Known phrase for citation verification.', source_title='Sample paper',
                     source_short_label='sample', source_author='Sample Author', source_sha256='a' * 64,
                     segment_id='sample-p2-s00001', segment_index=1, pdf_page=2)]
    payloads = pipeline.generate_api_payloads(segments, 'native_header')
    upload = pipeline.build_file_upload_rows_from_payloads(
        payloads, tmp_path / 'metadata-api', artifact_catalog=catalog)
    kits = [pipeline.write_native_metadata_test_kit(
        segments, tmp_path / name, artifact_catalog=catalog)
        for name in ('native-metadata-test-kit', 'native-metadata-compatibility-probe')]
    for kit in kits:
        rows = pipeline.load_upload_plan_rows(Path(kit['upload_plan']))
        assert kit['file_count'] == 1
        assert rows[0]['title'] == pipeline.native_segment_title(segments[0])
        assert rows[0]['docAuthor'] == 'Sample Author'
        assert Path(rows[0]['text_file']).read_text(encoding='utf8') == segments[0]['text']
        if canonical:
            assert rows[0]['text_file'] == upload[0]['text_file']
            assert not kit['files_dir']
            assert 'does not automatically apply' in Path(kit['checklist']).read_text()
        else:
            assert Path(kit['files_dir']).is_dir()
            assert rows[0]['text_file'] != upload[0]['text_file']
    if catalog:
        catalog.remove_empty_alias_directories()
        assert len(list(tmp_path.rglob('*.txt'))) == 1
        assert all((tmp_path / path).is_file() for path in catalog.roles.values())
