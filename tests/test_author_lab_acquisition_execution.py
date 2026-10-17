"""Concurrent OCR must preserve complete evidence and keep PDF access on its owner thread."""

import subprocess
import threading
import time
from types import SimpleNamespace

import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


def pdf(path, count=7):
    with fitz.open() as document:
        for number in range(count):
            page = document.new_page(width=200, height=240)
            page.insert_text(
                (20, 40),
                f"Physical page {number + 1}. " + "A complete ordinary sentence. " * 3,
                fontsize=8,
            )
        document.save(path)


def test_lab_reads_healthy_native_pages_without_marking_them_all_front_matter(
    tmp_path, monkeypatch
):
    path = tmp_path / "document.pdf"
    pdf(path, 14)
    seen = []

    def read(document, task, executable):
        seen.append(task["page"])
        return [{**task, "text": "Exploratory text", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", read)
    result = acquisition.collect(
        AuthorEvidenceContext.from_samples(
            [], path=path, acquisition_mode="lab", ocr_workers=1
        )
    )
    trace = result.evidence_bundle["trace"]
    assert set(range(1, 13)) <= set(seen)
    assert trace["opening_plan"] == [1, 2, 3, 4, 5]
    assert trace["lab_audit"]["pages"] == list(range(1, 13))
    assert trace["complete"]


def test_concurrent_workers_get_only_materialized_images_and_keep_plan_order(
    tmp_path, monkeypatch
):
    path = tmp_path / "document.pdf"
    pdf(path, 5)
    owner = threading.get_ident()
    original = fitz.Page.get_pixmap
    renders = []

    def render(page, *args, **kwargs):
        assert threading.get_ident() == owner
        renders.append(page.number)
        return original(page, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    lock = threading.Lock()
    active = maximum = 0
    barrier = threading.Barrier(2)

    def read(prepared, task, executable):
        nonlocal active, maximum
        assert isinstance(prepared, acquisition._RasterRegion)
        assert threading.get_ident() != owner
        with lock:
            active += 1
            maximum = max(maximum, active)
        if task["page"] <= 2:
            barrier.wait(timeout=5)
        time.sleep(0.01 if task["page"] == 1 else 0.002)
        with lock:
            active -= 1
        return [{**task, "text": f"Credit {task['page']}", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", read)
    result = acquisition.collect(
        AuthorEvidenceContext.from_samples(
            [], path=path, acquisition_mode="lab", ocr_workers=2
        )
    )
    assert maximum == 2
    assert {0, 1, 2, 3, 4} <= set(renders)
    assert [r["page"] for r in result.evidence_bundle["ocr"]] == [1, 2, 3, 4, 5]
    assert result.evidence_bundle["trace"]["complete"]


def test_worker_failure_does_not_cancel_other_pages_or_become_complete(
    tmp_path, monkeypatch
):
    path = tmp_path / "document.pdf"
    pdf(path, 5)

    def read(prepared, task, executable):
        if task["page"] == 2:
            raise RuntimeError("injected recognition failure")
        return [{**task, "text": "Read remaining evidence", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", read)
    result = acquisition.collect(
        AuthorEvidenceContext.from_samples(
            [], path=path, acquisition_mode="lab", ocr_workers=2
        )
    )
    assert {r["page"] for r in result.evidence_bundle["ocr"]} == {1, 3, 4, 5}
    trace = result.evidence_bundle["trace"]
    assert not trace["complete"]
    assert any(a["page"] == 2 and a["status"] == "failed" for a in trace["attempts"])


def test_each_new_inference_reads_images_again(tmp_path, monkeypatch):
    path = tmp_path / "document.pdf"
    pdf(path, 2)
    called = []

    def read(prepared, task, executable):
        called.append(task["page"])
        return [{**task, "text": "Read evidence", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", read)
    for _ in range(2):
        acquisition.collect(
            AuthorEvidenceContext.from_samples(
                [], path=path, acquisition_mode="lab", ocr_workers=2
            )
        )
    assert sorted(called) == [1, 1, 2, 2]
    assert not acquisition._CACHE


def test_quality_retry_reuses_only_the_current_materialized_region(
    monkeypatch, tmp_path
):
    executable = tmp_path / "tesseract.exe"
    executable.touch()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        text = b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        if len(calls) == 2:
            text += b"5\t1\t1\t1\t1\t1\t10\t20\t80\t15\t95\tAmelia\n"
        return SimpleNamespace(stdout=text)

    monkeypatch.setattr(subprocess, "run", run)
    prepared = acquisition._RasterRegion(
        100, 100, {2.5: {"payload": b"same-region", "border": 0, "preprocessing": []}}
    )
    readings, attempts = acquisition._read_region(
        prepared,
        {"page": 1, "clip": [0, 0, 100, 100], "reason": "image_without_native_overlay"},
        str(executable),
    )
    assert [a["psm"] for a in attempts] == [3, 1]
    assert readings[0]["text"] == "Amelia"
    assert [payload for _, payload in calls] == [b"same-region", b"same-region"]


def test_invalid_acquisition_mode_is_not_silently_accepted():
    with pytest.raises(ValueError, match="acquisition mode"):
        acquisition.collect(
            AuthorEvidenceContext.from_samples([], acquisition_mode="typo")
        )


def test_retry_raster_is_rendered_only_when_requested_and_on_the_pdf_owner(
    tmp_path, monkeypatch
):
    path = tmp_path / "document.pdf"
    pdf(path, 2)
    owner = threading.get_ident()
    render = fitz.Page.get_pixmap
    seen = []

    def tracked(page, *args, **kwargs):
        assert threading.get_ident() == owner
        seen.append((page.number, kwargs["matrix"].a))
        return render(page, *args, **kwargs)

    def read(prepared, task, executable):
        assert set(prepared.frames) == {2.5}
        if task["page"] == 1:
            frame = prepared.request_frame(2.0)
            assert frame["payload"] and not frame.get("error")
        return [{**task, "text": "Read all requested evidence", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(fitz.Page, "get_pixmap", tracked)
    monkeypatch.setattr(acquisition, "_read_region", read)
    found = acquisition.collect(
        AuthorEvidenceContext.from_samples(
            [],
            path=path,
            acquisition_mode="lab",
            ocr_workers=2,
        )
    )
    assert sorted(seen) == [(0, 2.0), (0, 2.5), (1, 2.5)]
    assert found.evidence_bundle["trace"]["complete"]
