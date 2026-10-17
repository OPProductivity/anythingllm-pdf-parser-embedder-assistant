"""Publishing responsibility versus page-region and parent-work ambiguity."""

import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile
from author_metadata.publishing_acts import reconcile
from author_metadata.structured_credit import _names

pytestmark = pytest.mark.offline_deterministic


def context(text, kind="generic", scope="single_work"):
    return AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": text}],
        title_hint="document-0123",
        profile=DocumentProfile(kind, (), (1,), work_scope=scope),
    )


def test_explicit_author_rights_overrule_weak_name_guess_on_neutral_title():
    result = reconcile(
        context(
            "ISBN 1234\nCopyright 2024\nThe right of Amina Rao to be identified as author of this work has been asserted."
        ),
        {
            "author": "Public Reception Theory",
            "source": "text_opening_title_block_byline",
        },
    )
    assert result["author"] == "Amina Rao"
    assert result["publishing_logic"]["claims"][0]["work"] == "this work"
    assert result["publishing_logic"]["genre_hypothesis"] == "book"


def test_equivalent_rights_wording_works_without_text_cover():
    result = reconcile(
        context(
            "Text copyright: Gabriel Santos 2024\nThe rights of Gabriel Santos as author have been asserted in accordance with the Copyright Act."
        ),
        {},
    )
    assert result["author"] == "Gabriel Santos"


def test_copyright_ownership_alone_is_not_authorship():
    assert not reconcile(
        context("Copyright Amina Rao 2024. All rights reserved."), {}
    ).get("author")


def test_parent_author_rights_do_not_replace_local_chapter_author():
    current = {"author": "Gabriel Santos", "source": "text_byline"}
    result = reconcile(
        context(
            "Copyright2024\nThe right of Amina Rao to be identified as author of this work has been asserted.",
            kind="book_chapter",
            scope="chapter",
        ),
        current,
    )
    assert result["author"] == "Gabriel Santos"


def test_conflicting_rights_claims_do_not_select_the_first():
    text = "Copyright 2024\nThe right of Amina Rao to be identified as author of this work.\nThe right of Gabriel Santos to be identified as author of this work."
    result = reconcile(context(text), {})
    assert not result.get("author")
    assert "conflicting" in result["publishing_logic"]["verdict"]


def test_question_box_and_numbered_section_are_not_a_byline():
    text = (
        "What does the audience see?\n4. Public Reception Theory\n"
        + "The chapter examines how readers interpret performances and build meaning. "
        * 15
    )
    result = reconcile(
        context(text),
        {
            "author": "Public Reception Theory",
            "source": "text_opening_title_block_byline",
        },
    )
    assert not result["author"]
    assert "numbered section" in result["publishing_logic"]["verdict"]


def test_affiliation_number_does_not_erase_an_author_credit():
    current = {"author": "Amina Rao", "source": "text_byline"}
    assert (
        reconcile(
            context(
                "1. Amina Rao\nExample University\nAbstract\n"
                + "The argument examines questions of public life. " * 20
            ),
            current,
        )["author"]
        == "Amina Rao"
    )


def test_missing_fifth_page_is_acquired_for_a_rights_statement(tmp_path):
    path = tmp_path / "document-0099.pdf"
    doc = fitz.open()
    for _ in range(5):
        doc.new_page()
    doc[4].insert_text(
        (50, 60),
        "Copyright 2024. All rights reserved.\nThe right of Amina Rao to be identified as author of this work.",
    )
    doc.save(path)
    doc.close()
    ctx = AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": ""}],
        path=path,
        title_hint="document-0099",
        profile=DocumentProfile("book", (), (1,)),
    )
    result = reconcile(ctx, {})
    assert result["author"] == "Amina Rao"
    assert 5 in result["publishing_logic"]["acquired_native_pages"]


def test_terminal_comma_and_generational_suffix_preserve_people():
    assert _names("Amina Rao,") == ["Amina Rao"]
    assert _names("Amina Rao, Jr., Gabriel Santos") == [
        "Amina Rao, Jr.",
        "Gabriel Santos",
    ]


