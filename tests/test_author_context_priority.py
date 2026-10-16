"""Decision-level coverage for context-dependent specialist priorities."""

import pytest

from author_metadata.context_priority import rank_candidates

pytestmark = pytest.mark.offline_deterministic


def credit(name="Amina Rao", source="text_byline"):
    return {"author": name, "source": source}


def role(kind, confidence=0.8):
    return {"author": "Amina Rao", "role": kind, "confidence": confidence}


def test_footnote_does_not_overrule_author_consensus_on_shared_inputs():
    current = credit()
    result = rank_candidates(
        current,
        [("title_linked_author", credit(), 88)],
        role_evidence=[role("footnote")],
    )
    assert result["author"] == "Amina Rao"
    row = result["context_priority"]["candidates"][0]
    assert row["score"] == 102
    assert row["contributions"][0]["contribution"] == 0


def test_same_cues_gain_different_weight_when_they_explicitly_credit_translator():
    current = credit(source="filename_explicit_byline")
    result = rank_candidates(current, [], role_evidence=[role("translator", 0.98)])
    assert not result["author"]
    assert result["context_priority"]["candidates"][0]["score"] < 40


def test_separate_title_linked_author_credit_outranks_another_translation_role():
    result = rank_candidates(
        credit(),
        [("title_linked_author", credit(), 88)],
        role_evidence=[role("translator", 0.98)],
    )
    assert result["author"] == "Amina Rao"
    assert result["context_priority"]["candidates"][0]["score"] > 80


def test_editor_role_is_valid_for_whole_book_but_not_chapter():
    assert (
        rank_candidates(
            credit(), [], role_evidence=[role("editor", 0.98)], whole_book=True
        )["author"]
        == "Amina Rao"
    )
    assert not rank_candidates(credit(), [], role_evidence=[role("editor", 0.98)])[
        "author"
    ]


def test_copyright_region_is_not_a_copyright_holder_author_exclusion():
    assert (
        rank_candidates(credit(), [], role_evidence=[role("copyright_region", 0.99)])[
            "author"
        ]
        == "Amina Rao"
    )


def test_ambiguous_competing_work_credits_preserve_classic_verdict():
    current = credit()
    assert (
        rank_candidates(
            current, [("title_linked_author", credit("Gabriel Santos"), 88)]
        )["author"]
        == "Amina Rao"
    )


def test_title_linked_credit_can_recover_an_unknown():
    result = rank_candidates(
        {"author": "", "source": "not_found"}, [("title_linked_author", credit(), 88)]
    )
    assert result["author"] == "Amina Rao"
    assert result["context_priority"]["score_kind"] == "heuristic_decision_points"


def test_region_labels_can_overlap_on_the_same_atoms():
    from author_metadata.context_priority import region_scores

    roles = dict(
        region_scores(
            {"text": "1 Copyright Amina Rao", "bbox": [30, 740, 500, 770]},
            {"height": 800},
        )
    )
    assert set(roles) == {"footnote", "footer", "copyright_region"}


def test_opening_title_retry_uses_catalog_structure_not_name_list():
    from author_metadata.physical_work import opening_title

    title = opening_title(
        [
            "Cities and Public Memory",
            "Amina Rao; Gabriel Santos",
            "Example Journal, Vol. 12, No. 4",
            "Stable URL:",
            "https://jstor.org/example",
        ]
    )
    assert title == "Cities and Public Memory"
    assert not opening_title(
        [
            "Collected Essays on Memory",
            "Edited by Amina Rao",
            "Introduction",
            "Some ordinary text.",
        ]
    )


def test_quoted_physical_title_can_precede_a_prominent_byline():
    from author_metadata.portable_layout import layout_credit

    def line(text, y, size, left=50, right=500):
        return {"text": text, "box": [left, y, right, y + size], "size": size}

    result = layout_credit(
        [
            line("Rethinking the “Classical” Account", 80, 20),
            line("Gabriel Santos", 115, 15),
            line(
                "The article traces how public institutions shape cultural memory.",
                155,
                10,
            ),
            line(
                "Its argument draws on historical evidence from several cities.",
                170,
                10,
            ),
            line(
                "The following sections examine the social consequences of those choices.",
                185,
                10,
            ),
        ],
        600,
        800,
    )
    assert result["author"] == "Gabriel Santos"


def test_same_size_title_and_author_affiliation_pairs_keep_name_like_subtitle():
    from author_metadata.native_article import alternating_credit

    def line(text, y):
        return {
            "text": text,
            "box": [60, y, 500, y + 12],
            "size": 12,
            "baseline": y + 12,
        }

    lines = [
        line("Culture and Memory across Communities", 60),
        line("Public Entanglements", 80),
        line("Amina Rao, Jr.", 120),
        line("Example University", 140),
        line("Gabriel Santos", 170),
        line("Department of History, Example College", 190),
        line("Abstract", 230),
    ]
    assert (
        alternating_credit(lines, 600, 800)["author"]
        == "Amina Rao, Jr., Gabriel Santos"
    )
    lines.insert(6, line("Unparsed Contributor", 210))
    assert not alternating_credit(lines, 600, 800)["author"]
