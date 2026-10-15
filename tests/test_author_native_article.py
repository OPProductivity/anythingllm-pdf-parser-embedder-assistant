"""Complete scientific bylines survive fragments, sidebars and role boundaries."""

from unittest.mock import patch

import fitz
import pytest

from author_metadata.native_article import credit, positioned_lines, infer
from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile

pytestmark = pytest.mark.offline_deterministic


def line(text, y, size=12, left=160, right=500):
    return {
        "text": text,
        "box": [left, y, right, y + size],
        "size": size,
        "baseline": y + size,
    }


def abstract():
    return [
        line("Department of History, Example University", 240, 9),
        line("Abstract", 300, 14),
        line("The study examines the social history of many cities.", 330, 10),
    ]


def test_complete_wrapped_credit_excludes_sidebar_editor():
    lines = [
        line("Cities and Public Memory", 100, 20),
        line("Nadia Okafor, Gabriel Santos,", 155),
        line("and Elena Chen", 173),
        line("Editor: Morgan Ellis", 165, 9, left=20, right=130),
        *abstract(),
    ]
    lines.sort(key=lambda v: v["baseline"])
    assert (
        credit(lines, 600, 800)["author"] == "Nadia Okafor, Gabriel Santos, Elena Chen"
    )


@pytest.mark.parametrize(
    "role", ["Edited by", "Reviewed by", "Translated by", "Correspondence:"]
)
def test_labelled_role_cannot_supply_an_unlabelled_article_author(role):
    lines = [
        line("Cities and Public Memory", 100, 20),
        line(role, 145),
        line("Nadia Okafor", 170),
        *abstract(),
    ]
    assert not credit(lines, 600, 800)["author"]


def test_city_country_tail_is_ambiguity_instead_of_an_inverted_name():
    lines = [
        line("Cities and Public Memory", 100, 20),
        line("Nadia Okafor", 155),
        line("Aalborg, Denmark", 170),
        *abstract(),
    ]
    assert not credit(lines, 600, 800)["author"]


def test_credential_roster_is_left_to_its_complete_roster_parser():
    lines = [
        line("Cities and Public Memory", 100, 20),
        line("Nadia Okafor, MD, MPH", 155),
        *abstract(),
    ]
    assert not credit(lines, 600, 800)["author"]


def test_byline_nearly_as_large_as_title_has_a_separate_vertical_band():
    lines = [
        line("Cities and Public Memory", 100, 24),
        line("Nadia Okafor", 155, 22),
        line("Abstract: This study examines cities and public memory.", 210, 10),
    ]
    assert credit(lines, 600, 800)["author"] == "Nadia Okafor"


def test_long_affiliations_can_precede_an_abstract_later_on_the_first_page():
    lines = [
        line("Cities and Public Memory", 100, 20),
        line("Nadia Okafor and Gabriel Santos", 155),
        line("Department of History, Example University", 210, 10),
        line("Abstract", 550, 14),
    ]
    assert credit(lines, 600, 800)["author"] == "Nadia Okafor, Gabriel Santos"


def test_first_page_only_cannot_borrow_names_from_later_references(tmp_path):
    path = tmp_path / "document-555.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "Cities and Public Memory", fontsize=20)
    page.insert_text((60, 160), "Abstract", fontsize=12)
    page = doc.new_page()
    page.insert_text((60, 100), "Nadia Okafor", fontsize=12)
    doc.save(path)
    doc.close()
    ctx = AuthorEvidenceContext.from_samples(
        [], path=path, profile=DocumentProfile("scholarly_article", (), ())
    )
    assert not infer(ctx)["author"]


def test_native_fragments_preserve_comma_only_objects_but_remove_superscripts():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((160, 100), "Cities and Public Memory", fontsize=20)
    page.insert_text((160, 160), "Nadia Okafor", fontsize=12)
    page.insert_text((235, 155), "1,2", fontsize=6)
    page.insert_text((250, 160), ",", fontsize=12)
    page.insert_text((260, 160), "Gabriel Santos,", fontsize=12)
    page.insert_text((160, 177), "Elena Chen", fontsize=12)
    page.insert_text(
        (160, 215), "Department of History, Example University", fontsize=9
    )
    page.insert_text((160, 260), "Abstract", fontsize=14)
    with patch("author_metadata.portable_layout.subprocess.run") as ocr:
        result = credit(positioned_lines(page), page.rect.width, page.rect.height)
    assert result["author"] == "Nadia Okafor, Gabriel Santos, Elena Chen"
    ocr.assert_not_called()
    doc.close()
