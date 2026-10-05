from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("furniture", ["Publication Date", "CRITICAL PAPER"])
def test_document_furniture_is_not_an_author_name(furniture):
    assert not pipeline.looks_like_person_name(furniture)
    assert pipeline.looks_like_person_name("Jane Paper")


def test_repository_cover_name_is_not_a_person():
    assert not pipeline.looks_like_person_name("Kent Academic Repository")


@pytest.mark.parametrize("issue_header", ["Issue 1 (36), 2025", "Volume One / Number Five"])
def test_generic_issue_editor_cannot_become_article_author(issue_header):
    samples = [
        {"page": 1, "text": issue_header + "\nExample Article"},
        {"page": 2, "text": "Editors\nJane Doe\nOther material"},
    ]
    report = pipeline.infer_author_from_samples_or_filename(
        samples, Path("Jane Doe - Example Article.pdf"), title_hint="Example Article",
    )
    assert report["author"] == ""
    assert report["source"] == "unresolved_issue_editor_role"
    assert report["evidence"] == "Editors / Jane Doe"


def test_explicit_article_author_still_wins_in_an_issue():
    report = pipeline.infer_author_from_samples_or_filename(
        [{"page": 1, "text": "Issue 1 (36), 2025\nExample Article\nBy Jane Doe"},
         {"page": 2, "text": "Editors\nSarah Smith"}],
        Path("article.pdf"), title_hint="Example Article",
    )
    assert report["author"] == "Jane Doe"
    assert report["source"] == "text_byline"


def test_work_level_edited_by_credit_remains_available():
    report = pipeline.infer_author_from_samples_or_filename(
        [{"page": 1, "text": "Collected Essays\nEdited by Jane Doe"}],
        Path("collected-essays.pdf"), title_hint="Collected Essays",
    )
    assert report["author"] == "Jane Doe"
    assert report["source"] == "text_edited_by"
