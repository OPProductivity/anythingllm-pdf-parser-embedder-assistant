"""The company must establish publishing responsibility, not just ownership."""

import pytest
from author_metadata.contextual_roles import claims
from author_metadata.publishing_relations import select

pytestmark = pytest.mark.offline_deterministic


def samples():
    return [
        {"page": 1, "text": "A guide to contemporary manufacturing"},
        {
            "page": 2,
            "text": "N O R T H S T A R . C O M\nWe have some useful tips and insights for manufacturers.",
        },
        {
            "page": 3,
            "text": "NORTHSTAR.COM\nThree customers discuss their experiences.",
        },
        {"page": 14, "text": "© Copyright 2026, Northstar, Inc. All rights reserved."},
    ]


def proposals(values, kind="generic"):
    return [
        r
        for r in claims(values, kind=kind, work_scope="whole_work")
        if r["family"] == "corporate_brochure_domain_voice_and_legal_credit"
    ]


def test_recurring_domain_and_unquoted_publishing_voice_join_a_legal_credit():
    result = proposals(samples())
    assert len(result) == 1 and result[0]["author"] == "Northstar"
    assert result[0]["creator_type"] == "organization"
    assert result[0]["priority"] == 104


@pytest.mark.parametrize(
    "case",
    [
        "notice_only",
        "wrong_domain",
        "no_voice",
        "quoted_customer",
        "single_domain",
        "personal_copyright",
        "book",
    ],
)
def test_ownership_and_testimonial_shortcuts_are_insufficient(case):
    s = samples()
    kind = "generic"
    if case == "notice_only":
        s = s[-1:]
    elif case == "wrong_domain":
        s[2]["text"] = "DIFFERENT.COM\nA customer case study"
    elif case == "no_voice":
        s[1]["text"] = "NORTHSTAR.COM\nThe customer is the subject of this report."
    elif case == "quoted_customer":
        s[1]["text"] = (
            "NORTHSTAR.COM\n“We have some tips and insights for manufacturers,” said a customer."
        )
    elif case == "single_domain":
        s[2]["text"] = "No brand credit here"
    elif case == "personal_copyright":
        s[-1]["text"] = "© 2026 Amelia Hart. All rights reserved."
    elif case == "book":
        kind = "book"
    assert not proposals(s, kind)


def test_publisher_does_not_erase_the_explicit_individual_author():
    current = {
        "author": "Amelia Hart",
        "source": "text_byline",
        "page": 1,
        "evidence": "By Amelia Hart",
    }
    assert (
        select(current, proposals(samples()), kind="generic", work_scope="whole_work")[
            0
        ]
        == current
    )
