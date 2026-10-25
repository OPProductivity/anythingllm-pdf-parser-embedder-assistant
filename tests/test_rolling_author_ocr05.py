"""Rolling work must retain every region, its retries and owner-thread renders."""

import threading
import pytest
from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


def tasks(count):
    return [
        {"page": i + 1, "clip": [0, 0, 100, 100], "reason": "sparse_native"}
        for i in range(count)
    ]


def test_a_free_slot_advances_without_waiting_for_an_earlier_region(monkeypatch):
    third = threading.Event()
    owner = threading.get_ident()
    lock = threading.Lock()
    active = peak = 0

    def prepare(document, task, **kwargs):
        assert threading.get_ident() == owner
        return acquisition._RasterRegion(100, 100, {})

    def read(prepared, task, executable):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if task["page"] == 1:
                assert third.wait(3), (
                    "a free OCR slot was held behind the first slow region"
                )
            if task["page"] == 3:
                third.set()
            return [{"page": task["page"], "text": str(task["page"])}], [
                {"kind": "ocr", **task, "status": "read"}
            ]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(acquisition, "_prepare_region", prepare)
    monkeypatch.setattr(acquisition, "_read_region", read)
    results = list(acquisition._read_regions(None, tasks(6), "unused", workers=2))
    assert [task["page"] for task, _, _ in results] == list(range(1, 7))
    assert [r[0]["text"] for _, r, _ in results] == list(map(str, range(1, 7)))
    assert peak <= 2


def test_retry_raster_is_rendered_by_owner_while_other_regions_continue(monkeypatch):
    owner = threading.get_ident()
    rendered = []

    def prepare(document, task, *, factors):
        assert threading.get_ident() == owner
        rendered.append((task["page"], tuple(factors)))
        return acquisition._RasterRegion(
            100,
            100,
            {factor: {"payload": str(task["page"]).encode()} for factor in factors},
        )

    def read(prepared, task, executable):
        retry = prepared.request_frame(2.0)
        assert retry["payload"] == str(task["page"]).encode()
        return [{"page": task["page"]}], [
            {"kind": "ocr", **task, "status": "read", "psm": 3},
            {"kind": "ocr", **task, "status": "read", "psm": 1},
        ]

    monkeypatch.setattr(acquisition, "_prepare_region", prepare)
    monkeypatch.setattr(acquisition, "_read_region", read)
    results = list(acquisition._read_regions(None, tasks(5), "unused", workers=2))
    assert [task["page"] for task, _, _ in results] == list(range(1, 6))
    assert all(len(attempts) == 2 for _, _, attempts in results)
    assert all((n, (2.0,)) in rendered for n in range(1, 6))


@pytest.mark.parametrize("workers", [1, 2])
def test_region_failures_are_retained_in_plan_order(monkeypatch, workers):
    def prepare(document, task, **kwargs):
        if task["page"] == 2:
            raise OSError("deliberate read failure")
        return acquisition._RasterRegion(100, 100, {})

    def read(document, task, executable):
        if workers == 1 and task["page"] == 2:
            raise OSError("deliberate read failure")
        return [{"page": task["page"]}], [{"kind": "ocr", **task, "status": "read"}]

    monkeypatch.setattr(acquisition, "_prepare_region", prepare)
    monkeypatch.setattr(acquisition, "_read_region", read)
    results = list(acquisition._read_regions(None, tasks(3), "unused", workers=workers))
    assert [t["page"] for t, _, _ in results] == [1, 2, 3]
    assert results[1][1] == [] and results[1][2][0]["status"] == "failed"
