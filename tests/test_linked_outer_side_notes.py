from copy import deepcopy

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def _row(text, x0, x1, y0, size=10, spans=None):
    return {
        "text": text, "normalized": text, "x0": x0, "x1": x1,
        "y0": y0, "y1": y0 + size, "font_sizes": [size],
        "fonts": ["Times"], "spans": spans or [],
    }


def _page():
    body = [_row(f"Body prose continues across this complete line {index}.",
                 50, 470, 100 + 15 * index) for index in range(16)]
    body[7] = _row("With funding, Title 12 high school students continue.", 50, 470, 198,
                   spans=[
                       {"text": "With funding, Title 1", "x0": 50, "x1": 120,
                        "y0": 200, "y1": 210, "size": 10},
                       {"text": "2", "x0": 120.5, "x1": 124,
                        "y0": 198, "y1": 206, "size": 6},
                       {"text": " high school students continue.", "x0": 124,
                        "x1": 470, "y0": 200, "y1": 210, "size": 10},
                   ])
    notes = [
        _row("2 Title 1 schools have many students.", 520, 580, 200, 7),
        _row("The note continues outside the prose.", 520, 580, 210, 7),
        _row("Its last line belongs to the same note.", 520, 580, 220, 7),
    ]
    return body, notes


def test_linked_outer_note_keeps_content_and_marks_raised_reference():
    body, notes = _page()
    rows = body + notes
    original = deepcopy(rows)
    ordered = body[:8] + notes + body[8:]
    revised, replacements, starts, details = pipeline._layout_linked_outer_side_notes(
        rows, ordered, "two_column_column_first", 600, 800,
    )
    assert revised == body + notes
    assert replacements == {id(body[7]): "With funding, Title 1[2] high school students continue."}
    assert starts == {id(notes[0])}
    assert details[0]["marker"] == "2"
    assert details[0]["line_count"] == 3
    assert rows == original


@pytest.mark.parametrize("change", ["wrong_number", "unraised", "duplicate", "inside_body"])
def test_outer_note_ambiguity_does_not_change_text_or_order(change):
    body, notes = _page()
    if change == "wrong_number":
        notes[0]["text"] = notes[0]["normalized"] = "3 Title 1 schools have many students."
    elif change == "unraised":
        body[7]["spans"][1]["size"] = 10
    elif change == "duplicate":
        body[8] = deepcopy(body[7])
    else:
        for note in notes:
            note["x0"], note["x1"] = 430, 490
    rows = body + notes
    ordered = body[:8] + notes + body[8:]
    revised, replacements, starts, details = pipeline._layout_linked_outer_side_notes(
        rows, ordered, "two_column_column_first", 600, 800,
    )
    assert revised == ordered
    assert not replacements and not starts and not details


def test_single_column_does_not_relocate_outer_rows():
    body, notes = _page()
    rows = body + notes
    assert pipeline._layout_linked_outer_side_notes(
        rows, rows, "single_column_top_to_bottom", 600, 800,
    ) == (rows, {}, set(), [])
