"""Lab checkpoints cannot observe future image readings or mutate acquisition."""

from dataclasses import replace

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


def make_source(path):
    with fitz.open() as doc:
        for number in range(7):
            p = doc.new_page(width=240, height=300)
            p.insert_text((20, 40), f"Ordinary page {number + 1}. " + "This is ordinary text. " * 3, fontsize=9)
        doc.save(path)


def install_reader(monkeypatch):
    def read(document, task, executable):
        return [{**task, "text": f"Visible credit on page {task['page']}", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(acquisition, "_read_region", read)


def context(path, observer=None):
    return AuthorEvidenceContext.from_samples(
        [], path=path, acquisition_mode="lab", lab_page_limit=5,
        ocr_workers=1, acquisition_checkpoint=observer,
    )


def test_prefix_has_only_acquired_ocr_and_is_not_falsely_complete(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    make_source(path)
    install_reader(monkeypatch)
    snapshots = []
    result = acquisition.collect(context(path, snapshots.append))
    assert snapshots[0].evidence_bundle["trace"]["acquisition_prefix"]["phase"] == "native_complete"
    assert snapshots[0].evidence_bundle["ocr"] == []
    assert not snapshots[0].evidence_bundle["trace"]["complete"]
    region_snapshots = [s for s in snapshots if s.evidence_bundle["trace"]["acquisition_prefix"]["phase"] == "ocr_region_complete"]
    for index, snapshot in enumerate(region_snapshots, 1):
        assert len(snapshot.evidence_bundle["ocr"]) == index
        assert snapshot.evidence_bundle["trace"]["acquisition_prefix"]["completed_regions"] == index
        assert snapshot.acquisition_checkpoint is None
    assert snapshots[-1].evidence_bundle["trace"]["complete"]
    assert snapshots[-1].evidence_bundle["trace"]["acquisition_prefix"]["future_extensions_known"]
    assert result.evidence_bundle["trace"]["complete"]


def test_callback_mutation_does_not_change_native_or_ocr_evidence(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    make_source(path)
    install_reader(monkeypatch)
    plain = acquisition.collect(context(path))

    def malicious(snapshot):
        snapshot.evidence_bundle["native"].clear()
        snapshot.evidence_bundle["ocr"].clear()
        snapshot.evidence_bundle["trace"]["ocr_plan"].clear()
        for sample in snapshot.samples:
            sample["text"] = "Changed in callback"

    observed = acquisition.collect(context(path, malicious))
    assert observed.samples == plain.samples
    assert observed.evidence_bundle["native"] == plain.evidence_bundle["native"]
    assert observed.evidence_bundle["ocr"] == plain.evidence_bundle["ocr"]
    assert observed.evidence_bundle["trace"]["ocr_plan"] == plain.evidence_bundle["trace"]["ocr_plan"]
    assert observed.evidence_bundle["trace"]["attempts"] == plain.evidence_bundle["trace"]["attempts"]


def test_broken_observer_is_visible_and_does_not_cancel_other_rooms(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    make_source(path)
    install_reader(monkeypatch)

    def broken(snapshot):
        raise RuntimeError("Diagnostic sink failed")

    result = acquisition.collect(context(path, broken))
    trace = result.evidence_bundle["trace"]
    assert trace["complete"]
    assert trace["checkpoint_audit_errors"]
    assert {r["page"] for r in result.evidence_bundle["ocr"]} == {1, 2, 3, 4, 5}


def test_observed_bundle_does_not_reacquire_if_reassessed(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    make_source(path)
    install_reader(monkeypatch)
    snapshots = []
    acquisition.collect(context(path, snapshots.append))
    prefix = snapshots[1]
    assert acquisition.collect(prefix) is prefix
    assert replace(prefix).acquisition_checkpoint is None
