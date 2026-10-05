from collections import Counter

import pytest

from pdf_layout_geometry import verified_page_order

pytestmark = pytest.mark.offline_deterministic


def row(text, x0, x1, y0):
    return {"text": text, "normalized": text, "x0": x0, "x1": x1,
            "y0": y0, "y1": y0 + 12, "font_sizes": [10], "fonts": ["Times"], "spans": [],
            "native_ligature_space_repairs": 0}


def test_off_centre_gutter_places_complete_left_column_first():
    rows = [row(f"Left column has a complete academic sentence number {index}.", 140, 345, 150 + index * 17)
            for index in range(26)]
    rows += [row(f"Right column has a complete academic sentence number {index}.", 358, 625, 150 + index * 17)
             for index in range(26)]
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, reason, profile = verified_page_order(rows, 651, 800, baseline, "two_column_column_first")
    assert (kind, reason, profile) == ("two_column_column_first", "measured_column_gutter", "two_column")
    assert [item["text"].split()[0] for item in ordered] == ["Left"] * 26 + ["Right"] * 26
    assert Counter(map(id, ordered)) == Counter(map(id, rows))


def test_three_prose_tracks_are_not_forced_through_two_column_rule():
    rows = [row(f"Column {column} has a complete and useful prose sentence {index}.", x, x + 152,
                310 + index * 15)
            for column, x in enumerate((81, 249, 417), start=1) for index in range(25)]
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, reason, profile = verified_page_order(rows, 629, 811, baseline, "two_column_column_first")
    assert ordered == baseline
    assert (kind, reason, profile) == ("two_column_column_first", "three_column_layout_needs_review", "three_column")


def test_sustained_single_column_rejoins_inline_fragments_without_losing_characters():
    rows = [row(f"A full width academic sentence has prose at position {index}.", 90, 515, y)
            for index, y in enumerate((100, 130, 160, 300, 330, 360, 520, 550, 580))]
    rows += [row("The text contains an", 90, 245, 425), row("italic phrase here.", 247, 430, 425)]
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, reason, profile = verified_page_order(rows, 600, 800, baseline, "two_column_column_first")
    assert (kind, reason, profile) == ("visual_line_order", "sustained_single_column_inline_join", "single_column")
    assert any("The text contains an italic phrase here." == item["text"] for item in ordered)
    old = Counter(char for item in rows for char in item["text"] if not char.isspace())
    new = Counter(char for item in ordered for char in item["text"] if not char.isspace())
    assert old == new


def test_ambiguous_page_keeps_existing_order():
    rows = [row(f"Left ordinary scholarly prose sentence {index}.", 50, 270, 320 + index * 15)
            for index in range(12)]
    rows += [row("A wide title or abstract line with enough prose to be visible.", 50, 530, y)
             for y in (100, 120, 140)]
    baseline = list(reversed(rows))
    assert verified_page_order(rows, 600, 800, baseline, "two_column_column_first") == (
        baseline, "two_column_column_first", "", "unresolved"
    )
