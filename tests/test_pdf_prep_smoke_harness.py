import pytest

from benchmarks.run_pdf_prep_smoke_harness import (
    completed_phase_timings,
    final_status,
    make_args,
    summarize_result,
)


pytestmark = pytest.mark.offline_deterministic


def test_smoke_harness_final_status_requires_ready_readiness():
    rows = [
        {"status": "complete", "readiness_status": "ready"},
        {"status": "complete", "readiness_status": "needs_review"},
    ]

    assert final_status(rows) == "needs_review"


def test_smoke_harness_final_status_promotes_exceptions_to_error():
    rows = [
        {"status": "complete", "readiness_status": "ready"},
        {"status": "error", "readiness_status": None},
    ]

    assert final_status(rows) == "error"


def test_smoke_harness_uses_local_only_drawer_visible_args():
    args = make_args()

    assert args.prepare_and_upload is False
    assert args.anythingllm_create_document_folders is False
    assert args.unstructured_strategy == "auto"
    assert args.run_vector_eval is False


def test_smoke_harness_reuses_batch_context_and_runtime_probe():
    context = {}
    probe = {"backend_available": True, "tesseract_available": True}

    args = make_args(
        batch_inspection_context=context,
        unstructured_runtime_probe=probe,
    )

    assert args.batch_inspection_context is context
    assert args.unstructured_runtime_probe is probe


def test_smoke_harness_phase_timings_keep_completed_phases_slowest_first():
    rows = completed_phase_timings(
        [
            {"stage": "metadata", "timing_event": "phase_completed", "phase_elapsed_seconds": 0.2},
            {"stage": "ignored", "timing_event": "phase_started", "phase_elapsed_seconds": 99},
            {"stage": "ocr", "timing_event": "phase_completed", "phase_elapsed_seconds": 4.25},
        ]
    )

    assert rows == [
        {"stage": "ocr", "seconds": 4.25},
        {"stage": "metadata", "seconds": 0.2},
    ]


def test_smoke_harness_summary_uses_current_run_summary_keys(tmp_path):
    pdf = tmp_path / "source.pdf"
    result = summarize_result(
        pdf,
        1,
        0,
        {
            "readiness_status": "ready",
            "readiness_reasons": [],
            "selected_backend": "pymupdf",
            "ocr_assisted_extraction_used": False,
            "pdf_page_count": 6,
            "segments": 12,
            "warnings": ["advisory"],
            "errors": [],
            "output_root": str(tmp_path / "run"),
        },
        [{"stage": "metadata", "timing_event": "phase_completed", "phase_elapsed_seconds": 0.1}],
    )

    assert result["pdf_page_count"] == 6
    assert result["segments"] == 12
    assert result["warnings"] == 1
    assert result["summary_path"].endswith("run-summary.json")
    assert result["phase_timings"] == [{"stage": "metadata", "seconds": 0.1}]
