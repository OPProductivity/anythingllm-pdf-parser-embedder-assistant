import pytest

from semantic_segmentation import detect_page_transition, split_semantic_page
from segmentation_policy import policy_for


pytestmark = pytest.mark.offline_deterministic


def test_page_limit_prefers_sentence_boundary_and_preserves_offsets():
    text = (
        "The first sentence establishes context. "
        "The second sentence contains enough detail for retrieval. "
        "The third sentence closes the argument cleanly."
    )
    rows = split_semantic_page(text, target=70, hard_limit=100, mode="page_limit")

    assert len(rows) >= 2
    assert all(len(row["text"]) <= 100 for row in rows)
    assert all(text[row["char_start_page"]:row["char_end_page"]].strip() == row["text"] for row in rows)
    assert rows[0]["boundary_debug"]["winner_kind"] == "sentence"


def test_passages_and_page_limit_use_distinct_drift_policies():
    text = ("Alpha beta gamma delta. " * 30).strip()
    page_limit = split_semantic_page(text, target=100, hard_limit=160, mode="page_limit")
    passages = split_semantic_page(text, target=100, hard_limit=160, mode="passages")

    assert all(len(row["text"]) <= 160 for row in page_limit + passages)
    assert policy_for("page_limit").target_drift_fraction != policy_for("passages").target_drift_fraction
    assert policy_for("page_limit").semantic_priority > policy_for("passages").semantic_priority


def test_page_preserving_automatic_ignores_target_until_safety_ceiling():
    text = ("A complete sentence for one source page. " * 30).strip()

    automatic = split_semantic_page(text, target=80, hard_limit=4_000, mode="page_limit")
    shorter = split_semantic_page(text, target=80, hard_limit=4_000, mode="page_passages")

    assert len(automatic) == 1
    assert automatic[0]["text"] == text
    assert len(shorter) > 1
    assert "page_end" not in shorter[0]["boundary_debug"]["reason"]
    assert all(row["char_start_page"] < row["char_end_page"] for row in shorter)


def test_none_mode_keeps_the_full_page_as_one_prepared_record():
    text = "First section.\n\nSecond section." + (" More content." * 80)

    rows = split_semantic_page(text, target=100, hard_limit=160, mode="none")

    assert len(rows) == 1
    assert rows[0]["text"] == text
    assert rows[0]["boundary_debug"]["reason"] == "no_local_segmentation"


def test_page_mode_keeps_safe_page_whole():
    text = "One complete page of text. It remains a page parent."
    rows = split_semantic_page(text, target=20, hard_limit=100, mode="page")

    assert len(rows) == 1
    assert rows[0]["text"] == text


def test_transition_detects_and_reconstructs_hyphenated_page_boundary():
    row = detect_page_transition(
        "The field was dominated by U.S. historiography domi-",
        "nant at that time. Turner reasoned that the frontier mattered.",
        64,
        65,
        "wray",
    )

    assert row["continuation_detected"] is True
    assert "dominant at that time." in row["reconstructed_text"]
    assert row["boundary_id"] == "wray-b064-065-tr01"
    assert row["upload_eligible"] is False


def test_transition_non_event_is_manifest_only():
    row = detect_page_transition(
        "This page ends with a complete sentence.",
        "A new section begins independently.",
        10,
        11,
        "book",
    )

    assert row["continuation_detected"] is False
    assert row["reconstructed_text"] == ""


def test_page_end_retention_keeps_last_complete_sentence_in_final_segment():
    text = (
        ("Lead material with words. " * 3)
        + "Final word word word word. "
        + "dangling fragment"
    )

    rows = split_semantic_page(text, target=45, hard_limit=55, mode="page_limit")

    assert len(rows) >= 2
    assert all(len(row["text"]) <= 55 for row in rows)
    assert "Final word word word word." in rows[-1]["text"]
    assert rows[-1]["boundary_debug"].get("retained_last_complete_sentence") is True
