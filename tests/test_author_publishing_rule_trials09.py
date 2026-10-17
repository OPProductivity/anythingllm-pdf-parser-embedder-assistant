"""Publishing relationships found in manual review, with invented names and negatives."""

import pytest
from author_metadata.text_responsibility import _credit, claims
from author_metadata.publishing_relations import opening_byline_claims, select

pytestmark = pytest.mark.offline_deterministic


def test_blank_display_spacing_after_a_comma_continues_the_whole_roster():
    text = "Amelia Hart,\n\nBenedict Vale\n\n& Caroline North\nUniversity Press"
    assert _credit(text) == ["Amelia Hart", "Benedict Vale", "Caroline North"]


def test_blank_paragraph_without_a_roster_separator_still_ends_credit():
    assert _credit("Amelia Hart\n\nBenedict Vale") == ["Amelia Hart"]


def test_two_displayed_editor_rosters_do_not_conflict_after_continuation():
    samples = [
        {
            "page": 1,
            "text": "Common Life and Shared Memory\nEdited by\nAmelia Hart,\n\nBenedict Vale\n\n& Caroline North",
        },
        {
            "page": 4,
            "text": "Common Life and Shared Memory\nEdited by Amelia Hart, Benedict Vale, and Caroline North\nUniversity Press",
        },
    ]
    found = claims(
        samples,
        kind="book",
        work_scope="single_work",
        title_hint="Common Life and Shared Memory",
        opening_pages=[1, 4],
    )
    selected, reason = select({}, found, kind="book", work_scope="single_work")
    assert selected["author"] == "Amelia Hart, Benedict Vale, Caroline North"
    assert "conflict" not in reason


def physical_claim(
    *, body_y=100, kind="scholarly_article", work_scope="article", page=1
):
    proposals = [
        {
            "author": "Amelia Hart, Benedict Vale",
            "page": page,
            "mechanism": "physical_heading_byline_body",
            "evidence": "A Shared History / Amelia Hart, Benedict Vale",
            "geometry": {"byline": [20, 60, 200, 80]},
        }
    ]
    bundle = {
        "native": [
            {
                "page": 1,
                "horizontal_lines": [
                    {
                        "text": "Abstract: We examine public memory.",
                        "box": [20, body_y, 500, body_y + 15],
                    }
                ],
            }
        ]
    }
    return opening_byline_claims(proposals, bundle, kind=kind, work_scope=work_scope)


def test_abstract_bound_opening_roster_challenges_a_later_section_heading():
    found = physical_claim()
    current = {
        "author": "Social Memory",
        "page": 4,
        "source": "text_structured_opening_credit",
    }
    selected, _ = select(current, found, kind="scholarly_article", work_scope="article")
    assert selected["author"] == "Amelia Hart, Benedict Vale"


@pytest.mark.parametrize(
    "settings",
    [
        {"body_y": 20},
        {"kind": "book_chapter"},
        {"work_scope": "whole_issue"},
        {"page": 4},
    ],
)
def test_same_geometry_without_selected_article_boundary_is_not_enough(settings):
    assert not physical_claim(**settings)


def test_explicit_prominent_byline_allows_a_short_title_without_filename_help():
    from author_metadata.portable_layout import layout_credit_reports

    lines = [
        {"text": "The Journey", "box": [120, 50, 320, 80], "size": 30},
        {"text": "By Amelia Hart", "box": [130, 90, 300, 110], "size": 18},
        {
            "text": "The ordinary story starts here and continues in several printed columns.",
            "box": [20, 125, 430, 140],
            "size": 10,
        },
        {
            "text": "Its fictional speakers are the subjects of this prose, not additional credited writers.",
            "box": [20, 145, 430, 160],
            "size": 10,
        },
    ]
    reports = list(layout_credit_reports(lines, 450, 700))
    assert any(r["author"] == "Amelia Hart" for r in reports)


def test_short_title_does_not_admit_a_small_body_byline_fragment():
    from author_metadata.portable_layout import layout_credit_reports

    lines = [
        {"text": "The Journey", "box": [120, 50, 320, 80], "size": 30},
        {"text": "By Amelia Hart", "box": [130, 300, 300, 310], "size": 8},
        {
            "text": "The ordinary story starts here and continues in several printed columns.",
            "box": [20, 320, 430, 340],
            "size": 10,
        },
        {
            "text": "Its fictional speakers are subjects of this prose and not credited writers.",
            "box": [20, 345, 430, 360],
            "size": 10,
        },
    ]
    assert not list(layout_credit_reports(lines, 450, 700))


def test_body_lines_do_not_trigger_quadratic_prefix_name_parsing(monkeypatch):
    from author_metadata import native_article

    names = native_article._names
    called = []

    def count(value):
        called.append(value)
        return names(value)

    monkeypatch.setattr(native_article, "_names", count)
    counts = []
    for body_count in (80, 240):
        called.clear()
        lines = [
            {
                "text": "Public Memory and Shared History",
                "box": [30, 20, 500, 45],
                "size": 24,
                "baseline": 45,
            }
        ]
        lines += [
            {
                "text": "Ordinary prose continues here without an author affiliation or an abstract label.",
                "box": [30, 70 + i * 14, 500, 82 + i * 14],
                "size": 10,
                "baseline": 82 + i * 14,
            }
            for i in range(body_count)
        ]
        assert not list(native_article.credit_reports(lines, 600, 4000))
        counts.append(len(called))
    assert 0 < counts[0] and counts[1] <= counts[0] * 4
