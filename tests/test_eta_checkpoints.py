import json

import pytest


pytestmark = pytest.mark.offline_deterministic


def test_eta_checkpoint_retains_exactly_ten_numbers_and_material_reasons(tmp_path):
    import rag_pdf_gradio_app as app

    record = app.update_eta_checkpoint_record(
        tmp_path,
        numbers={
            "opening_expected_seconds": 120,
            "final_expected_seconds": 120,
            "ignored_extra_number": 999,
        },
    )
    assert tuple(record["numbers"]) == app.ETA_CHECKPOINT_NUMBER_FIELDS
    assert len(record["numbers"]) == 10
    assert "ignored_extra_number" not in record["numbers"]

    updated = app.update_eta_checkpoint_record(
        tmp_path,
        numbers={"final_expected_seconds": 90},
        increments={"material_recalculation_count": 1},
        recalculation_reason="confirmed_batch_cache_plan",
    )
    assert updated["numbers"]["opening_expected_seconds"] == 120
    assert updated["numbers"]["final_expected_seconds"] == 90
    assert updated["numbers"]["material_recalculation_count"] == 1
    assert updated["recalculation_reasons"] == ["confirmed_batch_cache_plan"]
    assert json.loads((tmp_path / "eta-checkpoints.json").read_text(encoding="utf-8")) == updated


def test_status_reprice_updates_checkpoint_and_cache_basis(tmp_path):
    import rag_pdf_gradio_app as app

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        app.update_eta_checkpoint_record(
            tmp_path,
            numbers={
                "opening_expected_seconds": 120,
                "final_expected_seconds": 120,
            },
        )
        app.update_live_automatic_run_status(
            tmp_path,
            state="running",
            phase="Preparing",
            expected_seconds=120,
            confirmed_fraction=0.1,
        )
        status = app.update_live_automatic_run_status(
            tmp_path,
            state="running",
            phase="Submitting",
            expected_seconds=90,
            confirmed_fraction=0.2,
            eta_reprice_reason="confirmed_batch_cache_plan",
            eta_basis="cache_plan_confirmed",
        )
        checkpoint = json.loads(
            (tmp_path / "eta-checkpoints.json").read_text(encoding="utf-8")
        )
        events = [
            json.loads(line)
            for line in (tmp_path / "eta-recalculation-events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    assert status["eta_basis"] == "cache_plan_confirmed"
    assert checkpoint["numbers"]["material_recalculation_count"] == 1
    assert checkpoint["numbers"]["final_expected_seconds"] == 90
    assert len(events) == 1
    assert events[0]["status"] == "applied"
    assert events[0]["reason"] == "confirmed_batch_cache_plan"
    assert events[0]["confirmed_percent"] == 20.0
    assert 0.0 <= events[0]["displayed_percent"] <= 100.0
    assert events[0]["previous_expected_seconds"] == 120
    assert events[0]["new_expected_seconds"] == 90
    assert events[0]["delta_seconds"] == -30


def test_eta_ui_distinguishes_initial_and_confirmed_cache_plan():
    import rag_pdf_gradio_app as app

    initial = app.automatic_run_timing_html(
        expected_seconds=120,
        state="running",
        started_epoch=100,
        now=110,
    )
    confirmed = app.automatic_run_timing_html(
        expected_seconds=90,
        state="running",
        started_epoch=100,
        now=110,
        eta_basis="cache_plan_confirmed",
    )

    assert "initial estimate" in initial
    assert 'data-eta-basis="initial_estimate"' in initial
    assert "cache plan confirmed" in confirmed
    assert 'data-eta-basis="cache_plan_confirmed"' in confirmed


def test_stable_opening_eta_stops_calling_itself_initial_after_two_minutes():
    import rag_pdf_gradio_app as app

    initial = app.automatic_run_timing_html(
        expected_seconds=600,
        state="running",
        started_epoch=100,
        now=219,
    )
    established = app.automatic_run_timing_html(
        expected_seconds=600,
        state="running",
        started_epoch=100,
        now=220,
    )

    assert "initial estimate" in initial
    assert "initial estimate" not in established
    # Provenance stays available to the diagnostics even when the UI label is
    # deliberately neutral.
    assert 'data-eta-basis="initial_estimate"' in established


def test_complete_grouped_upload_vector_proof_can_enter_reporting_tail():
    import rag_pdf_gradio_app as app

    assert app.upload_report_has_complete_vector_proof(
        {
            "status": "complete",
            "newly_attached_records": 467,
            "confirmed_vector_records": 467,
        }
    )
    assert not app.upload_report_has_complete_vector_proof(
        {
            "status": "complete",
            "newly_attached_records": 467,
            "confirmed_vector_records": 466,
        }
    )
    assert not app.upload_report_has_complete_vector_proof(
        {"status": "reconciliation_pending", "uploaded": 1, "embedded": 1}
    )


def test_live_reprice_is_not_mislabeled_as_initial_estimate(tmp_path):
    import rag_pdf_gradio_app as app

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        app.update_live_automatic_run_status(
            tmp_path,
            state="running",
            phase="Preparing",
            expected_seconds=120,
            confirmed_fraction=0.1,
        )
        status = app.update_live_automatic_run_status(
            tmp_path,
            state="running",
            phase="OCR observed",
            expected_seconds=150,
            confirmed_fraction=0.2,
            eta_reprice_reason="ocr_runtime_observed",
        )
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    rendered = app.automatic_run_timing_html(
        expected_seconds=150,
        state="running",
        started_epoch=100,
        now=110,
        eta_basis=status["eta_basis"],
    )
    assert status["eta_basis"] == "live_observations"
    assert "live estimate" not in rendered
    assert "initial estimate" not in rendered
    assert "Est: 02m20s" in rendered


def test_ocr_runtime_reprices_have_four_or_fewer_visible_checkpoints():
    import rag_pdf_gradio_app as app

    assert app.ocr_runtime_eta_reprice_checkpoints(20) == (1, 7, 14, 20)
    assert len(app.ocr_runtime_eta_reprice_checkpoints(375)) <= 4
    assert app.ocr_runtime_eta_reprice_checkpoints(0) == (1, 2, 4, 8)
