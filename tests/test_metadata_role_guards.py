from author_metadata.dispatcher import infer_author_from_samples
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
    report = infer_author_from_samples(
        samples, Path("Jane Doe - Example Article.pdf"), title_hint="Example Article",
    )
    assert report["author"] == ""
    assert report["source"] == "unresolved_issue_editor_role"
    assert report["evidence"] == "Editors / Jane Doe"


def test_explicit_article_author_still_wins_in_an_issue():
    report = infer_author_from_samples(
        [{"page": 1, "text": "Issue 1 (36), 2025\nExample Article\nBy Jane Doe"},
         {"page": 2, "text": "Editors\nSarah Smith"}],
        Path("article.pdf"), title_hint="Example Article",
    )
    assert report["author"] == "Jane Doe"
    assert report["source"] == "text_byline"


def test_work_level_edited_by_credit_remains_available():
    report = infer_author_from_samples(
        [{"page": 1, "text": "Collected Essays\nEdited by Jane Doe"}],
        Path("collected-essays.pdf"), title_hint="Collected Essays",
    )
    assert report["author"] == "Jane Doe"
    assert report["source"] == "text_edited_by"


def test_affiliated_book_reviewers_survive_filename_title_suffix():
    title = "Book Review - Everybody Lives Near Appalachia - Ricky Mullins, Brooke Mullins"
    lines = [
        "Book Review",
        "Everybody Lives Near Appalachia: Examining a Book's Impact",
        "Ricky Mullins, Example University",
        "Brooke Mullins, Example University",
        "Book Reviewed: Hillbilly Elegy, by J. D. Vance",
    ]
    report = pipeline.infer_author_from_text_samples(
        [{"page": 1, "text": "\n".join(lines)}], title_hint=title,
    )
    assert report["author"] == "Ricky Mullins, Brooke Mullins"
    assert report["source"] == "text_opening_title_block_byline"


@pytest.mark.parametrize("heading", ["Interviewees", "Participants", "Contributors", "Editors", "Reviewers"])
def test_book_review_role_heading_does_not_promote_affiliated_people(heading):
    lines = [
        "Book Review",
        "A Close Reading of a Public Book",
        heading,
        "Alice Martin, Example University",
        "Bob Lee, Example University",
        "Book Reviewed: Example Book, by Jane Writer",
    ]
    assert pipeline.extract_opening_title_block_byline(lines) == []


def test_book_review_without_review_title_does_not_promote_affiliated_people():
    lines = [
        "Book Review",
        "Alice Martin, Example University",
        "Bob Lee, Example University",
        "Book Reviewed: Example Book, by Jane Writer",
    ]
    assert pipeline.extract_opening_title_block_byline(lines) == []


@pytest.mark.parametrize(
    ("heading", "reviewed_work"),
    [
        ("BOOK REVIEWS", "Book Reviewed: A Public Book, by Jane Writer"),
        ("A Review Essay on Public Culture", "Work Reviewed: A Public Book, by Jane Writer"),
        ("Documentary Film Review", "Film Reviewed: A Public Film, directed by Jane Writer"),
        ("Exhibition review: Images of the City", "Exhibition Reviewed: Images, curated by Jane Writer"),
    ],
)
def test_review_heading_variants_keep_a_bounded_affiliated_byline(heading, reviewed_work):
    lines = [
        heading,
        "A Close Reading of Public Culture",
        "Alice Martin, Example University",
        "Bob Lee, Example University",
        reviewed_work,
    ]
    assert pipeline.extract_opening_title_block_byline(lines) == ["Alice Martin", "Bob Lee"]


def test_wrapped_explicit_review_credit_collects_both_affiliated_people():
    lines = [
        "Exhibition Review",
        "Disobedient Bodies at the Museum",
        "Reviewed by Sarah Walker, Nottingham Trent University and Ania Sadkowska,",
        "Coventry University",
        "Overview",
    ]
    names, evidence = pipeline.extract_affiliated_review_credit(lines)
    assert names == ["Sarah Walker", "Ania Sadkowska"]
    assert "Coventry University" in evidence


def test_peer_reviewers_and_curators_are_not_explicit_review_authors():
    peer_review = [
        "Research Article",
        "A Review of Public Culture",
        "Reviewed by Sarah Walker, Nottingham Trent University and Ania Sadkowska,",
        "Coventry University",
    ]
    curators = [
        "Exhibition Review",
        "Disobedient Bodies at the Museum",
        "Curated by Sarah Walker, Nottingham Trent University and Ania Sadkowska,",
        "Coventry University",
    ]
    assert pipeline.extract_affiliated_review_credit(peer_review)[0] == []
    assert pipeline.extract_affiliated_review_credit(curators)[0] == []


def test_exhibition_heading_is_not_part_of_a_reviewer_name():
    assert not pipeline.looks_like_person_name("Exhibition Review")
    assert pipeline.normalize_author_candidate("> Louis Rogers") == "Louis Rogers"


def test_reviewed_book_authors_do_not_outrank_the_review_author():
    samples = [
        {"page": 1, "text": (
            "Comparative policy agendas: a review essay\n"
            "Policy agendas in Australia, by Keith Dowding and Aaron Martin, Basel,\n"
            "Publisher details\n"
            "Jonathan Drew\n"
            "To cite this article: Jonathan Drew (2020)\n"
        )},
        {"page": 2, "text": (
            "BOOK REVIEW\n"
            "Comparative policy agendas: a review essay\n"
            "Jonathan Drew\n"
            "School of Humanities and Social Sciences, Deakin University\n"
            "Policy agendas in Australia, by Keith Dowding and Aaron Martin\n"
        )},
    ]
    report = pipeline.infer_author_from_text_samples(
        samples, title_hint="Comparative policy agendas: a review essay",
    )
    assert report["author"] == "Jonathan Drew"
