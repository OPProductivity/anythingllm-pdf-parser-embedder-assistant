"""Portable bylines, contributor boundaries and bounded OCR decisions."""

from pathlib import Path
from unittest.mock import patch

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile
from author_metadata.structured_credit import (
    infer,
    credential_roster,
    positioned_opening,
    publisher_titlepage,
    document_record,
    remove_translator_role,
    work_rights_credit,
    biographic_titlepage,
    continued_byline,
    whole_book_editors,
)
from author_metadata.bounded_ocr import _credit, infer as ocr_infer

pytestmark = pytest.mark.offline_deterministic


def context(
    text,
    title="Urban Change and Public Memory",
    kind="scholarly_article",
    path="neutral.pdf",
):
    return AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": text}],
        title_hint=title,
        path=Path(path),
        profile=DocumentProfile(kind, (), (1,)),
    )


@pytest.mark.parametrize(
    "credit,expected",
    [
        ("Amina Rao and Gabriel Santos", "Amina Rao, Gabriel Santos"),
        ("NADIA OKAFOR", "NADIA OKAFOR"),
        ("Elena Chen / University of Helsinki", "Elena Chen"),
    ],
)
def test_title_credit_without_abstract_or_filename_person(credit, expected):
    sample = context(
        "Urban Change and Public Memory\n" + credit + "\n1 The history of cities\n"
        "This article studies the changing public spaces of several cities over a long period of time."
    )
    assert infer(sample)["author"] == expected


def test_role_panel_is_not_a_work_byline():
    sample = context(
        "Urban Change and Public Memory\nReviewed by\nElena Chen\n"
        "This article studies the changing public spaces of several cities over a long period of time."
    )
    assert not infer(sample)["author"]


def test_roster_includes_bare_name_but_not_biography_sentence():
    sample = context(
        "Urban Change and Public Memory\nDr. Amina Rao, Example University\n"
        "Her research considers urban history.\nGabriel Santos, Example University\n"
        "Gabriel Santos, is a researcher at Example University who studies old cities."
    )
    assert credential_roster(sample)["author"] == "Amina Rao, Gabriel Santos"


def test_one_affiliated_name_does_not_define_a_complete_roster():
    assert not credential_roster(
        context("Urban Change and Public Memory\nAmina Rao, Example University")
    )["author"]


def test_positioned_header_uses_visible_hierarchy_for_internal_metadata(tmp_path):
    source = tmp_path / "document-123.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (80, 100), "Urban Change and the Politics of Public Memory", fontsize=17
    )
    page.insert_text((80, 135), "Amina Rao and Gabriel Santos", fontsize=11)
    page.insert_text((80, 175), "1 Opening discussion", fontsize=12)
    doc.save(source)
    doc.close()
    sample = context("", title="EXPORT_2024_LAYOUT", path=source)
    assert positioned_opening(sample)["author"] == "Amina Rao, Gabriel Santos"


def test_clean_native_text_does_not_launch_ocr(tmp_path):
    sample = context(
        "Clear body text without a byline. " * 20, path=tmp_path / "real.pdf"
    )
    with patch("author_metadata.bounded_ocr.subprocess.run") as run:
        assert not ocr_infer(sample)["author"]
    run.assert_not_called()


def test_ocr_thesis_requires_submission_context():
    sample = context("", kind="generic")
    text = "City Memory and Political Change\nTHESIS\nPresented for the Degree\nBy\nAmina Rao, B. S.\n2024"
    assert _credit(text, sample, 2)["author"] == "Amina Rao"
    assert not _credit(
        "A quoted narrative\nBy\nAmina Rao\nAnother sentence.", sample, 2
    )["author"]


def test_ocr_parent_editors_do_not_match_selected_chapter():
    sample = context(
        "", title="Santos Chapter Cities and Public Memory", kind="generic"
    )
    text = "A Handbook of Social History\nEdited by\nAmina Rao\nExample University Press and several other lines about publication."
    assert not _credit(text, sample, 1)["author"]


def test_ocr_interview_credits_writer_separately_from_subject():
    sample = context(
        "",
        title="Elena Chen with Gabriel Santos",
        kind="generic",
        path="Elena Chen and Gabriel Santos - In Conversation.pdf",
    )
    text = "IN CONVERSATION\nElena Chen\nwith Gabriel Santos\nThe participants discuss films and their work."
    report = _credit(text, sample, 1)
    assert report["author"] == "Gabriel Santos"
    assert report["role"] == "interviewer"


