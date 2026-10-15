"""Ground-truth scoring must not hide missing people or contaminated names."""

import pytest
from benchmarks.author_portability_eval import complete_matches

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize(
    "actual,expected,match",
    [
        ("Gomez, Amina Rao", "Amina Rao; Gomez", True),
        ("Pietari Kaapa", "Pietari Kääpä", True),
        ("Conformism Pietari Kääpä", "Pietari Kääpä", False),
        ("Gomez", "Laura E. Gómez", False),
        ("Amina Mullins", "Amina Mullins; Gabriel Mullins", False),
        ("张伟", "李明", False),
        ("张伟", "张伟", True),
        ("", "", True),
    ],
)
def test_complete_credits(actual, expected, match):
    assert complete_matches(actual, expected) is match
