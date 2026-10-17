"""Planned front matter must survive the collector and downstream adapters."""

from unittest.mock import patch
import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.evidence_collection import collect
from author_metadata.profile import DocumentProfile
from author_metadata.dispatcher import infer_author

pytestmark = pytest.mark.offline_deterministic


def apparatus_pdf(path, *, pages=20, all_contents=False):
    doc = fitz.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        if n <= 15 or all_contents:
            page.insert_text((50, 60), "Contents", fontsize=18)
            for i in range(8):
                page.insert_text(
                    (50, 100 + i * 20),
                    f"Chapter {i + 1}: Public life and historical records ........ {i * 10 + 1}",
                    fontsize=10,
                )
        elif n == 16:
            page.insert_text((50, 70), "Collective Memory and Public Life", fontsize=20)
            page.insert_text((50, 130), "Amelia Hart", fontsize=14)
            page.insert_text((50, 190), "Westport University Press", fontsize=10)
            page.insert_text(
                (50, 240),
                "First edition. Published in Westport and distributed internationally.",
                fontsize=10,
            )
        elif n == 17:
            page.insert_text((50, 70), "Copyright", fontsize=18)
            page.insert_text(
                (50, 120),
                "Amelia Hart asserts authorship of the present work.",
                fontsize=10,
            )
            page.insert_text(
                (50, 150),
                "This edition was prepared for publication by Westport University Press.",
                fontsize=10,
            )
        else:
            page.insert_text(
                (50, 60),
                "Introduction" if n == 18 else "Continued discussion",
                fontsize=18,
            )
            for i in range(8):
                page.insert_text(
                    (50, 100 + i * 20),
                    "This study examines the role of historical records in public life and collective memory.",
                    fontsize=10,
                )
    doc.save(path)
    doc.close()


def context(path):
    return AuthorEvidenceContext.from_samples(
        [],
        path=path,
        title_hint="document-100",
        profile=DocumentProfile("book", (), (), work_scope="single_work"),
    )


def test_continuing_apparatus_is_acquired_beyond_twelve(tmp_path):
    path = tmp_path / "document-100.pdf"
    apparatus_pdf(path)
    with patch("author_metadata.evidence_collection.subprocess.run") as ocr:
        result = collect(context(path))
    ocr.assert_not_called()
    trace = result.evidence_bundle["trace"]
    assert {16, 17, 18} <= set(trace["native_plan"])
    assert 17 in {s["page"] for s in result.planned_opening_pages}
    assert 20 not in {s["page"] for s in result.planned_opening_pages}
    assert trace["complete"]


def test_late_rights_and_titlepage_reach_final_adjudication(tmp_path):
    path = tmp_path / "document-100.pdf"
    apparatus_pdf(path)
    result = infer_author(context(path))
    assert result["author"] == "Amelia Hart"
    assert any(
        c["page"] == 17 and c["author"] == "Amelia Hart"
        for c in result["evidence_review"]["claims"]
    )
    assert result["evidence_review"]["all_scheduled_experts_assessed"]


def test_frontmatter_window_limit_is_declared(tmp_path):
    path = tmp_path / "document-100.pdf"
    apparatus_pdf(path, pages=40, all_contents=True)
    result = collect(context(path))
    trace = result.evidence_bundle["trace"]
    assert max(trace["opening_plan"]) == 32
    assert "32-page" in trace["planning_limit_reached"]
    assert 39 in trace["native_plan"] and 40 in trace["native_plan"]
    assert 33 not in trace["native_plan"]
