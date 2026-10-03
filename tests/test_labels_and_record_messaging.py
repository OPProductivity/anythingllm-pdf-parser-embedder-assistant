import inspect
import unicodedata

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize(('author', 'expected'), [
    ('Jos\u00e9 Esteban Mu\u00f1oz', 'Munoz'),
    ('Sonia Hern\u00e1ndez', 'Hernandez'),
    ('SYLVANNA M. FALC\u00d3N', 'FALCON'),
    ('Catherine S. Ram\u00edrez', 'Ramirez'),
    ('Ren\u00e9 Garc\u00eda-M\u00e1rquez', 'Garcia-Marquez'),
    ('Fran\u00e7ois L\u00e9vesque', 'Levesque'),
    ('Bj\u00f6rn M\u00fcller', 'Muller'),
    ("Jane O'Neill", "O'Neill"),
    ('George Packer', 'Packer'),
    ('Jane Doe, John Roe', 'Roe'),
])
@pytest.mark.parametrize('normalization', ['NFC', 'NFD'])
def test_short_labels_fold_accents_without_changing_author(author, expected, normalization):
    original = unicodedata.normalize(normalization, author)
    assert pipeline.default_short_label('Collected Essays', original) == expected
    assert original == unicodedata.normalize(normalization, author)
    assert pipeline.compact_label_token(expected) == expected.casefold().replace("'", '-')


@pytest.mark.parametrize(('title', 'expected'), [
    ('\u00c9tudes culturelles', 'Etudes'),
    ('The \u00c9migr\u00e9 Experience', 'Emigre'),
    ('Collected Essays', 'Collected'),
    ('123', 'PDF'),
    ('', 'PDF'),
])
def test_short_label_title_fallback(title, expected):
    assert pipeline.default_short_label(title, '') == expected


def test_native_identity_uses_complete_accented_surname_label():
    author = 'Jos\u00e9 Esteban Mu\u00f1oz'
    row = {'source_short_label': pipeline.default_short_label('Feeling Brown', author),
           'pdf_page': 1, 'pdf_page_end': 14, 'segment_index': 1}
    assert pipeline.native_identity_stem(row).startswith('munoz-p1-14-')
    assert author == 'Jos\u00e9 Esteban Mu\u00f1oz'


def test_shared_record_messages_do_not_assume_page_parent_segmentation():
    # Guard shared coordinator/GUI producers, not page-parent-only fast paths.
    for function in (pipeline.update_workspace_embeddings_desktop_queue,
                     pipeline.maybe_upload_segment_files_source_transactions,
                     app.upload_prepared_automatic_batch):
        source = inspect.getsource(function)
        assert 'record_label=f"page-parent record(s)' not in source
        assert 'writing its page-parent record to this workspace;' not in source
    assert 'selected upload record(s)' in inspect.getsource(app.upload_prepared_automatic_batch)


def test_shared_count_semantics_are_representation_neutral():
    report = app.explicit_upload_count_schema({'status': 'complete'})
    assert 'page-parent' not in report['count_semantics']['selected_records']
