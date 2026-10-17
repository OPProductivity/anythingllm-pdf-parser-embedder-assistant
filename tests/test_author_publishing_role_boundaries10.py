"""General role-boundary controls derived from manual publishing comparisons."""

import pytest

from author_metadata.names import looks_like_person_name
from author_metadata.structured_credit import _names
from author_metadata.text_responsibility import _credit
from author_metadata.publishing_relations import (
    primary_creator_role_claims,
    select,
    visible_credit_truncation_claims,
)

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("separator", [" · ", " • ", " and ", "; "])
def test_display_roster_separates_people_and_keeps_surname_hyphens(separator):
    assert _names("Amelia Hart-Jones" + separator + "Benedict Vale") == [
        "Amelia Hart-Jones",
        "Benedict Vale",
    ]


def test_explicit_credit_preserves_two_people_separated_by_a_display_dash():
    assert _credit("Amelia Hart - Benedict Vale") == ["Amelia Hart", "Benedict Vale"]


def test_imprint_and_corporate_apparatus_end_a_complete_editor_roster():
    assert _credit(
        "Amelia Hart\nBenedict Vale\ni Palgrave\nPublishing Group\nLondon"
    ) == ["Amelia Hart", "Benedict Vale"]


def test_imprint_word_inside_a_person_without_publisher_apparatus_is_not_a_boundary():
    assert _credit("Amelia Hart\nBenedict Macmillan") == [
        "Amelia Hart",
        "Benedict Macmillan",
    ]


def test_catalogue_physical_description_is_not_part_of_the_final_person():
    assert _credit(
        "Amelia Hart, Benedict Vale. P. cm. Includes bibliographical references."
    ) == ["Amelia Hart", "Benedict Vale."]


def test_unfinished_parenthetical_title_fragment_is_incomplete_person_evidence():
    assert not looks_like_person_name("(Public Memory")
    assert looks_like_person_name("Amelia Hart-Jones")


def title_role_proposal(**changes):
    return {
        "author": "Amelia Hart, Benedict Vale",
        "page": 3,
        "source": "text_ocr_author_credit",
        "mechanism": "compact_publisher_titlepage_role_order",
        "evidence": "A Shared Public History / Amelia Hart and Benedict Vale",
        "secondary_role_boundary": {"text": "Edited by Caroline North"},
        **changes,
    }


def test_separately_credited_primary_creators_have_priority_over_edition_editor():
    found = primary_creator_role_claims(
        [title_role_proposal()], kind="book", work_scope="single_work"
    )
    editor = {
        "author": "Caroline North",
        "role": "editor",
        "work": "selected work",
        "priority": 105,
        "family": "explicit_publishing_act_span",
        "page": 3,
        "source": "text_scoped_responsibility_span",
        "evidence": "Edited by Caroline North",
    }
    verdict, _ = select({}, [editor] + found, kind="book", work_scope="single_work")
    assert verdict["author"] == "Amelia Hart, Benedict Vale"


@pytest.mark.parametrize(
    "scope,changes",
    [
        ("chapter", {}),
        ("whole_issue", {}),
        ("single_work", {"secondary_role_boundary": {}}),
        ("single_work", {"mechanism": "unrelated_title_guess"}),
    ],
)
def test_primary_creator_priority_requires_the_actual_work_and_role_relation(
    scope, changes
):
    assert not primary_creator_role_claims(
        [title_role_proposal(**changes)], kind="book", work_scope=scope
    )


def truncation_fixture(*, short="Benedict Val", visible=True, page=1):
    full = {
        "author": "Amelia Hart, Benedict Valerian",
        "role": "editor",
        "work": "selected work",
        "priority": 105,
        "family": "explicit_publishing_act_span",
        "page": 1,
        "source": "text_scoped_responsibility_span",
        "evidence": "Edited by Amelia Hart · Benedict Valerian",
        "extraction": "native_or_supplied_text",
    }
    partial = {
        **full,
        "author": "Amelia Hart, " + short,
        "page": page,
        "extraction": "native_and_ocr",
    }
    bundle = {
        "native": [
            {
                "page": 1,
                "horizontal_lines": [
                    {"text": "Edited by", "visible": visible},
                    {"text": "Amelia Hart · Benedict Valerian", "visible": visible},
                ],
            }
        ]
    }
    return [full, partial], bundle


