from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic

TITLE = "Social Inequality and Rural Education"


def report(text, *, title=TITLE, page=1, tail=()):
    return pipeline.infer_author_from_samples_or_filename(
        [{"page": page, "text": text}, *tail], Path("article.pdf"), title_hint=title,
    )


@pytest.mark.parametrize("prefix", ["", "I\n"])
@pytest.mark.parametrize("suffix", ["", " - Academic News", " | Academic News"])
def test_dated_credit_is_native_title_corroborated_metadata(prefix, suffix):
    text = prefix + TITLE + "\nALICE MARTIN | JULY 26, 2019\nDiscussion follows."
    result = report(text, title=TITLE + suffix)
    assert result["author"] == "ALICE MARTIN"
    assert result["source"] == "text_dated_opening_byline"
    assert result["page"] == 1
    assert result["evidence"] == "ALICE MARTIN | JULY 26, 2019"


@pytest.mark.parametrize("credit", [
    "Alice Martin | February 30, 2020", "Alice Martin | July 0, 2019",
    "Alice Martin | July 32, 2019", "Alice Martin | WrongMonth 26, 2019",
    "Alice Martin | July 26", "Alice Martin | 2019",
    "Example University | July 26, 2019", "Alice Martin and Bruno Santos | July 26, 2019",
])
def test_incomplete_invalid_collective_or_institution_dated_credits_are_not_recovered(credit):
    assert pipeline.extract_dated_opening_byline([TITLE, credit], title_hint=TITLE) == []


@pytest.mark.parametrize("heading", ["Interview subjects", "Editors", "Participants"])
def test_intervening_scope_heading_breaks_title_credit_adjacency(heading):
    result = report(TITLE + "\n" + heading + "\nAlice Martin | July 26, 2019")
    assert result.get("source") != "text_dated_opening_byline"


def test_valid_dated_credit_cannot_override_stronger_explicit_byline():
    result = report(TITLE + "\nAlice Martin | July 26, 2019\nBy Sarah Coleman")
    assert result["author"] == "Sarah Coleman"


def test_dated_credit_remains_outside_post_ocr_author_allowlist():
    assert "text_dated_opening_byline" in pipeline.TRUSTED_AUTHOR_INFERENCE_SOURCES
    assert "text_dated_opening_byline" not in pipeline.POST_EXTRACTION_AUTHOR_TRUSTED_SOURCES
    result = pipeline.recover_author_from_selected_extraction([
        {"pdf_page": 1, "text": TITLE + "\nAlice Martin | July 26, 2019"},
    ], title_hint=TITLE)
    assert result.get("source") != "text_dated_opening_byline"


def test_dated_recovery_is_not_applied_to_later_pages_or_unmatched_titles():
    text = TITLE + "\nAlice Martin | July 26, 2019"
    assert report(text, page=2).get("source") != "text_dated_opening_byline"
    assert report(text, title="A Different Academic Article").get("source") != "text_dated_opening_byline"


def test_actual_title_person_and_explicit_translator_exclusions_still_apply():
    title = "The Political Thought of Alice Martin"
    assert report(title + "\nAlice Martin | July 26, 2019", title=title).get("source") != "text_dated_opening_byline"
    result = report(TITLE + "\nAlice Martin | July 26, 2019",
                    tail=[{"page": 3, "text": "Translated by Alice Martin"}])
    assert result["author"] == ""


def test_date_evidence_selects_credit_not_a_pipe_in_the_title():
    title = "School Choice | America"
    result = report(title + "\nAlice Martin | July 26, 2019", title=title)
    assert result["author"] == "Alice Martin"
    assert result["evidence"] == "Alice Martin | July 26, 2019"
