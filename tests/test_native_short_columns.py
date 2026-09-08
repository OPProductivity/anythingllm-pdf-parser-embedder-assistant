from collections import Counter

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def column_rows(right_start=294, count=12):
    return [dict(text=f"{side} ordinary academic prose with several meaningful words {i}",
                 x0=x, x1=x + 230, y0=65 + i * 12, y1=77 + i * 12)
            for i in range(count) for side, x in [("left", 47), ("right", right_start)]]


@pytest.mark.parametrize("right_start,count", [(294, 40), (312, 12), (294, 12)])
def test_offset_and_short_columns_preserve_every_row(right_start, count):
    rows = column_rows(right_start, count)
    ordered, mode, regions = pipeline._layout_reading_order(rows, 594, 792)
    assert mode == "two_column_column_first"
    assert regions is None
    assert Counter(map(id, rows)) == Counter(map(id, ordered))
    assert [r["text"].split()[0] for r in ordered] == ["left"] * count + ["right"] * count


def test_short_columns_keep_title_and_reference_heading():
    rows = column_rows()
    title = dict(text="Full width title", x0=80, x1=520, y0=20, y1=35)
    heading = dict(text="References", x0=294, x1=370, y0=49, y1=61)
    ordered = pipeline._layout_reading_order([title, heading] + rows, 594, 792)[0]
    assert ordered[0] is title
    assert ordered[13] is heading


@pytest.mark.parametrize("case", ["single", "table", "overlap", "crossing", "too_short", "nonconcurrent", "three_columns"])
def test_ambiguous_layouts_do_not_take_new_path(case):
    rows = column_rows()
    if case == "single":
        rows = rows[::2]
    elif case == "table":
        rows = [dict(r, text="Revenue 123.45") for r in rows]
    elif case == "overlap":
        rows = column_rows(260)
    elif case == "crossing":
        rows.append(dict(text="Full width body sentence across both columns", x0=47, x1=550, y0=120, y1=132))
    elif case == "too_short":
        rows = column_rows(count=5)
    elif case == "nonconcurrent":
        rows = [dict(r, y0=r["y0"] + 250, y1=r["y1"] + 250) if r["x0"] == 294 else r for r in rows]
    else:
        rows = [dict(r, x1=r["x0"] + 140) for r in rows]
        rows += [dict(r, x0=215, x1=355) for r in rows[::2]]
    assert pipeline._layout_short_or_offset_columns(rows, 594, 792) is None


def test_real_pdf_geometry_uses_production_native_path(tmp_path):
    source = tmp_path / "two-columns.pdf"
    with pipeline.fitz.open() as doc:
        page = doc.new_page(width=594, height=792)
        for i in range(12):
            for label, x in [("Left", 47), ("Right", 294)]:
                page.insert_text((x, 65 + i * 12), f"{label} academic prose has several meaningful words {i}.", fontsize=9)
        doc.save(source)
    from rag_pdf_tools import get_pages_with_pymupdf
    pages, _ = get_pages_with_pymupdf(source)
    result, evidence = pipeline.apply_region_aware_native_layout(source, pages)
    assert evidence["two_column_page_count"] == 1
    lines = result[0]["text"].splitlines()
    assert all(line.startswith("Left") for line in lines[:12])
    assert all(line.startswith("Right") for line in lines[12:])
    assert Counter(result[0]["text"].split()) == Counter(pages[0]["text"].split())
