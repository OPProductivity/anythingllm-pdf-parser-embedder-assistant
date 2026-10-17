"""Role and acquisition controls use invented people, not corpus identities."""

from pathlib import Path
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.contextual_roles import claims
from author_metadata.dispatcher import infer_author
from author_metadata.identity import resolve_author_from_metadata_and_inference

pytestmark = pytest.mark.offline_deterministic


def test_explicit_complete_filename_is_retained_when_chapter_byline_absent():
    context = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "CHAPTER 4\nA study of collective memory\n"
                + "This chapter considers social events in their historical setting. "
                * 20,
            }
        ],
        path=Path("Amelia Hart - Collective memory.pdf"),
    )
    report = infer_author(context)
    assert report["author"] == "Amelia Hart"
    assert report["source"] == "filename_leading_name"


def test_neutral_filename_never_supplies_person():
    context = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "CHAPTER 4\nA study of collective memory\n"
                + "This chapter considers social events in their historical setting. "
                * 20,
            }
        ],
        path=Path("document-001.pdf"),
    )
    assert not infer_author(context).get("author")


def test_registrant_not_regulator_is_typed_corporate_credit():
    sample = {
        "page": 1,
        "text": "SECURITIES AND EXCHANGE COMMISSION\nFORM 10-K\nOrchard Systems, Inc.\n(Exact name of registrant as specified in its charter)",
    }
    found = claims([sample], kind="corporate_publication", work_scope="whole_work")
    assert [x["author"] for x in found] == ["Orchard Systems, Inc."]
    assert found[0]["creator_type"] == "organization"
    assert (
        resolve_author_from_metadata_and_inference("", found[0])["author"]
        == "Orchard Systems, Inc."
    )


def test_research_organization_corroborates_act_not_contact():
    sample = {
        "page": 2,
        "text": "ACME RESEARCH\nFor this report, Acme Research surveyed more than 300 participants.\nContact: Amelia Hart\nAcme Research provides research reports.",
    }
    found = claims([sample], kind="report", work_scope="whole_work")
    assert [x["author"] for x in found] == ["Acme Research"]
    contact = {
        "page": 1,
        "text": "A research report\nContact: Acme Research\nSponsored by Acme Research",
    }
    assert not claims([contact], kind="report", work_scope="whole_work")


def test_personal_report_roster_not_erased_by_institutional_act():
    sample = {
        "page": 1,
        "text": "A research report\nBy Amelia Hart and Bernard Stone\nAcme Research conducted this study. Acme Research reports results.",
    }
    assert not claims([sample], kind="report", work_scope="whole_work")


def test_opinion_credit_does_not_invent_missing_given_name():
    sample = {
        "page": 5,
        "text": "Reporter of decisions: Clara Reed\nJustice Hart delivered the opinion of the Court.\nBernard Stone argued the cause for petitioner.",
    }
    found = claims([sample], kind="legal_opinion", work_scope="whole_work")
    assert [x["author"] for x in found] == ["Hart"]
    assert found[0]["creator_type"] == "person_surname"
    assert resolve_author_from_metadata_and_inference("", found[0])["author"] == "Hart"


def test_cjk_review_signature_not_reviewed_book_credit():
    sample = {
        "page": 1,
        "text": "書評・新刊紹介\n山田太郎 著\nISBN 978-0-123456-01-0\n本書について議論する。\n（東京大学図書館　田中明）\n注・参照文献",
    }
    found = claims([sample], kind="review", work_scope="review")
    assert [x["author"] for x in found] == ["田中明"]
    assert (
        resolve_author_from_metadata_and_inference("", found[0])["author"] == "田中明"
    )


def test_chapter_lead_and_wrapped_contributors_form_one_roster():
    from author_metadata.publishing_relations import claims as relations, select

    sample = {
        "page": 3,
        "text": (
            "2. Collective memory\nLead author: Amelia Hart1\n"
            "Contributing authors: Bernard Stone,\nClara Reed, David Lane\n\n"
            "This chapter investigates public memories and historical records."
        ),
    }
    found, _ = relations([sample], kind="book_chapter", work_scope="chapter")
    chosen, _ = select(
        {"author": "Clara Reed, David Lane", "source": "text_byline"},
        found,
        kind="book_chapter",
        work_scope="chapter",
    )
    assert chosen["author"] == "Amelia Hart, Bernard Stone, Clara Reed, David Lane"


def test_selected_essay_does_not_inherit_containing_collection_editors():
    from author_metadata.text_responsibility import claims as spans

    found = spans(
        [
            {
                "page": 1,
                "text": (
                    "Amelia Hart, ‘Collective Memory and Public Life,’ in Another Collection, "
                    "edited by Bernard Stone and Clara Reed"
                ),
            }
        ],
        kind="generic",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert any(c["work"] == "parent book" and c["role"] == "editor" for c in found)
    assert not any(c["priority"] > 0 for c in found)


def test_editor_roster_ends_before_separate_publisher_imprint():
    from author_metadata.text_responsibility import claims as spans

    found = spans(
        [
            {
                "page": 4,
                "text": (
                    "Collective Memory and Public Life\nEdited by Amelia Hart and Bernard Stone, Jr.\n"
                    "Westport Academic Press\nLONDON AND NEW YORK"
                ),
            }
        ],
        kind="book",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert [c["author"] for c in found if c["priority"] > 0] == [
        "Amelia Hart, Bernard Stone, Jr."
    ]


def test_page_local_chapter_projection_with_no_opening_page_does_not_crash():
    from author_metadata.book_chapter import infer as chapter
    from author_metadata.profile import DocumentProfile

    context = AuthorEvidenceContext.from_samples(
        [{"page": 8, "text": "Later chapter discussion."}],
        path=Path("Amelia Hart - Collective Memory and Public Life.pdf"),
        title_hint="Amelia Hart - Collective Memory and Public Life",
        profile=DocumentProfile("book_chapter", (), (8,), work_scope="chapter"),
    )
    assert not chapter(context)["author"]


def test_spaced_titlepage_conjunction_retains_both_editors():
    from author_metadata.text_responsibility import claims as spans

    found = spans(
        [
            {
                "page": 4,
                "text": "Collective Memory and Public Life\nEdited by\nAMELIA HART\n\nAND\nBERNARD STONE\n\nWestport University Press",
            }
        ],
        kind="book",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert [c["author"] for c in found if c["priority"] > 0] == [
        "AMELIA HART, BERNARD STONE"
    ]


def test_series_book_list_does_not_borrow_another_titles_editor():
    from author_metadata.text_responsibility import claims as spans

    text = (
        "Titles in the Series\nCollective Memory and Public Life by Amelia Hart and Bernard Stone.\n"
        "The Future of Farming edited by Clara Reed and David Lane\n"
        "History of Cities edited by Elena Rose and Farid Abbas"
    )
    found = spans(
        [{"page": 3, "text": text}],
        kind="book",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert not any(c["priority"] > 0 for c in found)
