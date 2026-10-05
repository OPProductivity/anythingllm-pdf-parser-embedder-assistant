from pathlib import Path

import pytest

from author_metadata.pdf import recover_author_from_selected_extraction
from author_metadata.work_identity import resolve_work_identity


pytestmark = pytest.mark.offline_deterministic


def sample(text):
    return [{"page": 1, "text": text}]


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
