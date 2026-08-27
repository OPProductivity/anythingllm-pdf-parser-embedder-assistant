import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import fitz

import auto_anythingllm_pipeline as pipeline
import anythingllm_pdf_assistant_cli as assistant_cli
import prepared_batch_recovery as checkpoint
import rag_pdf_gradio_app as app
import rag_pdf_tools
from run_control import RunRecorder, RunResult
from run_request import LOCAL_ONLY, RunRequest
from segmentation_policy import policy_for


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize(
    ("status", "body", "expected_state"),
    [
        (422, "invalid prepared record", "rejected"),
        (401, "invalid key", "global_hold"),
        (429, "rate limited", "global_hold"),
        (500, "internal failure", "submission_unknown"),
    ],
)
def test_http_outcome_receipt_state_preserves_mutation_certainty(status, body, expected_state):
    outcome = pipeline.classify_anythingllm_mutation_outcome(
        stage="attachment",
        status=status,
        response_text=body,
    )
    assert pipeline.mutation_outcome_receipt_state(outcome) == expected_state


def test_single_record_and_final_record_desktop_queues_remain_observably_alive():
    common = {
        "desktop_queue_observer_state": "connected",
        "desktop_queue_last_event_age_seconds": 1,
    }
    assert pipeline.healthy_owned_desktop_queue(
        {
            **common,
            "queue_records": 1,
            "desktop_queue_current": 1,
            "desktop_queue_completed": 0,
        }
    )
    assert pipeline.healthy_owned_desktop_queue(
        {
            **common,
            "queue_records": 4,
            "desktop_queue_current": 4,
            "desktop_queue_completed": 3,
        }
    )
    assert not pipeline.healthy_owned_desktop_queue(
        {
            **common,
            "queue_records": 1,
            "desktop_queue_current": 1,
            "desktop_queue_completed": 1,
        }
    )


