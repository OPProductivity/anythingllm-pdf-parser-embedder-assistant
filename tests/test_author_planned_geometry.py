import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.profile import DocumentProfile

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("kind", ["generic", "book_chapter"])
def test_later_planned_article_reaches_physical_roster_experts(tmp_path, kind):
    path = tmp_path / "document-100.pdf"
    doc = fitz.open()
    for n in range(1, 21):
        page = doc.new_page()
        if n <= 15:
            page.insert_text((50, 60), "Contents", fontsize=18)
            for i in range(8):
                page.insert_text(
                    (50, 100 + i * 20),
                    f"Section {i + 1}: Public historical records ........ {i + 1}",
                    fontsize=10,
                )
        elif n == 16:
            page.insert_text((50, 70), "Collective Memory and Public Life", fontsize=20)
            page.insert_text(
                (50, 130), "Amelia Hart, Benedict Vale and Cecilia Brooks", fontsize=14
            )
            page.insert_text((50, 165), "University of Westport", fontsize=10)
            page.insert_text((50, 210), "Abstract", fontsize=12)
            page.insert_text(
                (50, 250),
                "This study investigates public records and the preservation of collective memory.",
                fontsize=10,
            )
        else:
            page.insert_text(
                (50, 60),
                "Introduction" if n == 17 else "Continued discussion",
                fontsize=18,
            )
            for i in range(8):
                page.insert_text(
                    (50, 100 + i * 20),
                    "This study discusses historical records, communities, and their shared public life.",
                    fontsize=10,
                )
    doc.save(path)
    doc.close()
    result = infer_author(
        AuthorEvidenceContext.from_samples(
            [],
            path=path,
            title_hint="document-100",
            profile=DocumentProfile(
                kind,
                (),
                (),
                work_scope="chapter" if kind == "book_chapter" else "single_work",
            ),
        )
    )
    assert any(
        a["expert"] == "physical_byline" and a["page"] == 16 and a["proposals"]
        for a in result["evidence_review"]["expert_attempts"]
    )
    assert result["author"] == "Amelia Hart, Benedict Vale, Cecilia Brooks"


def test_side_role_panel_does_not_interrupt_title_or_supply_its_creator():
    from author_metadata.native_article import credit

    def row(text, y, size=12, left=210, right=550):
        return {
            "text": text,
            "box": [left, y, right, y + size],
            "size": size,
            "baseline": y + size,
        }

    lines = [
        row("Cities and Public", 100, 20),
        row("Memory across Generations", 124, 20),
        row("OPEN ACCESS", 115, 7, 25, 110),
        row("EDITED BY", 148, 6, 25, 90),
        row("Morgan Ellis", 162, 9, 25, 120),
        row("Amelia Hart, Benedict Vale", 170, 12),
        row("Department of History, Example University", 205, 9),
        row("Abstract", 240, 14),
        row("This study examines the role of records in public life.", 265, 10),
    ]
    lines.sort(key=lambda v: v["baseline"])
    assert credit(lines, 600, 800)["author"] == "Amelia Hart, Benedict Vale"


def test_author_contact_columns_can_use_different_type_sizes():
    from author_metadata.native_article import email_roster_reports

    def row(text, y, size=12, left=50, right=200):
        return {
            "text": text,
            "box": [left, y - size, right, y + 2],
            "size": size,
            "baseline": y,
        }

    lines = [
        row("Cities and Public Memory", 100, 20, right=500),
        row("Amelia Hart", 150),
        row("hart@example.edu", 150, 8, 300, 500),
        row("Benedict Vale", 170),
        row("vale@example.edu", 170, 8, 300, 500),
        row("Department of History, Example University", 200, 10, right=500),
        row("Editor: Morgan Ellis", 220, 9),
        row("Abstract", 250, 14),
        row("This study examines records and public memory.", 280, 10, right=500),
    ]
    reports = list(email_roster_reports(lines, 600, 800))
    assert [r["author"] for r in reports] == ["Amelia Hart, Benedict Vale"]


def test_date_can_separate_author_columns_from_abstract_without_local_affiliation():
    from author_metadata.native_article import credit

    def row(text, y, size=12, left=50, right=500):
        return {
            "text": text,
            "box": [left, y, right, y + size],
            "size": size,
            "baseline": y + size,
        }

    lines = [
        row("Records and Computational Public Memory", 100, 20),
        row("Amelia Hart", 150, 12, 130, 230),
        row("Benedict Vale", 150, 12, 300, 410),
        row("February 9, 2025", 190, 12, 220, 350),
        row("Abstract", 235, 14),
        row("This paper studies the role of records in many cities.", 265, 10),
    ]
    assert credit(lines, 600, 800)["author"] == "Amelia Hart, Benedict Vale"


def test_geometric_roster_suffix_remains_visible_but_is_not_complete():
    from author_metadata.native_article import credit_reports

    def row(text, y, size=12):
        return {
            "text": text,
            "box": [50, y, 550, y + size],
            "size": size,
            "baseline": y + size,
        }

    lines = [
        row("Cities and Public Memory", 100, 20),
        row("Amelia Hart, Benedict", 150),
        row("Vale, Caroline Smith,", 165),
        row("Daniel Green, Elena Rao", 180),
        row("Department of History, Example University", 215, 9),
        row("Abstract", 250, 14),
    ]
    reports = list(credit_reports(lines, 600, 800))
    assert any(
        r["author"]
        == "Amelia Hart, Benedict Vale, Caroline Smith, Daniel Green, Elena Rao"
        and r.get("eligible_for_selection", True)
        for r in reports
    )
    partial = [r for r in reports if r["author"] == "Daniel Green, Elena Rao"]
    assert partial and all(not r["eligible_for_selection"] for r in partial)
    assert all(r["mechanism"] == "partial_native_author_band" for r in partial)
