"""Avoid a measured regex cost without narrowing publication classification."""

import itertools
import re

import pytest

from author_metadata import profile

pytestmark = pytest.mark.offline_deterministic

PREVIOUS = r"(?im)^\s*(?:J Eng Educ\.|[A-Za-z ]+\s+\d{4};\d{1,3}:\d+[-–]\d+)"


@pytest.mark.parametrize(
    "text",
    [
        "J Eng Educ.",
        "j eng educ. continuation",
        "J\tEng Educ.",
        "Journal Name 2024;12:123-130",
        "Journal Name\n2024;12:123–130",
        " 2024;12:1-2",
        "2024;12:1-2",
        "JournalName2024;12:1-2",
        "Journal Name 2024 ;12:1-2",
        "Journal Name 2024;1234:1-2",
        "Journal Name 2024;12:1-",
        "Journal Name 2024;12:1—2",
        "Journal\tName 2024;12:1-2",
        "Journal\t2024;12:1-2",
        "in text. Journal 2024;12:1-2",
        "K 2024;12:1-2",
        "İ ٢٠٢٤;١٢:١٢–٢٠",
        "É 2024;12:1-2",
        "An unrelated cover",
        "",
    ],
)
def test_citation_prerequisite_preserves_previous_language(text):
    for prefix, suffix in itertools.product(
        ("", "\n", " \t", "\u00a0"), ("", "\nbody")
    ):
        value = prefix + text + suffix
        assert profile._scholarly_footer_citation(value) == bool(
            re.search(PREVIOUS, value)
        )


def test_whitespace_cover_never_enters_expensive_expression(monkeypatch):
    search = profile.re.search
    seen = []

    def checked(pattern, value, flags=0):
        seen.append(pattern)
        assert pattern != PREVIOUS, (
            "Absent required citation syntax must bypass backtracking"
        )
        return search(pattern, value, flags)

    monkeypatch.setattr(profile.re, "search", checked)
    value = "\n".join(" " * 200 for _ in range(20)) + "\nAn unrelated title page"
    assert not profile._scholarly_footer_citation(value)
    assert seen
