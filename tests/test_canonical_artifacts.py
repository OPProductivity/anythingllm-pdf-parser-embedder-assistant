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


@pytest.mark.parametrize('canonical_body', [True, False])
@pytest.mark.parametrize('publisher', ['cli', 'gui'])
def test_private_retention_reuses_bytes_without_changing_public_exports(tmp_path, monkeypatch,
                                                                        canonical_body, publisher):
    import json
    import shutil
    import portable_paths
    import rag_pdf_gradio_app as app

    monkeypatch.setenv(portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(tmp_path / 'home'))
    root = tmp_path / 'home/run-state/automatic-runs/r-test/document'
    catalog = CanonicalArtifacts(root, pipeline.atomic_write_text)
    body = 'Academic paragraph.\n' * 40
    payload = catalog.text(root / 'metadata-api/parent.txt', body if canonical_body else 'Different body')
    prepared = catalog.text(root / 'Paper-pdf-parsed.txt', 'Complete parsed transcript')
    pipeline.write_json(root / 'artifact-locations.json', {'schema_version': 1, 'roles': catalog.roles})
    summary = {'pdf': 'Paper.pdf', 'output_root': str(root), 'readiness_status': 'ready',
               'api_upload_status': 'skipped_prepare_only', 'post_upload_verification_status': 'not_checked_no_upload',
               'anythingllm_runtime_validation_status': 'not_checked_no_upload', 'segment_mode': 'page'}
    segments = [{'pdf_page': 2, 'text': body}, {'pdf_page': 3, 'text': body}]
    retained = pipeline.retain_successful_run_leanly(root, summary, {}, prepared, segments=segments,
                                                   preserve_preexisting_children=False)
    assert retained['applied']
    summary['lean_retention'] = retained
    actuals = [Path(path) for path in retained['retained_segment_paths']]
    assert actuals[0] == actuals[1]
    if canonical_body:
        assert actuals == [payload, payload]
        assert not list(root.glob('*-p002-s01.txt'))
    sources = pipeline.local_segment_export_sources(prepared, retained)
    assert [suffix for _path, suffix in sources] == ['-p002-s01.txt', '-p003-s01.txt']
    moved = tmp_path / 'relocated'
    shutil.copytree(root, moved)
    assert all(path.is_relative_to(moved) for path, _ in pipeline.local_segment_export_sources(
        moved / prepared.name, retained))
    if publisher == 'cli':
        public, _ = pipeline.publish_cli_text_outputs(tmp_path / 'public', root.parent, [summary])
    else:
        public = app.promote_flat_no_logs_batch_output(tmp_path / 'public', root.parent, ['Paper.pdf'], [summary])
    assert {path.name: path.read_text(encoding='utf8') for path in public.iterdir()} == {
        'Paper-complete-pdf-parsed.txt': 'Complete parsed transcript',
        'Paper-p002-s01.txt': body, 'Paper-p003-s01.txt': body}
    index = json.loads((root / 'artifact-locations.json').read_text())
    assert all((root / relative).is_file() for relative in index['roles'].values())
    assert payload.is_file()


@pytest.mark.parametrize('path', ['missing.txt', '../outside.txt'])
def test_missing_or_escaping_canonical_export_fails_instead_of_silently_omitting_it(tmp_path, path):
    with pytest.raises(ValueError, match='canonical local segment'):
        pipeline.local_segment_export_sources(tmp_path / 'Paper.txt', {
            'retained_segment_exports': [{'filename': 'Paper-p001-s01.txt', 'path': path}]})


def test_staged_byte_reuse_does_not_normalize_line_endings(tmp_path):
    catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    path = tmp_path / 'body.txt'
    path.write_bytes(b'Line\r\r\nNext\n')
    catalog.register(path)
    assert catalog.existing_bytes(b'Line\r\r\nNext\n') == path
    assert catalog.existing_bytes(b'Line\n\nNext\n') is None


def test_export_mapping_preserves_page_body_pairing_when_canonical_names_sort_differently(tmp_path):
    left = tmp_path / 'z.txt'
    right = tmp_path / 'a.txt'
    left.write_text('Page two body')
    right.write_text('Page ten body')
    exports = pipeline.local_segment_export_sources(tmp_path / 'Paper.txt', {
        'retained_segment_exports': [{'filename': 'Paper-p010-s01.txt', 'path': 'a.txt'},
                                     {'filename': 'Paper-p002-s01.txt', 'path': 'z.txt'}]})
    assert [(suffix, path.read_text()) for path, suffix in exports] == [
        ('-p002-s01.txt', 'Page two body'), ('-p010-s01.txt', 'Page ten body')]
