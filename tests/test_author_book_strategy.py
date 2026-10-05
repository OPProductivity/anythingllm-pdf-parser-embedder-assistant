from pathlib import Path

import pytest

from author_metadata.book import infer
from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile
from author_metadata.dispatcher import infer_author
from auto_anythingllm_pipeline import pdf_metadata
import fitz

pytestmark = pytest.mark.offline_deterministic


def context(text, title='Shared Histories', *, extra=(), path=None):
    samples = ({'page': 1, 'text': text}, *extra)
    return AuthorEvidenceContext(samples, title, Path(path) if path else None,
                                 DocumentProfile('book', ('fixture',), (1,)))


@pytest.mark.parametrize('role', ['Edited by', 'By', 'Written by'])
def test_reverse_explicit_credit_and_short_main_title(role):
    result = infer(context(f'University Press\nALICE SMITH\nAND\nBOB JONES\n{role}\nSHARED\nHISTORIES\nA Longer Subtitle'))
    assert result['author'] == 'ALICE SMITH, BOB JONES'


def test_two_word_title_does_not_match_different_book():
    assert not infer(context('Other Histories\nBy\nAlice Smith'))['author']


@pytest.mark.parametrize('role', ['Series Editors', 'Translated by', 'Foreword by', 'Introduction by', 'Afterword by'])
def test_ancillary_credit_is_not_work_author(role):
    assert not infer(context(f'Shared Histories\n{role}\nAlice Smith\nUniversity Press'))['author']


def test_three_stacked_work_authors_are_not_restarted_as_suffix_credits():
    report = infer(context('Shared Histories\nAlice Smith\nBob Jones\nCarol Reed\nUniversity Press'))
    assert report['author'] == 'Alice Smith, Bob Jones, Carol Reed'


def test_person_shaped_book_title_can_precede_a_different_author():
    report = infer(context('Alice Martin\nBruno Santos\nUniversity Press', title='Alice Martin'))
    assert report['author'] == 'Bruno Santos'


def test_production_pdf_sampler_reaches_later_book_title_page(tmp_path):
    path = tmp_path / 'shared-histories.pdf'
    with fitz.open() as pdf:
        pdf.set_metadata({'title': 'Shared Histories'})
        for number in range(1, 7):
            page = pdf.new_page()
            text = ('Praise for Shared Histories' if number == 1 else
                    'Shared Histories\nAlice Smith\nUniversity Press' if number == 5 else
                    'Front matter')
            page.insert_text((72, 72), text)
        pdf.save(path)
    metadata = pdf_metadata(path, include_author_samples=True)
    samples = metadata['_author_text_samples']
    assert 5 in [sample['page'] for sample in samples]
    author_context = AuthorEvidenceContext.from_samples(samples, path=path, title_hint='Shared Histories')
    assert author_context.profile.kind == 'book'
    assert 5 in author_context.profile_evidence()['sampled_pages']
    assert 5 not in author_context.profile_evidence()['classification_pages']
    assert infer_author(author_context)['author'] == 'Alice Smith'


def test_quoted_endorsement_is_not_work_author():
    assert not infer(context('Praise for Shared Histories\n"Shared Histories"\nAlice Smith\nUniversity Press'))['author']


def test_multi_author_copyright_with_compact_filename_suffix():
    title = 'The Politics of Memory_ A Study of Public Life -Alice Smith, Bob Jones'
    result = infer(context('The Politics of Memory\nA Study of Public Life\nAlice Smith and Bob Jones\nLondon \u2022 New York', title,
        extra=({'page': 2, 'text': 'Copyright \u00a9 2024 by Alice Smith and Bob Jones'},)))
    assert result['author'] == 'Alice Smith, Bob Jones'


def test_partial_copyright_is_not_complete_author_corroboration():
    result = infer(context('Shared Histories\nAlice Smith and Bob Jones',
        extra=({'page': 2, 'text': 'Copyright 2024 by Alice Smith'},)))
    assert not result['author']


def test_conflicting_work_credits_remain_unresolved():
    assert not infer(context('Shared Histories\nBy\nAlice Smith',
        extra=({'page': 2, 'text': 'Shared Histories\nBy\nBob Jones'},),
        path='Shared Histories -- Alice Smith -- 2024 -- University Press.pdf'))['author']


def test_catalog_uses_complete_field_as_lower_confidence_evidence():
    report = infer(context('Praise for Shared Histories',
        path='Shared Histories -- Alice Smith, Bob Jones -- 2024 -- University Press.pdf'))
    assert report['author'] == 'Alice Smith, Bob Jones'
    assert report['source'] == 'filename_author_fallback'
    assert report['page'] == 0


@pytest.mark.parametrize('filename', [
    'Shared Histories -- Alice Smith -- Unknown Label.pdf',
    'Shared Histories -- Alice Smith.pdf',
    'Unrelated Book -- Alice Smith -- 2024 -- University Press.pdf',
    'Shared Histories -- Translated by Alice Smith -- University Press.pdf',
    'Shared Histories and Different Work -- Alice Smith -- University Press.pdf',
])
def test_unqualified_catalog_is_not_used(filename):
    assert not infer(context('Praise for Shared Histories', path=filename))['author']


@pytest.mark.parametrize('role', ['Series Editors', 'Translated by', 'Foreword by'])
def test_catalog_cannot_override_printed_ancillary_role(role):
    assert not infer(context(f'Praise for Shared Histories\n{role}\nAlice Smith',
        path='Shared Histories -- Alice Smith -- University Press.pdf'))['author']
