from copy import deepcopy
from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as p

pytestmark = pytest.mark.offline_deterministic


def row(text, y, x=40, width=500, size=8):
    return dict(
        text=text,
        normalized=text,
        x0=x,
        x1=x + width,
        y0=y,
        y1=y + size,
        font_sizes=[size],
        fonts=["Times"],
        spans=[],
    )


def prose_reference(number="1", size=11):
    r = row("The body contains a genuine raised source reference.", 500, size=size)
    r["spans"] = [
        dict(text=r["text"], size=size, y1=r["y1"]),
        dict(text=number, size=size - 2, y1=r["y1"] - 3),
    ]
    return r


def evaluate(rows, rules=(), body=8):
    original = deepcopy(rows)
    excluded, groups, candidates = p._layout_note_groups(rows, 600, 800, body, rules)
    assert rows == original
    assert excluded <= {id(r) for r in rows}
    assert len(excluded) == sum(g["line_count"] for g in groups)
    return excluded, groups


@pytest.mark.parametrize("start", [600, 608, 620, 650])
def test_single_reference_note_above_and_below_old_cutoff(start):
    rows = [
        prose_reference(),
        row("1 A source note with enough content to establish a block.", start),
        row("The same note continues below, with no second numbered note.", start + 12),
        row("Its final line is still part of that footnote.", start + 24),
    ]
    excluded, groups = evaluate(rows)
    assert excluded == {id(r) for r in rows[1:]}
    assert groups[0]["reason"] == "body_reference_confirmed_footnote_block"


def test_separate_number_and_hanging_continuations_keep_source_identity():
    rows = [
        prose_reference(),
        row("1", 606, x=60, width=5),
        row(
            "This note is separated from its number in the PDF text stream.",
            606,
            x=75,
            width=430,
        ),
        row("This continuation hangs left of the numbered first line.", 618),
        row("The final line reaches the traditional lower-note band.", 630),
    ]
    assert evaluate(rows)[0] == {id(r) for r in rows[1:]}


@pytest.mark.parametrize(
    "fault",
    ["", "no_reference", "same_font", "not_raised", "too_short", "too_high", "no_gap"],
)
def test_single_line_note_still_requires_independent_body_evidence(fault):
    rows = [
        prose_reference(),
        row("1 A sufficiently long source citation on a single lower-page line.", 640),
    ]
    if fault == "no_reference":
        rows[0]["spans"] = []
    if fault == "same_font":
        rows[1]["font_sizes"] = [11]
    if fault == "not_raised":
        rows[0]["spans"][-1]["y1"] = rows[0]["y1"]
    if fault == "too_short":
        rows[1]["normalized"] = rows[1]["text"] = "1 See this."
    if fault == "too_high":
        rows[1]["y0"] = 600
        rows[1]["y1"] = 608
    if fault == "no_gap":
        rows.insert(
            1,
            row(
                "A larger-font paragraph ends immediately above this line.",
                628,
                size=11,
            ),
        )
    assert (id(rows[-1]) in evaluate(rows)[0]) == (not fault)


@pytest.mark.parametrize("fault", ["", "copyright", "body_font", "gap", "other_column"])
def test_separator_can_complete_only_a_contiguous_small_note_block(fault):
    first = row("An earlier footnote continues here without its original marker.", 580)
    second = row(
        "The continuation remains below the separator and above note one.", 592
    )
    if fault == "copyright":
        first["text"] = first["normalized"] = "Copyright 2026. All rights reserved."
    if fault == "body_font":
        first["font_sizes"] = [11]
    if fault == "gap":
        first["y0"] = 560
        first["y1"] = 568
    if fault == "other_column":
        first["x0"] = 340
        first["x1"] = 550
    rows = [
        prose_reference(),
        first,
        second,
        row("1 This independently referenced note establishes the lower block.", 610),
        row("Its continuation reaches the lower footnote band.", 622),
    ]
    rule_y = 552 if fault == "gap" else 572
    excluded, _ = evaluate(rows, [(40, rule_y, 550)])
    assert id(rows[3]) in excluded
    assert (id(first) in excluded) == (not fault)
    if fault != "other_column":
        assert (id(second) in excluded) == (not fault)


