"""Shared computations preserve independent interpretations and input scope."""
import itertools
import re

import pytest

from author_metadata import book, field_map
from author_metadata.lexical_scope import lexical_scope

pytestmark = pytest.mark.offline_deterministic


def atom(index, x, y, text="Amelia", block=0, height=12):
    return {"id": f"p1-n{index}", "text": text, "x0": x, "x1": x + 30,
            "y0": y, "y1": y + height, "font_size": 12,
            "source_block": block, "source_line": int(y)}


def test_book_lexical_results_are_fresh_lists_and_never_cross_inferences(monkeypatch):
    original = book.split_author_line_candidates
    calls = []

    def tracked(value, **kwargs):
        calls.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(book, "split_author_line_candidates", tracked)
    for _ in range(2):
        with lexical_scope():
            names = book._names("Amelia Hart")
            assert names == ["Amelia Hart"]
            names.append("Different person")
            assert book._names("Amelia Hart") == ["Amelia Hart"]
    assert calls == ["Amelia Hart", "Amelia Hart"]
    book._names("Amelia Hart")
    assert len(calls) == 3


def test_repeated_membership_preserves_kinds_and_independent_geometry(monkeypatch):
    atoms = [atom(0, 20, 10), atom(1, 54, 10, "Hart")]
    original = field_map._composition
    calls = []

    def tracked(atoms, indexes, kind):
        calls.append(tuple(indexes))
        return original(atoms, indexes, kind)

    monkeypatch.setattr(field_map, "_composition", tracked)
    groups = field_map._line_compositions(atoms, 600)
    same = [g for g in groups if g["atom_ids"] == ["p1-n0", "p1-n1"]]
    assert {g["kind"] for g in same} == {"extractor_line", "gap_split_line", "aligned_line"}
    assert calls.count((0, 1)) == 1
    assert len({g["id"] for g in same}) == 3
    assert all(g["text"] == "Amelia Hart" and g["bbox"] == [20, 10, 84, 22] for g in same)
    same[0]["bbox"][0] = -100
    same[0]["atom_ids"].append("foreign")
    assert all(g["bbox"][0] == 20 and len(g["atom_ids"]) == 2 for g in same[1:])


def test_baseline_window_keeps_the_earliest_eligible_row():
    # A tall atom can overlap two existing rows. Keep the first qualifying row,
    # rather than the nearest row or most recently created row.
    atoms = [atom(0, 20, 0, "First", block=0, height=12),
             atom(1, 20, 7, "Second", block=1, height=12),
             atom(2, 55, 2.68, "Shared", block=2, height=12)]
    groups = field_map._line_compositions(atoms, 600)
    aligned = [g["atom_ids"] for g in groups if g["kind"] == "aligned_line"]
    assert ["p1-n0", "p1-n2"] in aligned
    assert ["p1-n1"] in aligned


def test_responsibility_suffix_retains_whitespace_and_word_boundaries():
    old = r"\s+(?:editors?|authors?)\.?$"
    new = r"(?<!\s)\s+(?:editors?|authors?)\.?$"
    atoms = ("Amelia Hart", "author", "authors", "editor", "editors.", "Authoring", " ", "\t", "\n", "\u2003")
    for parts in itertools.product(atoms, repeat=4):
        value = "".join(parts)
        assert re.sub(old, "", value, flags=re.I) == re.sub(new, "", value, flags=re.I), repr(value)
