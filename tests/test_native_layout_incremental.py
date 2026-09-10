from collections import Counter
from copy import deepcopy
from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


def row(text, x, y, width=120, size=10):
    return {"text": text, "normalized": text, "x0": x, "y0": y,
            "x1": x + width, "y1": y + size, "font_sizes": [size], "fonts": ["Times"]}


def refined(rows, mode="visual_line_order"):
    original = deepcopy(rows)
    ordered = sorted(rows, key=lambda r: (r["y0"], r["x0"]))
    result, mode, reasons = pipeline._layout_refine_reading_order(rows, ordered, mode, 600, 800)
    assert Counter(map(id, result)) == Counter(map(id, rows))
    assert rows == original
    return "\n".join(r["text"] for r in result), mode, reasons


def test_keyword_sidebar_is_contiguous_but_body_is_not_deleted():
    rows = [row("A B S T R A C T", 200, 100), row("Keywords:", 30, 120),
            row("Geometry", 30, 132), row("Reading order", 30, 144),
            row("First abstract sentence", 200, 120, 330), row("Second abstract sentence", 200, 132, 330)]
    text, _, reasons = refined(rows)
    assert "Keywords:\nGeometry\nReading order\nA B S T R A C T\nFirst abstract sentence\nSecond abstract sentence" == text
    assert reasons == ["keyword_sidebar"]


@pytest.mark.parametrize("change", ["no_abstract", "wide_keyword", "distant", "one_keyword"])
def test_keyword_lookalikes_are_unchanged(change):
    rows = [row("ABSTRACT", 200, 100), row("Keywords:", 30, 120),
            row("Geometry", 30, 132), row("Reading order", 30, 144)]
    if change == "no_abstract":
        rows[0]["normalized"] = "Introduction"
    elif change == "wide_keyword":
        rows[1]["x1"] = 300
    elif change == "distant":
        rows[0]["y0"] = 10
    else:
        rows.pop()
    assert refined(rows)[2] == []


def test_reference_zone_does_not_interleave_author_biographies():
    rows = [row("Alpha, A. (2020). A reference", 30, 50, 220),
            row("Reference continuation", 30, 63, 220), row("15-18.", 30, 76, 80),
            row("https://doi.org/example", 320, 50, 220),
            row("Beta, B. (2021). Another reference", 320, 63, 220), row("Publisher.", 320, 76),
            row("About the Authors", 220, 110, 170), row("A biography across the page", 30, 140, 530)]
    text, _, reasons = refined(rows)
    assert text.index("15-18.") < text.index("https://doi.org") < text.index("Beta,") < text.index("About the Authors")
    assert reasons == ["short_reference_zone"]
    rows[3]["text"] = "No DOI evidence"
    assert refined(rows)[2] == []


def test_correspondence_moved_after_both_columns_not_removed():
    rows = [row("First body", 30, 100, 240), row("Right body", 320, 100, 230),
            row("All correspondence should be directed to the author", 30, 650, 240, 9),
            row("name@example.test", 30, 662, 240, 9), row("Journal published by the society", 30, 674, 240, 9),
            row("Right body continuation", 320, 680, 230)]
    text, _, reasons = refined(rows, "two_column_column_first")
    assert text.index("Right body continuation") < text.index("All correspondence")
    assert reasons == ["correspondence_tail"]
    rows[3]["text"] = "ordinary sentence"
    assert refined(rows, "two_column_column_first")[2] == []


def test_fragment_words_cannot_establish_columns():
    rows = [row(f"word{i}", 30 + (i % 10)*50, 50 + (i//10)*25, 30) for i in range(30)]
    rows += [row("A full width reference sentence", 30, y, 520) for y in (140, 160, 180)]
    assert refined(rows, "two_column_column_first")[1:] == ("visual_line_order", ["fragmented_single_column"])
    for r in rows[:30]:
        r["normalized"] = "This is genuine column prose with multiple words"
    assert refined(rows, "two_column_column_first")[2] == []


def chars(space_start=3, next_start=6, ligature="ﬁ", space_end=5.5, next_char="n"):
    return [{"c": ligature, "bbox": (0, 0, 6, 10), "origin": (0, 8)},
            {"c": " ", "bbox": (space_start, 0, space_end, 10), "origin": (space_start, 8)},
            {"c": next_char, "bbox": (next_start, 0, next_start+5, 10), "origin": (next_start, 8)}]


@pytest.mark.parametrize("ligature", ["ﬀ", "ﬁ", "ﬂ", "ﬃ", "ﬄ", "ﬅ", "ﬆ"])
def test_overlapping_space_repaired_without_letter_substitution(ligature):
    source = chars(ligature=ligature)
    original = deepcopy(source)
    assert pipeline._layout_ligature_space_repair(source, 10) == (ligature+"n", 1)
    assert source == original


@pytest.mark.parametrize("kwargs", [
    {"space_start": 6, "space_end": 8.5, "next_start": 9},
    {"space_start": -1}, {"next_start": 12}, {"next_start": 3},
    {"ligature": "f"}, {"next_char": "2"}, {"next_char": ","},
])
def test_real_word_spaces_and_ambiguous_geometry_stay(kwargs):
    source = chars(**kwargs)
    assert pipeline._layout_ligature_space_repair(source, 10) == ("".join(c["c"] for c in source), 0)


def test_page_without_ligature_signal_does_not_request_extra_geometry():
    class Page:
        def get_text(self, *args, **kwargs):
            raise AssertionError("Unneeded raw character inspection")
    pipeline._layout_repair_ligature_spans(Page(), [{"type":0,"lines":[{"spans":[{"text":"Ordinary text"}]}]}])


def test_fresh_native_parse_observes_changed_source_at_same_path(tmp_path):
    path = tmp_path / "source.pdf"
    for text in ("First source version", "Second source version"):
        with fitz.open() as doc:
            doc.new_page().insert_text((72,72), text)
            doc.save(path)
        pages, _, _ = tools.get_backend_pages(path, "pymupdf", "hi_res")
        assert text in pages[0]["text"]


def test_checkpoint_from_different_assistant_version_is_not_reused(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source identity fixture")
    version = tmp_path / "VERSION"
    version.write_text("0.5.4", encoding="utf-8")
    runtime = {"backend_module_origin": "fixture", "tesseract_executable": ""}
    with patch.object(tools, "package_resource_path", return_value=version):
        tools.save_unstructured_ocr_checkpoint(source, "hi_res", runtime, tmp_path / "run",
                                               [{"page":1,"text":"Earlier OCR"}], 1, [])
        assert tools.load_unstructured_ocr_checkpoint(source, "hi_res", runtime, tmp_path / "run") is not None
        version.write_text("0.5.5", encoding="utf-8")
        assert tools.load_unstructured_ocr_checkpoint(source, "hi_res", runtime, tmp_path / "run") is None
