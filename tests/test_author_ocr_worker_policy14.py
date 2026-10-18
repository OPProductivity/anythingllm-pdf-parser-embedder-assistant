"""A larger OCR budget must remain bounded and keep PDF access on its owner."""

import threading
import fitz
import pytest
from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize(
    "logical,expected", [(None, 1), (1, 1), (2, 1), (4, 2), (8, 4), (16, 8), (64, 8)]
)
def test_auto_worker_budget_leaves_capacity_for_other_work(
    monkeypatch, logical, expected
):
    monkeypatch.setattr(
        acquisition.os, "process_cpu_count", lambda: logical, raising=False
    )
    assert acquisition._ocr_worker_budget(0) == expected


@pytest.mark.parametrize(
    "requested,expected", [(-1, 1), (1, 1), (2, 2), (4, 4), (8, 8), (99, 8)]
)
def test_explicit_worker_budget_is_bounded(monkeypatch, requested, expected):
    monkeypatch.setattr(acquisition.os, "process_cpu_count", lambda: 1, raising=False)
    assert acquisition._ocr_worker_budget(requested) == expected


def test_eight_workers_finish_retries_in_plan_order_without_pdf_access(
    tmp_path, monkeypatch
):
    path = tmp_path / "document.pdf"
    with fitz.open() as doc:
        for _ in range(9):
            doc.new_page(width=200, height=240)
        doc.save(path)
    owner = threading.get_ident()
    prepare = acquisition._prepare_region
    renders = []

    def checked_prepare(document, task, **kwargs):
        assert threading.get_ident() == owner
        renders.append((task["page"], kwargs.get("factors")))
        return prepare(document, task, **kwargs)

    monkeypatch.setattr(acquisition, "_prepare_region", checked_prepare)
    barrier = threading.Barrier(8)
    active = maximum = 0
    lock = threading.Lock()

    def recognize(prepared, task, executable):
        nonlocal active, maximum
        assert isinstance(prepared, acquisition._RasterRegion)
        assert threading.get_ident() != owner
        with lock:
            active += 1
            maximum = max(maximum, active)
        if task["page"] <= 8:
            barrier.wait(timeout=5)
        assert prepared.request_frame(2.0)
        with lock:
            active -= 1
        return [{"page": task["page"], "text": "Independent reading", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", recognize)
    tasks = [
        {"page": number, "clip": [0, 0, 200, 240], "reason": "lab_full_page_audit"}
        for number in range(1, 10)
    ]
    with fitz.open(path) as doc:
        result = list(acquisition._read_regions(doc, tasks, "unused", workers=8))
    assert maximum == 8
    assert [task["page"] for task, _, _ in result] == list(range(1, 10))
    assert all(
        readings[0]["page"] == task["page"] and attempts[0]["status"] == "read"
        for task, readings, attempts in result
    )
    assert {page for page, factors in renders if factors == (2.0,)} == set(range(1, 10))
