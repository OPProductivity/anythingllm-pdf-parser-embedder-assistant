from datetime import datetime
from unittest.mock import patch

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
    existing = tmp_path / 'r-20260910-120000-2'
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
    with patch.object(app, 'datetime') as clock:
        clock.now.return_value = datetime(2026,9,10,12)
        target = app.promote_flat_no_logs_batch_output(tmp_path, staging,
            [tmp_path / f'Long source title {i}.pdf' for i in range(count)], summaries)
    assert target.name == 'r-20260910-120000-3'
    assert len(list(target.glob('*.txt'))) == count
    assert receipt.exists() and previous.read_text() == 'previous run'
    assert [p.read_text(encoding='utf-8') for p in sorted(target.glob('*.txt'))] == [f'complete text {i}' for i in range(count)]
    assert all(s['flat_no_logs_output_directory'] == str(target) for s in summaries)
