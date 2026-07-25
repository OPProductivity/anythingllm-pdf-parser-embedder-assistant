import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
from unittest import mock

import pytest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace

import fitz

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
warnings.filterwarnings("ignore", category=ResourceWarning, message=r"unclosed event loop .*")

import auto_anythingllm_pipeline as pipeline  # noqa: E402


pytestmark = pytest.mark.offline_deterministic


class PipelineCoreTests(unittest.TestCase):
    def test_unstructured_asyncio_compatibility_uses_supported_inspect_predicate(self):
        import asyncio
        import inspect
        import rag_pdf_tools

        original = getattr(asyncio, "iscoroutinefunction", None)
        try:
            asyncio.iscoroutinefunction = lambda _function: False
            rag_pdf_tools.ensure_unstructured_asyncio_compatibility()
            self.assertIs(asyncio.iscoroutinefunction, inspect.iscoroutinefunction)
        finally:
            if original is None:
                delattr(asyncio, "iscoroutinefunction")
            else:
                asyncio.iscoroutinefunction = original

    def test_unstructured_import_installs_compatibility_before_importing_vendor_module(self):
        import rag_pdf_tools

        original_import_module = rag_pdf_tools.importlib.import_module
        original_compatibility = rag_pdf_tools.ensure_unstructured_asyncio_compatibility
        calls = []
        sentinel = object()
        try:
            rag_pdf_tools.ensure_unstructured_asyncio_compatibility = lambda: calls.append("compatibility")
            rag_pdf_tools.importlib.import_module = lambda module_name: (calls.append(module_name) or sentinel)
            self.assertIs(rag_pdf_tools.import_optional_backend("unstructured.partition.pdf"), sentinel)
            self.assertEqual(calls, ["compatibility", "unstructured.partition.pdf"])
        finally:
            rag_pdf_tools.importlib.import_module = original_import_module
            rag_pdf_tools.ensure_unstructured_asyncio_compatibility = original_compatibility

    def test_unstructured_runtime_status_requires_importable_partition_module(self):
        import rag_pdf_tools

        original_resolution = rag_pdf_tools.ensure_optional_backend_path
        original_import = rag_pdf_tools.import_optional_backend
        try:
            rag_pdf_tools.ensure_optional_backend_path = lambda module_name: {
                "status": "already_available",
                "path": "",
            }

            def missing_transitive_dependency(module_name):
                raise ModuleNotFoundError("No module named 'pi_heif'")

            rag_pdf_tools.import_optional_backend = missing_transitive_dependency
            status = rag_pdf_tools.unstructured_runtime_status("hi_res")
        finally:
            rag_pdf_tools.ensure_optional_backend_path = original_resolution
            rag_pdf_tools.import_optional_backend = original_import
        self.assertFalse(status["backend_available"])
        self.assertIn("ModuleNotFoundError", status["backend_import_error"])

    def test_unstructured_page_extraction_reuses_supplied_runtime_probe(self):
        import rag_pdf_tools

        class FakeElement:
            category = "NarrativeText"
            metadata = SimpleNamespace(page_number=1)

            def __str__(self):
                return "Recovered OCR text."

        original_runtime = rag_pdf_tools.unstructured_runtime_status
        original_import = rag_pdf_tools.import_optional_backend
        try:
            rag_pdf_tools.unstructured_runtime_status = lambda *_args, **_kwargs: self.fail(
                "the supplied run-scoped probe must avoid a second runtime check"
            )
            rag_pdf_tools.import_optional_backend = lambda _module: SimpleNamespace(
                partition_pdf=lambda **_kwargs: [FakeElement()]
            )
            pages, page_count, elements = rag_pdf_tools.get_pages_with_unstructured(
                Path("fixture.pdf"),
                "hi_res",
                runtime_probe={"ocr_required": True, "tesseract_available": True},
            )
        finally:
            rag_pdf_tools.unstructured_runtime_status = original_runtime
            rag_pdf_tools.import_optional_backend = original_import

        self.assertEqual(page_count, 1)
        self.assertEqual(pages[0]["text"], "[NarrativeText] Recovered OCR text.")
        self.assertEqual(elements[0]["pdf_page"], 1)

    def test_unstructured_ocr_page_workers_are_conservatively_bounded(self):
        import rag_pdf_tools

        original = os.environ.get("RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS")
        try:
            os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS"] = "99"
            self.assertEqual(rag_pdf_tools.unstructured_ocr_page_workers(), 4)
            os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS"] = "not-a-number"
            self.assertEqual(rag_pdf_tools.unstructured_ocr_page_workers(), 2)
        finally:
            if original is None:
                os.environ.pop("RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS", None)
            else:
                os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS"] = original

    def test_unstructured_ocr_timeout_is_bounded_and_configurable(self):
        import rag_pdf_tools

        original = os.environ.get("RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS")
        try:
            os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS"] = "1"
            self.assertEqual(
                rag_pdf_tools.unstructured_ocr_page_timeout_seconds(),
                rag_pdf_tools.UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MIN,
            )
            os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS"] = "999999"
            self.assertEqual(
                rag_pdf_tools.unstructured_ocr_page_timeout_seconds(),
                rag_pdf_tools.UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MAX,
            )
        finally:
            if original is None:
                os.environ.pop("RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS", None)
            else:
                os.environ["RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS"] = original

    def test_unstructured_ocr_cache_requires_the_exact_source_identity(self):
        import rag_pdf_tools

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            pdf_path = root_path / "source.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "OCR cache fixture")
            document.save(pdf_path)
            document.close()
            runtime = {
                "backend_module_origin": "fixture-unstructured",
                "tesseract_executable": "",
            }
            pages = [{"page": 1, "text": "Recovered OCR text", "kind": "unstructured_elements"}]
            elements = [{"element_index": 1, "pdf_page": 1, "category": "NarrativeText", "chars": 18, "preview": "Recovered OCR text"}]
            cache_dir = root_path / "cache"
            stored = rag_pdf_tools.save_unstructured_ocr_cache(
                pdf_path, "hi_res", runtime, cache_dir, pages, 1, elements
            )
            cached = rag_pdf_tools.load_unstructured_ocr_cache(
                pdf_path, "hi_res", runtime, cache_dir
            )

        self.assertTrue(Path(stored).name.startswith("unstructured-ocr-"))
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0][0]["text"], "Recovered OCR text")
        self.assertEqual(cached[0][0]["unstructured_execution"]["mode"], "persistent_ocr_cache_hit")

    def test_unstructured_circuit_breaker_only_treats_runtime_failures_as_batch_wide(self):
        self.assertTrue(pipeline.is_unstructured_runtime_failure("Unstructured OCR timed out after 240s"))
        self.assertTrue(pipeline.is_unstructured_runtime_failure("Tesseract was not found"))
        self.assertFalse(pipeline.is_unstructured_runtime_failure("PDF page 7 had malformed text"))

    def test_unstructured_hi_res_uses_isolated_page_lane_and_reuses_runtime_probe(self):
        import rag_pdf_tools

        with tempfile.TemporaryDirectory() as root:
            pdf_path = Path(root) / "two-pages.pdf"
            document = fitz.open()
            document.new_page()
            document.new_page()
            document.save(pdf_path)
            document.close()
            original_workers = rag_pdf_tools.unstructured_ocr_page_workers
            original_parallel = rag_pdf_tools._parallel_unstructured_ocr_pages
            try:
                captured = {}
                rag_pdf_tools.unstructured_ocr_page_workers = lambda: 2
                rag_pdf_tools._parallel_unstructured_ocr_pages = (
                    lambda path, pages, strategy, workers, runtime: (
                        captured.update({
                            "path": path, "pages": pages, "strategy": strategy,
                            "workers": workers, "runtime": runtime,
                        }) or
                        ([{"page": 1, "text": "one"}, {"page": 2, "text": "two"}], 2, [])
                    )
                )
                result = rag_pdf_tools.get_pages_with_unstructured(
                    pdf_path,
                    "hi_res",
                    runtime_probe={"ocr_required": True, "tesseract_available": True},
                )
            finally:
                rag_pdf_tools.unstructured_ocr_page_workers = original_workers
                rag_pdf_tools._parallel_unstructured_ocr_pages = original_parallel

        self.assertEqual(result[1], 2)
        self.assertEqual(captured["workers"], 2)
        self.assertEqual(captured["runtime"]["tesseract_available"], True)

    def test_single_photographed_page_uses_crop_path_without_unstructured_partition(self):
        import rag_pdf_tools

        with tempfile.TemporaryDirectory() as root:
            pdf_path = Path(root) / "one-page.pdf"
            document = fitz.open()
            document.new_page()
            document.save(pdf_path)
            document.close()
            original_photo = rag_pdf_tools._photographed_page_result
            original_partition = rag_pdf_tools._unstructured_partition_elements
            try:
                rag_pdf_tools._photographed_page_result = lambda *_args: {
                    "page": 1,
                    "page_row": {"page": 1, "text": "Photographed page text", "reading_regions": []},
                    "element_rows": [{"element_index": 1, "pdf_page": 1, "category": "Photo", "chars": 22, "preview": "Photographed page text"}],
                }
                rag_pdf_tools._unstructured_partition_elements = lambda *_args, **_kwargs: self.fail(
                    "single photographed page should not fall back to vendor partition"
                )
                pages, count, elements = rag_pdf_tools.get_pages_with_unstructured(
                    pdf_path, "hi_res", runtime_probe={"ocr_required": True, "tesseract_available": True}
                )
            finally:
                rag_pdf_tools._photographed_page_result = original_photo
                rag_pdf_tools._unstructured_partition_elements = original_partition

        self.assertEqual(count, 1)
        self.assertEqual(pages[0]["unstructured_execution"]["mode"], "direct_single_photographed_page")
        self.assertEqual(elements[0]["category"], "Photo")

    def test_pymupdf4llm_initializes_tesseract_and_uses_measured_ocr_dpi(self):
        import rag_pdf_tools

        calls = []

        class FakePyMuPdf4Llm:
            @staticmethod
            def to_markdown(path, **kwargs):
                calls.append((path, kwargs))
                return [{"metadata": {"page": 1}, "text": "Recovered scan text."}]

        original_import = rag_pdf_tools.import_optional_backend
        original_runtime = rag_pdf_tools.ensure_tesseract_runtime
        original_worker_count = os.environ.get("RAG_PDF_OCR_PAGE_WORKERS")
        try:
            os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = "1"
            rag_pdf_tools.import_optional_backend = lambda name: FakePyMuPdf4Llm
            rag_pdf_tools.ensure_tesseract_runtime = lambda: {"available": True, "tessdata_prefix": "configured"}
            pages, count = rag_pdf_tools.get_pages_with_pymupdf4llm(Path("scanned.pdf"))
        finally:
            if original_worker_count is None:
                os.environ.pop("RAG_PDF_OCR_PAGE_WORKERS", None)
            else:
                os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = original_worker_count
            rag_pdf_tools.import_optional_backend = original_import
            rag_pdf_tools.ensure_tesseract_runtime = original_runtime
        self.assertEqual(count, 1)
        self.assertEqual(pages[0]["text"], "Recovered scan text.")
        self.assertEqual(calls[0][1]["page_chunks"], True)
        self.assertEqual(calls[0][1]["ocr_dpi"], rag_pdf_tools.PYMUPDF4LLM_OCR_DPI)

    def test_pymupdf4llm_ocr_worker_count_is_globally_bounded(self):
        import rag_pdf_tools

        original_worker_count = os.environ.get("RAG_PDF_OCR_PAGE_WORKERS")
        try:
            os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = "999"
            self.assertEqual(rag_pdf_tools.pymupdf4llm_ocr_page_workers(), 4)
            os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = "invalid"
            self.assertEqual(rag_pdf_tools.pymupdf4llm_ocr_page_workers(), 4)
            os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = "0"
            self.assertEqual(rag_pdf_tools.pymupdf4llm_ocr_page_workers(), 1)
        finally:
            if original_worker_count is None:
                os.environ.pop("RAG_PDF_OCR_PAGE_WORKERS", None)
            else:
                os.environ["RAG_PDF_OCR_PAGE_WORKERS"] = original_worker_count

    def test_pymupdf4llm_uses_complete_process_isolated_pages_when_available(self):
        import rag_pdf_tools

        class FakeDocument:
            page_count = 2

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakePyMuPdf4Llm:
            @staticmethod
            def to_markdown(*_args, **_kwargs):
                raise AssertionError("Sequential fallback should not run after complete parallel OCR")

        original_import = rag_pdf_tools.import_optional_backend
        original_runtime = rag_pdf_tools.ensure_tesseract_runtime
        original_workers = rag_pdf_tools.pymupdf4llm_ocr_page_workers
        original_open = rag_pdf_tools.fitz.open
        original_parallel = rag_pdf_tools._parallel_pymupdf4llm_pages
        try:
            rag_pdf_tools.import_optional_backend = lambda _name: FakePyMuPdf4Llm
            rag_pdf_tools.ensure_tesseract_runtime = lambda: {"available": True}
            rag_pdf_tools.pymupdf4llm_ocr_page_workers = lambda: 4
            rag_pdf_tools.fitz.open = lambda _path: FakeDocument()
            rag_pdf_tools._parallel_pymupdf4llm_pages = lambda path, count, dpi, workers: [
                {"page": 1, "text": "First", "kind": "markdown_page"},
                {"page": 2, "text": "Second", "kind": "markdown_page"},
            ]
            pages, count = rag_pdf_tools.get_pages_with_pymupdf4llm(Path("scanned.pdf"))
        finally:
            rag_pdf_tools.import_optional_backend = original_import
            rag_pdf_tools.ensure_tesseract_runtime = original_runtime
            rag_pdf_tools.pymupdf4llm_ocr_page_workers = original_workers
            rag_pdf_tools.fitz.open = original_open
            rag_pdf_tools._parallel_pymupdf4llm_pages = original_parallel
        self.assertEqual(count, 2)
        self.assertEqual([row["text"] for row in pages], ["First", "Second"])
        self.assertEqual(
            rag_pdf_tools.pymupdf4llm_execution_evidence(pages),
            {
                "requested_workers": 4,
                "actual_workers": 2,
                "mode": "parallel_process_isolated",
                "fallback_reason": "",
            },
        )

    def test_pymupdf4llm_discards_failed_parallel_result_and_falls_back_sequentially(self):
        import rag_pdf_tools

        class FakeDocument:
            page_count = 2

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakePyMuPdf4Llm:
            @staticmethod
            def to_markdown(*_args, **_kwargs):
                return [
                    {"metadata": {"page": 1}, "text": "Sequential first"},
                    {"metadata": {"page": 2}, "text": "Sequential second"},
                ]

        original_import = rag_pdf_tools.import_optional_backend
        original_runtime = rag_pdf_tools.ensure_tesseract_runtime
        original_workers = rag_pdf_tools.pymupdf4llm_ocr_page_workers
        original_open = rag_pdf_tools.fitz.open
        original_parallel = rag_pdf_tools._parallel_pymupdf4llm_pages
        try:
            rag_pdf_tools.import_optional_backend = lambda _name: FakePyMuPdf4Llm
            rag_pdf_tools.ensure_tesseract_runtime = lambda: {"available": True}
            rag_pdf_tools.pymupdf4llm_ocr_page_workers = lambda: 4
            rag_pdf_tools.fitz.open = lambda _path: FakeDocument()
            rag_pdf_tools._parallel_pymupdf4llm_pages = lambda *_args: (_ for _ in ()).throw(RuntimeError("worker failure"))
            pages, count = rag_pdf_tools.get_pages_with_pymupdf4llm(Path("scanned.pdf"))
        finally:
            rag_pdf_tools.import_optional_backend = original_import
            rag_pdf_tools.ensure_tesseract_runtime = original_runtime
            rag_pdf_tools.pymupdf4llm_ocr_page_workers = original_workers
            rag_pdf_tools.fitz.open = original_open
            rag_pdf_tools._parallel_pymupdf4llm_pages = original_parallel
        self.assertEqual(count, 2)
        self.assertEqual([row["text"] for row in pages], ["Sequential first", "Sequential second"])
        evidence = rag_pdf_tools.pymupdf4llm_execution_evidence(pages)
        self.assertEqual(evidence["requested_workers"], 4)
        self.assertEqual(evidence["actual_workers"], 1)
        self.assertEqual(evidence["mode"], "sequential_after_parallel_fallback")
        self.assertIn("RuntimeError: worker failure", evidence["fallback_reason"])

    def test_literal_probe_allows_whitespace_only_pdf_line_wrapping(self):
        rows = pipeline.literal_eval(
            "The negative cultural representations\n   of the poor whites have long-lasting effects.",
            [{"kind": "exact_phrase", "expected_phrase": "The negative cultural representations of the poor whites have long-lasting effects."}],
        )
        self.assertEqual(rows[0]["status"], "pass")
        self.assertEqual(rows[0]["match_mode"], "whitespace_normalized")
        self.assertEqual(rows[0]["char_position"], -1)

    def test_literal_probe_still_fails_when_non_whitespace_text_differs(self):
        rows = pipeline.literal_eval(
            "The negative cultural representations of the poor whites have short-lived effects.",
            [{"kind": "exact_phrase", "expected_phrase": "The negative cultural representations of the poor whites have long-lasting effects."}],
        )
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(rows[0]["match_mode"], "missing")

    def test_prepare_pdf_public_boundary_rejects_non_pdf_before_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            text_file = root / "not-a-pdf.txt"
            text_file.write_text("not a pdf", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a PDF"):
                pipeline.prepare_pdf(text_file, root / "output", SimpleNamespace())

    def test_validate_pdf_inputs_keeps_valid_pdfs_and_ignores_other_entries(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_pdf = root / "good.pdf"
            valid_pdf.write_bytes(b"%PDF-1.7\n%stub\n")
            text_file = root / "notes.txt"
            text_file.write_text("not a pdf", encoding="utf-8")
            empty_pdf = root / "empty.pdf"
            empty_pdf.write_bytes(b"")

            files, report = app.validate_pdf_inputs([str(valid_pdf), str(text_file), str(empty_pdf)])

            self.assertEqual(files, [str(valid_pdf)])
            self.assertIsNone(report)

    def test_gradio_embedder_model_choices_cover_multiple_provider_catalogs(self):
        import rag_pdf_gradio_app as app

        self.assertIn(
            "all-MiniLM-L6-v2",
            app.anythingllm_embedder_model_choices("anythingllm", "all-MiniLM-L6-v2"),
        )
        self.assertIn(
            "text-embedding-3-small",
            app.anythingllm_embedder_model_choices("openai", "text-embedding-3-small"),
        )
        self.assertIn(
            "gemini-embedding-2",
            app.anythingllm_embedder_model_choices("gemini", "gemini-embedding-2"),
        )
        self.assertIn(
            "openai/text-embedding-3-small",
            app.anythingllm_embedder_model_choices("openrouter", "openai/text-embedding-3-small"),
        )
        generic_openai_choices = app.anythingllm_embedder_model_choices("generic-openai", "baai/bge-m3")
        self.assertIn("baai/bge-m3", generic_openai_choices)
        self.assertIn("text-embedding-3-small", generic_openai_choices)
        self.assertIn("Portable registry", app.known_embedder_catalog_summary())
        self.assertTrue(any(row.get("model") == "baai/bge-m3" for row in app.portable_catalog_entries()))

    def test_workspace_updates_auto_select_first_workspace(self):
        import rag_pdf_gradio_app as app

        original_local_workspace_choices = app.local_workspace_choices
        original_api_get_json = app.api_get_json
        original_runtime = app.ensure_anythingllm_runtime
        try:
            app.local_workspace_choices = lambda: ([("Workspace A (alpha)", "alpha")], "local ok")
            update, status = app.workspace_update_from_local("loaded")
            self.assertEqual(update["value"], "alpha")
            self.assertIn("loaded", status)

            manual_update, manual_status = app.workspace_update_from_local(
                "loaded", auto_select=False
            )
            self.assertIsNone(manual_update["value"])
            self.assertIn("Select a target workspace explicitly", manual_status)

            app.ensure_anythingllm_runtime = lambda api_url, api_key="", timeout=1.25, autostart_local=False: {
                "status": "reachable",
                "api_url": "http://127.0.0.1:3001",
                "message": "AnythingLLM is reachable at http://127.0.0.1:3001.",
                "start": {"status": "not_attempted"},
            }
            app.api_get_json = lambda api_url, path, api_key: (
                200,
                {"workspaces": [{"name": "Workspace B", "slug": "beta"}]},
            )
            api_update, api_status = app.refresh_workspaces("http://127.0.0.1:3001", "")
            self.assertEqual(api_update["value"], "beta")
            self.assertIn("Auto-selected `beta`", api_status)

            safe_api_update, safe_api_status = app.refresh_workspaces(
                "http://127.0.0.1:3001", "", auto_select=False
            )
            self.assertIsNone(safe_api_update["value"])
            self.assertIn("Select a target workspace explicitly", safe_api_status)
        finally:
            app.local_workspace_choices = original_local_workspace_choices
            app.api_get_json = original_api_get_json
            app.ensure_anythingllm_runtime = original_runtime

    def test_new_document_workspace_is_the_interactive_default_without_auto_creation(self):
        import rag_pdf_gradio_app as app

        original_choices = app.local_workspace_choices
        try:
            app.local_workspace_choices = lambda: ([('Assistant Chats', 'assistant-chats')], 'local ok')
            update, status = app.workspace_update_from_local(
                'loaded', auto_select=False, include_new_document_choice=True
            )
            self.assertEqual(update['value'], app.NEW_DOCUMENT_WORKSPACE_VALUE)
            self.assertEqual(update['choices'][0], (
                app.NEW_DOCUMENT_WORKSPACE_LABEL,
                app.NEW_DOCUMENT_WORKSPACE_VALUE,
            ))
            self.assertIn('created after confirmation', status)
        finally:
            app.local_workspace_choices = original_choices

    def test_confirmed_new_document_workspace_is_created_only_when_run_is_confirmed(self):
        import rag_pdf_gradio_app as app

        original_create = app.create_new_document_workspace
        original_run = app.run_automatic
        original_choices = app.local_workspace_choices
        original_desktop_refresh = app.request_desktop_workspace_refresh
        created = []
        captured = {}
        try:
            def create_workspace(*args):
                created.append(args)
                return {'status': 'created', 'workspace_slug': 'pdf-new-document'}

            def run_automatic(*args, **kwargs):
                self.assertEqual(args, ())
                captured['workspace_slug'] = kwargs['workspace_slug']
                return ('summary', 'files', 'artifacts', 'download-state', 'button', 'readiness', 'timer')

            app.create_new_document_workspace = create_workspace
            app.run_automatic = run_automatic
            app.local_workspace_choices = lambda: ([('PDF new document', 'pdf-new-document')], 'local ok')
            app.request_desktop_workspace_refresh = lambda: {'status': 'not_installed_or_not_running'}
            settings = {field: '' for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update({
                'files': ['C:/example.pdf'],
                'document_label': 'Example PDF',
                'mode': app.MODE_NATIVE_UPLOAD_LABEL,
                'workspace_slug': app.NEW_DOCUMENT_WORKSPACE_VALUE,
            })
            confirmation_settings = settings | {
                'native_upload_scope': app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
                'segment_mode': app.SEGMENT_PASSAGES_LABEL,
                'anythingllm_chunk_size': 768,
                'anythingllm_chunk_overlap': 128,
            }
            confirmation = app.automatic_confirmation_html(confirmation_settings)
            self.assertIn('New workspace for this document', confirmation)
            self.assertEqual(created, [])

            result = app.run_automatic_from_confirmation(settings)
            self.assertEqual(captured['workspace_slug'], 'pdf-new-document')
            self.assertEqual(result[7]['value'], 'pdf-new-document')
            self.assertEqual(len(created), 1)
        finally:
            app.create_new_document_workspace = original_create
            app.run_automatic = original_run
            app.local_workspace_choices = original_choices
            app.request_desktop_workspace_refresh = original_desktop_refresh

    def test_automatic_ocr_preflight_marks_only_strong_scan_evidence_as_likely(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        original_coverage = app.automatic_full_native_text_coverage
        original_runtime = app.unstructured_runtime_status
        profiles = {
            "scan.pdf": {
                "page_count": 12, "sampled_pages": 3, "mean_chars_per_page": 4,
                "image_density": 1.0, "sparse_fraction": 1.0, "ocr_risk_bucket": "high",
            },
            "mixed.pdf": {
                "page_count": 8, "sampled_pages": 3, "mean_chars_per_page": 320,
                "image_density": .1, "sparse_fraction": .4, "ocr_risk_bucket": "possible",
            },
        }
        calls = []
        try:
            app.automatic_timing_document_profile = lambda files, **_kwargs: profiles[Path(files[0]).name]
            app.automatic_full_native_text_coverage = lambda _path: {
                "status": "verified", "low_text_pages": [], "image_backed_low_text_pages": [],
            }
            app.unstructured_runtime_status = lambda strategy: calls.append(strategy) or {
                "backend_available": False,
                "tesseract_available": False,
            }
            manifest = app.automatic_ocr_preflight_manifest(
                ["scan.pdf", "mixed.pdf"], backend_mode="Automatic"
            )
        finally:
            app.automatic_timing_document_profile = original_profile
            app.automatic_full_native_text_coverage = original_coverage
            app.unstructured_runtime_status = original_runtime

        self.assertEqual([row["name"] for row in manifest["likely_files"]], ["scan.pdf"])
        self.assertEqual([row["name"] for row in manifest["possible_files"]], ["mixed.pdf"])
        self.assertEqual(manifest["runtime"]["status"], "unavailable")
        self.assertEqual(calls, ["hi_res"])
        self.assertIn("withheld from AnythingLLM upload", " ".join(manifest["warnings"]))

    def test_automatic_ocr_preflight_defers_runtime_for_text_first_batch(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        original_coverage = app.automatic_full_native_text_coverage
        original_runtime = app.unstructured_runtime_status
        try:
            app.automatic_timing_document_profile = lambda files, **_kwargs: {
                "page_count": 20, "sampled_pages": 3, "mean_chars_per_page": 1400,
                "image_density": 0, "sparse_fraction": 0, "ocr_risk_bucket": "low",
            }
            app.automatic_full_native_text_coverage = lambda _path: {
                "status": "verified", "low_text_pages": [], "image_backed_low_text_pages": [],
            }
            calls = []
            app.unstructured_runtime_status = lambda *_args, **_kwargs: calls.append("probe") or {
                "backend_available": True,
                "tesseract_available": True,
            }
            manifest = app.automatic_ocr_preflight_manifest(["text.pdf"], backend_mode="Automatic")
        finally:
            app.automatic_timing_document_profile = original_profile
            app.automatic_full_native_text_coverage = original_coverage
            app.unstructured_runtime_status = original_runtime

        self.assertEqual(manifest["status"], "clear")
        self.assertEqual(manifest["runtime"]["status"], "deferred_native_text_clear")
        self.assertEqual(calls, [])

    def test_automatic_ocr_preflight_defers_runtime_for_sparse_image_pages(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        original_coverage = app.automatic_full_native_text_coverage
        original_runtime = app.unstructured_runtime_status
        calls = []
        try:
            app.automatic_timing_document_profile = lambda files, **_kwargs: {
                "page_count": 20, "sampled_pages": 3, "mean_chars_per_page": 1400,
                "image_density": 0, "sparse_fraction": 0, "ocr_risk_bucket": "low",
            }
            app.automatic_full_native_text_coverage = lambda _path: {
                "status": "verified",
                "low_text_pages": [{"page": 17, "native_text_characters": 0, "image_count": 1}],
                "image_backed_low_text_pages": [{"page": 17, "native_text_characters": 0, "image_count": 1}],
            }
            app.unstructured_runtime_status = lambda *_args, **_kwargs: calls.append("probe") or {
                "backend_available": True, "tesseract_available": True,
            }
            manifest = app.automatic_ocr_preflight_manifest(["mixed.pdf"], backend_mode="Automatic")
        finally:
            app.automatic_timing_document_profile = original_profile
            app.automatic_full_native_text_coverage = original_coverage
            app.unstructured_runtime_status = original_runtime

        self.assertEqual(manifest["files"][0]["risk"], "possible")
        self.assertEqual(manifest["files"][0]["low_text_page_count"], 1)
        self.assertEqual(manifest["runtime"]["status"], "deferred_native_text_clear")
        self.assertEqual(calls, [])

    def test_automatic_confirmation_renders_ocr_preflight_warning(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_confirmation_html({
            "mode": app.MODE_LOCAL_ONLY_LABEL,
            "segment_mode": app.SEGMENT_PASSAGES_LABEL,
            "target_passage_length": 750,
            "ocr_preflight_manifest": {
                "status": "warning",
                "warnings": ["1 PDF looks scan-only from a native sample."],
            },
        })
        self.assertIn("OCR preflight", rendered)
        self.assertIn("scan-only", rendered)

    def test_guarded_desktop_refresh_is_once_per_completed_upload_and_never_for_failed_result(self):
        import rag_pdf_gradio_app as app

        original_run = app.run_automatic
        original_refresh = app.request_desktop_workspace_refresh
        refreshes = []
        settings = {field: '' for field in app.AUTOMATIC_RUN_FIELDS}
        settings.update({
            'files': ['C:/example.pdf'],
            'document_label': 'Example PDF',
            'mode': app.MODE_NATIVE_UPLOAD_LABEL,
            'workspace_slug': 'existing-workspace',
        })
        successful = (
            {'value': '<div class="run-summary-panel"></div>'}, [], '', [], {}, '',
            {'value': '<div data-run-state="successful"></div>'},
        )
        failed = (
            {'value': '<div class="run-summary-panel summary-status error"></div>'}, [], '', [], {}, '',
            {'value': '<div data-run-state="failed"></div>'},
        )
        warning = (
            {'value': '<div class="run-summary-panel summary-status warning"></div>'}, [], '', [], {}, '',
            {'value': '<div class="run-summary-panel summary-status warning"></div>'},
        )
        try:
            app.request_desktop_workspace_refresh = lambda: refreshes.append('refresh') or {'status': 'refreshed'}
            app.run_automatic = lambda *args, **kwargs: successful
            refreshed_outputs = app.run_automatic_from_confirmation(settings)
            self.assertEqual(refreshes, ['refresh'])
            self.assertIn("Desktop refresh: renderer reloaded", refreshed_outputs[6]["value"])

            refreshes.clear()
            app.run_automatic = lambda *args, **kwargs: failed
            app.run_automatic_from_confirmation(settings)
            self.assertEqual(refreshes, [])

            app.run_automatic = lambda *args, **kwargs: warning
            app.run_automatic_from_confirmation(settings)
            self.assertEqual(refreshes, ['refresh'])
        finally:
            app.run_automatic = original_run
            app.request_desktop_workspace_refresh = original_refresh

    def test_completed_native_upload_refreshes_when_gradio_returns_summary_status_warning(self):
        import rag_pdf_gradio_app as app

        rendered = {'value': '<div class="run-summary-panel summary-status warning"></div>'}
        outputs = ({'value': 'summary'}, [], '', [], {}, '', rendered)

        self.assertTrue(app.completed_native_upload_requires_desktop_refresh(outputs))

    def test_default_upload_layout_is_visible_to_the_desktop_documents_drawer(self):
        import rag_pdf_gradio_app as app

        updates = app.reset_automatic_run_settings_to_defaults()

        # The dedicated-folder checkbox remains opt-in so root-level records
        # stay visible in AnythingLLM Desktop's Documents drawer.
        self.assertFalse(updates[15]["value"])

    def test_desktop_refresh_result_never_claims_that_the_documents_drawer_is_confirmed(self):
        import rag_pdf_gradio_app as app

        result = app.desktop_refresh_result_html({"status": "refreshed"})

        self.assertIn("renderer reloaded", result)
        self.assertIn("verify the Documents drawer separately", result)
        self.assertNotIn("drawer updated", result)

    def test_background_reconciliation_preserves_a_valid_workspace_and_stays_observational(self):
        import rag_pdf_gradio_app as app

        original_choices = app.local_workspace_choices
        original_snapshot = app.workspace_ingestion_observer_snapshot
        original_readiness_report = app.native_upload_readiness_report
        original_readiness_html = app.native_upload_readiness_html
        original_settings = app.refresh_anythingllm_settings
        original_reference = app.anythingllm_settings_reference_html
        try:
            app.local_workspace_choices = lambda: ([('Workspace A', 'workspace-a')], 'local workspace list refreshed')
            app.workspace_ingestion_observer_snapshot = lambda slug, api_url: {
                'observed_at': '2026-07-13T12:00:00',
                'api': {'reachable': True},
                'workspace_documents': 3,
                'embedded_vectors': 9,
                'database_status': 'observed',
            }
            app.native_upload_readiness_report = lambda *args, **kwargs: {'workspace_slug': 'workspace-a'}
            app.native_upload_readiness_html = lambda report: f"readiness:{report['workspace_slug']}"
            app.refresh_anythingllm_settings = lambda *args: ('settings', 'chunk', 'overlap', 'max', 'recommended', 'engine', 'model')
            app.anythingllm_settings_reference_html = lambda: 'reference'

            result = app.refresh_background_reconciliation(
                'http://127.0.0.1:3001', '', 'workspace-a', True, '512', '0', 1000
            )

            self.assertEqual(len(result), 12)
            self.assertEqual(result[0]['value'], 'workspace-a')
            self.assertIn('Background sync refreshed', result[1])
            self.assertEqual(result[2], 'readiness:workspace-a')
            self.assertIn('does not declare an embedding complete', result[3])
            self.assertEqual(result[-1], 'reference')
        finally:
            app.local_workspace_choices = original_choices
            app.workspace_ingestion_observer_snapshot = original_snapshot
            app.native_upload_readiness_report = original_readiness_report
            app.native_upload_readiness_html = original_readiness_html
            app.refresh_anythingllm_settings = original_settings
            app.anythingllm_settings_reference_html = original_reference

    def test_background_reconciliation_never_calls_desktop_refresh_bridge(self):
        import rag_pdf_gradio_app as app

        original_choices = app.local_workspace_choices
        original_snapshot = app.workspace_ingestion_observer_snapshot
        original_readiness_report = app.native_upload_readiness_report
        original_readiness_html = app.native_upload_readiness_html
        original_settings = app.refresh_anythingllm_settings
        original_reference = app.anythingllm_settings_reference_html
        original_refresh = app.request_desktop_workspace_refresh
        refreshes = []
        try:
            app.local_workspace_choices = lambda: ([('Workspace A', 'workspace-a')], 'local ok')
            app.workspace_ingestion_observer_snapshot = lambda slug, api_url: {
                'observed_at': '2026-07-14T21:00:00',
                'api': {'reachable': True},
                'workspace_documents': 3,
                'embedded_vectors': 9,
                'database_status': 'observed',
            }
            app.native_upload_readiness_report = lambda *args, **kwargs: {'workspace_slug': 'workspace-a'}
            app.native_upload_readiness_html = lambda report: 'readiness'
            app.refresh_anythingllm_settings = lambda *args: ('settings', 'chunk', 'overlap', 'max', 'recommended', 'engine', 'model')
            app.anythingllm_settings_reference_html = lambda: 'reference'
            app.request_desktop_workspace_refresh = lambda: refreshes.append('called') or {'status': 'refreshed'}

            app.refresh_background_reconciliation(
                'http://127.0.0.1:3001', '', 'workspace-a', True, '512', '0', 1000
            )
            self.assertEqual(refreshes, [])
        finally:
            app.local_workspace_choices = original_choices
            app.workspace_ingestion_observer_snapshot = original_snapshot
            app.native_upload_readiness_report = original_readiness_report
            app.native_upload_readiness_html = original_readiness_html
            app.refresh_anythingllm_settings = original_settings
            app.anythingllm_settings_reference_html = original_reference
            app.request_desktop_workspace_refresh = original_refresh

    def test_background_reconciliation_reverts_a_missing_workspace_to_new_workspace_choice(self):
        import rag_pdf_gradio_app as app

        original_choices = app.local_workspace_choices
        try:
            app.local_workspace_choices = lambda: ([('Workspace A', 'workspace-a')], 'local ok')
            update, _status = app._background_workspace_update('deleted-workspace')
            self.assertEqual(update['value'], app.NEW_DOCUMENT_WORKSPACE_VALUE)
        finally:
            app.local_workspace_choices = original_choices

    def test_desktop_refresh_bridge_is_narrow_authenticated_and_guarded(self):
        import rag_pdf_gradio_app as app

        class FakeResponse:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true, "action": "refresh-workspaces"}'

        original_descriptor_path = app.desktop_refresh_bridge_descriptor_path
        original_urlopen = app.urllib.request.urlopen
        observed = {}
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor = Path(temp_dir) / app.DESKTOP_REFRESH_BRIDGE_FILENAME
                descriptor.write_text(json.dumps({
                    "marker": app.DESKTOP_REFRESH_BRIDGE_MARKER,
                    "schemaVersion": 1,
                    "draftGuardVersion": app.DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION,
                    "port": 43123,
                    "token": "x" * 43,
                }), encoding="utf-8")
                app.desktop_refresh_bridge_descriptor_path = lambda: descriptor

                def fake_urlopen(request, timeout):
                    observed["url"] = request.full_url
                    observed["method"] = request.get_method()
                    observed["token"] = request.get_header("X-anythingllm-pdf-prep-bridge")
                    observed["timeout"] = timeout
                    return FakeResponse()

                app.urllib.request.urlopen = fake_urlopen
                result = app.request_desktop_workspace_refresh(timeout_seconds=0.25)

            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(observed["url"], "http://127.0.0.1:43123/v1/refresh-workspaces")
            self.assertEqual(observed["method"], "POST")
            self.assertEqual(observed["token"], "x" * 43)
            self.assertEqual(observed["timeout"], 0.25)
            self.assertNotIn("token", result)
            self.assertIn("active AnythingLLM Desktop workspace sidebar", app.desktop_workspace_refresh_note(result))
        finally:
            app.desktop_refresh_bridge_descriptor_path = original_descriptor_path
            app.urllib.request.urlopen = original_urlopen

    def test_outdated_desktop_draft_guard_is_fail_closed_before_http_request(self):
        import rag_pdf_gradio_app as app

        original_descriptor_path = app.desktop_refresh_bridge_descriptor_path
        original_urlopen = app.urllib.request.urlopen
        called = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor = Path(temp_dir) / app.DESKTOP_REFRESH_BRIDGE_FILENAME
                descriptor.write_text(json.dumps({
                    "marker": app.DESKTOP_REFRESH_BRIDGE_MARKER,
                    "schemaVersion": 1,
                    "port": 43123,
                    "token": "x" * 43,
                }), encoding="utf-8")
                app.desktop_refresh_bridge_descriptor_path = lambda: descriptor
                app.urllib.request.urlopen = lambda *args, **kwargs: called.append(True)
                result = app.request_desktop_workspace_refresh()
            self.assertEqual(result["status"], "draft_guard_outdated")
            self.assertEqual(called, [])
            self.assertIn("outdated", app.desktop_workspace_refresh_note(result))
        finally:
            app.desktop_refresh_bridge_descriptor_path = original_descriptor_path
            app.urllib.request.urlopen = original_urlopen

    def test_desktop_refresh_descriptor_rejects_non_capability_tokens_before_http_request(self):
        import rag_pdf_gradio_app as app

        original_descriptor_path = app.desktop_refresh_bridge_descriptor_path
        original_urlopen = app.urllib.request.urlopen
        called = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor = Path(temp_dir) / app.DESKTOP_REFRESH_BRIDGE_FILENAME
                descriptor.write_text(json.dumps({
                    "marker": app.DESKTOP_REFRESH_BRIDGE_MARKER,
                    "schemaVersion": 1,
                    "draftGuardVersion": app.DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION,
                    "port": 43123,
                    # Correct length but not a 32-byte base64url token.
                    "token": "!" * 43,
                }), encoding="utf-8")
                app.desktop_refresh_bridge_descriptor_path = lambda: descriptor
                app.urllib.request.urlopen = lambda *args, **kwargs: called.append(True)
                result = app.request_desktop_workspace_refresh()

            self.assertEqual(result["status"], "invalid_descriptor")
            self.assertEqual(called, [])
        finally:
            app.desktop_refresh_bridge_descriptor_path = original_descriptor_path
            app.urllib.request.urlopen = original_urlopen

    def test_stale_desktop_bridge_descriptor_never_opens_a_loopback_connection(self):
        import rag_pdf_gradio_app as app

        original_descriptor_path = app.desktop_refresh_bridge_descriptor_path
        original_process_live = app.desktop_bridge_process_is_live
        original_urlopen = app.urllib.request.urlopen
        called = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor = Path(temp_dir) / app.DESKTOP_REFRESH_BRIDGE_FILENAME
                descriptor.write_text(json.dumps({
                    "marker": app.DESKTOP_REFRESH_BRIDGE_MARKER,
                    "schemaVersion": 1,
                    "draftGuardVersion": app.DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION,
                    "pid": 424242,
                    "port": 43123,
                    "token": "x" * 43,
                }), encoding="utf-8")
                app.desktop_refresh_bridge_descriptor_path = lambda: descriptor
                app.desktop_bridge_process_is_live = lambda _pid: False
                app.urllib.request.urlopen = lambda *args, **kwargs: called.append(True)
                result = app.request_desktop_workspace_refresh()

            self.assertEqual(result["status"], "not_installed_or_not_running")
            self.assertTrue(result["stale_descriptor"])
            self.assertEqual(called, [])
        finally:
            app.desktop_refresh_bridge_descriptor_path = original_descriptor_path
            app.desktop_bridge_process_is_live = original_process_live
            app.urllib.request.urlopen = original_urlopen

    def test_desktop_bridge_draft_rejection_is_reported_without_retrying(self):
        import rag_pdf_gradio_app as app

        original_descriptor_path = app.desktop_refresh_bridge_descriptor_path
        original_urlopen = app.urllib.request.urlopen
        calls = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor = Path(temp_dir) / app.DESKTOP_REFRESH_BRIDGE_FILENAME
                descriptor.write_text(json.dumps({
                    "marker": app.DESKTOP_REFRESH_BRIDGE_MARKER,
                    "schemaVersion": 1,
                    "draftGuardVersion": app.DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION,
                    "port": 43123,
                    "token": "x" * 43,
                }), encoding="utf-8")
                app.desktop_refresh_bridge_descriptor_path = lambda: descriptor

                def deferred(*_args, **_kwargs):
                    calls.append(True)
                    raise urllib.error.HTTPError(
                        "http://127.0.0.1:43123/v1/refresh-workspaces",
                        409,
                        "Conflict",
                        {},
                        io.BytesIO(b'{"ok": false, "error": "unsent_draft_detected"}'),
                    )

                app.urllib.request.urlopen = deferred
                result = app.request_desktop_workspace_refresh()

            self.assertEqual(result["status"], "draft_protected")
            self.assertEqual(calls, [True])
            self.assertIn("unsent draft text", app.desktop_workspace_refresh_note(result))
        finally:
            app.desktop_refresh_bridge_descriptor_path = original_descriptor_path
            app.urllib.request.urlopen = original_urlopen

    def test_bridge_installer_uses_broad_fail_closed_draft_detection_before_refresh_event(self):
        source = (PROJECT_ROOT / "Install-AnythingLLMDesktopRefreshBridge.ps1").read_text(encoding="utf-8")

        self.assertIn("DRAFT_GUARD_VERSION = 2", source)
        self.assertIn("'[contenteditable]'", source)
        self.assertIn("'[role=\"textbox\"]'", source)
        self.assertIn("inspectionFailed: true", source)
        self.assertLess(source.index("DRAFT_CHECK_SCRIPT"), source.index("REFRESH_SCRIPT, true"))

    def test_bridge_installer_has_read_only_115_compatibility_probe_and_archive_verification(self):
        source = (PROJECT_ROOT / "Install-AnythingLLMDesktopRefreshBridge.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$Validate", source)
        self.assertIn("anythingllm-1.15-main-window-x", source)
        self.assertIn("$versionText -match '^[vV]?1\\.15\\.0(?:-r\\d+)?$'", source)
        self.assertIn("$packedBridgeEntries.Count -ne 1", source)
        self.assertIn("-Validate is read-only", source)
        self.assertIn('const BRIDGE_REVISION = "drawer-audit-v2"', source)
        self.assertIn("BridgeDiagnosticsPresent", source)
        self.assertIn("CurrentBridgeRevision", source)

    def test_automatic_completion_requires_workspace_evidence_for_green_status(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(app.automatic_completion([{}], False)["state"], "successful")
        successful_upload = {
            "api_upload_status": "complete",
            "post_upload_verification_status": "pass",
            "anythingllm_runtime_validation_status": "pass",
        }
        successful_completion = app.automatic_completion([successful_upload], True)
        self.assertEqual(successful_completion["state"], "successful")
        self.assertIn("Ready for retrieval", successful_completion["message"])
        self.assertIn("Documents drawer", successful_completion["message"])
        runtime_warning = dict(successful_upload, anythingllm_runtime_validation_status="vector_retrieval_failed")
        runtime_completion = app.automatic_completion([runtime_warning], True)
        self.assertEqual(runtime_completion["state"], "warning")
        self.assertEqual(runtime_completion["code"], "AUTO-RETRIEVAL-VERIFY-001")
        self.assertNotIn("Storage verified", runtime_completion["message"])
        chat_timeout = dict(successful_upload, anythingllm_runtime_validation_status="pass_with_chat_timeout")
        timeout_completion = app.automatic_completion([chat_timeout], True)
        self.assertEqual(timeout_completion["state"], "warning")
        self.assertIn("Stored, but retrieval is unverified", timeout_completion["message"])
        vector_timeout = dict(successful_upload, anythingllm_runtime_validation_status="vector_runtime_timeout")
        vector_timeout_completion = app.automatic_completion([vector_timeout], True)
        self.assertEqual(vector_timeout_completion["state"], "warning")
        self.assertIn("Stored, but retrieval is unverified", vector_timeout_completion["message"])
        provider_auth = dict(successful_upload, anythingllm_runtime_validation_status="blocked_provider_authentication")
        provider_auth_completion = app.automatic_completion([provider_auth], True)
        self.assertEqual(provider_auth_completion["code"], "AUTO-RETRIEVAL-AUTH-001")
        self.assertIn("Stored, but retrieval is unavailable", provider_auth_completion["message"])
        missing_workspace_evidence = dict(successful_upload, post_upload_verification_status="not_checked")
        self.assertEqual(app.automatic_completion([missing_workspace_evidence], True)["state"], "failed")
        expired_openrouter_key = {
            "anythingllm_embedder_warning_code": "AUTO-OPENROUTER-KEY-REVERIFY-001"
        }
        completion = app.automatic_completion([expired_openrouter_key], True)
        self.assertEqual(completion["code"], "AUTO-OPENROUTER-KEY-REVERIFY-001")
        self.assertIn("Update it in AnythingLLM Settings", completion["message"])
        metadata_review = dict(successful_upload, post_upload_verification_status="review")
        self.assertEqual(app.automatic_completion([metadata_review], True)["state"], "warning")
        concurrent_write = dict(successful_upload, post_upload_verification_status="concurrent_write_ambiguous")
        concurrent_completion = app.automatic_completion([concurrent_write], True)
        self.assertEqual(concurrent_completion["state"], "warning")
        self.assertIn("Prepared files remain available", concurrent_completion["message"])
        pending_reconciliation = app.automatic_completion([{
            "api_upload_status": "reconciliation_pending",
            "post_upload_verification_status": "docs_without_vectors",
        }], True)
        self.assertEqual(pending_reconciliation["state"], "warning")
        self.assertEqual(pending_reconciliation["code"], "AUTO-EMBEDDING-RECONCILE-001")
        self.assertIn("Local preparation is complete", pending_reconciliation["message"])

    def test_ocr_withheld_terminal_phase_never_claims_searchable_vectors(self):
        import rag_pdf_gradio_app as app

        completion = app.automatic_completion([{
            "pdf": "mixed-text.pdf",
            "api_upload_status": "skipped_needs_ocr_review",
        }], True)

        self.assertEqual(completion["state"], "warning")
        self.assertEqual(completion["code"], "AUTO-OCR-REVIEW-001")
        phase = app.automatic_completion_phase(completion, True)
        self.assertIn("upload withheld", phase)
        self.assertNotIn("vectors verified", phase.casefold())

    def test_photographed_spread_hold_is_not_described_as_missing_ocr(self):
        import rag_pdf_gradio_app as app

        completion = app.automatic_completion([{
            "pdf": "open-book-photo.pdf",
            "api_upload_status": "skipped_needs_ocr_review",
            "api_upload_warning": (
                "AnythingLLM upload was withheld because photographed spreads require visual review."
            ),
        }], True)

        self.assertEqual(completion["code"], "AUTO-LAYOUT-REVIEW-001")
        self.assertIn("photographed spreads require visual review", completion["message"])
        self.assertNotIn("reliable OCR is required", completion["message"])
        self.assertEqual(
            app.automatic_completion_phase(completion, True),
            "Local preparation complete — upload withheld for layout review",
        )

    def test_automatic_completion_names_chat_retrieval_as_unverified(self):
        import rag_pdf_gradio_app as app

        completion = app.automatic_completion([{
            "api_upload_status": "complete",
            "post_upload_verification_status": "pass",
            "anythingllm_runtime_validation_status": "chat_citation_failed",
        }], True)

        self.assertEqual(completion["state"], "warning")
        self.assertEqual(completion["code"], "AUTO-RETRIEVAL-CHAT-001")
        self.assertIn("chat retrieval", completion["message"])
        self.assertNotIn("one or more runtime checks", completion["message"])

    def test_green_completion_keeps_drawer_visibility_separate_from_retrieval(self):
        import rag_pdf_gradio_app as app

        successful_upload = {
            "api_upload_status": "complete",
            "post_upload_verification_status": "pass",
            "anythingllm_runtime_validation_status": "pass",
        }
        completion = app.automatic_completion([successful_upload], True)

        self.assertIn("Documents drawer visibility is reported separately", completion["message"])
        self.assertNotIn("will request", completion["message"])

    def test_top_anythingllm_status_distinguishes_running_from_unavailable_desktop(self):
        import rag_pdf_gradio_app as app

        original_health = app.anythingllm_observer_api_health
        try:
            app.anythingllm_observer_api_health = lambda _url: {"reachable": True, "http_status": 200, "error": ""}
            running = app.anythingllm_startup_status_html("http://127.0.0.1:3001")
            app.anythingllm_observer_api_health = lambda _url: {"reachable": False, "http_status": None, "error": "refused"}
            unavailable = app.anythingllm_startup_status_html("http://127.0.0.1:3001")
        finally:
            app.anythingllm_observer_api_health = original_health

        self.assertEqual(running, "")
        self.assertIn("Please start AnythingLLM Desktop", unavailable)
        self.assertIn("Refresh Status", unavailable)

    def test_startup_status_view_detects_a_runtime_loss_after_page_load(self):
        import rag_pdf_gradio_app as app

        original_health = app.anythingllm_observer_api_health
        try:
            app.anythingllm_observer_api_health = lambda _url: {"reachable": True, "http_status": 200, "error": ""}
            running_html, running_module = app.anythingllm_startup_status_view("http://127.0.0.1:3001")
            app.anythingllm_observer_api_health = lambda _url: {"reachable": False, "http_status": None, "error": "refused"}
            unavailable_html, unavailable_module = app.anythingllm_startup_status_view("http://127.0.0.1:3001")
        finally:
            app.anythingllm_observer_api_health = original_health

        self.assertEqual(running_html, "")
        self.assertFalse(running_module["visible"])
        self.assertIn("AnythingLLM is not available", unavailable_html)
        self.assertTrue(unavailable_module["visible"])

    def test_app_open_initialization_uses_one_guarded_local_start_attempt(self):
        import rag_pdf_gradio_app as app

        original_workspaces = app.load_workspaces_on_open
        original_readiness = app.native_upload_readiness_report
        original_readiness_html = app.native_upload_readiness_html
        captured = {}
        try:
            app.load_workspaces_on_open = lambda: ({"value": "new-document"}, "local workspaces")

            def fake_readiness(api_url, api_key, workspace_slug, **kwargs):
                captured.update(
                    api_url=api_url,
                    api_key=api_key,
                    workspace_slug=workspace_slug,
                    kwargs=kwargs,
                )
                return {
                    "runtime_api_url": "http://127.0.0.1:3001",
                    "runtime_api_reachable": True,
                    "runtime_start_status": "started",
                }

            app.native_upload_readiness_report = fake_readiness
            app.native_upload_readiness_html = lambda report: "readiness:started"
            result = app.initialize_anythingllm_on_app_open(
                "http://127.0.0.1:3001", "existing-key", ""
            )
        finally:
            app.load_workspaces_on_open = original_workspaces
            app.native_upload_readiness_report = original_readiness
            app.native_upload_readiness_html = original_readiness_html

        self.assertEqual(captured["workspace_slug"], "new-document")
        self.assertTrue(captured["kwargs"]["autostart_runtime"])
        self.assertFalse(captured["kwargs"]["verify_authentication"])
        self.assertEqual(result[2], "readiness:started")
        self.assertEqual(result[3], "")
        self.assertFalse(result[4]["visible"])

    def test_startup_status_timer_is_passive_and_low_frequency(self):
        import rag_pdf_gradio_app as app

        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertGreaterEqual(app.ANYTHINGLLM_STARTUP_STATUS_INTERVAL_SECONDS, 10)
        self.assertIn("anythingllm_startup_status_timer = gr.Timer(", source)
        self.assertIn("anythingllm_startup_status_timer.tick(", source)
        self.assertIn("fn=anythingllm_startup_status_view", source)
        self.assertIn("fn=initialize_anythingllm_on_app_open", source)

    def test_runtime_preflight_records_autostart_without_persisting_a_key(self):
        import rag_pdf_gradio_app as app

        report = {
            "runtime_api_url": "http://127.0.0.1:3001",
            "runtime_api_reachable": True,
            "runtime_start_status": "started",
            "runtime_start_message": "Started AnythingLLM Desktop from the installed executable.",
            "authenticated": True,
            "credential": "must-not-be-written",
        }
        phase, detail = app.automatic_runtime_start_notice(report)
        self.assertEqual(phase, "AnythingLLM Desktop started automatically")
        self.assertIn("started before PDF preparation", detail)
        self.assertEqual(app.automatic_runtime_start_notice({"runtime_start_status": "already_running"}), ("", ""))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = app.record_automatic_runtime_preflight(Path(tmpdir), report)
            saved = path.read_text(encoding="utf-8")
        self.assertIn("runtime_start_status", saved)
        self.assertNotIn("must-not-be-written", saved)

    def test_browser_watchdog_reports_a_lost_localhost_app_connection(self):
        import rag_pdf_gradio_app as app
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse
        from fastapi.testclient import TestClient

        self.assertIn('fetch("/healthz"', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('Connection to the PDF app was lost.', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('Connection restored.', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('Start or restart the PDF app server.', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertNotIn('then refresh this page', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertNotIn('Refresh this page before starting a new run', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('document.addEventListener("change"', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('input.type !== "file"', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('window.ragLocalServerConnectionWatchdogInstalled', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('ragLocalServerConnectionWatchdog = "installed"', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertNotIn("ragLocalServerWasOffline", app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn("consecutiveConnectionFailures >= 4", app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('rag-server-connection-dismiss', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('}, 4000);', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('id="rag-local-server-connection-watchdog-style"', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('#rag-server-connection-banner .rag-server-connection-dismiss', app.APP_CONNECTION_WATCHDOG_HEAD)
        self.assertIn('#rag-server-connection-banner[hidden]', app.APP_CONNECTION_WATCHDOG_HEAD)
        # Gradio's launch(js=...) contract requires a callback. Turning this
        # into an IIFE makes Gradio invoke it as an event preprocessor and can
        # erase every input for confirmation-click handlers.
        self.assertTrue(app.APP_JS.lstrip().startswith("() => {"))
        self.assertIn('.pdf-upload-input .file-preview', app.APP_CSS)
        self.assertIn('.toast-wrap', app.APP_CSS)
        self.assertIn('top: 96px !important;', app.APP_CSS)

        test_app = FastAPI()
        test_app.add_middleware(app.LocalServerConnectionWatchdogMiddleware)

        @test_app.get("/")
        def root_page():
            return HTMLResponse(
                "<html><head><title>PDF app</title></head><body></body></html>",
                headers={"X-Local-App-Contract": "preserved"},
            )

        @test_app.get("/already-instrumented")
        def already_instrumented_page():
            return HTMLResponse(
                '<html><head><script id="rag-local-server-connection-watchdog"></script></head><body></body></html>'
            )

        @test_app.get("/not-html")
        def not_html():
            return {"ok": True}

        client = TestClient(test_app)
        initial_document = client.get("/").text
        self.assertEqual(initial_document.count('id="rag-local-server-connection-watchdog"'), 1)
        self.assertLess(
            initial_document.index('ragLocalServerConnectionWatchdogInstalled'),
            initial_document.lower().index("</head>"),
        )
        self.assertNotIn('ragLocalServerConnectionWatchdogInstalled', client.get("/not-html").text)
        self.assertEqual(client.get("/").headers["X-Local-App-Contract"], "preserved")
        # A future route must never inherit an extra script merely because it
        # happens to return HTML, and pre-instrumented HTML remains exactly
        # one watchdog rather than accumulating copies on middleware changes.
        self.assertNotIn(
            'ragLocalServerConnectionWatchdogInstalled',
            client.get("/already-instrumented").text,
        )
        self.assertEqual(
            client.get("/already-instrumented").text.count('id="rag-local-server-connection-watchdog"'),
            1,
        )

        # The marker guard is exercised on a root document too: a future
        # Gradio release may provide its own equivalent script, and our
        # middleware must preserve it rather than layering a second watcher.
        preinstrumented_app = FastAPI()
        preinstrumented_app.add_middleware(app.LocalServerConnectionWatchdogMiddleware)

        @preinstrumented_app.get("/")
        def preinstrumented_root_page():
            return HTMLResponse(
                '<html><head><script id="rag-local-server-connection-watchdog"></script></head><body></body></html>',
                headers={"X-Local-App-Contract": "preinstrumented"},
            )

        preinstrumented_response = TestClient(preinstrumented_app).get("/")
        self.assertEqual(preinstrumented_response.status_code, 200)
        self.assertEqual(preinstrumented_response.headers["X-Local-App-Contract"], "preinstrumented")
        self.assertEqual(
            preinstrumented_response.text.count('id="rag-local-server-connection-watchdog"'),
            1,
        )
        self.assertNotIn("ragLocalServerConnectionWatchdogInstalled", preinstrumented_response.text)

    def test_watchdog_health_endpoint_is_lightweight_and_available(self):
        import asyncio
        import rag_pdf_gradio_app as app

        response = asyncio.run(app.local_pdf_app_healthz())
        self.assertEqual(response.status_code, 204)

    def test_refresh_top_anythingllm_status_flashes_only_for_an_unchanged_outage(self):
        import rag_pdf_gradio_app as app

        original_health = app.anythingllm_observer_api_health
        try:
            app.anythingllm_observer_api_health = lambda _url: {"reachable": False, "http_status": None, "error": "refused"}
            unavailable_html, unavailable_module, unavailable_button = app.refresh_anythingllm_startup_status("http://127.0.0.1:3001")
            self.assertIn("AnythingLLM is not available", unavailable_html)
            self.assertTrue(unavailable_module["visible"])
            self.assertEqual(unavailable_button["variant"], "secondary")

            app.anythingllm_observer_api_health = lambda _url: {"reachable": True, "http_status": 200, "error": ""}
            running_html, running_module, running_button = app.refresh_anythingllm_startup_status("http://127.0.0.1:3001")
            self.assertEqual(running_html, "")
            self.assertFalse(running_module["visible"])
            self.assertEqual(running_button["variant"], "secondary")

        finally:
            app.anythingllm_observer_api_health = original_health

    def test_idle_run_timer_is_compact_and_estimate_labeled(self):
        import rag_pdf_gradio_app as app

        idle = app.automatic_run_timing_html(state="ready")
        estimated = app.automatic_run_timing_html(expected_seconds=83, state="ready")

        self.assertIn("Est: 00m00s", idle)
        self.assertIn("Est: 01m23s", estimated)
        self.assertNotIn("Run timer", estimated)

    def test_confirmed_dispatch_passes_expected_seconds_once_by_keyword(self):
        import rag_pdf_gradio_app as app

        original_run = app.run_automatic
        captured = {}
        try:
            app.run_automatic = lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs) or (None,) * 7
            settings = {field: f"value-{index}" for index, field in enumerate(app.AUTOMATIC_RUN_FIELDS)}
            settings.update({
                "expected_seconds": 321,
                "ocr_preflight_manifest": {"status": "ready"},
                "estimate_range": "04m00s - 06m00s",
                "estimate_confidence": "medium confidence",
                "estimate_comparable_runs": 3,
                "_reserved_run_root": "C:/tmp/app-run-confirmed",
                "retain_detailed_evidence": True,
            })
            app.dispatch_confirmed_automatic_run(settings, progress="progress-token")
        finally:
            app.run_automatic = original_run
        self.assertEqual(captured["args"], ())
        self.assertEqual(captured["kwargs"]["expected_seconds"], 321)
        self.assertEqual(captured["kwargs"]["progress"], "progress-token")
        self.assertEqual(captured["kwargs"]["run_root_override"], "C:/tmp/app-run-confirmed")
        self.assertEqual(captured["kwargs"]["download_segments_folder"], f"value-{len(app.AUTOMATIC_RUN_FIELDS) - 1}")

    def test_runtime_guard_stays_idle_for_local_preparation(self):
        import rag_pdf_gradio_app as app

        probe_calls = []
        state, result = app.poll_automatic_runtime_guard(
            app.new_automatic_runtime_guard(),
            False,
            "Extracting native PDF text",
            "http://127.0.0.1:3001",
            "key",
            now=100,
            probe=lambda *_args, **_kwargs: probe_calls.append(True),
        )

        self.assertFalse(state["desktop_required"])
        self.assertEqual(result["status"], "not_required")
        self.assertEqual(probe_calls, [])

    def test_runtime_guard_requires_two_spaced_api_failures_and_resets_after_health(self):
        import rag_pdf_gradio_app as app

        observations = iter((
            {"status": "connection_refused"},
            {"status": "reachable"},
            {"status": "connection_refused"},
            {"status": "connection_refused"},
        ))
        state = app.new_automatic_runtime_guard()
        outcomes = []
        for moment in (100, 112, 124, 136):
            state, result = app.poll_automatic_runtime_guard(
                state,
                True,
                "Vector verification",
                "http://127.0.0.1:3001",
                "key",
                now=moment,
                probe=lambda *_args, **_kwargs: next(observations),
            )
            outcomes.append(result["status"])

        self.assertEqual(outcomes, ["transient_failure", "healthy", "transient_failure", "unavailable"])
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertEqual(len(state["checks"]), 4)

    def test_runtime_guard_confirms_a_failed_probe_promptly(self):
        import rag_pdf_gradio_app as app

        state = app.new_automatic_runtime_guard()
        state, first = app.poll_automatic_runtime_guard(
            state,
            True,
            "Extracting native PDF text",
            "http://127.0.0.1:3001",
            "key",
            now=100,
            probe=lambda *_args, **_kwargs: {"status": "connection_refused"},
        )
        self.assertEqual(first["status"], "transient_failure")
        self.assertEqual(
            state["next_check_epoch"],
            100 + app.AUTOMATIC_RUNTIME_GUARD_RECHECK_SECONDS,
        )

        state, second = app.poll_automatic_runtime_guard(
            state,
            True,
            "Extracting native PDF text",
            "http://127.0.0.1:3001",
            "key",
            now=102,
            probe=lambda *_args, **_kwargs: {"status": "connection_refused"},
        )
        self.assertEqual(second["status"], "unavailable")

    def test_runtime_guard_turns_probe_error_into_bounded_failure_evidence(self):
        import rag_pdf_gradio_app as app

        state, result = app.poll_automatic_runtime_guard(
            app.new_automatic_runtime_guard(),
            True,
            "Submitting AnythingLLM queue",
            "http://127.0.0.1:3001",
            "key",
            now=100,
            probe=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("socket closed")),
        )

        self.assertEqual(result["status"], "transient_failure")
        self.assertEqual(state["checks"][-1]["health_status"], "probe_error")

    def test_submission_auth_failure_recovers_only_when_local_desktop_is_down(self):
        import rag_pdf_gradio_app as app

        summary = {"api_upload_status": "error_authentication_required"}
        unavailable = app.submission_runtime_recovery_needed(
            summary,
            "http://127.0.0.1:3001",
            "",
            probe=lambda *_args, **_kwargs: {"status": "unreachable", "error": "connection refused"},
        )
        still_reachable = app.submission_runtime_recovery_needed(
            summary,
            "http://127.0.0.1:3001",
            "",
            probe=lambda *_args, **_kwargs: {"status": "reachable"},
        )
        remote = app.submission_runtime_recovery_needed(
            summary,
            "https://anythingllm.example.test",
            "",
            probe=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not probe remote")),
        )

        self.assertTrue(unavailable["needed"])
        self.assertEqual(
            unavailable["reason"],
            "local_runtime_unavailable_after_submission_auth_failure",
        )
        self.assertFalse(still_reachable["needed"])
        self.assertEqual(still_reachable["reason"], "runtime_still_reachable")
        self.assertFalse(remote["needed"])
        self.assertEqual(remote["reason"], "not_submission_runtime_loss")

    def test_embedder_network_failure_uses_the_same_local_runtime_recovery_gate(self):
        import rag_pdf_gradio_app as app

        recovery = app.submission_runtime_recovery_needed(
            {"status": "network_error"},
            "http://127.0.0.1:3001",
            "",
            probe=lambda *_args, **_kwargs: {"status": "unreachable", "error": "connection reset"},
        )

        self.assertTrue(recovery["needed"])
        self.assertEqual(recovery["health"]["status"], "unreachable")

    def test_runtime_recovery_starts_a_missing_desktop_but_never_force_restarts_a_live_process(self):
        import rag_pdf_gradio_app as app

        original_process = app.anythingllm_desktop_process_running
        original_ensure = app.ensure_anythingllm_runtime
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            try:
                app.anythingllm_desktop_process_running = lambda: False
                callback_events = []

                def fake_ensure(**kwargs):
                    self.assertEqual(
                        kwargs["startup_timeout"],
                        app.AUTOMATIC_RUNTIME_RECOVERY_STARTUP_TIMEOUT_SECONDS,
                    )
                    kwargs["status_callback"]("waiting_for_runtime", {"status": "unreachable"})
                    kwargs["status_callback"]("ready_after_start", {"status": "reachable"})
                    return {"status": "reachable"}

                app.ensure_anythingllm_runtime = fake_ensure
                started = app.attempt_automatic_runtime_start(
                    root,
                    "http://127.0.0.1:3001",
                    "key",
                    status_callback=lambda phase, _snapshot: callback_events.append(phase),
                )

                app.anythingllm_desktop_process_running = lambda: True
                app.ensure_anythingllm_runtime = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run"))
                withheld = app.attempt_automatic_runtime_start(root, "http://127.0.0.1:3001", "key")
            finally:
                app.anythingllm_desktop_process_running = original_process
                app.ensure_anythingllm_runtime = original_ensure

        self.assertEqual(started["status"], "ready")
        self.assertEqual(callback_events, ["waiting_for_runtime", "ready_after_start"])
        self.assertEqual(withheld["status"], "restart_withheld_manual_activity_uncertain")
        self.assertEqual(withheld["action"], "restart_withheld_process_alive")

    def test_runtime_recovery_resumes_only_before_anythingllm_submission(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            self.assertTrue(
                app.can_resume_local_preparation_after_runtime_start(
                    output, {"status": "ready"}
                )
            )
            ledger = output / "inspection" / "embedding-batch-ledger.json"
            ledger.parent.mkdir()
            ledger.write_text("{}", encoding="utf-8")
            self.assertFalse(
                app.can_resume_local_preparation_after_runtime_start(
                    output, {"status": "ready"}
                )
            )
            self.assertFalse(
                app.can_resume_local_preparation_after_runtime_start(
                    output, {"status": "startup_timeout"}
                )
            )

    def test_automatic_recovery_is_durably_limited_to_one_attempt_per_run(self):
        import rag_pdf_gradio_app as app

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def is_alive(self):
                return True

            def start(self):
                return None

        original_thread = app.threading.Thread
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "app-run-interrupted"
            root.mkdir()
            key = str(root)
            try:
                app.threading.Thread = DeferredThread
                self.assertTrue(app.schedule_automatic_recovery(root, reason="operator_cancellation"))
                self.assertTrue((root / app.AUTOMATIC_RUN_RECOVERY_ATTEMPT).is_file())
                self.assertFalse(app.schedule_automatic_recovery(root, reason="duplicate"))
            finally:
                app.threading.Thread = original_thread
                app.ACTIVE_AUTOMATIC_RECOVERY_THREADS.pop(key, None)

    def test_scheduled_recovery_uses_the_guarded_automatic_policy(self):
        import rag_pdf_gradio_app as app

        class InlineThread:
            def __init__(self, *, target, name, daemon):
                self.target = target

            def is_alive(self):
                return False

            def start(self):
                self.target()

        original_thread = app.threading.Thread
        original_recover = app.recover_automatic_run
        captured = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "app-run-interrupted"
            root.mkdir()
            try:
                app.threading.Thread = InlineThread
                app.recover_automatic_run = lambda run_root, **kwargs: captured.update(
                    run_root=str(run_root), **kwargs
                )
                self.assertTrue(app.schedule_automatic_recovery(root, reason="runtime_interrupted"))
            finally:
                app.threading.Thread = original_thread
                app.recover_automatic_run = original_recover
                app.ACTIVE_AUTOMATIC_RECOVERY_THREADS.pop(str(root), None)

        self.assertEqual(captured["run_root"], str(root))
        self.assertEqual(captured["policy"], "automatic_recover")
        self.assertTrue(captured["automatic"])

    def test_successful_fast_completion_skips_broad_batch_diagnostics(self):
        import rag_pdf_gradio_app as app

        successful_summary = {
            "api_upload_status": "complete",
            "post_upload_verification_status": "pass",
            "anythingllm_runtime_validation_status": "pass",
        }
        self.assertFalse(
            app.automatic_batch_diagnostics_required([successful_summary], prepare_and_upload=True)
        )
        self.assertTrue(
            app.automatic_batch_diagnostics_required(
                [successful_summary], prepare_and_upload=True, retain_detailed_evidence=True
            )
        )
        self.assertTrue(
            app.automatic_batch_diagnostics_required(
                [{**successful_summary, "post_upload_verification_status": "review"}],
                prepare_and_upload=True,
            )
        )
        self.assertFalse(
            app.automatic_batch_diagnostics_required(
                [successful_summary], prepare_and_upload=True, cancellation_requested=True
            )
        )

    def test_queue_progress_never_displays_a_false_zero_denominator(self):
        message = pipeline.anythingllm_embed_progress_message(
            {"type": "chunk_progress", "docIndex": 0, "totalDocs": 0, "chunksProcessed": 1, "totalChunks": 0}
        )
        observation = pipeline.format_vector_observation(1, 0, "observing")

        self.assertIn("total not yet confirmed", message)
        self.assertIn("expected count not yet confirmed", observation)
        self.assertNotIn("1/0", message)
        self.assertNotIn("1/0", observation)

    def test_large_queue_progress_keeps_the_confirmed_denominator(self):
        message = pipeline.anythingllm_embed_progress_message(
            {"type": "doc_complete", "docIndex": 326, "totalDocs": 663}
        )
        observation = pipeline.format_vector_observation(327, 663, "queue active")

        self.assertIn("327/663", message)
        self.assertIn("327/663", observation)
        self.assertIn("queue active", observation)

    def test_automatic_timing_html_exposes_range_and_evidence_without_overstating_it(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_run_timing_html(
            expected_seconds=83,
            state="ready",
            estimate_range="01m05s - 01m55s",
            confidence_label="medium confidence",
            comparable_runs=4,
        )
        self.assertIn("Est: 01m23s", rendered)
        self.assertIn("Range 01m05s - 01m55s", rendered)
        self.assertNotIn("medium confidence", rendered)
        self.assertNotIn("comparable run", rendered)

    def test_automatic_timing_html_hides_low_confidence_range_and_formats_hours(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_run_timing_html(
            expected_seconds=4 * 3600,
            state="ready",
            estimate_range="03h00m00s - 05h00m00s",
            confidence_label="low confidence",
            comparable_runs=0,
        )
        self.assertIn("Est: 04h00m00s", rendered)
        self.assertNotIn("Range", rendered)
        self.assertNotIn("confidence", rendered)
        self.assertNotIn("comparable", rendered)

    def test_fresh_automatic_defaults_only_use_unstructured_after_native_warning_signals(self):
        import rag_pdf_gradio_app as app

        self.assertFalse(app.fresh_automatic_run_setting_values()["deep_extraction"])
        self.assertFalse(app.fresh_automatic_run_setting_values()["download_full_folder"])

    def test_diagnostics_export_selects_only_forensic_run_artifacts(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "run-summary.json").write_text("{}", encoding="utf-8")
            (root / "diagnostics.json").write_text("[]", encoding="utf-8")
            (root / "inspection").mkdir()
            (root / "inspection" / "post-upload-verification.csv").write_text("status\npass\n", encoding="utf-8")
            (root / "selected").mkdir()
            (root / "selected" / "anythingllm-upload.txt").write_text("prepared text", encoding="utf-8")

            paths, error = app.diagnostic_evidence_paths(root)

        self.assertEqual(error, "")
        self.assertIn("run-summary.json", {path.name for path in paths})
        self.assertIn("inspection", {path.name for path in paths})
        self.assertNotIn("selected", {path.name for path in paths})

    def test_lean_ready_retention_keeps_root_text_and_page_bounded_segment_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "Example-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            layout_review = selected / "layout-region-review.json"
            layout_review.write_text('{"status": "applied"}', encoding="utf-8")
            lane_review = selected / "retrieval-lane-review.json"
            lane_review.write_text('{"status": "review_only"}', encoding="utf-8")
            supplementary_candidates = selected / "supplementary-content-candidates.txt"
            supplementary_candidates.write_text("[SUPPLEMENTARY CANDIDATE]", encoding="utf-8")
            (selected / "anythingllm-upload.txt").write_text("internal copy", encoding="utf-8")
            (root / "metadata-api").mkdir()
            (root / "metadata-api" / "payload.jsonl").write_text("{}\n", encoding="utf-8")
            (root / "candidates").mkdir()
            (root / "candidates" / "candidate.txt").write_text("candidate", encoding="utf-8")
            (root / "source-profile.json").write_text("{}", encoding="utf-8")
            (root / "pdf-input-preflight.json").write_text("{}", encoding="utf-8")
            (root / "output-capacity-preflight.json").write_text("{}", encoding="utf-8")
            summary = {
                "readiness_status": "ready",
                "selected_backend": "pymupdf",
                "api_upload_status": "skipped_prepare_only",
                "post_upload_verification_status": "not_checked_no_upload",
                "anythingllm_runtime_validation_status": "not_checked_no_upload",
                "upload_file": str(parsed),
                "manifest": str(selected / "segment-manifest.jsonl"),
                "variant_outputs": {"full-document": {"upload_file": "stale.txt"}},
            }
            result = pipeline.retain_successful_run_leanly(
                root,
                summary,
                {"source_file": "C:/Example.pdf", "filename": "Example.pdf", "pdf_page_count": 2},
                parsed,
                segments=(
                    {"pdf_page": 1, "text": "First page, first chunk."},
                    {"pdf_page": 1, "text": "First page, second chunk."},
                    {"pdf_page": 2, "text": "Second page."},
                ),
            )
            compact = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))

            self.assertTrue(result["applied"])
            self.assertFalse(selected.exists())
            self.assertFalse(layout_review.exists())
            self.assertFalse(lane_review.exists())
            self.assertFalse(supplementary_candidates.exists())
            self.assertTrue((root / "Example-pdf-parsed.txt").exists())
            self.assertEqual(
                (root / "segments" / "Example-p001-s01.txt").read_text(encoding="utf-8"),
                "First page, first chunk.",
            )
            self.assertEqual(
                (root / "segments" / "Example-p001-s02.txt").read_text(encoding="utf-8"),
                "First page, second chunk.",
            )
            self.assertEqual(
                (root / "segments" / "Example-p002-s01.txt").read_text(encoding="utf-8"),
                "Second page.",
            )
            self.assertFalse((root / "metadata-api").exists())
            self.assertFalse((root / "candidates").exists())
            self.assertFalse((root / "pdf-input-preflight.json").exists())
            self.assertFalse((root / "output-capacity-preflight.json").exists())
            self.assertEqual(compact["artifacts"]["parsed_text"], "Example-pdf-parsed.txt")
            self.assertEqual(compact["artifacts"]["segments_directory"], "segments")
            self.assertEqual(compact["artifacts"]["retained_segment_files"], 3)
            self.assertEqual(summary["upload_file"], str(root / "Example-pdf-parsed.txt"))
            self.assertEqual(summary["manifest"], "")
            self.assertEqual(summary["variant_outputs"], {})
            self.assertEqual(
                compact["verification_receipt"]["storage"]["drawer_layout"],
                "not_checked",
            )
            self.assertEqual(
                compact["verification_receipt"]["runtime"]["chat_status"],
                "not_checked",
            )

    def test_lean_retention_preserves_evidence_when_upload_or_verification_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "Example-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            inspection = root / "inspection"
            inspection.mkdir()
            report = inspection / "post-upload-verification.json"
            report.write_text('{"status": "partial_vector_coverage"}', encoding="utf-8")

            result = pipeline.retain_successful_run_leanly(
                root,
                {
                    "readiness_status": "ready",
                    "api_upload_status": "error",
                    "post_upload_verification_status": "partial_vector_coverage",
                    "anythingllm_runtime_validation_status": "vector_runtime_timeout",
                },
                {},
                parsed,
            )
            self.assertTrue(report.exists())

        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "upload_or_verification_needs_review")

    def test_no_logs_retention_creates_only_flat_text_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "Example-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            (root / "source-profile.json").write_text("{}", encoding="utf-8")
            for name in ("run-checkpoint.json", "run-checkpoints.jsonl", "run-result.json"):
                (root / name).write_text("worker receipt", encoding="utf-8")
            summary = {
                "readiness_status": "ready",
                "api_upload_status": "skipped_prepare_only",
                "post_upload_verification_status": "not_checked_no_upload",
                "anythingllm_runtime_validation_status": "not_checked_no_upload",
                "upload_file": str(parsed),
                "manifest": str(selected / "segment-manifest.jsonl"),
            }
            result = pipeline.retain_successful_run_without_logs(
                root,
                summary,
                {
                    "source_file": "C:/Example.pdf",
                    "filename": "Example.pdf",
                    "source_sha256": "a" * 64,
                },
                parsed,
                segments=(
                    {"pdf_page": 1, "text": "First segment."},
                    {"pdf_page": 1, "text": "Second segment."},
                    {"pdf_page": 2, "text": "Third segment."},
                ),
            )

            expected_names = {
                "Example-aaaaaaaaaaaa-complete-pdf-parsed.txt",
                "Example-aaaaaaaaaaaa-p001-s01.txt",
                "Example-aaaaaaaaaaaa-p001-s02.txt",
                "Example-aaaaaaaaaaaa-p002-s01.txt",
            }
            self.assertTrue(result["applied"])
            self.assertEqual(result["policy"], "flat_local_no_logs_v1")
            self.assertEqual({path.name for path in root.iterdir()}, expected_names)
            self.assertFalse((root / "segments").exists())
            self.assertFalse((root / "run-summary.json").exists())
            self.assertFalse((root / "run-checkpoint.json").exists())
            self.assertFalse((root / "run-checkpoints.jsonl").exists())
            self.assertFalse((root / "run-result.json").exists())
            self.assertEqual(summary["upload_file"], str(root / "Example-aaaaaaaaaaaa-complete-pdf-parsed.txt"))

    def test_no_logs_retention_keeps_evidence_when_preparation_needs_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "Example-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            inspection = root / "inspection"
            inspection.mkdir()
            report = inspection / "diagnostics.csv"
            report.write_text("needs review", encoding="utf-8")

            result = pipeline.retain_successful_run_without_logs(
                root,
                {
                    "readiness_status": "needs_review",
                    "api_upload_status": "skipped_prepare_only",
                    "post_upload_verification_status": "not_checked_no_upload",
                    "anythingllm_runtime_validation_status": "not_checked_no_upload",
                },
                {"filename": "Example.pdf"},
                parsed,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "run_needs_review")
            self.assertTrue(parsed.exists())
            self.assertTrue(report.exists())

    def test_no_logs_retention_checks_every_flat_destination_before_moving_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "Example-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            collision = root / "Example-aaaaaaaaaaaa-p001-s02.txt"
            collision.write_text("existing export", encoding="utf-8")
            summary = {
                "readiness_status": "ready",
                "api_upload_status": "skipped_prepare_only",
                "post_upload_verification_status": "not_checked_no_upload",
                "anythingllm_runtime_validation_status": "not_checked_no_upload",
                "upload_file": str(parsed),
            }

            with self.assertRaises(FileExistsError):
                pipeline.retain_successful_run_without_logs(
                    root,
                    summary,
                    {
                        "source_file": "C:/Example.pdf",
                        "filename": "Example.pdf",
                        "source_sha256": "a" * 64,
                    },
                    parsed,
                    segments=(
                        {"pdf_page": 1, "text": "First segment."},
                        {"pdf_page": 1, "text": "Second segment."},
                    ),
                )

            self.assertEqual(collision.read_text(encoding="utf-8"), "existing export")
            self.assertFalse((root / "Example-aaaaaaaaaaaa-complete-pdf-parsed.txt").exists())
            self.assertFalse((root / "Example-aaaaaaaaaaaa-p001-s01.txt").exists())
            self.assertTrue((root / "segments").is_dir())

    def test_no_logs_export_promotion_uses_a_stable_title_derived_folder(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "A source title.pdf"
            pdf.write_bytes(b"source fixture")
            temporary = root / "app-run-20990101-010101" / "A-source-title"
            temporary.mkdir(parents=True)
            parsed = temporary / "A-source-title-aaaaaaaaaaaa-complete-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            summary = {"source_sha256": "a" * 64, "upload_file": str(parsed)}

            moved = app.promote_flat_no_logs_output(root, temporary, pdf, summary)

            self.assertEqual(moved.name, "parsed-pdf-A-source-title-aaaaaaaaaaaa")
            self.assertEqual(summary["upload_file"], str(moved / parsed.name))
            self.assertTrue((moved / parsed.name).is_file())
            self.assertNotIn("20990101", moved.name)

    def test_generated_output_directory_prefers_the_prepared_text_parent(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            flat_export = root / "parsed-pdf-Example-aaaaaaaaaaaa"
            flat_export.mkdir()
            parsed = flat_export / "Example-aaaaaaaaaaaa-complete-pdf-parsed.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            unrelated = root / "app-run-20990101-010101" / "inspection" / "audit.csv"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("audit", encoding="utf-8")

            target = app.generated_output_directory([str(parsed), str(unrelated)], root)
            state = app.output_folder_button_state([str(parsed), str(unrelated)], root)

            self.assertEqual(target, flat_export)
            self.assertTrue(state["visible"])
            self.assertTrue(state["interactive"])
            self.assertEqual(state["value"], "Open Generated Output Folder")

    def test_generated_output_directory_opens_the_shared_root_for_a_batch(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app-run-20990101-010101"
            first = root / "first" / "First-complete-pdf-parsed.txt"
            second = root / "second" / "Second-complete-pdf-parsed.txt"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")

            target = app.generated_output_directory([str(first), str(second)], root.parent)

        self.assertEqual(target, root)

    def test_open_generated_output_directory_prefers_terminal_paths_to_stale_browser_state(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stale_directory = root / "stale-run"
            completed_directory = root / "completed-run"
            stale_directory.mkdir()
            completed_directory.mkdir()
            stale_file = stale_directory / "stale-complete-pdf-parsed.txt"
            completed_file = completed_directory / "completed-complete-pdf-parsed.txt"
            stale_file.write_text("stale", encoding="utf-8")
            completed_file.write_text("completed", encoding="utf-8")
            original_status = app.LIVE_AUTOMATIC_RUN_STATUS
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "successful",
                "output_paths": [str(completed_file)],
            }
            try:
                with mock.patch.object(app, "launch_windows_explorer") as open_explorer:
                    app.open_generated_output_directory([str(stale_file)], str(root))
            finally:
                app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        open_explorer.assert_called_once_with(str(completed_directory))

    def test_metadata_layout_reserves_the_identity_area_as_soon_as_a_file_is_selected(self):
        import rag_pdf_gradio_app as app

        selected = app.metadata_selection_layout_state(["example.pdf"], [])
        cleared = app.metadata_selection_layout_state([], [])

        self.assertTrue(selected["open"])
        self.assertFalse(cleared["open"])

    def test_retained_run_diagnostics_renders_compact_summary(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "selected").mkdir()
            (root / "selected" / "Example-pdf-parsed.txt").write_text("prepared text", encoding="utf-8")
            (root / "run-summary.json").write_text(
                json.dumps(
                    {
                        "outcome": {"readiness_status": "ready", "selected_backend": "pymupdf"},
                        "source": {"filename": "Example.pdf", "pdf_page_count": 2},
                        "preparation": {"segments": 3, "chunk_size": 512, "chunk_overlap": 0},
                        "artifacts": {"parsed_text": "selected/Example-pdf-parsed.txt"},
                    }
                ),
                encoding="utf-8",
            )
            rendered = app.retained_run_diagnostics_html(root)

        self.assertIn("Completed-run diagnostics", rendered)
        self.assertIn("Example-pdf-parsed.txt (available)", rendered)

    def test_retained_run_diagnostics_stays_hidden_until_a_run_is_selected(self):
        import rag_pdf_gradio_app as app

        empty = app.retained_run_diagnostics_update("")
        missing = app.retained_run_diagnostics_update("C:/does-not-exist")

        self.assertFalse(empty["visible"])
        self.assertEqual(empty["value"], "")
        self.assertTrue(missing["visible"])
        self.assertIn("Choose a completed PDF output folder", missing["value"])

    def test_terminal_output_folder_button_uses_existing_default_root_without_downloads(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            available = app.output_folder_button_state([], temp_dir)
            missing = app.output_folder_button_state([], str(Path(temp_dir) / "missing"))
            original_default = app.AUTO_OUTPUT_DIR
            app.AUTO_OUTPUT_DIR = Path(temp_dir)
            try:
                automatic_default = app.output_folder_button_state([], "")
            finally:
                app.AUTO_OUTPUT_DIR = original_default

        self.assertTrue(available["visible"])
        self.assertTrue(available["interactive"])
        self.assertTrue(automatic_default["visible"])
        self.assertTrue(automatic_default["interactive"])
        self.assertFalse(missing["visible"])
        self.assertFalse(missing["interactive"])

    def test_lean_worker_cleanup_removes_transport_receipts_only_after_ready_retention(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in app.AUTOMATIC_SUCCESS_WORKER_ARTIFACTS:
                (root / name).write_text("transport receipt", encoding="utf-8")
            retained = root / "run-summary.json"
            retained.write_text("{}", encoding="utf-8")

            removed = app.cleanup_automatic_success_worker_artifacts(
                root,
                {"lean_retention": {"applied": True}},
            )

            self.assertEqual(removed, list(app.AUTOMATIC_SUCCESS_WORKER_ARTIFACTS))
            self.assertTrue(retained.exists())
            self.assertTrue(all(not (root / name).exists() for name in removed))

    def test_primary_prepared_downloads_exclude_manifests_and_diagnostics(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prepared = root / "anythingllm-upload.txt"
            manifest = root / "segment-manifest.jsonl"
            prepared.write_text("prepared text", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")

            paths = app.primary_prepared_download_paths([
                {"upload_file": str(prepared), "manifest": str(manifest)},
                {"upload_file": str(root / "missing.txt")},
            ])

        self.assertEqual(paths, [str(prepared)])

    def test_progress_duration_is_always_its_own_compact_line(self):
        import rag_pdf_gradio_app as app

        self.assertIn(".automatic-run-progress-timing", app.APP_CSS)
        self.assertIn("flex-basis: 100%", app.APP_CSS)
        self.assertIn("margin-top: -1px", app.APP_CSS)

    def test_running_estimate_never_displays_a_negative_duration(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_run_timing_html(
            expected_seconds=9,
            state="running",
            started_epoch=100,
            now=111,
        )

        self.assertIn("Est: 00m00s", rendered)
        self.assertNotIn("Est: -", rendered)

    def test_workspace_name_is_sanitized_before_anythingllm_can_create_an_invalid_namespace(self):
        unsafe_name = "PDF — Example Book: Example Author’s Views / 2026"
        safe_name = pipeline.lancedb_safe_workspace_name(unsafe_name)

        self.assertEqual(safe_name, "PDF Example Book Example Authors Views 2026")
        self.assertNotIn("'", safe_name)
        self.assertTrue(pipeline.is_lancedb_safe_namespace("pdf-example-book-example-authors-views-2026"))
        self.assertFalse(pipeline.is_lancedb_safe_namespace("pdf-example-author's-views"))

    def test_new_workspace_name_collision_uses_a_visible_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "anythingllm.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("create table workspaces (name text, slug text)")
                con.execute(
                    "insert into workspaces (name, slug) values (?, ?)",
                    ("PDF Sample Author 2026-07-13", "pdf-sample-author-2026-07-13"),
                )
                con.commit()
            finally:
                con.close()

            name, suffix = pipeline.unique_lancedb_workspace_name(
                "PDF Sample Author 2026-07-13",
                temp_dir,
            )
        self.assertEqual(name, "PDF Sample Author 2026-07-13 2")
        self.assertEqual(suffix, 2)

    def test_native_boundary_policy_uses_zero_overlap_without_saving_global_settings(self):
        import rag_pdf_gradio_app as app

        segment_update, target_update, inherit_update, overlap_update, note = app.native_upload_boundary_policy_update(
            app.NATIVE_BOUNDARY_PAGE_LIMIT_LABEL,
            750,
        )
        self.assertEqual(segment_update["value"], app.SEGMENT_PAGE_LIMIT_LABEL)
        self.assertEqual(inherit_update["value"], False)
        self.assertEqual(overlap_update["value"], "0")
        self.assertIn("does not write the global", note)
        self.assertEqual(target_update["value"], "750")

    def test_native_boundary_policy_refreshes_eta_with_derived_controls(self):
        import rag_pdf_gradio_app as app

        original_refresh = app.refresh_automatic_run_estimate
        observed = []
        try:
            app.refresh_automatic_run_estimate = lambda *args: observed.append(args) or "refreshed ETA"
            updates = app.native_upload_boundary_policy_and_timer_update(
                app.NATIVE_BOUNDARY_PAGE_LIMIT_LABEL,
                "750",
                ["input.pdf"],
                [],
                app.MODE_NATIVE_UPLOAD_LABEL,
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "workspace",
                app.SEGMENT_PASSAGES_LABEL,
                "512",
                "20",
                "Automatic",
                "auto",
            )
        finally:
            app.refresh_automatic_run_estimate = original_refresh

        self.assertEqual(updates[-1], "refreshed ETA")
        self.assertEqual(observed[0][5], app.SEGMENT_PAGE_LIMIT_LABEL)
        self.assertEqual(observed[0][6], "750")
        self.assertEqual(observed[0][8], "0")

    def test_timing_features_distinguish_none_mode_and_local_target_length(self):
        import rag_pdf_gradio_app as app

        profile = {"page_count": 10, "mean_chars_per_page": 4_000}
        none_mode = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_NONE_LABEL, chunk_size=800, target_passage_length=300,
        )
        smaller_target = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, chunk_size=800, target_passage_length=300,
        )
        larger_target = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, chunk_size=800, target_passage_length=600,
        )

        self.assertEqual(none_mode["estimated_records"], 1)
        self.assertEqual(smaller_target["target_passage_length"], 300)
        self.assertGreater(smaller_target["estimated_records"], larger_target["estimated_records"])

    def test_create_document_workspace_selects_the_new_workspace(self):
        import rag_pdf_gradio_app as app

        original_create = app.create_validation_workspace
        original_choices = app.local_workspace_choices
        original_readiness = app.native_upload_readiness_report
        original_runtime = app.ensure_anythingllm_runtime
        original_auth = app.verify_anythingllm_upload_auth
        try:
            app.ensure_anythingllm_runtime = lambda *args, **kwargs: {
                "status": "reachable", "api_url": "http://127.0.0.1:3001",
            }
            app.verify_anythingllm_upload_auth = lambda *args, **kwargs: {
                "authenticated": True, "status": "authenticated", "message": "ok",
            }
            app.create_validation_workspace = lambda *args, **kwargs: {
                "status": "created",
                "workspace_slug": "pdf-example",
                "workspace_name": kwargs["workspace_name"],
                "error": "",
            }
            app.local_workspace_choices = lambda: ([], "local workspace list refreshed")
            app.native_upload_readiness_report = lambda *args, **kwargs: {
                "workspace_slug": "pdf-example",
                "workspace_slug_found": True,
                "local_db_found": True,
                "runtime_api_reachable": True,
                "authenticated": True,
                "upload_succeeded": None,
                "workspace_slug_message": "found",
                "local_db_message": "found",
                "runtime_api_message": "ready",
                "authentication_message": "ready",
                "upload_message": "not run",
            }
            update, status, _readiness = app.create_document_workspace_for_upload(
                "http://127.0.0.1:3001", "", "Example PDF", []
            )
        finally:
            app.create_validation_workspace = original_create
            app.local_workspace_choices = original_choices
            app.native_upload_readiness_report = original_readiness
            app.ensure_anythingllm_runtime = original_runtime
            app.verify_anythingllm_upload_auth = original_auth

        self.assertEqual(update["value"], "pdf-example")
        self.assertIn("Created and selected workspace", status)
        self.assertIn("PDF — Example PDF", update["choices"][0][0])

    def test_workspace_creation_reports_runtime_unavailable_before_mutation(self):
        import rag_pdf_gradio_app as app

        original_runtime = app.ensure_anythingllm_runtime
        original_create = app.create_validation_workspace
        called = []
        try:
            app.ensure_anythingllm_runtime = lambda *args, **kwargs: {
                "status": "unreachable",
                "api_url": "http://127.0.0.1:3001",
                "message": "AnythingLLM local API did not start.",
                "start": {"status": "start_failed", "error": "Desktop executable failed."},
            }
            app.create_validation_workspace = lambda *args, **kwargs: called.append(True) or {}
            result = app.create_new_document_workspace(
                "http://127.0.0.1:3001", "", "Example PDF", []
            )
        finally:
            app.ensure_anythingllm_runtime = original_runtime
            app.create_validation_workspace = original_create

        self.assertEqual(result["status"], "runtime_unavailable")
        self.assertIn("Desktop executable failed", result["error"])
        self.assertEqual(called, [])

    def test_live_workspace_api_overrides_transient_local_database_lag(self):
        import rag_pdf_gradio_app as app

        original_local = app.local_workspace_slug_exists
        original_runtime = app.ensure_anythingllm_runtime
        original_auth = app.verify_anythingllm_upload_auth
        original_api_workspace = app.api_workspace_slug_exists
        try:
            app.local_workspace_slug_exists = lambda slug: (False, "SQLite row is not visible yet.")
            app.ensure_anythingllm_runtime = lambda *args, **kwargs: {
                "status": "reachable", "api_url": "http://127.0.0.1:3001", "message": "ready",
                "start": {"status": "already_running"},
            }
            app.verify_anythingllm_upload_auth = lambda *args, **kwargs: {
                "authenticated": True, "status": "authenticated", "message": "accepted",
            }
            app.api_workspace_slug_exists = lambda *args, **kwargs: (
                True, "Checked live AnythingLLM API at http://127.0.0.1:3001."
            )
            report = app.native_upload_readiness_report(
                "http://127.0.0.1:3001", "", "newly-created", autostart_runtime=True,
                verify_authentication=True,
            )
        finally:
            app.local_workspace_slug_exists = original_local
            app.ensure_anythingllm_runtime = original_runtime
            app.verify_anythingllm_upload_auth = original_auth
            app.api_workspace_slug_exists = original_api_workspace

        self.assertTrue(report["workspace_slug_found"])
        self.assertTrue(report["workspace_api_found"])
        self.assertIn("local storage is still catching up", report["workspace_slug_message"])

    def test_reconciled_failure_preserves_specific_workspace_code(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "failed",
                "details": "AUTO-WORKSPACE-004: Could not create the new document workspace",
                "started_epoch": 1.0,
                "updated_epoch": 2.0,
                "expected_seconds": 0,
            }
            outputs = app.refresh_live_automatic_run_ui()
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertIn("AUTO-WORKSPACE-004", outputs[7]["value"])

    def test_new_workspace_name_field_is_editable_and_preserves_user_edits(self):
        import rag_pdf_gradio_app as app

        generated, marker = app.update_new_workspace_name_control(
            app.NEW_DOCUMENT_WORKSPACE_VALUE,
            "Sample Author's Boundary Study",
            [],
            "",
            "",
        )
        self.assertTrue(generated["visible"])
        self.assertTrue(generated["value"].startswith("PDF Sample Authors Boundary Study "))
        preserved, _marker = app.update_new_workspace_name_control(
            app.NEW_DOCUMENT_WORKSPACE_VALUE,
            "Changed detected title",
            [],
            "My Sample Author comparison",
            marker,
        )
        self.assertEqual(preserved["value"], "My Sample Author comparison")

    def test_live_run_progress_is_whole_percent_rounded_and_capped_when_a_phase_is_slow(self):
        import rag_pdf_gradio_app as app

        record = {
            "state": "running",
            "phase": "Verifying AnythingLLM batch 1",
            "details": "PDF 1/1",
            "confirmed_fraction": 0.40,
            "phase_start_fraction": 0.40,
            "phase_started_epoch": 100.0,
            "phase_allowance": 0.08,
            "phase_budget_seconds": 10.0,
        }
        self.assertEqual(app.paced_progress_percent(record, now=105.0), 44.0)
        self.assertEqual(app.paced_progress_percent(record, now=160.0), 48.0)
        rendered = app.automatic_live_status_html(record)
        self.assertIn('role="progressbar"', rendered)
        self.assertIn("48%", rendered)

    def test_failed_progress_freezes_at_its_terminal_checkpoint(self):
        import rag_pdf_gradio_app as app

        record = {
            "state": "failed",
            "confirmed_fraction": 0.095,
            "phase_start_fraction": 0.095,
            "phase_started_epoch": 100.0,
            "phase_allowance": 0.08,
            "phase_budget_seconds": 20.0,
            "display_anchor_fraction": 0.123,
        }

        self.assertEqual(app.paced_progress_percent(record, now=101.0), 13)
        self.assertEqual(app.paced_progress_percent(record, now=1_000.0), 13)

    def test_live_run_status_has_one_evidence_bar_with_elapsed_time_but_no_duplicate_estimate(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_live_status_html({
            "state": "running",
            "phase": "Submitting AnythingLLM batch 2",
            "expected_seconds": 100,
            "started_epoch": time.time() - 25,
            "confirmed_fraction": .65,
        })
        self.assertIn("Total progress:", rendered)
        self.assertEqual(rendered.count('role="progressbar"'), 1)
        self.assertIn('aria-label="Overall run progress"', rendered)
        self.assertIn("Elapsed", rendered)
        self.assertNotIn("remaining", rendered)
        self.assertNotIn("Est:", rendered)
        self.assertNotIn("automatic-run-time-progress", rendered)

    def test_progress_label_never_uses_estimate_text_as_a_second_progress_bar(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_live_status_html({
            "state": "running",
            "phase": "Submitting AnythingLLM batch 2",
            "expected_seconds": 10,
            "started_epoch": time.time() - 25,
            "confirmed_fraction": .65,
        })
        self.assertEqual(rendered.count('role="progressbar"'), 1)
        self.assertIn("Elapsed", rendered)
        self.assertNotIn("Est:", rendered)
        self.assertNotIn("overrun", rendered)
        self.assertNotIn("250%", rendered)

    def test_terminal_live_status_preserves_completed_duration(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_live_status_html({
            "state": "successful",
            "phase": "Local preparation complete",
            "started_epoch": 100.0,
            "finished_epoch": 165.0,
            "last_activity_epoch": 165.0,
            "confirmed_fraction": 1.0,
        })
        self.assertIn("Completed 01m05s", rendered)
        self.assertNotIn("Elapsed", rendered)

    def test_terminal_duration_ends_at_last_pipeline_activity_not_late_finalization(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_live_status_html({
            "state": "warning",
            "phase": "Searchable vectors verified",
            "started_epoch": 100.0,
            "last_activity_epoch": 185.0,
            "finished_epoch": 9_000.0,
            "confirmed_fraction": 1.0,
        })
        self.assertIn("Completed 01m25s", rendered)
        self.assertNotIn("Completed 148m20s", rendered)

    def test_idle_run_status_keeps_one_visible_ready_progress_bar(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_live_status_html({"state": "ready"})
        self.assertEqual(rendered.count('role="progressbar"'), 1)
        self.assertIn('aria-valuenow="0"', rendered)
        self.assertIn("Ready — Confirm to begin processing.", rendered)
        update = app.clear_live_automatic_run_status()
        self.assertTrue(update["visible"])
        self.assertIn('aria-valuenow="0"', update["value"])

    def test_terminal_run_statuses_keep_the_single_overall_progress_bar(self):
        import rag_pdf_gradio_app as app

        for state in ("successful", "warning", "failed", "cancelled"):
            with self.subTest(state=state):
                rendered = app.automatic_live_status_html({
                    "state": state,
                    "phase": "Finalizing run",
                    "confirmed_fraction": 0.87,
                })
                self.assertEqual(rendered.count('role="progressbar"'), 1)
                self.assertIn('aria-label="Overall run progress"', rendered)
                self.assertNotIn("automatic-run-time-progress", rendered)
                if state in {"successful", "warning"}:
                    self.assertIn('aria-valuenow="100"', rendered)
                if state == "cancelled":
                    self.assertNotIn('aria-valuenow="100"', rendered)

    def test_elapsed_time_indicator_does_not_move_back_when_batch_eta_grows(self):
        import rag_pdf_gradio_app as app

        before_recalibration = {
            "expected_seconds": 100,
            "started_epoch": 1,
            "elapsed_percent_floor": 0,
        }
        self.assertEqual(app.displayed_elapsed_time_percent(before_recalibration, now=52), 51)
        after_recalibration = {
            "expected_seconds": 200,
            "started_epoch": 1,
            "elapsed_percent_floor": 51,
        }
        self.assertEqual(app.raw_elapsed_time_percent(after_recalibration, now=53), 26)
        self.assertEqual(app.displayed_elapsed_time_percent(after_recalibration, now=53), 51)

    def test_running_elapsed_diagnostic_never_claims_complete(self):
        import rag_pdf_gradio_app as app

        record = {"state": "running", "expected_seconds": 10, "started_epoch": 1, "elapsed_percent_floor": 99}
        self.assertEqual(app.displayed_elapsed_time_percent(record, now=120), 99)
        record["state"] = "successful"
        self.assertEqual(app.displayed_elapsed_time_percent(record, now=120), 100)

    def test_progress_trace_records_evidence_and_elapsed_time_separately(self):
        import rag_pdf_gradio_app as app

        original = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.LIVE_AUTOMATIC_RUN_STATUS = {}
                app.update_live_automatic_run_status(
                    temp_dir,
                    state="running",
                    phase="Submitting AnythingLLM batch 1 of 2",
                    expected_seconds=10,
                    confirmed_fraction=.5,
                )
                entries = [
                    json.loads(line)
                    for line in (Path(temp_dir) / "progress-trace.jsonl").read_text(encoding="utf-8").splitlines()
                ]
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["confirmed_percent"], 50.0)
        self.assertIn("visible_progress_percent", entries[0])
        self.assertIn("active_window_seconds", entries[0])
        self.assertTrue(entries[0]["activity_observed"])
        self.assertLessEqual(entries[0]["elapsed_percent_display"], 100)

    def test_total_progress_follows_confirmed_evidence_but_stays_short_of_terminal(self):
        import rag_pdf_gradio_app as app

        # The ETA is not proof of work. Confirmed stage evidence may advance to
        # 98%, but only terminal evidence is permitted to render 100%.
        record = {
            "state": "running",
            "confirmed_fraction": .98,
            "phase_start_fraction": .98,
            "phase_started_epoch": 100.0,
            "phase_allowance": 0.0,
            "display_anchor_fraction": .98,
            "display_anchor_epoch": 100.0,
            "display_target_fraction": .98,
            "started_epoch": 100.0,
            "expected_seconds": 100,
        }
        self.assertEqual(app.paced_progress_percent(record, now=182.0), 98)
        record["state"] = "successful"
        self.assertEqual(app.paced_progress_percent(record, now=182.0), 100)

    def test_success_style_targets_the_visible_confirmation_button(self):
        import rag_pdf_gradio_app as app

        self.assertIn('"confirm-automatic-run-button"', app.APP_JS)
        self.assertIn("#confirm-automatic-run-button button.rag-run-success", app.APP_CSS)
        self.assertIn("#confirm-automatic-run-button button.rag-run-warning", app.APP_CSS)
        self.assertIn("#confirm-automatic-run-button button.rag-run-failed", app.APP_CSS)
        self.assertIn("#confirm-automatic-run-button button.rag-run-processing", app.APP_CSS)
        self.assertIn('button.classList.toggle("rag-run-processing"', app.APP_JS)

    def test_native_pipeline_progress_ranges_are_monotonic_through_embedding_and_reports(self):
        import auto_anythingllm_pipeline as pipeline

        self.assertLess(pipeline.PIPELINE_PROGRESS_STORAGE_INSPECTION, pipeline.PIPELINE_PROGRESS_EMBEDDING_START)
        self.assertLess(pipeline.PIPELINE_PROGRESS_EMBEDDING_START, pipeline.PIPELINE_PROGRESS_EMBEDDING_END)
        self.assertLess(pipeline.PIPELINE_PROGRESS_EMBEDDING_END, pipeline.PIPELINE_PROGRESS_POST_UPLOAD_OBSERVATION)
        self.assertLess(pipeline.PIPELINE_PROGRESS_POST_UPLOAD_OBSERVATION, pipeline.PIPELINE_PROGRESS_REPORTING)

    def test_timing_formula_reserves_many_records_for_a_scanned_pdf(self):
        import rag_pdf_gradio_app as app

        profile = {
            "page_count": 8,
            "mean_chars_per_page": 0,
            "text_density_bucket": "low",
            "layout_bucket": "image_or_table_heavy",
            "ocr_risk_bucket": "high",
        }
        features = app.timing_model_features(
            profile,
            app.MODE_NATIVE_UPLOAD_LABEL,
            app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL,
            chunk_size=512,
            chunk_overlap=20,
            backend_mode="Unstructured",
            unstructured_strategy="ocr_only",
        )
        self.assertEqual(features["estimated_records"], 80)
        self.assertEqual(features["estimated_batches"], 1)
        self.assertEqual(features["embedding_submission_strategy"], "desktop_queue")
        self.assertEqual(features["embedding_verification_mode"], "checkpoint")

    def test_local_eta_uses_general_fast_text_and_bounded_scan_classes(self):
        import rag_pdf_gradio_app as app

        ordinary = {
            "mode": app.MODE_LOCAL_ONLY_LABEL,
            "page_count": 228,
            "estimated_records": 1_235,
            "layout_bucket": "text_first",
            "line_density_bucket": "medium",
            "page_variability_bucket": "mixed",
            "ocr_risk_bucket": "low",
            "ocr_planned": False,
            "ocr_escalation_possible": False,
        }
        scan = {
            "mode": app.MODE_LOCAL_ONLY_LABEL,
            "page_count": 18,
            "estimated_records": 18,
            "layout_bucket": "image_or_table_heavy",
            "line_density_bucket": "low",
            "page_variability_bucket": "consistent",
            "ocr_risk_bucket": "high",
            "ocr_planned": False,
            "ocr_escalation_possible": True,
        }
        self.assertGreaterEqual(app.timing_model_base_seconds(ordinary), 20)
        self.assertLess(app.timing_model_base_seconds(ordinary), 25)
        self.assertGreaterEqual(app.timing_model_base_seconds(scan), 39)

    def test_batch_formula_uses_shared_setup_and_per_document_boundaries(self):
        import rag_pdf_gradio_app as app

        single = {
            "mode": app.MODE_LOCAL_ONLY_LABEL,
            "document_count": 1,
            "page_count": 20,
            "estimated_records": 20,
            "layout_bucket": "text_first",
            "line_density_bucket": "medium",
            "page_variability_bucket": "consistent",
            "ocr_planned": False,
            "ocr_escalation_possible": False,
        }
        batch = {**single, "document_count": 10, "page_count": 200, "estimated_records": 200}
        single_seconds = app.timing_model_base_seconds(single)
        batch_seconds = app.timing_model_base_seconds(batch)

        self.assertGreater(batch_seconds, single_seconds)
        self.assertLess(batch_seconds, single_seconds * 10)

    def test_local_estimate_can_be_shorter_than_the_native_upload_floor(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        original_history = app.hydrated_timing_model_history
        try:
            app.automatic_timing_document_profile = lambda _files: {
                "page_count": 5, "mean_chars_per_page": 3_700,
                "text_density_bucket": "high", "layout_bucket": "text_first",
                "ocr_risk_bucket": "low", "line_density_bucket": "medium",
                "page_variability_bucket": "consistent", "file_size_bucket": "light",
            }
            app.hydrated_timing_model_history = lambda: []
            estimate = app.estimate_automatic_run(
                ["ordinary.pdf"], app.MODE_LOCAL_ONLY_LABEL, "local only",
                segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, backend_mode="Automatic",
            )
        finally:
            app.automatic_timing_document_profile = original_profile
            app.hydrated_timing_model_history = original_history
        self.assertEqual(estimate["expected_seconds"], 8)

    def test_in_run_eta_recalibration_uses_completed_batches_not_document_identity(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(
            app.recalibrated_run_eta_seconds(120, 20, 10, 2, [4, 5]),
            120,
        )
        self.assertLess(
            app.recalibrated_run_eta_seconds(240, 45, 10, 3, [4, 5, 6]),
            240,
        )
        self.assertGreater(
            app.recalibrated_run_eta_seconds(120, 90, 10, 3, [24, 28, 31]),
            120,
        )

    def test_in_run_eta_recalibration_does_not_treat_one_cold_batch_as_steady_cadence(self):
        import rag_pdf_gradio_app as app

        revised = app.recalibrated_run_eta_seconds(
            7_005, 54, 952, 3, [12.45, 4.75, 3.30],
            remaining_batch_count=949,
            remaining_non_batch_seconds=4_100,
        )
        # A single initial connection/setup request must not turn a 116-minute
        # opening estimate into the former ~157-minute spike.
        self.assertLess(revised, 9_000)

    def test_batch_recalibration_keeps_later_documents_in_the_remaining_work(self):
        import rag_pdf_gradio_app as app

        revised = app.recalibrated_run_eta_seconds(
            1_215, 69, 173, 7, [2.4, 2.7, 2.5, 2.6],
            remaining_batch_count=166,
            remaining_non_batch_seconds=175,
        )
        self.assertGreater(revised, 600)
        self.assertLess(revised, 1_215)

    def test_embedding_timing_lanes_keep_local_models_separate_and_pool_cloud_provider(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(app.embedding_timing_lane("ollama", "qwen3-embedding:0.6b"), "local:ollama:qwen3-embedding:0.6b")
        self.assertNotEqual(
            app.embedding_timing_lane("ollama", "qwen3-embedding:0.6b"),
            app.embedding_timing_lane("ollama", "gemma:latest"),
        )
        self.assertEqual(
            app.embedding_timing_lane("openrouter", "model-a"),
            app.embedding_timing_lane("openrouter", "model-b"),
        )

    def test_local_output_eta_uses_the_selected_simulation_model_lane(self):
        import rag_pdf_gradio_app as app

        engine, model = app.timing_local_simulation_identity("Ollama: qwen3-embedding:0.6b")
        self.assertEqual((engine, model), ("ollama", "qwen3-embedding:0.6b"))
        profile = {
            "page_count": 3, "documents": 1, "mean_chars_per_page": 1500,
            "ocr_risk_bucket": "low", "text_density_bucket": "medium",
            "layout_bucket": "text_first", "line_density_bucket": "medium",
            "page_variability_bucket": "consistent", "file_size_bucket": "light",
        }
        qwen = app.timing_model_features(
            profile, app.MODE_LOCAL_ONLY_LABEL, "local only",
            simulation_engine="ollama", simulation_model="qwen3-embedding:0.6b",
        )
        gemma = app.timing_model_features(
            profile, app.MODE_LOCAL_ONLY_LABEL, "local only",
            simulation_engine="ollama", simulation_model="embeddinggemma:latest",
        )
        self.assertNotEqual(qwen["embedding_timing_lane"], gemma["embedding_timing_lane"])
        self.assertNotEqual(qwen["timing_formula_lane"], gemma["timing_formula_lane"])
        no_embedding = dict(qwen, embedding_engine="disabled", embedding_model="none")
        self.assertGreater(
            app.timing_model_base_seconds(qwen),
            app.timing_model_base_seconds(no_embedding),
        )

    def test_batch_timing_can_use_vector_observed_warning_without_learning_run_duration(self):
        import rag_pdf_gradio_app as app

        row = {
            "source": "automatic-run", "state": "warning",
            "duration_provenance": "active_observation_window",
            "actual_batches": 2, "batch_seconds": [2.4, 2.8],
            "page_count": 8, "actual_seconds": 30,
        }
        self.assertFalse(app.timing_model_observation_usable(row))
        self.assertTrue(app.timing_model_batch_observation_usable(row))
        row["timing_terminal_message"] = (
            "Searchable vectors and runtime retrieval succeeded, but the final storage observation could not confirm workspace document-list rows for one or more uploads."
        )
        self.assertTrue(app.timing_model_observation_usable(row))

    def test_batch_timing_learns_only_accepted_measurements_from_failed_run(self):
        import rag_pdf_gradio_app as app

        row = {
            "source": "automatic-run", "state": "failed",
            "duration_provenance": "active_observation_window", "actual_batches": 2,
            "batch_seconds": [122.0, 300.0], "ocr_used": False,
            "batch_measurements": [
                {"state": "accepted", "elapsed_seconds": 122.0, "submission_seconds": 120.0},
                {"state": "unresolved", "elapsed_seconds": 300.0, "submission_seconds": 120.0},
            ],
            "mode": app.MODE_NATIVE_UPLOAD_LABEL,
            "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            "native_upload_transport": "raw_text_document",
            "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            "embedding_engine": "openrouter", "embedding_model": "provider-model",
            "embedding_verification_mode": "checkpoint", "embedding_verification_interval": 3,
        }
        features = dict(row, embedding_timing_lane=app.embedding_timing_lane("openrouter", "another-model"))

        self.assertTrue(app.timing_model_batch_observation_usable(row))
        prior, samples, _source = app.timing_model_batch_prior_seconds(features, [row])
        self.assertEqual(samples, 1)
        self.assertAlmostEqual(prior, 131.76)

    def test_serialized_eta_does_not_borrow_retired_concurrent_batch_timings(self):
        import rag_pdf_gradio_app as app

        historical = {
            "source": "automatic-run", "state": "successful",
            "duration_provenance": "active_observation_window", "actual_batches": 1,
            "batch_measurements": [{"state": "accepted", "elapsed_seconds": 5.0}],
            "mode": app.MODE_NATIVE_UPLOAD_LABEL,
            "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            "native_upload_transport": "raw_text_document",
            "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            "embedding_engine": "openrouter", "embedding_model": "provider-model",
            "embedding_verification_mode": "every_batch", "embedding_verification_interval": 1,
            "embedding_submission_parallelism": 4,
        }
        features = dict(historical, embedding_submission_parallelism=1)
        features["embedding_timing_lane"] = app.embedding_timing_lane("openrouter", "provider-model")

        prior, samples, source = app.timing_model_batch_prior_seconds(features, [historical])

        self.assertEqual(samples, 0)
        self.assertEqual(prior, 180.0)
        self.assertIn("serialized", source)

    def test_native_page_parent_eta_counts_one_outer_record_per_page(self):
        import rag_pdf_gradio_app as app

        profile = {
            "page_count": 13, "documents": 1, "mean_chars_per_page": 2837,
            "ocr_risk_bucket": "low", "text_density_bucket": "high",
            "layout_bucket": "text_first", "line_density_bucket": "medium",
            "page_variability_bucket": "consistent", "file_size_bucket": "light",
        }
        features = app.timing_model_features(
            profile,
            app.MODE_NATIVE_UPLOAD_LABEL,
            app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL,
            chunk_size=8191,
            chunk_overlap=0,
            target_passage_length=750,
            native_upload_transport="raw_text_document",
            native_upload_representation="page_parents",
        )

        self.assertEqual(features["estimated_records"], 13)
        self.assertEqual(features["estimated_batches"], 1)

    def test_partial_indexing_takes_priority_over_generic_submission_timeout(self):
        import rag_pdf_gradio_app as app

        completion = app.automatic_completion([{
            "api_upload_status": "failed",
            "api_upload_error": "timed out",
            "post_upload_verification_status": "partial_vector_coverage",
            "post_upload_matching_vectors": 8,
            "post_upload_expected_payloads": 28,
            "anythingllm_runtime_validation_status": "not_checked",
        }], True)

        self.assertEqual(completion["code"], "AUTO-EMBEDDING-PARTIAL-001")
        self.assertIn("8 of 28", completion["message"])
        self.assertIn("20", completion["message"])

    def test_ambiguous_timeout_is_never_reported_as_submission_rejection(self):
        import rag_pdf_gradio_app as app

        completion = app.automatic_completion([{
            "api_upload_status": "error",
            "api_upload_error": "timed out",
            "api_upload_error_classification": "client_timeout_submission_unknown",
            "post_upload_verification_status": "not_checked",
            "anythingllm_runtime_validation_status": "not_checked",
        }], True)

        self.assertEqual(completion["code"], "AUTO-EMBEDDING-RECONCILE-001")
        self.assertEqual(completion["state"], "warning")
        self.assertNotIn("AUTO-EMBEDDING-SUBMIT-001", completion["message"])
        self.assertIn("outcome remains unknown, not rejected", completion["message"])

    def test_batch_prior_requires_matching_scope_and_segmentation_lane(self):
        import rag_pdf_gradio_app as app

        base = {
            "source": "automatic-run", "state": "successful",
            "duration_provenance": "active_observation_window", "actual_batches": 2,
            "batch_seconds": [2.0, 2.2], "ocr_used": False,
            "mode": app.MODE_NATIVE_UPLOAD_LABEL,
            "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            "embedding_engine": "openrouter", "embedding_model": "first-model",
        }
        features = dict(base)
        features["embedding_model"] = "second-model"
        features["embedding_timing_lane"] = app.embedding_timing_lane("openrouter", "second-model")
        prior, samples, _source = app.timing_model_batch_prior_seconds(features, [base])
        self.assertEqual(samples, 2)
        self.assertLess(prior, 3.0)
        features["native_upload_scope"] = app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL
        _prior, samples, _source = app.timing_model_batch_prior_seconds(features, [base])
        self.assertEqual(samples, 0)
        features["native_upload_scope"] = app.NATIVE_UPLOAD_SCOPE_ALL_LABEL
        features["segment_mode"] = app.SEGMENT_PASSAGES_LABEL
        _prior, samples, _source = app.timing_model_batch_prior_seconds(features, [base])
        self.assertEqual(samples, 0)
        features["segment_mode"] = app.SEGMENT_PAGE_LIMIT_LABEL
        features["effective_segment_target"] = 900
        _prior, samples, source = app.timing_model_batch_prior_seconds(features, [base])
        # Exact chunk/target evidence remains preferred, but a same-provider,
        # same scope/transport/segmentation-family cadence is a guarded
        # fallback when the history would otherwise be empty.
        self.assertEqual(samples, 2)
        self.assertIn("family", source)

    def test_timing_formula_lane_keeps_transport_and_chunking_conditions_distinct(self):
        import rag_pdf_gradio_app as app

        base = {
            "mode": app.MODE_NATIVE_UPLOAD_LABEL,
            "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            "native_upload_transport": "file_upload",
            "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            "target_passage_length": 750,
            "chunk_size": 1024,
            "chunk_overlap": 100,
            "embedding_timing_lane": "cloud:openrouter",
        }
        remote = dict(base, native_upload_transport="raw_text")
        alternate_chunking = dict(base, chunk_size=512, chunk_overlap=50)
        self.assertNotEqual(app.timing_formula_lane(base), app.timing_formula_lane(remote))
        self.assertNotEqual(app.timing_formula_lane(base), app.timing_formula_lane(alternate_chunking))

    def test_timing_uses_the_effective_splitter_cap_for_record_estimate(self):
        import rag_pdf_gradio_app as app

        profile = {
            "page_count": 50,
            "documents": 1,
            "mean_chars_per_page": 3_500,
            "ocr_risk_bucket": "low",
            "text_density_bucket": "high",
            "layout_bucket": "text_first",
            "line_density_bucket": "high",
            "page_variability_bucket": "mixed",
            "file_size_bucket": "medium",
        }
        constrained = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, target_passage_length=750,
            chunk_size=350, chunk_overlap=0,
        )
        unconstrained = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, target_passage_length=750,
            chunk_size=1_024, chunk_overlap=0,
        )
        self.assertEqual(constrained["effective_segment_target"], 350)
        self.assertEqual(unconstrained["effective_segment_target"], 750)
        self.assertGreater(constrained["estimated_records"], unconstrained["estimated_records"])
        local_constrained = app.timing_model_features(
            profile, app.MODE_LOCAL_ONLY_LABEL, "local only",
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, target_passage_length=750,
            chunk_size=350, chunk_overlap=0,
            simulation_engine="ollama", simulation_model="qwen3-embedding:0.6b",
        )
        self.assertEqual(local_constrained["effective_segment_target"], 350)
        self.assertGreater(
            local_constrained["estimated_records"],
            unconstrained["estimated_records"],
        )

    def test_native_probe_eta_counts_one_submission_cycle_per_pdf(self):
        import rag_pdf_gradio_app as app

        profile = {
            "page_count": 24,
            "documents": 10,
            "mean_chars_per_page": 1_500,
            "ocr_risk_bucket": "low",
            "text_density_bucket": "medium",
            "layout_bucket": "text_first",
            "line_density_bucket": "medium",
            "page_variability_bucket": "mixed",
            "file_size_bucket": "light",
        }
        probe = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, target_passage_length=750,
            chunk_size=350, chunk_overlap=75,
        )
        full = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, target_passage_length=750,
            chunk_size=350, chunk_overlap=75,
        )
        self.assertEqual(probe["estimated_records"], 2)
        self.assertEqual(probe["estimated_batches"], 10)
        self.assertLess(probe["estimated_records"], full["estimated_records"])
        self.assertGreater(
            app.timing_model_base_seconds(probe, batch_seconds_prior=3),
            150,
        )

    def test_native_probe_eta_learns_its_repeated_observation_cost(self):
        import rag_pdf_gradio_app as app

        features = {
            "mode": app.MODE_NATIVE_UPLOAD_LABEL,
            "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
            "native_upload_transport": "file_upload",
            "embedding_timing_lane": "cloud:openrouter",
            "document_count": 10,
            "page_count": 24,
            "estimated_records": 2,
            "estimated_batches": 10,
            "layout_bucket": "text_first",
            "line_density_bucket": "medium",
            "page_variability_bucket": "mixed",
            "ocr_planned": False,
            "ocr_risk_bucket": "low",
        }
        row = {
            **features,
            "source": "automatic-run",
            "state": "successful",
            "duration_provenance": "active_observation_window",
            "run_key": "production-probe-run",
            "actual_seconds": 410,
        }
        seconds, samples, source = app.timing_native_probe_observation_prior(features, [row])
        self.assertEqual(samples, 1)
        self.assertGreater(seconds, 20)
        self.assertIn("measured cloud:openrouter", source)

    def test_observed_ocr_surcharge_is_scoped_to_one_matching_mode_file(self):
        import rag_pdf_gradio_app as app

        estimate = {
            "expected_seconds": 819,
            "features": {
                "page_count": 318,
                "mode": app.MODE_LOCAL_ONLY_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
                "ocr_planned": False,
            },
        }
        history = [
            {
                "source": "automatic-run",
                "state": "successful",
                "actual_seconds": 35.1,
                "duration_provenance": "active_observation_window",
                "page_count": 8,
                "ocr_used": True,
                "mode": app.MODE_LOCAL_ONLY_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            },
            {
                # A native-upload observation is intentionally not allowed to
                # turn local packaging into a remote-ingestion prediction.
                "source": "automatic-run",
                "state": "successful",
                "actual_seconds": 332.7,
                "duration_provenance": "active_observation_window",
                "page_count": 18,
                "ocr_used": True,
                "mode": app.MODE_NATIVE_UPLOAD_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            },
        ]
        surcharge = app.ocr_runtime_surcharge_seconds(estimate, history, observed_pages=18)
        self.assertGreater(surcharge, 0)
        self.assertLessEqual(surcharge, 40)

    def test_native_upload_eta_does_not_learn_a_lower_multiplier_from_local_only_history(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        original_history = app.hydrated_timing_model_history
        original_config = app.anythingllm_embedding_config
        try:
            app.automatic_timing_document_profile = lambda _files: {
                "page_count": 12, "mean_chars_per_page": 3_000,
                "text_density_bucket": "high", "layout_bucket": "text_first",
                "ocr_risk_bucket": "low", "line_density_bucket": "medium",
                "page_variability_bucket": "consistent", "file_size_bucket": "light",
            }
            app.anythingllm_embedding_config = lambda _storage: {
                "engine": "openrouter", "model": "qwen/qwen3-embedding-8b", "batch_size": 9,
            }
            app.hydrated_timing_model_history = lambda: [{
                "source": "automatic-run", "actual_seconds": 30, "page_count": 12,
                "mode": app.MODE_LOCAL_ONLY_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
                "chunk_size": 0, "chunk_overlap": 0, "backend_mode": "Automatic",
                "unstructured_strategy": "auto", "embedding_engine": "openrouter",
                "embedding_model": "qwen/qwen3-embedding-8b", "ocr_used": False,
            }]
            estimate = app.estimate_automatic_run(
                ["sample.pdf"], app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, chunk_size=0, chunk_overlap=0,
                backend_mode="Automatic", unstructured_strategy="auto",
            )
        finally:
            app.automatic_timing_document_profile = original_profile
            app.hydrated_timing_model_history = original_history
            app.anythingllm_embedding_config = original_config

        self.assertEqual(estimate["comparable_runs"], 0)
        self.assertIn("first-run formula", estimate["source"])

    def test_timing_features_separate_local_submission_batches_from_anythingllm_config(self):
        import rag_pdf_gradio_app as app

        original_config = app.anythingllm_embedding_config
        try:
            app.anythingllm_embedding_config = lambda _storage: {"batch_size": 9}
            features = app.timing_model_features(
                {"page_count": 1, "mean_chars_per_page": 5_000},
                app.MODE_NATIVE_UPLOAD_LABEL,
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL,
                chunk_size=512,
            )
        finally:
            app.anythingllm_embedding_config = original_config

        self.assertEqual(features["embedding_batch_size"], 2)
        self.assertEqual(features["anythingllm_config_batch_size"], 9)
        self.assertEqual(features["estimated_batches"], 1)

    def test_scan_risk_does_not_add_ocr_work_until_ocr_is_selected(self):
        import rag_pdf_gradio_app as app

        profile = {
            "page_count": 8,
            "mean_chars_per_page": 0,
            "text_density_bucket": "low",
            "layout_bucket": "image_or_table_heavy",
            "ocr_risk_bucket": "high",
        }
        plain = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, chunk_size=512,
            backend_mode="PyMuPDF", unstructured_strategy="auto",
        )
        ocr = app.timing_model_features(
            profile, app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL, chunk_size=512,
            backend_mode="Unstructured", unstructured_strategy="hi_res",
        )
        self.assertFalse(plain["ocr_planned"])
        self.assertTrue(ocr["ocr_planned"])
        self.assertLess(plain["estimated_records"], ocr["estimated_records"])

    def test_progress_allocation_uses_general_scan_class_not_equal_pdf_slots(self):
        import rag_pdf_gradio_app as app

        original_profile = app.automatic_timing_document_profile
        try:
            def profile_for(files):
                if "scan" in Path(files[0]).name:
                    return {
                        "page_count": 18,
                        "mean_chars_per_page": 0,
                        "sparse_fraction": 1.0,
                        "image_density": 1.0,
                        "text_density_bucket": "low",
                        "layout_bucket": "image_or_table_heavy",
                        "ocr_risk_bucket": "high",
                    }
                return {
                    "page_count": 12,
                    "mean_chars_per_page": 3_000,
                    "sparse_fraction": .08,
                    "image_density": 1.0,
                    "text_density_bucket": "high",
                    "layout_bucket": "image_or_table_heavy",
                    "ocr_risk_bucket": "low",
                }

            app.automatic_timing_document_profile = profile_for
            allocations = app.automatic_progress_file_allocations(
                ["native-ocr-text-layer.pdf", "image-scan.pdf"],
                segment_mode=app.SEGMENT_PAGE_LIMIT_LABEL,
                chunk_size=750,
                backend_mode="Automatic",
            )
        finally:
            app.automatic_timing_document_profile = original_profile

        self.assertEqual(len(allocations), 2)
        self.assertFalse(allocations[0]["ocr_likely_from_preflight"])
        self.assertTrue(allocations[1]["ocr_likely_from_preflight"])
        self.assertGreater(allocations[1]["share"], allocations[0]["share"])
        self.assertAlmostEqual(allocations[-1]["end_share"], 1.0)

    def test_timing_model_ignores_fixture_and_incomplete_observations(self):
        import rag_pdf_gradio_app as app

        self.assertFalse(app.timing_model_observation_usable({
            "source": "automatic-run", "page_count": 0, "actual_seconds": .2,
        }))
        self.assertFalse(app.timing_model_observation_usable({
            "source": "fixture", "page_count": 8, "actual_seconds": 30,
        }))
        self.assertFalse(app.timing_model_observation_usable({
            "source": "automatic-run", "state": "warning", "page_count": 8, "actual_seconds": 30,
        }))
        self.assertFalse(app.timing_model_observation_usable({
            "source": "automatic-run", "state": "successful", "page_count": 8, "actual_seconds": 30,
        }))
        self.assertTrue(app.timing_model_observation_usable({
            "source": "automatic-run", "state": "successful", "page_count": 8, "actual_seconds": 30,
            "duration_provenance": "active_observation_window",
        }))
        self.assertFalse(app.timing_model_observation_usable({
            "source": "automatic-run", "state": "successful", "page_count": 8, "actual_seconds": 30,
            "duration_provenance": "active_observation_window",
            "run_key": str(PROJECT_ROOT / "tmp-output" / "app-run-fixture"),
        }))

    def test_unplanned_large_ocr_candidate_cannot_add_a_multi_hour_upload_jump(self):
        import rag_pdf_gradio_app as app

        estimate = {
            "expected_seconds": 2_352,
            "features": {
                "page_count": 1_756,
                "mode": app.MODE_NATIVE_UPLOAD_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
                "ocr_planned": False,
            },
        }
        surcharge = app.ocr_runtime_surcharge_seconds(estimate, [], observed_pages=791)
        self.assertGreaterEqual(surcharge, 0)
        self.assertLessEqual(surcharge, 791 * 6)
        self.assertLessEqual(estimate["expected_seconds"] + surcharge, 2_352 + 791 * 6)

    def test_refresh_presentation_clears_prior_terminal_run_record(self):
        import rag_pdf_gradio_app as app

        app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "successful", "phase": "Ready for retrieval"}
        updates = app.reset_automatic_run_presentation()
        self.assertEqual(app.LIVE_AUTOMATIC_RUN_STATUS, {})
        self.assertTrue(updates[0]["visible"])
        self.assertIn('aria-valuenow="0"', updates[0]["value"])
        self.assertIn("Ready — Confirm to begin processing.", updates[0]["value"])
        self.assertIn("Est: 00m00s", updates[1]["value"])
        self.assertEqual(len(updates), 19)
        self.assertFalse(updates[3]["interactive"])
        self.assertNotIn("visible", updates[4])
        self.assertNotIn("visible", updates[7])
        self.assertFalse(updates[8]["interactive"])
        self.assertFalse(updates[10]["visible"])
        self.assertEqual(updates[13], [])

    def test_fresh_selection_resets_per_run_controls_to_defaults(self):
        import rag_pdf_gradio_app as app

        app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "successful", "phase": "Complete"}
        updates = app.reset_automatic_run_settings_to_defaults()
        self.assertEqual(len(updates), 41)
        self.assertEqual(updates[0]["value"], "")  # document label
        self.assertEqual(updates[5]["value"], str(app.AUTO_OUTPUT_DIR))
        self.assertEqual(updates[7]["value"], "")  # visible API-key field
        self.assertEqual(updates[8]["value"], app.INITIAL_WORKSPACE_VALUE)
        self.assertEqual(updates[9]["value"], "")  # new workspace name
        self.assertEqual(updates[10], "")  # generated-name auto state
        self.assertEqual(updates[25]["value"], "Automatic")
        self.assertEqual(updates[34]["value"], False)  # do not retain apply-before-run
        self.assertEqual(updates[38]["value"], "")  # validation phrases

    def test_active_run_blocks_next_selection_presentation_callbacks(self):
        """Late selection events must not redraw an in-flight run as a new one."""
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "running", "run_root": "active-run"}
            def assert_noop(updates, expected_count):
                self.assertEqual(len(updates), expected_count)
                self.assertTrue(all(update.get("__type__") == "update" for update in updates))

            assert_noop(app.detected_metadata_preview(["next.pdf"]), 5)
            assert_noop(
                app.update_new_workspace_name_control(
                    app.NEW_DOCUMENT_WORKSPACE_VALUE, "Next PDF", ["next.pdf"], "", ""
                ),
                2,
            )
            assert_noop(app.automatic_mode_ui_updates(app.MODE_LOCAL_ONLY_LABEL), 15)
            self.assertEqual(app.automatic_process_button_state(["next.pdf"], [])["__type__"], "update")
            assert_noop(app.scan_selected_pdf_directory("C:\\not-used"), 7)
            assert_noop(app.choose_and_scan_pdf_directory("C:\\not-used"), 9)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    def test_output_mode_updates_hide_upload_only_controls_without_rewriting_values(self):
        import rag_pdf_gradio_app as app

        local_updates = app.automatic_mode_ui_updates(app.MODE_LOCAL_ONLY_LABEL)
        self.assertEqual(len(local_updates), 15)
        for update in local_updates[:14]:
            self.assertFalse(update["visible"])
            self.assertNotIn("value", update)
        self.assertEqual(local_updates[14]["value"], "")
        self.assertFalse(local_updates[14]["visible"])

        upload_updates = app.automatic_mode_ui_updates(app.MODE_NATIVE_UPLOAD_LABEL)
        for update in upload_updates[:14]:
            self.assertTrue(update["visible"])
            self.assertNotIn("value", update)
        self.assertEqual(upload_updates[14]["value"], "")
        self.assertFalse(upload_updates[14]["visible"])

    def test_local_only_confirmation_omits_workspace_and_anythingllm_chunk_summary(self):
        import rag_pdf_gradio_app as app

        rendered = app.automatic_confirmation_html({
            "mode": app.MODE_LOCAL_ONLY_LABEL,
            "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
            "target_passage_length": 750,
            "workspace_slug": app.NEW_DOCUMENT_WORKSPACE_VALUE,
            "anythingllm_chunk_size": 768,
            "anythingllm_chunk_overlap": 128,
        })
        self.assertIn("Local output only", rendered)
        self.assertIn("750 character target", rendered)
        self.assertNotIn("workspace", rendered.casefold())
        self.assertNotIn("768 chunk", rendered)

    def test_validated_local_only_run_strips_upload_only_values(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_estimate = app.estimate_automatic_run
        try:
            app.validate_pdf_inputs = lambda _files: (["C:/tmp/example.pdf"], None)
            app.estimate_automatic_run = lambda *_args, **_kwargs: {
                "expected_seconds": 12,
                "source": "test",
            }
            settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update({
                "pdf_files": ["C:/tmp/example.pdf"],
                "folder_pdf_files": [],
                "mode": app.MODE_LOCAL_ONLY_LABEL,
                "api_url": "http://127.0.0.1:3001",
                "api_key": "fixture-placeholder",  # pragma: allowlist secret
                "workspace_slug": app.NEW_DOCUMENT_WORKSPACE_VALUE,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
                "native_metadata_mode": "Strict metadata only",
                "anythingllm_create_document_folders": True,
                "anythingllm_document_folder_name": "old-upload-folder",
                "auto_apply_recommended_settings": True,
            })
            canonical, report, _warnings, allowed = app.validated_automatic_run_settings(
                [settings[field] for field in app.AUTOMATIC_RUN_FIELDS]
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.estimate_automatic_run = original_estimate

        self.assertIsNone(report)
        self.assertTrue(allowed)
        self.assertEqual(canonical["api_url"], "")
        self.assertEqual(canonical["api_key"], "")
        self.assertEqual(canonical["workspace_slug"], "")
        self.assertEqual(canonical["native_upload_scope"], "local_only")
        self.assertEqual(canonical["native_metadata_mode"], "not_applicable")
        self.assertFalse(canonical["anythingllm_create_document_folders"])
        self.assertEqual(canonical["anythingllm_document_folder_name"], "")
        self.assertFalse(canonical["auto_apply_recommended_settings"])

    def test_each_automatic_run_root_is_reserved_freshly(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            first = app.create_fresh_automatic_run_root(temp_dir)
            second = app.create_fresh_automatic_run_root(temp_dir)
        self.assertTrue(first.name.startswith("app-run-"))
        self.assertTrue(second.name.startswith("app-run-"))
        self.assertNotEqual(first, second)

    def test_fresh_selection_eta_uses_defaults_not_prior_controls(self):
        import rag_pdf_gradio_app as app

        captured = []
        original_estimate = app.refresh_automatic_run_estimate
        try:
            app.refresh_automatic_run_estimate = lambda *values, **kwargs: captured.append((values, kwargs)) or {"value": "fresh"}
            result = app.refresh_automatic_run_estimate_for_fresh_selection(["fresh.pdf"], [])
        finally:
            app.refresh_automatic_run_estimate = original_estimate

        self.assertEqual(result, {"value": "fresh"})
        self.assertEqual(len(captured), 1)
        defaults = app.fresh_automatic_run_setting_values(["fresh.pdf"], [])
        self.assertEqual(captured[0][0], (
            defaults["pdf_files"],
            defaults["folder_pdf_files"],
            defaults["mode"],
            defaults["native_upload_scope"],
            defaults["workspace_slug"],
            defaults["segment_mode"],
            defaults["target_passage_length"],
            defaults["anythingllm_chunk_size"],
            defaults["anythingllm_chunk_overlap"],
            defaults["backend_mode"],
            defaults["unstructured_strategy"],
            defaults["local_check_mode"],
        ))
        self.assertIsNone(captured[0][1]["folder_manifest"])
        self.assertNotIn("profile_document_limit", captured[0][1])

    def test_timing_profile_policy_is_stable_for_small_and_large_folder_runs(self):
        import rag_pdf_gradio_app as app

        self.assertIsNone(
            app.automatic_timing_profile_document_limit(["x.pdf"] * app.BATCH_FOLDER_FULL_PROFILE_DOCUMENT_LIMIT)
        )
        self.assertEqual(
            app.automatic_timing_profile_document_limit(["x.pdf"] * (app.BATCH_FOLDER_FULL_PROFILE_DOCUMENT_LIMIT + 1)),
            app.BATCH_FOLDER_INITIAL_PROFILE_DOCUMENT_LIMIT,
        )

    def test_fresh_selection_history_defaults_match_the_reset_controls(self):
        import rag_pdf_gradio_app as app

        defaults = app.fresh_automatic_run_setting_values(["fresh.pdf"], [])
        updates = app.reset_automatic_run_settings_to_defaults()
        field_to_update = {
            "document_label": 0,
            "document_author": 1,
            "document_short_label": 2,
            "use_file_title_fallback": 3,
            "mode": 4,
            "output_root_override": 5,
            "api_url": 6,
            "api_key": 7,
            "workspace_slug": 8,
            "native_upload_scope": 11,
            "native_upload_custom_range": 12,
            "native_metadata_mode": 14,
            "anythingllm_create_document_folders": 15,
            "anythingllm_document_folder_name": 16,
            "local_check_mode": 17,
            "custom_ollama_model": 18,
            "ollama_url": 19,
            "vector_audit_scope": 20,
            "deep_extraction": 21,
            "include_front_matter": 22,
            "include_back_matter": 23,
            "segment_mode": 24,
            "backend_mode": 25,
            "first_page_override": 26,
            "end_page_override": 27,
            "target_passage_length": 29,
            "page_preserve_ceiling": 30,
            "inherit_anythingllm_settings": 31,
            "anythingllm_chunk_size": 32,
            "anythingllm_chunk_overlap": 33,
            "auto_apply_recommended_settings": 34,
            "download_full_folder": 35,
            "download_segments_folder": 36,
            "advanced_end_section_names": 37,
            "automatic_validation_phrases": 38,
            "unstructured_strategy": 39,
            "generate_inline_fallback": 40,
        }
        for field, index in field_to_update.items():
            self.assertEqual(updates[index]["value"], defaults[field], field)

    def test_idle_estimate_callback_cannot_replace_a_running_timer(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "running", "run_root": "active-run"}
            update = app.refresh_automatic_run_estimate(
                ["future.pdf"], [], app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL
            )
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(update, {"__type__": "update"})

    def test_idle_estimate_callback_cannot_replace_a_completed_timer(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "successful",
                "phase": "Ready for retrieval",
                "started_epoch": 100.0,
                "last_activity_epoch": 147.0,
            }
            update = app.refresh_automatic_run_estimate(
                ["future.pdf"], [], app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL
            )
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(update, {"__type__": "update"})

    def test_live_progress_does_not_walk_back_for_a_small_reestimate(self):
        import rag_pdf_gradio_app as app

        record = {
            "state": "running",
            "phase": "Submitting AnythingLLM batch 2",
            "confirmed_fraction": 0.765,
            "phase_start_fraction": 0.765,
            "phase_started_epoch": 100.0,
            "phase_allowance": 0.025,
            "phase_budget_seconds": 20.0,
            "display_anchor_fraction": 0.79,
            "display_anchor_epoch": 100.0,
            "display_target_fraction": 0.79,
        }
        self.assertEqual(app.paced_progress_percent(record, now=100.0), 79)

    def test_live_progress_limits_a_large_forward_estimate_to_four_points_per_second(self):
        import rag_pdf_gradio_app as app

        record = {
            "state": "running",
            "confirmed_fraction": 0.70,
            "phase_start_fraction": 0.70,
            "phase_started_epoch": 100.0,
            "phase_allowance": 0.0,
            "display_anchor_fraction": 0.40,
            "display_anchor_epoch": 100.0,
            "display_target_fraction": 0.70,
        }
        self.assertEqual(app.paced_progress_percent(record, now=100.5), 42)
        self.assertEqual(app.paced_progress_percent(record, now=108.0), 70)

    def test_running_estimate_counts_down_and_stops_at_zero(self):
        import rag_pdf_gradio_app as app

        countdown = app.automatic_run_timing_html(
            expected_seconds=138,
            state="running",
            started_epoch=100.0,
            now=103.0,
        )
        overrun = app.automatic_run_timing_html(
            expected_seconds=138,
            state="running",
            started_epoch=100.0,
            now=240.0,
        )
        self.assertIn("Est: 02m15s", countdown)
        self.assertIn("Est: 00m00s", overrun)

    def test_running_estimate_always_counts_down_and_can_accelerate_with_progress(self):
        import rag_pdf_gradio_app as app

        baseline = app.automatic_run_timing_html(
            expected_seconds=154,
            state="running",
            started_epoch=100.0,
            now=101.0,
            server_driven=True,
        )
        accelerated = app.automatic_run_timing_html(
            expected_seconds=154,
            state="running",
            started_epoch=100.0,
            now=101.0,
            eta_acceleration_seconds=12,
            server_driven=True,
        )
        self.assertIn("Est: 02m33s", baseline)
        self.assertIn("Est: 02m21s", accelerated)
        self.assertIn('data-server-timer="true"', accelerated)

    def test_browser_countdown_replays_missed_seconds_instead_of_skipping_them(self):
        import rag_pdf_gradio_app as app

        self.assertIn("ragAutomaticRunDisplayedRemaining -= 1", app.APP_JS)
        self.assertIn("catchUp ? 110 : untilNextSecond", app.APP_JS)
        self.assertIn('timer.dataset.serverTimer === "true"', app.APP_JS)

    def test_running_status_refresh_replaces_the_server_ticked_eta(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "running",
                "phase": "Extracting text",
                "expected_seconds": 120,
                "started_epoch": 100.0,
                "confirmed_fraction": 0.2,
                "eta_acceleration_seconds": 5,
            }
            result = app.refresh_live_automatic_run_ui()
            self.assertIn('data-server-timer="true"', result[1])
            self.assertTrue(result[6]["interactive"])
            self.assertTrue(result[6]["visible"])
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    def test_inflight_anythingllm_batch_is_cancellable_by_owned_worker_termination(self):
        import rag_pdf_gradio_app as app

        self.assertTrue(app.automatic_run_cancel_is_safe("Submitting AnythingLLM batch 2 of 6 (5 records)"))
        self.assertTrue(app.automatic_run_cancel_is_safe("Verifying AnythingLLM batch 2 of 6"))
        rendered = app.automatic_live_status_html(
            {"state": "running", "phase": "Submitting AnythingLLM batch 2", "cancel_available": True}
        )
        self.assertIn('data-cancel-available="true"', rendered)
        self.assertIn('data-cancel-requested="false"', rendered)

    def test_accepted_cancellation_stops_the_visible_estimate(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        original_cancelled = set(app.CANCELLED_AUTOMATIC_RUN_ROOTS)
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "running",
                "run_root": "C:/temp/active-run",
                "expected_seconds": 120,
                "confirmed_fraction": 0.5,
                "cancel_available": False,
            }
            result = app.cancel_or_reset_automatic_run()
            self.assertIn('data-run-state="cancelled"', result[2]["value"])
            self.assertEqual(result[3]["value"], "Stopping processing…")
            self.assertIn(str(Path("C:/temp/active-run")), app.CANCELLED_AUTOMATIC_RUN_ROOTS)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status
            app.CANCELLED_AUTOMATIC_RUN_ROOTS.clear()
            app.CANCELLED_AUTOMATIC_RUN_ROOTS.update(original_cancelled)

    def test_prestart_cancel_preserves_the_ready_confirm_action(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {}
            result = app.cancel_or_reset_automatic_run(["selected.pdf"], [], {})
            confirm_button = result[3]
            retired_review_button = result[5]
            self.assertEqual(confirm_button["value"], "Confirm and start processing")
            self.assertEqual(confirm_button["variant"], "primary")
            self.assertTrue(confirm_button["interactive"])
            self.assertFalse(retired_review_button["visible"])
            self.assertFalse(result[6]["interactive"])
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    def test_cancellation_freezes_nonterminal_progress_at_the_last_confirmed_checkpoint(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        original_cancelled = set(app.CANCELLED_AUTOMATIC_RUN_ROOTS)
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "running",
                "run_root": "C:/temp/active-run",
                "confirmed_fraction": 0.387,
            }
            app.CANCELLED_AUTOMATIC_RUN_ROOTS.add("C:/temp/active-run")
            displayed, active = app.cancellation_safe_display_progress(
                "C:/temp/active-run", 0.936
            )
            self.assertTrue(active)
            self.assertEqual(displayed, 0.387)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status
            app.CANCELLED_AUTOMATIC_RUN_ROOTS.clear()
            app.CANCELLED_AUTOMATIC_RUN_ROOTS.update(original_cancelled)

    def test_cancel_requested_progress_never_advances_from_elapsed_time_or_late_events(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "running",
                "run_root": "C:/temp/active-run",
                "confirmed_fraction": 0.095,
                "cancel_requested": True,
                "phase_started_epoch": 100.0,
                "display_anchor_fraction": 0.095,
                "display_target_fraction": 0.095,
                "display_anchor_epoch": 100.0,
            }
            updated = app.update_live_automatic_run_status(
                "C:/temp/active-run",
                state="running",
                phase="Late worker callback",
                expected_seconds=100,
                confirmed_fraction=0.80,
                cancel_requested=True,
            )
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(updated["confirmed_fraction"], 0.095)
        self.assertEqual(updated["phase_allowance"], 0.0)
        self.assertEqual(app.paced_progress_percent(updated, now=1_000.0), 10)

    def test_cancel_requested_progress_freezes_at_the_visible_checkpoint_not_the_elapsed_estimate(self):
        import rag_pdf_gradio_app as app

        status = {
            "state": "running",
            "cancel_requested": True,
            "confirmed_fraction": 0.452,
            # The small paced allowance was already visible when the operator
            # clicked Cancel. That visible checkpoint, rather than a later
            # elapsed-time estimate or worker event, is what must remain.
            "display_anchor_fraction": 0.52,
            "display_target_fraction": 0.52,
            "display_anchor_epoch": 100.0,
            "expected_seconds": 60,
            "started_epoch": 0.0,
        }

        self.assertEqual(app.paced_progress_percent(status, now=10_000.0), 52)

    def test_runtime_retry_resets_visible_progress_before_replaying_preparation(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "state": "running",
                    "run_root": tmpdir,
                    "confirmed_fraction": 0.98,
                    "display_anchor_fraction": 0.995,
                    "display_target_fraction": 0.995,
                    "display_anchor_epoch": 100.0,
                    "started_epoch": 90.0,
                    "phase": "Preparation complete",
                }
                updated = app.update_live_automatic_run_status(
                    tmpdir,
                    state="running",
                    phase="AnythingLLM stopped; restarting before retry",
                    expected_seconds=100,
                    confirmed_fraction=0.10,
                    reset_progress=True,
                )
            finally:
                app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(updated["confirmed_fraction"], 0.10)
        self.assertLess(app.paced_progress_percent(updated, now=101.0), 20)

    def test_processing_label_stays_blue_while_force_stop_is_light_grey(self):
        import rag_pdf_gradio_app as app

        self.assertIn("#confirm-automatic-run-button button.rag-run-processing:disabled", app.APP_CSS)
        self.assertIn("background: #2563eb", app.APP_CSS)
        self.assertIn("#cancel-automatic-run-button button.rag-cancel-deferred", app.APP_CSS)
        self.assertIn("Stopping processing…", app.APP_JS)

    def test_cancel_terminates_only_the_owned_preparation_worker_tree(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "app-run-current"
            run_root.mkdir()
            marker = run_root / app.AUTOMATIC_RUN_WORKER_MARKER
            marker.write_text(
                json.dumps(
                    {
                        "kind": "automatic-preparation-worker",
                        "pid": 4242,
                        "run_root": str(run_root),
                    }
                ),
                encoding="utf-8",
            )
            owned_process = mock.Mock(pid=4242)
            owned_process.poll.return_value = None
            app.ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES[str(run_root)] = owned_process
            try:
                with mock.patch.object(app.subprocess, "run") as taskkill:
                    taskkill.return_value = SimpleNamespace(returncode=0)
                    self.assertTrue(app.terminate_automatic_run_worker(run_root))
            finally:
                app.ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES.pop(str(run_root), None)
            taskkill.assert_called_once()
            self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "4242", "/T", "/F"])

    def test_cancel_never_taskkills_a_stale_worker_marker_after_server_restart(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "app-run-stale"
            run_root.mkdir()
            (run_root / app.AUTOMATIC_RUN_WORKER_MARKER).write_text(
                json.dumps(
                    {
                        "kind": "automatic-preparation-worker",
                        "pid": 4242,
                        "run_root": str(run_root),
                    }
                ),
                encoding="utf-8",
            )
            app.ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES.pop(str(run_root), None)
            with mock.patch.object(app.subprocess, "run") as taskkill:
                self.assertFalse(app.terminate_automatic_run_worker(run_root))
            taskkill.assert_not_called()

    def test_confirmed_submission_locations_exclude_unsubmitted_plans(self):
        locations = pipeline.confirmed_submission_locations_from_ledger(
            {
                "batches": [
                    {"submission_state": "accepted", "locations": ["custom-documents/accepted.json"]},
                    {"submission_state": "cancelled_before_submission", "locations": ["custom-documents/not-sent.json"]},
                    {"submission_state": "rejected", "locations": ["custom-documents/refused.json"]},
                ],
                "inflight_batch": {"submission_state": "unresolved", "locations": ["custom-documents/unknown.json"]},
            }
        )
        self.assertEqual(locations, ["custom-documents/accepted.json", "custom-documents/unknown.json"])

    def test_queue_cleanup_blocks_uncertain_or_manual_activity_without_requests(self):
        original_delete = pipeline.delete_json
        try:
            pipeline.delete_json = lambda *_args, **_kwargs: self.fail("uncertain activity must not mutate Desktop")
            result = pipeline.remove_confirmed_workspace_queue_entries(
                "http://127.0.0.1:3001", "test-key", "safe-workspace",
                ["custom-documents/owned.json"], {"status": "quiet_stream_uncertain"},
            )
        finally:
            pipeline.delete_json = original_delete
        self.assertEqual(result["status"], "blocked_by_manual_activity_or_uncertainty")
        self.assertEqual(result["attempted"], 0)

    def test_queue_cleanup_retries_once_with_a_total_bounded_contract(self):
        original_delete = pipeline.delete_json
        calls = []
        try:
            def fake_delete(*_args, **_kwargs):
                calls.append(1)
                return (503, "busy") if len(calls) == 1 else (204, "")

            pipeline.delete_json = fake_delete
            result = pipeline.remove_confirmed_workspace_queue_entries(
                "http://localhost:3001", "test-key", "safe-workspace",
                ["custom-documents/owned.json"], {"status": "owned_activity_observed"},
                total_timeout=5, request_timeout=1,
            )
        finally:
            pipeline.delete_json = original_delete
        self.assertEqual(calls, [1, 1])
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["status"], "complete")

    def test_recovery_uses_nearest_worker_configuration_and_blocks_manual_activity(self):
        import rag_pdf_gradio_app as app

        original_resolve = app.resolve_anythingllm_api_key
        original_observe = app.observe_workspace_embedding_queue_activity
        original_remove = app.remove_confirmed_workspace_queue_entries
        try:
            app.resolve_anythingllm_api_key = lambda *_args, **_kwargs: ("managed-key", "managed_desktop_key")
            app.observe_workspace_embedding_queue_activity = lambda *_args, **_kwargs: {"status": "non_owned_activity_observed"}
            app.remove_confirmed_workspace_queue_entries = lambda *_args, **_kwargs: self.fail("manual activity must block cleanup")
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir) / "app-run-current"
                for name, api_url in (("first", "http://127.0.0.1:3001"), ("second", "http://127.0.0.1:3002")):
                    document = root / name
                    inspection = document / "inspection"
                    inspection.mkdir(parents=True)
                    (document / ".automatic-worker-config.json").write_text(
                        json.dumps({"args": {"anythingllm_api_url": api_url}}), encoding="utf-8"
                    )
                    (inspection / "embedding-batch-ledger.json").write_text(
                        json.dumps({"workspace_slug": f"workspace-{name}", "batches": [
                            {"submission_state": "accepted", "locations": [f"custom-documents/{name}.json"]}
                        ]}), encoding="utf-8"
                    )
                result = app.recover_automatic_run(root, policy="cancel_confirmed_queues", observation_seconds=0)
        finally:
            app.resolve_anythingllm_api_key = original_resolve
            app.observe_workspace_embedding_queue_activity = original_observe
            app.remove_confirmed_workspace_queue_entries = original_remove
        self.assertEqual([row["api_url"] for row in result["groups"]], ["http://127.0.0.1:3001", "http://127.0.0.1:3002"])
        self.assertTrue(all(row["status"] == "blocked_by_manual_activity_or_uncertainty" for row in result["groups"]))

    def test_restart_anyway_requires_explicit_confirmation(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            result = app.recover_automatic_run(
                Path(tmpdir), policy="restart_anythingllm_anyway", explicit_restart_confirmation=False,
            )
        self.assertEqual(result["status"], "restart_confirmation_required")

    def test_automatic_recovery_resumes_only_after_confirmed_owned_cleanup(self):
        import rag_pdf_gradio_app as app

        original_resolve = app.resolve_anythingllm_api_key
        original_observe = app.observe_workspace_embedding_queue_activity
        original_remove = app.remove_confirmed_workspace_queue_entries
        original_detect = app.detect_anythingllm_api_url
        original_submit = app.submit_embedding_resume_manifest
        try:
            app.resolve_anythingllm_api_key = lambda *_args, **_kwargs: ("managed-key", "managed_desktop_key")
            app.observe_workspace_embedding_queue_activity = lambda *_args, **_kwargs: {"status": "owned_activity_observed"}
            app.remove_confirmed_workspace_queue_entries = lambda *_args, **_kwargs: {"status": "complete", "removed": 1}
            app.detect_anythingllm_api_url = lambda *_args, **_kwargs: {"status": "reachable"}
            submissions = []
            app.submit_embedding_resume_manifest = lambda *args, **kwargs: (submissions.append((args, kwargs)) or {"status": "submitted"})
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir) / "app-run-current"
                inspection = root / "document" / "inspection"
                inspection.mkdir(parents=True)
                (root / "document" / ".automatic-worker-config.json").write_text(
                    json.dumps({"args": {"anythingllm_api_url": "http://127.0.0.1:3001"}}), encoding="utf-8"
                )
                (inspection / "embedding-batch-ledger.json").write_text(
                    json.dumps({"workspace_slug": "safe-workspace", "batches": [
                        {"submission_state": "unresolved", "locations": ["custom-documents/owned.json"]}
                    ]}), encoding="utf-8"
                )
                (inspection / "resume-embedding-manifest.json").write_text(
                    json.dumps({"workspace_slug": "safe-workspace", "recovery": {"remaining_locations": ["custom-documents/owned.json"]}}),
                    encoding="utf-8",
                )
                result = app.recover_automatic_run(
                    root, policy="automatic_recover", automatic=True, observation_seconds=0, grace_seconds=0,
                )
        finally:
            app.resolve_anythingllm_api_key = original_resolve
            app.observe_workspace_embedding_queue_activity = original_observe
            app.remove_confirmed_workspace_queue_entries = original_remove
            app.detect_anythingllm_api_url = original_detect
            app.submit_embedding_resume_manifest = original_submit
        self.assertEqual(len(submissions), 1)
        self.assertEqual(result["groups"][0]["action"], "reconcile_missing_and_resume")
        self.assertEqual(result["groups"][0]["resume"]["status"], "submitted")

    def test_terminal_run_status_reconciles_the_full_ui_after_a_lost_stream(self):
        import rag_pdf_gradio_app as app

        original = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir) / "prepared"
                output_dir.mkdir()
                parsed = output_dir / "Example-complete-pdf-parsed.txt"
                parsed.write_text("prepared", encoding="utf-8")
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "state": "warning",
                    "phase": "Searchable vectors verified; document-list observation needs review",
                    "details": "Searchable vectors are available; the Desktop document list is incomplete.",
                    "expected_seconds": 120,
                    "confirmed_fraction": 1.0,
                    "started_epoch": 100.0,
                    "updated_epoch": 170.0,
                    "output_paths": [str(parsed)],
                }
                updates = app.refresh_live_automatic_run_ui()
            # Terminal evidence remains visible until the user resets or starts
            # a new run, so the observer cannot restore the old estimate.
            self.assertEqual(app.LIVE_AUTOMATIC_RUN_STATUS["state"], "warning")
            second_updates = app.refresh_live_automatic_run_ui()
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original

        self.assertEqual(len(updates), 10)
        self.assertIn("Compl: 01m10s", updates[1])
        self.assertFalse(updates[2]["visible"])
        self.assertEqual(updates[3]["value"], "Completed — upload checks need review")
        self.assertFalse(updates[3]["interactive"])
        self.assertNotIn("visible", updates[4])
        self.assertNotIn("visible", updates[5])
        self.assertIn(">warning<", updates[8]["value"])
        self.assertTrue(updates[9]["visible"])
        self.assertTrue(updates[9]["interactive"])
        self.assertIn("Total progress: 100%", second_updates[0]["value"])
        self.assertIn("Compl: 01m10s", second_updates[1])

    def test_cancelled_run_never_claims_upload_verification_or_full_completion(self):
        import rag_pdf_gradio_app as app

        original = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {
                "state": "cancelled",
                "phase": "Processing stopped by operator",
                "details": "Stop requested. The current safe checkpoint finished; later PDFs were not submitted.",
                "expected_seconds": 720,
                "confirmed_fraction": 1.0,
                "started_epoch": 100.0,
                "last_activity_epoch": 170.0,
                "updated_epoch": 9_000.0,
                "cancel_requested": True,
                "cancel_available": False,
            }
            updates = app.refresh_live_automatic_run_ui()
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original

        self.assertIn('data-run-state="cancelled"', updates[0]["value"])
        self.assertIn("Total progress: 99%", updates[0]["value"])
        self.assertNotIn("Searchable vectors verified", updates[0]["value"])
        self.assertIn("Stopped: 01m10s", updates[1])
        self.assertEqual(updates[3]["value"], "Processing stopped")
        self.assertFalse(updates[3]["interactive"])

    def test_cancelled_preflight_exception_is_rendered_as_a_controlled_stop(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "cancelled-run"
            run_root.mkdir()
            original_status = app.LIVE_AUTOMATIC_RUN_STATUS
            original_cancelled = set(app.CANCELLED_AUTOMATIC_RUN_ROOTS)
            try:
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "state": "preparing",
                    "run_root": str(run_root),
                    "expected_seconds": 90,
                    "started_epoch": time.time() - 3,
                }
                app.CANCELLED_AUTOMATIC_RUN_ROOTS.add(str(run_root))
                outputs = app.automatic_error_outputs(
                    "AUTO-EMBEDDER-001",
                    "AnythingLLM cannot currently create embeddings",
                    ["A concurrent preflight failed."],
                )
            finally:
                app.LIVE_AUTOMATIC_RUN_STATUS = original_status
                app.CANCELLED_AUTOMATIC_RUN_ROOTS.clear()
                app.CANCELLED_AUTOMATIC_RUN_ROOTS.update(original_cancelled)

        self.assertIn(">cancelled<", outputs[0]["value"])
        self.assertEqual(outputs[4]["value"], "Processing stopped")
        self.assertFalse(outputs[4]["interactive"])
        self.assertIn("Stopped:", outputs[6])

    def test_refresh_anythingllm_embedder_model_dropdown_mentions_live_ollama_refresh(self):
        import rag_pdf_gradio_app as app

        update = app.refresh_anythingllm_embedder_model_dropdown("ollama", "embeddinggemma:latest")
        self.assertIn("Live Ollama embedding models are re-queried", update["info"])
        self.assertIn("limit cards", update["info"])

    def test_refresh_anythingllm_embedder_model_dropdown_mentions_native_and_openai_cards(self):
        import rag_pdf_gradio_app as app

        native_update = app.refresh_anythingllm_embedder_model_dropdown("anythingllm", "all-MiniLM-L6-v2")
        openai_update = app.refresh_anythingllm_embedder_model_dropdown("openai", "text-embedding-3-small")

        self.assertIn("all-MiniLM-L6-v2", native_update["info"])
        self.assertIn("nomic-embed-text-v1", native_update["info"])
        self.assertIn("text-embedding-3-small", openai_update["info"])
        self.assertIn("text-embedding-3-large", openai_update["info"])

    def test_refresh_anythingllm_embedder_model_controls_updates_recommended_limit(self):
        import rag_pdf_gradio_app as app

        model_update, max_update, recommended_update, status = app.refresh_anythingllm_embedder_model_controls(
            "openrouter",
            "qwen/qwen3-embedding-8b",
            2048,
        )
        self.assertEqual(model_update["value"], "qwen/qwen3-embedding-8b")
        self.assertEqual(max_update["value"], 32768)
        self.assertEqual(recommended_update["value"], 32768)
        self.assertIn("32768", status)

    def test_anythingllm_settings_reference_html_mentions_current_and_recommended_values(self):
        import rag_pdf_gradio_app as app

        html_value = app.anythingllm_settings_reference_html()
        self.assertIn("Current AnythingLLM values", html_value)
        self.assertIn("Recommended embedder max chunk", html_value)

    def test_expand_all_uses_gradio6_content_visibility_and_compact_control(self):
        import rag_pdf_gradio_app as app

        self.assertIn("data-testid='accordion-content'", app.EXPAND_ALL_CLICK_JS)
        self.assertIn('getComputedStyle(content).display === "none"', app.EXPAND_ALL_CLICK_JS)
        self.assertIn(".native-upload-subaccordion", app.EXPAND_ALL_CLICK_JS)
        self.assertIn("#expand-all-accordions-button {", app.APP_CSS)
        self.assertIn("width: auto !important", app.APP_CSS)

    def test_download_folder_control_is_visually_grouped_with_downloads(self):
        import rag_pdf_gradio_app as app

        self.assertIn(".automatic-download-section", app.APP_CSS)
        self.assertIn("#automatic-download-section", app.APP_CSS)
        self.assertIn(".download-folder-control", app.APP_CSS)
        self.assertIn(".downloads-header-row", app.APP_CSS)
        self.assertIn(".downloads-header-title", app.APP_CSS)
        self.assertIn("justify-content: flex-start", app.APP_CSS)
        self.assertIn("position: static !important", app.APP_CSS)
        self.assertIn("max-height: 28px !important", app.APP_CSS)
        self.assertIn("#expand-all-accordions-button", app.APP_CSS)
        self.assertIn("background: var(--background-fill-primary) !important", app.APP_CSS)
        self.assertIn(".download-folder-control label", app.APP_CSS)
        self.assertIn("background: rgba(59, 130, 246, 0.12) !important", app.APP_CSS)
        self.assertIn(".downloads-header-row {", app.APP_CSS)
        self.assertIn("flex: 1 1 100% !important", app.APP_CSS)
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("Run output and downloads", source)
        self.assertIn('elem_id="automatic-download-section"', source)
        self.assertIn("output-downloads-accordion", source)
        self.assertIn('label="PDF files"', source)
        self.assertIn('file_types=[".pdf"]', source)
        self.assertIn('Select PDF Folder Here', source)
        self.assertNotIn('auto_folder_file_table = gr.Dataframe', source)
        self.assertIn('label="PDFs to process"', source)
        self.assertIn("Only PDF files were uploaded from this mixed folder", source)
        self.assertNotIn('auto_folder_notice_ok = gr.Button("Ok"', source)
        self.assertNotIn('Scan Folder', source)
        self.assertNotIn('label="Selected folder"', source)
        self.assertIn("Download Full Folder", source)
        self.assertIn("Download Segments Folder", source)
        self.assertNotIn("Download as Folder", source)
        self.assertNotIn("Upload unit type", source)
        self.assertIn("All segments", source)
        self.assertIn("Two test segments", source)
        self.assertIn('label="AnythingLLM output folder"', source)
        self.assertIn('label="Include foreword/preface"', source)
        self.assertIn('label="Include notes/bibliography/index"', source)
        self.assertIn("Run read-only storage audit", source)
        self.assertIn("Generate dry-run stale-artifact repair plan", source)
        self.assertIn("[data-testid='accordion-content']", app.APP_CSS)

    def test_detect_anythingllm_api_url_rejects_plain_text_stub_and_falls_back_to_json_api(self):
        original_get_json = pipeline.get_json
        try:
            def fake_get_json(url, api_key=None, timeout=30):
                if url.startswith("http://127.0.0.1:8888"):
                    return 200, "OK"
                if url.startswith("http://127.0.0.1:3001"):
                    return 200, '{"online": true, "message": "pong"}'
                raise urllib.error.URLError("unreachable")

            pipeline.get_json = fake_get_json
            result = pipeline.detect_anythingllm_api_url("http://127.0.0.1:8888", timeout=0.01)
            self.assertEqual(result["api_url"], "http://127.0.0.1:3001")
            self.assertEqual(result["status"], "reachable")
            self.assertEqual(result["attempts"][0]["api_url"], "http://127.0.0.1:8888")
            self.assertEqual(result["attempts"][0]["status"], "collector_stub")
        finally:
            pipeline.get_json = original_get_json

    def test_ensure_anythingllm_runtime_autostarts_desktop_when_api_is_down(self):
        original_detect = pipeline.detect_anythingllm_api_url
        original_start = pipeline.start_anythingllm_desktop
        original_sleep = pipeline.time.sleep
        try:
            calls = {"detect": 0}

            def fake_detect(preferred_url="", api_key=None, timeout=2.0):
                calls["detect"] += 1
                if calls["detect"] == 1:
                    return {
                        "status": "unreachable",
                        "api_url": "http://127.0.0.1:3001",
                        "attempts": [],
                        "message": "down",
                    }
                return {
                    "status": "reachable",
                    "api_url": "http://127.0.0.1:3001",
                    "attempts": [],
                    "message": "up",
                }

            pipeline.detect_anythingllm_api_url = fake_detect
            pipeline.start_anythingllm_desktop = lambda executable_path=None: {
                "status": "started",
                "started": True,
                "already_running": False,
                "executable": "C:/AnythingLLM.exe",
                "error": "",
            }
            pipeline.time.sleep = lambda seconds: None
            result = pipeline.ensure_anythingllm_runtime(
                "http://127.0.0.1:3001",
                autostart_local=True,
                startup_timeout=5,
            )
        finally:
            pipeline.detect_anythingllm_api_url = original_detect
            pipeline.start_anythingllm_desktop = original_start
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["status"], "reachable")
        self.assertEqual(result["start"]["status"], "started")
        self.assertTrue(result["waited_for_runtime"])
        self.assertEqual(result["lifecycle"][-1], {"phase": "ready_after_start", "status": "reachable"})

    def test_ensure_anythingllm_runtime_reports_startup_lifecycle_without_changing_recovery(self):
        original_detect = pipeline.detect_anythingllm_api_url
        original_start = pipeline.start_anythingllm_desktop
        original_sleep = pipeline.time.sleep
        try:
            calls = {"detect": 0}
            events = []

            def fake_detect(preferred_url="", api_key=None, timeout=2.0):
                calls["detect"] += 1
                return {
                    "status": "reachable" if calls["detect"] >= 2 else "unreachable",
                    "api_url": "http://127.0.0.1:3001",
                    "attempts": [],
                    "message": "up" if calls["detect"] >= 2 else "down",
                }

            pipeline.detect_anythingllm_api_url = fake_detect
            pipeline.start_anythingllm_desktop = lambda executable_path=None: {
                "status": "started", "started": True, "already_running": False,
                "executable": "C:/AnythingLLM.exe", "error": "",
            }
            pipeline.time.sleep = lambda seconds: None
            result = pipeline.ensure_anythingllm_runtime(
                "http://127.0.0.1:3001",
                autostart_local=True,
                startup_timeout=5,
                status_callback=lambda phase, snapshot: events.append((phase, snapshot.get("status"))),
            )
        finally:
            pipeline.detect_anythingllm_api_url = original_detect
            pipeline.start_anythingllm_desktop = original_start
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["status"], "reachable")
        self.assertIn(("starting_desktop", "unreachable"), events)
        self.assertIn(("waiting_for_runtime", "unreachable"), events)
        self.assertEqual(events[-1], ("ready_after_start", "reachable"))

    def test_stale_artifact_report_groups_audit_findings_into_candidate_buckets(self):
        original_audit = pipeline.anythingllm_storage_audit
        try:
            pipeline.anythingllm_storage_audit = lambda storage_dir, workspace_slug="": {
                "status": "complete",
                "storage_dir": str(storage_dir),
                "workspace_slug": workspace_slug,
                "workspace_found": True,
                "missing_docpath_file_count": 2,
                "unreferenced_custom_document_count": 4,
                "orphan_vector_docid_count": 6,
                "error": "",
            }
            report = pipeline.anythingllm_stale_artifact_report(Path("C:/tmp/storage"), "alpha")
            buckets = {bucket["bucket"]: bucket for bucket in report["candidate_buckets"]}
            self.assertEqual(report["status"], "complete")
            self.assertIn("workspace_rows_missing_custom_document_files", buckets)
            self.assertIn("unreferenced_custom_document_json_files", buckets)
            self.assertIn("orphan_document_vectors", buckets)
            self.assertIn("No deletion", report["operator_summary"])
        finally:
            pipeline.anythingllm_storage_audit = original_audit

    def test_storage_audit_html_reports_read_only_audit_counts(self):
        import rag_pdf_gradio_app as app

        original_audit = app.anythingllm_storage_audit
        try:
            app.anythingllm_storage_audit = lambda storage_dir, workspace_slug: {
                "status": "complete",
                "storage_dir": "C:/Users/test/AppData/Roaming/anythingllm-desktop/storage",
                "workspace_document_global_count": 12,
                "workspace_document_selected_count": 5,
                "document_vector_global_count": 20,
                "custom_document_json_global_count": 11,
                "missing_docpath_file_count": 1,
                "unreferenced_custom_document_count": 2,
                "orphan_vector_docid_count": 3,
                "sample_missing_docpaths": ["docs/missing.json"],
                "sample_unreferenced_custom_documents": ["docs/orphan.json"],
                "sample_orphan_vector_docids": ["orphan-doc"],
            }
            rendered = app.storage_audit_html("alpha")
            self.assertIn("Storage audit", rendered)
            self.assertIn("Read-only audit", rendered)
            self.assertIn("Workspace document rows (global)", rendered)
            self.assertIn("orphan-doc", rendered)
        finally:
            app.anythingllm_storage_audit = original_audit

    def test_stale_artifact_report_html_renders_candidate_buckets_and_steps(self):
        import rag_pdf_gradio_app as app

        original_report = app.anythingllm_stale_artifact_report
        try:
            app.anythingllm_stale_artifact_report = lambda storage_dir, workspace_slug: {
                "status": "complete",
                "storage_dir": "C:/Users/test/AppData/Roaming/anythingllm-desktop/storage",
                "workspace_slug": "alpha",
                "candidate_buckets": [
                    {
                        "bucket": "orphan_document_vectors",
                        "count": 3,
                        "scope": "global",
                        "risk": "high",
                        "reason": "Vectors do not map back to workspace rows.",
                        "recommended_first_step": "Review matching LanceDB rows first.",
                    }
                ],
                "recommended_sequence": [
                    {
                        "step": 1,
                        "title": "Create a fresh AnythingLLM backup",
                        "details": "Snapshot DB and storage first.",
                    }
                ],
                "operator_summary": "Dry-run report found 1 stale-artifact bucket. No deletion was performed.",
                "error": "",
            }
            rendered = app.stale_artifact_report_html("alpha")
            self.assertIn("Dry-run stale-artifact repair plan", rendered)
            self.assertIn("orphan_document_vectors", rendered)
            self.assertIn("Create a fresh AnythingLLM backup", rendered)
            self.assertIn("No deletion", rendered)
        finally:
            app.anythingllm_stale_artifact_report = original_report

    def test_embedding_observer_requires_expected_counts_and_a_quiet_period(self):
        import rag_pdf_gradio_app as app

        baseline = {
            "api": {"reachable": True},
            "database_status": "observed",
            "workspace_documents": 1,
            "embedded_vectors": 1,
            "latest_document_epoch_ms": 100,
            "log": {"matches": ["baseline"]},
            "observed_epoch": 1000,
        }
        progressed = dict(
            baseline,
            workspace_documents=2,
            embedded_vectors=2,
            latest_document_epoch_ms=200,
            observed_epoch=1010,
        )
        status, quiet_since, quiet_seconds = app._observer_state_status(progressed, baseline, 2, None)
        self.assertEqual(status, "progress_observed")
        self.assertIsNone(quiet_since)
        self.assertEqual(quiet_seconds, 0)

        candidate = dict(progressed, observed_epoch=1020)
        status, quiet_since, quiet_seconds = app._observer_state_status(candidate, progressed, 2, None)
        self.assertEqual(status, "completion_candidate")
        self.assertEqual(quiet_since, 1020)
        self.assertEqual(quiet_seconds, 0)

        complete = dict(candidate, observed_epoch=1020 + app.EMBEDDING_OBSERVER_QUIET_SECONDS)
        status, quiet_since, quiet_seconds = app._observer_state_status(complete, candidate, 2, quiet_since)
        self.assertEqual(status, "complete_observed")
        self.assertEqual(quiet_seconds, app.EMBEDDING_OBSERVER_QUIET_SECONDS)

        incomplete = dict(complete, workspace_documents=1, embedded_vectors=1)
        status, quiet_since, quiet_seconds = app._observer_state_status(incomplete, complete, 2, quiet_since)
        self.assertEqual(status, "progress_observed")
        self.assertIsNone(quiet_since)
        self.assertEqual(quiet_seconds, 0)

    def test_embedding_observer_labels_runtime_and_database_limitations(self):
        import rag_pdf_gradio_app as app

        status, quiet_since, quiet_seconds = app._observer_state_status(
            {"api": {"reachable": False}, "database_status": "observed"}, {}, 1, None
        )
        self.assertEqual((status, quiet_since, quiet_seconds), ("runtime_unreachable", None, 0))
        status, quiet_since, quiet_seconds = app._observer_state_status(
            {"api": {"reachable": True}, "database_status": "database_busy"}, {}, 1, None
        )
        self.assertEqual((status, quiet_since, quiet_seconds), ("database_busy", None, 0))

    def test_embedding_observer_accepts_native_lancedb_record_evidence(self):
        import rag_pdf_gradio_app as app

        baseline = {
            "api": {"reachable": True},
            "database_status": "observed",
            "workspace_documents": 0,
            "observed_records": 2,
            "embedded_vectors": 2,
            "observed_epoch": 100,
        }
        status, quiet_since, quiet_seconds = app._observer_state_status(baseline, {}, 2, None)
        self.assertEqual((status, quiet_since, quiet_seconds), ("completion_candidate", 100, 0))
        stable = dict(baseline, observed_epoch=100 + app.EMBEDDING_OBSERVER_QUIET_SECONDS)
        status, quiet_since, quiet_seconds = app._observer_state_status(stable, baseline, 2, quiet_since)
        self.assertEqual(status, "complete_observed")
        self.assertEqual(quiet_seconds, app.EMBEDDING_OBSERVER_QUIET_SECONDS)

    def test_ocr_assistance_detects_scan_recovery_by_pymupdf4llm(self):
        evidence = pipeline.ocr_assistance_evidence(
            {
                "backend": "pymupdf4llm",
                "quality": {"included_words": 3000},
            },
            [
                {
                    "backend": "pymupdf",
                    "quality": {
                        "included_pages": 8,
                        "empty_pages": 8,
                        "scanned_likelihood": "high",
                    },
                }
            ],
            {"unstructured_runtime": {}},
        )
        self.assertEqual(evidence, {
            "used": True,
            "evidence": "pymupdf4llm_recovered_text_from_empty_native_layer",
        })

    def test_ocr_assistance_directly_checks_native_layer_for_explicit_pymupdf4llm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "scan.pdf"
            document = fitz.open()
            document.new_page()
            document.save(pdf_path)
            document.close()
            evidence = pipeline.ocr_assistance_evidence(
                {
                    "backend": "pymupdf4llm",
                    "quality": {"included_words": 3},
                    "segments": [{"pdf_page": 1, "text": "Recovered scan text"}],
                },
                [],
                {"source_file": str(pdf_path)},
            )
        self.assertEqual(evidence, {
            "used": True,
            "evidence": "pymupdf4llm_recovered_text_from_direct_empty_native_layer_probe",
        })

    def test_background_log_retention_keeps_recent_and_malformed_records(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"recorded_at": "2024-01-01T00:00:00", "state": "old"}),
                    json.dumps({"recorded_at": app.datetime.now().isoformat(timespec="seconds"), "state": "recent"}),
                    "malformed-for-manual-inspection",
                ]) + "\n",
                encoding="utf-8",
            )
            result = app.prune_background_jsonl(path, retention_days=365)
            remaining = path.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "pruned")
        self.assertEqual(result["removed"], 1)
        self.assertIn('"state": "recent"', remaining)
        self.assertIn("malformed-for-manual-inspection", remaining)

    def test_version_and_system_theme_are_explicit_ui_contracts(self):
        import rag_pdf_gradio_app as app

        self.assertRegex(app.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(app.APP_VERSION, "0.5.0")
        self.assertEqual(app.APP_BASE_COMMIT, "portable-package")
        self.assertIn('window.matchMedia("(prefers-color-scheme: dark)")', app.APP_JS)
        self.assertIn('systemThemeQuery.addEventListener("change", applySystemTheme)', app.APP_JS)
        self.assertIn('localStorage.setItem(themeFollowSystemKey, followSystem ? "true" : "false")', app.APP_JS)
        self.assertIn("syncFollowSystemControl", app.APP_JS)
        self.assertIn('initialUrl.searchParams.delete("__theme")', app.APP_JS)
        self.assertIn("THEME_TOGGLE_JS", Path(app.__file__).read_text(encoding="utf-8"))
        self.assertIn('const followSystem = nextDark === window.matchMedia("(prefers-color-scheme: dark)").matches', app.THEME_TOGGLE_JS)
        self.assertIn('localStorage.setItem("rag-pdf-follow-system-theme", followSystem ? "true" : "false")', app.THEME_TOGGLE_JS)
        self.assertIn('localStorage.setItem("rag-pdf-theme", nextDark ? "dark" : "light")', app.THEME_TOGGLE_JS)
        self.assertIn(".gradio-container .progress-text", app.APP_CSS)
        self.assertIn("display: none !important", app.APP_CSS)
        self.assertIn("decorateSelectedPdfActions", app.APP_JS)
        self.assertIn("Replace selected PDF", app.APP_JS)
        self.assertIn(".icon-button-wrapper.top-panel", app.APP_JS)
        self.assertIn("wireAutomaticRunTimer", app.APP_JS)
        self.assertNotIn("RUN_TIMER_START_JS", Path(app.__file__).read_text(encoding="utf-8"))

    def test_numeric_dropdown_update_keeps_custom_values_and_presets(self):
        import rag_pdf_gradio_app as app

        update = app.numeric_dropdown_update(832, app.CHUNK_SIZE_PRESET_CHOICES, interactive=True)
        self.assertEqual(update["value"], "832")
        self.assertIn("768", update["choices"])
        self.assertIn("832", update["choices"])

        custom_update = app.numeric_dropdown_update(913, app.CHUNK_SIZE_PRESET_CHOICES, interactive=False)
        self.assertEqual(custom_update["value"], "913")
        self.assertEqual(custom_update["choices"][0], "913")
        self.assertFalse(custom_update["interactive"])

    def test_simulation_preflight_blocks_oversized_probe_early(self):
        original = pipeline.get_embeddings_with_adapter_response
        calls = []
        try:
            def fake_embeddings(texts, adapter):
                calls.append(len(texts[0]))
                if len(calls) == 1:
                    return [[0.1, 0.2, 0.3]], pipeline.empty_remote_usage(provider="openrouter", model="test")
                raise RuntimeError("maximum context length exceeded")

            pipeline.get_embeddings_with_adapter_response = fake_embeddings
            result = pipeline.simulation_preflight(
                {"provider": "openrouter", "model": "openai/text-embedding-3-small", "url": "https://openrouter.ai/api/v1/embeddings"},
                effective_limit=4096,
                batch_size=1,
            )
        finally:
            pipeline.get_embeddings_with_adapter_response = original
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "SIM-PRE-LIMIT")
        self.assertGreaterEqual(result["boundary_probe_chars"], result["safe_probe_chars"])

    def test_unknown_embedder_capability_uses_4096_conservative_fallback(self):
        capability = pipeline.resolve_embedder_capability("generic-openai", "unknown/provider-model")
        self.assertEqual(capability["status"], "unknown_capability")
        self.assertEqual(capability["recommended_anythingllm_limit"], 4096)
        self.assertEqual(capability["safe_max_chunk_length"], 4096)

    def test_read_validation_workspace_template_prefers_deepseek_like_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    """
                    create table workspaces(
                        id integer primary key,
                        name text,
                        slug text,
                        chatProvider text,
                        chatModel text,
                        topN integer,
                        similarityThreshold real,
                        vectorSearchMode text,
                        chatMode text
                    )
                    """
                )
                con.execute(
                    "insert into workspaces(id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode) values (1,'Other','other','openrouter','gpt-4.1-mini',4,0.25,'default','query')"
                )
                con.execute(
                    "insert into workspaces(id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode) values (2,'DeepSeek','deepseek-main','openrouter','deepseek-v4-pro',8,0.3,'default','query')"
                )
                con.commit()
            finally:
                con.close()

            template = pipeline.read_validation_workspace_template(storage)
            self.assertEqual(template["status"], "pass")
            self.assertEqual(template["source_workspace_slug"], "deepseek-main")
            self.assertEqual(template["chat_model"], "deepseek-v4-pro")

    def test_update_workspace_runtime_template_sqlite_applies_chat_model_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    """
                    create table workspaces(
                        id integer primary key,
                        name text,
                        slug text,
                        chatProvider text,
                        chatModel text,
                        topN integer,
                        similarityThreshold real,
                        vectorSearchMode text,
                        chatMode text
                    )
                    """
                )
                con.execute(
                    "insert into workspaces(id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode) values (1,'Validation','validation-1','ollama','llama3',4,0.25,'default','chat')"
                )
                con.commit()
            finally:
                con.close()

            result = pipeline.update_workspace_runtime_template_sqlite(
                storage,
                "validation-1",
                {
                    "source_workspace_slug": "deepseek-main",
                    "chat_provider": "openrouter",
                    "chat_model": "deepseek-v4-pro",
                    "top_n": 8,
                    "similarity_threshold": 0.3,
                    "vector_search_mode": "default",
                    "chat_mode": "query",
                },
            )
            gate = pipeline.read_workspace_model_gate(storage, "validation-1")
            self.assertEqual(result["status"], "pass")
            self.assertTrue(result["verified"])
            self.assertEqual(gate["chat_provider"], "openrouter")
            self.assertEqual(gate["chat_model"], "deepseek-v4-pro")

    def test_run_temporary_workspace_validation_cleans_workspace_by_default(self):
        original_create = pipeline.create_validation_workspace
        original_upload = pipeline.maybe_upload_to_anythingllm
        original_post = pipeline.verify_anythingllm_post_upload
        original_runtime = pipeline.validate_anythingllm_native_runtime
        original_delete = pipeline.delete_validation_workspace
        seen_prefixes = []
        upload_calls = []
        status_events = []
        try:
            def fake_create(*args, **kwargs):
                seen_prefixes.append(kwargs.get("name_prefix", ""))
                return {
                    "status": "created",
                    "workspace_slug": "chunk-survival-test-001",
                    "workspace_name": "Chunk Survival Test 001",
                }

            pipeline.create_validation_workspace = fake_create
            def fake_upload(*args, **kwargs):
                upload_calls.append(kwargs)
                kwargs["status_callback"]("AnythingLLM batch 1 accepted", {"batch": 1})
                return {"status": "complete", "uploaded": 1, "locations": []}

            pipeline.maybe_upload_to_anythingllm = fake_upload
            pipeline.verify_anythingllm_post_upload = lambda *args, **kwargs: {
                "status": "pass",
                "classification": "native_metadata_llm_visible",
                "message": "ok",
            }
            pipeline.validate_anythingllm_native_runtime = lambda *args, **kwargs: {
                "status": "pass",
            }
            pipeline.delete_validation_workspace = lambda *args, **kwargs: {
                "status": "deleted", "error": ""
            }
            result = pipeline.run_temporary_workspace_validation(
                "http://127.0.0.1:3001",
                "api-key",
                PROJECT_ROOT,
                "abc123",
                [{"textContent": "x", "metadata": {"chunkSource": "segment://1"}}],
                status_callback=lambda message, detail: status_events.append((message, detail)),
            )
        finally:
            pipeline.create_validation_workspace = original_create
            pipeline.maybe_upload_to_anythingllm = original_upload
            pipeline.verify_anythingllm_post_upload = original_post
            pipeline.validate_anythingllm_native_runtime = original_runtime
            pipeline.delete_validation_workspace = original_delete
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["cleanup_policy"], "cleanup_always")
        self.assertEqual(result["retention_status"], "cleaned_up")
        self.assertEqual(seen_prefixes, ["Chunk Survival Validation abc123"])
        self.assertEqual(upload_calls[0]["folder_name"], "custom-documents/chunk-survival-test-001-docs")
        self.assertEqual(
            upload_calls[0]["embedding_batch_size"],
            pipeline.ANYTHINGLLM_VALIDATION_EMBEDDING_UPDATE_BATCH_SIZE,
        )
        self.assertEqual(upload_calls[0]["embedding_batch_size"], 3)
        self.assertTrue(callable(upload_calls[0]["status_callback"]))
        self.assertIn(("AnythingLLM batch 1 accepted", {"batch": 1}), status_events)

    def test_run_temporary_workspace_validation_can_cleanup_after_success(self):
        original_create = pipeline.create_validation_workspace
        original_upload = pipeline.maybe_upload_to_anythingllm
        original_post = pipeline.verify_anythingllm_post_upload
        original_runtime = pipeline.validate_anythingllm_native_runtime
        original_delete = pipeline.delete_validation_workspace
        try:
            pipeline.create_validation_workspace = lambda *args, **kwargs: {
                "status": "created", "workspace_slug": "cleanup-test", "workspace_name": "Cleanup Test"
            }
            pipeline.maybe_upload_to_anythingllm = lambda *args, **kwargs: {
                "status": "complete", "uploaded": 1, "locations": [], "document_folder_path": "C:/managed-folder"
            }
            pipeline.verify_anythingllm_post_upload = lambda *args, **kwargs: {"status": "pass"}
            pipeline.validate_anythingllm_native_runtime = lambda *args, **kwargs: {"status": "pass"}
            pipeline.delete_validation_workspace = lambda *args, **kwargs: {"status": "deleted", "error": ""}
            result = pipeline.run_temporary_workspace_validation(
                "http://127.0.0.1:3001",
                "api-key",
                PROJECT_ROOT,
                "abc123",
                [{"textContent": "x", "metadata": {"chunkSource": "segment://1"}}],
                cleanup_policy="cleanup_on_success",
            )
        finally:
            pipeline.create_validation_workspace = original_create
            pipeline.maybe_upload_to_anythingllm = original_upload
            pipeline.verify_anythingllm_post_upload = original_post
            pipeline.validate_anythingllm_native_runtime = original_runtime
            pipeline.delete_validation_workspace = original_delete

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["retention_status"], "cleaned_up")
        self.assertEqual(result["cleanup_result"]["status"], "deleted")

    def test_temporary_workspace_validation_does_not_mask_partial_embedding_as_complete(self):
        original_create = pipeline.create_validation_workspace
        original_upload = pipeline.maybe_upload_to_anythingllm
        original_post = pipeline.verify_anythingllm_post_upload
        original_runtime = pipeline.validate_anythingllm_native_runtime
        original_delete = pipeline.delete_validation_workspace
        try:
            pipeline.create_validation_workspace = lambda *args, **kwargs: {
                "status": "created", "workspace_slug": "partial-test", "workspace_name": "Partial Test"
            }
            pipeline.maybe_upload_to_anythingllm = lambda *args, **kwargs: {
                "status": "error",
                "uploaded": 92,
                "embedded": 5,
                "locations": [],
                "errors": [{"endpoint": "update-embeddings", "error": "timed out"}],
            }
            pipeline.verify_anythingllm_post_upload = lambda *args, **kwargs: {
                "status": "partial_vector_coverage"
            }
            pipeline.validate_anythingllm_native_runtime = lambda *args, **kwargs: {"status": "pass"}
            pipeline.delete_validation_workspace = lambda *args, **kwargs: {"status": "deleted", "error": ""}
            result = pipeline.run_temporary_workspace_validation(
                "http://127.0.0.1:3001",
                "api-key",
                PROJECT_ROOT,
                "abc123",
                [{"textContent": "x", "metadata": {"chunkSource": "segment://1"}}],
            )
        finally:
            pipeline.create_validation_workspace = original_create
            pipeline.maybe_upload_to_anythingllm = original_upload
            pipeline.verify_anythingllm_post_upload = original_post
            pipeline.validate_anythingllm_native_runtime = original_runtime
            pipeline.delete_validation_workspace = original_delete

        self.assertEqual(result["status"], "upload_failed")
        self.assertEqual(result["error"], "timed out")
        self.assertEqual(result["retention_status"], "cleaned_up")

    def test_temporary_workspace_validation_waits_for_coverage_and_skips_runtime_when_incomplete(self):
        original_create = pipeline.create_validation_workspace
        original_upload = pipeline.maybe_upload_to_anythingllm
        original_poll = pipeline.poll_post_upload
        original_runtime = pipeline.validate_anythingllm_native_runtime
        original_delete = pipeline.delete_validation_workspace
        runtime_calls = []
        try:
            pipeline.create_validation_workspace = lambda *args, **kwargs: {
                "status": "created", "workspace_slug": "coverage-test", "workspace_name": "Coverage Test"
            }
            pipeline.maybe_upload_to_anythingllm = lambda *args, **kwargs: {
                "status": "complete", "uploaded": 2, "embedded": 2, "locations": []
            }
            pipeline.poll_post_upload = lambda *args, **kwargs: SimpleNamespace(
                final_evidence={"status": "partial_vector_coverage", "message": "still indexing"},
                attempts=3,
                elapsed_seconds=9.0,
                observer_failures=[],
            )
            pipeline.validate_anythingllm_native_runtime = lambda *args, **kwargs: runtime_calls.append(True)
            pipeline.delete_validation_workspace = lambda *args, **kwargs: {"status": "deleted", "error": ""}
            result = pipeline.run_temporary_workspace_validation(
                "http://127.0.0.1:3001",
                "api-key",
                PROJECT_ROOT,
                "abc123",
                [{"textContent": "x", "metadata": {"chunkSource": "segment://1"}}],
            )
        finally:
            pipeline.create_validation_workspace = original_create
            pipeline.maybe_upload_to_anythingllm = original_upload
            pipeline.poll_post_upload = original_poll
            pipeline.validate_anythingllm_native_runtime = original_runtime
            pipeline.delete_validation_workspace = original_delete

        self.assertEqual(result["status"], "post_upload_failed")
        self.assertEqual(result["post_upload_report"]["polling_attempts"], 3)
        self.assertEqual(result["runtime_validation_status"], "not_run_post_upload_incomplete")
        self.assertEqual(runtime_calls, [])

    def test_temporary_workspace_validation_forwards_cached_embedder_probe(self):
        original_create = pipeline.create_validation_workspace
        original_upload = pipeline.maybe_upload_to_anythingllm
        original_post = pipeline.verify_anythingllm_post_upload
        original_runtime = pipeline.validate_anythingllm_native_runtime
        original_delete = pipeline.delete_validation_workspace
        captured = {}
        try:
            pipeline.create_validation_workspace = lambda *args, **kwargs: {
                "status": "created", "workspace_slug": "cached-probe-test", "workspace_name": "Cached Probe Test"
            }
            pipeline.maybe_upload_to_anythingllm = lambda *args, **kwargs: {
                "status": "complete", "uploaded": 1, "locations": []
            }
            pipeline.verify_anythingllm_post_upload = lambda *args, **kwargs: {"status": "pass"}

            def fake_runtime(*args, **kwargs):
                captured["embedder_probe_override"] = kwargs.get("embedder_probe_override")
                return {"status": "pass", "embedder_probe": kwargs.get("embedder_probe_override") or {}}

            pipeline.validate_anythingllm_native_runtime = fake_runtime
            pipeline.delete_validation_workspace = lambda *args, **kwargs: {"status": "deleted", "error": ""}
            result = pipeline.run_temporary_workspace_validation(
                "http://127.0.0.1:3001",
                "api-key",
                PROJECT_ROOT,
                "abc123",
                [{"textContent": "x", "metadata": {"chunkSource": "segment://1"}}],
                embedder_probe_override={"status": "pass", "dimension": 4096},
            )
        finally:
            pipeline.create_validation_workspace = original_create
            pipeline.maybe_upload_to_anythingllm = original_upload
            pipeline.verify_anythingllm_post_upload = original_post
            pipeline.validate_anythingllm_native_runtime = original_runtime
            pipeline.delete_validation_workspace = original_delete

        self.assertEqual(result["runtime_validation_status"], "pass")
        self.assertEqual(captured["embedder_probe_override"]["dimension"], 4096)

    def test_delete_validation_workspace_removes_only_its_managed_document_folder(self):
        original_delete_json = pipeline.delete_json
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Path(tmpdir)
                managed_folder = storage / "documents" / "custom-documents" / "chunk-survival-test-001-docs"
                managed_folder.mkdir(parents=True)
                (managed_folder / "payload.json").write_text("{}", encoding="utf-8")
                pipeline.delete_json = lambda *args, **kwargs: (200, "{}")
                result = pipeline.delete_validation_workspace(
                    "http://127.0.0.1:3001",
                    "chunk-survival-test-001",
                    api_key="provided-key",  # pragma: allowlist secret -- synthetic authentication fixture
                    storage_dir=storage,
                    document_folder_path=managed_folder,
                )
        finally:
            pipeline.delete_json = original_delete_json

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["document_folder_cleanup"]["status"], "deleted")
        self.assertFalse(managed_folder.exists())

    def test_validation_document_cleanup_rejects_non_managed_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            other_folder = storage / "documents" / "someone-elses-documents"
            other_folder.mkdir(parents=True)
            result = pipeline.cleanup_validation_workspace_documents(
                "chunk-survival-test-001",
                storage_dir=storage,
                document_folder_path=other_folder,
            )
            self.assertTrue(other_folder.exists())

        self.assertEqual(result["status"], "rejected_unmanaged_path")

    def test_validation_document_cleanup_accepts_specific_pdf_subfolder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            pdf_folder = storage / "documents" / "custom-documents" / "Example-PDF-12345678"
            pdf_folder.mkdir(parents=True)
            (pdf_folder / "segment.json").write_text("{}", encoding="utf-8")
            result = pipeline.cleanup_validation_workspace_documents(
                "temporary-workspace",
                storage_dir=storage,
                document_folder_path=pdf_folder,
            )

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(pdf_folder.exists())

    def test_validation_document_cleanup_rejects_custom_documents_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            root = storage / "documents" / "custom-documents"
            root.mkdir(parents=True)
            result = pipeline.cleanup_validation_workspace_documents(
                "temporary-workspace",
                storage_dir=storage,
                document_folder_path=root,
            )
            still_exists = root.exists()

        self.assertEqual(result["status"], "rejected_unmanaged_path")
        self.assertTrue(still_exists)

    def test_relocate_uploaded_document_moves_out_of_custom_documents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            source = storage / "documents" / "custom-documents" / "sample-author-p15-s0001.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("hello", encoding="utf-8")

            relocated, error = pipeline.relocate_uploaded_document(
                storage,
                "custom-documents/sample-author-p15-s0001.txt",
                "page-bounded-subchunking-test-A",
            )

            self.assertEqual(error, "")
            self.assertEqual(relocated, "page-bounded-subchunking-test-A/sample-author-p15-s0001.txt")
            self.assertFalse(source.exists())
            self.assertTrue((storage / "documents" / relocated).exists())

    def test_relocate_uploaded_document_returns_relative_location_for_absolute_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            source = storage / "documents" / "custom-documents" / "control.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("hello", encoding="utf-8")

            relocated, error = pipeline.relocate_uploaded_document(
                storage,
                str(source),
                "internal-chunk-control",
            )

            self.assertEqual(error, "")
            self.assertEqual(relocated, "internal-chunk-control/control.txt")
            self.assertFalse(Path(relocated).is_absolute())
            self.assertTrue((storage / "documents" / relocated).exists())

    def test_relocate_uploaded_document_normalizes_root_absolute_path_without_moving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            source = storage / "documents" / "custom-documents" / "drawer-visible.json"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("hello", encoding="utf-8")

            relocated, error = pipeline.relocate_uploaded_document(
                storage,
                str(source),
                "custom-documents",
            )

            self.assertEqual(error, "")
            self.assertEqual(relocated, "custom-documents/drawer-visible.json")
            self.assertFalse(Path(relocated).is_absolute())
            self.assertTrue(source.exists())

    def test_relocate_uploaded_document_keeps_workspace_and_document_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            source = storage / "documents" / "custom-documents" / "segment.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("hello", encoding="utf-8")

            relocated, error = pipeline.relocate_uploaded_document(
                storage,
                "custom-documents/segment.txt",
                "custom-documents/example-pdf-1234abcd",
            )

            self.assertEqual(error, "")
            self.assertEqual(
                relocated,
                "custom-documents/example-pdf-1234abcd/segment.txt",
            )
            self.assertTrue((storage / "documents" / relocated).exists())

    def test_document_title_folder_name_uses_title_and_hash(self):
        folder_name = pipeline.document_title_folder_name(
            "Sample Author Matt - Not quite white / white trash",
            "189891f31edf2c536afd9971cec08af7c7c0e5a20181e06aecc616196e772d7a",  # pragma: allowlist secret -- fixed SHA-256 fixture
        )
        self.assertIn("189891f3", folder_name)
        self.assertNotIn("/", folder_name)
        self.assertTrue(folder_name.startswith("Sample-Author-Matt-Not-quite-white-white-trash"))

    def test_managed_upload_folder_uses_custom_documents_when_document_folders_are_disabled(self):
        folder_name = pipeline.managed_anythingllm_upload_folder_name(
            workspace_slug="pdf-workspace-3",
            explicit_folder_name="",
        )

        self.assertEqual(folder_name, "custom-documents")

    def test_managed_upload_folder_nests_document_under_custom_documents_by_default(self):
        folder_name = pipeline.managed_anythingllm_upload_folder_name(
            workspace_slug="pdf-workspace-3",
            source_title="Example PDF",
            source_sha="12345678abcdef",  # pragma: allowlist secret -- short deterministic SHA fixture
            create_document_folders=True,
        )

        self.assertEqual(
            folder_name,
            "custom-documents/Example-PDF-12345678",  # pragma: allowlist secret -- derived fixture path
        )

    def test_managed_upload_folder_preserves_explicit_manual_name(self):
        folder_name = pipeline.managed_anythingllm_upload_folder_name(
            workspace_slug="pdf-workspace-3",
            explicit_folder_name="Manual PDF Folder",
        )

        self.assertEqual(folder_name, "Manual PDF Folder")

    def test_manual_upload_root_still_nests_each_document_when_enabled(self):
        folder_name = pipeline.managed_anythingllm_upload_folder_name(
            workspace_slug="pdf-workspace-3",
            source_title="Example PDF",
            source_sha="12345678abcdef",  # pragma: allowlist secret -- short deterministic SHA fixture
            create_document_folders=True,
            explicit_folder_name="Manual PDF Folder",
        )

        self.assertEqual(folder_name, "custom-documents/Manual PDF Folder/Example-PDF-12345678")

    def test_upload_plan_rows_to_expected_payloads_reads_text_file_contents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            text_file = Path(tmpdir) / "segment.txt"
            text_file.write_text("probe text", encoding="utf-8")
            payloads = pipeline.upload_plan_rows_to_expected_payloads(
                [
                    {
                        "filename": "segment.txt",
                        "title": "segment",
                        "chunkSource": "segment://1",
                        "text_file": str(text_file),
                    }
                ]
            )
            self.assertEqual(payloads[0]["textContent"], "probe text")

    def test_provenance_manifest_records_actual_pymupdf4llm_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pipeline.write_provenance_review_manifest(
                Path(tmpdir),
                {},
                {},
                {
                    "backend": "pymupdf4llm",
                    "quality": {},
                    "segments": [],
                    "pymupdf4llm_execution": {
                        "requested_workers": 4,
                        "actual_workers": 1,
                        "mode": "sequential_after_parallel_fallback",
                        "fallback_reason": "RuntimeError: worker failure",
                    },
                },
                {"used": True, "evidence": "test"},
                [],
                [],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            manifest["selected_extraction"]["pymupdf4llm_execution"]["mode"],
            "sequential_after_parallel_fallback",
        )
        self.assertEqual(
            manifest["selected_extraction"]["pymupdf4llm_execution"]["actual_workers"],
            1,
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIn("pymupdf4llm_ocr_page_workers", manifest["selected_extraction"])
        self.assertEqual(
            manifest["selected_extraction"]["ocr_page_workers_scope"],
            "global_process_isolated_setting",
        )

    def test_next_validation_workspace_prefix_uses_alphabetic_series(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    """
                    create table workspaces(
                        id integer primary key,
                        name text,
                        slug text,
                        chatProvider text,
                        chatModel text,
                        topN integer,
                        similarityThreshold real,
                        vectorSearchMode text,
                        chatMode text
                    )
                    """
                )
                con.execute(
                    "insert into workspaces(id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode) values (1,'A Chunk Survival Validation abc 2026-07-05 12:00:00','a-chunk-survival','openrouter','deepseek-v4-pro',8,0.3,'default','query')"
                )
                con.execute(
                    "insert into workspaces(id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode) values (2,'B Chunk Survival Validation def 2026-07-05 12:05:00','b-chunk-survival','openrouter','deepseek-v4-pro',8,0.3,'default','query')"
                )
                con.commit()
            finally:
                con.close()

            prefix = pipeline.next_validation_workspace_prefix(storage, "Chunk Survival Validation feedface")
            self.assertTrue(prefix.startswith("C Chunk Survival Validation feedface"))

    def test_automatic_process_button_state_tracks_input_and_processed_state(self):
        import rag_pdf_gradio_app as app

        disabled = app.automatic_process_button_state([], [], processed=False)
        ready = app.automatic_process_button_state(["example.pdf"], [], processed=False)
        done = app.automatic_process_button_state(["example.pdf"], [], processed=True)

        self.assertFalse(disabled["interactive"])
        self.assertEqual(disabled["value"], "Confirm and start processing")
        self.assertTrue(ready["interactive"])
        self.assertEqual(ready["value"], "Confirm and start processing")
        self.assertEqual(ready["variant"], "primary")
        self.assertEqual(done["value"], "Processing successful ✓")
        self.assertEqual(done["variant"], "huggingface")
        self.assertFalse(done["interactive"])

    def test_pending_selection_shows_inert_action_controls_before_metadata_finishes(self):
        import rag_pdf_gradio_app as app

        confirm, cancel = app.automatic_selection_pending_action_states(["example.pdf"], [])

        self.assertTrue(confirm["visible"])
        self.assertFalse(confirm["interactive"])
        self.assertEqual(confirm["value"], "Confirm and start processing")
        self.assertTrue(cancel["visible"])
        self.assertFalse(cancel["interactive"])

    def test_advanced_backend_menu_includes_automatic_and_explicit_tesseract_ocr(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(app.ADVANCED_BACKEND_CHOICES[0], "Automatic")
        self.assertIn("PyMuPDF", app.ADVANCED_BACKEND_CHOICES)
        self.assertIn("PyMuPDF4LLM", app.ADVANCED_BACKEND_CHOICES)
        self.assertIn("Unstructured", app.ADVANCED_BACKEND_CHOICES)
        self.assertIn("Unstructured OCR (Tesseract)", app.ADVANCED_BACKEND_CHOICES)

    def test_advanced_progress_distinguishes_candidate_evaluation_from_selection(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(
            app.advanced_diagnostic_progress_stage("Extracting and evaluating with unstructured (fast)"),
            "Testing fallback candidate: Unstructured (not selected yet)",
        )
        self.assertEqual(
            app.advanced_diagnostic_progress_stage("Extracting and evaluating with pymupdf"),
            "Evaluating extraction candidate: pymupdf",
        )

    def test_advanced_diagnostics_uses_the_shared_automatic_backend_contract(self):
        import rag_pdf_gradio_app as app

        automatic = app.advanced_diagnostic_backend_settings("Automatic", "auto")
        forced_ocr = app.advanced_diagnostic_backend_settings(
            "Unstructured OCR (Tesseract)", "fast"
        )
        forced_layout = app.advanced_diagnostic_backend_settings("Unstructured", "ocr_only")

        self.assertEqual(automatic[:2], ("automatic", "auto"))
        self.assertIn("shared with the Automatic tab", automatic[2])
        self.assertEqual(forced_ocr[:2], ("unstructured", "hi_res"))
        self.assertEqual(forced_layout[:2], ("unstructured", "ocr_only"))

    def test_advanced_diagnostics_picker_uses_readable_pdf_validation(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            non_pdf = Path(tmpdir) / "not-a-pdf.txt"
            non_pdf.write_text("not a PDF", encoding="utf-8")
        updates = app.advanced_diagnostic_pdf_selection_update(str(non_pdf))

        self.assertEqual(len(updates), 5)
        self.assertTrue(updates[-1]["visible"])
        self.assertIn("needs attention", updates[-1]["value"])

    def test_advanced_diagnostics_generation_stays_disabled_without_a_readable_pdf(self):
        import rag_pdf_gradio_app as app

        no_selection = app.advanced_diagnostic_action_state("")
        invalid = app.advanced_diagnostic_action_state("C:/does-not-exist.pdf")

        self.assertFalse(no_selection["interactive"])
        self.assertFalse(invalid["interactive"])

    def test_advanced_diagnostic_status_reports_start_and_selected_backend(self):
        import rag_pdf_gradio_app as app

        started = app.advanced_diagnostic_running_status()
        completed = app.advanced_diagnostic_completion_status(
            {"readiness_status": "ready", "selected_backend": "pymupdf"}
        )

        self.assertTrue(started[0]["visible"])
        self.assertIn("started", started[0]["value"])
        self.assertEqual(started[1]["value"], "Diagnostic extraction is running…")
        self.assertTrue(completed["visible"])
        self.assertIn("pymupdf", completed["value"])

    def test_end_section_additions_keep_the_shared_default_headings(self):
        import rag_pdf_gradio_app as app

        headings = app.merged_end_section_headings("Appendix\nFilmography\nreferences")

        self.assertIn("References", headings)
        self.assertIn("Appendix", headings)
        self.assertIn("Filmography", headings)
        self.assertEqual(sum(item.casefold() == "references" for item in headings), 1)

    def test_selected_pdf_shows_preparation_status_until_confirmation_is_ready(self):
        import rag_pdf_gradio_app as app

        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        updates = app.reset_automatic_run_presentation(["example.pdf"], [])

        self.assertEqual(app.LIVE_AUTOMATIC_RUN_STATUS["state"], "preparing")
        self.assertIn("Finishing preparation", updates[0]["value"])
        self.assertNotIn("Ready — Confirm", updates[0]["value"])
        app.LIVE_AUTOMATIC_RUN_STATUS = {}

    def test_automatic_confirmation_uses_declared_chunk_overlap_field(self):
        """The legacy validator remains usable without restoring the review UI."""
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "confirmation.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Confirmation test document.")
            document.save(pdf_path)
            document.close()

            settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update(
                {
                    "pdf_files": [str(pdf_path)],
                    "folder_pdf_files": [],
                    "document_label": "Confirmation test",
                    "document_author": "",
                    "document_short_label": "Confirmation",
                    "use_file_title_fallback": True,
                    "mode": app.MODE_NATIVE_UPLOAD_LABEL,
                    "workspace_slug": app.NEW_DOCUMENT_WORKSPACE_VALUE,
                    "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
                    "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
                    "anythingllm_chunk_size": 768,
                    "anythingllm_chunk_overlap": 128,
                }
            )
            (
                confirmation,
                rendered,
                timing,
                confirmed_settings,
                confirm_button,
                review_button,
                cancel_button,
                failure_banner,
            ) = (
                app.prepare_automatic_confirmation(
                    *(settings[field] for field in app.AUTOMATIC_RUN_FIELDS)
                )
            )

        self.assertNotIn("visible", confirmation)
        self.assertIn(app.MODE_NATIVE_UPLOAD_LABEL, rendered)
        self.assertIn("New workspace for this document", rendered)
        self.assertIn(app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL, rendered)
        self.assertIn(app.SEGMENT_PAGE_LIMIT_LABEL, rendered)
        self.assertIn("768 chunk / 128 overlap", rendered)
        self.assertNotIn("PDF pages", rendered)
        self.assertNotIn("Estimated duration", rendered)
        self.assertIn("Est:", timing)
        self.assertEqual(confirmed_settings["anythingllm_chunk_overlap"], 128)
        self.assertNotIn("visible", confirm_button)
        self.assertTrue(confirm_button["interactive"])
        self.assertFalse(review_button["visible"])
        self.assertFalse(review_button["interactive"])
        self.assertNotIn("visible", cancel_button)
        self.assertTrue(cancel_button["interactive"])
        self.assertFalse(failure_banner["visible"])

    def test_confirmation_validation_keeps_only_the_normal_action_row_available(self):
        import rag_pdf_gradio_app as app

        settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
        settings.update({"pdf_files": [], "folder_pdf_files": []})
        _, _, _, confirmed_settings, confirm_button, review_button, cancel_button, failure_banner = (
            app.prepare_automatic_confirmation(*(settings[field] for field in app.AUTOMATIC_RUN_FIELDS))
        )

        self.assertEqual(confirmed_settings, {})
        self.assertNotIn("visible", confirm_button)
        self.assertFalse(confirm_button["interactive"])
        self.assertFalse(review_button["visible"])
        self.assertFalse(review_button["interactive"])
        self.assertNotIn("visible", cancel_button)
        self.assertFalse(cancel_button["interactive"])
        self.assertFalse(failure_banner["visible"])

    def test_legacy_confirmation_callback_cannot_clear_an_active_run(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "running", "run_root": "active-run"}
            updates = app.prepare_automatic_confirmation(*([None] * len(app.AUTOMATIC_RUN_FIELDS)))
            status = dict(app.LIVE_AUTOMATIC_RUN_STATUS)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(status["state"], "running")
        self.assertEqual(len(updates), 8)
        self.assertTrue(all(update.get("__type__") == "update" for update in updates))

    def test_automatic_error_closes_a_live_run_before_the_observer_can_repaint(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "state": "running",
                    "run_root": tmpdir,
                    "expected_seconds": 90,
                    "confirmed_fraction": .4,
                }
                outputs = app.automatic_error_outputs("AUTO-TEST-FAIL", "Preparation failed", ["fixture failure"])
                status = dict(app.LIVE_AUTOMATIC_RUN_STATUS)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["phase"], "Run needs attention")
        self.assertIn("AUTO-TEST-FAIL", status["details"])
        self.assertIn("AUTO-TEST-FAIL", outputs[0]["value"])

    def test_confirmed_run_reports_missing_or_invalid_state_without_silence(self):
        import rag_pdf_gradio_app as app

        result = app.run_automatic_from_confirmation({})

        self.assertEqual(len(result), 9)
        self.assertTrue(result[0]["visible"])
        self.assertIn("AUTO-CONFIRM-001", result[0]["value"])
        self.assertTrue(result[8]["visible"])
        self.assertIn("AUTO-CONFIRM-001", result[8]["value"])

    def test_confirmed_run_converts_unexpected_dispatch_error_into_visible_report(self):
        import rag_pdf_gradio_app as app

        original_run = app.run_automatic
        try:
            app.run_automatic = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("test dispatch failure"))
            settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update({
                "files": ["C:/test.pdf"],
                "mode": app.MODE_LOCAL_ONLY_LABEL,
                "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            })
            result = app.run_automatic_from_confirmation(settings)
        finally:
            app.run_automatic = original_run

        self.assertEqual(len(result), 9)
        self.assertIn("AUTO-RUN-UNEXPECTED-001", result[0]["value"])
        self.assertTrue(result[8]["visible"])
        self.assertIn("test dispatch failure", result[0]["value"])

    def test_confirmed_run_rejects_malformed_backend_result_contract(self):
        import rag_pdf_gradio_app as app

        original_run = app.run_automatic
        try:
            app.run_automatic = lambda *args, **kwargs: ("only one output",)
            settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update({"files": ["C:/test.pdf"], "mode": app.MODE_LOCAL_ONLY_LABEL})
            result = app.run_automatic_from_confirmation(settings)
        finally:
            app.run_automatic = original_run

        self.assertIn("AUTO-RUN-UNEXPECTED-001", result[0]["value"])
        self.assertIn("invalid UI result contract", result[0]["value"])
        self.assertTrue(result[8]["visible"])

    def test_confirmed_run_validation_exception_still_renders_failure_contract(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validated_automatic_run_settings
        try:
            app.validated_automatic_run_settings = lambda values: (_ for _ in ()).throw(ValueError("malformed UI payload"))
            result = app.run_automatic_from_confirmation(*([None] * len(app.AUTOMATIC_RUN_FIELDS)))
        finally:
            app.validated_automatic_run_settings = original_validate

        self.assertEqual(len(result), 9)
        self.assertIn("AUTO-RUN-UNEXPECTED-001", result[0]["value"])
        self.assertIn("malformed UI payload", result[0]["value"])
        self.assertTrue(result[8]["visible"])

    def test_confirmed_run_surfaces_handled_pipeline_error_outside_output_accordion(self):
        import rag_pdf_gradio_app as app

        original_run = app.run_automatic
        try:
            error_summary = app.run_summary_html(
                app.app_error_report("AUTO-TEST-RESULT", "Prepared failure", ["test result error"])
            )
            app.run_automatic = lambda *args, **kwargs: (
                app.gr.update(value=error_summary, visible=True),
                app.gr.update(value=[], visible=False),
                "artifacts",
                [],
                app.gr.update(),
                "readiness",
                "timer",
            )
            settings = {field: None for field in app.AUTOMATIC_RUN_FIELDS}
            settings.update({"files": ["C:/test.pdf"], "mode": app.MODE_LOCAL_ONLY_LABEL})
            result = app.run_automatic_from_confirmation(settings)
        finally:
            app.run_automatic = original_run

        self.assertEqual(len(result), 9)
        self.assertTrue(result[8]["visible"])
        self.assertIn("AUTO-RUN-RESULT-001", result[8]["value"])

    def test_confirm_stream_acknowledges_before_the_long_pipeline_returns(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validated_automatic_run_settings
        original_run = app.run_automatic_from_confirmation
        settings = {"expected_seconds": 194, "files": ["C:/test.pdf"], "mode": app.MODE_LOCAL_ONLY_LABEL}
        terminal = (
            {"value": "completed"},
            [],
            "artifacts",
            [],
            {"value": "Processing successful ✓", "variant": "huggingface"},
            "readiness",
            "timer",
            {"value": "workspace"},
            {"visible": False},
        )
        try:
            app.validated_automatic_run_settings = lambda values: (settings, None, [], True)
            app.run_automatic_from_confirmation = lambda *args, **kwargs: terminal
            stream = app.run_automatic_from_confirmation_stream(*([None] * len(app.AUTOMATIC_RUN_FIELDS)))
            preprocessing = next(stream)
            started = next(stream)
            observed_state = dict(app.LIVE_AUTOMATIC_RUN_STATUS)
            completed = next(stream)
        finally:
            app.validated_automatic_run_settings = original_validate
            app.run_automatic_from_confirmation = original_run

        self.assertEqual(len(preprocessing), 12)
        self.assertIn(">preparing<", preprocessing[0]["value"])
        self.assertEqual(preprocessing[9]["value"], "Preparing…")
        self.assertFalse(preprocessing[10]["interactive"])
        self.assertFalse(preprocessing[11]["visible"])
        self.assertEqual(len(started), 12)
        self.assertIn(">running<", started[0]["value"])
        self.assertIn('data-run-state="running"', started[6])
        self.assertEqual(started[9]["value"], "Processing started")
        self.assertTrue(started[10]["interactive"])
        self.assertFalse(started[11]["visible"])
        self.assertEqual(observed_state["state"], "preparing")
        self.assertEqual(observed_state["phase"], "Pre-processing complete — starting pipeline")
        self.assertEqual(len(completed), 12)
        self.assertEqual(completed[9]["value"], "Processing successful ✓")
        self.assertEqual(completed[9]["variant"], "huggingface")
        self.assertFalse(completed[10]["interactive"])
        self.assertTrue(completed[11]["visible"])
        self.assertTrue(completed[11]["interactive"])

    def test_run_action_rows_keep_confirmation_controls_mounted(self):
        import rag_pdf_gradio_app as app

        element_ids = {
            component.get("props", {}).get("elem_id")
            for component in app.demo.config["components"]
        }
        self.assertIn("automatic-actions", element_ids)
        self.assertNotIn("automatic-normal-actions", element_ids)
        self.assertNotIn("automatic-confirm-actions", element_ids)
        self.assertIn("automatic-run-failure", element_ids)
        normal, confirmation, cancel = app.automatic_action_row_updates()
        self.assertNotIn("visible", normal)
        self.assertFalse(confirmation["interactive"])
        self.assertFalse(cancel["interactive"])

        component_ids = {
            component.get("props", {}).get("elem_id"): component["id"]
            for component in app.demo.config["components"]
            if component.get("props", {}).get("elem_id")
        }
        confirm_id = component_ids["confirm-automatic-run-button"]
        cancel_id = component_ids["cancel-automatic-run-button"]
        review_id = component_ids["automatic-process-button"]
        components_by_id = {component["id"]: component for component in app.demo.config["components"]}
        # Confirm stays available as the empty-state call to action; Cancel is
        # intentionally absent until at least one PDF has been selected.
        self.assertIsNot(components_by_id[confirm_id].get("props", {}).get("visible"), False)
        self.assertIs(components_by_id[cancel_id].get("props", {}).get("visible"), False)
        confirm_dependencies = [
            dependency
            for dependency in app.demo.config["dependencies"]
            if (confirm_id, "click") in dependency.get("targets", [])
        ]
        review_dependencies = [
            dependency
            for dependency in app.demo.config["dependencies"]
            if (review_id, "click") in dependency.get("targets", [])
        ]
        self.assertEqual(len(confirm_dependencies), 1)
        self.assertEqual(len(review_dependencies), 0)
        self.assertIsNone(confirm_dependencies[0]["js"])
        self.assertTrue(confirm_dependencies[0]["backend_fn"])
        # Confirm submits concrete current controls rather than State-only
        # review data, so the direct action cannot become a visible no-op.
        self.assertGreater(len(confirm_dependencies[0]["inputs"]), 1)
        self.assertEqual(len(confirm_dependencies[0]["outputs"]), 12)

    def test_run_control_containers_never_use_dynamic_visibility(self):
        """A Gradio container visibility transition previously stacked action bars."""
        import rag_pdf_gradio_app as app

        idle = app.reset_automatic_run_presentation()
        self.assertNotIn("visible", idle[4])
        self.assertNotIn("visible", idle[7])

        cancelled = app.cancel_or_reset_automatic_run()
        self.assertNotIn("visible", cancelled[4])

        action_row, _confirm, _cancel = app.automatic_action_row_updates()
        self.assertNotIn("visible", action_row)

    def test_pdf_selection_has_one_direct_owner_without_duplicate_run_warning_callbacks(self):
        import rag_pdf_gradio_app as app

        self.assertFalse(any(
            "repeat_run_history_notice" in str(dependency.get("api_name") or "")
            for dependency in app.demo.config["dependencies"]
        ))

        pdf_component = next(
            component for component in app.demo.config["components"]
            if component.get("type") == "file" and component.get("props", {}).get("label") == "PDF files"
        )
        selection_dependencies = [
            dependency for dependency in app.demo.config["dependencies"]
            if (pdf_component["id"], "change") in dependency.get("targets", [])
        ]
        # A selection has exactly one direct owner. Its narrow chained helpers
        # refresh defaults, ETA, metadata, workspace name, and Review
        # in order; parallel direct listeners previously caused visible stale
        # values before that chain caught up.
        self.assertEqual(len(selection_dependencies), 1)
        self.assertEqual(selection_dependencies[0].get("api_name"), "merge_uploaded_pdfs_into_folder_batch")

    def test_run_timer_estimate_refreshes_for_mode_and_upload_scope(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {}
            with tempfile.TemporaryDirectory() as tmpdir:
                pdf_path = Path(tmpdir) / "timer.pdf"
                document = fitz.open()
                document.new_page().insert_text((72, 72), "Timer test document.")
                document.save(pdf_path)
                document.close()
                local_timer = app.refresh_automatic_run_estimate(
                    [str(pdf_path)], [], app.MODE_LOCAL_ONLY_LABEL, app.NATIVE_UPLOAD_SCOPE_ALL_LABEL
                )
                upload_timer = app.refresh_automatic_run_estimate(
                    [str(pdf_path)], [], app.MODE_NATIVE_UPLOAD_LABEL, app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL
                )
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        local_seconds = int(re.search(r'data-expected-seconds="(\d+)"', local_timer).group(1))
        upload_seconds = int(re.search(r'data-expected-seconds="(\d+)"', upload_timer).group(1))
        # Local-only preparation is not subject to the native-upload floor;
        # a tiny ordinary text PDF can now receive the tested 8-second
        # minimum while a true upload retains a conservative larger budget.
        self.assertGreaterEqual(local_seconds, 8)
        self.assertGreater(upload_seconds, local_seconds)
        self.assertIn("Est:", local_timer)
        self.assertIn("Est:", upload_timer)

    def test_automatic_process_button_stays_disabled_for_folder_without_pdfs(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "notes.txt"
            note.write_text("not a pdf", encoding="utf-8")
            disabled = app.automatic_process_button_state([], [str(note)], processed=False)

        self.assertFalse(disabled["interactive"])
        self.assertEqual(disabled["value"], "Confirm and start processing")

    def test_batch_folder_status_warns_when_no_pdfs_are_found(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "notes.txt"
            note.write_text("not a pdf", encoding="utf-8")
            rendered = app.batch_folder_status_html([str(note)])

        self.assertIn("No PDFs found in selected folder", rendered)
        self.assertIn("Ignored 1 non-PDF file", rendered)

    def test_batch_folder_status_reports_mixed_folder_as_ready(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "notes.txt"
            note.write_text("not a pdf", encoding="utf-8")
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            rendered = app.batch_folder_status_html([str(note), str(pdf)])

        self.assertIn("Batch folder ready", rendered)
        self.assertIn("Found 1 readable PDF file", rendered)
        self.assertIn("Ignoring 1 non-PDF file", rendered)

    def test_scan_selected_pdf_directory_keeps_only_pdf_candidates(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        try:
            app.LIVE_AUTOMATIC_RUN_STATUS = {}
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                note = root / "notes.txt"
                note.write_text("not a pdf", encoding="utf-8")
                pdf = root / "paper.pdf"
                pdf.write_bytes(b"%PDF-1.4\n")
                scanned_files, status_html, *_ = app.scan_selected_pdf_directory(str(root), "", "", "", True)
                expected_pdf = str(pdf)
        finally:
            app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(scanned_files, [expected_pdf])
        self.assertIn("Batch folder ready", status_html)
        self.assertIn("Ignoring 1 non-PDF file", status_html)

    def test_folder_picker_recurses_and_streams_a_visible_progress_state(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nested = root / "week-1" / "readings"
            nested.mkdir(parents=True)
            pdf = nested / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nnested fixture\n")

            events = list(app.stream_selected_pdf_directory(str(root), True))

        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(any("Scanning PDF folder" in str(event[4]) for event in events[:-1]))
        selected, manifest, selector, selection_accordion, status, picker_area, button = events[-1]
        self.assertEqual(selected, [str(pdf)])
        self.assertEqual(manifest["selected_pdf_candidates"], [str(pdf)])
        self.assertEqual(manifest["directories_scanned"], 3)
        self.assertEqual(selector["value"], [str(pdf)])
        self.assertTrue(selection_accordion["visible"])
        self.assertIn("Batch folder ready", status["value"])
        self.assertFalse(picker_area["visible"])
        self.assertEqual(button["value"], "Select PDF Folder Here")

    def test_batch_folder_specific_selection_is_reversible(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4\nfirst")
            second.write_bytes(b"%PDF-1.4\nsecond")
            manifest = {
                "root": str(root),
                "pdf_candidates": [str(first), str(second)],
                "selected_pdf_candidates": [str(first), str(second)],
            }

            updated, selected = app.update_batch_folder_selection(manifest, [str(second)])
            self.assertEqual(selected, [str(second)])
            self.assertEqual(updated["pdf_candidates"], [str(first), str(second)])

            result = app.apply_batch_folder_file_selection(updated, [])
            self.assertEqual(result[0], [])
            self.assertEqual(result[1]["pdf_candidates"], [str(first), str(second)])
            self.assertEqual(len(result[2]["choices"]), 2)
            self.assertIn("0 of 2 PDFs selected", result[3]["value"])

    def test_batch_folder_picker_choices_include_page_and_ocr_candidate_counts(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            document = fitz.open()
            document.new_page()
            document.new_page()
            document.save(pdf)
            document.close()

            choices = app.batch_folder_selection_choices({
                "root": str(Path(tmpdir)),
                "pdf_candidates": [str(pdf)],
                "picker_page_details": app.pdf_picker_page_details([str(pdf)]),
            })

        self.assertIn("paper.pdf", choices[0][0])
        self.assertIn("(2 pages, 0 OCR)", choices[0][0])

    def test_long_pdf_picker_scan_is_exact_and_cached_for_confirmation(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "long.pdf"
            document = fitz.open()
            for _ in range(60):
                document.new_page()
            document.save(pdf)
            document.close()

            original_coverage = app.automatic_full_native_text_coverage
            calls = []

            def counted_coverage(path):
                calls.append(str(path))
                return original_coverage(path)

            app.PDF_PICKER_NATIVE_INSPECTION_CACHE.clear()
            app.automatic_full_native_text_coverage = counted_coverage
            try:
                details = app.pdf_picker_page_details([str(pdf)])
                manifest = app.automatic_ocr_preflight_manifest([str(pdf)])
            finally:
                app.automatic_full_native_text_coverage = original_coverage
                app.PDF_PICKER_NATIVE_INSPECTION_CACHE.clear()

        item = details[str(pdf)]
        self.assertEqual(item["pages"], 60)
        self.assertEqual(item["ocr_label"], "0")
        self.assertTrue(item["page_scan_complete"])
        self.assertEqual(manifest["files"][0]["image_backed_low_text_page_count"], 0)
        self.assertEqual(len(calls), 1)

    def test_new_ordinary_picker_pdf_merges_into_existing_folder_batch(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            folder_pdf = root / "folder.pdf"
            direct_pdf = root / "added.pdf"
            for pdf in (folder_pdf, direct_pdf):
                document = fitz.open()
                document.new_page()
                document.save(pdf)
                document.close()

            manifest = {
                "root": str(root),
                "pdf_candidates": [str(folder_pdf)],
                "selected_pdf_candidates": [str(folder_pdf)],
                "picker_page_details": {str(folder_pdf): {"pages": 1, "ocr_label": "0"}},
            }
            result = app.merge_uploaded_pdfs_into_folder_batch([str(direct_pdf)], manifest)

        self.assertEqual(result[0]["value"], [])
        self.assertEqual(result[1], [str(folder_pdf), str(direct_pdf)])
        self.assertEqual(result[2]["selected_pdf_candidates"], [str(folder_pdf), str(direct_pdf)])
        choices = result[3]["choices"]
        self.assertIn("added.pdf", choices[1][0])
        self.assertIn("(1 pages, 0 OCR)", choices[1][0])

    def test_recursive_folder_scan_stops_at_interactive_safety_limit(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for index in range(3):
                (root / f"paper-{index}.pdf").write_bytes(b"%PDF-1.4\nfixture\n")

            scan = app.recursive_pdf_folder_scan(str(root), max_documents=2)

        self.assertTrue(scan["truncated"])
        self.assertEqual(len(scan["pdf_paths"]), 2)

    def test_folder_candidate_starts_a_fresh_confirm_state(self):
        """Folder selection follows the same no-resume contract as upload."""
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nfolder selection fixture\n")
            scanned_files, *_ = app.scan_selected_pdf_directory(str(Path(tmpdir)), "", "", "", True)
            app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "successful", "phase": "Old run"}
            reset = app.reset_automatic_run_presentation([], scanned_files)
            confirm = app.automatic_process_button_state([], scanned_files, processed=False)

        self.assertEqual(scanned_files, [str(pdf)])
        self.assertEqual(app.LIVE_AUTOMATIC_RUN_STATUS.get("state"), "preparing")
        self.assertEqual(app.LIVE_AUTOMATIC_RUN_STATUS.get("phase"), "Finishing preparation")
        self.assertFalse(reset[3]["interactive"])
        self.assertFalse(reset[8]["interactive"])
        self.assertTrue(confirm["interactive"])
        self.assertEqual(confirm["value"], "Confirm and start processing")

    def test_selected_pdf_list_html_renders_name_and_size_rows(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nhello")
            rendered = app.selected_pdf_list_html([str(pdf)])

        self.assertIn("paper.pdf", rendered)
        self.assertTrue(" B" in rendered or " KB" in rendered or " MB" in rendered)
        self.assertIn("download-row", rendered)
        self.assertNotIn("download-title", rendered)

    def test_mixed_folder_notice_includes_the_selected_pdf_list(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nhello")
            rendered = app.batch_folder_notice_html([str(pdf)])

        self.assertIn("Only PDF files were uploaded from this mixed folder", rendered)
        self.assertIn("paper.pdf", rendered)

    def test_batch_folder_status_rejects_fake_pdf_files(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_pdf = Path(tmpdir) / "fake.pdf"
            fake_pdf.write_text("not actually a pdf", encoding="utf-8")
            rendered = app.batch_folder_status_html([str(fake_pdf)])
            report = app.no_pdf_in_folder_report([str(fake_pdf)])

        self.assertIn("No PDFs found in selected folder", rendered)
        self.assertIn("without a valid PDF header", rendered)
        self.assertIn("AUTO-INPUT-003", report)
        self.assertIn("valid PDF header", report)

    def test_workspace_inspector_html_uses_layer_specific_truthful_language(self):
        import rag_pdf_gradio_app as app

        original = app.workspace_storage_inspector
        try:
            app.workspace_storage_inspector = lambda storage, slug: {
                "status": "complete",
                "workspace_name": "Test Workspace",
                "workspace_slug": slug,
                "workspace_document_count": 1,
                "raw_native_doc_count": 0,
                "embedded_chunk_count": 1,
                "sqlite_workspace_metadata_fields": ["title", "docAuthor"],
                "custom_document_json_fields": ["title", "chunkSource", "token_count_estimate"],
                "lancedb_row_fields": ["id", "text", "title"],
                "page_segment_visibility": "visible_in_chunk_text",
                "sample_workspace_document": {},
                "sample_custom_document_record": {},
                "sample_lancedb_row": {},
            }
            rendered = app.workspace_inspector_html("test")
        finally:
            app.workspace_storage_inspector = original

        self.assertIn("Workspace storage check", rendered)
        self.assertIn("AnythingLLM storage path", rendered)
        self.assertIn("Copy path", rendered)
        self.assertIn("navigator.clipboard.writeText", rendered)
        self.assertIn("layer-specific", rendered)
        self.assertIn("SQLite workspace_documents.metadata fields", rendered)
        self.assertIn("Custom document JSON fields", rendered)
        self.assertIn("LanceDB row fields", rendered)
        self.assertIn("Text-visible page/segment evidence", rendered)
        self.assertNotIn(">Metadata fields seen<", rendered)

    def test_native_upload_readiness_html_lists_separate_preflight_states(self):
        import rag_pdf_gradio_app as app

        rendered = app.native_upload_readiness_html(
            {
                "local_db_found": True,
                "local_db_message": "db ok",
                "workspace_slug_found": False,
                "workspace_slug_message": "workspace missing",
                "runtime_api_reachable": True,
                "runtime_api_url": "http://127.0.0.1:3001",
                "runtime_api_message": "api ok",
                "authenticated": False,
                "authentication_message": "auth failed",
                "upload_succeeded": None,
                "upload_message": "not run",
            }
        )
        self.assertIn("Native upload readiness", rendered)
        self.assertIn("Local DB found", rendered)
        self.assertIn("Workspace slug found", rendered)
        self.assertIn("Runtime API reachable", rendered)
        self.assertIn("Authenticated", rendered)
        self.assertIn("Upload succeeded", rendered)

    def test_run_automatic_strips_anythingllm_api_url_for_local_only_runs(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_prepare = app.prepare_pdf
        captured = {}
        try:
            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)

            def fake_prepare(pdf_path, out_dir, args):
                captured["anythingllm_api_url"] = args.anythingllm_api_url
                captured["prepare_and_upload"] = args.prepare_and_upload
                captured["native_upload_transport"] = getattr(args, "native_upload_transport", "")
                captured["workspace_slug"] = args.workspace_slug
                captured["document_folders"] = args.anythingllm_create_document_folders
                return {
                    "pdf": str(pdf_path),
                    "readiness_status": "ready",
                    "readiness_reasons": [],
                    "vector_validation_status": "not_run",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                }

            app.prepare_pdf = fake_prepare
            app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_LOCAL_ONLY_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Strict metadata only",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PASSAGES_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.prepare_pdf = original_prepare
        self.assertFalse(captured["prepare_and_upload"])
        self.assertEqual(captured["anythingllm_api_url"], "")
        self.assertEqual(captured["native_upload_transport"], "raw_text")
        self.assertEqual(captured["workspace_slug"], "")
        self.assertFalse(captured["document_folders"])

    def test_run_automatic_reports_folder_without_pdfs(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "notes.txt"
            note.write_text("still not a pdf", encoding="utf-8")
            result = app.run_automatic(
                [],
                [str(note)],
                "",
                "",
                "",
                True,
                app.MODE_LOCAL_ONLY_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Strict metadata only",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PASSAGES_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )

        summary_html = result[0]["value"]
        self.assertIn("AUTO-INPUT-003", summary_html)
        self.assertIn("does not contain PDFs", summary_html)

    def test_run_automatic_degrades_unavailable_simulation_and_still_prepares(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_prepare = app.prepare_pdf
        original_resolve_simulation = app.resolve_simulation_run
        original_workspace_exists = app.local_workspace_slug_exists
        original_readiness = app.native_upload_readiness_report
        original_embedder_probe = app.verify_anythingllm_runtime_embedder
        captured = {}
        try:
            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)
            app.local_workspace_slug_exists = lambda slug: (True, "workspace exists")
            app.native_upload_readiness_report = lambda *args, **kwargs: {
                "local_db_found": True,
                "local_db_message": "db ok",
                "workspace_slug_found": True,
                "workspace_slug_message": "workspace exists",
                "runtime_api_url": "http://127.0.0.1:3001",
                "runtime_api_reachable": True,
                "runtime_api_status": "reachable",
                "runtime_api_message": "api ok",
                "runtime_start_status": "not_attempted",
                "runtime_start_message": "not attempted",
                "authenticated": True,
                "authentication_status": "authenticated",
                "authentication_message": "auth ok",
                "upload_succeeded": None,
                "upload_status": "not_run",
                "upload_message": "not run",
            }
            app.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {
                "status": "pass", "provider": "test", "model": "test", "dimension": 3
            }
            app.resolve_simulation_run = lambda *args, **kwargs: {
                "enabled": False,
                "adapter": None,
                "note": "",
                "error_report": app.app_error_report(
                    "AUTO-SIM-003",
                    "Selected Ollama model is not installed",
                    ["Selected model: missing-model"],
                    ["Choose None if you only want files."],
                ),
            }

            def fake_prepare(pdf_path, out_dir, args):
                captured["called"] = True
                captured["run_vector_eval"] = args.run_vector_eval
                return {
                    "pdf": str(pdf_path),
                    "readiness_status": "ready",
                    "readiness_reasons": [],
                    "vector_validation_status": "not_run",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                }

            app.prepare_pdf = fake_prepare
            result = app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_LOCAL_ONLY_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "",
                "",
                "",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Strict metadata only",
                False,
                "",
                "missing-model",
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PAGE_LIMIT_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )
            native_result = app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_NATIVE_UPLOAD_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "workspace-a",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Strict metadata only",
                False,
                "",
                "missing-model",
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PAGE_LIMIT_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.prepare_pdf = original_prepare
            app.resolve_simulation_run = original_resolve_simulation
            app.local_workspace_slug_exists = original_workspace_exists
            app.native_upload_readiness_report = original_readiness
            app.verify_anythingllm_runtime_embedder = original_embedder_probe

        self.assertTrue(captured["called"])
        self.assertFalse(captured["run_vector_eval"])
        self.assertIn("Retrieval simulation warning", result[0]["value"])
        self.assertIn("Completed", result[0]["value"])
        self.assertIn("Retrieval simulation warning", native_result[0]["value"])
        self.assertIn("Needs attention", native_result[0]["value"])

    def test_run_automatic_legacy_probe_scope_keeps_the_full_ledger_limit(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_prepare = app.prepare_pdf
        original_workspace_exists = app.local_workspace_slug_exists
        original_readiness = app.native_upload_readiness_report
        original_embedder_probe = app.verify_anythingllm_runtime_embedder
        captured = {}
        try:
            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)
            app.local_workspace_slug_exists = lambda slug: (True, "workspace exists")
            app.native_upload_readiness_report = lambda *args, **kwargs: {
                "local_db_found": True,
                "local_db_message": "db ok",
                "workspace_slug_found": True,
                "workspace_slug_message": "workspace exists",
                "runtime_api_url": "http://127.0.0.1:3001",
                "runtime_api_reachable": True,
                "runtime_api_status": "reachable",
                "runtime_api_message": "api ok",
                "runtime_start_status": "not_attempted",
                "runtime_start_message": "not attempted",
                "authenticated": True,
                "authentication_status": "authenticated",
                "authentication_message": "auth ok",
                "upload_succeeded": None,
                "upload_status": "not_run",
                "upload_message": "not run",
            }
            app.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {
                "status": "pass", "provider": "test", "model": "test", "dimension": 3
            }

            def fake_prepare(pdf_path, out_dir, args):
                captured["prepare_and_upload"] = args.prepare_and_upload
                captured["upload_limit"] = args.upload_limit
                captured["native_upload_transport"] = args.native_upload_transport
                captured["native_upload_representation"] = args.native_upload_representation
                captured["workspace_slug"] = args.workspace_slug
                captured["document_folders"] = args.anythingllm_create_document_folders
                return {
                    "pdf": str(pdf_path),
                    "readiness_status": "ready",
                    "readiness_reasons": [],
                    "vector_validation_status": "not_run",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                }

            app.prepare_pdf = fake_prepare
            result = app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_NATIVE_UPLOAD_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "workspace-a",
                app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
                "",
                "Strict metadata only",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PAGE_LIMIT_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.prepare_pdf = original_prepare
            app.local_workspace_slug_exists = original_workspace_exists
            app.native_upload_readiness_report = original_readiness
            app.verify_anythingllm_runtime_embedder = original_embedder_probe

        self.assertTrue(captured["prepare_and_upload"])
        self.assertEqual(captured["upload_limit"], 0)
        self.assertEqual(captured["native_upload_transport"], "file_upload")
        self.assertEqual(captured["native_upload_representation"], "page_parents")
        self.assertEqual(captured["workspace_slug"], "workspace-a")
        self.assertTrue(captured["document_folders"])
        self.assertIn("Needs attention", result[0]["value"])

    def test_run_automatic_upload_mode_fails_fast_when_runtime_api_is_down(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_prepare = app.prepare_pdf
        original_workspace_exists = app.local_workspace_slug_exists
        original_readiness = app.native_upload_readiness_report
        try:
            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)
            app.local_workspace_slug_exists = lambda slug: (True, "workspace exists")
            app.native_upload_readiness_report = lambda *args, **kwargs: {
                "local_db_found": True,
                "local_db_message": "db ok",
                "workspace_slug_found": True,
                "workspace_slug_message": "workspace exists",
                "runtime_api_url": "http://127.0.0.1:3001",
                "runtime_api_reachable": False,
                "runtime_api_status": "unreachable",
                "runtime_api_message": "AnythingLLM did not expose a usable API endpoint.",
                "runtime_start_status": "started",
                "runtime_start_message": "Started AnythingLLM Desktop.",
                "authenticated": None,
                "authentication_status": "not_checked",
                "authentication_message": "not checked",
                "upload_succeeded": None,
                "upload_status": "not_run",
                "upload_message": "not run",
            }

            def should_not_run(*args, **kwargs):
                raise AssertionError("prepare_pdf should not run when upload preflight fails")

            app.prepare_pdf = should_not_run
            result = app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_NATIVE_UPLOAD_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "workspace-a",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Strict metadata only",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                False,
                False,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PAGE_LIMIT_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                512,
                75,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.prepare_pdf = original_prepare
            app.local_workspace_slug_exists = original_workspace_exists
            app.native_upload_readiness_report = original_readiness

        self.assertIn("AUTO-UPLOAD-001", result[0]["value"])
        self.assertIn("AnythingLLM runtime API is not reachable", result[0]["value"])

    def test_run_automatic_upload_mode_does_not_block_completed_upload_on_needs_review(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_workspace_exists = app.local_workspace_slug_exists
        original_readiness = app.native_upload_readiness_report
        original_execute = app.execute_preparation
        original_legacy = app.legacy_summary_from_run
        original_embedder_probe = app.verify_anythingllm_runtime_embedder
        try:
            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)
            app.local_workspace_slug_exists = lambda slug: (True, "workspace exists")
            app.native_upload_readiness_report = lambda *args, **kwargs: {
                "local_db_found": True,
                "local_db_message": "db ok",
                "workspace_slug_found": True,
                "workspace_slug_message": "workspace exists",
                "runtime_api_url": "http://127.0.0.1:3001",
                "runtime_api_reachable": True,
                "runtime_api_status": "reachable",
                "runtime_api_message": "reachable",
                "runtime_start_status": "not_attempted",
                "runtime_start_message": "not attempted",
                "authenticated": True,
                "authentication_status": "authenticated",
                "authentication_message": "authenticated",
                "upload_succeeded": None,
                "upload_status": "not_run",
                "upload_message": "not run",
            }
            app.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {
                "status": "pass", "provider": "test", "model": "test", "dimension": 3
            }

            class FakeControlledRun:
                status = "pass"
                operator_summary = "ok"

                @staticmethod
                def to_dict():
                    return {"status": "pass"}

            def fake_execute_preparation(pdf_path, out_dir, args, prepare_fn):
                return FakeControlledRun()

            def fake_legacy_summary(_run):
                out_dir = PROJECT_ROOT / "tmp-output" / "dummy"
                selected_dir = out_dir / "selected"
                metadata_api = out_dir / "metadata-api"
                inspection_dir = out_dir / "inspection"
                native_dir = out_dir / "native-kit"
                for path in (selected_dir, metadata_api, inspection_dir, native_dir):
                    path.mkdir(parents=True, exist_ok=True)
                report = selected_dir / "readiness-report.html"
                upload_file = selected_dir / "anythingllm-upload.txt"
                manifest = selected_dir / "segment-manifest.jsonl"
                page_parent_manifest = selected_dir / "page-parent-manifest.jsonl"
                child_parent_map = selected_dir / "child-parent-map.csv"
                metadata_payloads = metadata_api / "raw-text-payloads-native-header.jsonl"
                page_parent_upload_plan = metadata_api / "page-parent-upload-plan.csv"
                metadata_layer_visibility = inspection_dir / "metadata-layer-visibility.csv"
                column_explanations = inspection_dir / "column-explanations.csv"
                author_csv = inspection_dir / "author-inference-evaluation.csv"
                author_json = inspection_dir / "author-inference-evaluation.json"
                runtime_validation = inspection_dir / "anythingllm-runtime-validation.json"
                post_upload = inspection_dir / "post-upload-verification.csv"
                runtime_probe = inspection_dir / "anythingllm-runtime-validation.csv"
                for file in (
                    report,
                    upload_file,
                    manifest,
                    page_parent_manifest,
                    child_parent_map,
                    metadata_payloads,
                    page_parent_upload_plan,
                    metadata_layer_visibility,
                    column_explanations,
                    author_csv,
                    author_json,
                    runtime_validation,
                    post_upload,
                    runtime_probe,
                ):
                    file.write_text("ok", encoding="utf-8")
                return {
                    "upload_file": str(upload_file),
                    "manifest": str(manifest),
                    "page_parent_manifest": str(page_parent_manifest),
                    "child_parent_map": str(child_parent_map),
                    "representation_comparison": "",
                    "harmonization_report": "",
                    "representation_recommendation": "",
                    "report": str(report),
                    "variant_summary": "",
                    "metadata_payloads": str(metadata_payloads),
                    "page_parent_metadata_payloads": "",
                    "page_parent_upload_plan": str(page_parent_upload_plan),
                    "metadata_layer_visibility": str(metadata_layer_visibility),
                    "column_explanations": str(column_explanations),
                    "author_inference_evaluation_csv": str(author_csv),
                    "author_inference_evaluation_json": str(author_json),
                    "edge_case_report": "",
                    "edge_case_results": "",
                    "diagnostics_report": "",
                    "diagnostics_csv": "",
                    "workspace_model_gate": "",
                    "post_upload_verification": str(post_upload),
                    "anythingllm_runtime_validation": str(runtime_probe),
                    "native_test_kit": {},
                    "native_probe_kit": {},
                    "variant_outputs": {},
                    "author_inference_passed": 1,
                    "author_inference_failed": 0,
                    "readiness_status": "needs_review",
                    "readiness_reasons": ["exact_literal_probe_failed"],
                    "total_pipeline_seconds": 1.0,
                    "vector_validation_status": "not_run",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                    "selected_backend": "pymupdf",
                    "pdf_page_count": 1,
                    "start_page": 1,
                    "end_page": 0,
                    "segment_mode": "passages",
                    "segments": 2,
                    "page_parents": 1,
                    "segment_harmonization_risk": "low",
                    "segment_units_exceeding_effective_limit": 0,
                    "page_parent_harmonization_risk": "low",
                    "page_parent_units_exceeding_effective_limit": 0,
                    "marker_char_ratio": "0.0",
                    "avg_content_chars": "100",
                    "chunk_size": 768,
                    "chunk_overlap": 128,
                    "chunk_settings_source": "anythingllm_sqlite_read_only",
                    "anythingllm_embedding_engine": "openrouter",
                    "anythingllm_embedding_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_effective_model_source": "EMBEDDING_MODEL_PREF",
                    "anythingllm_embedding_generic_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_provider_support": "locally_verified",
                    "anythingllm_embedding_anomalies": [],
                    "anythingllm_embedding_max_chunk_length": 32768,
                    "anythingllm_embedding_batch_size": 9,
                    "selected_region_embedding_coverage": "100%",
                    "backend_word_disagreement": "none",
                    "outline_reliability": "good",
                    "storage_inspection_status": "ok",
                    "storage_workspace_document_count": 2,
                    "storage_raw_native_doc_count": 2,
                    "storage_embedded_chunk_count": 2,
                    "storage_page_segment_visibility": "visible_in_chunk_text",
                    "storage_sample_custom_document_title": "dummy",
                    "storage_sample_lancedb_title": "dummy",
                    "anythingllm_runtime_status": "reachable",
                    "edge_case_status": "pass",
                    "edge_case_failures": 0,
                    "edge_case_warnings": 0,
                    "diagnostic_error_count": 0,
                    "diagnostic_warning_count": 0,
                    "metadata_schema_status": "pass",
                    "api_upload_status": "complete",
                    "api_upload_error": "",
                    "api_upload_warning": "Upload continued even though preparation needs review.",
                    "api_uploaded": 2,
                    "api_embedded": 2,
                    "api_authentication_mode": "temporary_desktop_api_key",
                    "api_document_foldering_enabled": True,
                    "api_document_folder_name": "Codex Native Upload Smoke Test",
                    "api_document_folder_path": "C:\\\\tmp\\\\documents\\\\Codex Native Upload Smoke Test",
                    "api_temporary_key_cleanup": "deleted",
                    "native_metadata_rows": 2,
                    "workspace_model_gate_status": "pass",
                    "post_upload_verification_status": "pass",
                    "post_upload_classification": "native_metadata_llm_visible",
                    "anythingllm_runtime_validation_status": "pass",
                    "anythingllm_runtime_vector_checks_passed": 2,
                    "anythingllm_runtime_vector_checks_total": 2,
                    "anythingllm_runtime_chat_model": "deepseek-v4-pro",
                    "anythingllm_runtime_chat_error": "",
                }

            app.execute_preparation = fake_execute_preparation
            app.legacy_summary_from_run = fake_legacy_summary

            result = app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_NATIVE_UPLOAD_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "workspace-a",
                app.NATIVE_UPLOAD_SCOPE_PROBE_LABEL,
                "",
                "Native title header (priority)",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                True,
                True,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PASSAGES_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                768,
                128,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.local_workspace_slug_exists = original_workspace_exists
            app.native_upload_readiness_report = original_readiness
            app.execute_preparation = original_execute
            app.legacy_summary_from_run = original_legacy
            app.verify_anythingllm_runtime_embedder = original_embedder_probe

        self.assertIn("Status</div><div class=\"summary-value\">completed</div>", result[0]["value"])
        self.assertIn("Native metadata upload</div><div class=\"summary-value\">complete</div>", result[0]["value"])
        self.assertIn("Native metadata upload warning</div><div class=\"summary-value\">Upload continued even though preparation needs review.</div>", result[0]["value"])

    def test_run_summary_html_marks_completed_upload_with_review_flags_not_error(self):
        import rag_pdf_gradio_app as app

        rendered = app.run_summary_html(
            "\n".join(
                [
                    "Status: completed",
                    "Readiness: needs_review",
                    "Native metadata upload: complete",
                ]
            )
        )

        self.assertIn("Completed with review flags", rendered)
        self.assertNotIn('summary-status error', rendered)

    def test_run_summary_html_never_labels_running_work_as_completed(self):
        import rag_pdf_gradio_app as app

        rendered = app.run_summary_html("Status: running\nThe parser/chunker has started.")

        self.assertIn('>Running<', rendered)
        self.assertNotIn('>Completed<', rendered)

    def test_run_summary_html_marks_completed_local_only_review_flags_not_error(self):
        import rag_pdf_gradio_app as app

        rendered = app.run_summary_html(
            "\n".join(
                [
                    "Status: completed",
                    f"Mode: {app.MODE_LOCAL_ONLY_LABEL}",
                    "Readiness: needs_review",
                    "Readiness reasons: exact_literal_probe_failed",
                    "AnythingLLM API/upload: skipped because mode is Create local files only",
                ]
            )
        )

        self.assertIn("Completed with review flags", rendered)
        self.assertNotIn('summary-status error', rendered)

    def test_normalize_simulation_choice_accepts_old_skip_alias(self):
        import rag_pdf_gradio_app as app

        self.assertEqual(app.normalize_simulation_choice("Skip vector simulation"), app.SIMULATION_SKIP_LABEL)

    def test_chunk_survival_validation_runs_for_needs_review_literal_probe_only(self):
        selected = {
            "readiness_status": "needs_review",
            "readiness_reasons": ["exact_literal_probe_failed"],
        }
        self.assertTrue(pipeline.should_run_chunk_survival_validation(selected))

    def test_chunk_survival_validation_blocks_ocr_like_needs_review(self):
        selected = {
            "readiness_status": "needs_review",
            "readiness_reasons": ["ocr_or_text_layer_failure_likely"],
        }
        self.assertFalse(pipeline.should_run_chunk_survival_validation(selected))

    def test_ocr_assistance_evidence_uses_resolved_strategy_before_reporting(self):
        evidence = pipeline.ocr_assistance_evidence(
            {"backend": "unstructured"},
            [],
            {"unstructured_runtime": {"resolved_strategy": "ocr_only"}},
        )

        self.assertTrue(evidence["used"])
        self.assertEqual(evidence["evidence"], "unstructured_ocr_only")

    def test_clean_complete_scan_ocr_can_explain_coverage_disagreement(self):
        selected = {
            "backend": "unstructured",
            "segments": [{"pdf_page": page, "text": "body"} for page in range(1, 6)],
            "quality": {
                "included_pages": 5,
                "included_words": 1594,
                "included_chars": 9885,
                "average_words_per_page": 318.8,
                "replacement_chars": 0,
                "ocr_layout_artifact_ratio": 0.0,
                "scanned_likelihood": "possible",
            },
            "native_chunk_eval": {"status": "pass"},
        }
        candidates = [
            {
                "backend": "pymupdf",
                "segments": [],
                "quality": {"included_words": 0, "scanned_likelihood": "high"},
            },
            {
                "backend": "pymupdf4llm",
                "segments": [{"pdf_page": page} for page in range(1, 6)],
                "quality": {
                    "included_words": 885,
                    "average_words_per_page": 177.0,
                    "ocr_layout_artifact_ratio": 0.0345,
                    "scanned_likelihood": "possible",
                },
            },
            selected,
        ]

        result = pipeline.explainable_ocr_coverage_disagreement(
            selected,
            candidates,
            {"pdf_page_count": 5},
            {"used": True, "evidence": "unstructured_hi_res"},
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "clean_complete_ocr_recovery_from_absent_native_layer")

    def test_clean_substantial_peer_keeps_ocr_coverage_disagreement_blocking(self):
        selected = {
            "backend": "unstructured",
            "segments": [{"pdf_page": page} for page in range(1, 6)],
            "quality": {
                "included_pages": 5,
                "included_words": 1600,
                "included_chars": 10_000,
                "average_words_per_page": 320.0,
                "replacement_chars": 0,
                "ocr_layout_artifact_ratio": 0.0,
            },
            "native_chunk_eval": {"status": "pass"},
        }
        candidates = [
            {
                "backend": "pymupdf",
                "segments": [],
                "quality": {"included_words": 0, "scanned_likelihood": "high"},
            },
            {
                "backend": "pymupdf4llm",
                "segments": [{"pdf_page": page} for page in range(1, 6)],
                "quality": {
                    "included_words": 900,
                    "average_words_per_page": 180.0,
                    "ocr_layout_artifact_ratio": 0.0,
                    "scanned_likelihood": "possible",
                },
            },
            selected,
        ]

        result = pipeline.explainable_ocr_coverage_disagreement(
            selected,
            candidates,
            {"pdf_page_count": 5},
            {"used": True, "evidence": "unstructured_hi_res"},
        )

        self.assertFalse(result["accepted"])
        self.assertFalse(result["checks"]["shorter_peer_has_objective_weakness"])

    def test_prepare_pdf_does_not_auto_run_temporary_workspace_validation_for_local_only(self):
        original_run_temp_validation = pipeline.run_temporary_workspace_validation
        original_detect_api = pipeline.detect_anythingllm_api_url
        try:
            calls = {"count": 0}

            def fake_temp_validation(*args, **kwargs):
                calls["count"] += 1
                return {
                    "status": "complete",
                    "workspace_slug": "temp",
                    "workspace_name": "temp",
                    "workspace_create_status": "complete",
                    "upload_status": "complete",
                    "post_upload_status": "pass",
                    "runtime_validation_status": "pass",
                    "retention_status": "left_visible_for_manual_review",
                    "post_upload_report": {},
                    "runtime_validation_report": {},
                    "upload_report": {},
                    "error": "",
                }

            pipeline.run_temporary_workspace_validation = fake_temp_validation
            pipeline.detect_anythingllm_api_url = lambda api_url, api_key="", timeout=1.25: {"api_url": api_url}

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                pdf_path = root / "sample.pdf"
                doc = fitz.open()
                page = doc.new_page()
                page.insert_textbox(
                    fitz.Rect(60, 60, 540, 760),
                    "Introduction\n\n" + ("Distinctive prose for smoke validation. " * 80),
                    fontsize=10,
                )
                doc.save(pdf_path)
                doc.close()

                args = SimpleNamespace(
                    document_label="",
                    document_author="",
                    document_short_label="",
                    use_file_title_fallback=True,
                    deep_extraction=False,
                    include_front_matter=True,
                    include_back_matter=True,
                    backend_mode="pymupdf",
                    first_page_override=0,
                    end_page_override=0,
                    target_passage_length=500,
                    end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
                    validation_phrases=[],
                    unstructured_strategy="fast",
                    marker_style="short",
                    disable_inline_markers=False,
                    run_vector_eval=False,
                    ollama_model="bge-m3:latest",
                    ollama_url="http://127.0.0.1:11434/api/embed",
                    max_vector_probes=4,
                    prepare_and_upload=False,
                    anythingllm_api_url="http://127.0.0.1:3001",
                    anythingllm_api_key="",
                    workspace_slug="",
                    test_workspace_slug="test",
                    upload_limit=0,
                    anythingllm_storage_dir=str(root / "missing-storage"),
                    anythingllm_chunk_size=400,
                    anythingllm_chunk_overlap=40,
                )
                pipeline.prepare_pdf(pdf_path, root / "output", args)

            self.assertEqual(calls["count"], 0)
        finally:
            pipeline.run_temporary_workspace_validation = original_run_temp_validation
            pipeline.detect_anythingllm_api_url = original_detect_api

    def test_build_run_diagnostics_skips_workspace_gate_warning_for_local_only(self):
        diagnostics = pipeline.build_run_diagnostics(
            profile={"needs_password": False},
            selected={
                "quality": {},
                "outline_validation": {"reliability": "ok"},
                "end_page": 5,
                "vector_validation_status": "not_run_extraction_only",
                "fallback_marker_status": "ok",
            },
            candidates=[],
            storage_report={"status": "complete"},
            upload_report={"status": "skipped_prepare_only"},
            workspace_gate={
                "status": "workspace_missing",
                "message": "Workspace `test` was not found.",
            },
            post_upload_report={"status": "not_checked"},
            metadata_schema_report={"runtime_api_status": "reachable_authorized"},
            runtime_validation_report={"status": "not_run"},
            temporary_workspace_validation={"status": "not_run"},
        )

        codes = {row["code"] for row in diagnostics}
        self.assertNotIn("ANYTHINGLLM_WORKSPACE_MODEL_GATE_BLOCKED", codes)

    def test_build_run_diagnostics_renames_missing_outline_warning(self):
        diagnostics = pipeline.build_run_diagnostics(
            profile={"needs_password": False},
            selected={
                "quality": {},
                "outline_validation": {"reliability": "missing"},
                "end_page": 10,
                "detected_end_page": 10,
                "include_back_matter": False,
                "vector_validation_status": "not_run_extraction_only",
                "fallback_marker_status": "ok",
            },
            candidates=[],
            storage_report={"status": "complete"},
            upload_report={"status": "skipped_prepare_only"},
            workspace_gate={"status": "not_checked", "message": ""},
            post_upload_report={"status": "not_checked"},
            metadata_schema_report={"runtime_api_status": "not_checked"},
            runtime_validation_report={"status": "not_run"},
            temporary_workspace_validation={"status": "not_run"},
        )
        codes = {row["code"] for row in diagnostics}
        self.assertIn("PDF_HAS_NO_BOOKMARKS", codes)
        self.assertNotIn("PDF_OUTLINE_MISSING", codes)

    def test_build_run_diagnostics_demotes_end_matter_warning_when_back_matter_included(self):
        diagnostics = pipeline.build_run_diagnostics(
            profile={"needs_password": False},
            selected={
                "quality": {},
                "outline_validation": {"reliability": "ok"},
                "end_page": None,
                "detected_end_page": None,
                "include_back_matter": True,
                "vector_validation_status": "not_run_extraction_only",
                "fallback_marker_status": "ok",
            },
            candidates=[],
            storage_report={"status": "complete"},
            upload_report={"status": "skipped_prepare_only"},
            workspace_gate={"status": "not_checked", "message": ""},
            post_upload_report={"status": "not_checked"},
            metadata_schema_report={"runtime_api_status": "not_checked"},
            runtime_validation_report={"status": "not_run"},
            temporary_workspace_validation={"status": "not_run"},
        )
        row = next(row for row in diagnostics if row["code"] == "BOUNDARY_END_MATTER_NOT_DETECTED")
        self.assertEqual(row["severity"], "info")

    def test_run_automatic_local_only_without_api_url_skips_api_resolution(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_readiness = app.native_upload_readiness_report
        original_execute = app.execute_preparation
        original_legacy = app.legacy_summary_from_run
        original_detect_api = app.detect_anythingllm_api_url
        try:
            calls = {"count": 0}

            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)
            app.native_upload_readiness_report = lambda *args, **kwargs: app.initial_native_upload_readiness_report()

            class FakeControlledRun:
                status = "pass"
                operator_summary = "ok"

                @staticmethod
                def to_dict():
                    return {"status": "pass"}

            def fake_execute_preparation(pdf_path, out_dir, args, prepare_fn):
                return FakeControlledRun()

            def fake_legacy_summary(_run):
                out_dir = PROJECT_ROOT / "tmp-output" / "dummy-local-only"
                selected_dir = out_dir / "selected"
                selected_dir.mkdir(parents=True, exist_ok=True)
                upload_file = selected_dir / "anythingllm-upload.txt"
                manifest = selected_dir / "segment-manifest.jsonl"
                page_parent_manifest = selected_dir / "page-parent-manifest.jsonl"
                child_parent_map = selected_dir / "child-parent-map.csv"
                report = selected_dir / "readiness-report.html"
                for file in (upload_file, manifest, page_parent_manifest, child_parent_map, report):
                    file.write_text("ok", encoding="utf-8")
                return {
                    "upload_file": str(upload_file),
                    "manifest": str(manifest),
                    "page_parent_manifest": str(page_parent_manifest),
                    "child_parent_map": str(child_parent_map),
                    "representation_comparison": "",
                    "harmonization_report": "",
                    "representation_recommendation": "",
                    "report": str(report),
                    "variant_summary": "",
                    "metadata_payloads": "",
                    "page_parent_metadata_payloads": "",
                    "page_parent_upload_plan": "",
                    "metadata_layer_visibility": "",
                    "column_explanations": "",
                    "author_inference_evaluation_csv": "",
                    "author_inference_evaluation_json": "",
                    "edge_case_report": "",
                    "edge_case_results": "",
                    "diagnostics_report": "",
                    "diagnostics_csv": "",
                    "workspace_model_gate": "",
                    "post_upload_verification": "",
                    "anythingllm_runtime_validation": "",
                    "native_test_kit": {},
                    "native_probe_kit": {},
                    "variant_outputs": {},
                    "author_inference_passed": 1,
                    "author_inference_failed": 0,
                    "readiness_status": "ready",
                    "readiness_reasons": [],
                    "total_pipeline_seconds": 1.0,
                    "vector_validation_status": "not_run_extraction_only",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                    "selected_backend": "pymupdf",
                    "pdf_page_count": 1,
                    "start_page": 1,
                    "end_page": 0,
                    "segment_mode": "passages",
                    "segments": 1,
                    "page_parents": 1,
                    "segment_harmonization_risk": "low",
                    "segment_units_exceeding_effective_limit": 0,
                    "page_parent_harmonization_risk": "low",
                    "page_parent_units_exceeding_effective_limit": 0,
                    "marker_char_ratio": "0.0",
                    "avg_content_chars": "100",
                    "chunk_size": 768,
                    "chunk_overlap": 128,
                    "chunk_settings_source": "anythingllm_sqlite_read_only",
                    "anythingllm_embedding_engine": "openrouter",
                    "anythingllm_embedding_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_effective_model_source": "EMBEDDING_MODEL_PREF",
                    "anythingllm_embedding_generic_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_provider_support": "locally_verified",
                    "anythingllm_embedding_anomalies": [],
                    "anythingllm_embedding_max_chunk_length": 32768,
                    "anythingllm_embedding_batch_size": 9,
                    "selected_region_embedding_coverage": "100%",
                    "backend_word_disagreement": "none",
                    "outline_reliability": "good",
                    "storage_inspection_status": "ok",
                    "storage_workspace_document_count": 0,
                    "storage_raw_native_doc_count": 0,
                    "storage_embedded_chunk_count": 0,
                    "storage_page_segment_visibility": "not_checked",
                    "storage_sample_custom_document_title": "",
                    "storage_sample_lancedb_title": "",
                    "anythingllm_runtime_status": "not_checked",
                    "edge_case_status": "pass",
                    "edge_case_failures": 0,
                    "edge_case_warnings": 0,
                    "diagnostic_error_count": 0,
                    "diagnostic_warning_count": 0,
                    "metadata_schema_status": "source_contract",
                    "api_upload_status": "skipped_prepare_only",
                    "api_upload_error": "",
                    "api_upload_warning": "",
                    "api_uploaded": 0,
                    "api_embedded": 0,
                    "api_authentication_mode": "not_applicable",
                    "api_document_foldering_enabled": False,
                    "api_document_folder_name": "",
                    "api_document_folder_path": "",
                    "api_temporary_key_cleanup": "not_applicable",
                    "native_metadata_rows": 0,
                    "workspace_model_gate_status": "not_checked",
                    "post_upload_verification_status": "not_checked",
                    "post_upload_classification": "",
                    "anythingllm_runtime_validation_status": "not_run",
                    "anythingllm_runtime_vector_checks_passed": 0,
                    "anythingllm_runtime_vector_checks_total": 0,
                    "anythingllm_runtime_chat_model": "",
                    "anythingllm_runtime_chat_error": "",
                    "temporary_workspace_validation_status": "not_run",
                }

            def fake_detect_anythingllm_api_url(*args, **kwargs):
                calls["count"] += 1
                return {"status": "reachable", "api_url": "http://127.0.0.1:3001"}

            app.execute_preparation = fake_execute_preparation
            app.legacy_summary_from_run = fake_legacy_summary
            app.detect_anythingllm_api_url = fake_detect_anythingllm_api_url

            app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_LOCAL_ONLY_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "",
                "",
                "",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Native title header (priority)",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                True,
                True,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PASSAGES_LABEL,
                "",
                "",
                "fast",
                True,
                True,
                768,
                128,
                False,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.native_upload_readiness_report = original_readiness
            app.execute_preparation = original_execute
            app.legacy_summary_from_run = original_legacy
            app.detect_anythingllm_api_url = original_detect_api

        self.assertEqual(calls["count"], 0)

    def test_run_automatic_local_only_with_explicit_local_api_skips_runtime_readiness(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_readiness = app.native_upload_readiness_report
        original_execute = app.execute_preparation
        original_legacy = app.legacy_summary_from_run
        original_apply = app.apply_recommended_anythingllm_settings
        try:
            calls = {"readiness": 0, "apply": 0}

            app.validate_pdf_inputs = lambda files: (["C:\\tmp\\dummy.pdf"], None)

            def fake_readiness(*args, **kwargs):
                calls["readiness"] += 1
                return {
                    "local_db_found": True,
                    "local_db_message": "ok",
                    "workspace_slug": "",
                    "workspace_slug_found": None,
                    "workspace_slug_message": "No workspace selected.",
                    "runtime_api_url": "http://127.0.0.1:3001",
                    "runtime_api_reachable": True,
                    "runtime_api_status": "reachable",
                    "runtime_api_message": "reachable",
                    "runtime_start_status": "started",
                    "runtime_start_message": "started",
                    "authenticated": None,
                    "authentication_status": "not_checked",
                    "authentication_message": "not checked",
                    "upload_succeeded": None,
                    "upload_status": "not_run",
                    "upload_message": "not run",
                }

            class FakeControlledRun:
                status = "pass"
                operator_summary = "ok"

                @staticmethod
                def to_dict():
                    return {"status": "pass"}

            def fake_execute_preparation(pdf_path, out_dir, args, prepare_fn):
                return FakeControlledRun()

            def fake_legacy_summary(_run):
                out_dir = PROJECT_ROOT / "tmp-output" / "dummy-local-api"
                selected_dir = out_dir / "selected"
                selected_dir.mkdir(parents=True, exist_ok=True)
                upload_file = selected_dir / "anythingllm-upload.txt"
                manifest = selected_dir / "segment-manifest.jsonl"
                page_parent_manifest = selected_dir / "page-parent-manifest.jsonl"
                child_parent_map = selected_dir / "child-parent-map.csv"
                report = selected_dir / "readiness-report.html"
                for file in (upload_file, manifest, page_parent_manifest, child_parent_map, report):
                    file.write_text("ok", encoding="utf-8")
                return {
                    "upload_file": str(upload_file),
                    "manifest": str(manifest),
                    "page_parent_manifest": str(page_parent_manifest),
                    "child_parent_map": str(child_parent_map),
                    "representation_comparison": "",
                    "harmonization_report": "",
                    "representation_recommendation": "",
                    "report": str(report),
                    "variant_summary": "",
                    "metadata_payloads": "",
                    "page_parent_metadata_payloads": "",
                    "page_parent_upload_plan": "",
                    "metadata_layer_visibility": "",
                    "column_explanations": "",
                    "author_inference_evaluation_csv": "",
                    "author_inference_evaluation_json": "",
                    "edge_case_report": "",
                    "edge_case_results": "",
                    "diagnostics_report": "",
                    "diagnostics_csv": "",
                    "workspace_model_gate": "",
                    "post_upload_verification": "",
                    "anythingllm_runtime_validation": "",
                    "native_test_kit": {},
                    "native_probe_kit": {},
                    "variant_outputs": {},
                    "author_inference_passed": 1,
                    "author_inference_failed": 0,
                    "readiness_status": "ready",
                    "readiness_reasons": [],
                    "total_pipeline_seconds": 1.0,
                    "vector_validation_status": "not_run_extraction_only",
                    "vector_error_detail": "",
                    "simulation_provider": "",
                    "simulation_model": "",
                    "vector_embedded_chunks": 0,
                    "vector_embedded_segments": 0,
                    "vector_eval_seconds": 0,
                    "vector_remote_requests": 0,
                    "vector_request_batches": 0,
                    "vector_probe_count": 0,
                    "vector_remote_prompt_tokens": 0,
                    "vector_remote_total_tokens": 0,
                    "vector_remote_cost": 0.0,
                    "vector_remote_key_source": "",
                    "vector_remote_timeout_seconds": 0,
                    "vector_remote_slow_requests": 0,
                    "vector_remote_usage_missing_responses": 0,
                    "vector_remote_embedding_missing_responses": 0,
                    "vector_remote_latency_ms_total": 0,
                    "vector_remote_latency_ms_max": 0,
                    "vector_remote_anomalies": [],
                    "selected_backend": "pymupdf",
                    "pdf_page_count": 1,
                    "start_page": 1,
                    "end_page": 0,
                    "segment_mode": "passages",
                    "segments": 1,
                    "page_parents": 1,
                    "segment_harmonization_risk": "low",
                    "segment_units_exceeding_effective_limit": 0,
                    "page_parent_harmonization_risk": "low",
                    "page_parent_units_exceeding_effective_limit": 0,
                    "marker_char_ratio": "0.0",
                    "avg_content_chars": "100",
                    "chunk_size": 768,
                    "chunk_overlap": 128,
                    "chunk_settings_source": "anythingllm_sqlite_read_only",
                    "anythingllm_embedding_engine": "openrouter",
                    "anythingllm_embedding_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_effective_model_source": "EMBEDDING_MODEL_PREF",
                    "anythingllm_embedding_generic_model": "qwen/qwen3-embedding-8b",
                    "anythingllm_embedding_provider_support": "locally_verified",
                    "anythingllm_embedding_anomalies": [],
                    "anythingllm_embedding_max_chunk_length": 32768,
                    "anythingllm_embedding_batch_size": 9,
                    "selected_region_embedding_coverage": "100%",
                    "backend_word_disagreement": "none",
                    "outline_reliability": "good",
                    "storage_inspection_status": "ok",
                    "storage_workspace_document_count": 0,
                    "storage_raw_native_doc_count": 0,
                    "storage_embedded_chunk_count": 0,
                    "storage_page_segment_visibility": "not_checked",
                    "storage_sample_custom_document_title": "",
                    "storage_sample_lancedb_title": "",
                    "anythingllm_runtime_status": "reachable",
                    "edge_case_status": "pass",
                    "edge_case_failures": 0,
                    "edge_case_warnings": 0,
                    "diagnostic_error_count": 0,
                    "diagnostic_warning_count": 0,
                    "metadata_schema_status": "source_contract",
                    "api_upload_status": "skipped_prepare_only",
                    "api_upload_error": "",
                    "api_upload_warning": "",
                    "api_uploaded": 0,
                    "api_embedded": 0,
                    "api_authentication_mode": "not_applicable",
                    "api_document_foldering_enabled": False,
                    "api_document_folder_name": "",
                    "api_document_folder_path": "",
                    "api_temporary_key_cleanup": "not_applicable",
                    "native_metadata_rows": 0,
                    "workspace_model_gate_status": "not_checked",
                    "post_upload_verification_status": "not_checked",
                    "post_upload_classification": "",
                    "anythingllm_runtime_validation_status": "not_run",
                    "anythingllm_runtime_vector_checks_passed": 0,
                    "anythingllm_runtime_vector_checks_total": 0,
                    "anythingllm_runtime_chat_model": "",
                    "anythingllm_runtime_chat_error": "",
                    "temporary_workspace_validation_status": "not_run",
                }

            app.native_upload_readiness_report = fake_readiness
            app.execute_preparation = fake_execute_preparation
            app.legacy_summary_from_run = fake_legacy_summary
            app.apply_recommended_anythingllm_settings = lambda *_args, **_kwargs: calls.__setitem__("apply", calls["apply"] + 1)

            app.run_automatic(
                ["C:\\tmp\\dummy.pdf"],
                [],
                "",
                "",
                "",
                True,
                app.MODE_LOCAL_ONLY_LABEL,
                str(PROJECT_ROOT / "tmp-output"),
                "http://127.0.0.1:3001",
                "",
                "",
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                "",
                "Native title header (priority)",
                True,
                "",
                app.SIMULATION_SKIP_LABEL,
                "",
                app.DEFAULT_OLLAMA_URL,
                "Focused (up to 300 chunks)",
                False,
                True,
                True,
                "Automatic",
                0,
                0,
                750,
                0,
                app.SEGMENT_PASSAGES_LABEL,
                "",
                "",
                "auto",
                True,
                True,
                768,
                128,
                True,
                True,
                False,
            )
        finally:
            app.validate_pdf_inputs = original_validate
            app.native_upload_readiness_report = original_readiness
            app.execute_preparation = original_execute
            app.legacy_summary_from_run = original_legacy
            app.apply_recommended_anythingllm_settings = original_apply

        self.assertEqual(calls["readiness"], 0)
        self.assertEqual(calls["apply"], 0)

    def test_prepare_pdf_automatic_backend_activates_unstructured_fallback_when_default_backend_fails(self):
        original_get_backend_pages = pipeline.get_backend_pages
        try:
            backend_calls = []

            def fake_get_backend_pages(pdf_path, backend, unstructured_strategy, **_kwargs):
                backend_calls.append(backend)
                base_pages = [{"page": 1, "text": "Introduction\n\n" + ("Body prose. " * 120), "kind": "page"}]
                if backend == "pymupdf":
                    return base_pages, 1, []
                if backend == "pymupdf4llm":
                    raise RuntimeError("PyMuPDF4LLM is not installed.")
                if backend == "unstructured":
                    return (
                        [{"page": 1, "text": "Introduction\n\n" + ("Body prose. " * 120), "kind": "unstructured_elements"}],
                        1,
                        [{"element_index": 1, "pdf_page": 1, "category": "NarrativeText", "chars": 120, "preview": "Body prose"}],
                    )
                raise AssertionError(f"unexpected backend {backend}")

            pipeline.get_backend_pages = fake_get_backend_pages

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                pdf_path = root / "sample.pdf"
                doc = fitz.open()
                page = doc.new_page()
                page.insert_textbox(
                    fitz.Rect(60, 60, 540, 760),
                    "Introduction\n\n" + ("Distinctive prose for fallback validation. " * 80),
                    fontsize=10,
                )
                doc.save(pdf_path)
                doc.close()

                args = SimpleNamespace(
                    document_label="",
                    document_author="",
                    document_short_label="",
                    use_file_title_fallback=True,
                    deep_extraction=False,
                    include_front_matter=True,
                    include_back_matter=True,
                    backend_mode="automatic",
                    first_page_override=0,
                    end_page_override=0,
                    target_passage_length=500,
                    segment_mode="page_limit",
                    end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
                    validation_phrases=[],
                    unstructured_strategy="fast",
                    marker_style="short",
                    disable_inline_markers=False,
                    run_vector_eval=False,
                    ollama_model="bge-m3:latest",
                    ollama_url="http://127.0.0.1:11434/api/embed",
                    max_vector_probes=4,
                    prepare_and_upload=False,
                    anythingllm_api_url="",
                    anythingllm_api_key="",
                    workspace_slug="",
                    test_workspace_slug="test",
                    upload_limit=0,
                    anythingllm_storage_dir=str(root / "missing-storage"),
                    anythingllm_chunk_size=400,
                    anythingllm_chunk_overlap=40,
                )
                pipeline.prepare_pdf(pdf_path, root / "output", args)

            self.assertEqual(backend_calls[:2], ["pymupdf", "pymupdf4llm"])
            self.assertIn("unstructured", backend_calls)
        finally:
            pipeline.get_backend_pages = original_get_backend_pages

    def test_prepare_pdf_automatic_backend_activates_unstructured_when_default_backends_produce_no_usable_segments(self):
        original_get_backend_pages = pipeline.get_backend_pages
        try:
            backend_calls = []

            def fake_get_backend_pages(pdf_path, backend, unstructured_strategy, **_kwargs):
                backend_calls.append(backend)
                if backend in {"pymupdf", "pymupdf4llm"}:
                    return ([{"page": 1, "text": "", "kind": "page"}], 1, [])
                if backend == "unstructured":
                    return (
                        [{"page": 1, "text": "Recovered OCR prose. " * 80, "kind": "unstructured_elements"}],
                        1,
                        [{"element_index": 1, "pdf_page": 1, "category": "NarrativeText", "chars": 120, "preview": "Recovered OCR prose"}],
                    )
                raise AssertionError(f"unexpected backend {backend}")

            pipeline.get_backend_pages = fake_get_backend_pages

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                pdf_path = root / "sample.pdf"
                doc = fitz.open()
                page = doc.new_page()
                page.insert_textbox(
                    fitz.Rect(60, 60, 540, 760),
                    "Placeholder",
                    fontsize=10,
                )
                doc.save(pdf_path)
                doc.close()

                args = SimpleNamespace(
                    document_label="",
                    document_author="",
                    document_short_label="",
                    use_file_title_fallback=True,
                    deep_extraction=False,
                    include_front_matter=True,
                    include_back_matter=True,
                    backend_mode="automatic",
                    first_page_override=0,
                    end_page_override=0,
                    target_passage_length=500,
                    segment_mode="page_limit",
                    end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
                    validation_phrases=[],
                    unstructured_strategy="auto",
                    marker_style="short",
                    disable_inline_markers=False,
                    run_vector_eval=False,
                    ollama_model="bge-m3:latest",
                    ollama_url="http://127.0.0.1:11434/api/embed",
                    max_vector_probes=4,
                    prepare_and_upload=False,
                    anythingllm_api_url="",
                    anythingllm_api_key="",
                    workspace_slug="",
                    test_workspace_slug="test",
                    upload_limit=0,
                    anythingllm_storage_dir=str(root / "missing-storage"),
                    anythingllm_chunk_size=400,
                    anythingllm_chunk_overlap=40,
                )
                result = pipeline.prepare_pdf(pdf_path, root / "output", args)
                profile = json.loads((root / "output" / "source-profile.json").read_text(encoding="utf-8"))

            self.assertEqual(result["selected_backend"], "unstructured")
            self.assertIn("unstructured", backend_calls)
            self.assertTrue(profile["unstructured_runtime"]["used_for_selected_output"])
            self.assertEqual(profile["unstructured_runtime"]["selected_strategy"], "hi_res")
            self.assertEqual(
                profile["unstructured_runtime"]["selected_strategy_reason"],
                "ocr_enabled_for_difficult_pdf",
            )
        finally:
            pipeline.get_backend_pages = original_get_backend_pages

    def test_resolve_unstructured_strategy_prefers_hi_res_for_scanned_like_candidates_when_ocr_is_available(self):
        original_runtime_status = pipeline.unstructured_runtime_status
        try:
            pipeline.unstructured_runtime_status = lambda strategy="fast": {
                "backend_available": True,
                "backend_resolution": "resolved_via_optional_search_paths",
                "ocr_required": strategy in {"hi_res", "ocr_only"},
                "tesseract_available": True,
                "tesseract_executable": "C:/Program Files/Tesseract-OCR/tesseract.exe",
                "tessdata_prefix": "C:/Program Files/Tesseract-OCR/tessdata",
            }
            result = pipeline.resolve_unstructured_strategy(
                "auto",
                prior_candidates=[
                    {
                        "quality": {"scanned_likelihood": "high", "included_words": 120},
                        "error": "",
                    }
                ],
            )
        finally:
            pipeline.unstructured_runtime_status = original_runtime_status

        self.assertEqual(result["resolved"], "hi_res")
        self.assertEqual(result["reason"], "ocr_enabled_for_difficult_pdf")

    def test_sparse_or_mixed_pages_do_not_trigger_document_wide_hi_res_ocr(self):
        runtime_probe = {
            "backend_available": True,
            "tesseract_available": True,
        }
        candidates = [
            {"quality": {"scanned_likelihood": "possible", "included_words": 5_356}, "error": ""},
            {"quality": {"scanned_likelihood": "possible", "included_words": 5_401}, "error": ""},
        ]

        self.assertFalse(pipeline.has_document_wide_ocr_evidence(candidates))
        result = pipeline.resolve_unstructured_strategy(
            "auto", prior_candidates=candidates, runtime_probe=runtime_probe
        )

        self.assertEqual(result["resolved"], "fast")
        self.assertEqual(result["reason"], "fast_sufficient_for_text_pdf")

    def test_complete_native_text_candidate_suppresses_coverage_only_ocr_tiebreaker(self):
        candidates = [
            {
                "backend": "pymupdf",
                "segments": [{"id": "short"}],
                "quality": {
                    "included_pages": 3,
                    "included_words": 723,
                    "empty_pages": 0,
                    "scanned_likelihood": "possible",
                },
                "native_chunk_eval": {"status": "pass"},
            },
            {
                "backend": "pymupdf4llm",
                "segments": [{"id": "full"}],
                "quality": {
                    "included_pages": 12,
                    "included_words": 6076,
                    "empty_pages": 0,
                    "scanned_likelihood": "low",
                },
                "native_chunk_eval": {"status": "pass"},
            },
        ]
        self.assertTrue(pipeline.has_complete_native_text_candidate(candidates, 12))
        self.assertFalse(pipeline.has_complete_native_text_candidate(candidates, 18))

    def test_complete_native_text_candidate_accepts_blank_front_pages_without_ocr(self):
        candidate = {
            "backend": "pymupdf",
            "segments": [{"id": "complete"}],
            "quality": {
                "included_pages": 226,
                "included_words": 79_665,
                "empty_pages": 6,
                "scanned_likelihood": "low",
            },
            "native_chunk_eval": {"status": "pass"},
        }
        self.assertTrue(pipeline.has_complete_native_text_candidate([candidate], 228))

    def test_complete_native_text_candidate_skips_layout_ocr_for_sparse_image_pages(self):
        candidate = {
            "segments": [{"id": "native"}],
            "quality": {
                "included_pages": 26,
                "included_words": 5_356,
                "empty_pages": 7,
                "average_words_per_page": 206.0,
                "scanned_likelihood": "possible",
            },
            "native_chunk_eval": {"status": "pass"},
        }
        preflight = {
            "full_native_text_coverage": {
                "status": "verified",
                "image_backed_low_text_pages": [{"page": 3}, {"page": 24}, {"page": 26}],
            }
        }

        self.assertTrue(
            pipeline.has_complete_native_text_candidate([candidate], 26, preflight)
        )
        self.assertFalse(pipeline.has_complete_native_text_candidate([candidate], 26))

    def test_vector_observation_marks_expansion_as_rechunked(self):
        self.assertEqual(
            pipeline.format_vector_observation(99, 98, "pass"),
            "98 records → 99 vectors observed (re-chunked; pass)",
        )
        self.assertEqual(
            pipeline.format_vector_observation(5, 5, "pass"),
            "5/5 vectors observed (pass)",
        )

    def test_resolve_unstructured_strategy_accepts_a_run_scoped_runtime_probe(self):
        runtime_probe = {
            "backend_available": True,
            "backend_resolution": "venv",
            "tesseract_available": True,
            "tesseract_executable": "C:/Program Files/Tesseract-OCR/tesseract.exe",
        }
        original_runtime_status = pipeline.unstructured_runtime_status
        try:
            pipeline.unstructured_runtime_status = lambda *args, **kwargs: self.fail(
                "a supplied run-scoped probe must be reused"
            )
            result = pipeline.resolve_unstructured_strategy(
                "auto",
                prior_candidates=[{"quality": {"scanned_likelihood": "high"}, "error": ""}],
                runtime_probe=runtime_probe,
            )
        finally:
            pipeline.unstructured_runtime_status = original_runtime_status

        self.assertEqual(result["resolved"], "hi_res")
        self.assertEqual(result["runtime"]["backend_resolution"], "venv")

    def test_inline_marker_loss_is_informational_when_native_metadata_is_primary(self):
        diagnostics = pipeline.build_run_diagnostics(
            profile={"needs_password": False},
            selected={
                "quality": {},
                "outline_validation": {"reliability": "ok"},
                "detected_end_page": 1,
                "include_back_matter": False,
                "vector_validation_status": "not_run_extraction_only",
                "fallback_marker_status": "warning",
                "inline_fallback_required": False,
            },
            candidates=[],
            storage_report={"status": "complete"},
            upload_report={"status": "skipped_prepare_only"},
            workspace_gate={"status": "not_checked", "message": ""},
            post_upload_report={"status": "not_checked"},
            metadata_schema_report={"runtime_api_status": "not_checked"},
            runtime_validation_report={"status": "not_run"},
            temporary_workspace_validation={"status": "not_run"},
        )

        marker_row = next(row for row in diagnostics if row["code"] == "INLINE_FALLBACK_MARKER_LOSS")
        self.assertEqual(marker_row["severity"], "info")

    def test_detected_metadata_preview_includes_text_layer_profile_rows(self):
        import rag_pdf_gradio_app as app

        original_pdf_metadata = app.pdf_metadata
        original_infer_author = app.infer_author_from_pdf_text
        original_text_layer_preview = app.metadata_text_layer_preview
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_pdf = Path(tmpdir) / "sample.pdf"
            fake_pdf.write_bytes(b"%PDF-1.4\n")
            try:
                app.pdf_metadata = lambda path: {
                    "title": "Sample Title",
                    "author": "",
                    "outline": [],
                    "pdf_page_count": 12,
                    "subject": "",
                    "keywords": "",
                    "creator": "",
                    "producer": "",
                    "creationDate": "",
                    "modDate": "",
                    "needs_password": False,
                    "is_encrypted": False,
                }
                app.infer_author_from_pdf_text = lambda path, title_hint="": {
                    "author": "Sample Author",
                    "source": "page_scan",
                    "page": 1,
                    "evidence": "Sample Author",
                }
                app.metadata_text_layer_preview = lambda path: {
                    "status": "ok",
                    "quality": {
                        "scanned_likelihood": "possible",
                        "included_words": 1200,
                        "included_pages": 12,
                        "empty_pages": 1,
                        "image_heavy_low_text_pages": 2,
                    },
                    "sample": "Example extracted text from the PDF body.",
                    "sample_page": 2,
                }
                _, _, _, html_preview, accordion_update = app.detected_metadata_preview([str(fake_pdf)])
            finally:
                app.pdf_metadata = original_pdf_metadata
                app.infer_author_from_pdf_text = original_infer_author
                app.metadata_text_layer_preview = original_text_layer_preview

        self.assertIn("Text-layer check", html_preview)
        self.assertIn("OCR / scanned risk", html_preview)
        self.assertIn("Sample extracted text", html_preview)
        self.assertIn("Possible scanned or low-text PDF", html_preview)
        self.assertTrue(accordion_update["open"])

    def test_download_files_update_can_package_current_outputs_as_zip(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            file_a = root / "a.txt"
            nested = root / "nested"
            nested.mkdir()
            file_b = nested / "b.json"
            segment_zip = root / "manual-segment-files.zip"
            file_a.write_text("alpha", encoding="utf-8")
            file_b.write_text("beta", encoding="utf-8")
            with zipfile.ZipFile(segment_zip, "w") as archive:
                archive.writestr("segment.txt", "segment")

            update = app.download_files_update([str(file_a), str(file_b), str(segment_zip)])
            values = update["value"]
            self.assertEqual(len(values), 1)
            bundle = Path(values[0])
            self.assertTrue(bundle.exists())
            with zipfile.ZipFile(bundle, "r") as archive:
                names = set(archive.namelist())
            self.assertIn("a.txt", names)
            self.assertIn("nested/b.json", names)
            self.assertIn("manual-segment-files.zip", names)

            segment_only = app.download_files_update([str(file_a), str(segment_zip)], False, True)["value"]
            self.assertEqual(segment_only, [str(segment_zip)])

            both = app.download_files_update([str(file_a), str(segment_zip)], True, True)["value"]
            self.assertEqual(len(both), 2)
            self.assertIn(str(segment_zip), both)

            individual = app.download_files_update([str(file_a), str(segment_zip)], False, False)["value"]
            self.assertEqual(individual, [str(file_a), str(segment_zip)])

    def test_downloads_outside_gradio_safe_roots_are_copied_to_the_download_cache(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "outside-output.txt"
            source.write_text("prepared text", encoding="utf-8")
            original = app.is_gradio_safe_download_path
            try:
                app.is_gradio_safe_download_path = lambda _path: False
                update = app.download_files_update([str(source)], False, False)
            finally:
                app.is_gradio_safe_download_path = original

        cached = Path(update["value"][0])
        self.assertTrue(cached.is_file())
        self.assertEqual(cached.parent, app.GRADIO_DOWNLOAD_CACHE_DIR)
        self.assertEqual(cached.read_text(encoding="utf-8"), "prepared text")

    def test_prepared_text_download_recovers_from_a_compact_run_summary(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = root / "selected"
            selected.mkdir()
            parsed = selected / "prepared.txt"
            parsed.write_text("prepared text", encoding="utf-8")
            (root / "run-summary.json").write_text(
                json.dumps({"artifacts": {"parsed_text": "selected/prepared.txt"}}),
                encoding="utf-8",
            )
            paths = app.primary_prepared_download_paths(
                [{"output_root": str(root), "upload_file": str(root / "removed-upload-copy.txt")}]
            )

        self.assertEqual(paths, [str(parsed)])

    def test_cancel_uses_a_durable_marker_when_control_and_worker_do_not_share_memory(self):
        import rag_pdf_gradio_app as app

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            run_root = output_root / "app-run-current"
            run_root.mkdir()
            (run_root / "run-progress.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "run_root": str(run_root),
                        "expected_seconds": 90,
                        "confirmed_fraction": 0.4,
                    }
                ),
                encoding="utf-8",
            )
            original_root = app.AUTO_OUTPUT_DIR
            original_live = app.LIVE_AUTOMATIC_RUN_STATUS
            try:
                app.AUTO_OUTPUT_DIR = output_root
                app.LIVE_AUTOMATIC_RUN_STATUS = {}
                app.cancel_or_reset_automatic_run(
                    run_activity_html='<div data-run-state="running"></div>'
                )
                self.assertTrue((run_root / app.AUTOMATIC_RUN_CANCELLATION_MARKER).is_file())
                self.assertTrue(app.automatic_run_cancellation_requested(run_root))
            finally:
                app.AUTO_OUTPUT_DIR = original_root
                app.LIVE_AUTOMATIC_RUN_STATUS = original_live
                app.CANCELLED_AUTOMATIC_RUN_ROOTS.discard(str(run_root))

    def test_cancellation_recovery_records_a_frozen_checkpoint_without_claiming_remote_stop(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "cancelled-run"
            run_root.mkdir()
            try:
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "run_root": str(run_root),
                    "phase": "Submitting AnythingLLM batch 2",
                    "confirmed_fraction": 0.387,
                }
                recovery = app.write_automatic_cancellation_recovery(run_root, "example.pdf")
                record = json.loads(Path(recovery).read_text(encoding="utf-8"))
            finally:
                app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(record["checkpoint"]["confirmed_percent"], 38.7)
        self.assertEqual(record["checkpoint"]["phase"], "Submitting AnythingLLM batch 2")
        self.assertIn("already accepted", record["anythingllm_result"])

    def test_cancellation_recovery_serializes_the_owned_worker_marker(self):
        import rag_pdf_gradio_app as app

        original_status = app.LIVE_AUTOMATIC_RUN_STATUS
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir) / "cancelled-run"
            run_root.mkdir()
            marker = run_root / app.AUTOMATIC_RUN_WORKER_MARKER
            marker.write_text(
                json.dumps({
                    "kind": "automatic-preparation-worker",
                    "pid": 24680,
                    "run_root": str(run_root),
                }),
                encoding="utf-8",
            )
            try:
                app.LIVE_AUTOMATIC_RUN_STATUS = {
                    "run_root": str(run_root),
                    "confirmed_fraction": 0.2,
                }
                worker = app.active_automatic_run_worker(run_root)
                recovery = app.write_automatic_cancellation_recovery(
                    run_root, "example.pdf", worker
                )
                record = json.loads(Path(recovery).read_text(encoding="utf-8"))
            finally:
                app.LIVE_AUTOMATIC_RUN_STATUS = original_status

        self.assertEqual(record["worker"]["marker"], str(marker))

    def test_target_passage_length_control_update_changes_by_segment_mode(self):
        import rag_pdf_gradio_app as app

        passages = app.target_passage_length_control_update("AnythingLLM-parity subchunking", 750)
        page_preserving = app.target_passage_length_control_update(app.SEGMENT_PAGE_LIMIT_LABEL, 750)
        page_passages = app.target_passage_length_control_update(app.SEGMENT_PAGE_PASSAGES_LABEL, 750)
        whole_page = app.target_passage_length_control_update("Whole-page chunks", 750)
        unsegmented = app.target_passage_length_control_update(app.SEGMENT_NONE_LABEL, 750)

        self.assertTrue(passages["interactive"])
        self.assertEqual(passages["label"], "Target passage length")
        self.assertIn("passage-style", passages["info"])
        self.assertEqual(passages["value"], "750")
        self.assertFalse(page_preserving["interactive"])
        self.assertIn("keeps each page intact", page_preserving["info"])
        self.assertEqual(page_preserving["value"], "750")
        self.assertTrue(page_passages["interactive"])
        self.assertEqual(page_passages["label"], "Target subchunk length within each page")
        self.assertIn("without crossing", page_passages["info"])
        self.assertEqual(page_passages["value"], "750")
        self.assertFalse(whole_page["interactive"])
        self.assertIn("ignore this setting", whole_page["info"])
        self.assertEqual(whole_page["value"], "750")
        self.assertFalse(unsegmented["interactive"])
        self.assertIn("No local segmentation", unsegmented["info"])

    def test_inherited_target_length_follows_mode_and_uses_conservative_embedder_guard(self):
        import rag_pdf_gradio_app as app

        state = {
            "chunking": {"chunk_size": 768, "chunk_overlap": 128},
            "embedder": {
                "effective_model": "short-context-test",
                "policy": {"recommended_limit": 256},
            },
        }
        inherited = app.target_passage_sizing_plan(
            app.SEGMENT_PAGE_LIMIT_LABEL,
            app.TARGET_PASSAGE_INHERIT_LABEL,
            1000,
            True,
            0,
            0,
            resolved_state=state,
        )
        self.assertTrue(inherited["inherited"])
        self.assertTrue(inherited["page_preserving"])
        self.assertFalse(inherited["page_bounded"])
        self.assertEqual(inherited["resolved_target"], 750)
        self.assertEqual(inherited["estimated_embedder_character_budget"], 768)

        custom = app.target_passage_sizing_plan(
            app.SEGMENT_PASSAGES_LABEL,
            app.TARGET_PASSAGE_CUSTOM_LABEL,
            1000,
            True,
            0,
            0,
            resolved_state=state,
        )
        self.assertEqual(custom["resolved_target"], 1000)
        self.assertTrue(custom["exceeds_splitter"])
        self.assertTrue(custom["exceeds_embedder_estimate"])
        self.assertIn("Target-length warning", app.target_passage_length_warning_html(custom))

    def test_whole_page_target_policy_explicitly_ignores_target(self):
        import rag_pdf_gradio_app as app

        plan = app.target_passage_sizing_plan(
            app.SEGMENT_PAGE_ONLY_LABEL,
            app.TARGET_PASSAGE_INHERIT_LABEL,
            750,
            False,
            768,
            0,
            resolved_state={"chunking": {}, "embedder": {"policy": {"recommended_limit": 8192}}},
        )
        self.assertTrue(plan["whole_page"])
        self.assertIn("intentionally ignored", app.target_passage_length_warning_html(plan))

    def test_page_preserve_ceiling_is_bounded_by_live_splitter_and_custom_ranges_are_explicit(self):
        import rag_pdf_gradio_app as app
        from auto_anythingllm_pipeline import select_upload_payloads

        plan = app.target_passage_sizing_plan(
            app.SEGMENT_PAGE_LIMIT_LABEL,
            app.TARGET_PASSAGE_CUSTOM_LABEL,
            750,
            True,
            8191,
            0,
            resolved_state={"chunking": {"chunk_size": 8191}, "embedder": {"policy": {}}},
            page_preserve_ceiling=1200,
        )
        self.assertEqual(plan["page_preserve_effective_ceiling"], 1200)
        self.assertIn("1200", app.target_passage_length_warning_html(plan))
        self.assertEqual(app.parse_native_upload_custom_range("1-3, 4, 9, 12-14"), (1, 2, 3, 4, 9, 12, 13, 14))
        self.assertEqual(select_upload_payloads(list("abcdef"), upload_indices=(1, 3, 6)), ["a", "c", "f"])
        with self.assertRaises(ValueError):
            app.parse_native_upload_custom_range("3-1")
        with self.assertRaises(ValueError):
            select_upload_payloads(list("abc"), upload_indices=(4,))

    def test_custom_range_control_is_editable_only_for_one_pdf_and_confirmation_keeps_reason(self):
        import rag_pdf_gradio_app as app

        single_scope, single_range = app.native_upload_scope_batch_guard(
            app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL,
            ["C:/single.pdf"],
            [],
            app.SEGMENT_PAGE_LIMIT_LABEL,
        )
        self.assertEqual(single_scope["choices"], [
            app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
            app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL,
        ])
        self.assertEqual(single_scope["value"], app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL)
        self.assertEqual(single_range["value"], "")
        self.assertTrue(single_range["visible"])
        self.assertTrue(single_range["interactive"])

        batch_scope, batch_range = app.native_upload_scope_batch_guard(
            app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL,
            ["C:/one.pdf", "C:/two.pdf"],
            [],
            app.SEGMENT_PAGE_LIMIT_LABEL,
        )
        self.assertEqual(batch_scope["choices"], [app.NATIVE_UPLOAD_SCOPE_ALL_LABEL])
        self.assertEqual(batch_scope["value"], app.NATIVE_UPLOAD_SCOPE_ALL_LABEL)
        self.assertEqual(batch_range["value"], "")
        self.assertFalse(batch_range["visible"])
        self.assertFalse(batch_range["interactive"])

        unsupported_scope, unsupported_range = app.native_upload_scope_batch_guard(
            app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL,
            ["C:/single.pdf"],
            [],
            app.SEGMENT_PASSAGES_LABEL,
        )
        self.assertEqual(unsupported_scope["choices"], [app.NATIVE_UPLOAD_SCOPE_ALL_LABEL])
        self.assertEqual(unsupported_scope["value"], app.NATIVE_UPLOAD_SCOPE_ALL_LABEL)
        self.assertEqual(unsupported_range["value"], "")
        self.assertFalse(unsupported_range["visible"])
        self.assertFalse(unsupported_range["interactive"])

        self.assertTrue(app.native_upload_custom_range_supported(app.SEGMENT_PAGE_ONLY_LABEL))
        self.assertTrue(app.native_upload_custom_range_supported(app.SEGMENT_PAGE_LIMIT_LABEL))
        self.assertFalse(app.native_upload_custom_range_supported(app.SEGMENT_PASSAGES_LABEL))
        self.assertFalse(app.native_upload_custom_range_supported(app.SEGMENT_PAGE_PASSAGES_LABEL))
        self.assertFalse(app.native_upload_custom_range_supported(app.SEGMENT_NONE_LABEL))

        original_validate = app.validated_automatic_run_settings
        try:
            report = app.app_error_report(
                "AUTO-NATIVE-RANGE-001",
                "Custom range is available for one PDF only",
                ["For a batch, choose All segments instead."],
            )
            app.validated_automatic_run_settings = lambda values: ({}, report, [], False)
            result = app.run_automatic_from_confirmation(*([None] * len(app.AUTOMATIC_RUN_FIELDS)))
        finally:
            app.validated_automatic_run_settings = original_validate
        self.assertIn("AUTO-NATIVE-RANGE-001", result[0]["value"])
        self.assertIn("Custom range is available for one PDF only", result[0]["value"])
        self.assertIn("For a batch, choose All segments instead.", result[0]["value"])

    def test_custom_range_batch_is_rejected_before_ocr_preflight(self):
        import rag_pdf_gradio_app as app

        values = app.fresh_automatic_run_setting_values(
            ["C:/one.pdf", "C:/two.pdf"],
            [],
        )
        values["native_upload_scope"] = app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
        values["native_upload_custom_range"] = "1-3"
        ordered_values = [values[field] for field in app.AUTOMATIC_RUN_FIELDS]

        original_validate = app.validate_pdf_inputs
        original_preflight = app.automatic_ocr_preflight_manifest
        try:
            app.validate_pdf_inputs = lambda _files: (["C:/one.pdf", "C:/two.pdf"], None)

            def fail_if_called(*_args, **_kwargs):
                self.fail("batch Custom Range must be rejected before OCR preflight")

            app.automatic_ocr_preflight_manifest = fail_if_called
            _settings, report, warnings, allowed = app.validated_automatic_run_settings(ordered_values)
        finally:
            app.validate_pdf_inputs = original_validate
            app.automatic_ocr_preflight_manifest = original_preflight

        self.assertFalse(allowed)
        self.assertEqual(warnings, [])
        self.assertIn("AUTO-NATIVE-RANGE-001", report)
        self.assertIn("Custom range is available for one PDF only", report)

    def test_custom_range_passage_mode_is_rejected_before_ocr_preflight(self):
        import rag_pdf_gradio_app as app

        values = app.fresh_automatic_run_setting_values(["C:/one.pdf"], [])
        values["native_upload_scope"] = app.NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
        values["native_upload_custom_range"] = "1-3"
        values["segment_mode"] = app.SEGMENT_PASSAGES_LABEL
        ordered_values = [values[field] for field in app.AUTOMATIC_RUN_FIELDS]

        original_validate = app.validate_pdf_inputs
        original_preflight = app.automatic_ocr_preflight_manifest
        try:
            app.validate_pdf_inputs = lambda _files: (["C:/one.pdf"], None)

            def fail_if_called(*_args, **_kwargs):
                self.fail("unsupported Custom Range must be rejected before OCR preflight")

            app.automatic_ocr_preflight_manifest = fail_if_called
            _settings, report, warnings, allowed = app.validated_automatic_run_settings(ordered_values)
        finally:
            app.validate_pdf_inputs = original_validate
            app.automatic_ocr_preflight_manifest = original_preflight

        self.assertFalse(allowed)
        self.assertEqual(warnings, [])
        self.assertIn("AUTO-NATIVE-RANGE-002", report)
        self.assertIn("Custom range requires a page-based segmentation mode", report)

    def test_no_segmentation_target_policy_and_pipeline_mapping_are_explicit(self):
        import rag_pdf_gradio_app as app

        plan = app.target_passage_sizing_plan(
            app.SEGMENT_NONE_LABEL,
            app.TARGET_PASSAGE_INHERIT_LABEL,
            750,
            True,
            0,
            0,
            resolved_state={"chunking": {"chunk_size": 768}, "embedder": {"policy": {}}},
        )
        self.assertTrue(plan["unsegmented"])
        self.assertTrue(plan["target_ignored"])
        self.assertIn("one content file per PDF", app.target_passage_length_warning_html(plan))
        self.assertEqual(app.pipeline_segment_mode(app.SEGMENT_NONE_LABEL), "none")
        self.assertEqual(app.pipeline_segment_mode("none"), "none")
        self.assertEqual(app.pipeline_segment_mode(app.SEGMENT_PAGE_LIMIT_LABEL), "page_limit")
        self.assertEqual(app.pipeline_segment_mode(app.SEGMENT_PAGE_PASSAGES_LABEL), "page_passages")
        self.assertEqual(app.pipeline_segment_mode("4-page chunks"), "page_limit")

        segment_components = [
            component.get("props", {})
            for component in app.demo.config["components"]
            if component.get("props", {}).get("label") == "Segmentation mode"
        ]
        # Automatic and Advanced diagnostics intentionally expose the same
        # segmentation modes.  A second control is no longer a duplicate
        # implementation with a divergent policy.
        self.assertEqual(len(segment_components), 2)
        for component in segment_components:
            self.assertIn(
                (app.SEGMENT_NONE_LABEL, app.SEGMENT_NONE_LABEL),
                component.get("choices", []),
            )
            self.assertIn(
                (app.SEGMENT_PAGE_PASSAGES_LABEL, app.SEGMENT_PAGE_PASSAGES_LABEL),
                component.get("choices", []),
            )
            self.assertNotIn(("4-page chunks", "4-page chunks"), component.get("choices", []))

    def test_extraction_backend_help_matches_backend_behavior(self):
        import rag_pdf_gradio_app as app

        automatic_html = app.extraction_backend_help("Automatic")
        pymupdf_html = app.extraction_backend_help("PyMuPDF")
        pymupdf4llm_html = app.extraction_backend_help("PyMuPDF4LLM")
        unstructured_html = app.extraction_backend_help("Unstructured")

        self.assertIn("lighter local extraction paths", automatic_html)
        self.assertIn("plain text", pymupdf_html)
        self.assertIn("Markdown-like text", pymupdf4llm_html)
        self.assertIn("layout-aware elements", unstructured_html)

    def test_ollama_provider_catalog_entries_include_runtime_embedding_models(self):
        import embedder_capabilities as caps
        import rag_pdf_gradio_app as app

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "models": [
                            {"name": "bge-m3:latest"},
                            {"name": "embeddinggemma:latest"},
                            {"name": "gemma3n:e2b"},
                        ]
                    }
                ).encode("utf-8")

        original_urlopen = caps.urllib.request.urlopen
        original_inspect = caps.inspect_ollama_model
        try:
            caps._ollama_runtime_catalog_cached.cache_clear()

            def fake_urlopen(req, timeout=0):
                return FakeResponse()

            caps.urllib.request.urlopen = fake_urlopen
            caps.inspect_ollama_model = lambda model: {
                "status": "loaded",
                "model": model,
                "context_length": 8192 if "bge-m3" in model else 2048,
                "embedding_length": 1024 if "bge-m3" in model else 768,
                "architecture": "embedding",
                "capabilities": ["embedding"],
                "error": "",
            }
            rows = caps.provider_catalog_entries("ollama", force_refresh=True)
            choices = app.anythingllm_embedder_model_choices("ollama", "embeddinggemma:latest")
        finally:
            caps.urllib.request.urlopen = original_urlopen
            caps.inspect_ollama_model = original_inspect
            caps._ollama_runtime_catalog_cached.cache_clear()

        models = [row.get("model") for row in rows]
        self.assertIn("bge-m3:latest", models)
        self.assertIn("embeddinggemma:latest", models)
        self.assertNotIn("gemma3n:e2b", models)
        self.assertIn("embeddinggemma:latest", choices)
        self.assertIn("bge-m3:latest", choices)

    def test_simulation_status_mentions_non_ollama_provider_cards(self):
        import embedder_capabilities as caps
        import rag_pdf_gradio_app as app

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "models": [
                            {"name": "embeddinggemma:latest"},
                            {"name": "bge-m3:latest"},
                            {"name": "gemma3n:e2b"},
                        ]
                    }
                ).encode("utf-8")

        original_default_storage = app.default_anythingllm_storage_dir
        original_embed_config = app.anythingllm_embedding_config
        original_app_urlopen = app.urllib.request.urlopen
        original_caps_urlopen = caps.urllib.request.urlopen
        original_inspect = caps.inspect_ollama_model
        try:
            app.default_anythingllm_storage_dir = lambda: Path("C:/fake-storage")
            app.anythingllm_embedding_config = lambda _storage: {
                "engine": "anythingllm",
                "normalized_engine": "anythingllm",
                "model": "all-MiniLM-L6-v2",
                "effective_model": "all-MiniLM-L6-v2",
                "anomalies": [],
            }

            def fake_urlopen(req, timeout=0):
                return FakeResponse()

            caps._ollama_runtime_catalog_cached.cache_clear()
            app.urllib.request.urlopen = fake_urlopen
            caps.urllib.request.urlopen = fake_urlopen
            caps.inspect_ollama_model = lambda model: {
                "status": "loaded",
                "model": model,
                "context_length": 8192 if "bge-m3" in model else 2048,
                "embedding_length": 1024 if "bge-m3" in model else 768,
                "architecture": "embedding",
                "capabilities": ["embedding"],
                "error": "",
            }
            _choices, _value, status = app.ollama_model_choices("http://127.0.0.1:11434", app.simulation_default_choice_label())
        finally:
            app.default_anythingllm_storage_dir = original_default_storage
            app.anythingllm_embedding_config = original_embed_config
            app.urllib.request.urlopen = original_app_urlopen
            caps.urllib.request.urlopen = original_caps_urlopen
            caps.inspect_ollama_model = original_inspect
            caps._ollama_runtime_catalog_cached.cache_clear()

        self.assertIn("embedding-oriented Ollama model", status)
        self.assertIn("omitted", status)

    def test_gradio_preview_embedder_policy_uses_model_aware_limit(self):
        import rag_pdf_gradio_app as app

        max_update, recommended_update, status_text = app.preview_anythingllm_embedder_policy(
            "openrouter",
            "baai/bge-m3",
            0,
        )
        self.assertEqual(max_update["value"], 8192)
        self.assertEqual(recommended_update["value"], 8192)
        self.assertIn("baai/bge-m3", status_text)
        self.assertIn("8192", status_text)

    def test_page_segment_marker_detection_handles_filename_underscores(self):
        self.assertTrue(
            pipeline.text_contains_page_or_segment_metadata(
                "Reference Work__pdf_hash_p0025_s00001__Introduction.txt"
            )
        )
        self.assertFalse(
            pipeline.text_contains_page_or_segment_metadata(
                "Reference Work Introduction and chapter context"
            )
        )

    def test_runtime_native_validation_requires_deepseek_and_checks_citation(self):
        payload = {
            "textContent": "Distinctive passage about rights-bearing individuals.",
            "metadata": {
                "title": "Reference Work | p25 | s00001",
                "chunkSource": "segment://pdf_hash_p0025_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass",
                "chat_provider": "generic-openai",
                "chat_model": "deepseek-v4-pro",
            }

            def fake_post(url, body, api_key=None, timeout_label="request", timeout_seconds=120):
                if url.endswith("/vector-search"):
                    return {
                        "http_status": 200,
                        "data": {
                            "results": [
                                {
                                    "metadata": {
                                        "title": payload["metadata"]["title"],
                                        "chunkSource": payload["metadata"]["chunkSource"],
                                    }
                                }
                            ]
                        },
                        "error": "",
                    }
                return {
                    "http_status": 200,
                    "data": {
                        "textResponse": "The sourceDocument identifies PDF page 25 and segment s00001.",
                        "sources": [],
                    },
                    "error": "",
                }

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001",
                "provided-key",
                "test",
                [payload],
                1,
                Path("unused"),
                include_chat_probe=True,
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["vector_checks"][0]["top_1_expected"])
        self.assertTrue(
            result["chat_check"]["response_contains_expected_page_segment"]
        )

    def test_runtime_native_validation_uses_vector_provenance_without_chat_by_default(self):
        payload = {
            "textContent": "Distinctive passage about page-aware retrieval.",
            "metadata": {
                "title": "Reference Work | p7 | s00001",
                "chunkSource": "segment://pdf_hash_p0007_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}

            def fake_post(url, body, **_kwargs):
                self.assertTrue(url.endswith("/vector-search"))
                return {
                    "http_status": 200,
                    "data": {"results": [{"metadata": dict(payload["metadata"])}]},
                    "error": "",
                }

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", [payload], 1, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe

        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["chat_probe_requested"])
        self.assertEqual(result["chat_check"]["status"], "skipped_not_required")
        self.assertGreaterEqual(result["validation_seconds"], 0.0)
        self.assertGreaterEqual(result["vector_search_seconds"], 0.0)
        self.assertEqual(result["chat_seconds"], 0.0)

    def test_runtime_validation_selects_distinct_body_rich_pages_over_ocr_letterhead(self):
        payloads = [
            {
                "textContent": "EXAMPLE UNIVERSITY CONTACT DETAILS 00000000 00000000 000000",
                "metadata": {"title": "letter p1 s00001", "chunkSource": "segment://letter_p0001_s00001"},
            },
            {
                "textContent": (
                    "This is to certify that the applicant attended the committee meeting and "
                    "submitted the requested academic materials for consideration by the faculty."
                ),
                "metadata": {"title": "letter p1 s00002", "chunkSource": "segment://letter_p0001_s00002"},
            },
            {
                "textContent": (
                    "The committee considered the submitted proposal, discussed the syllabus, and "
                    "recorded its recommendation in the minutes of the meeting."
                ),
                "metadata": {"title": "letter p3 s00003", "chunkSource": "segment://letter_p0003_s00003"},
            },
        ]

        selected = pipeline.select_runtime_validation_payloads(payloads)

        self.assertEqual(len(selected), 2)
        self.assertNotIn(payloads[0], selected)
        self.assertEqual(
            {pipeline.expected_page_segment_tokens(payload)["page_number"] for payload in selected},
            {1, 3},
        )
        self.assertTrue(any(
            "applicant attended" in pipeline.runtime_validation_query_text(payload)
            for payload in selected
        ))

    def test_runtime_validation_query_prefers_body_before_references(self):
        payload = {
            "textContent": (
                "The author argues that public institutions helped the subject achieve success, "
                "but the account does not acknowledge those contributions or propose practical "
                "interventions to improve the lives of affected communities. This conclusion "
                "connects the analysis to the article's central public-policy critique.\n\n"
                "REFERENCES\n"
                "Anderson, M. (2019). A lengthy bibliographic title about unrelated subjects. "
                "Journal of Historical Analysis, 44(2), 120-144. "
                "Bennett, Q. (2020). Another bibliographic title with many distinctive words. "
                "Research Review, 15(7), 201-238."
            ),
            "metadata": {"title": "article p14", "chunkSource": "page-parent://article::pdf-p0014"},
        }

        query = pipeline.runtime_validation_query_text(payload)

        self.assertIn("public-policy critique", query)
        self.assertNotIn("bibliographic title", query)

    def test_runtime_validation_uses_one_probe_for_two_segments_on_the_same_page(self):
        payloads = [
            {
                "textContent": "First distinctive body passage with sufficient semantic content.",
                "metadata": {"title": "Source p1 s00001", "chunkSource": "segment://source_p0001_s00001"},
            },
            {
                "textContent": "Second distinctive body passage from that same physical page.",
                "metadata": {"title": "Source p1 s00002", "chunkSource": "segment://source_p0001_s00002"},
            },
        ]

        selected = pipeline.select_runtime_validation_payloads(payloads)

        self.assertEqual(len(selected), 1)
        self.assertEqual(pipeline.expected_page_segment_tokens(selected[0])["page_number"], 1)

    def test_runtime_native_validation_separates_chat_timeout_from_citation_failure(self):
        payload = {
            "textContent": "Distinctive passage about rights-bearing individuals.",
            "metadata": {
                "title": "Reference Work | p25 | s00001",
                "chunkSource": "segment://pdf_hash_p0025_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }

            def fake_post(url, body, api_key=None, timeout_label="request", timeout_seconds=120):
                if url.endswith("/vector-search"):
                    return {
                        "http_status": 200,
                        "data": {"results": [{"metadata": dict(payload["metadata"])}]},
                        "error": "",
                    }
                return {"http_status": 0, "data": {}, "error": "DeepSeek chat failed: timed out"}

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", [payload], 1, Path("unused"),
                include_chat_probe=True,
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post

        self.assertTrue(result["vector_checks"][0]["top_1_expected"])
        self.assertEqual(result["status"], "pass_with_chat_timeout")

    def test_runtime_native_validation_retries_and_records_transient_vector_timeout(self):
        payload = {
            "textContent": "Distinctive passage about rights-bearing individuals.",
            "metadata": {
                "title": "Reference Work | p25 | s00001",
                "chunkSource": "segment://pdf_hash_p0025_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        original_sleep = pipeline.time.sleep
        vector_attempts = 0
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}
            pipeline.time.sleep = lambda _seconds: None

            def fake_post(url, body, api_key=None, timeout_label="request", timeout_seconds=120):
                nonlocal vector_attempts
                if url.endswith("/vector-search"):
                    vector_attempts += 1
                    if vector_attempts == 1:
                        return {"http_status": None, "data": {}, "error": "vector search failed: timed out", "elapsed_seconds": .1}
                    return {
                        "http_status": 200,
                        "data": {"results": [{"metadata": dict(payload["metadata"])}]},
                        "error": "",
                        "elapsed_seconds": .1,
                    }
                return {
                    "http_status": 200,
                    "data": {"textResponse": "PDF page 25 and segment s00001", "sources": []},
                    "error": "",
                    "elapsed_seconds": .1,
                }

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", [payload], 1, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["status"], "pass")
        self.assertEqual(vector_attempts, 2)
        self.assertEqual(result["vector_checks"][0]["retry_count"], 1)
        self.assertEqual(result["vector_checks"][0]["attempts"][0]["error_class"], "timeout")
        self.assertTrue(result["vector_checks"][0]["endpoint"].endswith("/vector-search"))

    def test_runtime_native_validation_reports_vector_timeout_as_runtime_event(self):
        payload = {
            "textContent": "Distinctive passage about rights-bearing individuals.",
            "metadata": {
                "title": "Reference Work | p25 | s00001",
                "chunkSource": "segment://pdf_hash_p0025_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        original_sleep = pipeline.time.sleep
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}
            pipeline.time.sleep = lambda _seconds: None
            pipeline.post_json_captured = lambda url, body, **kwargs: (
                {"http_status": None, "data": {}, "error": "vector search failed: timed out", "elapsed_seconds": .1}
                if url.endswith("/vector-search")
                else {"http_status": 200, "data": {"textResponse": "PDF page 25 and segment s00001"}, "error": "", "elapsed_seconds": .1}
            )
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", [payload], 1, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["status"], "vector_runtime_timeout")
        self.assertEqual(result["vector_checks"][0]["error_class"], "timeout")
        self.assertEqual(result["vector_checks"][0]["retry_count"], 1)

    def test_runtime_native_validation_classifies_query_embedding_authentication_failure(self):
        payload = {
            "textContent": "Distinctive passage about rights-bearing individuals.",
            "metadata": {
                "title": "Reference Work | p25 | s00001",
                "chunkSource": "segment://pdf_hash_p0025_s00001",
            },
        }
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}
            pipeline.post_json_captured = lambda url, body, **kwargs: (
                {"http_status": 401, "data": {}, "error": "OpenRouter Failed to embed: 401 User not found", "elapsed_seconds": .1}
                if url.endswith("/vector-search")
                else {"http_status": 200, "data": {"textResponse": ""}, "error": "", "elapsed_seconds": .1}
            )
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", [payload], 1, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe

        self.assertEqual(result["status"], "blocked_provider_authentication")
        self.assertIn("401", result["vector_checks"][0]["error"])

    def test_runtime_validation_keeps_partial_live_timeout_reviewable_after_exact_hit(self):
        payloads = [
            {
                "textContent": "Distinctive first passage about rights-bearing individuals.",
                "metadata": {
                    "title": "Reference Work | p25 | s00001",
                    "chunkSource": "segment://pdf_hash_p0025_s00001",
                },
            },
            {
                "textContent": "Distinctive second passage about civic responsibility.",
                "metadata": {
                    "title": "Reference Work | p26 | s00002",
                    "chunkSource": "segment://pdf_hash_p0026_s00002",
                },
            },
        ]
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        original_sleep = pipeline.time.sleep
        vector_calls = 0
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}
            pipeline.time.sleep = lambda _seconds: None

            def fake_post(url, body, **kwargs):
                nonlocal vector_calls
                if url.endswith("/vector-search"):
                    vector_calls += 1
                    if vector_calls <= 2:
                        return {"http_status": None, "data": {}, "error": "vector search failed: timed out", "elapsed_seconds": .1}
                    return {
                        "http_status": 200,
                        "data": {"results": [{"metadata": dict(payloads[1]["metadata"])}]},
                        "error": "",
                        "elapsed_seconds": .1,
                    }
                return {"http_status": 200, "data": {"textResponse": ""}, "error": "", "elapsed_seconds": .1}

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", payloads, 2, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["status"], "pass_with_vector_timeout")
        self.assertFalse(result["vector_checks"][0]["top_1_expected"])
        self.assertTrue(result["vector_checks"][1]["top_1_expected"])

    def test_runtime_validation_reconciles_a_partial_timeout_after_chat_settles(self):
        payloads = [
            {
                "textContent": "Distinctive first passage about rights-bearing individuals.",
                "metadata": {
                    "title": "Reference Work | p25 | s00001",
                    "chunkSource": "segment://pdf_hash_p0025_s00001",
                },
            },
            {
                "textContent": "Distinctive second passage about civic responsibility.",
                "metadata": {
                    "title": "Reference Work | p26 | s00002",
                    "chunkSource": "segment://pdf_hash_p0026_s00002",
                },
            },
        ]
        original_gate = pipeline.read_workspace_model_gate
        original_post = pipeline.post_json_captured
        original_probe = pipeline.verify_anythingllm_runtime_embedder
        original_sleep = pipeline.time.sleep
        vector_calls = 0
        try:
            pipeline.read_workspace_model_gate = lambda *args, **kwargs: {
                "status": "pass", "chat_provider": "generic-openai", "chat_model": "deepseek-v4-pro"
            }
            pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {"status": "pass"}
            pipeline.time.sleep = lambda _seconds: None

            def fake_post(url, body, **kwargs):
                nonlocal vector_calls
                if url.endswith("/vector-search"):
                    vector_calls += 1
                    if vector_calls <= 2:
                        return {"http_status": None, "data": {}, "error": "vector search failed: timed out", "elapsed_seconds": .1}
                    metadata = payloads[1]["metadata"] if vector_calls == 3 else payloads[0]["metadata"]
                    return {
                        "http_status": 200,
                        "data": {"results": [{"metadata": dict(metadata)}]},
                        "error": "",
                        "elapsed_seconds": .1,
                    }
                return {
                    "http_status": 200,
                    "data": {"textResponse": "PDF page 25 and segment s00001", "sources": []},
                    "error": "",
                    "elapsed_seconds": .1,
                }

            pipeline.post_json_captured = fake_post
            result = pipeline.validate_anythingllm_native_runtime(
                "http://127.0.0.1:3001", "provided-key", "test", payloads, 2, Path("unused")
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.post_json_captured = original_post
            pipeline.verify_anythingllm_runtime_embedder = original_probe
            pipeline.time.sleep = original_sleep

        self.assertEqual(vector_calls, 4)
        self.assertEqual(result["vector_recheck_status"], "recovered")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["vector_checks"][-1]["recheck_of"], payloads[0]["metadata"]["chunkSource"])
        self.assertTrue(result["vector_checks"][-1]["expected_in_top_n"])

    def test_upload_readiness_blocks_material_ocr_coverage_disagreement_only_for_ocr(self):
        selected = {
            "backend": "unstructured",
            "unstructured_strategy": "hi_res",
            "readiness_reasons": ["backend_text_coverage_disagreement"],
        }
        self.assertEqual(
            pipeline.upload_block_reason_for_readiness(selected),
            "ocr_backend_text_coverage_disagreement",
        )
        selected["unstructured_strategy"] = "fast"
        self.assertEqual(pipeline.upload_block_reason_for_readiness(selected), "")

    def test_runtime_verification_status_uses_live_embedder_probe_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            (storage / ".env").write_text("EMBEDDING_ENGINE=openrouter\n", encoding="utf-8")
            (storage / "anythingllm.db").write_text("", encoding="utf-8")
            original_probe = pipeline.verify_anythingllm_runtime_embedder
            try:
                pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: {
                    "status": "pass",
                    "message": "AnythingLLM returned embeddings from /api/v1/openai/embeddings (1024 dimensions).",
                    "dimension": 1024,
                }
                result = pipeline.anythingllm_runtime_verification_status(storage)
            finally:
                pipeline.verify_anythingllm_runtime_embedder = original_probe
        self.assertEqual(result["status"], "runtime_verified")
        self.assertEqual(result["probe"]["dimension"], 1024)

    def test_post_upload_verifier_does_not_treat_raw_files_as_searchable_vectors(self):
        payload = {
            "textContent": "Distinctive passage for temporary workspace validation.",
            "metadata": {
                "title": "Oldfield | p1 | s00001",
                "chunkSource": "segment://oldfield_p0001_s00001",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("create table workspaces(id integer primary key, name text, slug text)")
                con.execute(
                    "create table workspace_documents(id integer primary key, workspaceId integer, docId text, filename text, docpath text, metadata text)"
                )
                con.execute("create table document_vectors(id integer primary key, docId text, vectorId text)")
                con.execute("insert into workspaces(id, name, slug) values (1, 'Validation', 'validation-workspace')")
                con.commit()
            finally:
                con.close()
            custom_doc = storage / "documents" / "custom-documents" / "raw-oldfield-p1-s00001.json"
            custom_doc.parent.mkdir(parents=True)
            custom_doc.write_text(
                json.dumps(
                    {
                        "title": payload["metadata"]["title"],
                        "chunkSource": payload["metadata"]["chunkSource"],
                        "text": payload["textContent"],
                    }
                ),
                encoding="utf-8",
            )
            original_native_rows = pipeline.inspect_native_metadata_rows
            original_lancedb_rows = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "matching_table_names": [],
                    "text_contains_segment_or_page": False,
                    "vector_ids": [],
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "validation-workspace",
                    "abc123",
                    [payload],
                    upload_locations=[str(custom_doc)],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native_rows
                pipeline.inspect_lancedb_vector_ids = original_lancedb_rows
        self.assertEqual(result["status"], "docs_without_vectors")
        self.assertEqual(result["classification"], "raw_upload_present_not_embedded")
        self.assertEqual(result["upload_location_matching_files"], 1)

    def test_uploaded_relative_locations_resolve_under_anythingllm_documents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Path(tmpdir)
            relative = Path("custom-documents") / "source-folder" / "segment.json"
            document = storage / "documents" / relative
            document.parent.mkdir(parents=True)
            document.write_text(
                json.dumps({"title": "Example | PDF page 3 | Segment: 2", "text": "distinctive passage"}),
                encoding="utf-8",
            )

            report = pipeline.inspect_uploaded_location_files(
                storage,
                [relative.as_posix()],
                expected_needles=["distinctive passage"],
            )

        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["existing_files"], 1)
        self.assertEqual(report["matching_files"], 1)
        self.assertEqual(report["missing_locations"], 0)

    def test_uploaded_relative_locations_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = pipeline.inspect_uploaded_location_files(
                Path(tmpdir),
                ["../outside.json"],
            )

        self.assertEqual(report["resolved_locations"], 0)
        self.assertEqual(report["rejected_locations"], 1)

    def test_response_page_segment_matching_accepts_zero_padding(self):
        expected = {"page_number": 25, "segment_number": 1}
        self.assertTrue(
            pipeline.response_contains_page_segment(
                "sourceDocument shows p0025 and s00001.",
                expected,
            )
        )

    def test_paragraph_aware_split_preserves_offsets(self):
        text = (
            "First paragraph has a complete sentence and useful context.\n\n"
            "Second paragraph has another complete sentence and more context.\n\n"
            "Third paragraph finishes the example with enough text to split."
        )
        segments = pipeline.split_page_with_offsets(text, target_chars=100, min_boundary=40)
        self.assertGreaterEqual(len(segments), 2)
        for segment in segments:
            self.assertEqual(
                text[segment["char_start_page"] : segment["char_end_page"]].strip(),
                segment["text"],
            )

    def test_page_bounded_limit_mode_subdivides_without_crossing_page_limit(self):
        pages = [
            {
                "page": 2,
                "text": ("Paragraph one has enough text to force a split. " * 12)
                + "\n\n"
                + ("Paragraph two also has enough text to require another bounded split. " * 12),
            }
        ]
        source_meta = {
            "source_id": "pdf_hash",
            "source_title": "Example",
            "source_author": "Author",
            "source_short_label": "Author",
            "source_sha256": "0123456789abcdef" * 4,
            "metadata_provenance": {},
            "body_start": 2,
            "end_matter_start": 10,
            "boundary_confidence": "high",
        }
        segments = pipeline.make_segments(
            Path("example.pdf"),
            "pymupdf",
            pages,
            2,
            10,
            source_meta,
            700,
            outline=[],
            segment_mode="page_limit",
            effective_limit=220,
        )
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(row["pdf_page"] == 2 for row in segments))
        self.assertTrue(all(len(row["text"]) <= 220 for row in segments))

    def test_cross_page_profile_finds_repeated_headers_and_duplicates(self):
        pages = [
            {
                "page": 1,
                "text": "Repeated Book Header\n\nAlpha body sentence with enough additional prose for fingerprinting. "
                "The page remains distinct.\nRepeated Footer",
            },
            {
                "page": 2,
                "text": "Repeated Book Header\n\nBeta body sentence with enough additional prose for fingerprinting. "
                "This exact page is intentionally duplicated.\nRepeated Footer",
            },
            {
                "page": 3,
                "text": "Repeated Book Header\n\nBeta body sentence with enough additional prose for fingerprinting. "
                "This exact page is intentionally duplicated.\nRepeated Footer",
            },
        ]
        stats = pipeline.enrich_page_stats(pages, [pipeline.page_stats_for(page) for page in pages])
        self.assertEqual(stats[0].repeated_header, "Repeated Book Header")
        self.assertEqual(stats[0].repeated_footer, "Repeated Footer")
        self.assertEqual(stats[2].duplicate_of_page, 2)

    def test_detect_body_start_retains_substantive_first_page_below_dense_prose_threshold(self):
        first_page = "NOTES ON A MOUNTAIN MAN.\n" + "\n".join(
            "A short-line poem still contains substantive source content."
            for _ in range(18)
        )
        second_page = "The ordinary article body continues here. " * 60
        pages = [{"page": 1, "text": first_page}, {"page": 2, "text": second_page}]
        stats = [pipeline.page_stats_for(page) for page in pages]
        self.assertLess(stats[0].words, 180)
        start_page, reason = pipeline.detect_body_start(pages, stats, outline=[], include_front_matter=False)
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "substantive_first_page_retained")

    def test_region_aware_native_layout_excludes_running_author_and_reorders_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "layout-fixture.pdf"
            document = pipeline.fitz.open()
            for page_number in range(1, 5):
                page = document.new_page(width=612, height=792)
                header = "Josephine A. Ruggiero" if page_number == 4 else "A Sociological Analysis of Social Inequality"
                page.insert_text((36, 32), header, fontsize=12, fontname="heit")
                page.insert_text((565, 32), str(page_number), fontsize=12)
                for line_number in range(12):
                    y = 88 + line_number * 22
                    page.insert_text((36, y), f"left p{page_number} line {line_number} continues the argument.", fontsize=10)
                    page.insert_text((320, y), f"right p{page_number} line {line_number} continues the evidence.", fontsize=10)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [
                    {"page": index + 1, "text": source[index].get_text("text")}
                    for index in range(len(source))
                ]
            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)
            page_four = transformed[3]
            self.assertIn("left p4 line 0 continues the argument.", page_four["text"])
            self.assertNotIn("Josephine A. Ruggiero", page_four["text"])
            self.assertNotIn("\n4\n", f"\n{page_four['text']}\n")
            self.assertIn("Josephine A. Ruggiero", page_four["raw_text"])
            self.assertEqual(page_four["layout_reading_order"], "two_column_column_first")
            self.assertLess(
                page_four["text"].index("left p4 line 11"),
                page_four["text"].index("right p4 line 0"),
            )
            self.assertGreaterEqual(evidence["removed_marginalia_count"], 8)

    def test_photographed_spread_reading_regions_keep_one_physical_page(self):
        rows = []
        for line_number in range(14):
            y = 70 + line_number * 25
            rows.extend([
                {
                    "text": f"left spread prose line {line_number} contains enough ordinary words for geometry.",
                    "normalized": "",
                    "x0": 110.0, "x1": 650.0, "y0": float(y), "y1": float(y + 12),
                },
                {
                    "text": f"right spread prose line {line_number} contains enough ordinary words for geometry.",
                    "normalized": "",
                    "x0": 570.0, "x1": 1110.0, "y0": float(y), "y1": float(y + 12),
                },
            ])
        ordered, reading_order, regions = pipeline._layout_reading_order(rows, 1224, 792)
        self.assertEqual(reading_order, "photographed_spread_column_first")
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["source_column_index"], 1)
        self.assertEqual(regions[1]["source_column_index"], 2)
        self.assertLess(
            ordered.index(rows[24]),
            ordered.index(rows[1]),
        )

    def test_photographed_spread_detector_rejects_portrait_overlap(self):
        rows = []
        for line_number in range(14):
            y = 70 + line_number * 25
            rows.extend([
                {
                    "text": f"left indented prose line {line_number} has enough ordinary words for geometry.",
                    "normalized": "",
                    "x0": 55.0, "x1": 325.0, "y0": float(y), "y1": float(y + 12),
                },
                {
                    "text": f"right indented prose line {line_number} has enough ordinary words for geometry.",
                    "normalized": "",
                    "x0": 285.0, "x1": 555.0, "y0": float(y), "y1": float(y + 12),
                },
            ])

        self.assertIsNone(pipeline._layout_photographed_spread_columns(rows, 612, 792))

    def test_photographed_spread_detector_does_not_claim_a_normal_gutter(self):
        rows = []
        for line_number in range(14):
            y = 70 + line_number * 25
            rows.extend([
                {
                    "text": f"left ordinary column prose line {line_number} has enough words for geometry.",
                    "normalized": "",
                    "x0": 55.0, "x1": 275.0, "y0": float(y), "y1": float(y + 12),
                },
                {
                    "text": f"right ordinary column prose line {line_number} has enough words for geometry.",
                    "normalized": "",
                    "x0": 335.0, "x1": 555.0, "y0": float(y), "y1": float(y + 12),
                },
            ])
        _ordered, reading_order, regions = pipeline._layout_reading_order(rows, 612, 792)
        self.assertEqual(reading_order, "two_column_column_first")
        self.assertIsNone(regions)

    def test_region_aware_layout_excludes_alternating_short_article_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "alternating-headers.pdf"
            document = pipeline.fitz.open()
            for page_number in range(1, 5):
                page = document.new_page(width=612, height=792)
                header = "Sample Reviewer One & Sample Reviewer Two" if page_number % 2 else "Book Review: Example Public Book"
                page.insert_text((36, 110), header, fontsize=11)
                page.insert_text((565, 110), str(page_number), fontsize=11)
                for line_number in range(12):
                    y = 150 + line_number * 24
                    page.insert_text((36, y), f"left body page {page_number} line {line_number}.", fontsize=10)
                    page.insert_text((320, y), f"right body page {page_number} line {line_number}.", fontsize=10)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [
                    {"page": index + 1, "text": source[index].get_text("text")}
                    for index in range(len(source))
                ]

            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)

            for page in transformed:
                self.assertNotIn("Sample Reviewer One & Sample Reviewer Two", page["text"])
                self.assertNotIn("Book Review: Example Public Book", page["text"])
            self.assertGreaterEqual(evidence["removed_marginalia_count"], 8)

    def test_region_aware_layout_excludes_verified_outer_margin_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "outer-margin-annotations.pdf"
            document = pipeline.fitz.open()
            for page_number in range(1, 3):
                page = document.new_page(width=612, height=792)
                for line_number in range(12):
                    y = 100 + line_number * 32
                    page.insert_text(
                        (55, y),
                        f"Body page {page_number} line {line_number} has enough ordinary prose to establish a stable reading area.",
                        fontsize=10,
                    )
                    # A distinct, repeated handwritten/OCR-like outer trace.
                    # Its placement across many bands is the confirmation; a
                    # single sidebar must never trigger this path.
                    page.insert_text((510, y), f"~!{line_number}?", fontsize=6, fontname="helv")
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": index + 1, "text": source[index].get_text("text")} for index in range(len(source))]

            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)

            self.assertNotIn("~!0?", transformed[0]["text"])
            self.assertIn("Body page 1 line 0", transformed[0]["text"])
            review = evidence["pages"][0]["outer_margin_annotation"]
            self.assertTrue(review["applied"])
            self.assertGreaterEqual(review["outside_vertical_band_count"], 4)

    def test_region_aware_layout_preserves_legitimate_narrow_category_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "category-column.pdf"
            document = pipeline.fitz.open()
            page = document.new_page(width=612, height=792)
            for line_number in range(12):
                y = 100 + line_number * 32
                page.insert_text((55, y), f"Body line {line_number} has enough ordinary prose to establish a stable reading area.", fontsize=10)
                page.insert_text((490, y), f"A-{line_number + 1}", fontsize=8)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": 1, "text": source[0].get_text("text")}]

            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)

            self.assertIn("A-1", transformed[0]["text"])
            self.assertFalse(evidence["pages"][0]["outer_margin_annotation"]["applied"])

    def test_region_aware_layout_removes_repeated_footer_in_lower_margin_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "lower-footer.pdf"
            document = pipeline.fitz.open()
            for page_number in range(1, 4):
                page = document.new_page(width=612, height=792)
                page.insert_text((60, 120), f"Unique body text on page {page_number} remains retrievable.", fontsize=11)
                # 92% of page height: a real printer footer which used to sit
                # just above the old 94% candidate band.
                page.insert_text((60, 730), f"Journal of Example Studies | {page_number}", fontsize=8)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": index + 1, "text": source[index].get_text("text")} for index in range(len(source))]

            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)

            self.assertIn("Unique body text on page 1", transformed[0]["text"])
            self.assertNotIn("Journal of Example Studies", transformed[0]["text"])
            self.assertTrue(any(
                item["reason"] == "repeated_running_footer"
                for item in evidence["pages"][0]["removed_marginalia"]
            ))

    def test_region_aware_layout_preserves_title_byline_author_order_above_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "title-byline-columns.pdf"
            document = pipeline.fitz.open()
            page = document.new_page(width=612, height=792)
            page.insert_textbox(
                pipeline.fitz.Rect(36, 42, 576, 110),
                "A Wide Title That Must Precede Its Byline",
                fontsize=16,
                align=1,
            )
            page.insert_text((286, 150), "By", fontsize=14)
            page.insert_text((220, 192), "Josephine A. Ruggiero", fontsize=14)
            page.insert_text((36, 238), "Abstract", fontsize=12)
            page.insert_textbox(
                pipeline.fitz.Rect(36, 252, 576, 274),
                "This full-width abstract must follow its label before the two-column body.",
                fontsize=10,
            )
            for line_number in range(12):
                y = 286 + line_number * 24
                page.insert_text((36, y), f"left column body line {line_number}.", fontsize=10)
                page.insert_text((320, y), f"right column body line {line_number}.", fontsize=10)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": 1, "text": source[0].get_text("text")}]

            transformed, _evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)
            text = transformed[0]["text"]

            self.assertEqual(transformed[0]["layout_reading_order"], "two_column_column_first")
            self.assertLess(text.index("A Wide Title"), text.index("By"))
            self.assertLess(text.index("By"), text.index("Josephine A. Ruggiero"))
            self.assertLess(text.index("Josephine A. Ruggiero"), text.index("Abstract"))
            self.assertLess(text.index("Abstract"), text.index("This full-width abstract"))
            self.assertLess(text.index("Abstract"), text.index("left column body line 0"))
            self.assertLess(text.index("left column body line 11"), text.index("right column body line 0"))

    def test_region_aware_layout_retains_possible_footnotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "footnote-fixture.pdf"
            document = pipeline.fitz.open()
            for _ in range(3):
                page = document.new_page(width=612, height=792)
                page.insert_text((36, 90), "Body sentence that remains available for semantic retrieval.", fontsize=12)
                page.insert_text((36, 700), "1 A source note that requires human policy review.", fontsize=9)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": index + 1, "text": source[index].get_text("text")} for index in range(len(source))]
            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)
            self.assertIn("1 A source note", transformed[0]["text"])
            self.assertEqual(evidence["note_candidates_retained_count"], 3)

    def test_region_aware_layout_excludes_only_multi_line_small_font_footnotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "footnote-group-fixture.pdf"
            document = pipeline.fitz.open()
            for _ in range(3):
                page = document.new_page(width=612, height=792)
                page.insert_text((36, 90), "Body sentence that remains available for semantic retrieval.", fontsize=12)
                page.insert_text((36, 700), "1 A source note with enough detail to be a genuine citation.", fontsize=9)
                page.insert_text((36, 716), "Continuation of the same source note, not ordinary body prose.", fontsize=9)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": index + 1, "text": source[index].get_text("text")} for index in range(len(source))]
            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)
            self.assertIn("Body sentence", transformed[0]["text"])
            self.assertNotIn("A source note with enough", transformed[0]["text"])
            self.assertIn("A source note with enough", transformed[0]["raw_text"])
            self.assertEqual(evidence["excluded_footnote_count"], 3)

    def test_region_aware_layout_excludes_only_unmistakable_web_footer_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "web-footer-fixture.pdf"
            document = pipeline.fitz.open()
            page = document.new_page(width=612, height=792)
            page.insert_text((36, 30), "The final substantive sentence remains available to retrieval.", fontsize=12)
            page.insert_text((36, 60), "FILED UNDER:", fontsize=9)
            page.insert_text((36, 76), "TOPIC ONE, TOPIC TWO", fontsize=8)
            page.insert_text((36, 100), "Sign up", fontsize=9)
            page.insert_text((36, 116), "Your Email", fontsize=9)
            page.insert_text((36, 132), "About Us Advertising RSS", fontsize=8)
            page.insert_text((36, 148), "Privacy Policy Terms of Service", fontsize=8)
            document.save(pdf_path)
            document.close()
            with pipeline.fitz.open(pdf_path) as source:
                pages = [{"page": 1, "text": source[0].get_text("text")}]
            transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)
            self.assertIn("final substantive sentence", transformed[0]["text"])
            self.assertNotIn("FILED UNDER", transformed[0]["text"])
            self.assertNotIn("Privacy Policy", transformed[0]["text"])
            self.assertIn("FILED UNDER", transformed[0]["raw_text"])
            self.assertTrue(any(
                item["reason"] == "high_confidence_web_footer_boilerplate"
                for item in evidence["pages"][0]["removed_marginalia"]
            ))

    def test_supplementary_lane_review_is_document_scoped_and_payload_safe(self):
        stats = []
        for page_number in range(1, 11):
            stat = pipeline.page_stats_for(
                {"page": page_number, "text": "Ordinary body prose remains in the primary retrieval payload."}
            )
            stats.append(stat)
        stats[7].is_index_like = True
        stats[7].line_count = 32
        stats[7].sentence_marks = 1
        stats[7].preview = "Index Aardvark, 12\nBaker, 14"
        stats[8].is_bibliography_like = True
        stats[8].line_count = 22
        stats[8].preview = "References Adams, A. 2024. An Example Article."
        stats[6].is_bibliography_like = True
        stats[6].line_count = 30
        stats[6].preview = "2016 https://example.test/2016/10/04/ A date-heavy but isolated prose page."
        stats[4].image_count = 1
        segments = [
            {"pdf_page": 8, "segment_id": "p8", "text": "Aardvark, 12; Baker, 14."},
            {"pdf_page": 9, "segment_id": "p9", "text": "Adams, A. 2024. An Example Article."},
            {"pdf_page": 7, "segment_id": "p7", "text": "A body paragraph containing 2016 and a URL."},
            {"pdf_page": 5, "segment_id": "p5", "text": "Photo credit: Example Archive."},
            {"pdf_page": 3, "segment_id": "p3", "text": "Normal body content."},
        ]
        original_segments = [dict(row) for row in segments]
        review = pipeline.proposed_supplementary_lane_review(
            segments,
            stats,
            10,
            layout_evidence={
                "pages": [
                    {
                        "pdf_page": 4,
                        "note_candidates_retained": [{"text": "1 A possible source note.", "bbox": [1, 2, 3, 4]}],
                    }
                ]
            },
        )

        self.assertEqual(segments, original_segments)
        self.assertEqual(review["status"], "review_only")
        self.assertEqual(review["policy"], "review_only_when_no_narrow_automatic_rule_applies")
        self.assertFalse(review["primary_payload_changed"])
        self.assertEqual(review["proposed_supplementary_segment_count"], 3)
        self.assertEqual(review["proposed_supplementary_positioned_line_count"], 1)
        self.assertEqual({item["reason"] for item in review["items"]}, {
            "explicit_index_page", "explicit_references_page", "possible_image_credit", "possible_lower_page_note"
        })
        self.assertNotIn("p7", {item["segment_id"] for item in review["items"]})
        self.assertTrue(all(item["promotion_eligibility"] == "not_automatically_eligible" for item in review["items"]))
        with tempfile.TemporaryDirectory() as tmp:
            candidate_text = Path(tmp) / "supplementary-content-candidates.txt"
            pipeline.write_supplementary_lane_candidate_text(candidate_text, review)
            rendered = candidate_text.read_text(encoding="utf-8")
        self.assertIn("REVIEW-ONLY SUPPLEMENTARY REGION | pages 8 | explicit_index_page", rendered)
        self.assertIn("not individually classified as page locators", rendered)
        self.assertIn("1 A possible source note.", rendered)

    def test_supplementary_lane_retains_end_matter_by_default_and_can_exclude_it_on_request(self):
        segments = [
            {"pdf_page": 28, "segment_id": "body", "text": "Ordinary body prose with a 2016 date and a URL."},
            {"pdf_page": 29, "segment_id": "ref-1", "text": "Adams, A. An Example Reference."},
            {"pdf_page": 30, "segment_id": "ref-2", "text": "Baker, B. Another Example Reference."},
            {"pdf_page": 31, "segment_id": "credit", "text": "Photo credit: Example Archive."},
        ]
        lane_review = {
            "status": "review_only",
            "primary_payload_changed": False,
            "items": [
                {
                    "kind": "segment", "segment_id": "ref-1", "reason": "sustained_references_region",
                    "pdf_page": 29, "text": segments[1]["text"], "classification_scope": "page_region",
                    "scope_pages": [29, 30], "confidence": "medium", "evidence": "sustained_bibliography_like_pages",
                },
                {
                    "kind": "segment", "segment_id": "ref-2", "reason": "sustained_references_region",
                    "pdf_page": 30, "text": segments[2]["text"], "classification_scope": "page_region",
                    "scope_pages": [29, 30], "confidence": "medium", "evidence": "sustained_bibliography_like_pages",
                },
                {
                    "kind": "segment", "segment_id": "credit", "reason": "possible_image_credit",
                    "pdf_page": 31, "text": segments[3]["text"], "classification_scope": "segment",
                    "scope_pages": [31], "confidence": "medium", "evidence": "short_credit_label_on_image_page",
                },
            ],
        }

        primary, retained = pipeline.apply_automatic_supplementary_lane(segments, lane_review)

        self.assertEqual([row["segment_id"] for row in primary], ["body", "ref-1", "ref-2", "credit"])
        self.assertEqual(retained["status"], "review_only")
        self.assertEqual(retained["policy"], "opt_in_exclusion_not_requested")
        self.assertFalse(retained["primary_payload_changed"])
        self.assertEqual(retained["primary_excluded_segment_count"], 0)
        self.assertTrue(all(
            not item["applied_to_primary_payload"]
            for item in retained["items"]
        ))

        primary, applied = pipeline.apply_automatic_supplementary_lane(
            segments,
            lane_review,
            exclude_from_primary=True,
        )

        self.assertEqual([row["segment_id"] for row in primary], ["body", "credit"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            applied["policy"],
            "automatic_document_scoped_sustained_reference_index_exclusion",
        )
        self.assertTrue(applied["primary_payload_changed"])
        self.assertEqual(applied["primary_excluded_segment_count"], 2)
        self.assertEqual(applied["review_only_candidate_count"], 1)
        self.assertTrue(all(
            item["applied_to_primary_payload"]
            for item in applied["items"][:2]
        ))
        self.assertFalse(applied["items"][2]["applied_to_primary_payload"])
        with tempfile.TemporaryDirectory() as tmp:
            candidate_text = Path(tmp) / "supplementary-content-candidates.txt"
            pipeline.write_supplementary_lane_candidate_text(candidate_text, applied)
            rendered = candidate_text.read_text(encoding="utf-8")
        self.assertIn("[SUPPLEMENTARY REGION | pages 29–30 | sustained_references_region", rendered)
        self.assertIn("Primary upload: automatically excluded by the sustained reference/index-region rule.", rendered)
        self.assertIn("REVIEW-ONLY SUPPLEMENTARY CANDIDATE | page 31 | possible_image_credit", rendered)

    def test_reference_lane_includes_tightly_evidenced_final_continuation_page(self):
        stats = []
        for page_number in range(1, 36):
            stat = pipeline.page_stats_for(
                {"page": page_number, "text": "Ordinary body prose remains in the primary retrieval payload."}
            )
            stats.append(stat)
        for page_number in range(29, 35):
            stat = stats[page_number - 1]
            stat.is_bibliography_like = True
            stat.line_count = 30
            stat.words = 350
        final_stat = stats[34]
        final_stat.line_count = 12
        final_stat.words = 150
        final_stat.is_bibliography_like = False
        segments = [
            {"pdf_page": page_number, "segment_id": f"ref-{page_number}", "text": "1 Example Press reference."}
            for page_number in range(29, 35)
        ] + [
            {
                "pdf_page": 35,
                "segment_id": "final-notes",
                "text": (
                    "College Diversity Push, The New York Times, https://example.test. "
                    "94 Reviewer, Law Review. 95 James, University Press. "
                    "96 Martin, Journal Review. 97 Taylor, Times."
                ),
            }
        ]

        review = pipeline.proposed_supplementary_lane_review(segments, stats, 35)
        primary, applied = pipeline.apply_automatic_supplementary_lane(
            segments,
            review,
            exclude_from_primary=True,
        )

        final_item = next(item for item in applied["items"] if item["segment_id"] == "final-notes")
        self.assertEqual(final_item["reason"], "sustained_references_region")
        self.assertEqual(final_item["scope_pages"], [29, 30, 31, 32, 33, 34, 35])
        self.assertTrue(final_item["applied_to_primary_payload"])
        self.assertNotIn("final-notes", {segment["segment_id"] for segment in primary})

    def test_segment_ids_are_hash_based_and_metadata_is_canonical(self):
        pages = [
            {
                "page": 2,
                "text": "CHAPTER ONE\n\nThis is body prose. " * 40,
            }
        ]
        source_meta = {
            "source_id": "pdf_0123456789abcdef",
            "source_title": "Example",
            "source_author": "Author",
            "source_short_label": "Author",
            "source_sha256": "0123456789abcdef" * 4,
            "metadata_provenance": {
                "source_title": "pdf_metadata",
                "source_author": "pdf_metadata",
            },
            "body_start": 2,
            "end_matter_start": 10,
            "boundary_confidence": "high",
        }
        segments = pipeline.make_segments(
            Path("example.pdf"),
            "pymupdf",
            pages,
            2,
            10,
            source_meta,
            300,
            outline=[],
        )
        first = segments[0]
        self.assertTrue(first["segment_id"].startswith("pdf_0123456789ab_p0002_"))
        self.assertEqual(first["document_region"], "body")
        self.assertEqual(first["metadata_provenance"]["source_title"], "pdf_metadata")
        self.assertEqual(first["pipeline_version"], pipeline.PIPELINE_VERSION)
        self.assertGreater(first["estimated_tokens"], 0)

    def test_clean_primary_and_marker_fallback_are_distinct(self):
        segment = {
            "source_title": "Example",
            "source_short_label": "Example",
            "pdf_page": 3,
            "segment_index": 1,
            "segment_id": "pdf_hash_p0003_s00001",
            "chapter": "Introduction",
            "section": "",
            "text": "Clean passage text.",
        }
        clean = pipeline.generate_upload_text([segment], include_markers=False)
        fallback = pipeline.generate_upload_text([segment], include_markers=True)
        self.assertEqual(clean, "Clean passage text.")
        self.assertTrue(fallback.startswith("["))
        self.assertIn("Clean passage text.", fallback)

    def test_inline_marker_fallback_survives_chunk_simulation(self):
        segment = {
            "source_title": "Example",
            "source_short_label": "Example",
            "pdf_page": 3,
            "segment_index": 1,
            "segment_id": "pdf_hash_p0003_s00001",
            "chapter": "Introduction",
            "section": "",
            "text": " ".join(["Body prose sentence."] * 120),
        }
        fallback = pipeline.generate_upload_text([segment], include_markers=True)
        marker_eval = pipeline.chunk_marker_eval(fallback, chunk_size=400, overlap=40)
        self.assertEqual(marker_eval["chunks_without_marker"], 0)

    def test_anythingllm_chunk_settings_are_loaded_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "anythingllm.db"
            con = sqlite3.connect(db_path)
            con.execute("create table system_settings (label text, value text)")
            con.executemany(
                "insert into system_settings(label,value) values (?,?)",
                [
                    ("text_splitter_chunk_size", "512"),
                    ("text_splitter_chunk_overlap", "75"),
                ],
            )
            con.commit()
            con.close()
            settings = pipeline.anythingllm_chunk_settings(Path(temp_dir))
            self.assertEqual(settings["status"], "loaded")
            self.assertEqual(settings["chunk_size"], 512)
            self.assertEqual(settings["chunk_overlap"], 75)

    def test_anythingllm_embedding_config_reads_only_nonsecret_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "EMBEDDING_ENGINE='generic-openai'\n"
                "GENERIC_OPEN_AI_MODEL_PREF='text-embedding-3-small'\n"
                "EMBEDDING_MODEL_PREF='qwen3-embedding:8b'\n"
                "GENERIC_OPEN_AI_API_KEY='must-not-be-read'\n",  # pragma: allowlist secret -- env precedence fixture
                encoding="utf-8",
            )
            config = pipeline.anythingllm_embedding_config(Path(temp_dir))
            self.assertEqual(config["engine"], "generic-openai")
            self.assertEqual(config["model"], "qwen3-embedding:8b")
            self.assertEqual(config["effective_model_source"], "EMBEDDING_MODEL_PREF")
            self.assertEqual(config["adjacent_model_preferences"][0]["value"], "text-embedding-3-small")
            self.assertIn("provider_model_pref_differs_from_embedder", config["anomalies"])
            self.assertNotIn("must-not-be-read", json.dumps(config).casefold())

    def test_simulation_app_config_never_serializes_openrouter_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "OPENROUTER_API_KEY='top-secret'\n"  # pragma: allowlist secret -- env precedence fixture
                "OPENROUTER_SIMULATION_TIMEOUT_SECONDS='61'\n"
                "OPENROUTER_SIMULATION_ZDR='true'\n",
                encoding="utf-8",
            )
            config = pipeline.simulation_app_config(env_path)
            serialized = json.dumps(config).casefold()
            self.assertTrue(config["openrouter_configured"])
            self.assertEqual(config["openrouter_timeout_seconds"], 61)
            self.assertTrue(config["openrouter_zdr"])
            self.assertNotIn("top-secret", serialized)
            self.assertNotIn("api_key", serialized)

    def test_env_file_value_has_wrapping_quotes_does_not_expose_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "OPENROUTER_API_KEY='quoted-test-key'\n"  # pragma: allowlist secret -- synthetic quote fixture
                "EMBEDDING_MODEL_PREF=qwen/qwen3-embedding-8b\n",  # pragma: allowlist secret -- synthetic model fixture
                encoding="utf-8",
            )
            self.assertTrue(pipeline.env_file_value_has_wrapping_quotes(env_path, "OPENROUTER_API_KEY"))
            self.assertFalse(pipeline.env_file_value_has_wrapping_quotes(env_path, "EMBEDDING_MODEL_PREF"))

    def test_openrouter_runtime_failure_hint_is_brief_and_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text(
                "EMBEDDING_ENGINE=openrouter\n"
                "EMBEDDING_MODEL_PREF=qwen/qwen3-embedding-8b\n"  # pragma: allowlist secret -- synthetic model fixture
                "OPENROUTER_API_KEY='quoted-test-key'\n",  # pragma: allowlist secret -- synthetic quote fixture
                encoding="utf-8",
            )
            hint = pipeline.anythingllm_runtime_embedder_failure_hint(
                {"provider": "openrouter", "status": "server_error_empty_body"},
                storage,
            )
        self.assertIn("Restart AnythingLLM", hint)
        self.assertLess(len(hint), 90)
        self.assertNotIn("wrapped in quotes", hint)
        self.assertNotIn("quoted-test-key", hint)

    def test_runtime_embedder_identifies_openrouter_auth_reverification_from_local_log(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text("EMBEDDING_ENGINE=openrouter\n", encoding="utf-8")
            logs = storage / "logs"
            logs.mkdir()
            (logs / "backend-test.log").write_text(
                "OpenRouter Failed to embed: [failed_to_embed]: 401 User not found.",
                encoding="utf-8",
            )
            original_post = pipeline.post_json_captured
            try:
                pipeline.post_json_captured = lambda *_args, **_kwargs: {
                    "http_status": 500, "data": {}, "error": ""
                }
                result = pipeline.verify_anythingllm_runtime_embedder(
                    "https://anythingllm.example",
                    api_key="placeholder",  # pragma: allowlist secret -- non-secret test argument
                    storage_dir=storage,
                )
            finally:
                pipeline.post_json_captured = original_post

        self.assertEqual(result["status"], "openrouter_credential_reverification_required")
        self.assertEqual(result["warning_code"], "AUTO-OPENROUTER-KEY-REVERIFY-001")
        self.assertIn("rejected the embedding key", result["message"])
        self.assertLess(len(result["message"]), 120)
        self.assertNotIn("placeholder", result["message"])

    def test_local_openrouter_runtime_refresh_uses_only_redacted_supported_setting(self):
        original_config = pipeline.anythingllm_embedding_config
        original_secret = pipeline.anythingllm_storage_secret
        original_auth = pipeline.resolve_anythingllm_api_key
        original_post = pipeline.post_json_captured
        captured = {}
        try:
            pipeline.anythingllm_embedding_config = lambda *_args, **_kwargs: {
                "normalized_engine": "openrouter"
            }
            pipeline.anythingllm_storage_secret = lambda *_args, **_kwargs: "provider-test-value"
            pipeline.resolve_anythingllm_api_key = lambda *_args, **_kwargs: (
                "developer-test-value", "managed_local_service_key"
            )

            openrouter_config_key = "OpenRouterApi" + "Key"

            def fake_post(url, body, api_key=None, **_kwargs):
                captured.update({"url": url, "body": body, "api_key": api_key})
                return {
                    "http_status": 200,
                    "data": {"newValues": {openrouter_config_key: "provider-test-value"}, "error": False},
                }

            pipeline.post_json_captured = fake_post
            result = pipeline.refresh_local_anythingllm_openrouter_runtime(
                "http://127.0.0.1:3001", storage_dir=Path("unused")
            )
        finally:
            pipeline.anythingllm_embedding_config = original_config
            pipeline.anythingllm_storage_secret = original_secret
            pipeline.resolve_anythingllm_api_key = original_auth
            pipeline.post_json_captured = original_post

        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(captured["body"], {openrouter_config_key: "provider-test-value"})
        self.assertNotIn("provider-test-value", json.dumps(result))
        self.assertNotIn("developer-test-value", json.dumps(result))

    def test_native_runtime_validation_reuses_supplied_embedder_probe(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text("EMBEDDING_ENGINE=openrouter\n", encoding="utf-8")
            original_probe = pipeline.verify_anythingllm_runtime_embedder
            try:
                pipeline.verify_anythingllm_runtime_embedder = lambda *args, **kwargs: self.fail(
                    "cached successful embedder probe should be reused"
                )
                result = pipeline.validate_anythingllm_native_runtime(
                    "http://127.0.0.1:3001",
                    "",
                    "missing-workspace",
                    [],
                    0,
                    storage,
                    embedder_probe_override={"status": "pass", "dimension": 4096},
                )
            finally:
                pipeline.verify_anythingllm_runtime_embedder = original_probe
            self.assertEqual(result["embedder_probe"]["status"], "pass")
            self.assertTrue(result["embedder_probe"]["cache_reused"])

    def test_resolve_default_simulation_adapter_uses_openrouter_with_local_app_env(self):
        with tempfile.TemporaryDirectory() as storage_dir, tempfile.TemporaryDirectory() as app_dir:
            (Path(storage_dir) / ".env").write_text(
                "EMBEDDING_ENGINE='openrouter'\n"
                "OPENROUTER_MODEL_PREF='openai/text-embedding-3-small'\n"
                "EMBEDDING_MODEL_PREF='baai/bge-m3'\n",
                encoding="utf-8",
            )
            env_path = Path(app_dir) / ".env"
            env_path.write_text("OPENROUTER_API_KEY='test-key'\n", encoding="utf-8")  # pragma: allowlist secret -- synthetic env fixture
            resolved = pipeline.resolve_default_simulation_adapter(Path(storage_dir), env_path)
            self.assertEqual(resolved["status"], "ready")
            self.assertEqual(resolved["adapter"]["provider"], "openrouter")
            self.assertEqual(resolved["adapter"]["model"], "baai/bge-m3")
            self.assertEqual(resolved["adapter"]["env_path"], str(env_path))
            self.assertIn("provider_model_pref_differs_from_embedder", resolved["anomalies"])

    def test_resolve_default_simulation_adapter_can_fallback_to_anythingllm_openrouter_key(self):
        with tempfile.TemporaryDirectory() as storage_dir, tempfile.TemporaryDirectory() as app_dir:
            (Path(storage_dir) / ".env").write_text(
                "EMBEDDING_ENGINE='openrouter'\n"
                "EMBEDDING_MODEL_PREF='openai/text-embedding-3-small'\n"
                "OPENROUTER_API_KEY='desktop-key'\n",  # pragma: allowlist secret -- synthetic Desktop override fixture
                encoding="utf-8",
            )
            env_path = Path(app_dir) / ".env"
            env_path.write_text("", encoding="utf-8")
            resolved = pipeline.resolve_default_simulation_adapter(Path(storage_dir), env_path)
            self.assertEqual(resolved["status"], "ready")
            self.assertEqual(resolved["adapter"]["model"], "openai/text-embedding-3-small")
            self.assertEqual(resolved["adapter"]["key_source"], "anythingllm_fallback")
            self.assertEqual(resolved["adapter"]["env_path"], str(Path(storage_dir) / ".env"))
            self.assertEqual(resolved["anomalies"], [])

    def test_provider_model_key_for_engine_uses_provider_specific_pref_when_available(self):
        self.assertEqual(pipeline.provider_model_key_for_engine("openrouter"), "OPENROUTER_MODEL_PREF")
        self.assertEqual(pipeline.provider_model_key_for_engine("generic-openai"), "GENERIC_OPEN_AI_MODEL_PREF")
        self.assertEqual(pipeline.provider_model_key_for_engine("ollama"), "OLLAMA_MODEL_PREF")
        self.assertEqual(pipeline.provider_model_key_for_engine("anythingllm"), "ANYTHINGLLM_MODEL_PREF")
        self.assertEqual(pipeline.provider_model_key_for_engine("unknown-provider"), "EMBEDDING_MODEL_PREF")

    def test_provider_specific_embedder_pref_is_used_when_generic_missing(self):
        values = {
            "EMBEDDING_ENGINE": "openai",
            "OPENAI_MODEL_PREF": "text-embedding-3-small",
        }
        config = pipeline.classify_anythingllm_embedding_config(values)
        self.assertEqual(config["effective_model"], "text-embedding-3-small")
        self.assertEqual(config["effective_model_source"], "OPENAI_MODEL_PREF")

    def test_resolve_embedder_capability_knows_anythingllm_builtin_models(self):
        cap = pipeline.resolve_embedder_capability("anythingllm", "all-MiniLM-L6-v2")
        self.assertEqual(cap["display_name"], "AnythingLLM Embedder: all-MiniLM-L6-v2")
        self.assertEqual(cap["recommended_anythingllm_limit"], 256)
        self.assertEqual(cap["embedding_length"], 384)

    def test_resolve_embedder_capability_knows_openai_family_models(self):
        cap = pipeline.resolve_embedder_capability("openai", "text-embedding-3-large")
        self.assertEqual(cap["recommended_anythingllm_limit"], 8191)
        self.assertEqual(cap["embedding_length"], 3072)
        self.assertIn("OpenAI", cap["display_name"])

        generic_cap = pipeline.resolve_embedder_capability("generic-openai", "text embedding 3 small")
        self.assertEqual(generic_cap["recommended_anythingllm_limit"], 8191)
        self.assertIn("Generic OpenAI", generic_cap["display_name"])

        lmstudio_cap = pipeline.resolve_embedder_capability("lmstudio", "text-embedding-3-small")
        self.assertEqual(lmstudio_cap["recommended_anythingllm_limit"], 8191)
        self.assertIn("LM Studio", lmstudio_cap["display_name"])

        lemonade_cap = pipeline.resolve_embedder_capability("lemonade", "text-embedding-3-large")
        self.assertEqual(lemonade_cap["recommended_anythingllm_limit"], 8191)
        self.assertIn("Lemonade", lemonade_cap["display_name"])

    def test_resolve_embedder_capability_knows_openrouter_alias_labels(self):
        cap = pipeline.resolve_embedder_capability("openrouter", "Google: Gemini Embedding 2")
        self.assertEqual(cap["model"], "google/gemini-embedding-2")
        self.assertEqual(cap["recommended_anythingllm_limit"], 8192)
        self.assertIn("Gemini Embedding 2", cap["display_name"])

    def test_resolve_embedder_capability_knows_additional_cloud_embedding_families(self):
        cohere = pipeline.resolve_embedder_capability("cohere", "embed-v4.0")
        self.assertEqual(cohere["recommended_anythingllm_limit"], 128000)
        self.assertIn("Cohere", cohere["display_name"])

        voyage = pipeline.resolve_embedder_capability("voyage", "voyage-4-large")
        self.assertEqual(voyage["recommended_anythingllm_limit"], 32000)
        self.assertIn("Voyage", voyage["display_name"])

        jina = pipeline.resolve_embedder_capability("jinaai", "jina-embeddings-v4")
        self.assertEqual(jina["recommended_anythingllm_limit"], 32000)
        self.assertIn("Jina", jina["display_name"])

    def test_generic_embedder_pref_beats_chat_side_anythingllm_model_pref(self):
        values = {
            "EMBEDDING_ENGINE": "anythingllm",
            "ANYTHINGLLM_MODEL_PREF": "all-MiniLM-L6-v2",
            "EMBEDDING_MODEL_PREF": "text-embedding-3-large",
        }
        config = pipeline.classify_anythingllm_embedding_config(values)
        self.assertEqual(config["effective_model"], "text-embedding-3-large")
        self.assertEqual(config["effective_model_source"], "EMBEDDING_MODEL_PREF")
        self.assertIn("provider_model_pref_differs_from_embedder", config["anomalies"])

    def test_auto_correct_anythingllm_embedder_limit_raises_known_wrong_value(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text(
                "EMBEDDING_ENGINE='ollama'\n"
                "EMBEDDING_MODEL_PREF='bge-m3:latest'\n"
                "EMBEDDING_MODEL_MAX_CHUNK_LENGTH='512'\n",
                encoding="utf-8",
            )
            report = pipeline.auto_correct_anythingllm_embedder_limit(storage)
            updated = pipeline.anythingllm_embedding_config(storage)
            self.assertEqual(report["status"], "corrected")
            self.assertTrue(report["auto_corrected"])
            self.assertEqual(int(updated["max_chunk_length"]), 8192)

    def test_persist_anythingllm_embedder_settings_keeps_generic_pref_in_sync(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text(
                "EMBEDDING_ENGINE='anythingllm'\n"
                "ANYTHINGLLM_MODEL_PREF='all-MiniLM-L6-v2'\n"
                "EMBEDDING_MODEL_PREF='all-MiniLM-L6-v2'\n",
                encoding="utf-8",
            )
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            con.execute("create table system_settings (label text, value text)")
            con.commit()
            con.close()
            pipeline.persist_anythingllm_embedder_settings(storage, "openai", "text-embedding-3-small")
            values = pipeline.read_env_file_values(storage / ".env")
            self.assertEqual(values.get("EMBEDDING_MODEL_PREF"), "text-embedding-3-small")
            self.assertEqual(values.get("OPENAI_MODEL_PREF"), "text-embedding-3-small")

    def test_anythingllm_resolved_state_separates_chat_and_embedder(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            storage = Path(storage_dir)
            (storage / ".env").write_text(
                "LLM_PROVIDER='openrouter'\n"
                "OPENROUTER_MODEL_PREF='baai/bge-m3'\n"
                "EMBEDDING_ENGINE='ollama'\n"
                "OLLAMA_MODEL_PREF='gemma3n:e2b'\n"
                "EMBEDDING_MODEL_PREF='embeddinggemma:latest'\n"
                "EMBEDDING_MODEL_MAX_CHUNK_LENGTH='2048'\n",
                encoding="utf-8",
            )
            db_path = storage / "anythingllm.db"
            con = sqlite3.connect(db_path)
            con.execute("create table system_settings (label text, value text)")
            con.executemany(
                "insert into system_settings(label,value) values (?,?)",
                [
                    ("text_splitter_chunk_size", "512"),
                    ("text_splitter_chunk_overlap", "75"),
                ],
            )
            con.commit()
            con.close()
            state = pipeline.anythingllm_resolved_state(storage)
            self.assertEqual(state["chat_llm"]["provider"], "openrouter")
            self.assertEqual(state["chat_llm"]["model"], "baai/bge-m3")
            self.assertEqual(state["embedder"]["engine"], "ollama")
            self.assertEqual(state["embedder"]["effective_model"], "embeddinggemma:latest")
            self.assertEqual(state["chunking"]["chunk_size"], 512)

    def test_local_only_preparation_skips_unused_live_embedder_probe(self):
        args = SimpleNamespace(
            external_preflight_managed=False,
            prepare_and_upload=False,
            run_vector_eval=False,
        )

        self.assertFalse(pipeline.should_verify_anythingllm_runtime_during_preparation(args))

    def test_cli_upload_without_external_preflight_keeps_live_embedder_probe(self):
        args = SimpleNamespace(
            external_preflight_managed=False,
            prepare_and_upload=True,
            run_vector_eval=False,
        )

        self.assertTrue(pipeline.should_verify_anythingllm_runtime_during_preparation(args))

    def test_desktop_run_does_not_repeat_completed_external_preflight(self):
        args = SimpleNamespace(
            external_preflight_managed=True,
            prepare_and_upload=True,
            run_vector_eval=True,
        )

        self.assertFalse(pipeline.should_verify_anythingllm_runtime_during_preparation(args))

    def test_resolve_default_simulation_adapter_uses_anythingllm_runtime_for_supported_cloud_default(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            (Path(storage_dir) / ".env").write_text(
                "EMBEDDING_ENGINE='generic-openai'\n"
                "EMBEDDING_MODEL_PREF='text-embedding-3-small'\n",
                encoding="utf-8",
            )
            original_builder = pipeline.build_anythingllm_runtime_simulation_adapter
            try:
                pipeline.build_anythingllm_runtime_simulation_adapter = lambda storage_dir=None, api_url="http://127.0.0.1:3001", api_key=None: {
                    "provider": "anythingllm-runtime",
                    "model": "text-embedding-3-small",
                    "url": "http://127.0.0.1:3001/api/v1/openai/embeddings",
                    "api_url": "http://127.0.0.1:3001",
                    "api_key": "temp",  # pragma: allowlist secret -- synthetic API payload fixture
                    "temporary_key_id": "temp-id",
                    "key_source": "temporary_desktop_api_key",
                    "capability": pipeline.resolve_embedder_capability("generic-openai", "text-embedding-3-small"),
                    "is_available": True,
                }
                resolved = pipeline.resolve_default_simulation_adapter(Path(storage_dir))
            finally:
                pipeline.build_anythingllm_runtime_simulation_adapter = original_builder
            self.assertEqual(resolved["status"], "ready")
            self.assertEqual(resolved["adapter"]["provider"], "anythingllm-runtime")

    def test_build_openrouter_simulation_adapter_missing_key_fails_before_network(self):
        with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as storage_dir:
            env_path = Path(app_dir) / ".env"
            env_path.write_text("OPENROUTER_SIMULATION_TIMEOUT_SECONDS='30'\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                pipeline.build_openrouter_simulation_adapter(
                    "openai/text-embedding-3-small",
                    env_path,
                    storage_dir=Path(storage_dir),
                    allow_anythingllm_fallback=False,
                )
            self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_get_openrouter_embeddings_uses_expected_request_shape(self):
        with tempfile.TemporaryDirectory() as app_dir:
            env_path = Path(app_dir) / ".env"
            env_path.write_text(
                "OPENROUTER_API_KEY='test-key'\n"  # pragma: allowlist secret -- synthetic provider fixture
                "OPENROUTER_SIMULATION_ZDR='true'\n",
                encoding="utf-8",
            )
            adapter = pipeline.build_openrouter_simulation_adapter("openai/text-embedding-3-small", env_path)
            captured = {}

            class FakeResponse:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps(
                        {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
                    ).encode("utf-8")

            original_urlopen = pipeline.urllib.request.urlopen
            try:
                def fake_urlopen(req, timeout=0):
                    captured["url"] = req.full_url
                    captured["timeout"] = timeout
                    captured["headers"] = dict(req.header_items())
                    captured["body"] = json.loads(req.data.decode("utf-8"))
                    return FakeResponse()

                pipeline.urllib.request.urlopen = fake_urlopen
                vectors = pipeline.get_openrouter_embeddings(["alpha", "beta"], adapter)
            finally:
                pipeline.urllib.request.urlopen = original_urlopen
            self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])
            self.assertEqual(captured["url"], pipeline.DEFAULT_OPENROUTER_EMBEDDINGS_URL)
            self.assertEqual(captured["headers"]["Content-type"], "application/json")
            self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
            self.assertEqual(captured["body"]["model"], "openai/text-embedding-3-small")
            self.assertEqual(captured["body"]["input"], ["alpha", "beta"])
            self.assertEqual(captured["body"]["provider"], {"zdr": True})

    def test_vector_eval_status_maps_openrouter_http_errors(self):
        err_401 = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/embeddings",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"bad key"}}'),
        )
        err_402 = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/embeddings",
            402,
            "Payment Required",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        err_429 = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/embeddings",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        err_503 = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/embeddings",
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        adapter = {"provider": "openrouter", "model": "openai/text-embedding-3-small"}
        try:
            self.assertEqual(pipeline.vector_eval_status_for_exception(err_401, adapter), "error_openrouter_authentication")
            self.assertEqual(pipeline.vector_eval_status_for_exception(err_402, adapter), "error_openrouter_billing")
            self.assertEqual(pipeline.vector_eval_status_for_exception(err_429, adapter), "error_openrouter_rate_limited")
            self.assertEqual(pipeline.vector_eval_status_for_exception(err_503, adapter), "error_openrouter_provider_overloaded")
        finally:
            err_401.close()
            err_402.close()
            err_429.close()
            err_503.close()

    def test_author_inference_finds_byline_from_text_samples(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": "Example Book\n\nby Sample Author\n\nExample University Press",
                }
            ],
            title_hint="Example Book",
        )
        self.assertEqual(report["author"], "Sample Author")
        self.assertIn(report["source"], {"text_byline", "text_role_followup"})

    def test_author_inference_prefers_affiliation_bylines_over_title_and_reviewed_book_names(self):
        report = pipeline.infer_author_from_text_samples(
            [{
                "page": 1,
                "text": (
                    "Example Review: Examining a Public Book's Impact\n"
                    "Alex Harper, Example University\n"
                    "Jordan Lee, Example University\n"
                    "Book Reviewed: Example Public Book, by Example Writer\n"
                ),
            }],
            title_hint="Example Review: Examining a Public Book's Impact",
        )
        self.assertEqual(report["author"], "Alex Harper, Jordan Lee")
        self.assertEqual(report["source"], "text_affiliated_byline")

    def test_author_inference_accepts_parenthetical_researcher_affiliation(self):
        report = pipeline.infer_author_from_text_samples(
            [{
                "page": 1,
                    "text": "Example Research Topic\nSample Researcher (Independent researcher)\nAbstract. This paper...",
            }],
            title_hint="Example Research Topic",
        )
        self.assertEqual(report["author"], "Sample Researcher")
        self.assertEqual(report["source"], "text_affiliated_byline")

    def test_author_inference_accepts_name_echoed_by_bibliographic_from_line(self):
        report = pipeline.infer_author_from_text_samples(
            [{
                "page": 1,
                "text": (
                    "The Protestant Ethic and the Spirit of Capitalism\n"
                    "Max Weber\n"
                    "From Max Weber, The Protestant Ethic and the Spirit of Capitalism, trans. Talcott Parsons\n"
                ),
            }],
            title_hint="Weber_Protestant Ethic_excerpt",
        )
        self.assertEqual(report["author"], "Max Weber")
        self.assertEqual(report["source"], "text_bibliographic_byline")

    def test_author_inference_finds_stacked_academic_names(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding\n"
                        "Jacob Devlin\n"
                        "Ming-Wei Chang\n"
                        "Kenton Lee\n"
                        "Kristina Toutanova\n"
                        "Google AI Language\n"
                        "Abstract\n"
                    ),
                }
            ],
            title_hint="BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        )
        self.assertIn("Jacob Devlin", report["author"])
        self.assertIn("Kristina Toutanova", report["author"])
        self.assertEqual(report["source"], "text_top_block_names")

    def test_author_inference_finds_comma_separated_academic_names(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Dense Passage Retrieval for Open-Domain Question Answering\n"
                        "Vladimir Karpukhin, Barlas Oguz, Sewon Min, Patrick Lewis\n"
                        "Facebook AI\n"
                        "Abstract\n"
                    ),
                }
            ],
            title_hint="Dense Passage Retrieval for Open-Domain Question Answering",
        )
        self.assertIn("Vladimir Karpukhin", report["author"])
        self.assertIn("Patrick Lewis", report["author"])
        self.assertEqual(report["source"], "text_top_block_names")

    def test_author_inference_finds_semicolon_and_middle_dot_names(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Example Paper Title\n"
                        "Alice Smith; Bob Jones · Carla Gomez and David Chen\n"
                        "Department of Computer Science\n"
                        "Abstract\n"
                    ),
                }
            ],
            title_hint="Example Paper Title",
        )
        self.assertIn("Alice Smith", report["author"])
        self.assertIn("Bob Jones", report["author"])
        self.assertIn("Carla Gomez", report["author"])
        self.assertIn("David Chen", report["author"])
        self.assertEqual(report["source"], "text_top_block_names")

    def test_author_inference_finds_adjacent_names_with_footnote_numbers(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Learning Transferable Visual Models From Natural Language Supervision\n"
                        "Alec Radford * 1 Jong Wook Kim * 1 Chris Hallacy 1 Aditya Ramesh 1 Ilya Sutskever 1\n"
                        "Abstract\n"
                    ),
                }
            ],
            title_hint="Learning Transferable Visual Models From Natural Language Supervision",
        )
        self.assertIn("Alec Radford", report["author"])
        self.assertIn("Jong Wook Kim", report["author"])
        self.assertIn("Ilya Sutskever", report["author"])
        self.assertEqual(report["source"], "text_top_block_names")

    def test_author_inference_accepts_lowercase_name_particles(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Example Paper Title\n"
                        "Jan van der Meer & Maria de la Cruz\n"
                        "Institute for Applied Systems\n"
                        "Abstract\n"
                    ),
                }
            ],
            title_hint="Example Paper Title",
        )
        self.assertIn("Jan van der Meer", report["author"])
        self.assertIn("Maria de la Cruz", report["author"])
        self.assertEqual(report["source"], "text_top_block_names")

    def test_author_inference_finds_edited_by_pattern(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": "Collected Essays\n\nEdited by Jane Doe\n\nUniversity Press",
                }
            ],
            title_hint="Collected Essays",
        )
        self.assertEqual(report["author"], "Jane Doe")
        self.assertEqual(report["source"], "text_edited_by")

    def test_author_inference_ignores_crossmark_and_citation_boilerplate(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Example Journal\n"
                        "ISSN: 0000-0000 (Print) 0000-0000 (Online) Journal homepage: www.example.test/journal\n"
                        "Example Book: A Review of Public Ideas\n"
                        "on Policy, Merit, and Community\n"
                        "Sample Reviewer\n"
                        "To cite this article: Sample Reviewer (2018) Example Book...\n"
                        "Published online: 07 Aug 2018.\n"
                        "Submit your article to this journal\n"
                        "Article views: 585\n"
                        "View related articles\n"
                        "View Crossmark data\n"
                    ),
                }
            ],
            title_hint="Example Book: A Review of Public Ideas on Policy, Merit, and Community",
        )
        self.assertEqual(report["author"], "Sample Reviewer")
        self.assertIn(report["source"], {"text_top_block_names", "text_role_followup"})

    def test_author_inference_reads_instructor_label(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Example Course Reading List\n"
                        "Annotated Bibliography Assignment\n"
                        "Instructor: Dr. Example Instructor (instructor@example.edu)\n"
                    ),
                }
            ],
            title_hint="Annotated Bibliography Assignment",
        )
        self.assertEqual(report["author"], "Dr. Example Instructor")
        self.assertEqual(report["source"], "text_instructor_label")

    def test_author_inference_reads_author_s_label(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": (
                        "Example Research Article\n"
                        "Author(s): Alex Harper and Jordan Lee\n"
                        "Source: Example Journal\n"
                    ),
                }
            ],
            title_hint="Example Research Article",
        )
        self.assertIn("Alex Harper", report["author"])
        self.assertIn("Jordan Lee", report["author"])
        self.assertEqual(report["source"], "text_author_label")

    def test_author_inference_can_fallback_to_filename(self):
        report = pipeline.infer_author_from_filename(
            Path("Example Document - Sample Author.pdf"),
            title_hint="Example Document",
        )
        self.assertEqual(report["author"], "Sample Author")
        self.assertEqual(report["source"], "filename_author_fallback")

    def test_author_inference_uses_title_page_name_before_copyright_byline(self):
        report = pipeline.infer_author_from_text_samples(
            [
                {
                    "page": 3,
                    "text": (
                        "Example Book\n"
                        "AN EXAMPLE TITLE AND\n"
                        "ITS SAMPLE SUBTITLE\n"
                        "Sample Author\n"
                        "Example University Press   Example City   2006\n"
                    ),
                },
                {
                    "page": 4,
                    "text": (
                        "© 2006 Example University Press\n"
                        "All rights reserved\n"
                        "Designed by Example Designer\n"
                        "Typeset in Example Typeface\n"
                        "by Example Typesetting Service\n"
                    ),
                },
            ],
            title_hint="Example Book: An Example Title and Its Sample Subtitle",
        )
        self.assertEqual(report["author"], "Sample Author")
        self.assertEqual(report["source"], "text_top_block_names")

    def test_organization_byline_is_not_treated_as_person_name(self):
        self.assertFalse(
            pipeline.looks_like_person_name(
                "Inc. Tseng Information Systems",
                title_hint="Not Quite White: White Trash and the Boundaries of Whiteness",
            )
        )

    def test_native_segment_title_uses_compact_identity_stem(self):
        row = {
            "source_short_label": "Sample Author",
            "source_title": "Not Quite White",
            "pdf_page": 99,
            "logical_page": "85",
            "page_line_start": 12,
            "page_line_end": 18,
            "segment_index": 332,
            "chapter": "Chapter 3 The Feebleminded Menace",
            "section": "",
        }
        title = pipeline.native_segment_title(row, include_heading=True)
        self.assertEqual(title, "sample-author-p99-lp85-ln12-18-s00332-ch03")

    def test_native_page_parent_title_omits_child_segment_number(self):
        row = {
            "source_short_label": "Sample Author",
            "source_title": "Not Quite White",
            "pdf_page": 99,
            "logical_page": "85",
            "page_line_start": 1,
            "page_line_end": 29,
            "chapter": "Chapter 3 The Feebleminded Menace",
            "section": "",
        }
        title = pipeline.native_page_parent_title(row, include_heading=True)
        self.assertEqual(title, "sample-author-p99-lp85-page-parent-ch03")

    def test_native_segment_title_uses_a_page_range_for_unsegmented_content(self):
        row = {
            "source_short_label": "Sample Author",
            "source_title": "Not Quite White",
            "pdf_page": 10,
            "pdf_page_end": 21,
            "logical_page": "1",
            "page_line_start": None,
            "page_line_end": None,
            "segment_index": 1,
            "chapter": "",
            "section": "",
        }
        self.assertEqual(
            pipeline.native_segment_title(row, include_heading=True),
            "sample-author-p10-21-lp1-s00001",
        )

    def test_make_segments_page_mode_keeps_one_segment_per_page(self):
        pages = [
            {"page": 10, "text": "Chapter 1\n" + ("A" * 700)},
            {"page": 11, "text": "Chapter 1\n" + ("B" * 700)},
        ]
        source_meta = {
            "source_id": "source-1",
            "source_title": "Example",
            "source_author": "Author",
            "source_short_label": "Example",
            "source_sha256": "a" * 64,
            "source_published_epoch_ms": None,
            "metadata_provenance": {},
            "body_start": 10,
            "end_matter_start": None,
            "boundary_confidence": "high",
            "repeated_headers": [],
            "repeated_footers": [],
            "duplicate_pages": {},
        }
        segments = pipeline.make_segments(
            Path("example.pdf"),
            "pymupdf",
            pages,
            10,
            None,
            source_meta,
            300,
            outline=None,
            segment_mode="page",
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["pdf_page"], 10)
        self.assertEqual(segments[1]["pdf_page"], 11)
        self.assertEqual(segments[0]["char_start_page"], 0)
        self.assertEqual(segments[0]["char_end_page"], len(segments[0]["text"]))
        self.assertEqual(segments[0]["page_line_start"], 1)
        self.assertEqual(segments[0]["page_line_end"], 2)

    def test_make_segments_none_prepares_one_full_content_record_with_page_spans(self):
        pages = [
            {"page": 10, "text": "Chapter 1\n" + ("A" * 700)},
            {"page": 11, "text": "Chapter 1\n" + ("B" * 700)},
        ]
        source_meta = {
            "source_id": "source-1",
            "source_title": "Example",
            "source_author": "Author",
            "source_short_label": "Example",
            "source_sha256": "a" * 64,
            "source_published_epoch_ms": None,
            "metadata_provenance": {},
            "body_start": 10,
            "end_matter_start": None,
            "boundary_confidence": "high",
            "repeated_headers": [],
            "repeated_footers": [],
            "duplicate_pages": {},
        }
        segments = pipeline.make_segments(
            Path("example.pdf"), "pymupdf", pages, 10, None, source_meta, 300,
            outline=None, segment_mode="none",
        )

        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual((segment["pdf_page"], segment["pdf_page_end"]), (10, 11))
        self.assertEqual(segment["boundary_debug"]["reason"], "no_local_segmentation")
        self.assertEqual(len(segment["page_spans"]), 2)
        self.assertIn("A" * 100, segment["text"])
        self.assertIn("B" * 100, segment["text"])
        payload = pipeline.generate_api_payloads(segments, "native_header")[0]
        self.assertIn("PDF page range: 10-11", payload["metadata"]["description"])
        self.assertIn("exact page-level citations are unavailable", payload["metadata"]["description"])

    def test_photographed_regions_keep_the_original_pdf_page_and_expose_region_metadata(self):
        source_meta = {
            "source_id": "source-1", "source_title": "Example", "source_author": "Author",
            "source_short_label": "Example", "source_sha256": "a" * 64,
            "source_published_epoch_ms": None, "metadata_provenance": {}, "body_start": 7,
            "end_matter_start": None, "boundary_confidence": "high", "repeated_headers": [],
            "repeated_footers": [], "duplicate_pages": {},
        }
        pages = [{
            "page": 7,
            "text": "left and right source regions",
            "reading_regions": [
                {"text": "Left region " + ("alpha " * 40), "reading_region": "spread_left", "reading_region_index": 1, "reading_region_count": 2, "source_column_index": 1},
                {"text": "Right region " + ("beta " * 40), "reading_region": "spread_right", "reading_region_index": 2, "reading_region_count": 2, "source_column_index": 2},
            ],
        }]
        segments = pipeline.make_segments(Path("example.pdf"), "unstructured", pages, 7, None, source_meta, 600, segment_mode="page")
        self.assertEqual(len(segments), 2)
        self.assertEqual([row["pdf_page"] for row in segments], [7, 7])
        self.assertEqual([row["logical_page"] for row in segments], [7, 7])
        self.assertEqual([row["reading_region_index"] for row in segments], [1, 2])
        self.assertIn("r2", pipeline.native_segment_title(segments[1]))
        payload = pipeline.generate_api_payloads(segments, "native_header")[1]
        self.assertIn("PDF page: 7.", payload["metadata"]["description"])
        self.assertIn("Reading region: spread right (2 of 2).", payload["metadata"]["description"])

    def test_photographed_ocr_removes_only_a_verified_repeated_running_header(self):
        pages = [
            {"page": 2, "text": "CULTURE AND CIVILIZATION\n\nOpening prose.", "reading_regions": [{
                "text": "CULTURE AND CIVILIZATION\n\nOpening prose.", "ocr_method": "tesseract_photographed_page_crop"
            }]},
            {"page": 3, "text": "CULTURE AS HISTORY\n\nFirst body page.", "reading_regions": [{
                "text": "CULTURE AS HISTORY\n\nFirst body page.", "ocr_method": "tesseract_photographed_page_crop"
            }]},
            {"page": 4, "text": "Middle body page.", "reading_regions": [{
                "text": "Middle body page.", "ocr_method": "tesseract_photographed_page_crop"
            }]},
            {"page": 5, "text": "CULTURE AS HISTORY\n\nhistory and science; stories of philosophy.", "reading_regions": [{
                "text": "CULTURE AS HISTORY\n\nhistory and science; stories of philosophy.", "ocr_method": "tesseract_photographed_page_crop"
            }]},
        ]
        cleaned, evidence = pipeline.remove_verified_photographed_ocr_running_headers(pages)
        self.assertEqual(evidence["verified_headers"], ["CULTURE AS HISTORY"])
        self.assertEqual(cleaned[0]["text"], "CULTURE AND CIVILIZATION\n\nOpening prose.")
        self.assertEqual(cleaned[1]["text"], "First body page.")
        self.assertEqual(cleaned[3]["text"], "history and science; stories of philosophy.")
        self.assertEqual(cleaned[3]["reading_regions"][0]["raw_text"].splitlines()[0], "CULTURE AS HISTORY")

    def test_photographed_ocr_cleanup_removes_only_isolated_margin_marks(self):
        import rag_pdf_tools

        cleaned = rag_pdf_tools.clean_photographed_ocr_text(
            "The argument | continues.\nphilo- ')\nsophes\nEnlighten- , ment\n|\nA real (parenthetical) remains."
        )
        self.assertEqual(
            cleaned,
            "The argument  continues.\nphilo- sophes\nEnlighten-ment\nA real (parenthetical) remains.",
        )

    def test_photographed_page_crop_preserves_right_edge_glyphs_with_margin_guard(self):
        import rag_pdf_tools

        left, _top, right, _bottom = rag_pdf_tools.PHOTOGRAPHED_PAGE_CROP
        self.assertLess(left, .04)
        self.assertGreaterEqual(right, .96)
        self.assertLess(right, 1.0)

    def test_photographed_spread_specs_preserve_one_source_page_as_two_regions(self):
        import rag_pdf_tools

        self.assertEqual(rag_pdf_tools.photographed_spread_crop_specs(600, 900), [])
        self.assertEqual(rag_pdf_tools.photographed_spread_crop_specs(1800, 900), [])
        specs = rag_pdf_tools.photographed_spread_crop_specs(1800, 900, .5)
        self.assertEqual([spec[0] for spec in specs], ["spread_left", "spread_right"])
        self.assertEqual([spec[1] for spec in specs], [1, 2])

    def test_photographed_spread_keeps_ambiguous_narrow_column_in_full_page_fallback(self):
        import rag_pdf_tools

        narrow_specs = rag_pdf_tools.photographed_spread_crop_specs(1800, 900, .31)
        # Even substantial OCR text must not make geometry alone discard a
        # plausible index/category column on the narrow side.
        self.assertFalse(
            rag_pdf_tools.keep_photographed_spread_regions(
                narrow_specs, ["A\n" * 180, "Readable source text.\n" * 80]
            )
        )
        equal_specs = rag_pdf_tools.photographed_spread_crop_specs(1800, 900, .5)
        self.assertTrue(
            rag_pdf_tools.keep_photographed_spread_regions(
                equal_specs, ["A\n" * 180, "B\n" * 180]
            )
        )

    def test_photographed_spread_requires_a_visible_fold_not_landscape_shape_alone(self):
        from PIL import Image, ImageDraw, ImageStat
        import rag_pdf_tools

        plain = Image.new("L", (900, 400), color=245)
        self.assertIsNone(rag_pdf_tools.photographed_fold_gutter_fraction(plain, ImageStat))
        folded = plain.copy()
        ImageDraw.Draw(folded).rectangle((440, 40, 460, 360), fill=80)
        gutter = rag_pdf_tools.photographed_fold_gutter_fraction(folded, ImageStat)
        self.assertIsNotNone(gutter)
        self.assertGreater(gutter, .46)
        self.assertLess(gutter, .54)

    def test_unrotated_photo_gate_requires_uneven_dark_border_evidence(self):
        from PIL import Image, ImageDraw, ImageStat
        import rag_pdf_tools

        page = SimpleNamespace(rotation=0)
        flatbed = Image.new("L", (500, 800), color=250)
        self.assertFalse(rag_pdf_tools.photographed_page_visual_signal(page, flatbed, ImageStat))
        photographed = flatbed.copy()
        ImageDraw.Draw(photographed).rectangle((0, 0, 35, 800), fill=190)
        self.assertTrue(rag_pdf_tools.photographed_page_visual_signal(page, photographed, ImageStat))
        self.assertTrue(
            rag_pdf_tools.photographed_page_visual_signal(SimpleNamespace(rotation=180), flatbed, ImageStat)
        )

    def test_embedded_scan_region_requires_a_substantial_single_image(self):
        import rag_pdf_tools

        page = SimpleNamespace(
            rect=fitz.Rect(0, 0, 600, 800),
            get_images=lambda full=True: [(17,)],
            get_image_rects=lambda _xref: [fitz.Rect(70, 80, 530, 720)],
        )
        fraction = rag_pdf_tools.embedded_scanned_image_fraction(page)
        self.assertIsNotNone(fraction)
        self.assertGreater(fraction[0], .11)
        self.assertLess(fraction[2], .89)

        incidental = SimpleNamespace(
            rect=fitz.Rect(0, 0, 600, 800),
            get_images=lambda full=True: [(17,)],
            get_image_rects=lambda _xref: [fitz.Rect(220, 300, 380, 460)],
        )
        self.assertIsNone(rag_pdf_tools.embedded_scanned_image_fraction(incidental))

    def test_neighbour_runover_requires_adjacent_text_evidence_before_exclusion(self):
        import rag_pdf_tools

        narrow = "sociol analy commen inequal educat politi instit stratification mobility"
        dominant = "The complete target page has only the intended source text."
        pages = [
            {"page": 1, "text": "Sociological analysis commentary inequality education political institutions stratification mobility."},
            {
                "page": 2,
                "text": "full page including spillover",
                "reading_regions": [{"text": "full page including spillover", "annotations_excluded": "outer_margin_crop"}],
                "_neighbour_page_runover_candidate": {
                    "side": "spread_left", "narrow_text": narrow, "dominant_text": dominant,
                    "dominant_crop_fraction": [.3, .05, .95, .95],
                },
            },
        ]
        rag_pdf_tools.resolve_confirmed_neighbour_runovers(pages)
        self.assertEqual(pages[1]["text"], dominant)
        self.assertEqual(
            pages[1]["spread_preprocessing"]["neighbour_page_runover"]["decision"], "confirmed_excluded"
        )

        ambiguous = [{
            "page": 1, "text": "Unrelated material.",
            "reading_regions": [{"text": "complete page", "annotations_excluded": "outer_margin_crop"}],
            "_neighbour_page_runover_candidate": {
                "side": "spread_right", "narrow_text": narrow, "dominant_text": dominant,
                "dominant_crop_fraction": [.05, .05, .7, .95],
            },
        }]
        rag_pdf_tools.resolve_confirmed_neighbour_runovers(ambiguous)
        self.assertEqual(ambiguous[0]["text"], "Unrelated material.")
        self.assertEqual(
            ambiguous[0]["spread_preprocessing"]["neighbour_page_runover"]["decision"], "ambiguous_retained"
        )

    def test_neighbour_runover_can_confirm_short_word_beginnings(self):
        import rag_pdf_tools

        narrow = "soci anal comm ineq educ polit instit stratif mobili repres"
        adjacent = (
            "Sociological analysis commentary inequality education political "
            "institutions stratification mobility representations."
        )
        match = rag_pdf_tools.neighbour_fragment_match(narrow, adjacent)
        self.assertTrue(match["confirmed"])
        self.assertGreaterEqual(match["matched"], 8)

    def test_neighbour_runover_ignores_short_common_word_noise(self):
        import rag_pdf_tools

        narrow = "the and for are but not you all can had has was were"
        adjacent = "The ordinary page contains unrelated prose and common function words."
        match = rag_pdf_tools.neighbour_fragment_match(narrow, adjacent)
        self.assertFalse(match["confirmed"])
        self.assertEqual(match["fragments"], 0)

    def test_build_page_line_map_tracks_original_page_lines(self):
        raw = "Header Line\n\nFirst body line\nSecond body line\n\nThird body line"
        mapped = pipeline.build_page_line_map(raw)
        self.assertEqual(mapped["clean_text"], "Header Line\n\nFirst body line Second body line\n\nThird body line")
        start, end = pipeline.detect_page_line_range(mapped, 13, 45)
        self.assertEqual((start, end), (3, 4))

    def test_build_page_parent_rows_groups_child_segments_by_page(self):
        segments = [
            {
                "source_id": "source-1",
                "source_title": "Example",
                "source_author": "Author",
                "source_short_label": "Ex",
                "source_file": "example.pdf",
                "source_sha256": "a" * 64,
                "backend": "pymupdf",
                "pdf_page": 12,
                "logical_page": "3",
                "document_region": "body",
                "part": "",
                "chapter": "Introduction",
                "section": "",
                "subsection": "",
                "segment_id": "seg-1",
                "segment_index": 1,
                "char_start_page": 0,
                "char_end_page": 100,
                "page_line_start": 4,
                "page_line_end": 7,
                "text": "First half.",
            },
            {
                "source_id": "source-1",
                "source_title": "Example",
                "source_author": "Author",
                "source_short_label": "Ex",
                "source_file": "example.pdf",
                "source_sha256": "a" * 64,
                "backend": "pymupdf",
                "pdf_page": 12,
                "logical_page": "3",
                "document_region": "body",
                "part": "",
                "chapter": "Introduction",
                "section": "",
                "subsection": "",
                "segment_id": "seg-2",
                "segment_index": 2,
                "char_start_page": 101,
                "char_end_page": 220,
                "page_line_start": 8,
                "page_line_end": 12,
                "text": "Second half.",
            },
        ]
        parents = pipeline.build_page_parent_rows(segments)
        self.assertEqual(len(parents), 1)
        parent = parents[0]
        self.assertEqual(parent["pdf_page"], 12)
        self.assertEqual(parent["logical_page"], "3")
        self.assertEqual(parent["segment_count"], 2)
        self.assertIn("seg-1", parent["segment_ids"])
        self.assertIn("seg-2", parent["segment_ids"])
        self.assertIn("First half.", parent["text"])
        self.assertIn("Second half.", parent["text"])
        self.assertEqual(parent["page_line_start"], 4)
        self.assertEqual(parent["page_line_end"], 12)

    def test_representation_comparison_rows_reports_segments_and_page_parents(self):
        segments = [
            {"text": "A" * 100},
            {"text": "B" * 120},
        ]
        parents = [
            {"text": "A" * 220},
        ]
        rows = pipeline.representation_comparison_rows(
            segments,
            parents,
            512,
            75,
            {"max_chunk_length": "2048"},
        )
        names = [row["representation"] for row in rows]
        self.assertIn("passage_segments", names)
        self.assertIn("page_parents", names)
        self.assertIn("relationship", names)

    def test_metadata_layer_visibility_rows_reports_direct_and_derived_fields(self):
        segment_payloads = [
            {
                "metadata": {
                    "title": "Example -- pdf-p0012 -- logical-p3 -- s00008",
                    "docAuthor": "Author",
                    "description": "PDF page: 12. Logical page: 3. Segment: s00008.",
                    "docSource": "local-pdf://sha256/abc",
                    "chunkSource": "segment://seg-1",
                }
            }
        ]
        page_parent_payloads = [
            {
                "metadata": {
                    "title": "Example -- pdf-p0012 -- logical-p3 -- page-parent",
                    "docAuthor": "Author",
                    "description": "PDF page: 12. Logical page: 3. Parent page id: parent-1.",
                    "docSource": "local-pdf://sha256/abc",
                    "chunkSource": "page-parent://parent-1",
                }
            }
        ]
        rows = pipeline.metadata_layer_visibility_rows(
            segment_payloads,
            page_parent_payloads,
            {"schema": {"title": "", "docAuthor": "", "description": "", "docSource": "", "chunkSource": ""}},
            {"metadata_fields_seen": ["title", "docAuthor", "description", "docSource", "chunkSource", "text"]},
        )
        by_field = {row["field"]: row for row in rows}
        self.assertEqual(by_field["title"]["anythingllm_raw_text_contract"], "yes")
        self.assertEqual(by_field["title"]["chunk_text_visible_expected"], "yes_sourceDocument_header")
        self.assertEqual(by_field["pdf_page"]["field_type"], "derived_provenance")
        self.assertEqual(by_field["pdf_page"]["chunk_text_visible_expected"], "only_if_encoded_into_promoted_text_fields")

    def test_expected_upload_needles_uses_payload_metadata(self):
        payloads = [
            {
                "metadata": {
                    "title": "Sample Author -- pdf-p0099 -- logical-p85 -- page-parent",
                    "description": "PDF page: 99. Logical page: 85.",
                    "chunkSource": "page-parent://source-1::pdf-p0099",
                }
            }
        ]
        needles = pipeline.expected_upload_needles(payloads, source_sha="abcdef1234567890")
        self.assertIn("abcdef1234567890", needles)  # pragma: allowlist secret -- fixed test needle
        self.assertIn("page-parent://source-1::pdf-p0099", needles)
        self.assertTrue(any("Sample Author" in needle for needle in needles))

    def test_upload_plan_rows_to_expected_payloads_preserves_native_metadata_fields(self):
        rows = [
            {
                "filename": "sample-p0009-s00001.txt",
                "title": "Sample -- p0009 -- s00001",
                "docAuthor": "Author",
                "description": "PDF page 9; segment one.",
                "docSource": "local-pdf://sha256/abc",
                "chunkSource": "segment://sample-1",
            }
        ]
        payloads = pipeline.upload_plan_rows_to_expected_payloads(rows)
        self.assertEqual(payloads[0]["filename"], "sample-p0009-s00001.txt")
        self.assertEqual(payloads[0]["metadata"]["title"], "sample-p0009-s00001.txt")
        self.assertEqual(payloads[0]["metadata"]["chunkSource"], "segment://sample-1")
        self.assertIn("Sample -- p0009 -- s00001", payloads[0]["metadata"]["description"])

    def test_raw_text_payloads_include_filename_required_by_anythingllm_workspace_rows(self):
        segment = {
            "source_title": "Sample Source",
            "source_author": "Author",
            "source_sha256": "abc123",
            "source_id": "sample",
            "source_short_label": "sample",
            "source_file": "sample.pdf",
            "backend": "pymupdf",
            "pdf_page": 9,
            "logical_page": 3,
            "page_line_start": 1,
            "page_line_end": 4,
            "segment_id": "sample-p0009-s00001",
            "segment_index": 1,
            "document_region": "body",
            "part": "",
            "chapter": "",
            "section": "",
            "subsection": "",
            "text": "Sample text.",
        }
        payload = pipeline.generate_api_payloads([segment], "native_header")[0]
        self.assertEqual(payload["filename"], "sample-p9-lp3-ln1-4-s00001.txt")
        self.assertEqual(payload["metadata"]["title"], "sample-p9-lp3-ln1-4-s00001")

    def test_page_parent_payloads_include_filename_required_by_anythingllm_workspace_rows(self):
        parent = {
            "parent_id": "sample::pdf-p0009",
            "source_title": "Sample Source",
            "source_author": "Author",
            "source_short_label": "sample",
            "source_sha256": "abc123",
            "pdf_page": 9,
            "logical_page": 3,
            "page_line_start": 1,
            "page_line_end": 4,
            "segment_count": 2,
            "part": "",
            "chapter": "",
            "section": "",
            "subsection": "",
            "title": "sample-p0009-lp3-page-parent",
            "text": "Parent text.",
        }
        payload = pipeline.generate_page_parent_payloads([parent], "native_header")[0]
        self.assertEqual(payload["filename"], "sample-p0009-lp3-page-parent.txt")
        self.assertEqual(payload["metadata"]["chunkSource"], "page-parent://sample::pdf-p0009")

    def test_explain_observed_columns_reports_layers(self):
        rows = pipeline.explain_observed_columns(
            {
                "sample_workspace_document": {
                    "id": 1,
                    "docId": "doc-1",
                    "filename": "raw.json",
                    "docpath": "custom-documents/raw.json",
                    "metadata": "{}",
                    "metadata_parsed": {
                        "title": "Example",
                        "docAuthor": "Author",
                        "chunkSource": "segment://seg-1",
                    },
                },
                "sample_custom_document_record": {
                    "title": "Example",
                    "pageContent": "Body text",
                },
                "sample_lancedb_row": {
                    "title": "Example",
                    "text": "Chunk text",
                    "vector": [0.1, 0.2],
                },
            }
        )
        layers = {row["layer"] for row in rows}
        self.assertIn("workspace_documents_row", layers)
        self.assertIn("workspace_metadata_json", layers)
        self.assertIn("custom_document_json", layers)
        self.assertIn("lancedb_row", layers)

    def test_harmonization_rows_reports_exceeding_units(self):
        rows = pipeline.harmonization_rows(
            [{"text": "A" * 300}, {"text": "B" * 900}],
            [{"text": "C" * 1200}],
            512,
            75,
            {"max_chunk_length": "700"},
        )
        by_name = {row["representation"]: row for row in rows}
        self.assertEqual(by_name["passage_segments"]["effective_limit"], 512)
        self.assertEqual(by_name["passage_segments"]["units_exceeding_effective_limit"], 1)
        self.assertEqual(by_name["page_parents"]["units_exceeding_effective_limit"], 1)

    def test_response_contains_page_segment_accepts_page_parent_page_only(self):
        payload = {
            "metadata": {
                "title": "sample-author-p99-lp85-ln1-29-page-parent-ch03",
                "description": "PDF page: 99. Logical page: 85.",
                "chunkSource": "page-parent://source-1::pdf-p0099",
            }
        }
        expected = pipeline.expected_page_segment_tokens(payload)
        self.assertEqual(expected["representation"], "page_parent")
        self.assertTrue(
            pipeline.response_contains_page_segment(
                "The passage is on PDF page 99 in the cited source.",
                expected,
            )
        )

    def test_storage_snapshot_diff_reports_added_rows(self):
        before = {"status": "complete", "tables": [{"name": "workspace", "row_count": 3, "columns": ["id"]}]}
        after = {"status": "complete", "tables": [{"name": "workspace", "row_count": 5, "columns": ["id", "text"]}]}
        diff = pipeline.compare_storage_snapshots(before, after)
        self.assertEqual(diff["total_added_rows"], 2)
        self.assertEqual(diff["rows"][0]["added_rows"], 2)

    def test_native_compatibility_probe_uses_first_and_middle_segments(self):
        payloads = [
            {"textContent": str(index), "metadata": {"title": f"segment-{index}", "chunkSource": f"segment://{index}"}}
            for index in range(5)
        ]
        calls = []
        original_post = pipeline.post_json
        try:
            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append((url, body))
                if url.endswith("/raw-text"):
                    return 200, json.dumps({"documents": [{"location": f"custom/{body['metadata']['title']}.json"}]})
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            report = pipeline.maybe_upload_payloads(
                "http://anythingllm",
                "key",
                payloads,
                upload_limit=2,
                workspace_slug="test",
            )
        finally:
            pipeline.post_json = original_post
        uploaded_titles = [body["metadata"]["title"] for url, body in calls if url.endswith("/raw-text")]
        self.assertEqual(uploaded_titles, ["segment-0", "segment-2"])
        self.assertTrue(
            all("addToWorkspaces" not in body for url, body in calls if url.endswith("/raw-text"))
        )
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["embedded"], 2)

    def test_native_upload_writes_redacted_receipts_and_marks_ambiguous_post_for_reconciliation(self):
        payload = {
            "textContent": "private prepared source text must not be copied to receipts",
            "metadata": {
                "docSource": "local-pdf://sha256/" + "a" * 64,
                "chunkSource": "segment://example::p0001::s00001",
            },
        }
        original_post = pipeline.post_json
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = Path(temp_dir) / "submission-receipts.jsonl"
            try:
                pipeline.post_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out"))
                report = pipeline.maybe_upload_payloads(
                    "http://anythingllm", "key", [payload], workspace_slug="test",
                    submission_receipt_path=receipt_path, run_id="run-test",
                )
            finally:
                pipeline.post_json = original_post
            receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["state"] for row in receipts], ["submitted", "submission_unknown"])
        self.assertEqual(receipts[-1]["run_id"], "run-test")
        self.assertIn("Reconcile", receipts[-1]["next_check"])
        self.assertNotIn("private prepared source", receipt_path.read_text(encoding="utf-8") if receipt_path.exists() else json.dumps(receipts))
        self.assertEqual(report["submission_receipt_path"], str(receipt_path))

    def test_output_capacity_preflight_reports_insufficient_space_without_writing_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf = Path(temp_dir) / "tiny.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            original_disk_usage = pipeline.shutil.disk_usage
            try:
                pipeline.shutil.disk_usage = lambda _path: shutil._ntuple_diskusage(10_000, 9_999, 1)
                result = pipeline.output_capacity_preflight(pdf, Path(temp_dir))
            finally:
                pipeline.shutil.disk_usage = original_disk_usage
        self.assertEqual(result["status"], "insufficient_space")
        self.assertGreater(result["required_free_bytes"], result["available_free_bytes"])

    def test_safe_get_retry_retries_transient_read_but_not_a_success(self):
        original_get = pipeline.get_json
        calls = []
        try:
            def fake_get(*_args, **_kwargs):
                calls.append(1)
                return (503, "busy") if len(calls) == 1 else (200, '{"online": true}')
            pipeline.get_json = fake_get
            result = pipeline.get_json_with_retry(
                "http://anythingllm/api/ping", max_attempts=3,
                sleeper=lambda _seconds: None, jitter=lambda _a, _b: 0,
            )
        finally:
            pipeline.get_json = original_get
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(len(result["attempts"]), 2)

    def test_embedding_updates_are_bounded_and_report_each_accepted_batch(self):
        original_post = pipeline.post_json
        calls = []
        try:
            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append((url, body, api_key))
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "test",
                [f"custom-documents/segment-{index}.json" for index in range(45)],
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual([len(call[1]["adds"]) for call in calls], [1] + [2] * 22)
        self.assertEqual(result["requested"], 45)
        self.assertEqual(result["accepted"], 45)
        self.assertEqual([batch["accepted"] for batch in result["batches"]], [1] + [2] * 22)
        self.assertTrue(all("submission_seconds" in batch for batch in result["batches"]))
        self.assertTrue(all("batch_elapsed_seconds" in batch for batch in result["batches"]))
        self.assertEqual(result["errors"], [])

    def test_desktop_queue_submits_every_location_once_and_keeps_final_verification(self):
        original_post = pipeline.post_json
        calls = []
        statuses = []
        locations = [f"custom-documents/segment-{index}.json" for index in range(12)]
        try:
            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append((url, body, api_key))
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_desktop_queue(
                "http://anythingllm",
                "key",
                "queue-workspace",
                locations,
                status_callback=lambda message, _report: statuses.append(message),
                batch_verifier=lambda report: {
                    "status": "pass",
                    "matching_vector_rows": len(report["locations"]),
                },
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["adds"], locations)
        self.assertEqual(result["requested"], len(locations))
        self.assertEqual(result["accepted"], len(locations))
        self.assertEqual(result["queue_records"], len(locations))
        self.assertEqual(result["submission_strategy"], "desktop_queue")
        self.assertEqual(len(result["batches"]), 1)
        self.assertEqual(result["batches"][0]["verification"]["status"], "pass")
        self.assertTrue(any("sequential queue" in message for message in statuses))

    def test_desktop_queue_does_not_replay_an_uncertain_full_list_submission(self):
        original_post = pipeline.post_json
        calls = []
        locations = ["custom-documents/a.json", "custom-documents/b.json"]
        try:
            def timeout_post(*_args, **_kwargs):
                calls.append(1)
                raise TimeoutError("timed out")

            pipeline.post_json = timeout_post
            result = pipeline.update_workspace_embeddings_desktop_queue(
                "http://anythingllm",
                "key",
                "queue-workspace",
                locations,
                batch_verifier=lambda report: {
                    "status": "pass",
                    "matching_vector_rows": len(report["locations"]),
                },
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(calls, [1])
        self.assertEqual(result["accepted"], len(locations))
        self.assertEqual(result["batches"][0]["acceptance_basis"], "vector_observed_after_client_timeout")
        self.assertEqual(result["errors"], [])

    def test_embedding_update_skips_exact_vectors_that_already_exist(self):
        original_post = pipeline.post_json
        calls = []
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (calls.append(1) or 200, "{}")
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                ["custom-documents/segment-1.json", "custom-documents/segment-2.json"],
                batch_inspector=lambda batch: {
                    "status": "pass",
                    "matching_vector_rows": batch["requested"],
                },
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(calls, [])
        self.assertEqual(result["accepted"], 2)
        self.assertTrue(all(batch["searchability_proven"] for batch in result["batches"]))
        self.assertTrue(all(
            batch["acceptance_basis"] == "exact_vectors_preexisted_before_submission"
            for batch in result["batches"]
        ))

    def test_slow_warmup_switches_remaining_requests_to_single_record_mode(self):
        original_post = pipeline.post_json
        batch_sizes = []
        try:
            def fake_post(_url, body, api_key=None, **_kwargs):
                batch_sizes.append(len(body["adds"]))
                return 200, "{}"

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(5)],
                adaptive_single_record_threshold_seconds=0,
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(batch_sizes, [1, 1, 1, 1, 1])
        self.assertTrue(result["adaptive_single_record_mode"])

    def test_planned_embedding_batch_count_includes_one_record_warmup(self):
        self.assertEqual(pipeline.planned_embedding_batch_count(0), 0)
        self.assertEqual(pipeline.planned_embedding_batch_count(1), 1)
        self.assertEqual(pipeline.planned_embedding_batch_count(2), 2)
        self.assertEqual(pipeline.planned_embedding_batch_count(8), 5)

    def test_embedding_submission_timeout_uses_records_and_accepted_warmup_rate(self):
        self.assertEqual(pipeline.embedding_submission_timeout_seconds(2), 240.0)
        self.assertEqual(pipeline.embedding_submission_timeout_seconds(1, 30), 180.0)
        self.assertEqual(pipeline.embedding_submission_timeout_seconds(2, 90), 345.0)
        self.assertEqual(pipeline.embedding_submission_timeout_seconds(2, 600), 480.0)

    def test_embedding_updates_record_a_dynamic_timeout_after_warmup(self):
        original_post = pipeline.post_json
        original_perf_counter = pipeline.time.perf_counter
        observed_timeouts = []
        try:
            elapsed = [0.0]

            def fake_perf_counter():
                elapsed[0] += 90.0
                return elapsed[0]

            pipeline.time.perf_counter = fake_perf_counter

            def fake_post(_url, _body, api_key=None, timeout=None):
                observed_timeouts.append(timeout)
                return 200, "{}"

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(3)],
                adaptive_single_record_threshold_seconds=999,
            )
        finally:
            pipeline.post_json = original_post
            pipeline.time.perf_counter = original_perf_counter

        self.assertEqual(observed_timeouts, [240.0, 345.0])
        self.assertEqual(result["batches"][0]["submission_timeout_basis"], "bootstrap")
        self.assertEqual(result["batches"][1]["submission_timeout_basis"], "accepted_warmup_throughput")

    def test_recovery_manifest_excludes_individually_confirmed_timeout_records(self):
        original_post = pipeline.post_json
        locations = ["custom-documents/segment-1.json", "custom-documents/segment-2.json"]
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "embedding-batch-ledger.json"
            try:
                pipeline.post_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out"))
                pipeline.update_workspace_embeddings_batched(
                    "http://anythingllm",
                    "key",
                    "safe-workspace",
                    locations,
                    warmup_batch_size=0,
                    warmup_batch_count=0,
                    ledger_path=ledger,
                    batch_verifier=lambda _batch: {
                        "status": "timeout",
                        "confirmed_locations": [locations[0]],
                        "unresolved_locations": [locations[1]],
                    },
                )
            finally:
                pipeline.post_json = original_post
            recovery = json.loads((Path(temp_dir) / "resume-embedding-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(recovery["recovery"]["remaining_locations"], [locations[1]])

    def test_embedding_checkpoint_policy_verifies_first_periodic_and_final_batches(self):
        original_post = pipeline.post_json
        verified = []
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (200, json.dumps({"success": True}))
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "test",
                [f"custom-documents/segment-{index}.json" for index in range(25)],
                batch_verifier=lambda batch: (verified.append(batch["batch"]) or {"status": "pass"}),
                verification_interval=3,
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(verified, [1, 3, 6, 9, 12, 13])
        self.assertEqual(result["deferred_verification_batches"], [2, 4, 5, 7, 8, 10, 11])
        self.assertEqual(result["verification_mode"], "checkpoint")
        self.assertEqual(result["batches"][1]["verification"]["status"], "deferred_to_checkpoint")
        self.assertTrue(result["final_verification_required"])

    def test_embedding_every_batch_policy_remains_available_for_diagnostics(self):
        original_post = pipeline.post_json
        verified = []
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (200, json.dumps({"success": True}))
            pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "test",
                [f"custom-documents/segment-{index}.json" for index in range(15)],
                batch_verifier=lambda batch: (verified.append(batch["batch"]) or {"status": "pass"}),
                verification_mode="every_batch",
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(verified, [1, 2, 3, 4, 5, 6, 7, 8])

    def test_embedding_update_stops_after_a_rejected_batch_and_keeps_partial_progress(self):
        original_post = pipeline.post_json
        calls = []
        try:
            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append(body)
                if len(calls) == 2:
                    return 503, "busy"
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "test",
                [f"custom-documents/segment-{index}.json" for index in range(45)],
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["batches"][-1]["http_status"], 503)
        self.assertEqual(result["errors"][0]["batch"], 2)

    def test_embedding_update_defers_accepted_batch_with_delayed_vector_visibility(self):
        original_post = pipeline.post_json
        calls = []
        try:
            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append(body)
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "test",
                [f"custom-documents/segment-{index}.json" for index in range(10)],
                batch_verifier=lambda _batch: {"status": "timeout", "message": "No vectors observed."},
            )
        finally:
            pipeline.post_json = original_post

        self.assertGreater(len(calls), 1)
        self.assertEqual(result["accepted"], 10)
        self.assertTrue(all(batch["submission_state"] == "accepted" for batch in result["batches"]))
        self.assertEqual(result["batches"][0]["verification"]["status"], "pending_delayed_indexing")
        self.assertIn(1, result["deferred_verification_batches"])
        self.assertFalse(result["errors"])

    def test_embedding_timeout_is_durable_and_never_misreported_as_a_rejected_or_complete_batch(self):
        original_post = pipeline.post_json
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "embedding-batch-ledger.json"
            try:
                def timeout_post(*_args, **_kwargs):
                    raise TimeoutError("timed out")

                pipeline.post_json = timeout_post
                result = pipeline.update_workspace_embeddings_batched(
                    "http://anythingllm",
                    "key",
                    "safe-workspace",
                    ["custom-documents/segment-1.json"],
                    ledger_path=ledger,
                )
            finally:
                pipeline.post_json = original_post

            saved = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["batches"][0]["submission_state"], "unresolved")
        self.assertEqual(saved["batches"][0]["submission_state"], "unresolved")

    def test_embedding_timeout_can_continue_only_after_exact_vector_reconciliation(self):
        original_post = pipeline.post_json
        calls = []
        verified = []
        try:
            def timeout_then_succeed(*_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise TimeoutError("timed out")
                return 200, json.dumps({"success": True})

            pipeline.post_json = timeout_then_succeed
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(8)],
                batch_verifier=lambda batch: (
                    verified.append(batch["batch"]) or {"status": "pass", "matching_vector_rows": 2}
                ),
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 5)
        self.assertEqual(verified, [1, 5])
        self.assertEqual(result["accepted"], 8)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["batches"][0]["acceptance_basis"], "vector_observed_after_client_timeout")
        self.assertEqual(
            result["runtime_events"][0]["classification"],
            "client_timeout_recovered_by_vector_observation",
        )

    def test_embedding_scheduler_hard_caps_requested_concurrency_at_one(self):
        original_post = pipeline.post_json
        calls = []
        verified = []
        try:
            def fake_post(_url, body, api_key=None, **_kwargs):
                calls.append(list(body.get("adds") or []))
                return 200, json.dumps({"success": True})

            def fake_verify(batch):
                verified.append(batch["batch"])
                if batch["batch"] == 2:
                    return {
                        "status": "partial_vector_coverage",
                        "matching_vector_rows": 1,
                        "lancedb_matching_rows": 1,
                        "message": "Only one of two records materialized.",
                    }
                return {"status": "pass", "matching_vector_rows": 2, "lancedb_matching_rows": 2}

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(20)],
                batch_size=2,
                warmup_batch_size=0,
                warmup_batch_count=0,
                concurrent_batch_limit=6,
                initial_concurrent_batches=4,
                verification_mode="every_batch",
                batch_verifier=fake_verify,
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 2)
        self.assertEqual(verified, [1, 2])
        self.assertNotIn("parallelism_schedule", result)
        self.assertEqual(result["batches"][1]["submission_state"], "verification_failed")
        self.assertNotIn("recommended_resume_parallelism", result)

    def test_embedding_timeout_can_continue_after_workspace_attachment_without_replaying_it(self):
        original_post = pipeline.post_json
        calls = []
        locations = ["custom-documents/segment-1.json", "custom-documents/segment-2.json"]
        try:
            def timeout_then_succeed(*_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise TimeoutError("timed out")
                return 200, json.dumps({"success": True})

            pipeline.post_json = timeout_then_succeed
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                locations,
                batch_size=1,
                warmup_batch_size=0,
                warmup_batch_count=0,
                concurrent_batch_limit=1,
                verification_mode="every_batch",
                batch_verifier=lambda batch: (
                    {"status": "workspace_attached_pending_vectors"}
                    if batch["batch"] == 1
                    else {"status": "pass"}
                ),
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(
            result["batches"][0]["acceptance_basis"],
            "workspace_attached_after_client_timeout",
        )
        self.assertEqual(result["batches"][0]["lifecycle_state"], "workspace_attached")
        self.assertEqual(
            result["runtime_events"][0]["classification"],
            "client_timeout_recovered_by_workspace_attachment",
        )

    def test_runtime_retrieval_accepts_expected_source_within_top_n(self):
        original_gate = pipeline.read_workspace_model_gate
        original_key = pipeline.resolve_anythingllm_api_key
        original_request = pipeline.post_json_captured_with_retry
        payload = {
            "textContent": "A distinctive discussion of bounded asynchronous reconciliation for testing.",
            "metadata": {
                "title": "Test PDF p0001 s00001",
                "chunkSource": "segment://test::p0001::s00001",
            },
        }
        expected_source = payload["metadata"]["chunkSource"]
        try:
            pipeline.read_workspace_model_gate = lambda *_args, **_kwargs: {"status": "pass"}
            pipeline.resolve_anythingllm_api_key = lambda *_args, **_kwargs: ("key", "provided_api_key")

            def fake_request(url, _body, **_kwargs):
                if url.endswith("/vector-search"):
                    return {
                        "http_status": 200,
                        "data": {"results": [
                            {"metadata": {"chunkSource": "segment://other::p0002::s00001"}},
                            {"metadata": {"chunkSource": expected_source}},
                        ]},
                        "error": "", "endpoint": url, "error_class": "none",
                        "total_elapsed_seconds": 0.01, "retry_count": 0, "attempts": [],
                    }
                return {
                    "http_status": 200,
                    "data": {"textResponse": "The source is page 1, segment 1."},
                    "error": "", "endpoint": url, "error_class": "none",
                    "total_elapsed_seconds": 0.01, "retry_count": 0, "attempts": [],
                }

            pipeline.post_json_captured_with_retry = fake_request
            result = pipeline.validate_anythingllm_native_runtime(
                "http://anythingllm", "key", "workspace", [payload], 0, Path("."),
                embedder_probe_override={"status": "pass"},
            )
        finally:
            pipeline.read_workspace_model_gate = original_gate
            pipeline.resolve_anythingllm_api_key = original_key
            pipeline.post_json_captured_with_retry = original_request

        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["vector_checks"][0]["top_1_expected"])
        self.assertTrue(result["vector_checks"][0]["expected_in_top_n"])
        self.assertEqual(result["vector_checks"][0]["expected_result_rank"], 2)

    def test_interrupted_embedding_writes_an_explicit_resume_manifest(self):
        original_post = pipeline.post_json
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "embedding-batch-ledger.json"
            try:
                pipeline.post_json = lambda *_args, **_kwargs: (503, "busy")
                pipeline.update_workspace_embeddings_batched(
                    "http://anythingllm",
                    "key",
                    "safe-workspace",
                    ["custom-documents/segment-1.json", "custom-documents/segment-2.json"],
                    ledger_path=ledger,
                )
            finally:
                pipeline.post_json = original_post
            recovery = json.loads((Path(temp_dir) / "resume-embedding-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(recovery["recovery"]["state"], "resume_available")
        self.assertEqual(recovery["recovery"]["remaining_locations"], [
            "custom-documents/segment-1.json", "custom-documents/segment-2.json",
        ])
    def test_operator_cancellation_stops_before_the_next_embedding_batch(self):
        original_post = pipeline.post_json
        calls = []
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (calls.append(1) or 200, json.dumps({"success": True}))
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(10)],
                cancel_callback=lambda: True,
            )
        finally:
            pipeline.post_json = original_post
        self.assertEqual(calls, [])
        self.assertEqual(result["batches"][0]["submission_state"], "cancelled_before_submission")

    def test_embedding_scheduler_never_runs_more_than_one_request(self):
        original_post = pipeline.post_json
        active = 0
        peak = 0
        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "embedding-batch-ledger.json"
            try:
                def measured_post(*_args, **_kwargs):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        time.sleep(0.03)
                        return 200, json.dumps({"success": True})
                    finally:
                        with lock:
                            active -= 1

                pipeline.post_json = measured_post
                result = pipeline.update_workspace_embeddings_batched(
                    "http://anythingllm",
                    "key",
                    "safe-workspace",
                    [f"custom-documents/segment-{index}.json" for index in range(25)],
                    concurrent_batch_limit=6,
                    verification_mode="none",
                    ledger_path=ledger,
                )
            finally:
                pipeline.post_json = original_post
            persisted = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(result["accepted"], 25)
        self.assertEqual(result["errors"], [])
        self.assertEqual(peak, 1)
        self.assertNotIn("parallelism_schedule", result)
        self.assertEqual(persisted["concurrent_batch_limit"], 1)

    def test_embedding_scheduler_serializes_after_exact_vector_evidence(self):
        """Production advances only after each batch has vector evidence."""
        original_post = pipeline.post_json
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (200, json.dumps({"success": True}))
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(25)],
                concurrent_batch_limit=6,
                verification_mode="every_batch",
                batch_verifier=lambda _report: {"status": "pass"},
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(result["accepted"], 25)
        self.assertNotIn("parallelism_schedule", result)
        self.assertTrue(all(
            batch.get("lifecycle_state") == "vector_observed"
            for batch in result["batches"]
        ))

    def test_embedding_scheduler_holds_later_batch_without_exact_vector_evidence(self):
        """HTTP acknowledgement alone must not start another request."""
        original_post = pipeline.post_json
        try:
            pipeline.post_json = lambda *_args, **_kwargs: (200, json.dumps({"success": True}))
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                [f"custom-documents/segment-{index}.json" for index in range(25)],
                concurrent_batch_limit=6,
                verification_mode="every_batch",
                batch_verifier=lambda _report: {"status": "timeout"},
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(result["accepted"], 1)
        self.assertNotIn("parallelism_schedule", result)
        self.assertEqual(result["batches"][0]["submission_state"], "reconciliation_pending")
        self.assertEqual(result["errors"][0]["status"], "pending_delayed_indexing")

    def test_embedding_rate_limit_retries_once_at_single_request_limit(self):
        original_post = pipeline.post_json
        original_sleep = pipeline.time.sleep
        locations = [f"custom-documents/segment-{index}.json" for index in range(20)]
        calls = []
        sleeps = []
        rate_limited_once = False
        lock = threading.Lock()
        try:
            def fake_post(_url, body, api_key=None, **_kwargs):
                nonlocal rate_limited_once
                batch_locations = tuple(body["adds"])
                with lock:
                    calls.append(batch_locations)
                    if batch_locations == tuple(locations[2:4]) and not rate_limited_once:
                        rate_limited_once = True
                        return 429, "Too Many Requests"
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            pipeline.time.sleep = lambda seconds: sleeps.append(seconds)
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                locations,
                batch_size=2,
                warmup_batch_size=0,
                warmup_batch_count=0,
                concurrent_batch_limit=6,
                verification_mode="none",
            )
        finally:
            pipeline.post_json = original_post
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["accepted"], len(locations))
        self.assertEqual(result["errors"], [])
        self.assertEqual(calls.count(tuple(locations[0:2])), 1)
        self.assertEqual(calls.count(tuple(locations[2:4])), 2)
        self.assertEqual(calls.count(tuple(locations[4:6])), 1)
        self.assertEqual(sleeps, [pipeline.ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS])
        self.assertEqual(result["batches"][1]["rate_limit_retry_count"], 1)
        self.assertEqual(result["runtime_events"][0]["event"], "rate_limit_retry")
        self.assertNotIn("parallelism_fallback_applied", result)

    def test_repeated_embedding_rate_limit_stops_after_one_safe_retry(self):
        original_post = pipeline.post_json
        original_sleep = pipeline.time.sleep
        locations = [f"custom-documents/segment-{index}.json" for index in range(8)]
        calls = []
        sleeps = []
        try:
            def fake_post(_url, body, api_key=None, **_kwargs):
                batch_locations = tuple(body["adds"])
                calls.append(batch_locations)
                if batch_locations == tuple(locations[2:4]):
                    return 429, "Too Many Requests"
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            pipeline.time.sleep = lambda seconds: sleeps.append(seconds)
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                locations,
                batch_size=2,
                warmup_batch_size=0,
                warmup_batch_count=0,
                verification_mode="none",
            )
        finally:
            pipeline.post_json = original_post
            pipeline.time.sleep = original_sleep

        self.assertEqual(result["accepted"], 2)
        self.assertEqual(calls.count(tuple(locations[2:4])), 2)
        self.assertEqual(calls.count(tuple(locations[4:6])), 0)
        self.assertEqual(sleeps, [pipeline.ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS])
        self.assertEqual(result["batches"][1]["rate_limit_retry_count"], 1)
        self.assertEqual(result["errors"][0]["status"], 429)

    def test_embedding_non_429_rejection_stops_before_later_serial_batches(self):
        original_post = pipeline.post_json
        locations = [f"custom-documents/segment-{index}.json" for index in range(12)]
        calls = []
        try:
            def fake_post(_url, body, api_key=None, **_kwargs):
                batch_locations = tuple(body["adds"])
                calls.append(batch_locations)
                if batch_locations == tuple(locations[2:4]):
                    return 503, "Service Unavailable"
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            result = pipeline.update_workspace_embeddings_batched(
                "http://anythingllm",
                "key",
                "safe-workspace",
                locations,
                batch_size=2,
                warmup_batch_size=0,
                warmup_batch_count=0,
                concurrent_batch_limit=6,
                verification_mode="none",
            )
        finally:
            pipeline.post_json = original_post

        self.assertEqual(len(calls), 2)
        self.assertNotIn("parallelism_fallback_applied", result)
        self.assertNotIn("recommended_resume_parallelism", result)
        self.assertNotIn("parallelism_schedule", result)

    def test_inflight_cancellation_keeps_every_unsubmitted_location_in_resume_manifest(self):
        original_post = pipeline.post_json
        locations = [f"custom-documents/segment-{index}.json" for index in range(10)]
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "embedding-batch-ledger.json"
            try:
                pipeline.post_json = lambda *_args, **_kwargs: (calls.append(1) or 200, json.dumps({"success": True}))
                result = pipeline.update_workspace_embeddings_batched(
                    "http://anythingllm",
                    "key",
                    "safe-workspace",
                    locations,
                    ledger_path=ledger,
                    cancel_callback=lambda: len(calls) >= 1,
                )
            finally:
                pipeline.post_json = original_post
            recovery = json.loads((Path(temp_dir) / "resume-embedding-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, [1])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual([batch["submission_state"] for batch in result["batches"]], ["accepted", "cancelled_before_submission"])
        self.assertEqual(recovery["recovery"]["from_batch"], 2)
        self.assertEqual(recovery["recovery"]["remaining_locations"], locations[1:])

    def test_temporary_key_cleanup_retries_once_and_records_attempts(self):
        original_delete = pipeline.delete_temporary_desktop_api_key
        calls = []
        responses = iter([
            {"status": "delete_failed", "error": "first failure"},
            {"status": "deleted", "error": ""},
        ])
        try:
            pipeline.delete_temporary_desktop_api_key = lambda api_url, key_id: (
                calls.append((api_url, key_id)) or next(responses)
            )
            result = pipeline.cleanup_temporary_desktop_api_key(
                "http://127.0.0.1:3001", "temporary-key-id"
            )
        finally:
            pipeline.delete_temporary_desktop_api_key = original_delete

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["attempt_count"], 2)
        self.assertTrue(result["retry_attempted"])
        self.assertEqual(result["first_attempt_status"], "delete_failed")

    def test_cleanup_warning_promotes_final_run_to_needs_review(self):
        selected = {"readiness_status": "ready", "readiness_reasons": []}
        upload_report = {
            "status": "complete_with_key_cleanup_warning",
            "temporary_key_cleanup": {
                "status": "delete_failed",
                "attempt_count": 2,
                "retry_attempted": True,
            },
        }

        applied = pipeline.apply_temporary_key_cleanup_review(
            selected, upload_report, prepare_and_upload=True
        )

        self.assertTrue(applied)
        self.assertEqual(upload_report["status"], "complete_with_key_cleanup_warning")
        self.assertEqual(selected["readiness_status"], "needs_review")
        self.assertIn("cleanup_warning", selected["readiness_reasons"])
        self.assertEqual(upload_report["cleanup_obligations"][0]["kind"], "temporary_desktop_api_key")
        self.assertEqual(upload_report["cleanup_obligations"][0]["attempt_count"], 2)
        self.assertIn("needs review", upload_report["warnings"][0]["warning"].casefold())

    def test_cleanup_warning_is_not_added_for_local_only_or_successful_cleanup(self):
        selected = {"readiness_status": "ready", "readiness_reasons": []}
        upload_report = {"temporary_key_cleanup": {"status": "delete_failed"}}
        self.assertFalse(
            pipeline.apply_temporary_key_cleanup_review(
                selected, upload_report, prepare_and_upload=False
            )
        )
        self.assertEqual(selected["readiness_status"], "ready")
        self.assertNotIn("cleanup_obligations", upload_report)

    def test_historical_retrieval_preset_message_does_not_claim_a_universal_winner(self):
        import rag_pdf_gradio_app as app

        original_persist = app.persist_anythingllm_chunk_settings
        original_refresh = app.refresh_anythingllm_settings
        original_bridge = app.refresh_desktop_after_anythingllm_mutation
        try:
            app.persist_anythingllm_chunk_settings = lambda *_args, **_kwargs: {
                "runtime_verification_message": " verified."
            }
            app.refresh_anythingllm_settings = lambda *_args, **_kwargs: ("", {}, {}, {}, {}, {}, {})
            app.refresh_desktop_after_anythingllm_mutation = lambda: " Bridge refreshed."
            result = app.apply_tested_retrieval_preset_ui(True)
        finally:
            app.persist_anythingllm_chunk_settings = original_persist
            app.refresh_anythingllm_settings = original_refresh
            app.refresh_desktop_after_anythingllm_mutation = original_bridge

        message = result[-1]
        self.assertIn("historical retrieval comparison preset", message)
        self.assertIn("current local operating default remains page-bounded subchunking with a 750-character target", message)
        self.assertNotIn("best tested retrieval preset", message)

        original_resolved_state = app.anythingllm_resolved_state
        try:
            app.anythingllm_resolved_state = lambda *_args, **_kwargs: {
                "chunking": {"chunk_size": 350, "chunk_overlap": 0},
                "embedder": {"max_chunk_length": 32768, "policy": {"recommended_limit": 32768, "risk_label": "ok"}},
            }
            reference = app.anythingllm_settings_reference_html().casefold()
        finally:
            app.anythingllm_resolved_state = original_resolved_state

        self.assertIn("historical retrieval comparison preset", reference)
        self.assertIn("page-preserving automatic", reference)
        self.assertNotIn("best tested", reference)

    def test_blank_key_uses_managed_local_service_key(self):
        payloads = [
            {"textContent": "test", "metadata": {"title": "segment", "chunkSource": "segment://1"}}
        ]
        original_create = pipeline.create_temporary_desktop_api_key
        original_delete = pipeline.delete_temporary_desktop_api_key
        original_post = pipeline.post_json
        original_resolve = pipeline.resolve_anythingllm_api_key
        calls = []
        try:
            pipeline.create_temporary_desktop_api_key = lambda api_url: {
                "status": "created",
                "id": 7,
                "secret": "temporary-secret",  # pragma: allowlist secret -- synthetic state fixture
                "error": "",
            }
            pipeline.delete_temporary_desktop_api_key = lambda api_url, key_id: {
                "status": "deleted",
                "error": "",
            }
            pipeline.resolve_anythingllm_api_key = lambda *args: ("managed-secret", "managed_local_service_key")

            def fake_post(url, body, api_key=None, **_kwargs):
                calls.append((url, api_key))
                if url.endswith("/raw-text"):
                    return 200, json.dumps({"documents": [{"location": "custom/segment.json"}]})
                return 200, json.dumps({"success": True})

            pipeline.post_json = fake_post
            report = pipeline.maybe_upload_payloads(
                "http://127.0.0.1:3001",
                "",
                payloads,
                upload_limit=1,
                workspace_slug="test",
            )
        finally:
            pipeline.create_temporary_desktop_api_key = original_create
            pipeline.delete_temporary_desktop_api_key = original_delete
            pipeline.post_json = original_post
            pipeline.resolve_anythingllm_api_key = original_resolve

        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["authentication_mode"], "managed_local_service_key")
        self.assertEqual(report["temporary_key_cleanup"]["status"], "not_applicable")
        self.assertTrue(all(api_key == "managed-secret" for _, api_key in calls))  # pragma: allowlist secret -- synthetic managed-key assertion

    def test_resume_prefers_managed_local_service_key(self):
        import rag_pdf_gradio_app as app

        original_output_dir = app.AUTO_OUTPUT_DIR
        original_resolve = app.resolve_anythingllm_api_key
        original_temporary = app.create_temporary_desktop_api_key
        original_update = app.update_workspace_embeddings_batched
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "app-run-test" / "document" / "resume-embedding-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({
                "workspace_slug": "safe-workspace",
                "batch_size": 5,
                "recovery": {"state": "resume_available", "from_batch": 2, "remaining_locations": ["custom/segment-6.json"]},
            }), encoding="utf-8")
            calls = []
            try:
                app.AUTO_OUTPUT_DIR = root
                app.resolve_anythingllm_api_key = lambda *_args, **_kwargs: ("managed-secret", "managed_local_service_key")
                app.create_temporary_desktop_api_key = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("temporary key should not be created"))
                app.update_workspace_embeddings_batched = lambda api_url, key, slug, locations, **_kwargs: (
                    calls.append((api_url, key, slug, locations)) or {"accepted": len(locations), "errors": []}
                )
                html_result, _button = app.resume_latest_embedding_manifest("http://127.0.0.1:3001", "", "safe-workspace")
            finally:
                app.AUTO_OUTPUT_DIR = original_output_dir
                app.resolve_anythingllm_api_key = original_resolve
                app.create_temporary_desktop_api_key = original_temporary
                app.update_workspace_embeddings_batched = original_update

        self.assertEqual(calls, [("http://127.0.0.1:3001", "managed-secret", "safe-workspace", ["custom/segment-6.json"])])
        self.assertIn("managed_local_service_key", html_result)

    def test_resume_reconciles_late_timed_out_batch_before_resubmission(self):
        import rag_pdf_gradio_app as app

        original_documents_dir = app.default_anythingllm_documents_dir
        original_storage_dir = app.default_anythingllm_storage_dir
        original_verify = app.verify_anythingllm_post_upload
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            documents = root / "documents"
            relative_locations = [f"custom-documents/segment-{number}.json" for number in (1, 2)]
            for number, relative in enumerate(relative_locations, start=1):
                target = documents / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({
                    "title": f"segment-{number}",
                    "docSource": "local-pdf://sha256/" + "a" * 64,
                    "chunkSource": f"segment://segment-{number}",
                }), encoding="utf-8")
            manifest = {
                "workspace_slug": "safe-workspace",
                "batches": [{
                    "batch": 2,
                    "submission_state": "unresolved",
                    "locations": relative_locations,
                }],
                "recovery": {
                    "state": "resume_available",
                    "remaining_locations": [*relative_locations, "custom-documents/never-submitted.json"],
                },
            }
            try:
                app.default_anythingllm_documents_dir = lambda: documents
                app.default_anythingllm_storage_dir = lambda: root
                app.verify_anythingllm_post_upload = lambda *_args, **_kwargs: {
                    "status": "pass",
                    "lancedb_matching_rows": 2,
                }
                remaining, report = app.reconcile_resume_manifest_late_vectors(manifest)
            finally:
                app.default_anythingllm_documents_dir = original_documents_dir
                app.default_anythingllm_storage_dir = original_storage_dir
                app.verify_anythingllm_post_upload = original_verify

        self.assertEqual(report["reconciled_locations"], 2)
        self.assertEqual(remaining, ["custom-documents/never-submitted.json"])

    def test_file_upload_transport_uses_segment_files_and_embed_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample_file = tmp_path / "sample-p0001-s00001.txt"
            sample_file.write_text("hello", encoding="utf-8")
            upload_rows = [
                {
                    "filename": sample_file.name,
                    "title": "sample-p0001-s00001",
                    "docAuthor": "Author",
                    "description": "PDF page 1; segment 1.",
                    "docSource": "local-pdf://sha256/abc",
                    "chunkSource": "segment://seg-1",
                    "text_file": str(sample_file),
                }
            ]
            original_post_multipart = pipeline.post_multipart_form
            original_post_json = pipeline.post_json
            calls = []
            try:
                def fake_post_multipart(url, fields, file_field_name, file_path, api_key=None, timeout=120):
                    calls.append(("multipart", url, json.loads(fields["metadata"]), Path(file_path).name, api_key))
                    return 200, json.dumps({"documents": [{"location": "custom-documents/sample-p0001-s00001.json"}]})

                def fake_post_json(url, body, api_key=None, **_kwargs):
                    calls.append(("json", url, body, api_key))
                    return 200, json.dumps({"success": True})

                pipeline.post_multipart_form = fake_post_multipart
                pipeline.post_json = fake_post_json
                report = pipeline.maybe_upload_to_anythingllm(
                    "http://anythingllm",
                    "key",
                    [],
                    upload_limit=1,
                    workspace_slug="test",
                    upload_transport="file_upload",
                    upload_plan_rows=upload_rows,
                )
            finally:
                pipeline.post_multipart_form = original_post_multipart
                pipeline.post_json = original_post_json

            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["transport"], "file_upload")
            self.assertEqual(report["uploaded"], 1)
            self.assertEqual(report["embedded"], 1)
            self.assertEqual(calls[0][0], "multipart")
            self.assertEqual(calls[0][3], sample_file.name)
            self.assertEqual(calls[0][2]["chunkSource"], "segment://seg-1")

    def test_file_upload_transport_moves_segments_into_pdf_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            storage = tmp_path / "anythingllm-storage"
            loose_uploaded = storage / "documents" / "custom-documents" / "sample-p0001-s00001.json"
            loose_uploaded.parent.mkdir(parents=True)
            loose_uploaded.write_text("{}", encoding="utf-8")
            sample_file = tmp_path / "sample-p0001-s00001.txt"
            sample_file.write_text("hello", encoding="utf-8")
            upload_rows = [
                {
                    "filename": sample_file.name,
                    "title": "sample-p0001-s00001",
                    "docSource": "local-pdf://sha256/abc",
                    "chunkSource": "segment://seg-1",
                    "text_file": str(sample_file),
                }
            ]
            original_post_multipart = pipeline.post_multipart_form
            original_post_json = pipeline.post_json
            try:
                pipeline.post_multipart_form = lambda *_args, **_kwargs: (
                    200,
                    json.dumps({"documents": [{"location": "custom-documents/sample-p0001-s00001.json"}]}),
                )
                pipeline.post_json = lambda *_args, **_kwargs: (200, json.dumps({"success": True}))
                report = pipeline.maybe_upload_to_anythingllm(
                    "http://anythingllm",
                    "key",
                    [],
                    upload_limit=1,
                    workspace_slug="test",
                    upload_transport="file_upload",
                    upload_plan_rows=upload_rows,
                    storage_dir=storage,
                    folder_name="custom-documents/Original-PDF-abc12345",  # pragma: allowlist secret -- synthetic folder fixture
                )
            finally:
                pipeline.post_multipart_form = original_post_multipart
                pipeline.post_json = original_post_json

            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["locations"], ["custom-documents/Original-PDF-abc12345/sample-p0001-s00001.json"])  # pragma: allowlist secret -- synthetic folder fixture
            self.assertEqual(report["document_folder_name"], "custom-documents/Original-PDF-abc12345")  # pragma: allowlist secret -- synthetic folder fixture
            self.assertTrue((storage / "documents" / "custom-documents" / "Original-PDF-abc12345" / "sample-p0001-s00001.json").exists())  # pragma: allowlist secret -- synthetic folder fixture
            self.assertFalse(loose_uploaded.exists())

    def test_file_upload_transport_submits_relative_location_when_desktop_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            storage = tmp_path / "anythingllm-storage"
            uploaded = storage / "documents" / "custom-documents" / "drawer-visible.json"
            uploaded.parent.mkdir(parents=True)
            uploaded.write_text("{}", encoding="utf-8")
            source = tmp_path / "drawer-visible.txt"
            source.write_text("hello", encoding="utf-8")
            upload_rows = [{
                "filename": source.name,
                "title": "drawer-visible",
                "docSource": "local-pdf://sha256/abc",
                "chunkSource": "segment://drawer-visible",
                "text_file": str(source),
            }]
            original_post_multipart = pipeline.post_multipart_form
            original_post_json = pipeline.post_json
            json_bodies = []
            try:
                pipeline.post_multipart_form = lambda *_args, **_kwargs: (
                    200,
                    json.dumps({"documents": [{"location": str(uploaded)}]}),
                )
                def fake_post_json(_url, body, **_kwargs):
                    json_bodies.append(body)
                    return 200, json.dumps({"success": True})
                pipeline.post_json = fake_post_json
                report = pipeline.maybe_upload_to_anythingllm(
                    "http://anythingllm",
                    "key",
                    [],
                    workspace_slug="test",
                    upload_transport="file_upload",
                    upload_plan_rows=upload_rows,
                    storage_dir=storage,
                    folder_name="custom-documents",
                )
            finally:
                pipeline.post_multipart_form = original_post_multipart
                pipeline.post_json = original_post_json

            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["locations"], ["custom-documents/drawer-visible.json"])
            self.assertEqual(json_bodies[0]["adds"], ["custom-documents/drawer-visible.json"])

    def test_file_upload_compatibility_probe_uses_first_and_middle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upload_rows = []
            for index in range(5):
                sample_file = tmp_path / f"sample-p0001-s{index:05d}.txt"
                sample_file.write_text(f"segment {index}", encoding="utf-8")
                upload_rows.append(
                    {
                        "filename": sample_file.name,
                        "title": f"segment-{index}",
                        "docAuthor": "Author",
                        "description": f"PDF page 1; segment {index}.",
                        "docSource": "local-pdf://sha256/abc",
                        "chunkSource": f"segment://seg-{index}",
                        "text_file": str(sample_file),
                    }
                )
            original_post_multipart = pipeline.post_multipart_form
            original_post_json = pipeline.post_json
            calls = []
            try:
                def fake_post_multipart(url, fields, file_field_name, file_path, api_key=None, timeout=120):
                    calls.append(("multipart", Path(file_path).name, json.loads(fields["metadata"])))
                    return 200, json.dumps({"documents": [{"location": f"custom-documents/{Path(file_path).name}.json"}]})

                def fake_post_json(url, body, api_key=None, **_kwargs):
                    calls.append(("json", body))
                    return 200, json.dumps({"success": True})

                pipeline.post_multipart_form = fake_post_multipart
                pipeline.post_json = fake_post_json
                report = pipeline.maybe_upload_to_anythingllm(
                    "http://anythingllm",
                    "key",
                    [],
                    upload_limit=2,
                    workspace_slug="test",
                    upload_transport="file_upload",
                    upload_plan_rows=upload_rows,
                )
            finally:
                pipeline.post_multipart_form = original_post_multipart
                pipeline.post_json = original_post_json

            uploaded_files = [call[1] for call in calls if call[0] == "multipart"]
            self.assertEqual(uploaded_files, ["sample-p0001-s00000.txt", "sample-p0001-s00002.txt"])
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["uploaded"], 2)
            self.assertEqual(report["embedded"], 2)

    def test_file_upload_transport_requires_upload_plan_rows(self):
        report = pipeline.maybe_upload_to_anythingllm(
            "http://anythingllm",
            "key",
            [],
            upload_limit=1,
            workspace_slug="test",
            upload_transport="file_upload",
            upload_plan_rows=[],
        )
        self.assertEqual(report["status"], "error_missing_upload_files")

    def test_choose_native_upload_transport_respects_explicit_raw_text_for_local_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = pipeline.choose_native_upload_transport(
                "http://127.0.0.1:3001",
                "raw_text",
                upload_plan_rows=[{"filename": "sample.txt"}],
                storage_dir=Path(tmp),
            )
        self.assertEqual(result, "raw_text")

    def test_build_file_upload_rows_from_payloads_preserves_payload_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = pipeline.build_file_upload_rows_from_payloads(
                [
                    {
                        "filename": "sample-p0001-s00001.txt",
                        "textContent": "hello world",
                        "metadata": {
                            "title": "sample-p0001-s00001",
                            "docAuthor": "Author",
                            "description": "PDF page 1; segment 1.",
                            "docSource": "local-pdf://sha256/abc",
                            "chunkSource": "segment://seg-1",
                        },
                    }
                ],
                Path(tmp) / "upload-files",
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["filename"], "sample-p0001-s00001.txt")
            self.assertEqual(rows[0]["title"], "sample-p0001-s00001")
            self.assertEqual(rows[0]["chunkSource"], "segment://seg-1")
            self.assertTrue(Path(rows[0]["text_file"]).exists())
            self.assertEqual(Path(rows[0]["text_file"]).read_text(encoding="utf-8"), "hello world")

    def test_temporary_desktop_key_is_loopback_only(self):
        result = pipeline.create_temporary_desktop_api_key("https://anythingllm.example")
        self.assertEqual(result["status"], "not_local_desktop")

    def test_metadata_schema_uses_managed_local_service_key(self):
        original_create = pipeline.create_temporary_desktop_api_key
        original_delete = pipeline.delete_temporary_desktop_api_key
        original_get = pipeline.get_json
        original_resolve = pipeline.resolve_anythingllm_api_key
        try:
            pipeline.create_temporary_desktop_api_key = lambda api_url: {
                "status": "created",
                "id": 9,
                "secret": "temporary-secret",  # pragma: allowlist secret -- synthetic temporary-key response
                "error": "",
            }
            pipeline.delete_temporary_desktop_api_key = lambda api_url, key_id: {
                "status": "deleted",
                "error": "",
            }
            pipeline.get_json = lambda url, api_key=None, timeout=30: (
                200,
                json.dumps({"schema": pipeline.ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS}),
            )
            pipeline.resolve_anythingllm_api_key = lambda *args: ("managed-secret", "managed_local_service_key")
            report = pipeline.get_anythingllm_metadata_schema(
                "http://127.0.0.1:3001",
                "",
            )
        finally:
            pipeline.create_temporary_desktop_api_key = original_create
            pipeline.delete_temporary_desktop_api_key = original_delete
            pipeline.get_json = original_get
            pipeline.resolve_anythingllm_api_key = original_resolve
        self.assertEqual(report["runtime_api_status"], "reachable_authorized")
        self.assertEqual(report["authentication_mode"], "managed_local_service_key")
        self.assertEqual(report.get("temporary_key_cleanup", {}).get("status", "not_applicable"), "not_applicable")
        self.assertTrue(report["schema_matches_source_contract"])

    def test_post_upload_verifier_reports_vector_only_workspace_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    docId text, filename text, docpath text, workspaceId integer, metadata text
                );
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into document_vectors values ('orphan-doc', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [{"segment_id": "pdf_a_p0001_s00001", "pdf_page": 1, "segment_index": 1}],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["classification"], "native_metadata_llm_visible_legacy_workspace_table_unused")
            self.assertEqual(result["matching_vector_rows"], 1)
            self.assertEqual(result["chunk_survival_flag"], "preserved")
            self.assertEqual(result["page_provenance_risk"], "low")
            self.assertTrue(result["workspace_documents_globally_unused"])

    def test_post_upload_verifier_does_not_claim_to_read_the_authenticated_desktop_drawer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    docId text, filename text, docpath text, workspaceId integer, metadata text
                );
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into workspace_documents values
                    ('doc-1', 'one.txt', 'custom-documents/one.json', 1, '{"chunkSource":"segment://one"}');
                insert into document_vectors values ('doc-1', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [{"metadata": {"chunkSource": "segment://one"}}],
                    frontend_api_url="http://127.0.0.1:3001",
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["classification"], "native_metadata_llm_visible")
            self.assertEqual(
                result["desktop_frontend_observation"],
                "requires_authenticated_desktop_session",
            )
            self.assertIsNone(result["desktop_frontend_document_count"])

    def test_post_upload_verifier_rejects_absolute_workspace_docpaths_for_drawer_visibility(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            absolute_docpath = storage / "documents" / "custom-documents" / "one.json"
            absolute_docpath.parent.mkdir(parents=True)
            absolute_docpath.write_text('{"text":"page 1"}', encoding="utf-8")
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                f"""
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (docId text, filename text, docpath text, workspaceId integer, metadata text);
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into workspace_documents values ('doc-1', 'one.json', '{str(absolute_docpath).replace("\\", "\\\\")}', 1, '{{"chunkSource":"segment://one"}}');
                insert into document_vectors values ('doc-1', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "text_contains_page_or_segment": True,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [{"metadata": {"chunkSource": "segment://one"}}],
                    upload_locations=["custom-documents/one.json"],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors

            self.assertEqual(result["status"], "review")
            self.assertEqual(
                result["classification"],
                "desktop_drawer_workspace_path_incompatible",
            )
            self.assertEqual(result["desktop_drawer_workspace_absolute_paths"], 1)
            self.assertEqual(
                result["desktop_drawer_workspace_path_status"],
                "absolute_paths_incompatible",
            )

    def test_upload_location_inspection_rejects_outside_paths_and_marks_nested_drawer_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            documents = storage / "documents" / "custom-documents"
            documents.mkdir(parents=True)
            root_record = documents / "root.json"
            root_record.write_text('{"text":"page 1 segment 1"}', encoding="utf-8")
            nested = documents / "managed" / "nested.json"
            nested.parent.mkdir()
            nested.write_text('{"text":"page 2 segment 2"}', encoding="utf-8")
            outside = storage / "outside.json"
            outside.write_text('{"confidential":"must not be inspected"}', encoding="utf-8")

            result = pipeline.inspect_uploaded_location_files(
                storage,
                [
                    "custom-documents/root.json",
                    "custom-documents/managed/nested.json",
                    str(outside),
                ],
            )

            self.assertEqual(result["existing_files"], 2)
            self.assertEqual(result["rejected_locations"], 1)
            self.assertEqual(result["desktop_drawer_root_locations"], 1)
            self.assertEqual(result["desktop_drawer_nested_locations"], 1)
            self.assertNotIn("confidential", json.dumps(result["sample_record"]))

    def test_post_upload_verifier_warns_when_uploaded_records_are_nested_and_drawer_hidden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            documents = storage / "documents" / "custom-documents" / "managed"
            documents.mkdir(parents=True)
            location = documents / "one.json"
            location.write_text('{"text":"sourceDocument: one page 1 segment 1"}', encoding="utf-8")
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (docId text, filename text, docpath text, workspaceId integer, metadata text);
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into workspace_documents values ('doc-1', 'one.txt', 'custom-documents/managed/one.json', 1, '{"chunkSource":"segment://one"}');
                insert into document_vectors values ('doc-1', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "text_contains_page_or_segment": True,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [{"metadata": {"chunkSource": "segment://one"}}],
                    upload_locations=["custom-documents/managed/one.json"],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors

            self.assertEqual(result["status"], "review")
            self.assertEqual(result["classification"], "desktop_drawer_layout_nested")
            self.assertEqual(result["desktop_drawer_layout"], "nested_may_be_hidden")

    def test_post_upload_verifier_marks_likely_rechunked_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    docId text, filename text, docpath text, workspaceId integer, metadata text
                );
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into document_vectors values ('orphan-doc', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 22,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [
                        {"segment_id": "pdf_a_p0001_s00001", "pdf_page": 1, "segment_index": 1},
                        {"segment_id": "pdf_a_p0002_s00001", "pdf_page": 2, "segment_index": 1},
                    ],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["chunk_survival_flag"], "likely_rechunked")
            self.assertEqual(result["page_provenance_risk"], "low")
            self.assertIn("Uploaded payloads expanded from 2 to 22", result["message"])

    def test_post_upload_verifier_keeps_missing_workspace_rows_warning_when_global_rows_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    docId text, filename text, docpath text, workspaceId integer, metadata text
                );
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into workspaces values (2, 'Other', 'other');
                insert into workspace_documents values ('other-doc', 'other.txt', 'other.txt', 2, '{"chunkSource":"segment://other"}');
                insert into document_vectors values ('orphan-doc', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 1,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    [{"segment_id": "pdf_a_p0001_s00001", "pdf_page": 1, "segment_index": 1}],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
            self.assertEqual(result["status"], "pass_with_missing_workspace_document_records")
            self.assertEqual(result["classification"], "native_metadata_llm_visible_vector_only")
            self.assertFalse(result["workspace_documents_globally_unused"])

    def test_post_upload_verifier_uses_actual_uploaded_subset_for_survival_ratio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    docId text, filename text, docpath text, workspaceId integer, metadata text
                );
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into document_vectors values ('orphan-doc', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            original_locations = pipeline.inspect_uploaded_location_files
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 2,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                pipeline.inspect_uploaded_location_files = lambda *args, **kwargs: {
                    "status": "complete",
                    "existing_files": 2,
                    "matching_files": 2,
                    "metadata_visible": True,
                    "sample_path": "dummy.json",
                }
                payloads = [
                    {"segment_id": f"pdf_a_p0001_s{i:05d}", "pdf_page": 1, "segment_index": i}
                    for i in range(1, 11)
                ]
                result = pipeline.verify_anythingllm_post_upload(
                    storage,
                    "test",
                    "a" * 64,
                    payloads,
                    upload_locations=["dummy-a.json", "dummy-b.json"],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
                pipeline.inspect_uploaded_location_files = original_locations
            self.assertEqual(result["expected_payload_count"], 10)
            self.assertEqual(result["uploaded_payload_count"], 2)
            self.assertEqual(result["upload_chain_local_expected_count"], 10)
            self.assertEqual(result["upload_chain_custom_documents_matching_count"], 2)
            self.assertEqual(result["upload_chain_lancedb_matching_count"], 2)
            self.assertEqual(result["chunk_survival_flag"], "preserved")
            self.assertEqual(result["chunk_survival_ratio"], 1.0)

    def test_post_upload_verifier_marks_partial_vector_coverage_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (docId text, filename text, docpath text, workspaceId integer, metadata text);
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into document_vectors values ('orphan-doc', 'vector-1');
                """
            )
            con.commit()
            con.close()
            original_native = pipeline.inspect_native_metadata_rows
            original_vectors = pipeline.inspect_lancedb_vector_ids
            original_locations = pipeline.inspect_uploaded_location_files
            try:
                pipeline.inspect_native_metadata_rows = lambda *args, **kwargs: {
                    "matching_rows": 2,
                    "matching_table_names": ["test"],
                    "vector_ids": ["vector-1"],
                    "text_contains_segment_or_page": True,
                }
                pipeline.inspect_lancedb_vector_ids = lambda *args, **kwargs: {
                    "matching_rows": 0,
                    "text_contains_page_or_segment": False,
                }
                pipeline.inspect_uploaded_location_files = lambda *args, **kwargs: {
                    "status": "complete", "existing_files": 5, "matching_files": 5,
                    "metadata_visible": True, "sample_path": "dummy.json",
                }
                payloads = [
                    {"segment_id": f"pdf_a_p0001_s{i:05d}", "pdf_page": 1, "segment_index": i}
                    for i in range(1, 6)
                ]
                result = pipeline.verify_anythingllm_post_upload(
                    storage, "test", "a" * 64, payloads,
                    upload_locations=[f"dummy-{index}.json" for index in range(5)],
                )
            finally:
                pipeline.inspect_native_metadata_rows = original_native
                pipeline.inspect_lancedb_vector_ids = original_vectors
                pipeline.inspect_uploaded_location_files = original_locations

        self.assertEqual(result["chunk_survival_flag"], "partial_or_missing")
        self.assertEqual(result["status"], "partial_vector_coverage")
        self.assertIn("incomplete", result["message"])

    def test_fast_post_upload_observation_uses_bounded_count_not_full_vector_materialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (docId text, filename text, docpath text, workspaceId integer, metadata text);
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                """
            )
            con.commit()
            con.close()
            original_count = pipeline.inspect_native_metadata_count
            original_vectors = pipeline.inspect_lancedb_vector_ids
            original_locations = pipeline.inspect_uploaded_location_files
            try:
                count_calls = []
                def fake_count(*args, **kwargs):
                    count_calls.append(kwargs)
                    return {
                        "status": "complete", "matching_rows": 2,
                        "matching_table_names": ["test"], "text_contains_segment_or_page": False,
                    }
                pipeline.inspect_native_metadata_count = fake_count
                pipeline.inspect_lancedb_vector_ids = lambda *_args, **_kwargs: self.fail(
                    "fast observation must not materialize vector IDs"
                )
                pipeline.inspect_uploaded_location_files = lambda *args, **kwargs: {
                    "status": "complete", "existing_files": 2, "matching_files": 2,
                    "metadata_visible": True, "sample_path": "dummy.json",
                }
                payloads = [{"metadata": {"chunkSource": f"segment://{index}"}} for index in range(2)]
                result = pipeline.verify_anythingllm_post_upload(
                    storage, "test", "a" * 64, payloads, observation_mode="fast"
                )
            finally:
                pipeline.inspect_native_metadata_count = original_count
                pipeline.inspect_lancedb_vector_ids = original_vectors
                pipeline.inspect_uploaded_location_files = original_locations
        self.assertEqual(result["observation_mode"], "fast")
        self.assertEqual(
            result["workspace_document_observation"],
            "counts_only_deferred_to_full_observation",
        )
        self.assertEqual(
            count_calls[0]["expected_chunk_sources"],
            ["segment://0", "segment://1"],
        )
        self.assertEqual(result["lancedb_matching_rows"], 2)
        self.assertIn(result["status"], {"review", "pass_with_missing_workspace_document_records"})

    def test_fast_post_upload_passes_when_exact_page_vectors_are_visible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (docId text, filename text, docpath text, workspaceId integer, metadata text);
                create table document_vectors (docId text, vectorId text);
                insert into workspaces values (1, 'Test', 'test');
                insert into workspace_documents values ('other', 'other.txt', 'other.txt', 1, '{}');
                """
            )
            con.commit()
            con.close()
            original_count = pipeline.inspect_native_metadata_count
            original_locations = pipeline.inspect_uploaded_location_files
            try:
                pipeline.inspect_native_metadata_count = lambda *args, **kwargs: {
                    "status": "complete", "matching_rows": 1,
                    "matching_table_names": ["test"], "text_contains_segment_or_page": True,
                }
                pipeline.inspect_uploaded_location_files = lambda *args, **kwargs: {
                    "status": "complete", "existing_files": 1, "matching_files": 1,
                    "metadata_visible": True, "sample_path": "dummy.json",
                }
                result = pipeline.verify_anythingllm_post_upload(
                    storage, "test", "a" * 64,
                    [{"metadata": {"chunkSource": "segment://0"}}], observation_mode="fast",
                )
            finally:
                pipeline.inspect_native_metadata_count = original_count
                pipeline.inspect_uploaded_location_files = original_locations
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["classification"],
            "native_metadata_llm_visible_fast_document_observation_deferred",
        )

    def test_exact_vector_success_defers_full_post_upload_observation(self):
        report = {
            "status": "pass",
            "matching_vector_rows": 6,
            "lancedb_matching_rows": 6,
        }
        self.assertFalse(
            pipeline.full_post_upload_observation_is_required(report, 6)
        )

    def test_post_upload_mismatch_or_recovery_requires_full_observation(self):
        complete_report = {
            "status": "pass",
            "matching_vector_rows": 6,
            "lancedb_matching_rows": 6,
        }
        missing_report = {
            "status": "partial_vector_coverage",
            "matching_vector_rows": 5,
            "lancedb_matching_rows": 5,
        }
        self.assertTrue(
            pipeline.full_post_upload_observation_is_required(missing_report, 6)
        )
        self.assertTrue(
            pipeline.full_post_upload_observation_is_required(
                complete_report, 6, ambiguous_submission=True
            )
        )
        self.assertTrue(
            pipeline.full_post_upload_observation_is_required(
                complete_report, 6, failed_checkpoint=True
            )
        )

    def test_workspace_storage_inspector_reads_lance_workspace_without_workspace_documents(self):
        try:
            import lancedb
            import pyarrow as pa
        except ImportError:
            self.skipTest("lancedb/pyarrow not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            (storage / "lancedb").mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(storage / "anythingllm.db")
            con.executescript(
                """
                create table workspaces (id integer primary key, name text, slug text);
                create table workspace_documents (
                    id integer primary key, docId text, filename text, docpath text, workspaceId integer, metadata text, createdAt text
                );
                create table document_vectors (id integer primary key, docId text, vectorId text);
                insert into workspaces values (1, 'Vector Only', 'vector-only');
                """
            )
            con.commit()
            con.close()

            db = lancedb.connect(str(storage / "lancedb"))
            schema = pa.schema(
                [
                    ("id", pa.string()),
                    ("url", pa.string()),
                    ("title", pa.string()),
                    ("docAuthor", pa.string()),
                    ("description", pa.string()),
                    ("docSource", pa.string()),
                    ("chunkSource", pa.string()),
                    ("published", pa.string()),
                    ("wordCount", pa.int64()),
                    ("token_count_estimate", pa.int64()),
                    ("text", pa.string()),
                    ("vector", pa.list_(pa.float32(), 2)),
                ]
            )
            db.create_table(
                "vector-only",
                data=[
                    {
                        "id": "vec-1",
                        "url": "file://sample-p15-s00001.txt",
                        "title": "sample-p15-s00001.txt",
                        "docAuthor": "",
                        "description": "PDF page: 15 | Segment: 1",
                        "docSource": "local-pdf://sha256/demo",
                        "chunkSource": "segment://pdf_demo_p0015_s00001",
                        "published": "",
                        "wordCount": 100,
                        "token_count_estimate": 120,
                        "text": "sourceDocument: Demo\nPDF page: 15\nSegment: 1\nBody text",
                        "vector": [0.1, 0.2],
                    }
                ],
                schema=schema,
                mode="overwrite",
            )
            report = pipeline.workspace_storage_inspector(storage, "vector-only")
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["workspace_document_count"], 0)
            self.assertEqual(report["lancedb_workspace_row_count"], 1)
            self.assertEqual(report["embedded_chunk_count"], 1)
            self.assertEqual(report["page_segment_visibility"], "visible_in_chunk_text")
            self.assertEqual(report["sample_lancedb_row"]["title"], "sample-p15-s00001.txt")
            self.assertIn("title", report["lancedb_row_fields"])
            self.assertEqual(report["sqlite_workspace_metadata_fields"], [])
            self.assertEqual(report["custom_document_json_fields"], [])

    def test_strict_and_native_header_payloads_are_distinct(self):
        segment = {
            "source_title": "Example Book",
            "source_short_label": "Example",
            "source_author": "Author",
            "source_sha256": "a" * 64,
            "source_published_epoch_ms": None,
            "pdf_page": 12,
            "logical_page": "3",
            "segment_index": 8,
            "segment_id": "pdf_aaaaaaaaaaaa_p0012_s00008",
            "document_region": "body",
            "part": "",
            "chapter": "Introduction",
            "section": "",
            "subsection": "",
            "text": "Clean text only.",
        }
        strict = pipeline.generate_api_payloads([segment], "strict")[0]
        native = pipeline.generate_api_payloads([segment], "native_header")[0]
        self.assertEqual(strict["textContent"], "Clean text only.")
        self.assertEqual(native["textContent"], "Clean text only.")
        self.assertIn("example", strict["metadata"]["title"])
        self.assertIn("lp3", strict["metadata"]["title"])
        self.assertIn("p12", native["metadata"]["title"])
        self.assertNotIn("published", native["metadata"])
        units = pipeline.simulate_native_header_chunks([segment], chunk_size=10, overlap=2)
        audit = pipeline.native_header_chunk_eval(units)
        self.assertEqual(audit["status"], "pass")
        self.assertTrue(all("sourceDocument:" in unit["text"] for unit in units))

    def test_pdf_date_conversion(self):
        self.assertEqual(
            pipeline.pdf_date_to_epoch_ms("D:19700101000000"),
            0,
        )
        self.assertIsNone(pipeline.pdf_date_to_epoch_ms(""))

    def test_page_targeted_vector_probe_uses_top_three(self):
        segments = [
            {"segment_id": "s1", "pdf_page": 1, "chapter": "One", "text": "alpha concept"},
            {"segment_id": "s2", "pdf_page": 2, "chapter": "Two", "text": "beta concept"},
            {"segment_id": "s3", "pdf_page": 3, "chapter": "Three", "text": "gamma concept"},
        ]
        probes = [
            {
                "kind": "page_targeted",
                "query": "alpha",
                "expected_segment_id": "s1",
                "expected_phrase": "alpha",
                "expected_pdf_page": 1,
                "chapter": "One",
            }
        ]
        original_available = pipeline.ollama_available
        original_embed = pipeline.get_ollama_embeddings
        try:
            pipeline.ollama_available = lambda _url: True
            pipeline.get_ollama_embeddings = lambda texts, _model, _url: [
                [1.0, 0.0] if "alpha" in text else ([0.0, 1.0] if "beta" in text else [0.2, 0.2])
                for text in texts
            ]
            results, status, detail, usage = pipeline.vector_eval(segments, probes, "test", "http://ollama/api/embed")
        finally:
            pipeline.ollama_available = original_available
            pipeline.get_ollama_embeddings = original_embed
        self.assertEqual(status, "complete")
        self.assertEqual(detail, "")
        self.assertEqual(usage.get("requests"), 0)
        self.assertEqual(results[0]["status"], "pass")

    def test_vector_sampling_keeps_late_expected_segments(self):
        rows = [
            {
                "retrieval_unit_id": f"unit-{index}",
                "segment_id": f"segment-{index}",
                "text": f"text {index}",
            }
            for index in range(1000)
        ]
        probes = [{"expected_segment_id": "segment-999"}]
        selected = pipeline.select_vector_eval_rows(rows, probes, 100)
        self.assertEqual(len(selected), 100)
        self.assertIn("segment-999", {row["segment_id"] for row in selected})

    def test_bad_outline_is_rejected_and_text_fallback_remains_available(self):
        pages = [
            {"page": page, "text": ("Introduction\n\n" if page == 3 else "") + ("Dense prose sentence. " * 80)}
            for page in range(1, 8)
        ]
        outline = [
            {"level": 1, "title": "Introduction", "pdf_page": 7},
            {"level": 1, "title": "Completely Missing Chapter", "pdf_page": 6},
        ]
        validation = pipeline.validate_outline_against_text(outline, pages, 7)
        self.assertEqual(validation["reliability"], "untrusted")
        stats = [pipeline.page_stats_for(page) for page in pages]
        start_page, reason = pipeline.detect_body_start(
            pages,
            stats,
            outline=pipeline.usable_outline_from_validation(outline, validation),
        )
        self.assertEqual(start_page, 3)
        self.assertEqual(reason, "opening_heading")

    def test_include_front_matter_starts_at_the_first_nonempty_page(self):
        pages = [
            {"page": 1, "text": "Title page\n\n" + ("Front matter prose. " * 50)},
            {"page": 2, "text": "Introduction\n\n" + ("Body prose. " * 80)},
        ]
        stats = [pipeline.page_stats_for(page) for page in pages]
        start_page, reason = pipeline.detect_body_start(
            pages,
            stats,
            include_front_matter=True,
        )
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "include_front_matter_first_nonempty")

    def test_dense_prose_is_not_mistaken_for_a_table_of_contents(self):
        pages = [
            {"page": 1, "text": "Body text.\n" * 100},
            # The short-line/heading signals resemble a contents page, but
            # the word and sentence density make it normal prose. This is the
            # Weber failure shape that previously shifted body start to page 2.
            {"page": 2, "text": "Conclusion\n" + ("Dense prose sentence.\n" * 100)},
        ]
        stats = [pipeline.page_stats_for(page) for page in pages]

        self.assertFalse(stats[1].is_toc_like)
        start_page, reason = pipeline.detect_body_start(pages, stats)
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "prose_density")

        # Defend against legacy/precomputed PageStat rows, too.
        stats[1].is_toc_like = True
        start_page, reason = pipeline.detect_body_start(pages, stats)
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "prose_density")

    def test_toc_requires_real_leaders_and_structured_entries(self):
        noise_page = {
            "page": 1,
            "text": "Contents of the debate\n" + ("Ordinary prose ... with punctuation and a Conclusion.\n" * 30),
        }
        toc_page = {
            "page": 2,
            "text": "Contents\nIntroduction .... 3\nChapter One .... 9\nConclusion .... 31\nIndex .... 45",
        }
        self.assertFalse(pipeline.page_stats_for(noise_page).is_toc_like)
        self.assertTrue(pipeline.page_stats_for(toc_page).is_toc_like)

    def test_unconfirmed_toc_never_discards_earlier_pages(self):
        pages = [
            {"page": 1, "text": "Front text. " * 30},
            {"page": 2, "text": "Contents\nChapter One .... 3\nConclusion .... 9\nIndex .... 12"},
            {"page": 3, "text": "brief tail"},
        ]
        stats = [pipeline.page_stats_for(page) for page in pages]
        start_page, reason = pipeline.detect_body_start(pages, stats)
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "table_of_contents_unconfirmed_retained")

    def test_opening_heading_must_be_a_heading_not_a_prose_mention(self):
        pages = [
            {"page": 1, "text": "This discussion cites an Introduction and a Conclusion. " * 30},
            {"page": 2, "text": "Body prose. " * 100},
        ]
        stats = [pipeline.page_stats_for(page) for page in pages]
        start_page, reason = pipeline.detect_body_start(pages, stats)
        self.assertEqual(start_page, 1)
        self.assertEqual(reason, "prose_density")

    def test_reliable_structure_reference_requires_complete_clean_coverage(self):
        quality = {
            "included_pages": 12,
            "empty_pages": 0,
            "included_words": 6154,
            "scanned_likelihood": "low",
        }
        self.assertTrue(pipeline.is_reliable_structure_reference(quality, 12))
        self.assertFalse(pipeline.is_reliable_structure_reference({**quality, "included_words": 700}, 12))
        self.assertFalse(pipeline.is_reliable_structure_reference({**quality, "scanned_likelihood": "high"}, 12))

    def test_prepared_pdf_text_filename_is_source_named_and_windows_safe(self):
        self.assertEqual(
            pipeline.parsed_pdf_text_filename(Path("Sample Review: Example Book? 2018.pdf")),
            "Sample-Review-Example-Book-2018-pdf-parsed.txt",
        )
        self.assertEqual(
            pipeline.parsed_pdf_text_filename(Path("CON.pdf")),
            "CON-pdf-parsed.txt",
        )

    def test_notes_and_index_endmatter_are_detected_after_body(self):
        pages = []
        for page in range(1, 21):
            heading = "Notes" if page == 15 else ("Index" if page == 19 else f"Chapter text {page}")
            pages.append({"page": page, "text": heading + "\n\n" + ("Body sentence with prose. " * 45)})
        detected = pipeline.detect_end_section_start(
            pages,
            pipeline.DEFAULT_END_SECTION_HEADINGS,
        )
        self.assertEqual(detected["page"], 15)
        self.assertEqual(detected["heading"].casefold(), "notes")

    def test_image_heavy_low_text_profile_is_ocr_likely(self):
        pages = [{"page": page, "text": ""} for page in range(1, 11)]
        geometry = {"image_count": 1, "rotation": 0}
        stats = [pipeline.page_stats_for(page, geometry) for page in pages]
        quality = pipeline.extraction_quality(pages, stats, 1, None)
        self.assertEqual(quality["scanned_likelihood"], "high")

    def test_ocr_layout_artifact_density_prevents_noisy_candidate_from_winning(self):
        clean_pages = [{"page": 1, "text": "Readable historical prose. " * 60}]
        noisy_pages = [{
            "page": 1,
            "text": ("Readable historical prose. " * 60) + (" | <br> **==> picture [1 x 1] " * 30),
        }]
        geometry = {"image_count": 1, "rotation": 0}
        clean_quality = pipeline.extraction_quality(
            clean_pages,
            [pipeline.page_stats_for(page, geometry) for page in clean_pages],
            1,
            None,
        )
        noisy_quality = pipeline.extraction_quality(
            noisy_pages,
            [pipeline.page_stats_for(page, geometry) for page in noisy_pages],
            1,
            None,
        )
        self.assertGreater(noisy_quality["ocr_layout_artifact_ratio"], 0.005)

        def candidate(backend, quality):
            return {
                "backend": backend,
                "quality": quality,
                "chunk_eval": {"suspicious_chunks": 0},
                "literal_results": [],
                "native_chunk_eval": {"status": "pass"},
            }

        clean_score, _ = pipeline.score_candidate(candidate("unstructured", clean_quality))
        noisy_score, noisy_reasons = pipeline.score_candidate(candidate("pymupdf4llm", noisy_quality))
        self.assertGreater(clean_score, noisy_score)
        self.assertIn("high_ocr_layout_artifact_ratio", noisy_reasons)

    def test_untrusted_outline_is_warning_not_edge_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            selected_dir = Path(temp_dir)
            for filename in [
                "anythingllm-upload.txt",
                "anythingllm-upload-inline-metadata-fallback.txt",
                "anythingllm-upload-frontmatter-and-body.txt",
                "anythingllm-upload-body-with-endmatter.txt",
            ]:
                (selected_dir / filename).write_text("text", encoding="utf-8")
            segment = {
                "source_id": "id",
                "source_title": "title",
                "source_file": "file.pdf",
                "source_sha256": "a" * 64,
                "backend": "pymupdf",
                "pdf_page": 2,
                "chapter": "Introduction",
                "segment_id": "segment",
                "segment_index": 1,
                "page_line_start": 3,
                "page_line_end": 6,
                "text": "body",
                "headings_on_page": ["Introduction"],
                "chapter_source": "page_text_heading",
                "section_source": "",
            }
            selected = {
                "backend": "pymupdf",
                "start_page": 2,
                "start_reason": "opening_heading",
                "end_page": 9,
                "segments": [segment],
                "marker_stats": {"marker_char_ratio": 0.01},
                "chunk_eval": {"chunks_without_marker": 1, "suspicious_chunks": 0},
                "native_chunk_eval": {
                    "status": "pass",
                    "retrieval_chunks": 1,
                    "chunks_without_source_document": 0,
                    "chunks_without_page_or_segment": 0,
                },
                "outline_validation": {"reliability": "untrusted", "pass_rate": 0.0},
                "quality": {"included_words": 9000},
                "readiness_status": "ready",
            }
            native_payload = pipeline.generate_api_payloads([{
                **segment,
                "source_short_label": "title",
                "source_author": "",
                "logical_page": "",
                "document_region": "body",
                "part": "",
                "section": "",
                "subsection": "",
            }], "native_header")
            report = pipeline.evaluate_edge_cases(
                {"pdf_page_count": 10, "filename": "generic.pdf", "detected_title": "Generic"},
                selected,
                selected_dir,
                native_payload,
            )
            self.assertEqual(report["overall_status"], "pass")
            outline_row = next(row for row in report["rows"] if row["check"] == "outline_validation")
            self.assertEqual(outline_row["status"], "warning")


class PipelinePdfIntegrationTests(unittest.TestCase):
    def test_pdf_metadata_and_page_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "profile.pdf"
            doc = fitz.open()
            doc.set_metadata(
                {
                    "title": "Profile Test",
                    "author": "Test Author",
                    "subject": "Retrieval",
                    "keywords": "rag, pdf",
                }
            )
            for page_number in range(1, 4):
                page = doc.new_page()
                page.insert_text(
                    (72, 72),
                    f"Repeated Header\n\nChapter {page_number}\n\n"
                    + ("This is a complete paragraph for retrieval testing. " * 12)
                    + "\nRepeated Footer",
                )
            doc.set_toc([[1, "Introduction", 1], [1, "Notes", 3]])
            doc.save(pdf_path)
            doc.close()

            metadata = pipeline.pdf_metadata(pdf_path, include_page_geometry=True)
            self.assertEqual(metadata["pdf_page_count"], 3)
            self.assertEqual(metadata["title"], "Profile Test")
            self.assertEqual(metadata["keywords"], "rag, pdf")
            self.assertEqual(len(metadata["page_geometry"]), 3)
            self.assertFalse(metadata["needs_password"])
            self.assertNotIn("_author_text_samples", metadata)

            metadata_with_samples = pipeline.pdf_metadata(
                pdf_path,
                include_page_geometry=True,
                include_author_samples=True,
            )
            self.assertEqual(
                [row["page"] for row in metadata_with_samples["_author_text_samples"]],
                [1, 2, 3],
            )
            self.assertIn("Repeated Header", metadata_with_samples["_author_text_samples"][0]["text"])
            self.assertEqual(metadata_with_samples["_author_sample_error"], "")

    def test_author_samples_reuse_the_metadata_open_and_keep_the_same_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "author-sample.pdf"
            doc = fitz.open()
            for page_number in range(1, 7):
                page = doc.new_page()
                text = "Sample Research Paper\nby Jane Doe\n" if page_number == 1 else "Ordinary body text"
                page.insert_text((72, 72), text)
            doc.save(pdf_path)
            doc.close()

            metadata = pipeline.pdf_metadata(pdf_path, include_author_samples=True)
            report = pipeline.infer_author_from_samples_or_filename(
                metadata["_author_text_samples"],
                pdf_path,
                title_hint="Sample Research Paper",
            )

            self.assertEqual(report["author"], "Jane Doe")
            self.assertIn(report["source"], {"text_byline", "text_role_followup"})

    def test_prepare_pdf_writes_clean_primary_fallback_and_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "book.pdf"
            doc = fitz.open()
            doc.set_metadata({"title": "Integration Book", "author": "Test Author"})
            for page_number in range(1, 13):
                page = doc.new_page()
                heading = "Introduction" if page_number == 2 else ("Notes" if page_number == 10 else f"Section {page_number}")
                page.insert_textbox(
                    fitz.Rect(60, 60, 540, 760),
                    heading + "\n\n" + (f"Distinctive prose from physical page {page_number}. " * 45),
                    fontsize=10,
                )
            doc.set_toc([[1, "Introduction", 2], [1, "Notes", 10]])
            doc.save(pdf_path)
            doc.close()

            timing_events = []
            args = SimpleNamespace(
                document_label="",
                document_author="",
                document_short_label="",
                use_file_title_fallback=True,
                deep_extraction=False,
                include_front_matter=False,
                include_back_matter=False,
                backend_mode="pymupdf",
                first_page_override=0,
                end_page_override=0,
                target_passage_length=500,
                end_section_names=pipeline.DEFAULT_END_SECTION_HEADINGS,
                validation_phrases=[],
                unstructured_strategy="fast",
                marker_style="short",
                disable_inline_markers=False,
                run_vector_eval=False,
                ollama_model="bge-m3:latest",
                ollama_url="http://127.0.0.1:11434/api/embed",
                max_vector_probes=4,
                prepare_and_upload=False,
                anythingllm_api_url="",
                anythingllm_api_key="",
                workspace_slug="",
                test_workspace_slug="test",
                upload_limit=0,
                anythingllm_storage_dir=str(root / "missing-storage"),
                anythingllm_chunk_size=400,
                anythingllm_chunk_overlap=40,
                timing_event_callback=lambda stage, event: timing_events.append((stage, dict(event))),
                run_author_inference_sample_evaluation=False,
            )
            output_dir = root / "output"
            original_author_reader = pipeline.infer_author_from_pdf_text
            original_author_evaluation = pipeline.evaluate_author_inference_samples
            try:
                # Preparation must reuse the transient samples gathered with
                # metadata; reopening the PDF through this legacy helper
                # would reintroduce the redundant per-document read.
                pipeline.infer_author_from_pdf_text = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("prepare_pdf reopened the PDF for author inference")
                )
                pipeline.evaluate_author_inference_samples = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("ordinary preparation ran the unrelated author sample suite")
                )
                summary = pipeline.prepare_pdf(pdf_path, output_dir, args)
            finally:
                pipeline.infer_author_from_pdf_text = original_author_reader
                pipeline.evaluate_author_inference_samples = original_author_evaluation
            primary = Path(summary["upload_file"]).read_text(encoding="utf-8")
            fallback = Path(summary["inline_metadata_fallback"]).read_text(encoding="utf-8")
            self.assertFalse(primary.startswith("["))
            self.assertTrue(fallback.startswith("["))
            self.assertEqual(summary["start_page"], 2)
            self.assertEqual(summary["end_page"], 10)
            self.assertTrue(Path(summary["variant_summary"]).exists())
            self.assertTrue(Path(summary["diagnostics_report"]).exists())
            profile = json.loads((output_dir / "source-profile.json").read_text(encoding="utf-8"))
            provenance_manifest = json.loads(
                Path(summary["provenance_review_manifest"]).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance_manifest["schema_version"], 1)
            self.assertEqual(
                provenance_manifest["source"]["sha256"],
                profile["source_sha256"],
            )
            self.assertEqual(
                provenance_manifest["review_artifacts"]["canonical_extracted_text"],
                "anythingllm-upload.txt",
            )
            self.assertEqual(
                provenance_manifest["selected_extraction"]["pymupdf4llm_execution"],
                {},
            )
            self.assertEqual(
                provenance_manifest["selected_extraction"]["pymupdf4llm_ocr_page_workers"],
                0,
            )
            self.assertEqual(
                provenance_manifest["selected_extraction"]["ocr_page_workers_scope"],
                "not_applicable",
            )
            self.assertEqual(profile["metadata_provenance"]["source_title"], "pdf_metadata")
            self.assertEqual(profile["anythingllm_chunk_simulation"]["chunk_size"], 400)
            self.assertEqual(profile["boundary_decisions"]["end_matter"]["starts_at_pdf_page"], 10)
            self.assertFalse(profile["boundary_decisions"]["end_matter"]["included_in_primary_output"])
            metadata_timing = next(
                event for stage, event in timing_events
                if stage == "source_metadata_and_author_inference"
            )
            self.assertTrue(metadata_timing["author_samples_reused"])
            self.assertEqual(metadata_timing["pdf_pages"], 12)
            self.assertGreater(metadata_timing["source_bytes"], 0)
            self.assertNotIn("_author_text_samples", profile)
            self.assertNotIn("_author_sample_error", profile)
            self.assertEqual(summary["author_inference_evaluation_status"], "skipped_not_requested")
            self.assertEqual(summary["author_inference_evaluation_csv"], "")

            args.include_back_matter = True
            output_with_backmatter = root / "output-with-backmatter"
            summary_with_backmatter = pipeline.prepare_pdf(pdf_path, output_with_backmatter, args)
            profile_with_backmatter = json.loads((output_with_backmatter / "source-profile.json").read_text(encoding="utf-8"))
            self.assertIsNone(summary_with_backmatter["end_page"])
            self.assertEqual(summary_with_backmatter["detected_end_page"], 10)
            self.assertTrue(summary_with_backmatter["include_back_matter"])
            self.assertEqual(profile_with_backmatter["boundary_decisions"]["end_matter"]["starts_at_pdf_page"], 10)
            self.assertTrue(profile_with_backmatter["boundary_decisions"]["end_matter"]["included_in_primary_output"])
            self.assertEqual(profile_with_backmatter["boundary_decisions"]["end_matter"]["excluded_end_range"], "none")

    def test_author_inference_sample_evaluation_writes_reports(self):
        original_samples = pipeline.AUTHOR_INFERENCE_SAMPLE_PAPERS
        original_ensure = pipeline.ensure_downloaded_pdf
        original_infer = pipeline.infer_author_from_pdf_text
        try:
            pipeline.AUTHOR_INFERENCE_SAMPLE_PAPERS = [
                {
                    "file": "sample.pdf",
                    "url": "https://example.com/sample.pdf",
                    "title_hint": "Sample Title",
                    "expected_contains": ["Jane Doe", "John Roe"],
                }
            ]
            pipeline.ensure_downloaded_pdf = lambda path, url: Path(path)
            pipeline.infer_author_from_pdf_text = lambda path, title_hint="": {
                "author": "Jane Doe; John Roe",
                "source": "front_pages",
                "page": 1,
                "evidence": "by Jane Doe and John Roe",
            }
            with tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir) / "author-eval"
                result = pipeline.evaluate_author_inference_samples(output_dir)
                self.assertEqual(result["passed"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertTrue(Path(result["csv"]).exists())
                self.assertTrue(Path(result["json"]).exists())
                rows = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
                self.assertEqual(rows[0]["inferred_author"], "Jane Doe; John Roe")
        finally:
            pipeline.AUTHOR_INFERENCE_SAMPLE_PAPERS = original_samples
            pipeline.ensure_downloaded_pdf = original_ensure
            pipeline.infer_author_from_pdf_text = original_infer

class TimingAndInspectionSafetyTests(unittest.TestCase):
    def test_legacy_success_is_low_confidence_learning_not_active_duration_truth(self):
        import rag_pdf_gradio_app as app

        row = {
            "source": "automatic-run", "state": "successful",
            "page_count": 12, "actual_seconds": 90,
            "duration_provenance": "legacy_wall_clock",
        }
        self.assertFalse(app.timing_model_observation_usable(row))
        self.assertTrue(app.timing_model_legacy_observation_usable(row))
        self.assertTrue(app.timing_model_learning_observation_usable(row))

    def test_completed_phase_evidence_revises_eta_without_claiming_progress(self):
        import rag_pdf_gradio_app as app

        # A real, substantial phase at 25% evidence can reduce a grossly
        # pessimistic opening estimate, but the bounded correction cannot
        # collapse the ETA to the elapsed time alone.
        revised = app.evidence_paced_eta_seconds(3_200, 380, .25)
        self.assertLess(revised, 3_200)
        self.assertGreater(revised, 380)

    def test_visible_eta_forwards_inherited_anythingllm_settings(self):
        import rag_pdf_gradio_app as app

        original_validate = app.validate_pdf_inputs
        original_estimate = app.estimate_automatic_run
        try:
            captured = {}
            app.validate_pdf_inputs = lambda inputs: (["sample.pdf"], None)
            app.estimate_automatic_run = lambda *_args, **kwargs: (
                captured.update(kwargs) or {"expected_seconds": 90, "source": "test"}
            )
            app.refresh_automatic_run_estimate(
                ["sample.pdf"], [], app.MODE_NATIVE_UPLOAD_LABEL,
                app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                target_passage_length=750, anythingllm_chunk_size=750,
                api_url="http://127.0.0.1:3001/api", inherit_anythingllm_settings=True,
            )
            self.assertEqual(captured["api_url"], "http://127.0.0.1:3001/api")
            self.assertTrue(captured["inherit_anythingllm_settings"])
        finally:
            app.validate_pdf_inputs = original_validate
            app.estimate_automatic_run = original_estimate

    def test_pipeline_timing_observer_failure_does_not_escape(self):
        args = SimpleNamespace(
            timing_event_callback=lambda _stage, _event: (_ for _ in ()).throw(RuntimeError("observer unavailable"))
        )
        self.assertFalse(
            pipeline.emit_pipeline_timing_event(args, "storage_audit", elapsed_seconds=1.2)
        )

    def test_batch_inspection_context_reuses_only_matching_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            (storage / "anythingllm.db").write_text("placeholder", encoding="utf-8")
            args = SimpleNamespace(batch_inspection_context={
                "anythingllm_preparation_config": {"chunk_settings": {"chunk_size": 400}},
                "resolved_anythingllm_runtime_state": {"status": "ready"},
            })
            context, reused = pipeline.get_batch_inspection_context(args, storage, "workspace")
            self.assertFalse(reused)
            self.assertEqual(context["anythingllm_preparation_config"]["chunk_settings"]["chunk_size"], 400)
            self.assertEqual(context["resolved_anythingllm_runtime_state"]["status"], "ready")
            context["global_read_only"]["storage_report"] = {"status": "complete"}
            _same, reused = pipeline.get_batch_inspection_context(args, storage, "workspace")
            self.assertTrue(reused)
            _changed, reused = pipeline.get_batch_inspection_context(args, storage, "other-workspace")
            self.assertFalse(reused)


if __name__ == "__main__":
    unittest.main()