def test_later_chapter_credit_with_two_running_heads_revises_parent_scope(tmp_path):
    path = tmp_path / "document-0100.pdf"
    doc = fitz.open()
    for _ in range(5):
        doc.new_page(width=864, height=648)
    doc[2].insert_text((500, 80), "CHAPTER5")
    doc[2].insert_text((500, 110), "Cities and Public Memory")
    doc[2].insert_text((500, 145), "Amina Rao")
    doc[2].insert_text((40, 90), "Previous bibliography on the opposite leaf")
    for i in (3, 4):
        doc[i].insert_text((40, 40), str(20 + i) + " Amina Rao")
    doc.save(path)
    doc.close()
    ctx = AuthorEvidenceContext.from_samples(
        [], path=path, profile=DocumentProfile("book", (), ())
    )
    current = {"author": "Gabriel Santos", "source": "text_edited_by"}
    result = reconcile(ctx, current)
    assert result["author"] == "Amina Rao"
    assert result["publishing_logic"]["genre_hypothesis"] == "book_chapter"
    doc = fitz.open(path)
    doc[4].add_redact_annot(fitz.Rect(0, 0, 864, 100))
    doc[4].apply_redactions()
    other = tmp_path / "document-0101.pdf"
    doc.save(other)
    doc.close()
    weak = AuthorEvidenceContext.from_samples([], path=other, profile=ctx.profile)
    assert reconcile(weak, current)["author"] == "Gabriel Santos"


def test_drawing_order_does_not_hide_a_question_box_above_a_heading(tmp_path):
    path = tmp_path / "document-0102.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 140), "4. Public Reception Theory")
    page.insert_textbox(
        fitz.Rect(50, 180, 520, 650),
        "The chapter studies how readers interpret performances. " * 40,
        fontsize=10,
    )
    # Insert the visually earlier box last, as real PDF drawing orders may do.
    page.insert_text((50, 60), "What does the audience understand?")
    native = page.get_text()
    doc.save(path)
    doc.close()
    ctx = AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": native}],
        path=path,
        profile=DocumentProfile("book_chapter", (), (1,), work_scope="chapter"),
    )
    result = reconcile(
        ctx,
        {
            "author": "Public Reception Theory",
            "source": "text_opening_title_block_byline",
        },
    )
    assert not result["author"]


def test_signed_editorial_priority_precedes_a_quoted_affiliated_name():
    from author_metadata.scholarly_article import infer

    ctx = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "Editorial\nA letter about our institution\nAn introduction to the issue.",
            },
            {
                "page": 3,
                "text": "Culture Review\nDear University President:\nThis is a quoted letter of support for our new department.",
            },
            {
                "page": 9,
                "text": "Amina Rao\nFounding Editor\nDepartment of History, Example University",
            },
        ],
        title_hint="document-0123",
        profile=DocumentProfile("scholarly_article", (), (1, 3, 9)),
    )
    result = infer(ctx)
    assert result["author"] == "Amina Rao"
    assert result["source"] == "text_editorial_end_signature"


def test_feature_deck_byline_is_distinct_from_related_paper_authors():
    from author_metadata.native_article import deck_credit

    def line(t, y, size):
        return {
            "text": t,
            "box": [50, y, 500, y + size],
            "baseline": y + size,
            "size": size,
        }

    lines = [
        line("INSIGHT", 30, 8),
        line("How experience shapes individuality", 80, 26),
        line(
            "The summary describes the main result and its wider implications for this field.",
            140,
            12,
        ),
        line("AMINA A RAO", 190, 10),
        line(
            "Related research article Gabriel Santos and Elena Chen reported another study. The commentary discusses its implications for future work.",
            230,
            9,
        ),
    ]
    assert deck_credit(lines, 600, 800)["author"] == "AMINA A RAO"
    lines.pop(2)
    assert not deck_credit(lines, 600, 800)["author"]


def test_middle_initial_is_not_an_indefinite_article():
    from author_metadata.names import looks_like_person_name

    assert looks_like_person_name("Bassem A Hassan")
    assert looks_like_person_name("AMINA A RAO")
    assert not looks_like_person_name("A Public Theory")


def test_repeated_masthead_does_not_displace_author_affiliation_abstract():
    from author_metadata.native_article import alternating_credit

    def line(t, y):
        return {"text": t, "box": [50, y, 550, y + 12], "baseline": y + 12, "size": 12}

    text = [
        "A dated browser header with many words about the site and the publication",
        "A Journal of Media and Popular Culture",
        "An extended masthead with another descriptive subtitle",
        "A second repeated masthead line with a long descriptive publication name",
        "How Digital Technologies Change Aesthetics and Style",
        "Amina Rao,",
        "Example University",
        "Abstract",
    ]
    assert (
        alternating_credit(
            [line(t, 30 + i * 20) for i, t in enumerate(text)], 600, 800
        )["author"]
        == "Amina Rao"
    )


def test_terminal_separator_does_not_make_a_partial_roster_complete():
    assert not _names("Amina Rao, Gabriel Santos,")
    assert _names("Amina Rao,") == ["Amina Rao"]
    assert _names("Amina Rao, Jr.,") == ["Amina Rao, Jr."]