def test_visible_full_role_credit_explains_same_page_truncated_ocr():
    claims, bundle = truncation_fixture()
    extra = visible_credit_truncation_claims(claims, bundle)
    verdict, _ = select({}, claims + extra, kind="book", work_scope="single_work")
    assert verdict["author"] == "Amelia Hart, Benedict Valerian"
    assert extra[0]["truncated_reading"] == "Amelia Hart, Benedict Val"
    assert len(claims) == 2  # Both readings remain available for scrutiny.


@pytest.mark.parametrize(
    "changes",
    [
        {"short": "Benedict Vance"},
        {"short": "Benedict Valerian"},
        {"visible": False},
        {"page": 2},
    ],
)
def test_native_text_has_no_blanket_priority_over_different_or_unverified_evidence(
    changes,
):
    claims, bundle = truncation_fixture(**changes)
    assert not visible_credit_truncation_claims(claims, bundle)


def test_confirmed_chapter_changes_scope_before_parent_editor_adjudication(tmp_path):
    import fitz
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.dispatcher import infer_author
    from author_metadata.profile import DocumentProfile

    path = tmp_path / "document.pdf"
    texts = [
        "A Shared Public History\nEdited by Caroline North and Daniel West\nUniversity Press",
        "Copyright 2024 University Press. All rights reserved.",
        "CHAPTER 3\nCollective Memory and Public Life\nAmelia Hart\nOrdinary chapter prose continues here.",
        "Amelia Hart\nThe chapter continues with additional ordinary scholarly prose.",
        "Amelia Hart\nThe concluding discussion belongs to this same chapter writer.",
    ]
    with fitz.open() as document:
        for text in texts:
            page = document.new_page()
            page.insert_text((40, 40), text, fontsize=11)
        document.save(path)
    samples = [{"page": n + 1, "text": text} for n, text in enumerate(texts)]
    profile = DocumentProfile("book", ("parent title page",), (1, 2, 3, 4, 5))
    found = infer_author(
        AuthorEvidenceContext.from_samples(
            samples,
            path=path,
            title_hint="A Shared Public History",
            profile=profile,
        )
    )
    assert found["author"] == "Amelia Hart"
    review = found["evidence_review"]
    assert review["scope_reconsideration"]["after"] == "chapter"
    editors = [c for c in review["claims"] if c.get("role") == "editor"]
    assert editors and all(c["work"] == "parent book" for c in editors)
    assert review["collection"]["complete"]


def test_contextual_role_scan_parses_an_unchanged_roster_once_per_page(
    tmp_path, monkeypatch
):
    import fitz
    from author_metadata import context_priority, field_map
    from author_metadata.context import AuthorEvidenceContext

    path = tmp_path / "document.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(path)
    groups = [
        {
            "id": str(n),
            "kind": "extractor_line",
            "text": "Ordinary scholarly prose continues here.",
        }
        for n in range(180)
    ]
    monkeypatch.setattr(
        field_map,
        "map_page",
        lambda *args: {
            "groups": groups,
            "field_candidates": [],
        },
    )
    original = context_priority._names
    seen = []

    def parse(value):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(context_priority, "_names", parse)
    current = {
        "author": "Amelia Hart, Benedict Vale",
        "source": "text_strict_credit_block",
    }
    context = AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": "A Shared History"}], path=path
    )
    counts = []
    for group_count in (30, 180):
        monkeypatch.setattr(
            field_map,
            "map_page",
            lambda *args: {
                "groups": groups[:group_count],
                "field_candidates": [],
            },
        )
        seen.clear()
        result = context_priority.infer(context, current)
        assert result["author"] == current["author"]
        counts.append(seen.count(current["author"]))
    assert counts[0] > 0 and counts[0] == counts[1]