@pytest.mark.parametrize(
    "fault",
    [
        "no_reference",
        "not_raised",
        "same_size",
        "different_number",
        "body_sized_note",
        "large_gap",
    ],
)
def test_reference_lookalikes_cannot_broaden_removal(fault):
    rows = [
        prose_reference(),
        row("1 A numbered lower-page paragraph that needs positive evidence.", 600),
        row("Its continuation is not a license to guess that this is a footnote.", 612),
        row("The ending of this block.", 624),
    ]
    if fault == "no_reference":
        rows[0]["spans"] = []
    if fault == "not_raised":
        rows[0]["spans"][-1]["y1"] = rows[0]["y1"]
    if fault == "same_size":
        rows[0]["spans"][-1]["size"] = 11
    if fault == "different_number":
        rows[0]["spans"][-1]["text"] = "2"
    if fault == "body_sized_note":
        rows[1]["font_sizes"] = [11]
    if fault == "large_gap":
        rows[-1]["y0"] = 680
        rows[-1]["y1"] = 688
    assert id(rows[1]) not in evaluate(rows)[0]


def test_thin_separator_supplies_gap_evidence_without_deleting_other_column():
    rows = [
        prose_reference(),
        row("Body ends just above the note.", 599, size=11),
        row(
            "1 This left-column footnote is clearly smaller than the referenced prose.",
            612,
            width=220,
        ),
        row(
            "Its continuation reaches the lower band and is still a footnote.",
            624,
            width=220,
        ),
        row(
            "Right column body at the same height must remain intact.",
            620,
            x=340,
            width=220,
            size=11,
        ),
    ]
    assert not evaluate(rows)[0]
    assert evaluate(rows, [(40, 610, 120)])[0] == {id(rows[2]), id(rows[3])}


@pytest.mark.parametrize(
    "heading",
    [
        "Contents",
        "Learning outcomes",
        "Learning outcomes of the course unit",
        "Works cited",
    ],
)
def test_numbered_lists_and_end_matter_are_not_notes(heading):
    rows = [
        row(heading, 400, size=12),
        row("1. A numbered entry in a real content list.", 640),
        row("This entry has an ordinary continuation.", 652),
    ]
    assert not evaluate(rows, body=12)[0]


@pytest.mark.parametrize("number", ["33", "2001", "88.1"])
def test_numeric_grid_cells_are_not_folios(number):
    rows = [
        row(number, 725, width=20),
        row("2002", 725, x=100, width=20),
        row("1999", 740, width=20),
        row("28", 740, x=100, width=20),
    ]
    original = deepcopy(rows)
    assert p._layout_numeric_table_cells(rows, 800) == {id(r) for r in rows}
    assert rows == original


@pytest.mark.parametrize("noise_size,peer_size", [(18.5, 9), (6.25, 8)])
def test_margin_noise_with_different_font_is_not_a_table(noise_size, peer_size):
    rows = [
        row("9", 725, width=20, size=noise_size),
        row("416", 725, x=100, width=20, size=peer_size),
        row("417", 740, width=20, size=peer_size),
    ]
    assert id(rows[0]) not in p._layout_numeric_table_cells(rows, 800)


def test_isolated_page_number_not_protected_by_nearby_prose():
    rows = [
        row("9", 730, width=10),
        row("Some nearby prose", 730, x=100, width=200),
        row("8", 748, width=10),
    ]
    assert not p._layout_numeric_table_cells(rows, 800)


def test_repeated_means_distinct_pages_and_raw_content_is_preserved(tmp_path):
    source = tmp_path / "repetition.pdf"
    with fitz.open() as doc:
        doc.new_page(width=600, height=800)
        doc.new_page(width=600, height=800)
        doc.save(source)
    page_rows = {
        0: [
            row("Table label", 40),
            row("Table label", 55),
            row("Running header", 75),
            row("Body", 300),
        ],
        1: [row("Running header", 75), row("Second body", 300)],
    }
    with patch.object(
        p, "_layout_line_rows", side_effect=lambda page: page_rows[page.number]
    ):
        pages, ev = p.apply_region_aware_native_layout(
            source,
            [dict(page=1, text="original first"), dict(page=2, text="original second")],
        )
    assert pages[0]["text"].count("Table label") == 2
    assert all("Running header" not in page["text"] for page in pages)
    assert [page["raw_text"] for page in pages] == ["original first", "original second"]


def test_separator_rectangle_is_optional_evidence():
    class Page:
        rect = fitz.Rect(0, 0, 600, 800)

        def get_drawings(self):
            return [dict(items=[("re", fitz.Rect(40, 610, 120, 610.5), 1)])]

    assert p._layout_note_separator_rules(Page(), [row("1 Source note.", 612)])[0] == [
        (40, 610, 120)
    ]
