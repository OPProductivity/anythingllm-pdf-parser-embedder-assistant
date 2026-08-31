import json
from unittest import mock

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
            eta_reprice_context={"cached_records": 50, "fresh_records": 10},
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
    assert events[0]["decision_context"] == {
        "cached_records": 50,
        "fresh_records": 10,
    }


def test_eta_suppression_retains_guard_context(tmp_path):
    import rag_pdf_gradio_app as app

    event = app.append_eta_recalculation_event(
        tmp_path,
        status="suppressed",
        reason="owned_queue_rate",
        suppression_reason="combined_cache_and_provider_queue_rate",
        elapsed_seconds=120,
        confirmed_fraction=.5,
        progress_phase="desktop_queue",
        previous_expected_seconds=900,
        new_expected_seconds=900,
        decision_context={
            "queue_records": 463,
            "cached_records": 238,
            "fresh_records": 225,
        },
    )

    assert event["decision_context"]["fresh_records"] == 225
    persisted = [
        json.loads(line)
        for line in (tmp_path / "eta-recalculation-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert persisted[0]["suppression_reason"] == "combined_cache_and_provider_queue_rate"
    assert persisted[0]["decision_context"]["cached_records"] == 238


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
        cache_reuse_documents=3,
        cache_total_documents=5,
    )

    assert "initial estimate" in initial
    assert 'data-eta-basis="initial_estimate"' in initial
    assert "3 of 5 documents already cached" in confirmed
    assert 'data-eta-basis="cache_plan_confirmed"' in confirmed


def test_eta_ui_labels_a_prepared_only_cache_snapshot_without_claiming_final_plan():
    import rag_pdf_gradio_app as app

    rendered = app.automatic_run_timing_html(
        expected_seconds=90,
        state="running",
        started_epoch=100,
        now=110,
        eta_basis="prepared_cache_snapshot",
        cache_reuse_documents=3,
        cache_total_documents=5,
        cache_plan_partial=True,
    )

    assert "3 of 5 prepared documents already cache-backed" in rendered
    assert 'data-cache-plan-partial="true"' in rendered


def test_eta_ui_cache_wording_shows_exact_record_share_when_known():
    import rag_pdf_gradio_app as app

    rendered = app.automatic_run_timing_html(
        expected_seconds=90,
        state="running",
        started_epoch=100,
        now=110,
        eta_basis="cache_plan_confirmed",
        cache_reuse_documents=33,
        cache_total_documents=42,
        cache_reuse_records=1758,
        cache_total_records=2935,
    )

    assert "33 of 42 documents already cached" in rendered
    assert "1,758 of 2,935 page records cache-backed" in rendered
    assert 'data-cache-total-records="2935"' in rendered


def test_partial_cache_eta_keeps_unprepared_local_work_in_the_forecast():
    import rag_pdf_gradio_app as app

    early = app.prepared_cache_snapshot_eta_seconds(
        3600,
        120,
        fresh_provider_requests=600,
        cached_attachment_records=400,
        cached_source_windows=4,
        unprepared_records=600,
        initial_estimated_records=1000,
        initial_non_batch_seconds=900,
        provider_request_seconds=2.0,
        features={"document_count": 10},
        cache_attachment_prior={"record_seconds": .08, "source_seconds": 3.0},
    )
    late = app.prepared_cache_snapshot_eta_seconds(
        3600,
        300,
        fresh_provider_requests=600,
        cached_attachment_records=400,
        cached_source_windows=4,
        unprepared_records=0,
        initial_estimated_records=1000,
        initial_non_batch_seconds=900,
        provider_request_seconds=2.0,
        features={"document_count": 10},
        cache_attachment_prior={"record_seconds": .08, "source_seconds": 3.0},
    )

    assert 300 < late < early < 3600


def test_partial_cache_sample_projects_unknown_records_from_multiple_exact_sources():
    import rag_pdf_gradio_app as app

    projection = app.prepared_cache_snapshot_projection(
        fresh_provider_requests=4566,
        cached_attachment_records=384,
        unprepared_records=4564,
        observed_records=386,
        observed_source_windows=9,
    )

    # Nine independent PDFs are enough to inform the unknown remainder, but
    # not enough to claim the observed near-100% record rate outright.
    assert 0.65 < projection["conservative_cache_fraction"] < 0.75
    assert projection["projected_cached_unprepared_records"] > 3000
    assert projection["projected_fresh_unprepared_records"] < 1500


def test_partial_cache_eta_uses_conservative_sample_not_all_fresh_unknowns():
    import rag_pdf_gradio_app as app

    all_fresh_unknowns = app.prepared_cache_snapshot_eta_seconds(
        10554,
        96,
        fresh_provider_requests=4566,
        cached_attachment_records=384,
        cached_source_windows=9,
        unprepared_records=4564,
        initial_estimated_records=4950,
        initial_non_batch_seconds=2026,
        provider_request_seconds=1.722,
        features={"document_count": 55},
        cache_attachment_prior={"record_seconds": .08, "source_seconds": 3.0},
    )
    sampled = app.prepared_cache_snapshot_eta_seconds(
        10554,
        96,
        fresh_provider_requests=4566,
        cached_attachment_records=384,
        cached_source_windows=9,
        unprepared_records=4564,
        initial_estimated_records=4950,
        initial_non_batch_seconds=2026,
        provider_request_seconds=1.722,
        features={"document_count": 55},
        cache_attachment_prior={"record_seconds": .08, "source_seconds": 3.0},
        observed_records=386,
        observed_source_windows=9,
        remaining_source_windows=46,
    )

    assert sampled < all_fresh_unknowns
    assert sampled > 96


def test_downward_reprice_retains_the_existing_presentation_ratio():
    import rag_pdf_gradio_app as app

    displayed = app.reprice_presentation_expected_seconds(
        previous_expected_seconds=10554,
        previous_presentation_expected_seconds=7388,
        new_expected_seconds=9885,
        is_material_reprice=True,
    )
    upward = app.reprice_presentation_expected_seconds(
        previous_expected_seconds=10554,
        previous_presentation_expected_seconds=7388,
        new_expected_seconds=12000,
        is_material_reprice=True,
    )

    assert displayed < 7388
    assert displayed == pytest.approx(6920, abs=1)
    assert upward == 12000


def test_live_status_renders_partial_cache_wording_after_the_durable_update():
    import rag_pdf_gradio_app as app

    with mock.patch.object(app.time, "time", return_value=110.0):
        rendered = app.automatic_live_status_html({
            "state": "running",
            "phase": "Preparing PDF",
            "confirmed_fraction": .08,
            "started_epoch": 100.0,
            "expected_seconds": 700,
            "presentation_expected_seconds": 490,
            "eta_basis": "prepared_cache_snapshot",
            "cache_reuse_records": 384,
            "cache_reuse_documents": 9,
            "cache_total_documents": 55,
            "cache_plan_partial": True,
        })

    assert "9 of 55 prepared documents already cache-backed" in rendered


def test_exact_zero_cache_plan_does_not_claim_cached_documents():
    import rag_pdf_gradio_app as app

    rendered = app.automatic_run_timing_html(
        expected_seconds=90,
        state="running",
        started_epoch=100,
        now=110,
        eta_basis="execution_plan_confirmed",
    )

    assert "initial estimate" not in rendered
    assert "cached" not in rendered


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


def test_run_progress_snapshot_coalesces_same_phase_queue_repaints(tmp_path):
    import rag_pdf_gradio_app as app

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    original_snapshots = app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_STATE
    original_interval = app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_INTERVAL_SECONDS
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_STATE = {}
        app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_INTERVAL_SECONDS = 99.0
        with mock.patch.object(app, "_write_automatic_run_json") as write:
            app.update_live_automatic_run_status(
                tmp_path,
                state="running",
                phase="AnythingLLM queue: 1/100",
                progress_phase="desktop_queue",
                completed_units=1,
                total_units=100,
            )
            app.update_live_automatic_run_status(
                tmp_path,
                state="running",
                phase="AnythingLLM queue: 2/100",
                progress_phase="desktop_queue",
                completed_units=2,
                total_units=100,
            )
            app.update_live_automatic_run_status(
                tmp_path,
                state="successful",
                phase="Ready",
                progress_phase="reporting",
                completed_units=100,
                total_units=100,
            )
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status
        app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_STATE = original_snapshots
        app.AUTOMATIC_RUN_PROGRESS_SNAPSHOT_INTERVAL_SECONDS = original_interval

    # The first lifecycle record and terminal record are durable; the rapid
    # second x/y repaint remains immediately visible in memory but is not a
    # second atomic status rewrite.
    progress_writes = [
        call for call in write.call_args_list
        if str(call.args[0]).endswith("run-progress.json")
    ]
    assert len(progress_writes) == 2


def test_queue_eta_upward_cap_is_run_level_but_never_blocks_a_decrease():
    import rag_pdf_gradio_app as app

    assert app.cap_queue_eta_reprice_to_opening(2_000, opening_expected=1_000) == 1_200
    assert app.cap_queue_eta_reprice_to_opening(850, opening_expected=1_000) == 850


def test_bounded_queue_group_counts_as_one_upward_eta_evidence_window():
    import rag_pdf_gradio_app as app

    context = {"prepared_records": 80, "batch_cache_plan_observed": False}
    shared = {
        "desktop_queue_records_per_minute": 30,
        "desktop_queue_completed": 20,
        "queue_records": 40,
        "source_window_total": 4,
        "source_queue_group_total": 2,
    }
    first = app.observe_batch_queue_forecast(
        context,
        {**shared, "source_path": "first.pdf", "source_queue_group_index": 1},
        elapsed_seconds=60,
        provider_request_seconds_prior=2,
    )
    same_group = app.observe_batch_queue_forecast(
        context,
        {**shared, "source_path": "second.pdf", "source_queue_group_index": 1},
        elapsed_seconds=61,
        provider_request_seconds_prior=2,
    )
    second_group = app.observe_batch_queue_forecast(
        context,
        {**shared, "source_path": "third.pdf", "source_queue_group_index": 2},
        elapsed_seconds=62,
        provider_request_seconds_prior=2,
    )

    assert first["observed_windows"] == 1
    assert same_group["observed_windows"] == 1
    assert second_group["observed_windows"] == 2


def test_timing_timeline_coalesces_repeated_queue_observations(tmp_path):
    import rag_pdf_gradio_app as app

    original_dir = app.TIMING_MODEL_DIR
    original_events = app.TIMING_MODEL_EVENTS_PATH
    original_timeline = app.TIMING_TIMELINE_LAST_PERSISTED
    original_interval = app.TIMING_TIMELINE_SNAPSHOT_INTERVAL_SECONDS
    try:
        app.TIMING_MODEL_DIR = tmp_path / "model"
        app.TIMING_MODEL_EVENTS_PATH = app.TIMING_MODEL_DIR / "events.jsonl"
        app.TIMING_TIMELINE_LAST_PERSISTED = {}
        app.TIMING_TIMELINE_SNAPSHOT_INTERVAL_SECONDS = 99.0
        root = tmp_path / "run"
        assert app.record_timing_model_event(
            root, "Desktop queue: 1/100", {"timing_event": "queue_progress", "batch": 1}
        )
        assert not app.record_timing_model_event(
            root, "Desktop queue: 2/100", {"timing_event": "queue_progress", "batch": 1}
        )
        assert app.record_timing_model_event(
            root, "Desktop queue", {"timing_event": "desktop_queue_completed", "batch": 1}
        )
        rows = [
            json.loads(line)
            for line in (root / "timing-evidence-timeline.jsonl").read_text(encoding="utf-8").splitlines()
        ]
    finally:
        app.TIMING_MODEL_DIR = original_dir
        app.TIMING_MODEL_EVENTS_PATH = original_events
        app.TIMING_TIMELINE_LAST_PERSISTED = original_timeline
        app.TIMING_TIMELINE_SNAPSHOT_INTERVAL_SECONDS = original_interval

    assert [row["event"] for row in rows] == ["queue_progress", "desktop_queue_completed"]