def test_server_start_ownership_lock_serializes_competing_starters():
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_starter():
        with assistant_cli._server_start_ownership_lock(62991):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_starter():
        assert first_entered.wait(timeout=2)
        with assistant_cli._server_start_ownership_lock(62991):
            second_entered.set()

    first = threading.Thread(target=first_starter)
    second = threading.Thread(target=second_starter)
    first.start()
    second.start()
    assert first_entered.wait(timeout=2)
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_checkpoint_hash_cache_reuses_unchanged_artifacts_but_verifier_remains_independent(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    text = source_root / "source.txt"
    text.write_text("prepared", encoding="utf-8")
    plan = source_root / "upload-plan.csv"
    plan.write_text(
        "filename,title,docAuthor,description,docSource,chunkSource,text_file\n"
        f"source.txt,Source,,,local-pdf://sha256/{'a' * 64},page-parent://source::p1,{text}\n",
        encoding="utf-8",
    )
    summary = {
        "pdf": str(tmp_path / "source.pdf"),
        "source_sha256": "a" * 64,
        "output_root": str(source_root),
        "native_upload_plan": str(plan),
        "api_upload_status": "not_started",
    }
    (source_root / "run-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    cache = {}
    with mock.patch.object(checkpoint, "_sha256", wraps=checkpoint._sha256) as digest:
        for stage in ("preparation_in_progress", "preparation_complete"):
            checkpoint.write_prepared_batch_checkpoint(
                tmp_path,
                [summary],
                total_sources=1,
                workspace_slug="workspace",
                api_url="http://127.0.0.1:3001/api",
                stage=stage,
                artifact_cache=cache,
            )
        assert digest.call_count == 3

    text.write_text("changed", encoding="utf-8")
    assert checkpoint.verify_prepared_batch_checkpoint(tmp_path)["reusable"] is False


def test_workspace_identity_reads_shared_native_evidence_once(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    native = {"author": "", "title": "", "_author_text_samples": []}
    with (
        mock.patch.object(app, "pdf_metadata", return_value=native) as read_metadata,
        mock.patch.object(app, "workspace_person_identity_from_pdf", return_value={}) as person,
        mock.patch.object(
            app,
            "workspace_institutional_identity_from_pdf",
            return_value={"label": "Institute", "kind": "institution"},
        ) as institution,
    ):
        result = app.workspace_source_identity_from_pdf(pdf)
    assert result["label"] == "Institute"
    read_metadata.assert_called_once_with(pdf, include_author_samples=True)
    assert person.call_args.kwargs["native_metadata"] is native
    assert institution.call_args.kwargs["native_metadata"] is native


def test_concurrent_workspace_identity_callbacks_share_one_inspection(tmp_path):
    pdf = tmp_path / "concurrent.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    native = {"author": "", "title": "", "_author_text_samples": []}

    def slow_metadata(*_args, **_kwargs):
        time.sleep(0.04)
        return native

    results = []
    with (
        mock.patch.object(app, "pdf_metadata", side_effect=slow_metadata) as read_metadata,
        mock.patch.object(
            app,
            "workspace_person_identity_from_pdf",
            return_value={"label": "Author", "kind": "person"},
        ),
    ):
        workers = [
            threading.Thread(target=lambda: results.append(app.workspace_source_identity_from_pdf(pdf)))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
    assert all(not worker.is_alive() for worker in workers)
    assert [result["label"] for result in results] == ["Author", "Author"]
    assert read_metadata.call_count == 1


def test_title_adjacent_author_is_trusted_but_one_word_style_markers_are_not():
    trusted = pipeline.infer_author_from_text_samples(
        [{"page": 1, "text": "Visual Pleasure and Narrative Cinema\nLaura Mulvey\nI\nIntroduction\n"}],
        title_hint="Visual Pleasure and Narrative Cinema",
    )
    assert trusted["author"] == "Laura Mulvey"
    assert trusted["source"] == "text_title_adjacent_byline"

    marker = pipeline.infer_author_from_text_samples(
        [{"page": 1, "text": "A Sample Article\nBold\nIntroduction\n"}],
        title_hint="A Sample Article",
    )
    assert marker.get("author") in {None, ""}


def test_catalog_filename_author_cues_are_delimited_not_first_word_guesses():
    cases = {
        "LúciaNagib_2011_Chapter7THESELF.pdf": ("Lúcia Nagib", "filename_compact_name_before_year"),
        "Jennifer L Fleissner Chapter 2 The Great Outdoors.pdf": ("Jennifer L Fleissner", "filename_name_before_section_label"),
        "WARTENBERG-2023.pdf": ("Wartenberg", "filename_surname_before_year"),
    }
    for filename, expected in cases.items():
        report = pipeline.infer_author_from_filename(Path(filename), title_hint="")
        assert (report["author"], report["source"]) == expected
    assert not pipeline.infer_author_from_filename(Path("Politics of Virtue.pdf"))["author"]
    assert not pipeline.infer_author_from_filename(Path("Carnal_Thoughts.pdf"))["author"]


def test_grouped_queue_cadence_does_not_use_the_slowest_source_window(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    ledger = {
        "batches": [
            {
                "requested": 100,
                "searchability_proven": True,
                "verification": {"desktop_queue_observer": {
                    "desktop_queue_records_per_minute": rate,
                    "desktop_queue_completed": 100,
                    "desktop_queue_observer_state": "connected",
                }},
            }
            for rate in (60.0, 6.0)
        ]
    }
    (run_root / "batch-embedding-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    row = {
        "run_key": str(run_root),
        "mode": app.MODE_NATIVE_UPLOAD_LABEL,
        "embedding_submission_strategy": "desktop_queue",
        "actual_records": 200,
        "actual_seconds": 300,
        "cached_attachment_reused_records": 0,
    }
    cadence = app.timing_model_desktop_queue_cadence(row)
    assert cadence["basis"] == "whole_run_upper_bound_for_grouped_source_windows"
    assert cadence["seconds_per_record"] == 1.5
    assert cadence["records_per_minute"] == 40.0


def test_large_batch_learned_multiplier_ignores_small_run_ratios():
    profile = {
        "documents": 17,
        "page_count": 900,
        "mean_chars_per_page": 3_200,
        "text_density_bucket": "high",
        "layout_bucket": "text_first",
        "ocr_risk_bucket": "low",
        "line_density_bucket": "medium",
        "page_variability_bucket": "mixed",
        "file_size_bucket": "medium",
    }
    arguments = (
        [f"source-{index}.pdf" for index in range(17)],
        app.MODE_NATIVE_UPLOAD_LABEL,
        app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
    )
    keywords = {
        "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
        "chunk_size": 8191,
        "chunk_overlap": 20,
        "target_passage_length": 750,
        "backend_mode": "Automatic",
    }
    with (
        mock.patch.object(app, "automatic_timing_document_profile", return_value=profile),
        mock.patch.object(app, "hydrated_timing_model_history", return_value=[]),
        mock.patch.object(
            app,
            "timing_stage_prior_seconds",
            return_value=(0.0, 0, "conservative unmeasured phase prior"),
        ),
    ):
        baseline = app.estimate_automatic_run(*arguments, **keywords)

    small_slow_run = {
        **baseline["features"],
        "source": "automatic-run",
        "state": "successful",
        "duration_provenance": "active_observation_window",
        "timing_formula_revision": app.TIMING_MODEL_FORMULA_REVISION,
        "run_key": "C:/real-runs/small-slow-run",
        "document_count": 2,
        "actual_records": 100,
        "estimated_records": 100,
        "actual_seconds": float(baseline["expected_seconds"]) * 4.0,
    }
    with (
        mock.patch.object(app, "automatic_timing_document_profile", return_value=profile),
        mock.patch.object(app, "hydrated_timing_model_history", return_value=[small_slow_run]),
        mock.patch.object(
            app,
            "timing_stage_prior_seconds",
            return_value=(0.0, 0, "conservative unmeasured phase prior"),
        ),
    ):
        estimate = app.estimate_automatic_run(*arguments, **keywords)

    assert estimate["expected_seconds"] == baseline["expected_seconds"]
    assert estimate["comparable_runs"] == 0


def test_desktop_timing_topology_distinguishes_source_windows_from_legacy_shared_batch(tmp_path):
    common = {
        "mode": app.MODE_NATIVE_UPLOAD_LABEL,
        "embedding_submission_strategy": "desktop_queue",
        "document_count": 2,
    }
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    assert app.timing_model_desktop_queue_topology({
        **common,
        "run_key": str(legacy_root),
    }) == app.DESKTOP_QUEUE_TOPOLOGY_LEGACY_SHARED

    window_root = tmp_path / "windows"
    window_root.mkdir()
    (window_root / "source-transaction-ledger.json").write_text(
        json.dumps({"transactions": [{"state": "exact_vectors_proven"}, {"state": "exact_vectors_proven"}]}),
        encoding="utf-8",
    )
    assert app.timing_model_desktop_queue_topology({
        **common,
        "run_key": str(window_root),
    }) == app.DESKTOP_QUEUE_TOPOLOGY_SOURCE_WINDOWS


def test_small_desktop_multiplier_requires_matching_scale_and_queue_topology():
    target = {
        "mode": app.MODE_NATIVE_UPLOAD_LABEL,
        "embedding_submission_strategy": "desktop_queue",
        "desktop_queue_topology": app.DESKTOP_QUEUE_TOPOLOGY_SOURCE_WINDOWS,
        "document_count": 4,
        "estimated_records": 44,
        "ocr_preflight_likely_pages": 0,
    }
    matching = {
        **target,
        "actual_records": 40,
    }
    assert app.timing_model_multiplier_observation_comparable(target, matching)
    assert not app.timing_model_multiplier_observation_comparable(target, {
        **matching,
        "desktop_queue_topology": app.DESKTOP_QUEUE_TOPOLOGY_LEGACY_SHARED,
    })
    # OCR preparation does not partition the Desktop queue-rate regime. A slow
    # current-topology OCR run may still be valid evidence for provider delay.
    assert app.timing_model_multiplier_observation_comparable(target, {
        **matching,
        "ocr_used": True,
    })
    assert not app.timing_model_multiplier_observation_comparable(target, {
        **matching,
        "actual_records": 100,
    })
    assert not app.timing_model_multiplier_observation_comparable(target, {
        **matching,
        "document_count": 9,
    })


def test_stale_selection_finish_cannot_authorize_a_newer_file_set():
    pending, *_ = app.automatic_selection_begin_state(
        {"state": "ready", "revision": 4},
        "",
        ["C:/old.pdf"],
        [],
        {},
    )
    finished = app.automatic_selection_finish_state(pending, ["C:/new.pdf"], [], {})[0]
    assert finished["state"] == "pending"
    assert not app.automatic_selection_is_ready(finished)


def test_generator_close_runs_outer_stream_cleanup():
    releases = []

    def body(*_values, **_kwargs):
        yield ("preparing",)
        yield ("started",)

    with (
        mock.patch.object(app, "_run_automatic_from_confirmation_stream_body", body),
        mock.patch.object(
            app,
            "release_automatic_anythingllm_mutation_lease",
            side_effect=lambda owner=None: releases.append(owner),
        ),
    ):
        stream = app.run_automatic_from_confirmation_stream()
        assert next(stream) == ("preparing",)
        stream.close()
    assert len(releases) == 1
    assert releases[0].startswith("automatic-run-")


def test_unowned_stream_cleanup_cannot_release_another_run_lease():
    original_handle = app.AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_HANDLE
    original_owner = app.AUTOMATIC_ANYTHINGLLM_MUTATION_OWNER
    try:
        app.AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_HANDLE = object()
        app.AUTOMATIC_ANYTHINGLLM_MUTATION_OWNER = "existing-run-owner"

        def body(*_values, **_kwargs):
            yield ("duplicate-confirm-noop",)

        with mock.patch.object(app, "_run_automatic_from_confirmation_stream_body", body):
            stream = app.run_automatic_from_confirmation_stream()
            next(stream)
            stream.close()
        assert app.AUTOMATIC_ANYTHINGLLM_MUTATION_OWNER == "existing-run-owner"
        assert app.AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_HANDLE is not None
    finally:
        app.AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_HANDLE = original_handle
        app.AUTOMATIC_ANYTHINGLLM_MUTATION_OWNER = original_owner


def test_unexpected_owned_running_stream_exit_cannot_leave_live_status_stranded():
    original_status = app.LIVE_AUTOMATIC_RUN_STATUS

    def body(*_values, _confirmation_owner_token="", **_kwargs):
        app.update_live_automatic_run_status(
            state="running",
            phase="Preparing PDF",
            confirmation_owner_token=_confirmation_owner_token,
        )
        raise RuntimeError("simulated stream failure")
        yield  # pragma: no cover - keeps this a generator

    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        with mock.patch.object(app, "_run_automatic_from_confirmation_stream_body", body):
            with pytest.raises(RuntimeError, match="simulated stream failure"):
                next(app.run_automatic_from_confirmation_stream())
        assert app.LIVE_AUTOMATIC_RUN_STATUS["state"] == "failed"
        assert app.LIVE_AUTOMATIC_RUN_STATUS["phase"] == "Run ended before a terminal result"
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status


def test_pymupdf_page_worker_reports_activity_while_native_call_is_slow(tmp_path):
    activity_path = tmp_path / "activity.json"
    observed = {}

    def slow_page(*_args):
        time.sleep(0.08)
        return {"page": 1, "text": "done", "kind": "markdown_page"}

    def run():
        observed["result"] = rag_pdf_tools._pymupdf4llm_one_page_observed(
            "source.pdf", 0, 200, str(activity_path)
        )

    with (
        mock.patch.object(rag_pdf_tools, "PYMUPDF4LLM_WORKER_HEARTBEAT_SECONDS", 0.01),
        mock.patch.object(rag_pdf_tools, "_pymupdf4llm_one_page", side_effect=slow_page),
    ):
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 1
        activity = {}
        while time.monotonic() < deadline:
            try:
                activity = json.loads(activity_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            if activity.get("phase") == "extracting_page_with_pymupdf4llm":
                break
            time.sleep(0.005)
        worker.join(timeout=1)
    assert not worker.is_alive()
    assert activity["page"] == 1
    assert activity["phase"] == "extracting_page_with_pymupdf4llm"
    assert observed["result"]["text"] == "done"


def test_unresponsive_pymupdf_pool_never_retries_the_same_native_call_in_parent(tmp_path):
    pdf = tmp_path / "two-pages.pdf"
    document = fitz.open()
    try:
        document.new_page()
        document.new_page()
        document.save(pdf)
    finally:
        document.close()
    backend = mock.Mock()
    with (
        mock.patch.object(rag_pdf_tools, "import_optional_backend", return_value=backend),
        mock.patch.object(
            rag_pdf_tools,
            "ensure_tesseract_runtime",
            return_value={"available": True},
        ),
        mock.patch.object(rag_pdf_tools, "pymupdf4llm_ocr_page_workers", return_value=2),
        mock.patch.object(
            rag_pdf_tools,
            "_parallel_pymupdf4llm_pages",
            side_effect=rag_pdf_tools.Pymupdf4llmWorkerUnresponsiveError("heartbeat stale"),
        ),
    ):
        with pytest.raises(rag_pdf_tools.Pymupdf4llmWorkerUnresponsiveError):
            rag_pdf_tools.get_pages_with_pymupdf4llm(pdf)
    backend.to_markdown.assert_not_called()


def test_crashed_pymupdf_pool_never_retries_the_same_native_call_in_parent(tmp_path):
    pdf = tmp_path / "one-page.pdf"
    document = fitz.open()
    try:
        document.new_page()
        document.new_page()
        document.save(pdf)
    finally:
        document.close()
    backend = mock.Mock()
    with (
        mock.patch.object(rag_pdf_tools, "import_optional_backend", return_value=backend),
        mock.patch.object(
            rag_pdf_tools,
            "ensure_tesseract_runtime",
            return_value={"available": True},
        ),
        mock.patch.object(rag_pdf_tools, "pymupdf4llm_ocr_page_workers", return_value=2),
        mock.patch.object(
            rag_pdf_tools,
            "_parallel_pymupdf4llm_pages",
            side_effect=rag_pdf_tools.Pymupdf4llmWorkerIsolationError("child exited"),
        ),
    ):
        with pytest.raises(rag_pdf_tools.Pymupdf4llmWorkerIsolationError):
            rag_pdf_tools.get_pages_with_pymupdf4llm(pdf)
    backend.to_markdown.assert_not_called()


def test_run_recorder_event_log_is_compact_and_checkpoint_stays_complete(tmp_path):
    result = RunResult("run-compact", str(tmp_path), "page_limit", policy_for("page_limit").to_dict())
    result.verification_evidence = {"large": "x" * 100_000}
    recorder = RunRecorder(result)
    recorder.finish("pass", "complete")
    checkpoint_payload = json.loads((tmp_path / "run-checkpoint.json").read_text(encoding="utf-8"))
    events = (tmp_path / "run-checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
    assert checkpoint_payload["verification_evidence"]["large"] == "x" * 100_000
    assert sum(len(line) for line in events) < 2_000


def test_worker_crash_retains_exit_stage_and_stderr_evidence(tmp_path):
    run_root = tmp_path / "run"
    output = run_root / "source"
    process = mock.Mock(pid=8811, returncode=23)
    process.poll.return_value = 23

    def crashed_worker(*_args, **kwargs):
        kwargs["stderr"].write("native backend terminated unexpectedly\n")
        kwargs["stderr"].flush()
        return process

    with (
        mock.patch.object(app.subprocess, "Popen", side_effect=crashed_worker),
        mock.patch.object(app, "windows_process_creation_token", return_value="created-8811"),
    ):
        result = app.execute_automatic_preparation_in_worker(
            "source.pdf",
            output,
            SimpleNamespace(),
            run_root,
            lambda *_args, **_kwargs: None,
        )

    assert result["status"] == "failed"
    assert result["worker_exit_code"] == 23
    assert result["worker_last_stage"] == ""
    assert result["worker_stderr_path"].endswith(".automatic-worker-stderr.log")
    assert "native backend terminated unexpectedly" in result["worker_stderr_tail"]


@pytest.mark.parametrize(
    ("segment_mode", "groups", "representation"),
    [
        ("page_limit", (), "segments"),
        ("page_passages", (), "segments"),
        ("custom_page_ranges", (2, 3), "page_parents"),
    ],
)
def test_run_request_preserves_all_live_segmentation_intent(segment_mode, groups, representation):
    request = RunRequest.from_automatic_settings(
        {
            "files": ["C:/source.pdf"],
            "backend_mode": "Automatic",
            "custom_page_group_sizes": groups,
        },
        mode=LOCAL_ONLY,
        segment_mode=segment_mode,
        native_upload_representation=representation,
    )
    assert request.segment_mode == segment_mode
    assert request.custom_page_group_sizes == groups
    assert request.native_upload_representation == representation
    assert tuple(request.to_legacy_namespace().custom_page_group_sizes) == groups
