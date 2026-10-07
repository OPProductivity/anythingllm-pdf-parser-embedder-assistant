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


def test_publisher_cover_comma_separated_authors():
    text = (f'{TITLE}\nLourdes Gutiérrez Nájera, Korinta Maldonado\n'
            'Journal Quarterly, Volume 69, Number 4, pp.\n809-821 (Article)\n'
            'Published by Johns Hopkins University Press\nDOI: 10.1353/aq.2017.0067\n'
            'https://muse.jhu.edu/article/680486')
    assert parse(text) == 'Lourdes Gutiérrez Nájera, Korinta Maldonado'


def test_wrapped_title_and_affiliated_credit():
    title = '“New White Ethnics” or “New Latinos”? Hispanic/Latino Pan-ethnicity and Ancestry Reporting among South American Immigrants to the United States'
    text = ('Original Research Article\n“New White Ethnics”\nor “New Latinos”?\n'
            'Hispanic/Latino\nPan-ethnicity and\nAncestry Reporting\n'
            'among South\nAmerican Immigrants\nto the United States\n'
            'Rebecca A. Schut\nPopulation Studies Center, University of Pennsylvania\nAbstract')
    assert parse(text, title=title) == 'Rebecca A. Schut'


def test_machine_pdf_title_with_affiliated_abstract_byline():
    text = ('Progress report\nGeographies of race and\nethnicity II: Environmental\n'
            'racism, racial capitalism and\nstate-sanctioned violence\n'
            'Laura Pulido\nUniversity of Southern California, USA\nAbstract')
    assert parse(text, title='PHG646495 524..533') == 'Laura Pulido'


def test_author_before_selected_title_and_abstract():
    text = ('Special section: On ethnic names\nLinda Martín Alcoff\n'
            'Latino vs. Hispanic\nThe politics of ethnic names\nAbstract\n'
            'The article begins here.\nDOI: 10.1000/example')
    assert parse(text, title='Latino vs. Hispanic') == 'Linda Martín Alcoff'


def test_displaced_title_block_before_abstract():
    text = ('Sociology of Race and Ethnicity\nArticle body printed before title block.\n'
            'Corresponding Author:\nEvelyn Nakano Glenn, Example University\n'
            'Settler Colonialism as\nStructure: A Framework for\n'
            'Comparative Studies of U.S.\nRace and Gender Formation\n'
            'Evelyn Nakano Glenn1\nAbstract\nUnderstanding the framework.\n'
            'DOI: 10.1000/example')
    assert parse(text, title='Glenn Settler Colonialism as Structure (2015)') == 'Evelyn Nakano Glenn'


def test_credentialled_multi_author_credit_before_abstract():
    text = ('Different Views\nLatino Terminology: Conceptual Bases for Standardized Terminology\n'
            'DAVID E. HAYES-BAUTISTA, PHD, AND JORGE CHAPA\n'
            'Abstract: Conceptually, this article examines naming.\n'
            'DOI: 10.1000/example')
    assert parse(text, title='hayes-bautista-chapa-2011-latino-terminology-conceptual-bases-for-standardized-terminology') == 'David E. Hayes-Bautista, Jorge Chapa'


def test_wrapped_title_tail_does_not_become_abstract_author():
    text = ('“New White Ethnics” or “New Latinos”? Hispanic/Latino Pan-ethnicity and\n'
            'Ancestry Reporting\nAbstract\nArticle prose.\nDOI: 10.1000/example')
    assert parse(text, title='“New White Ethnics” or “New Latinos”? Hispanic/Latino Pan-ethnicity and Ancestry Reporting') == ''


def test_affiliation_suffix_requires_corresponding_name_and_marker():
    text = ('Journal of Society\naColumbia University\nCorresponding Author:\n'
            'Maria Abascal, Columbia University\n'
            f'{TITLE}\nMaria Abascala\nAbstract\nArticle prose.\nDOI: 10.1000/example')
    assert parse(text) == 'Maria Abascal'
    assert parse(text.replace('aColumbia University\n', '')) == 'Maria Abascala'