def test_short_ocr_title_requires_explicit_credit():
    sample = context("", title="Santos-Cities", kind="generic")
    prose = "This work considers the history of cities and the life of people living in them."
    assert (
        _credit("Cities\n- Gabriel Santos\n" + prose, sample, 1)["author"]
        == "Gabriel Santos"
    )
    assert not _credit("Cities\nGabriel Santos\n" + prose, sample, 1)["author"]


def test_ocr_wrapped_title_is_not_promoted_as_a_person():
    sample = context(
        "", title="Santos Chapter Cities and Public Memory", kind="generic"
    )
    text = (
        "Cities and Public Memory\nIdeas and Institutions\nRace Relations\nGabriel Santos\n"
        "This chapter explores the many historical changes in public memory and everyday life in cities."
    )
    assert _credit(text, sample, 3)["author"] == "Gabriel Santos"


def test_chapter_wrapper_does_not_require_filename_title():
    sample = context(
        "Chapter Title: Cities and Memory\nChapter Author(s): Amina Rao\n"
        "Book Title: A Handbook of History\nBook Editor(s): Gabriel Santos\nPublished by: Example Press\nStable URL: https://example.org/1",
        title="document-0042",
        kind="book_chapter",
    )
    assert document_record(sample)["author"] == "Amina Rao"


def test_explicit_translator_is_removed_from_generic_bibliographic_credit():
    sample = context(
        "Cities and Memory\nAmina Rao; Gabriel Santos\nTRANSLATED BY GABRIEL SANTOS"
    )
    assert (
        remove_translator_role({"author": "Amina Rao, Gabriel Santos"}, sample)[
            "author"
        ]
        == "Amina Rao"
    )


def test_publisher_titlepage_uses_typography_with_a_neutral_filename(tmp_path):
    source = tmp_path / "0042.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((80, 90), "Urban Change and Public Memory", fontsize=22)
    page.insert_text((80, 135), "Amina Rao", fontsize=12)
    page.insert_text((80, 170), "University of Example Press", fontsize=10)
    text = page.get_text()
    doc.save(source)
    doc.close()
    sample = context(text, title="0042", kind="book", path=source)
    assert publisher_titlepage(sample)["author"] == "Amina Rao"


def test_late_titlepage_is_linked_to_same_work_author_rights():
    sample = AuthorEvidenceContext.from_samples(
        [
            {"page": 3, "text": "A series of books"},
            {"page": 5, "text": "Urban Change and Public Memory\nAmina Rao"},
            {
                "page": 7,
                "text": "The right of Amina Rao to be identified as author of this work has been asserted.",
            },
        ],
        title_hint="Urban Change and Public Memory",
        profile=DocumentProfile("generic", (), (3, 5, 7)),
    )
    assert work_rights_credit(sample)["author"] == "Amina Rao"
    wrong = AuthorEvidenceContext.from_samples(
        sample.samples, title_hint="Another Selected Work", profile=sample.profile
    )
    assert not work_rights_credit(wrong)["author"]


def test_all_titlepage_authors_need_individual_biographic_support():
    samples = [
        {
            "page": 2,
            "text": "The Authors\nAmina Rao is a historian.\nGabriel Santos is a film scholar.",
        },
        {
            "page": 3,
            "text": "Urban Change and Public Memory\nAmina Rao and Gabriel Santos\nLondon",
        },
    ]
    sample = AuthorEvidenceContext.from_samples(
        samples, profile=DocumentProfile("generic", (), (2, 3))
    )
    assert biographic_titlepage(sample)["author"] == "Amina Rao, Gabriel Santos"
    samples[0]["text"] = "The Authors\nAmina Rao is a historian."
    incomplete = AuthorEvidenceContext.from_samples(samples, profile=sample.profile)
    assert not biographic_titlepage(incomplete)["author"]


def test_empty_ocr_attempt_is_not_cached_as_a_permanent_failure(tmp_path, monkeypatch):
    import subprocess
    import sys
    from author_metadata import bounded_ocr

    monkeypatch.setattr(bounded_ocr.shutil, "which", lambda _: sys.executable)
    source = tmp_path / "scan.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(30, 30, 100, 60), fill=(0, 0, 0))
    doc.save(source)
    doc.close()
    sample = context("", kind="generic", path=source)
    completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    with patch(
        "author_metadata.bounded_ocr.subprocess.run", return_value=completed
    ) as run:
        ocr_infer(sample)
        ocr_infer(sample)
    # Each request independently reads the nonblank image, then performs its
    # empty-reading quality retry. Neither an empty result nor success is cached.
    assert run.call_count == 4
    assert not any(key[0] == str(source) for key in bounded_ocr._CACHE)


