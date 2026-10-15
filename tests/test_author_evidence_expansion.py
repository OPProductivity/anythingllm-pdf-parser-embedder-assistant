"""Credits must stay attached to their own work across common PDF layouts."""

from pathlib import Path

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.identity import resolve_author_from_metadata_and_inference
from author_metadata.profile import DocumentProfile

pytestmark = pytest.mark.offline_deterministic


def context(kind, title, samples, name="example.pdf"):
    return AuthorEvidenceContext.from_samples(
        samples, path=Path(name), title_hint=title,
        profile=DocumentProfile(kind, (), tuple(row["page"] for row in samples)),
    )


def test_book_editor_list_is_complete_and_series_list_is_ignored():
    sample = context("book", "Precarity and Belonging -- catalog fields", [
        {"page": 1, "text": "Precarity and Belonging"},
        {"page": 2, "text": "Precarity and Belonging\nLabor and Citizenship\n"
                            "EDITED BY CATHERINE RAMIREZ, SYLVANNA FALCON,\n"
                            "JUAN POBLETE, STEVEN MCKAY, AND\nFELICITY SCHAEFFER\nRutgers University Press"},
        {"page": 3, "text": "Precarity and Belonging\nA series edited by Alice Martin\n"
                            "Humor and Citizenship\nedited by Bob Smith and Carol Jones"},
    ])
    report = infer_author(sample)
    assert report["author"] == "CATHERINE RAMIREZ, SYLVANNA FALCON, JUAN POBLETE, STEVEN MCKAY, FELICITY SCHAEFFER"
    assert resolve_author_from_metadata_and_inference("", report)["author"] == report["author"]


def test_repeated_platform_footer_requires_work_identity_and_two_pages():
    footer = ("EBSCO Publishing : eBook Collection (EBSCOhost) - printed on\n"
              "1/19/2018 via LIBRARY 29 AN: 313985 ; Veblen, Thorstein.; The Theory of the Leisure Class\n")
    good = context("book_chapter", "Veblen 4", [
        {"page": 1, "text": "Chapter Four - Consumption\n" + footer},
        {"page": 2, "text": "More chapter text\n" + footer},
    ], "Veblen 4.pdf")
    assert infer_author(good)["author"] == "Thorstein Veblen"
    wrong = context("book_chapter", "Smith 4", list(good.samples), "Smith 4.pdf")
    assert infer_author(wrong)["author"] == ""


def test_local_chapter_byline_beats_parent_volume_editor():
    sample = context("book_chapter", "Marx-The Philosophic and Economic Manuscripts of 1844 Chapter 2", [
        {"page": 1, "text": "Literary Theory: An Anthology. Edited by Julie Rivkin and Michael Ryan.\n"
                            "The Philosophic and Economic\nManuscripts of 1844\nKarl Marx\nChapter 2\n"
                            "Private property is a concept in Marx that has been misunderstood."},
    ], "Marx-Chapter.pdf")
    assert infer_author(sample)["author"] == "Karl Marx"


@pytest.mark.parametrize("title,heading,author,column_x,body_x", [
    ("Cultural Keywords - Afterlives", "Afterlives", "Maya Okafor", 296, 54),
    ("Migrant Memory and Place", "Migrant Memory and Place", "Leila Rahman", 54, 296),
])
def test_positioned_column_heading_matches_local_work_across_layouts(
        tmp_path, title, heading, author, column_x, body_x):
    source = tmp_path / f"{title}.pdf"
    document = fitz.open()
    page = document.new_page(width=575, height=612)
    page.insert_text((body_x, 45), "Earlier body text from another work in this column.")
    page.insert_text((column_x, 86), heading)
    page.insert_text((column_x, 107), author)
    page.insert_text((column_x, 170),
                     "The discussion begins with evidence about public memory and local history.")
    document.save(source)
    document.close()
    with fitz.open(source) as opened:
        samples = [{"page": 1, "text": opened[0].get_text()}]
    report = infer_author(AuthorEvidenceContext.from_samples(
        samples, path=source, title_hint=title,
        profile=DocumentProfile("scholarly_article", (), (1,)),
    ))
    assert report["author"] == author


@pytest.mark.parametrize("heading,body", [
    ("References", True),
    ("Cultural Keywords", True),
    ("Afterlives", False),
])
def test_positioned_column_credit_rejects_unrelated_heading_or_orphan_name(
        tmp_path, heading, body):
    source = tmp_path / "Cultural Keywords - Afterlives.pdf"
    document = fitz.open()
    page = document.new_page(width=575, height=612)
    page.insert_text((54, 45), "Earlier body text from another work in this column.")
    page.insert_text((296, 86), heading)
    page.insert_text((296, 107), "Maya Okafor")
    if body:
        page.insert_text((296, 170),
                         "The discussion begins with evidence about public memory and local history.")
    document.save(source)
    document.close()
    with fitz.open(source) as opened:
        samples = [{"page": 1, "text": opened[0].get_text()}]
    report = infer_author(AuthorEvidenceContext.from_samples(
        samples, path=source, title_hint="Cultural Keywords - Afterlives",
        profile=DocumentProfile("scholarly_article", (), (1,)),
    ))
    assert report["author"] == ""
