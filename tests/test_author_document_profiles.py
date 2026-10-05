from author_metadata.dispatcher import infer_author_from_samples
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
from author_metadata.profile import classify_document
from author_metadata.pdf import (
    infer_author_from_initial_pdf_pages, selected_extraction_author_samples,
    recover_author_from_selected_extraction,
)


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


def test_browser_print_is_not_scholarly_from_body_abstract_or_reference_doi():
    samples = [{"page": 1, "text": (
        "Here is what we are working on\nMozilla\nAbstract images are available.\n"
        "https://example.org/article | 1/3"
    )}, {"page": 3, "text": "References\nDOI: 10.1000/example"}]
    profile = classify_document(samples, "Here is what we are working on")
    assert profile.kind == "web_article"
    assert profile.publication_type == "browser_print_article"


def test_whole_issue_suppresses_title_adjacent_person_guess():
    samples = [{"page": 1, "text": (
        "THE EXAMPLE WEEKLY\nVOL. I.\nMONDAY, JUNE 11, 1900.\nNo. 1 7.\n"
        "A Person's Name Appears in a Story\nAlice Jones"
    )}]
    result = infer_author_from_samples(samples, Path("example-weekly.pdf"))
    assert result["source"] == "whole_issue_no_single_author"
    assert not result["author"]


def test_numbered_chapter_roles_do_not_become_book_editor_credit():
    samples = [{"page": 1, "text": "An Edited Book\nEdited by Example Editor"},
               {"page": 3, "text": "5. Preparing for the field\nCoordinated by Adam Boyette\nContributors: Dorsa Amir"}]
    result = infer_author_from_samples(samples, Path("chapter.pdf"))
    assert result["source"] == "chapter_roles_not_resolved"
    assert not result["author"]


def test_pipeline_exports_the_canonical_pdf_author_helpers():
    assert pipeline.infer_author_from_initial_pdf_pages is infer_author_from_initial_pdf_pages
    assert pipeline.selected_extraction_author_samples is selected_extraction_author_samples


def test_selected_extraction_records_the_document_profile():
    result = recover_author_from_selected_extraction([{
        'page': 1,
        'text': 'Research on Social Inequality\nAlice Martin\nExample University\nAbstract\nResearch follows.',
    }], title_hint='Research on Social Inequality')
    assert result['document_profile']['kind'] == 'scholarly_article'
    assert result['document_profile']['classification_pages'] == [1]
    assert result['document_profile']['sampled_pages'] == [1]
    assert result['document_profile']['scope_complete'] is False


@pytest.mark.parametrize("citation_credit,citation_title,expected", [
    ("Sara Riva", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border", "Sara Riva"),
    ("Another Person", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border", ""),
    ("Sara Riva", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Borderlands", ""),
])
def test_truncated_scholarly_pdf_title_needs_matching_publisher_citation(
    citation_credit, citation_title, expected,
):
    visible = "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border"
    title = visible[:-2]
    sample = {"page": 1, "text": (
        "Journal of Immigrant & Refugee Studies\n"
        "Tracing Invisibility as a Colonial Project: Indigenous\n"
        "Women Who Seek Asylum at the U.S.-Mexico\n"
        "Border\nSara Riva\n"
        f"To cite this article: {citation_credit} (2021): {citation_title}\n"
        "DOI: 10.1000/example"
    )}
    result = infer_author_from_samples([sample], Path("article.pdf"), title_hint=title)
    assert result["author"] == expected


def test_book_stacked_authors_are_one_credit_not_conflicting_suffixes():
    samples = [{"page": 1, "text": (
        "Research on Social Inequality\nAlice Martin\nBruno Santos\nUniversity Press"
    )}]
    result = infer_author_from_samples(
        samples, Path("book.pdf"), title_hint="Research on Social Inequality",
    )
    assert result["author"] == "Alice Martin, Bruno Santos"
    assert result["source"] == "text_titlepage_publisher_byline"


def test_book_conflicting_title_pages_still_abstain():
    samples = [
        {"page": 1, "text": "Research on Social Inequality\nBy\nAlice Martin\nUniversity Press"},
        {"page": 3, "text": "Research on Social Inequality\nBy\nBruno Santos\nUniversity Press"},
    ]
    result = infer_author_from_samples(
        samples, Path("book.pdf"), title_hint="Research on Social Inequality",
    )
    assert result["author"] == ""
    assert result["source"] == "conflicting_work_credits"


def test_book_conflict_cannot_be_overridden_by_filename_byline():
    samples = [
        {"page": 1, "text": "Shared Histories\nBy\nAlice Smith\nUniversity Press"},
        {"page": 3, "text": "Shared Histories\nBy\nBob Jones\nUniversity Press"},
    ]
    result = infer_author_from_samples(
        samples, Path("Shared-Histories-by-Alice-Smith.pdf"), title_hint="Shared Histories",
    )
    assert result["author"] == ""
    assert result["source"] == "conflicting_work_credits"
    assert "page 1: Alice Smith" in result["evidence"]
    assert "page 3: Bob Jones" in result["evidence"]


def test_book_missing_credit_still_allows_explicit_filename_byline():
    samples = [{"page": 1, "text": "Praise for Shared Histories\nUniversity Press"}]
    result = infer_author_from_samples(
        samples, Path("Shared-Histories-by-Alice-Smith.pdf"), title_hint="Shared Histories",
    )
    assert result["author"] == "Alice Smith"
    assert result["source"] == "filename_explicit_byline"


def test_selected_extraction_cannot_override_conflicting_book_credits():
    pages = [
        {"page": 1, "text": "Shared Histories\nBy\nAlice Smith\nUniversity Press"},
        {"page": 3, "text": "Shared Histories\nBy\nBob Jones\nUniversity Press"},
    ]
    result = recover_author_from_selected_extraction(pages, title_hint="Shared Histories")
    assert result["author"] == ""
    assert result["source"] == "selected_extraction_conflicting_work_credits"
