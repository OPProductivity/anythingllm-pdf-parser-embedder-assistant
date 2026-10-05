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


def test_three_prose_tracks_follow_each_column_without_losing_rows():
    rows = [row(f"Column {column} has a complete and useful prose sentence {index}.", x, x + 152,
                310 + index * 15)
            for column, x in enumerate((81, 249, 417), start=1) for index in range(25)]
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, reason, profile = verified_page_order(rows, 629, 811, baseline, "two_column_column_first")
    assert [item["text"].split()[1] for item in ordered] == [str(number) for number in (1, 2, 3) for _ in range(25)]
    assert Counter(map(id, ordered)) == Counter(map(id, rows))
    assert (kind, reason, profile) == ("three_column_column_first", "verified_three_column_tracks", "three_column")


def test_three_column_title_precedes_body_and_wide_caption_follows_it():
    rows = [row(f"Column {column} has a complete and useful prose sentence {index}.", x, x + 152,
                310 + index * 15)
            for column, x in enumerate((81, 249, 417), start=1) for index in range(25)]
    title = {**row("A large title across these three columns.", 81, 548, 100), "font_sizes": [25]}
    caption = {**row("Figure caption spanning two columns of the page.", 249, 565, 250), "font_sizes": [7]}
    rows.extend((title, caption))
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, reason, profile = verified_page_order(rows, 629, 811, baseline, "two_column_column_first")
    assert ordered[0] is title
    assert ordered[-1] is caption
    assert [item["text"].split()[1] for item in ordered[1:-1]] == [str(number) for number in (1, 2, 3) for _ in range(25)]
    assert Counter(map(id, ordered)) == Counter(map(id, rows))
    assert (kind, reason, profile) == ("three_column_column_first", "verified_three_column_tracks", "three_column")


def test_three_column_sustained_large_callout_does_not_split_a_body_sentence():
    rows = [row(f"Column {column} has a complete and useful prose sentence {index}.", x, x + 152,
                310 + index * 15)
            for column, x in enumerate((81, 249, 417), start=1) for index in range(25)]
    callout = [
        {**row(f"Large display quotation line {index}.", 249, 400, 385 + index * 17),
         "font_sizes": [16]}
        for index in range(4)
    ]
    rows.extend(callout)
    baseline = sorted(rows, key=lambda item: (item["y0"], item["x0"]))
    ordered, kind, _, _ = verified_page_order(rows, 629, 811, baseline, "two_column_column_first")
    assert kind == "three_column_column_first"
    assert [item["text"].split()[1] for item in ordered[:-4]] == [
        str(number) for number in (1, 2, 3) for _ in range(25)
    ]
    assert ordered[-4:] == callout
    assert Counter(map(id, ordered)) == Counter(map(id, rows))


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
