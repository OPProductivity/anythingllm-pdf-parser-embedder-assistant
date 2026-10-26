from pathlib import Path
from unittest.mock import patch

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline
from canonical_artifacts import CanonicalArtifacts
from native_layout_reads import NativeLayoutReads
from rag_pdf_tools import _tsv_column_line_text
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def test_capacity_recreates_an_empty_public_destination_removed_mid_batch(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source size projection")
    output = tmp_path / "exports"
    output.mkdir()
    first = app.automatic_batch_output_capacity_preflight([source], output)
    output.rmdir()
    second = app.automatic_batch_output_capacity_preflight(
        [source], output, projected_source_bytes=first["projected_source_bytes"]
    )
    assert output.is_dir()
    assert second["status"] == "pass"
    assert second["required_free_bytes"] == first["required_free_bytes"]
    assert second["available_free_bytes"] > 0


def test_capacity_inspection_error_is_a_safe_hold_not_unknown_zero_space(tmp_path):
    with patch.object(app.shutil, "disk_usage", side_effect=PermissionError("unavailable")):
        result = app.automatic_batch_output_capacity_preflight([], tmp_path)
    assert result["status"] == "output_unavailable"
    assert result["available_free_bytes"] is None
    assert result["error_type"] == "PermissionError"
    assert "unavailable" in result["message"]
    assert app.terminal_output_capacity_evidence(result)["available_bytes"] is None


def test_advisory_eta_samples_three_pages_but_required_coverage_still_reads_all(
    tmp_path,
):
    source = tmp_path / "many-pages.pdf"
    with fitz.open() as doc:
        for number in range(30):
            page = doc.new_page()
            page.insert_text(
                (30, 40), f"Page {number}: retained content throughout the source."
            )
        doc.save(source)
    original = fitz.Page.get_text
    calls = []

    def count(page, *args, **kwargs):
        calls.append(page.number)
        return original(page, *args, **kwargs)

    with patch.object(fitz.Page, "get_text", count):
        profile = app.automatic_timing_document_profile([source])
        assert len(calls) == 3 and profile["sampled_pages"] == 3
        calls.clear()
        coverage = app.automatic_full_native_text_coverage(source)
        assert len(calls) == 30 and coverage["page_count"] == 30


def test_txt_only_catalog_defers_unused_pool_but_manifest_use_validates_it(tmp_path):
    pool = tmp_path / "manifest-text.jsonl"
    pool.write_text("invalid body pool", encoding="utf8")
    with pytest.raises(ValueError):
        CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
    catalog = CanonicalArtifacts(
        tmp_path, pipeline.atomic_write_text, defer_manifest_text=True
    )
    assert (
        catalog.text(tmp_path / "one.txt", "Literal text.").read_text()
        == "Literal text."
    )
    with pytest.raises(ValueError):
        catalog.jsonl(tmp_path / "one.jsonl", [{"text": "long body " * 100}])
    assert not (tmp_path / "one.jsonl").exists()


def test_catalog_root_resolves_once_but_targets_are_still_checked(tmp_path):
    original = Path.resolve
    roots = []

    def count(path, *args, **kwargs):
        if path == tmp_path:
            roots.append(path)
        return original(path, *args, **kwargs)

    with patch.object(Path, "resolve", count):
        catalog = CanonicalArtifacts(tmp_path, pipeline.atomic_write_text)
        catalog.text(tmp_path / "one.txt", "one")
        catalog.text(tmp_path / "two.txt", "two")
        with pytest.raises(ValueError):
            catalog.text(tmp_path / "../outside.txt", "forbidden")
    assert len(roots) == 1


def test_native_document_name_is_validated_once_and_revalidated_on_change(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        for _ in range(3):
            page = doc.new_page()
            page.insert_text((30, 40), "Independent native content")
        doc.save(source)
    original = Path.resolve
    calls = []

    def count(path, *args, **kwargs):
        if path == source:
            calls.append(path)
        return original(path, *args, **kwargs)

    with patch.object(Path, "resolve", count):
        collector = NativeLayoutReads(source, pipeline._native_layout_profile)
        with fitz.open(source) as doc:
            for page in doc:
                collector.text(page)
            with patch.object(
                type(doc), "name", property(lambda _: str(tmp_path / "different.pdf"))
            ):
                assert collector.consume(doc[0]) is None
    assert len(calls) == 2  # scope anchor, then the first open Document


def test_split_ocr_blocks_keep_physical_lines_and_every_word_with_one_input_pass():
    class Rows(list):
        passes = 0

        def __iter__(self):
            self.passes += 1
            return super().__iter__()

    rows = Rows()
    for block, line, top, left, word in [
        (2, 1, 10, 90, "title"),
        (1, 1, 10, 10, "A"),
        (2, 2, 40, 90, "François"),
        (1, 2, 40, 10, "Author"),
        (3, 1, 70, 10, "1."),
        (3, 1, 70, 30, "Footnote"),
    ]:
        rows.append(
            {
                "block": block,
                "paragraph": 1,
                "line": line,
                "top": top,
                "left": left,
                "height": 10,
                "word": 1,
                "text": word,
            }
        )
    assert _tsv_column_line_text(rows) == "A title\nAuthor François\n1. Footnote"
    assert rows.passes == 1
