from types import SimpleNamespace

from author_metadata.report import infer_report_author
from author_metadata.review import infer_review_author
from author_metadata.web_article import infer_web_article_author
import pytest

pytestmark = pytest.mark.offline_deterministic


def context(text, title='A Study of Public Cultural Memory', extra=''):
    return SimpleNamespace(title_hint=title, opening_pages=[
        {'page': 1, 'text': text}, {'page': 3, 'text': extra}])


def test_report_prepared_by_precedes_front_recipient():
    actual = infer_report_author(context('Technical Report\nJohn Jones\nPrepared for John Jones\nPrepared by\nAlice Smith'))
    assert actual['author'] == 'Alice Smith'
    assert actual['source'] == 'text_report_prepared_by'


def test_report_recipient_is_not_author():
    assert not infer_report_author(context('Technical Report\nPrepared for\nJohn Jones'))['author']


def test_review_explicit_credit_precedes_reviewed_author():
    actual = infer_review_author(context('Book Review\nBook Reviewed: Example by John Jones\nReviewed by Alice Smith'))
    assert actual['author'] == 'Alice Smith'


def test_review_affiliated_layout():
    text = 'Book Review\nA Study of Public Cultural Memory\nAlice Smith, Example University\nBob Jones, Example University\nBook Reviewed: Example by Jane Doe'
    assert infer_review_author(context(text))['author'] == 'Alice Smith, Bob Jones'


@pytest.mark.parametrize('role', ['Interview subjects', 'Conversation participants', 'Advisory-board members', 'Quoted experts'])
def test_review_role_block_does_not_become_authors(role):
    text = f'Book Review\nA Study of Public Cultural Memory\n{role}\nAlice Smith, Example University\nBob Jones, Example University\nBook Reviewed: Example by Jane Doe'
    assert not infer_review_author(context(text))['author']


@pytest.mark.parametrize('strategy,text', [
    (infer_review_author, 'Book Review\nReviewed by Alice Smith'),
    (infer_report_author, 'Technical Report\nPrepared by Alice Smith'),
    (infer_web_article_author, 'A Study of Public Cultural Memory\nBy Alice Smith'),
])
def test_later_translator_role_is_not_bypassed(strategy, text):
    assert not strategy(context(text, extra='Translated by Alice Smith'))['author']


def test_web_date_requires_valid_date_and_matched_title():
    text = 'A Study of Public Cultural Memory\nAlice Smith | February 31, 2025'
    assert not infer_web_article_author(context(text))['author']
    text = text.replace('February 31', 'February 21')
    assert infer_web_article_author(context(text))['author'] == 'Alice Smith'
    assert not infer_web_article_author(context(text, title='An Unrelated Article Title'))['author']


def test_web_explicit_byline_precedes_dated_name():
    text = 'A Study of Public Cultural Memory\nJohn Jones | February 21, 2025\nBy Alice Smith'
    assert infer_web_article_author(context(text))['author'] == 'Alice Smith'
