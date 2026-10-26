import json
from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline
from native_layout_reads import NativeLayoutReads
from rag_pdf_tools import get_backend_pages

pytestmark = pytest.mark.offline_deterministic


def make_source(path, varied_geometry=True):
    with fitz.open() as doc:
        for index in range(3):
            page = doc.new_page(width=612, height=792)
            page.insert_text((55, 70), "RUNNING AUTHOR", fontsize=9)
            for row in range(16):
                page.insert_text(
                    (55, 120 + row * 17),
                    "Left column ordinary body prose with publishing evidence.",
                    fontsize=9,
                )
                page.insert_text(
                    (320, 120 + row * 17),
                    "Right column of the same source, preserved independently.",
                    fontsize=9,
                )
            page.insert_text(
                (55, 700),
                "1 A substantive footnote remains governed by the original rules.",
                fontsize=7,
            )
            page.draw_line((55, 680), (180, 680))
            if varied_geometry and index == 1:
                page.set_rotation(90)
            if varied_geometry and index == 2:
                page.set_cropbox(fitz.Rect(20, 30, 590, 770))
        doc.save(path)


@pytest.mark.parametrize("varied_geometry", [False, True])
def test_full_page_views_and_layout_verdicts_remain_identical(
    tmp_path, varied_geometry
):
    path = tmp_path / "source.pdf"
    make_source(path, varied_geometry=varied_geometry)
    original = fitz.Page.get_textpage
    counts = []

    def track(page, *args, **kwargs):
        counts.append(page.number)
        return original(page, *args, **kwargs)

    with patch.object(fitz.Page, "get_textpage", track):
        ordinary, _, _ = get_backend_pages(path, "pymupdf", "auto")
        expected, expected_evidence = pipeline.apply_region_aware_native_layout(
            path, ordinary
        )
    assert counts == [0, 1, 2, 0, 1, 2]
    counts.clear()
    collector = NativeLayoutReads(path, pipeline._native_layout_profile)
    with patch.object(fitz.Page, "get_textpage", track):
        actual, _, _ = get_backend_pages(
            path, "pymupdf", "auto", native_layout_reads=collector
        )
        assert actual == ordinary
        actual, evidence = pipeline.apply_region_aware_native_layout(
            path, actual, native_layout_reads=collector
        )
    if varied_geometry:
        # MuPDF may mark a handle dirty while normalizing a rotated page.
        # Later pages must safely fall back rather than trust that dirty handle.
        assert 3 <= len(counts) <= 6
    else:
        assert counts == [0, 1, 2]
    assert actual == expected
    assert evidence == expected_evidence
    assert collector.values == {}
    assert all(doc.is_closed for doc in collector.documents.values())
    # No TextPage or transfer object leaks into the resulting page evidence.
    json.dumps(actual)
    json.dumps(evidence)


def test_changed_source_invalidates_values_and_other_paths_cannot_use_them(tmp_path):
    path, other = tmp_path / "source.pdf", tmp_path / "other.pdf"
    make_source(path)
    make_source(other)
    collector = NativeLayoutReads(path, pipeline._native_layout_profile)
    with fitz.open(path) as doc:
        collector.text(doc[0])
    with fitz.open(other) as doc:
        assert collector.consume(doc[0]) is None
    with path.open("ab") as handle:
        handle.write(b"\n% changed source\n")
    with fitz.open(path) as doc:
        assert collector.consume(doc[0]) is None
    assert collector.values == {} and not collector.valid


def test_changed_geometry_or_dirty_document_cannot_reuse_measurements(tmp_path):
    path = tmp_path / "source.pdf"
    make_source(path)
    collector = NativeLayoutReads(path, pipeline._native_layout_profile)
    with fitz.open(path) as doc:
        collector.text(doc[0])
    with fitz.open(path) as doc:
        doc[0].set_rotation(180)
        assert collector.consume(doc[0]) is None


def test_missing_collector_values_fall_back_to_original_layout(tmp_path):
    path = tmp_path / "source.pdf"
    make_source(path)
    pages, _, _ = get_backend_pages(path, "pymupdf", "auto")
    expected = pipeline.apply_region_aware_native_layout(path, pages)
    fresh = NativeLayoutReads(path, pipeline._native_layout_profile)
    assert (
        pipeline.apply_region_aware_native_layout(
            path, pages, native_layout_reads=fresh
        )
        == expected
    )
