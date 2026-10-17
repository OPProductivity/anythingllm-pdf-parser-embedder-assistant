import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.profile import DocumentProfile
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def make_pdf(path):
    doc = fitz.open()
    for n in range(5):
        page = doc.new_page()
        if n == 0:
            page.insert_text((50, 70), "Collective Memory and Public Life", fontsize=18)
            page.insert_text((50, 120), "By Benedict Vale", fontsize=14)
        elif n == 1:
            page.insert_text(
                (50, 70),
                "Copyright 2025. The rights of Amelia Hart as author have been asserted.",
                fontsize=10,
            )
        else:
            page.insert_text(
                (50, 70),
                "This study examines records and collective public memory. " * 6,
                fontsize=10,
            )
    doc.save(path)
    doc.close()


def fail_second_text(monkeypatch):
    original = fitz.Page.get_text

    def read(page, option="text", *args, **kwargs):
        if page.number == 1 and option == "text":
            raise RuntimeError("injected native text failure")
        return original(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", read)


def test_initial_sampling_error_does_not_cancel_later_pages(tmp_path, monkeypatch):
    path = tmp_path / "document.pdf"
    make_pdf(path)
    fail_second_text(monkeypatch)
    metadata = pipeline.pdf_metadata(path, include_author_samples=True)
    assert "page 2" in metadata["_author_sample_error"]
    assert {1, 3, 4, 5} <= {s["page"] for s in metadata["_author_text_samples"]}


def test_native_failure_gets_ocr_attempt_and_can_revise_an_early_author(
    tmp_path, monkeypatch
):
    from author_metadata import evidence_collection

    path = tmp_path / "document.pdf"
    make_pdf(path)
    fail_second_text(monkeypatch)
    seen = []

    def read_region(document, task, executable):
        seen.append(task)
        pix = document[task["page"] - 1].get_pixmap()
        assert pix.samples and pix.samples.count(b"\xff") != len(pix.samples)
        text = (
            "Copyright 2025. The rights of Amelia Hart as author have been asserted."
            if task["page"] == 2
            else document[task["page"] - 1].get_text("text")
        )
        return [
            {**task, "text": text, "lines": [], "width": 595, "height": 842, "scale": 1}
        ], [{"kind": "ocr", **task, "status": "read", "characters": len(text)}]

    monkeypatch.setattr(evidence_collection, "_read_region", read_region)
    context = AuthorEvidenceContext.from_samples(
        [],
        path=path,
        title_hint="Collective Memory and Public Life",
        ocr_workers=1,  # This control injects a synchronous PDF-reading adapter.
        profile=DocumentProfile("book", (), (), work_scope="single_work"),
    )
    result = infer_author(
        context, sampling_error="page 2: injected native text failure"
    )
    assert result["author"] == "Amelia Hart"
    assert result["initial_sampling_warning"]
    assert any(t["page"] == 2 and t["reason"] == "native_read_failed" for t in seen)
    trace = result["evidence_review"]["collection"]
    assert any(
        a["kind"] == "native" and a["page"] == 2 and a["status"] == "failed"
        for a in trace["attempts"]
    )
    assert {1, 2, 3, 4, 5} <= {
        a["page"] for a in trace["attempts"] if a["kind"] == "native"
    }
    assert result["evidence_review"]["all_scheduled_experts_assessed"]
