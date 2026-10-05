"""Project MUSE review credits must not become reviewed-work authors."""

import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author

pytestmark = pytest.mark.offline_deterministic


TITLE = "Blood Oranges: Colonialism and Agriculture in the South Texas Borderlands by Timothy Paul Bowman (review)"
COVER = (
    "Blood Oranges: Colonialism and Agriculture in the South\n"
    "Texas Borderlands by Timothy Paul Bowman (review)\n"
    "{credit}\n"
    "Southwestern Historical Quarterly, Volume 121, Number 3, January\n"
    "2018, pp. 353-354 (Review)\n"
    "Published by Texas State Historical Association\n"
    "https://muse.jhu.edu/article/680115\n"
)
BODY = "Book Reviews\nBlood Oranges: Colonialism and Agriculture in the South Texas Borderlands. By Timothy Paul Bowman. (Press, 2016).\n"
ENDING = (
    "The review concludes.\nTexas A&M University\nSonia Hern\u00e1ndez\n"
    "From South Texas to the Nation. By John Weber. (Press, 2015).\n"
)


def inferred(credit="Sonia Hern\u00c3\u00a1ndez", *, cover=COVER, ending=ENDING, title=TITLE):
    samples = [
        {"page": 1, "text": cover.format(credit=credit)},
        {"page": 2, "text": BODY},
        {"page": 3, "text": ending},
    ]
    return infer_author(AuthorEvidenceContext.from_samples(samples, title_hint=title))


def test_review_credit_repairs_mojibake_only_with_independent_signature():
    report = inferred()
    assert report["author"] == "Sonia Hern\u00e1ndez"
    assert report["source"] == "text_review_byline"


@pytest.mark.parametrize("name", [
    "Alice Martin", "Jos\u00e9 Hern\u00e1ndez", "Jos\u00e9 Esteban Mu\u00f1oz",
    "Jean-Luc L\u00e9vesque", "Fran\u00e7ois L\u2019\u00c9cuyer", "\u0141ukasz \u017bulczyk",
    "Maria C\u00e2ndida", "\u00c2ngela Martin", "\u00c3ngela Silva",
])
def test_healthy_cover_name_needs_no_encoding_repair(name):
    assert inferred(credit=name)["author"] == name


@pytest.mark.parametrize("change", [
    "missing_signature", "second_review_before_signature", "different_signature",
    "lost_mojibake_byte", "missing_publisher", "article_not_review",
    "wrong_url", "wrong_title", "reviewed_book_author", "translator_credit",
])
def test_weak_or_wrong_role_evidence_abstains(change):
    cover, ending, credit, title = COVER, ENDING, "Sonia Hern\u00c3\u00a1ndez", TITLE
    if change == "missing_signature":
        ending = "No reviewer signature here."
    elif change == "second_review_before_signature":
        ending = ENDING.replace("Texas A&M University", "Another Reviewed Book. By John Weber. (Press, 2015).\nTexas A&M University")
    elif change == "different_signature":
        ending = ENDING.replace("Sonia Hern\u00e1ndez", "Timothy Paul Bowman")
    elif change == "lost_mojibake_byte":
        credit = "Sonia Hern\u00c3ndez"
    elif change == "missing_publisher":
        cover = COVER.replace("Published by ", "Distributed through ")
    elif change == "article_not_review":
        cover = COVER.replace("(Review)", "(Article)")
    elif change == "wrong_url":
        cover = COVER.replace("muse.jhu.edu", "unrelated.example")
    elif change == "wrong_title":
        title = "Unrelated Academic Work"
    elif change == "reviewed_book_author":
        credit = "Timothy Paul Bowman"
    elif change == "translator_credit":
        credit = "Translated by Sonia Hern\u00e1ndez"
    assert inferred(credit=credit, cover=cover, ending=ending, title=title)["author"] == ""
