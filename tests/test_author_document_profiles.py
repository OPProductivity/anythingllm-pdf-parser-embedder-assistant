from author_metadata.dispatcher import infer_author_from_samples
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
from author_metadata.profile import classify_document


pytestmark = pytest.mark.offline_deterministic


def test_review_with_reviewed_book_is_not_routed_as_book():
    samples = [{"page": 1, "text": (
        "Book Review\nA Study of Shared Histories\nRicky Mullins, Example University\n"
        "Brooke Mullins, Example University\nBook Reviewed: Another Book, by J. D. Vance\n"
        "ISBN 9781234567890\nCopyright 2020"
    )}]
    assert classify_document(samples, "A Study of Shared Histories").kind == "review"


def test_report_prepared_by_is_separate_from_prepared_for():
    samples = [{"page": 1, "text": (
        "Technical Report No. 14\nAn Evaluation of Public Libraries\n"
        "Prepared by\nAlice Smith\nPrepared for\nJohn Jones\nExecutive Summary"
    )}]
    assert classify_document(samples, "An Evaluation of Public Libraries").kind == "report"
    result = infer_author_from_samples(
        samples, Path("report.pdf"), title_hint="An Evaluation of Public Libraries",
    )
    assert result["author"] == "Alice Smith"
    assert result["source"] == "text_report_prepared_by"


def test_book_frontmatter_allows_later_matching_title_page():
    samples = [
        {"page": 1, "text": "The Hidden Politics of Public Memory"},
        {"page": 2, "text": (
            "The Hidden Politics of Public Memory\nJane Author\nOxford University Press"
        )},
        {"page": 3, "text": "Copyright 2024\nISBN 9781234567890\nContents"},
    ]
    title = "The Hidden Politics of Public Memory -- Jane Author"
    assert classify_document(samples, title).kind == "book"
    result = pipeline.infer_author_from_text_samples(samples, title_hint=title)
    assert result["author"] == "Jane Author"
    assert result["source"] == "text_titlepage_publisher_byline"
    assert result["page"] == 2


def test_book_title_fragment_and_series_editor_do_not_become_author():
    samples = [
        {"page": 1, "text": "The Hidden Politics of Public Memory"},
        {"page": 2, "text": (
            "The Hidden Politics of Public Memory\nSeries Editors\nJane Author\nOxford University Press"
        )},
        {"page": 3, "text": "Copyright 2024\nISBN 9781234567890\nContents"},
    ]
    result = pipeline.infer_author_from_text_samples(samples, title_hint="The Hidden Politics of Public Memory")
    assert result["author"] != "Jane Author"


def test_ambiguous_keyword_title_does_not_select_special_route():
    samples = [{"page": 1, "text": "Annual Report Analysis\nAbstract\nAlice Smith\nThe study examines reports."}]
    assert classify_document(samples, "Annual Report Analysis").kind == "scholarly_article"


def test_web_article_layout_is_distinct_from_report_and_book():
    samples = [{"page": 1, "text": (
        "Duncan Green\nMarch 3rd, 2025\nWhy should you be interested in a new blog on activism?\n"
        "A discussion of public work."
    )}]
    assert classify_document(samples, "Why should you be interested in a new blog on activism?").kind == "web_article"


def test_profile_never_claims_complete_document_scope_from_sampled_pages():
    profile = classify_document([{"page": 1, "text": "Book Review\nBook Reviewed: Example"}])
    assert profile.sampled_pages == (1,)
    assert not profile.scope_complete
