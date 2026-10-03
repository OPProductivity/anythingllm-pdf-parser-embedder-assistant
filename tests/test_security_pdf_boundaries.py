import argparse
import os
from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_tools as tools
from pdf_budgets import DEFAULTS, PdfBudgetExceeded, check, check_pages, check_raster
from source_guard import locked_pdf_source

pytestmark = pytest.mark.offline_deterministic


def pdf(path):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "An ordinary academic source with native text.")
        document.save(path)


def args(source, output, overwrite=False):
    return argparse.Namespace(pdf=str(source), out_dir=str(output), output_base_name="",
                              backend="pymupdf", unstructured_strategy="auto", validation_phrase=[],
                              overwrite=overwrite, source_label="Academic", start_page=1,
                              stop_after_page=0, stop_heading=[], segment_chars=650,
                              min_boundary_chars=250, min_page_chars=1)


@pytest.mark.parametrize("method", [tools.extract_pdf, tools.segment_pdf])
def test_exports_do_not_overwrite_without_explicit_consent(tmp_path, method, capsys):
    source = tmp_path / "academic.pdf"
    pdf(source)
    output = tmp_path / "exports"
    method(args(source, output))
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with patch.object(tools, "get_backend_pages", side_effect=AssertionError("must reject before extraction")):
        with pytest.raises(FileExistsError):
            method(args(source, output))
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    method(args(source, output, True))
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    assert ".pdf-export-" not in capsys.readouterr().out


def test_failed_export_does_not_leave_reservations(tmp_path):
    source = tmp_path / "academic.pdf"
    pdf(source)
    output = tmp_path / "exports"
    with patch.object(tools, "get_backend_pages", side_effect=RuntimeError("extraction failed")):
        with pytest.raises(RuntimeError):
            tools.extract_pdf(args(source, output))
    assert not list(output.iterdir())


def test_preflight_rejects_source_over_budget_before_extraction(tmp_path, monkeypatch):
    source = tmp_path / "academic.pdf"
    pdf(source)
    monkeypatch.setenv("ANYTHINGLLM_PDF_MAX_SOURCE_BYTES", "1")
    report = pipeline.pdf_input_preflight(source)
    assert report["status"] != "pass"
    assert "SOURCE_BYTES" in report["message"]


def test_agreed_defaults_and_nontruncating_text_limits(monkeypatch):
    assert DEFAULTS["SOURCE_BYTES"] == 6 * 1024**3
    assert DEFAULTS["TEXT_BYTES"] == 150 * 1024**2
    monkeypatch.setenv("ANYTHINGLLM_PDF_MAX_TEXT_BYTES", "10")
    with pytest.raises(PdfBudgetExceeded):
        check_pages([{"text": "123456"}, {"text": "123456"}])
    monkeypatch.setenv("ANYTHINGLLM_PDF_MAX_TEXT_BYTES", "0")
    with pytest.raises(PdfBudgetExceeded):
        check("TEXT_BYTES", 1)


def test_raster_geometry_rejected_before_allocation(monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_PDF_MAX_RASTER_PIXELS", "10")
    with fitz.open() as document:
        page = document.new_page()
        with pytest.raises(PdfBudgetExceeded):
            check_raster(page, 200)


@pytest.mark.skipif(os.name != "nt", reason="Windows mandatory sharing qualification")
def test_source_lock_allows_readers_but_prevents_write_and_replacement(tmp_path):
    source = tmp_path / "academic.pdf"
    pdf(source)
    original = source.read_bytes()
    with locked_pdf_source(source) as stable:
        assert stable == source.resolve()
        with fitz.open(stable) as document:
            assert document.page_count == 1
        with pytest.raises(OSError):
            source.write_bytes(b"Substituted")
        replacement = tmp_path / "replacement.pdf"
        replacement.write_bytes(original)
        with pytest.raises(OSError):
            os.replace(replacement, source)
    assert source.read_bytes() == original
