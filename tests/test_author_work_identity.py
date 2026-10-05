from pathlib import Path

import pytest

from author_metadata.pdf import recover_author_from_selected_extraction, corroborates_filename_surname
from author_metadata.identity import normalize_metadata_title
from author_metadata.work_identity import resolve_work_identity


pytestmark = pytest.mark.offline_deterministic


def sample(text):
    return [{"page": 1, "text": text}]


@pytest.mark.parametrize("value", ["2.png", "scan.JPEG", "page-001.tif", "capture.webp", "photo.heic"])
def test_raster_export_names_are_not_document_titles(value):
    assert normalize_metadata_title(value) == ""


def test_human_title_with_image_term_remains_valid():
    assert normalize_metadata_title("The PNG Image as Evidence") == "The PNG Image as Evidence"


def test_browser_metadata_container_is_separated_only_with_visible_title():
    pages = sample(
        "News\nA long headline about a public scientific discovery\n"
        "Associated Press\nhttps://example.org/story | 1/3"
    )
    identity = resolve_work_identity(
        "A long headline about a public scientific discovery | Science | Example News",
        Path("story.pdf"), pages,
    )
    assert identity.title == "A long headline about a public scientific discovery"
    assert identity.container_title == "Example News"
    assert identity.visible_title_verified
    assert identity.profile.kind == "web_article"


def test_mismatched_browser_title_is_not_claimed_as_verified_work_title():
    pages = sample("Actual article heading about policy\nhttps://example.org/story | 1/3")
    identity = resolve_work_identity(
        "Unrelated marketing title | Example News", Path("story.pdf"), pages,
    )
    assert identity.title == "Unrelated marketing title | Example News"
    assert not identity.visible_title_verified
    assert not identity.as_evidence()["work_title"]


def test_operator_title_override_is_preserved_without_physical_scope_change():
    pages = sample("THE EXAMPLE WEEKLY\nVOL. I.\nJUNE 11, 1900.\nNo. 17.\nAlice Smith")
    identity = resolve_work_identity(
        "Publisher workstation title", Path("issue.pdf"), pages,
        title_override="My research issue",
    )
    assert identity.title == "My research issue"
    assert identity.title_source == "user_override"
    assert identity.profile.work_scope == "whole_issue"


def test_selected_extraction_preserves_confirmed_whole_issue_abstention():
    pages = sample("THE EXAMPLE WEEKLY\nVOL. I.\nJUNE 11, 1900.\nNo. 17.\nAlice Smith")
    identity = resolve_work_identity("", Path("issue.pdf"), pages)
    recovered = recover_author_from_selected_extraction(
        [{"page": 1, "text": "Alice Smith\nBy Alice Smith"}],
        title_hint=identity.title, profile=identity.profile,
    )
    assert not recovered["author"]
    assert recovered["document_profile"]["work_scope"] == "whole_issue"


@pytest.mark.parametrize("surname,source,native,credit,expected", [
    ("Bonner", "filename_leading_surname", [], "Marita Bonner", True),
    ("Bonner", "user_override", [], "Marita Bonner", False),
    ("Bonner", "filename_leading_surname", sample("Native byline"), "Marita Bonner", False),
    ("Bonner", "filename_leading_surname", [], "Marita Hughes", False),
    ("Bonner", "filename_leading_surname", [], "Marita Bonner, John Doe", False),
])
def test_scan_filename_surname_expansion_requires_matching_first_page_byline(
    surname, source, native, credit, expected,
):
    report = {"author": credit, "source": "selected_extraction_text_byline", "page": 1}
    assert corroborates_filename_surname(surname, source, native, report) is expected


def test_scan_filename_surname_expansion_rejects_later_and_weaker_credit():
    report = {"author": "Marita Bonner", "source": "selected_extraction_text_byline", "page": 2}
    assert not corroborates_filename_surname("Bonner", "filename_leading_surname", [], report)
    report.update(page=1, source="selected_extraction_text_top_block_names")
    assert not corroborates_filename_surname("Bonner", "filename_leading_surname", [], report)
