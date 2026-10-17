"""Identifier logos, represented groups and physical roster boundaries."""

import fitz
import pytest
from author_metadata.native_article import (
    positioned_lines,
    credit,
    credit_reports,
)
from author_metadata.names import normalize_author_candidate
from author_metadata.evidence_collection import _plan

pytestmark = pytest.mark.offline_deterministic


def row(text, y, size=12, left=50, right=540):
    return {
        "text": text,
        "box": [left, y, right, y + size],
        "size": size,
        "baseline": y + size,
    }


def test_only_linked_identifier_icon_is_removed(tmp_path):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 100), "Amelia Hart", fontsize=12)
    p.insert_text((120, 100), "ID", fontsize=8)
    p.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(119, 91, 131, 103),
            "uri": "https://orcid.org/0000-0001-0002-0003",
        }
    )
    path = tmp_path / "document.pdf"
    doc.save(path)
    doc.close()
    with fitz.open(path) as pdf:
        lines = positioned_lines(pdf[0])
        assert any("Amelia Hart" in line["text"] and "ID" not in line["text"] for line in lines)
        assert not any("ID" in line["text"] for line in lines)
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 100), "Amelia Id", fontsize=12)
    assert any("Amelia Id" == line["text"] for line in positioned_lines(p))
    doc.close()


def test_large_gaps_with_affiliation_markers_preserve_separate_people(tmp_path):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 100), "Amelia Hart", fontsize=12)
    p.insert_text((116, 96), "1", fontsize=7)
    p.insert_text((137, 100), "Benedict Vale", fontsize=12)
    p.insert_text((211, 96), "2", fontsize=7)
    p.insert_text((232, 100), "Caroline Smith", fontsize=12)
    from author_metadata.native_article import _column_lines

    text = " ".join(line["text"] for line in _column_lines(positioned_lines(p), 0, 600))
    assert "Amelia Hart, Benedict Vale, Caroline Smith" in text
    doc.close()


def test_wrapped_initial_suffix_is_retained_as_partial():
    lines = [
        row("Cities and Public Memory", 100, 20),
        row("Amelia Hart, Benedict", 150),
        row("J. Vale, Caroline Smith", 165),
        row("Department of History, Example University", 200, 9),
        row("Abstract", 240, 14),
    ]
    found = list(credit_reports(lines, 600, 800))
    assert (
        credit(lines, 600, 800)["author"]
        == "Amelia Hart, Benedict J. Vale, Caroline Smith"
    )
    assert any(
        r["author"] == "J. Vale, Caroline Smith"
        and not r.get("eligible_for_selection", True)
        for r in found
    )


def test_represented_group_stays_with_personal_author_roster():
    lines = [
        row("Cities and Public Memory", 100, 20),
        row("Amelia Hart, Benedict Vale, on behalf of the", 150),
        row("Example Research Consortium", 165),
        row("Department of History, Example University", 200, 9),
        row("Abstract", 240, 14),
    ]
    result = credit(lines, 600, 800)
    assert result["author"] == "Amelia Hart, Benedict Vale, Example Research Consortium"
    assert result["represented_groups"][0]["role"] == "represented author group"


def test_group_mentioned_after_abstract_is_not_an_author():
    lines = [
        row("Cities and Public Memory", 100, 20),
        row("Amelia Hart", 150),
        row("Department of History, Example University", 180, 9),
        row("Abstract", 210, 14),
        row("Target Language", 225, 12),
        row("The Example Research Consortium funded this study.", 245, 10),
    ]
    assert [
        r["author"]
        for r in credit_reports(lines, 600, 800)
        if r.get("eligible_for_selection", True)
    ] == ["Amelia Hart"]


def test_dotless_base_accent_is_repaired_without_changing_unaccented_dotless_names():
    assert normalize_author_candidate("Benedict del Rı´o") == "Benedict del Río"
    assert normalize_author_candidate("Ayşe Yıldız") == "Ayşe Yıldız"


def test_acknowledgments_after_article_body_do_not_reopen_frontmatter():
    doc = fitz.open()
    for n in range(10):
        p = doc.new_page()
        p.insert_text(
            (50, 80),
            "Abstract"
            if n == 0
            else "Acknowledgments"
            if n == 4
            else "Continued discussion",
            fontsize=18,
        )
        p.insert_textbox(
            fitz.Rect(50, 120, 540, 700),
            "This study examines records and public memory. " * 35,
            fontsize=11,
        )
    pages, extensions = _plan(doc)
    assert set(pages) == {1, 2, 3, 4, 5, 9, 10}
    assert not any(e["reason"] == "front_matter_sequence" for e in extensions)
    doc.close()


def test_book_title_containing_abstract_does_not_close_frontmatter():
    doc = fitz.open()
    for n in range(15):
        p = doc.new_page()
        p.insert_text(
            (50, 80), "Abstract Cities" if n == 0 else "Contents", fontsize=18
        )
    pages, extensions = _plan(doc)
    assert set(range(6, 13)) <= set(pages)
    assert any(e["reason"] == "front_matter_sequence" for e in extensions)
    doc.close()
