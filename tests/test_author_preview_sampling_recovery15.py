"""The real UI helper must survive a broken seed-page text provider."""
import fitz
import pytest

from author_metadata import evidence_collection
from author_metadata import pdf as api

pytestmark = pytest.mark.offline_deterministic


def make_source(path):
    doc = fitz.open()
    for number in range(5):
        page = doc.new_page()
        if number == 0:
            text = "Collective Memory and Public Life\nBy Amelia Hart"
        elif number == 1:
            text = "Copyright 2025. The rights of Amelia Hart as author have been asserted."
        else:
            text = "This study examines records and collective public memory. " * 6
        page.insert_text((50, 70), text, fontsize=10)
    doc.set_metadata({"title": "Collective Memory and Public Life"})
    doc.save(path)
    doc.close()


def capture_real_inference(monkeypatch):
    reports = []
    real = api.infer_author

    def capture(context, **kwargs):
        report = real(context, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(api, "infer_author", capture)
    return reports


def install_rendered_ocr_fixture(monkeypatch, native_reader):
    # Only the external OCR output is supplied. The actual page rendering,
    # failed native read, acquisition plan and responsibility experts run.
    def read_region(document, task, executable):
        page = document[task["page"] - 1]
        pix = page.get_pixmap()
        assert pix.samples and pix.samples.count(b"\xff") != len(pix.samples)
        text = native_reader(page, "text")
        return [{**task, "text": text, "lines": [], "width": pix.width,
                 "height": pix.height, "scale": 1}], [
            {"kind": "ocr", **task, "status": "read", "characters": len(text)}
        ]

    monkeypatch.setattr(evidence_collection, "_ocr_worker_budget", lambda value: 1)
    monkeypatch.setattr(evidence_collection, "_read_region", read_region)


@pytest.mark.parametrize("failed_page", [1, 2, 4])
def test_preview_seed_failure_still_reads_later_pages_and_ocr(
    tmp_path, monkeypatch, failed_page
):
    path = tmp_path / "document.pdf"
    make_source(path)
    native = fitz.Page.get_text

    def read(page, option="text", *args, **kwargs):
        if page.number + 1 == failed_page and option == "text":
            raise RuntimeError("injected native provider failure")
        return native(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", read)
    install_rendered_ocr_fixture(monkeypatch, native)
    reports = capture_real_inference(monkeypatch)
    result = api.infer_author_from_initial_pdf_pages(path, page_limit=4)
    assert result["author"] == "Amelia Hart"
    assert f"page {failed_page}" in result["initial_sampling_warning"]
    assert reports[-1]["initial_sampling_warning"]
    trace = reports[-1]["evidence_review"]
    # Recovery must not erase the failed native provider from completeness.
    assert not trace["collection"]["complete"]
    assert any(a["kind"] == "native" and a["page"] == failed_page
               and a["status"] == "failed"
               for a in trace["collection"]["attempts"])
    assert trace["all_scheduled_experts_assessed"]
    assert {1, 2, 3, 4, 5} <= {
        a["page"] for a in trace["collection"]["attempts"] if a["kind"] == "native"
    }
    assert any(t["page"] == failed_page and t["reason"] == "native_read_failed"
               for t in trace["collection"]["ocr_plan"])
    assert any(a["kind"] == "ocr" and a["page"] == failed_page
               and a["reason"] == "native_read_failed" and a["status"] == "read"
               for a in trace["collection"]["attempts"])


def test_preview_healthy_seed_preserves_a_warning_free_result(tmp_path, monkeypatch):
    path = tmp_path / "document.pdf"
    make_source(path)
    install_rendered_ocr_fixture(monkeypatch, fitz.Page.get_text)
    result = api.infer_author_from_initial_pdf_pages(path, page_limit=4)
    assert result["author"] == "Amelia Hart"
    assert "initial_sampling_warning" not in result


def test_preview_unopenable_pdf_remains_an_explicit_error(tmp_path, monkeypatch):
    result = api.infer_author_from_initial_pdf_pages(tmp_path / "missing.pdf")
    assert result["source"] == "error"
    assert not result["author"]
    assert result["error"]
