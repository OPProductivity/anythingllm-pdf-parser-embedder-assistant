import argparse
from unittest.mock import patch

import fitz
import pytest

import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


def source_pdf(path):
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Ordinary academic text for an export test.")
        document.save(path)


def arguments(source, output, overwrite=False):
    return argparse.Namespace(pdf=str(source), out_dir=str(output), output_base_name="",
        backend="pymupdf", unstructured_strategy="auto", validation_phrase=[],
        overwrite=overwrite, source_label="Academic", start_page=1, stop_after_page=0,
        stop_heading=[], segment_chars=650, min_boundary_chars=250, min_page_chars=1)


@pytest.mark.parametrize("method", [tools.extract_pdf, tools.segment_pdf])
def test_repeated_cli_export_requires_consent_before_extraction(tmp_path, method, capsys):
    source = tmp_path / "paper.pdf"
    source_pdf(source)
    output = tmp_path / "exports"
    method(arguments(source, output))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with patch.object(tools, "get_backend_pages", side_effect=AssertionError("extraction must not start")):
        with pytest.raises(FileExistsError):
            method(arguments(source, output))
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    method(arguments(source, output, overwrite=True))
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    assert ".pdf-export-" not in capsys.readouterr().out


@pytest.mark.parametrize("overwrite", [False, True])
def test_extraction_failure_preserves_existing_files_and_removes_staging(tmp_path, overwrite):
    source = tmp_path / "paper.pdf"
    source_pdf(source)
    output = tmp_path / "exports"
    output.mkdir()
    unrelated = output / "notes.txt"
    unrelated.write_text("Keep these notes")
    if overwrite:
        tools.extract_pdf(arguments(source, output))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with patch.object(tools, "get_backend_pages", side_effect=RuntimeError("extraction failed")):
        with pytest.raises(RuntimeError):
            tools.extract_pdf(arguments(source, output, overwrite=overwrite))
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_publish_failure_restores_original_export_set(tmp_path, monkeypatch):
    import cli_exports

    source = tmp_path / "paper.pdf"
    source_pdf(source)
    output = tmp_path / "exports"
    tools.extract_pdf(arguments(source, output))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    replace = cli_exports.os.replace
    attempts = []

    def fail_second_publication(src, dest):
        if str(src).endswith("page-report.csv") and not str(src).endswith(".previous"):
            attempts.append(True)
            raise OSError("publication interrupted")
        return replace(src, dest)

    monkeypatch.setattr(cli_exports.os, "replace", fail_second_publication)
    with pytest.raises(OSError, match="publication interrupted"):
        tools.extract_pdf(arguments(source, output, overwrite=True))
    assert attempts
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
