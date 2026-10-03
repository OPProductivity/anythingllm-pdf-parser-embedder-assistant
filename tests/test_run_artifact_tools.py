import json
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
from canonical_artifacts import CanonicalArtifacts
from run_artifact_tools import evidence_path, materialize_optional_artifacts, read_rows


pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def retained_run(tmp_path):
    selected = tmp_path
    segments = [dict(text='A known phrase continued across a page.', source_title='Sample paper',
                     source_short_label='sample', source_author='Sample Author', source_sha256='a' * 64,
                     source_id='sample', source_file='sample.pdf', segment_id='sample-p2-s00001',
                     segment_index=1, pdf_page=2, char_start_page=0, char_end_page=43, backend='pymupdf')]
    pipeline.append_jsonl(selected / 'segment-manifest.jsonl', segments)
    pipeline.append_jsonl(selected / 'page-transition-manifest.jsonl', [
        dict(boundary_id='sample-p2-p3', continuation_detected=True, reconstructed_text='Exact joined text.'),
        dict(boundary_id='sample-p3-p4', continuation_detected=False, reconstructed_text=''),
    ])
    review = dict(items=[dict(kind='segment', pdf_page=2, text='Preserved reference.', reason='references')])
    pipeline.write_json(selected / 'retrieval-lane-review.json', review)
    pipeline.write_json(tmp_path / 'run-summary.json', dict(workspace_slug='retained-workspace'))
    pipeline.atomic_write_text(tmp_path / 'canonical.txt', segments[0]['text'])
    pipeline.write_json(tmp_path / 'artifact-locations.json', {
        'roles': {'canonical.txt': 'canonical.txt', 'segment-manifest.jsonl': 'segment-manifest.jsonl'}})
    return tmp_path, segments


def test_diagnostics_are_rendered_only_on_request_and_repeated_safely(retained_run):
    root, _ = retained_run
    summary = (root / 'run-summary.json').read_bytes()
    assert not (root / 'on-demand').exists()
    paths = materialize_optional_artifacts(root)
    assert len(paths) == 2
    assert paths[0].read_text() == 'Exact joined text.'
    assert 'Preserved reference.' in paths[1].read_text()
    assert materialize_optional_artifacts(root) == paths
    assert (root / 'run-summary.json').read_bytes() == summary


def test_diagnostics_validate_all_transition_names_before_writing(retained_run):
    root, _ = retained_run
    pipeline.append_jsonl(root / 'page-transition-manifest.jsonl', [
        dict(boundary_id='../outside', continuation_detected=True, reconstructed_text='unsafe')])
    with pytest.raises(ValueError, match='Unsafe'):
        materialize_optional_artifacts(root)
    assert not (root / 'on-demand').exists()


def test_deferred_manual_plan_can_keep_text_in_manifest(retained_run):
    root, segments = retained_run
    # No registered payload: preparation must not materialize a kit TXT.
    catalog = CanonicalArtifacts(root, pipeline.atomic_write_text)
    kit = pipeline.write_native_metadata_test_kit(
        segments, root / 'normal-kit', artifact_catalog=catalog, defer_payloads=True,
        text_manifest=root / 'segment-manifest.jsonl')
    plan = Path(kit['upload_plan'])
    before = plan.read_bytes()
    row = pipeline.load_upload_plan_rows(plan)[0]
    assert row['text_file'] == ''
    assert read_rows(row['text_manifest'])[0]['segment_id'] == row['segment_id']
    assert not list(plan.parent.rglob('*.txt'))
    paths = materialize_optional_artifacts(root, 'manual-kits')
    assert root / 'canonical.txt' in paths
    assert plan.read_bytes() == before
    assert len(list(root.rglob('*.txt'))) == 1


def test_unindexed_flat_exports_are_not_manual_kit_dependencies(retained_run):
    root, segments = retained_run
    (root / 'artifact-locations.json').write_text('{}')
    paths = materialize_optional_artifacts(root, 'manual-kits')
    assert root / 'canonical.txt' not in paths
    assert all(path.is_relative_to(root / 'on-demand') for path in paths)
    assert (root / 'canonical.txt').read_text() == segments[0]['text']


def test_upload_alternatives_regenerate_original_payload_metadata_and_text(retained_run):
    root, segments = retained_run
    summary = (root / 'run-summary.json').read_bytes()
    materialize_optional_artifacts(root, 'upload-alternatives')
    for representation in ('segments', 'page-parents'):
        for mode in ('strict', 'native_header'):
            rows = pipeline.load_upload_plan_rows(
                root / 'on-demand/upload-alternatives' / f'upload-plan-{representation}-{mode}.csv')
            expected = (pipeline.generate_api_payloads(segments, mode) if representation == 'segments' else
                        pipeline.generate_page_parent_payloads(pipeline.build_page_parent_rows(segments), mode))
            assert len(rows) == len(expected)
            assert rows[0]['filename'] == expected[0]['filename']
            assert rows[0]['title'] == expected[0]['metadata']['title']
            assert Path(rows[0]['text_file']).read_text() == expected[0]['textContent']
    assert (root / 'run-summary.json').read_bytes() == summary


def test_relocated_canonical_roles_override_stale_paths(retained_run):
    root, _ = retained_run
    source = root / 'segment-manifest.jsonl'
    moved = root / 'renamed-manifest.jsonl'
    source.replace(moved)
    (root / 'artifact-locations.json').write_text(json.dumps({
        'roles': {'segment-manifest.jsonl': 'renamed-manifest.jsonl'}}))
    assert evidence_path(root, 'segment-manifest.jsonl') == moved
    materialize_optional_artifacts(root, 'manual-kits')


def test_role_lookup_accepts_windows_index_separators(retained_run):
    root, _ = retained_run
    (root / 'artifact-locations.json').write_text(json.dumps({
        'roles': {'metadata-api\\alternative.jsonl': 'segment-manifest.jsonl'}}))
    assert evidence_path(root, 'metadata-api/alternative.jsonl') == root / 'segment-manifest.jsonl'


@pytest.mark.parametrize('relative', ['../outside.jsonl', 'C:/outside.jsonl'])
def test_unsafe_evidence_paths_fail_before_output(retained_run, relative):
    root, _ = retained_run
    (root / 'artifact-locations.json').write_text(json.dumps({
        'roles': {'segment-manifest.jsonl': relative}}))
    with pytest.raises(ValueError, match='Unsafe'):
        materialize_optional_artifacts(root, 'manual-kits')
    assert not (root / 'on-demand').exists()


@pytest.mark.parametrize('oversized', [0, 1, 3])
def test_upload_representation_resolution_preserves_oversized_parent_guard(oversized):
    rows = [{'representation': 'page_parents', 'units_exceeding_effective_limit': oversized}]
    actual, adjustment = pipeline.resolve_native_upload_representation('page_parents', rows)
    assert actual == ('segments' if oversized else 'page_parents')
    assert bool(adjustment) == bool(oversized)
    if oversized:
        assert adjustment['oversized_page_parent_units'] == oversized
    assert pipeline.resolve_native_upload_representation('segments', rows) == ('segments', {})