def test_continued_byline_requires_same_person_in_complete_running_credit():
    sample = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "Urban Change and Public Memory\nAmina Rao and\nCities develop.",
            },
            {
                "page": 2,
                "text": "24 • Amina Rao and Gabriel Santos\nThe chapter continues.",
            },
        ],
        profile=DocumentProfile("book_chapter", (), (1, 2)),
    )
    assert continued_byline(sample)["author"] == "Amina Rao, Gabriel Santos"
    other = AuthorEvidenceContext.from_samples(
        [
            sample.samples[0],
            {
                "page": 2,
                "text": "Elena Chen and Gabriel Santos\nThe chapter continues.",
            },
        ],
        profile=sample.profile,
    )
    assert not continued_byline(other)["author"]


def test_access_platform_line_cannot_replace_visible_person():
    sample = context(
        "Urban Change and Public Memory\nAmina Rao\nAccess Academia\n"
        "Published on: 2025-01-01\nLicense: Creative Commons"
    )
    assert infer(sample)["author"] == "Amina Rao"


def test_tail_translator_with_joined_initials_is_not_a_coauthor():
    sample = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "Urban Change and Public Memory\nAmina Rao; Gabriel J. Santos",
            },
            {"page": 28, "text": "Translated by GabrielJ. Santos"},
        ],
        profile=DocumentProfile("book_chapter", (), (1, 28)),
    )
    assert (
        remove_translator_role({"author": "Amina Rao, Gabriel J. Santos"}, sample)[
            "author"
        ]
        == "Amina Rao"
    )


def test_whole_book_editor_credit_depends_on_scope_and_title_not_page_count(tmp_path):
    source = tmp_path / "misleading-chapter.pdf"
    doc = fitz.open()
    for _ in range(80):
        doc.new_page()
    doc.save(source)
    doc.close()
    sample = context(
        "Urban Change and Public Memory\nEdited by\nAmina Rao\nand\nGabriel Santos\nExample Press",
        kind="generic",
        path=source,
    )
    assert whole_book_editors(sample)["author"] == "Amina Rao, Gabriel Santos"
    short = tmp_path / "chapter.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(short)
    doc.close()
    assert (
        whole_book_editors(
            context(sample.samples[0]["text"], kind="generic", path=short)
        )["author"]
        == "Amina Rao, Gabriel Santos"
    )
    from dataclasses import replace

    chapter = replace(
        sample,
        path=short,
        profile=DocumentProfile("book_chapter", (), (1,), work_scope="chapter"),
    )
    assert not whole_book_editors(chapter)["author"]


@pytest.mark.parametrize(
    "citation_author,expected", [("Amina Rao", "Amina Rao"), ("Elena Chen", "")]
)
def test_exact_publisher_title_can_omit_visible_subtitle(citation_author, expected):
    sample = context(
        "Urban Change and Public Memory\nA second descriptive subtitle\nAmina Rao\n"
        f"To cite this article: {citation_author} (2024) Urban Change and Public Memory\n"
        "DOI: 10.1000/example"
    )
    assert infer(sample)["author"] == expected


def test_positioned_byline_above_complete_heading(tmp_path):
    source = tmp_path / "0043.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 40), "Amina Rao", fontsize=11)
    page.insert_text((45, 210), "Urban Change and Public Memory", fontsize=22)
    page.insert_textbox(
        fitz.Rect(30, 350, 520, 700),
        "The urban history of public memory develops through social change. " * 15,
        fontsize=11,
    )
    doc.save(source)
    doc.close()
    assert positioned_opening(context("", path=source))["author"] == "Amina Rao"
    assert not positioned_opening(
        context("", title="Another Work on Political Culture", path=source)
    )["author"]


def test_institution_is_not_a_second_person_in_positioned_credit(tmp_path):
    source = tmp_path / "neutral.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((80, 90), "Urban Change and Public Memory", fontsize=22)
    page.insert_text((80, 135), "Amina Rao, Example Tech", fontsize=11)
    page.insert_text((80, 175), "1 Opening discussion", fontsize=12)
    doc.save(source)
    doc.close()
    assert not positioned_opening(context("", title="Export Packet", path=source))[
        "author"
    ]
