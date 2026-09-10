from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as p

pytestmark = pytest.mark.offline_deterministic


def row(text, x0=10, x1=20, y0=200, y1=650):
    return dict(
        text=text,
        normalized=text,
        x0=x0,
        x1=x1,
        y0=y0,
        y1=y1,
        font_sizes=[9],
        fonts=["Times"],
        spans=[],
    )


@pytest.mark.parametrize(
    "text", ["Copyright 2008. All rights reserved.", "© 2008 University Press"]
)
def test_only_repeated_vertical_notice_removed(tmp_path, text):
    source = tmp_path / "copyright.pdf"
    with fitz.open() as doc:
        for _ in range(3):
            doc.new_page(width=600, height=800)
        doc.save(source)
    central = row(text, 100, 500, 200, 210)
    body = row(
        "A chapter discussing copyright must not lose its content.", 100, 500, 300, 312
    )
    rows = [[central, body], [row(text), body], [row(text), body]]
    with patch.object(p, "_layout_line_rows", side_effect=lambda pg: rows[pg.number]):
        pages, evidence = p.apply_region_aware_native_layout(
            source, [dict(page=n, text="original") for n in range(1, 4)]
        )
    assert text in pages[0]["text"]
    assert all(text not in pg["text"] for pg in pages[1:])
    assert all(body["text"] in pg["text"] for pg in pages)
    assert all(pg["raw_text"] == "original" for pg in pages)
    assert all(
        ev["removed_marginalia"][0]["reason"] == "repeated_vertical_copyright_notice"
        for ev in evidence["pages"][1:]
    )


def test_duplicate_on_one_page_does_not_establish_repetition(tmp_path):
    source = tmp_path / "one-page.pdf"
    with fitz.open() as doc:
        doc.new_page(width=600, height=800)
        doc.save(source)
    notice = row("Copyright 2008. All rights reserved.")
    with patch.object(p, "_layout_line_rows", return_value=[notice, dict(notice)]):
        pages, _ = p.apply_region_aware_native_layout(
            source, [dict(page=1, text="original")]
        )
    assert notice["text"] in pages[0]["text"]


@pytest.mark.parametrize(
    "candidate",
    [
        row("Copyright 2008. All rights reserved.", 100, 500, 200, 210),
        row("Copyright 2008. All rights reserved.", 280, 290),
        row("An analysis of copyright 2008 and its implications."),
        row("Author biography and institutional affiliation"),
    ],
)
def test_copyright_lookalikes_preserved(candidate):
    assert not p._layout_vertical_copyright_key(candidate, 600)


def test_only_confirmed_marginalia_does_not_resurrect_raw_page(tmp_path):
    source = tmp_path / "blank-publisher-page.pdf"
    with fitz.open() as doc:
        for _ in range(2):
            doc.new_page(width=600, height=800)
        doc.save(source)
    notice = row("Copyright 1999. All rights reserved.")
    footer = row("Repeated publisher download information", 100, 500, 760, 770)
    rows = [
        [notice, footer, row("Actual first-page body text.", 100, 500, 300, 312)],
        [dict(notice), dict(footer)],
    ]
    with patch.object(p, "_layout_line_rows", side_effect=lambda pg: rows[pg.number]):
        pages, _ = p.apply_region_aware_native_layout(
            source,
            [
                dict(page=1, text="Original first page"),
                dict(page=2, text="Raw publisher notice"),
            ],
        )
    assert pages[0]["text"] == "Actual first-page body text."
    assert pages[1]["text"] == "" and pages[1]["raw_text"] == "Raw publisher notice"
    assert pages[1]["layout_marginalia_only_page"]


def test_unexplained_empty_layout_keeps_original_fallback(tmp_path):
    source = tmp_path / "uncertain.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.save(source)
    with patch.object(p, "_layout_line_rows", return_value=[]):
        pages, _ = p.apply_region_aware_native_layout(
            source, [dict(page=1, text="Original recoverable text")]
        )
    assert pages[0]["text"] == "Original recoverable text"
    assert not pages[0].get("layout_marginalia_only_page")
