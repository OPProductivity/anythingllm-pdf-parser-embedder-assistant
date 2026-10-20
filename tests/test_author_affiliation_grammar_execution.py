"""Keep literal affiliation boundaries when executing the grammar once."""
import pytest

from author_metadata.constants import AUTHOR_AFFILIATION_HINTS
from author_metadata.names import has_author_affiliation_hint

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("hint", sorted(AUTHOR_AFFILIATION_HINTS))
def test_every_literal_affiliation_term_retains_its_boundary(hint):
    assert has_author_affiliation_hint("(" + hint.upper() + ")")
    assert not has_author_affiliation_hint("prefix" + hint + "suffix")


def test_person_names_and_long_prose_do_not_become_affiliations():
    assert not has_author_affiliation_hint("Celia Pressley")
    assert has_author_affiliation_hint("Department of History, Example University")
    assert not has_author_affiliation_hint("This is a university " + "discussion " * 40)
