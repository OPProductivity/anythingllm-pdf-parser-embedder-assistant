from datetime import datetime
import hashlib
from unittest.mock import patch
import uuid

import pytest

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('count', [1, 3])
def test_timestamp_promotion_preserves_staging_and_existing_exports(tmp_path, count):
    import rag_pdf_gradio_app as app

    # Simulate a fast completion in the same second as staging creation and
    # another completed run. Existing directories must never be overwritten.
    staging = tmp_path / 'r-20260910-120000'
    staging.mkdir()
    receipt = staging / 'run-progress.json'
    receipt.write_text('retained until normal staging cleanup')
    fixed_id = uuid.UUID('12345678-1234-1234-1234-123456789abc')
    run_hash = hashlib.sha256(fixed_id.bytes).hexdigest()[:10]
    existing = tmp_path / f'r-20260910-120000-{run_hash}'
    existing.mkdir()
    previous = existing / 'previous.txt'
    previous.write_text('previous run')
    summaries = []
    for i in range(count):
        folder = staging / str(i)
        folder.mkdir()
        export = folder / f'source-{i}-complete-pdf-parsed.txt'
        export.write_text(f'complete text {i}', encoding='utf-8')
        summaries.append({'upload_file': str(export)})
    with patch.object(app, 'datetime') as clock, patch.object(app.uuid, 'uuid4', return_value=fixed_id):
        clock.now.return_value = datetime(2026,9,10,12)
        target = app.promote_flat_no_logs_batch_output(tmp_path, staging,
            [tmp_path / f'Long source title {i}.pdf' for i in range(count)], summaries)
    assert target.name == f'r-20260910-120000-{run_hash}-2'
    assert len(list(target.glob('*.txt'))) == count
    assert receipt.exists() and previous.read_text() == 'previous run'
    assert [p.read_text(encoding='utf-8') for p in sorted(target.glob('*.txt'))] == [f'complete text {i}' for i in range(count)]
    assert all(s['flat_no_logs_output_directory'] == str(target) for s in summaries)


def test_run_discovery_accepts_hashed_and_historical_names(tmp_path):
    import rag_pdf_gradio_app as app

    names = ['r-20260911-141856', 'r-20260911-141856-336ee95e6e', 'app-run-20260901-100000']
    for name in names:
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'run-progress.json').write_text('{}')
    assert {p.parent.name for p in app.automatic_run_artifact_paths(tmp_path, 'run-progress.json')} == set(names)


def test_same_second_runs_have_distinct_hashes(tmp_path):
    import rag_pdf_gradio_app as app

    with patch.object(app, 'datetime') as clock:
        clock.now.return_value = datetime(2026, 9, 11, 14, 18, 56)
        first = app.create_fresh_automatic_run_root(tmp_path)
        second = app.create_fresh_automatic_run_root(tmp_path)
    assert first.name.startswith('r-20260911-141856-')
    assert second.name.startswith('r-20260911-141856-')
    assert len(first.name.rsplit('-', 1)[1]) == 10
    assert first.name != second.name
