"""Transport and computation changes must preserve publishing evidence."""

from dataclasses import replace
import io
import itertools
import re
from threading import Event

import fitz
from PIL import Image
import pytest

from author_metadata import evidence_collection as acquisition
from author_metadata.context import AuthorEvidenceContext
from author_metadata.portable_book import _opening_layouts
from author_metadata.portable_layout import _native_page
from author_metadata.publishing_acts import frontmatter_samples

pytestmark = pytest.mark.offline_deterministic


def test_planning_and_native_read_forwarding_are_request_local(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    with fitz.open() as doc:
        for _ in range(7):
            page = doc.new_page()
            page.insert_text((40, 60), "A sufficiently ordinary article introduction")
        doc.save(path)
    original = fitz.Page.get_text
    calls = []

    def tracked(page, *args, **kwargs):
        calls.append(page.number)
        return original(page, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", tracked)
    with fitz.open(path) as doc:
        readings = {}
        first = acquisition._plan(doc, native_text=readings)
        second = acquisition._plan(doc, native_text=readings)
        assert first == second
        assert calls == list(range(5))
        acquisition._plan(doc)
        assert calls == list(range(5)) * 2


def test_ocr_transport_preserves_every_rendered_rgb_pixel(tmp_path):
    path = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=200, height=250)
        page.draw_rect((10, 10, 190, 230), color=(0.2, 0.4, 0.8))
        page.insert_text((25, 90), "Amelia Hart", fontsize=18)
        doc.save(path)
    task = {"page": 1, "clip": [0, 0, 200, 250], "reason": "sparse_native"}
    with fitz.open(path) as doc:
        prepared = acquisition._prepare_region(doc, task, factors=(2.0,))
        image = Image.open(io.BytesIO(prepared.frames[2.0]["payload"]))
        pixels = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        assert image.mode == "RGB"
        assert image.size == (pixels.width, pixels.height)
        assert image.tobytes() == pixels.samples


def test_recognition_can_begin_before_the_remaining_batch_is_rendered(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    with fitz.open() as doc:
        for _ in range(2):
            page = doc.new_page(width=200, height=250)
            page.insert_text((25, 90), "Amelia Hart")
        doc.save(path)
    started = Event()
    prepare = acquisition._prepare_region

    def checked_prepare(document, task, **kwargs):
        if task["page"] == 2:
            assert started.wait(2), "first OCR waited for the entire render batch"
        return prepare(document, task, **kwargs)

    def recognize(prepared, task, executable):
        started.set()
        return [{"page": task["page"], "text": "Amelia Hart"}], [
            {"kind": "ocr", **task, "status": "read"}]

    monkeypatch.setattr(acquisition, "_prepare_region", checked_prepare)
    monkeypatch.setattr(acquisition, "_read_region", recognize)
    tasks = [{"page": n, "clip": [0, 0, 200, 250], "reason": "sparse_native"}
             for n in (1, 2)]
    with fitz.open(path) as doc:
        results = list(acquisition._read_regions(doc, tasks, "unused", workers=2))
    assert [task["page"] for task, _, _ in results] == [1, 2]


def test_unexpected_render_error_releases_an_earlier_retry_waiter(monkeypatch):
    waiting, released = Event(), Event()

    def prepare(document, task, **kwargs):
        if task["page"] == 2:
            assert waiting.wait(2)
            raise KeyError("unexpected render failure")
        return acquisition._RasterRegion(100, 100, {})

    def recognize(prepared, task, executable):
        waiting.set()
        try:
            prepared.request_frame(2.0)
        finally:
            released.set()

    monkeypatch.setattr(acquisition, "_prepare_region", prepare)
    monkeypatch.setattr(acquisition, "_read_region", recognize)
    tasks = [{"page": n, "clip": [0, 0, 100, 100], "reason": "sparse_native"}
             for n in (1, 2)]
    with pytest.raises(KeyError, match="unexpected render failure"):
        list(acquisition._read_regions(None, tasks, "unused", workers=2))
    assert released.is_set()


def test_experts_receive_the_same_acquired_native_text_and_geometry(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "The Work Title", fontsize=22)
        page.insert_text((40, 100), "Amelia Hart", fontsize=14)
        doc.save(path)
    with fitz.open(path) as doc:
        native = {"page": 1, "text": doc[0].get_text("text", sort=True),
                  "layout_lines": _native_page(doc[0]), "height": doc[0].rect.height}
    context = AuthorEvidenceContext.from_samples([], path=path)
    standalone = frontmatter_samples(context)
    layouts = list(_opening_layouts(context))
    acquired = replace(context, evidence_bundle={"native": [native], "trace": {
        "opening_plan": [1], "document_page_count": 1}})
    assert frontmatter_samples(acquired) == standalone
    assert list(_opening_layouts(acquired)) == layouts

    def fail(*args, **kwargs):
        raise AssertionError("acquired geometry was read from PDF again")

    monkeypatch.setattr("author_metadata.portable_book._native_page", fail)
    assert list(_opening_layouts(acquired)) == layouts
    # A standalone call still has a real extraction path, not cross-call reuse.
    with pytest.raises(AssertionError, match="read from PDF again"):
        list(_opening_layouts(context))


@pytest.mark.parametrize("old,new", [
    (r"\s+(?:and|&)\s+|\s*[;·•]\s*|\s*,\s*",
     r"(?<!\s)\s+(?:and|&)\s+|(?<!\s)\s+[;·•,]\s*|[;·•,]\s*"),
    (r"\s+and\s+|\s*;\s*", r"(?<!\s)\s+and\s+|(?<!\s)\s+;\s*|;\s*"),
    (r"\s+[–—-]\s+", r"(?<!\s)\s+[–—-]\s+"),
])
def test_roster_separators_keep_wrapped_and_consecutive_boundaries(old, new):
    atoms = ("Amelia", "Hart", "and", "&", ";", ",", "·", "—", "-", " ", "\n", "\t", "\r\n ")
    for parts in itertools.product(atoms, repeat=4):
        text = "".join(parts)
        assert re.split(old, text, flags=re.I) == re.split(new, text, flags=re.I), repr(text)
