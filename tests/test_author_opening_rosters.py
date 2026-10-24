"""Normal publishing layouts with long and mixed affiliation rosters."""

import fitz
import pytest
from author_metadata.native_article import (
    positioned_lines,
    email_roster_reports,
    credit_reports,
)

pytestmark = pytest.mark.offline_deterministic

NAMES = [
    "Amelia Hart",
    "Bernard Stone",
    "Celia Green",
    "David Lane",
    "Elena Rose",
    "Farid Abbas",
    "Grace Wu",
    "Hugo Silva",
    "Iris Meier",
    "Jules Roy",
    "Kiara Shah",
]


def test_multiline_email_affiliations_collect_complete_roster():
    doc = fitz.open()
    page = doc.new_page(width=700, height=1100)
    page.insert_text(
        (40, 55), "Community History and Shared Responsibility", fontsize=18
    )
    for i, name in enumerate(NAMES):
        y = 90 + i * 75
        page.insert_text((40, y), name, fontsize=11)
        page.insert_text((350, y), f"writer{i}@example.invalid", fontsize=11)
        page.insert_text((40, y + 17), "Department of History", fontsize=9)
        page.insert_text((40, y + 29), "Westport University", fontsize=9)
        page.insert_text((40, y + 41), "Westport, 1234, Country", fontsize=9)
    page.insert_text((40, 930), "Editor: Olivia Reed", fontsize=10)
    page.insert_text((40, 970), "Abstract", fontsize=12)
    reports = list(email_roster_reports(positioned_lines(page), 700, 1100))
    assert [r["author"] for r in reports] == [", ".join(NAMES)]
    doc.close()


def test_parallel_inline_name_email_blocks_before_dash_abstract():
    doc = fitz.open()
    page = doc.new_page(width=620, height=840)
    page.insert_text((80, 75), "Rule-Based Metadata Extraction for Research Papers", fontsize=17)
    page.insert_text((80, 125), "Jahongir Azimjonov, aazim@example.invalid", fontsize=10)
    page.insert_text((335, 125), "Jumabek Alikhanov, jumabek@example.invalid", fontsize=10)
    page.insert_text((80, 145), "Westport University", fontsize=9)
    page.insert_text((335, 145), "Eastport University", fontsize=9)
    page.insert_text((80, 205), "Abstract- This article considers metadata extraction.", fontsize=10)
    reports = list(email_roster_reports(positioned_lines(page), 620, 840))
    assert [r["author"] for r in reports] == ["Jahongir Azimjonov, Jumabek Alikhanov"]
    doc.close()


def test_stacked_roster_is_not_limited_to_eight_name_lines():
    doc = fitz.open()
    page = doc.new_page(width=700, height=1100)
    page.insert_text(
        (40, 60), "Community History and Shared Responsibility", fontsize=18
    )
    for i, name in enumerate(NAMES):
        page.insert_text((40, 100 + i * 14), name, fontsize=11)
    page.insert_text(
        (40, 280), "Department of History, Westport University", fontsize=9
    )
    page.insert_text((40, 310), "Abstract", fontsize=12)
    page.insert_text(
        (40, 340),
        "This study investigates community history and the responsibility of groups for their shared records.",
        fontsize=10,
    )
    reports = list(credit_reports(positioned_lines(page), 700, 1100))
    assert reports and all(r["author"] == ", ".join(NAMES) for r in reports)
    doc.close()


def test_email_contacts_after_body_boundary_do_not_form_author_roster():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (40, 60), "Community History and Shared Responsibility", fontsize=18
    )
    page.insert_text((40, 100), "Abstract", fontsize=12)
    page.insert_text(
        (40, 140), "For further information contact the following people.", fontsize=10
    )
    page.insert_text((40, 180), "Amelia Hart amelia@example.invalid", fontsize=11)
    page.insert_text((40, 210), "Westport University", fontsize=9)
    assert not list(
        email_roster_reports(positioned_lines(page), page.rect.width, page.rect.height)
    )
    doc.close()


def test_unpaired_byline_person_prevents_complete_email_subset_claim():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (40, 60), "Community History and Shared Responsibility", fontsize=18
    )
    page.insert_text((40, 100), "Amelia Hart amelia@example.invalid", fontsize=11)
    page.insert_text((40, 120), "Westport University", fontsize=9)
    page.insert_text((40, 150), "Bernard Stone", fontsize=11)
    page.insert_text((40, 170), "Westport University", fontsize=9)
    page.insert_text((40, 200), "Abstract", fontsize=12)
    assert not list(
        email_roster_reports(positioned_lines(page), page.rect.width, page.rect.height)
    )
    doc.close()


def test_complete_physical_roster_can_challenge_accepted_partial_result():
    from author_metadata.publishing_relations import select

    claim = {
        "author": "Amelia Hart, Bernard Stone",
        "source": "text_structured_opening_credit",
        "page": 1,
        "evidence": "Title / two name-email rows / affiliations / abstract",
        "role": "author",
        "work": "selected work",
        "family": "physical_complete_email_roster",
        "priority": 109,
    }
    chosen, _ = select(
        {"author": "Amelia Hart", "source": "text_byline"},
        [claim],
        kind="scholarly_article",
        work_scope="single_work",
    )
    assert chosen["author"] == "Amelia Hart, Bernard Stone"


def test_praise_contact_panel_cannot_establish_email_author_roster():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 60), "Praise for Collective Memory", fontsize=18)
    page.insert_text((40, 100), "Amelia Hart amelia@example.invalid", fontsize=11)
    page.insert_text((40, 120), "Westport University", fontsize=9)
    page.insert_text((40, 160), "Abstract", fontsize=12)
    assert not list(
        email_roster_reports(positioned_lines(page), page.rect.width, page.rect.height)
    )
    doc.close()
