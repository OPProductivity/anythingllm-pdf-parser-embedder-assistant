from author_metadata.context import AuthorEvidenceContext
from author_metadata.scholarly_article import infer
import pytest

pytestmark = pytest.mark.offline_deterministic

TITLE = 'The Cultural Politics of Shared Memory'


def parse(text, title=TITLE, page=1):
    context = AuthorEvidenceContext.from_samples([dict(page=page, text=text)], title_hint=title)
    return infer(context)['author']


@pytest.mark.parametrize('credit', [
    'Alice Smith1', 'Alice Smith*', 'Alice Smith\u00b9',
    'Alice Smith (Independent researcher)', 'Alice Smith, Example University',
    'Alice Smith\nExample University',
])
def test_abstract_credit_variants(credit):
    assert parse(f'DOI: 10.1000/example\n{TITLE}\n{credit}\nAbstract\nWe examine cultural memory.') == 'Alice Smith'


def test_publication_dates_between_byline_and_abstract():
    assert parse(f'{TITLE}\nAlice Smith1\nReceived: 1 March 2023 / Accepted: 2 April 2023\nPublished online:\n3 May 2023\nCopyright 2023\nAbstract') == 'Alice Smith'


def test_doi_between_title_and_parenthetical_credit():
    assert parse(f'{TITLE}\nDOI: 10.1000/example\nAlice Smith (Independent researcher)\nAbstract') == 'Alice Smith'


def test_cover_citation_multi_author():
    assert parse(f'{TITLE}\nAlice Smith & Bob Jones\nTo cite this article: Alice Smith & Bob Jones (2023) {TITLE}\nDOI: 10.1000/example') == 'Alice Smith, Bob Jones'


def test_unlabelled_abstract_after_affiliation():
    assert parse(f'DOI: 10.1000/example\n{TITLE}\nAlice Smith\nExample University\nThis article considers the changing nature of cultural memory in contemporary society and its implications for public education.') == 'Alice Smith'


def test_jstor_short_title_cover():
    assert parse('Hillbilly Elegy\nAuthor(s): Alice Smith\nSource: Journal of Society\nPublished by: Example Press\nStable URL: https://www.jstor.org/stable/123\nDOI: 10.1000/example', title='Hillbilly Elegy') == 'Alice Smith'


def test_stacked_author_block():
    assert parse(f'{TITLE}\nAlice Smith\nBob Jones\nAbstract') == 'Alice Smith, Bob Jones'


@pytest.mark.parametrize('joiner', ['and', '&'])
def test_wrapped_conjoined_author_credit(joiner):
    text = f'{TITLE}\nAlice Smith1\n{joiner} Bob Jones1\nAbstract\nResearch follows.'
    assert parse(text) == 'Alice Smith, Bob Jones'


def test_conjoined_role_line_is_not_a_second_author():
    text = f'{TITLE}\nAlice Smith\nand Contributors\nBob Jones\nAbstract'
    assert parse(text) == ''


@pytest.mark.parametrize('role', [
    'Interview with', 'Interviewees:', 'Participants:', 'Contributors:',
    'Series Editors', 'Volume Editors', 'Editorial Board', 'Acknowledgments',
    'References', 'Bibliography',
])
def test_non_author_roles_rejected(role):
    assert parse(f'{TITLE}\n{role}\nAlice Smith\nExample University\nAbstract') == ''


@pytest.mark.parametrize('text', [
    f'{TITLE}\nAalborg, Denmark\nAbstract',
    f'{TITLE}\nAbstract\nAlice Smith\nExample University',
    f'{TITLE}\nIntroduction\nAlice Smith\nExample University',
    f'{TITLE}\nAlice Smith is a professor at Example University\nAbstract',
    f'{TITLE}\nAlice Smith\nTo cite this article: Bob Jones (2023) {TITLE}\nDOI: 10.1000/example',
    f'{TITLE}\nAuthor(s): Alice Smith\nSource: Journal of Society\nDOI: 10.1000/example',
    'Another Work About Society\nAuthor(s): Alice Smith\nSource: Journal of Society\nStable URL: https://www.jstor.org/stable/123\nDOI: 10.1000/example',
    f'{TITLE}\nAlice Smith\n2018\nA story without an abstract or affiliation.\nDOI: 10.1000/example',
])
def test_non_credit_layouts_rejected(text):
    assert parse(text) == ''


def test_wrong_title_rejected():
    assert parse('Another Work About Society\nAlice Smith\nAbstract') == ''


def test_page_four_is_not_used():
    assert parse(f'{TITLE}\nAlice Smith\nAbstract', page=4) == ''


def test_non_scholarly_context_is_not_used():
    assert parse(f'{TITLE}\nAlice Smith\nExample University') == ''


def test_translated_article_credits_original_author():
    assert parse(f'Journal of Society\nVol. 2, Issue No. 1, 2023\n{TITLE}1\nAlice Smith\nTranslated by\nBob Jones\nExample University\nDOI: 10.1000/example') == 'Alice Smith'


@pytest.mark.parametrize('body', [
    f'{TITLE}\nTranslated by\nBob Jones\nExample University',
    f'{TITLE}\nInterview with\nAlice Smith\nTranslated by\nBob Jones',
    f'{TITLE}\nSeries Editors\nAlice Smith\nTranslated by\nBob Jones',
    f'{TITLE}\nTranslated by\nBob Jones\nAbstract',
    'Another Title About Society\nAlice Smith\nTranslated by\nBob Jones',
])
def test_translator_or_subject_is_not_author(body):
    assert parse(f'Journal of Society\nVol. 2, Issue No. 1, 2023\n{body}\nDOI: 10.1000/example') == ''
