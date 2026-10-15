"""Neutral exports preserve whole work credits and reject adjacent roles."""

from unittest.mock import patch

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.portable_book import infer as infer_book
from author_metadata.portable_layout import layout_credit
from author_metadata.profile import DocumentProfile

pytestmark = pytest.mark.offline_deterministic


def line(text, y, size=12, left=60, right=440):
    return {"text": text, "box": [left, y, right, y + size], "size": size}


def body(y=260, left=40):
    return [
        line(
            "This study describes the history of public culture in several cities.",
            y,
            10,
            left,
        ),
        line(
            "It examines the social and political causes of urban transformation.",
            y + 14,
            10,
            left,
        ),
    ]


def test_centered_heading_can_belong_to_a_wider_body_column():
    lines = [
        line("Cities and Public Memory", 100, 20, 130, 360),
        line("Nadia Okafor", 150, 12, 200, 290),
        *body(195),
    ]
    assert layout_credit(lines, 600, 800)["author"] == "Nadia Okafor"


def test_quoted_scholarly_title_has_abstract_evidence_unlike_endorsement():
    lines = [
        line('"Urban memory": A history of cities', 100, 20),
        line("Nadia Okafor", 150),
        line("Abstract", 200, 10),
        *body(220),
    ]
    assert layout_credit(lines, 600, 800)["author"] == "Nadia Okafor"
    lines.pop(2)
    assert not layout_credit(lines, 600, 800)["author"]


def test_qualifications_do_not_hide_a_complete_two_author_credit():
    lines = [
        line("Cities and Public Memory", 100, 20),
        line("Nadia Okafor, PhD, and Gabriel Santos", 150),
        *body(190),
    ]
    assert layout_credit(lines, 600, 800)["author"] == "Nadia Okafor, Gabriel Santos"


@pytest.mark.parametrize("boundary", ["C h a p t e r  1 0", "Abstract"])
def test_opening_author_can_precede_heading_with_independent_work_boundary(boundary):
    lines = [
        line("Nadia Okafor", 80, 18, right=210),
        line("Cities and Public Memory", 125, 17),
        *body(220),
    ]
    if boundary == "Abstract":
        lines.insert(2, line(boundary, 195, 10))
    else:
        lines.insert(0, line(boundary, 30, 10))
    assert layout_credit(lines, 600, 800)["author"] == "Nadia Okafor"


def test_repeated_section_heading_is_not_an_author_above_a_smaller_title():
    from author_metadata.structured_credit import _words

    lines = [
        line("Different Perspectives", 45, 22),
        line("The History of Cities", 100, 16),
        line("Abstract", 155, 10),
        *body(180),
    ]
    assert not layout_credit(
        lines, 600, 800, repeated_headers={_words("Different Perspectives")}
    )["author"]


@pytest.mark.parametrize("role", ["Edited by", "Translated by", "Reviewed by"])
def test_role_above_a_chapter_heading_is_not_an_unlabelled_author(role):
    lines = [
        line("Chapter 1", 30),
        line(role + " Nadia Okafor", 80, 18),
        line("Cities and Public Memory", 125, 17),
        *body(220),
    ]
    assert not layout_credit(lines, 600, 800)["author"]


def make_book(tmp_path, *, pages=80, praise=False):
    source = tmp_path / "document-734.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "Cities and Public Memory", fontsize=24)
    page = doc.new_page()
    page.insert_text((60, 100), "Cities and Public Memory", fontsize=24)
    page.insert_text((60, 160), "Edited by Nadia Okafor, Gabriel Santos,", fontsize=12)
    page.insert_text((60, 180), "and Elena Chen", fontsize=12)
    if praise:
        page.insert_text((60, 60), "Praise for", fontsize=12)
        doc[0].insert_text((60, 60), "Praise for", fontsize=12)
    for _ in range(pages - 2):
        doc.new_page()
    doc.set_metadata({"title": "UNRELATED_EXPORT_PACKET", "author": "Export Operator"})
    samples = [{"page": i + 1, "text": doc[i].get_text()} for i in range(2)]
    doc.save(source)
    doc.close()
    return AuthorEvidenceContext.from_samples(
        samples,
        path=source,
        title_hint="UNRELATED_EXPORT_PACKET",
        profile=DocumentProfile("book", (), (1, 2)),
    )


def test_physical_title_recovers_complete_inline_editor_list_without_ocr(tmp_path):
    context = make_book(tmp_path)
    with patch("author_metadata.portable_layout.subprocess.run") as ocr:
        result = infer_book(context)
    assert result["author"] == "Nadia Okafor, Gabriel Santos, Elena Chen"
    assert result["physical_title"] == "Cities and Public Memory"
    ocr.assert_not_called()


def test_parent_volume_frontmatter_cannot_define_short_chapter_authorship(tmp_path):
    assert not infer_book(make_book(tmp_path, pages=15))["author"]


def test_praise_pages_do_not_provide_a_substitute_work_title(tmp_path):
    assert not infer_book(make_book(tmp_path, praise=True))["author"]
